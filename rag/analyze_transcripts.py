"""
Meeting transcript analysis pipeline for croton.news.

Two-stage analysis:
  1. Deepgram audio sentiment — re-process MP3s with sentiment=true for tone-based analysis
  2. LLM interest scoring — classify chunks by newsworthiness with tags

Usage:
    python3 analyze_transcripts.py deepgram <event_id>    # Re-transcribe one meeting with sentiment
    python3 analyze_transcripts.py deepgram all            # Re-transcribe all meetings
    python3 analyze_transcripts.py map <event_id>          # Map Deepgram sentiment → chunks
    python3 analyze_transcripts.py map all                 # Map all
    python3 analyze_transcripts.py interest <event_id>     # LLM interest scoring for one meeting
    python3 analyze_transcripts.py interest all            # LLM interest scoring for all
    python3 analyze_transcripts.py highlights <event_id>   # Generate highlight report
    python3 analyze_transcripts.py highlights all          # Generate all highlight reports
    python3 analyze_transcripts.py compare <event_id>      # Compare Deepgram vs LLM analysis
    python3 analyze_transcripts.py migrate                 # Add new columns to DB
    python3 analyze_transcripts.py stats                   # Show analysis coverage

Environment:
    DEEPGRAM_API_KEY    — Required for 'deepgram' command
    ZAI_KEY             — Override z.ai API key (default: from write_article.py)
"""

import json
import glob
import os
import re
import sqlite3
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# ── Paths ──

RAG_DB = os.path.join(os.path.dirname(__file__), "rag.db")
TRANSCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "transcripts")
SENTIMENT_DIR = os.path.join(os.path.dirname(__file__), "transcripts_sentiment")
HIGHLIGHTS_DIR = os.path.join(os.path.dirname(__file__), "highlights")

# Audio files: on VPS at /opt/croton-news/audio/, locally not available
# When running on VPS, this path works directly
AUDIO_DIR = os.environ.get("AUDIO_DIR", "/opt/croton-news/audio")

# ── API Config ──

DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY", "")
DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"

# z.ai for LLM interest scoring (same as write_article.py)
ZAI_KEY = os.environ.get("ZAI_KEY", "")
ZAI_URL = "https://api.z.ai/api/anthropic/v1/messages"

# ── DB Schema Migration ──


