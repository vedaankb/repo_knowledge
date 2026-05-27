from __future__ import annotations

import asyncio
from typing import Iterable

import google.generativeai as genai
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import get_settings

_configured = False


def _ensure_configured() -> None:
    global _configured
    if _configured:
        return
    settings = get_settings()
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    genai.configure(api_key=settings.gemini_api_key)
    _configured = True


@retry(stop=stop_after_attempt(4), wait=wait_exponential(min=1, max=8))
def _embed_one(text: str, task_type: str) -> list[float]:
    _ensure_configured()
    settings = get_settings()
    result = genai.embed_content(
        model=settings.gemini_embed_model,
        content=text[:8000],
        task_type=task_type,
    )
    emb = result.get("embedding") if isinstance(result, dict) else getattr(result, "embedding", None)
    if emb is None:
        raise RuntimeError("No embedding returned from Gemini")
    if isinstance(emb, dict) and "values" in emb:
        emb = emb["values"]
    return list(emb)


async def embed_texts(texts: Iterable[str], *, task_type: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
    texts_list = list(texts)
    if not texts_list:
        return []
    settings = get_settings()
    sem = asyncio.Semaphore(8)

    async def one(t: str) -> list[float]:
        async with sem:
            return await asyncio.to_thread(_embed_one, t, task_type)

    return await asyncio.gather(*(one(t) for t in texts_list))


async def embed_query(text: str) -> list[float]:
    [vec] = await embed_texts([text], task_type="RETRIEVAL_QUERY")
    return vec
