from __future__ import annotations

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .config import get_settings
from .github_client import GitHubClient
from .indexer import (
    finish_sync_run,
    get_repo_token,
    index_repo_delta,
    ingest_prs,
    list_repos,
    start_sync_run,
)

log = logging.getLogger(__name__)


async def sync_repo(repo_id, owner: str, name: str) -> None:
    settings = get_settings()
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
    repos = await list_repos()
    github_repos = [r for r in repos if (r.get("source") or "github") == "github"]
    log.info(
        "Scheduler tick: %d total, %d github repos to sync",
        len(repos), len(github_repos),
    )
    for r in github_repos:
        await sync_repo(r["id"], r["owner"], r["name"])


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
