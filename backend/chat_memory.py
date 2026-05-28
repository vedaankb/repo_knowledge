"""Separate vector store for chat-history RAG.

Stores every user/assistant turn with a Gemini embedding so we can retrieve the
most relevant prior turns by similarity. Each turn is assigned a monotonic
`turn_index` per chat_id, so the LLM can reason about ordering ("the first
question you asked", "earlier in turn 4 you said...").
"""
from __future__ import annotations

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
) -> int:
    """Embed and store a single turn. Returns the assigned turn_index (1-based).

    The turn_index is computed atomically inside the same INSERT so two
    concurrent persists for the same chat_id will get distinct indices
    (subject to standard MVCC; for a single-user POC this is plenty).
    """
    if role not in {"user", "assistant"}:
        raise ValueError(f"invalid role: {role}")
    text = (content or "").strip()
    if not text:
        return 0

    embs = await embed_texts([text[:8000]], task_type="RETRIEVAL_DOCUMENT")
    if not embs:
        return 0

    sql = """
    INSERT INTO chat_turns (chat_id, repo_id, role, content, embedding, turn_index)
    VALUES (
        $1, $2, $3, $4, $5::vector,
        (SELECT COALESCE(MAX(turn_index), 0) + 1
         FROM chat_turns
         WHERE chat_id = $1)
    )
    RETURNING turn_index
    """
    async with pool().acquire() as conn:
        async with conn.transaction():
            idx = await conn.fetchval(
                sql, chat_id, repo_id, role, text, _vec_literal(embs[0])
            )
    return int(idx or 0)


async def retrieve_chat_context(
    chat_id: str,
    question: str,
    *,
    k_semantic: int = 6,
    k_recent: int = 8,
    max_chars_per_turn: int = 1800,
) -> list[dict]:
    """Return prior turns for this chat, deduped, in chronological order.

    Killer-context recipe:
      1) ANCHOR: always include turn #1 (the very first message) — establishes
         what the chat is about and what the user originally asked for.
      2) RECENT: the last `k_recent` turns (short-range follow-up references).
      3) SEMANTIC: top `k_semantic` turns by embedding similarity to the new
         question (long-range memory: "didn't you mention X earlier?").
    Then dedupe by id and sort ascending by turn_index so the LLM reads the
    history in real chronological order.
    """
    q_vec = await embed_query(question)
    q_lit = _vec_literal(q_vec)

    anchor_sql = """
    SELECT id, role, content, created_at, turn_index, 1.0::float AS score
    FROM chat_turns
    WHERE chat_id = $1
    ORDER BY turn_index ASC
    LIMIT 2
    """
    rec_sql = """
    SELECT id, role, content, created_at, turn_index, 1.0::float AS score
    FROM chat_turns
    WHERE chat_id = $1
    ORDER BY turn_index DESC
    LIMIT $2
    """
    sem_sql = """
    SELECT id, role, content, created_at, turn_index,
           1 - (embedding <=> $2::vector) AS score
    FROM chat_turns
    WHERE chat_id = $1
    ORDER BY embedding <=> $2::vector
    LIMIT $3
    """
    async with pool().acquire() as conn:
        anchor_rows = await conn.fetch(anchor_sql, chat_id)
        rec_rows = await conn.fetch(rec_sql, chat_id, k_recent)
        sem_rows = await conn.fetch(sem_sql, chat_id, q_lit, k_semantic)

    by_id: dict = {}
    # Priority: anchor first (lowest score wins doesn't matter since we dedupe by id),
    # then recent, then semantic. Semantic carries a real similarity score; recent
    # and anchor get a synthetic 1.0 so they're trusted absolutely.
    for r in list(anchor_rows) + list(rec_rows) + list(sem_rows):
        if r["id"] not in by_id:
            by_id[r["id"]] = r

    rows = sorted(by_id.values(), key=lambda r: r["turn_index"])
    out: list[dict] = []
    for r in rows:
        content = r["content"]
        if len(content) > max_chars_per_turn:
            content = content[:max_chars_per_turn] + "..."
        out.append({
            "role": r["role"],
            "content": content,
            "turn_index": int(r["turn_index"]),
            "score": float(r["score"]),
        })
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


async def last_turn_index(chat_id: str) -> int:
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COALESCE(MAX(turn_index), 0) AS n FROM chat_turns WHERE chat_id = $1",
            chat_id,
        )
    return int(row["n"]) if row else 0
