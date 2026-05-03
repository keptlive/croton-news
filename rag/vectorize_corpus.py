#!/usr/bin/env python3
"""
Vectorize history and code corpora for croton.news RAG.

Uses same Gemini embedding-2-preview (3072-dim) as the meetings DB.
Creates separate SQLite databases with FTS5 + vector search.

Usage:
    python vectorize_corpus.py history   # Vectorize history corpus → history.db
    python vectorize_corpus.py code      # Vectorize village code → code.db
    python vectorize_corpus.py stats     # Show stats for all DBs
"""

import json
import os
import re
import sqlite3
import struct
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = "gemini-embedding-2-preview"
DIMENSION = 3072
BATCH_SIZE = 50
BASE_URL = os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta")

# Corpus configs
CORPORA = {
    "history": {
        "db": SCRIPT_DIR / "history.db",
        "sources": [
            SCRIPT_DIR / "history" / "sources" / "archive_org",
            SCRIPT_DIR / "history" / "sources" / "web",
            SCRIPT_DIR / "history" / "sources" / "blogs" / "crotonhistory_org",
            SCRIPT_DIR / "history" / "sources" / "blogs" / "croton_friends",
            SCRIPT_DIR / "history" / "sources" / "government",
        ],
        "chunk_size": 800,  # words per chunk
        "chunk_overlap": 100,
    },
    "code": {
        "db": SCRIPT_DIR / "code.db",
        "sources": [
            SCRIPT_DIR / "croton-code" / "local-laws-text",
        ],
        "chunk_size": 500,  # smaller chunks for precise law lookup
        "chunk_overlap": 80,
    },
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file TEXT NOT NULL,
    title TEXT,
    source TEXT,
    chunk_index INTEGER,
    content TEXT NOT NULL,
    char_count INTEGER,
    word_count INTEGER
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    content, source_file, title,
    content='chunks',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, content, source_file, title)
    VALUES (new.id, new.content, new.source_file, new.title);
END;

CREATE TABLE IF NOT EXISTS embeddings (
    chunk_id INTEGER PRIMARY KEY,
    embedding BLOB NOT NULL,
    model TEXT,
    dimension INTEGER,
    FOREIGN KEY (chunk_id) REFERENCES chunks(id)
);

CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_file);
CREATE INDEX IF NOT EXISTS idx_chunks_title ON chunks(title);
"""


# ── Chunking ────────────────────────────────────────────────────────

def parse_frontmatter(text):
    """Extract YAML frontmatter and body from a text file."""
    if text.startswith('---'):
        parts = text.split('---', 2)
        if len(parts) >= 3:
            meta = {}
            for line in parts[1].strip().split('\n'):
                if ':' in line:
                    key, val = line.split(':', 1)
                    meta[key.strip()] = val.strip().strip('"')
            return meta, parts[2].strip()
    return {}, text


def chunk_text(text, chunk_size=800, overlap=100):
    """Split text into overlapping word-based chunks."""
    words = text.split()
    if len(words) <= chunk_size:
        return [text]

    chunks = []
    start = 0
    while start < len(words):
        end = start + chunk_size
        chunk = ' '.join(words[start:end])
        chunks.append(chunk)
        start = end - overlap

    return chunks


def load_and_chunk(source_dirs, chunk_size, chunk_overlap):
    """Load all .txt files from source dirs, chunk them."""
    all_chunks = []

    for src_dir in source_dirs:
        src_dir = Path(src_dir)
        if not src_dir.exists():
            print(f"  Warning: {src_dir} not found, skipping")
            continue

        # Handle both flat files and subdirectories
        txt_files = list(src_dir.glob("*.txt"))
        if not txt_files:
            # Check subdirectories
            txt_files = list(src_dir.rglob("*.txt"))

        for fpath in sorted(txt_files):
            with open(fpath) as f:
                text = f.read()

            if len(text.strip()) < 50:
                continue

            meta, body = parse_frontmatter(text)
            title = meta.get('title', fpath.stem)
            source = meta.get('source', '')

            # Skip PDFs that weren't extracted
            if fpath.suffix == '.pdf':
                continue

            chunks = chunk_text(body, chunk_size, chunk_overlap)

            for i, chunk in enumerate(chunks):
                words = len(chunk.split())
                if words < 10:
                    continue
                all_chunks.append({
                    'source_file': fpath.name,
                    'title': title,
                    'source': source,
                    'chunk_index': i,
                    'content': chunk,
                    'char_count': len(chunk),
                    'word_count': words,
                })

    return all_chunks


# ── Embedding ───────────────────────────────────────────────────────

def embed_batch(texts):
    """Embed a batch of texts using Gemini batch API."""
    url = f"{BASE_URL}/models/{MODEL}:batchEmbedContents?key={GEMINI_API_KEY}"

    requests_body = []
    for text in texts:
        truncated = text[:2000]
        requests_body.append({
            "model": f"models/{MODEL}",
            "content": {"parts": [{"text": truncated}]},
        })

    payload = json.dumps({"requests": requests_body}).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    resp = urllib.request.urlopen(req, timeout=60)
    data = json.loads(resp.read())

    return [emb.get("values", []) for emb in data.get("embeddings", [])]


def float_list_to_blob(floats):
    return struct.pack(f"{len(floats)}f", *floats)


# ── Main Pipeline ───────────────────────────────────────────────────

def vectorize(corpus_name):
    """Full pipeline: chunk → store → embed."""
    if corpus_name not in CORPORA:
        print(f"Unknown corpus: {corpus_name}")
        print(f"Available: {', '.join(CORPORA.keys())}")
        return

    config = CORPORA[corpus_name]
    db_path = config["db"]

    print(f"=== Vectorizing '{corpus_name}' corpus ===")
    print(f"Database: {db_path}")

    # 1. Init DB
    db = sqlite3.connect(str(db_path))
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)

    # 2. Check existing chunks
    existing = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    if existing > 0:
        print(f"  Database has {existing} existing chunks")
        resp = input("  Re-chunk from scratch? [y/N] ").strip().lower()
        if resp == 'y':
            db.execute("DELETE FROM embeddings")
            db.execute("DELETE FROM chunks")
            db.execute("DELETE FROM chunks_fts")
            db.commit()
            print("  Cleared existing data")
        else:
            print("  Keeping existing chunks, will only embed un-embedded ones")

    # 3. Chunk
    if db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0:
        print(f"\nChunking text files...")
        chunks = load_and_chunk(
            config["sources"],
            config["chunk_size"],
            config["chunk_overlap"],
        )
        print(f"  {len(chunks)} chunks from {len(set(c['source_file'] for c in chunks))} files")

        # Insert
        for c in chunks:
            db.execute("""
                INSERT INTO chunks (source_file, title, source, chunk_index, content, char_count, word_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (c['source_file'], c['title'], c['source'],
                  c['chunk_index'], c['content'], c['char_count'], c['word_count']))
        db.commit()
        print(f"  Inserted {len(chunks)} chunks")

    # 4. Embed
    rows = db.execute("""
        SELECT c.id, c.content FROM chunks c
        LEFT JOIN embeddings e ON e.chunk_id = c.id
        WHERE e.chunk_id IS NULL
        ORDER BY c.id
    """).fetchall()

    total = len(rows)
    if total == 0:
        print("All chunks already embedded.")
    else:
        print(f"\nEmbedding {total} chunks with {MODEL}...")
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
                print(f"  {i + len(batch)}/{total} ({pct:.0f}%)")

            except urllib.error.HTTPError as e:
                body = e.read().decode()[:200]
                print(f"  Batch FAILED: {e.code} {body}")
                errors += len(batch)
                if e.code == 429:
                    print("  Rate limited, waiting 30s...")
                    time.sleep(30)
                else:
                    time.sleep(2)

            except Exception as e:
                print(f"  Batch ERROR: {e}")
                errors += len(batch)
                time.sleep(2)

            time.sleep(0.5)

        print(f"\nDone: {embedded} embedded, {errors} errors")

    # 5. Stats
    show_stats_single(db, corpus_name)
    db.close()


def show_stats_single(db, name):
    """Show stats for a single database."""
    chunks = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    embs = db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    files = db.execute("SELECT COUNT(DISTINCT source_file) FROM chunks").fetchone()[0]
    words = db.execute("SELECT SUM(word_count) FROM chunks").fetchone()[0] or 0

    print(f"\n  {name}: {chunks} chunks, {embs} embedded, {files} files, {words:,} words")
    if embs > 0:
        size = db.execute("SELECT SUM(LENGTH(embedding)) FROM embeddings").fetchone()[0] or 0
        print(f"  Embedding storage: {size / 1024 / 1024:.1f} MB")


def show_stats():
    """Show stats for all corpora."""
    print("=== Corpus Stats ===\n")
    for name, config in CORPORA.items():
        db_path = config["db"]
        if db_path.exists():
            db = sqlite3.connect(str(db_path))
            show_stats_single(db, name)
            db.close()
        else:
            print(f"  {name}: not yet created")

    # Also show meetings DB
    rag_db = SCRIPT_DIR / "rag.db"
    if rag_db.exists():
        db = sqlite3.connect(str(rag_db))
        chunks = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        embs = db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        print(f"\n  meetings (rag.db): {chunks} chunks, {embs} embedded")
        db.close()


def main():
    if not GEMINI_API_KEY:
        print("Error: GEMINI_API_KEY not set")
        print("  export GEMINI_API_KEY=your_key_here")
        sys.exit(1)

    cmd = sys.argv[1] if len(sys.argv) > 1 else ""

    if cmd in CORPORA:
        vectorize(cmd)
    elif cmd == "stats":
        show_stats()
    elif cmd == "all":
        for name in CORPORA:
            vectorize(name)
    else:
        print("Usage:")
        print("  python vectorize_corpus.py history   # Vectorize history corpus")
        print("  python vectorize_corpus.py code      # Vectorize village code")
        print("  python vectorize_corpus.py all        # Both")
        print("  python vectorize_corpus.py stats      # Show stats")


if __name__ == "__main__":
    main()
