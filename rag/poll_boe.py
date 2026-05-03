#!/usr/bin/env python3
"""
Poll CHUFSD YouTube channel for new Board of Education meeting videos.

Discovers new videos, extracts auto-captions, creates transcript JSON,
ingests chunks into rag.db, and optionally triggers article generation.

Usage:
    python3 poll_boe.py                  # Discover + ingest new videos
    python3 poll_boe.py --write          # Also generate articles for new meetings
    python3 poll_boe.py --list           # Just list what's new
    python3 poll_boe.py --ingest VIDEO_ID  # Process a specific video

Dependencies: requests, sqlite3
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from html import unescape

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RAG_DB = os.path.join(BASE_DIR, "rag.db")
TRANSCRIPTS_DIR = os.path.join(BASE_DIR, "transcripts")
WRITE_ARTICLE = os.path.join(BASE_DIR, "write_article.py")

# Only process videos from the last N days (skip ancient backlog)
from datetime import timedelta
LOOKBACK_DAYS = 90

CHANNEL_ID = "UC8PPKYkTdJs0GJ10k-lABXQ"  # Stream CHS (CHUFSD)
STREAMS_URL = f"https://www.youtube.com/channel/{CHANNEL_ID}/streams"

# Patterns for BOE meeting titles
BOE_PATTERNS = [
    r"Board of Education",
    r"BOE",
    r"CHUFSD.*Board",
    r"Board.*Meeting",
    r"Work Session",
    r"Budget.*Hearing",
    r"Budget.*Vote",
]


def log(msg):
    print(f"[boe] {msg}")


def get_channel_video_ids():
    """Get video IDs from the CHUFSD YouTube RSS feed (reliable, includes dates)."""
    rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
    try:
        req = urllib.request.Request(rss_url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; croton.news/1.0)"
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            xml_data = resp.read()

        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml_data)
        ns = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}

        ids = set()
        for entry in root.findall("atom:entry", ns):
            video_id = entry.find("yt:videoId", ns).text
            ids.add(video_id)
        return ids
    except Exception as e:
        log(f"Error fetching RSS: {e}")
        return set()


def get_existing_event_ids(db):
    """Get set of YouTube event IDs already in the database."""
    rows = db.execute(
        "SELECT event_id FROM meetings WHERE event_id LIKE 'yt-%'"
    ).fetchall()
    return {row["event_id"].replace("yt-", "") for row in rows}


def get_video_info(video_id):
    """Get video title and date via oEmbed API."""
    try:
        url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0"
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return data.get("title", "")
    except Exception:
        return ""


DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"
VIDEOS_DIR = os.path.join(BASE_DIR, "..", "videos")
VPS_HOST = "107.173.0.190"  # WireClaw VPS (has yt-dlp with cookies)
VPS_KEY = os.path.expanduser("~/.ssh/wireclaw_key") if os.path.exists(
    os.path.expanduser("~/.ssh/wireclaw_key")) else os.path.expanduser("~/.ssh/andy_vps_key")


def download_youtube_audio(video_id):
    """Download audio from YouTube via yt-dlp on WireClaw VPS."""
    audio_path = f"/tmp/boe-{video_id}.wav"

    # Try yt-dlp on WireClaw VPS (has browser cookies)
    log(f"  Downloading audio via WireClaw VPS...")
    script = (
        f"yt-dlp -x --audio-format wav --audio-quality 0 "
        f"-o '/tmp/boe-{video_id}.%(ext)s' "
        f"'https://www.youtube.com/watch?v={video_id}' 2>&1 && "
        f"echo 'OK'"
    )
    try:
        r = subprocess.run(
            ["ssh", "-i", VPS_KEY, "-o", "StrictHostKeyChecking=no",
             f"root@{VPS_HOST}", script],
            capture_output=True, text=True, timeout=300
        )
        if "OK" not in r.stdout:
            log(f"  yt-dlp failed: {r.stdout[-200:]}")
            return None

        # SCP audio file back
        subprocess.run(
            ["scp", "-i", VPS_KEY, "-o", "StrictHostKeyChecking=no",
             f"root@{VPS_HOST}:/tmp/boe-{video_id}.wav", audio_path],
            capture_output=True, timeout=120
        )
        if not os.path.exists(audio_path):
            log(f"  SCP failed")
            return None

        size_mb = os.path.getsize(audio_path) / 1024 / 1024
        log(f"  Downloaded: {size_mb:.0f} MB")
        return audio_path

    except Exception as e:
        log(f"  Download error: {e}")
        return None


def transcribe_audio(video_id, audio_path):
    """Transcribe audio with Deepgram Nova 3 + diarization."""
    api_key = os.environ.get("DEEPGRAM_API_KEY")
    if not api_key:
        log("  ERROR: DEEPGRAM_API_KEY not set")
        return None

    log(f"  Transcribing with Deepgram Nova 3...")
    params = "&".join([
        "model=nova-3", "diarize=true", "utterances=true", "smart_format=true",
        "language=en", "sentiment=true", "topics=true",
        "paragraphs=true", "summarize=v2",
    ])
    url = f"{DEEPGRAM_URL}?{params}"

    with open(audio_path, "rb") as f:
        audio_data = f.read()

    req = urllib.request.Request(url, data=audio_data, headers={
        "Authorization": f"Token {api_key}",
        "Content-Type": "audio/wav",
    })

    try:
        resp = urllib.request.urlopen(req, timeout=600)
        dg_result = json.loads(resp.read())
    except Exception as e:
        log(f"  Deepgram error: {e}")
        return None

    # Save raw response
    raw_dir = os.path.join(TRANSCRIPTS_DIR, "raw")
    os.makedirs(raw_dir, exist_ok=True)
    raw_path = os.path.join(raw_dir, f"deepgram-yt-{video_id}.json")
    with open(raw_path, "w") as f:
        json.dump(dg_result, f)

    # Parse utterances
    utterances = []
    for utt in dg_result.get("results", {}).get("utterances", []):
        utterances.append({
            "speaker": f"Speaker {utt.get('speaker', 0)}",
            "text": utt.get("transcript", ""),
            "start": utt.get("start", 0),
            "end": utt.get("end", 0),
            "timestamp": f"{int(utt.get('start', 0) // 60)}:{int(utt.get('start', 0) % 60):02d}",
        })

    full_text = " ".join(u["text"] for u in utterances)
    dg_summary = ""
    try:
        dg_summary = dg_result["results"]["summary"]["short"]
    except (KeyError, TypeError):
        pass

    log(f"  Transcribed: {len(full_text.split())} words, {len(utterances)} utterances")
    return {
        "utterances": utterances,
        "full_text": full_text,
        "word_count": len(full_text.split()),
        "speaker_count": len(set(u["speaker"] for u in utterances)),
        "duration_seconds": utterances[-1]["end"] if utterances else 0,
        "summary": dg_summary,
    }


def fetch_youtube_captions(video_id):
    """Fetch auto-captions via youtube-transcript-api (no auth needed from non-blocked IPs)."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        ytt = YouTubeTranscriptApi()
        transcript = ytt.fetch(video_id)
        entries = list(transcript)
        if not entries:
            return None

        utterances = []
        for e in entries:
            utterances.append({
                "speaker": "Unknown Speaker",
                "text": e.text,
                "start": e.start,
                "end": e.start + e.duration,
                "timestamp": f"{int(e.start // 60)}:{int(e.start % 60):02d}",
            })

        full_text = " ".join(e.text for e in entries)
        log(f"  Got captions: {len(full_text.split())} words via youtube-transcript-api")
        return {
            "utterances": utterances,
            "full_text": full_text,
            "word_count": len(full_text.split()),
            "speaker_count": 1,
            "duration_seconds": entries[-1].start + entries[-1].duration if entries else 0,
            "summary": "",
        }
    except Exception as e:
        log(f"  youtube-transcript-api failed: {type(e).__name__}: {str(e)[:100]}")
        return None


