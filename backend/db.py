from __future__ import annotations

import asyncpg
from typing import Optional

from .config import get_settings

_pool: Optional[asyncpg.Pool] = None


async def init_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        settings = get_settings()
        _pool = await asyncpg.create_pool(
            dsn=settings.database_url,
            min_size=1,
            max_size=10,
        )
        async with _pool.acquire() as conn:
            await _ensure_schema(conn)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialized")
    return _pool


SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS repos (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner           TEXT NOT NULL,
    name            TEXT NOT NULL,
    default_branch  TEXT NOT NULL,
    visibility      TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'github',
    label           TEXT,
    last_indexed_sha TEXT,
    last_synced_at  TIMESTAMPTZ,
    github_token_ref TEXT,
    gemini_token_ref TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner, name)
);

ALTER TABLE repos ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'github';
ALTER TABLE repos ADD COLUMN IF NOT EXISTS label TEXT;
ALTER TABLE repos ADD COLUMN IF NOT EXISTS gemini_token_ref TEXT;

CREATE TABLE IF NOT EXISTS code_chunks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repo_id         UUID NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    file            TEXT NOT NULL,
    symbol          TEXT,
    kind            TEXT NOT NULL,
    language        TEXT,
    content         TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    start_line      INTEGER,
    end_line        INTEGER,
    commit_sha      TEXT,
    indexed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    embedding       vector(768),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (repo_id, file, symbol, start_line, end_line, content_hash)
);

ALTER TABLE code_chunks ADD COLUMN IF NOT EXISTS commit_sha TEXT;
ALTER TABLE code_chunks ADD COLUMN IF NOT EXISTS indexed_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE INDEX IF NOT EXISTS code_chunks_repo_file_idx ON code_chunks (repo_id, file);
CREATE INDEX IF NOT EXISTS code_chunks_commit_idx ON code_chunks (repo_id, commit_sha);
CREATE INDEX IF NOT EXISTS code_chunks_embedding_idx
    ON code_chunks USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS feature_skills (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repo_id         UUID NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    pr_number       INTEGER NOT NULL,
    title           TEXT NOT NULL,
    description     TEXT,
    summary         TEXT NOT NULL,
    changed_files   JSONB NOT NULL DEFAULT '[]'::jsonb,
    author          TEXT,
    merged_at       TIMESTAMPTZ,
    merge_commit_sha TEXT,
    embedding       vector(768),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (repo_id, pr_number)
);

CREATE INDEX IF NOT EXISTS feature_skills_repo_idx ON feature_skills (repo_id, merged_at DESC);
CREATE INDEX IF NOT EXISTS feature_skills_embedding_idx
    ON feature_skills USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS commit_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repo_id         UUID NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    commit_sha      TEXT NOT NULL,
    message         TEXT NOT NULL,
    author          TEXT NOT NULL,
    author_email    TEXT NOT NULL,
    committed_at    TIMESTAMPTZ NOT NULL,
    parents         TEXT[] NOT NULL DEFAULT '{}',
    changed_files   JSONB NOT NULL DEFAULT '[]',
    commit_summary  TEXT NOT NULL,
    embedding       vector(768),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(repo_id, commit_sha)
);

CREATE INDEX IF NOT EXISTS commit_log_repo_idx ON commit_log (repo_id);
CREATE INDEX IF NOT EXISTS commit_log_sha_idx ON commit_log (repo_id, commit_sha);
CREATE INDEX IF NOT EXISTS commit_log_committed_at_idx ON commit_log (repo_id, committed_at DESC);
CREATE INDEX IF NOT EXISTS commit_log_embedding_idx
    ON commit_log USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS chat_turns (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chat_id     TEXT NOT NULL,
    repo_id     UUID NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT NOT NULL,
    turn_index  INTEGER NOT NULL DEFAULT 0,
    embedding   vector(768),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE chat_turns ADD COLUMN IF NOT EXISTS turn_index INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS chat_turns_chat_idx ON chat_turns (chat_id, created_at DESC);
CREATE INDEX IF NOT EXISTS chat_turns_chat_turn_idx ON chat_turns (chat_id, turn_index);
CREATE INDEX IF NOT EXISTS chat_turns_embedding_idx
    ON chat_turns USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS sync_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repo_id         UUID NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at        TIMESTAMPTZ,
    status          TEXT NOT NULL,
    kind            TEXT NOT NULL,
    files_scanned   INTEGER DEFAULT 0,
    chunks_upserted INTEGER DEFAULT 0,
    prs_ingested    INTEGER DEFAULT 0,
    error           TEXT
);

CREATE INDEX IF NOT EXISTS sync_runs_repo_idx ON sync_runs (repo_id, started_at DESC);

CREATE TABLE IF NOT EXISTS organizations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL UNIQUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS purna_tokens (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    token_hash      TEXT NOT NULL UNIQUE,
    label           TEXT,
    revoked_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS workspaces (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    source_kind     TEXT NOT NULL DEFAULT 'code_repo',
    config          JSONB NOT NULL DEFAULT '{}'::jsonb,
    knowledge_path  TEXT NOT NULL,
    repo_id         UUID REFERENCES repos(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (org_id, name)
);

CREATE TABLE IF NOT EXISTS knowledge_decisions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL,
    action          TEXT NOT NULL,
    reason          TEXT NOT NULL,
    commit_sha      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS knowledge_decisions_workspace_idx ON knowledge_decisions (workspace_id, created_at DESC);
"""


async def seed_fake_token(conn: asyncpg.Connection) -> None:
    import hashlib
    # 1. Ensure "POC Demo Org" exists
    org_id = await conn.fetchval(
        "INSERT INTO organizations (name) VALUES ($1) ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id",
        "POC Demo Org"
    )
    
    # 2. Hash "purna_test_demo"
    token_plaintext = "purna_test_demo"
    token_hash = hashlib.sha256(token_plaintext.encode("utf-8")).hexdigest()
    
    # 3. Ensure token exists
    await conn.execute(
        "INSERT INTO purna_tokens (org_id, token_hash, label) VALUES ($1, $2, $3) ON CONFLICT (token_hash) DO NOTHING",
        org_id,
        token_hash,
        "POC Demo Token"
    )


async def _ensure_schema(conn: asyncpg.Connection) -> None:
    await conn.execute(SCHEMA_SQL)
    await seed_fake_token(conn)
