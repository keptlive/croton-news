#!/usr/bin/env python3
"""
croton.news — Meeting Video Pipeline

End-to-end automation: discover new meetings → download video → transcribe → chunk → embed

Usage:
    python3 pipeline.py discover          # Discover & register new ChampDS meetings (with agendas)
    python3 pipeline.py process-new       # Full auto: discover → download → transcribe → enrich → ingest → write
    python3 pipeline.py download EVENT_ID # Download video for a single event
    python3 pipeline.py download-all      # Download all missing videos
    python3 pipeline.py transcribe EVENT_ID  # Transcribe video with Deepgram Nova 3
    python3 pipeline.py transcribe-all    # Transcribe all videos missing transcripts
    python3 pipeline.py enrich EVENT_ID   # Enrich transcript (proper nouns + speaker names)
    python3 pipeline.py enrich --all      # Enrich all transcripts
    python3 pipeline.py enrich --fix-names # Re-run speaker name correction only
    python3 pipeline.py ingest EVENT_ID   # Chunk and embed a transcript into rag.db
    python3 pipeline.py full EVENT_ID     # Download + transcribe + enrich + ingest
    python3 pipeline.py status            # Show pipeline status for all meetings
    python3 pipeline.py refresh-agendas   # Re-check recent meetings for agenda/video updates
    python3 pipeline.py match-orphans     # Link calendar-only meetings to ChampDS events
    python3 pipeline.py extract-minutes   # Extract minutes text from agenda approval PDFs

Requires:
    - ffmpeg (for video download)
    - DEEPGRAM_API_KEY environment variable (for transcription)
    - GEMINI_API_KEY environment variable (for embeddings)

ChampDS API:
    Portal: https://play.champds.com/crotononhudsonny/event/{event_id}
    API:    https://playapi.champds.com/crotononhudsonny/event/{event_id}
    HLS:    https://securestream7.champds.com/CrotonOnHudsonNYOD/_definst_{media_path}/playlist.m3u8?PLAY
"""

import json
import os
import sqlite3
import subprocess
import sys
import urllib.request
import urllib.parse
import urllib.error
import time

# Load .env file if present
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RAG_DB = os.path.join(BASE_DIR, "rag.db")
TRANSCRIPTS_DIR = os.path.join(BASE_DIR, "transcripts")
VIDEOS_DIR = os.path.join(os.path.dirname(BASE_DIR), "videos")
# On VPS, videos are at /opt/croton-news/videos
if not os.path.isdir(VIDEOS_DIR):
    VIDEOS_DIR = os.path.join(os.path.dirname(BASE_DIR), "site", "videos")
if not os.path.isdir(VIDEOS_DIR):
    VIDEOS_DIR = "/opt/croton-news/videos"

CHAMPDS_API = "https://playapi.champds.com/crotononhudsonny/event"
CHAMPDS_STREAM = "https://securestream7.champds.com/CrotonOnHudsonNYOD/_definst_"
DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"

# Board IDs that record video on ChampDS
VIDEO_BOARDS = {1: "Board of Trustees", 2: "Planning Board", 3: "Zoning Board of Appeals"}

COMMITTEE_MAP = {
    "Board of Trustees": "Board Of Trustees",
    "Board of Trustees Work Session": "Board of Trustees Work Session",
    "Board of Trustees Meeting": "Board Of Trustees",
    "Board of Trustees Organizational Meeting": "Board Of Trustees",
    "Planning Board": "Planning Board",
    "Planning Board Meeting": "Planning Board",
    "Zoning Board of Appeals": "Zoning Board of Appeals",
    "Zoning Board of Appeals Meeting": "Zoning Board of Appeals",
    "Water Control Commission Meeting": "Water Control Commission",
    "Waterfront Advisory Committee": "Waterfront Advisory Committee",
    "Waterfront Advisory Committee Meeting (WAC)": "Waterfront Advisory Committee",
    "Conservation Advisory Council": "Conservation Advisory Council",
    "Advisory Board on the Visual Environment": "Advisory Board on the Visual Environment (VEB)",
    "Advisory Board on the Visual Environment (VEB)": "Advisory Board on the Visual Environment (VEB)",
    "Board of Education": "Board of Education",
    "CHUFSD Board of Education": "Board of Education",
    "Water Advisory Committee": "Water Advisory Committee",
    "Water Control Commission": "Water Control Commission",
    "Preview": "Preview",
    "Other": "Other",
}

# Max consecutive empty events before stopping dynamic scan
MAX_CONSECUTIVE_EMPTY = 20


def get_db():
    db = sqlite3.connect(RAG_DB)
    db.row_factory = sqlite3.Row
    return db


# ── Discovery ──────────────────────────────────────────────────────

def get_champds_event(event_id):
    """Fetch event metadata from ChampDS API."""
    url = f"{CHAMPDS_API}/{event_id}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read())
    except (urllib.error.HTTPError, urllib.error.URLError):
        return None


def extract_agenda(data):
    """Extract nested agenda items from ChampDS API response into simplified JSON.

    Returns list of dicts: [{title, description, children: [...], attachments: [{name, url}]}]
    or None if no agenda data.
    """
    agenda_data = data.get("Agenda", {})
    items = agenda_data.get("AgendaItems", [])
    if not items:
        return None

    CHAMPDS_ATT_BASE = "https://play.champds.com/ATT/crotononhudsonny"

    def walk(item_list):
        result = []
        for item in item_list:
            title = (item.get("Title") or "").strip()
            if not title:
                continue
            desc = (item.get("Description") or "").strip()
            # Extract attachments with download URLs
            attachments = []
            for att in item.get("Attachments", []):
                nick = (att.get("MediaNickName") or "").strip()
                mfile = att.get("MediaFileName") or ""
                mloc = att.get("MediaFileLocation") or ""
                mtype = att.get("MediaTypeID")
                if not nick:
                    continue
                if mtype == 2 or mfile.startswith("http"):
                    attachments.append({"name": nick, "url": mfile})
                elif mfile and mloc:
                    attachments.append({"name": nick, "url": f"{CHAMPDS_ATT_BASE}/{mloc}/{mfile}"})
            children = walk(item.get("Children", []))
            result.append({
                "title": title,
                "description": desc,
                "children": children,
                "attachments": attachments,
            })
        return result

    return walk(items)


