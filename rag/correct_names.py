#!/usr/bin/env python3
"""
Minutes-Based Name Correction for croton.news

Corrects misspelled names in the entity database and chunk speaker fields
using the same canonical name logic as enrich_transcript.py.

Minutes (summaries.db index_json) are the source of truth for spellings,
with majority voting to handle LLM extraction inconsistencies.

Usage:
    python3 correct_names.py scan         # Show all mismatches (dry run)
    python3 correct_names.py fix          # Apply corrections to entity DB + chunks
    python3 correct_names.py scan -v      # Verbose: show match scores

This script fixes:
1. Entity names (rag.db entities table)
2. Chunk speaker fields (rag.db chunks table)
3. FTS index (rebuilt after changes)

For transcript speaker_map corrections, use:
    python3 enrich_transcript.py --fix-names
"""

import json
import os
import sqlite3
import sys
from difflib import SequenceMatcher

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RAG_DB = os.path.join(BASE_DIR, "rag.db")

# Import canonical name loading from enrich_transcript
sys.path.insert(0, BASE_DIR)
from enrich_transcript import (
    _load_canonical_names,
    _strip_speaker_title,
    NAME_MATCH_THRESHOLD,
)


def find_entity_corrections(canonical_names, minutes_names, verbose=False):
    """Find entity DB names that need correction per minutes spellings."""
    db = sqlite3.connect(RAG_DB)
    db.row_factory = sqlite3.Row

    entities = {
        r["name"]: r["id"]
        for r in db.execute("SELECT id, name FROM entities WHERE type='person'").fetchall()
    }
    db.close()

    corrections = []
    for ename in sorted(entities):
        if ename in minutes_names:
            continue  # Exact match with minutes, fine

        base = _strip_speaker_title(ename)
        if len(base.split()) < 2:
            continue  # Skip single-word entities

        # Check exact match after title strip
        if base in minutes_names and base != ename:
            corrections.append((ename, base, 1.0, "title-stripped"))
            if verbose:
                print(f"  \"{ename}\" → \"{base}\" (1.00, title-stripped)")
            continue

        # Fuzzy match against minutes names only (source of truth)
        base_words = len(base.split())
        best_match = None
        best_score = 0

        for mname in minutes_names:
            mwords = len(mname.split())
            if abs(base_words - mwords) > 1:
                continue
            if mname.startswith("Mr.") or mname.startswith("Mrs."):
                continue

            score = SequenceMatcher(None, base.lower(), mname.lower()).ratio()
            if score > best_score:
                best_score = score
                best_match = mname

        # Fallback: last-name matching (catches "Nick Natopoulos" → "John Nikitopoulos")
        if (not best_match or best_score < NAME_MATCH_THRESHOLD) and len(base.split()) >= 2:
            base_last = base.split()[-1].lower()
            for mname in minutes_names:
                mparts = mname.split()
                if len(mparts) < 2:
                    continue
                last_score = SequenceMatcher(None, base_last, mparts[-1].lower()).ratio()
                if last_score >= NAME_MATCH_THRESHOLD and last_score > best_score:
                    best_score = last_score
                    best_match = mname

        if best_match and best_score >= NAME_MATCH_THRESHOLD and best_match != ename:
            corrections.append((ename, best_match, best_score, "minutes"))
            if verbose:
                print(f"  \"{ename}\" → \"{best_match}\" ({best_score:.2f}, minutes)")

    return corrections


def find_chunk_corrections(canonical_names, minutes_names, verbose=False):
    """Find chunk speaker names that need correction."""
    db = sqlite3.connect(RAG_DB)
    db.row_factory = sqlite3.Row

    speakers = set()
    for row in db.execute(
        "SELECT DISTINCT speaker FROM chunks WHERE doc_type='transcript' AND speaker IS NOT NULL"
    ).fetchall():
        s = row["speaker"]
        if s and not s.startswith("Speaker "):
            speakers.add(s)
    db.close()

    corrections = []
    for sname in sorted(speakers):
        if sname in minutes_names:
            continue

        base = _strip_speaker_title(sname)
        if len(base.split()) < 2:
            continue

        if base in minutes_names and base != sname:
            corrections.append((sname, base, 1.0, "title-stripped"))
            if verbose:
                print(f"  \"{sname}\" → \"{base}\" (1.00, title-stripped)")
            continue

        base_words = len(base.split())
        best_match = None
        best_score = 0

        for mname in minutes_names:
            mwords = len(mname.split())
            if abs(base_words - mwords) > 1:
                continue
            if mname.startswith("Mr.") or mname.startswith("Mrs."):
                continue

            score = SequenceMatcher(None, base.lower(), mname.lower()).ratio()
            if score > best_score:
                best_score = score
                best_match = mname

        if best_match and best_score >= NAME_MATCH_THRESHOLD and best_match != sname:
            corrections.append((sname, best_match, best_score, "minutes"))
            if verbose:
                print(f"  \"{sname}\" → \"{best_match}\" ({best_score:.2f}, minutes)")

    return corrections


def scan(verbose=False):
    """Show all name mismatches without applying fixes."""
    canonical_names, minutes_names = _load_canonical_names()
    print(f"Canonical names: {len(canonical_names)} ({len(minutes_names)} from minutes)\n")

    print("=== Entity DB ===")
    entity_fixes = find_entity_corrections(canonical_names, minutes_names, verbose)
    if entity_fixes:
        for wrong, correct, score, method in entity_fixes:
            print(f"  {wrong} → {correct} ({score:.2f})")
    else:
        print("  All OK ✓")

    print("\n=== Chunk speakers ===")
    chunk_fixes = find_chunk_corrections(canonical_names, minutes_names, verbose)
    if chunk_fixes:
        db = sqlite3.connect(RAG_DB)
        for wrong, correct, score, method in chunk_fixes:
            count = db.execute(
                "SELECT COUNT(*) FROM chunks WHERE speaker = ?", (wrong,)
            ).fetchone()[0]
            print(f"  {wrong} → {correct} ({score:.2f}) [{count} chunks]")
        db.close()
    else:
        print("  All OK ✓")

    total = len(entity_fixes) + len(chunk_fixes)
    print(f"\nTotal: {total} corrections needed")
    return entity_fixes, chunk_fixes