def migrate_db(db):
    """Add analysis columns to chunks table if missing."""
    # Check existing columns
    cols = {row[1] for row in db.execute("PRAGMA table_info(chunks)")}

    added = []
    if "interest_score" not in cols:
        db.execute("ALTER TABLE chunks ADD COLUMN interest_score REAL")
        added.append("interest_score")
    if "tags" not in cols:
        db.execute("ALTER TABLE chunks ADD COLUMN tags TEXT")
        added.append("tags")
    if "deepgram_sentiment" not in cols:
        db.execute("ALTER TABLE chunks ADD COLUMN deepgram_sentiment TEXT")
        added.append("deepgram_sentiment")
    if "deepgram_sentiment_score" not in cols:
        db.execute("ALTER TABLE chunks ADD COLUMN deepgram_sentiment_score REAL")
        added.append("deepgram_sentiment_score")

    if added:
        db.commit()
        print(f"Added columns: {', '.join(added)}")
    else:
        print("All columns already exist")

    # Create index for fast highlight queries
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_chunks_interest
        ON chunks(interest_score DESC) WHERE interest_score IS NOT NULL
    """)
    db.commit()


# ── Deepgram Sentiment Re-processing ──


def deepgram_transcribe_with_sentiment(audio_path, event_id):
    """Send audio to Deepgram with sentiment=true, diarize=true.

    Returns the full Deepgram response JSON.
    """
    if not DEEPGRAM_API_KEY:
        print("ERROR: Set DEEPGRAM_API_KEY environment variable")
        sys.exit(1)

    params = (
        "model=nova-3"
        "&diarize=true"
        "&punctuate=true"
        "&utterances=true"
        "&sentiment=true"
        "&smart_format=true"
        "&language=en"
    )
    url = f"{DEEPGRAM_URL}?{params}"

    file_size = os.path.getsize(audio_path)
    print(f"  Sending {audio_path} ({file_size / 1024 / 1024:.1f} MB) to Deepgram...")

    with open(audio_path, "rb") as f:
        audio_data = f.read()

    req = urllib.request.Request(url, data=audio_data, headers={
        "Authorization": f"Token {DEEPGRAM_API_KEY}",
        "Content-Type": "audio/mp3",
    })

    try:
        resp = urllib.request.urlopen(req, timeout=600)  # 10 min timeout for large files
        result = json.loads(resp.read())
        return result
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        print(f"  Deepgram API error {e.code}: {body[:500]}")
        return None
    except Exception as e:
        print(f"  Deepgram error: {e}")
        return None


def extract_sentiment_data(deepgram_response):
    """Extract structured sentiment data from Deepgram response.

    Deepgram returns sentiment at three levels:
    - Per-utterance: utterance.sentiment (str) + utterance.sentiment_score (float)
    - Per-segment: results.sentiments.segments[] with text spans
    - Per-word: word.sentiment (str) + word.sentiment_score (float)
    - Average: results.sentiments.average

    Returns:
        {
            "segments": [{"text", "start_word", "end_word", "sentiment", "score"}, ...],
            "average": {"sentiment", "score"},
            "utterances": [{"speaker", "text", "start", "end", "sentiment", "score"}, ...],
        }
    """
    results = deepgram_response.get("results", {})

    # Per-segment sentiment (sentence-level spans)
    sentiments = results.get("sentiments", {})
    segments = []
    for seg in sentiments.get("segments", []):
        segments.append({
            "text": seg.get("text", ""),
            "start_word": seg.get("start_word"),
            "end_word": seg.get("end_word"),
            "sentiment": seg.get("sentiment", "neutral"),
            "score": seg.get("sentiment_score", 0.0),
        })

    average = sentiments.get("average", {})
    avg_data = {
        "sentiment": average.get("sentiment", "neutral"),
        "score": average.get("sentiment_score", 0.0),
    }

    # Utterances — sentiment is directly on each utterance object
    utterances = []
    for utt in results.get("utterances", []):
        utterances.append({
            "speaker": utt.get("speaker", -1),
            "text": utt.get("transcript", ""),
            "start": utt.get("start", 0),
            "end": utt.get("end", 0),
            "sentiment": utt.get("sentiment", "neutral"),
            "score": round(utt.get("sentiment_score", 0.0), 4),
        })

    return {
        "segments": segments,
        "average": avg_data,
        "utterances": utterances,
    }


def process_deepgram(event_id):
    """Re-process one meeting's audio with Deepgram sentiment analysis."""
    audio_path = os.path.join(AUDIO_DIR, f"{event_id}.mp3")
    if not os.path.exists(audio_path):
        print(f"  Audio not found: {audio_path}")
        return False

    # Check if already processed
    out_path = os.path.join(SENTIMENT_DIR, f"sentiment-{event_id}.json")
    if os.path.exists(out_path):
        print(f"  Already processed: {out_path}")
        return True

    result = deepgram_transcribe_with_sentiment(audio_path, event_id)
    if not result:
        return False

    # Extract sentiment data
    sentiment_data = extract_sentiment_data(result)

    # Load original transcript for speaker_map
    orig_path = os.path.join(TRANSCRIPTS_DIR, f"transcript-{event_id}.json")
    speaker_map = {}
    orig_meta = {}
    if os.path.exists(orig_path):
        with open(orig_path) as f:
            orig = json.load(f)
            speaker_map = orig.get("speaker_map", {})
            orig_meta = {
                "title": orig.get("title"),
                "date": orig.get("date"),
                "committee": orig.get("title"),
            }

    # Save enriched result
    os.makedirs(SENTIMENT_DIR, exist_ok=True)
    output = {
        "event_id": event_id,
        **orig_meta,
        "speaker_map": speaker_map,
        "sentiment_average": sentiment_data["average"],
        "sentiment_segments": sentiment_data["segments"],
        "utterances_with_sentiment": sentiment_data["utterances"],
        "deepgram_metadata": result.get("metadata", {}),
        "processed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    n_seg = len(sentiment_data["segments"])
    n_utt = len(sentiment_data["utterances"])
    avg = sentiment_data["average"]
    print(f"  Saved: {out_path}")
    print(f"  {n_seg} sentiment segments, {n_utt} utterances")
    print(f"  Average sentiment: {avg['sentiment']} ({avg['score']:.3f})")
    return True


def cmd_deepgram(event_ids):
    """Run Deepgram sentiment analysis for specified meetings."""
    for eid in event_ids:
        print(f"\n=== Deepgram sentiment: event {eid} ===")
        process_deepgram(eid)
        time.sleep(1)  # Brief pause between API calls


# ── Map Deepgram Sentiment → Chunks ──


def map_sentiment_to_chunks(db, event_id):
    """Map Deepgram per-utterance sentiment onto existing chunks via timestamp overlap."""
    sent_path = os.path.join(SENTIMENT_DIR, f"sentiment-{event_id}.json")
    if not os.path.exists(sent_path):
        print(f"  No sentiment data for event {event_id}")
        return 0

    with open(sent_path) as f:
        data = json.load(f)

    utterances = data.get("utterances_with_sentiment", [])
    if not utterances:
        print(f"  No utterances with sentiment for event {event_id}")
        return 0

    # Load chunks for this event
    chunks = db.execute("""
        SELECT id, start_time, end_time FROM chunks
        WHERE doc_id = ? AND doc_type = 'transcript'
        ORDER BY start_time
    """, (str(event_id),)).fetchall()

    if not chunks:
        print(f"  No transcript chunks for event {event_id}")
        return 0

    updated = 0
    for chunk_id, c_start, c_end in chunks:
        if c_start is None or c_end is None:
            continue

        # Find overlapping utterances (within chunk's time range)
        overlapping = []
        for u in utterances:
            u_start = u.get("start", 0)
            u_end = u.get("end", 0)
            # Check overlap: utterance overlaps chunk if u_start < c_end and u_end > c_start
            if u_start < c_end and u_end > c_start:
                # Weight by overlap duration
                overlap_start = max(c_start, u_start)
                overlap_end = min(c_end, u_end)
                overlap_dur = overlap_end - overlap_start
                if overlap_dur > 0:
                    overlapping.append((u["score"], overlap_dur))

        if not overlapping:
            continue

        # Weighted average sentiment score by overlap duration
        total_weight = sum(dur for _, dur in overlapping)
        if total_weight > 0:
            weighted_score = sum(score * dur for score, dur in overlapping) / total_weight
        else:
            weighted_score = 0.0

        # Derive label
        if weighted_score > 0.333:
            label = "positive"
        elif weighted_score < -0.333:
            label = "negative"
        else:
            label = "neutral"

        db.execute("""
            UPDATE chunks
            SET deepgram_sentiment = ?, deepgram_sentiment_score = ?
            WHERE id = ?
        """, (label, round(weighted_score, 4), chunk_id))
        updated += 1

    db.commit()
    print(f"  Mapped sentiment to {updated}/{len(chunks)} chunks for event {event_id}")
    return updated


def cmd_map(db, event_ids):
    """Map Deepgram sentiment to chunks for specified meetings."""
    for eid in event_ids:
        print(f"\n=== Mapping sentiment: event {eid} ===")
        map_sentiment_to_chunks(db, eid)


# ── LLM Interest Scoring ──


def call_llm(system_prompt, user_prompt, model="claude-sonnet-4-20250514", max_tokens=4000):
    """Call LLM via z.ai API."""
    payload = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }).encode()

    req = urllib.request.Request(ZAI_URL, data=payload, headers={
        "Content-Type": "application/json",
        "x-api-key": ZAI_KEY,
    })

    try:
        resp = urllib.request.urlopen(req, timeout=120)
        data = json.loads(resp.read())
        return data["content"][0]["text"]
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        print(f"  LLM API error {e.code}: {body[:300]}")
        return None
    except Exception as e:
        print(f"  LLM error: {e}")
        return None


