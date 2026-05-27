# repo-knowledge POC

A chatbot that turns any GitHub repository into a strictly grounded RAG knowledge base.

- Indexes code symbols via tree-sitter
- Indexes merged PRs as "feature skills" (how the team builds things)
- Embeds with **Gemini `text-embedding-004`**, stores in **Postgres + pgvector**
- Answers strictly from indexed context — never from general knowledge or the internet
- Auto delta-sync every 12h (and on demand)

## Architecture

```
[GitHub REST API] --> [Indexer] --+--> code_chunks (pgvector)
                                  +--> feature_skills (pgvector)
                                  
[User Q] -> [Embed] -> [pgvector top-k] -> [Gemini chat w/ strict prompt] -> answer + citations
```

## Quick start

### 1. Start Postgres + pgvector

```bash
docker compose up -d
```

### 2. Set up Python env

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure secrets

```bash
cp .env.example .env
# edit .env and set GEMINI_API_KEY (and GITHUB_TOKEN for private repos / higher rate limits)
```

### 4. Run the server

```bash
uvicorn backend.main:app --reload --port 8000
```

Open http://localhost:8000.

## How to use

1. Paste a GitHub repo URL (public or private) and click **Add repo**.
2. The initial index runs in the background. Watch the **Status** panel.
3. Once chunks/feature skills are populated, ask questions in the chat.
4. The bot will answer strictly from indexed content and cite `file:symbol L#-L#` or `PR #N`. If context is insufficient, it refuses.
5. The 12h scheduler runs delta syncs automatically; click **Sync now** to force one.

## Strict RAG policy

The bot's system prompt forbids using outside knowledge. If retrieval returns nothing above the score threshold, it short-circuits with a refusal message before calling the LLM.

Threshold and top-k are tunable via `.env`:

- `TOP_K_CODE` (default 10)
- `TOP_K_SKILLS` (default 5)
- `MIN_RETRIEVAL_SCORE` (default 0.30, cosine similarity)

## Schema

- `repos(owner, name, default_branch, last_indexed_sha, last_synced_at, ...)`
- `code_chunks(repo_id, file, symbol, kind, language, content, embedding vector(768), ...)`
- `feature_skills(repo_id, pr_number, title, summary, changed_files, embedding vector(768), ...)`
- `sync_runs(...)` — per-run audit log

## Notes / limitations (POC)

- Files larger than `MAX_FILE_BYTES` (default 400KB) and binaries are skipped.
- Tree-sitter is used for the languages listed in `backend/chunker.py`; other text files fall back to sliding-window chunking.
- Private repos require `GITHUB_TOKEN` with `repo` scope (or a GitHub App token).
- Embedding dimension is fixed to 768 (Gemini `text-embedding-004`).