def fix():
    """Apply corrections to entity DB and chunks."""
    canonical_names, minutes_names = _load_canonical_names()
    entity_fixes = find_entity_corrections(canonical_names, minutes_names)
    chunk_fixes = find_chunk_corrections(canonical_names, minutes_names)

    if not entity_fixes and not chunk_fixes:
        print("Nothing to fix.")
        return

    db = sqlite3.connect(RAG_DB)
    db.row_factory = sqlite3.Row
    applied = 0

    # Fix entities
    for wrong, correct, score, method in entity_fixes:
        eid = db.execute("SELECT id FROM entities WHERE name = ?", (wrong,)).fetchone()
        if not eid:
            continue
        eid = eid["id"]

        # Check if correct name already exists
        existing = db.execute("SELECT id FROM entities WHERE name = ?", (correct,)).fetchone()
        if existing:
            # Merge into existing entity — handle unique constraints
            target_id = existing["id"]
            # Move non-duplicate mentions
            db.execute("""
                UPDATE entity_mentions SET entity_id = ?
                WHERE entity_id = ? AND NOT EXISTS (
                    SELECT 1 FROM entity_mentions em2
                    WHERE em2.entity_id = ? AND em2.chunk_id = entity_mentions.chunk_id
                        AND em2.role = entity_mentions.role
                )
            """, (target_id, eid, target_id))
            # Delete remaining duplicate mentions
            db.execute("DELETE FROM entity_mentions WHERE entity_id = ?", (eid,))
            # Move relationships
            db.execute("""
                UPDATE relationships SET source_id = ?
                WHERE source_id = ? AND NOT EXISTS (
                    SELECT 1 FROM relationships r2
                    WHERE r2.source_id = ? AND r2.target_id = relationships.target_id
                        AND r2.type = relationships.type
                )
            """, (target_id, eid, target_id))
            db.execute("DELETE FROM relationships WHERE source_id = ?", (eid,))
            db.execute("""
                UPDATE relationships SET target_id = ?
                WHERE target_id = ? AND NOT EXISTS (
                    SELECT 1 FROM relationships r2
                    WHERE r2.target_id = ? AND r2.source_id = relationships.source_id
                        AND r2.type = relationships.type
                )
            """, (target_id, eid, target_id))
            db.execute("DELETE FROM relationships WHERE target_id = ?", (eid,))
            # Delete the old entity
            db.execute("DELETE FROM entities WHERE id = ?", (eid,))
            print(f"  MERGED: {wrong} → {correct} (entity {eid} → {existing['id']})")
        else:
            new_slug = correct.lower().replace(" ", "-").replace("'", "")
            db.execute(
                "UPDATE entities SET name = ?, slug = ? WHERE id = ?",
                (correct, new_slug, eid),
            )
            print(f"  RENAMED: {wrong} → {correct}")
        applied += 1

    # Fix chunks
    for wrong, correct, score, method in chunk_fixes:
        count = db.execute(
            "SELECT COUNT(*) FROM chunks WHERE speaker = ?", (wrong,)
        ).fetchone()[0]
        if count:
            db.execute(
                "UPDATE chunks SET speaker = ? WHERE speaker = ?", (correct, wrong)
            )
            print(f"  CHUNKS: {wrong} → {correct} ({count} rows)")
            applied += 1

    # Fix article text, summaries, and headlines
    # Only do safe string replacements for the same entity/chunk corrections
    # (NOT regex-based name finding, which catches "Labor Day" etc.)
    all_fixes = {}
    for wrong, correct, score, method in entity_fixes + chunk_fixes:
        if wrong != correct and len(wrong.split()) >= 2:
            all_fixes[wrong] = correct

    if all_fixes:
        print("\n=== Fixing article text & summaries ===")
        text_fields = ["article", "quick_summary", "complete_summary", "headline"]
        rows = db.execute(
            "SELECT id, article, quick_summary, complete_summary, headline FROM meetings"
        ).fetchall()
        text_applied = 0
        for row in rows:
            mid = row["id"]
            updates = {}
            for field in text_fields:
                text = row[field]
                if not text:
                    continue
                fixed = text
                for wrong, correct in all_fixes.items():
                    if wrong in fixed:
                        fixed = fixed.replace(wrong, correct)
                if fixed != text:
                    updates[field] = fixed
            if updates:
                set_clause = ", ".join(f"{f} = ?" for f in updates)
                values = list(updates.values()) + [mid]
                db.execute(f"UPDATE meetings SET {set_clause} WHERE id = ?", values)
                fields_fixed = ", ".join(updates.keys())
                print(f"  Article {mid}: fixed {fields_fixed}")
                text_applied += 1
        if not text_applied:
            print("  All article text OK ✓")
        applied += text_applied

    # Rebuild FTS
    if applied:
        print("\n  Rebuilding FTS index...")
        try:
            db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
        except Exception as e:
            print(f"  FTS warning: {e}")

    db.commit()
    db.close()
    print(f"\nApplied {applied} corrections.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "scan":
        verbose = "-v" in sys.argv or "--verbose" in sys.argv
        scan(verbose)
    elif cmd == "fix":
        scan()
        print()
        fix()
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)
