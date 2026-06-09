from __future__ import annotations

import asyncio
import re
from dataclasses import asdict
from typing import Optional
from uuid import UUID



import google.generativeai as genai

from .api_keys import require_current_gemini_key
from .chat_memory import last_turn_index, persist_turn, retrieve_chat_context
from .config import get_settings
from .embeddings import _ensure_configured
from .retriever import CodeHit, SkillHit, retrieve


REFUSAL_TEXT = (
    "I don't have enough information from the indexed repository context to answer that. "
    "Try rephrasing, ask about a specific file/function/PR, or trigger a re-index."
)

PLAN_SYSTEM_PROMPT = """You are a planning assistant for a software project.

You may combine three sources to answer:
  1. CONTEXT chunks from the indexed repo (use first when relevant)
  2. PRIOR conversation in this chat
  3. Your own general programming knowledge, security best practices, and any
     Google Search results the runtime exposes via tools

GUIDELINES:
- Lead with what you find in CONTEXT and PRIOR. Cite repo evidence as
  [file.py:func L10-L40 @sha] or [PR #123].
- Clearly mark additions from outside the repo with "(general knowledge)",
  "(industry practice)", or "(web)" so the dev can see what is repo-specific
  vs. general.
- Be concrete: propose ordered steps, name real files/symbols from CONTEXT,
  suggest commands.
- For vulnerability / security / hardening questions: walk through likely
  weaknesses given the visible code plus standard threats for that stack
  (e.g., OWASP top 10 categories that apply here).
- Do NOT invent files or symbols that aren't in CONTEXT; only reason about them.

CHAT MEMORY:
- PRIOR CONVERSATION lists past messages tagged [turn N · role]. Treat turn 1
  as the chat anchor. Refer back to specific turns when relevant.
- Don't invent earlier turns not present.
"""

SYSTEM_PROMPT = """You are a strict repository-grounded assistant for a single GitHub repo.

HARD RULES:
- Use ONLY the provided CONTEXT (code chunks and PR/feature notes) from THIS repo.
- Do NOT use general programming knowledge, library knowledge, framework defaults,
  best practices, or anything outside CONTEXT — even if you are confident.
- Do NOT speculate, infer beyond what is shown, or fill in missing steps.
- If CONTEXT does not contain enough to answer, reply with exactly:
  "I don't have enough information from the indexed repository context to answer that."
- Cite every concrete claim. ALWAYS include the chunk's commit SHA when present, e.g.
  [file.py:func L10-L40 @a1b2c3d] or [PR #123 @merge_sha]. If a chunk has no
  commit SHA (uploaded zip), cite without @sha.
- If the user has scoped the chat to a specific commit, mention that scope once at
  the top of your answer.

CHAT MEMORY:
- The PRIOR CONVERSATION block lists prior messages from THIS chat only, tagged
  [turn N · role]. Turn 1 is always the chat's anchor. Use these for resolving
  references like "the file we just looked at", "your earlier answer in turn 4",
  "redo it for the other one". When you reference an earlier moment, cite the
  turn number, e.g. "as discussed in turn 3".
- Never invent earlier turns. If the user references something that isn't in
  the shown history, say you don't see it and ask them to repeat it.

ANSWER STYLE:
- For "what is this repo / overview" questions: synthesize from README/AGENTS/ARCHITECTURE
  chunks and the most recent PR/feature notes. Describe what THIS repo does, not
  the technology in general.
- For procedural questions (how to run / build / test / deploy / install locally):
  synthesize a step-by-step answer ONLY from files in CONTEXT such as README,
  Dockerfile, docker-compose, Makefile, package.json scripts, pyproject.toml,
  requirements.txt, .env.example, shell scripts, etc. Quote exact commands/paths
  from those files. Do NOT invent commands.
- For "where is X" / "how does X work" questions: lean on code chunks; name files,
  symbols, and line ranges.
- For "why was X changed / when was bug Y fixed" questions: lean on PR/feature notes.
- If multiple sources disagree, say so and cite both.
- Be concise. Use bullet points and fenced code blocks for commands.
"""


def _format_code_context(hits: list[CodeHit]) -> str:
    if not hits:
        return "(no code chunks)"
    lines: list[str] = []
    for h in hits:
        header = f"[{h.file}"
        if h.symbol:
            header += f":{h.symbol}"
        if h.start_line and h.end_line:
            header += f" L{h.start_line}-L{h.end_line}"
        if h.commit_sha:
            header += f" @{h.commit_sha[:7]}"
        header += f"]  (score={h.score:.2f})"
        lines.append(header)
        lines.append(h.content)
        lines.append("---")
    return "\n".join(lines)