def summarize_agenda(agenda_items):
    """Generate a human-readable quick_summary from agenda items for the Coming Up section."""
    if not agenda_items:
        return None

    import re as _re

    # Skip procedural and boilerplate items
    skip_terms = {
        "call to order", "adjournment", "pledge of allegiance", "roll call",
        "approval of vouchers", "consent agenda", "correspondence to the board",
        "approval of minutes", "public comment", "reports from the mayor",
        "report from the village manager", "new business", "old business",
        "executive session", "responses to questions",
    }

    def is_procedural(title):
        tl = title.lower().strip()
        return any(skip in tl for skip in skip_terms) or tl.endswith("p.m.") or tl.endswith("pm")

    def clean_title(title):
        """Extract the human-readable essence from a legal agenda item title."""
        t = title.strip()
        # Strip tax map references: "Section XX.XX Block X Lot X.X"
        t = _re.sub(r'\s*[-—]\s*Located in a .*$', '', t, flags=_re.IGNORECASE)
        t = _re.sub(r'\s*designated on the Tax Maps.*$', '', t, flags=_re.IGNORECASE)
        t = _re.sub(r'\s*Section \d+\.\d+ Block \d+.*$', '', t, flags=_re.IGNORECASE)
        # Strip "Consider authorizing the Village Manager to" → just the action
        t = _re.sub(r'^Consider (authorizing the Village Manager to |determining that |adopting |scheduling )',
                     '', t, flags=_re.IGNORECASE)
        # Strip "Request for a ... variance from Village Zoning Code" → simplify
        t = _re.sub(r'Request for (?:a |an )?(.+?) (?:variance|variances) from Village Zoning Code.*',
                     r'seeking \1 variance', t, flags=_re.IGNORECASE)
        # Clean up owner/applicant format: "Name, owner---Address" → "Address (Name)"
        m = _re.match(r'^(.+?),\s*(?:owners?|applicants?)\s*[-—]+\s*(.+?)(?:\s*[-—]|$)', t, _re.IGNORECASE)
        if m:
            t = f"{m.group(2).strip()} ({m.group(1).strip()})"
        # Strip architect/representative prefix
        t = _re.sub(r'^[\w\s]+,\s*(?:architect|representative)\s*(?:for\s+)?', '', t, flags=_re.IGNORECASE)
        # Extract dollar amounts for prominence
        dollar = _re.search(r'\$[\d,]+(?:\.\d{2})?', t)
        # Extract address for prominence
        addr = _re.search(r'\d+\s+(?:Croton Point|South Riverside|[A-Z]\w+)\s+(?:Ave|Road|Street|Drive|Way|Lane|Blvd)\w*', t, _re.IGNORECASE)
        # Cap length
        if len(t) > 120:
            t = t[:117] + "..."
        return t.strip()

    # Collect substantive items with cleaned titles, scored by importance
    scored_items = []
    def collect(items, depth=0):
        for item in items:
            if not isinstance(item, dict):
                continue
            title = item.get("title", "")
            if not title or is_procedural(title):
                children = item.get("children", [])
                if children:
                    collect(children, depth + 1)
                continue
            cleaned = clean_title(title)
            if cleaned and len(cleaned) > 5:
                # Score by importance: dollar amounts, policy items, hearings rank higher
                score = 0
                tl = title.lower()
                if "$" in title:
                    score += 3
                if any(w in tl for w in ["hearing", "public hearing", "seqra", "seqr"]):
                    score += 3
                if any(w in tl for w in ["resolution", "local law", "home rule", "ordinance"]):
                    score += 2
                if any(w in tl for w in ["authorize", "accept", "adopt", "approve"]):
                    score += 1
                if any(w in tl for w in ["variance", "permit", "site plan", "subdivision"]):
                    score += 2
                if any(w in tl for w in ["correspondence", "letter", "email", "membership"]):
                    score -= 1  # Lower priority for correspondence
                if any(w in tl for w in ["resignation", "voucher"]):
                    score -= 1
                scored_items.append((score, cleaned))
            # Also check children for substantive items
            collect(item.get("children", []), depth + 1)
    collect(agenda_items)

    # Sort by score descending, take top items
    scored_items.sort(key=lambda x: -x[0])
    substantive = [item for _, item in scored_items]

    if not substantive:
        total = len(agenda_items)
        return f"{total} agenda items scheduled."

    # Extract key facts from the full agenda: dollar amounts, addresses, project names
    all_text = " ".join(item.get("title", "") for item in _flatten_agenda(agenda_items))
    dollars = _re.findall(r'\$[\d,]+(?:\.\d{2})?', all_text)
    # Find resolution names from attachment titles
    att_names = []
    def get_atts(items):
        for item in items:
            if not isinstance(item, dict):
                continue
            for att in item.get("attachments", []):
                name = att.get("name", "")
                # Extract meaningful resolution/project names
                m = _re.match(r'Resolution \d+-\d+ _(.+?)_\.pdf', name)
                if m:
                    att_names.append(m.group(1).replace("_", " "))
            get_atts(item.get("children", []))
    get_atts(agenda_items)

    # Build a readable summary from top highlights
    highlights = substantive[:3]
    if len(highlights) == 1:
        summary = highlights[0]
    elif len(highlights) == 2:
        summary = f"{highlights[0]}; {highlights[1]}"
    else:
        summary = f"{highlights[0]}; {highlights[1]}; {highlights[2]}"

    # Add count if there are more items
    remaining = len(substantive) - len(highlights)
    if remaining > 0:
        summary += f" — plus {remaining} more items"

    # Cap length
    if len(summary) > 300:
        summary = summary[:297] + "..."
    return summary


def _flatten_agenda(items):
    """Flatten nested agenda items into a single list."""
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        result.append(item)
        result.extend(_flatten_agenda(item.get("children", [])))
    return result


