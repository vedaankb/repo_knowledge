# Phase 4: Scaling Strategy

## Git LFS for Large Knowledge Repositories

When knowledge repositories grow beyond ~10,000 chunks, embedding storage becomes a bottleneck. This document outlines strategies for scaling.

---

## Problem Statement

**Current limitation:** Each chunk JSON file contains a 768-dimensional float32 embedding (~6KB per chunk). For large repositories:
- 10k chunks = ~60 MB
- 100k chunks = ~600 MB
- 1M chunks = ~6 GB

Git repositories perform poorly with large binary blobs, and GitHub has size limits.

---

## Solution 1: Git LFS (Large File Storage)

### Overview
Store chunk JSON files in Git LFS instead of regular Git objects.

### Implementation

1. **Enable Git LFS in knowledge repo:**
   ```bash
   cd myapp-knowledge
   git lfs install
   git lfs track "chunks/**/*.json"
   git add .gitattributes
   git commit -m "Enable Git LFS for chunks"
   ```

2. **Update purna CLI publish:**
   ```python
   # In purna_cli/publish.py
   def publish_with_lfs(repo_root, config, github_token):
       # Instead of GitHub Contents API, use git commands
       subprocess.run(["git", "add", "chunks/"], cwd=knowledge_repo_path)
       subprocess.run(["git", "commit", "-m", "Publish chunks"], cwd=knowledge_repo_path)
       subprocess.run(["git", "push"], cwd=knowledge_repo_path)
   ```

3. **Backend importer updates:**
   - Clone knowledge repo locally instead of using GitHub API
   - Read files directly from local clone
   - Git LFS handles large file downloads automatically

### Pros
- Transparent to most workflows
- GitHub supports LFS (100 GB free per repo)
- Efficient delta transfers

### Cons
- Requires git CLI (not just HTTP API)
- LFS bandwidth limits on GitHub
- Slightly more complex setup

---

## Solution 2: Compressed Embeddings

### Overview
Reduce embedding size using quantization and compression.

### Techniques

#### 2.1 Float16 Quantization
Convert `float32[768]` to `float16[768]`:
- **Size:** 6 KB → 3 KB (50% reduction)
- **Quality:** Minimal loss for retrieval tasks
- **Implementation:**
  ```python
  import numpy as np
  
  def compress_embedding(emb: list[float]) -> bytes:
      arr = np.array(emb, dtype=np.float32)
      arr16 = arr.astype(np.float16)
      return arr16.tobytes()
  
  def decompress_embedding(data: bytes) -> list[float]:
      arr16 = np.frombuffer(data, dtype=np.float16)
      return arr16.astype(np.float32).tolist()
  ```

#### 2.2 Gzip Compression
Compress JSON files after quantization:
- **Size:** 3 KB → ~1-1.5 KB (additional 50-66% reduction)
- **Overall:** 6 KB → 1-1.5 KB (75-80% total reduction)

#### 2.3 Matryoshka Embeddings (Already Supported)
Gemini's `outputDimensionality` parameter:
- Use 256 or 512 dimensions instead of 768
- **Size:** 6 KB → 2 KB (256-dim) or 4 KB (512-dim)
- **Quality:** Slight reduction, still effective for most cases

### Implementation Plan

1. **Update schema version to 2:**
   ```json
   {
     "schema_version": 2,
     "embedding_format": "float16_gzip",
     "embedding_dimensions": 512
   }
   ```

2. **Chunk JSON with compressed embeddings:**
   ```json
   {
     "file": "backend/main.py",
     "content": "...",
     "embedding_compressed": "base64_encoded_gzipped_float16_bytes"
   }
   ```

3. **Backend importer decompression:**
   ```python
   def import_chunk_v2(chunk_data):
       if "embedding_compressed" in chunk_data:
           emb = decompress_embedding(
               base64.b64decode(chunk_data["embedding_compressed"])
           )
       else:
           emb = chunk_data["embedding"]
       
       # Insert into pgvector
       ...
   ```

