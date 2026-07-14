#!/usr/bin/env python3
"""
BoardDocs API client for CHUFSD Board of Education.

Fetches meeting lists, agendas, and official minutes from BoardDocs Pro.
Minutes serve as the authoritative source of names, votes, and motions
for the BOE pipeline — equivalent to ecode360/summaries.db for village meetings.

Usage:
    python3 boarddocs.py list                    # List all meetings
    python3 boarddocs.py agenda MEETING_ID       # Show agenda
    python3 boarddocs.py minutes MEETING_ID      # Fetch + parse minutes
    python3 boarddocs.py fetch-all               # Fetch all available minutes
    python3 boarddocs.py sync                    # Sync minutes to rag.db meetings
    python3 boarddocs.py names                   # Extract canonical names from all minutes
"""

import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
from html import unescape

# Load .env
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
MINUTES_DIR = os.path.join(BASE_DIR, "transcripts", "minutes")

BOARDDOCS_BASE = "https://go.boarddocs.com"
DISTRICT_PATH = "/ny/chufsd/Board.nsf"
COMMITTEE_ID = "A9QMJP5B81AB"


def log(msg):
    print(f"[boarddocs] {msg}")


# ── API Client ───────────────────────────────────────────────────────

def _post(endpoint, data=None):
    """POST to BoardDocs API and return response body.
    Uses IPRoyal proxy if IPROYAL_PROXY env var is set (BoardDocs blocks VPS IPs)."""
    url = f"{BOARDDOCS_BASE}{DISTRICT_PATH}/{endpoint}?open"
    form_data = {"current_committee_id": COMMITTEE_ID}
    if data:
        form_data.update(data)
    body = urllib.parse.urlencode(form_data).encode()

    proxy_url = os.environ.get("IPROYAL_PROXY", "")

    if proxy_url:
        # Use proxy via subprocess curl (urllib proxy support is limited for HTTPS CONNECT)
        import subprocess
        curl_cmd = [
            "curl", "-ks", "-x", proxy_url,
            "-X", "POST", url,
            "-d", urllib.parse.urlencode(form_data),
            "-H", "Content-Type: application/x-www-form-urlencoded",
            "-H", f"Referer: {BOARDDOCS_BASE}{DISTRICT_PATH}/Public",
            "--max-time", "30",
        ]
        try:
            r = subprocess.run(curl_cmd, capture_output=True, text=True, timeout=35)
            if r.returncode == 0 and r.stdout:
                return r.stdout
            # Fall through to direct request if proxy fails
            log(f"  Proxy failed (rc={r.returncode}), trying direct...")
        except Exception as e:
            log(f"  Proxy error: {e}, trying direct...")

    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": f"{BOARDDOCS_BASE}{DISTRICT_PATH}/Public",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        log(f"  API error ({endpoint}): {e}")
        return ""


import urllib.parse


def fetch_meetings_list():
    """Fetch list of all BOE meetings from BoardDocs. Returns list of dicts."""
    raw = _post("BD-GetMeetingsList")
    if not raw:
        return []
    try:
        meetings = json.loads(raw)
    except json.JSONDecodeError:
        log("  Failed to parse meetings list JSON")
        return []

    result = []
    for m in meetings:
        numberdate = str(m.get("numberdate", ""))
        # numberdate is YYYYMMDD format
        date = ""
        if len(numberdate) == 8:
            date = f"{numberdate[:4]}-{numberdate[4:6]}-{numberdate[6:8]}"
        result.append({
            "unique": m.get("unique", ""),
            "date": date,
            "name": m.get("name", ""),
            "numberdate": numberdate,
        })
    return result


def fetch_agenda(meeting_id):
    """Fetch and parse agenda for a meeting. Returns structured dict."""
    html = _post("BD-GetAgenda", {"id": meeting_id})
    if not html or "Error" in html[:200]:
        return None
    return parse_agenda_html(html)


