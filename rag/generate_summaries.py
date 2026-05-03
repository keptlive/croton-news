#!/usr/bin/env python3
"""
Generate quick_summary for articles that are missing one.

Runs as a separate pipeline step after article generation.
Uses a lightweight LLM call (Gemini Flash) with just the headline
and first ~1500 chars of the article to produce a 1-2 sentence summary.

Usage:
  python3 generate_summaries.py          # backfill all missing
  python3 generate_summaries.py --id 91  # specific article
  python3 generate_summaries.py --dry    # preview without saving
"""

import os
import sys
import json
import sqlite3
import requests

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rag.db")
GEMINI_PROXY = "http://192.227.249.10:8787/gemini"
GEMINI_MODEL = "gemini-2.0-flash"

PROMPT = """Write a 1-2 sentence news summary for a local newspaper homepage.
It should tell the reader what happened and why it matters, in under 400 characters.
Do NOT start with the committee name or "The Board...". Lead with the most newsworthy fact.
Return ONLY the summary text, no labels or quotes.

Headline: {headline}

Article excerpt:
{excerpt}"""


def get_summary(headline, article_text):
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print("ERROR: GEMINI_API_KEY not set", file=sys.stderr)
        return None

    excerpt = article_text[:1500]
    url = f"{GEMINI_PROXY}/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}"

    payload = {
        "contents": [{"parts": [{"text": PROMPT.format(headline=headline, excerpt=excerpt)}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 256},
    }

    try:
        resp = requests.post(url, json=payload, timeout=30)
        if resp.status_code != 200:
            print(f"  Gemini API error: {resp.status_code}", file=sys.stderr)
            return None
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        # Clean up any wrapping quotes
        if text.startswith('"') and text.endswith('"'):
            text = text[1:-1]
        return text
    except Exception as e:
        print(f"  Error: {e}", file=sys.stderr)
        return None


def main():
    args = sys.argv[1:]
    target_id = None
    dry_run = False

    i = 0
    while i < len(args):
        if args[i] == "--id" and i + 1 < len(args):
            target_id = int(args[i + 1])
            i += 2
        elif args[i] == "--dry":
            dry_run = True
            i += 1
        else:
            i += 1

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    if target_id:
        rows = db.execute(
            "SELECT id, headline, article FROM meetings WHERE id = ? AND article IS NOT NULL",
            (target_id,)
        ).fetchall()
    else:
        rows = db.execute(
            """SELECT id, headline, article FROM meetings
               WHERE article IS NOT NULL AND article != ''
               AND (quick_summary IS NULL OR quick_summary = '')
               ORDER BY date DESC"""
        ).fetchall()

    if not rows:
        print("No articles need summaries.")
        return

    print(f"Generating summaries for {len(rows)} article(s)...")

    for row in rows:
        headline = row["headline"] or "(no headline)"
        print(f"\n  [{row['id']}] {headline}")

        summary = get_summary(headline, row["article"])
        if not summary:
            print("    SKIPPED (no summary generated)")
            continue

        print(f"    → {summary}")

        if not dry_run:
            db.execute(
                "UPDATE meetings SET quick_summary = ? WHERE id = ?",
                (summary, row["id"])
            )
            db.commit()
            print("    SAVED")
        else:
            print("    (dry run)")

    print(f"\nDone.")


if __name__ == "__main__":
    main()
