from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from uuid import UUID

import httpx
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .chat import answer, preview
from .chat_memory import count_turns, delete_chat
from .config import get_settings
from .db import close_pool, init_pool, pool
from .github_client import GitHubClient, parse_repo_url
from .indexer import (
    delete_repo,
    finish_sync_run,
    get_repo_by_owner_name,
    get_repo_row,
    get_repo_token,
    index_local_directory,
    index_repo_delta,
    index_repo_initial,
    ingest_prs,
    list_repos,
    start_sync_run,
    upsert_repo,
)
from .scheduler import build_scheduler, sync_repo

MAX_ZIP_BYTES = 100 * 1024 * 1024
MAX_ZIP_ENTRIES = 20_000

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("repo_knowledge")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_pool()
    scheduler = build_scheduler()
    scheduler.start()
    app.state.scheduler = scheduler
    log.info("App started. Scheduler running every %dh.",
             get_settings().sync_interval_hours)
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        await close_pool()


app = FastAPI(title="repo-knowledge POC", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class RegisterRepoRequest(BaseModel):
    url: str
    token: Optional[str] = None


class ChatRequest(BaseModel):
    repo_id: UUID
    chat_id: str = Field(min_length=4, max_length=80)
    question: str
    commit_sha: Optional[str] = Field(default=None, max_length=64)


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True}


@app.post("/api/repos")
async def register_repo(req: RegisterRepoRequest, bg: BackgroundTasks) -> dict:
    settings = get_settings()
    try:
        owner, name = parse_repo_url(req.url)
    except ValueError as e:
        raise HTTPException(400, str(e))

    user_token = (req.token or "").strip() or None
    effective_token = user_token or settings.github_token

    try:
        async with GitHubClient(token=effective_token) as gh:
            info = await gh.get_repo(owner, name)
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        if status in (401, 403, 404):
            if user_token:
                raise HTTPException(
                    status_code=401,
                    detail={
                        "code": "invalid_token",
                        "message": (
                            "GitHub rejected that token. Ensure it has 'repo' scope "
                            "and access to this repository."
                        ),
                    },
                )
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "auth_required",
                    "message": (
                        "Could not access this repo. If it's private, paste a GitHub "
                        "access token with 'repo' scope below."
                    ),
                },
            )
        raise HTTPException(502, f"GitHub error: {e}")
    except Exception as e:
        raise HTTPException(404, f"Could not access repo {owner}/{name}: {e}")

    repo_id = await upsert_repo(
        info.owner, info.name, info.default_branch, info.visibility,
        source="github",
        github_token=user_token,
    )
    bg.add_task(_initial_index_task, repo_id, info.owner, info.name)

    return {
        "repo_id": str(repo_id),
        "owner": info.owner,
        "name": info.name,
        "default_branch": info.default_branch,
        "visibility": info.visibility,
        "indexing": "queued",
    }


async def _initial_index_task(repo_id: UUID, owner: str, name: str) -> None:
    settings = get_settings()
    run_id = await start_sync_run(repo_id, kind="initial")
    files_scanned = 0
    chunks_upserted = 0
    prs_ingested = 0
    try:
        token = await get_repo_token(repo_id) or settings.github_token
        async with GitHubClient(token=token) as gh:
            files_scanned, chunks_upserted, _ = await index_repo_initial(repo_id, gh)
            prs_ingested = await ingest_prs(repo_id, gh, since_iso=None)
        await finish_sync_run(
            run_id, "success",
            files_scanned=files_scanned,
            chunks_upserted=chunks_upserted,
            prs_ingested=prs_ingested,
        )
        log.info("Initial index for %s/%s done: files=%d chunks=%d prs=%d",
                 owner, name, files_scanned, chunks_upserted, prs_ingested)
    except Exception as e:
        log.exception("initial index failed for %s/%s", owner, name)
        await finish_sync_run(run_id, "error", error=str(e))


@app.post("/api/repos/{repo_id}/sync")
async def sync_now(repo_id: UUID, bg: BackgroundTasks) -> dict:
    repo = await get_repo_row(repo_id)
    if not repo:
        raise HTTPException(404, "repo not found")
    bg.add_task(sync_repo, repo_id, repo["owner"], repo["name"])
    return {"status": "queued"}


