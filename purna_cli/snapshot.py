"""Snapshot command - create project artifacts from filesystem scan (no git)."""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import PurnaConfig
from .fs_index import (
    iter_project_files,
    read_project_file,
    save_baseline_content,
    synthetic_commit_info,
    synthetic_snapshot_id,
)
from .utils import file_path_hash
from .chunking import process_file, should_skip_file


async def create_snapshot(
    repo_root: Path,
    config: PurnaConfig,
    commit_sha: Optional[str] = None,
    gemini_key: Optional[str] = None,
    all_tracked: bool = False,
) -> tuple[bool, str]:
    """
    Snapshot the project directory: chunk and embed text files.
    Scans the filesystem — no git required.
    Returns (success, message)
    """
    if commit_sha is None:
        commit_sha = synthetic_snapshot_id(repo_root)

    cfg = config.load()

    if gemini_key is None:
        import os
        gemini_key = os.getenv("GEMINI_API_KEY", "")

    if not gemini_key:
        return False, "GEMINI_API_KEY not set. Export it or pass --gemini-key"

    file_paths = iter_project_files(repo_root)
    changed_files = [
        {"path": p, "status": "added", "additions": 0, "deletions": 0}
        for p in file_paths
    ]

    commit_info = synthetic_commit_info(
        commit_sha,
        "Filesystem baseline index",
        changed_files,
    )

    active_files = changed_files

    commit_data = {
        "sha": commit_info["sha"],
        "message": commit_info["message"],
        "author": commit_info["author"],
        "author_email": commit_info["author_email"],
        "committed_at": commit_info["committed_at"],
        "parents": commit_info["parents"],
        "changed_files": changed_files,
        "commit_summary": commit_info["commit_summary"],
    }

    commits_dir = config.local_dir / "commits"
    commits_dir.mkdir(parents=True, exist_ok=True)
    commit_file = commits_dir / f"{commit_sha}.json"
    with open(commit_file, "w") as f:
        json.dump(commit_data, f, indent=2)

    chunks_dir = config.local_dir / "chunks" / commit_sha
    chunks_dir.mkdir(parents=True, exist_ok=True)

    total_chunks = 0
    processed_files = 0

    for file_info in active_files:
        file_path = file_info["path"]

        full_path = repo_root / file_path
        if should_skip_file(str(full_path)):
            continue

        fhash = file_path_hash(file_path)
        staging_file = config.staging_dir / f"{fhash}.json"

        if staging_file.exists():
            try:
                with open(staging_file, "r") as f:
                    chunks = json.load(f)

                for chunk in chunks:
                    chunk["commit_sha"] = commit_sha

                chunk_file = chunks_dir / f"{fhash}.json"
                with open(chunk_file, "w") as f:
                    json.dump(chunks, f, indent=2)

                staging_file.unlink()
                total_chunks += len(chunks)
                processed_files += 1
                continue
            except Exception as e:
                print(f"Warning: Failed to merge staged chunks for {file_path}: {e}")

        content = read_project_file(repo_root, file_path)
        if content is None:
            continue

        try:
            chunks = await process_file(file_path, content, repo_root, commit_sha, gemini_key)

            if chunks:
                chunk_file = chunks_dir / f"{fhash}.json"
                with open(chunk_file, "w") as f:
                    json.dump(chunks, f, indent=2)

                save_baseline_content(config.local_dir, file_path, content)
                total_chunks += len(chunks)
                processed_files += 1

        except Exception as e:
            print(f"Warning: Failed to process {file_path}: {e}")
            continue

    state = config.load_state()
    state["last_snapshot_sha"] = commit_sha
    state["last_snapshot_at"] = datetime.utcnow().isoformat() + "Z"
    state["pending_files"] = [f["path"] for f in active_files]
    state["staging_count"] = len(list(config.staging_dir.glob("*.json")))
    config.save_state(state)

    message = f"Snapshot created for {commit_sha[:12]}: {processed_files} files, {total_chunks} chunks"
    return True, message