def fetch_minutes(meeting_id):
    """Fetch official minutes HTML for a meeting."""
    html = _post("BD-GetMinutes", {"id": meeting_id})
    if not html or len(html) < 500:
        return None
    if "Error" in html[:200] or "Object variable not set" in html:
        return None
    return html


# ── HTML Parsers ─────────────────────────────────────────────────────

def parse_agenda_html(html):
    """Parse BoardDocs agenda HTML into structured categories and items."""
    categories = []
    items = []

    # Extract categories
    for m in re.finditer(r'class="category-name">(.*?)</span>', html):
        categories.append(unescape(m.group(1)).strip())

    # Extract items with type and title
    for m in re.finditer(r'Xtitle="(.*?)"', html):
        raw = unescape(m.group(1)).strip()
        parts = raw.split(" - ", 1)
        if len(parts) == 2:
            items.append({"type": parts[0].strip(), "title": parts[1].strip()})
        else:
            items.append({"type": "", "title": raw})

    return {"categories": categories, "items": items}


def parse_minutes_html(html):
    """Parse BoardDocs minutes HTML into structured data.

    Returns dict with: attendees, motions, speakers, full_text, sections.
    This is the BOE equivalent of the summaries.db index_json for village meetings.
    """
    # Strip HTML to plain text
    text = re.sub(r'<br\s*/?>', '\n', html)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = unescape(text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    full_text = text.strip()

    result = {
        "attendees": [],
        "also_present": [],
        "absent": [],
        "motions": [],
        "speakers": set(),
        "full_text": full_text,
        "word_count": len(full_text.split()),
    }

    # Extract attendees — "Members present" section
    present_m = re.search(
        r'(?:Members?\s+present|PRESENT)[:\s]*(.*?)(?=\n\s*\n|Meeting called|Also Present|Members?\s+absent|ABSENT)',
        full_text, re.I | re.DOTALL
    )
    if present_m:
        names_text = present_m.group(1).strip()
        for name in re.split(r'[,;]\s*', names_text):
            name = name.strip().rstrip('.')
            # Skip noise: sentences, fragments, notes
            if not name or len(name) < 3 or len(name) > 40:
                continue
            if any(w in name.lower() for w in ['meeting', 'called', 'order', 'time', 'are ', 'were ', 'the ']):
                continue
            result["attendees"].append(name)
            result["speakers"].add(name.split(",")[0].strip())

    # Also present (administrators, staff)
    also_m = re.search(
        r'(?:Also\s+Present|ALSO\s+PRESENT)[:\s]*(.*?)(?=\n\s*\n|Meeting called|Members?\s+absent)',
        full_text, re.I | re.DOTALL
    )
    if also_m:
        for name in re.split(r'[,;]\s*', also_m.group(1).strip()):
            name = name.strip()
            if name and len(name) > 2:
                result["also_present"].append(name)
                result["speakers"].add(name.split(",")[0].strip())

    # Absent members
    absent_m = re.search(
        r'(?:Members?\s+absent|ABSENT)[:\s]*(.*?)(?=\n\s*\n|Meeting called)',
        full_text, re.I | re.DOTALL
    )
    if absent_m:
        for name in re.split(r'[,;]\s*', absent_m.group(1).strip()):
            name = name.strip()
            if name and len(name) > 2:
                result["absent"].append(name)

    # Extract motions — "Motion by X, second by Y"
    for m in re.finditer(
        r'Motion\s+by\s+([^,]+),\s*second(?:ed)?\s+by\s+([^.\n]+)',
        full_text, re.I
    ):
        moved_by = m.group(1).strip()
        seconded_by = m.group(2).strip()
        result["motions"].append({
            "moved_by": moved_by,
            "seconded_by": seconded_by,
        })
        result["speakers"].add(moved_by)
        result["speakers"].add(seconded_by)

    # Extract vote results
    for m in re.finditer(
        r'(?:Final Resolution|RESOLVED):\s*(.*?)(?:\n|$)',
        full_text, re.I
    ):
        resolution = m.group(1).strip()
        if result["motions"]:
            result["motions"][-1]["result"] = resolution

    # Extract yes/no vote names
    for m in re.finditer(r'Yes:\s*(.*?)(?:\n|$)', full_text):
        names = [n.strip() for n in m.group(1).split(",") if n.strip()]
        if result["motions"]:
            result["motions"][-1]["yes_votes"] = names
        for n in names:
            result["speakers"].add(n)

    # Extract public speakers — "X spoke to..." patterns
    for m in re.finditer(
        r'([A-Z][a-z]+ [A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+spoke\s+(?:to|about|regarding)',
        full_text
    ):
        result["speakers"].add(m.group(1))

    result["speakers"] = sorted(result["speakers"])
    return result


def _clean_name(raw):
    """Clean a name: strip parentheticals, honorifics, extra whitespace."""
    name = re.sub(r'\(.*?\)', '', raw).strip()  # Remove parenthetical notes
    name = re.sub(r'\s+', ' ', name).strip()    # Normalize whitespace
    name = re.sub(r'^(Mr\.|Ms\.|Mrs\.|Dr\.)\s*', '', name)  # Strip honorifics
    return name


def extract_canonical_names(parsed_minutes):
    """Extract canonical name spellings from parsed minutes.

    Returns list of dicts with name, role, source suitable for the
    entity database and enrich_transcript.py name correction.
    """
    names = []

    # Board members from attendance (format: "First Last" or "First Last, Role")
    for entry in parsed_minutes.get("attendees", []):
        parts = entry.split(",", 1)
        name = _clean_name(parts[0])
        role = parts[1].strip() if len(parts) > 1 else "Board Member"
        if name and len(name) > 3:
            names.append({"name": name, "role": role, "source": "boarddocs_attendance"})

    # Administrators from "also present"
    for entry in parsed_minutes.get("also_present", []):
        parts = entry.split(",", 1)
        name = _clean_name(parts[0])
        role = parts[1].strip() if len(parts) > 1 else "Staff"
        # Skip noise entries
        if not name or len(name) < 4 or len(name) > 40:
            continue
        if any(w in name.lower() for w in ['meeting', 'order', 'time', 'the ', 'who ', 'every ']):
            continue
        names.append({"name": name, "role": role, "source": "boarddocs_staff"})

    # Motion makers/seconders
    for motion in parsed_minutes.get("motions", []):
        for field in ("moved_by", "seconded_by"):
            raw = motion.get(field, "")
            name = _clean_name(raw)
            if name and len(name) > 2:
                names.append({"name": name, "role": "Board Member", "source": "boarddocs_motion"})

    return names


# ── Database Integration ─────────────────────────────────────────────

def sync_to_meetings(db_path=RAG_DB):
    """Sync BoardDocs meetings to rag.db.

    For each BoardDocs meeting that has a corresponding rag.db entry (by date),
    store the boarddocs_id and mark has_minutes if minutes are available.
    """
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    # Ensure columns exist
    cols = [r["name"] for r in db.execute("PRAGMA table_info(meetings)")]
    if "boarddocs_id" not in cols:
        db.execute("ALTER TABLE meetings ADD COLUMN boarddocs_id TEXT")
    if "minutes_text" not in cols:
        db.execute("ALTER TABLE meetings ADD COLUMN minutes_text TEXT")

    meetings = fetch_meetings_list()
    if not meetings:
        # network/403 failure previously exited 0 ("Synced 0") — silent
        # failure invisible to run_job/watchdog (2026-07-14 audit gap 4)
        log("ERROR: BoardDocs meetings list fetch failed (proxy + direct) — sync aborted")
        sys.exit(1)
    matched = 0
    for m in meetings:
        if not m["date"]:
            continue
        row = db.execute(
            "SELECT id, boarddocs_id FROM meetings WHERE date = ? AND committee = 'Board of Education'",
            (m["date"],)
        ).fetchone()
        if row and not row["boarddocs_id"]:
            db.execute("UPDATE meetings SET boarddocs_id = ? WHERE id = ?",
                        (m["unique"], row["id"]))
            matched += 1

    db.commit()
    db.close()
    log(f"Synced {matched} BoardDocs IDs to meetings table")


def sync_local_minutes(db_path=RAG_DB):
    """Load stored minutes JSON files into meetings.minutes_text.

    Network-free companion to sync: `fetch-all` (or the phone relay's
    boarddocs-fetch) stores minutes-bd-<ID>.json in MINUTES_DIR, but nothing
    loaded them into the DB — 105 phone-fetched files sat unused on
    2026-07-13. Matches on meetings.boarddocs_id.
    """
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT id, boarddocs_id FROM meetings WHERE boarddocs_id IS NOT NULL "
        "AND (minutes_text IS NULL OR minutes_text = '')").fetchall()
    loaded = 0
    for row in rows:
        json_path = os.path.join(MINUTES_DIR, f"minutes-bd-{row['boarddocs_id']}.json")
        if not os.path.exists(json_path):
            continue
        try:
            with open(json_path) as f:
                parsed = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log(f"  skip {row['boarddocs_id']}: {e}")
            continue
        full_text = (parsed.get("full_text") or "").strip()
        if not full_text:
            continue
        db.execute(
            "UPDATE meetings SET minutes_text = ?, has_minutes = 1 WHERE id = ?",
            (full_text, row["id"]))
        loaded += 1
        log(f"  loaded minutes for {row['boarddocs_id']} ({len(full_text)} chars)")
    db.commit()
    db.close()
    log(f"Loaded minutes into {loaded} meeting(s) from local files")