@app.get("/api/repos")
async def get_repos() -> list[dict]:
    repos = await list_repos()
    out: list[dict] = []
    for r in repos:
        out.append({
            "id": str(r["id"]),
            "owner": r["owner"],
            "name": r["name"],
            "default_branch": r["default_branch"],
            "visibility": r["visibility"],
            "source": r.get("source") or "github",
            "label": r.get("label"),
            "last_indexed_sha": r.get("last_indexed_sha"),
            "last_synced_at": r["last_synced_at"].isoformat() if r.get("last_synced_at") else None,
        })
    return out


@app.get("/api/repos/{repo_id}/status")
async def repo_status(repo_id: UUID) -> dict:
    repo = await get_repo_row(repo_id)
    if not repo:
        raise HTTPException(404, "repo not found")
    async with pool().acquire() as conn:
        last_run = await conn.fetchrow(
            """SELECT id, started_at, ended_at, status, kind,
                      files_scanned, chunks_upserted, prs_ingested, error
               FROM sync_runs
               WHERE repo_id = $1
               ORDER BY started_at DESC LIMIT 1""",
            repo_id,
        )
        counts = await conn.fetchrow(
            """SELECT
                 (SELECT COUNT(*) FROM code_chunks WHERE repo_id = $1) AS code_chunks,
                 (SELECT COUNT(*) FROM feature_skills WHERE repo_id = $1) AS feature_skills
            """,
            repo_id,
        )
    return {
        "id": str(repo["id"]),
        "owner": repo["owner"],
        "name": repo["name"],
        "default_branch": repo["default_branch"],
        "visibility": repo["visibility"],
        "source": repo.get("source") or "github",
        "label": repo.get("label"),
        "last_indexed_sha": repo.get("last_indexed_sha"),
        "last_synced_at": repo["last_synced_at"].isoformat() if repo.get("last_synced_at") else None,
        "counts": {
            "code_chunks": counts["code_chunks"] if counts else 0,
            "feature_skills": counts["feature_skills"] if counts else 0,
        },
        "last_run": (
            {
                "id": str(last_run["id"]),
                "started_at": last_run["started_at"].isoformat() if last_run["started_at"] else None,
                "ended_at": last_run["ended_at"].isoformat() if last_run["ended_at"] else None,
                "status": last_run["status"],
                "kind": last_run["kind"],
                "files_scanned": last_run["files_scanned"],
                "chunks_upserted": last_run["chunks_upserted"],
                "prs_ingested": last_run["prs_ingested"],
                "error": last_run["error"],
            }
            if last_run
            else None
        ),
    }


@app.post("/api/chat")
async def chat(req: ChatRequest) -> dict:
    repo = await get_repo_row(req.repo_id)
    if not repo:
        raise HTTPException(404, "repo not found")
    if not (req.question or "").strip():
        raise HTTPException(400, "question is required")
    commit_sha = (req.commit_sha or "").strip() or None
    return await answer(req.repo_id, req.chat_id, req.question.strip(), commit_sha=commit_sha)


@app.post("/api/chat/preview")
async def chat_preview(req: ChatRequest) -> dict:
    repo = await get_repo_row(req.repo_id)
    if not repo:
        raise HTTPException(404, "repo not found")
    if not (req.question or "").strip():
        raise HTTPException(400, "question is required")
    commit_sha = (req.commit_sha or "").strip() or None
    return await preview(req.repo_id, req.chat_id, req.question.strip(), commit_sha=commit_sha)


@app.get("/api/repos/{repo_id}/commits")
async def list_indexed_commits(repo_id: UUID) -> dict:
    """Return distinct commit SHAs that currently have chunks indexed."""
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """SELECT commit_sha, COUNT(*) AS chunks
               FROM code_chunks
               WHERE repo_id = $1 AND commit_sha IS NOT NULL
               GROUP BY commit_sha
               ORDER BY MAX(indexed_at) DESC
               LIMIT 50""",
            repo_id,
        )
    return {
        "commits": [
            {"commit_sha": r["commit_sha"], "chunks": int(r["chunks"])}
            for r in rows
        ]
    }


