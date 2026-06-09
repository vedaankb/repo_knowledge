"""Publish command - push artifacts to knowledge GitHub repo"""

import json
import base64
from datetime import datetime
from pathlib import Path
from typing import Optional
import httpx

from .config import PurnaConfig
from .utils import get_current_sha


async def publish_to_knowledge_repo(
    repo_root: Path,
    config: PurnaConfig,
    github_token: str,
    force: bool = False,
) -> tuple[bool, str]:
    """
    Publish local artifacts to knowledge GitHub repository
    Uses GitHub Contents API to upload files
    Returns (success, message)
    """
    cfg = config.load()
    knowledge_repo = cfg.get("knowledge", {}).get("github")
    knowledge_branch = cfg.get("knowledge", {}).get("branch", "main")
    
    if not knowledge_repo:
        return False, "knowledge.github not configured in .purnaOS/config.yaml"
    
    if not github_token:
        return False, "GitHub token required for publishing. Use --github-token or GITHUB_TOKEN env var"
    
    # Load state
    state = config.load_state()
    current_sha = get_current_sha(repo_root)
    
    # Check if already published
    if not force and state.get("last_published_sha") == current_sha:
        return True, f"Already published {current_sha[:8]}"
    
    # Collect files to publish from local/
    local_dir = config.local_dir
    
    files_to_upload = []
    
    # Commits
    commits_dir = local_dir / "commits"
    if commits_dir.exists():
        for commit_file in commits_dir.glob("*.json"):
            target_path = f"commits/{commit_file.name}"
            files_to_upload.append((commit_file, target_path))
    
    # Chunks
    chunks_dir = local_dir / "chunks"
    if chunks_dir.exists():
        for sha_dir in chunks_dir.iterdir():
            if sha_dir.is_dir():
                for chunk_file in sha_dir.glob("*.json"):
                    target_path = f"chunks/{sha_dir.name}/{chunk_file.name}"
                    files_to_upload.append((chunk_file, target_path))
    
    # Deleted
    deleted_dir = local_dir / "deleted"
    if deleted_dir.exists():
        for deleted_file in deleted_dir.glob("*.json"):
            target_path = f"deleted/{deleted_file.name}"
            files_to_upload.append((deleted_file, target_path))
    
    if not files_to_upload:
        return False, "No artifacts to publish. Run 'purna snapshot' first."
    
    # Upload files via GitHub Contents API
    base_url = f"https://api.github.com/repos/{knowledge_repo}/contents"
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    
    uploaded = 0
    failed = []
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        for local_file, target_path in files_to_upload:
            content = local_file.read_text()
            content_b64 = base64.b64encode(content.encode()).decode()
            
            # Check if file exists (to get SHA for update)
            url = f"{base_url}/{target_path}"
            existing_sha = None
            
            try:
                resp = await client.get(url, headers=headers, params={"ref": knowledge_branch})
                if resp.status_code == 200:
                    existing_sha = resp.json().get("sha")
            except:
                pass
            
            # Upload/update file
            payload = {
                "message": f"Update {target_path}",
                "content": content_b64,
                "branch": knowledge_branch,
            }
            
            if existing_sha:
                payload["sha"] = existing_sha
            
            try:
                resp = await client.put(url, headers=headers, json=payload)
                if resp.status_code in [200, 201]:
                    uploaded += 1
                else:
                    failed.append(target_path)
            except Exception as e:
                failed.append(f"{target_path} ({e})")
    
    # Update manifest.json
    manifest_url = f"{base_url}/manifest.json"
    manifest_data = {
        "schema_version": 1,
        "source_repo": cfg.get("source", {}).get("remote", "origin"),
        "head_sha": current_sha,
        "published_at": datetime.utcnow().isoformat() + "Z",
        "total_chunks": len([f for _, p in files_to_upload if p.startswith("chunks/")]),
        "total_commits": len([f for _, p in files_to_upload if p.startswith("commits/")]),
    }
    
    manifest_b64 = base64.b64encode(json.dumps(manifest_data, indent=2).encode()).decode()
    
    # Get existing manifest SHA
    manifest_sha = None
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.get(manifest_url, headers=headers, params={"ref": knowledge_branch})
            if resp.status_code == 200:
                manifest_sha = resp.json().get("sha")
        except:
            pass
        
        # Upload manifest
        manifest_payload = {
            "message": f"Update manifest for {current_sha[:8]}",
            "content": manifest_b64,
            "branch": knowledge_branch,
        }
        if manifest_sha:
            manifest_payload["sha"] = manifest_sha
        
        try:
            await client.put(manifest_url, headers=headers, json=manifest_payload)
        except Exception as e:
            return False, f"Failed to update manifest: {e}"
    
    # Update state
    state["last_published_sha"] = current_sha
    state["last_published_at"] = datetime.utcnow().isoformat() + "Z"
    state["pending_files"] = []
    config.save_state(state)
    
    message = f"Published {uploaded} files to {knowledge_repo}"
    if failed:
        message += f"\nFailed: {len(failed)} files"
    
    return True, message


async def upload_local_artifacts(
    repo_root: Path,
    config: PurnaConfig,
    api_url: str,
    purna_token: str,
) -> tuple[bool, str]:
    """
    Upload local artifacts to PurnaOS backend
    """
    workspace_cfg = config.load_workspace()
    workspace_id = workspace_cfg.get("workspace_id")
    if not workspace_id:
        return False, "workspace_id not found in workspace.yaml"
        
    local_dir = config.local_dir
    payload = {
        "workspace_id": workspace_id,
        "purna_token": purna_token,
        "commits": {},
        "chunks": {},
        "deleted": {}
    }
    
    # 1. Collect commits
    commits_dir = local_dir / "commits"
    if commits_dir.exists():
        for commit_file in commits_dir.glob("*.json"):
            try:
                with open(commit_file) as f:
                    payload["commits"][commit_file.stem] = json.load(f)
            except Exception:
                pass
                
    # 2. Collect chunks
    chunks_dir = local_dir / "chunks"
    if chunks_dir.exists():
        for sha_dir in chunks_dir.iterdir():
            if sha_dir.is_dir():
                sha = sha_dir.name
                payload["chunks"][sha] = {}
                for chunk_file in sha_dir.glob("*.json"):
                    try:
                        with open(chunk_file) as f:
                            payload["chunks"][sha][chunk_file.stem] = json.load(f)
                    except Exception:
                        pass
                        
    # 3. Collect deleted
    deleted_dir = local_dir / "deleted"
    if deleted_dir.exists():
        for deleted_file in deleted_dir.glob("*.json"):
            try:
                with open(deleted_file) as f:
                    payload["deleted"][deleted_file.stem] = json.load(f)
            except Exception:
                pass
                
    # Send request
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            resp = await client.post(
                f"{api_url}/api/purna/artifacts",
                json=payload
            )
            if resp.status_code == 200:
                # Update state
                state = config.load_state()
                current_sha = get_current_sha(repo_root)
                state["last_published_sha"] = current_sha
                state["last_published_at"] = datetime.utcnow().isoformat() + "Z"
                state["pending_files"] = []
                config.save_state(state)
                return True, "Successfully uploaded artifacts to PurnaOS"
            else:
                return False, f"Failed to upload artifacts: {resp.text}"
        except Exception as e:
            return False, f"Error uploading artifacts: {e}"