def fetch_and_store_minutes(meeting_id, date=""):
    """Fetch minutes for a meeting and store as JSON."""
    os.makedirs(MINUTES_DIR, exist_ok=True)

    json_path = os.path.join(MINUTES_DIR, f"minutes-bd-{meeting_id}.json")
    if os.path.exists(json_path):
        log(f"  Minutes already stored: {json_path}")
        with open(json_path) as f:
            return json.load(f)

    html = fetch_minutes(meeting_id)
    if not html:
        return None

    parsed = parse_minutes_html(html)
    parsed["boarddocs_id"] = meeting_id
    parsed["date"] = date

    # Store raw HTML
    html_path = os.path.join(MINUTES_DIR, f"minutes-bd-{meeting_id}.html")
    with open(html_path, "w") as f:
        f.write(html)

    # Store parsed JSON
    with open(json_path, "w") as f:
        json.dump(parsed, f, indent=2)

    log(f"  Stored minutes: {len(parsed['full_text'])} chars, "
        f"{len(parsed['attendees'])} attendees, {len(parsed['motions'])} motions")
    return parsed


def get_all_canonical_names():
    """Load canonical names from ALL stored BoardDocs minutes.

    This is called by enrich_transcript.py to get the BOE name authority list,
    equivalent to how summaries.db provides village meeting names.
    """
    names = {}  # name -> {role, count, source}

    if not os.path.isdir(MINUTES_DIR):
        return names

    for fname in os.listdir(MINUTES_DIR):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(MINUTES_DIR, fname)) as f:
                parsed = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        for entry in extract_canonical_names(parsed):
            name = entry["name"]
            if name in names:
                names[name]["count"] += 1
            else:
                names[name] = {
                    "role": entry["role"],
                    "count": 1,
                    "source": entry["source"],
                }

        # Extract specific roles from minutes body text for role-based matching
        # "Board President Ana Teague", "Superintendent Stephen Walker", etc.
        text = parsed.get("full_text", "")
        role_patterns = [
            (r'Board Vice President ([A-Z][a-z]+ [A-Z][a-z]+)', "Board Vice President"),
            (r'Board President ([A-Z][a-z]+ [A-Z][a-z]+)', "Board President"),
            (r'Superintendent (?:Dr\. )?(?:of Schools )?([A-Z][a-z]+ [A-Z][a-z]+)', "Superintendent of Schools"),
            (r'Assistant Superintendent (?:for \w+ )?([A-Z][a-z]+ [A-Z][a-z]+)', "Assistant Superintendent"),
            (r'District Clerk ([A-Z][a-z]+ [A-Z][a-z]+)', "District Clerk"),
        ]
        for pattern, role in role_patterns:
            for m in re.finditer(pattern, text):
                name = _clean_name(m.group(1))
                if name and len(name) > 4 and len(name) < 35:
                    if name in names:
                        # Upgrade role to the more specific one
                        if role != "Board Member":
                            names[name]["role"] = role
                    else:
                        names[name] = {"role": role, "count": 1, "source": "boarddocs_role"}

    return names


