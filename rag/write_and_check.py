#!/usr/bin/env python3
"""
croton.news — Write articles via WireClaw writer + fact-check via editor.

Finds meetings with transcripts but no articles, writes them through the
GLM-5-Turbo writer agent, then fact-checks against the transcript/minutes.

Designed to run after pipeline.py process-new (which handles download/transcribe/enrich/ingest).

Usage:
    python3 write_and_check.py                  # Process all ready meetings
    python3 write_and_check.py --meeting-id 126 # Process specific meeting
    python3 write_and_check.py --dry-run        # Show what would be processed
"""
import urllib.request
import json
import sqlite3
import os
import sys
import re

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "rag.db")

# Writer and editor system prompts (synced from WireClaw)
WRITER_PROMPT_PATH = os.path.join(BASE_DIR, "prompts", "writer.md")
EDITOR_PROMPT_PATH = os.path.join(BASE_DIR, "prompts", "editor.md")

# Fallback: inline minimal prompts if files don't exist
FALLBACK_WRITER = """You are a local news journalist for croton.news covering Croton-on-Hudson village government.
Write factual, engaging articles based ONLY on the provided transcript/minutes.
Do NOT invent quotes, names, statistics, or details not in the source material.
Every claim must be traceable to the source."""

FALLBACK_EDITOR = """You are a strict fact-checking editor. Verify every claim in the article against the source material.
Check: names, quotes, votes, dollar amounts, dates, and roles.
Flag any fabricated or unverifiable claims."""


def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def api_call(system_prompt, user_prompt, max_tokens=16000):
    """Call the z.ai API with GLM-5-Turbo."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    api_base = os.environ.get("ANTHROPIC_BASE_URL", "")
    if not api_key or not api_base:
        print("  ERROR: ANTHROPIC_API_KEY and ANTHROPIC_BASE_URL must be set")
        return None

    payload = json.dumps({
        "model": "glm-5-turbo",
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}]
    })

    req = urllib.request.Request(
        f"{api_base}/v1/messages",
        data=payload.encode(),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01"
        }
    )

    try:
        resp = urllib.request.urlopen(req, timeout=300)
        resp_data = json.loads(resp.read())
        output = ""
        for block in resp_data.get("content", []):
            if block.get("type") == "text":
                output += block["text"]
        return output
    except Exception as e:
        print(f"  API error: {e}")
        return None


def load_prompt(path, fallback):
    """Load a system prompt from file, fall back to inline."""
    if os.path.exists(path):
        with open(path) as f:
            return f.read()
    return fallback


def find_related_meetings(db, meeting, source_text):
    """Find other meetings that reference the same addresses, projects, or applicants."""
    related = []
    mid = meeting["id"]

    # Extract addresses (e.g., "129 Scenic Drive") and applicant names from source
    import re
    addresses = re.findall(r'\d+\s+(?:[A-Z][a-z]+\s+){1,3}(?:Ave|Road|Street|Drive|Way|Lane|Blvd|Place|Court)\w*',
                           source_text[:5000])
    # Deduplicate
    addresses = list(set(addresses))

    for addr in addresses[:3]:  # Limit to 3 addresses
        # Search agenda_json for this address
        matches = db.execute("""
            SELECT id, committee, date, quick_summary FROM meetings
            WHERE id != ? AND agenda_json LIKE ? AND date >= date('now', '-180 days')
            ORDER BY date
        """, (mid, f"%{addr}%")).fetchall()

        for m in matches:
            if not any(r["id"] == m["id"] for r in related):
                url = f"meetings/{m['id']}"
                related.append({
                    "id": m["id"],
                    "committee": m["committee"],
                    "date": m["date"],
                    "summary": m["quick_summary"] or "",
                    "url": url,
                })

    return related[:5]  # Cap at 5 related meetings


def get_source_text(db, meeting):
    """Get the best available source text for a meeting (transcript or minutes)."""
    event_id = meeting["event_id"]
    # Try transcript chunks first
    if event_id:
        chunks = db.execute(
            "SELECT content FROM chunks WHERE doc_id = ? ORDER BY chunk_index",
            (str(event_id),)
        ).fetchall()
        if chunks:
            return "\n".join(c["content"] for c in chunks), "transcript"

    # Fall back to minutes_text
    if meeting["minutes_text"]:
        return meeting["minutes_text"], "minutes"

    return None, None


def write_article(db, meeting, writer_prompt):
    """Write an article for a meeting using the writer agent."""
    source_text, source_type = get_source_text(db, meeting)
    if not source_text:
        print(f"  No source text available for meeting {meeting['id']}")
        return None

    # Cap source text to avoid token limits
    if len(source_text) > 60000:
        source_text = source_text[:60000]

    agenda = meeting["agenda_json"] or ""
    mid = meeting["id"]

    # Find related meetings for cross-references
    related = find_related_meetings(db, meeting, source_text)
    related_section = ""
    if related:
        related_section = "\n== RELATED MEETINGS (link to these if relevant) ==\n\n"
        for r in related:
            related_section += f"- [{r['committee']} ({r['date']})](/{r['url']}) — {r['summary']}\n"

    prompt = f"""Write a news article for meeting ID {mid}: {meeting["committee"]} on {meeting["date"]}.

