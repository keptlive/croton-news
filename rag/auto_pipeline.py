#!/usr/bin/env python3
"""
auto_pipeline.py — hourly meeting auto-processor for croton.news.

For each ChampDS event in a sliding window (recent past meetings):
  * If video is published and not yet ingested → pipeline.py full + write_article.py
  * If a placeholder article already exists and video has now appeared → upgrade
  * If past meeting + no video yet + agenda items present → write placeholder
    article (marked "PRELIMINARY — will be enriched")

Runs hourly via cron. Logs to /var/log/auto_pipeline.log.
"""

import json
import os
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta

BASE_DIR = "/opt/croton-news/rag"
RAG_DB = os.path.join(BASE_DIR, "rag.db")
LOG = "/var/log/auto_pipeline.log"
PIPELINE = os.path.join(BASE_DIR, "pipeline.py")
WRITE_ARTICLE = os.path.join(BASE_DIR, "write_article.py")
GENERATE_SUMMARIES = os.path.join(BASE_DIR, "generate_summaries.py")
UPCOMING_CACHE = "/opt/croton-news/static/upcoming_agendas.json"

WINDOW_DAYS_BACK = 21
WINDOW_DAYS_FORWARD = 1
UPCOMING_DAYS_FORWARD = 60


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def fetch(eid):
    url = f"https://playapi.champds.com/crotononhudsonny/event/{eid}"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return json.loads(r.read())
    except Exception:
        return None


def get_max_event_id(db):
    row = db.execute(
        "SELECT MAX(CAST(event_id AS INTEGER)) FROM meetings WHERE event_id GLOB '[0-9]*'"
    ).fetchone()
    return (row[0] or 1100)


def scan_range(db, scan_back=80, scan_forward=20):
    max_known = get_max_event_id(db)
    start = max(1080, max_known - scan_back)
    end = max_known + scan_forward
    log(f"scanning events {start}..{end} (max known: {max_known})")
    return range(start, end + 1)


def get_meeting_row(db, eid):
    return db.execute(
        "SELECT id, event_id, date, committee, article_model "
        "FROM meetings WHERE event_id = ?",
        (eid,),
    ).fetchone()


def in_window(date_str):
    if not date_str:
        return False
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
    except ValueError:
        return False
    today = datetime.now().date()
    return (today - timedelta(days=WINDOW_DAYS_BACK)) <= d <= (today + timedelta(days=WINDOW_DAYS_FORWARD))


def is_past(date_str):
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
    except ValueError:
        return False
    return d < datetime.now().date()


def run_pipeline_full(eid):
    log(f"  -> pipeline.py full {eid}")
    r = subprocess.run(
        ["python3", PIPELINE, "full", eid],
        cwd=BASE_DIR, capture_output=True, text=True, timeout=1800,
    )
    if r.returncode != 0:
        log(f"     ERR pipeline rc={r.returncode}: {r.stderr[-300:]}")
        return False
    return True


def run_write_article(eid):
    log(f"  -> write_article.py {eid} (opus)")
    r = subprocess.run(
        ["python3", WRITE_ARTICLE, eid, "--model", "claude-opus-4-5"],
        cwd=BASE_DIR, capture_output=True, text=True, timeout=900,
    )
    if r.returncode != 0:
        log(f"     ERR write_article rc={r.returncode}: {r.stderr[-300:]}")
        return False
    return True


def write_placeholder(db, data):
    ev = data.get("Event", {}) or {}
    eid = str(ev.get("CustomerEventID", ""))
    title = (ev.get("EventTitle") or "").strip()
    date = (ev.get("EventDateTimeCustomerLocal") or "")[:10]
    if not eid or not title or not date:
        return False

    agenda = data.get("Agenda", {}) or {}
    items = agenda.get("AgendaItems", []) or []
    if not items:
        return False

    # Build agenda outline
    bullet_lines = []
    actions_lines = []
    for it in items:
        t = (it.get("Title") or "").strip()
        if not t:
            continue
        bullet_lines.append(f"- **{t}**")
        actions_lines.append(f"- {t}")
        attachments = it.get("Attachments") or []
        for at in attachments[:5]:
            nick = (at.get("MediaNickName") or "").strip()
            if nick:
                bullet_lines.append(f"    - Attachment: {nick}")

    if not bullet_lines:
        return False

    pretty_committee = title
    headline = f"Preliminary: {title} — agenda for {date}"
    quick_summary = (
        f"Preliminary agenda-only summary for the {title} on {date}. "
        f"This article will be enriched with full coverage when ChampDS publishes the meeting video."
    )
    article = (
        f"_Editor's note: This is a **preliminary** article generated from the published agenda only. "
        f"It will be replaced with full coverage — including direct quotes, discussion, and any "
        f"votes taken — once the meeting video is released by ChampDS._\n\n"
        f"The {title} met on {date}. The published agenda included the following items:\n\n"
        + "\n".join(bullet_lines)
        + "\n\nFull coverage forthcoming."
    )
    complete_summary = "\n".join(actions_lines)

    db.execute(
        """
        INSERT OR REPLACE INTO meetings
            (date, committee, event_id, headline, quick_summary, complete_summary, article,
             has_transcript, has_video, has_audio,
             article_model, article_generated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 'placeholder-agenda', datetime('now'))
    """,
        (date, pretty_committee, eid, headline, quick_summary, complete_summary, article),
    )
    db.commit()
    log(f"  + placeholder for {eid} ({date} {title})")
    return True


