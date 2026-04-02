#!/usr/bin/env python3
"""Batch-generate AI summaries for ecode360 documents.

Stores results in search.db summaries table with structured fields:
doc_id, summary, topics, key_people, key_locations, generated_at

Designed to run as a scheduled task — processes 10-20 docs per run,
skips already-summarized docs.

Usage:
    python3 batch_summaries.py                  # default: 20 docs, 5s interval
    python3 batch_summaries.py --limit 10       # process 10 docs
    python3 batch_summaries.py --dry-run        # show pending docs
"""

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent
SEARCH_DB = BASE_DIR / "search.db"
MINUTES_DIR = BASE_DIR / "minutes"

SUMMARY_MODEL = "nvidia/llama-3.1-nemotron-ultra-253b-v1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def get_openrouter_key():
    # Check multiple locations for the credentials file
    for cred_path in [
        BASE_DIR.parent / "openrouter_credentials.json",  # /opt/croton-news/
        Path("/opt/openrouter_credentials.json"),           # legacy location
    ]:
        if cred_path.exists():
            with open(cred_path) as f:
                key = json.load(f).get("openrouter_api_key", "")
                if key:
                    return key
    return os.environ.get("OPENROUTER_API_KEY", "")


def init_summaries_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS summaries (
            doc_id TEXT PRIMARY KEY,
            summary TEXT,
            topics TEXT,
            key_people TEXT,
            key_locations TEXT,
            generated_at TEXT
        )
    """)
    conn.commit()


def get_pending_docs(conn):
    """Return doc_ids that have text files but no summary yet."""
    c = conn.cursor()
    c.execute("""
        SELECT d.doc_id, d.committee, d.date
        FROM documents d
        LEFT JOIN summaries s ON d.doc_id = s.doc_id
        WHERE s.doc_id IS NULL
        ORDER BY d.date DESC
    """)
    return c.fetchall()


def generate_structured_summary(key, doc_id, committee, date, text):
    """Call Nemotron to produce summary + structured metadata as JSON."""
    doc_text = text[:16000]

    prompt = f"""Analyze these {committee} meeting minutes from {date} in Croton-on-Hudson, NY.

Return a JSON object with exactly these fields:
{{
  "summary": "bulleted summary (see format rules below)",
  "topics": "comma-separated list of 3-8 key topics discussed",
  "key_people": "comma-separated list of people mentioned by name with their role/title if stated",
  "key_locations": "comma-separated list of specific addresses, locations, or properties mentioned"
}}

SUMMARY FORMAT RULES:
• Start IMMEDIATELY with the first bullet — no title, heading, or introduction
• Use "•" for main topics and "  ◦" (indented) for key details
• Each main bullet = one major topic or decision, 1-2 sentences with full context
• Sub-bullets for: vote tallies, dollar amounts, specific addresses, names, deadlines
• Cover every significant topic — no arbitrary limit — but be CONCISE
• For public hearings: summarize the issue, arguments, and outcome — do NOT transcribe verbatim
• End after the last bullet — no closing remarks

TOPICS examples: "budget approval, zoning variance, site plan review, public hearing, infrastructure update"
KEY_PEOPLE examples: "Mayor Brian Pugh, Village Manager Bryan Healy, Ali Jaffery (resident)"
KEY_LOCATIONS examples: "52 Mount Airy Road, Dobbs Park, 25 S. Riverside Ave"

Return ONLY valid JSON, no markdown code fences, no explanation.

MEETING MINUTES:
{doc_text}"""

    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": SUMMARY_MODEL,
            "messages": [
                {"role": "system", "content": "You analyze village government meeting minutes and return structured JSON. Always return valid JSON with keys: summary, topics, key_people, key_locations. No markdown fences."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 2000,
        },
        timeout=60,
    )
    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"].strip()

    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*\n?", "", raw)
        raw = re.sub(r"\n?```\s*$", "", raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Fallback: treat entire response as summary
        log.warning("%s — JSON parse failed, using raw as summary", doc_id)
        data = {"summary": raw, "topics": committee, "key_people": "", "key_locations": ""}

    return {
        "summary": data.get("summary", "").strip(),
        "topics": data.get("topics", "").strip(),
        "key_people": data.get("key_people", "").strip(),
        "key_locations": data.get("key_locations", "").strip(),
    }


def main():
    parser = argparse.ArgumentParser(description="Batch-generate document summaries")
    parser.add_argument("--limit", type=int, default=20, help="Max docs to process per run (default: 20)")
    parser.add_argument("--interval", type=int, default=5, help="Seconds between API calls (default: 5)")
    parser.add_argument("--dry-run", action="store_true", help="Show pending docs without processing")
    args = parser.parse_args()

    if not SEARCH_DB.exists():
        log.error("search.db not found at %s", SEARCH_DB)
        sys.exit(1)

    key = get_openrouter_key()
    if not key:
        log.error("No OpenRouter API key found")
        sys.exit(1)

    conn = sqlite3.connect(str(SEARCH_DB))
    init_summaries_table(conn)
    pending = get_pending_docs(conn)

    log.info("Total pending: %d documents", len(pending))
    if args.dry_run:
        for doc_id, committee, date in pending[:30]:
            log.info("  %s | %s | %s", doc_id, committee, date)
        if len(pending) > 30:
            log.info("  ... and %d more", len(pending) - 30)
        conn.close()
        return

    batch = pending[:args.limit]
    log.info("Processing %d docs this run", len(batch))

    done = 0
    errors = 0
    for i, (doc_id, committee, date) in enumerate(batch):
        txt_path = MINUTES_DIR / f"{doc_id}.txt"
        if not txt_path.exists():
            log.warning("SKIP %s — no text file", doc_id)
            continue

        with open(txt_path) as f:
            text = f.read()
        if len(text.strip()) < 100:
            log.warning("SKIP %s — text too short (%d chars)", doc_id, len(text))
            continue

        try:
            result = generate_structured_summary(key, doc_id, committee or "committee", date or "unknown", text)
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT OR REPLACE INTO summaries (doc_id, summary, topics, key_people, key_locations, generated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (doc_id, result["summary"], result["topics"], result["key_people"], result["key_locations"], now),
            )
            conn.commit()
            done += 1
            log.info("[%d/%d] %s (%s %s) — summary: %d chars, topics: %s",
                     done, len(batch), doc_id, committee, date, len(result["summary"]),
                     result["topics"][:60])
        except Exception as e:
            errors += 1
            log.error("[%d/%d] %s FAILED: %s", done + errors, len(batch), doc_id, e)

        # Rate limit (skip sleep on last item)
        if i < len(batch) - 1:
            time.sleep(args.interval)

    conn.close()
    remaining = len(pending) - done
    log.info("Done: %d summaries generated, %d errors, %d remaining", done, errors, remaining)


if __name__ == "__main__":
    main()
