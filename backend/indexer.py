from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional
from uuid import UUID

from pgvector.asyncpg import register_vector

from .chunker import Chunk, chunk_file, should_skip_path
from .config import get_settings
from .db import pool
from .embeddings import embed_texts
from .github_client import GitHubClient

log = logging.getLogger(__name__)


def _vec_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{v:.7f}" for v in values) + "]"


async def _register_vector(conn) -> None:
    try:
        await register_vector(conn)
    except Exception:
        pass


async def upsert_repo(
    owner: str,
    name: str,
    default_branch: str,
    visibility: str,
    source: str = "github",
    label: Optional[str] = None,
    github_token: Optional[str] = None,
) -> UUID:
    sql = """
    INSERT INTO repos (owner, name, default_branch, visibility, source, label, github_token_ref)
    VALUES ($1, $2, $3, $4, $5, $6, $7)
    ON CONFLICT (owner, name) DO UPDATE
      SET default_branch = EXCLUDED.default_branch,
          visibility = EXCLUDED.visibility,
          source = EXCLUDED.source,
          label = COALESCE(EXCLUDED.label, repos.label),
          github_token_ref = COALESCE(EXCLUDED.github_token_ref, repos.github_token_ref),
          updated_at = now()
    RETURNING id
    """
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            sql, owner, name, default_branch, visibility, source, label, github_token,
        )
    return row["id"]


async def get_repo_token(repo_id: UUID) -> Optional[str]:
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT github_token_ref FROM repos WHERE id = $1", repo_id
        )
    return row["github_token_ref"] if row else None


async def delete_repo(repo_id: UUID) -> bool:
    async with pool().acquire() as conn:
        result = await conn.execute("DELETE FROM repos WHERE id = $1", repo_id)
    return result.endswith("1")


async def get_repo_row(repo_id: UUID) -> Optional[dict]:
    async with pool().acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM repos WHERE id = $1", repo_id)
    return dict(row) if row else None


async def get_repo_by_owner_name(owner: str, name: str) -> Optional[dict]:
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM repos WHERE owner = $1 AND name = $2", owner, name
        )
    return dict(row) if row else None


async def list_repos() -> list[dict]:
    async with pool().acquire() as conn:
        rows = await conn.fetch("SELECT * FROM repos ORDER BY created_at DESC")
    return [dict(r) for r in rows]


