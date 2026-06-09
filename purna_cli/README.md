# PurnaOS CLI

Client-side tool for building knowledge repositories from your source code.

## What is PurnaOS?

PurnaOS shifts code indexing from server-side to client-side. Instead of sending your GitHub URL to a server, you:

1. Run `purna` locally in your repository
2. It chunks your code, embeds it with Gemini, and creates artifacts
3. Artifacts are pushed to a dedicated "knowledge repository" on GitHub
4. The chat backend imports these pre-processed artifacts

**Benefits:**
- Your source code never leaves your machine
- Embeddings are computed once, not on every server sync
- Works with private repos without sharing credentials
- Git-based distribution with full version history

---

## Installation

### Prerequisites
- Python 3.10+
- Git
- Gemini API key ([Get one here](https://aistudio.google.com/apikey))
- GitHub Personal Access Token with `repo` scope

### Install

```bash
# From the repo root
cd repo_knowledge
pip install -e .

# Verify installation
purna --help
```

---

## Quick Start

### 1. Initialize in Your Repository

```bash
cd /path/to/your/project
purna init
```

This creates `.purnaOS/config.yaml`. Edit it to configure your knowledge repository:

```yaml
knowledge:
  github: your-org/your-project-knowledge  # Change this
  branch: main
```

### 2. Bootstrap Knowledge Repository

```bash
purna bootstrap
```

Follow the prompts to create a new GitHub repository for storing knowledge artifacts.

### 3. Install Git Hooks

```bash
purna install
```

This installs `post-commit` and `pre-push` hooks that automatically run `purna snapshot` and `purna publish`.

### 4. Set Environment Variables

```bash
export GEMINI_API_KEY=your_gemini_key_here
export GITHUB_TOKEN=your_github_token_here
```

### 5. Make Changes and Commit

```bash
# Make some code changes
echo "def hello(): print('world')" > example.py
git add example.py
git commit -m "Add example function"

# purna snapshot runs automatically via post-commit hook
# Creates chunks in .purnaOS/local/

git push

# purna publish runs automatically via pre-push hook
# Uploads artifacts to knowledge repo
```

### 6. Optional: Watch Mode

For real-time chunking as you edit:

```bash
purna watch
```

Leave this running in a terminal. It will monitor file changes and stage chunks immediately (without waiting for commits).

---

## Commands

### `purna init`
Initialize `.purnaOS` in current repository.

**Usage:**
```bash
purna init
```

### `purna bootstrap`
Create a new knowledge repository on GitHub.

**Usage:**
```bash
purna bootstrap
```

Interactive prompts guide you through:
- Repository name
- Organization (optional)
- Public vs private
- GitHub token

### `purna install`
Install git hooks for automatic snapshot and publish.

**Usage:**
```bash
purna install
```

Hooks created:
- `post-commit` - Runs `purna snapshot`
- `pre-push` - Runs `purna publish`

### `purna uninstall`
Remove purna git hooks.

**Usage:**
```bash
purna uninstall
```

### `purna snapshot`
Create a snapshot of the current commit.

**Usage:**
```bash
purna snapshot

# Or for a specific commit
purna snapshot --sha abc123

# With custom Gemini key
purna snapshot --gemini-key your_key_here
```

Creates:
- `.purnaOS/local/commits/{sha}.json` - Commit metadata
- `.purnaOS/local/chunks/{sha}/{file}.json` - Code chunks with embeddings
- `.purnaOS/local/deleted/{sha}.json` - Deleted files (if any)

### `purna publish`
Publish local artifacts to knowledge repository.

**Usage:**
```bash
purna publish

# Force re-publish
purna publish --force

# With custom GitHub token
purna publish --github-token your_token_here
```

Uploads:
- All commit artifacts
- All chunk artifacts
- Updates `manifest.json`

### `purna watch`
Watch for file changes and create draft chunks in real-time.

**Usage:**
```bash
purna watch

# With custom Gemini key
purna watch --gemini-key your_key_here
```

**How it works:**
- Monitors repository for file changes
- Waits 3 seconds (debounce) after last change
- Chunks and embeds changed files
- Saves to `.purnaOS/local/staging/` with pseudo-SHA "working"
- On commit, staged chunks are merged into the commit snapshot

**Stop:** Press `Ctrl+C`

### `purna status`
Show purna status and configuration.

**Usage:**
```bash
purna status
```

Shows:
- Repository root
- Knowledge repo URL
- Last published commit SHA
- Pending files count
- Staging directory statistics

---

## Configuration

### `.purnaOS/config.yaml`

Full configuration reference:

```yaml
# Schema version for compatibility
version: 1

# Source repository info
source:
  remote: origin           # Git remote name
  default_branch: main     # Default branch to track

# Knowledge repository
knowledge:
  github: org/repo-name    # GitHub repo for artifacts
  branch: main             # Branch to publish to

# Sync triggers and debouncing
sync:
  debounce_ms: 3000        # Wait time after file edit (ms)
  on_edit: true            # Enable watch mode
  on_commit: true          # Enable post-commit hook
  on_push: true            # Enable pre-push hook

# Chunking configuration
chunk:
  reuse_backend_rules: true # Use same rules as backend
  max_file_bytes: 400000    # Skip files larger than this

# Embedding configuration
embed:
  model: gemini-embedding-001  # Gemini model
  dimensions: 768               # Output dimensions
  task_type: RETRIEVAL_DOCUMENT
```

### `.purnaOS/local/state.json`

Auto-managed state file (do not edit):

```json
{
  "schema_version": 1,
  "last_published_sha": "abc123...",
  "last_published_at": "2026-06-05T03:32:00Z",
  "pending_files": ["backend/main.py"],
  "staging_count": 42
}
```

---

## Workflow Examples

### Daily Development Flow

```bash
# Morning: Start watch daemon
purna watch &

# Write code...
vim backend/api.py

# Watch daemon chunks files as you save

# Ready to commit
git add backend/api.py
git commit -m "Add new API endpoint"

# post-commit hook runs: purna snapshot

# Push to remote
git push

# pre-push hook runs: purna publish
```

### One-Time Snapshot

```bash
# Chunk and embed current HEAD
purna snapshot

# Publish artifacts
purna publish

# Backend can now import
```

### Bulk Historical Processing

```bash
# Process all commits in history
for sha in $(git log --format=%H); do
  purna snapshot --sha $sha
done

# Publish everything
purna publish --force
```

---

## Integration with Chat Backend

Once artifacts are published:

1. **Register knowledge repo:**
   ```bash
   curl -X POST http://localhost:8000/api/repos/knowledge \
     -H "Content-Type: application/json" \
     -H "X-Gemini-Key: your_gemini_key" \
     -d '{
       "url": "https://github.com/org/your-project-knowledge",
       "token": "ghp_...",
       "branch": "main"
     }'
   ```

2. **Backend imports artifacts:**
   - Downloads from knowledge repo
   - Imports into pgvector
   - Ready for chat

3. **Ask questions:**
   - Open chat UI
   - Select the repository
   - Ask about your code

---

## Troubleshooting

### "Gemini API key not set"

```bash
export GEMINI_API_KEY=your_key_here
# Or pass --gemini-key to each command
```

### "GitHub token required"

```bash
export GITHUB_TOKEN=your_token_here
# Or pass --github-token to publish command
```

### Hooks not running

```bash
# Check if hooks are installed
ls .git/hooks/post-commit
ls .git/hooks/pre-push

# Reinstall if missing
purna install

# Make sure they're executable
chmod +x .git/hooks/post-commit
chmod +x .git/hooks/pre-push
```

### Watch daemon not processing files

- Check debounce time (default 3 seconds)
- Ensure files aren't in `.purnaOS/` directory
- Check file size (default max 400 KB)
- Verify file isn't binary or in skip list

### Publish failing with rate limits

GitHub Contents API has rate limits. Solutions:
- Wait a few minutes and retry
- Use smaller commits
- Future: Switch to Git LFS (Phase 4)

---

## Advanced Topics

### Custom Skip Rules

Edit `.purnaOS/config.yaml`:

```yaml
chunk:
  include:
    - "**/*.py"
    - "**/*.js"
  exclude:
    - "**/node_modules/**"
    - "**/test/**"
```

### Branch-Per-Developer

For teams, each developer can publish to their own branch:

```yaml
knowledge:
  github: org/myapp-knowledge
  branch: dev/alice  # or dev/bob, dev/carol
```

Use a GitHub Action to merge dev branches into main.

### Pre-Commit Validation

Add a pre-commit hook that runs purna tests:

```bash
#!/bin/bash
# .git/hooks/pre-commit

# Ensure purna config is valid
purna status > /dev/null || exit 1
```

---

## FAQ

**Q: Does this work with monorepos?**
A: Yes! Initialize `.purnaOS` at the root. All code is chunked and published together.

**Q: Can I use this with private GitHub repos?**
A: Yes, both source and knowledge repos can be private. Just use a token with `repo` scope.

**Q: What happens to my existing indexed repos?**
A: They continue to work. PurnaOS adds a new `source=purna_knowledge` type but doesn't break `source=github` repos.

**Q: Do I need to run watch constantly?**
A: No! Hooks are enough. Watch is optional for real-time feedback during active development.

**Q: Can I delete .purnaOS/local/ ?**
A: Yes, it's a cache. You can recreate snapshots with `purna snapshot`.

**Q: How do I migrate an existing repo?**
A: No migration tool yet. Start fresh: `purna init`, `purna snapshot`, `purna publish`.

---

## License

Same as parent project (see root LICENSE file).

## Contributing

Issues and PRs welcome! See CONTRIBUTING.md (if it exists).

---

## Links

- [Full Documentation](../docs/purnaos-implementation-summary.md)
- [Schema Specification](../docs/purnaos-schema.md)
- [Phase 4 Scaling Strategy](../docs/phase4-scaling.md)
- [Main Repository](https://github.com/your-org/repo-knowledge)
