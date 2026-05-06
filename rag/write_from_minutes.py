#!/usr/bin/env python3
"""
write_from_minutes.py — Generate footnoted articles from BoardDocs meeting minutes + attachments.

Uses official minutes as the primary source, supplemented by agenda packet
attachments (budget presentations, reports, policies) for additional detail.
Generates articles with proper footnotes citing both minutes and specific documents.

Usage:
    python3 write_from_minutes.py              # Process all eligible meetings
    python3 write_from_minutes.py 95           # Process specific meeting ID
    python3 write_from_minutes.py 95 --regen   # Regenerate even if article exists
    python3 write_from_minutes.py --dry-run    # Show what would be processed

Cron: daily 7:30 AM (after boarddocs sync)
"""

import json
import os
import sqlite3
import sys
from datetime import datetime

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
MODEL = "anthropic/claude-sonnet-4"
ARTICLE_MODEL_TAG = "claude-sonnet-4-minutes"
MIN_MINUTES_LENGTH = 1000


def get_openrouter_key():
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        try:
            import subprocess
            r = subprocess.run(
                ["systemctl", "show", "croton-news", "--property=Environment"],
                capture_output=True, text=True, timeout=5,
            )
            for part in r.stdout.split():
                if part.startswith("OPENROUTER_API_KEY="):
                    key = part.split("=", 1)[1]
        except Exception:
            pass
    return key


PROMPT_TEMPLATE = """You are writing a news article for croton.news covering the {committee} meeting on {day_of_week}, {date_formatted}.

This article is based on the official meeting minutes, supplemented by {num_attachments} agenda packet documents that provide additional detail.

SOURCES AVAILABLE:

**Primary Source — Official Minutes:**
{minutes_text}

{attachments_section}

REQUIREMENTS:

1. FOOTNOTES: Every factual claim, vote, dollar amount, or quote MUST have a footnote.
   Format: [1], [2], etc. inline.

   At the end, list footnotes in this exact format:

   **Footnotes:**
   [1] Minutes: "quoted text from minutes"
   [2] Minutes: Motion by [Name], seconded by [Name]
   [3] [{example_doc_name}]({example_doc_url}): [relevant detail from document]
   [4] Minutes: Resolution [number], approved [vote count]

   - For minutes citations, use "Minutes:" prefix with a direct quote or paraphrase
   - For attachment citations, use a markdown link with the document filename as link text
     and the document URL as the href: [filename.pdf](URL): detail
   - Every number/statistic from an attachment MUST cite that specific document
   - IMPORTANT: Always use the exact URL provided for each document

2. COVERAGE: Cover ALL substantive items from the minutes. Skip only purely procedural
   items (call to order, pledge, adjournment) unless something notable occurred.
   Use attachment documents to add specific details (dollar amounts, program descriptions,
   terms of agreements) that the minutes reference but don't fully explain.

3. DAY OF WEEK: The meeting was on {day_of_week}. Do NOT use any other day name.

4. VOICE: Past tense. Neutral, factual, local newspaper style.

5. FORMAT:
   - Newspaper-style headline (plain text, no markdown #)
   - Plain text paragraphs (no markdown bold/italic in body)
   - Organize by topic/importance, not agenda order
   - ~1000-1200 words for meetings with attachments, ~800 for minutes-only
   - End with **Footnotes:** section

6. VOTES: Include all vote counts. Note split votes or abstentions by name.

7. QUOTES: Use direct quotes from minutes where they add value.
   Attribute to the correct speaker.

8. NUMBERS: When referencing budget figures, contract amounts, or statistics,
   prefer the specific numbers from attachment documents over rounded figures
   in minutes. Always footnote the source document."""


ATTACHMENTS_SECTION_TEMPLATE = """**Supplemental Source — Agenda Packet Documents:**
{docs}"""

DOC_TEMPLATE = """--- Document: {filename} ({pages}p, {chars} chars)
    URL: {url}
---
{text}"""


def get_attachments(db, meeting_id):
    """Get packet_pdfs attachments for this meeting's boarddocs_id."""
    # Get boarddocs_id for this meeting
    row = db.execute(
        "SELECT boarddocs_id FROM meetings WHERE id = ?", (meeting_id,)
    ).fetchone()
    if not row or not row["boarddocs_id"]:
        return []

    boarddocs_id = row["boarddocs_id"]
    attachments = db.execute("""
        SELECT nickname, agenda_item_title, text, pages, char_count, source_url
        FROM packet_pdfs
        WHERE event_id = ? AND char_count > 100
        ORDER BY char_count DESC
    """, (boarddocs_id,)).fetchall()

    return attachments


