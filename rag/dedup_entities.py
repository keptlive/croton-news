#!/usr/bin/env python3
"""
Deduplicate and enrich entities in rag.db.

1. Merge title-prefixed variants, spelling variants, and last-name-only entries
2. Assign roles/titles to all person entities from curated list + transcript context
3. Clean up junk entities (title-only, single chars, etc.)

Run after entity extraction (entities.py) to keep the database clean.
Baked into pipeline.py — runs automatically after every ingest.

Usage:
    python3 dedup_entities.py --dry-run    # Preview changes
    python3 dedup_entities.py              # Apply changes
"""

import json
import os
import re
import sqlite3
import sys
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RAG_DB = os.path.join(BASE_DIR, "rag.db")

# Known canonical names → list of aliases
# Add entries here as new duplicates are discovered
CANONICAL_NAMES = {
    "Bryan Healy": [
        "Brian Healy", "Manager Healy", "Village Manager Bryan Healy",
        "Village Manager Healy",
    ],
    "Brian Pugh": [
        "Mayor Brian Pugh", "Mayor Pugh",
    ],
    "Maria Slippen": [
        "Karen Slippen", "Slippen", "Trustee Slippen", "Trustee Maria Slippen",
    ],
    "Stacey Nachtaler": [
        "Stacy Nachtaler", "Nachtaler", "Trustee Nachtaler", "Trustee Stacey Nachtaler",
        "Trustee Stacy Nachteller",
    ],
    "Andrew Cortese": [
        "Andrew Cortez",
    ],
    "Vincent Salanitro": [
        "Vince Salanitro", "Village Engineer Vincent Salanitro",
        "Village Engineer Salanitro", "Engineer Salanitro",
    ],
    "James Tuman": [
        "Chair Tuman", "Chairman Tuman", "ZBA Chair James Tuman",
        "ZBA Chair Tuman", "Jim Tuman",
    ],
    "Casey Rascob": [
        "Casey Raskob",
    ],
    "Genette Toone": [
        "Treasurer Genette Toone", "Treasurer Toone",
    ],
    "Len Simon": [
        "Leonard Simon", "Chairperson Len Simon",
    ],
    "Joseph Arnold": [
        "Joseph Arno", "Joseph Arne",
    ],
    "Gabriella Mirabelli": [
        "Gabriela Mirabelli",
    ],
    "Dan Cayer": [
        "Dan Kayer",
    ],
    "Deborah Schupack": [
        "Deborah Schupak",
    ],
    "Orit Daly": [
        "Ori Daily",
    ],
    "Frank Balby": [
        "DPW Superintendent Frank Balby",
    ],
    "Nora Nicholson": [
        "Karen Nicholson", "Annemarie Nicholson", "Ann Nicholson",
        "Dana Nicholson", "Liz Nicholson", "Trustee Nicholson",
        "Nora M. Nicholson", "Nora Moriarty Nicholson",
    ],
    "Cara Politi": [
        "Karen Politi", "Trustee Politi",
    ],
    # Board of Education
    "Brendan Walker": [
        "Superintendent Walker", "Dr. Walker", "Superintendent Brendan Walker",
    ],
    "Anamica Chaudhuri": [
        "President Chaudhuri", "Board President Chaudhuri",
    ],
    "Sarah Carrier": [
        "Vice President Carrier", "Board Vice President Carrier",
    ],
    "Omar Faruk": [
        "Assistant Superintendent Faruk",
    ],
    "Laura Fjeld": [
        "Assistant Superintendent Fjeld",
    ],
}

