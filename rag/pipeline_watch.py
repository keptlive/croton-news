#!/usr/bin/env python3
"""pipeline_watch.py — daily outcome-level health check for croton.news.

Runs at 9:00 (after the 6:00 croton pipeline and the 8:00 WireClaw
enrichment). Verifies that the things that should have happened actually
happened — independent of job exit codes — and emails one consolidated
alert when anything is broken or needs a manual (phone-relay) action.
Sends a "all healthy" digest on Mondays so silence is never ambiguous.

Checks:
  1. job cadence      — job_runs.db: each wrapped cron job succeeded recently
  2. ingestion        — a transcript has been ingested in the last 14 days
  3. articles         — no transcript >2 days old is missing its article
  4. BOE coverage     — a Board of Education meeting ingested in last 35 days
  5. upcoming agendas — static/upcoming_agendas.json fresh (<26h)
  6. site up          — https://croton.news returns 200 with expected title
  7. backups          — latest rag-*.db backup <26h old and >50MB
  8. disk             — root filesystem <90% used
  9. phone relay      — yt- meetings awaiting transcripts (manual boe-fetch)

Usage: pipeline_watch.py [--dry-run]   (--dry-run prints instead of emailing)
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone


def utcnow():
    """Naive UTC now (DB dates are naive strings)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

BASE = "/opt/croton-news"
RAG_DB = os.path.join(BASE, "rag", "rag.db")
JOBS_DB = os.path.join(BASE, "rag", "job_runs.db")
AGENDAS_JSON = os.path.join(BASE, "static", "upcoming_agendas.json")
BACKUP_DIR = os.path.join(BASE, "backups")
NOTIFY = [os.path.join(BASE, "venv", "bin", "python"),
          os.path.join(BASE, "rag", "notify.py")]

# job name -> (max age hours, manual hint)
EXPECTED_JOBS = {
    "daily-pipeline": (26, "run: cd /opt/croton-news/rag && ./auto_discover.sh"),
    "scrapers": (8, "run: /opt/croton-news/run_scrapers.sh"),
    "db-backup": (26, "run: /opt/croton-news/rag/backup_db.sh"),
    "boarddocs-sync": (26, "run: venv/bin/python rag/boarddocs.py sync"),
    "boe-poll": (26, "run: venv/bin/python rag/poll_boe.py --write"),
    "upcoming-agendas": (6, "run: venv/bin/python rag/auto_pipeline.py"),
}

problems = []      # things that are broken
actions = []       # manual actions the operator must take
notes = []         # informational (included in digest)


def hours_ago(ts_str):
    try:
        dt = datetime.fromisoformat(ts_str)
        return (utcnow() - dt).total_seconds() / 3600
    except (ValueError, TypeError):
        return None


def check_jobs():
    if not os.path.exists(JOBS_DB):
        problems.append("job_runs.db missing — no wrapped job has ever run")
        return
    db = sqlite3.connect(f"file:{JOBS_DB}?mode=ro", uri=True)
    for job, (max_h, hint) in EXPECTED_JOBS.items():
        row = db.execute(
            "SELECT finished_at, exit_code FROM job_runs WHERE job=? AND exit_code=0 "
            "ORDER BY id DESC LIMIT 1", (job,)).fetchone()
        last_any = db.execute(
            "SELECT finished_at, exit_code FROM job_runs WHERE job=? "
            "ORDER BY id DESC LIMIT 1", (job,)).fetchone()
        if not row:
            state = f"never succeeded (last exit: {last_any[1] if last_any else 'never ran'})"
            problems.append(f"job {job}: {state}. Fix: {hint}")
            continue
        age = hours_ago(row[0])
        if age is None or age > max_h:
            problems.append(
                f"job {job}: last success {age:.0f}h ago (max {max_h}h). Fix: {hint}")
        else:
            notes.append(f"job {job}: ok ({age:.1f}h ago)")
    db.close()


