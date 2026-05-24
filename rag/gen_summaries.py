#!/usr/bin/env python3
"""Generate polished Coming Up summaries for upcoming meetings via z.ai API."""
import urllib.request
import json
import sqlite3
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "rag.db")


def flatten(items, depth=0):
    lines = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = item.get("title", "")
        atts = item.get("attachments", [])
        att_names = [a.get("name", "") for a in atts if a.get("name")]
        indent = "  " * depth
        line = f"{indent}- {title}"
        if att_names:
            names_str = ", ".join(att_names[:2])
            line += f" [{len(att_names)} docs: {names_str}]"
        lines.append(line)
        lines.extend(flatten(item.get("children", []), depth + 1))
    return lines


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    api_base = os.environ.get("ANTHROPIC_BASE_URL", "")
    if not api_key or not api_base:
        print("ERROR: ANTHROPIC_API_KEY and ANTHROPIC_BASE_URL must be set")
        sys.exit(1)

    api_url = f"{api_base}/v1/messages"

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    # Find upcoming meetings with agendas but no article yet
    meetings = db.execute("""
        SELECT id, committee, date, agenda_json, quick_summary FROM meetings
        WHERE date >= date('now') AND agenda_json IS NOT NULL
        AND (article IS NULL OR article = '')
        ORDER BY date
    """).fetchall()

    if not meetings:
        print("  No upcoming meetings needing summaries")
        db.close()
        return

    updated = 0
    for m in meetings:
        old_summary = m["quick_summary"] or ""
        # Skip if already polished (doesn't start with a count or raw agenda text)
        if old_summary and not old_summary[0].isdigit() and "agenda items" not in old_summary.lower():
            continue

        agenda = json.loads(m["agenda_json"])
        agenda_text = "\n".join(flatten(agenda))

        prompt = f"""Write a 1-2 sentence preview summary for this upcoming {m["committee"]} meeting on {m["date"]} for the croton.news "Coming Up" section.

AGENDA:
{agenda_text}

Rules:
- Write like a local news journalist previewing the meeting for residents
- Lead with the most newsworthy/impactful items
- Mention specific addresses, dollar amounts, and project names when available
- Skip procedural items (call to order, adjournment, vouchers, minutes approval)
- Keep it under 250 characters
- Do NOT use quotes or attribution
- Output ONLY the summary text, nothing else"""

        try:
            payload = json.dumps({
                "model": "glm-5-turbo",
                "max_tokens": 200,
                "messages": [{"role": "user", "content": prompt}]
            })

            req = urllib.request.Request(api_url, data=payload.encode(), headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01"
            })

            resp = urllib.request.urlopen(req, timeout=60)
            resp_data = json.loads(resp.read())
            summary = resp_data["content"][0]["text"].strip()

            db.execute("UPDATE meetings SET quick_summary = ? WHERE id = ?",
                       (summary, m["id"]))
            updated += 1
            print(f"  {m['committee']} ({m['date']}): {summary}")

        except Exception as e:
            print(f"  ERROR for {m['committee']} ({m['date']}): {e}")

    db.commit()
    db.close()
    print(f"  {updated} summaries polished")


if __name__ == "__main__":
    main()
