"""Watch command - debounced FS watcher for real-time chunking"""

import asyncio
import os
import time
from pathlib import Path
from typing import Set
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent

from .config import PurnaConfig
from .chunking import process_file, should_skip_file
from .fs_index import (
    SKIP_WALK_DIRS,
    WORKING_SNAPSHOT_ID,
    compute_text_diff,
    load_baseline_content,
    save_baseline_content,
)
from .utils import content_hash, file_path_hash


class DeboucedFileHandler(FileSystemEventHandler):
    """Debounced file system event handler"""
    
    def __init__(self, config: PurnaConfig, repo_root: Path, debounce_ms: int = 3000):
        self.config = config
        self.repo_root = repo_root.resolve()
        self.debounce_seconds = debounce_ms / 1000.0
        self.pending_files: Set[str] = set()
        self.last_event_time = 0
        self.lock = asyncio.Lock()
        
    def on_modified(self, event):
        if event.is_directory:
            return
        self._mark_pending(event.src_path)
    
    def on_created(self, event):
        if event.is_directory:
            return
        self._mark_pending(event.src_path)
    
    def _mark_pending(self, path: str):
        """Mark a file as pending for processing"""
        full_path = Path(path)
        if not full_path.is_file():
            return

        try:
            rel = full_path.resolve().relative_to(self.repo_root).as_posix()
        except ValueError:
            return

        parts = rel.split("/")
        if any(part in SKIP_WALK_DIRS for part in parts):
            return

        base = parts[-1]
        if base.startswith(".") and base not in {
            ".env.example", ".env.sample", ".env.template",
        }:
            return

        if should_skip_file(str(full_path)):
            return

        self.pending_files.add(str(full_path))
        self.last_event_time = time.time()
    
    async def get_debounced_files(self) -> Set[str]:
        """Get files that have been stable for debounce period"""
        async with self.lock:
            now = time.time()
            if now - self.last_event_time < self.debounce_seconds:
                return set()
            
            files = self.pending_files.copy()
            self.pending_files.clear()
            return files


def compute_diff(
    file_path_rel: str,
    content: str,
    config: PurnaConfig,
) -> tuple[str, bool]:
    """Diff against last indexed content on disk — no git."""
    old_content = load_baseline_content(config.local_dir, file_path_rel)
    return compute_text_diff(file_path_rel, old_content, content)