INTEREST_SYSTEM_PROMPT = """You are an analyst identifying the most newsworthy and interesting parts of municipal government meetings in Croton-on-Hudson, NY.

For each chunk of meeting transcript, assign:
1. **interest_score** (1-5):
   - 1 = Routine procedural (roll call, approval of minutes, adjournment)
   - 2 = Standard business (reports, updates, routine approvals)
   - 3 = Notable discussion (substantive debate, important info, community impact)
   - 4 = Highly newsworthy (contentious debate, major decisions, significant public comment, large financial items)
   - 5 = Must-report (explosive disagreements, landmark votes, shocking revelations, major policy shifts)

2. **tags** (comma-separated, from this list):
   - procedural: roll call, minutes approval, adjournment, scheduling
   - report: committee/staff reports, updates
   - debate: substantive back-and-forth, disagreement
   - vote: motions, resolutions, official votes
   - public-comment: residents addressing the board
   - financial: budget, contracts, grants, taxes, fees
   - policy: new rules, amendments, ordinances, regulations
   - development: construction, zoning, permits, subdivisions
   - infrastructure: roads, water, sewer, utilities
   - environment: parks, sustainability, conservation
   - safety: police, fire, emergency services
   - contentious: heated exchanges, raised voices, strong disagreement
   - revelation: new information disclosed for first time
   - humor: lighthearted moments, jokes

3. **sentiment** (overall tone):
   - positive, neutral, negative, contentious, mixed

Respond in JSON array format. Each element: {"id": <chunk_id>, "interest_score": N, "tags": "tag1,tag2", "sentiment": "label"}"""


