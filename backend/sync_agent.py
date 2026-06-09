import asyncio
import json
import logging
import google.generativeai as genai
from typing import Optional

from .api_keys import require_current_gemini_key
from .config import get_settings

log = logging.getLogger(__name__)

SYNC_AGENT_SYSTEM_PROMPT = """You are the PurnaOS LLM Sync Agent, a knowledge gatekeeper for a code repository.
Your job is to analyze file edits/diffs and decide whether they are worth appending to the repository's knowledge base.

You must choose one of the following actions:
1. "append": The change contains meaningful logic, API surface changes, new features, bug fixes with behavioral impact, configuration updates, architectural changes, or new dependencies with usage implications.
2. "skip": The change is trivial and has no semantic value for understanding the repository. Examples: typos, whitespace changes, formatting, comments-only changes, generated lockfiles, test-only tweaks with no production impact, or reverts of prior skips.
3. "defer": The change looks like an incomplete editing session (e.g. watch fired mid-typing), or a small change that is likely part of a larger in-progress feature that should settle before indexing.

You must return a JSON object with the following fields:
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

Please analyze this change and decide on the action (append, skip, or defer). Return a JSON object.
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
        
        # Validate keys
        if "action" not in result or result["action"] not in ("append", "skip", "defer"):
            result["action"] = "append"  # Default safe fallback
        if "reason" not in result:
            result["reason"] = "Default fallback"
        if "update_understanding" not in result:
            result["update_understanding"] = False
            
        return result
    except Exception as e:
        log.error(f"Error in sync agent decision: {e}")
        return {
            "action": "append",  # Safe fallback
            "reason": f"Error running sync agent: {e}",
            "update_understanding": False
        }
