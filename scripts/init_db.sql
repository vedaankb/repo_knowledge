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