# Known roles for Croton officials and frequent participants
# Add entries here as new people are identified
KNOWN_ROLES = {
    "Brian Pugh": "Mayor",
    "Bryan Healy": "Village Manager",
    "Maria Slippen": "Trustee",
    "Stacey Nachtaler": "Trustee",
    "James Tuman": "ZBA Chair",
    "Vincent Salanitro": "Village Engineer",
    "Joshua Subin": "Village Attorney",
    "Genette Toone": "Village Treasurer",
    "Len Simon": "Trustee",
    "Nora Nicholson": "Trustee",
    "Ann Gallelli": "Former Trustee",
    "Cara Politi": "Former Trustee",
    "Casey Rascob": "Court Clerk",
    "Frank Balby": "DPW Superintendent",
    "Andrew Cortese": "Developer",
    "Alan Kasay": "Village Auditor",
    "Patty Buchanan": "Croton 100, Climate Activist",
    "Joel Gingold": "Resident",
    "Louis Montana": "Resident",
    "Ed Riley": "Resident",
    "Gabriella Mirabelli": "Resident, Attorney",
    "Matthew Rubenstein": "Resident",
    "Dan Cayer": "Resident, Writer",
    "Deborah Schupack": "Resident, Author",
    "Joseph Arnold": "Resident",
    "Norm Janssen": "Westchester Modular Construction",
    "Bo Balaban": "Planning Board Member",
    "Ralph Rossi": "Planning Board Member",
    "Christine Wagner": "Resident",
    "David Steele": "Resident",
    "Ashley Steele": "Resident",
    "Edward Wohl": "Resident",
    "Ruben Dahlia": "Resident",
    "Vincent Cohan": "Resident",
    "Orit Daly": "Resident, Artist",
    "Mike Mastrogiacomo": "Engineer, Mastrogiacomo Engineering",
    "Kory Salomone": "Attorney, Zarin & Steinmetz",
    "Adam Thiber": "Resident",
    "Travis Schnell": "Resident",
    "Hannah Robbins": "Resident",
    "Darren Blom": "ZBA Member",
    "Rocco Spallone": "ZBA Member",
    "Andrea DeGeorge Garbarini": "Planning Board Member",
    "Peter Skylar": "Resident",
    "Christine O'Connor": "Resident",
    "Paul Doyle": "Resident",
    "Jay Sherman": "Resident",
    "Ira Lipton": "Resident",
    "Susan Screlia": "Resident",
    "Valerie Monastra": "Resident",
    "Bill Goldsmith": "Resident",
    "Art Roosa Jr": "Fire Council",
    "Barry Donaldson": "Resident",
    "Bob Small": "Resident",
    "Adriana Zavala": "Resident",
    # Board of Education
    "Brendan Walker": "Superintendent, CHUFSD",
    "Anamica Chaudhuri": "Board of Education President",
    "Sarah Carrier": "Board of Education Vice President",
    "Iris Grink": "Board of Education Member",
    "Josh Nathan": "Board of Education Member",
    "Gael Sullivan-Davis": "Board of Education Member",
    "Andrea Fuentes": "Board of Education Member",
    "Ting-Yi Oei": "Board of Education Member",
    "Omar Faruk": "Assistant Superintendent for Business, CHUFSD",
    "Laura Fjeld": "Assistant Superintendent for Curriculum & Instruction, CHUFSD",
}

# Title-only entities to delete (these are roles, not people)
TITLE_ONLY_DELETE = [
    "ZBA Chair", "Planning Board Chair", "Acting Planning Board Vice Chair",
    "ZBA Member Darren", "ZBA Member Rocco", "Vice Chair",
    "Acting Chair", "Acting Vice Chair",
]

# Title prefixes to strip when auto-detecting duplicates
TITLE_PREFIXES = [
    "Mayor", "Trustee", "Chair", "Chairman", "Chairwoman", "Chairperson",
    "Vice Chair", "Acting Chair", "Acting Vice Chair",
    "Village Manager", "Village Engineer", "Village Attorney",
    "Treasurer", "Inspector", "Chief", "Officer", "Sergeant",
    "Deputy Mayor", "Superintendent", "Director", "Commissioner",
    "Councilmember", "Board Member", "ZBA Chair", "Planning Board Chair",
]


def normalize_name(name):
    """Strip title prefixes to find the base name."""
    n = name.strip()
    for prefix in sorted(TITLE_PREFIXES, key=len, reverse=True):
        if n.startswith(prefix + " "):
            n = n[len(prefix) + 1:].strip()
            break
    return n


