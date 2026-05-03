#!/usr/bin/env python3
"""
rag_tool.py — JSON-speaking tool interface for the croton-writer agent.

This script is called over SSH by the WireClaw croton-writer agent
(GLM 5.0 Turbo via Zhipu — NOT Claude/Anthropic). It exposes the
building blocks the agent needs to write an article:

    meeting_info EVENT_ID
    get_transcript EVENT_ID [--max-chars N]
    get_quote_pool EVENT_ID [--limit N] [--min-chars N] [--max-chars N]
    verify_quote EVENT_ID TS "TEXT"
    search_references QUERY [--limit N]
    recent_meetings [--committee NAME] [--limit N]
    save_article EVENT_ID               (reads JSON from stdin)

Every subcommand writes a single JSON payload to stdout on success.
Errors go to stderr and exit nonzero.
"""

import json
import os
import re
import sqlite3
import sys
import urllib.request
from datetime import datetime

# Load .env (for GEMINI_API_KEY so search can use embeddings)
_env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env):
    with open(_env) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RAG_DB = os.path.join(BASE_DIR, "rag.db")
TRANSCRIPTS_DIR = os.path.join(BASE_DIR, "transcripts")
PACKET_CACHE_DIR = os.path.join(BASE_DIR, "packets")

CHAMPDS_API = "https://playapi.champds.com/crotononhudsonny/event/{eid}"
CHAMPDS_ATT_BASE = "https://play.champds.com/ATT/crotononhudsonny"

PROCEDURAL_PHRASES = (
    "all in favor", "second the motion", "call to order", "i so move",
    "any opposed", "motion carries", "we are adjourned", "stand adjourned",
    "thank you very much", "i make a motion", "all in favor signify",
)


def die(msg, code=2):
    print(json.dumps({"error": msg}), file=sys.stderr)
    sys.exit(code)


def out(payload):
    print(json.dumps(payload, default=str, ensure_ascii=False))


def get_db():
    db = sqlite3.connect(RAG_DB)
    db.row_factory = sqlite3.Row
    _ensure_packet_table(db)
    return db


