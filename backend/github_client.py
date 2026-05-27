from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import AsyncIterator, Optional

import httpx

GITHUB_API = "https://api.github.com"


_REPO_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com[/:]([^/\s]+)/([^/\s#?]+?)(?:\.git)?/?$"
)


def parse_repo_url(url: str) -> tuple[str, str]:
    url = url.strip()
    m = _REPO_URL_RE.match(url)
    if not m:
        if "/" in url and len(url.split("/")) == 2:
            owner, name = url.split("/")
            return owner.strip(), name.strip()
        raise ValueError(f"Could not parse GitHub repo URL: {url}")
    return m.group(1), m.group(2)


@dataclass
class RepoInfo:
    owner: str
    name: str
    default_branch: str
    visibility: str
    head_sha: str


class GitHubClient:
    def __init__(self, token: Optional[str] = None) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "repo-knowledge-poc",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            base_url=GITHUB_API,
            headers=headers,
            timeout=httpx.Timeout(30.0, read=60.0),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "GitHubClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def get_repo(self, owner: str, name: str) -> RepoInfo:
        r = await self._client.get(f"/repos/{owner}/{name}")
        r.raise_for_status()
        data = r.json()
        default_branch = data["default_branch"]
        head_sha = await self.get_branch_head_sha(owner, name, default_branch)
        return RepoInfo(
            owner=data["owner"]["login"],
            name=data["name"],
            default_branch=default_branch,
            visibility=data.get("visibility", "public"),
            head_sha=head_sha,
        )

    async def get_branch_head_sha(self, owner: str, name: str, branch: str) -> str:
        r = await self._client.get(f"/repos/{owner}/{name}/branches/{branch}")
        r.raise_for_status()
        return r.json()["commit"]["sha"]

    async def list_tree(
        self, owner: str, name: str, sha: str
    ) -> list[dict]:
        r = await self._client.get(
            f"/repos/{owner}/{name}/git/trees/{sha}",
            params={"recursive": "1"},
        )
        r.raise_for_status()
        data = r.json()
        return [t for t in data.get("tree", []) if t.get("type") == "blob"]

    async def get_blob(self, owner: str, name: str, sha: str) -> Optional[bytes]:
        r = await self._client.get(f"/repos/{owner}/{name}/git/blobs/{sha}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        if data.get("encoding") == "base64":
            try:
                return base64.b64decode(data["content"])
            except Exception:
                return None
        content = data.get("content")
        return content.encode("utf-8") if isinstance(content, str) else None

    async def compare(
        self, owner: str, name: str, base_sha: str, head_sha: str
    ) -> dict:
        r = await self._client.get(
            f"/repos/{owner}/{name}/compare/{base_sha}...{head_sha}"
        )
        r.raise_for_status()
        return r.json()

    async def list_merged_prs(
        self,
        owner: str,
        name: str,
        since_iso: Optional[str] = None,
        max_pages: int = 10,
    ) -> AsyncIterator[dict]:
        page = 1
        while page <= max_pages:
            r = await self._client.get(
                f"/repos/{owner}/{name}/pulls",
                params={
                    "state": "closed",
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": 50,
                    "page": page,
                },
            )
            r.raise_for_status()
            items = r.json()
            if not items:
                return
            stop = False
            for pr in items:
                if not pr.get("merged_at"):
                    continue
                if since_iso and pr["merged_at"] <= since_iso:
                    stop = True
                    continue
                yield pr
            if stop:
                return
            page += 1

    async def list_pr_files(
        self, owner: str, name: str, pr_number: int
    ) -> list[dict]:
        files: list[dict] = []
        page = 1
        while True:
            r = await self._client.get(
                f"/repos/{owner}/{name}/pulls/{pr_number}/files",
                params={"per_page": 100, "page": page},
            )
            r.raise_for_status()
            items = r.json()
            if not items:
                break
            files.extend(items)
            if len(items) < 100:
                break
            page += 1
        return files