def _format_skill_context(hits: list[SkillHit]) -> str:
    if not hits:
        return "(no PR/feature notes)"
    lines: list[str] = []
    for h in hits:
        lines.append(f"[PR #{h.pr_number}] {h.title}  (score={h.score:.2f})")
        lines.append(h.summary)
        lines.append("---")
    return "\n".join(lines)


def _format_history(history: list[dict], current_turn_idx: int | None = None) -> str:
    """Render prior turns with their per-chat sequence number.

    Lines look like: `[turn 3 · user] how does auth flow?`
    """
    if not history:
        return "(no prior turns)"
    lines: list[str] = []
    for turn in history:
        role = turn.get("role", "user")
        content = (turn.get("content") or "").strip()
        if not content:
            continue
        if len(content) > 1600:
            content = content[:1600] + "..."
        idx = turn.get("turn_index")
        prefix = f"[turn {idx} · {role}]" if idx is not None else f"[{role}]"
        lines.append(f"{prefix} {content}")
    if current_turn_idx is not None:
        lines.append(
            f"(The user's NEW message will be turn {current_turn_idx + 1} in this chat.)"
        )
    return "\n".join(lines) if lines else "(no prior turns)"


def _format_preferences(prefs: Optional[list[str]]) -> str:
    """Render user `/remember` entries as a high-priority prompt section."""
    if not prefs:
        return ""
    cleaned: list[str] = []
    seen: set[str] = set()
    for p in prefs:
        s = (p or "").strip()
        if not s or s.lower() in seen:
            continue
        seen.add(s.lower())
        cleaned.append(s[:400])
        if len(cleaned) >= 30:
            break
    if not cleaned:
        return ""
    bullets = "\n".join(f"- {p}" for p in cleaned)
    return (
        "=== USER BEHAVIOR / STYLE PREFERENCES (from /remember) ===\n"
        "The user has explicitly told you how they like things. Follow these in "
        "ALL responses, across ALL chats, unless they conflict with the HARD "
        "RULES above (in which case the HARD RULES win):\n"
        f"{bullets}\n\n"
    )


def _build_prompt(
    question: str,
    code: list[CodeHit],
    skills: list[SkillHit],
    history: list[dict],
    commit_sha: Optional[str] = None,
    file_paths: Optional[list[str]] = None,
    mode: str = "strict",
    current_turn_idx: Optional[int] = None,
    user_preferences: Optional[list[str]] = None,
) -> str:
    system = PLAN_SYSTEM_PROMPT if mode == "plan" else SYSTEM_PROMPT
    topics = extract_topics(history, cap=20)
    topics_line = ", ".join(topics) if topics else "(none yet)"

    scope_lines: list[str] = []
    if commit_sha:
        scope_lines.append(
            f"User scoped this question to commit {commit_sha}. "
            f"Only chunks indexed at (a prefix of) this commit were retrieved."
        )
    if file_paths:
        scope_lines.append(
            "User scoped this question to specific files: "
            + ", ".join(file_paths)
            + ". Only chunks from these files were retrieved."
        )
    scope_block = ""
    if scope_lines:
        scope_block = "=== ACTIVE SCOPE ===\n" + "\n".join(scope_lines) + "\n\n"

    mode_block = ""
    if mode == "plan":
        mode_block = (
            "=== MODE: /plan ===\n"
            "You are in PLAN mode. Combine the CONTEXT below with your general "
            "knowledge and any web search tool the runtime provides. Mark "
            "non-repo information clearly. Still cite real repo files/PRs with "
            "the bracket format.\n\n"
        )

    closing = (
        "Answer using the CONTEXT above as the primary source. You may add "
        "general knowledge and (if available) web search results, but mark them "
        "as such and never invent repo files or symbols."
        if mode == "plan"
        else (
            "Answer using ONLY the CONTEXT above. Prior conversation and topics "
            "are for resolving references (e.g. 'that function we looked at'); "
            "do not invent facts that are not in CONTEXT."
        )
    )

    history_note = (
        "Each line is one prior message in THIS chat, tagged with its turn "
        "number [turn N · role]. Turn 1 is the chat's anchor (the original "
        "request). Use these to resolve references like 'that file we looked "
        "at', 'your earlier answer', or 'the function from turn 4'. Do NOT "
        "invent earlier turns that aren't shown."
    )
    prefs_block = _format_preferences(user_preferences)
    return (
        f"{system}\n\n"
        f"{prefs_block}"
        f"{mode_block}"
        f"{scope_block}"
        f"=== TOPICS DISCUSSED IN THIS CHAT (rolling memory) ===\n{topics_line}\n\n"
        f"=== PRIOR CONVERSATION (this chat only) ===\n"
        f"{history_note}\n\n"
        f"{_format_history(history, current_turn_idx=current_turn_idx)}\n\n"
        f"=== REPO CONVENTIONS / PR NOTES ===\n{_format_skill_context(skills)}\n\n"
        f"=== CODE CONTEXT ===\n{_format_code_context(code)}\n\n"
        f"=== USER QUESTION ===\n{question}\n\n"
        f"{closing}"
    )