def _ensure_packet_table(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS packet_pdfs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            media_file TEXT NOT NULL,
            location TEXT,
            nickname TEXT,
            agenda_item_title TEXT,
            kind TEXT,
            size_bytes INTEGER,
            pages INTEGER,
            char_count INTEGER,
            truncated INTEGER DEFAULT 0,
            text TEXT,
            error TEXT,
            downloaded_at TEXT DEFAULT (datetime('now')),
            UNIQUE (event_id, media_file)
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_packet_event ON packet_pdfs(event_id)")
    db.execute("""
        CREATE TABLE IF NOT EXISTS packet_boilerplate (
            line TEXT PRIMARY KEY
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS packet_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    db.commit()


# Per-process cache of the boilerplate set to avoid re-querying on every
# extraction call inside a single packet_backfill run.
_BOILERPLATE_CACHE = None


def _get_boilerplate(db, rebuild_after_n=100):
    """Return the boilerplate set. Rebuild from corpus if stale or missing."""
    global _BOILERPLATE_CACHE
    if _BOILERPLATE_CACHE is not None:
        return _BOILERPLATE_CACHE

    rows = db.execute("SELECT line FROM packet_boilerplate").fetchall()
    cached_set = {r[0] for r in rows}

    last_built_row = db.execute(
        "SELECT value FROM packet_meta WHERE key='boilerplate_built_at_count'"
    ).fetchone()
    last_built = int(last_built_row[0]) if last_built_row else 0

    current_count = db.execute(
        "SELECT COUNT(*) FROM packet_pdfs WHERE kind IN ('pdf','pdf_ocr') AND text IS NOT NULL"
    ).fetchone()[0]

    needs_rebuild = (
        not cached_set
        or abs(current_count - last_built) >= rebuild_after_n
    )

    if needs_rebuild and current_count >= 30:
        built = _build_boilerplate_set(db)
        if built:
            db.execute("DELETE FROM packet_boilerplate")
            db.executemany(
                "INSERT OR IGNORE INTO packet_boilerplate (line) VALUES (?)",
                [(l,) for l in built],
            )
            db.execute(
                "INSERT OR REPLACE INTO packet_meta (key, value) VALUES "
                "('boilerplate_built_at_count', ?)",
                (str(current_count),),
            )
            db.commit()
            cached_set = built

    _BOILERPLATE_CACHE = cached_set
    return cached_set


# ── Helpers ─────────────────────────────────────────────────────────

def normalize_quote(s):
    s = (s or "").lower()
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def deflutter(s):
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r"\b(\w+)(\s+\1\b)+", r"\1", s)
    return s


def resolve_speaker(raw, speaker_map):
    speaker = raw or "Unknown"
    if speaker_map:
        num = speaker.replace("Speaker ", "")
        if num in speaker_map:
            return speaker_map[num]
    return speaker


def load_transcript(event_id):
    path = os.path.join(TRANSCRIPTS_DIR, f"transcript-{event_id}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def build_quote_pool(transcript, limit=80, min_chars=60, max_chars=420):
    speaker_map = transcript.get("speaker_map") or {}
    pool = []
    for u in transcript.get("utterances", []):
        text = (u.get("text") or "").strip()
        if len(text) < min_chars or len(text) > max_chars:
            continue
        lc = text.lower()
        if any(p in lc for p in PROCEDURAL_PHRASES):
            continue
        speaker = resolve_speaker(u.get("speaker", ""), speaker_map)
        if re.match(r"^speaker\s*\d*$", speaker.lower()):
            speaker = "Unknown speaker"
        ts = int(u.get("start", 0))
        pool.append({"ts": ts, "speaker": speaker, "text": text})
    pool.sort(key=lambda x: -len(x["text"]))
    pool = pool[:limit]
    pool.sort(key=lambda x: x["ts"])
    return pool


# ── Subcommands ─────────────────────────────────────────────────────

def cmd_meeting_info(args):
    if not args:
        die("usage: meeting_info EVENT_ID")
    eid = args[0]
    db = get_db()
    row = db.execute(
        "SELECT id, event_id, date, committee, headline, quick_summary, "
        "has_transcript, has_video, word_count, speaker_count, "
        "article_model, article_generated_at, "
        "CASE WHEN article IS NOT NULL AND length(article) > 0 THEN length(article) ELSE 0 END AS article_chars "
        "FROM meetings WHERE event_id = ? ORDER BY date DESC LIMIT 1",
        (eid,),
    ).fetchone()
    db.close()
    transcript = load_transcript(eid)
    transcript_present = transcript is not None
    payload = {
        "event_id": eid,
        "meeting_row": dict(row) if row else None,
        "transcript_available": transcript_present,
    }
    if transcript_present:
        payload["transcript_word_count"] = transcript.get("word_count")
        payload["transcript_utterance_count"] = len(transcript.get("utterances") or [])
        payload["transcript_date"] = transcript.get("date")
        payload["transcript_title"] = transcript.get("title")
    out(payload)


def cmd_get_transcript(args):
    if not args:
        die("usage: get_transcript EVENT_ID [--max-chars N]")
    eid = args[0]
    max_chars = 90000
    if "--max-chars" in args:
        max_chars = int(args[args.index("--max-chars") + 1])
    t = load_transcript(eid)
    if not t:
        die(f"no transcript for {eid}")
    full_text = t.get("full_text", "") or ""
    if len(full_text) > max_chars:
        full_text = full_text[:max_chars] + "\n\n[truncated]"
    payload = {
        "event_id": eid,
        "date": t.get("date"),
        "title": t.get("title"),
        "word_count": t.get("word_count"),
        "speaker_count": t.get("speaker_count"),
        "full_text": full_text,
        "utterance_count": len(t.get("utterances") or []),
    }
    out(payload)


def cmd_get_quote_pool(args):
    if not args:
        die("usage: get_quote_pool EVENT_ID [--limit N] [--min-chars N] [--max-chars N]")
    eid = args[0]
    limit = 80
    min_chars = 60
    max_chars = 420
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])
    if "--min-chars" in args:
        min_chars = int(args[args.index("--min-chars") + 1])
    if "--max-chars" in args:
        max_chars = int(args[args.index("--max-chars") + 1])
    t = load_transcript(eid)
    if not t:
        die(f"no transcript for {eid}")
    pool = build_quote_pool(t, limit=limit, min_chars=min_chars, max_chars=max_chars)
    out({"event_id": eid, "count": len(pool), "quotes": pool})


def cmd_verify_quote(args):
    if len(args) < 3:
        die('usage: verify_quote EVENT_ID TS "TEXT"')
    eid = args[0]
    try:
        ts = int(args[1])
    except ValueError:
        die("TS must be an integer")
    text = " ".join(args[2:])
    t = load_transcript(eid)
    if not t:
        die(f"no transcript for {eid}")
    pool = build_quote_pool(t, limit=999, min_chars=1, max_chars=5000)
    entry = next((q for q in pool if q["ts"] == ts), None)
    if entry is None:
        out({"ok": False, "reason": "timestamp not in pool", "pool_text": None})
        return
    norm_a = normalize_quote(text)
    norm_b = normalize_quote(entry["text"])
    df_a = deflutter(norm_a)
    df_b = deflutter(norm_b)
    match = (
        norm_a in norm_b or norm_b in norm_a
        or df_a in df_b or df_b in df_a
    )
    out({
        "ok": bool(match),
        "reason": "match" if match else "text differs from pool entry",
        "pool_text": entry["text"],
        "pool_speaker": entry["speaker"],
    })


def cmd_search_references(args):
    if not args:
        die("usage: search_references QUERY [--limit N]")
    query_parts = []
    limit = 10
    skip = False
    for i, a in enumerate(args):
        if skip:
            skip = False
            continue
        if a == "--limit":
            limit = int(args[i + 1])
            skip = True
        else:
            query_parts.append(a)
    query = " ".join(query_parts)
    if not query:
        die("empty query")
    try:
        from search import rag_search
    except Exception as e:
        die(f"rag_search import failed: {e}")
    try:
        hits = rag_search(query, limit=limit * 3)
    except Exception as e:
        die(f"rag_search failed: {e}")

    db = get_db()
    seen = set()
    results = []
    for h in hits:
        raw_doc = h.get("doc_id") or ""
        clean_doc = raw_doc.split("-")[0] if raw_doc else ""
        if not clean_doc:
            continue
        chunk_date = h.get("date") or "1970-01-01"
        mtg = db.execute(
            "SELECT id, headline, quick_summary, date, committee "
            "FROM meetings WHERE event_id = ? "
            "ORDER BY ABS(julianday(date) - julianday(?)) LIMIT 1",
            (clean_doc, chunk_date),
        ).fetchone()
        if not mtg or not mtg["id"]:
            continue
        if mtg["id"] in seen:
            continue
        seen.add(mtg["id"])
        results.append({
            "meeting_id": mtg["id"],
            "event_id": clean_doc,
            "date": mtg["date"],
            "committee": mtg["committee"],
            "headline": mtg["headline"] or "",
            "quick_summary": mtg["quick_summary"] or "",
            "snippet": (h.get("content") or "")[:320],
            "speaker": h.get("speaker") or "",
            "url": f"/article/{mtg['id']}",
        })
        if len(results) >= limit:
            break
    db.close()
    out({"query": query, "count": len(results), "results": results})


def cmd_recent_meetings(args):
    committee = None
    limit = 20
    skip = False
    for i, a in enumerate(args):
        if skip:
            skip = False
            continue
        if a == "--committee":
            committee = args[i + 1]
            skip = True
        elif a == "--limit":
            limit = int(args[i + 1])
            skip = True
    db = get_db()
    if committee:
        rows = db.execute(
            "SELECT id, event_id, date, committee, headline FROM meetings "
            "WHERE committee = ? ORDER BY date DESC LIMIT ?",
            (committee, limit),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT id, event_id, date, committee, headline FROM meetings "
            "ORDER BY date DESC LIMIT ?",
            (limit,),
        ).fetchall()
    db.close()
    out({"count": len(rows), "meetings": [dict(r) for r in rows]})


def fetch_champds_event(eid):
    with urllib.request.urlopen(CHAMPDS_API.format(eid=eid), timeout=20) as r:
        return json.loads(r.read())


def download_attachment(location, filename, sink_path):
    url = f"{CHAMPDS_ATT_BASE}/{location}/{filename}"
    req = urllib.request.Request(url, headers={"User-Agent": "croton-news-packet-fetcher/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    with open(sink_path, "wb") as f:
        f.write(data)
    return len(data)


def extract_pdf_text(path, max_chars=30000):
    try:
        import pymupdf
    except Exception as e:
        return {"error": f"pymupdf not available: {e}"}
    try:
        doc = pymupdf.open(path)
    except Exception as e:
        return {"error": f"pymupdf open failed: {e}"}
    parts = []
    for page in doc:
        parts.append(page.get_text())
    full = "\n\n".join(parts)
    truncated = False
    if len(full) > max_chars:
        full = full[:max_chars] + "\n\n[...truncated...]"
        truncated = True
    return {
        "pages": doc.page_count,
        "chars": len(full),
        "truncated": truncated,
        "text": full,
    }


# ── OCR pipeline (local tesseract, no cloud) ────────────────────────

def ocr_pdf(pdf_path, dpi=300, psm=6, max_pages=30):
    """Render PDF pages with pdftoppm, OCR each with tesseract.
    Returns (text, pages_ocred, pages_total)."""
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        prefix = os.path.join(tmp, "page")
        # Render
        r = subprocess.run(
            ["pdftoppm", "-r", str(dpi), "-png", pdf_path, prefix,
             "-l", str(max_pages)],
            capture_output=True, timeout=300,
        )
        if r.returncode != 0:
            return "", 0, 0
        pages = sorted(f for f in os.listdir(tmp) if f.endswith(".png"))
        parts = []
        for page in pages:
            r = subprocess.run(
                ["tesseract", os.path.join(tmp, page), "-",
                 "--psm", str(psm), "-l", "eng"],
                capture_output=True, text=True, timeout=60,
            )
            if r.returncode == 0:
                parts.append(r.stdout)
        return "\n\n".join(parts), len(parts), len(pages)


_ENTITY_VOCAB_CACHE = None

# Strong single-word tokens that are village-specific and unlikely false positives
_SINGLE_WORD_SEED = [
    "Croton", "Hudson", "Westchester", "Ossining", "Cortlandt", "Harmon",
    "Riverside", "Truesdale", "Elmore", "Nordica", "Radnor", "Glengary",
    "Pugh", "Nicholson", "Healy", "Natopoulos", "Gallelli", "Simon",
    "Nachseller", "Falkner", "Pecora", "DiSanto", "Slippen", "Krisky",
    "Olcott", "Senasqua", "Gouveia", "Kellerhouse", "Axon", "Lexipol",
    "Deepgram", "ChampDS", "Westchester",
]


def _build_entity_vocab(db):
    global _ENTITY_VOCAB_CACHE
    if _ENTITY_VOCAB_CACHE is not None:
        return _ENTITY_VOCAB_CACHE
    entities = []
    try:
        rows = db.execute(
            "SELECT DISTINCT name FROM entities "
            "WHERE COALESCE(mention_count, 0) >= 3 "
            "AND type IN ('person','organization','location','topic') "
            "AND length(name) BETWEEN 4 AND 60"
        ).fetchall()
        entities = [r[0].strip() for r in rows if r[0]]
    except Exception:
        pass
    hardcoded = [
        "Croton-on-Hudson", "Village of Croton-on-Hudson", "Village Board",
        "Village Manager", "Village Clerk", "Village Attorney",
        "Board of Trustees", "Planning Board", "Zoning Board of Appeals",
        "Waterfront Advisory Committee", "Conservation Advisory Council",
        "Recreation Advisory Committee", "Sustainability Committee",
        "Advisory Board on the Visual Environment", "Police Advisory Committee",
        "Department of Public Works", "Croton Fire Department",
        "Brian Pugh", "Nora Nicholson", "Bryan Healy", "Nick Natopoulos",
        "Len Simon", "Ann Gallelli", "Stacy Nachseller",
        "Van Wyck Street", "South Riverside Avenue", "Croton Point Avenue",
        "Gouveia Park", "Half Moon Bay", "Temple Israel",
        "Kellerhouse Municipal Building", "Croton Landing",
        "Chapter 179", "Chapter 230", "Local Law", "Village Code",
    ]
    entities.extend(hardcoded)
    entities.extend(_SINGLE_WORD_SEED)
    # Dedupe preserving order
    seen = set()
    vocab = []
    for e in entities:
        if e and e not in seen:
            seen.add(e)
            vocab.append(e)
    _ENTITY_VOCAB_CACHE = vocab
    return vocab


def _normalize_for_match(s):
    """Normalize hyphen/space differences for fuzzy comparison."""
    return re.sub(r"[\s\-]+", " ", s.lower()).strip()


# Proper-noun pattern that allows lowercase connectors in the middle.
# Sequence starts with a Title-Cased word, can have Title-Cased words or
# lowercase connectors like "of"/"on"/"the"/"and" in the middle, and must
# end with another Title-Cased word (so "The Village" matches but "The of"
# does not).
_CONNECTOR = r"(?:of|on|the|and|de|la|von|van|du|for|at|in|to)"
_TITLE = r"[A-Z][A-Za-z\'\-]{1,30}"
_PROPER_NOUN_RE = re.compile(
    rf"\b{_TITLE}(?:\s+(?:{_CONNECTOR}|{_TITLE})){{0,6}}\b"
)

_SINGLE_PROPER_RE = re.compile(r"\b[A-Z][A-Za-z\'\-]{2,30}\b")


def correct_ocr_text(text, vocab, cutoff=0.87, single_cutoff=0.90):
    """Replace near-matches of vocab entries in the text.
    Returns (text, n_replacements, example_fixes)."""
    from difflib import SequenceMatcher, get_close_matches

    vocab_norm = {_normalize_for_match(v): v for v in vocab}
    multi_vocab = [v for v in vocab if " " in v or "-" in v]
    single_vocab = [v for v in vocab if " " not in v and "-" not in v]

    replacements = {}
    example_fixes = []

    # Pass 1: multi-word proper-noun phrases
    for match in _PROPER_NOUN_RE.finditer(text):
        candidate = match.group(0)
        if len(candidate) < 6:
            continue
        cand_norm = _normalize_for_match(candidate)
        if cand_norm in vocab_norm:
            canon = vocab_norm[cand_norm]
            if candidate != canon:
                replacements[candidate] = canon
            continue
        if candidate in replacements:
            continue
        # Fuzzy against normalized multi-word vocab
        best = None
        best_ratio = 0
        for v in multi_vocab:
            v_norm = _normalize_for_match(v)
            # Only compare if word counts are close to keep cost low
            if abs(len(v_norm.split()) - len(cand_norm.split())) > 1:
                continue
            ratio = SequenceMatcher(None, cand_norm, v_norm).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best = v
        if best and best_ratio >= cutoff:
            replacements[candidate] = best

    # Pass 2: single-word proper nouns (stricter cutoff to avoid false positives)
    for match in _SINGLE_PROPER_RE.finditer(text):
        candidate = match.group(0)
        if candidate in single_vocab or candidate in replacements:
            continue
        close = get_close_matches(candidate, single_vocab, n=1, cutoff=single_cutoff)
        if close and close[0] != candidate:
            # Only fix if candidate is clearly a corruption, not a legit word
            if len(candidate) >= 4:
                replacements[candidate] = close[0]

    if replacements:
        # Sort by length desc so long phrases get replaced first
        for bad in sorted(replacements, key=lambda s: -len(s)):
            good = replacements[bad]
            new_text = re.sub(
                r"\b" + re.escape(bad) + r"\b",
                good.replace("\\", r"\\"),
                text,
            )
            if new_text != text:
                text = new_text
                if len(example_fixes) < 5:
                    example_fixes.append(f"{bad} -> {good}")
    return text, len(replacements), example_fixes


def _process_packet(db, eid, max_chars=12000, max_pdfs=20, force=False):
    """Download + extract all attachments for an event, cache in DB.
    Returns structured dict with agenda items and attachment data."""
    data = fetch_champds_event(eid)
    ev = data.get("Event", {}) or {}
    if not ev:
        return None

    boilerplate = _get_boilerplate(db)

    os.makedirs(PACKET_CACHE_DIR, exist_ok=True)
    cache_dir = os.path.join(PACKET_CACHE_DIR, eid)
    os.makedirs(cache_dir, exist_ok=True)

    items_out = []
    pdf_extracted = 0
    pdf_skipped = 0
    pdf_cached = 0

    # Flatten the agenda tree: each item may have Children with their own
    # Attachments. ChampDS nests e.g. "New Business" → "[sub-item X]" with
    # all the real document attachments on the sub-item, not the parent.
    def walk_agenda(items, parent_title=""):
        out = []
        for it in items or []:
            t = (it.get("Title") or "").strip()
            if parent_title:
                combined = f"{parent_title} — {t}" if t else parent_title
            else:
                combined = t
            out.append({
                "title": combined or parent_title or "(untitled)",
                "attachments": it.get("Attachments") or [],
            })
            kids = it.get("Children") or []
            if kids:
                out.extend(walk_agenda(kids, combined))
        return out

    flat_items = walk_agenda((data.get("Agenda", {}) or {}).get("AgendaItems", []))

    # Also include any top-level Agenda.Attachments (general packet attachments)
    top_atts = (data.get("Agenda", {}) or {}).get("Attachments") or []
    if top_atts:
        flat_items.insert(0, {
            "title": "General Attachments",
            "attachments": top_atts,
        })

    for item in flat_items:
        title = item["title"]
        if not title:
            continue
        atts_out = []
        for at in (item.get("attachments") or []):
            nick = (at.get("MediaNickName") or "").strip()
            mfile = at.get("MediaFileName") or ""
            mloc = at.get("MediaFileLocation") or ""
            mtype = at.get("MediaTypeID")
            size = at.get("SizeBytes") or 0
            att_info = {
                "name": nick,
                "size_bytes": size,
                "media_type_id": mtype,
            }

            if mtype == 2 or mfile.startswith("http"):
                att_info["url"] = mfile
                att_info["kind"] = "external_url"
                atts_out.append(att_info)
                continue

            if not mfile or not mloc:
                continue

            att_info["url"] = f"{CHAMPDS_ATT_BASE}/{mloc}/{mfile}"
            is_pdf = mfile.lower().endswith(".pdf")

            # Check cache. Even with --force, preserve successful OCR results
            # (pdf_ocr) — force is for re-running pymupdf on new extractor logic,
            # not for nuking carefully-recovered OCR text.
            existing = db.execute(
                "SELECT kind, pages, char_count, truncated, text, error "
                "FROM packet_pdfs WHERE event_id=? AND media_file=?",
                (eid, mfile),
            ).fetchone()

            cached = None if force else existing

            if existing and existing["kind"] == "pdf_ocr" and existing["text"]:
                # Preserve OCR'd content unconditionally — force doesn't touch it.
                att_info["kind"] = "pdf_ocr"
                att_info["pages"] = existing["pages"]
                att_info["chars"] = existing["char_count"]
                att_info["text"] = existing["text"][:max_chars]
                att_info["cached"] = True
                pdf_cached += 1
                atts_out.append(att_info)
                continue

            if cached and cached["kind"] == "pdf" and cached["text"]:
                att_info["kind"] = "pdf"
                att_info["pages"] = cached["pages"]
                att_info["chars"] = cached["char_count"]
                att_info["truncated"] = bool(cached["truncated"])
                att_info["text"] = cached["text"][:max_chars]
                att_info["cached"] = True
                pdf_cached += 1
                atts_out.append(att_info)
                continue

            if not is_pdf:
                att_info["kind"] = "file_other"
                atts_out.append(att_info)
                continue

            if pdf_extracted + pdf_cached >= max_pdfs:
                att_info["kind"] = "pdf_skipped_cap"
                pdf_skipped += 1
                atts_out.append(att_info)
                continue

            local = os.path.join(cache_dir, mfile)
            try:
                if not os.path.exists(local):
                    download_attachment(mloc, mfile, local)
                extracted = extract_pdf_text(local, max_chars=max_chars)
                if "error" in extracted:
                    att_info["extract_error"] = extracted["error"]
                    att_info["kind"] = "pdf_error"
                    db.execute(
                        "INSERT OR REPLACE INTO packet_pdfs "
                        "(event_id, media_file, location, nickname, agenda_item_title, "
                        "kind, size_bytes, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (eid, mfile, mloc, nick, title, "pdf_error", size, extracted["error"]),
                    )
                else:
                    raw_text = extracted["text"]
                    is_scanned = extracted["chars"] <= 20
                    if not is_scanned and boilerplate:
                        raw_text, _stripped = clean_packet_text(raw_text, boilerplate)
                    kind = "pdf" if not is_scanned else "pdf_scanned"
                    att_info["pages"] = extracted["pages"]
                    att_info["chars"] = len(raw_text)
                    att_info["truncated"] = extracted["truncated"]
                    att_info["text"] = raw_text
                    att_info["kind"] = kind
                    db.execute(
                        "INSERT OR REPLACE INTO packet_pdfs "
                        "(event_id, media_file, location, nickname, agenda_item_title, "
                        "kind, size_bytes, pages, char_count, truncated, text) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (eid, mfile, mloc, nick, title, kind, size,
                         extracted["pages"], len(raw_text),
                         1 if extracted["truncated"] else 0, raw_text),
                    )
                    if kind == "pdf":
                        pdf_extracted += 1
            except Exception as e:
                att_info["fetch_error"] = str(e)
                att_info["kind"] = "pdf_fetch_error"
                db.execute(
                    "INSERT OR REPLACE INTO packet_pdfs "
                    "(event_id, media_file, location, nickname, agenda_item_title, "
                    "kind, size_bytes, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (eid, mfile, mloc, nick, title, "pdf_fetch_error", size, str(e)),
                )
            atts_out.append(att_info)
        items_out.append({"title": title, "attachments": atts_out})
    db.commit()

    return {
        "event_id": eid,
        "date": (ev.get("EventDateTimeCustomerLocal") or "")[:10],
        "title": ev.get("EventTitle") or "",
        "agenda_item_count": len(items_out),
        "pdfs_extracted": pdf_extracted,
        "pdfs_cached": pdf_cached,
        "pdfs_skipped": pdf_skipped,
        "agenda_items": items_out,
    }


def cmd_fetch_agenda_packet(args):
    if not args:
        die("usage: fetch_agenda_packet EVENT_ID [--max-chars-per-pdf N] [--max-pdfs N] [--force]")
    eid = args[0]
    max_chars = 12000
    max_pdfs = 20
    force = "--force" in args
    if "--max-chars-per-pdf" in args:
        max_chars = int(args[args.index("--max-chars-per-pdf") + 1])
    if "--max-pdfs" in args:
        max_pdfs = int(args[args.index("--max-pdfs") + 1])

    db = get_db()
    payload = _process_packet(db, eid, max_chars=max_chars, max_pdfs=max_pdfs, force=force)
    db.close()
    if payload is None:
        die(f"no ChampDS event for {eid}")
    out(payload)


def cmd_packet_backfill(args):
    """Iterate a range of event IDs, extracting all PDFs to the cache."""
    start = 1080
    end = 1200
    if "--from" in args:
        start = int(args[args.index("--from") + 1])
    if "--to" in args:
        end = int(args[args.index("--to") + 1])

    db = get_db()
    results = []
    total_extracted = 0
    total_cached = 0
    for eid_int in range(start, end + 1):
        eid = str(eid_int)
        try:
            payload = _process_packet(db, eid, max_chars=40000, max_pdfs=60)
        except Exception as e:
            print(json.dumps({"event_id": eid, "error": str(e)}), flush=True)
            continue
        if not payload:
            continue
        total_extracted += payload["pdfs_extracted"]
        total_cached += payload["pdfs_cached"]
        summary = {
            "event_id": eid,
            "title": payload["title"][:50],
            "date": payload["date"],
            "extracted": payload["pdfs_extracted"],
            "cached": payload["pdfs_cached"],
        }
        results.append(summary)
        print(json.dumps(summary), flush=True)
    db.close()
    out({
        "range": f"{start}-{end}",
        "events_processed": len(results),
        "total_newly_extracted": total_extracted,
        "total_cached_already": total_cached,
    })


def cmd_ocr_scanned(args):
    """Find all pdf_scanned rows and OCR them locally with tesseract.
    Applies entity-spell-correction against the known-entities vocab."""
    dry_run = "--dry-run" in args
    limit = None
    event_filter = None
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])
    if "--event" in args:
        event_filter = args[args.index("--event") + 1]

    db = get_db()
    vocab = _build_entity_vocab(db)

    query = "SELECT id, event_id, media_file, location, nickname, size_bytes FROM packet_pdfs WHERE kind='pdf_scanned'"
    params = ()
    if event_filter:
        query += " AND event_id = ?"
        params = (event_filter,)
    query += " ORDER BY CAST(event_id AS INTEGER) DESC, id ASC"
    if limit:
        query += f" LIMIT {limit}"

    rows = db.execute(query, params).fetchall()
    print(json.dumps({
        "vocab_size": len(vocab),
        "candidates": len(rows),
        "dry_run": dry_run,
    }), flush=True)

    stats = {
        "processed": 0,
        "recovered": 0,
        "still_empty": 0,
        "errors": 0,
        "replacements_total": 0,
    }

    for row in rows:
        rid = row["id"]
        eid = row["event_id"]
        mfile = row["media_file"]
        mloc = row["location"] or ""
        nick = row["nickname"] or ""
        local = os.path.join(PACKET_CACHE_DIR, eid, mfile)

        if not os.path.exists(local):
            try:
                os.makedirs(os.path.dirname(local), exist_ok=True)
                download_attachment(mloc, mfile, local)
            except Exception as e:
                stats["errors"] += 1
                print(json.dumps({"id": rid, "event_id": eid, "mfile": mfile, "error": f"download: {e}"}), flush=True)
                continue

        try:
            text, ocred, total = ocr_pdf(local)
        except Exception as e:
            stats["errors"] += 1
            print(json.dumps({"id": rid, "event_id": eid, "mfile": mfile, "error": f"ocr: {e}"}), flush=True)
            continue

        if not text or len(text.strip()) < 30:
            stats["still_empty"] += 1
            print(json.dumps({
                "id": rid, "event_id": eid, "nick": nick[:40],
                "pages_ocred": ocred, "pages_total": total,
                "chars": len(text), "status": "empty",
            }), flush=True)
            continue

        corrected, n_fixes, examples = correct_ocr_text(text, vocab)
        boilerplate = _get_boilerplate(db)
        if boilerplate:
            corrected, _stripped = clean_packet_text(corrected, boilerplate)
        stats["replacements_total"] += n_fixes
        stats["processed"] += 1
        stats["recovered"] += 1

        print(json.dumps({
            "id": rid, "event_id": eid, "nick": nick[:40],
            "pages_ocred": ocred, "pages_total": total,
            "chars": len(corrected), "fixes": n_fixes,
            "examples": examples,
            "preview": corrected[:160].replace("\n", " "),
        }), flush=True)

        if not dry_run:
            db.execute(
                "UPDATE packet_pdfs SET kind='pdf_ocr', pages=?, char_count=?, text=?, error=NULL WHERE id=?",
                (total, len(corrected), corrected, rid),
            )
            db.commit()

    db.close()
    print(json.dumps({"done": True, **stats}), flush=True)


def _build_boilerplate_set(db, min_events=15, min_len=8, max_len=250):
    """Re-extract raw text from the cached PDF files on disk and return the
    set of lines that appear in >=min_events distinct events. Reads from
    disk rather than the DB so the set is computed from GROUND TRUTH text,
    not from text that may already have been cleaned."""
    try:
        import pymupdf
    except Exception:
        return set()

    rows = db.execute(
        "SELECT event_id, media_file FROM packet_pdfs "
        "WHERE kind='pdf' AND text IS NOT NULL"
    ).fetchall()

    line_events = {}
    for row in rows:
        eid = row[0]
        mfile = row[1]
        path = os.path.join(PACKET_CACHE_DIR, eid, mfile)
        if not os.path.exists(path):
            continue
        try:
            doc = pymupdf.open(path)
            text = "\n\n".join(p.get_text() for p in doc)
            doc.close()
        except Exception:
            continue
        seen_in_doc = set()
        for raw in text.splitlines():
            line = raw.strip()
            if len(line) < min_len or len(line) > max_len:
                continue
            if re.match(r"^(page\s+\d+(\s+of\s+\d+)?|pg\s+\d+|\d+\s*|\d+/\d+)$", line.lower()):
                continue
            if line in seen_in_doc:
                continue
            seen_in_doc.add(line)
            line_events.setdefault(line, set()).add(eid)
    return {line for line, events in line_events.items() if len(events) >= min_events}


def clean_packet_text(text, boilerplate):
    """Remove any line whose stripped form is in the boilerplate set.
    Collapse runs of blank lines. Returns (clean_text, n_lines_stripped)."""
    out = []
    stripped = 0
    prev_blank = False
    for raw in text.splitlines():
        line = raw.strip()
        # Strip ubiquitous page numbers standalone
        if re.match(r"^(page\s+\d+(\s+of\s+\d+)?|\d+\s*$)$", line.lower()):
            stripped += 1
            continue
        if line in boilerplate:
            stripped += 1
            continue
        if not line:
            if prev_blank:
                continue
            prev_blank = True
        else:
            prev_blank = False
        out.append(raw)
    return "\n".join(out).strip(), stripped


def cmd_dedupe_text(args):
    """Build a boilerplate set from the corpus and strip it from all packet rows."""
    dry_run = "--dry-run" in args
    min_events = 15
    if "--min-events" in args:
        min_events = int(args[args.index("--min-events") + 1])

    db = get_db()
    print(json.dumps({"stage": "building_boilerplate_set", "min_events": min_events}), flush=True)
    boilerplate = _build_boilerplate_set(db, min_events=min_events)
    print(json.dumps({"stage": "boilerplate_built", "lines": len(boilerplate)}), flush=True)

    rows = db.execute(
        "SELECT id, event_id, nickname, kind, char_count, text FROM packet_pdfs "
        "WHERE kind IN ('pdf', 'pdf_ocr') AND text IS NOT NULL"
    ).fetchall()

    total_chars_before = 0
    total_chars_after = 0
    total_rows_changed = 0
    total_lines_stripped = 0

    for row in rows:
        rid = row["id"]
        before = row["text"] or ""
        if not before:
            continue
        after, stripped = clean_packet_text(before, boilerplate)
        if stripped == 0 or after == before:
            total_chars_before += len(before)
            total_chars_after += len(before)
            continue
        total_chars_before += len(before)
        total_chars_after += len(after)
        total_rows_changed += 1
        total_lines_stripped += stripped
        if not dry_run:
            db.execute(
                "UPDATE packet_pdfs SET text=?, char_count=? WHERE id=?",
                (after, len(after), rid),
            )
    if not dry_run:
        db.commit()
    db.close()

    print(json.dumps({
        "done": True,
        "dry_run": dry_run,
        "rows_changed": total_rows_changed,
        "lines_stripped_total": total_lines_stripped,
        "chars_before": total_chars_before,
        "chars_after": total_chars_after,
        "chars_removed": total_chars_before - total_chars_after,
        "pct_reduction": round(100 * (1 - total_chars_after / total_chars_before), 1) if total_chars_before else 0,
    }), flush=True)


def cmd_recorrect_ocr(args):
    """Re-apply entity spell correction to all pdf_ocr rows without re-running tesseract.
    Useful after improving the vocab or the correction logic."""
    dry_run = "--dry-run" in args
    limit = None
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])

    db = get_db()
    vocab = _build_entity_vocab(db)

    query = "SELECT id, event_id, nickname, text FROM packet_pdfs WHERE kind='pdf_ocr' AND text IS NOT NULL ORDER BY id"
    if limit:
        query += f" LIMIT {limit}"
    rows = db.execute(query).fetchall()

    print(json.dumps({"vocab_size": len(vocab), "candidates": len(rows), "dry_run": dry_run}), flush=True)

    total_fixes = 0
    total_changed = 0
    for row in rows:
        rid = row["id"]
        eid = row["event_id"]
        nick = row["nickname"] or ""
        original = row["text"] or ""
        corrected, n_fixes, examples = correct_ocr_text(original, vocab)
        if n_fixes == 0 or corrected == original:
            continue
        total_fixes += n_fixes
        total_changed += 1
        print(json.dumps({
            "id": rid, "event_id": eid, "nick": nick[:40],
            "fixes": n_fixes,
            "examples": examples,
        }), flush=True)
        if not dry_run:
            db.execute(
                "UPDATE packet_pdfs SET text=?, char_count=? WHERE id=?",
                (corrected, len(corrected), rid),
            )
    if not dry_run:
        db.commit()
    db.close()
    print(json.dumps({"done": True, "rows_changed": total_changed, "total_fixes": total_fixes}), flush=True)


def cmd_save_article(args):
    if not args:
        die("usage: save_article EVENT_ID  (reads JSON from stdin)")
    eid = args[0]
    try:
        payload = json.loads(sys.stdin.read())
    except Exception as e:
        die(f"stdin is not valid JSON: {e}")
    required = ("headline", "quick_summary", "key_actions", "article")
    missing = [k for k in required if k not in payload]
    if missing:
        die(f"missing fields: {missing}")

    headline = (payload.get("headline") or "").strip()
    quick_summary = (payload.get("quick_summary") or "").strip()
    key_actions_field = payload.get("key_actions")
    if isinstance(key_actions_field, list):
        key_actions = "\n".join(f"- {x}" for x in key_actions_field)
    else:
        key_actions = str(key_actions_field or "").strip()
    article = (payload.get("article") or "").strip()
    model = payload.get("model") or "glm-5-turbo"

    db = get_db()
    meeting = db.execute(
        "SELECT id FROM meetings WHERE event_id = ? ORDER BY date DESC LIMIT 1",
        (eid,),
    ).fetchone()
    t = load_transcript(eid)
    date = t.get("date") if t else None
    committee = t.get("title") if t else None

    if meeting:
        db.execute(
            "UPDATE meetings SET headline=?, quick_summary=?, complete_summary=?, "
            "article=?, article_model=?, article_generated_at=datetime('now') "
            "WHERE event_id=?",
            (headline, quick_summary, key_actions, article, model, eid),
        )
        mtg_id = meeting["id"]
    else:
        cur = db.execute(
            "INSERT INTO meetings "
            "(date, committee, event_id, headline, quick_summary, complete_summary, "
            "article, has_transcript, has_video, has_audio, article_model, "
            "article_generated_at, word_count, speaker_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, 0, ?, datetime('now'), ?, ?)",
            (
                date, committee, eid, headline, quick_summary, key_actions,
                article, model,
                (t or {}).get("word_count"),
                (t or {}).get("speaker_count"),
            ),
        )
        mtg_id = cur.lastrowid
    db.commit()
    db.close()
    out({"ok": True, "meeting_id": mtg_id, "event_id": eid, "article_chars": len(article)})


COMMANDS = {
    "meeting_info": cmd_meeting_info,
    "get_transcript": cmd_get_transcript,
    "get_quote_pool": cmd_get_quote_pool,
    "verify_quote": cmd_verify_quote,
    "search_references": cmd_search_references,
    "recent_meetings": cmd_recent_meetings,
    "fetch_agenda_packet": cmd_fetch_agenda_packet,
    "packet_backfill": cmd_packet_backfill,
    "ocr_scanned": cmd_ocr_scanned,
    "recorrect_ocr": cmd_recorrect_ocr,
    "dedupe_text": cmd_dedupe_text,
    "save_article": cmd_save_article,
}


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    sub = sys.argv[1]
    if sub not in COMMANDS:
        die(f"unknown subcommand: {sub}. Available: {', '.join(COMMANDS)}")
    COMMANDS[sub](sys.argv[2:])


if __name__ == "__main__":
    main()
