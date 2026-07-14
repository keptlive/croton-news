"""
Ingest transcripts and meeting summaries into rag.db chunks table.

Usage:
    python3 ingest.py transcripts   # Ingest transcript JSON files
    python3 ingest.py minutes       # Ingest summaries.db articles
    python3 ingest.py all           # Both
    python3 ingest.py stats         # Show counts
"""

import json
import glob
import os
import re
import sqlite3
import sys

RAG_DB = os.path.join(os.path.dirname(__file__), "rag.db")
TRANSCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "transcripts")

# Summaries DB: check multiple possible locations
_SUMMARIES_CANDIDATES = [
    os.path.join(os.path.dirname(__file__), "..", "scrapers", "summaries.db"),  # local dev
    os.path.join(os.path.dirname(__file__), "..", "ecode360", "summaries.db"),  # VPS
]
SUMMARIES_DB = next((p for p in _SUMMARIES_CANDIDATES if os.path.exists(p)), _SUMMARIES_CANDIDATES[0])

# Map event titles to committee names
TITLE_TO_COMMITTEE = {
    "Board of Trustees": "Board Of Trustees",
    "Board of Trustees Work Session": "Board Of Trustees",
    "Board of Trustees Meeting": "Board Of Trustees",
    "Planning Board": "Planning Board",
    "Planning Board Meeting": "Planning Board",
    "Zoning Board of Appeals": "Zoning Board of Appeals",
    "Sustainability Committee": "Sustainability Committee",
    "Recreation Advisory Committee": "Recreation Advisory Committee",
    "Board of Education": "Board of Education",
    "CHUFSD Board of Education": "Board of Education",
    "CHUFSD Board of Education Regular Meeting": "Board of Education",
    "CHUFSD Board of Education Work Session": "Board of Education",
    "CHUFSD Board of Education Business Meeting": "Board of Education",
    "CHUFSD Board of Education Special Meeting": "Board of Education",
    "Board of Education Regular Meeting": "Board of Education",
    "Board of Education Work Session": "Board of Education",
    "Board of Education Business Meeting": "Board of Education",
    "Board of Education Special Meeting": "Board of Education",
}


def get_committee(title):
    """Resolve a transcript title to a committee name."""
    if not title:
        return None
    # Exact match
    if title in TITLE_TO_COMMITTEE:
        return TITLE_TO_COMMITTEE[title]
    # Partial match
    for key, val in TITLE_TO_COMMITTEE.items():
        if key.lower() in title.lower():
            return val
    return title  # Use title as-is if no match


MAX_CHUNK_CHARS = 1500  # Cap speaker turns to prevent oversized chunks


def merge_speaker_turns(utterances):
    """Merge consecutive utterances from the same speaker into turns.

    Each turn = one chunk with combined text and spanning timestamps.
    Long turns are split at sentence boundaries to stay under MAX_CHUNK_CHARS.
    """
    if not utterances:
        return []

    # First pass: merge consecutive same-speaker utterances
    raw_turns = []
    current = {
        "speaker": utterances[0].get("speaker", "Unknown"),
        "text": utterances[0].get("text", ""),
        "start": utterances[0].get("start", 0),
        "end": utterances[0].get("end", 0),
    }

    for u in utterances[1:]:
        speaker = u.get("speaker", "Unknown")
        if speaker == current["speaker"]:
            current["text"] += " " + u.get("text", "")
            current["end"] = u.get("end", current["end"])
        else:
            raw_turns.append(current)
            current = {
                "speaker": speaker,
                "text": u.get("text", ""),
                "start": u.get("start", 0),
                "end": u.get("end", 0),
            }

    raw_turns.append(current)

    # Second pass: split oversized turns
    turns = []
    for turn in raw_turns:
        if len(turn["text"]) <= MAX_CHUNK_CHARS:
            turns.append(turn)
        else:
            # Split at sentence boundaries
            text = turn["text"]
            duration = turn["end"] - turn["start"]
            total_chars = len(text)
            sentences = re.split(r'(?<=[.!?])\s+', text)
            chunk_text = ""
            for sent in sentences:
                if len(chunk_text) + len(sent) + 1 > MAX_CHUNK_CHARS and chunk_text:
                    # Estimate timestamps proportionally
                    frac_start = len(chunk_text) / total_chars if total_chars else 0
                    frac_end = (len(chunk_text) + len(sent)) / total_chars if total_chars else 1
                    turns.append({
                        "speaker": turn["speaker"],
                        "text": chunk_text.strip(),
                        "start": turn["start"] + duration * (1 - frac_start - (frac_end - frac_start)),
                        "end": turn["start"] + duration * frac_end,
                    })
                    chunk_text = sent
                else:
                    chunk_text = (chunk_text + " " + sent).strip()
            if chunk_text.strip():
                frac = (total_chars - len(chunk_text)) / total_chars if total_chars else 0
                turns.append({
                    "speaker": turn["speaker"],
                    "text": chunk_text.strip(),
                    "start": turn["start"] + duration * frac,
                    "end": turn["end"],
                })

    return turns


