"""
Migrate from summaries.db (per-document) to meetings table in rag.db (per-meeting).

Consolidates multiple documents from the same date+committee into one meeting record.
Picks the best article when duplicates exist.

Usage:
    python3 migrate_meetings.py          # Run migration
    python3 migrate_meetings.py stats    # Show meeting stats
"""

import json
import glob
import os
import sqlite3
import sys

RAG_DB = os.path.join(os.path.dirname(__file__), "rag.db")
TRANSCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "transcripts")

_SUMMARIES_CANDIDATES = [
    os.path.join(os.path.dirname(__file__), "..", "scrapers", "summaries.db"),
    os.path.join(os.path.dirname(__file__), "..", "ecode360", "summaries.db"),
]
SUMMARIES_DB = next((p for p in _SUMMARIES_CANDIDATES if os.path.exists(p)), _SUMMARIES_CANDIDATES[0])


def migrate(rag_db):
    """Migrate summaries.db → meetings table in rag.db."""

    # Create meetings table
    schema_path = os.path.join(os.path.dirname(__file__), "meetings_schema.sql")
    with open(schema_path) as f:
        rag_db.executescript(f.read())

    # Load all summaries
    sdb = sqlite3.connect(SUMMARIES_DB)
    sdb.row_factory = sqlite3.Row
    rows = sdb.execute("""
        SELECT doc_id, committee, date, short_summary, executive_summary,
               news_article, headline, byline, model
        FROM summaries
        ORDER BY date DESC
    """).fetchall()
    sdb.close()

    # Load transcript metadata
    transcript_meta = {}
    for filepath in glob.glob(os.path.join(TRANSCRIPTS_DIR, "transcript-*.json")):
        with open(filepath) as f:
            data = json.load(f)
        eid = str(data.get("event_id", ""))
        transcript_meta[eid] = {
            "date": data.get("date"),
            "title": data.get("title", ""),
            "word_count": data.get("word_count"),
            "speaker_count": data.get("speaker_count"),
            "duration_seconds": data.get("duration_seconds"),
        }

    # Group summaries by date+committee
    from collections import defaultdict
    meetings = defaultdict(list)
    for row in rows:
        key = (row["date"], row["committee"])
        meetings[key].append(dict(row))

    created = 0
    skipped = 0

    for (date, committee), docs in meetings.items():
        # Check if already migrated
        existing = rag_db.execute(
            "SELECT id FROM meetings WHERE date = ? AND committee = ?",
            (date, committee)
        ).fetchone()
        if existing:
            skipped += 1
            continue

        # Collect source doc IDs (exclude transcript-based IDs)
        minutes_doc_ids = [d["doc_id"] for d in docs if not d["doc_id"].endswith("-transcript") and not d["doc_id"].endswith("-opus-news")]
        transcript_doc_ids = [d["doc_id"] for d in docs if d["doc_id"].endswith("-transcript")]
        opus_doc_ids = [d["doc_id"] for d in docs if d["doc_id"].endswith("-opus-news")]

        # Find event_id from transcript doc IDs
        event_id = None
        for tid in transcript_doc_ids + opus_doc_ids:
            eid = tid.split("-")[0]
            if eid in transcript_meta:
                event_id = eid
                break
        # Also check if any event_id matches this date
        if not event_id:
            for eid, meta in transcript_meta.items():
                if meta["date"] == date:
                    event_id = eid
                    break

        # Pick best article (prefer opus > transcript-based > minutes-based)
        best_article = None
        best_headline = None
        best_model = None
        for d in docs:
            if d["doc_id"].endswith("-opus-news"):
                best_article = d["news_article"]
                best_headline = d["headline"]
                best_model = d["model"]
                break
        if not best_article:
            for d in docs:
                if d["doc_id"].endswith("-transcript"):
                    best_article = d["news_article"]
                    best_headline = d["headline"]
                    best_model = d["model"]
                    break
        if not best_article:
            # Use the longest minutes-based article
            best_doc = max(docs, key=lambda d: len(d["news_article"] or ""))
            best_article = best_doc["news_article"]
            best_headline = best_doc["headline"]
            best_model = best_doc["model"]

        # Pick best quick summary
        best_quick = None
        for d in docs:
            if d["short_summary"] and len(d["short_summary"]) > 20:
                if not best_quick or len(d["short_summary"]) > len(best_quick):
                    best_quick = d["short_summary"]

        # Combine executive summaries for complete summary
        exec_parts = []
        for d in docs:
            if d["executive_summary"] and len(d["executive_summary"]) > 20:
                exec_parts.append(d["executive_summary"])
        complete_summary = "\n\n---\n\n".join(exec_parts) if exec_parts else None

        all_doc_ids = minutes_doc_ids + transcript_doc_ids + opus_doc_ids
        has_transcript = bool(event_id and event_id in transcript_meta)

        # Transcript metadata
        tmeta = transcript_meta.get(event_id, {})

        rag_db.execute("""
            INSERT OR IGNORE INTO meetings
                (date, committee, event_id, doc_ids,
                 has_transcript, has_minutes, has_video, has_audio,
                 quick_summary, complete_summary, article, headline,
                 word_count, speaker_count, duration_seconds,
                 article_model, article_generated_at,
                 summary_model, summary_generated_at)
            VALUES (?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, datetime('now'),
                    ?, datetime('now'))
        """, (
            date, committee, event_id, ",".join(all_doc_ids),
            1 if has_transcript else 0,
            1 if minutes_doc_ids else 0,
            1 if has_transcript else 0,  # video exists if transcript exists
            1 if has_transcript else 0,
            best_quick, complete_summary, best_article, best_headline,
            tmeta.get("word_count"),
            tmeta.get("speaker_count"),
            tmeta.get("duration_seconds"),
            best_model,
            best_model,
        ))
        created += 1

    rag_db.commit()
    print(f"Created {created} meetings, skipped {skipped} existing")