async def start_sync_run(repo_id: UUID, kind: str) -> UUID:
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO sync_runs (repo_id, status, kind)
               VALUES ($1, 'running', $2) RETURNING id""",
            repo_id, kind,
        )
    return row["id"]


async def finish_sync_run(
    run_id: UUID,
    status: str,
    files_scanned: int = 0,
    chunks_upserted: int = 0,
    prs_ingested: int = 0,
    error: Optional[str] = None,
) -> None:
    async with pool().acquire() as conn:
        await conn.execute(
            """UPDATE sync_runs
               SET ended_at = now(), status = $2, files_scanned = $3,
                   chunks_upserted = $4, prs_ingested = $5, error = $6
               WHERE id = $1""",
            run_id, status, files_scanned, chunks_upserted, prs_ingested, error,
        )


async def _upsert_chunks(
    repo_id: UUID,
    chunks: list[Chunk],
    embeddings: list[list[float]],
    commit_sha: Optional[str] = None,
) -> int:
    if not chunks:
        return 0
    sql = """
    INSERT INTO code_chunks
      (repo_id, file, symbol, kind, language, content, content_hash,
       start_line, end_line, commit_sha, embedding)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::vector)
    ON CONFLICT (repo_id, file, symbol, start_line, end_line, content_hash) DO UPDATE
      SET content = EXCLUDED.content,
          kind = EXCLUDED.kind,
          language = EXCLUDED.language,
          commit_sha = EXCLUDED.commit_sha,
          embedding = EXCLUDED.embedding,
          updated_at = now()
    """
    count = 0
    async with pool().acquire() as conn:
        async with conn.transaction():
            for c, emb in zip(chunks, embeddings):
                await conn.execute(
                    sql,
                    repo_id, c.file, c.symbol, c.kind, c.language,
                    c.content, c.content_hash, c.start_line, c.end_line,
                    commit_sha, _vec_literal(emb),
                )
                count += 1
    return count


async def _delete_file_chunks(repo_id: UUID, files: list[str]) -> None:
    if not files:
        return
    async with pool().acquire() as conn:
        await conn.execute(
            "DELETE FROM code_chunks WHERE repo_id = $1 AND file = ANY($2::text[])",
            repo_id, files,
        )


async def _embed_and_store(
    repo_id: UUID,
    chunks: list[Chunk],
    commit_sha: Optional[str] = None,
) -> int:
    if not chunks:
        return 0
    settings = get_settings()
    bs = settings.embed_batch_size
    total = 0
    for i in range(0, len(chunks), bs):
        batch = chunks[i:i + bs]
        embs = await embed_texts((c.content for c in batch), task_type="RETRIEVAL_DOCUMENT")
        total += await _upsert_chunks(repo_id, batch, embs, commit_sha=commit_sha)
    return total


async def index_repo_initial(repo_id: UUID, gh: GitHubClient) -> tuple[int, int, str]:
    repo = await get_repo_row(repo_id)
    assert repo is not None
    owner = repo["owner"]
    name = repo["name"]
    info = await gh.get_repo(owner, name)
    head_sha = info.head_sha

    tree = await gh.list_tree(owner, name, head_sha)
    settings = get_settings()

    files_scanned = 0
    all_chunks: list[Chunk] = []

    sem = asyncio.Semaphore(8)

    async def fetch_one(entry: dict) -> list[Chunk]:
        nonlocal files_scanned
        path = entry["path"]
        size = entry.get("size") or 0
        if size and size > settings.max_file_bytes:
            return []
        async with sem:
            raw = await gh.get_blob(owner, name, entry["sha"])
        if raw is None:
            return []
        files_scanned += 1
        return chunk_file(path, raw)

    results = await asyncio.gather(*(fetch_one(e) for e in tree), return_exceptions=True)
    for r in results:
        if isinstance(r, Exception):
            log.warning("file fetch error: %s", r)
            continue
        all_chunks.extend(r)

    chunks_upserted = await _embed_and_store(repo_id, all_chunks, commit_sha=head_sha)

    async with pool().acquire() as conn:
        await conn.execute(
            """UPDATE repos
               SET last_indexed_sha = $2, last_synced_at = now(), updated_at = now()
               WHERE id = $1""",
            repo_id, head_sha,
        )
    return files_scanned, chunks_upserted, head_sha


async def index_repo_delta(repo_id: UUID, gh: GitHubClient) -> tuple[int, int, str]:
    repo = await get_repo_row(repo_id)
    assert repo is not None
    owner = repo["owner"]
    name = repo["name"]
    last_sha = repo.get("last_indexed_sha")
    if not last_sha:
        return await index_repo_initial(repo_id, gh)

    info = await gh.get_repo(owner, name)
    head_sha = info.head_sha
    if head_sha == last_sha:
        async with pool().acquire() as conn:
            await conn.execute(
                "UPDATE repos SET last_synced_at = now() WHERE id = $1", repo_id
            )
        return 0, 0, head_sha

    diff = await gh.compare(owner, name, last_sha, head_sha)
    changed_files = diff.get("files", [])
    removed = [f["filename"] for f in changed_files if f.get("status") == "removed"]
    modified_or_added = [
        f for f in changed_files
        if f.get("status") in {"modified", "added", "renamed", "changed"}
    ]
    renamed_previous = [
        f["previous_filename"] for f in changed_files
        if f.get("status") == "renamed" and f.get("previous_filename")
    ]

    await _delete_file_chunks(repo_id, removed + renamed_previous)
    await _delete_file_chunks(repo_id, [f["filename"] for f in modified_or_added])

    files_scanned = 0
    all_chunks: list[Chunk] = []
    sem = asyncio.Semaphore(8)
    settings = get_settings()

    async def fetch_changed(f: dict) -> list[Chunk]:
        nonlocal files_scanned
        path = f["filename"]
        sha = f.get("sha")
        if not sha:
            return []
        async with sem:
            raw = await gh.get_blob(owner, name, sha)
        if raw is None:
            return []
        if len(raw) > settings.max_file_bytes:
            return []
        files_scanned += 1
        return chunk_file(path, raw)

    results = await asyncio.gather(
        *(fetch_changed(f) for f in modified_or_added), return_exceptions=True
    )
    for r in results:
        if isinstance(r, Exception):
            log.warning("delta fetch error: %s", r)
            continue
        all_chunks.extend(r)

    chunks_upserted = await _embed_and_store(repo_id, all_chunks, commit_sha=head_sha)

    async with pool().acquire() as conn:
        await conn.execute(
            """UPDATE repos
               SET last_indexed_sha = $2, last_synced_at = now(), updated_at = now()
               WHERE id = $1""",
            repo_id, head_sha,
        )
    return files_scanned, chunks_upserted, head_sha


async def _upsert_pr_skill(
    repo_id: UUID,
    pr: dict,
    changed_files: list[str],
    summary: str,
    embedding: list[float],
) -> None:
    sql = """
    INSERT INTO feature_skills
      (repo_id, pr_number, title, description, summary, changed_files,
       author, merged_at, merge_commit_sha, embedding)
    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8::timestamptz, $9, $10::vector)
    ON CONFLICT (repo_id, pr_number) DO UPDATE
      SET title = EXCLUDED.title,
          description = EXCLUDED.description,
          summary = EXCLUDED.summary,
          changed_files = EXCLUDED.changed_files,
          author = EXCLUDED.author,
          merged_at = EXCLUDED.merged_at,
          merge_commit_sha = EXCLUDED.merge_commit_sha,
          embedding = EXCLUDED.embedding,
          updated_at = now()
    """
    async with pool().acquire() as conn:
        await conn.execute(
            sql,
            repo_id,
            pr["number"],
            pr.get("title") or "",
            pr.get("body") or "",
            summary,
            json.dumps(changed_files),
            (pr.get("user") or {}).get("login"),
            pr.get("merged_at"),
            pr.get("merge_commit_sha"),
            _vec_literal(embedding),
        )


def _build_pr_summary(pr: dict, files: list[str]) -> str:
    title = (pr.get("title") or "").strip()
    body = (pr.get("body") or "").strip()
    body_short = body[:600]
    files_short = ", ".join(files[:25])
    return (
        f"PR #{pr['number']}: {title}\n"
        f"Author: {(pr.get('user') or {}).get('login')}\n"
        f"Merged: {pr.get('merged_at')}\n"
        f"Files: {files_short}\n"
        f"Description: {body_short}"
    )


async def ingest_prs(repo_id: UUID, gh: GitHubClient, since_iso: Optional[str]) -> int:
    repo = await get_repo_row(repo_id)
    assert repo is not None
    owner = repo["owner"]
    name = repo["name"]

    prs: list[dict] = []
    async for pr in gh.list_merged_prs(owner, name, since_iso=since_iso):
        prs.append(pr)

    if not prs:
        return 0

    summaries: list[str] = []
    file_lists: list[list[str]] = []
    for pr in prs:
        try:
            files = await gh.list_pr_files(owner, name, pr["number"])
        except Exception as e:
            log.warning("list_pr_files failed for #%s: %s", pr.get("number"), e)
            files = []
        filenames = [f["filename"] for f in files]
        file_lists.append(filenames)
        summaries.append(_build_pr_summary(pr, filenames))

    embeddings = await embed_texts(summaries, task_type="RETRIEVAL_DOCUMENT")

    for pr, files, summary, emb in zip(prs, file_lists, summaries, embeddings):
        await _upsert_pr_skill(repo_id, pr, files, summary, emb)
    return len(prs)


async def index_local_directory(repo_id: UUID, root: Path) -> tuple[int, int]:
    """Walk a directory, chunk every relevant file, embed, and store.

    Used for uploaded zips. Replaces all existing code_chunks for this repo so
    re-uploads are idempotent.
    """
    settings = get_settings()
    root = root.resolve()

    async with pool().acquire() as conn:
        await conn.execute("DELETE FROM code_chunks WHERE repo_id = $1", repo_id)

    files_scanned = 0
    all_chunks: list[Chunk] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _skip_local_dir(d)]
        for fname in filenames:
            abs_path = Path(dirpath) / fname
            try:
                rel = abs_path.relative_to(root).as_posix()
            except ValueError:
                continue
            if should_skip_path(rel):
                continue
            try:
                size = abs_path.stat().st_size
            except OSError:
                continue
            if size == 0 or size > settings.max_file_bytes:
                continue
            try:
                raw = abs_path.read_bytes()
            except Exception:
                continue
            files_scanned += 1
            all_chunks.extend(chunk_file(rel, raw))

    chunks_upserted = await _embed_and_store(repo_id, all_chunks)

    async with pool().acquire() as conn:
        await conn.execute(
            """UPDATE repos
               SET last_synced_at = now(), updated_at = now()
               WHERE id = $1""",
            repo_id,
        )
    return files_scanned, chunks_upserted


def _skip_local_dir(name: str) -> bool:
    return name in {
        "node_modules", ".git", "dist", "build", "out", "target",
        ".next", ".nuxt", ".venv", "venv", "__pycache__", ".pytest_cache",
        ".mypy_cache", ".ruff_cache", "vendor", ".idea", ".vscode",
        "coverage", ".turbo",
    }
