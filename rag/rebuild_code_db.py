#!/usr/bin/env python3
"""Rebuild code.db with consolidated chapters + local law amendments.

Chunks chapters at section boundaries (§) for precise legal search.
Keeps local laws chunked by word count with overlap.
"""
import json
import os
import re
import sqlite3
import struct
import sys
import time
import urllib.request

SCRIPT_DIR = "/opt/croton-news/rag"
CHAPTERS_DIR = os.path.join(SCRIPT_DIR, "croton-code", "chapters")
LAWS_DIR = os.path.join(SCRIPT_DIR, "croton-code", "local-laws-text")
DB_PATH = os.path.join(SCRIPT_DIR, "code.db")
BACKUP_PATH = DB_PATH + ".bak"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = "gemini-embedding-2-preview"
DIMENSION = 3072
BATCH_SIZE = 50
BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


def parse_frontmatter(text):
    """Extract YAML frontmatter and body."""
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


def chunk_chapter_by_section(text, chapter_num, chapter_name):
    """Split a chapter into chunks at section (§) boundaries.

    Merges small sections together to avoid tiny chunks.
    Each chunk gets section metadata.
    """
    chunks = []

    # Split at section markers: § NNN-N or § NNN-N.N
    # Handle PDF format where § may be indented with spaces
    # Match lines like "     § 108-1. Purpose; findings."
    parts = re.split(r'(?=^\s*§\s*\d)', text, flags=re.MULTILINE)

    current_chunk = ""
    current_section_id = ""
    current_section_title = ""

    for part in parts:
        part = part.strip()
        if not part:
            continue

        # Extract section ID if this starts with §
        # Match: "     § 108-1. Purpose; findings. [Amended ...]"
        part = part.strip()
        section_match = re.match(r'^(§\s*[\d\-\.]+[A-Z]?)\.?\s+(.+?)(?:\.|\[|$)', part)
        if section_match:
            section_id = section_match.group(1).strip()
            section_title = section_match.group(2).strip().rstrip('.')
        else:
            section_id = ""
            section_title = ""

        # If adding this part would make the chunk too big, flush
        if len(current_chunk) > 2000 and part:
            if current_chunk.strip() and len(current_chunk.strip()) >= 100:
                chunks.append({
                    "content": current_chunk.strip(),
                    "section_id": current_section_id,
                    "section_title": current_section_title,
                    "chapter_num": chapter_num,
                    "chapter_name": chapter_name,
                })
            current_chunk = part
            current_section_id = section_id or current_section_id
            current_section_title = section_title or current_section_title
        # If this part is small, merge with current
        elif len(part) < 300 and current_chunk:
            current_chunk += "\n\n" + part
            if section_id and not current_section_id:
                current_section_id = section_id
                current_section_title = section_title
        else:
            # Flush current if it has content
            if current_chunk.strip() and len(current_chunk.strip()) >= 100:
                chunks.append({
                    "content": current_chunk.strip(),
                    "section_id": current_section_id,
                    "section_title": current_section_title,
                    "chapter_num": chapter_num,
                    "chapter_name": chapter_name,
                })
            current_chunk = part
            current_section_id = section_id
            current_section_title = section_title

    # Don't forget the last chunk
    if current_chunk.strip() and len(current_chunk.strip()) >= 100:
        chunks.append({
            "content": current_chunk.strip(),
            "section_id": current_section_id,
            "section_title": current_section_title,
            "chapter_num": chapter_num,
            "chapter_name": chapter_name,
        })

    return chunks


def chunk_law_text(text, chunk_size=500, overlap=80):
    """Word-based chunking for local law amendments."""
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


