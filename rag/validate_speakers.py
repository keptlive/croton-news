#!/usr/bin/env python3
"""Deterministic speaker-label validator — runs BEFORE agents consume a transcript.

Every personal-name speaker label in a transcript must be provable from the
meeting's own record:
  * meeting has minutes  -> surname must appear in minutes_text, agenda_json,
    or be spoken aloud in the transcript content (self-introduced commenters).
  * meeting has no minutes yet -> surname must appear in agenda_json, spoken
    content, recent minutes for the same committee (120 days), or a verified
    entity.

Why: the Village Attorney changed on 2026-06-24 (Joshua Subin -> Lori Lee
Dickson). The enricher's stale roster labeled the new attorney's voice
"Joshua Subin" on two transcripts; the name appeared nowhere in the spoken
audio or the minutes, and it reached a published article. A speaker label is
an enricher ASSERTION, not evidence — this check demands evidence.

Usage:
    python3 validate_speakers.py --meeting 143 [--fix]
    python3 validate_speakers.py --event 1174 [--fix]
    python3 validate_speakers.py --recent 30 [--fix]

--fix relabels violating speakers to "Unknown Speaker" in BOTH the chunks
table and the transcript JSON file (speaker_map + utterances + full_text),
so downstream agents can never attribute a quote to an unproven name.
Exit codes: 0 = clean (or fixed), 3 = violations found and not fixed.
"""
import argparse, json, os, re, sqlite3, sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RAG_DB = os.path.join(BASE_DIR, "rag.db")
TRANSCRIPTS_DIR = os.path.join(BASE_DIR, "transcripts")

# Labels containing any of these words are roles/placeholders, not personal
# names — they carry no false-fact risk and are skipped.
ROLE_WORDS = {
    "speaker", "unknown", "resident", "village", "deputy", "assistant",
    "attorney", "manager", "clerk", "mayor", "trustee", "chief",
    "superintendent", "engineer", "treasurer", "chair", "chairman",
    "chairperson", "member", "director", "coordinator", "officer",
    "consultant", "applicant", "moderator", "presenter", "interpreter",
    "public", "board", "commissioner", "secretary", "liaison",
}

NAME_TOKEN = re.compile(r"^[A-Z][a-zA-Z'\-]+$")


def is_personal_name(label):
    """True for labels like 'Joshua Subin' — 2+ capitalized tokens, no
    role words, no parentheses/digits."""
    if not label or "(" in label or any(ch.isdigit() for ch in label):
        return False
    tokens = label.split()
    if len(tokens) < 2:
        return False
    if any(t.lower().strip(".,") in ROLE_WORDS for t in tokens):
        return False
    return all(NAME_TOKEN.match(t) for t in tokens)


def surname_in(surname, text):
    if not text:
        return False
    return re.search(r"\b" + re.escape(surname) + r"\b", text, re.I) is not None