def _gen_sync(prompt: str, mode: str = "strict") -> str:
    settings = get_settings()
    api_key = require_current_gemini_key()
    genai.configure(api_key=api_key)

    max_tokens = (
        settings.max_output_tokens_plan
        if mode == "plan"
        else settings.max_output_tokens_strict
    )
    generation_config = {
        "temperature": 0.4 if mode == "plan" else 0.1,
        "top_p": 0.9,
        "max_output_tokens": max_tokens,
    }

    # In plan mode, try to enable Google Search grounding so the model can pull
    # current best practices / CVE info off the web. The exact tool format
    # varies by SDK version and Gemini API tier; fall back silently if it isn't
    # supported, so plan mode still works using just the model's own knowledge.
    resp = None
    if mode == "plan":
        for tools_spec in (
            [{"google_search_retrieval": {}}],
            [{"google_search": {}}],
        ):
            try:
                model = genai.GenerativeModel(
                    settings.gemini_chat_model, tools=tools_spec
                )
                resp = model.generate_content(
                    prompt, generation_config=generation_config
                )
                break
            except Exception:
                resp = None
                continue

    if resp is None:
        model = genai.GenerativeModel(settings.gemini_chat_model)
        resp = model.generate_content(prompt, generation_config=generation_config)

    text = ""
    truncated = False
    if hasattr(resp, "text") and resp.text:
        text = resp.text.strip()
    else:
        try:
            parts = resp.candidates[0].content.parts
            text = "".join(getattr(p, "text", "") for p in parts).strip()
        except Exception:
            text = ""

    try:
        finish = getattr(resp.candidates[0], "finish_reason", None)
        # SDK may return enum name or int; MAX_TOKENS / STOP with length = cut off.
        finish_name = (
            finish.name if hasattr(finish, "name") else str(finish or "")
        ).upper()
        if "MAX_TOKENS" in finish_name or finish == 2:
            truncated = True
    except Exception:
        pass

    if truncated and text:
        text += (
            "\n\n---\n"
            "*Response reached the output length limit and may be incomplete. "
            "Reply **continue** or ask a narrower follow-up (e.g. focus on one "
            "OWASP category) to get the rest.*"
        )
    return text


# Lightweight, LLM-free topic extractor.
# Matches:
#   [path/to/file.py]            -> path/to/file.py
#   [path/to/file.py:symbol]     -> path/to/file.py:symbol
#   [file L10-L40]               -> file
#   PR #123                       -> PR #123
#   `path/to/file.py`            -> path/to/file.py
_BRACKET_RE = re.compile(
    r"\[(?P<file>[A-Za-z0-9_./\-]+\.[A-Za-z0-9]+)(?::(?P<symbol>[A-Za-z_][\w.]*))?"
    r"(?:\s+L\d+-L\d+)?\]"
)
_PR_RE = re.compile(r"\bPR\s*#(?P<pr>\d+)\b")
_BACKTICK_PATH_RE = re.compile(r"`(?P<file>[A-Za-z0-9_./\-]+\.[A-Za-z0-9]+)`")


def extract_topics(history: Optional[list[dict]], cap: int = 20) -> list[str]:
    """Pull recurring repo identifiers out of prior turns. No LLM call.

    Considers BOTH user and assistant content so that a user typing
    `@backend/main.py` in turn 2 still surfaces as a topic for turn 6.
    """
    topics: list[str] = []
    seen: set[str] = set()
    if not history:
        return topics
    for turn in history:
        if turn.get("role") not in ("user", "assistant"):
            continue
        content = (turn.get("content") or "")[:4000]
        if not content:
            continue
        for m in _BRACKET_RE.finditer(content):
            tag = m.group("file")
            if m.group("symbol"):
                tag += ":" + m.group("symbol")
            if tag not in seen:
                seen.add(tag)
                topics.append(tag)
                if len(topics) >= cap:
                    return topics
        for m in _BACKTICK_PATH_RE.finditer(content):
            tag = m.group("file")
            if tag not in seen and "/" in tag or tag.count(".") == 1:
                seen.add(tag)
                topics.append(tag)
                if len(topics) >= cap:
                    return topics
        for m in _PR_RE.finditer(content):
            tag = f"PR #{m.group('pr')}"
            if tag not in seen:
                seen.add(tag)
                topics.append(tag)
                if len(topics) >= cap:
                    return topics
    return topics