# ── CLI ──────────────────────────────────────────────────────────────

def cmd_list():
    meetings = fetch_meetings_list()
    db = sqlite3.connect(RAG_DB)
    db.row_factory = sqlite3.Row

    log(f"{len(meetings)} meetings on BoardDocs")
    print()
    for m in meetings[:30]:
        row = db.execute(
            "SELECT id, has_transcript, article IS NOT NULL as has_article "
            "FROM meetings WHERE date = ? AND committee = 'Board of Education'",
            (m["date"],)
        ).fetchone()
        status = ""
        if row:
            parts = []
            if row["has_transcript"]:
                parts.append("transcript")
            if row["has_article"]:
                parts.append("article")
            status = f"  [{'+ '.join(parts) or 'stub'}]" if parts else "  [in DB]"

        mins_path = os.path.join(MINUTES_DIR, f"minutes-bd-{m['unique']}.json")
        has_mins = " [minutes]" if os.path.exists(mins_path) else ""

        print(f"  {m['date']}  {m['name']:30s}{status}{has_mins}")

    db.close()


def cmd_agenda(meeting_id):
    agenda = fetch_agenda(meeting_id)
    if not agenda:
        log("No agenda found")
        return
    for item in agenda["items"]:
        print(f"  [{item['type']:15s}] {item['title']}")


