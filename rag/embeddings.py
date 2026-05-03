"""
Embed all chunks in rag.db using Gemini embedding-2-preview.

Usage:
    python3 embeddings.py          # Embed all un-embedded chunks
    python3 embeddings.py stats    # Show embedding stats
    python3 embeddings.py test     # Test cosine similarity search
"""

import json
import os
import sqlite3
import struct
import sys
import time
import urllib.request
import urllib.error

RAG_DB = os.path.join(os.path.dirname(__file__), "rag.db")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = "gemini-embedding-2-preview"
DIMENSION = 3072  # gemini-embedding-2-preview
BATCH_SIZE = 50  # Gemini supports up to 100, but be conservative
BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


def embed_batch(texts):
    """Embed a batch of texts using Gemini batch API."""
    url = f"{BASE_URL}/models/{MODEL}:batchEmbedContents?key={GEMINI_API_KEY}"

    requests_body = []
    for text in texts:
        # Truncate to ~2000 chars to stay within token limits
        truncated = text[:2000] if len(text) > 2000 else text
        requests_body.append({
            "model": f"models/{MODEL}",
            "content": {"parts": [{"text": truncated}]},
        })

    payload = json.dumps({"requests": requests_body}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    resp = urllib.request.urlopen(req, timeout=60)
    data = json.loads(resp.read())

    embeddings = []
    for emb in data.get("embeddings", []):
        values = emb.get("values", [])
        embeddings.append(values)

    return embeddings


def float_list_to_blob(floats):
    """Pack a list of floats into a binary blob (float32)."""
    return struct.pack(f"{len(floats)}f", *floats)


def blob_to_float_list(blob):
    """Unpack a binary blob into a list of floats."""
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def cosine_similarity(a, b):
    """Compute cosine similarity between two float lists."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0
    return dot / (norm_a * norm_b)


def embed_all(db):
    """Embed all chunks that don't have embeddings yet."""
    # Find chunks without embeddings
    rows = db.execute("""
        SELECT c.id, c.content FROM chunks c
        LEFT JOIN embeddings e ON e.chunk_id = c.id
        WHERE e.chunk_id IS NULL
        ORDER BY c.id
    """).fetchall()

    total = len(rows)
    if total == 0:
        print("All chunks already embedded.")
        return 0

    print(f"Embedding {total} chunks in batches of {BATCH_SIZE}...")
    embedded = 0
    errors = 0

    for i in range(0, total, BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        chunk_ids = [r[0] for r in batch]
        texts = [r[1] for r in batch]

        try:
            embeddings = embed_batch(texts)

            for chunk_id, emb in zip(chunk_ids, embeddings):
                if not emb:
                    errors += 1
                    continue
                blob = float_list_to_blob(emb)
                db.execute("""
                    INSERT OR REPLACE INTO embeddings (chunk_id, embedding, model, dimension)
                    VALUES (?, ?, ?, ?)
                """, (chunk_id, blob, MODEL, len(emb)))

            db.commit()
            embedded += len(embeddings)
            pct = 100 * (i + len(batch)) / total
            print(f"  {i + len(batch)}/{total} ({pct:.0f}%) - batch OK")

        except urllib.error.HTTPError as e:
            body = e.read().decode()[:200]
            print(f"  Batch {i}-{i+len(batch)} FAILED: {e.code} {body}")
            errors += len(batch)
            # Rate limit — back off
            if e.code == 429:
                print("  Rate limited, waiting 30s...")
                time.sleep(30)
            else:
                time.sleep(2)

        except Exception as e:
            print(f"  Batch {i}-{i+len(batch)} ERROR: {e}")
            errors += len(batch)
            time.sleep(2)

        # Small delay to avoid rate limits
        time.sleep(0.5)

    print(f"\nDone: {embedded} embedded, {errors} errors")
    return embedded


def show_stats(db):
    """Show embedding statistics."""
    total_chunks = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    total_emb = db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    print(f"Chunks: {total_chunks}")
    print(f"Embeddings: {total_emb}")
    print(f"Coverage: {100 * total_emb / total_chunks:.1f}%")

    if total_emb > 0:
        dim = db.execute("SELECT dimension FROM embeddings LIMIT 1").fetchone()[0]
        model = db.execute("SELECT model FROM embeddings LIMIT 1").fetchone()[0]
        print(f"Model: {model}")
        print(f"Dimensions: {dim}")
        size = db.execute("SELECT SUM(LENGTH(embedding)) FROM embeddings").fetchone()[0]
        print(f"Storage: {size / 1024 / 1024:.1f} MB")


def test_search(db, query="affordable housing"):
    """Test vector search with a query."""
    print(f"\nVector search: \"{query}\"")

    # Embed query
    embeddings = embed_batch([query])
    if not embeddings or not embeddings[0]:
        print("Failed to embed query")
        return
    query_emb = embeddings[0]

    # Brute-force cosine similarity (fine for <10K chunks)
    rows = db.execute("""
        SELECT e.chunk_id, e.embedding, c.content, c.doc_id, c.date, c.speaker, c.doc_type
        FROM embeddings e
        JOIN chunks c ON c.id = e.chunk_id
    """).fetchall()

    scores = []
    for row in rows:
        emb = blob_to_float_list(row[1])
        sim = cosine_similarity(query_emb, emb)
        scores.append((sim, row[2], row[3], row[4], row[5], row[6]))

    scores.sort(reverse=True)
    print(f"Top 5 results:")
    for sim, content, doc_id, date, speaker, doc_type in scores[:5]:
        speaker_str = f" ({speaker})" if speaker else ""
        print(f"  [{doc_type}] {sim:.4f} | {doc_id} {date}{speaker_str}")
        print(f"    {content[:100]}...")
        print()


def main():
    if not GEMINI_API_KEY:
        print("Error: GEMINI_API_KEY not set")
        sys.exit(1)

    cmd = sys.argv[1] if len(sys.argv) > 1 else "embed"
    db = sqlite3.connect(RAG_DB)

    if cmd == "embed":
        embed_all(db)
        show_stats(db)
    elif cmd == "stats":
        show_stats(db)
    elif cmd == "test":
        query = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "affordable housing"
        test_search(db, query)
    else:
        print(f"Unknown command: {cmd}")

    db.close()


if __name__ == "__main__":
    main()
