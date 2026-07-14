#!/usr/bin/env python3
"""Publish a WireClaw-generated article to the meetings database.

Usage: python3 publish_article.py <json_path> <meeting_id> <article_model> [--force]

Runs the deterministic publish gate (validate_article.py) first — quote
attribution/verbatim checks, name and dollar-figure provenance. On
violations: nothing is published, the report is saved to
rag/validation/article-<id>-report.json, and exit code is 3 so the caller
can retry the writer with the report as feedback. --force bypasses the
gate (manual use only).
"""
import json, os, sqlite3, sys
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)


def publish(json_path, meeting_id, article_model, force=False):
    with open(json_path) as f:
        d = json.loads(f.read(), strict=False)

    # ── publish gate ──────────────────────────────────────────────
    if not force:
        from validate_article import validate, REPORT_DIR
        vdb = sqlite3.connect(f"file:{os.path.join(BASE_DIR, 'rag.db')}?mode=ro", uri=True)
        vdb.row_factory = sqlite3.Row
        violations = validate(d, meeting_id, vdb)
        vdb.close()
        os.makedirs(REPORT_DIR, exist_ok=True)
        report_path = os.path.join(REPORT_DIR, f"article-{meeting_id}-report.json")
        with open(report_path, "w") as rf:
            json.dump({"meeting_id": meeting_id, "passed": not violations,
                       "violations": violations}, rf, indent=2, ensure_ascii=False)
        if violations:
            print(f"GATE BLOCKED: {len(violations)} violation(s) — not publishing. "
                  f"Report: {report_path}", file=sys.stderr)
            for v in violations:
                print(f"  [{v['type']}] {v['detail']}", file=sys.stderr)
            sys.exit(3)

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
        orphan_tags = re.findall(r"\{\{quote:\d+\}\}", article)
        if orphan_tags:
            print(f"WARNING: {len(orphan_tags)} {{{{quote}}}} tags but no event_id — stripping", file=sys.stderr)
            article = re.sub(r"\{\{quote:\d+\}\}", "", article)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    db.execute("""UPDATE meetings SET
        headline = ?,
        quick_summary = CASE WHEN ? != '' THEN ? ELSE quick_summary END,
        complete_summary = CASE WHEN ? != '' THEN ? ELSE complete_summary END,
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
    args = [a for a in sys.argv[1:] if a != "--force"]
    force = "--force" in sys.argv
    if len(args) != 3:
        print(f"Usage: {sys.argv[0]} <json_path> <meeting_id> <article_model> [--force]", file=sys.stderr)
        sys.exit(1)
    publish(args[0], int(args[1]), args[2], force=force)
