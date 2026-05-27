from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Iterable, Optional

try:
    from tree_sitter_languages import get_parser  # type: ignore
    _TS_AVAILABLE = True
except Exception:
    _TS_AVAILABLE = False


EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rb": "ruby",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cs": "c_sharp",
    ".php": "php",
    ".swift": "swift",
    ".scala": "scala",
    ".lua": "lua",
}

TEXT_EXTENSIONS = {
    ".md", ".markdown", ".rst", ".txt", ".yaml", ".yml", ".json",
    ".toml", ".ini", ".env", ".cfg", ".sh", ".bash", ".zsh",
    ".dockerfile", ".sql", ".html", ".css", ".scss",
}

SKIP_DIRS = {
    "node_modules", ".git", "dist", "build", "out", "target",
    ".next", ".nuxt", ".venv", "venv", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "vendor", ".idea", ".vscode",
    "coverage", ".turbo",
}

SKIP_BASENAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock"}

# Files that may have no extension or be dotfiles but are critical for
# understanding how to run/build/configure a repo. Always include these.
ALWAYS_INCLUDE_BASENAMES = {
    # Build / container / orchestration
    "Dockerfile", "Dockerfile.dev", "Dockerfile.prod", "Containerfile",
    ".dockerignore",
    "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml",
    "Makefile", "GNUmakefile", "CMakeLists.txt",
    "Procfile", "Rakefile", "Gemfile", "Justfile", "justfile",
    "Vagrantfile", "Brewfile", "Caddyfile",
    # Runtime / version pinning
    ".nvmrc", ".node-version", ".tool-versions",
    ".python-version", ".ruby-version",
    # Env templates
    ".env.example", ".env.sample", ".env.template",
    # Project manifests
    "pyproject.toml", "setup.py", "setup.cfg",
    "Pipfile", "Pipfile.lock",
    "go.mod", "go.sum",
    "Cargo.toml",
    "package.json", "tsconfig.json", "jsconfig.json",
    "requirements.txt", "requirements-dev.txt", "requirements_dev.txt",
    # Docs / contribution
    "README", "README.md", "README.rst",
    "AGENTS.md", "AGENT.md",
    "CONTRIBUTING.md", "CONTRIBUTING",
    "CHANGELOG.md", "CHANGELOG",
    "ARCHITECTURE.md", "ARCHITECTURE",
    "LICENSE", "LICENSE.md", "NOTICE",
}


SYMBOL_NODE_TYPES: dict[str, set[str]] = {
    "python": {"function_definition", "class_definition"},
    "javascript": {"function_declaration", "method_definition", "class_declaration"},
    "typescript": {"function_declaration", "method_definition", "class_declaration", "interface_declaration"},
    "tsx": {"function_declaration", "method_definition", "class_declaration", "interface_declaration"},
    "go": {"function_declaration", "method_declaration", "type_declaration"},
    "ruby": {"method", "class", "module"},
    "rust": {"function_item", "impl_item", "struct_item", "enum_item", "trait_item"},
    "java": {"method_declaration", "class_declaration", "interface_declaration"},
    "kotlin": {"function_declaration", "class_declaration"},
    "c": {"function_definition"},
    "cpp": {"function_definition", "class_specifier", "struct_specifier"},
    "c_sharp": {"method_declaration", "class_declaration", "interface_declaration"},
    "php": {"function_definition", "method_declaration", "class_declaration"},
    "swift": {"function_declaration", "class_declaration", "protocol_declaration"},
    "scala": {"function_definition", "class_definition", "object_definition", "trait_definition"},
    "lua": {"function_declaration", "local_function"},
}


@dataclass
class Chunk:
    file: str
    symbol: Optional[str]
    kind: str
    language: Optional[str]
    content: str
    start_line: int
    end_line: int

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8", errors="ignore")).hexdigest()


def should_skip_path(path: str) -> bool:
    parts = path.split("/")
    if any(p in SKIP_DIRS for p in parts):
        return True
    base = parts[-1] if parts else path
    if base in SKIP_BASENAMES:
        return True
    if base in ALWAYS_INCLUDE_BASENAMES:
        return False
    if base.startswith(".") and base not in (".env.example",):
        return True
    return False