async def process_pending_files(
    handler: DeboucedFileHandler,
    repo_root: Path,
    gemini_key: str
):
    """Process pending files from the watch handler"""
    files = await handler.get_debounced_files()
    
    if not files:
        return 0
    
    config = handler.config
    staging_dir = config.staging_dir
    staging_dir.mkdir(parents=True, exist_ok=True)
    
    processed_count = 0
    
    for file_path_abs in files:
        try:
            # Convert absolute path to relative
            file_path_rel = str(Path(file_path_abs).relative_to(repo_root))
            
            # Read file content
            with open(file_path_abs, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            
            # Check if content has changed (compare content_hash)
            file_hash = file_path_hash(file_path_rel)
            staging_file = staging_dir / f"{file_hash}.json"
            
            new_hash = content_hash(content)
            
            # Skip if unchanged
            if staging_file.exists():
                import json
                with open(staging_file) as f:
                    existing = json.load(f)
                if existing and existing[0].get('content_hash') == new_hash:
                    continue  # Content unchanged, skip
            
            # 1. Compute cumulative diff against last memory-committed content
            diff, is_new_file = compute_diff(file_path_rel, content, config)

            # No accumulated change (no-op save or revert to memory state)
            if not diff.strip() and not is_new_file:
                continue

            # 2. Load workspace config and state
            workspace_cfg = config.load_workspace()
            workspace_id = workspace_cfg.get("workspace_id")
            api_url = workspace_cfg.get("api_url", "http://localhost:8000")
            
            state = config.load_state()
            purna_token = state.get("purna_token")
            
            if not workspace_id or not purna_token:
                print("Error: Workspace not fully configured. Run 'purna understand' first.")
                continue
                
            # 3. Call POST /api/purna/events
            import httpx
            headers = {"X-Gemini-Key": gemini_key} if gemini_key else {}
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{api_url}/api/purna/events",
                    json={
                        "workspace_id": workspace_id,
                        "purna_token": purna_token,
                        "file_path": file_path_rel,
                        "diff": diff,
                        "event_type": "file_edit",
                        "is_new_file": is_new_file
                    },
                    headers=headers,
                )
                if resp.status_code != 200:
                    print(f"✗ {file_path_rel}: Event check failed: {resp.text}")
                    continue
                decision = resp.json()
                
            # 4. Handle decision
            if decision["action"] == "append":
                print(f"✦ {file_path_rel}: Agent decided to APPEND ({decision['reason']})")
                # Process: chunk and embed
                chunks = await process_file(
                    file_path_rel,
                    content,
                    repo_root,
                    WORKING_SNAPSHOT_ID,
                    gemini_key
                )
                
                if chunks:
                    import json
                    with open(staging_file, 'w') as f:
                        json.dump(chunks, f, indent=2)

                    # Persist under chunks/working/ so future snapshots include it
                    working_dir = config.local_dir / "chunks" / WORKING_SNAPSHOT_ID
                    working_dir.mkdir(parents=True, exist_ok=True)
                    with open(working_dir / f"{file_hash}.json", 'w') as f:
                        json.dump(chunks, f, indent=2)

                    save_baseline_content(config.local_dir, file_path_rel, content)

                    # Upload ONLY this file's chunks (keeps timestamps of
                    # untouched chunks intact for new-vs-old questions)
                    async with httpx.AsyncClient(timeout=60.0) as client:
                        resp = await client.post(
                            f"{api_url}/api/purna/artifacts",
                            json={
                                "workspace_id": workspace_id,
                                "purna_token": purna_token,
                                "commits": {},
                                "chunks": {WORKING_SNAPSHOT_ID: {file_hash: chunks}},
                                "deleted": {},
                            },
                            headers=headers,
                        )
                    if resp.status_code == 200:
                        print(f"✓ {file_path_rel}: Successfully chunked, embedded, and uploaded")
                        processed_count += len(chunks)
                    else:
                        print(f"✗ {file_path_rel}: Upload failed: {resp.text}")
            elif decision["action"] == "skip":
                # Do NOT advance the baseline: the change stays staged so the
                # agent always sees the CUMULATIVE diff since the last memory
                # update. Many small skips can add up to a later append.
                print(f"⤼ {file_path_rel}: Agent decided to SKIP ({decision['reason']})")
            elif decision["action"] == "defer":
                print(f"⏳ {file_path_rel}: Agent decided to DEFER ({decision['reason']})")
        
        except Exception as e:
            print(f"✗ {file_path_abs}: {e}")
            continue
    
    # Update state
    state = config.load_state()
    state["staging_count"] = len(list(staging_dir.glob("*.json")))
    config.save_state(state)
    
    return processed_count


async def watch_repository(
    repo_root: Path,
    config: PurnaConfig,
    gemini_key: str
):
    """
    Watch repository for file changes and process them in real-time
    Runs continuously until interrupted
    """
    cfg = config.load()
    debounce_ms = cfg.get("sync", {}).get("debounce_ms", 3000)
    
    print(f"👁  Watching {repo_root} for changes...")
    print(f"   Debounce: {debounce_ms}ms")
    print(f"   Press Ctrl+C to stop")
    print()
    
    # Create handler and observer
    handler = DeboucedFileHandler(config, repo_root, debounce_ms)
    observer = Observer()
    observer.schedule(handler, str(repo_root), recursive=True)
    observer.start()
    
    try:
        while True:
            # Process pending files every second
            await asyncio.sleep(1)
            
            processed = await process_pending_files(handler, repo_root, gemini_key)
            if processed > 0:
                print(f"Processed {processed} chunks")
    
    except KeyboardInterrupt:
        print("\nStopping watcher...")
    finally:
        observer.stop()
        observer.join()


async def watch_once(
    repo_root: Path,
    config: PurnaConfig,
    gemini_key: str,
    duration_seconds: int = 60
):
    """
    Watch for a limited time (for testing)
    """
    cfg = config.load()
    debounce_ms = cfg.get("sync", {}).get("debounce_ms", 3000)
    
    handler = DeboucedFileHandler(config, repo_root, debounce_ms)
    observer = Observer()
    observer.schedule(handler, str(repo_root), recursive=True)
    observer.start()
    
    try:
        end_time = time.time() + duration_seconds
        while time.time() < end_time:
            await asyncio.sleep(1)
            await process_pending_files(handler, repo_root, gemini_key)
    finally:
        observer.stop()
        observer.join()
