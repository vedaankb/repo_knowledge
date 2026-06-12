"""Main CLI entry point"""

import asyncio
import os
import sys
from pathlib import Path
import argparse

from dotenv import load_dotenv

# Load repo_knowledge .env so POC GEMINI_API_KEY is available without export
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from .config import PurnaConfig, find_project_root
from .fs_index import get_project_identity
from .hooks import install_hooks, uninstall_hooks
from .snapshot import create_snapshot
from .publish import publish_to_knowledge_repo, upload_local_artifacts
from .bootstrap import cmd_bootstrap_interactive


def cmd_init(args):
    """Initialize .purnaOS in current project directory"""
    repo_root = find_project_root()
    
    config = PurnaConfig(repo_root)
    
    if config.exists():
        print(f".purnaOS already initialized in {repo_root}")
        return 0
    
    # Create directory structure
    config.ensure_dirs()
    
    # Create default config.yaml
    default_config = {
        "version": 1,
        "source": {
            "remote": "origin",
            "default_branch": "main",
        },
        "knowledge": {
            "github": "",  # User must fill this
            "branch": "main",
        },
        "sync": {
            "debounce_ms": 3000,
            "on_edit": True,
            "on_commit": True,
            "on_push": True,
        },
        "chunk": {
            "reuse_backend_rules": True,
            "max_file_bytes": 400000,
        },
        "embed": {
            "model": "gemini-embedding-001",
            "dimensions": 768,
            "task_type": "RETRIEVAL_DOCUMENT",
        },
    }
    
    config.save(default_config)
    
    print(f"✓ Initialized .purnaOS in {repo_root}")
    print(f"  Edit .purnaOS/config.yaml to configure your knowledge repository")
    
    return 0


def cmd_install(args):
    """Install git hooks (optional; requires git)"""
    repo_root = find_project_root()
    if not (repo_root / ".git").exists():
        print("Error: git hooks require a git repository (.git not found)")
        return 1
    
    config = PurnaConfig(repo_root)
    if not config.exists():
        print("Error: .purnaOS not initialized. Run 'purna init' first")
        return 1
    
    success, message = install_hooks(repo_root)
    print(message)
    return 0 if success else 1


def cmd_uninstall(args):
    """Uninstall git hooks (optional; requires git)"""
    repo_root = find_project_root()
    if not (repo_root / ".git").exists():
        print("Error: git hooks require a git repository (.git not found)")
        return 1
    
    success, message = uninstall_hooks(repo_root)
    print(message)
    return 0 if success else 1


def cmd_snapshot(args):
    """Create filesystem snapshot of the project"""
    repo_root = find_project_root()
    
    config = PurnaConfig(repo_root)
    if not config.exists():
        print("Error: .purnaOS not initialized. Run 'purna init' first")
        return 1
    
    gemini_key = args.gemini_key or os.getenv("GEMINI_API_KEY")
    
    if not args.quiet:
        print("Creating snapshot...")
    
    success, message = asyncio.run(create_snapshot(
        repo_root, config, 
        commit_sha=args.sha,
        gemini_key=gemini_key
    ))
    
    if not args.quiet:
        print(message)
    
    return 0 if success else 1


def cmd_publish(args):
    """Publish artifacts to knowledge repo"""
    repo_root = find_project_root()
    
    config = PurnaConfig(repo_root)
    if not config.exists():
        print("Error: .purnaOS not initialized. Run 'purna init' first")
        return 1
    
    github_token = args.github_token or os.getenv("GITHUB_TOKEN")
    
    if not args.quiet:
        print("Publishing to knowledge repo...")
    
    success, message = asyncio.run(publish_to_knowledge_repo(
        repo_root, config,
        github_token=github_token,
        force=args.force
    ))
    
    if not args.quiet:
        print(message)
    
    return 0 if success else 1


def cmd_status(args):
    """Show purna status"""
    repo_root = find_project_root()
    
    config = PurnaConfig(repo_root)
    if not config.exists():
        print(".purnaOS not initialized")
        return 1
    
    cfg = config.load()
    state = config.load_state()
    
    workspace_id = cfg.get("workspace_id")
    repo_id = cfg.get("repo_id")
    api_url = cfg.get("api_url", "http://localhost:8000")
    
    print(f"PurnaOS Status")
    print(f"  Repo root: {repo_root}")
    if workspace_id:
        print(f"  Workspace ID: {workspace_id}")
    if repo_id:
        print(f"  Repository ID: {repo_id}")
    if workspace_id:
        print(f"  Connect URL: {api_url}/?workspace_id={workspace_id}")
    print(f"  Knowledge repo: {cfg.get('knowledge', {}).get('github', 'not configured')}")
    print(f"  Last published: {state.get('last_published_sha', 'never')[:8] if state.get('last_published_sha') else 'never'}")
    print(f"  Pending files: {len(state.get('pending_files', []))}")
    print(f"  Staging count: {state.get('staging_count', 0)}")
    
    return 0