def show_stats(db):
    """Show meeting statistics."""
    total = db.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]
    print(f"\nTotal meetings: {total}")

    by_committee = db.execute(
        "SELECT committee, COUNT(*) FROM meetings GROUP BY committee ORDER BY 2 DESC"
    ).fetchall()
    print("\nBy committee:")
    for comm, cnt in by_committee:
        print(f"  {comm}: {cnt}")

    has_article = db.execute("SELECT COUNT(*) FROM meetings WHERE article IS NOT NULL AND article != ''").fetchone()[0]
    has_quick = db.execute("SELECT COUNT(*) FROM meetings WHERE quick_summary IS NOT NULL AND quick_summary != ''").fetchone()[0]
    has_complete = db.execute("SELECT COUNT(*) FROM meetings WHERE complete_summary IS NOT NULL AND complete_summary != ''").fetchone()[0]
    has_transcript = db.execute("SELECT COUNT(*) FROM meetings WHERE has_transcript = 1").fetchone()[0]
    has_video = db.execute("SELECT COUNT(*) FROM meetings WHERE has_video = 1").fetchone()[0]

    print(f"\nContent coverage:")
    print(f"  Quick summary:    {has_quick}/{total} ({100*has_quick//total}%)")
    print(f"  Complete summary: {has_complete}/{total} ({100*has_complete//total}%)")
    print(f"  Article:          {has_article}/{total} ({100*has_article//total}%)")
    print(f"  Transcript:       {has_transcript}/{total} ({100*has_transcript//total}%)")
    print(f"  Video/Audio:      {has_video}/{total} ({100*has_video//total}%)")

    # Missing content
    missing = db.execute("""
        SELECT date, committee,
               CASE WHEN quick_summary IS NULL OR quick_summary = '' THEN 1 ELSE 0 END as no_quick,
               CASE WHEN article IS NULL OR article = '' THEN 1 ELSE 0 END as no_article
        FROM meetings
        WHERE quick_summary IS NULL OR quick_summary = ''
           OR article IS NULL OR article = ''
        ORDER BY date DESC
    """).fetchall()
    if missing:
        print(f"\nMeetings missing content: {len(missing)}")
        for date, comm, no_q, no_a in missing[:10]:
            lacks = []
            if no_q:
                lacks.append("quick_summary")
            if no_a:
                lacks.append("article")
            print(f"  {date} {comm}: missing {', '.join(lacks)}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "migrate"
    db = sqlite3.connect(RAG_DB)

    if cmd == "migrate":
        print("=== Migrating summaries → meetings ===")
        migrate(db)
        show_stats(db)
    elif cmd == "stats":
        show_stats(db)

    db.close()


if __name__ == "__main__":
    main()
