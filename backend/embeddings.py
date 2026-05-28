"""Embedding via Gemini REST v1 (not v1beta).

google-generativeai 0.8.x hardcodes v1beta, which does not serve
text-embedding-004. We call the v1 REST endpoint directly with httpx so we
are not pinned to the SDK's API version.
"""
from __future__ import annotations

import asyncio
from typing import Iterable

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from .api_keys import (
    KeyNotConfiguredError,
    get_current_gemini_key,
    require_current_gemini_key,
)
from .config import get_settings

_EMBED_BASE = "https://generativelanguage.googleapis.com/v1/models"

_http: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=60.0))
    return _http


def _model_id(model: str) -> str:
    """Strip leading 'models/' if already present, return bare id."""
    return model.removeprefix("models/").removeprefix("tunedModels/")


@retry(stop=stop_after_attempt(4), wait=wait_exponential(min=1, max=8))
async def _embed_one(text: str, task_type: str) -> list[float]:
    settings = get_settings()
    api_key = require_current_gemini_key()

    model_id = _model_id(settings.gemini_embed_model)
    url = f"{_EMBED_BASE}/{model_id}:embedContent"
    payload = {
        "model": f"models/{model_id}",
        "content": {"parts": [{"text": text[:8000]}]},
        "taskType": task_type,
        "outputDimensionality": get_settings().embedding_dim,
    }
    headers = {"x-goog-api-key": api_key}

    r = await _client().post(url, json=payload, headers=headers)
    if r.status_code == 404:
        raise RuntimeError(
            f"Embedding model '{model_id}' not found on Gemini v1 API. "
            "Check GEMINI_EMBED_MODEL in .env (e.g. text-embedding-004)."
        )
    r.raise_for_status()
    data = r.json()
    values = data.get("embedding", {}).get("values")
    if not values:
        raise RuntimeError(f"No embedding values in response: {data}")
    return list(values)


async def embed_texts(
    texts: Iterable[str], *, task_type: str = "RETRIEVAL_DOCUMENT"
) -> list[list[float]]:
    texts_list = list(texts)
    if not texts_list:
        return []
    settings = get_settings()
    sem = asyncio.Semaphore(8)

    async def one(t: str) -> list[float]:
        async with sem:
            return await _embed_one(t, task_type)

    return await asyncio.gather(*(one(t) for t in texts_list))


async def embed_query(text: str) -> list[float]:
    [vec] = await embed_texts([text], task_type="RETRIEVAL_QUERY")
    return vec


def _ensure_configured() -> None:
    """Legacy shim — validates a key is present in the current request context."""
    require_current_gemini_key()
