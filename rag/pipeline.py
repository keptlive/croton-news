#!/usr/bin/env python3
"""
croton.news — Meeting Video Pipeline

End-to-end automation: discover new meetings → download video → transcribe → chunk → embed

Usage:
    python3 pipeline.py discover          # Discover & register new ChampDS meetings (with agendas)
    python3 pipeline.py process-new       # Full auto: discover → download → transcribe → enrich → ingest → write
    python3 pipeline.py download EVENT_ID # Download video for a single event
    python3 pipeline.py download-all      # Download all missing videos
    python3 pipeline.py transcribe EVENT_ID  # Transcribe video with Deepgram Nova 3
    python3 pipeline.py transcribe-all    # Transcribe all videos missing transcripts
    python3 pipeline.py enrich EVENT_ID   # Enrich transcript (proper nouns + speaker names)
    python3 pipeline.py enrich --all      # Enrich all transcripts
    python3 pipeline.py enrich --fix-names # Re-run speaker name correction only
    python3 pipeline.py ingest EVENT_ID   # Chunk and embed a transcript into rag.db
    python3 pipeline.py full EVENT_ID     # Download + transcribe + enrich + ingest
    python3 pipeline.py status            # Show pipeline status for all meetings

Requires:
    - ffmpeg (for video download)
    - DEEPGRAM_API_KEY environment variable (for transcription)
    - GEMINI_API_KEY environment variable (for embeddings)

ChampDS API:
    Portal: https://play.champds.com/crotononhudsonny/event/{event_id}
    API:    https://playapi.champds.com/crotononhudsonny/event/{event_id}
    HLS:    https://securestream7.champds.com/CrotonOnHudsonNYOD/_definst_{media_path}/playlist.m3u8?PLAY
"""

import json
import os
import sqlite3
import subprocess
import sys
import urllib.request
import urllib.error
import time

# Load .env file if present
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RAG_DB = os.path.join(BASE_DIR, "rag.db")
TRANSCRIPTS_DIR = os.path.join(BASE_DIR, "transcripts")
VIDEOS_DIR = os.path.join(os.path.dirname(BASE_DIR), "videos")
# On VPS, videos are at /opt/croton-news/videos
if not os.path.isdir(VIDEOS_DIR):
    VIDEOS_DIR = os.path.join(os.path.dirname(BASE_DIR), "site", "videos")
if not os.path.isdir(VIDEOS_DIR):
    VIDEOS_DIR = "/opt/croton-news/videos"

CHAMPDS_API = "https://playapi.champds.com/crotononhudsonny/event"
CHAMPDS_STREAM = "https://securestream7.champds.com/CrotonOnHudsonNYOD/_definst_"
DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"

# Board IDs that record video on ChampDS
VIDEO_BOARDS = {1: "Board of Trustees", 2: "Planning Board", 3: "Zoning Board of Appeals"}

COMMITTEE_MAP = {
    "Board of Trustees": "Board Of Trustees",
    "Board of Trustees Work Session": "Board of Trustees Work Session",
    "Board of Trustees Meeting": "Board Of Trustees",
    "Board of Trustees Organizational Meeting": "Board Of Trustees",
    "Planning Board": "Planning Board",
    "Planning Board Meeting": "Planning Board Meeting",
    "Zoning Board of Appeals": "Zoning Board of Appeals",
    "Zoning Board of Appeals Meeting": "Zoning Board of Appeals",
    "Waterfront Advisory Committee": "Waterfront Advisory Committee",
    "Waterfront Advisory Committee Meeting (WAC)": "Waterfront Advisory Committee Meeting (WAC)",
    "Conservation Advisory Council": "Conservation Advisory Council",
    "Advisory Board on the Visual Environment": "Advisory Board on the Visual Environment (VEB)",
    "Advisory Board on the Visual Environment (VEB)": "Advisory Board on the Visual Environment (VEB)",
    "Board of Education": "Board of Education",
    "CHUFSD Board of Education": "Board of Education",
    "Water Advisory Committee": "Water Advisory Committee",
    "Water Control Commission": "Water Control Commission",
    "Preview": "Preview",
    "Other": "Other",
}

