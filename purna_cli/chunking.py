"""Chunking logic - reuses backend/chunker.py"""

import sys
from pathlib import Path

# Add backend to path to import chunker
backend_path = Path(__file__).parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from chunker import chunk_file, ALWAYS_INCLUDE_BASENAMES, SKIP_DIRS
from embeddings import embed_texts, _embed_one
from api_keys import set_current_gemini_key

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
    
    # Chunk the file
    chunks = chunk_file(file_path, content)
    
    if not chunks:
        return []
    
    # Prepare texts for embedding
    texts = [c["content"] for c in chunks]
    
    # Get embeddings
    embeddings = await embed_texts(texts)
    
    # Build result with all metadata
    results = []
    for chunk, embedding in zip(chunks, embeddings):
        results.append({
            "file": file_path,
            "symbol": chunk.get("symbol", ""),
            "kind": chunk.get("kind", ""),
            "language": chunk.get("language", ""),
            "content": chunk["content"],
            "content_hash": content_hash(chunk["content"]),
            "start_line": chunk.get("start_line", 0),
            "end_line": chunk.get("end_line", 0),
            "char_count": len(chunk["content"]),
            "embedding": embedding,
        })
    
    return results


def should_skip_file(file_path: str, max_size_bytes: int = 400000) -> bool:
    """Check if file should be skipped based on size and type"""
    path = Path(file_path)
    
    # Check size
    if path.exists() and path.stat().st_size > max_size_bytes:
        return True
    
    # Check binary
    if path.suffix in {'.png', '.jpg', '.jpeg', '.gif', '.pdf', '.zip', '.tar', '.gz'}:
        return True
    
    return False
