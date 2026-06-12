"""Per-request Gemini API key resolution.

Priority: X-Gemini-Key header → per-repo stored key → GEMINI_API_KEY in .env
(POC default; per-user keys in the UI later).
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

from .config import get_settings

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


def get_env_gemini_key() -> str:
    return (get_settings().gemini_api_key or "").strip()


def get_current_gemini_key() -> str:
    """Return the active Gemini key, or empty string if not configured."""
    return (_current_key.get() or get_env_gemini_key() or "").strip()


class KeyNotConfiguredError(RuntimeError):
    """Raised by embedding / chat code when no Gemini key is in context."""

    def __init__(self) -> None:
        super().__init__(KEY_NOT_CONFIGURED_MESSAGE)


def require_current_gemini_key() -> str:
    key = get_current_gemini_key()
    if not key:
        raise KeyNotConfiguredError()
    return key