def validate_meeting(db, meeting, fix=False):
    """Returns list of violation dicts for one meetings-table row."""
    doc_id = str(meeting["event_id"] or f"meeting-{meeting['id']}")
    speakers = [r[0] for r in db.execute(
        "SELECT DISTINCT speaker FROM chunks WHERE doc_id = ? AND doc_type = 'transcript'",
        (doc_id,)) if r[0]]
    names = [s for s in speakers if is_personal_name(s)]
    if not names:
        return []

    minutes = meeting["minutes_text"] or ""
    agenda = meeting["agenda_json"] or ""
    spoken = "\n".join(r[0] for r in db.execute(
        "SELECT content FROM chunks WHERE doc_id = ? AND doc_type = 'transcript'", (doc_id,)))

    recent_minutes = ""
    verified_entities = set()
    if not minutes:
        recent_minutes = "\n".join(r[0] for r in db.execute(
            "SELECT minutes_text FROM meetings WHERE committee = ? AND minutes_text IS NOT NULL "
            "AND date >= date(?, '-120 day') AND date <= ?",
            (meeting["committee"], meeting["date"], meeting["date"])))
        verified_entities = {r[0].lower() for r in db.execute(
            "SELECT name FROM entities WHERE type = 'person' AND verified = 1")}

    violations = []
    for name in names:
        surname = name.split()[-1]
        if minutes:
            ok = (surname_in(surname, minutes) or surname_in(surname, agenda)
                  or surname_in(surname, spoken))
            vtype = "attendance-mismatch"
            detail = (f"speaker label '{name}' but surname '{surname}' appears in "
                      f"neither this meeting's minutes, agenda, nor spoken content")
        else:
            ok = (surname_in(surname, agenda) or surname_in(surname, spoken)
                  or surname_in(surname, recent_minutes)
                  or name.lower() in verified_entities)
            vtype = "unverifiable-name"
            detail = (f"speaker label '{name}' (no minutes yet) — surname '{surname}' "
                      f"absent from agenda, spoken content, 120-day committee minutes, "
                      f"and verified entities")
        if not ok:
            violations.append({"meeting_id": meeting["id"], "doc_id": doc_id,
                               "type": vtype, "speaker": name, "detail": detail})

    if fix and violations:
        wdb = sqlite3.connect(RAG_DB)
        bad = [v["speaker"] for v in violations]
        for name in bad:
            wdb.execute("UPDATE chunks SET speaker = 'Unknown Speaker' "
                        "WHERE doc_id = ? AND doc_type = 'transcript' AND speaker = ?",
                        (doc_id, name))
        wdb.commit()
        wdb.close()
        # FTS indexes the speaker column — external-content tables need an
        # explicit rebuild after direct UPDATEs.
        wdb = sqlite3.connect(RAG_DB)
        wdb.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
        wdb.commit()
        wdb.close()
        tpath = os.path.join(TRANSCRIPTS_DIR, f"transcript-{doc_id}.json")
        if os.path.exists(tpath):
            d = json.load(open(tpath))
            d["speaker_map"] = {k: ("Unknown Speaker" if v in bad else v)
                                for k, v in (d.get("speaker_map") or {}).items()}
            for u in d.get("utterances", []):
                if u.get("speaker") in bad:
                    u["speaker"] = "Unknown Speaker"
            ft = d.get("full_text") or ""
            for name in bad:
                ft = ft.replace(name, "Unknown Speaker")
            d["full_text"] = ft
            tmp = tpath + ".tmp"
            with open(tmp, "w") as f:
                json.dump(d, f, ensure_ascii=False)
            os.replace(tmp, tpath)
        for v in violations:
            v["fixed"] = True
    return violations


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--meeting", type=int, help="meetings.id")
    g.add_argument("--event", help="chunks doc_id / meetings.event_id")
    g.add_argument("--recent", type=int, help="all transcribed meetings from last N days")
    ap.add_argument("--fix", action="store_true",
                    help="relabel violating speakers to 'Unknown Speaker' in chunks + transcript JSON")
    args = ap.parse_args()

    db = sqlite3.connect(f"file:{RAG_DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    if args.meeting:
        rows = db.execute("SELECT * FROM meetings WHERE id = ?", (args.meeting,)).fetchall()
    elif args.event:
        rows = db.execute("SELECT * FROM meetings WHERE CAST(event_id AS TEXT) = ?",
                          (str(args.event),)).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM meetings WHERE has_transcript = 1 AND date >= date('now', ?)",
            (f"-{args.recent} day",)).fetchall()

    all_violations = []
    for m in rows:
        all_violations.extend(validate_meeting(db, m, fix=args.fix))
    db.close()

    print(json.dumps({"checked": len(rows), "violations": all_violations},
                     indent=2, ensure_ascii=False))
    if all_violations and not args.fix:
        sys.exit(3)


if __name__ == "__main__":
    main()
