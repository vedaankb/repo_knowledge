from __future__ import annotations

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .api_keys import set_current_gemini_key
from .config import get_settings
from .github_client import GitHubClient
from .indexer import (
    finish_sync_run,
    get_repo_gemini_token,
    get_repo_token,
    index_repo_delta,
    ingest_prs,
    list_repos,
    start_sync_run,
)
from .knowledge_importer import sync_knowledge_repo

log = logging.getLogger(__name__)


async def sync_repo(repo_id, owner: str, name: str, source: str = "github") -> None:
    """Periodic sync job for a single repository"""
    settings = get_settings()
    
    # Restore Gemini key for this repo
    stored = await get_repo_gemini_token(repo_id)
    if stored:
        set_current_gemini_key(stored)
    
    # Different sync logic based on source type
    if source == "purna_knowledge":
        # Knowledge repo: import new artifacts
        try:
            log.info(f"Syncing knowledge repo {owner}/{name}...")
            stats = await sync_knowledge_repo(repo_id, owner, name)
            log.info(
                f"Knowledge repo sync complete for {owner}/{name}: "
                f"{stats['commits_imported']} new commits, {stats['chunks_imported']} chunks"
            )
        except Exception as e:
            log.error(f"Knowledge repo sync failed for {owner}/{name}: {e}", exc_info=True)
    else:
        # Source repo: delta sync via GitHub API
        run_id = await start_sync_run(repo_id, kind="delta")
        files_scanned = 0
        chunks_upserted = 0
        prs_ingested = 0
        try:
            token = await get_repo_token(repo_id) or settings.github_token
            async with GitHubClient(token=token) as gh:
                files_scanned, chunks_upserted, _ = await index_repo_delta(repo_id, gh)
                since_iso = datetime.now(timezone.utc).isoformat()
                prs_ingested = await ingest_prs(repo_id, gh, since_iso=None)
            await finish_sync_run(
                run_id, "success",
                files_scanned=files_scanned,
                chunks_upserted=chunks_upserted,
                prs_ingested=prs_ingested,
            )
            log.info("Synced %s/%s: files=%d chunks=%d prs=%d",
                     owner, name, files_scanned, chunks_upserted, prs_ingested)
        except Exception as e:
            log.exception("sync failed for %s/%s", owner, name)
            await finish_sync_run(run_id, "error", error=str(e))


async def sync_all_repos() -> None:
    """Sync all registered repositories"""
    repos = await list_repos()
    log.info("Scheduler tick: %d total repos to sync", len(repos))
    
    for r in repos:
        source = r.get("source") or "github"
        await sync_repo(r["id"], r["owner"], r["name"], source)


def build_scheduler() -> AsyncIOScheduler:
    settings = get_settings()
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        sync_all_repos,
        "interval",
        hours=settings.sync_interval_hours,
        id="sync_all_repos",
        next_run_time=None,
    )
    return scheduler
