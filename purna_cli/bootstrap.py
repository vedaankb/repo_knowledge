"""Bootstrap command - create knowledge GitHub repository"""

import asyncio
import json
from pathlib import Path
import httpx
from typing import Optional

from .config import PurnaConfig


async def bootstrap_knowledge_repo(
    repo_name: str,
    github_token: str,
    org: Optional[str] = None,
    private: bool = True,
) -> tuple[bool, str]:
    """
    Create a new GitHub repository for knowledge artifacts
    Initializes it with README and manifest.json template
    
    Args:
        repo_name: Name for the knowledge repository
        github_token: GitHub personal access token with repo scope
        org: Optional organization name (if None, creates under user account)
        private: Whether the repo should be private (default True)
    
    Returns:
        (success, message or error)
    """
    
    # Read template files
    templates_dir = Path(__file__).parent.parent / "templates" / "knowledge_repo"
    readme_content = (templates_dir / "README.md").read_text()
    manifest_content = (templates_dir / "manifest.json").read_text()
    gitignore_content = (templates_dir / ".gitignore").read_text()
    
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Create repository
        if org:
            create_url = f"https://api.github.com/orgs/{org}/repos"
        else:
            create_url = "https://api.github.com/user/repos"
        
        create_payload = {
            "name": repo_name,
            "description": "PurnaOS knowledge repository - processed code artifacts",
            "private": private,
            "auto_init": False,
        }
        
        try:
            resp = await client.post(create_url, headers=headers, json=create_payload)
            
            if resp.status_code == 422:
                return False, f"Repository {repo_name} already exists"
            
            if resp.status_code not in [200, 201]:
                return False, f"Failed to create repository: {resp.status_code} {resp.text}"
            
            repo_data = resp.json()
            full_name = repo_data["full_name"]
            html_url = repo_data["html_url"]
        
        except Exception as e:
            return False, f"Error creating repository: {e}"
        
        # Wait a moment for repo to be ready
        await asyncio.sleep(2)
        
        # Create initial files via Contents API
        base_url = f"https://api.github.com/repos/{full_name}/contents"
        
        files_to_create = [
            ("README.md", readme_content, "Initial commit: Add README"),
            ("manifest.json", manifest_content, "Initial commit: Add manifest"),
            (".gitignore", gitignore_content, "Initial commit: Add .gitignore"),
        ]
        
        for filename, content, message in files_to_create:
            import base64
            content_b64 = base64.b64encode(content.encode()).decode()
            
            payload = {
                "message": message,
                "content": content_b64,
                "branch": "main",
            }
            
            try:
                resp = await client.put(
                    f"{base_url}/{filename}",
                    headers=headers,
                    json=payload
                )
                
                if resp.status_code not in [200, 201]:
                    print(f"Warning: Failed to create {filename}: {resp.status_code}")
            
            except Exception as e:
                print(f"Warning: Error creating {filename}: {e}")
        
        # Create directory structure (via dummy .gitkeep files)
        directories = ["commits", "chunks", "skills", "deleted"]
        
        for dirname in directories:
            gitkeep_b64 = base64.b64encode(b"").decode()
            payload = {
                "message": f"Initial commit: Create {dirname}/ directory",
                "content": gitkeep_b64,
                "branch": "main",
            }
            
            try:
                await client.put(
                    f"{base_url}/{dirname}/.gitkeep",
                    headers=headers,
                    json=payload
                )
            except:
                pass  # Not critical
    
    return True, f"Created knowledge repository: {html_url}"


def cmd_bootstrap_interactive():
    """Interactive bootstrap command"""
    import os
    
    print("PurnaOS Knowledge Repository Bootstrap")
    print("=" * 50)
    print()
    
    # Get inputs
    repo_name = input("Knowledge repository name (e.g., 'myapp-knowledge'): ").strip()
    if not repo_name:
        print("Error: Repository name is required")
        return 1
    
    org = input("GitHub organization (leave empty for personal account): ").strip() or None
    
    private_input = input("Private repository? [Y/n]: ").strip().lower()
    private = private_input != 'n'
    
    github_token = input("GitHub token (with 'repo' scope): ").strip()
    if not github_token:
        github_token = os.getenv("GITHUB_TOKEN", "")
    
    if not github_token:
        print("Error: GitHub token is required")
        return 1
    
    print()
    print("Creating knowledge repository...")
    
    success, message = asyncio.run(bootstrap_knowledge_repo(
        repo_name=repo_name,
        github_token=github_token,
        org=org,
        private=private,
    ))
    
    print()
    if success:
        print(f"✓ {message}")
        print()
        print("Next steps:")
        print("1. Update your .purnaOS/config.yaml with:")
        if org:
            print(f"   knowledge.github: {org}/{repo_name}")
        else:
            print(f"   knowledge.github: <your-username>/{repo_name}")
        print("2. Run 'purna snapshot' to create your first artifact")
        print("3. Run 'purna publish' to push to the knowledge repo")
    else:
        print(f"✗ {message}")
        return 1
    
    return 0