def build_attachments_section(attachments, max_chars=20000):
    """Build attachment text section, truncating large docs to fit context."""
    if not attachments:
        return ""

    docs = []
    used_chars = 0
    for att in attachments:
        text = att["text"]
        # Budget for each doc: proportional to its content, up to max
        budget = min(len(text), max(2000, (max_chars - used_chars) // max(1, len(attachments) - len(docs))))
        if used_chars + budget > max_chars:
            budget = max_chars - used_chars
        if budget <= 0:
            break

        truncated = text[:budget]
        if len(text) > budget:
            truncated += f"\n[... truncated, {len(text) - budget} more chars ...]"

        docs.append(DOC_TEMPLATE.format(
            filename=att["nickname"],
            pages=att["pages"],
            chars=att["char_count"],
            url=att["source_url"] or "",
            text=truncated,
        ))
        used_chars += len(truncated)

    return ATTACHMENTS_SECTION_TEMPLATE.format(docs="\n\n".join(docs))


def generate_article(committee, date, minutes_text, attachments):
    """Call OpenRouter to generate a footnoted article from minutes + attachments."""
    from openai import OpenAI

    key = get_openrouter_key()
    if not key:
        print("  ERROR: No OPENROUTER_API_KEY found")
        return None

    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=key)

    # Compute day of week
    try:
        d = datetime.strptime(date, "%Y-%m-%d")
        day_of_week = d.strftime("%A")
        date_formatted = d.strftime("%B %d, %Y")
    except Exception:
        day_of_week = ""
        date_formatted = date

    # Build attachments section
    attachments_section = build_attachments_section(attachments)
    num_attachments = len(attachments) if attachments else 0

    # Example footnote for prompt
    if attachments:
        example_doc_name = attachments[0]["nickname"]
        example_doc_url = attachments[0]["source_url"] or "#"
    else:
        example_doc_name = "Document Name.pdf"
        example_doc_url = "#"


    prompt = PROMPT_TEMPLATE.format(
        committee=committee,
        date=date,
        day_of_week=day_of_week,
        date_formatted=date_formatted,
        minutes_text=minutes_text[:28000],
        attachments_section=attachments_section or "(No supplemental documents available)",
        num_attachments=num_attachments,
        example_doc_name=example_doc_name,
        example_doc_url=example_doc_url,
    )

    # Adjust max tokens based on available sources
    max_tokens = 4500 if attachments else 3500

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"  ERROR: API call failed: {e}")
        return None


def generate_summary(headline, article_text):
    """Generate a quick_summary from article body."""
    lines = article_text.split("\n\n")
    for line in lines:
        line = line.strip()
        if line.startswith("_This article") or not line or line.startswith("["):
            continue
        if line.startswith("**Footnotes"):
            break
        if len(line) > 120:
            return line[:150].rsplit(" ", 1)[0] + "..."
        return line
    return headline


def find_eligible_meetings(db, specific_id=None, regen=False):
    """Find meetings with minutes but no article (or all if regen)."""
    if specific_id:
        return db.execute("""
            SELECT id, date, committee, minutes_text, headline, boarddocs_id
            FROM meetings WHERE id = ?
        """, (specific_id,)).fetchall()

    if regen:
        return db.execute("""
            SELECT id, date, committee, minutes_text, headline, boarddocs_id
            FROM meetings
            WHERE has_minutes = 1
            AND minutes_text IS NOT NULL
            AND length(minutes_text) > ?
            AND date < date('now')
            ORDER BY date DESC
        """, (MIN_MINUTES_LENGTH,)).fetchall()

    return db.execute("""
        SELECT id, date, committee, minutes_text, headline, boarddocs_id
        FROM meetings
        WHERE has_minutes = 1
        AND minutes_text IS NOT NULL
        AND length(minutes_text) > ?
        AND (article IS NULL OR article = '')
        AND date < date('now')
        ORDER BY date DESC
    """, (MIN_MINUTES_LENGTH,)).fetchall()


def process_meeting(db, meeting, regen=False):
    """Generate and save article for one meeting."""
    mid = meeting["id"]
    date = meeting["date"]
    committee = meeting["committee"]
    minutes = meeting["minutes_text"]

    # Get attachments
    attachments = get_attachments(db, mid)
    att_info = f", {len(attachments)} attachments" if attachments else ""
    print(f"  [{mid}] {date} {committee} ({len(minutes)} chars of minutes{att_info})")

    if meeting["headline"] and not regen:
        print(f"  SKIP: Already has article: \"{meeting['headline']}\"")
        return False

    article_text = generate_article(committee, date, minutes, attachments)
    if not article_text:
        return False

    # Parse headline — skip source label if model put it first
    lines = article_text.strip().split("\n")
    headline = ""
    body_start = 0
    for i, line in enumerate(lines):
        line_clean = line.strip().lstrip("#").strip().strip("*").strip()
        if not line_clean:
            continue
        if line_clean.startswith("_This article") or line_clean.startswith("_Source:"):
            continue
        headline = line_clean
        body_start = i + 1
        break
    body = "\n".join(lines[body_start:]).strip()

    if not body or len(body) < 200:
        print(f"  WARNING: Article too short ({len(body)} chars), skipping")
        return False

    summary = generate_summary(headline, body)

    # Tag model based on whether attachments were used
    model_tag = ARTICLE_MODEL_TAG
    if attachments:
        model_tag = "claude-sonnet-4-minutes+docs"

    db.execute("""
        UPDATE meetings SET
            article = ?,
            headline = ?,
            article_model = ?,
            article_generated_at = datetime('now'),
            quick_summary = ?
        WHERE id = ?
    """, (body, headline, model_tag, summary, mid))
    db.commit()

    print(f"  OK: \"{headline}\" ({len(body)} chars, {model_tag})")
    return True


def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    regen = "--regen" in args
    specific_id = None
    for a in args:
        if a.isdigit():
            specific_id = a

    db = sqlite3.connect(RAG_DB)
    db.row_factory = sqlite3.Row

    meetings = find_eligible_meetings(db, specific_id, regen)
    print(f"Found {len(meetings)} eligible meeting(s)")

    if not meetings:
        print("Nothing to process.")
        db.close()
        return

    processed = 0
    for m in meetings:
        if dry_run:
            atts = get_attachments(db, m["id"])
            att_info = f", {len(atts)} attachments" if atts else ""
            print(f"  [DRY RUN] {m['id']} {m['date']} {m['committee']} ({len(m['minutes_text'])} chars{att_info})")
            continue
        if process_meeting(db, m, regen):
            processed += 1

    db.close()

    if dry_run:
        print(f"\nDry run complete. {len(meetings)} meeting(s) would be processed.")
    else:
        print(f"\nDone. Processed {processed}/{len(meetings)} meeting(s).")


if __name__ == "__main__":
    main()
