from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from .config import get_settings
from .db import pool
from .embeddings import embed_query


def _vec_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{v:.7f}" for v in values) + "]"


@dataclass
class CodeHit:
    file: str
    symbol: Optional[str]
    language: Optional[str]
    content: str
    start_line: Optional[int]
    end_line: Optional[int]
    commit_sha: Optional[str]
    score: float
    indexed_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class SkillHit:
    pr_number: int
    title: str
    summary: str
    changed_files: list[str]
    author: Optional[str]
    merged_at: Optional[str]
    score: float


async def retrieve(
    repo_id: UUID,
    query: str,
    *,
    commit_sha: Optional[str] = None,
    file_paths: Optional[list[str]] = None,
) -> tuple[list[CodeHit], list[SkillHit]]:
    settings = get_settings()
    q_vec = await embed_query(query)
    q_lit = _vec_literal(q_vec)

    # Build code SQL dynamically so scopes compose cleanly.
    code_where = ["repo_id = $1"]
    code_params: list = [repo_id, q_lit, settings.top_k_code]
    if commit_sha:
        code_params.append(commit_sha)
        code_where.append(f"commit_sha LIKE ${len(code_params)} || '%'")
    if file_paths:
        code_params.append(list(file_paths))
        code_where.append(f"file = ANY(${len(code_params)}::text[])")

    code_sql = f"""
    SELECT file, symbol, language, content, start_line, end_line, commit_sha,
           indexed_at, updated_at,
           1 - (embedding <=> $2::vector) AS score
    FROM code_chunks
    WHERE {' AND '.join(code_where)}
    ORDER BY embedding <=> $2::vector
    LIMIT $3
    """
    skill_sql = """
    SELECT pr_number, title, summary, changed_files, author, merged_at,
           1 - (embedding <=> $2::vector) AS score
    FROM feature_skills
    WHERE repo_id = $1
    ORDER BY embedding <=> $2::vector
    LIMIT $3
    """
    async with pool().acquire() as conn:
        code_rows = await conn.fetch(code_sql, *code_params)
        skill_rows = await conn.fetch(skill_sql, repo_id, q_lit, settings.top_k_skills)

    code = [
        CodeHit(
            file=r["file"],
            symbol=r["symbol"],
            language=r["language"],
            content=r["content"],
            start_line=r["start_line"],
            end_line=r["end_line"],
            commit_sha=r["commit_sha"],
            score=float(r["score"]),
            indexed_at=r["indexed_at"].isoformat() if r["indexed_at"] else None,
            updated_at=r["updated_at"].isoformat() if r["updated_at"] else None,
        )
        for r in code_rows
        if float(r["score"]) >= settings.min_retrieval_score
    ]
    skills = [
        SkillHit(
            pr_number=r["pr_number"],
            title=r["title"],
            summary=r["summary"],
            changed_files=list(r["changed_files"]) if r["changed_files"] else [],
            author=r["author"],
            merged_at=r["merged_at"].isoformat() if r["merged_at"] else None,
            score=float(r["score"]),
        )
        for r in skill_rows
        if float(r["score"]) >= settings.min_retrieval_score
    ]
    return code, skills


async def retrieve_recent_chunks(repo_id: UUID, limit: int = 8) -> list[CodeHit]:
    """Most recently indexed/updated chunks, for 'what is new' questions."""
    sql = """
    SELECT file, symbol, language, content, start_line, end_line, commit_sha,
           indexed_at, updated_at
    FROM code_chunks
    WHERE repo_id = $1
    ORDER BY GREATEST(indexed_at, updated_at) DESC
    LIMIT $2
    """
    async with pool().acquire() as conn:
        rows = await conn.fetch(sql, repo_id, limit)
    return [
        CodeHit(
            file=r["file"],
            symbol=r["symbol"],
            language=r["language"],
            content=r["content"],
            start_line=r["start_line"],
            end_line=r["end_line"],
            commit_sha=r["commit_sha"],
            score=0.0,
            indexed_at=r["indexed_at"].isoformat() if r["indexed_at"] else None,
            updated_at=r["updated_at"].isoformat() if r["updated_at"] else None,
        )
        for r in rows
    ]
