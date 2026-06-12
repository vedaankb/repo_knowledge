"""Knowledge repository importer - imports pre-processed artifacts into pgvector"""

import asyncio
import json
from datetime import datetime, timezone
from typing import Optional, Union
from uuid import UUID
import httpx

from .db import pool
from .github_client import _auth_headers, _fetch_json
from .config import get_settings


async def fetch_manifest(owner: str, name: str, branch: str = "main", token: Optional[str] = None) -> Optional[dict]:
    """Fetch manifest.json from knowledge repository"""
    url = f"https://api.github.com/repos/{owner}/{name}/contents/manifest.json"
    try:
        data = await _fetch_json(url, token=token, params={"ref": branch})
        if data and "content" in data:
            import base64
            content = base64.b64decode(data["content"]).decode("utf-8")
            return json.loads(content)
    except Exception as e:
        print(f"Error fetching manifest: {e}")
    return None


async def fetch_artifact(
    owner: str, 
    name: str, 
    path: str, 
    branch: str = "main",
    token: Optional[str] = None,
) -> Optional[dict]:
    """Fetch a JSON artifact from knowledge repository"""
    url = f"https://api.github.com/repos/{owner}/{name}/contents/{path}"
    try:
        data = await _fetch_json(url, token=token, params={"ref": branch})
        if data and "content" in data:
            import base64
            content = base64.b64decode(data["content"]).decode("utf-8")
            return json.loads(content)
    except Exception as e:
        print(f"Error fetching {path}: {e}")
    return None


async def list_directory(
    owner: str,
    name: str,
    path: str,
    branch: str = "main",
    token: Optional[str] = None,
) -> list[dict]:
    """List files in a directory in knowledge repository"""
    url = f"https://api.github.com/repos/{owner}/{name}/contents/{path}"
    try:
        data = await _fetch_json(url, token=token, params={"ref": branch})
        if isinstance(data, list):
            return data
    except Exception as e:
        print(f"Error listing {path}: {e}")
    return []


