#!/usr/bin/env python3
"""Insert/update an article in the Croton rag.db."""
import json, sqlite3, sys

DB_PATH = "/opt/croton-news/rag/rag.db"

def main():
    data = json.load(sys.stdin)
    for f in ["meeting_id", "headline", "article"]:
        if f not in data:
            print(f"ERROR: Missing {f}", file=sys.stderr)
            sys.exit(1)

    mid = data["meeting_id"]
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    meeting = db.execute("SELECT id, date, committee, article FROM meetings WHERE id = ?", (mid,)).fetchone()
    if not meeting:
        print(f"ERROR: Meeting {mid} not found", file=sys.stderr)
        sys.exit(1)

    if meeting["article"]:
        print(f"WARNING: Meeting {mid} already has an article — overwriting", file=sys.stderr)

    article = data["article"]
    if data.get("key_actions"):
        article = f"**Key actions:**\n{data['key_actions']}\n\n{article}"

    db.execute("""
        UPDATE meetings SET
            headline = ?,
            quick_summary = ?,
            article = ?,
            article_model = ?,
            article_generated_at = datetime('now')
        WHERE id = ?
    """, (data["headline"], data.get("quick_summary", ""), article,
           data.get("article_model", "glm-5-turbo"), mid))
    db.commit()
    db.close()
    print(f"OK: Article saved for meeting {mid} ({meeting['committee']} {meeting['date']})")
    print(f"  Headline: {data['headline']}")

if __name__ == "__main__":
    main()
