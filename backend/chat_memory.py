"""Separate vector store for chat-history RAG.

Stores each user/assistant turn with a Gemini embedding so we can retrieve the
most relevant prior turns by similarity. This keeps the LLM prompt bounded
regardless of how long the conversation grows.
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

from .db import pool
from .embeddings import embed_query, embed_texts


def _vec_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{v:.7f}" for v in values) + "]"


async def persist_turn(
    chat_id: str,
    repo_id: UUID,
    role: str,
    content: str,
) -> None:
    """Embed and store a single turn. Safe to call after the answer is finalized."""
    if role not in {"user", "assistant"}:
        raise ValueError(f"invalid role: {role}")
    text = (content or "").strip()
    if not text:
        return
    embs = await embed_texts([text[:8000]], task_type="RETRIEVAL_DOCUMENT")
    if not embs:
        return
    sql = """
    INSERT INTO chat_turns (chat_id, repo_id, role, content, embedding)
    VALUES ($1, $2, $3, $4, $5::vector)
    """
    async with pool().acquire() as conn:
        await conn.execute(sql, chat_id, repo_id, role, text, _vec_literal(embs[0]))


async def retrieve_chat_context(
    chat_id: str,
    question: str,
    *,
    k_semantic: int = 4,
    k_recent: int = 4,
    max_chars_per_turn: int = 1200,
) -> list[dict]:
    """Return prior turns for this chat, deduped, in chronological order.

    Combines:
      - top-k semantically similar prior turns (long-range memory)
      - last-k recent turns by time (short-range follow-up references)
    """
    q_vec = await embed_query(question)
    q_lit = _vec_literal(q_vec)

    sem_sql = """
    SELECT id, role, content, created_at,
           1 - (embedding <=> $2::vector) AS score
    FROM chat_turns
    WHERE chat_id = $1
    ORDER BY embedding <=> $2::vector
    LIMIT $3
    """
    rec_sql = """
    SELECT id, role, content, created_at, 1.0::float AS score
    FROM chat_turns
    WHERE chat_id = $1
    ORDER BY created_at DESC
    LIMIT $2
    """
    async with pool().acquire() as conn:
        sem_rows = await conn.fetch(sem_sql, chat_id, q_lit, k_semantic)
        rec_rows = await conn.fetch(rec_sql, chat_id, k_recent)

    by_id: dict = {}
    for r in list(sem_rows) + list(rec_rows):
        if r["id"] not in by_id:
            by_id[r["id"]] = r

    rows = list(by_id.values())
    rows.sort(key=lambda r: r["created_at"])
    out: list[dict] = []
    for r in rows:
        content = r["content"]
        if len(content) > max_chars_per_turn:
            content = content[:max_chars_per_turn] + "..."
        out.append({"role": r["role"], "content": content})
    return out


async def delete_chat(chat_id: str) -> int:
    async with pool().acquire() as conn:
        result = await conn.execute(
            "DELETE FROM chat_turns WHERE chat_id = $1", chat_id
        )
    try:
        return int(result.split()[-1])
    except Exception:
        return 0


async def count_turns(chat_id: str) -> int:
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS n FROM chat_turns WHERE chat_id = $1", chat_id
        )
    return int(row["n"]) if row else 0