def ingest_transcripts(db):
    """Ingest transcript JSON files as speaker-turn chunks."""
    files = sorted(glob.glob(os.path.join(TRANSCRIPTS_DIR, "transcript-*.json")))
    if not files:
        print(f"No transcripts found in {TRANSCRIPTS_DIR}")
        return 0

    total_chunks = 0
    for filepath in files:
        with open(filepath) as f:
            data = json.load(f)

        event_id = str(data.get("event_id", ""))
        date = data.get("date", "")
        title = data.get("title", "")
        committee = get_committee(title)
        utterances = data.get("utterances", [])
        speaker_map = data.get("speaker_map", {})

        # Merge consecutive same-speaker utterances into turns
        turns = merge_speaker_turns(utterances)

        # Check if already ingested
        existing = db.execute(
            "SELECT COUNT(*) FROM chunks WHERE doc_id = ? AND doc_type = 'transcript'",
            (event_id,)
        ).fetchone()[0]
        if existing:
            print(f"  Skip {event_id} ({existing} chunks already exist)")
            continue

        chunk_count = 0
        # vote-critical short utterances ("So moved." / "Second." / "Aye.")
        # must survive the length filter — mover/seconder attribution was
        # being dropped from RAG entirely (2026-07-14 transcription audit)
        _vote_re = re.compile(
            r"\b(aye|nay|second(ed)?|so moved|opposed?|all in favor|abstain(ed)?|motion carries)\b",
            re.I)
        for i, turn in enumerate(turns):
            text = turn["text"].strip()
            if not text or (len(text) < 30 and not _vote_re.search(text)):
                continue  # Skip trivial utterances (but never vote responses)

            # Resolve speaker name from speaker_map
            speaker = turn["speaker"]
            if speaker_map:
                # speaker_map keys are "0", "1", etc. or "Speaker 0" -> name
                num = speaker.replace("Speaker ", "")
                if num in speaker_map:
                    speaker = speaker_map[num]
                elif speaker in speaker_map:
                    speaker = speaker_map[speaker]

            db.execute("""
                INSERT INTO chunks (doc_id, doc_type, committee, date, chunk_index,
                                    content, speaker, start_time, end_time, char_count)
                VALUES (?, 'transcript', ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                event_id, committee, date, i,
                text, speaker,
                turn["start"], turn["end"],
                len(text),
            ))
            chunk_count += 1

        total_chunks += chunk_count
        print(f"  {event_id} ({date}, {committee}): {len(utterances)} utterances -> {chunk_count} chunks")

    db.commit()
    return total_chunks


def chunk_text(text, max_chars=500, overlap=100):
    """Split text into overlapping paragraph-aligned chunks."""
    if not text or not text.strip():
        return []

    # Split on double newlines (paragraphs) first
    paragraphs = re.split(r'\n\s*\n', text.strip())

    chunks = []
    current = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        if len(current) + len(para) + 1 <= max_chars:
            current = (current + "\n\n" + para).strip()
        else:
            if current:
                chunks.append(current)
            # If single paragraph exceeds max, split by sentences
            if len(para) > max_chars:
                sentences = re.split(r'(?<=[.!?])\s+', para)
                current = ""
                for sent in sentences:
                    if len(current) + len(sent) + 1 <= max_chars:
                        current = (current + " " + sent).strip()
                    else:
                        if current:
                            chunks.append(current)
                        current = sent
            else:
                current = para

    if current:
        chunks.append(current)

    return chunks


def ingest_minutes(db):
    """Ingest summaries.db news articles as paragraph chunks."""
    if not os.path.exists(SUMMARIES_DB):
        print(f"Summaries DB not found: {SUMMARIES_DB}")
        return 0

    sdb = sqlite3.connect(SUMMARIES_DB)
    sdb.row_factory = sqlite3.Row
    rows = sdb.execute("""
        SELECT doc_id, committee, date, news_article, executive_summary, headline
        FROM summaries
        WHERE news_article IS NOT NULL AND news_article != ''
    """).fetchall()
    sdb.close()

    total_chunks = 0
    for row in rows:
        doc_id = row["doc_id"]
        committee = row["committee"]
        date = row["date"]
        text = row["news_article"]

        # Check if already ingested
        existing = db.execute(
            "SELECT COUNT(*) FROM chunks WHERE doc_id = ? AND doc_type = 'article'",
            (doc_id,)
        ).fetchone()[0]
        if existing:
            print(f"  Skip {doc_id} ({existing} chunks already exist)")
            continue

        # Chunk the article text
        chunks = chunk_text(text)
        chunk_count = 0
        for i, chunk in enumerate(chunks):
            if len(chunk.strip()) < 20:
                continue

            db.execute("""
                INSERT INTO chunks (doc_id, doc_type, committee, date, chunk_index,
                                    content, char_count)
                VALUES (?, 'article', ?, ?, ?, ?, ?)
            """, (doc_id, committee, date, i, chunk.strip(), len(chunk.strip())))
            chunk_count += 1

        total_chunks += chunk_count
        headline = row["headline"] or doc_id
        print(f"  {doc_id} ({date}): {headline[:50]} -> {chunk_count} chunks")

    db.commit()
    return total_chunks


def show_stats(db):
    """Print chunk statistics."""
    total = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    by_type = db.execute(
        "SELECT doc_type, COUNT(*), SUM(char_count) FROM chunks GROUP BY doc_type"
    ).fetchall()

    print(f"\nTotal chunks: {total}")
    for doc_type, count, chars in by_type:
        avg = chars // count if count else 0
        print(f"  {doc_type}: {count} chunks, {chars:,} chars total, ~{avg} avg chars/chunk")

    # Speaker stats for transcripts
    speakers = db.execute("""
        SELECT speaker, COUNT(*) as cnt FROM chunks
        WHERE doc_type = 'transcript' AND speaker IS NOT NULL
        GROUP BY speaker ORDER BY cnt DESC LIMIT 10
    """).fetchall()
    if speakers:
        print(f"\nTop speakers:")
        for speaker, cnt in speakers:
            print(f"  {speaker}: {cnt} chunks")

    # Date range
    dates = db.execute(
        "SELECT MIN(date), MAX(date) FROM chunks WHERE date IS NOT NULL"
    ).fetchone()
    if dates[0]:
        print(f"\nDate range: {dates[0]} to {dates[1]}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"

    db = sqlite3.connect(RAG_DB)

    if cmd in ("transcripts", "all"):
        print("=== Ingesting transcripts ===")
        n = ingest_transcripts(db)
        print(f"Inserted {n} transcript chunks\n")

    if cmd in ("minutes", "all"):
        print("=== Ingesting minutes/articles ===")
        n = ingest_minutes(db)
        print(f"Inserted {n} article chunks\n")

    if cmd in ("stats", "all"):
        show_stats(db)

    db.close()


if __name__ == "__main__":
    main()