def _parse_committed_at(value: Union[str, datetime]) -> datetime:
    """Parse ISO timestamps from CLI/git artifacts for asyncpg TIMESTAMPTZ."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def import_commit_artifact(repo_id: UUID, commit_data: dict):
    """Import a single commit artifact into commit_log table"""
    from .embeddings import embed_texts
    
    # Embed the commit summary for semantic search
    summary = commit_data.get("commit_summary", "")
    if not summary:
        # Generate summary if not provided
        msg = commit_data["message"]
        files_count = len(commit_data.get("changed_files", []))
        summary = f"{msg[:200]}. Modified {files_count} files."
    
    # Get embedding
    embeddings = await embed_texts([summary])
    embedding = embeddings[0] if embeddings else None
    
    if not embedding:
        return
    
    # Format embedding as pgvector literal
    embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
    
    # Parse changed_files to JSON if it's a list of dicts
    changed_files_json = json.dumps(commit_data.get("changed_files", []))
    
    sql = """
        INSERT INTO commit_log (
            repo_id, commit_sha, message, author, author_email,
            committed_at, parents, changed_files, commit_summary, embedding
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10::vector)
        ON CONFLICT (repo_id, commit_sha)
        DO UPDATE SET
            message = EXCLUDED.message,
            author = EXCLUDED.author,
            author_email = EXCLUDED.author_email,
            committed_at = EXCLUDED.committed_at,
            parents = EXCLUDED.parents,
            changed_files = EXCLUDED.changed_files,
            commit_summary = EXCLUDED.commit_summary,
            embedding = EXCLUDED.embedding
    """
    
    async with pool().acquire() as conn:
        await conn.execute(
            sql,
            repo_id,
            commit_data["sha"],
            commit_data["message"],
            commit_data["author"],
            commit_data["author_email"],
            _parse_committed_at(commit_data["committed_at"]),
            commit_data.get("parents", []),
            changed_files_json,
            summary,
            embedding_str,
        )



async def import_chunks_for_commit(
    repo_id: UUID,
    commit_sha: str,
    owner: str,
    name: str,
    branch: str = "main",
    token: Optional[str] = None,
):
    """Import all chunk artifacts for a specific commit"""
    # List chunk files for this commit
    chunks_path = f"chunks/{commit_sha}"
    chunk_files = await list_directory(owner, name, chunks_path, branch, token=token)
    
    if not chunk_files:
        return 0
    
    total_imported = 0
    
    for chunk_file in chunk_files:
        if chunk_file["type"] != "file" or not chunk_file["name"].endswith(".json"):
            continue
        
        # Fetch chunk artifact
        chunk_path = f"{chunks_path}/{chunk_file['name']}"
        chunks_array = await fetch_artifact(owner, name, chunk_path, branch, token=token)
        
        if not chunks_array:
            continue
        
        # Insert chunks into code_chunks table
        for chunk in chunks_array:
            await _upsert_chunk(repo_id, commit_sha, chunk)
            total_imported += 1
    
    return total_imported


async def _upsert_chunk(repo_id: UUID, commit_sha: str, chunk: dict):
    """Insert or update a single chunk in code_chunks table"""
    sql = """
        INSERT INTO code_chunks (
            repo_id, file, symbol, kind, language, content,
            content_hash, start_line, end_line, embedding, commit_sha, indexed_at
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::vector, $11, $12)
        ON CONFLICT (repo_id, file, symbol, start_line, end_line, content_hash)
        DO UPDATE SET
            kind = EXCLUDED.kind,
            language = EXCLUDED.language,
            content = EXCLUDED.content,
            embedding = EXCLUDED.embedding,
            commit_sha = EXCLUDED.commit_sha,
            updated_at = now()
    """
    
    # Format embedding as pgvector literal
    embedding_str = "[" + ",".join(str(x) for x in chunk["embedding"]) + "]"

    indexed_at_raw = chunk.get("indexed_at")
    indexed_at = (
        _parse_committed_at(indexed_at_raw)
        if indexed_at_raw
        else datetime.now(timezone.utc)
    )

    async with pool().acquire() as conn:
        await conn.execute(
            sql,
            repo_id,
            chunk["file"],
            chunk.get("symbol", ""),
            chunk.get("kind", ""),
            chunk.get("language", ""),
            chunk["content"],
            chunk.get("content_hash", ""),
            chunk.get("start_line", 0),
            chunk.get("end_line", 0),
            embedding_str,
            commit_sha,
            indexed_at,
        )


async def import_skills_artifact(repo_id: UUID, skill_data: dict):
    """Import a single skill artifact into feature_skills table"""
    sql = """
        INSERT INTO feature_skills (
            repo_id, pr_number, title, summary, changed_files, embedding
        ) VALUES ($1, $2, $3, $4, $5, $6::vector)
        ON CONFLICT (repo_id, pr_number)
        DO UPDATE SET
            title = EXCLUDED.title,
            summary = EXCLUDED.summary,
            changed_files = EXCLUDED.changed_files,
            embedding = EXCLUDED.embedding,
            updated_at = now()
    """
    
    embedding_str = "[" + ",".join(str(x) for x in skill_data["embedding"]) + "]"
    
    async with pool().acquire() as conn:
        await conn.execute(
            sql,
            repo_id,
            skill_data["pr_number"],
            skill_data["title"],
            skill_data.get("skill_summary", ""),
            skill_data.get("changed_files", []),
            embedding_str,
        )


async def apply_deletions(repo_id: UUID, deleted_data: dict):
    """Apply file deletions from deleted artifact"""
    deleted_files = deleted_data.get("deleted_files", [])
    
    if not deleted_files:
        return 0
    
    # Delete chunks for these files
    sql = "DELETE FROM code_chunks WHERE repo_id = $1 AND file = ANY($2::text[])"
    
    async with pool().acquire() as conn:
        result = await conn.execute(sql, repo_id, deleted_files)
        # Extract number of deleted rows from result string like "DELETE 5"
        deleted_count = 0
        if result:
            parts = result.split()
            if len(parts) == 2 and parts[0] == "DELETE":
                deleted_count = int(parts[1])
        return deleted_count


async def index_from_knowledge_repo(
    repo_id: UUID,
    owner: str,
    name: str,
    last_indexed_sha: Optional[str] = None,
    branch: str = "main",
) -> dict:
    """
    Import artifacts from knowledge repository into pgvector
    
    Args:
        repo_id: Database ID of the repo record
        owner: Knowledge repo owner
        name: Knowledge repo name
        last_indexed_sha: Last commit SHA that was imported (for incremental)
        branch: Knowledge repo branch
    
    Returns:
        Statistics dict with counts of imported artifacts
    """
    stats = {
        "commits_imported": 0,
        "chunks_imported": 0,
        "skills_imported": 0,
        "files_deleted": 0,
    }
    
    from .indexer import get_repo_token
    token = await get_repo_token(repo_id)
    
    # Fetch manifest
    manifest = await fetch_manifest(owner, name, branch, token=token)
    if not manifest:
        raise ValueError(f"Could not fetch manifest from {owner}/{name}")
    
    head_sha = manifest.get("head_sha")
    
    # If we're already at this SHA, nothing to do
    if head_sha == last_indexed_sha:
        return stats
    
    # List all commits in knowledge repo
    commit_files = await list_directory(owner, name, "commits", branch, token=token)
    
    # Import each commit's artifacts
    for commit_file in commit_files:
        if commit_file["type"] != "file" or not commit_file["name"].endswith(".json"):
            continue
        
        commit_sha = commit_file["name"].replace(".json", "")
        
        # Skip if we've already processed this commit (incremental)
        if last_indexed_sha and commit_sha == last_indexed_sha:
            continue
        
        # Fetch and import commit metadata
        commit_path = f"commits/{commit_file['name']}"
        commit_data = await fetch_artifact(owner, name, commit_path, branch, token=token)
        if commit_data:
            await import_commit_artifact(repo_id, commit_data)
            stats["commits_imported"] += 1
        
        # Import chunks for this commit
        chunks_count = await import_chunks_for_commit(repo_id, commit_sha, owner, name, branch, token=token)
        stats["chunks_imported"] += chunks_count
        
        # Check for deletions
        deleted_files = await list_directory(owner, name, "deleted", branch, token=token)
        for deleted_file in deleted_files:
            if deleted_file["name"] == f"{commit_sha}.json":
                deleted_data = await fetch_artifact(owner, name, f"deleted/{deleted_file['name']}", branch, token=token)
                if deleted_data:
                    deleted_count = await apply_deletions(repo_id, deleted_data)
                    stats["files_deleted"] += deleted_count
    
    # Import skills
    skill_files = await list_directory(owner, name, "skills", branch, token=token)
    for skill_file in skill_files:
        if skill_file["type"] != "file" or not skill_file["name"].endswith(".json"):
            continue
        
        skill_data = await fetch_artifact(owner, name, f"skills/{skill_file['name']}", branch, token=token)
        if skill_data:
            await import_skills_artifact(repo_id, skill_data)
            stats["skills_imported"] += 1
    
    # Update repo record with new head_sha
    async with pool().acquire() as conn:
        await conn.execute(
            """
            UPDATE repos 
            SET last_indexed_sha = $1, last_synced_at = now()
            WHERE id = $2
            """,
            head_sha,
            repo_id,
        )
    
    return stats


async def sync_knowledge_repo(repo_id: UUID, owner: str, name: str, branch: str = "main"):
    """
    Periodic sync for a knowledge repository
    Checks for new artifacts and imports them incrementally
    """
    # Get current state
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT last_indexed_sha FROM repos WHERE id = $1",
            repo_id
        )
    
    if not row:
        raise ValueError(f"Repo {repo_id} not found")
    
    last_indexed_sha = row["last_indexed_sha"]
    
    # Import new artifacts
    stats = await index_from_knowledge_repo(repo_id, owner, name, last_indexed_sha, branch)
    
    return stats
