# PurnaOS Repository Knowledge Platform (POC)

A client-driven, edit-triggered repository knowledge platform that turns any codebase into a strictly grounded, temporally aware RAG knowledge base.

Unlike traditional server-side indexing that requires git commits or webhook configuration, PurnaOS runs a local CLI daemon that monitors file saves, computes cumulative diffs, and uses an LLM Sync Agent as a gatekeeper to decide when changes are substantial enough to append to the knowledge base.

---

## Key Features

- **Client-Driven & Git-Independent:** Works on any local directory—no git repository or commit history required.
- **LLM-Gated Sync Agent:** Evaluates cumulative diffs on every file save. Trivial edits (whitespace, comments, typos) are skipped, while substantial logical or structural additions are chunked, embedded, and appended.
- **Temporal Awareness:** Chunks are tracked with precise `indexed_at` and `updated_at` timestamps, enabling the chatbot to answer questions about what is new, what changed recently, or what baseline existed.
- **Seamless Auto-Connection:** Onboarding generates a direct workspace URL that auto-provisions and connects your chat tab in a single click.
- **Strict RAG Grounding:** Answers questions strictly from the indexed context of your repository—never from general knowledge or the internet.

---

## Architecture

```
[Local Codebase] --(purna watch)--> [LLM Sync Agent] --(if substantial)--> [FastAPI Backend]
                                                                                  |
                                                                                  v
[User Chat] <------------------- [pgvector top-k + Temporal Context] <--- [Postgres + pgvector]
```

---

## Quick Start

### 1. Start the Backend & Database

Ensure Docker Desktop is running, then start the PostgreSQL database with `pgvector` and the FastAPI server:

```bash
# Start the database container
docker compose up -d db

# Set up Python virtual environment (Python 3.10 - 3.12 recommended)
python3.12 -m venv .venv
source .venv/bin/activate

# Install dependencies and the purna package in editable mode
pip install -r requirements.txt
pip install -e .

# Configure secrets
cp .env.example .env
# Edit .env and set GEMINI_API_KEY (Google AI Studio key)

# Start the FastAPI server
uvicorn backend.main:app --reload --port 8000
```

The backend will be running at `http://localhost:8000`.

---

## Implementing PurnaOS in Your Repository

You can onboard any local repository or codebase into PurnaOS in two simple steps.

### Step 1: Onboard the Repository

Navigate to your target repository directory and run the unified onboarding command:

```bash
cd /path/to/your/repository

# Run onboarding with the fake test token
purna understand --purna-token purna_test_demo
```

This command will:
1. Contact the PurnaOS control plane to validate the token and provision a workspace.
2. Initialize a `.purnaOS` directory containing your workspace configuration (`workspace.yaml`) and state.
3. Perform a **baseline index** (chunking with tree-sitter and embedding with Gemini).
4. Upload the baseline artifacts to the backend.
5. Print your workspace details and a **direct browser connection link**.
6. Start the **`purna watch`** file monitoring daemon in the foreground.

```text
✓ Provisioned workspace: 2bc212a7-d939-4abd-b3d9-f41be5227f79
✓ Linked repository ID:  ad33d93c-96e6-4738-9069-af51ebee4905
🔗 Connect to this workspace in your browser: http://localhost:8000/?workspace_id=2bc212a7-d939-4abd-b3d9-f41be5227f79

🚀 Running initial baseline index...
Snapshot created for local-1125a3: 101 files, 570 chunks
📤 Uploading baseline artifacts to PurnaOS...
✓ Successfully uploaded baseline artifacts
👁  Starting purna watch daemon...
```

### Step 2: Open the Chat UI

1. Click the **🔗 Connect URL** printed in your terminal (or open `http://localhost:8000/?workspace_id=<your-workspace-id>`).
2. The frontend will automatically detect the workspace ID, provision a chat tab, connect to your repository, and clean up the URL.
3. You are now ready to chat with your codebase!

---

## The Real-Time Edit Loop

Once `purna watch` is running, it monitors your files for changes:

1. **Make a Trivial Edit:** Add a comment or fix a typo in a file and save. The watchdog triggers, computes the diff, and the LLM Sync Agent decides to **SKIP** it. The baseline is not advanced, allowing changes to accumulate.
2. **Make a Substantial Edit:** Add a new logical function or feature (e.g., `def calculate_discount(price): return price * 0.9`) and save. The LLM Sync Agent decides to **APPEND** the change. It is chunked, embedded, and uploaded.
3. **Chat in Real Time:** Ask the chatbot about your new function. It retrieves the newly appended chunks, notices their recent timestamps, and answers with perfect recency awareness.

---

## CLI Commands Reference

- `purna understand`: Unified onboarding (provisions workspace, runs baseline index, starts watch daemon).
- `purna watch`: Watches the repository for changes and evaluates them with the LLM Sync Agent.
- `purna status`: Displays the current PurnaOS workspace ID, repository ID, and browser connection link.
- `purna delete`: Deletes the PurnaOS configuration (`.purnaOS`) and local artifacts from the repository.
- `purna snapshot`: Manually captures a filesystem snapshot of the repository.
- `purna publish`: Manually publishes local chunks and artifacts to the backend.
- `purna init`: Initializes the `.purnaOS` directory.