@app.delete("/api/chats/{chat_id}")
async def delete_chat_endpoint(chat_id: str) -> dict:
    deleted = await delete_chat(chat_id)
    return {"deleted_turns": deleted}


@app.get("/api/chats/{chat_id}/turns/count")
async def chat_turns_count(chat_id: str) -> dict:
    n = await count_turns(chat_id)
    return {"chat_id": chat_id, "turns": n}


@app.delete("/api/repos/{repo_id}")
async def remove_repo(repo_id: UUID) -> dict:
    ok = await delete_repo(repo_id)
    if not ok:
        raise HTTPException(404, "repo not found")
    return {"deleted": True}


_NAME_SAFE_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def _safe_label(filename: str) -> str:
    base = Path(filename).stem or "upload"
    base = _NAME_SAFE_RE.sub("-", base).strip("-")
    return (base or "upload")[:80]


def _safe_extract_zip(zip_path: Path, target: Path) -> int:
    target = target.resolve()
    target.mkdir(parents=True, exist_ok=True)
    extracted = 0
    with zipfile.ZipFile(zip_path) as zf:
        members = zf.infolist()
        if len(members) > MAX_ZIP_ENTRIES:
            raise HTTPException(400, f"zip has too many entries (>{MAX_ZIP_ENTRIES})")
        total_size = sum(m.file_size for m in members)
        if total_size > 5 * MAX_ZIP_BYTES:
            raise HTTPException(400, "zip contents too large when extracted")
        for m in members:
            if m.is_dir():
                continue
            dest = (target / m.filename).resolve()
            if not str(dest).startswith(str(target) + os.sep) and dest != target:
                raise HTTPException(400, "zip contains unsafe path")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(m, "r") as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            extracted += 1
    return extracted


@app.post("/api/repos/upload")
async def upload_repo(
    bg: BackgroundTasks,
    file: UploadFile = File(...),
) -> dict:
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(400, "Only .zip uploads are supported")

    tmp_root = Path(tempfile.mkdtemp(prefix="repoknow_zip_"))
    zip_path = tmp_root / "upload.zip"
    total = 0
    with open(zip_path, "wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ZIP_BYTES:
                out.close()
                shutil.rmtree(tmp_root, ignore_errors=True)
                raise HTTPException(400, f"zip exceeds {MAX_ZIP_BYTES // (1024 * 1024)}MB limit")
            out.write(chunk)

    extract_root = tmp_root / "src"
    try:
        _safe_extract_zip(zip_path, extract_root)
    except HTTPException:
        shutil.rmtree(tmp_root, ignore_errors=True)
        raise
    except zipfile.BadZipFile:
        shutil.rmtree(tmp_root, ignore_errors=True)
        raise HTTPException(400, "Uploaded file is not a valid zip")

    children = [c for c in extract_root.iterdir() if c.is_dir()]
    root_for_index = children[0] if len(children) == 1 else extract_root

    label = _safe_label(file.filename)
    name = f"{label}-{uuid.uuid4().hex[:8]}"
    repo_id = await upsert_repo(
        owner="upload",
        name=name,
        default_branch="(zip)",
        visibility="private",
        source="upload",
        label=label,
    )
    bg.add_task(_zip_index_task, repo_id, root_for_index, tmp_root)
    return {
        "repo_id": str(repo_id),
        "owner": "upload",
        "name": name,
        "label": label,
        "source": "upload",
        "indexing": "queued",
    }


async def _zip_index_task(repo_id: UUID, root: Path, cleanup_root: Path) -> None:
    run_id = await start_sync_run(repo_id, kind="upload")
    files_scanned = 0
    chunks_upserted = 0
    try:
        files_scanned, chunks_upserted = await index_local_directory(repo_id, root)
        await finish_sync_run(
            run_id, "success",
            files_scanned=files_scanned,
            chunks_upserted=chunks_upserted,
        )
        log.info("Zip index done for repo %s: files=%d chunks=%d",
                 repo_id, files_scanned, chunks_upserted)
    except Exception as e:
        log.exception("zip indexing failed for repo %s", repo_id)
        await finish_sync_run(run_id, "error", error=str(e))
    finally:
        shutil.rmtree(cleanup_root, ignore_errors=True)


FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(str(FRONTEND_DIR / "index.html"))
