#!/usr/bin/env python3
"""
Build a SQLite FTS5 full-text search index from Croton ecode360 documents.

Chunks documents into paragraphs, stores metadata (committee, date, type),
and enables fast keyword + phrase search across all municipal records.
"""

import json
import os
import re
import sqlite3

MINUTES_DIR = os.path.join(os.path.dirname(__file__), "minutes")
INDEX_PATH = os.path.join(os.path.dirname(__file__), "croton_document_index.json")
DB_PATH = os.path.join(os.path.dirname(__file__), "search.db")

# Chunk size target (chars). Paragraphs smaller than MIN get merged with next.
CHUNK_TARGET = 1500
CHUNK_MIN = 200


def chunk_text(text, target=CHUNK_TARGET, minimum=CHUNK_MIN):
    """Split text into semantic chunks at paragraph boundaries."""
    # Split on double newlines (paragraph breaks)
    paragraphs = re.split(r"\n\s*\n", text)
    chunks = []
    current = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        if len(current) + len(para) < target:
            current = current + "\n\n" + para if current else para
        else:
            if current and len(current) >= minimum:
                chunks.append(current)
            current = para

    if current and len(current) >= minimum:
        chunks.append(current)

    # If no chunks were created (very short doc), use entire text
    if not chunks and text.strip():
        chunks = [text.strip()]

    return chunks


def build_db():
    """Build the FTS5 search database from downloaded documents."""
    # Load document index
    with open(INDEX_PATH) as f:
        docs = json.load(f)

    doc_map = {d["doc_id"]: d for d in docs}

    # Create database
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Documents table
    c.execute("""
        CREATE TABLE documents (
            doc_id TEXT PRIMARY KEY,
            committee TEXT,
            date TEXT,
            type TEXT,
            text_size INTEGER,
            preview TEXT
        )
    """)

    # Chunks table with FTS5
    c.execute("""
        CREATE VIRTUAL TABLE chunks USING fts5(
            doc_id,
            committee,
            date,
            chunk_index,
            content,
            tokenize='porter unicode61'
        )
    """)

    # Also a regular chunks table for metadata queries
    c.execute("""
        CREATE TABLE chunk_meta (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id TEXT,
            committee TEXT,
            date TEXT,
            chunk_index INTEGER,
            char_count INTEGER
        )
    """)

    total_chunks = 0
    total_docs = 0

    for txt_file in sorted(os.listdir(MINUTES_DIR)):
        if not txt_file.endswith(".txt"):
            continue
        doc_id = txt_file.replace(".txt", "")

        # Only index Croton documents
        if doc_id not in doc_map:
            continue

        meta = doc_map[doc_id]
        text_path = os.path.join(MINUTES_DIR, txt_file)
        with open(text_path) as f:
            text = f.read()

        if len(text) < 20:
            continue

        committee = meta.get("committee", "unknown")
        date = meta.get("date") or "unknown"
        doc_type = meta.get("type", "document")
        preview = text[:200].strip().replace("\n", " ")

        # Insert document
        c.execute(
            "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?)",
            (doc_id, committee, date, doc_type, len(text), preview),
        )

        # Chunk and index
        chunks = chunk_text(text)
        for i, chunk in enumerate(chunks):
            c.execute(
                "INSERT INTO chunks VALUES (?, ?, ?, ?, ?)",
                (doc_id, committee, date, str(i), chunk),
            )
            c.execute(
                "INSERT INTO chunk_meta (doc_id, committee, date, chunk_index, char_count) VALUES (?, ?, ?, ?, ?)",
                (doc_id, committee, date, i, len(chunk)),
            )
            total_chunks += 1

        total_docs += 1

    conn.commit()

    # Print stats
    print(f"Indexed {total_docs} documents → {total_chunks} chunks")
    print(f"Database: {DB_PATH} ({os.path.getsize(DB_PATH):,} bytes)")

    # Test a search
    print("\n--- Test searches ---")
    for query in ["budget", "zoning variance", "planning board site plan", "police", "water sewer"]:
        c.execute(
            "SELECT doc_id, committee, date, snippet(chunks, 4, '>>>', '<<<', '...', 30) "
            "FROM chunks WHERE chunks MATCH ? ORDER BY rank LIMIT 3",
            (query,),
        )
        results = c.fetchall()
        print(f"\n'{query}': {len(results)} results")
        for r in results:
            print(f"  [{r[1]}] {r[2]} — ...{r[3][:100]}...")

    conn.close()


def search(query, committee=None, limit=10):
    """Search the index. Returns list of (doc_id, committee, date, snippet, rank)."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    if committee:
        c.execute(
            "SELECT doc_id, committee, date, snippet(chunks, 4, '>>>', '<<<', '...', 40), rank "
            "FROM chunks WHERE chunks MATCH ? AND committee = ? ORDER BY rank LIMIT ?",
            (query, committee, limit),
        )
    else:
        c.execute(
            "SELECT doc_id, committee, date, snippet(chunks, 4, '>>>', '<<<', '...', 40), rank "
            "FROM chunks WHERE chunks MATCH ? ORDER BY rank LIMIT ?",
            (query, limit),
        )

    results = c.fetchall()
    conn.close()
    return results


if __name__ == "__main__":
    build_db()
