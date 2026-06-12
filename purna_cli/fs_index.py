"""Filesystem-based project indexing (no git dependency)."""

from __future__ import annotations

import difflib
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .chunking import should_skip_file

# Keep in sync with backend/chunker.py SKIP_DIRS + purna-local dirs
SKIP_WALK_DIRS = {
    "node_modules", ".git", "dist", "build", "out", "target",
    ".next", ".nuxt", ".venv", "venv", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "vendor", ".idea", ".vscode",
    "coverage", ".turbo", ".purnaOS",
}

WORKING_SNAPSHOT_ID = "working"


def synthetic_snapshot_id(repo_root: Path) -> str:
    """Stable snapshot id for a project directory (no git)."""
    h = hashlib.sha256(str(repo_root.resolve()).encode("utf-8")).hexdigest()[:16]
    return f"local-{h}"


def get_project_identity(repo_root: Path) -> tuple[str, str]:
    """Owner/name for control plane provisioning — directory based."""
    root = repo_root.resolve()
    return "local", root.name


def iter_project_files(repo_root: Path) -> list[str]:
    """Walk the project tree and return relative text-eligible file paths."""
    root = repo_root.resolve()
    files: list[str] = []

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        parts = rel.split("/")
        if any(part in SKIP_WALK_DIRS for part in parts):
            continue
        if parts[-1].startswith(".") and parts[-1] not in {
            ".env.example", ".env.sample", ".env.template",
        }:
            continue
        if should_skip_file(str(path)):
            continue
        files.append(rel)

    return sorted(files)


def read_project_file(repo_root: Path, file_path: str) -> Optional[str]:
    """Read a project file as UTF-8 text; None if missing or binary."""
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


def synthetic_commit_info(
    snapshot_id: str,
    message: str,
    changed_files: list[dict],
) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    active = [f for f in changed_files if f.get("status") != "deleted"]
    return {
        "sha": snapshot_id,
        "message": message,
        "author": "purna",
        "author_email": "purna@local",
        "committed_at": now,
        "parents": [],
        "changed_files": changed_files,
        "commit_summary": f"{message}. Indexed {len(active)} files.",
    }


def compute_text_diff(
    file_path: str,
    old_content: Optional[str],
    new_content: str,
    max_chars: int = 4000,
) -> tuple[str, bool]:
    """Unified diff from last known content; no git."""
    is_new_file = old_content is None
    if is_new_file:
        diff_lines = [f"+{line}" for line in new_content.splitlines()[:200]]
        diff = "\n".join(diff_lines)
    else:
        diff = "".join(
            difflib.unified_diff(
                old_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{file_path}",
                tofile=f"b/{file_path}",
                n=3,
            )
        )
        if not diff.strip():
            diff_lines = [f"+{line}" for line in new_content.splitlines()[:50]]
            diff = "\n".join(diff_lines)

    if len(diff) > max_chars:
        diff = diff[:max_chars] + "\n... [diff truncated] ..."
    return diff, is_new_file


def baseline_content_path(config_local_dir: Path, file_path: str) -> Path:
    from .utils import file_path_hash
    return config_local_dir / "baseline_content" / f"{file_path_hash(file_path)}.txt"


def load_baseline_content(config_local_dir: Path, file_path: str) -> Optional[str]:
    path = baseline_content_path(config_local_dir, file_path)
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def save_baseline_content(config_local_dir: Path, file_path: str, content: str) -> None:
    path = baseline_content_path(config_local_dir, file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
