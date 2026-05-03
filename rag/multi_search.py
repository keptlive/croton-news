"""
Multi-database RAG search with LLM answer generation.

Searches across three databases:
  - meetings (rag.db): Village meeting transcripts and articles
  - history (history.db): Historical documents, books, blogs
  - code (code.db): Village code and laws

Uses Gemini embeddings for vector search, FTS5 for keyword,
RRF fusion, and Nemotron 120B via OpenRouter for answers.
"""

import json
import os
import sqlite3
import struct
import sys
import urllib.request

import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
EMBEDDING_MODEL = "gemini-embedding-2-preview"
GEMINI_BASE_URL = os.environ.get("GEMINI_BASE_URL",
    "https://generativelanguage.googleapis.com/v1beta")

# LLM providers (tried in order)
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
NEMOTRON_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"

GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "qwen/qwen3-32b"

RRF_K = 60

# Database paths
DB_PATHS = {
    "meetings": os.path.join(SCRIPT_DIR, "rag.db"),
    "history": os.path.join(SCRIPT_DIR, "history.db"),
    "code": os.path.join(SCRIPT_DIR, "code.db"),
}

# Per-DB embedding caches
_caches = {}


def _get_db(corpus):
    path = DB_PATHS.get(corpus)
    if not path or not os.path.exists(path):
        return None
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def _load_matrix(db, corpus):
    if corpus in _caches:
        return _caches[corpus]

    rows = db.execute("SELECT chunk_id, embedding FROM embeddings ORDER BY chunk_id").fetchall()
    if not rows:
        _caches[corpus] = ([], np.array([]), np.array([]))
        return _caches[corpus]

    ids = [r["chunk_id"] for r in rows]
    dim = len(rows[0]["embedding"]) // 4
    matrix = np.zeros((len(rows), dim), dtype=np.float32)
    for i, r in enumerate(rows):
        matrix[i] = np.frombuffer(r["embedding"], dtype=np.float32)

    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1

    _caches[corpus] = (ids, matrix, norms)
    return _caches[corpus]


def embed_query(query):
    """Embed a query string. Returns [] on any failure (graceful fallback to keyword-only)."""
    if not GEMINI_API_KEY:
        return []
    try:
        url = f"{GEMINI_BASE_URL}/models/{EMBEDDING_MODEL}:embedContent?key={GEMINI_API_KEY}"
        payload = json.dumps({
            "model": f"models/{EMBEDDING_MODEL}",
            "content": {"parts": [{"text": query}]},
        }).encode()
        req = urllib.request.Request(url, data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read())
        return data.get("embedding", {}).get("values", [])
    except Exception as e:
        print(f"Embedding error (falling back to keyword search): {e}", file=sys.stderr)
        return []


def keyword_search(db, query, limit=30):
    try:
        rows = db.execute("""
            SELECT chunks_fts.rowid, rank
            FROM chunks_fts WHERE chunks_fts MATCH ?
            ORDER BY rank LIMIT ?
        """, (query, limit)).fetchall()
        return [(r[0], r[1]) for r in rows]
    except Exception:
        return []


def vector_search(db, corpus, query_emb, limit=30):
    ids, matrix, norms = _load_matrix(db, corpus)
    if len(ids) == 0:
        return []

    q = np.array(query_emb, dtype=np.float32).reshape(1, -1)
    q_norm = np.linalg.norm(q)
    if q_norm == 0:
        return []

    sims = (matrix @ q.T).flatten() / (norms.flatten() * q_norm)

    MIN_SIM = 0.55
    valid = np.where(sims >= MIN_SIM)[0]
    if len(valid) == 0:
        return []

    n = min(limit, len(valid))
    top_local = np.argpartition(sims[valid], -n)[-n:]
    top_local = top_local[np.argsort(sims[valid][top_local])[::-1]]
    top_idx = valid[top_local]

    return [(ids[i], float(sims[i])) for i in top_idx]


def rrf(result_lists, k=RRF_K):
    scores = {}
    for rl in result_lists:
        for rank, (cid, _) in enumerate(rl):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def enrich_meetings(db, chunk_ids_scores, limit=15):
    results = []
    for cid, score in chunk_ids_scores[:limit]:
        row = db.execute("""
            SELECT id, doc_id, doc_type, committee, date, content,
                   speaker, start_time, end_time
            FROM chunks WHERE id = ?
        """, (cid,)).fetchone()
        if row:
            results.append({
                "chunk_id": row["id"],
                "doc_id": row["doc_id"],
                "doc_type": row["doc_type"],
                "committee": row["committee"],
                "date": row["date"],
                "content": row["content"],
                "speaker": row["speaker"],
                "start_time": row["start_time"],
                "end_time": row["end_time"],
                "score": round(score, 6),
                "corpus": "meetings",
            })
    return results