def score_chunks_batch(db, event_id):
    """Score all chunks for a meeting using LLM in batches."""
    chunks = db.execute("""
        SELECT id, speaker, content, start_time
        FROM chunks
        WHERE doc_id = ? AND doc_type = 'transcript'
          AND interest_score IS NULL
        ORDER BY chunk_index
    """, (str(event_id),)).fetchall()

    if not chunks:
        print(f"  No unscored chunks for event {event_id}")
        return 0

    # Load meeting info for context
    meeting = db.execute(
        "SELECT committee, date FROM meetings WHERE event_id = ?",
        (int(event_id),)
    ).fetchone()
    meeting_ctx = ""
    if meeting:
        meeting_ctx = f"Meeting: {meeting[0]} on {meeting[1]}\n"

    # Process in batches of 25 chunks
    BATCH_SIZE = 25
    total_scored = 0

    for batch_start in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[batch_start:batch_start + BATCH_SIZE]
        batch_num = batch_start // BATCH_SIZE + 1
        total_batches = (len(chunks) + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"  Batch {batch_num}/{total_batches} ({len(batch)} chunks)...")

        # Build the user prompt with chunk content
        lines = [meeting_ctx, "Score these transcript chunks:\n"]
        for chunk_id, speaker, content, start_time in batch:
            ts = ""
            if start_time is not None:
                mins = int(start_time // 60)
                secs = int(start_time % 60)
                ts = f" [{mins:02d}:{secs:02d}]"
            lines.append(f"--- CHUNK {chunk_id}{ts} ---")
            lines.append(f"Speaker: {speaker or 'Unknown'}")
            lines.append(content[:800])  # Cap to avoid token overflow
            lines.append("")

        user_prompt = "\n".join(lines)

        result = call_llm(INTEREST_SYSTEM_PROMPT, user_prompt)
        if not result:
            print(f"    LLM call failed, skipping batch")
            continue

        # Parse JSON response
        try:
            # Extract JSON array from response (handle markdown code blocks)
            json_match = re.search(r'\[.*\]', result, re.DOTALL)
            if not json_match:
                print(f"    No JSON array found in response")
                continue
            scores = json.loads(json_match.group())
        except json.JSONDecodeError as e:
            print(f"    JSON parse error: {e}")
            continue

        # Apply scores to DB
        batch_ids = {c[0] for c in batch}
        for item in scores:
            cid = item.get("id")
            if cid not in batch_ids:
                continue

            interest = item.get("interest_score")
            tags = item.get("tags", "")
            sentiment = item.get("sentiment", "")

            if interest is not None:
                db.execute("""
                    UPDATE chunks
                    SET interest_score = ?, tags = ?, sentiment = ?, sentiment_score = ?
                    WHERE id = ?
                """, (
                    float(interest),
                    tags,
                    sentiment,
                    # Map text sentiment to numeric score for the legacy column
                    {"positive": 0.5, "neutral": 0.0, "negative": -0.5,
                     "contentious": -0.7, "mixed": 0.1}.get(sentiment, 0.0),
                    cid,
                ))
                total_scored += 1

        db.commit()
        time.sleep(1)  # Rate limit

    print(f"  Scored {total_scored}/{len(chunks)} chunks for event {event_id}")
    return total_scored


def cmd_interest(db, event_ids):
    """Run LLM interest scoring for specified meetings."""
    for eid in event_ids:
        print(f"\n=== LLM interest scoring: event {eid} ===")
        score_chunks_batch(db, eid)


# ── Highlight Reports ──


def generate_highlights(db, event_id):
    """Generate a Markdown highlight report for one meeting."""
    meeting = db.execute(
        "SELECT id, committee, date, headline, duration_seconds FROM meetings WHERE event_id = ?",
        (int(event_id),)
    ).fetchone()

    if not meeting:
        print(f"  No meeting record for event {event_id}")
        return None

    m_id, committee, date, headline, duration = meeting
    duration_str = f"{int(duration // 3600)}h {int((duration % 3600) // 60)}m" if duration else "?"

    # Get top chunks by interest score
    top_chunks = db.execute("""
        SELECT id, speaker, content, start_time, end_time,
               interest_score, tags, sentiment, sentiment_score,
               deepgram_sentiment, deepgram_sentiment_score
        FROM chunks
        WHERE doc_id = ? AND doc_type = 'transcript'
          AND interest_score IS NOT NULL
        ORDER BY interest_score DESC, deepgram_sentiment_score ASC
        LIMIT 30
    """, (str(event_id),)).fetchall()

    if not top_chunks:
        print(f"  No scored chunks for event {event_id}")
        return None

    # Get sentiment distribution
    sentiment_dist = db.execute("""
        SELECT sentiment, COUNT(*) FROM chunks
        WHERE doc_id = ? AND doc_type = 'transcript' AND sentiment IS NOT NULL
        GROUP BY sentiment
    """, (str(event_id),)).fetchall()

    dg_sentiment_dist = db.execute("""
        SELECT deepgram_sentiment, COUNT(*) FROM chunks
        WHERE doc_id = ? AND doc_type = 'transcript' AND deepgram_sentiment IS NOT NULL
        GROUP BY deepgram_sentiment
    """, (str(event_id),)).fetchall()

    # Get score distribution
    score_dist = db.execute("""
        SELECT
            CAST(interest_score AS INT) as score,
            COUNT(*) as cnt
        FROM chunks
        WHERE doc_id = ? AND doc_type = 'transcript' AND interest_score IS NOT NULL
        GROUP BY CAST(interest_score AS INT)
        ORDER BY score
    """, (str(event_id),)).fetchall()

    total_chunks = db.execute(
        "SELECT COUNT(*) FROM chunks WHERE doc_id = ? AND doc_type = 'transcript'",
        (str(event_id),)
    ).fetchone()[0]

    # Build Markdown report
    lines = []
    lines.append(f"# Meeting Highlights: {committee}")
    lines.append(f"**Date:** {date} | **Duration:** {duration_str} | **Event ID:** {event_id}")
    if headline:
        lines.append(f"**Headline:** {headline}")
    lines.append("")

    # Overview stats
    lines.append("## Overview")
    lines.append(f"- **Total segments:** {total_chunks}")
    lines.append(f"- **Scored:** {sum(c for _, c in score_dist)}")
    lines.append("")

    # Interest score distribution
    lines.append("### Interest Score Distribution")
    score_labels = {1: "Procedural", 2: "Standard", 3: "Notable", 4: "Newsworthy", 5: "Must-Report"}
    for score, cnt in score_dist:
        bar = "#" * min(cnt, 40)
        lines.append(f"  {score} ({score_labels.get(score, '?'):12s}): {bar} {cnt}")
    lines.append("")

    # Sentiment comparison
    if sentiment_dist or dg_sentiment_dist:
        lines.append("### Sentiment Analysis")
        if sentiment_dist:
            lines.append("**LLM (text-based):**")
            for sent, cnt in sorted(sentiment_dist):
                lines.append(f"  - {sent}: {cnt}")
        if dg_sentiment_dist:
            lines.append("**Deepgram (audio-based):**")
            for sent, cnt in sorted(dg_sentiment_dist):
                lines.append(f"  - {sent}: {cnt}")
        lines.append("")

    # Top highlights (interest >= 4)
    highlights = [c for c in top_chunks if c[5] >= 4]
    if highlights:
        lines.append(f"## Top Highlights ({len(highlights)} segments)")
        lines.append("")
        for i, (cid, speaker, content, start, end, iscore, tags, sent, sscore,
                 dg_sent, dg_score) in enumerate(highlights, 1):
            ts = format_timestamp(start)
            ts_end = format_timestamp(end)
            lines.append(f"### {i}. [{ts} - {ts_end}] {speaker or 'Unknown'}")
            lines.append(f"**Interest:** {'*' * int(iscore)} ({iscore:.0f}/5) | "
                         f"**Tags:** {tags or 'none'} | "
                         f"**LLM sentiment:** {sent or '?'}")
            if dg_sent:
                lines.append(f"**Deepgram sentiment:** {dg_sent} ({dg_score:+.3f})")
            lines.append("")
            # Show content, truncated
            lines.append(f"> {content[:500]}")
            lines.append("")

    # Notable discussion (interest = 3)
    notable = [c for c in top_chunks if 3 <= c[5] < 4]
    if notable:
        lines.append(f"## Notable Discussion ({len(notable)} segments)")
        lines.append("")
        for cid, speaker, content, start, end, iscore, tags, sent, sscore, dg_sent, dg_score in notable[:15]:
            ts = format_timestamp(start)
            lines.append(f"- **[{ts}] {speaker or 'Unknown'}** — {content[:150]}...")
            tag_str = f" `{tags}`" if tags else ""
            dg_str = f" | DG: {dg_sent}({dg_score:+.2f})" if dg_sent else ""
            lines.append(f"  Score: {iscore:.0f}{tag_str}{dg_str}")
        lines.append("")

    # Most negative moments (from Deepgram audio analysis)
    if any(c[10] is not None for c in top_chunks):
        neg_chunks = db.execute("""
            SELECT id, speaker, content, start_time, deepgram_sentiment_score,
                   interest_score, tags
            FROM chunks
            WHERE doc_id = ? AND doc_type = 'transcript'
              AND deepgram_sentiment = 'negative'
            ORDER BY deepgram_sentiment_score ASC
            LIMIT 10
        """, (str(event_id),)).fetchall()

        if neg_chunks:
            lines.append(f"## Most Negative Moments (Deepgram Audio)")
            lines.append("*These segments had negative tone detected in the audio signal.*\n")
            for cid, speaker, content, start, dg_score, iscore, tags in neg_chunks:
                ts = format_timestamp(start)
                i_str = f" | Interest: {iscore:.0f}" if iscore else ""
                lines.append(f"- **[{ts}] {speaker or 'Unknown'}** (score: {dg_score:+.3f}{i_str})")
                lines.append(f"  > {content[:200]}...")
            lines.append("")

    # Save report
    os.makedirs(HIGHLIGHTS_DIR, exist_ok=True)
    out_path = os.path.join(HIGHLIGHTS_DIR, f"highlights-{event_id}.md")
    report = "\n".join(lines)
    with open(out_path, "w") as f:
        f.write(report)

    print(f"  Saved: {out_path}")
    n_high = len([c for c in top_chunks if c[5] >= 4])
    n_notable = len([c for c in top_chunks if 3 <= c[5] < 4])
    print(f"  {n_high} highlights, {n_notable} notable segments")
    return out_path


def format_timestamp(seconds):
    """Format seconds as HH:MM:SS or MM:SS."""
    if seconds is None:
        return "??:??"
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"
    return f"{s // 60:02d}:{s % 60:02d}"


def cmd_highlights(db, event_ids):
    """Generate highlight reports for specified meetings."""
    for eid in event_ids:
        print(f"\n=== Highlights: event {eid} ===")
        generate_highlights(db, eid)


# ── Compare Deepgram vs LLM ──


def cmd_compare(db, event_id):
    """Compare Deepgram audio sentiment vs LLM text sentiment for a meeting."""
    chunks = db.execute("""
        SELECT id, speaker, content, start_time,
               sentiment, sentiment_score,
               deepgram_sentiment, deepgram_sentiment_score,
               interest_score, tags
        FROM chunks
        WHERE doc_id = ? AND doc_type = 'transcript'
          AND sentiment IS NOT NULL
          AND deepgram_sentiment IS NOT NULL
        ORDER BY start_time
    """, (str(event_id),)).fetchall()

    if not chunks:
        print(f"No chunks with both LLM and Deepgram sentiment for event {event_id}")
        return

    print(f"\n=== Sentiment Comparison: event {event_id} ({len(chunks)} chunks) ===\n")

    # Agreement stats
    agree = 0
    disagree_interesting = []
    for c in chunks:
        cid, speaker, content, start, llm_sent, llm_score, dg_sent, dg_score, interest, tags = c
        # Normalize: map 'contentious' and 'mixed' to negative/neutral for comparison
        llm_norm = {"contentious": "negative", "mixed": "neutral"}.get(llm_sent, llm_sent)
        if llm_norm == dg_sent:
            agree += 1
        else:
            disagree_interesting.append(c)

    pct = agree / len(chunks) * 100 if chunks else 0
    print(f"Agreement: {agree}/{len(chunks)} ({pct:.1f}%)")
    print(f"Disagreements: {len(disagree_interesting)}")
    print()

    # Show most interesting disagreements (where one says negative but other doesn't, or vice versa)
    if disagree_interesting:
        print("=== Most Interesting Disagreements ===")
        print("(Where audio tone and text content gave different signals)\n")
        # Sort by interest score descending
        disagree_interesting.sort(key=lambda x: (x[8] or 0), reverse=True)
        for c in disagree_interesting[:15]:
            cid, speaker, content, start, llm_sent, llm_score, dg_sent, dg_score, interest, tags = c
            ts = format_timestamp(start)
            print(f"[{ts}] {speaker or 'Unknown'} — Interest: {interest or '?'}")
            print(f"  LLM: {llm_sent} ({llm_score:+.2f}) vs Deepgram: {dg_sent} ({dg_score:+.3f})")
            print(f"  Tags: {tags or 'none'}")
            print(f"  > {content[:200]}...")
            print()


# ── Stats ──


def cmd_stats(db):
    """Show analysis coverage stats."""
    total = db.execute(
        "SELECT COUNT(*) FROM chunks WHERE doc_type = 'transcript'"
    ).fetchone()[0]

    with_interest = db.execute(
        "SELECT COUNT(*) FROM chunks WHERE interest_score IS NOT NULL"
    ).fetchone()[0]

    with_dg = db.execute(
        "SELECT COUNT(*) FROM chunks WHERE deepgram_sentiment IS NOT NULL"
    ).fetchone()[0]

    with_llm_sent = db.execute(
        "SELECT COUNT(*) FROM chunks WHERE sentiment IS NOT NULL"
    ).fetchone()[0]

    print(f"\n=== Analysis Coverage ===")
    print(f"Total transcript chunks: {total}")
    print(f"LLM interest scored:     {with_interest}/{total} ({with_interest/total*100:.1f}%)" if total else "")
    print(f"LLM sentiment:           {with_llm_sent}/{total} ({with_llm_sent/total*100:.1f}%)" if total else "")
    print(f"Deepgram audio sentiment: {with_dg}/{total} ({with_dg/total*100:.1f}%)" if total else "")

    # Per-meeting breakdown
    meetings = db.execute("""
        SELECT c.doc_id,
               m.committee,
               m.date,
               COUNT(*) as total,
               SUM(CASE WHEN c.interest_score IS NOT NULL THEN 1 ELSE 0 END) as scored,
               SUM(CASE WHEN c.deepgram_sentiment IS NOT NULL THEN 1 ELSE 0 END) as dg_scored
        FROM chunks c
        LEFT JOIN meetings m ON m.event_id = CAST(c.doc_id AS INTEGER)
        WHERE c.doc_type = 'transcript'
        GROUP BY c.doc_id
        ORDER BY m.date
    """).fetchall()

    print(f"\n{'Event':>6} {'Date':>10} {'Committee':>25} {'Total':>6} {'LLM':>5} {'DG':>5}")
    print("-" * 65)
    for doc_id, committee, date, total, scored, dg_scored in meetings:
        print(f"{doc_id:>6} {date or '?':>10} {(committee or '?')[:25]:>25} {total:>6} {scored:>5} {dg_scored:>5}")

    # Sentiment files on disk
    sent_files = glob.glob(os.path.join(SENTIMENT_DIR, "sentiment-*.json"))
    highlight_files = glob.glob(os.path.join(HIGHLIGHTS_DIR, "highlights-*.md"))
    print(f"\nSentiment JSON files: {len(sent_files)}")
    print(f"Highlight reports:    {len(highlight_files)}")


# ── Main ──


def get_event_ids(db, arg):
    """Resolve 'all' or a specific event_id to a list."""
    if arg == "all":
        rows = db.execute(
            "SELECT DISTINCT doc_id FROM chunks WHERE doc_type = 'transcript' ORDER BY doc_id"
        ).fetchall()
        return [r[0] for r in rows]
    return [arg]


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]

    db = sqlite3.connect(RAG_DB)

    if cmd == "migrate":
        migrate_db(db)

    elif cmd == "deepgram":
        if len(sys.argv) < 3:
            print("Usage: analyze_transcripts.py deepgram <event_id|all>")
            return
        event_ids = get_event_ids(db, sys.argv[2])
        cmd_deepgram(event_ids)

    elif cmd == "map":
        if len(sys.argv) < 3:
            print("Usage: analyze_transcripts.py map <event_id|all>")
            return
        migrate_db(db)
        event_ids = get_event_ids(db, sys.argv[2])
        cmd_map(db, event_ids)

    elif cmd == "interest":
        if len(sys.argv) < 3:
            print("Usage: analyze_transcripts.py interest <event_id|all>")
            return
        migrate_db(db)
        event_ids = get_event_ids(db, sys.argv[2])
        cmd_interest(db, event_ids)

    elif cmd == "highlights":
        if len(sys.argv) < 3:
            print("Usage: analyze_transcripts.py highlights <event_id|all>")
            return
        event_ids = get_event_ids(db, sys.argv[2])
        cmd_highlights(db, event_ids)

    elif cmd == "compare":
        if len(sys.argv) < 3:
            print("Usage: analyze_transcripts.py compare <event_id>")
            return
        cmd_compare(db, sys.argv[2])

    elif cmd == "stats":
        cmd_stats(db)

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)

    db.close()


if __name__ == "__main__":
    main()
