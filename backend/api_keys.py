"""Per-request Gemini API key resolution.

The user pastes their own Gemini key into the sidebar; the browser ships it
on every request as `X-Gemini-Key`. A FastAPI middleware copies the header
into this ContextVar. Background tasks (initial index, zip index, scheduler)
explicitly set this ContextVar from the per-repo snapshot saved at register
time. There is NO `.env` fallback by design — each user brings their own key
so we never burn a shared quota.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

_current_key: ContextVar[Optional[str]] = ContextVar(
    "current_gemini_key", default=None
)

KEY_NOT_CONFIGURED_MESSAGE = (
    "Gemini API key is not configured. Open the sidebar, expand "
    "\"Gemini API key\", paste your Google AI Studio key, hit Save, then retry. "
    "Keys are stored in your browser only and sent per-request as X-Gemini-Key."
)


def set_current_gemini_key(key: Optional[str]) -> None:
    """Override the active Gemini key for this asyncio task / thread context."""
    _current_key.set((key or "").strip() or None)


def get_current_gemini_key() -> str:
    """Return the active Gemini key, or empty string if not configured.

    Caller is responsible for raising a clear error if empty AND a Gemini
    API call is actually needed.
    """
    return (_current_key.get() or "").strip()


class KeyNotConfiguredError(RuntimeError):
    """Raised by embedding / chat code when no Gemini key is in context."""

    def __init__(self) -> None:
        super().__init__(KEY_NOT_CONFIGURED_MESSAGE)


def require_current_gemini_key() -> str:
    key = get_current_gemini_key()
    if not key:
        raise KeyNotConfiguredError()
    return key
