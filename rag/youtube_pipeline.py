#!/usr/bin/env python3
"""
croton.news — YouTube Caption Pipeline for School Board Meetings

Downloads auto-captions from YouTube, cleans them up, extracts speaker names
from context, and outputs transcript JSON matching our existing format.

Usage:
    python3 youtube_pipeline.py list                    # List all channel videos
    python3 youtube_pipeline.py captions VIDEO_ID       # Download + clean captions for one video
    python3 youtube_pipeline.py captions-all [--since YYYY-MM-DD]  # All videos since date
    python3 youtube_pipeline.py download VIDEO_ID       # Download video+audio
    python3 youtube_pipeline.py download-recent N       # Download N most recent videos
    python3 youtube_pipeline.py transcribe VIDEO_ID     # Transcribe with Deepgram Nova 3
    python3 youtube_pipeline.py transcribe-all [N]      # Transcribe N most recent (default 16)
    python3 youtube_pipeline.py ingest VIDEO_ID         # Ingest transcript into rag.db meetings
    python3 youtube_pipeline.py ingest-all              # Ingest all transcripts
    python3 youtube_pipeline.py status                  # Show pipeline status

Channel: @streamchs1645 (Croton-Harmon School District Board of Education)

IMPORTANT: Speaker names are extracted ONLY from explicit context in the
transcript (roll calls, introductions, "thank you [Name]" patterns).
Names are NEVER fabricated or guessed.
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RAG_DB = os.path.join(BASE_DIR, "rag.db")
TRANSCRIPTS_DIR = os.path.join(BASE_DIR, "transcripts")
CAPTIONS_DIR = os.path.join(BASE_DIR, "captions")  # Raw YouTube SRT files
VIDEOS_DIR = os.path.join(os.path.dirname(BASE_DIR), "videos", "boe")

YOUTUBE_CHANNEL = "https://www.youtube.com/@streamchs1645"
COMMITTEE_NAME = "Board of Education"

# ═══════════════════════════════════════════════════════════════════
# PROPER NOUN CORRECTIONS
# YouTube auto-captions consistently mangle these Croton-specific terms.
# Add new corrections here as they're discovered.
# ═══════════════════════════════════════════════════════════════════
PROPER_NOUN_FIXES = {
    # District name
    "Preschool District": "Free School District",
    "preschool district": "Free School District",
    # Town name
    "Curtain Harmon": "Croton-Harmon",
    "Curtain-Harmon": "Croton-Harmon",
    "curtain harmon": "Croton-Harmon",
    "Curtin Harmon": "Croton-Harmon",
    # School names
    "Carrie E. Tompkins": "Carrie E. Tompkins",  # preserve correct
    "Pierre Van Cortlandt": "Pierre Van Cortlandt",
    "PVC": "PVC",
    "CET": "CET",
    "CHHS": "CHHS",
    # Common mishearings
    "Croton Harmon": "Croton-Harmon",
    "nisba": "NYSSBA",
    "nisus": "NYSUT",
    "Nisa ": "NYSSBA ",
    "nysud": "NYSUT",
    "BOCES": "BOCES",
    "boces": "BOCES",
    # Common word-level fixes (standalone "Curtain" → "Croton" only in school context)
}

# Regex patterns for standalone Croton fixes (only when clearly referring to the town/district)
CROTON_CONTEXT_PATTERNS = [
    (r'\bCurtain(?=[\s-](?:Harmon|on|Hudson|schools?|district|community))', 'Croton'),
    (r'\bcurtain(?=[\s-](?:harmon|on|hudson|schools?|district|community))', 'Croton'),
    (r'\bCurtin(?=[\s-](?:Harmon|on|Hudson))', 'Croton'),
]

# ═══════════════════════════════════════════════════════════════════
# KNOWN SCHOOL BOARD MEMBERS AND OFFICIALS
# Used ONLY for matching — never inserted without transcript evidence.
# Source: CHUFSD website, meeting agendas
# ═══════════════════════════════════════════════════════════════════
KNOWN_PEOPLE = {
    # Current board (2025-2026)
    "Ana Teague": "Board President",
    "Anamika Bhatnagar": "Board Vice President",
    "Sarah Carrier": "Board Trustee",
    "Neal Haber": "Board Trustee",
    "Omar Mayyasi": "Board Trustee",
    "Theo Oshiro": "Board Trustee",
    "Allison Samuels": "Board Trustee",
    "Filomena DiMarco": "Student Ex Officio",
    # Administration
    "Stephen Walker": "Superintendent",
    # Common references
    "Walker": "Superintendent",
    "Teague": "Board President",
    "Bhatnagar": "Board Vice President",
}

# Title patterns that signal a named speaker
TITLE_PATTERNS = [
    r"(?:President|Vice President|Board (?:Member|Trustee))\s+(\w[\w\s\-']+?)(?:\s+(?:said|asked|noted|stated|replied|responded|commented|continued|added|explained|suggested|recommended|proposed|reported|mentioned|thanked|acknowledged)|\s*[,.\?!])",
    r"(?:Superintendent|Dr\.|Mr\.|Mrs\.|Ms\.)\s+(\w[\w\-']+)",
    r"(?:thank you,?\s+)(\w[\w\-']+)",
    r"(?:over to (?:you,?\s+)?)(\w[\w\-']+)",
    r"(?:turn it (?:over )?to\s+)(\w[\w\s\-']+?)(?:\s+(?:to|for|who)|\s*[,.])",
    r"(\w[\w\-']+)(?:\s*,\s*(?:would you|could you|can you|do you|please))",
]

# Filler words/phrases to remove
FILLERS = [
    r'\b[Uu]m\b',
    r'\b[Uu]h\b',
    r'\b[Yy]ou know\b',
    r'\b[Ii] mean\b',
    r'\b[Ss]ort of\b',
    r'\b[Kk]ind of\b',
    r'\b[Ll]ike,?\s+',  # filler "like" (careful - also a real word)
]

# Don't remove "like" aggressively — only when preceded by comma or "was"
FILLER_LIKE_PATTERN = r',\s+like,?\s+'


def ensure_dirs():
    """Create output directories if they don't exist."""
    os.makedirs(CAPTIONS_DIR, exist_ok=True)
    os.makedirs(TRANSCRIPTS_DIR, exist_ok=True)
    os.makedirs(VIDEOS_DIR, exist_ok=True)