def parse_meeting_date(title):
    """Extract date from a meeting title like 'BOE Regular Meeting - March 12, 2026'."""
    # Try common patterns
    for fmt_pattern, fmt in [
        (r'(\w+ \d{1,2},?\s*\d{4})', '%B %d, %Y'),
        (r'(\d{1,2}/\d{1,2}/\d{2,4})', '%m/%d/%Y'),
        (r'(\d{1,2}/\d{1,2}/\d{2})', '%m/%d/%y'),
    ]:
        m = re.search(fmt_pattern, title)
        if m:
            try:
                dt = datetime.strptime(m.group(1).replace(",", ", ").replace("  ", " "), fmt)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
    return ""


def is_boe_meeting(title):
    """Check if a video title matches a BOE meeting pattern."""
    for pattern in BOE_PATTERNS:
        if re.search(pattern, title, re.I):
            return True
    return False


def create_transcript(video_id, title, date, captions):
    """Create a transcript JSON file from transcription data."""
    transcript = {
        "video_id": video_id,
        "youtube_url": f"https://www.youtube.com/watch?v={video_id}",
        "title": title,
        "date": date,
        "committee": "Board of Education",
        "meeting_type": "regular",
        "platform": "youtube",
        "source": "deepgram_nova3" if captions.get("speaker_count", 0) > 1 else "youtube_auto_captions",
        "diarization": "deepgram" if captions.get("speaker_count", 0) > 1 else "none",
        "full_text": captions["full_text"],
        "utterances": captions["utterances"],
        "word_count": captions["word_count"],
        "speaker_count": captions.get("speaker_count", 1),
        "duration_seconds": captions.get("duration_seconds", 0),
        "confirmed_speakers": {},
        "speaker_map": {},
        "note": "Speakers extracted from Deepgram diarization. Names require manual mapping.",
        "event_id": f"yt-{video_id}",
    }

    path = os.path.join(TRANSCRIPTS_DIR, f"transcript-yt-{video_id}.json")
    with open(path, "w") as f:
        json.dump(transcript, f, indent=2)
    log(f"  Saved transcript: {path}")
    return path


