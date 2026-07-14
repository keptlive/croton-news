#!/usr/bin/env python3
"""Re-transcribe all available videos with improved Deepgram settings.
Backs up old transcripts, generates new ones with keyterms + diarize_model=latest + find-and-replace.
Then re-ingests chunks.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
import urllib.parse
import urllib.error

RAG_DB = "/opt/croton-news/rag/rag.db"
VIDEOS_DIR = "/opt/croton-news/videos"
TRANSCRIPTS_DIR = "/opt/croton-news/rag/transcripts"
RAW_DIR = os.path.join(TRANSCRIPTS_DIR, "raw")
BACKUP_DIR = os.path.join(TRANSCRIPTS_DIR, "backup_pretranscribe")
DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"

def load_env():
    """Load .env file."""
    env_path = "/opt/croton-news/rag/.env"
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ.setdefault(key.strip(), val.strip())

def build_keyterms():
    """Build keyterm list: curated terms first, then people by mention count.

    Old version sorted 279 entity names alphabetically and sliced [:100]
    with curated terms appended last — nothing past "G" (Luntz, Slippen,
    Nachtaler) nor any curated term was ever actually sent to Deepgram.
    """
    keyterms = [
        "Croton-on-Hudson", "Croton-Harmon", "Croton Point", "CHUFSD",
        "Senasqua", "Gouveia", "Harckham", "Luposello", "Pracademic",
        "Cortlandt", "Truesdale", "Scenic Drive", "Croton Point Avenue",
        "Municipal Place", "South Riverside", "Quaker Bridge", "Van Wyck",
        "Pierre Van Cortlandt", "PVC", "Ossining", "Harmon",
    ]
    try:
        import re as _re
        from collections import Counter
        db = sqlite3.connect(f"file:{RAG_DB}?mode=ro", uri=True)
        # 1) names from official MINUTES (authoritative spellings — the
        # entities table is built from garbled transcripts, so names Deepgram
        # never heard right, e.g. Wetherbee/Pfrang, are missing there)
        _stop = {"Board", "Village", "School", "District", "Meeting", "Motion",
                 "Chairman", "Chairperson", "Trustee", "Mayor", "Public",
                 "Regular", "Special", "Work", "Session", "New", "York",
                 "Absent", "Present", "Action", "Roll", "Call", "The"}
        counts = Counter()
        for (mt,) in db.execute(
                "SELECT minutes_text FROM meetings WHERE minutes_text IS NOT NULL "
                "AND date >= date('now','-180 day')"):
            for m in _re.finditer(r"\b([A-Z][a-z]{2,})\s+([A-Z][a-z]{2,})\b", mt or ""):
                if m.group(1) in _stop or m.group(2) in _stop:
                    continue
                counts[f"{m.group(1)} {m.group(2)}"] += 1
        for name, c in counts.most_common(60):
            if c >= 2 and name not in keyterms:
                keyterms.append(name)
        # 2) then entities by mention count
        rows = db.execute(
            "SELECT name FROM entities WHERE type='person' AND name LIKE '% %' "
            "AND mention_count >= 2 ORDER BY mention_count DESC"
        ).fetchall()
        for r in rows:
            if r[0] not in keyterms:
                keyterms.append(r[0])
        db.close()
    except Exception as e:
        print(f"  Warning: Could not load entities: {e}")
    return keyterms[:100]  # URL length limit

def build_replacements():
    """Known Deepgram mishearings."""
    return [
        ("Courtland Harmony", "Croton-Harmon"), ("Cortland Harmony", "Croton-Harmon"),
        ("Nach Taylor", "Nachtaler"), ("Nachteller", "Nachtaler"),
        ("Thalby", "Balbi"), ("Balby", "Balbi"),
        ("Sabrizi", "Sibrizzi"), ("Jeanette Choon", "Genette Toone"),
        ("Cronin Point", "Croton Point"), ("Quotum Point", "Croton Point"),
        ("Sonosqua", "Senasqua"), ("Prakademic", "Pracademic"),
        ("Cena Drive", "Scenic Drive"),
        ("Croton Harmon", "Croton-Harmon"),
        ("Slippin", "Slippen"), ("Groton", "Croton"),
    ]

def transcribe(video_path, event_id, api_key, keyterms, replacements):
    """Transcribe a video with improved settings."""
    # Extract audio
    audio_path = f"/tmp/{event_id}.wav"
    print(f"  Extracting audio...")
    subprocess.run([
        "ffmpeg", "-i", video_path, "-vn", "-acodec", "pcm_s16le",
        "-ar", "16000", "-ac", "1", audio_path, "-y", "-loglevel", "warning"
    ], check=True)

    # Build params
    base_params = [
        "model=nova-3", "diarize_model=latest",
        "utterances=true", "smart_format=true",
        "language=en", "sentiment=true", "topics=true", "detect_language=true",
        "paragraphs=true", "summarize=v2",
    ]
    for kt in keyterms:
        base_params.append(f"keyterm={urllib.parse.quote(kt)}")
    for wrong, correct in replacements:
        base_params.append(f"replace={urllib.parse.quote(wrong)}:{urllib.parse.quote(correct)}")

    params = "&".join(base_params)
    url = f"{DEEPGRAM_URL}?{params}"

    with open(audio_path, "rb") as f:
        audio_data = f.read()

    audio_mb = len(audio_data) / 1024 / 1024
    print(f"  Uploading {audio_mb:.0f} MB audio to Deepgram...")

    req = urllib.request.Request(url, data=audio_data, headers={
        "Authorization": f"Token {api_key}",
        "Content-Type": "audio/wav",
    })

    resp = urllib.request.urlopen(req, timeout=600)
    dg_result = json.loads(resp.read())

    # Clean up audio
    os.remove(audio_path)

    return dg_result

def parse_deepgram(dg_result, event_id, meeting_info):
    """Parse Deepgram result into our transcript format."""
    utterances = []
    for u in dg_result.get("results", {}).get("utterances", []):
        utterances.append({
            "speaker": f"Speaker {u.get('speaker', 0)}",
            "text": u.get("transcript", ""),
            "start": u.get("start", 0),
            "end": u.get("end", 0),
            "timestamp": f"{int(u.get('start',0)//60):02d}:{int(u.get('start',0)%60):02d}",
            "confidence": u.get("confidence", 0),
            "sentiment": u.get("sentiment", {}).get("sentiment", "neutral") if isinstance(u.get("sentiment"), dict) else "neutral",
        })

    # Build full text
    full_text = ""
    for u in utterances:
        full_text += f"[{u['timestamp']}] {u['speaker']}: {u['text']}\n\n"

    transcript = {
        "event_id": event_id,
        "title": meeting_info.get("committee", ""),
        "date": meeting_info.get("date", ""),
        # required by enrich_transcript.py's gate — its absence made the
        # proper-noun fix pass silently skip every retranscribed file
        # ("Slippin" x38 in chunks despite NAME_FIXES having the fix)
        "platform": "deepgram-nova-3",
        "utterances": utterances,
        "speaker_map": {},
        "full_text": full_text,
        "enriched": False,
        "wireclaw_enriched": False,
    }

    # Add Deepgram summary if available
    summaries = dg_result.get("results", {}).get("summary", {})
    if summaries:
        transcript["deepgram_summary"] = summaries.get("short", "")

    return transcript

def main():
    load_env()
    api_key = os.environ.get("DEEPGRAM_API_KEY")
    if not api_key:
        print("ERROR: DEEPGRAM_API_KEY not set")
        sys.exit(1)

    # Create backup dir
    os.makedirs(BACKUP_DIR, exist_ok=True)

    # Get meeting info
    db = sqlite3.connect(RAG_DB)
    db.row_factory = sqlite3.Row

    # Find all videos on disk
    videos = []
    for f in sorted(os.listdir(VIDEOS_DIR)):
        if f.endswith(".mp4"):
            eid = f.replace(".mp4", "")
            videos.append((os.path.join(VIDEOS_DIR, f), eid))

    # Also check BOE videos
    boe_dir = os.path.join(VIDEOS_DIR, "boe")
    if os.path.exists(boe_dir):
        for f in sorted(os.listdir(boe_dir)):
            if f.endswith(".mp4"):
                eid = "yt-" + f.replace(".mp4", "")
                videos.append((os.path.join(boe_dir, f), eid))

    print(f"Found {len(videos)} videos to re-transcribe")

    keyterms = build_keyterms()
    replacements = build_replacements()
    print(f"Using {len(keyterms)} keyterms and {len(replacements)} replacements")

    success = 0
    errors = 0

    for video_path, event_id in videos:
        print(f"\n=== {event_id} ===")

        # Get meeting info
        if event_id.startswith("yt-"):
            row = db.execute(
                "SELECT date, committee FROM meetings WHERE event_id = ?", (event_id,)
            ).fetchone()
        else:
            row = db.execute(
                "SELECT date, committee FROM meetings WHERE event_id = ?", (int(event_id),)
            ).fetchone()

        if not row:
            print(f"  WARNING: No meeting found for event_id {event_id}, skipping")
            continue

        meeting_info = {"date": row["date"], "committee": row["committee"]}
        print(f"  Meeting: {meeting_info['committee']} on {meeting_info['date']}")

        # Backup old transcript
        old_transcript = os.path.join(TRANSCRIPTS_DIR, f"transcript-{event_id}.json")
        old_raw = os.path.join(RAW_DIR, f"deepgram-{event_id}.json")
        if os.path.exists(old_transcript):
            shutil.copy2(old_transcript, os.path.join(BACKUP_DIR, f"transcript-{event_id}.json"))
        if os.path.exists(old_raw):
            shutil.copy2(old_raw, os.path.join(BACKUP_DIR, f"deepgram-{event_id}.json"))

        try:
            # Transcribe
            dg_result = transcribe(video_path, event_id, api_key, keyterms, replacements)

            # Save raw Deepgram response
            os.makedirs(RAW_DIR, exist_ok=True)
            with open(os.path.join(RAW_DIR, f"deepgram-{event_id}.json"), "w") as f:
                json.dump(dg_result, f)

            # Parse into our format
            transcript = parse_deepgram(dg_result, event_id, meeting_info)
            n_utts = len(transcript["utterances"])

            # Save transcript
            with open(old_transcript, "w") as f:
                json.dump(transcript, f, indent=2)

            print(f"  OK: {n_utts} utterances")
            success += 1

            # Brief pause between requests to avoid rate limits
            time.sleep(2)

        except urllib.error.HTTPError as e:
            body = e.read().decode()[:200]
            print(f"  ERROR: {e.code} {body}")
            errors += 1
            if e.code == 429:
                print("  Rate limited, waiting 60s...")
                time.sleep(60)
        except Exception as e:
            print(f"  ERROR: {e}")
            errors += 1

    db.close()
    print(f"\n=== DONE: {success} success, {errors} errors ===")

if __name__ == "__main__":
    main()
