# PurnaOS Enterprise POC Demo Script

This document guides you through the live demonstration of the client-driven edit-triggered knowledge loop.

---

## 0. Gemini API key (POC)

For the POC, the Gemini key is set in `repo_knowledge/.env` as `GEMINI_API_KEY`. The backend and CLI pick it up automatically — no export or sidebar paste needed.

Copy `.env.example` to `.env` and set your key if you haven't already:

```bash
cp .env.example .env
# edit GEMINI_API_KEY=...
```

---

## 1. Install the `purna` CLI (one-time)

The `purna` command is not global until you install it from this repo. Use **Python 3.10–3.12** (3.14 is not supported yet by all dependencies).

```bash
cd /path/to/repo_knowledge
python3.12 -m venv .venv          # use python3.12 or python3.11 if available
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
purna --help                      # should print command list
```

After activation, `purna` works from **any** directory (e.g. your `image-size-tool` repo).

Without activating, call it by full path:

```bash
/path/to/repo_knowledge/.venv/bin/purna understand --purna-token purna_test_demo --gemini-key "$GEMINI_API_KEY"
```

---

## 2. Setup

### Terminal 1 — Start Backend & Database
Ensure Postgres is running and start the FastAPI server:
```bash
docker compose up -d db
uvicorn backend.main:app --reload --port 8000
```

### Terminal 2 — Onboard the Project
Navigate to any project directory (git **not** required) and run `purna understand`:
```bash
cd /path/to/your/repo
purna understand --purna-token purna_test_demo
```
*   **PurnaOS Token:** `purna_test_demo` (seeded fake test token).
*   **Gemini API Key:** Loaded from `.env` automatically (optional: `--gemini-key` to override).
*   This command will:
    1.  Validate the token and provision a workspace.
    2.  Create `.purnaOS/workspace.yaml` and `.purnaOS/state.json`.
    3.  Run a **baseline index** (chunk and embed all tracked files).
    4.  Upload the baseline artifacts to the backend.
    5.  Start the **`purna watch`** daemon in the foreground.

---

## 3. The Demo Loop

### Step 1: Open the Chat UI
1.  Open your browser and navigate to `http://localhost:8000`.
2.  Paste the **Workspace ID** or **Repo ID** printed by `purna understand` into the **PurnaOS Workspace** tab on the onboarding screen.
3.  Click **Connect**.
4.  You are now connected to the real-time workspace!

### Step 2: Make a Trivial Edit (SKIP)
1.  Open a file in your repository (e.g., `README.md` or a comment in a code file).
2.  Make a trivial edit (e.g., fix a typo, add a whitespace, or tweak a comment).
3.  Save the file.
4.  In **Terminal 2**, you will see:
    ```
    ⤼ README.md: Agent decided to SKIP (Trivial edit to documentation with no logic changes)
    ```
5.  In the **Chat UI**, the status panel will poll and show:
    ```
    last check: skip (Trivial edit to documentation with no logic changes)
    ```
6.  Ask the chat: *"What typo did I just fix?"*
7.  The chat will answer: *"I don't have enough information from the indexed repository context to answer that."* (Because the change was skipped from indexing!).

### Step 3: Make a Meaningful Edit (APPEND)
1.  Open a code file in your repository.
2.  Add a new meaningful function or feature (e.g., `def calculate_discount(price): return price * 0.9`).
3.  Save the file.
4.  In **Terminal 2**, you will see:
    ```
    ✦ features.py: Agent decided to APPEND (Adds a new discount calculation function)
    ✓ features.py: Successfully chunked, embedded, and uploaded
    ```
5.  In the **Chat UI**, the status panel will poll and show:
    ```
    last check: append (Adds a new discount calculation function)
    ```
6.  Ask the chat: *"What does the calculate_discount function do?"*
7.  The chat will answer with perfect grounded knowledge: *"The calculate_discount function takes a price and returns it with a 10% discount applied (price * 0.9)."*

---

## 4. Behind the Scenes

1.  **Zero-Commit Triggers:** No git commit or push is required. The save event triggers the entire evaluation.
2.  **Cost-Efficient Gatekeeper:** The LLM sync agent evaluates the unified diff first (very cheap). Chunking, embedding, and uploading only happen on `append` decisions, saving massive API costs and database space.
3.  **Audit Trail:** Every decision is logged to the `knowledge_decisions` table and displayed in real-time in the chat status panel.
