#!/usr/bin/env python3
"""Download, transcribe, enrich, and ingest any new videos."""
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from pipeline import (get_db, download_video, transcribe_video,
                       enrich_transcript_file, ingest_transcript,
                       VIDEOS_DIR, TRANSCRIPTS_DIR)


def main():
    db = get_db()

    # Download missing videos
    to_dl = db.execute("""SELECT event_id FROM meetings
        WHERE has_video = 1 AND has_transcript = 0
        AND event_id IS NOT NULL AND event_id NOT LIKE 'yt-%'""").fetchall()
    downloaded = 0
    for row in to_dl:
        eid = int(row["event_id"])
        vpath = os.path.join(VIDEOS_DIR, f"{eid}.mp4")
        if not os.path.exists(vpath):
            if download_video(eid):
                downloaded += 1
    if downloaded:
        print(f"  Downloaded {downloaded} videos")

    # Transcribe
    to_tx = db.execute("""SELECT event_id FROM meetings
        WHERE has_video = 1 AND has_transcript = 0
        AND event_id IS NOT NULL AND event_id NOT LIKE 'yt-%'""").fetchall()
    transcribed = 0
    for row in to_tx:
        eid = int(row["event_id"])
        vpath = os.path.join(VIDEOS_DIR, f"{eid}.mp4")
        if os.path.exists(vpath):
            if transcribe_video(eid):
                transcribed += 1
    if transcribed:
        print(f"  Transcribed {transcribed} videos")

    # Enrich
    to_enrich = db.execute("""SELECT event_id FROM meetings
        WHERE has_transcript = 1 AND event_id IS NOT NULL
        AND event_id NOT LIKE 'yt-%'""").fetchall()
    enriched = 0
    for row in to_enrich:
        eid = row["event_id"]
        tpath = os.path.join(TRANSCRIPTS_DIR, f"transcript-{eid}.json")
        if os.path.exists(tpath):
            with open(tpath) as f:
                tx = json.load(f)
            if not tx.get("enriched"):
                enrich_transcript_file(int(eid))
                enriched += 1
    if enriched:
        print(f"  Enriched {enriched} transcripts")

    # Ingest
    to_ingest = db.execute("""SELECT event_id FROM meetings
        WHERE has_transcript = 1 AND event_id IS NOT NULL
        AND event_id NOT LIKE 'yt-%'""").fetchall()
    ingested = 0
    for row in to_ingest:
        eid = row["event_id"]
        existing = db.execute(
            "SELECT COUNT(*) as n FROM chunks WHERE doc_id = ?", (eid,)
        ).fetchone()
        if existing["n"] == 0:
            if ingest_transcript(int(eid)):
                ingested += 1
    if ingested:
        print(f"  Ingested {ingested} transcripts")

    db.close()

    total = downloaded + transcribed + enriched + ingested
    if total == 0:
        print("  No new videos to process")


if __name__ == "__main__":
    main()
