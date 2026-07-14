#!/usr/bin/env python3
"""Chunk meetings.minutes_text into the RAG index.

Before this existed, official minutes lived only in meetings.minutes_text —
never chunked, never searchable (67 meetings' worth as of the 2026-07-13
audit). Idempotent: only processes meetings that have minutes_text but no
doc_type='minutes' chunks yet.

Also rebuilds chunks_fts afterwards — the FTS index is external-content
(content=chunks) with no triggers, so ANY new chunks are invisible to
keyword search until a rebuild. Run this as the daily pipeline's index
stage (embeddings.py follows it to cover vector search).

Usage: ingest_minutes.py [--dry-run]
"""
import os
import re
import sqlite3
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RAG_DB = os.path.join(BASE_DIR, "rag.db")

sys.path.insert(0, BASE_DIR)
from ingest import chunk_text  # noqa: E402 — same chunking as transcripts


def ingest_articles(db, dry=False):
    """Chunk published articles into the RAG index.

    Article chunking silently stopped ~2026-05-12 when writing moved to the
    WireClaw publish path (61 meetings' articles invisible to search —
    2026-07-13 audit finding 6). Idempotent: skips meetings that already
    have doc_type='article' chunks.
    """
    rows = db.execute("""
        SELECT id, event_id, committee, date, article FROM meetings
        WHERE article IS NOT NULL AND article != ''
          AND COALESCE(event_id, 'meeting-' || id) NOT IN (
              SELECT DISTINCT doc_id FROM chunks WHERE doc_type = 'article')
          AND COALESCE(event_id, 'meeting-' || id) || '-transcript' NOT IN (
              SELECT DISTINCT doc_id FROM chunks WHERE doc_type = 'article')
    """).fetchall()
    total = 0
    for m in rows:
        doc_id = m["event_id"] or f"meeting-{m['id']}"
        text = m["article"]
        # strip shortcodes/markdown noise before chunking
        text = re.sub(r"\{\{[^}]+\}\}", "", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        pieces = chunk_text(text, max_chars=800, overlap=100)
        if dry:
            print(f"would ingest {len(pieces)} article chunks for {doc_id} ({m['date']})")
            continue
        for i, piece in enumerate(pieces):
            db.execute("""
                INSERT INTO chunks (doc_id, doc_type, committee, date, chunk_index,
                                    content, speaker, char_count)
                VALUES (?, 'article', ?, ?, ?, ?, NULL, ?)
            """, (doc_id, m["committee"], m["date"], i, piece, len(piece)))
        total += len(pieces)
        print(f"ingested {len(pieces)} article chunks for {doc_id} ({m['date']} {m['committee']})")
    return total


def main():
    dry = "--dry-run" in sys.argv
    db = sqlite3.connect(RAG_DB)
    db.row_factory = sqlite3.Row

    rows = db.execute("""
        SELECT id, event_id, boarddocs_id, committee, date, minutes_text
        FROM meetings
        WHERE minutes_text IS NOT NULL AND minutes_text != ''
          AND COALESCE(event_id, boarddocs_id, 'meeting-' || id) NOT IN (
              SELECT DISTINCT doc_id FROM chunks WHERE doc_type = 'minutes')
    """).fetchall()

    total = 0
    for m in rows:
        # meetings with no event_id/boarddocs_id (advisory committees) were a
        # permanent black hole — fall back to a synthetic doc id (audit gap 3)
        doc_id = m["event_id"] or m["boarddocs_id"] or f"meeting-{m['id']}"
        text = re.sub(r"\n{3,}", "\n\n", m["minutes_text"]).strip()
        pieces = chunk_text(text, max_chars=800, overlap=100)
        if dry:
            print(f"would ingest {len(pieces)} minutes chunks for {doc_id} ({m['date']} {m['committee']})")
            continue
        for i, piece in enumerate(pieces):
            db.execute("""
                INSERT INTO chunks (doc_id, doc_type, committee, date, chunk_index,
                                    content, speaker, char_count)
                VALUES (?, 'minutes', ?, ?, ?, ?, NULL, ?)
            """, (doc_id, m["committee"], m["date"], i, piece, len(piece)))
        total += len(pieces)
        print(f"ingested {len(pieces)} minutes chunks for {doc_id} ({m['date']} {m['committee']})")

    article_total = ingest_articles(db, dry=dry)

    if not dry:
        db.commit()
        # self-heal: WireClaw enrichment re-ingests chunks by DELETE+INSERT
        # without touching embeddings, stranding orphans daily
        orphans = db.execute(
            "DELETE FROM embeddings WHERE chunk_id NOT IN (SELECT id FROM chunks)"
        ).rowcount
        # external-content FTS: rebuild so new chunks are keyword-searchable
        db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
        db.commit()
        print(f"done: {total} minutes + {article_total} article chunks; "
              f"{orphans} orphan embeddings purged; chunks_fts rebuilt")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