def ingest_transcript(db, video_id, transcript_path):
    """Chunk and insert transcript into rag.db."""
    with open(transcript_path) as f:
        t = json.load(f)

    event_id = f"yt-{video_id}"
    date = t.get("date", "")
    committee = "Board of Education"

    # Check if meeting row exists
    existing = db.execute(
        "SELECT id FROM meetings WHERE event_id = ?", (event_id,)
    ).fetchone()

    if not existing:
        db.execute("""
            INSERT INTO meetings (date, committee, event_id, has_transcript, has_video,
                                  has_audio, quick_summary)
            VALUES (?, ?, ?, 1, 0, 0, ?)
        """, (date, committee, event_id,
              f"Board of Education meeting covering district business and community discussion."))
        db.commit()
        log(f"  Created meeting row for {event_id}")

    # Check if chunks already exist
    existing_chunks = db.execute(
        "SELECT COUNT(*) as c FROM chunks WHERE doc_id = ?", (event_id,)
    ).fetchone()["c"]
    if existing_chunks > 0:
        log(f"  Chunks already exist ({existing_chunks}), skipping")
        return

    # Chunk the transcript (~800 chars per chunk)
    full_text = t.get("full_text", "")
    utterances = t.get("utterances", [])

    chunk_idx = 0
    current_chunk = ""
    current_start = 0

    for utt in utterances:
        text = utt.get("text", "")
        if not text:
            continue

        if len(current_chunk) + len(text) > 800:
            if current_chunk:
                db.execute("""
                    INSERT INTO chunks (doc_id, doc_type, committee, date, chunk_index,
                                       content, speaker, start_time, end_time, char_count)
                    VALUES (?, 'transcript', ?, ?, ?, ?, ?, ?, ?, ?)
                """, (event_id, committee, date, chunk_idx, current_chunk.strip(),
                      "Unknown Speaker", current_start, utt["start"], len(current_chunk)))
                chunk_idx += 1
            current_chunk = text + " "
            current_start = utt["start"]
        else:
            current_chunk += text + " "

    # Last chunk
    if current_chunk.strip():
        db.execute("""
            INSERT INTO chunks (doc_id, doc_type, committee, date, chunk_index,
                               content, speaker, start_time, end_time, char_count)
            VALUES (?, 'transcript', ?, ?, ?, ?, ?, ?, ?, ?)
        """, (event_id, committee, date, chunk_idx, current_chunk.strip(),
              "Unknown Speaker", current_start,
              utterances[-1]["end"] if utterances else 0,
              len(current_chunk)))

    db.commit()
    log(f"  Ingested {chunk_idx + 1} chunks for {event_id}")