def embed_batch(texts):
    """Embed a batch using Gemini."""
    url = f"{BASE_URL}/models/{MODEL}:batchEmbedContents?key={GEMINI_API_KEY}"
    requests_body = []
    for text in texts:
        truncated = text[:2000]
        requests_body.append({
            "model": f"models/{MODEL}",
            "content": {"parts": [{"text": truncated}]},
        })

    payload = json.dumps({"requests": requests_body}).encode()
    req = urllib.request.Request(url, data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    resp = urllib.request.urlopen(req, timeout=60)
    data = json.loads(resp.read())
    return [emb.get("values", []) for emb in data.get("embeddings", [])]


def float_list_to_blob(floats):
    return struct.pack(f'{len(floats)}f', *floats)


def main():
    if not GEMINI_API_KEY:
        print("ERROR: Set GEMINI_API_KEY")
        sys.exit(1)

    # Backup old DB
    if os.path.exists(DB_PATH):
        import shutil
        shutil.copy2(DB_PATH, BACKUP_PATH)
        print(f"Backed up old code.db to {BACKUP_PATH}")

    # Create new DB
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    db = sqlite3.connect(DB_PATH)
    db.execute("""CREATE TABLE chunks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_file TEXT NOT NULL,
        doc_type TEXT NOT NULL DEFAULT 'chapter',
        title TEXT,
        chapter_num INTEGER,
        chapter_name TEXT,
        section_id TEXT,
        section_title TEXT,
        local_law_num TEXT,
        date_filed TEXT,
        chunk_index INTEGER,
        content TEXT NOT NULL,
        embedding_content TEXT,
        char_count INTEGER,
        word_count INTEGER
    )""")

    db.execute("""CREATE VIRTUAL TABLE chunks_fts USING fts5(
        content, source_file, title, section_id, section_title,
        content='chunks', content_rowid='id',
        tokenize='porter unicode61'
    )""")

    db.execute("""CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
        INSERT INTO chunks_fts(rowid, content, source_file, title, section_id, section_title)
        VALUES (new.id, new.content, new.source_file, new.title, new.section_id, new.section_title);
    END""")

    db.execute("""CREATE TABLE embeddings (
        chunk_id INTEGER PRIMARY KEY,
        embedding BLOB NOT NULL,
        model TEXT,
        dimension INTEGER,
        FOREIGN KEY (chunk_id) REFERENCES chunks(id)
    )""")

    db.execute("CREATE INDEX idx_chunks_chapter ON chunks(chapter_num)")
    db.execute("CREATE INDEX idx_chunks_section ON chunks(section_id)")
    db.execute("CREATE INDEX idx_chunks_source ON chunks(source_file)")
    db.execute("CREATE INDEX idx_chunks_type ON chunks(doc_type)")

    total_chunks = 0

    # === PHASE 1: Ingest consolidated chapters ===
    print("\n=== Ingesting consolidated chapters ===")
    chapter_count = 0

    for fname in sorted(os.listdir(CHAPTERS_DIR)):
        if not fname.endswith('.txt'):
            continue

        fpath = os.path.join(CHAPTERS_DIR, fname)
        with open(fpath) as f:
            text = f.read()

        if len(text.strip()) < 100:
            continue

        meta, body = parse_frontmatter(text)
        title = meta.get('title', fname.replace('.txt', ''))

        # Extract chapter number and name from filename
        ch_match = re.match(r'Ch_(\d+)_(.+)\.txt', fname)
        if ch_match:
            chapter_num = int(ch_match.group(1))
            chapter_name = ch_match.group(2).replace('-', ' ').title()
        elif 'DL' in fname:
            chapter_num = 0
            chapter_name = "Disposition List"
        else:
            chapter_num = None
            chapter_name = title

        chunks = chunk_chapter_by_section(body, chapter_num, chapter_name)

        for i, chunk_data in enumerate(chunks):
            content = chunk_data["content"]
            db.execute("""INSERT INTO chunks
                (source_file, doc_type, title, chapter_num, chapter_name,
                 section_id, section_title, chunk_index, content, char_count, word_count)
                VALUES (?, 'chapter', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (fname, title, chunk_data["chapter_num"], chunk_data["chapter_name"],
                 chunk_data["section_id"], chunk_data["section_title"],
                 i, content, len(content), len(content.split())))
            total_chunks += 1

        chapter_count += 1
        print(f"  {fname}: {len(chunks)} chunks")

    db.commit()
    print(f"\nChapters: {chapter_count} files -> {total_chunks} chunks")

    # === PHASE 2: Ingest local law amendments ===
    print("\n=== Ingesting local law amendments ===")
    law_count = 0
    law_chunks_start = total_chunks

    for fname in sorted(os.listdir(LAWS_DIR)):
        if not fname.endswith('.txt'):
            continue

        fpath = os.path.join(LAWS_DIR, fname)
        with open(fpath) as f:
            text = f.read()

        if len(text.strip()) < 50:
            continue

        meta, body = parse_frontmatter(text)
        title = meta.get('title', fname.replace('.txt', ''))

        # Extract law number and chapter from filename/content
        law_match = re.match(r'LL_(\d+)-(\d+)\.txt', fname)
        law_num = f"LL {law_match.group(1)}-{law_match.group(2)}" if law_match else fname

        # Try to find chapter number from content
        ch_match = re.search(r'[Cc]hapter\s+(\d+)', body[:500])
        chapter_num = int(ch_match.group(1)) if ch_match else None

        # Get date
        date_filed = meta.get('date', '')

        chunks = chunk_law_text(body)

        for i, chunk in enumerate(chunks):
            if len(chunk.split()) < 10:
                continue
            db.execute("""INSERT INTO chunks
                (source_file, doc_type, title, chapter_num, local_law_num, date_filed,
                 chunk_index, content, char_count, word_count)
                VALUES (?, 'local_law', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (fname, title, chapter_num, law_num, date_filed,
                 i, chunk, len(chunk), len(chunk.split())))
            total_chunks += 1

        law_count += 1

    db.commit()
    law_chunks = total_chunks - law_chunks_start
    print(f"Local laws: {law_count} files -> {law_chunks} chunks")
    print(f"\nTotal: {total_chunks} chunks")

    # === PHASE 3: Generate embeddings ===
    print("\n=== Generating embeddings ===")

    rows = db.execute("SELECT id, content FROM chunks ORDER BY id").fetchall()
    embedded = 0
    errors = 0

    for i in range(0, len(rows), BATCH_SIZE):
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
                db.execute("INSERT INTO embeddings (chunk_id, embedding, model, dimension) VALUES (?, ?, ?, ?)",
                    (chunk_id, blob, MODEL, len(emb)))
            db.commit()
            embedded += len(embeddings)
            pct = 100 * (i + len(batch)) / len(rows)
            print(f"  {i + len(batch)}/{len(rows)} ({pct:.0f}%)")
        except Exception as e:
            print(f"  Batch error: {e}")
            errors += len(batch)
            if "429" in str(e):
                time.sleep(30)

    print(f"\nEmbedded: {embedded}, Errors: {errors}")

    # === Stats ===
    print("\n=== Final Stats ===")
    print(f"Chunks: {db.execute('SELECT count(*) FROM chunks').fetchone()[0]}")
    print(f"  Chapters: {db.execute('SELECT count(*) FROM chunks WHERE doc_type=''chapter''').fetchone()[0]}")
    print(f"  Local laws: {db.execute('SELECT count(*) FROM chunks WHERE doc_type=''local_law''').fetchone()[0]}")
    print(f"Embeddings: {db.execute('SELECT count(*) FROM embeddings').fetchone()[0]}")
    print(f"DB size: {os.path.getsize(DB_PATH) / 1024 / 1024:.1f} MB")

    db.close()


if __name__ == "__main__":
    main()
