"""
RAG search: keyword (FTS5) + vector (cosine) merged with Reciprocal Rank Fusion.

Usage:
    python3 search.py "affordable housing"
    python3 search.py "body cameras" --limit 10
    python3 search.py "chickens" --type article
"""

import json
import os
import sqlite3
import struct
import sys
import urllib.request

import numpy as np

RAG_DB = os.path.join(os.path.dirname(__file__), "rag.db")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = "gemini-embedding-2-preview"
# Use proxy if set (needed on VPS where Google blocks direct Gemini access)
BASE_URL = os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta")

# RRF constant (standard value)
RRF_K = 60

# In-memory embedding matrix cache (loaded once, reused across queries)
_emb_cache = {"ids": None, "matrix": None, "norms": None}


def _load_embedding_matrix(db):
    """Load all embeddings into a numpy matrix for fast batch cosine similarity."""
    if _emb_cache["matrix"] is not None:
        return _emb_cache["ids"], _emb_cache["matrix"], _emb_cache["norms"]

    rows = db.execute("SELECT chunk_id, embedding FROM embeddings ORDER BY chunk_id").fetchall()
    if not rows:
        return [], np.array([]), np.array([])

    ids = [r[0] for r in rows]
    dim = len(rows[0][1]) // 4
    matrix = np.zeros((len(rows), dim), dtype=np.float32)
    for i, (_, blob) in enumerate(rows):
        matrix[i] = np.frombuffer(blob, dtype=np.float32)

    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1  # avoid division by zero

    _emb_cache["ids"] = ids
    _emb_cache["matrix"] = matrix
    _emb_cache["norms"] = norms
    return ids, matrix, norms


def embed_query(query):
    """Embed a single query string. Returns [] on any failure (graceful fallback to keyword-only)."""
    if not GEMINI_API_KEY:
        return []
    try:
        url = f"{BASE_URL}/models/{MODEL}:embedContent?key={GEMINI_API_KEY}"
        payload = json.dumps({
            "model": f"models/{MODEL}",
            "content": {"parts": [{"text": query}]},
        }).encode()
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read())
        return data.get("embedding", {}).get("values", [])
    except Exception as e:
        print(f"Embedding error (falling back to keyword search): {e}", file=sys.stderr)
        return []


def keyword_search(db, query, limit=30):
    """FTS5 keyword search. Returns list of (chunk_id, rank)."""
    try:
        rows = db.execute("""
            SELECT chunks_fts.rowid, rank
            FROM chunks_fts
            WHERE chunks_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """, (query, limit)).fetchall()
        return [(r[0], r[1]) for r in rows]
    except Exception:
        return []


def vector_search(db, query_embedding, limit=30):
    """Numpy-accelerated cosine similarity search."""
    ids, matrix, norms = _load_embedding_matrix(db)
    if len(ids) == 0:
        return []

    q = np.array(query_embedding, dtype=np.float32).reshape(1, -1)
    q_norm = np.linalg.norm(q)
    if q_norm == 0:
        return []

    # Batch cosine similarity: (matrix / norms) dot (q / q_norm)
    similarities = (matrix @ q.T).flatten() / (norms.flatten() * q_norm)

    # Filter by minimum similarity threshold to avoid irrelevant results
    # Gemini embeddings have high baseline similarity (~0.5 even for nonsense)
    MIN_SIM = 0.60
    valid_mask = similarities >= MIN_SIM
    if not valid_mask.any():
        return []

    valid_indices = np.where(valid_mask)[0]
    valid_sims = similarities[valid_indices]
    n = min(limit, len(valid_indices))
    top_local = np.argpartition(valid_sims, -n)[-n:]
    top_local = top_local[np.argsort(valid_sims[top_local])[::-1]]
    top_idx = valid_indices[top_local]

    return [(ids[i], float(similarities[i])) for i in top_idx]


def reciprocal_rank_fusion(result_lists, k=RRF_K):
    """Merge multiple ranked result lists using RRF.

    Args:
        result_lists: list of lists, each containing (chunk_id, score) tuples
        k: RRF constant (default 60)

    Returns:
        Sorted list of (chunk_id, rrf_score) tuples.
    """
    scores = {}
    for result_list in result_lists:
        for rank, (chunk_id, _) in enumerate(result_list):
            if chunk_id not in scores:
                scores[chunk_id] = 0.0
            scores[chunk_id] += 1.0 / (k + rank + 1)

    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def enrich_results(db, chunk_ids_with_scores, limit=20):
    """Fetch full chunk data for result IDs."""
    results = []
    for chunk_id, rrf_score in chunk_ids_with_scores[:limit]:
        row = db.execute("""
            SELECT id, doc_id, doc_type, committee, date, chunk_index,
                   content, speaker, start_time, end_time, char_count
            FROM chunks WHERE id = ?
        """, (chunk_id,)).fetchone()

        if not row:
            continue

        results.append({
            "chunk_id": row[0],
            "doc_id": row[1],
            "doc_type": row[2],
            "committee": row[3],
            "date": row[4],
            "content": row[6],
            "speaker": row[7],
            "start_time": row[8],
            "end_time": row[9],
            "rrf_score": round(rrf_score, 6),
        })

    return results


def rag_search(query, limit=20, doc_type=None, committee=None, date_from=None, date_to=None):
    """Full RAG search: keyword + vector, merged with RRF.

    Returns list of enriched result dicts.
    """
    db = sqlite3.connect(RAG_DB)

    # 1. Keyword search
    kw_results = keyword_search(db, query, limit=50)

    # 2. Vector search
    query_emb = embed_query(query)
    vec_results = vector_search(db, query_emb, limit=50) if query_emb else []

    # 3. RRF merge
    merged = reciprocal_rank_fusion([kw_results, vec_results])

    # 4. Enrich
    results = enrich_results(db, merged, limit=limit * 2)  # over-fetch for filtering

    # 5. Apply filters
    if doc_type:
        results = [r for r in results if r["doc_type"] == doc_type]
    if committee:
        results = [r for r in results if r["committee"] == committee]
    if date_from:
        results = [r for r in results if r["date"] and r["date"] >= date_from]
    if date_to:
        results = [r for r in results if r["date"] and r["date"] <= date_to]

    db.close()
    return results[:limit]


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 search.py <query> [--limit N] [--type article|transcript]")
        sys.exit(1)

    query = sys.argv[1]
    limit = 10
    doc_type = None

    # Parse args
    args = sys.argv[2:]
    for i, arg in enumerate(args):
        if arg == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])
        elif arg == "--type" and i + 1 < len(args):
            doc_type = args[i + 1]

    print(f"Searching: \"{query}\" (limit={limit}, type={doc_type or 'all'})\n")

    results = rag_search(query, limit=limit, doc_type=doc_type)

    for i, r in enumerate(results):
        speaker = f" ({r['speaker']})" if r.get("speaker") else ""
        ts = ""
        if r.get("start_time") is not None:
            mins = int(r["start_time"] // 60)
            secs = int(r["start_time"] % 60)
            ts = f" [{mins}:{secs:02d}]"
        print(f"{i+1}. [{r['doc_type']}] {r['doc_id']} ({r['date']}){speaker}{ts} — RRF: {r['rrf_score']}")
        print(f"   {r['committee'] or 'N/A'}")
        print(f"   {r['content'][:120]}...")
        print()


if __name__ == "__main__":
    main()
