"""Configuration management for .purnaOS directory"""

import json
from pathlib import Path
from typing import Optional
import yaml


class PurnaConfig:
    """Manages .purnaOS/config.yaml"""
    
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self.purna_dir = repo_root / ".purnaOS"
        self.config_path = self.purna_dir / "config.yaml"
        self.workspace_path = self.purna_dir / "workspace.yaml"
        self.local_dir = self.purna_dir / "local"
        self.staging_dir = self.local_dir / "staging"
        self.state_path = self.local_dir / "state.json"
        self.hooks_dir = self.purna_dir / "hooks"
        
    def exists(self) -> bool:
        """Check if .purnaOS is initialized"""
        return self.purna_dir.exists() and (self.config_path.exists() or self.workspace_path.exists())
    
    def load(self) -> dict:
        """Load config.yaml or workspace.yaml"""
        if self.workspace_path.exists():
            return self.load_workspace()
        if not self.config_path.exists():
            raise FileNotFoundError(f".purnaOS/config.yaml or workspace.yaml not found in {self.repo_root}")
        with open(self.config_path) as f:
            return yaml.safe_load(f) or {}
    
    def save(self, config: dict):
        """Save config.yaml"""
        self.purna_dir.mkdir(exist_ok=True)
        with open(self.config_path, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    def load_workspace(self) -> dict:
        """Load workspace.yaml"""
        if not self.workspace_path.exists():
            raise FileNotFoundError(f".purnaOS/workspace.yaml not found in {self.repo_root}")
        with open(self.workspace_path) as f:
            return yaml.safe_load(f) or {}

    def save_workspace(self, config: dict):
        """Save workspace.yaml"""
        self.purna_dir.mkdir(exist_ok=True)
        with open(self.workspace_path, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
    
    def load_state(self) -> dict:
        """Load local/state.json"""
        if not self.state_path.exists():
            return {
                "schema_version": 1,
                "last_published_sha": None,
                "last_published_at": None,
                "pending_files": [],
                "staging_count": 0,
            }
        with open(self.state_path) as f:
            return json.load(f)
    
    def save_state(self, state: dict):
        """Save local/state.json"""
        self.local_dir.mkdir(parents=True, exist_ok=True)
        with open(self.state_path, "w") as f:
            json.dump(state, f, indent=2)
    
    def ensure_dirs(self):
        """Create .purnaOS directory structure"""
        self.purna_dir.mkdir(exist_ok=True)
        self.local_dir.mkdir(exist_ok=True)
        self.staging_dir.mkdir(exist_ok=True)
        self.hooks_dir.mkdir(exist_ok=True)
        
        # Create .gitignore for local/
        gitignore_path = self.purna_dir / ".gitignore"
        if not gitignore_path.exists():
            gitignore_path.write_text("local/\n")


def find_repo_root(start_path: Optional[Path] = None) -> Optional[Path]:
    """Find git repository root by looking for .git directory"""
    if start_path is None:
        start_path = Path.cwd()
    
    current = start_path.resolve()
    while current != current.parent:
        if (current / ".git").exists():
            return current
        current = current.parent
    return None