def cmd_minutes(meeting_id):
    meetings = fetch_meetings_list()
    date = ""
    for m in meetings:
        if m["unique"] == meeting_id:
            date = m["date"]
            break

    parsed = fetch_and_store_minutes(meeting_id, date)
    if not parsed:
        log("No minutes available")
        return

    print(f"  Attendees: {', '.join(parsed['attendees'])}")
    print(f"  Also present: {', '.join(parsed['also_present'])}")
    print(f"  Motions: {len(parsed['motions'])}")
    print(f"  Speakers: {', '.join(parsed['speakers'])}")
    print(f"  Word count: {parsed['word_count']}")


def cmd_fetch_all():
    meetings = fetch_meetings_list()
    fetched = 0
    skipped = 0
    no_minutes = 0

    for m in meetings:
        json_path = os.path.join(MINUTES_DIR, f"minutes-bd-{m['unique']}.json")
        if os.path.exists(json_path):
            skipped += 1
            continue

        log(f"Fetching {m['date']} {m['name']}...")
        parsed = fetch_and_store_minutes(m["unique"], m["date"])
        if parsed:
            fetched += 1
        else:
            no_minutes += 1
        time.sleep(1)  # Rate limit

    log(f"Fetched: {fetched}, skipped (cached): {skipped}, no minutes: {no_minutes}")


def cmd_names():
    names = get_all_canonical_names()
    log(f"{len(names)} unique names from BoardDocs minutes")
    print()
    for name, info in sorted(names.items(), key=lambda x: -x[1]["count"]):
        print(f"  {info['count']:3d}x  {name:30s}  [{info['role']}]")


def cmd_sync():
    sync_to_meetings()


def cmd_sync_local():
    sync_local_minutes()


def main():
    args = sys.argv[1:]
    if not args or args[0] == "list":
        cmd_list()
    elif args[0] == "agenda" and len(args) > 1:
        cmd_agenda(args[1])
    elif args[0] == "minutes" and len(args) > 1:
        cmd_minutes(args[1])
    elif args[0] == "fetch-all":
        cmd_fetch_all()
    elif args[0] == "names":
        cmd_names()
    elif args[0] == "sync":
        cmd_sync()
    elif args[0] == "sync-local":
        cmd_sync_local()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