== MEETING {source_type.upper()} ==

{source_text}

== AGENDA ==

{agenda}
{related_section}

== INSTRUCTIONS ==

Write a factual news article based on the {source_type} and agenda. Every claim must be traceable to the source. Do NOT invent quotes or details.

Output format:
JSON_START
{{"meeting_id": {mid}, "headline": "...", "quick_summary": "...", "key_actions": [...], "article": "..."}}
JSON_END
"""

    output = api_call(writer_prompt, prompt)
    if not output:
        return None

    m = re.search(r'_?JSON_START\s*(.*?)\s*_?JSON_END', output, re.DOTALL)
    if not m:
        print(f"  Could not extract JSON from writer output")
        return None

    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        # Try fixing trailing commas
        raw = m.group(1).strip()
        raw = re.sub(r',\s*}', '}', raw)
        raw = re.sub(r',\s*]', ']', raw)
        try:
            return json.loads(raw)
        except Exception:
            print(f"  JSON parse error in writer output")
            return None


def fact_check(db, meeting, article_text, headline, editor_prompt):
    """Fact-check an article against its source material."""
    source_text, source_type = get_source_text(db, meeting)
    if not source_text:
        return "SKIP", None

    if len(source_text) > 60000:
        source_text = source_text[:60000]

    prompt = f"""FACT-CHECK REVIEW — Meeting {meeting["id"]}: {meeting["committee"]} ({meeting["date"]})

HEADLINE: {headline}

== {source_type.upper()} (SOURCE OF TRUTH) ==

{source_text}

== ARTICLE TO CHECK ==

{article_text}

== INSTRUCTIONS ==

Compare the article against the {source_type}. Check every name, quote, vote, dollar amount, and factual claim.

Output format:
EDITOR_RESULT: PASS|CORRECTED|REJECT

If errors found:
CORRECTIONS:
- [ERROR 1]: ...

