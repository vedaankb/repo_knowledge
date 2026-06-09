# PurnaOS Schema Specification

## Overview

This document defines the schema for:
1. `.purnaOS/` directory structure in client source repositories
2. Knowledge repo artifact format (JSON manifests, chunks, commits, skills)

---

## 1. `.purnaOS/` Directory Structure

The `.purnaOS` directory lives in the **client source repository** (similar to `.git`):

```
.purnaOS/
  config.yaml          # Configuration file
  hooks/               # Git hook templates (installed by purna install)
  .gitignore          # Ignores local/ directory
  local/              # NOT committed — local staging and state
    staging/          # Draft chunks from watch daemon
    state.json        # Persistent CLI state
```

---

## 2. `.purnaOS/config.yaml` Schema

```yaml
# Schema version for compatibility checks
version: 1

# Source repository information
source:
  # Git remote name (default: origin)
  remote: origin
  # Default branch to track (default: main)
  default_branch: main

# Knowledge repository configuration
knowledge:
  # GitHub repository in format "owner/repo-name"
  # This is where chunked artifacts are published
  github: org/myapp-knowledge
  # Branch to publish to (default: main)
  branch: main

# Synchronization triggers and debouncing
sync:
  # Milliseconds to wait after file edits before processing (default: 3000)
  debounce_ms: 3000
  # Enable processing on file edits (default: true)
  on_edit: true
  # Enable snapshot creation on commits (default: true)
  on_commit: true
  # Enable publishing on git push (default: true)
  on_push: true

# Chunking configuration
chunk:
  # Reuse backend chunker.py rules (default: true)
  reuse_backend_rules: true
  # Optional: custom include patterns (glob)
  include: []
  # Optional: custom exclude patterns (glob)
  exclude: []
  # Max file size in bytes (default: 400000)
  max_file_bytes: 400000

# Embedding configuration
embed:
  # Embedding model identifier (default: gemini-embedding-001)
  model: gemini-embedding-001
  # Output dimensions (default: 768)
  dimensions: 768
  # Task type for Gemini API (default: RETRIEVAL_DOCUMENT)
  task_type: RETRIEVAL_DOCUMENT
```

---

## 3. `.purnaOS/local/state.json` Schema

**NOT committed to git** — tracks local CLI state:

```json
{
  "schema_version": 1,
  "last_published_sha": "abc123def456...",
  "last_published_at": "2026-06-05T03:32:00Z",
  "pending_files": [
    "backend/main.py",
    "frontend/app.js"
  ],
  "staging_count": 42
}
```

| Field | Type | Description |
|-------|------|-------------|
| `schema_version` | integer | State file format version |
| `last_published_sha` | string | Last commit SHA that was published to knowledge repo |
| `last_published_at` | ISO 8601 string | Timestamp of last successful publish |
| `pending_files` | array[string] | Files with unpublished edits in staging/ |
| `staging_count` | integer | Number of chunk files in staging/ |

---

## 4. Knowledge Repository Structure

The **knowledge repository** is a dedicated GitHub repository that stores processed artifacts:

```
knowledge-repo/
  manifest.json              # Root manifest
  commits/                   # Git commit history
    {sha}.json
  chunks/                    # Code chunks with embeddings
    {sha}/
      {file_path_hash}.json
  skills/                    # PR-based feature skills
    pr-{number}.json
  deleted/                   # Deleted files per commit
    {sha}.json
  README.md                  # Human-readable documentation
```

---

## 5. `manifest.json` Schema

Root manifest tracking the knowledge repo state:

