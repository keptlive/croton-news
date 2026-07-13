#!/usr/bin/env python3
"""Publish a WireClaw-generated article to the meetings database.

Usage: python3 publish_article.py <json_path> <meeting_id> <article_model>
"""
import json, sqlite3, sys
from datetime import datetime, timezone

def publish(json_path, meeting_id, article_model):
    with open(json_path) as f:
        d = json.load(f)

    headline = d.get("headline", "").strip()
    article = d.get("article", "").strip()
    quick_summary = d.get("quick_summary", "").strip()
    key_actions = d.get("key_actions", "").strip()

    if not headline:
        print(f"ERROR: No headline in {json_path}", file=sys.stderr)
        sys.exit(1)
    if not article:
        print(f"ERROR: No article in {json_path}", file=sys.stderr)
        sys.exit(1)

    # Validate: no raw None strings
    for field_name, val in [("headline", headline), ("article", article),
                             ("quick_summary", quick_summary)]:
        if val == "None" or val == "null":
            print(f"ERROR: {field_name} is literal None — refusing to publish", file=sys.stderr)
            sys.exit(1)

    # Validate: no unresolved template tags without event_id
    db = sqlite3.connect("/opt/croton-news/rag/rag.db")
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT event_id FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    if row and not row["event_id"]:
        import re
        orphan_tags = re.findall(r{{quote:d+}}, article)
        if orphan_tags:
            print(f"WARNING: {len(orphan_tags)} {{{{quote}}}} tags but no event_id — stripping", file=sys.stderr)
            article = re.sub(r{{quote:d+}}, , article)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    db.execute("""UPDATE meetings SET
        headline = ?,
        quick_summary = CASE WHEN ? !=  THEN ? ELSE quick_summary END,
        complete_summary = CASE WHEN ? !=  THEN ? ELSE complete_summary END,
        article = ?,
        article_model = ?,
        article_generated_at = ?
        WHERE id = ?""",
        (headline,
         quick_summary, quick_summary,
         key_actions, key_actions,
         article, article_model, now, meeting_id))
    db.commit()
    db.close()

    print(f"Published: {headline[:60]} (model: {article_model})")

if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(f"Usage: {sys.argv[0]} <json_path> <meeting_id> <article_model>", file=sys.stderr)
        sys.exit(1)
    publish(sys.argv[1], int(sys.argv[2]), sys.argv[3])