Then corrected article:
JSON_START
{{"meeting_id": {meeting["id"]}, "headline": "...", "article": "...", "corrections_made": [...]}}
JSON_END
"""

    output = api_call(editor_prompt, prompt)
    if not output:
        return "ERROR", None

    if "EDITOR_RESULT: PASS" in output:
        return "PASS", None
    elif "EDITOR_RESULT: REJECT" in output:
        return "REJECT", output
    elif "EDITOR_RESULT: CORRECTED" in output:
        m = re.search(r'_?JSON_START\s*(.*?)\s*_?JSON_END', output, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                return "CORRECTED", data
            except Exception:
                raw = m.group(1).strip()
                raw = re.sub(r',\s*}', '}', raw)
                raw = re.sub(r',\s*]', ']', raw)
                try:
                    return "CORRECTED", json.loads(raw)
                except Exception:
                    return "CORRECTED", None
        return "CORRECTED", None

    return "UNKNOWN", output


def main():
    dry_run = "--dry-run" in sys.argv
    specific_id = None
    for i, arg in enumerate(sys.argv):
        if arg == "--meeting-id" and i + 1 < len(sys.argv):
            specific_id = int(sys.argv[i + 1])

    db = get_db()

    # Find meetings ready for article writing
    if specific_id:
        meetings = db.execute("""
            SELECT id, committee, date, event_id, has_transcript, has_minutes,
                   minutes_text, agenda_json, article, article_model
            FROM meetings WHERE id = ?
        """, (specific_id,)).fetchall()
    else:
        meetings = db.execute("""
            SELECT id, committee, date, event_id, has_transcript, has_minutes,
                   minutes_text, agenda_json, article, article_model
            FROM meetings
            WHERE (has_transcript = 1 OR (minutes_text IS NOT NULL AND length(minutes_text) > 100))
            AND (article IS NULL OR article = '')
            AND date >= date('now', '-90 days')
            ORDER BY date DESC
        """).fetchall()

    if not meetings:
        print("No meetings ready for article writing")
        db.close()
        return

    print(f"Found {len(meetings)} meetings to process")

    if dry_run:
        for m in meetings:
            source_text, source_type = get_source_text(db, m)
            src_len = len(source_text) if source_text else 0
            print(f"  {m['id']:>3} {m['committee'][:40]:40} {m['date']}  source={source_type or 'none'} ({src_len} chars)")
        db.close()
        return

    writer_prompt = load_prompt(WRITER_PROMPT_PATH, FALLBACK_WRITER)
    editor_prompt = load_prompt(EDITOR_PROMPT_PATH, FALLBACK_EDITOR)

    results = {"written": 0, "pass": 0, "corrected": 0, "reject": 0, "error": 0}

    for m in meetings:
        print(f"\n--- {m['committee']} ({m['date']}) - Meeting {m['id']} ---")

        # Step 1: Write
        print("  Writing article...")
        article_data = write_article(db, m, writer_prompt)
        if not article_data:
            results["error"] += 1
            continue

        article_text = article_data.get("article", "")
        headline = article_data.get("headline", "")
        quick_summary = article_data.get("quick_summary", "")

        if not article_text or len(article_text) < 100:
            print(f"  Article too short ({len(article_text)} chars)")
            results["error"] += 1
            continue

        print(f"  Written: {headline[:60]}... ({len(article_text)} chars)")

        # Step 2: Fact-check
        print("  Fact-checking...")
        status, check_data = fact_check(db, m, article_text, headline, editor_prompt)
        print(f"  Result: {status}")

        if status == "CORRECTED" and check_data:
            article_text = check_data.get("article", article_text)
            headline = check_data.get("headline", headline)
            corrections = check_data.get("corrections_made", [])
            print(f"  Applied {len(corrections)} corrections")
            model_tag = "glm-5-turbo-writer-factchecked"
            results["corrected"] += 1
        elif status == "PASS":
            model_tag = "glm-5-turbo-writer-factchecked"
            results["pass"] += 1
        elif status == "REJECT":
            print("  REJECTED — skipping publication")
            results["reject"] += 1
            continue
        else:
            # Publish anyway but mark as unchecked
            model_tag = "glm-5-turbo-writer"
            results["error"] += 1

        # Step 3: Publish
        db.execute("""
            UPDATE meetings SET article = ?, headline = ?,
                   quick_summary = COALESCE(?, quick_summary),
                   article_model = ?, minutes_verified = CASE WHEN ? = 'PASS' OR ? = 'CORRECTED' THEN 1 ELSE 0 END
            WHERE id = ?
        """, (article_text, headline, quick_summary, model_tag, status, status, m["id"]))
        db.commit()
        results["written"] += 1
        print(f"  Published: {model_tag}")

    db.close()
    print(f"\nDone: {results['written']} written, {results['pass']} pass, "
          f"{results['corrected']} corrected, {results['reject']} reject, {results['error']} error")


if __name__ == "__main__":
    main()
