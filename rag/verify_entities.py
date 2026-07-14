#!/usr/bin/env python3
"""Verify entity spellings against authoritative sources.

A person entity is VERIFIED when its name appears in official minutes,
agenda JSON, or packet PDF text — sources written by clerks, not by
speech-to-text. Unverified entities render with "(sp?)" on the site and
carry a reader-correction form (the entities table is largely built from
Deepgram/caption transcripts, so misspellings like "Sabrizi" for Sibrizzi
propagate; see AUDIT-2026-07-13.md round 2).

Also reports authoritative names found in minutes that are MISSING from
entities entirely (e.g. Richard Wetherbee — never transcribed correctly,
so never became an entity).

Usage:
  verify_entities.py            # verify + report (writes verified column)
  verify_entities.py --dry-run  # report only
"""
import re
import sqlite3
import sys
from collections import Counter

RAG_DB = __file__.rsplit("/", 1)[0] + "/rag.db"

STOP = {
    "Board", "Village", "School", "District", "Meeting", "Motion", "Minutes",
    "Chairman", "Chairperson", "Trustee", "Mayor", "Public", "Regular",
    "Special", "Work", "Session", "New", "York", "Absent", "Present",
    "Action", "Roll", "Call", "The", "High", "Middle", "Elementary",
    "Superintendent", "Doctor", "Deputy", "Attorney", "Manager", "Engineer",
    "President", "Vice", "Member", "Members", "Item", "Report", "Committee",
    "Council", "Commission", "Education", "Croton", "Harmon", "Hudson",
    "Recommendation", "Resolution", "Approval", "Business", "Street",
    "Avenue", "Drive", "Road", "Court", "Recognition", "Tenure", "Grant",
}


def normalize(s):
    return re.sub(r"[^a-z ]+", "", (s or "").lower())


def main():
    dry = "--dry-run" in sys.argv
    db = sqlite3.connect(RAG_DB)
    db.row_factory = sqlite3.Row

    cols = [r["name"] for r in db.execute("PRAGMA table_info(entities)")]
    if "verified" not in cols and not dry:
        db.execute("ALTER TABLE entities ADD COLUMN verified INTEGER DEFAULT 0")

    # authoritative corpus: minutes + agendas + packet text
    corpus_parts = []
    for (t,) in db.execute("SELECT minutes_text FROM meetings WHERE minutes_text IS NOT NULL"):
        corpus_parts.append(t)
    for (t,) in db.execute("SELECT agenda_json FROM meetings WHERE agenda_json IS NOT NULL"):
        corpus_parts.append(t)
    for (t,) in db.execute("SELECT COALESCE(text,'') FROM packet_pdfs"):
        corpus_parts.append(t)
    corpus = " ".join(corpus_parts)
    corpus_norm = normalize(corpus)

    people = db.execute("SELECT id, name, mention_count FROM entities WHERE type='person'").fetchall()
    verified, unverified = 0, []
    for p in people:
        ok = normalize(p["name"]) in corpus_norm
        if not dry:
            db.execute("UPDATE entities SET verified = ? WHERE id = ?", (1 if ok else 0, p["id"]))
        if ok:
            verified += 1
        else:
            unverified.append((p["name"], p["mention_count"]))

    # authoritative names missing from entities
    counts = Counter()
    for m in re.finditer(r"\b([A-Z][a-z]{2,})\s+([A-Z][a-z]{2,})\b", corpus):
        if m.group(1) in STOP or m.group(2) in STOP:
            continue
        counts[f"{m.group(1)} {m.group(2)}"] += 1
    known = {normalize(p["name"]) for p in people}
    missing = [(n, c) for n, c in counts.most_common(200)
               if c >= 3 and normalize(n) not in known]

    if not dry:
        db.commit()
    print(f"person entities: {len(people)}; verified: {verified}; "
          f"unverified (will show (sp?)): {len(unverified)}")
    print("\nTop unverified (name | transcript mentions):")
    for n, c in sorted(unverified, key=lambda x: -x[1])[:15]:
        print(f"  {n} | {c}")
    print("\nAuthoritative names MISSING from entities (name | minutes/agenda count):")
    for n, c in missing[:20]:
        print(f"  {n} | {c}")
    db.close()


if __name__ == "__main__":
    main()
