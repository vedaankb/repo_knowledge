import hashlib
import json
import logging
from typing import Optional
from uuid import UUID
from fastapi import HTTPException
import asyncpg

from .db import pool

log = logging.getLogger(__name__)


def parse_workspace_config(raw) -> dict:
    """asyncpg returns JSONB columns as JSON strings — normalize to dict."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"understanding": raw}
        return parsed if isinstance(parsed, dict) else {"understanding": str(parsed)}
    return {}


async def validate_token(token: str) -> UUID:
    """
    Validate a PurnaOS token and return its organization ID.
    Raises HTTPException if invalid.
    """
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT org_id FROM purna_tokens WHERE token_hash = $1 AND revoked_at IS NULL",
            token_hash
        )
    if not row:
        raise HTTPException(status_code=401, detail="Invalid or revoked PurnaOS token")
    return row["org_id"]


async def provision_workspace(
    org_id: UUID,
    repo_owner: str,
    repo_name: str,
    gemini_key: Optional[str] = None,
    probe_data: Optional[dict] = None,
) -> tuple[UUID, UUID]:
    """
    Provision a workspace and its linked repos row.
    Returns (workspace_id, repo_id).
    """
    probe_data = probe_data or {}
    default_branch = probe_data.get("default_branch", "main")
    visibility = probe_data.get("visibility", "private")
    
    async with pool().acquire() as conn:
        async with conn.transaction():
            # 1. Create or get linked repos row
            # Note: repos has UNIQUE (owner, name)
            repo_row = await conn.fetchrow(
                "SELECT id FROM repos WHERE owner = $1 AND name = $2",
                repo_owner,
                repo_name
            )
            if repo_row:
                repo_id = repo_row["id"]
                # Update source and gemini key
                await conn.execute(
                    "UPDATE repos SET source = 'purna_workspace', gemini_token_ref = COALESCE($1, gemini_token_ref) WHERE id = $2",
                    gemini_key,
                    repo_id
                )
            else:
                repo_id = await conn.fetchval(
                    """
                    INSERT INTO repos (owner, name, default_branch, visibility, source, gemini_token_ref)
                    VALUES ($1, $2, $3, $4, 'purna_workspace', $5)
                    RETURNING id
                    """,
                    repo_owner,
                    repo_name,
                    default_branch,
                    visibility,
                    gemini_key
                )
            
            # 2. Create or get workspace
            ws_row = await conn.fetchrow(
                "SELECT id FROM workspaces WHERE org_id = $1 AND name = $2",
                org_id,
                repo_name
            )
            if ws_row:
                workspace_id = ws_row["id"]
                # Update linked repo_id
                await conn.execute(
                    "UPDATE workspaces SET repo_id = $1 WHERE id = $2",
                    repo_id,
                    workspace_id
                )
            else:
                workspace_id = await conn.fetchval(
                    """
                    INSERT INTO workspaces (org_id, name, source_kind, knowledge_path, repo_id)
                    VALUES ($1, $2, 'code_repo', $3, $4)
                    RETURNING id
                    """,
                    org_id,
                    repo_name,
                    f"data/knowledge/{org_id}/{repo_name}",
                    repo_id
                )
                
            return workspace_id, repo_id


async def log_decision(
    workspace_id: UUID,
    event_type: str,
    action: str,
    reason: str,
    commit_sha: Optional[str] = None,
) -> UUID:
    """Log an LLM sync agent decision to the database"""
    async with pool().acquire() as conn:
        decision_id = await conn.fetchval(
            """
            INSERT INTO knowledge_decisions (workspace_id, event_type, action, reason, commit_sha)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
            """,
            workspace_id,
            event_type,
            action,
            reason,
            commit_sha
        )
    return decision_id


async def get_recent_decisions(workspace_id: UUID, limit: int = 10) -> list[dict]:
    """Get recent decisions for a workspace"""
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, event_type, action, reason, commit_sha, created_at
            FROM knowledge_decisions
            WHERE workspace_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            workspace_id,
            limit
        )
    return [dict(r) for r in rows]