def get_db():
    db = sqlite3.connect(RAG_DB)
    db.row_factory = sqlite3.Row
    return db


# ═══════════════════════════════════════════════════════════════════
# STEP 1: LIST CHANNEL VIDEOS
# ═══════════════════════════════════════════════════════════════════

def list_channel_videos():
    """Get all videos from the channel with metadata."""
    print("Fetching video list from YouTube...")
    result = subprocess.run(
        ["yt-dlp", "--js-runtimes", "node", "--flat-playlist",
         "--print", "%(id)s|%(title)s|%(upload_date)s|%(duration_string)s",
         YOUTUBE_CHANNEL],
        capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        print(f"Error: {result.stderr}")
        return []

    videos = []
    for line in result.stdout.strip().split("\n"):
        if not line or "|" not in line:
            continue
        parts = line.split("|", 3)
        if len(parts) < 2:
            continue
        vid = parts[0].strip()
        title = parts[1].strip()
        upload_date = parts[2].strip() if len(parts) > 2 else "NA"
        duration = parts[3].strip() if len(parts) > 3 else "NA"

        # Parse date from title
        date = parse_date_from_title(title, upload_date)

        # Determine meeting type
        meeting_type = "Regular Meeting"
        title_lower = title.lower()
        if "work session" in title_lower:
            meeting_type = "Work Session"
        elif "special" in title_lower:
            meeting_type = "Special Meeting"
        elif "business" in title_lower:
            meeting_type = "Business Meeting"
        elif "budget" in title_lower:
            meeting_type = "Budget Hearing"
        elif "candidate" in title_lower:
            meeting_type = "Candidate Forum"
        elif "re-org" in title_lower or "reorganization" in title_lower:
            meeting_type = "Reorganization Meeting"

        videos.append({
            "video_id": vid,
            "title": title,
            "date": date,
            "duration": duration,
            "meeting_type": meeting_type,
            "upload_date": upload_date,
        })

    return sorted(videos, key=lambda v: v["date"] or "0000", reverse=True)


def parse_date_from_title(title, upload_date="NA"):
    """Extract meeting date from video title."""
    # Try patterns like "March 12, 2026" or "3/12/26" or "March 12th, 2026"
    # Pattern: Month Day, Year
    m = re.search(
        r'(?:January|February|March|April|May|June|July|August|September|October|November|December)'
        r'\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})',
        title
    )
    if m:
        month_str = re.search(
            r'(January|February|March|April|May|June|July|August|September|October|November|December)',
            title
        ).group(1)
        months = {
            "January": "01", "February": "02", "March": "03", "April": "04",
            "May": "05", "June": "06", "July": "07", "August": "08",
            "September": "09", "October": "10", "November": "11", "December": "12"
        }
        day = m.group(1).zfill(2)
        year = m.group(2)
        return f"{year}-{months[month_str]}-{day}"

    # Pattern: M/D/YY or M/D/YYYY
    m = re.search(r'(\d{1,2})/(\d{1,2})/(\d{2,4})', title)
    if m:
        month = m.group(1).zfill(2)
        day = m.group(2).zfill(2)
        year = m.group(3)
        if len(year) == 2:
            year = "20" + year
        return f"{year}-{month}-{day}"

    # Fallback: use upload date if available
    if upload_date and upload_date != "NA" and len(upload_date) == 8:
        return f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"

    return None


# ═══════════════════════════════════════════════════════════════════
# STEP 2: DOWNLOAD CAPTIONS
# ═══════════════════════════════════════════════════════════════════

def download_captions(video_id):
    """Download auto-generated English captions for a video."""
    ensure_dirs()
    out_path = os.path.join(CAPTIONS_DIR, f"{video_id}")
    srt_path = f"{out_path}.en.srt"

    if os.path.exists(srt_path):
        print(f"  Captions already downloaded: {srt_path}")
        return srt_path

    print(f"  Downloading captions for {video_id}...")
    result = subprocess.run(
        ["yt-dlp", "--js-runtimes", "node", "--write-auto-sub", "--sub-lang", "en", "--sub-format", "srt",
         "--skip-download", "-o", out_path,
         f"https://www.youtube.com/watch?v={video_id}"],
        capture_output=True, text=True, timeout=60
    )

    if result.returncode != 0 or not os.path.exists(srt_path):
        print(f"  ERROR: Failed to download captions: {result.stderr[:200]}")
        return None

    print(f"  Downloaded: {srt_path}")
    return srt_path


# ═══════════════════════════════════════════════════════════════════
# STEP 3: DOWNLOAD VIDEO + AUDIO
# ═══════════════════════════════════════════════════════════════════

def download_video(video_id, title=""):
    """Download video and extract audio for a meeting."""
    ensure_dirs()
    video_path = os.path.join(VIDEOS_DIR, f"{video_id}.mp4")
    audio_path = os.path.join(VIDEOS_DIR, f"{video_id}.mp3")

    if os.path.exists(video_path):
        print(f"  Video already exists: {video_path}")
    else:
        print(f"  Downloading video {video_id}...")
        result = subprocess.run(
            ["yt-dlp", "--js-runtimes", "node", "-f", "bestvideo[height<=720]+bestaudio/best[height<=720]",
             "--merge-output-format", "mp4",
             "-o", video_path,
             f"https://www.youtube.com/watch?v={video_id}"],
            capture_output=True, text=True, timeout=1800  # 30 min timeout
        )
        if result.returncode != 0:
            print(f"  ERROR downloading video: {result.stderr[:300]}")
            return None

    if os.path.exists(audio_path):
        print(f"  Audio already exists: {audio_path}")
    else:
        print(f"  Extracting audio...")
        result = subprocess.run(
            ["yt-dlp", "--js-runtimes", "node", "-x", "--audio-format", "mp3", "--audio-quality", "3",
             "-o", audio_path,
             f"https://www.youtube.com/watch?v={video_id}"],
            capture_output=True, text=True, timeout=600
        )
        if result.returncode != 0:
            # Try extracting from already-downloaded video
            if os.path.exists(video_path):
                subprocess.run(
                    ["ffmpeg", "-i", video_path, "-q:a", "3", "-map", "a", audio_path],
                    capture_output=True, timeout=300
                )

    return video_path


# ═══════════════════════════════════════════════════════════════════
# STEP 3B: DEEPGRAM TRANSCRIPTION
# ═══════════════════════════════════════════════════════════════════

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"


def transcribe_with_deepgram(video_id, video_info=None):
    """Transcribe a BOE meeting with Deepgram Nova 3 + full analysis.

    Extracts audio from video, sends to Deepgram with diarization, sentiment,
    topics, paragraphs, and summarization. Saves raw response for analysis
    and clean transcript for public use.
    """
    import urllib.request
    import urllib.error
    import time

    api_key = os.environ.get("DEEPGRAM_API_KEY")
    if not api_key:
        print("  ERROR: Set DEEPGRAM_API_KEY environment variable")
        return None

    video_path = os.path.join(VIDEOS_DIR, f"{video_id}.mp4")
    if not os.path.exists(video_path):
        print(f"  ERROR: Video not found: {video_path}")
        return None

    event_id = f"yt-{video_id}"
    out_path = os.path.join(TRANSCRIPTS_DIR, f"transcript-{event_id}.json")

    # Extract audio to WAV (16kHz mono — optimal for Deepgram)
    audio_path = os.path.join(VIDEOS_DIR, f"{video_id}_deepgram.wav")
    if not os.path.exists(audio_path):
        print(f"  Extracting audio from video...")
        result = subprocess.run([
            "ffmpeg", "-i", video_path, "-vn", "-acodec", "pcm_s16le",
            "-ar", "16000", "-ac", "1", audio_path, "-y", "-loglevel", "warning"
        ], capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            print(f"  ERROR: ffmpeg audio extraction failed: {result.stderr[:200]}")
            return None
        size_mb = os.path.getsize(audio_path) / 1024 / 1024
        print(f"  Audio extracted: {size_mb:.0f} MB")
    else:
        print(f"  Audio already extracted")

    # Upload to Deepgram — full analysis
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

    start_time = time.time()
    try:
        resp = urllib.request.urlopen(req, timeout=900)  # 15 min timeout for long meetings
        dg_result = json.loads(resp.read())
    except Exception as e:
        print(f"  ERROR: Deepgram request failed: {e}")
        return None
    elapsed = time.time() - start_time
    print(f"  Deepgram completed in {elapsed:.0f}s")

    # Save raw Deepgram response internally for analysis
    raw_dir = os.path.join(TRANSCRIPTS_DIR, "raw")
    os.makedirs(raw_dir, exist_ok=True)
    raw_path = os.path.join(raw_dir, f"deepgram-{event_id}.json")
    with open(raw_path, "w") as f:
        json.dump(dg_result, f)
    print(f"  Raw Deepgram saved: {raw_path}")

    # Parse utterances
    utterances = []
    for utt in dg_result.get("results", {}).get("utterances", []):
        entry = {
            "speaker": f"Speaker {utt.get('speaker', 0)}",
            "text": utt.get("transcript", ""),
            "start": utt.get("start", 0),
            "end": utt.get("end", 0),
            "timestamp": f"{int(utt.get('start', 0) // 60):02d}:{int(utt.get('start', 0) % 60):02d}",
        }
        sentiment = utt.get("sentiment")
        if sentiment:
            entry["sentiment"] = sentiment
        utterances.append(entry)

    # Apply proper noun fixes to all utterance text
    for u in utterances:
        u["text"] = fix_proper_nouns(u["text"])

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

    # Get info from video_info or existing data
    if not video_info:
        video_info = {"video_id": video_id, "title": "Board of Education Meeting", "date": None, "meeting_type": "Regular Meeting"}

    # Match confirmed speakers from KNOWN_PEOPLE against transcript text
    full_text = " ".join(u["text"] for u in utterances)
    confirmed_names = extract_speakers_from_context(
        [{"text": full_text, "start": 0, "end": 0}]
    )

    transcript = {
        "video_id": video_id,
        "event_id": event_id,
        "youtube_url": f"https://www.youtube.com/watch?v={video_id}",
        "title": video_info.get("title", ""),
        "date": video_info.get("date", ""),
        "committee": COMMITTEE_NAME,
        "meeting_type": video_info.get("meeting_type", "Regular Meeting"),
        "platform": "deepgram-nova-3",
        "source": "deepgram",
        "diarization": True,
        "full_text": " ".join(u["text"] for u in utterances),
        "utterances": utterances,
        "word_count": sum(len(u["text"].split()) for u in utterances),
        "speaker_count": len(set(u["speaker"] for u in utterances)),
        "duration_seconds": max((u["end"] for u in utterances), default=0),
        "speaker_map": {},
        "confirmed_speakers": list(confirmed_names.values()),
        "dg_summary": dg_summary,
        "dg_topics": dg_topics,
        "processing_seconds": round(elapsed, 1),
        "note": "Transcribed with Deepgram Nova 3. Speaker diarization by Deepgram. "
                "Proper nouns corrected for Croton-Harmon context.",
    }

    with open(out_path, "w") as f:
        json.dump(transcript, f, indent=2)

    # Clean up temp audio
    if os.path.exists(audio_path):
        os.remove(audio_path)

    print(f"  Saved: {out_path}")
    print(f"  Words: {transcript['word_count']}, Speakers: {transcript['speaker_count']}, "
          f"Duration: {transcript['duration_seconds']:.0f}s")
    if dg_summary:
        print(f"  Summary: {dg_summary[:120]}...")
    if dg_topics:
        print(f"  Topics: {', '.join(dg_topics[:8])}")

    return out_path


def cmd_transcribe(video_id):
    """Transcribe a single video with Deepgram."""
    videos = list_channel_videos()
    info = next((v for v in videos if v["video_id"] == video_id), None)
    if not info:
        info = {"video_id": video_id, "title": "Unknown", "date": None, "meeting_type": "Regular Meeting"}

    print(f"\nTranscribing: {info['title']} ({info.get('date', 'unknown')})")
    return transcribe_with_deepgram(video_id, info)


def cmd_transcribe_all(n=16):
    """Transcribe N most recent meetings that have local video."""
    videos = list_channel_videos()
    today = datetime.now().strftime("%Y-%m-%d")
    past = [v for v in videos if v["date"] and v["date"] <= today]

    results = {"success": 0, "failed": 0, "skipped": 0}
    processed = 0

    for v in past:
        if processed >= n:
            break

        video_id = v["video_id"]
        video_path = os.path.join(VIDEOS_DIR, f"{video_id}.mp4")
        event_id = f"yt-{video_id}"

        # Check if video exists locally
        if not os.path.exists(video_path):
            continue

        # Check if already transcribed with Deepgram
        out_path = os.path.join(TRANSCRIPTS_DIR, f"transcript-{event_id}.json")
        if os.path.exists(out_path):
            with open(out_path) as f:
                data = json.load(f)
            if data.get("platform") == "deepgram-nova-3":
                print(f"\nSkipping {video_id} — already transcribed with Deepgram")
                results["skipped"] += 1
                processed += 1
                continue

        print(f"\n{'='*60}")
        print(f"Transcribing: {v['title']} ({v['date']})")
        try:
            result = transcribe_with_deepgram(video_id, v)
            if result:
                results["success"] += 1
            else:
                results["failed"] += 1
        except Exception as e:
            print(f"  ERROR: {e}")
            import traceback
            traceback.print_exc()
            results["failed"] += 1

        processed += 1

    print(f"\n{'='*60}")
    print(f"Done! Success: {results['success']}, Failed: {results['failed']}, "
          f"Skipped: {results['skipped']}")


# ═══════════════════════════════════════════════════════════════════
# STEP 4: PARSE AND CLEAN CAPTIONS
# ═══════════════════════════════════════════════════════════════════

def parse_srt(srt_path):
    """Parse SRT file into list of {start, end, text} entries."""
    with open(srt_path) as f:
        content = f.read()

    entries = []
    current = {}
    for line in content.split("\n"):
        line = line.strip()
        if not line:
            if current.get("text"):
                entries.append(current)
            current = {}
            continue

        m = re.match(r"(\d{2}):(\d{2}):(\d{2}),(\d{3}) --> (\d{2}):(\d{2}):(\d{2}),(\d{3})", line)
        if m:
            current["start"] = (
                int(m.group(1)) * 3600 + int(m.group(2)) * 60 +
                int(m.group(3)) + int(m.group(4)) / 1000
            )
            current["end"] = (
                int(m.group(5)) * 3600 + int(m.group(6)) * 60 +
                int(m.group(7)) + int(m.group(8)) / 1000
            )
        elif re.match(r"^\d+$", line):
            pass  # Sequence number
        else:
            current["text"] = (current.get("text", "") + " " + line).strip()

    if current.get("text"):
        entries.append(current)

    return entries


def merge_overlapping_entries(entries):
    """Merge overlapping SRT segments into non-overlapping utterances.

    YouTube SRT has ~99% overlapping segments where each entry contains
    2-3 words of new content plus overlap from the previous entry.
    We reconstruct the actual word stream by detecting new words.
    """
    if not entries:
        return []

    # Build a timeline of text, merging overlapping segments
    merged = []
    current_text = ""
    current_start = entries[0].get("start", 0)
    last_end = 0

    for entry in entries:
        text = entry.get("text", "").strip()
        start = entry.get("start", 0)
        end = entry.get("end", 0)

        if not text:
            continue

        # Detect speaker change marker
        if ">>" in text:
            # Flush current
            if current_text.strip():
                merged.append({
                    "start": current_start,
                    "end": last_end,
                    "text": current_text.strip(),
                })
            # Handle text around >>
            parts = text.split(">>")
            # Text before >> belongs to previous speaker
            if parts[0].strip():
                merged.append({
                    "start": start,
                    "end": end,
                    "text": parts[0].strip(),
                    "speaker_change": True,
                })
            current_text = parts[-1].strip() + " "
            current_start = start
            last_end = end
            continue

        # Check for sentence boundaries (period, question mark)
        if current_text and (
            current_text.rstrip().endswith(".")
            or current_text.rstrip().endswith("?")
            or current_text.rstrip().endswith("!")
        ) and start - last_end > 2.0:
            # Pause + sentence end = new segment
            merged.append({
                "start": current_start,
                "end": last_end,
                "text": current_text.strip(),
            })
            current_text = ""
            current_start = start

        # Find genuinely new words (not overlap from previous entry)
        if current_text:
            # Compare last few words of current with start of new entry
            cur_words = current_text.split()
            new_words = text.split()
            # Find overlap
            best_overlap = 0
            for overlap_len in range(min(len(cur_words), len(new_words)), 0, -1):
                if cur_words[-overlap_len:] == new_words[:overlap_len]:
                    best_overlap = overlap_len
                    break
            # Append only non-overlapping words
            if best_overlap > 0:
                unique_words = new_words[best_overlap:]
            else:
                unique_words = new_words
            if unique_words:
                current_text += " ".join(unique_words) + " "
        else:
            current_text = text + " "

        last_end = end

    # Flush remaining
    if current_text.strip():
        merged.append({
            "start": current_start,
            "end": last_end,
            "text": current_text.strip(),
        })

    return merged


def fix_proper_nouns(text):
    """Apply Croton-specific proper noun corrections."""
    # Dictionary fixes
    for wrong, right in PROPER_NOUN_FIXES.items():
        text = text.replace(wrong, right)

    # Context-aware Croton fixes
    for pattern, replacement in CROTON_CONTEXT_PATTERNS:
        text = re.sub(pattern, replacement, text)

    return text


def remove_fillers(text):
    """Remove filler words for readability, preserving meaning."""
    # Remove standalone fillers
    text = re.sub(r'\b[Uu]m,?\s*', '', text)
    text = re.sub(r'\b[Uu]h,?\s*', '', text)

    # Remove "you know" when it's a filler (not "you know that...")
    text = re.sub(r'\byou know,?\s+(?!that|what|how|when|where|why|who|if)', '', text)

    # Clean up double spaces
    text = re.sub(r'\s{2,}', ' ', text)

    return text.strip()


def build_paragraphs(merged_entries, min_paragraph_words=30, max_paragraph_words=300):
    """Group merged entries into readable paragraphs with timestamps.

    Breaks on:
    - Speaker change markers (>>)
    - Long pauses (>3 seconds)
    - Topic shifts (approximated by sentence-ending punctuation + pause)
    """
    paragraphs = []
    current = {"start": 0, "end": 0, "text": "", "has_speaker_change": False}

    for entry in merged_entries:
        text = entry["text"]
        start = entry.get("start", 0)
        end = entry.get("end", 0)

        is_speaker_change = entry.get("speaker_change", False) or ">>" in text

        # Start new paragraph on speaker change
        if is_speaker_change and current["text"]:
            paragraphs.append(current)
            current = {"start": start, "end": end, "text": "", "has_speaker_change": True}

        # Start new paragraph on long pause
        if current["text"] and start - current["end"] > 3.0:
            paragraphs.append(current)
            current = {"start": start, "end": end, "text": "", "has_speaker_change": False}

        # Break if paragraph getting too long
        word_count = len(current["text"].split())
        if word_count > max_paragraph_words and (
            current["text"].rstrip().endswith((".", "?", "!"))
        ):
            paragraphs.append(current)
            current = {"start": start, "end": end, "text": "", "has_speaker_change": False}

        if not current["text"]:
            current["start"] = start
        current["end"] = end
        current["text"] = (current["text"] + " " + text).strip()

    if current["text"]:
        paragraphs.append(current)

    return paragraphs


# ═══════════════════════════════════════════════════════════════════
# STEP 5: EXTRACT SPEAKER NAMES FROM CONTEXT
# ═══════════════════════════════════════════════════════════════════

def extract_speakers_from_context(paragraphs):
    """Identify speakers using ONLY explicit evidence in the transcript.

    Strategies:
    1. Roll call detection: "Mr./Mrs./Dr. Name" + "here/present/aye"
    2. Introduction: "I'm [Name]", "This is [Name]"
    3. Handoff: "Thank you, [Name]", "Over to you, [Name]"
    4. Title+Name: "Superintendent Walker", "President Chaudhuri"
    5. Match against KNOWN_PEOPLE only when name appears verbatim

    NEVER fabricates names. If unsure, labels speaker as "Unknown Speaker".
    """
    full_text = " ".join(p["text"] for p in paragraphs)

    # Track which names we've seen explicit evidence for
    confirmed_names = {}  # normalized_name → full_name

    # Common words that get falsely matched as names
    NOT_NAMES = {
        "so", "the", "all", "everyone", "everybody", "much", "very",
        "for", "and", "our", "this", "that", "you", "we", "it", "a",
        "an", "okay", "ok", "yes", "no", "again", "both", "also",
        "here", "there", "really", "to", "folks", "favor", "tonight",
        "today", "sir", "ma'am", "him", "her", "them", "they", "those",
        "these", "now", "then", "too", "well", "just", "guys",
        "thanks", "bye", "hi", "hello", "oh", "ah", "um", "uh",
        "right", "great", "good", "nice", "sure", "fine", "absolutely",
        "certainly", "definitely", "exactly", "indeed", "please",
        "anyway", "anyways", "actually", "basically", "honestly",
        "obviously", "clearly", "simply", "superintendent",
        "board", "president", "member", "chair", "vice",
    }

    # Strategy 1: Find names mentioned with titles
    title_name_patterns = [
        r"(?:Superintendent|Dr\.)\s+(Walker|Faruk|Fjeld)",
        r"(?:President|Vice President)\s+(\w[\w'-]+)",
        r"(?:Board (?:M|m)ember)\s+(\w[\w'-]+)",
        r"(?:Mr\.|Mrs\.|Ms\.)\s+(\w[\w'-]+)",
        r"(?:Assistant Superintendent)\s+(\w[\w'-]+)",
    ]

    for pattern in title_name_patterns:
        for m in re.finditer(pattern, full_text):
            last_name = m.group(1)
            if len(last_name) < 3:
                continue
            # Match against known people ONLY
            for full_name, role in KNOWN_PEOPLE.items():
                if last_name.lower() in full_name.lower().split():
                    confirmed_names[last_name.lower()] = full_name
                    break
            # Don't add unknown title+name combos — risk of false positives too high

    # Strategy 2: "Thank you, [Name]" / "Over to you, [Name]"
    handoff_patterns = [
        r"[Tt]hank you,?\s+(\w[\w'-]+)",
        r"[Oo]ver to (?:you,?\s+)?(\w[\w'-]+)",
        r"[Tt]urn it (?:over )?to\s+(\w[\w'-]+)",
    ]

    for pattern in handoff_patterns:
        for m in re.finditer(pattern, full_text):
            name = m.group(1)
            if name.lower() in NOT_NAMES:
                continue
            if len(name) < 3:
                continue
            # Try matching to known people
            for full_name in KNOWN_PEOPLE:
                if name.lower() in full_name.lower().split():
                    confirmed_names[name.lower()] = full_name
                    break
            # Don't add unknown names from handoff patterns — too many false positives

    # Strategy 3: Roll call — look for sequences of names with "here/present/aye"
    roll_call = re.findall(
        r"(?:Mr\.|Mrs\.|Ms\.|Dr\.)\s+(\w[\w'-]+)\s*[\.\?\,]?\s*(?:here|present|aye|yes|absent)",
        full_text, re.IGNORECASE
    )
    for name in roll_call:
        for full_name in KNOWN_PEOPLE:
            if name.lower() in full_name.lower().split():
                confirmed_names[name.lower()] = full_name
                break

    # Strategy 4: Self-identification — only match against KNOWN_PEOPLE
    self_id = re.findall(
        r"(?:I'm|I am|[Mm]y name is)\s+(\w[\w'-]+(?:\s+\w[\w'-]+)?)",
        full_text
    )
    for name in self_id:
        if len(name) < 3:
            continue
        for full_name in KNOWN_PEOPLE:
            if name.lower() in full_name.lower() or full_name.lower().startswith(name.lower()):
                confirmed_names[name.split()[0].lower()] = full_name
                break

    # Filter out anything in NOT_NAMES that slipped through
    confirmed_names = {
        k: v for k, v in confirmed_names.items()
        if k not in NOT_NAMES and len(k) > 2
    }

    return confirmed_names


def assign_speakers_to_paragraphs(paragraphs, confirmed_names):
    """Try to assign speaker labels to paragraphs using context clues.

    Only assigns a name when there's clear evidence. Uses "Unknown Speaker"
    when we can't determine who's talking.
    """
    assigned = []
    current_speaker = None
    speaker_counter = 0
    unnamed_speakers = {}  # track unnamed speakers by their pattern

    for i, para in enumerate(paragraphs):
        text = para["text"]
        speaker = None

        # Check if paragraph starts with or contains a self-identification
        for name_lower, full_name in confirmed_names.items():
            # Look for the name in the first 100 chars as context for who's speaking
            first_chunk = text[:100].lower()
            if name_lower in first_chunk:
                # This paragraph mentions someone — but are they speaking or being spoken about?
                # Only assign if it's a handoff TO them in the previous paragraph
                if i > 0:
                    prev_text = paragraphs[i - 1]["text"].lower()
                    for pattern in ["over to", "turn it to", "thank you"]:
                        if pattern in prev_text and name_lower in prev_text:
                            speaker = full_name
                            break

        # Check if previous paragraph handed off to someone
        if not speaker and i > 0 and para.get("has_speaker_change"):
            prev = paragraphs[i - 1]["text"]
            for pattern in [
                r"[Oo]ver to (?:you,?\s+)?(\w[\w'-]+)",
                r"[Tt]urn it (?:over )?to\s+(\w[\w'-]+)",
                r"(?:Superintendent|Dr\.|President|Mr\.|Mrs\.)\s+(\w[\w'-]+)\s*[,.]?\s*$",
            ]:
                m = re.search(pattern, prev[-100:])
                if m:
                    name = m.group(1).lower()
                    if name in confirmed_names:
                        speaker = confirmed_names[name]
                        break

        # If we found a speaker, use them
        if speaker:
            current_speaker = speaker
        elif para.get("has_speaker_change"):
            current_speaker = None  # Reset on speaker change without identified speaker

        assigned.append({
            "start": para["start"],
            "end": para["end"],
            "text": para["text"],
            "speaker": current_speaker or "Unknown Speaker",
        })

    return assigned


# ═══════════════════════════════════════════════════════════════════
# STEP 6: BUILD TRANSCRIPT JSON
# ═══════════════════════════════════════════════════════════════════

def build_transcript(video_info, assigned_paragraphs, confirmed_names):
    """Build transcript JSON matching our existing format."""
    utterances = []
    for para in assigned_paragraphs:
        start = para["start"]
        minutes = int(start // 60)
        seconds = int(start % 60)
        timestamp = f"{minutes:02d}:{seconds:02d}"

        utterances.append({
            "speaker": para["speaker"],
            "text": para["text"],
            "start": round(start, 2),
            "end": round(para["end"], 2),
            "timestamp": timestamp,
        })

    # Count unique speakers
    speakers = set(u["speaker"] for u in utterances)
    speaker_count = len(speakers)

    # Build speaker map from confirmed names
    speaker_map = {}
    for name_lower, full_name in confirmed_names.items():
        role = KNOWN_PEOPLE.get(full_name, "")
        if role:
            speaker_map[full_name] = f"{full_name} ({role})"
        else:
            speaker_map[full_name] = full_name

    # Calculate total duration
    duration = max((u["end"] for u in utterances), default=0)

    # Full text for search
    full_text = "\n\n".join(
        f"[{u['timestamp']}] {u['speaker']}: {u['text']}"
        for u in utterances
    )

    word_count = sum(len(u["text"].split()) for u in utterances)

    transcript = {
        "video_id": video_info["video_id"],
        "event_id": f"yt-{video_info['video_id']}",
        "youtube_url": f"https://www.youtube.com/watch?v={video_info['video_id']}",
        "title": video_info["title"],
        "date": video_info["date"],
        "committee": COMMITTEE_NAME,
        "meeting_type": video_info.get("meeting_type", "Regular Meeting"),
        "platform": "youtube",
        "source": "youtube_auto_captions",
        "diarization": "context_extraction",
        "full_text": full_text,
        "utterances": utterances,
        "word_count": word_count,
        "speaker_count": speaker_count,
        "duration_seconds": round(duration, 2),
        "confirmed_speakers": list(confirmed_names.values()),
        "speaker_map": speaker_map,
        "note": "Speakers extracted from transcript context only. "
                "Names are never fabricated. 'Unknown Speaker' indicates "
                "insufficient context to identify the speaker.",
    }

    return transcript


# ═══════════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ═══════════════════════════════════════════════════════════════════

def process_captions(video_id, video_info=None):
    """Full caption processing pipeline for a single video."""
    ensure_dirs()

    # Download captions
    srt_path = download_captions(video_id)
    if not srt_path:
        return None

    # Parse SRT
    print(f"  Parsing SRT...")
    entries = parse_srt(srt_path)
    print(f"  Raw entries: {len(entries)}")

    # Merge overlapping segments
    merged = merge_overlapping_entries(entries)
    print(f"  Merged segments: {len(merged)}")

    # Build paragraphs
    paragraphs = build_paragraphs(merged)
    print(f"  Paragraphs: {len(paragraphs)}")

    # Fix proper nouns
    for p in paragraphs:
        p["text"] = fix_proper_nouns(p["text"])

    # Remove fillers
    for p in paragraphs:
        p["text"] = remove_fillers(p["text"])

    # Extract speaker names from context
    confirmed_names = extract_speakers_from_context(paragraphs)
    if confirmed_names:
        print(f"  Confirmed speakers: {', '.join(confirmed_names.values())}")
    else:
        print(f"  No speakers identified from context")

    # Assign speakers
    assigned = assign_speakers_to_paragraphs(paragraphs, confirmed_names)

    # Build info if not provided
    if not video_info:
        video_info = {
            "video_id": video_id,
            "title": f"Board of Education Meeting",
            "date": None,
            "meeting_type": "Regular Meeting",
        }

    # Build transcript
    transcript = build_transcript(video_info, assigned, confirmed_names)

    # Save
    out_path = os.path.join(TRANSCRIPTS_DIR, f"transcript-yt-{video_id}.json")
    with open(out_path, "w") as f:
        json.dump(transcript, f, indent=2)
    print(f"  Saved: {out_path}")
    print(f"  Words: {transcript['word_count']}, Speakers: {transcript['speaker_count']}, "
          f"Duration: {transcript['duration_seconds']:.0f}s")

    return transcript


def cmd_list():
    """List all channel videos."""
    videos = list_channel_videos()
    print(f"\n{'ID':<14} {'Date':<12} {'Type':<25} {'Title'}")
    print("-" * 90)
    for v in videos:
        date = v["date"] or "unknown"
        print(f"{v['video_id']:<14} {date:<12} {v['meeting_type']:<25} {v['title'][:50]}")
    print(f"\nTotal: {len(videos)} videos")


def cmd_captions(video_id):
    """Process captions for a single video."""
    # Try to get video info
    videos = list_channel_videos()
    info = next((v for v in videos if v["video_id"] == video_id), None)
    if not info:
        info = {"video_id": video_id, "title": "Unknown", "date": None, "meeting_type": "Regular Meeting"}

    print(f"\nProcessing: {info['title']}")
    transcript = process_captions(video_id, info)
    if transcript:
        print(f"\nDone! Transcript saved.")
        return transcript
    return None


def cmd_captions_all(since_date=None):
    """Download and process captions for all videos, optionally since a date."""
    videos = list_channel_videos()

    if since_date:
        videos = [v for v in videos if v["date"] and v["date"] >= since_date]
        print(f"Processing {len(videos)} videos since {since_date}")
    else:
        print(f"Processing all {len(videos)} videos")

    results = {"success": 0, "failed": 0, "skipped": 0}
    for v in videos:
        out_path = os.path.join(TRANSCRIPTS_DIR, f"transcript-yt-{v['video_id']}.json")
        if os.path.exists(out_path):
            print(f"\nSkipping {v['video_id']} — already processed")
            results["skipped"] += 1
            continue

        print(f"\n{'='*60}")
        print(f"Processing: {v['title']} ({v['date']})")
        try:
            transcript = process_captions(v["video_id"], v)
            if transcript:
                results["success"] += 1
            else:
                results["failed"] += 1
        except Exception as e:
            print(f"  ERROR: {e}")
            results["failed"] += 1

    print(f"\n{'='*60}")
    print(f"Done! Success: {results['success']}, Failed: {results['failed']}, "
          f"Skipped: {results['skipped']}")


def cmd_download_recent(n=16):
    """Download video+audio for N most recent meetings."""
    videos = list_channel_videos()
    # Filter out future livestreams
    today = datetime.now().strftime("%Y-%m-%d")
    past_videos = [v for v in videos if v["date"] and v["date"] <= today]

    to_download = past_videos[:n]
    print(f"Downloading {len(to_download)} most recent meetings...")

    for v in to_download:
        print(f"\n{'='*60}")
        print(f"Downloading: {v['title']} ({v['date']})")
        download_video(v["video_id"], v["title"])


def cmd_status():
    """Show pipeline status."""
    videos = list_channel_videos()
    today = datetime.now().strftime("%Y-%m-%d")

    has_captions = 0
    has_video = 0
    has_transcript = 0

    print(f"\n{'ID':<14} {'Date':<12} {'Cap':>3} {'Vid':>3} {'Trn':>3} {'Title'}")
    print("-" * 90)

    for v in videos[:40]:  # Show last 40
        vid = v["video_id"]
        cap = "✓" if os.path.exists(os.path.join(CAPTIONS_DIR, f"{vid}.en.srt")) else "·"
        video = "✓" if os.path.exists(os.path.join(VIDEOS_DIR, f"{vid}.mp4")) else "·"
        trn = "✓" if os.path.exists(os.path.join(TRANSCRIPTS_DIR, f"transcript-yt-{vid}.json")) else "·"

        if cap == "✓": has_captions += 1
        if video == "✓": has_video += 1
        if trn == "✓": has_transcript += 1

        date = v["date"] or "unknown"
        print(f"{vid:<14} {date:<12} {cap:>3} {video:>3} {trn:>3} {v['title'][:45]}")

    print(f"\nTotals: {len(videos)} videos, {has_captions} captions, "
          f"{has_video} videos, {has_transcript} transcripts")


def cmd_ingest(video_id):
    """Ingest a single processed transcript into rag.db meetings table."""
    transcript_path = os.path.join(TRANSCRIPTS_DIR, f"transcript-yt-{video_id}.json")
    if not os.path.exists(transcript_path):
        print(f"ERROR: No transcript found at {transcript_path}")
        print(f"Run 'captions {video_id}' first to process the captions.")
        return False

    with open(transcript_path) as f:
        data = json.load(f)

    event_id = data.get("event_id", f"yt-{video_id}")
    date = data.get("date")
    if not date:
        print(f"  WARNING: No date for {video_id}, skipping")
        return False

    # Check if video exists locally
    video_path = os.path.join(VIDEOS_DIR, f"{video_id}.mp4")
    has_video = 1 if os.path.exists(video_path) else 0
    audio_path = os.path.join(VIDEOS_DIR, f"{video_id}.mp3")
    has_audio = 1 if os.path.exists(audio_path) else 0

    db = get_db()

    # Check if already exists
    existing = db.execute(
        "SELECT id FROM meetings WHERE event_id = ?", (event_id,)
    ).fetchone()

    if existing:
        # Update existing row
        db.execute("""
            UPDATE meetings SET
                has_transcript = 1,
                has_video = ?,
                has_audio = ?,
                word_count = ?,
                speaker_count = ?,
                duration_seconds = ?
            WHERE event_id = ?
        """, (
            has_video, has_audio,
            data.get("word_count", 0),
            data.get("speaker_count", 0),
            data.get("duration_seconds", 0),
            event_id,
        ))
        print(f"  Updated meeting: {date} {COMMITTEE_NAME} (#{existing['id']})")
    else:
        # Insert new meeting
        meeting_type = data.get("meeting_type", "Regular Meeting")
        title = f"Board of Education {meeting_type}"
        db.execute("""
            INSERT INTO meetings (date, committee, event_id, has_transcript, has_video, has_audio,
                                  word_count, speaker_count, duration_seconds)
            VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)
        """, (
            date, COMMITTEE_NAME, event_id,
            has_video, has_audio,
            data.get("word_count", 0),
            data.get("speaker_count", 0),
            data.get("duration_seconds", 0),
        ))
        new_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        print(f"  Created meeting: {date} {COMMITTEE_NAME} (#{new_id})")

    db.commit()
    db.close()
    return True


def cmd_ingest_all():
    """Ingest all processed YouTube transcripts into rag.db meetings table."""
    import glob
    transcript_files = sorted(glob.glob(os.path.join(TRANSCRIPTS_DIR, "transcript-yt-*.json")))

    if not transcript_files:
        print("No YouTube transcripts found to ingest.")
        return

    print(f"Found {len(transcript_files)} YouTube transcripts")
    success = 0
    for path in transcript_files:
        # Extract video_id from filename: transcript-yt-{VIDEO_ID}.json
        filename = os.path.basename(path)
        video_id = filename.replace("transcript-yt-", "").replace(".json", "")
        print(f"\nIngesting {video_id}...")
        if cmd_ingest(video_id):
            success += 1

    print(f"\nIngested {success}/{len(transcript_files)} meetings")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]

    if cmd == "list":
        cmd_list()
    elif cmd == "captions" and len(sys.argv) > 2:
        cmd_captions(sys.argv[2])
    elif cmd == "captions-all":
        since = None
        if "--since" in sys.argv:
            idx = sys.argv.index("--since")
            if idx + 1 < len(sys.argv):
                since = sys.argv[idx + 1]
        cmd_captions_all(since)
    elif cmd == "download" and len(sys.argv) > 2:
        download_video(sys.argv[2])
    elif cmd == "download-recent":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 16
        cmd_download_recent(n)
    elif cmd == "transcribe" and len(sys.argv) > 2:
        cmd_transcribe(sys.argv[2])
    elif cmd == "transcribe-all":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 16
        cmd_transcribe_all(n)
    elif cmd == "ingest" and len(sys.argv) > 2:
        cmd_ingest(sys.argv[2])
    elif cmd == "ingest-all":
        cmd_ingest_all()
    elif cmd == "status":
        cmd_status()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