def find_auto_merges(db):
    """Auto-detect entities that should be merged based on title prefixes."""
    entities = db.execute(
        "SELECT id, name, type, slug FROM entities WHERE type = 'person'"
    ).fetchall()

    # Group by normalized name
    by_base = defaultdict(list)
    for e in entities:
        base = normalize_name(e["name"])
        by_base[base].append(e)

    merges = {}
    for base, group in by_base.items():
        if len(group) < 2:
            continue
        # Pick canonical: prefer the shortest non-titled version with 2+ words
        candidates = sorted(group, key=lambda e: (
            len(e["name"].split()) < 2,  # prefer 2+ word names
            any(e["name"].startswith(p + " ") for p in TITLE_PREFIXES),  # prefer untitled
            len(e["name"]),  # prefer shorter
        ))
        canonical = candidates[0]
        aliases = [e for e in candidates[1:] if e["id"] != canonical["id"]]
        if aliases:
            merges[canonical["name"]] = {
                "canonical_id": canonical["id"],
                "aliases": [(a["id"], a["name"]) for a in aliases],
            }

    return merges


def find_last_name_only(db):
    """Find single-word entities that match EXACTLY ONE multi-word entity's last name."""
    entities = db.execute(
        "SELECT id, name, type FROM entities WHERE type = 'person'"
    ).fetchall()

    singles = {e["name"]: e["id"] for e in entities if len(e["name"].split()) == 1}
    multis = [(e["name"], e["id"]) for e in entities if len(e["name"].split()) >= 2]

    # For each single name, find all matching multi-word names
    single_matches = defaultdict(list)
    for multi_name, multi_id in multis:
        last = multi_name.split()[-1]
        if last in singles:
            single_matches[last].append((multi_name, multi_id))

    merges = {}
    for single_name, matches in single_matches.items():
        # Only merge if exactly one multi-word match (unambiguous)
        if len(matches) != 1:
            continue
        canonical_name, canonical_id = matches[0]
        single_id = singles[single_name]
        if single_id != canonical_id:
            merges[canonical_name] = {
                "canonical_id": canonical_id,
                "aliases": [(single_id, single_name)],
            }

    return merges


def merge_entity(db, keep_id, remove_id, dry_run=False):
    """Merge remove_id into keep_id: transfer mentions, delete duplicate."""
    if dry_run:
        return

    # Transfer entity_mentions (ignore conflicts from UNIQUE constraint)
    db.execute("""
        INSERT OR IGNORE INTO entity_mentions (entity_id, chunk_id, role)
        SELECT ?, chunk_id, role FROM entity_mentions WHERE entity_id = ?
    """, (keep_id, remove_id))

    # Delete old mentions
    db.execute("DELETE FROM entity_mentions WHERE entity_id = ?", (remove_id,))

    # Update mention count on canonical
    count = db.execute(
        "SELECT COUNT(*) FROM entity_mentions WHERE entity_id = ?", (keep_id,)
    ).fetchone()[0]
    db.execute("UPDATE entities SET mention_count = ? WHERE id = ?", (count, keep_id))

    # Delete the duplicate entity
    db.execute("DELETE FROM entities WHERE id = ?", (remove_id,))