```json
{
  "schema_version": 1,
  "source_repo": "https://github.com/org/myapp",
  "source_owner": "org",
  "source_name": "myapp",
  "head_sha": "abc123def456789...",
  "default_branch": "main",
  "published_at": "2026-06-05T03:32:00Z",
  "total_chunks": 1247,
  "total_commits": 89,
  "total_skills": 34,
  "files_index": {
    "backend/main.py": {
      "last_sha": "abc123...",
      "chunk_count": 12,
      "last_modified": "2026-06-05T02:10:00Z"
    }
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `schema_version` | integer | Manifest format version |
| `source_repo` | string | Full GitHub URL of source repository |
| `source_owner` | string | GitHub owner/org |
| `source_name` | string | Repository name |
| `head_sha` | string | Latest commit SHA published |
| `default_branch` | string | Source repo default branch |
| `published_at` | ISO 8601 string | Timestamp of last publish |
| `total_chunks` | integer | Total chunk count across all commits |
| `total_commits` | integer | Number of commits with artifacts |
| `total_skills` | integer | Number of PR skills |
| `files_index` | object | Per-file metadata for incremental updates |

---

## 6. `commits/{sha}.json` Schema

Git commit metadata for provenance and history Q&A:

```json
{
  "sha": "abc123def456789...",
  "message": "Fix authentication bug in login flow",
  "author": "Jane Developer",
  "author_email": "jane@example.com",
  "committed_at": "2026-06-05T02:10:00Z",
  "parents": ["parent_sha1", "parent_sha2"],
  "changed_files": [
    {
      "path": "backend/auth.py",
      "status": "modified",
      "additions": 15,
      "deletions": 8
    },
    {
      "path": "backend/tests/test_auth.py",
      "status": "modified",
      "additions": 23,
      "deletions": 0
    }
  ],
  "commit_summary": "Authentication bug fix addressing race condition in token validation. Modified backend/auth.py and backend/tests/test_auth.py."
}
```

| Field | Type | Description |
|-------|------|-------------|
| `sha` | string | Full commit SHA (40 chars) |
| `message` | string | Commit message |
| `author` | string | Author display name |
| `author_email` | string | Author email |
| `committed_at` | ISO 8601 string | Commit timestamp |
| `parents` | array[string] | Parent commit SHAs |
| `changed_files` | array[object] | Files modified in this commit |
| `commit_summary` | string | Auto-generated summary for embedding/retrieval |

---

## 7. `chunks/{sha}/{file_path_hash}.json` Schema

Code chunks with embeddings for a specific file at a commit:

```json
[
  {
    "file": "backend/main.py",
    "symbol": "register_repo",
    "kind": "function_definition",
    "language": "python",
    "content": "@app.post(\"/api/repos\")\nasync def register_repo(req: RegisterRepoRequest):\n    \"\"\"Register a new repository for indexing\"\"\"\n    # ... function body ...",
    "content_hash": "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "start_line": 91,
    "end_line": 120,
    "char_count": 847,
    "embedding": [0.0123, -0.0456, 0.0789, ...]
  }
]
```

**File naming:** `{file_path_hash}.json` where hash = SHA256(file path)[:16] (first 16 chars)

**Array format:** Each file produces one JSON file containing an array of chunks.

| Field | Type | Description |
|-------|------|-------------|
| `file` | string | Relative file path from repo root |
| `symbol` | string | Function/class/method name (empty for non-symbol chunks) |
| `kind` | string | Tree-sitter node kind (e.g., `function_definition`, `class_definition`) |
| `language` | string | Programming language |
| `content` | string | Actual code content |
| `content_hash` | string | SHA256 hash of content for deduplication |
| `start_line` | integer | 1-indexed start line |
| `end_line` | integer | 1-indexed end line |
| `char_count` | integer | Character count |
| `embedding` | array[float] | 768-dimensional embedding vector |

---

## 8. `skills/pr-{number}.json` Schema

PR-based feature skills capturing team conventions:

```json
{
  "pr_number": 142,
  "title": "Add user authentication with JWT",
  "body": "Implements JWT-based authentication flow using Pydantic for request validation...",
  "author": "jane",
  "merged_at": "2026-06-01T14:30:00Z",
  "base_branch": "main",
  "head_branch": "feature/auth",
  "changed_files": [
    "backend/auth.py",
    "backend/models.py",
    "backend/middleware.py"
  ],
  "additions": 387,
  "deletions": 45,
  "skill_summary": "Team uses JWT for authentication with Pydantic validation. Auth middleware applied globally.",
  "embedding": [0.0234, -0.0567, ...]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `pr_number` | integer | Pull request number |
| `title` | string | PR title |
| `body` | string | PR description |
| `author` | string | PR author username |
| `merged_at` | ISO 8601 string | Merge timestamp |
| `base_branch` | string | Target branch |
| `head_branch` | string | Source branch |
| `changed_files` | array[string] | Files modified in PR |
| `additions` | integer | Lines added |
| `deletions` | integer | Lines deleted |
| `skill_summary` | string | Auto-generated summary for RAG |
| `embedding` | array[float] | 768-dimensional embedding |

---

## 9. `deleted/{sha}.json` Schema

Tracks files deleted in a commit for cleanup:

```json
{
  "commit_sha": "def456abc789...",
  "deleted_at": "2026-06-05T03:00:00Z",
  "deleted_files": [
    "backend/deprecated/old_auth.py",
    "frontend/legacy/old_component.js"
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `commit_sha` | string | Commit where deletion occurred |
| `deleted_at` | ISO 8601 string | Timestamp |
| `deleted_files` | array[string] | Paths of deleted files |

---

## 10. Content Hash Algorithm

Used for deduplication (skip re-embedding unchanged chunks):

```python
import hashlib

def content_hash(text: str) -> str:
    """Generate SHA256 hash with 'sha256:' prefix"""
    h = hashlib.sha256(text.encode('utf-8')).hexdigest()
    return f"sha256:{h}"
```

---

## 11. File Path Hash Algorithm

Used for chunk file naming to avoid filesystem issues with long/special paths:

```python
import hashlib

def file_path_hash(file_path: str) -> str:
    """Generate truncated SHA256 hash of file path"""
    h = hashlib.sha256(file_path.encode('utf-8')).hexdigest()
    return h[:16]  # First 16 characters
```

---

## 12. Size Guidelines

| Artifact Type | Typical Size | Notes |
|---------------|--------------|-------|
| Single chunk JSON | 3-8 KB | Embedding contributes ~6 KB |
| File chunks array | 10-100 KB | Depends on file complexity |
| Commit JSON | 1-5 KB | Scales with changed files count |
| Skill JSON | 2-10 KB | Depends on PR body length |
| Full snapshot (500 chunks) | 2-4 MB | Delta publishes are much smaller |

**For large repositories (10k+ chunks):** Plan Git LFS for chunk files or use compressed embeddings (`float16` + gzip) in future schema versions.

---

## 13. Schema Versioning

All schemas include a `schema_version` or `version` field:
- **Current version:** `1`
- **Breaking changes:** Increment major version
- **Backward-compatible additions:** Keep version `1`, add optional fields

The `purna` CLI and backend importer must validate schema versions and reject incompatible artifacts.

---

## 14. Migration Path (Future)

When schema version changes:

1. CLI checks knowledge repo `manifest.json` version
2. If outdated, prompts: `purna migrate --to-version 2`
3. Migration script transforms artifacts in place
4. Updates `manifest.schema_version`

---

## Next Steps

- Implement `purna` CLI tooling based on this schema
- Create knowledge repo template with README and `.gitignore`
- Build backend importer to consume these artifacts
- Add schema validation to both CLI and backend
