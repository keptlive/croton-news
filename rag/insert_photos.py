#!/usr/bin/env python3
"""
Post-article photo pipeline for croton.news.

After write_article.py generates text, this script:
1. Picks 2-3 key moments from the transcript (topic changes, key speakers)
2. Extracts video frames at those timestamps
3. Auto-crops based on layout detection (quad/podium/closeup/wide)
4. Upscales via Replicate API (Real-ESRGAN + face enhance)
5. Sharpens and saves to /photos/
6. Inserts {{photo:EVENT:SECONDS:CAPTION}} tags into the article
7. Creates an OG image for social sharing

Usage:
    python3 insert_photos.py EVENT_ID          # Full photo pipeline for one meeting
    python3 insert_photos.py EVENT_ID --dry-run # Show what would be done without saving
    python3 insert_photos.py --pending          # Process all articles missing photos
"""

import json
import os
import sqlite3
import subprocess
import sys
import time

# Load .env
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RAG_DB = os.path.join(BASE_DIR, "rag.db")
TRANSCRIPTS_DIR = os.path.join(BASE_DIR, "transcripts")
VIDEOS_DIR = os.path.join(os.path.dirname(BASE_DIR), "videos")
if not os.path.isdir(VIDEOS_DIR):
    VIDEOS_DIR = "/opt/croton-news/videos"
PHOTOS_DIR = os.path.join(os.path.dirname(BASE_DIR), "photos")
if not os.path.isdir(PHOTOS_DIR):
    PHOTOS_DIR = "/opt/croton-news/photos"

# Import frame_extract functions
sys.path.insert(0, BASE_DIR)
try:
    from frame_extract import (
        extract_frame, detect_layout, crop_frame, auto_face_crop,
        upscale_replicate, sharpen,
        QUAD_SPLIT, PODIUM_VIEW,
    )
    from PIL import Image
    HAS_FRAME_TOOLS = True
except ImportError as e:
    HAS_FRAME_TOOLS = False
    print(f"  Warning: frame_extract not available: {e}")


# LLM API for picking photo moments
ZAI_URL = "https://api.z.ai/api/anthropic/v1/messages"
ZAI_KEY = os.environ.get("ZAI_KEY", "")


def get_db():
    db = sqlite3.connect(RAG_DB)
    db.row_factory = sqlite3.Row
    return db