# Max consecutive empty events before stopping dynamic scan
MAX_CONSECUTIVE_EMPTY = 20


def get_db():
    db = sqlite3.connect(RAG_DB)
    db.row_factory = sqlite3.Row
    return db


# ── Discovery ──────────────────────────────────────────────────────

def get_champds_event(event_id):
    """Fetch event metadata from ChampDS API."""
    url = f"{CHAMPDS_API}/{event_id}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read())
    except (urllib.error.HTTPError, urllib.error.URLError):
        return None


def extract_agenda(data):
    """Extract nested agenda items from ChampDS API response into simplified JSON.

    Returns list of dicts: [{title, description, children: [...], attachments: [{name, url}]}]
    or None if no agenda data.
    """
    agenda_data = data.get("Agenda", {})
    items = agenda_data.get("AgendaItems", [])
    if not items:
        return None

    CHAMPDS_ATT_BASE = "https://play.champds.com/ATT/crotononhudsonny"

    def walk(item_list):
        result = []
        for item in item_list:
            title = (item.get("Title") or "").strip()
            if not title:
                continue
            desc = (item.get("Description") or "").strip()
            # Extract attachments with download URLs
            attachments = []
            for att in item.get("Attachments", []):
                nick = (att.get("MediaNickName") or "").strip()
                mfile = att.get("MediaFileName") or ""
                mloc = att.get("MediaFileLocation") or ""
                mtype = att.get("MediaTypeID")
                if not nick:
                    continue
                if mtype == 2 or mfile.startswith("http"):
                    attachments.append({"name": nick, "url": mfile})
                elif mfile and mloc:
                    attachments.append({"name": nick, "url": f"{CHAMPDS_ATT_BASE}/{mloc}/{mfile}"})
            children = walk(item.get("Children", []))
            result.append({
                "title": title,
                "description": desc,
                "children": children,
                "attachments": attachments,
            })
        return result

    return walk(items)


def summarize_agenda(agenda_items):
    """Generate a quick_summary from agenda items for display before article exists."""
    if not agenda_items:
        return None

    # Collect substantive items (skip procedural: call to order, adjournment, pledge)
    skip_terms = {"call to order", "adjournment", "pledge of allegiance", "roll call"}
    substantive = []
    def collect(items):
        for item in items:
            title_lower = item["title"].lower()
            if not any(skip in title_lower for skip in skip_terms):
                substantive.append(item["title"])
            collect(item.get("children", []))
    collect(agenda_items)

    total = len(agenda_items)
    # Count all items including children
    def count_all(items):
        n = 0
        for item in items:
            n += 1
            n += count_all(item.get("children", []))
        return n
    total_all = count_all(agenda_items)

    if not substantive:
        return f"{total} agenda items scheduled."

    # Pick up to 3 key items for the summary
    highlights = substantive[:3]
    summary = f"{total_all} agenda items including {highlights[0]}"
    if len(highlights) > 1:
        summary += f", {highlights[1]}"
    if len(highlights) > 2:
        summary += f", and {highlights[2]}"
    summary += "."
    # Cap length
    if len(summary) > 300:
        summary = summary[:297] + "..."
    return summary