def agenda_signature(data):
    """Compact hash of the agenda so we regenerate only when it actually changes.
    Covers agenda item titles + attachment hashed-filenames recursively."""
    import hashlib
    parts = []

    def walk(items):
        for it in items or []:
            parts.append((it.get("Title") or "").strip())
            for at in (it.get("Attachments") or []):
                parts.append(at.get("MediaFileName") or "")
                parts.append(str(at.get("SizeBytes") or 0))
            walk(it.get("Children") or [])

    ag = data.get("Agenda", {}) or {}
    walk(ag.get("AgendaItems") or [])
    for at in (ag.get("Attachments") or []):
        parts.append(at.get("MediaFileName") or "")

    blob = "\n".join(parts).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()


def dispatch_packet_writer(eid, reason="preview"):
    """Dispatch a one-shot croton-packet-writer task by invoking the
    dispatch_packet.py helper on wireclaw over SSH."""
    try:
        r = subprocess.run(
            ["ssh", "wireclaw", "python3", "/root/dispatch_packet.py", str(eid)],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            log(f"  ! dispatch {eid} failed: {r.stderr[:200]}")
            return False
        log(f"  → dispatched packet-writer for {eid} ({reason}): {r.stdout.strip()}")
        return True
    except Exception as e:
        log(f"  ! dispatch {eid} exception: {e}")
        return False


def _get_meta(db, key, default=None):
    row = db.execute("SELECT value FROM packet_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def _set_meta(db, key, value):
    db.execute(
        "INSERT OR REPLACE INTO packet_meta (key, value) VALUES (?, ?)",
        (key, value),
    )


def should_generate_upcoming_preview(db, eid, sig, has_agenda, has_packet_pdfs):
    """Decide whether to dispatch a packet-writer task for this upcoming event.

    Rules:
      - skip if the meeting has no agenda items or no PDFs
      - skip if an article already exists AND the agenda signature is unchanged
      - otherwise dispatch
    """
    if not has_agenda or not has_packet_pdfs:
        return False, "no agenda or no PDFs"

    existing = get_meeting_row(db, eid)
    last_sig_key = f"agenda_sig_{eid}"
    last_sig = _get_meta(db, last_sig_key)

    if existing and existing["article_model"] == "glm-5-turbo-packet" and last_sig == sig:
        return False, "cached (signature unchanged)"

    return True, "new" if not existing else "signature changed"


def remove_placeholder(db, eid):
    db.execute("DELETE FROM meetings WHERE event_id = ? AND article_model = 'placeholder-agenda'", (eid,))
    db.commit()


def is_upcoming(date_str):
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
    except ValueError:
        return False
    today = datetime.now().date()
    return today <= d <= (today + timedelta(days=UPCOMING_DAYS_FORWARD))


def collect_agenda(data):
    """Pull a clean list of agenda items + attachments for cache JSON.
    Walks children recursively to capture sub-items and their PDFs."""
    agenda = data.get("Agenda", {}) or {}
    items = agenda.get("AgendaItems") or []
    out = []

    def _collect_atts(item_or_child):
        attachments = []
        for at in (item_or_child.get("Attachments") or [])[:8]:
            nick = (at.get("MediaNickName") or "").strip()
            mfile = at.get("MediaFileName") or ""
            if not nick:
                continue
            href = ""
            if mfile.startswith("http"):
                href = mfile
            attachments.append({"name": nick, "href": href})
        return attachments

    for it in items:
        title = (it.get("Title") or "").strip()
        if not title:
            continue
        attachments = _collect_atts(it)
        children = it.get("Children") or []
        sub_items = []
        for child in children:
            ctitle = (child.get("Title") or "").strip()
            catts = _collect_atts(child)
            if ctitle:
                sub_items.append({"title": ctitle, "attachments": catts})
            attachments.extend(catts)
        entry = {"title": title, "attachments": attachments}
        if sub_items:
            entry["sub_items"] = sub_items
        out.append(entry)
    return out


def write_upcoming_cache(upcoming):
    upcoming.sort(key=lambda x: (x["date"], x.get("time") or ""))
    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "events": upcoming,
    }
    tmp = UPCOMING_CACHE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, UPCOMING_CACHE)
    log(f"wrote {len(upcoming)} upcoming events to {UPCOMING_CACHE}")