---

## Solution 3: Multi-Dev Publish Strategy

### Problem
Multiple developers pushing to the same knowledge repo creates merge conflicts in `manifest.json` and overlapping chunk files.

### Strategy: Branch-per-Developer

1. **Each developer publishes to their own branch:**
   ```yaml
   # .purnaOS/config.yaml
   knowledge:
     github: org/myapp-knowledge
     branch: dev/alice  # or dev/bob, dev/carol
   ```

2. **CI/CD merges dev branches:**
   - GitHub Action runs on `dev/*` branch push
   - Merges into `main` using smart conflict resolution:
     - `manifest.json`: Take latest `head_sha`
     - `chunks/`: Union of all chunk files
     - `commits/`: Union of all commit files

3. **Example GitHub Action:**
   ```yaml
   name: Merge Dev Knowledge
   on:
     push:
       branches:
         - 'dev/*'
   jobs:
     merge:
       runs-on: ubuntu-latest
       steps:
         - uses: actions/checkout@v3
           with:
             ref: main
         - name: Merge dev branch
           run: |
             git fetch origin ${{ github.ref }}
             git merge --no-commit --no-ff origin/${{ github.ref }}
             # Custom merge for manifest.json
             python scripts/merge_manifests.py
             git commit -m "Merge ${{ github.ref }} into main"
             git push
   ```

### Alternative: Server-Side Publish

Instead of developers publishing directly:
1. Developers push artifacts to an S3 bucket or API endpoint
2. Server-side process imports and publishes to knowledge repo
3. Avoids merge conflicts entirely

---

## Recommended Phased Approach

### Phase 4.1: Matryoshka + Float16
- Use `outputDimensionality=512` in Gemini embed calls
- Quantize to float16
- **Result:** 6 KB → 2 KB (66% reduction)
- **Effort:** Low (2-3 days)

### Phase 4.2: Gzip Compression
- Add gzip to float16 embeddings
- **Result:** 2 KB → 1 KB (additional 50%)
- **Effort:** Low (1-2 days)

### Phase 4.3: Git LFS
- Enable LFS for `chunks/**/*.json`
- Switch from GitHub Contents API to git CLI
- **Result:** Handles 1M+ chunks efficiently
- **Effort:** Medium (3-5 days)

### Phase 4.4: Multi-Dev Strategy
- Implement branch-per-developer workflow
- Add GitHub Action for smart merging
- **Result:** Supports teams of 5-50 developers
- **Effort:** Medium (5-7 days)

---

## Cost-Benefit Analysis

| Solution | Size Reduction | Complexity | Supports Large Repos | Team-Friendly |
|----------|----------------|------------|----------------------|---------------|
| Matryoshka (512-dim) | 33% | Low | Moderate | ✓ |
| Float16 | 50% | Low | Moderate | ✓ |
| Gzip | 75-80% (combined) | Low | Yes | ✓ |
| Git LFS | N/A (storage) | Medium | Yes | ✓ |
| Multi-dev branches | N/A | Medium | Yes | ✓✓ |

---

## Migration Path

When upgrading from schema v1 to v2:

1. **Backward compatibility:**
   - Backend supports both `embedding` (v1) and `embedding_compressed` (v2)
   - Importer detects schema version from manifest

2. **Gradual migration:**
   - New commits use v2 format
   - Old commits remain in v1 format
   - No need to reprocess entire history

3. **Full migration (optional):**
   ```bash
   purna migrate --from-version 1 --to-version 2
   ```
   - Reprocesses all chunks
   - Compresses embeddings
   - Updates manifest

---

## Conclusion

Phase 4 strategies are **not required for the POC** but become critical for production use at scale. Recommended priority:

1. **Immediate (if >10k chunks):** Matryoshka + Float16
2. **Next (if >50k chunks):** Add Gzip
3. **As needed (if >100k chunks):** Git LFS
4. **When scaling team (5+ devs):** Multi-dev branches

All solutions are incremental and can be adopted independently.