def discover_new_meetings(start_id=None, end_id=None):
    """Scan ChampDS event IDs, register new meetings in DB with agenda data.

    If start_id is None, begins from the highest known event_id in the DB.
    If end_id is None, scans dynamically until MAX_CONSECUTIVE_EMPTY empty events.
    Returns list of newly created meeting IDs.
    """
    db = get_db()
    known = {row["event_id"] for row in
             db.execute("SELECT event_id FROM meetings WHERE event_id IS NOT NULL").fetchall()}

    # Dynamic start: highest known event_id or default 1080
    if start_id is None:
        row = db.execute("""
            SELECT MAX(CAST(event_id AS INTEGER)) as max_eid
            FROM meetings WHERE event_id IS NOT NULL AND event_id NOT LIKE 'yt-%'
        """).fetchone()
        start_id = (row["max_eid"] or 1079) + 1

    dynamic_end = end_id is None
    if end_id is None:
        end_id = start_id + 500  # safety cap

    new_meeting_ids = []
    consecutive_empty = 0

    print(f"  Scanning from event {start_id}...")
    for eid in range(start_id, end_id + 1):
        eid_str = str(eid)
        if eid_str in known:
            consecutive_empty = 0  # known event resets counter
            continue

        data = get_champds_event(eid)
        time.sleep(0.3)  # rate limiting

        if not data or "Event" not in data:
            consecutive_empty += 1
            if dynamic_end and consecutive_empty >= MAX_CONSECUTIVE_EMPTY:
                print(f"  Stopping scan: {MAX_CONSECUTIVE_EMPTY} consecutive empty events after {eid}")
                break
            continue

        ev = data["Event"]
        title = ev.get("EventTitle", ev.get("Title", ""))
        if not title:
            consecutive_empty += 1
            if dynamic_end and consecutive_empty >= MAX_CONSECUTIVE_EMPTY:
                break
            continue

        consecutive_empty = 0  # valid event resets counter

        media = data.get("MediaInfo", {})
        media_type = media.get("MediaType", "")
        media_path = media.get("MediaPath", "") if media_type != "DISABLED" else ""
        date_raw = ev.get("EventDateTimeCustomerLocal",
                          ev.get("EventDateTimeUTC",
                                 ev.get("MeetingDate", "")))
        date = date_raw[:10] if date_raw else ""
        board = data.get("Board", {})
        raw_board_name = board.get("BoardName", "")

        # Map to canonical committee name, use title if it gives more detail
        committee = COMMITTEE_MAP.get(title, COMMITTEE_MAP.get(raw_board_name, title or raw_board_name))
        has_video = 1 if media_path else 0

        # Extract agenda
        agenda = extract_agenda(data)
        agenda_json_str = json.dumps(agenda) if agenda else None
        quick_summary = summarize_agenda(agenda) if agenda else None

        # Insert meeting into DB
        try:
            db.execute("""
                INSERT OR IGNORE INTO meetings
                (date, committee, event_id, has_video, agenda_json, media_path, board_name, quick_summary)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (date, committee, eid_str, has_video, agenda_json_str, media_path, raw_board_name, quick_summary))
            db.commit()

            # Check if it was actually inserted (not a duplicate)
            inserted = db.execute(
                "SELECT id FROM meetings WHERE event_id = ?", (eid_str,)
            ).fetchone()
            if inserted:
                new_meeting_ids.append(inserted["id"])
                n_agenda = len(agenda) if agenda else 0
                status = "VIDEO" if has_video else "agenda" if agenda else "registered"
                print(f"  {eid}: {title} ({date}) [{status}] {n_agenda} agenda items")
        except sqlite3.IntegrityError:
            pass  # UNIQUE constraint - meeting already exists for this date+committee

    db.close()
    return new_meeting_ids


def recheck_pending():
    """Re-check meetings that have no video yet — video may have been posted since discovery."""
    db = get_db()
    pending = db.execute("""
        SELECT id, event_id FROM meetings
        WHERE has_video = 0 AND event_id IS NOT NULL AND event_id NOT LIKE 'yt-%'
        AND date >= date('now', '-30 days')
    """).fetchall()

    updated = 0
    for row in pending:
        eid = row["event_id"]
        data = get_champds_event(int(eid))
        time.sleep(0.3)
        if not data:
            continue

        media = data.get("MediaInfo", {})
        media_type = media.get("MediaType", "")
        media_path = media.get("MediaPath", "")

        if media_path and media_type != "DISABLED":
            db.execute("""
                UPDATE meetings SET has_video = 1, media_path = ? WHERE id = ?
            """, (media_path, row["id"]))
            updated += 1
            print(f"  Event {eid}: video now available")

        # Also update agenda if it was missing
        existing = db.execute("SELECT agenda_json FROM meetings WHERE id = ?", (row["id"],)).fetchone()
        if not existing["agenda_json"]:
            agenda = extract_agenda(data)
            if agenda:
                agenda_json_str = json.dumps(agenda)
                quick_summary = summarize_agenda(agenda)
                db.execute("""
                    UPDATE meetings SET agenda_json = ?, quick_summary = COALESCE(quick_summary, ?)
                    WHERE id = ?
                """, (agenda_json_str, quick_summary, row["id"]))
                print(f"  Event {eid}: agenda updated ({len(agenda)} items)")

    db.commit()
    db.close()
    if updated:
        print(f"  {updated} meetings now have video available")
    return updated


# ── Video Download ─────────────────────────────────────────────────

def get_media_path(event_id):
    """Get HLS media path for an event from ChampDS API."""
    data = get_champds_event(event_id)
    if not data or "MediaInfo" not in data:
        return None
    return data["MediaInfo"].get("MediaPath", "")


def download_video(event_id):
    """Download meeting video via ffmpeg HLS."""
    out_path = os.path.join(VIDEOS_DIR, f"{event_id}.mp4")
    if os.path.exists(out_path):
        print(f"  Video already exists: {out_path}")
        return out_path

    media_path = get_media_path(event_id)
    if not media_path:
        print(f"  ERROR: No media path for event {event_id}")
        return None

    hls_url = f"{CHAMPDS_STREAM}{media_path}/playlist.m3u8?PLAY"
    print(f"  Downloading event {event_id}...")
    result = subprocess.run([
        "ffmpeg", "-headers", "Referer: https://play.champds.com/\r\n",
        "-i", hls_url, "-c", "copy", out_path, "-y", "-loglevel", "warning"
    ], capture_output=True, text=True)

    if result.returncode != 0:
        print(f"  ERROR: ffmpeg failed: {result.stderr[:200]}")
        return None

    size_mb = os.path.getsize(out_path) / 1024 / 1024
    print(f"  Downloaded: {out_path} ({size_mb:.0f} MB)")
    return out_path


# ── Transcription ──────────────────────────────────────────────────

def transcribe_video(event_id):
    """Transcribe video with Deepgram Nova 3 + diarization."""
    api_key = os.environ.get("DEEPGRAM_API_KEY")
    if not api_key:
        print("  ERROR: Set DEEPGRAM_API_KEY environment variable")
        return None

    video_path = os.path.join(VIDEOS_DIR, f"{event_id}.mp4")
    if not os.path.exists(video_path):
        print(f"  ERROR: Video not found: {video_path}")
        return None

    out_path = os.path.join(TRANSCRIPTS_DIR, f"transcript-{event_id}.json")
    if os.path.exists(out_path):
        print(f"  Transcript already exists: {out_path}")
        return out_path

    # Extract audio first (much smaller upload)
    audio_path = f"/tmp/{event_id}.wav"
    print(f"  Extracting audio...")
    subprocess.run([
        "ffmpeg", "-i", video_path, "-vn", "-acodec", "pcm_s16le",
        "-ar", "16000", "-ac", "1", audio_path, "-y", "-loglevel", "warning"
    ], check=True)

    # Upload to Deepgram — full analysis: sentiment, topics, detect_language
    print(f"  Transcribing with Deepgram Nova 3 (full analysis)...")
    params = "&".join([
        "model=nova-3", "diarize=true", "utterances=true", "smart_format=true",
        "language=en", "sentiment=true", "topics=true", "detect_language=true",
        "paragraphs=true", "summarize=v2",
    ])
    url = f"{DEEPGRAM_URL}?{params}"

    with open(audio_path, "rb") as f:
        audio_data = f.read()

    req = urllib.request.Request(url, data=audio_data, headers={
        "Authorization": f"Token {api_key}",
        "Content-Type": "audio/wav",
    })

    resp = urllib.request.urlopen(req, timeout=600)  # 10 min timeout for long meetings
    dg_result = json.loads(resp.read())

    # Save raw Deepgram response internally for analysis (sentiment, topics, etc.)
    raw_dir = os.path.join(TRANSCRIPTS_DIR, "raw")
    os.makedirs(raw_dir, exist_ok=True)
    raw_path = os.path.join(raw_dir, f"deepgram-{event_id}.json")
    with open(raw_path, "w") as f:
        json.dump(dg_result, f)
    print(f"  Raw Deepgram saved: {raw_path}")

    # Parse into clean public transcript format
    utterances = []
    for utt in dg_result.get("results", {}).get("utterances", []):
        entry = {
            "speaker": f"Speaker {utt.get('speaker', 0)}",
            "text": utt.get("transcript", ""),
            "start": utt.get("start", 0),
            "end": utt.get("end", 0),
            "timestamp": f"{int(utt.get('start', 0) // 60)}:{int(utt.get('start', 0) % 60):02d}",
        }
        # Store sentiment per-utterance internally
        sentiment = utt.get("sentiment")
        if sentiment:
            entry["sentiment"] = sentiment
        utterances.append(entry)

    # Get event metadata for title/date (ChampDS uses EventTitle, EventDateTimeUTC)
    ev_data = get_champds_event(event_id)
    ev = ev_data.get("Event", {}) if ev_data else {}
    title = ev.get("EventTitle", ev.get("Title", ""))
    date_raw = ev.get("EventDateTimeUTC", ev.get("MeetingDate", ""))
    date = date_raw[:10] if date_raw else ""

    # Extract Deepgram summaries and topics
    dg_summary = ""
    try:
        dg_summary = dg_result["results"]["summary"]["short"]
    except (KeyError, TypeError):
        pass
    dg_topics = []
    try:
        for seg in dg_result["results"]["topics"]["segments"]:
            for topic in seg.get("topics", []):
                t = topic.get("topic", "")
                if t and t not in dg_topics:
                    dg_topics.append(t)
    except (KeyError, TypeError):
        pass

    transcript = {
        "event_id": str(event_id),
        "title": title,
        "date": date,
        "platform": "deepgram-nova-3",
        "diarization": True,
        "full_text": " ".join(u["text"] for u in utterances),
        "utterances": utterances,
        "word_count": sum(len(u["text"].split()) for u in utterances),
        "speaker_count": len(set(u["speaker"] for u in utterances)),
        "speaker_map": {},
        "dg_summary": dg_summary,
        "dg_topics": dg_topics,
    }

    with open(out_path, "w") as f:
        json.dump(transcript, f, indent=2)

    # Cleanup temp audio
    os.remove(audio_path)

    # Update DB: mark transcript available and store word/speaker counts
    db = get_db()
    db.execute("""
        UPDATE meetings SET has_transcript = 1, word_count = ?, speaker_count = ?
        WHERE event_id = ?
    """, (transcript["word_count"], transcript["speaker_count"], str(event_id)))
    db.commit()
    db.close()

    print(f"  Transcribed: {transcript['word_count']} words, "
          f"{transcript['speaker_count']} speakers, {len(utterances)} utterances")
    return out_path


# ── Ingestion (chunk + embed) ──────────────────────────────────────

def ingest_transcript(event_id):
    """Chunk a transcript and insert into rag.db (uses existing ingest.py)."""
    transcript_path = os.path.join(TRANSCRIPTS_DIR, f"transcript-{event_id}.json")
    if not os.path.exists(transcript_path):
        print(f"  ERROR: Transcript not found: {transcript_path}")
        return False

    # Use existing ingest.py for chunking
    result = subprocess.run(
        [sys.executable, os.path.join(BASE_DIR, "ingest.py"), "transcripts"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  ERROR: Ingest failed: {result.stderr[:200]}")
        return False

    print(f"  Ingested transcript for event {event_id}")

    # Update meetings table
    db = get_db()
    db.execute("UPDATE meetings SET has_transcript = 1 WHERE event_id = ?", (str(event_id),))
    db.commit()
    db.close()

    # Run entity deduplication (prevents duplicate buildup)
    dedup_script = os.path.join(BASE_DIR, "dedup_entities.py")
    if os.path.exists(dedup_script):
        print(f"  Running entity deduplication...")
        subprocess.run([sys.executable, dedup_script], capture_output=True)

    # Enforce deleted_articles — permanently remove articles that were merged/killed
    enforce_deletions()

    return True


def enforce_deletions():
    """Remove any meetings that are in the deleted_articles table."""
    db = get_db()
    try:
        deleted = db.execute(
            "SELECT id FROM deleted_articles"
        ).fetchall()
        for row in deleted:
            existing = db.execute(
                "SELECT id FROM meetings WHERE id = ?", (row["id"],)
            ).fetchone()
            if existing:
                db.execute("DELETE FROM meetings WHERE id = ?", (row["id"],))
                print(f"  Enforced deletion: article {row['id']}")
        db.commit()
    except Exception:
        pass  # deleted_articles table may not exist yet
    db.close()


# ── Full Pipeline ──────────────────────────────────────────────────

def enrich_transcript_file(event_id):
    """Enrich a transcript: merge utterances, fix proper nouns, correct speaker names."""
    transcript_path = os.path.join(TRANSCRIPTS_DIR, f"transcript-{event_id}.json")
    if not os.path.exists(transcript_path):
        print(f"  ERROR: Transcript not found: {transcript_path}")
        return False

    # Import enrichment module
    try:
        enrich_module = os.path.join(BASE_DIR, "enrich_transcript.py")
        import importlib.util
        spec = importlib.util.spec_from_file_location("enrich_transcript", enrich_module)
        enrich = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(enrich)

        result = enrich.enrich_transcript(transcript_path)
        if not result:
            # May already be enriched, try name correction only
            enrich.fix_names_only(transcript_path)
        return True
    except Exception as e:
        print(f"  WARNING: Enrichment failed: {e}")
        return True  # Non-fatal, continue pipeline


def full_pipeline(event_id):
    """Run complete pipeline: download → transcribe → enrich → ingest."""
    print(f"\n{'='*60}")
    print(f"  PIPELINE: Event {event_id}")
    print(f"{'='*60}")

    print("\n[1/4] Downloading video...")
    video = download_video(event_id)
    if not video:
        return False

    print("\n[2/4] Transcribing...")
    transcript = transcribe_video(event_id)
    if not transcript:
        return False

    print("\n[3/4] Enriching transcript (proper nouns + speaker names)...")
    enrich_transcript_file(event_id)

    print("\n[4/4] Ingesting into rag.db...")
    success = ingest_transcript(event_id)

    if success:
        print(f"\n  COMPLETE: Event {event_id} fully processed")
    return success


# ── Process New (Full Automation) ─────────────────────────────────

def process_new():
    """Full automated pipeline: discover → re-check → download → transcribe → enrich → ingest → write.

    Each step is idempotent — safe to re-run. Designed for daily cron.
    """
    print(f"\n{'='*60}")
    print(f"  CROTON.NEWS PIPELINE — {time.strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")

    # Step 1: Discover new meetings
    print("\n[1/8] Discovering new meetings from ChampDS...")
    new_ids = discover_new_meetings()
    print(f"  → {len(new_ids)} new meetings registered")

    # Step 2: Re-check pending meetings for newly available video
    print("\n[2/8] Re-checking pending meetings for video...")
    recheck_pending()

    # Step 3: Download videos
    print("\n[3/8] Downloading missing videos...")
    db = get_db()
    to_download = db.execute("""
        SELECT event_id, media_path FROM meetings
        WHERE has_video = 1 AND has_transcript = 0
        AND event_id IS NOT NULL AND event_id NOT LIKE 'yt-%'
    """).fetchall()
    db.close()

    downloaded = 0
    for row in to_download:
        eid = int(row["event_id"])
        video_path = os.path.join(VIDEOS_DIR, f"{eid}.mp4")
        if not os.path.exists(video_path):
            result = download_video(eid)
            if result:
                downloaded += 1
    print(f"  → {downloaded} videos downloaded")

    # Step 4: Transcribe
    print("\n[4/8] Transcribing new videos...")
    db = get_db()
    to_transcribe = db.execute("""
        SELECT event_id FROM meetings
        WHERE has_video = 1 AND has_transcript = 0
        AND event_id IS NOT NULL AND event_id NOT LIKE 'yt-%'
    """).fetchall()
    db.close()

    transcribed = 0
    for row in to_transcribe:
        eid = int(row["event_id"])
        video_path = os.path.join(VIDEOS_DIR, f"{eid}.mp4")
        if os.path.exists(video_path):
            result = transcribe_video(eid)
            if result:
                transcribed += 1
    print(f"  → {transcribed} videos transcribed")

    # Step 5: Enrich
    print("\n[5/8] Enriching transcripts...")
    db = get_db()
    # Find meetings with transcripts that haven't been enriched
    # Enrichment modifies the transcript JSON in-place, adding "enriched": true
    to_enrich = db.execute("""
        SELECT event_id FROM meetings
        WHERE has_transcript = 1 AND event_id IS NOT NULL AND event_id NOT LIKE 'yt-%'
    """).fetchall()
    db.close()

    enriched = 0
    for row in to_enrich:
        eid = row["event_id"]
        transcript_path = os.path.join(TRANSCRIPTS_DIR, f"transcript-{eid}.json")
        if os.path.exists(transcript_path):
            with open(transcript_path) as f:
                tx = json.load(f)
            if not tx.get("enriched"):
                enrich_transcript_file(int(eid))
                enriched += 1
    print(f"  → {enriched} transcripts enriched")

    # Step 6: Ingest
    print("\n[6/8] Ingesting into RAG database...")
    db = get_db()
    to_ingest = db.execute("""
        SELECT event_id FROM meetings
        WHERE has_transcript = 1 AND event_id IS NOT NULL AND event_id NOT LIKE 'yt-%'
    """).fetchall()
    db.close()

    ingested = 0
    for row in to_ingest:
        eid = row["event_id"]
        # Check if chunks already exist for this event
        db2 = get_db()
        existing = db2.execute(
            "SELECT COUNT(*) as n FROM chunks WHERE doc_id = ?", (eid,)
        ).fetchone()
        db2.close()
        if existing["n"] == 0:
            if ingest_transcript(int(eid)):
                ingested += 1
    print(f"  → {ingested} transcripts ingested")

    # Step 7: Write articles
    print("\n[7/8] Generating articles for meetings without coverage...")
    db = get_db()
    to_write = db.execute("""
        SELECT id, event_id, committee, date FROM meetings
        WHERE has_transcript = 1
        AND (article IS NULL OR article = '')
        AND event_id IS NOT NULL
    """).fetchall()
    db.close()

    written = 0
    write_script = os.path.join(BASE_DIR, "write_article.py")
    if os.path.exists(write_script):
        for row in to_write:
            eid = row["event_id"]
            print(f"  Writing article for {row['committee']} ({row['date']})...")
            result = subprocess.run(
                [sys.executable, write_script, eid],
                capture_output=True, text=True, timeout=300
            )
            if result.returncode == 0:
                written += 1
            else:
                print(f"    WARNING: Article generation failed: {result.stderr[:200]}")
    print(f"  → {written} articles generated")

    # Step 8: Insert photos into articles
    print("\n[8/8] Inserting photos into articles...")
    photo_script = os.path.join(BASE_DIR, "insert_photos.py")
    photos_inserted = 0
    if os.path.exists(photo_script):
        db = get_db()
        needs_photos = db.execute("""
            SELECT event_id, committee, date FROM meetings
            WHERE article IS NOT NULL AND article != ''
            AND article NOT LIKE '%{{photo:%'
            AND has_video = 1
            AND event_id IS NOT NULL AND event_id NOT LIKE 'yt-%'
            ORDER BY date DESC
        """).fetchall()
        db.close()

        for row in needs_photos:
            eid = row["event_id"]
            print(f"  Photos for {row['committee']} ({row['date']})...")
            try:
                result = subprocess.run(
                    [sys.executable, photo_script, eid],
                    capture_output=True, text=True, timeout=600
                )
                if result.returncode == 0:
                    photos_inserted += 1
                else:
                    print(f"    WARNING: Photo insertion failed: {result.stderr[:200]}")
            except subprocess.TimeoutExpired:
                print(f"    WARNING: Photo insertion timed out for {eid}")
    print(f"  → {photos_inserted} articles got photos")

    print(f"\n{'='*60}")
    print(f"  PIPELINE COMPLETE")
    print(f"  New meetings: {len(new_ids)} | Downloaded: {downloaded} | "
          f"Transcribed: {transcribed} | Articles: {written} | Photos: {photos_inserted}")
    print(f"{'='*60}\n")


# ── Status ─────────────────────────────────────────────────────────

def show_status():
    """Show pipeline status for all meetings."""
    db = get_db()
    meetings = db.execute("""
        SELECT id, event_id, committee, date, has_video, has_transcript,
               CASE WHEN article IS NOT NULL AND article != '' THEN 1 ELSE 0 END as has_article,
               CASE WHEN agenda_json IS NOT NULL THEN 1 ELSE 0 END as has_agenda
        FROM meetings ORDER BY date DESC
    """).fetchall()

    print(f"\n{'ID':>3} {'Event':>6} {'Date':>10} {'Vid':>3} {'Tx':>3} {'Art':>3} {'Ag':>3}  Committee")
    print("-" * 85)
    for m in meetings:
        vid = "Y" if m["has_video"] else "-"
        tx = "Y" if m["has_transcript"] else "-"
        art = "Y" if m["has_article"] else "-"
        ag = "Y" if m["has_agenda"] else "-"
        eid = m["event_id"] or "-"
        print(f"{m['id']:>3} {eid:>6} {m['date']:>10} {vid:>3} {tx:>3} {art:>3} {ag:>3}  {m['committee']}")

    # Summary
    total = len(meetings)
    with_agenda = sum(1 for m in meetings if m["has_agenda"])
    with_video = sum(1 for m in meetings if m["has_video"])
    with_transcript = sum(1 for m in meetings if m["has_transcript"])
    with_article = sum(1 for m in meetings if m["has_article"])
    print(f"\nTotal: {total} | Agendas: {with_agenda} | Videos: {with_video} | "
          f"Transcripts: {with_transcript} | Articles: {with_article}")

    db.close()


# ── CLI ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "process-new":
        process_new()

    elif cmd == "discover":
        start = int(sys.argv[2]) if len(sys.argv) > 2 else None
        end = int(sys.argv[3]) if len(sys.argv) > 3 else None
        print("Discovering new meetings from ChampDS...")
        new_ids = discover_new_meetings(start, end)
        print(f"\nRegistered {len(new_ids)} new meetings in database")

    elif cmd == "download":
        if len(sys.argv) < 3:
            print("Usage: pipeline.py download EVENT_ID")
            sys.exit(1)
        download_video(int(sys.argv[2]))

    elif cmd == "download-all":
        db = get_db()
        rows = db.execute("""
            SELECT event_id FROM meetings
            WHERE has_video = 1 AND event_id IS NOT NULL
        """).fetchall()
        db.close()
        for row in rows:
            eid = int(row["event_id"])
            if not os.path.exists(os.path.join(VIDEOS_DIR, f"{eid}.mp4")):
                download_video(eid)

    elif cmd == "transcribe":
        if len(sys.argv) < 3:
            print("Usage: pipeline.py transcribe EVENT_ID")
            sys.exit(1)
        transcribe_video(int(sys.argv[2]))

    elif cmd == "transcribe-all":
        db = get_db()
        rows = db.execute("""
            SELECT event_id FROM meetings
            WHERE has_video = 1 AND has_transcript = 0 AND event_id IS NOT NULL
        """).fetchall()
        db.close()
        for row in rows:
            transcribe_video(int(row["event_id"]))

    elif cmd == "enrich":
        if len(sys.argv) < 3:
            print("Usage: pipeline.py enrich EVENT_ID | --all | --fix-names")
            sys.exit(1)
        if sys.argv[2] == "--all" or sys.argv[2] == "--fix-names":
            # Pass through to enrich_transcript.py
            result = subprocess.run(
                [sys.executable, os.path.join(BASE_DIR, "enrich_transcript.py"), sys.argv[2]],
                capture_output=False,
            )
        else:
            enrich_transcript_file(int(sys.argv[2]))

    elif cmd == "ingest":
        if len(sys.argv) < 3:
            print("Usage: pipeline.py ingest EVENT_ID")
            sys.exit(1)
        ingest_transcript(int(sys.argv[2]))

    elif cmd == "full":
        if len(sys.argv) < 3:
            print("Usage: pipeline.py full EVENT_ID")
            sys.exit(1)
        full_pipeline(int(sys.argv[2]))

    elif cmd == "status":
        show_status()

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)