def process():
    db = sqlite3.connect(RAG_DB)
    db.row_factory = sqlite3.Row
    actions = {"new_full": 0, "upgraded": 0, "placeholder": 0, "skipped": 0, "upcoming": 0}
    upcoming = []

    try:
        for eid_int in scan_range(db):
            eid = str(eid_int)
            data = fetch(eid)
            if not data:
                continue
            ev = data.get("Event", {}) or {}
            if not ev:
                continue

            date = (ev.get("EventDateTimeCustomerLocal") or "")[:10]
            event_title = ev.get("EventTitle") or ""

            if is_upcoming(date):
                agenda_items = collect_agenda(data)
                # Count PDFs: check both ingested packet_pdfs AND ChampDS API attachments
                pdf_count = db.execute(
                    "SELECT COUNT(*) FROM packet_pdfs WHERE event_id=? AND kind IN ('pdf','pdf_ocr') AND char_count > 100",
                    (eid,),
                ).fetchone()[0]
                # Also count attachments from ChampDS API (children have the PDFs)
                api_pdf_count = 0
                for it in (data.get("Agenda", {}) or {}).get("AgendaItems", []):
                    api_pdf_count += len(it.get("Attachments") or [])
                    for child in (it.get("Children") or []):
                        api_pdf_count += len(child.get("Attachments") or [])
                pdf_count = max(pdf_count, api_pdf_count)

                upcoming_meeting_id = None
                existing = get_meeting_row(db, eid)
                if existing:
                    upcoming_meeting_id = existing["id"]

                upcoming.append({
                    "event_id": eid,
                    "meeting_id": upcoming_meeting_id,
                    "date": date,
                    "time": (ev.get("EventDateTimeCustomerLocal") or "")[11:16],
                    "title": event_title,
                    "agenda_items": agenda_items,
                    "agenda_published": bool(agenda_items),
                    "pdf_count": pdf_count,
                    "champds_url": f"https://play.champds.com/crotononhudsonny/event/{eid}",
                })
                actions["upcoming"] += 1

                # Dispatch packet-writer task when this meeting has a real
                # agenda packet and either (a) no preview article exists yet
                # or (b) the agenda signature has changed since last write.
                sig = agenda_signature(data)
                should_gen, reason = should_generate_upcoming_preview(
                    db, eid, sig, bool(agenda_items), pdf_count > 0
                )
                if should_gen:
                    if dispatch_packet_writer(eid, reason=reason):
                        _set_meta(db, f"agenda_sig_{eid}", sig)
                        db.commit()
                        actions.setdefault("previews_dispatched", 0)
                        actions["previews_dispatched"] += 1
                continue

            if not in_window(date):
                actions["skipped"] += 1
                continue

            media = data.get("MediaInfo", {}) or {}
            media_path = media.get("MediaPath") or ""
            has_video = bool(media_path)

            existing = get_meeting_row(db, eid)
            is_placeholder = bool(existing) and existing["article_model"] == "placeholder-agenda"

            if has_video and (not existing or is_placeholder):
                log(f"event {eid} ({date} {event_title[:50]}) has video; ingesting")
                if is_placeholder:
                    remove_placeholder(db, eid)
                if run_pipeline_full(eid):
                    if run_write_article(eid):
                        if is_placeholder:
                            actions["upgraded"] += 1
                        else:
                            actions["new_full"] += 1
            elif (not has_video) and (not existing) and is_past(date):
                if write_placeholder(db, data):
                    actions["placeholder"] += 1

        write_upcoming_cache(upcoming)
    finally:
        db.close()

    # Backfill any missing quick_summary fields
    try:
        subprocess.run(
            ["python3", GENERATE_SUMMARIES],
            cwd=BASE_DIR, capture_output=True, text=True, timeout=120,
            env={**os.environ},
        )
    except Exception as e:
        log(f"  summary backfill error: {e}")

    log(f"actions: {actions}")


if __name__ == "__main__":
    process()
