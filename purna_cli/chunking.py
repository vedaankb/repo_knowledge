"""Chunking logic - reuses backend/chunker.py"""

import sys
from datetime import datetime, timezone
from pathlib import Path

# Add repo root so backend package imports resolve (embeddings uses relative imports)
repo_root = Path(__file__).parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from backend.chunker import chunk_file, ALWAYS_INCLUDE_BASENAMES, SKIP_DIRS
from backend.embeddings import embed_texts
from backend.api_keys import set_current_gemini_key

from .utils import content_hash, file_path_hash


async def process_file(
    file_path: str, 
    content: str, 
    repo_root: Path,
    commit_sha: str,
    gemini_key: str
) -> list[dict]:
    """
    Process a single file: chunk and embed
    Returns list of chunk dicts ready for JSON serialization
    """
    # Set API key for this operation
    set_current_gemini_key(gemini_key)
    
    # Skip if in excluded directory
    path_obj = Path(file_path)
    if any(part in SKIP_DIRS for part in path_obj.parts):
        return []
    
    # Chunk the file (backend chunker expects bytes)
    chunks = chunk_file(file_path, content.encode("utf-8"))
    
    if not chunks:
        return []
    
    # Prepare texts for embedding
    texts = [c.content for c in chunks]
    
    # Get embeddings
    embeddings = await embed_texts(texts)
    
    # Build result with all metadata
    indexed_at = datetime.now(timezone.utc).isoformat()
    results = []
    for chunk, embedding in zip(chunks, embeddings):
        results.append({
            "indexed_at": indexed_at,
            "file": file_path,
            "symbol": chunk.symbol or "",
            "kind": chunk.kind or "",
            "language": chunk.language or "",
            "content": chunk.content,
            "content_hash": content_hash(chunk.content),
            "start_line": chunk.start_line,
            "end_line": chunk.end_line,
            "char_count": len(chunk.content),
            "embedding": embedding,
        })
    
    return results


def should_skip_file(file_path: str, max_size_bytes: int = 400000) -> bool:
    """Check if file should be skipped based on size and type"""
    path = Path(file_path)
    
    # Check size
    if path.exists() and path.stat().st_size > max_size_bytes:
        return True
    
    # Check common binary / asset extensions
    if path.suffix.lower() in {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".icns", ".bmp", ".tiff",
        ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
        ".woff", ".woff2", ".ttf", ".eot", ".otf",
        ".mp3", ".mp4", ".mov", ".avi", ".wav",
        ".exe", ".dll", ".so", ".dylib", ".bin", ".dat", ".db", ".sqlite",
        ".pyc", ".class", ".jar", ".dmg",
    }:
        return True

    # Quick binary sniff for extensionless or unknown types
    if path.is_file():
        try:
            sample = path.read_bytes()[:8192]
            if b"\x00" in sample:
                return True
        except OSError:
            return True

    return False