def cmd_watch(args):
    """Watch project directory for changes"""
    repo_root = find_project_root()
    
    config = PurnaConfig(repo_root)
    if not config.exists():
        print("Error: .purnaOS not initialized. Run 'purna init' first")
        return 1
    
    gemini_key = args.gemini_key or os.getenv("GEMINI_API_KEY")
    if not gemini_key:
        print("Error: GEMINI_API_KEY not set. Export it or pass --gemini-key")
        return 1
    
    from .watch import watch_repository
    
    asyncio.run(watch_repository(repo_root, config, gemini_key))
    
    return 0


def probe_repository(repo_root: Path) -> dict:
    probe = {
        "default_branch": "main",
        "visibility": "private",
        "readme_excerpt": "",
        "languages": [],
        "layout": []
    }

    readme_path = repo_root / "README.md"
    if not readme_path.exists():
        readme_path = repo_root / "readme.md"
    if readme_path.exists():
        try:
            with open(readme_path, "r", encoding="utf-8", errors="ignore") as f:
                probe["readme_excerpt"] = f.read(1000)
        except Exception:
            pass
            
    # Simple language probe based on file extensions
    extensions = {
        ".py": "Python",
        ".js": "JavaScript",
        ".ts": "TypeScript",
        ".tsx": "TypeScript",
        ".jsx": "JavaScript",
        ".go": "Go",
        ".rs": "Rust",
        ".java": "Java",
        ".cpp": "C++",
        ".c": "C",
        ".rb": "Ruby",
        ".php": "PHP",
    }
    found_langs = set()
    try:
        for p in repo_root.glob("*"):
            if p.is_dir() and not p.name.startswith("."):
                probe["layout"].append(p.name)
                # Scan subdirectories briefly
                for sub in p.glob("*"):
                    if sub.is_file() and sub.suffix in extensions:
                        found_langs.add(extensions[sub.suffix])
            elif p.is_file() and not p.name.startswith("."):
                probe["layout"].append(p.name)
                if p.suffix in extensions:
                    found_langs.add(extensions[p.suffix])
    except Exception:
        pass
    probe["languages"] = list(found_langs)
    return probe


def cmd_understand(args):
    """Run unified onboarding with purna understand"""
    repo_root = find_project_root()
    config = PurnaConfig(repo_root)
    config.ensure_dirs()
    
    # 1. Resolve PurnaOS Token
    purna_token = args.purna_token or os.getenv("PURNA_TOKEN")
    if not purna_token:
        try:
            purna_token = input("Enter your PurnaOS Token: ").strip()
        except KeyboardInterrupt:
            print("\nCancelled.")
            return 1
    if not purna_token:
        print("Error: PurnaOS Token is required")
        return 1
        
    # 2. Resolve Gemini API Key
    gemini_key = args.gemini_key or os.getenv("GEMINI_API_KEY")
    if not gemini_key:
        try:
            gemini_key = input("Enter your Gemini API Key: ").strip()
        except KeyboardInterrupt:
            print("\nCancelled.")
            return 1
    if not gemini_key:
        print("Error: Gemini API Key is required")
        return 1
        
    api_url = args.api_url or os.getenv("PURNA_API_URL", "http://localhost:8000")
    
    # 3. Probe project directory (filesystem — no git)
    repo_owner, repo_name = get_project_identity(repo_root)
    probe_data = probe_repository(repo_root)
    
    # 4. Call POST /api/purna/understand
    import httpx
    print("Connecting to PurnaOS control plane...")
    try:
        resp = httpx.post(
            f"{api_url}/api/purna/understand",
            json={
                "purna_token": purna_token,
                "repo_owner": repo_owner,
                "repo_name": repo_name,
                "gemini_key": gemini_key,
                "probe_data": probe_data
            },
            timeout=30.0
        )
        if resp.status_code != 200:
            print(f"Error from control plane: {resp.text}")
            return 1
        data = resp.json()
    except Exception as e:
        print(f"Error connecting to control plane: {e}")
        return 1
        
    workspace_id = data["workspace_id"]
    repo_id = data["repo_id"]
    org_id = data["org_id"]
    
    # 5. Write workspace.yaml
    workspace_config = {
        "version": 2,
        "workspace_id": workspace_id,
        "repo_id": repo_id,
        "org_id": org_id,
        "api_url": api_url,
        "watch": {
            "enabled": True,
            "debounce_ms": data["sync"]["debounce_ms"]
        },
        "sync": {
            "mode": data["sync"]["mode"]
        }
    }
    config.save_workspace(workspace_config)
    
    # Also save credentials securely in state.json
    state = config.load_state()
    state["purna_token"] = purna_token
    state["gemini_key"] = gemini_key
    config.save_state(state)
    
    print(f"✓ Provisioned workspace: {workspace_id}")
    print(f"✓ Linked repository ID:  {repo_id}")
    print(f"🔗 Connect to this workspace in your browser: {api_url}/?workspace_id={workspace_id}")
    
    # 6. Run Baseline Index
    print("🚀 Running initial baseline index...")
    success, message = asyncio.run(create_snapshot(
        repo_root, config,
        commit_sha=None,
        gemini_key=gemini_key,
        all_tracked=True
    ))
    print(message)
    if not success:
        print("Error: Baseline snapshot creation failed")
        return 1
        
    # 7. Upload Baseline Artifacts
    print("📤 Uploading baseline artifacts to PurnaOS...")
    success, message = asyncio.run(
        upload_local_artifacts(repo_root, config, api_url, purna_token, gemini_key)
    )
    print(message)
    if not success:
        print("Error: Baseline artifact upload failed")
        return 1
        
    # 8. Start Watch Daemon
    print("👁  Starting purna watch daemon...")
    if args.background:
        import subprocess
        try:
            subprocess.Popen(
                [sys.executable, "-m", "purna_cli", "watch", "--gemini-key", gemini_key],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )
            print("✓ Watch daemon started in background.")
        except Exception as e:
            print(f"Error starting watch daemon in background: {e}")
            return 1
    else:
        from .watch import watch_repository
        try:
            asyncio.run(watch_repository(repo_root, config, gemini_key))
        except KeyboardInterrupt:
            print("\nStopped watch daemon.")
            
    return 0


