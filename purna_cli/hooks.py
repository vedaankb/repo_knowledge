"""Git hooks management"""

from pathlib import Path


POST_COMMIT_HOOK = """#!/usr/bin/env bash
# PurnaOS post-commit hook - creates commit snapshot

# Only run if purna is available
if command -v purna &> /dev/null; then
    purna snapshot --quiet || true
fi
"""


PRE_PUSH_HOOK = """#!/usr/bin/env bash
# PurnaOS pre-push hook - publishes artifacts to knowledge repo

# Only run if purna is available
if command -v purna &> /dev/null; then
    purna publish --quiet || true
fi
"""


def install_hooks(repo_root: Path) -> tuple[bool, str]:
    """
    Install git hooks for purna automation
    Returns (success, message)
    """
    git_hooks_dir = repo_root / ".git" / "hooks"
    
    if not git_hooks_dir.exists():
        return False, "No .git/hooks directory found. Is this a git repository?"
    
    hooks_to_install = {
        "post-commit": POST_COMMIT_HOOK,
        "pre-push": PRE_PUSH_HOOK,
    }
    
    installed = []
    skipped = []
    
    for hook_name, hook_content in hooks_to_install.items():
        hook_path = git_hooks_dir / hook_name
        
        if hook_path.exists():
            # Check if it's already a purna hook
            existing_content = hook_path.read_text()
            if "PurnaOS" in existing_content:
                skipped.append(hook_name)
                continue
            
            # Backup existing hook
            backup_path = git_hooks_dir / f"{hook_name}.backup"
            backup_path.write_text(existing_content)
            skipped.append(f"{hook_name} (backed up)")
        
        # Write hook
        hook_path.write_text(hook_content)
        hook_path.chmod(0o755)  # Make executable
        installed.append(hook_name)
    
    message = f"Installed hooks: {', '.join(installed)}"
    if skipped:
        message += f"\nSkipped (already exists): {', '.join(skipped)}"
    
    return True, message


def uninstall_hooks(repo_root: Path) -> tuple[bool, str]:
    """Remove purna git hooks"""
    git_hooks_dir = repo_root / ".git" / "hooks"
    
    if not git_hooks_dir.exists():
        return False, "No .git/hooks directory found"
    
    removed = []
    for hook_name in ["post-commit", "pre-push"]:
        hook_path = git_hooks_dir / hook_name
        
        if hook_path.exists():
            content = hook_path.read_text()
            if "PurnaOS" in content:
                hook_path.unlink()
                removed.append(hook_name)
                
                # Restore backup if exists
                backup_path = git_hooks_dir / f"{hook_name}.backup"
                if backup_path.exists():
                    backup_path.rename(hook_path)
    
    if removed:
        return True, f"Removed hooks: {', '.join(removed)}"
    return True, "No purna hooks found"