def enrich_corpus(db, chunk_ids_scores, corpus, limit=15):
    results = []
    seen_files = set()  # deduplicate by source file for code corpus

    for cid, score in chunk_ids_scores[:limit]:
        row = db.execute("SELECT * FROM chunks WHERE id = ?", (cid,)).fetchone()
        if not row:
            continue

        keys = row.keys()
        has_section = 'section_id' in keys
        d = dict(row)

        if has_section and corpus == "code":
            # For code: fetch the FULL LAW (all chunks from same file)
            src = d["source_file"]
            if src in seen_files:
                continue  # already included this law
            seen_files.add(src)

            all_chunks = db.execute("""
                SELECT content FROM chunks
                WHERE source_file = ?
                ORDER BY chunk_index
            """, (src,)).fetchall()
            full_law = '\n\n'.join(dict(c)["content"] for c in all_chunks)

            section_label = d.get("section_id") or ""
            if d.get("section_title"):
                section_label += f" {d['section_title']}"

            results.append({
                "chunk_id": d["id"],
                "source_file": src,
                "title": section_label or d.get("title", ""),
                "source": d.get("local_law_num") or "",
                "content": full_law,         # full law for display
                "full_context": full_law,     # full law for LLM
                "word_count": len(full_law.split()),
                "score": round(score, 6),
                "corpus": corpus,
            })
        else:
            # History: get neighboring chunks for context
            ci = d.get("chunk_index") or 0
            neighbors = db.execute("""
                SELECT content FROM chunks
                WHERE source_file = ? AND chunk_index BETWEEN ? AND ?
                ORDER BY chunk_index
            """, (d["source_file"], ci - 1, ci + 1)).fetchall()
            full_content = '\n'.join(dict(n)["content"] for n in neighbors)

            results.append({
                "chunk_id": d["id"],
                "source_file": d["source_file"],
                "title": d.get("title") or "",
                "source": d.get("source") or "",
                "content": d["content"],
                "full_context": full_content,
                "word_count": d["word_count"],
                "score": round(score, 6),
                "corpus": corpus,
            })
    return results


def search(query, corpus="meetings", limit=15):
    """Search a specific corpus. Returns enriched results."""
    db = _get_db(corpus)
    if not db:
        return []

    kw = keyword_search(db, query, limit=50)
    q_emb = embed_query(query)
    vec = vector_search(db, corpus, q_emb, limit=50) if q_emb else []
    merged = rrf([kw, vec])

    if corpus == "meetings":
        results = enrich_meetings(db, merged, limit)
    else:
        results = enrich_corpus(db, merged, corpus, limit)

    db.close()
    return results


def ask_llm(query, context_chunks, corpus):
    """Generate an answer using Qwen3-32B via Groq (fast) or OpenRouter (fallback)."""
    if not GROQ_KEY and not OPENROUTER_KEY:
        return None

    import re

    # Build context — use full_context (with neighbor chunks) when available
    context_parts = []
    for i, chunk in enumerate(context_chunks[:8]):
        source_label = ""
        if corpus == "meetings":
            source_label = f"[{chunk.get('committee', '')} {chunk.get('date', '')}]"
            if chunk.get('speaker'):
                source_label += f" ({chunk['speaker']})"
        elif corpus == "code":
            source_label = f"[{chunk.get('title', '')}]"
        else:
            source_label = f"[{chunk.get('title', '')} — {chunk.get('source', '')}]"

        text = chunk.get('full_context', chunk['content'])
        context_parts.append(f"{source_label}\n{text}")

    context = "\n\n---\n\n".join(context_parts)

    system_prompt = """You answer questions about Croton-on-Hudson, NY for the local news site croton.news.

Rules:
- ONLY state facts that appear in the provided context. Never guess or infer numbers, dates, or rules not explicitly stated.
- If the context doesn't fully answer the question, say what it does tell you and what's missing.
- Cite your sources: name the document, section number, date, or speaker.
- When multiple sources conflict, prefer the most recent.
- Be concise: 2-4 paragraphs.
- No thinking tags."""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Question: {query}\n\nContext:\n{context}"},
    ]

    # Groq/Qwen3 primary, OpenRouter/Nemotron fallback
    providers = []
    if GROQ_KEY:
        providers.append(("groq", GROQ_URL, GROQ_KEY, GROQ_MODEL))
    if OPENROUTER_KEY:
        providers.append(("openrouter", OPENROUTER_URL, OPENROUTER_KEY, NEMOTRON_MODEL))

    for name, url, key, model in providers:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "User-Agent": "croton.news/1.0",
        }
        if name == "openrouter":
            headers["HTTP-Referer"] = "https://croton.news"
            headers["X-Title"] = "croton.news"

        payload = json.dumps({
            "model": model,
            "messages": messages,
            "max_tokens": 800,
            "temperature": 0.3,
        }).encode()

        try:
            import requests as _http
            timeout = 60 if name == "openrouter" else 30
            resp = _http.post(url, data=payload, headers=headers, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]

            # Strip thinking tags
            content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL)
            content = re.sub(r'<\|think\|>.*?<\|/think\|>', '', content, flags=re.DOTALL)
            content = content.strip()

            # Clean filler
            content = re.sub(r'^(?:Okay|Sure|Alright|Let me)[,.]?\s*', '', content)

            # Trim trailing incomplete sentence
            if content and content[-1] not in '.!?")\u2019':
                last = max(content.rfind('.'), content.rfind('!'), content.rfind('"'))
                if last > len(content) * 0.5:
                    content = content[:last + 1]

            if len(content) > 20:
                return content
        except Exception as e:
            print(f"LLM error ({name}): {e}", file=sys.stderr)
            continue

    return None


# CLI
if __name__ == "__main__":
    query = sys.argv[1] if len(sys.argv) > 1 else "zoning setback requirements"
    corpus = sys.argv[2] if len(sys.argv) > 2 else "code"

    print(f"Searching '{corpus}' for: \"{query}\"\n")
    results = search(query, corpus, limit=5)

    for i, r in enumerate(results):
        print(f"{i+1}. [{r.get('title', r.get('committee', ''))}] score={r['score']}")
        print(f"   {r['content'][:150]}...")
        print()

    if results:
        print("Generating answer...\n")
        answer = ask_llm(query, results, corpus)
        if answer:
            print(f"Answer:\n{answer}")