def pick_photo_moments(transcript, article_text, event_id, committee):
    """Use LLM to pick 2-3 timestamps for photos based on article + transcript.

    Returns list of dicts: [{timestamp: int, caption: str, type: "speaker"|"scene"|"document"}]
    """
    utterances = transcript.get("utterances", [])
    speaker_map = transcript.get("speaker_map", {})

    # Build a condensed timeline for the LLM
    timeline = []
    for u in utterances[::5]:  # every 5th utterance for brevity
        speaker = u.get("speaker", "?")
        num = speaker.replace("Speaker ", "")
        if num in speaker_map:
            speaker = speaker_map[num]
        ts = int(u.get("start", 0))
        text = u.get("text", "")[:100]
        timeline.append(f"[{ts}s] {speaker}: {text}")

    timeline_text = "\n".join(timeline[:80])

    prompt = f"""You are selecting 2-3 photo moments from a {committee} meeting video for a news article.

The article covers these topics:
{article_text[:2000]}

Here is a condensed transcript timeline (timestamp in seconds, speaker, text):
{timeline_text}

Pick 2-3 timestamps that would make the best photos for this article:
- One early in the meeting showing the board/committee in session (within first 2 minutes)
- One at a key discussion moment (a speaker making an important point)
- Optionally one showing public comment or a vote

For each, provide:
- timestamp: seconds into the video
- caption: a short news-style caption (who is shown, what's happening)
- type: "board" (wide shot of board), "speaker" (individual speaking), "audience" (public), "document" (screen share)

Respond with ONLY a JSON array:
[
  {{"timestamp": 60, "caption": "The Board of Trustees convenes for the April 22 meeting", "type": "board"}},
  {{"timestamp": 1234, "caption": "Village Manager Bryan Healy presents the budget proposal", "type": "speaker"}}
]"""

    try:
        req_data = json.dumps({
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 512,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()

        import urllib.request
        req = urllib.request.Request(ZAI_URL, data=req_data, headers={
            "Content-Type": "application/json",
            "x-api-key": ZAI_KEY,
            "anthropic-version": "2023-06-01",
        })
        resp = urllib.request.urlopen(req, timeout=30)
        result = json.loads(resp.read())
        text = result.get("content", [{}])[0].get("text", "")

        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            text = text.rsplit("```", 1)[0]
        text = text.strip()

        moments = json.loads(text)
        return moments[:3]

    except Exception as e:
        print(f"  LLM photo selection failed: {e}")
        # Fallback: pick t=60 (opening), and a midpoint
        duration = utterances[-1].get("end", 300) if utterances else 300
        midpoint = int(duration * 0.4)
        return [
            {"timestamp": 60, "caption": f"{committee} meeting in session", "type": "board"},
            {"timestamp": midpoint, "caption": "Discussion during the meeting", "type": "speaker"},
        ]


def process_frame(event_id, timestamp, photo_type, upscale=True, sharpen_amount=1.5):
    """Extract, crop, upscale, and save a single frame.

    Returns the saved filename (relative to photos dir) or None.
    """
    video_path = os.path.join(VIDEOS_DIR, f"{event_id}.mp4")
    if not os.path.exists(video_path):
        print(f"  No video: {video_path}")
        return None

    os.makedirs(PHOTOS_DIR, exist_ok=True)

    # Extract frame
    frame_path = os.path.join(PHOTOS_DIR, f"{event_id}_t{timestamp}_raw.png")
    try:
        extract_frame(video_path, timestamp, frame_path)
    except Exception as e:
        print(f"  Frame extraction failed at t={timestamp}: {e}")
        return None

    img = Image.open(frame_path)
    layout = detect_layout(img)
    print(f"  t={timestamp}s: layout={layout}, type={photo_type}")

    # Crop based on photo type and detected layout
    if photo_type == "document" and layout == "quad":
        cropped = crop_frame(img, QUAD_SPLIT["document"])
    elif photo_type == "board":
        if layout == "quad":
            cropped = crop_frame(img, QUAD_SPLIT["board"])
        else:
            cropped = img  # wide shot, keep full
    elif photo_type == "speaker":
        if layout == "podium":
            cropped = crop_frame(img, PODIUM_VIEW["podium"])
        elif layout == "closeup":
            box = auto_face_crop(img)
            cropped = crop_frame(img, box)
        elif layout == "quad":
            cropped = crop_frame(img, QUAD_SPLIT["audience"])
        else:
            cropped = img
    elif photo_type == "audience":
        if layout == "quad":
            cropped = crop_frame(img, QUAD_SPLIT["audience"])
        else:
            cropped = img
    else:
        cropped = img

    # Upscale
    if upscale and HAS_FRAME_TOOLS:
        try:
            cropped = upscale_replicate(cropped, model="auto", scale=2, layout=layout)
        except Exception as e:
            print(f"  Upscale failed: {e}")

    # Sharpen
    if sharpen_amount > 0 and HAS_FRAME_TOOLS:
        cropped = sharpen(cropped, sharpen_amount)

    # Save as JPG for web
    final_name = f"{event_id}_t{timestamp}.jpg"
    final_path = os.path.join(PHOTOS_DIR, final_name)
    cropped.convert("RGB").save(final_path, "JPEG", quality=88, optimize=True)
    print(f"  Saved: {final_name} ({cropped.size[0]}x{cropped.size[1]})")

    # Cleanup raw frame
    if os.path.exists(frame_path):
        os.remove(frame_path)

    return final_name


def create_og_image(event_id):
    """Create an OG image (1200x630) for social sharing from the first photo or video frame."""
    og_name = f"{event_id}_t60_og.jpg"
    og_path = os.path.join(PHOTOS_DIR, og_name)

    if os.path.exists(og_path):
        return og_name

    # Try to use the t=60 photo if it exists
    source_path = os.path.join(PHOTOS_DIR, f"{event_id}_t60.jpg")
    if not os.path.exists(source_path):
        # Extract a frame at t=60 directly
        video_path = os.path.join(VIDEOS_DIR, f"{event_id}.mp4")
        if not os.path.exists(video_path):
            return None
        raw_path = os.path.join(PHOTOS_DIR, f"{event_id}_t60_raw.png")
        try:
            extract_frame(video_path, 60, raw_path)
            source_path = raw_path
        except Exception:
            return None

    try:
        img = Image.open(source_path)
        # Resize to OG dimensions (1200x630) with center crop
        target_w, target_h = 1200, 630
        target_ratio = target_w / target_h
        img_ratio = img.width / img.height

        if img_ratio > target_ratio:
            # Image is wider — crop sides
            new_w = int(img.height * target_ratio)
            x_offset = (img.width - new_w) // 2
            img = img.crop((x_offset, 0, x_offset + new_w, img.height))
        else:
            # Image is taller — crop top/bottom
            new_h = int(img.width / target_ratio)
            y_offset = (img.height - new_h) // 2
            img = img.crop((0, y_offset, img.width, y_offset + new_h))

        img = img.resize((target_w, target_h), Image.LANCZOS)
        img.convert("RGB").save(og_path, "JPEG", quality=85, optimize=True)
        print(f"  OG image: {og_name}")

        # Cleanup raw if we created it
        raw_path = os.path.join(PHOTOS_DIR, f"{event_id}_t60_raw.png")
        if os.path.exists(raw_path):
            os.remove(raw_path)

        return og_name
    except Exception as e:
        print(f"  OG image failed: {e}")
        return None


def insert_photos_into_article(event_id, dry_run=False):
    """Full photo pipeline for a single meeting article."""
    if not HAS_FRAME_TOOLS:
        print("ERROR: Pillow and frame_extract required")
        return False

    db = get_db()
    meeting = db.execute("SELECT * FROM meetings WHERE event_id = ?", (str(event_id),)).fetchone()
    if not meeting:
        print(f"No meeting found for event {event_id}")
        db.close()
        return False

    article = meeting["article"]
    if not article:
        print(f"No article for event {event_id}")
        db.close()
        return False

    # Check if photos already inserted
    if "{{photo:" in article:
        print(f"Photos already in article for event {event_id}")
        db.close()
        return True

    # Load transcript
    transcript_path = os.path.join(TRANSCRIPTS_DIR, f"transcript-{event_id}.json")
    if not os.path.exists(transcript_path):
        print(f"No transcript for event {event_id}")
        db.close()
        return False

    with open(transcript_path) as f:
        transcript = json.load(f)

    committee = meeting["committee"]
    print(f"\nPhoto pipeline: {committee} ({meeting['date']}), event {event_id}")

    # Step 1: Pick photo moments
    print("  Selecting photo moments...")
    moments = pick_photo_moments(transcript, article, event_id, committee)
    print(f"  Selected {len(moments)} moments")

    if dry_run:
        for m in moments:
            print(f"    t={m['timestamp']}s [{m['type']}]: {m['caption']}")
        db.close()
        return True

    # Step 2: Process each frame (extract, crop, upscale, sharpen)
    photo_tags = []
    for m in moments:
        ts = m["timestamp"]
        caption = m["caption"]
        photo_type = m.get("type", "board")

        filename = process_frame(event_id, ts, photo_type, upscale=True, sharpen_amount=1.5)
        if filename:
            tag = f"{{{{photo:{event_id}:{ts}:{caption}}}}}"
            photo_tags.append({"tag": tag, "timestamp": ts})

    if not photo_tags:
        print("  No photos generated")
        db.close()
        return False

    # Step 3: Insert photo tags into article at natural break points
    paragraphs = article.split("\n\n")
    if len(paragraphs) < 2:
        paragraphs = article.split("\n")

    # Insert first photo after the lead paragraph
    if len(photo_tags) >= 1 and len(paragraphs) >= 2:
        paragraphs.insert(1, "\n" + photo_tags[0]["tag"] + "\n")

    # Insert second photo roughly in the middle
    if len(photo_tags) >= 2 and len(paragraphs) >= 5:
        mid = len(paragraphs) // 2
        paragraphs.insert(mid, "\n" + photo_tags[1]["tag"] + "\n")

    # Insert third photo near the end (before last 2 paragraphs)
    if len(photo_tags) >= 3 and len(paragraphs) >= 8:
        end_pos = max(len(paragraphs) - 2, 3)
        paragraphs.insert(end_pos, "\n" + photo_tags[2]["tag"] + "\n")

    new_article = "\n\n".join(paragraphs)

    # Step 4: Create OG image
    create_og_image(event_id)

    # Step 5: Save updated article
    db.execute("UPDATE meetings SET article = ? WHERE event_id = ?", (new_article, str(event_id)))
    db.commit()
    db.close()

    print(f"  Inserted {len(photo_tags)} photos into article")
    return True


def process_pending():
    """Process all articles that don't have photos yet."""
    db = get_db()
    pending = db.execute("""
        SELECT event_id, committee, date FROM meetings
        WHERE article IS NOT NULL AND article != ''
        AND article NOT LIKE '%{{photo:%'
        AND has_video = 1
        AND event_id IS NOT NULL AND event_id NOT LIKE 'yt-%'
        ORDER BY date DESC
    """).fetchall()
    db.close()

    print(f"Found {len(pending)} articles without photos")
    success = 0
    for row in pending:
        try:
            if insert_photos_into_article(int(row["event_id"])):
                success += 1
        except Exception as e:
            print(f"  Error processing {row['event_id']}: {e}")

    print(f"\nProcessed {success}/{len(pending)} articles")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    if sys.argv[1] == "--pending":
        process_pending()
    else:
        event_id = sys.argv[1]
        dry_run = "--dry-run" in sys.argv
        insert_photos_into_article(int(event_id), dry_run=dry_run)