def detect_language(path: str) -> Optional[str]:
    base = path.rsplit("/", 1)[-1]
    if base.startswith("Dockerfile"):
        return None
    if base in {"Makefile", "GNUmakefile"}:
        return None
    _, ext = os.path.splitext(path.lower())
    return EXT_TO_LANG.get(ext)


def is_text_path(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    if base in ALWAYS_INCLUDE_BASENAMES:
        return True
    if base.startswith("Dockerfile"):
        return True
    _, ext = os.path.splitext(path.lower())
    return ext in TEXT_EXTENSIONS or ext in EXT_TO_LANG


def _node_name(node, source: bytes) -> Optional[str]:
    name_node = node.child_by_field_name("name")
    if name_node is not None:
        return source[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="ignore")
    for child in node.children:
        if child.type in {"identifier", "type_identifier", "property_identifier"}:
            return source[child.start_byte:child.end_byte].decode("utf-8", errors="ignore")
    return None


def _truncate(text: str, max_chars: int = 1200) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... [truncated]"


def chunk_code_with_tree_sitter(path: str, source: bytes, language: str) -> list[Chunk]:
    if not _TS_AVAILABLE:
        return chunk_sliding(path, source.decode("utf-8", errors="ignore"), language=language)
    try:
        parser = get_parser(language)
    except Exception:
        return chunk_sliding(path, source.decode("utf-8", errors="ignore"), language=language)

    tree = parser.parse(source)
    chunks: list[Chunk] = []
    targets = SYMBOL_NODE_TYPES.get(language, set())

    def walk(node) -> None:
        if node.type in targets:
            name = _node_name(node, source) or "(anonymous)"
            start_line = node.start_point[0] + 1
            end_line = node.end_point[0] + 1
            body = source[node.start_byte:node.end_byte].decode("utf-8", errors="ignore")
            content = f"{path} :: {name}\n{_truncate(body)}"
            chunks.append(Chunk(
                file=path,
                symbol=name,
                kind=node.type,
                language=language,
                content=content,
                start_line=start_line,
                end_line=end_line,
            ))
            return
        for child in node.children:
            walk(child)

    walk(tree.root_node)

    if not chunks:
        return chunk_sliding(path, source.decode("utf-8", errors="ignore"), language=language)
    return chunks


def chunk_sliding(
    path: str,
    text: str,
    *,
    language: Optional[str] = None,
    target_chars: int = 600,
    overlap: int = 80,
) -> list[Chunk]:
    text = text.strip()
    if not text:
        return []
    lines = text.splitlines()
    chunks: list[Chunk] = []
    buf: list[str] = []
    buf_chars = 0
    buf_start = 1
    line_no = 0

    def flush(end_line: int) -> None:
        nonlocal buf, buf_chars, buf_start
        if not buf:
            return
        content = f"{path}\n" + "\n".join(buf)
        chunks.append(Chunk(
            file=path,
            symbol=None,
            kind="text" if language is None else "text_block",
            language=language,
            content=content,
            start_line=buf_start,
            end_line=end_line,
        ))
        if overlap > 0 and buf:
            tail = buf[-2:] if len(buf) >= 2 else buf[-1:]
            buf = list(tail)
            buf_chars = sum(len(x) + 1 for x in buf)
            buf_start = max(1, end_line - len(buf) + 1)
        else:
            buf = []
            buf_chars = 0
            buf_start = end_line + 1

    for i, line in enumerate(lines, start=1):
        line_no = i
        buf.append(line)
        buf_chars += len(line) + 1
        if buf_chars >= target_chars:
            flush(i)

    if buf:
        flush(line_no)

    return chunks


def chunk_file(path: str, raw: bytes) -> list[Chunk]:
    if should_skip_path(path):
        return []
    if not is_text_path(path):
        return []
    try:
        if b"\x00" in raw[:4096]:
            return []
    except Exception:
        return []
    lang = detect_language(path)
    if lang is not None:
        return chunk_code_with_tree_sitter(path, raw, lang)
    try:
        text = raw.decode("utf-8", errors="ignore")
    except Exception:
        return []
    return chunk_sliding(path, text)


def dedupe_chunks(chunks: Iterable[Chunk]) -> list[Chunk]:
    seen: set[tuple] = set()
    out: list[Chunk] = []
    for c in chunks:
        key = (c.file, c.symbol, c.start_line, c.end_line, c.content_hash)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out
