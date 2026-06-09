"""Snapshot command - create commit artifacts"""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import PurnaConfig
from .utils import get_current_sha, get_commit_info, get_changed_files, get_file_content, file_path_hash
from .chunking import process_file, should_skip_file


async def create_snapshot(
    repo_root: Path,
    config: PurnaConfig,
    commit_sha: Optional[str] = None,
    gemini_key: Optional[str] = None,
    all_tracked: bool = False,
) -> tuple[bool, str]:
    """
    Create a snapshot of the current commit
    - Chunks and embeds all changed files
    - Saves artifacts to .purnaOS/local/ for later publish
    Returns (success, message)
    """
    if commit_sha is None:
        commit_sha = get_current_sha(repo_root)
    
    # Load config
    cfg = config.load()
    
    # Get Gemini API key
    if gemini_key is None:
        import os
        gemini_key = os.getenv("GEMINI_API_KEY", "")
    
    if not gemini_key:
        return False, "GEMINI_API_KEY not set. Export it or pass --gemini-key"
    
    # Get commit metadata
    commit_info = get_commit_info(commit_sha, repo_root)
    
    if all_tracked:
        from .utils import git_command
        tracked_output = git_command(["ls-files"], cwd=repo_root)
        changed_files = []
        for f in tracked_output.split("\n"):
            if f.strip():
                changed_files.append({
                    "path": f.strip(),
                    "status": "added",
                    "additions": 0,
                    "deletions": 0
                })
    else:
        changed_files = get_changed_files(commit_sha, repo_root)
    
    # Filter out deleted files
    active_files = [f for f in changed_files if f["status"] != "deleted"]
    deleted_files = [f["path"] for f in changed_files if f["status"] == "deleted"]
    
    # Create commit metadata JSON
    commit_data = {
        "sha": commit_info["sha"],
        "message": commit_info["message"],
        "author": commit_info["author"],
        "author_email": commit_info["author_email"],
        "committed_at": commit_info["committed_at"],
        "parents": commit_info["parents"],
        "changed_files": changed_files,
        "commit_summary": f"{commit_info['message']}. Modified {len(active_files)} files.",
    }
    
    # Save commit metadata to local/commits/
    commits_dir = config.local_dir / "commits"
    commits_dir.mkdir(parents=True, exist_ok=True)
    commit_file = commits_dir / f"{commit_sha}.json"
    with open(commit_file, "w") as f:
        json.dump(commit_data, f, indent=2)
    
    # Save deleted files if any
    if deleted_files:
        deleted_dir = config.local_dir / "deleted"
        deleted_dir.mkdir(parents=True, exist_ok=True)
        deleted_file = deleted_dir / f"{commit_sha}.json"
        with open(deleted_file, "w") as f:
            json.dump({
                "commit_sha": commit_sha,
                "deleted_at": datetime.utcnow().isoformat() + "Z",
                "deleted_files": deleted_files,
            }, f, indent=2)
    
    # Process each changed file
    chunks_dir = config.local_dir / "chunks" / commit_sha
    chunks_dir.mkdir(parents=True, exist_ok=True)
    
    total_chunks = 0
    processed_files = 0
    
    for file_info in active_files:
        file_path = file_info["path"]
        
        # Skip large/binary files
        full_path = repo_root / file_path
        if should_skip_file(str(full_path)):
            continue
        
        # Check if we have a staged chunk file for this file
        fhash = file_path_hash(file_path)
        staging_file = config.staging_dir / f"{fhash}.json"
        
        if staging_file.exists():
            try:
                with open(staging_file, "r") as f:
                    chunks = json.load(f)
                
                # Update commit_sha in each chunk
                for chunk in chunks:
                    chunk["commit_sha"] = commit_sha
                
                # Save to chunks/{sha}/{file_hash}.json
                chunk_file = chunks_dir / f"{fhash}.json"
                with open(chunk_file, "w") as f:
                    json.dump(chunks, f, indent=2)
                
                # Remove from staging
                staging_file.unlink()
                
                total_chunks += len(chunks)
                processed_files += 1
                continue
            except Exception as e:
                print(f"Warning: Failed to merge staged chunks for {file_path}: {e}")
        
        # Get file content at this commit
        content = get_file_content(file_path, commit_sha, repo_root)
        if content is None:
            continue
        
        # Process: chunk and embed
        try:
            chunks = await process_file(file_path, content, repo_root, commit_sha, gemini_key)
            
            if chunks:
                # Save to chunks/{sha}/{file_hash}.json
                chunk_file = chunks_dir / f"{fhash}.json"
                with open(chunk_file, "w") as f:
                    json.dump(chunks, f, indent=2)
                
                total_chunks += len(chunks)
                processed_files += 1
        
        except Exception as e:
            print(f"Warning: Failed to process {file_path}: {e}")
            continue
    
    # Update state
    state = config.load_state()
    state["last_snapshot_sha"] = commit_sha
    state["last_snapshot_at"] = datetime.utcnow().isoformat() + "Z"
    state["pending_files"] = list(set(state.get("pending_files", []) + [f["path"] for f in active_files]))
    state["staging_count"] = len(list(config.staging_dir.glob("*.json")))
    config.save_state(state)
    
    message = f"Snapshot created for {commit_sha[:8]}: {processed_files} files, {total_chunks} chunks"
    return True, message