def main():
    dry_run = "--dry-run" in sys.argv

    db = sqlite3.connect(RAG_DB)
    db.row_factory = sqlite3.Row

    total_merged = 0

    # 1. Apply explicit canonical merges
    print("=== Explicit canonical merges ===")
    for canonical_name, aliases in CANONICAL_NAMES.items():
        canonical = db.execute(
            "SELECT id FROM entities WHERE name = ?", (canonical_name,)
        ).fetchone()

        if not canonical:
            # Canonical doesn't exist — rename the first alias we find
            for alias in aliases:
                dup = db.execute(
                    "SELECT id FROM entities WHERE name = ?", (alias,)
                ).fetchone()
                if dup:
                    print(f"  Renaming {alias} (#{dup['id']}) → {canonical_name}")
                    if not dry_run:
                        slug = canonical_name.lower().replace(" ", "-").replace("'", "")
                        db.execute(
                            "UPDATE entities SET name = ?, slug = ? WHERE id = ?",
                            (canonical_name, slug, dup["id"])
                        )
                    canonical = dup
                    total_merged += 1
                    break

        if not canonical:
            continue

        for alias in aliases:
            dup = db.execute(
                "SELECT id FROM entities WHERE name = ?", (alias,)
            ).fetchone()
            if not dup or dup["id"] == canonical["id"]:
                continue
            print(f"  {alias} (#{dup['id']}) → {canonical_name} (#{canonical['id']})")
            merge_entity(db, canonical["id"], dup["id"], dry_run)
            total_merged += 1

    # 2. Auto-detect title-prefix duplicates
    print("\n=== Auto-detected title prefix merges ===")
    auto_merges = find_auto_merges(db)
    for canonical_name, info in auto_merges.items():
        for alias_id, alias_name in info["aliases"]:
            # Skip if already handled by explicit merges
            if not db.execute("SELECT 1 FROM entities WHERE id = ?", (alias_id,)).fetchone():
                continue
            print(f"  {alias_name} (#{alias_id}) → {canonical_name} (#{info['canonical_id']})")
            merge_entity(db, info["canonical_id"], alias_id, dry_run)
            total_merged += 1

    # 3. Merge last-name-only entries
    print("\n=== Last-name-only merges ===")
    ln_merges = find_last_name_only(db)
    for canonical_name, info in ln_merges.items():
        for alias_id, alias_name in info["aliases"]:
            if not db.execute("SELECT 1 FROM entities WHERE id = ?", (alias_id,)).fetchone():
                continue
            print(f"  {alias_name} (#{alias_id}) → {canonical_name} (#{info['canonical_id']})")
            merge_entity(db, info["canonical_id"], alias_id, dry_run)
            total_merged += 1

    # 4. Delete title-only entities (roles without names)
    print("\n=== Removing title-only entities ===")
    for title_name in TITLE_ONLY_DELETE:
        ent = db.execute("SELECT id FROM entities WHERE name = ?", (title_name,)).fetchone()
        if ent:
            print(f"  Removing: {title_name} (#{ent['id']})")
            if not dry_run:
                db.execute("DELETE FROM entity_mentions WHERE entity_id = ?", (ent["id"],))
                db.execute("DELETE FROM entities WHERE id = ?", (ent["id"],))
            total_merged += 1

    # 5. Remove junk entities (single chars, common words, etc.)
    print("\n=== Removing junk entities ===")
    junk = db.execute("""
        SELECT id, name FROM entities
        WHERE LENGTH(name) < 3
           OR name IN ('The', 'This', 'That', 'Board', 'Village', 'State', 'County')
           OR (type = 'person' AND LENGTH(name) < 4 AND name NOT LIKE '% %')
    """).fetchall()
    for j in junk:
        print(f"  Removing junk: {j['name']} (#{j['id']})")
        if not dry_run:
            db.execute("DELETE FROM entity_mentions WHERE entity_id = ?", (j["id"],))
            db.execute("DELETE FROM entities WHERE id = ?", (j["id"],))
        total_merged += 1

    # 6. Assign roles to all person entities
    print("\n=== Assigning roles ===")
    roles_assigned = 0
    people = db.execute(
        "SELECT id, name, metadata_json FROM entities WHERE type = 'person'"
    ).fetchall()
    for p in people:
        name = p["name"]
        meta = json.loads(p["metadata_json"]) if p["metadata_json"] else {}

        # Skip if already has a role
        if meta.get("role"):
            continue

        role = None

        # Check curated KNOWN_ROLES
        if name in KNOWN_ROLES:
            role = KNOWN_ROLES[name]

        # Auto-detect from title in name (for entities we haven't cleaned yet)
        if not role:
            stripped = normalize_name(name)
            if stripped != name:
                # The name had a title prefix — extract the role
                role = name[:len(name) - len(stripped)].strip()

        # Default: anyone unidentified who spoke at meetings is a Resident
        if not role:
            role = "Resident"

        meta["role"] = role
        print(f"  {name} → {role}")
        if not dry_run:
            db.execute(
                "UPDATE entities SET metadata_json = ? WHERE id = ?",
                (json.dumps(meta), p["id"])
            )
        roles_assigned += 1

    if not dry_run:
        db.commit()

    print(f"\n{'Would merge/remove' if dry_run else 'Merged/removed'} {total_merged} entities")
    print(f"{'Would assign' if dry_run else 'Assigned'} {roles_assigned} roles")

    # Show final count
    count = db.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    people_count = db.execute("SELECT COUNT(*) FROM entities WHERE type = 'person'").fetchone()[0]
    with_roles = db.execute(
        "SELECT COUNT(*) FROM entities WHERE type = 'person' AND metadata_json LIKE '%role%'"
    ).fetchone()[0]
    print(f"Entities: {count} total, {people_count} people ({with_roles} with roles)")

    db.close()


if __name__ == "__main__":
    main()