def check_content():
    db = sqlite3.connect(f"file:{RAG_DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    # 2. ingestion recency
    row = db.execute(
        "SELECT MAX(date) d FROM meetings WHERE has_transcript=1").fetchone()
    if row["d"]:
        days = (utcnow() - datetime.fromisoformat(row["d"])).days
        if days > 14:
            problems.append(
                f"no transcript ingested in {days} days (last: {row['d']}) — "
                "pipeline may be silently dead")
        else:
            notes.append(f"latest transcript: {row['d']} ({days}d ago)")

    # 3. transcripts missing articles (older than 2 days)
    cutoff = (utcnow() - timedelta(days=2)).strftime("%Y-%m-%d")
    rows = db.execute(
        "SELECT id, date, committee FROM meetings WHERE has_transcript=1 "
        "AND (article IS NULL OR article='') AND date < ? AND date > date('now','-60 day')",
        (cutoff,)).fetchall()
    if rows:
        ids = ", ".join(f"#{r['id']} {r['committee']} {r['date']}" for r in rows[:5])
        problems.append(
            f"{len(rows)} transcript(s) missing articles >2 days: {ids} — "
            "WireClaw enrichment (8:00 on WireClaw box) may be stuck; "
            "check /tmp/enrich-transcripts.log there")

    # 4. BOE recency
    row = db.execute(
        "SELECT MAX(date) d FROM meetings WHERE committee LIKE '%Education%' "
        "AND has_transcript=1").fetchone()
    if row["d"]:
        days = (utcnow() - datetime.fromisoformat(row["d"])).days
        if days > 35:
            problems.append(
                f"no Board of Education meeting ingested in {days} days "
                f"(last: {row['d']}) — check CHUFSD YouTube + BoardDocs")
        else:
            notes.append(f"latest BOE meeting: {row['d']} ({days}d ago)")

    # 10. document-storage gaps (2026-07-13 audit: these all rotted silently)
    # 10a. past meetings with PDF attachments but no stored packet rows
    # champds.com match: only real ChampDS PDF attachments count (Google-Docs
    # agenda links like event 1172 are not fetchable packets)
    n = db.execute(
        "SELECT COUNT(*) FROM meetings m WHERE m.agenda_json LIKE '%champds.com%.pdf%' "
        "AND m.date <= date('now') AND m.date >= date('now','-45 day') "
        "AND m.event_id NOT LIKE 'yt-%' AND m.event_id IS NOT NULL "
        "AND m.event_id NOT IN (SELECT DISTINCT event_id FROM packet_pdfs)").fetchone()[0]
    if n:
        problems.append(
            f"{n} past meeting(s) in last 45d have agenda PDFs but no packet_pdfs rows — "
            "run: venv/bin/python rag/rag_tool.py fetch_agenda_packet EVENT_ID")
    else:
        notes.append("packet PDFs: no coverage gaps (45d)")
    # 10b. chunks missing embeddings (vector search blind spots)
    n = db.execute(
        "SELECT COUNT(*) FROM chunks c LEFT JOIN embeddings e ON e.chunk_id=c.id "
        "WHERE e.chunk_id IS NULL").fetchone()[0]
    if n > 200:
        problems.append(
            f"{n} chunks have no embedding (semantic search blind) — "
            "run: venv/bin/python rag/embeddings.py")
    else:
        notes.append(f"embeddings: {n} chunks unembedded (ok)")
    # 10c. recent BOE meetings with boarddocs_id but no minutes
    n = db.execute(
        "SELECT COUNT(*) FROM meetings WHERE boarddocs_id IS NOT NULL "
        "AND date >= date('now','-90 day') AND date <= date('now','-14 day') "
        "AND (minutes_text IS NULL OR minutes_text = '')").fetchone()[0]
    if n:
        problems.append(
            f"{n} BOE meeting(s) (14-90d old) missing minutes — "
            "run: venv/bin/python rag/boarddocs.py fetch-all && boarddocs.py sync")
    else:
        notes.append("BOE minutes: no recent gaps")

    # 9. yt- meetings awaiting transcripts → phone relay needed
    rows = db.execute(
        "SELECT event_id, date, committee FROM meetings WHERE event_id LIKE 'yt-%' "
        "AND (has_transcript IS NULL OR has_transcript=0) "
        "AND date > date('now','-60 day')").fetchall()
    if rows:
        vids = ", ".join(f"{r['event_id']} ({r['date']})" for r in rows[:5])
        actions.append(
            f"{len(rows)} YouTube meeting(s) need transcripts via phone relay: {vids}. "
            "On phone (Termux): ~/bin/boe-fetch")
    db.close()


def check_article_quality():
    """Run the publish-gate validator over recently published articles.

    The gate blocks new publishes, but this catches anything that slipped
    in via --force, manual edits, or pre-gate publishes."""
    db = sqlite3.connect(f"file:{RAG_DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    recent = db.execute(
        "SELECT id FROM meetings WHERE article IS NOT NULL AND article != '' "
        "AND date >= date('now','-14 day')").fetchall()
    db.close()
    bad = []
    for r in recent:
        try:
            out = subprocess.run(
                [os.path.join(BASE, "venv", "bin", "python"),
                 os.path.join(BASE, "rag", "validate_article.py"),
                 "--published", str(r["id"])],
                capture_output=True, text=True, timeout=120)
            if out.returncode == 2:
                n = out.stdout.count("[")
                bad.append(f"#{r['id']} ({n} violation(s))")
        except Exception:
            pass
    if bad:
        problems.append(
            "published article(s) failing the quality gate: " + ", ".join(bad)
            + " — reports in rag/validation/")
    else:
        notes.append(f"article quality gate: {len(recent)} recent article(s) clean")


def check_boe_pending():
    """New BOE videos on CHUFSD YouTube that aren't in the DB yet.

    RSS works from the VPS; caption/audio download does not (YouTube blocks
    server IPs) — so a new video is by definition a phone-relay action.
    Reuses poll_boe's channel fetch + title patterns.
    """
    import re as _re
    sys.path.insert(0, os.path.join(BASE, "rag"))
    import poll_boe

    chan = poll_boe.get_channel_video_ids()
    if not chan:
        problems.append("could not fetch CHUFSD YouTube RSS (poll_boe discovery blind)")
        return
    db = sqlite3.connect(f"file:{RAG_DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    existing = poll_boe.get_existing_event_ids(db)
    db.close()
    pending = []
    for vid in sorted(chan - existing):
        title = poll_boe.get_video_info(vid)
        if title and any(_re.search(p, title, _re.I) for p in poll_boe.BOE_PATTERNS):
            pending.append(f"{vid} — {title}")
    if pending:
        actions.append(
            "new BOE video(s) on CHUFSD YouTube not yet ingested:\n    "
            + "\n    ".join(pending[:6])
            + "\n    → phone (Termux): ~/bin/boe-fetch ; then VPS: venv/bin/python rag/poll_boe.py --write")
    else:
        notes.append("BOE YouTube: no unprocessed meeting videos")


def check_calendar():
    """static/events.json freshness — an 86-day staleness went unnoticed
    because nothing watched it (2026-07-14 audit)."""
    p = os.path.join(BASE, "static", "events.json")
    try:
        age_d = (time.time() - os.path.getmtime(p)) / 86400
        if age_d > 8:
            problems.append(
                f"events.json (calendar) is {age_d:.0f} days stale — "
                "run: bash scrapers/update-calendar.sh")
        else:
            notes.append(f"calendar events.json: {age_d:.1f}d old")
    except OSError:
        problems.append("static/events.json missing")


def check_packet_completeness():
    """Agenda PDF refs vs stored packet rows — cap-skips leave no zero-row
    trace, so the old existence check missed 77 missing PDFs."""
    db = sqlite3.connect(f"file:{RAG_DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    gaps = []
    for m in db.execute(
            "SELECT id, event_id, agenda_json FROM meetings "
            "WHERE agenda_json LIKE '%champds.com%.pdf%' "
            "AND date >= date('now','-45 day')").fetchall():
        import re as _re
        refs = len(set(_re.findall(r"champds\.com[^\"]+\.pdf", m["agenda_json"] or "")))
        rows = db.execute("SELECT COUNT(*) FROM packet_pdfs WHERE event_id=?",
                          (str(m["event_id"]),)).fetchone()[0]
        if refs > rows:
            gaps.append(f"event {m['event_id']}: {rows}/{refs} PDFs")
    db.close()
    if gaps:
        problems.append(
            "packet PDFs incomplete: " + "; ".join(gaps[:5]) +
            " — run: venv/bin/python rag/rag_tool.py fetch_agenda_packet EVENT --max-pdfs 60 --force")
    else:
        notes.append("packet PDFs: complete vs agenda refs (45d)")


def check_photo_refs():
    """{{photo:}} refs in recent articles must resolve to files on disk."""
    db = sqlite3.connect(f"file:{RAG_DB}?mode=ro", uri=True)
    import re as _re
    photos_dir = os.path.join(BASE, "photos")
    dead = []
    for (mid, art) in db.execute(
            "SELECT id, article FROM meetings WHERE article IS NOT NULL "
            "AND date >= date('now','-30 day')").fetchall():
        for em, ts in _re.findall(r"\{\{photo:([\w\-]+):(\d+)", art or ""):
            base_name = f"{em}_t{ts}"
            if not (os.path.exists(os.path.join(photos_dir, base_name + ".jpg"))
                    or os.path.exists(os.path.join(photos_dir, base_name + "_enhanced.jpg"))):
                dead.append(f"#{mid}:{base_name}")
    db.close()
    if dead:
        problems.append("dead {{photo}} refs in recent articles: " + ", ".join(dead[:6]))
    else:
        notes.append("photo refs: all resolve (30d)")


def check_agendas():
    try:
        age_h = (time.time() - os.path.getmtime(AGENDAS_JSON)) / 3600
        if age_h > 26:
            problems.append(
                f"upcoming_agendas.json is {age_h:.0f}h stale — hourly "
                "auto_pipeline job not refreshing it")
        else:
            notes.append(f"upcoming_agendas.json: {age_h:.1f}h old")
    except OSError:
        problems.append("upcoming_agendas.json missing")


def check_site():
    try:
        out = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "-m", "20", "https://croton.news/"],
            capture_output=True, text=True, timeout=30).stdout.strip()
        if out != "200":
            problems.append(f"https://croton.news returned {out} (expected 200)")
        else:
            notes.append("site: 200 OK")
    except Exception as e:  # noqa: BLE001
        problems.append(f"site check failed: {e}")


def check_backups():
    try:
        backups = sorted(
            (f for f in os.listdir(BACKUP_DIR) if f.startswith("rag-") and f.endswith(".db")),
            key=lambda f: os.path.getmtime(os.path.join(BACKUP_DIR, f)))
        if not backups:
            problems.append("no backups in backups/")
            return
        newest = os.path.join(BACKUP_DIR, backups[-1])
        age_h = (time.time() - os.path.getmtime(newest)) / 3600
        size_mb = os.path.getsize(newest) / 1e6
        if age_h > 26:
            problems.append(f"newest backup {backups[-1]} is {age_h:.0f}h old")
        elif size_mb < 50:
            problems.append(f"newest backup {backups[-1]} is only {size_mb:.0f}MB — suspicious")
        else:
            notes.append(f"backup: {backups[-1]} ({size_mb:.0f}MB, {age_h:.1f}h)")
    except OSError as e:
        problems.append(f"backup check failed: {e}")


def check_disk():
    st = os.statvfs("/")
    used_pct = 100 * (1 - st.f_bavail / st.f_blocks)
    if used_pct > 90:
        problems.append(f"disk {used_pct:.0f}% full")
    else:
        notes.append(f"disk: {used_pct:.0f}% used")


def main():
    dry = "--dry-run" in sys.argv
    for fn in (check_jobs, check_content, check_boe_pending, check_agendas,
               check_calendar, check_packet_completeness, check_photo_refs,
               check_site, check_backups, check_disk, check_article_quality):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 — one broken check must not kill the watcher
            problems.append(f"watchdog check {fn.__name__} crashed: {e}")

    is_monday = utcnow().weekday() == 0
    body_parts = []
    if problems:
        body_parts.append("── PROBLEMS ──\n" + "\n".join(f"• {p}" for p in problems))
    if actions:
        body_parts.append("── MANUAL ACTIONS NEEDED ──\n" + "\n".join(f"• {a}" for a in actions))
    body_parts.append("── healthy ──\n" + "\n".join(f"• {n}" for n in notes))
    body = "\n\n".join(body_parts) + f"\n\ngenerated {utcnow():%Y-%m-%d %H:%M} UTC by pipeline_watch.py"

    if problems or actions:
        subject = f"pipeline: {len(problems)} problem(s), {len(actions)} action(s) needed"
    elif is_monday:
        subject = "weekly digest: pipeline healthy"
    else:
        print("all healthy, no email")
        print(body)
        return 0

    if dry:
        print(subject)
        print(body)
        return 0
    return subprocess.run(NOTIFY + [subject, body]).returncode


if __name__ == "__main__":
    sys.exit(main())
