"""Utility functions for purna CLI"""

import hashlib
import subprocess
from pathlib import Path
from typing import Optional


def content_hash(text: str) -> str:
    """Generate SHA256 hash with 'sha256:' prefix for deduplication"""
    h = hashlib.sha256(text.encode('utf-8')).hexdigest()
    return f"sha256:{h}"


def file_path_hash(file_path: str) -> str:
    """Generate truncated SHA256 hash of file path (first 16 chars)"""
    h = hashlib.sha256(file_path.encode('utf-8')).hexdigest()
    return h[:16]


def git_command(args: list[str], cwd: Optional[Path] = None) -> str:
    """Run a git command and return stdout"""
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True
    )
    return result.stdout.strip()


def get_current_sha(repo_root: Path) -> str:
    """Get current HEAD commit SHA"""
    return git_command(["rev-parse", "HEAD"], cwd=repo_root)


def get_commit_info(sha: str, repo_root: Path) -> dict:
    """Get commit metadata"""
    # Format: author|email|timestamp|message
    info = git_command([
        "log", "-1", "--format=%an|%ae|%cI|%s%n%b", sha
    ], cwd=repo_root)
    
    lines = info.split('\n')
    first_line = lines[0].split('|', 3)
    author, email, timestamp, message_start = first_line
    
    # Full message is first line subject + rest of lines
    full_message = message_start
    if len(lines) > 1:
        full_message += '\n' + '\n'.join(lines[1:])
    
    # Get parent commits
    parents = git_command(["rev-list", "--parents", "-n", "1", sha], cwd=repo_root).split()[1:]
    
    return {
        "sha": sha,
        "author": author,
        "author_email": email,
        "committed_at": timestamp,
        "message": full_message.strip(),
        "parents": parents,
    }


def get_changed_files(sha: str, repo_root: Path) -> list[dict]:
    """Get list of files changed in a commit with stats"""
    output = git_command([
        "diff-tree", "--no-commit-id", "--name-status", "--numstat", "-r", sha
    ], cwd=repo_root)
    
    files = []
    for line in output.split('\n'):
        if not line.strip():
            continue
        
        parts = line.split('\t')
        if len(parts) >= 3:
            # numstat format: additions deletions path
            try:
                additions = int(parts[0]) if parts[0] != '-' else 0
                deletions = int(parts[1]) if parts[1] != '-' else 0
                path = parts[2]
                
                # Determine status (M=modified, A=added, D=deleted)
                status_line = git_command([
                    "diff-tree", "--no-commit-id", "--name-status", "-r", sha
                ], cwd=repo_root)
                
                status = "modified"
                for sl in status_line.split('\n'):
                    if path in sl:
                        if sl.startswith('A'):
                            status = "added"
                        elif sl.startswith('D'):
                            status = "deleted"
                        elif sl.startswith('M'):
                            status = "modified"
                        break
                
                files.append({
                    "path": path,
                    "status": status,
                    "additions": additions,
                    "deletions": deletions,
                })
            except (ValueError, IndexError):
                continue
    
    return files


def read_worktree_text(repo_root: Path, file_path: str) -> Optional[str]:
    """Read a tracked file from the working tree as UTF-8 text; skip binary."""
    full_path = repo_root / file_path
    if not full_path.is_file():
        return None
    try:
        data = full_path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def get_file_content(file_path: str, sha: str, repo_root: Path) -> Optional[str]:
    """Get file content at a specific commit; returns None for missing/binary files."""
    try:
        result = subprocess.run(
            ["git", "show", f"{sha}:{file_path}"],
            cwd=repo_root,
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        return None
    if b"\x00" in result.stdout:
        return None
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


def get_repo_owner_name(repo_root: Path) -> tuple[str, str]:
    """Get owner and name of the git repository from origin remote"""
    try:
        remote_url = git_command(["remote", "get-url", "origin"], cwd=repo_root)
        if "github.com" in remote_url:
            parts = remote_url.split("github.com")[-1].strip("/:").replace(".git", "").split("/")
            if len(parts) >= 2:
                return parts[0], parts[1]
    except Exception:
        pass
    # Fallback to local directory name
    return "local", repo_root.name