def cmd_bootstrap(args):
    """Bootstrap a new knowledge repository"""
    return cmd_bootstrap_interactive()


def cmd_delete(args):
    """Delete PurnaOS configuration and local artifacts"""
    repo_root = find_project_root()
    
    config = PurnaConfig(repo_root)
    if not config.exists():
        print("PurnaOS is not initialized or has already been removed from this repository.")
        return 0
        
    # Ask for confirmation unless --yes is passed
    if not args.yes:
        try:
            confirm = input("⚠️  Are you sure you want to delete the PurnaOS configuration and local database artifacts from this repository? (y/N): ").strip().lower()
            if confirm not in ("y", "yes"):
                print("Aborted.")
                return 0
        except KeyboardInterrupt:
            print("\nAborted.")
            return 0
            
    # Uninstall git hooks if they exist
    if (repo_root / ".git").exists():
        try:
            uninstall_hooks(repo_root)
        except Exception:
            pass
            
    # Delete .purnaOS directory recursively
    import shutil
    try:
        shutil.rmtree(config.purna_dir)
        print("✓ Successfully removed .purnaOS configuration and cleared local artifacts.")
        return 0
    except Exception as e:
        print(f"Error removing .purnaOS directory: {e}")
        return 1


def main():
    """Main CLI entry point"""
    parser = argparse.ArgumentParser(
        prog="purna",
        description="PurnaOS CLI - Client-driven repository knowledge builder"
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Commands")
    
    # init
    parser_init = subparsers.add_parser("init", help="Initialize .purnaOS in repository")
    parser_init.set_defaults(func=cmd_init)
    
    # understand
    parser_understand = subparsers.add_parser("understand", help="Run unified onboarding with purna understand")
    parser_understand.add_argument("--purna-token", help="PurnaOS token")
    parser_understand.add_argument("--gemini-key", help="Gemini API key")
    parser_understand.add_argument("--api-url", help="PurnaOS API URL")
    parser_understand.add_argument("--background", "-b", action="store_true", help="Run watch daemon in background")
    parser_understand.set_defaults(func=cmd_understand)
    
    # install
    parser_install = subparsers.add_parser("install", help="Install git hooks")
    parser_install.set_defaults(func=cmd_install)
    
    # uninstall
    parser_uninstall = subparsers.add_parser("uninstall", help="Uninstall git hooks")
    parser_uninstall.set_defaults(func=cmd_uninstall)
    
    # snapshot
    parser_snapshot = subparsers.add_parser("snapshot", help="Create commit snapshot")
    parser_snapshot.add_argument("--sha", help="Commit SHA (default: HEAD)")
    parser_snapshot.add_argument("--gemini-key", help="Gemini API key")
    parser_snapshot.add_argument("--quiet", "-q", action="store_true", help="Suppress output")
    parser_snapshot.set_defaults(func=cmd_snapshot)
    
    # publish
    parser_publish = subparsers.add_parser("publish", help="Publish artifacts to knowledge repo")
    parser_publish.add_argument("--github-token", help="GitHub token")
    parser_publish.add_argument("--force", "-f", action="store_true", help="Force publish")
    parser_publish.add_argument("--quiet", "-q", action="store_true", help="Suppress output")
    parser_publish.set_defaults(func=cmd_publish)
    
    # status
    parser_status = subparsers.add_parser("status", help="Show purna status")
    parser_status.set_defaults(func=cmd_status)
    
    # watch
    parser_watch = subparsers.add_parser("watch", help="Watch repository for changes")
    parser_watch.add_argument("--gemini-key", help="Gemini API key")
    parser_watch.set_defaults(func=cmd_watch)
    
    # bootstrap
    parser_bootstrap = subparsers.add_parser("bootstrap", help="Create new knowledge repository on GitHub")
    parser_bootstrap.set_defaults(func=cmd_bootstrap)
    
    # delete
    parser_delete = subparsers.add_parser("delete", help="Delete PurnaOS configuration and local artifacts")
    parser_delete.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")
    parser_delete.set_defaults(func=cmd_delete)
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return 1
    
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
