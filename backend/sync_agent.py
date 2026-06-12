import asyncio
import json
import logging
import google.generativeai as genai
from typing import Optional

from .api_keys import require_current_gemini_key
from .config import get_settings

log = logging.getLogger(__name__)

SYNC_AGENT_SYSTEM_PROMPT = """You are the PurnaOS LLM Sync Agent, the gatekeeper of a code repository's knowledge base (its "memory").

IMPORTANT: The diff you see is CUMULATIVE — it is everything that has changed in this file since the last time its contents were committed to memory. Changes you skip or defer stay staged and will reappear, combined with future edits, in later evaluations. Nothing is lost by saying no; memory only updates when you say "append".

Your job: decide whether the ACCUMULATED change is now substantial enough to become memory. Only real logical or structural changes belong in memory — not tiny line edits or cosmetic refactors.

Choose one action:
1. "append": The accumulated change is SUBSTANTIAL — new features or functions, meaningful logic changes, API surface changes, bug fixes with behavioral impact, architectural changes, significant config changes, or new dependencies with usage implications. The bar is high: a reader of the knowledge base should genuinely need this to understand the repo.
2. "skip": The accumulated change is still trivial or cosmetic — typos, whitespace, formatting, comments-only edits, renamed locals, tiny refactors with identical behavior, generated files. It can keep accumulating.
3. "defer": The change looks mid-flight — incomplete code, syntax that suggests active typing, or a partial feature that should settle before indexing.

When in doubt between append and skip, choose skip — the change is not lost, and you will re-evaluate it together with future edits.

Return a JSON object:
{
  "action": "append" | "skip" | "defer",
  "reason": "A concise, 1-sentence explanation of why you chose this action.",
  "update_understanding": true | false
}

Set "update_understanding" to true only if the change is so significant that the high-level description/understanding of the repository should be revised.
"""


async def decide_sync_event(
    understanding: str,
    file_path: str,
    diff: str,
    event_type: str = "file_edit",
    is_new_file: bool = False,
) -> dict:
    """
    Evaluate whether a file edit event should be appended, skipped, or deferred.
    """
    settings = get_settings()
    api_key = require_current_gemini_key()
    genai.configure(api_key=api_key)
    
    prompt = f"""
Workspace Understanding: {understanding}
Event Type: {event_type}
File Path: {file_path}
Is New File: {is_new_file}

Unified Diff:
```diff
{diff}
```

This diff is the accumulated change since this file's contents were last committed to memory. Decide whether it is now substantial enough to append. Return a JSON object.
"""
    
    model = genai.GenerativeModel(
        settings.gemini_chat_model,
        system_instruction=SYNC_AGENT_SYSTEM_PROMPT
    )
    
    try:
        resp = await asyncio.to_thread(
            model.generate_content,
            prompt,
            generation_config={
                "response_mime_type": "application/json",
                "temperature": 0.1,
            }
        )
        
        text = resp.text.strip()
        result = json.loads(text)
        
        # Validate keys. Fail closed (defer): the change stays staged and is
        # re-evaluated later, so memory never fills with unvetted edits.
        if "action" not in result or result["action"] not in ("append", "skip", "defer"):
            result["action"] = "defer"
        if "reason" not in result:
            result["reason"] = "Default fallback"
        if "update_understanding" not in result:
            result["update_understanding"] = False
            
        return result
    except Exception as e:
        log.error(f"Error in sync agent decision: {e}")
        return {
            "action": "defer",  # Fail closed: change stays staged for re-evaluation
            "reason": f"Error running sync agent: {e}",
            "update_understanding": False
        }
