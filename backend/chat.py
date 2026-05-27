from __future__ import annotations

import asyncio
import re
from dataclasses import asdict
from typing import Optional
from uuid import UUID



import google.generativeai as genai

from .chat_memory import persist_turn, retrieve_chat_context
from .config import get_settings
from .embeddings import _ensure_configured
from .retriever import CodeHit, SkillHit, retrieve


REFUSAL_TEXT = (
    "I don't have enough information from the indexed repository context to answer that. "
    "Try rephrasing, ask about a specific file/function/PR, or trigger a re-index."
)

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


def _format_history(history: list[dict]) -> str:
    if not history:
        return "(no prior turns)"
    lines: list[str] = []
    for turn in history[-6:]:
        role = turn.get("role", "user")
        content = (turn.get("content") or "").strip()
        if not content:
            continue
        if len(content) > 800:
            content = content[:800] + "..."
        lines.append(f"{role}: {content}")
    return "\n".join(lines) if lines else "(no prior turns)"


def _build_prompt(
    question: str,
    code: list[CodeHit],
    skills: list[SkillHit],
    history: list[dict],
    commit_sha: Optional[str] = None,
) -> str:
    topics = extract_topics(history, cap=20)
    topics_line = ", ".join(topics) if topics else "(none yet)"
    scope_block = (
        f"=== ACTIVE COMMIT SCOPE ===\nUser scoped this question to commit {commit_sha}. "
        f"Only chunks indexed at (a prefix of) this commit were retrieved.\n\n"
        if commit_sha else ""
    )
    return (
        f"{SYSTEM_PROMPT}\n\n"
        f"{scope_block}"
        f"=== TOPICS DISCUSSED IN THIS CHAT (rolling memory) ===\n{topics_line}\n\n"
        f"=== PRIOR CONVERSATION (this chat only, last few turns) ===\n{_format_history(history)}\n\n"
        f"=== REPO CONVENTIONS / PR NOTES ===\n{_format_skill_context(skills)}\n\n"
        f"=== CODE CONTEXT ===\n{_format_code_context(code)}\n\n"
        f"=== USER QUESTION ===\n{question}\n\n"
        f"Answer using ONLY the CONTEXT above. Prior conversation and topics are for "
        f"resolving references (e.g. 'that function we looked at'); do not invent "
        f"facts that are not in CONTEXT."
    )


def _gen_sync(prompt: str) -> str:
    _ensure_configured()
    settings = get_settings()
    model = genai.GenerativeModel(settings.gemini_chat_model)
    resp = model.generate_content(
        prompt,
        generation_config={
            "temperature": 0.1,
            "top_p": 0.9,
            "max_output_tokens": 1024,
        },
    )
    if hasattr(resp, "text") and resp.text:
        return resp.text.strip()
    try:
        parts = resp.candidates[0].content.parts
        return "".join(getattr(p, "text", "") for p in parts).strip()
    except Exception:
        return ""


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
    """Pull recurring repo identifiers out of past assistant turns. No LLM call."""
    topics: list[str] = []
    seen: set[str] = set()
    if not history:
        return topics
    for turn in history:
        if turn.get("role") != "assistant":
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
) -> dict:
    """Return the top-k retrieved chunks without calling the LLM.

    Uses the same chat-history RAG as `answer` so the preview chunks match what
    the model will actually see.
    """
    history = await retrieve_chat_context(chat_id, question)
    retrieval_query = build_retrieval_query(question, history)
    code, skills = await retrieve(repo_id, retrieval_query, commit_sha=commit_sha)
    return {
        "code": [asdict(c) for c in code],
        "skills": [asdict(s) for s in skills],
        "history_turns_used": len(history),
        "commit_scope": commit_sha,
    }


async def answer(
    repo_id: UUID,
    chat_id: str,
    question: str,
    commit_sha: Optional[str] = None,
) -> dict:
    history = await retrieve_chat_context(chat_id, question)
    retrieval_query = build_retrieval_query(question, history)
    code, skills = await retrieve(repo_id, retrieval_query, commit_sha=commit_sha)

    if not code and not skills:
        msg = REFUSAL_TEXT
        if commit_sha:
            msg = (
                f"No indexed chunks were found for commit prefix '{commit_sha}'. "
                "Either the commit hasn't been indexed yet (try Sync), or remove the "
                "commit scope to broaden retrieval."
            )
        await persist_turn(chat_id, repo_id, "user", question)
        await persist_turn(chat_id, repo_id, "assistant", msg)
        return {
            "answer": msg,
            "sources": {"code": [], "skills": []},
            "grounded": False,
            "history_turns_used": len(history),
            "commit_scope": commit_sha,
        }

    prompt = _build_prompt(question, code, skills, history, commit_sha=commit_sha)
    text = await asyncio.to_thread(_gen_sync, prompt)
    if not text:
        text = REFUSAL_TEXT

    await persist_turn(chat_id, repo_id, "user", question)
    await persist_turn(chat_id, repo_id, "assistant", text)

    return {
        "answer": text,
        "sources": {
            "code": [asdict(c) for c in code],
            "skills": [asdict(s) for s in skills],
        },
        "grounded": True,
        "history_turns_used": len(history),
        "commit_scope": commit_sha,
    }