def discover_new_meetings(start_id=None, end_id=None):
    """Scan ChampDS event IDs, register new meetings in DB with agenda data.

    If start_id is None, begins from the highest known event_id in the DB.
    If end_id is None, scans dynamically until MAX_CONSECUTIVE_EMPTY empty events.
    Returns list of newly created meeting IDs.
    """
    db = get_db()
    known = {row["event_id"] for row in
             db.execute("SELECT event_id FROM meetings WHERE event_id IS NOT NULL").fetchall()}

    # Dynamic start: highest known event_id or default 1080
    if start_id is None:
        row = db.execute("""
            SELECT MAX(CAST(event_id AS INTEGER)) as max_eid
            FROM meetings WHERE event_id IS NOT NULL AND event_id NOT LIKE 'yt-%'
        """).fetchone()
        start_id = (row["max_eid"] or 1079) + 1

    dynamic_end = end_id is None
    if end_id is None:
        end_id = start_id + 500  # safety cap

    new_meeting_ids = []
    consecutive_empty = 0

    print(f"  Scanning from event {start_id}...")
    for eid in range(start_id, end_id + 1):
        eid_str = str(eid)
        if eid_str in known:
            consecutive_empty = 0  # known event resets counter
            continue

        data = get_champds_event(eid)
        time.sleep(0.3)  # rate limiting

        if not data or "Event" not in data:
            consecutive_empty += 1
            if dynamic_end and consecutive_empty >= MAX_CONSECUTIVE_EMPTY:
                print(f"  Stopping scan: {MAX_CONSECUTIVE_EMPTY} consecutive empty events after {eid}")
                break
            continue

        ev = data["Event"]
        title = ev.get("EventTitle", ev.get("Title", ""))
        if not title:
            consecutive_empty += 1
            if dynamic_end and consecutive_empty >= MAX_CONSECUTIVE_EMPTY:
                break
            continue

        consecutive_empty = 0  # valid event resets counter

        media = data.get("MediaInfo", {})
        media_type = media.get("MediaType", "")
        media_path = media.get("MediaPath", "") if media_type != "DISABLED" else ""
        date_raw = ev.get("EventDateTimeCustomerLocal",
                          ev.get("EventDateTimeUTC",
                                 ev.get("MeetingDate", "")))
        date = date_raw[:10] if date_raw else ""
        board = data.get("Board", {})
        raw_board_name = board.get("BoardName", "")

        # Map to canonical committee name, use title if it gives more detail
        committee = COMMITTEE_MAP.get(title, COMMITTEE_MAP.get(raw_board_name, title or raw_board_name))
        has_video = 1 if media_path else 0

        # Extract agenda
        agenda = extract_agenda(data)
        agenda_json_str = json.dumps(agenda) if agenda else None
        quick_summary = summarize_agenda(agenda) if agenda else None

        # Insert meeting into DB
        try:
            db.execute("""
                INSERT OR IGNORE INTO meetings
                (date, committee, event_id, has_video, agenda_json, media_path, board_name, quick_summary)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (date, committee, eid_str, has_video, agenda_json_str, media_path, raw_board_name, quick_summary))
            db.commit()

            # Check if it was actually inserted (not a duplicate)
            inserted = db.execute(
                "SELECT id FROM meetings WHERE event_id = ?", (eid_str,)
            ).fetchone()
            if inserted:
                new_meeting_ids.append(inserted["id"])
                n_agenda = len(agenda) if agenda else 0
                status = "VIDEO" if has_video else "agenda" if agenda else "registered"
                print(f"  {eid}: {title} ({date}) [{status}] {n_agenda} agenda items")
        except sqlite3.IntegrityError:
            pass  # UNIQUE constraint - meeting already exists for this date+committee

    db.close()
    return new_meeting_ids


def recheck_pending():
    """Re-check recent meetings for newly available video, agendas, and minutes.

    Checks ALL recent meetings with event_ids (not just those without video).
    Updates: video availability, agenda content, minutes attachments.
    """
    db = get_db()
    # Check all recent meetings with event_ids — not just those without video
    pending = db.execute("""
        SELECT id, event_id, has_video, agenda_json, has_minutes FROM meetings
        WHERE event_id IS NOT NULL AND event_id NOT LIKE 'yt-%'
        AND date >= date('now', '-60 days')
    """).fetchall()

    video_updated = 0
    agenda_updated = 0
    for row in pending:
        eid = row["event_id"]
        data = get_champds_event(int(eid))
        time.sleep(0.3)
        if not data:
            continue

        # Check for video
        if not row["has_video"]:
            media = data.get("MediaInfo", {})
            media_type = media.get("MediaType", "")
            media_path = media.get("MediaPath", "")
            if media_path and media_type != "DISABLED":
                db.execute("""
                    UPDATE meetings SET has_video = 1, media_path = ? WHERE id = ?
                """, (media_path, row["id"]))
                video_updated += 1
                print(f"  Event {eid}: video now available")

        # Always refresh agenda — it may have been posted or updated since discovery
        agenda = extract_agenda(data)
        if agenda:
            new_agenda_json = json.dumps(agenda)
            old_agenda_json = row["agenda_json"] or ""
            if new_agenda_json != old_agenda_json:
                quick_summary = summarize_agenda(agenda)
                # Only update quick_summary if no article exists yet
                db.execute("""
                    UPDATE meetings SET agenda_json = ?
                    WHERE id = ?
                """, (new_agenda_json, row["id"]))
                # Update quick_summary only if article hasn't been written
                db.execute("""
                    UPDATE meetings SET quick_summary = ?
                    WHERE id = ? AND (article IS NULL OR article = '')
                """, (quick_summary, row["id"]))
                agenda_updated += 1
                label = "updated" if old_agenda_json else "added"
                print(f"  Event {eid}: agenda {label} ({len(agenda)} items)")

    db.commit()
    db.close()
    if video_updated:
        print(f"  {video_updated} meetings now have video available")
    if agenda_updated:
        print(f"  {agenda_updated} meetings had agenda updates")
    return video_updated + agenda_updated


def match_orphan_meetings():
    """Match meetings that have no event_id to ChampDS events by date + committee.

    Meetings from calendar feeds or manual entry often lack event_ids.
    This scans ChampDS to find matching events and links them.
    """
    db = get_db()
    orphans = db.execute("""
        SELECT id, committee, date FROM meetings
        WHERE event_id IS NULL
        AND date >= date('now', '-60 days')
    """).fetchall()

    if not orphans:
        db.close()
        return 0

    # Build a lookup of what we need to find
    needed = {}
    for row in orphans:
        key = (row["date"], row["committee"])
        needed[key] = row["id"]

    # Scan ChampDS event IDs around the range we expect
    # Get the max known event_id as starting point
    max_row = db.execute("""
        SELECT MAX(CAST(event_id AS INTEGER)) as max_eid
        FROM meetings WHERE event_id IS NOT NULL AND event_id NOT LIKE 'yt-%'
    """).fetchone()
    scan_start = max((max_row["max_eid"] or 1079) - 50, 1)
    scan_end = (max_row["max_eid"] or 1079) + 100

    # Also check which event_ids are already claimed
    claimed = {row["event_id"] for row in
               db.execute("SELECT event_id FROM meetings WHERE event_id IS NOT NULL").fetchall()}

    matched = 0
    print(f"  Scanning ChampDS {scan_start}-{scan_end} for {len(orphans)} orphan meetings...")
    for eid in range(scan_start, scan_end + 1):
        if str(eid) in claimed:
            continue

        data = get_champds_event(eid)
        time.sleep(0.2)
        if not data or "Event" not in data:
            continue

        ev = data["Event"]
        title = ev.get("EventTitle", "")
        if not title:
            continue

        date_raw = ev.get("EventDateTimeCustomerLocal",
                          ev.get("EventDateTimeUTC", ""))
        date = date_raw[:10] if date_raw else ""

        board = data.get("Board", {})
        raw_board_name = board.get("BoardName", "")
        committee = COMMITTEE_MAP.get(title, COMMITTEE_MAP.get(raw_board_name, title or raw_board_name))

        key = (date, committee)
        if key in needed:
            meeting_id = needed[key]
            # Link the event_id and pull in any data
            media = data.get("MediaInfo", {})
            media_type = media.get("MediaType", "")
            media_path = media.get("MediaPath", "") if media_type != "DISABLED" else ""
            has_video = 1 if media_path else 0

            agenda = extract_agenda(data)
            agenda_json_str = json.dumps(agenda) if agenda else None
            quick_summary = summarize_agenda(agenda) if agenda else None

            db.execute("""
                UPDATE meetings SET event_id = ?, has_video = MAX(has_video, ?),
                    media_path = COALESCE(media_path, ?), board_name = COALESCE(board_name, ?),
                    agenda_json = COALESCE(?, agenda_json),
                    quick_summary = CASE WHEN article IS NULL OR article = '' THEN COALESCE(?, quick_summary) ELSE quick_summary END
                WHERE id = ?
            """, (str(eid), has_video, media_path, raw_board_name,
                  agenda_json_str, quick_summary, meeting_id))
            matched += 1
            claimed.add(str(eid))
            del needed[key]
            n_agenda = len(agenda) if agenda else 0
            print(f"  Matched: {committee} ({date}) → event {eid} [{'VIDEO' if has_video else 'no video'}, {n_agenda} agenda items]")

        if not needed:
            break

    db.commit()
    db.close()
    if matched:
        print(f"  {matched} orphan meetings linked to ChampDS events")
    return matched


# ── Minutes Extraction from Agenda Approvals ──────────────────────

def ocr_pdf(pdf_path):
    """OCR a scanned PDF using pymupdf to render pages + tesseract for text.

    Returns extracted text or empty string on failure.
    """
    try:
        import fitz
        import tempfile

        doc = fitz.open(pdf_path)
        all_text = []

        for page_num in range(len(doc)):
            page = doc[page_num]
            # Render page at 300 DPI for good OCR quality
            mat = fitz.Matrix(300 / 72, 300 / 72)
            pix = page.get_pixmap(matrix=mat)

            # Save as temporary PNG
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as img_tmp:
                pix.save(img_tmp.name)
                img_path = img_tmp.name

            # Run tesseract
            result = subprocess.run(
                ["tesseract", img_path, "stdout", "-l", "eng", "--psm", "6"],
                capture_output=True, text=True, timeout=60
            )
            if result.stdout.strip():
                all_text.append(result.stdout.strip())

            os.unlink(img_path)

        num_pages = len(doc)
        doc.close()
        text = "\n\n".join(all_text)
        if text:
            print(f"    OCR extracted {len(text.split())} words from {num_pages} pages")
        return text

    except Exception as e:
        print(f"    OCR failed: {e}")
        return ""


def extract_minutes_from_agendas():
    """Scan agendas for 'Approval of Minutes' items, download PDFs, and link to past meetings.

    When a committee approves minutes from a previous meeting, the draft minutes PDF
    is typically attached. This function:
    1. Finds agenda items referencing minutes approval
    2. Downloads the attached PDF
    3. Extracts text with pdftotext
    4. Matches to the referenced past meeting by committee + date
    5. Updates that meeting's has_minutes and minutes_text
    """
    import re
    import tempfile

    db = get_db()
    # Get all meetings with agendas (look back 90 days for recent approvals)
    meetings = db.execute("""
        SELECT id, committee, date, event_id, agenda_json, board_name FROM meetings
        WHERE agenda_json IS NOT NULL
        AND date >= date('now', '-90 days')
        ORDER BY date DESC
    """).fetchall()

    updated = 0
    for meeting in meetings:
        agenda = json.loads(meeting["agenda_json"])
        board_name = meeting["board_name"] or ""
        committee = meeting["committee"]

        # Find minutes approval items recursively
        minutes_refs = []
        def find_minutes_items(items):
            for item in items:
                title = item.get("title", "")
                tl = title.lower()
                # Match various patterns: "Approval of Minutes", "APPROVAL OF MINUTES",
                # children like "Minutes of March 17, 2026" or "May 6th Regular Meeting"
                if "minute" in tl:
                    children = item.get("children", [])
                    if children:
                        for child in children:
                            if not isinstance(child, dict):
                                continue
                            atts = child.get("attachments", [])
                            if atts:
                                minutes_refs.append({
                                    "title": child.get("title", ""),
                                    "attachments": atts,
                                    "parent_committee": committee,
                                })
                    else:
                        atts = item.get("attachments", [])
                        if atts and "approval" not in tl.replace("approval of minutes", ""):
                            minutes_refs.append({
                                "title": title,
                                "attachments": atts,
                                "parent_committee": committee,
                            })
                children = item.get("children", [])
                if isinstance(children, list):
                    find_minutes_items([c for c in children if isinstance(c, dict)])
        find_minutes_items([item for item in agenda if isinstance(item, dict)])

        for ref in minutes_refs:
            # Parse the date from the title
            ref_title = ref["title"]
            ref_date = parse_minutes_date(ref_title, meeting["date"])
            if not ref_date:
                print(f"  Could not parse date from: {ref_title}")
                continue

            # Determine which committee these minutes belong to
            # Usually same committee as the meeting approving them
            ref_committee = ref["parent_committee"]

            # Check if the referenced meeting already has minutes
            target = db.execute("""
                SELECT id, has_minutes, committee FROM meetings
                WHERE committee = ? AND date = ?
            """, (ref_committee, ref_date)).fetchone()

            if not target:
                # Try fuzzy match — strip "(WAC)", "Meeting", etc. and match first two words
                base_name = ref_committee.split("(")[0].replace("Meeting", "").strip()
                words = base_name.split()
                if len(words) >= 2:
                    pattern = f"%{words[0]}%{words[1]}%"
                else:
                    pattern = f"%{words[0]}%"
                target = db.execute("""
                    SELECT id, has_minutes, committee FROM meetings
                    WHERE date = ? AND committee LIKE ?
                """, (ref_date, pattern)).fetchone()

            if not target:
                # Try nearby dates (±3 days) for date mismatches in titles
                target = db.execute("""
                    SELECT id, has_minutes, committee FROM meetings
                    WHERE committee = ? AND date BETWEEN date(?, '-3 days') AND date(?, '+3 days')
                    AND has_minutes = 0
                    ORDER BY ABS(julianday(date) - julianday(?))
                    LIMIT 1
                """, (ref_committee, ref_date, ref_date, ref_date)).fetchone()

            if not target:
                print(f"  No matching meeting for {ref_committee} on {ref_date} (from: {ref_title})")
                continue

            if target["has_minutes"]:
                continue  # Already has minutes

            # Download the PDF and extract text
            for att in ref["attachments"]:
                url = att.get("url", "")
                name = att.get("name", "")
                if not url or not name.lower().endswith(".pdf"):
                    continue
                if "draft" not in name.lower() and "minute" not in name.lower():
                    continue  # Skip non-minutes attachments

                try:
                    print(f"  Downloading minutes: {name}")
                    req = urllib.request.Request(url, headers={
                        "User-Agent": "Mozilla/5.0",
                        "Referer": "https://play.champds.com/"
                    })
                    resp = urllib.request.urlopen(req, timeout=30)
                    pdf_data = resp.read()

                    # Save to temp file and extract text
                    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                        tmp.write(pdf_data)
                        tmp_path = tmp.name

                    # Try pdftotext first (better layout preservation)
                    result = subprocess.run(
                        ["pdftotext", "-layout", tmp_path, "-"],
                        capture_output=True, text=True, timeout=30
                    )
                    minutes_text = result.stdout.strip()

                    # Fallback to pymupdf text extraction
                    if not minutes_text or len(minutes_text) < 50:
                        try:
                            import fitz
                            doc = fitz.open(tmp_path)
                            minutes_text = "\n".join(page.get_text() for page in doc)
                            doc.close()
                        except Exception:
                            pass

                    # Fallback to OCR for scanned PDFs
                    if not minutes_text or len(minutes_text) < 50:
                        print(f"    Text extraction failed, trying OCR...")
                        minutes_text = ocr_pdf(tmp_path)

                    os.unlink(tmp_path)

                    if not minutes_text or len(minutes_text) < 50:
                        print(f"    WARNING: Could not extract text from {name} (even with OCR)")
                        continue

                    # Update the target meeting
                    db.execute("""
                        UPDATE meetings SET has_minutes = 1, minutes_text = ?
                        WHERE id = ?
                    """, (minutes_text, target["id"]))
                    db.commit()
                    updated += 1
                    word_count = len(minutes_text.split())
                    print(f"    Updated meeting {target['id']} ({ref_committee} {ref_date}): {word_count} words")
                    break  # Only need one PDF per minutes reference

                except Exception as e:
                    print(f"    ERROR downloading {name}: {e}")
                    continue

    db.close()
    print(f"  {updated} meetings updated with minutes text from agenda PDFs")
    return updated


def parse_minutes_date(title, approving_meeting_date):
    """Parse a date from a minutes approval title like 'Minutes of March 17, 2026'
    or 'May 6th Regular Meeting' or 'April 14, 2026'."""
    import re
    from datetime import datetime

    title_clean = title.strip()

    # Extract year from the approving meeting as fallback
    approving_year = approving_meeting_date[:4] if approving_meeting_date else "2026"

    # Month names
    months = {
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12
    }

    # Pattern: "Month DD, YYYY" or "Month DDth, YYYY" or "Month DD YYYY"
    m = re.search(r'(\w+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})?', title_clean, re.IGNORECASE)
    if m:
        month_name = m.group(1).lower()
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else int(approving_year)

        if month_name in months:
            month = months[month_name]
            # If referenced month is after the approving meeting month, it's probably previous year
            approving_month = int(approving_meeting_date[5:7]) if approving_meeting_date else 12
            if month > approving_month and not m.group(3):
                year = int(approving_year) - 1
            try:
                return datetime(year, month, day).strftime("%Y-%m-%d")
            except ValueError:
                pass

    # Pattern: "MM/DD/YYYY" or "MM.DD.YYYY"
    m = re.search(r'(\d{1,2})[/.](\d{1,2})[/.](\d{2,4})', title_clean)
    if m:
        month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if year < 100:
            year += 2000
        try:
            return datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            pass

    return None


# ── Video Download ─────────────────────────────────────────────────

def get_media_path(event_id):
    """Get HLS media path for an event from ChampDS API."""
    data = get_champds_event(event_id)
    if not data or "MediaInfo" not in data:
        return None
    return data["MediaInfo"].get("MediaPath", "")


def download_video(event_id):
    """Download meeting video via ffmpeg HLS."""
    out_path = os.path.join(VIDEOS_DIR, f"{event_id}.mp4")
    if os.path.exists(out_path):
        print(f"  Video already exists: {out_path}")
        return out_path

    media_path = get_media_path(event_id)
    if not media_path:
        print(f"  ERROR: No media path for event {event_id}")
        return None

    hls_url = f"{CHAMPDS_STREAM}{media_path}/playlist.m3u8?PLAY"
    print(f"  Downloading event {event_id}...")
    result = subprocess.run([
        "ffmpeg", "-headers", "Referer: https://play.champds.com/\r\n",
        "-i", hls_url, "-c", "copy", out_path, "-y", "-loglevel", "warning"
    ], capture_output=True, text=True)

    if result.returncode != 0:
        print(f"  ERROR: ffmpeg failed: {result.stderr[:200]}")
        return None

    size_mb = os.path.getsize(out_path) / 1024 / 1024
    print(f"  Downloaded: {out_path} ({size_mb:.0f} MB)")
    return out_path


# ── Transcription ──────────────────────────────────────────────────

def transcribe_video(event_id):
    """Transcribe video with Deepgram Nova 3 + diarization."""
    api_key = os.environ.get("DEEPGRAM_API_KEY")
    if not api_key:
        print("  ERROR: Set DEEPGRAM_API_KEY environment variable")
        return None

    video_path = os.path.join(VIDEOS_DIR, f"{event_id}.mp4")
    if not os.path.exists(video_path):
        print(f"  ERROR: Video not found: {video_path}")
        return None

    out_path = os.path.join(TRANSCRIPTS_DIR, f"transcript-{event_id}.json")
    if os.path.exists(out_path):
        print(f"  Transcript already exists: {out_path}")
        return out_path

    # Extract audio first (much smaller upload)
    audio_path = f"/tmp/{event_id}.wav"
    print(f"  Extracting audio...")
    subprocess.run([
        "ffmpeg", "-i", video_path, "-vn", "-acodec", "pcm_s16le",
        "-ar", "16000", "-ac", "1", audio_path, "-y", "-loglevel", "warning"
    ], check=True)

    # Upload to Deepgram — full analysis: sentiment, topics, detect_language
    print(f"  Transcribing with Deepgram Nova 3 (full analysis)...")
    # Build keyterm list from entities DB for proper noun recognition
    import sqlite3 as _sql
    _rag_path = os.path.join(os.path.dirname(__file__), "rag.db")
    _keyterms = []
    try:
        _edb = _sql.connect(_rag_path)
        _rows = _edb.execute(
            "SELECT DISTINCT name FROM entities WHERE type='person' ORDER BY name"
        ).fetchall()
        _keyterms = [r[0] for r in _rows if len(r[0]) > 3]
        _edb.close()
    except Exception:
        pass

    # Add Croton-specific terms
    _keyterms += [
        "Croton-on-Hudson", "Croton-Harmon", "Croton Point",
        "Senasqua", "Gouvea", "Harckham", "Luposello",
        "Cortlandt", "Truesdale", "Scenic Drive",
    ]

    # Known Deepgram mishearings → corrections (applied server-side before output)
    _replacements = [
        ("Courtland Harmony", "Croton-Harmon"), ("Cortland Harmony", "Croton-Harmon"),
        ("Nach Taylor", "Nachtaler"), ("Nachteller", "Nachtaler"),
        ("Thalby", "Balbi"), ("Balby", "Balbi"),
        ("Sabrizi", "Sibrizzi"), ("Jeanette Choon", "Genette Toone"),
        ("Cronin Point", "Croton Point"), ("Quotum Point", "Croton Point"),
        ("Sonosqua", "Senasqua"), ("Prakademic", "Pracademic"),
    ]

    base_params = [
        "model=nova-3", "diarize_model=latest",
        "utterances=true", "smart_format=true",
        "language=en", "sentiment=true", "topics=true", "detect_language=true",
        "paragraphs=true", "summarize=v2",
    ]
    # Add keyterms (up to 100 to stay within URL limits)
    for kt in _keyterms[:100]:
        base_params.append(f"keyterm={urllib.parse.quote(kt)}")
    # Add find-and-replace
    for wrong, correct in _replacements:
        base_params.append(f"replace={urllib.parse.quote(wrong)}:{urllib.parse.quote(correct)}")

    params = "&".join(base_params)
    url = f"{DEEPGRAM_URL}?{params}"

    with open(audio_path, "rb") as f:
        audio_data = f.read()

    req = urllib.request.Request(url, data=audio_data, headers={
        "Authorization": f"Token {api_key}",
        "Content-Type": "audio/wav",
    })

    resp = urllib.request.urlopen(req, timeout=600)  # 10 min timeout for long meetings
    dg_result = json.loads(resp.read())

    # Save raw Deepgram response internally for analysis (sentiment, topics, etc.)
    raw_dir = os.path.join(TRANSCRIPTS_DIR, "raw")
    os.makedirs(raw_dir, exist_ok=True)
    raw_path = os.path.join(raw_dir, f"deepgram-{event_id}.json")
    with open(raw_path, "w") as f:
        json.dump(dg_result, f)
    print(f"  Raw Deepgram saved: {raw_path}")

    # Parse into clean public transcript format
    utterances = []
    for utt in dg_result.get("results", {}).get("utterances", []):
        entry = {
            "speaker": f"Speaker {utt.get('speaker', 0)}",
            "text": utt.get("transcript", ""),
            "start": utt.get("start", 0),
            "end": utt.get("end", 0),
            "timestamp": f"{int(utt.get('start', 0) // 60)}:{int(utt.get('start', 0) % 60):02d}",
        }
        # Store sentiment per-utterance internally
        sentiment = utt.get("sentiment")
        if sentiment:
            entry["sentiment"] = sentiment
        utterances.append(entry)

    # Get event metadata for title/date (ChampDS uses EventTitle, EventDateTimeUTC)
    ev_data = get_champds_event(event_id)
    ev = ev_data.get("Event", {}) if ev_data else {}
    title = ev.get("EventTitle", ev.get("Title", ""))
    date_raw = ev.get("EventDateTimeUTC", ev.get("MeetingDate", ""))
    date = date_raw[:10] if date_raw else ""

    # Extract Deepgram summaries and topics
    dg_summary = ""
    try:
        dg_summary = dg_result["results"]["summary"]["short"]
    except (KeyError, TypeError):
        pass
    dg_topics = []
    try:
        for seg in dg_result["results"]["topics"]["segments"]:
            for topic in seg.get("topics", []):
                t = topic.get("topic", "")
                if t and t not in dg_topics:
                    dg_topics.append(t)
    except (KeyError, TypeError):
        pass

    transcript = {
        "event_id": str(event_id),
        "title": title,
        "date": date,
        "platform": "deepgram-nova-3",
        "diarization": True,
        "full_text": " ".join(u["text"] for u in utterances),
        "utterances": utterances,
        "word_count": sum(len(u["text"].split()) for u in utterances),
        "speaker_count": len(set(u["speaker"] for u in utterances)),
        "speaker_map": {},
        "dg_summary": dg_summary,
        "dg_topics": dg_topics,
    }

    with open(out_path, "w") as f:
        json.dump(transcript, f, indent=2)

    # Cleanup temp audio
    os.remove(audio_path)

    # Update DB: mark transcript available and store word/speaker counts
    db = get_db()
    db.execute("""
        UPDATE meetings SET has_transcript = 1, word_count = ?, speaker_count = ?
        WHERE event_id = ?
    """, (transcript["word_count"], transcript["speaker_count"], str(event_id)))
    db.commit()
    db.close()

    print(f"  Transcribed: {transcript['word_count']} words, "
          f"{transcript['speaker_count']} speakers, {len(utterances)} utterances")
    return out_path


# ── Ingestion (chunk + embed) ──────────────────────────────────────

def ingest_transcript(event_id):
    """Chunk a transcript and insert into rag.db (uses existing ingest.py)."""
    transcript_path = os.path.join(TRANSCRIPTS_DIR, f"transcript-{event_id}.json")
    if not os.path.exists(transcript_path):
        print(f"  ERROR: Transcript not found: {transcript_path}")
        return False

    # Use existing ingest.py for chunking
    result = subprocess.run(
        [sys.executable, os.path.join(BASE_DIR, "ingest.py"), "transcripts"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  ERROR: Ingest failed: {result.stderr[:200]}")
        return False

    print(f"  Ingested transcript for event {event_id}")

    # Update meetings table
    db = get_db()
    db.execute("UPDATE meetings SET has_transcript = 1 WHERE event_id = ?", (str(event_id),))
    db.commit()
    db.close()

    # Run entity deduplication (prevents duplicate buildup)
    dedup_script = os.path.join(BASE_DIR, "dedup_entities.py")
    if os.path.exists(dedup_script):
        print(f"  Running entity deduplication...")
        subprocess.run([sys.executable, dedup_script], capture_output=True)

    # Enforce deleted_articles — permanently remove articles that were merged/killed
    enforce_deletions()

    return True


def enforce_deletions():
    """Remove any meetings that are in the deleted_articles table."""
    db = get_db()
    try:
        deleted = db.execute(
            "SELECT id FROM deleted_articles"
        ).fetchall()
        for row in deleted:
            existing = db.execute(
                "SELECT id FROM meetings WHERE id = ?", (row["id"],)
            ).fetchone()
            if existing:
                db.execute("DELETE FROM meetings WHERE id = ?", (row["id"],))
                print(f"  Enforced deletion: article {row['id']}")
        db.commit()
    except Exception:
        pass  # deleted_articles table may not exist yet
    db.close()


# ── Full Pipeline ──────────────────────────────────────────────────

def enrich_transcript_file(event_id):
    """Enrich a transcript: merge utterances, fix proper nouns, correct speaker names."""
    transcript_path = os.path.join(TRANSCRIPTS_DIR, f"transcript-{event_id}.json")
    if not os.path.exists(transcript_path):
        print(f"  ERROR: Transcript not found: {transcript_path}")
        return False

    # Import enrichment module
    try:
        enrich_module = os.path.join(BASE_DIR, "enrich_transcript.py")
        import importlib.util
        spec = importlib.util.spec_from_file_location("enrich_transcript", enrich_module)
        enrich = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(enrich)

        result = enrich.enrich_transcript(transcript_path)
        if not result:
            # May already be enriched, try name correction only
            enrich.fix_names_only(transcript_path)
        return True
    except Exception as e:
        print(f"  WARNING: Enrichment failed: {e}")
        return True  # Non-fatal, continue pipeline


def full_pipeline(event_id):
    """Run complete pipeline: download → transcribe → enrich → ingest."""
    print(f"\n{'='*60}")
    print(f"  PIPELINE: Event {event_id}")
    print(f"{'='*60}")

    print("\n[1/4] Downloading video...")
    video = download_video(event_id)
    if not video:
        return False

    print("\n[2/4] Transcribing...")
    transcript = transcribe_video(event_id)
    if not transcript:
        return False

    print("\n[3/4] Enriching transcript (proper nouns + speaker names)...")
    enrich_transcript_file(event_id)

    print("\n[4/4] Ingesting into rag.db...")
    success = ingest_transcript(event_id)

    if success:
        print(f"\n  COMPLETE: Event {event_id} fully processed")
    return success


# ── Process New (Full Automation) ─────────────────────────────────

def process_new():
    """Full automated pipeline: discover → re-check → download → transcribe → enrich → ingest → write.

    Each step is idempotent — safe to re-run. Designed for daily cron.
    """
    print(f"\n{'='*60}")
    print(f"  CROTON.NEWS PIPELINE — {time.strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")

    # Step 1: Discover new meetings
    print("\n[1/9] Discovering new meetings from ChampDS...")
    new_ids = discover_new_meetings()
    print(f"  → {len(new_ids)} new meetings registered")

    # Step 2: Match orphan meetings (from calendar feed) to ChampDS events
    print("\n[2/9] Matching orphan meetings to ChampDS events...")
    match_orphan_meetings()

    # Step 3: Re-check recent meetings for video + agenda updates
    print("\n[3/10] Re-checking recent meetings for video and agenda updates...")
    recheck_pending()

    # Step 4: Extract minutes from agenda approval PDFs
    print("\n[4/10] Extracting minutes from agenda approval PDFs...")
    extract_minutes_from_agendas()

    # Step 5: Download videos
    print("\n[5/10] Downloading missing videos...")
    db = get_db()
    to_download = db.execute("""
        SELECT event_id, media_path FROM meetings
        WHERE has_video = 1 AND has_transcript = 0
        AND event_id IS NOT NULL AND event_id NOT LIKE 'yt-%'
    """).fetchall()
    db.close()

    downloaded = 0
    for row in to_download:
        eid = int(row["event_id"])
        video_path = os.path.join(VIDEOS_DIR, f"{eid}.mp4")
        if not os.path.exists(video_path):
            result = download_video(eid)
            if result:
                downloaded += 1
    print(f"  → {downloaded} videos downloaded")

    # Step 6: Transcribe
    print("\n[6/10] Transcribing new videos...")
    db = get_db()
    to_transcribe = db.execute("""
        SELECT event_id FROM meetings
        WHERE has_video = 1 AND has_transcript = 0
        AND event_id IS NOT NULL AND event_id NOT LIKE 'yt-%'
    """).fetchall()
    db.close()

    transcribed = 0
    for row in to_transcribe:
        eid = int(row["event_id"])
        video_path = os.path.join(VIDEOS_DIR, f"{eid}.mp4")
        if os.path.exists(video_path):
            result = transcribe_video(eid)
            if result:
                transcribed += 1
    print(f"  → {transcribed} videos transcribed")

    # Step 7: Enrich
    print("\n[7/10] Enriching transcripts...")
    db = get_db()
    # Find meetings with transcripts that haven't been enriched
    # Enrichment modifies the transcript JSON in-place, adding "enriched": true
    to_enrich = db.execute("""
        SELECT event_id FROM meetings
        WHERE has_transcript = 1 AND event_id IS NOT NULL AND event_id NOT LIKE 'yt-%'
    """).fetchall()
    db.close()

    enriched = 0
    for row in to_enrich:
        eid = row["event_id"]
        transcript_path = os.path.join(TRANSCRIPTS_DIR, f"transcript-{eid}.json")
        if os.path.exists(transcript_path):
            with open(transcript_path) as f:
                tx = json.load(f)
            if not tx.get("enriched"):
                enrich_transcript_file(int(eid))
                enriched += 1
    print(f"  → {enriched} transcripts enriched")

    # Step 8: Ingest
    print("\n[8/10] Ingesting into RAG database...")
    db = get_db()
    to_ingest = db.execute("""
        SELECT event_id FROM meetings
        WHERE has_transcript = 1 AND event_id IS NOT NULL AND event_id NOT LIKE 'yt-%'
    """).fetchall()
    db.close()

    ingested = 0
    for row in to_ingest:
        eid = row["event_id"]
        # Check if chunks already exist for this event
        db2 = get_db()
        existing = db2.execute(
            "SELECT COUNT(*) as n FROM chunks WHERE doc_id = ?", (eid,)
        ).fetchone()
        db2.close()
        if existing["n"] == 0:
            if ingest_transcript(int(eid)):
                ingested += 1
    print(f"  → {ingested} transcripts ingested")

    # Step 9: Write articles + fact-check (MUST go through write_and_check.py)
    # NEVER use write_article.py directly — it bypasses the editor/fact-checker.
    # write_and_check.py: writer drafts → editor verifies every name, quote, vote → publish
    print("\n[9/10] Writing and fact-checking articles...")
    wac_script = os.path.join(BASE_DIR, "write_and_check.py")
    if os.path.exists(wac_script):
        result = subprocess.run(
            [sys.executable, wac_script],
            capture_output=True, text=True, timeout=1800  # 30 min for multiple articles
        )
        print(result.stdout[-500:] if result.stdout else "  No output")
        if result.returncode != 0:
            print(f"  WARNING: write_and_check failed: {result.stderr[:300]}")
    else:
        print("  ERROR: write_and_check.py not found — articles will NOT be written")

    # Step 10: Insert photos into articles
    print("\n[10/10] Inserting photos into articles...")
    photo_script = os.path.join(BASE_DIR, "insert_photos.py")
    photos_inserted = 0
    if os.path.exists(photo_script):
        db = get_db()
        needs_photos = db.execute("""
            SELECT event_id, committee, date FROM meetings
            WHERE article IS NOT NULL AND article != ''
            AND article NOT LIKE '%{{photo:%'
            AND has_video = 1
            AND event_id IS NOT NULL AND event_id NOT LIKE 'yt-%'
            ORDER BY date DESC
        """).fetchall()
        db.close()

        for row in needs_photos:
            eid = row["event_id"]
            print(f"  Photos for {row['committee']} ({row['date']})...")
            try:
                result = subprocess.run(
                    [sys.executable, photo_script, eid],
                    capture_output=True, text=True, timeout=600
                )
                if result.returncode == 0:
                    photos_inserted += 1
                else:
                    print(f"    WARNING: Photo insertion failed: {result.stderr[:200]}")
            except subprocess.TimeoutExpired:
                print(f"    WARNING: Photo insertion timed out for {eid}")
    print(f"  → {photos_inserted} articles got photos")

    print(f"\n{'='*60}")
    print(f"  PIPELINE COMPLETE")
    print(f"  New meetings: {len(new_ids)} | Downloaded: {downloaded} | "
          f"Transcribed: {transcribed} | Articles: {written} | Photos: {photos_inserted}")
    print(f"{'='*60}\n")


# ── Status ─────────────────────────────────────────────────────────

def show_status():
    """Show pipeline status for all meetings."""
    db = get_db()
    meetings = db.execute("""
        SELECT id, event_id, committee, date, has_video, has_transcript,
               CASE WHEN article IS NOT NULL AND article != '' THEN 1 ELSE 0 END as has_article,
               CASE WHEN agenda_json IS NOT NULL THEN 1 ELSE 0 END as has_agenda
        FROM meetings ORDER BY date DESC
    """).fetchall()

    print(f"\n{'ID':>3} {'Event':>6} {'Date':>10} {'Vid':>3} {'Tx':>3} {'Art':>3} {'Ag':>3}  Committee")
    print("-" * 85)
    for m in meetings:
        vid = "Y" if m["has_video"] else "-"
        tx = "Y" if m["has_transcript"] else "-"
        art = "Y" if m["has_article"] else "-"
        ag = "Y" if m["has_agenda"] else "-"
        eid = m["event_id"] or "-"
        print(f"{m['id']:>3} {eid:>6} {m['date']:>10} {vid:>3} {tx:>3} {art:>3} {ag:>3}  {m['committee']}")

    # Summary
    total = len(meetings)
    with_agenda = sum(1 for m in meetings if m["has_agenda"])
    with_video = sum(1 for m in meetings if m["has_video"])
    with_transcript = sum(1 for m in meetings if m["has_transcript"])
    with_article = sum(1 for m in meetings if m["has_article"])
    print(f"\nTotal: {total} | Agendas: {with_agenda} | Videos: {with_video} | "
          f"Transcripts: {with_transcript} | Articles: {with_article}")

    db.close()


# ── CLI ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "process-new":
        process_new()

    elif cmd == "discover":
        start = int(sys.argv[2]) if len(sys.argv) > 2 else None
        end = int(sys.argv[3]) if len(sys.argv) > 3 else None
        print("Discovering new meetings from ChampDS...")
        new_ids = discover_new_meetings(start, end)
        print(f"\nRegistered {len(new_ids)} new meetings in database")

    elif cmd == "download":
        if len(sys.argv) < 3:
            print("Usage: pipeline.py download EVENT_ID")
            sys.exit(1)
        download_video(int(sys.argv[2]))

    elif cmd == "download-all":
        db = get_db()
        rows = db.execute("""
            SELECT event_id FROM meetings
            WHERE has_video = 1 AND event_id IS NOT NULL AND event_id NOT LIKE 'yt-%'
        """).fetchall()
        db.close()
        for row in rows:
            eid = int(row["event_id"])
            if not os.path.exists(os.path.join(VIDEOS_DIR, f"{eid}.mp4")):
                download_video(eid)

    elif cmd == "transcribe":
        if len(sys.argv) < 3:
            print("Usage: pipeline.py transcribe EVENT_ID")
            sys.exit(1)
        transcribe_video(int(sys.argv[2]))

    elif cmd == "transcribe-all":
        db = get_db()
        rows = db.execute("""
            SELECT event_id FROM meetings
            WHERE has_video = 1 AND has_transcript = 0 AND event_id IS NOT NULL
        """).fetchall()
        db.close()
        for row in rows:
            transcribe_video(int(row["event_id"]))

    elif cmd == "enrich":
        if len(sys.argv) < 3:
            print("Usage: pipeline.py enrich EVENT_ID | --all | --fix-names")
            sys.exit(1)
        if sys.argv[2] == "--all" or sys.argv[2] == "--fix-names":
            # Pass through to enrich_transcript.py
            result = subprocess.run(
                [sys.executable, os.path.join(BASE_DIR, "enrich_transcript.py"), sys.argv[2]],
                capture_output=False,
            )
        else:
            enrich_transcript_file(int(sys.argv[2]))

    elif cmd == "ingest":
        if len(sys.argv) < 3:
            print("Usage: pipeline.py ingest EVENT_ID")
            sys.exit(1)
        ingest_transcript(int(sys.argv[2]))

    elif cmd == "full":
        if len(sys.argv) < 3:
            print("Usage: pipeline.py full EVENT_ID")
            sys.exit(1)
        full_pipeline(int(sys.argv[2]))

    elif cmd == "status":
        show_status()

    elif cmd == "refresh-agendas":
        print("Refreshing agendas for recent meetings...")
        recheck_pending()

    elif cmd == "extract-minutes":
        print("Extracting minutes from agenda approval PDFs...")
        extract_minutes_from_agendas()

    elif cmd == "match-orphans":
        print("Matching orphan meetings to ChampDS events...")
        match_orphan_meetings()

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)