def build_retrieval_query(question: str, history: Optional[list[dict]]) -> str:
    """Bias retrieval toward the prior topic for follow-up questions.

    Cheapest possible: regex-extracted topics from history + last assistant turn.
    No extra LLM calls.
    """
    if not history:
        return question
    parts: list[str] = []
    topics = extract_topics(history, cap=5)
    if topics:
        parts.append("Context topics: " + ", ".join(topics))
    last_assistant = next(
        (h for h in reversed(history) if h.get("role") == "assistant"), None
    )
    if last_assistant and last_assistant.get("content"):
        parts.append(last_assistant["content"][:300])
    parts.append(question)
    return "\n".join(parts)


async def preview(
    repo_id: UUID,
    chat_id: str,
    question: str,
    commit_sha: Optional[str] = None,
    file_paths: Optional[list[str]] = None,
    mode: str = "strict",
    user_preferences: Optional[list[str]] = None,
) -> dict:
    """Return the top-k retrieved chunks without calling the LLM.

    Uses the same chat-history RAG as `answer` so the preview chunks match what
    the model will actually see.
    """
    history = await retrieve_chat_context(chat_id, question)
    retrieval_query = build_retrieval_query(question, history)
    code, skills = await retrieve(
        repo_id, retrieval_query, commit_sha=commit_sha, file_paths=file_paths
    )
    last_idx = await last_turn_index(chat_id)
    return {
        "code": [asdict(c) for c in code],
        "skills": [asdict(s) for s in skills],
        "history_turns_used": len(history),
        "history_turns_total": last_idx,
        "next_turn_index": last_idx + 1,
        "commit_scope": commit_sha,
        "file_scope": list(file_paths) if file_paths else [],
        "mode": mode,
    }


async def answer(
    repo_id: UUID,
    chat_id: str,
    question: str,
    commit_sha: Optional[str] = None,
    file_paths: Optional[list[str]] = None,
    mode: str = "strict",
    user_preferences: Optional[list[str]] = None,
) -> dict:
    history = await retrieve_chat_context(chat_id, question)
    last_idx = await last_turn_index(chat_id)
    retrieval_query = build_retrieval_query(question, history)
    code, skills = await retrieve(
        repo_id, retrieval_query, commit_sha=commit_sha, file_paths=file_paths
    )

    # In strict mode, refuse early when retrieval is empty.
    # In plan mode, proceed: the model can still reason from general knowledge.
    if mode == "strict" and not code and not skills:
        if commit_sha:
            msg = (
                f"No indexed chunks were found for commit prefix '{commit_sha}'. "
                "Either the commit hasn't been indexed yet (try Sync), or remove the "
                "commit scope to broaden retrieval."
            )
        elif file_paths:
            msg = (
                "No indexed chunks were found for the file(s) you referenced: "
                + ", ".join(file_paths)
                + ". Check the @-mention spelling, or remove the @file scope to "
                "broaden retrieval."
            )
        else:
            msg = REFUSAL_TEXT
        user_idx = await persist_turn(chat_id, repo_id, "user", question)
        assistant_idx = await persist_turn(chat_id, repo_id, "assistant", msg)
        return {
            "answer": msg,
            "sources": {"code": [], "skills": []},
            "grounded": False,
            "history_turns_used": len(history),
            "history_turns_total": assistant_idx,
            "user_turn_index": user_idx,
            "assistant_turn_index": assistant_idx,
            "commit_scope": commit_sha,
            "file_scope": list(file_paths) if file_paths else [],
            "mode": mode,
        }

    prompt = _build_prompt(
        question,
        code,
        skills,
        history,
        commit_sha=commit_sha,
        file_paths=file_paths,
        mode=mode,
        current_turn_idx=last_idx,
        user_preferences=user_preferences,
    )
    text = await asyncio.to_thread(_gen_sync, prompt, mode)
    if not text:
        text = REFUSAL_TEXT

    user_idx = await persist_turn(chat_id, repo_id, "user", question)
    assistant_idx = await persist_turn(chat_id, repo_id, "assistant", text)

    return {
        "answer": text,
        "sources": {
            "code": [asdict(c) for c in code],
            "skills": [asdict(s) for s in skills],
        },
        "grounded": bool(code or skills),
        "history_turns_used": len(history),
        "history_turns_total": assistant_idx,
        "user_turn_index": user_idx,
        "assistant_turn_index": assistant_idx,
        "commit_scope": commit_sha,
        "file_scope": list(file_paths) if file_paths else [],
        "mode": mode,
    }