def process_video(db, video_id, write_article=False):
    """Full pipeline for a single video: captions → transcript → ingest → article."""
    event_id = f"yt-{video_id}"
    transcript_path = os.path.join(TRANSCRIPTS_DIR, f"transcript-{event_id}.json")

    # Skip if transcript already exists
    if os.path.exists(transcript_path):
        log(f"  Transcript exists, checking DB...")
        ingest_transcript(db, video_id, transcript_path)
    else:
        title = get_video_info(video_id)
        if not title:
            log(f"  Could not get video info for {video_id}")
            return False

        if not is_boe_meeting(title):
            log(f"  Not a BOE meeting: {title}")
            return False

        date = parse_meeting_date(title)
        log(f"  {title} ({date})")

        # Try 1: youtube-transcript-api (auto-captions, no download needed)
        result = fetch_youtube_captions(video_id)

        # Try 2: Download audio + Deepgram transcription (if captions unavailable)
        audio_path = None
        if not result:
            log(f"  Falling back to audio download + Deepgram...")
            audio_path = download_youtube_audio(video_id)
            if not audio_path:
                log(f"  Audio download failed — skipping")
                return False
            result = transcribe_audio(video_id, audio_path)

        if not result or not result["utterances"]:
            log(f"  No transcript available")
            return False

        # Create transcript with Deepgram data
        create_transcript(video_id, title, date, result)
        ingest_transcript(db, video_id, transcript_path)

        # Clean up audio if downloaded
        if audio_path:
            try:
                os.remove(audio_path)
            except OSError:
                pass

    if write_article:
        existing = db.execute(
            "SELECT article FROM meetings WHERE event_id = ?", (event_id,)
        ).fetchone()
        if existing and existing["article"]:
            log(f"  Article already exists, skipping")
        else:
            log(f"  Generating article...")
            r = subprocess.run(
                ["python3", WRITE_ARTICLE, event_id, "--model", "claude-opus-4-5"],
                cwd=BASE_DIR, capture_output=True, text=True, timeout=900,
            )
            if r.returncode != 0:
                log(f"  Article generation failed: {r.stderr[-300:]}")
                return False
            log(f"  Article generated!")

    return True


def main():
    args = sys.argv[1:]
    write_articles = "--write" in args
    list_only = "--list" in args
    specific_id = None

    for i, arg in enumerate(args):
        if arg == "--ingest" and i + 1 < len(args):
            specific_id = args[i + 1]

    db = sqlite3.connect(RAG_DB)
    db.row_factory = sqlite3.Row

    if specific_id:
        log(f"Processing specific video: {specific_id}")
        process_video(db, specific_id, write_article=write_articles)
        db.close()
        return

    log("Checking CHUFSD YouTube channel for new BOE meetings...")
    channel_ids = get_channel_video_ids()
    existing_ids = get_existing_event_ids(db)

    new_ids = channel_ids - existing_ids
    log(f"Found {len(channel_ids)} videos, {len(existing_ids)} already tracked, {len(new_ids)} new")

    # Filter: only process videos that have a past date (skip future livestreams)
    today_str = datetime.now().strftime("%Y-%m-%d")

    if not new_ids:
        log("No new videos to process")
        db.close()
        return

    if list_only:
        for vid in sorted(new_ids):
            title = get_video_info(vid)
            print(f"  {vid}  {title}")
        db.close()
        return

    processed = 0
    skipped = 0
    for vid in sorted(new_ids):
        # Get title and check if it is a valid past meeting
        title = get_video_info(vid)
        if not title:
            skipped += 1
            continue
        if not is_boe_meeting(title):
            skipped += 1
            continue
        date = parse_meeting_date(title)
        # Skip future meetings (scheduled livestreams)
        if date and date > today_str:
            log(f"  Skipping future event: {title} ({date})")
            skipped += 1
            continue
        log(f"Processing {vid} — {title} ({date or 'no date'})...")
        if process_video(db, vid, write_article=write_articles):
            processed += 1

    db.close()
    log(f"Done: {processed} processed, {skipped} skipped")


if __name__ == "__main__":
    main()
