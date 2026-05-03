"""
Entity extraction and knowledge graph building for croton.news RAG.

Usage:
    python3 entities.py index_json    # Extract from article index_json
    python3 entities.py speakers      # Extract from transcript speaker maps
    python3 entities.py link          # Link entities to chunks
    python3 entities.py all           # All of the above
    python3 entities.py stats         # Show entity stats
"""

import json
import os
import re
import sqlite3
import sys
import unicodedata

RAG_DB = os.path.join(os.path.dirname(__file__), "rag.db")

# Summaries DB: check multiple possible locations
_SUMMARIES_CANDIDATES = [
    os.path.join(os.path.dirname(__file__), "..", "scrapers", "summaries.db"),
    os.path.join(os.path.dirname(__file__), "..", "ecode360", "summaries.db"),
]
SUMMARIES_DB = next((p for p in _SUMMARIES_CANDIDATES if os.path.exists(p)), _SUMMARIES_CANDIDATES[0])

TRANSCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "transcripts")


def slugify(text):
    """Convert text to URL-safe slug."""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[-\s]+", "-", text).strip("-")
    return text


TITLE_PREFIXES = [
    "Village Manager", "Village Attorney", "Village Engineer", "Village Clerk",
    "Acting Planning Board Vice Chair", "Planning Board Chair", "ZBA Chair",
    "Mayor", "Trustee", "Chief", "Treasurer", "Officer", "Deputy Mayor",
    "Commissioner", "Former Chief", "Mr.", "Mrs.", "Ms.", "Dr.",
]


def _strip_title(name):
    """Strip title prefix from a name, return (clean_name, title)."""
    for title in sorted(TITLE_PREFIXES, key=len, reverse=True):
        if name.startswith(title + " "):
            return name[len(title):].strip(), title
    return name, ""


def find_or_create_entity(db, name, entity_type, metadata=None, date=None):
    """Find existing entity or create new one. Returns entity ID."""
    slug = slugify(name)
    if not slug:
        return None

    existing = db.execute(
        "SELECT id, mention_count FROM entities WHERE slug = ?", (slug,)
    ).fetchone()

    if existing:
        # Update mention count and date range
        db.execute(
            "UPDATE entities SET mention_count = mention_count + 1 WHERE id = ?",
            (existing[0],)
        )
        if date:
            db.execute(
                "UPDATE entities SET last_seen_date = MAX(COALESCE(last_seen_date, ?), ?) WHERE id = ?",
                (date, date, existing[0])
            )
        return existing[0]

    meta_json = json.dumps(metadata) if metadata else None
    db.execute("""
        INSERT INTO entities (name, type, slug, metadata_json, first_seen_date, last_seen_date, mention_count)
        VALUES (?, ?, ?, ?, ?, ?, 1)
    """, (name, entity_type, slug, meta_json, date, date))

    return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def extract_from_index_json(db):
    """Extract entities from article index_json fields."""
    if not os.path.exists(SUMMARIES_DB):
        print(f"Summaries DB not found: {SUMMARIES_DB}")
        return

    sdb = sqlite3.connect(SUMMARIES_DB)
    sdb.row_factory = sqlite3.Row
    rows = sdb.execute("""
        SELECT doc_id, committee, date, index_json
        FROM summaries
        WHERE index_json IS NOT NULL AND index_json != ''
    """).fetchall()
    sdb.close()

    people_count = 0
    location_count = 0
    org_count = 0
    topic_count = 0

    for row in rows:
        try:
            data = json.loads(row["index_json"])
        except json.JSONDecodeError:
            continue

        doc_id = row["doc_id"]
        date = row["date"]

        # People
        for person in data.get("people", []):
            name = person.get("name", "").strip()
            if not name or len(name) < 5 or " " not in name:
                continue  # Require first+last name to avoid false positives
            role = person.get("role", "")
            # Strip known titles from name, store as role
            name, extracted_role = _strip_title(name)
            if extracted_role and not role:
                role = extracted_role
            if not name or len(name) < 3:
                continue
            eid = find_or_create_entity(db, name, "person", {"role": role}, date)
            if eid:
                people_count += 1

        # Locations
        for loc in data.get("locations", []):
            if isinstance(loc, str):
                loc_name = loc.strip()
            elif isinstance(loc, dict):
                loc_name = loc.get("name", "").strip()
            else:
                continue
            if not loc_name or len(loc_name) < 3:
                continue
            eid = find_or_create_entity(db, loc_name, "location", None, date)
            if eid:
                location_count += 1

        # Organizations
        for org in data.get("organizations", []):
            if isinstance(org, str):
                org_name = org.strip()
            elif isinstance(org, dict):
                org_name = org.get("name", "").strip()
            else:
                continue
            if not org_name or len(org_name) < 3:
                continue
            eid = find_or_create_entity(db, org_name, "organization", None, date)
            if eid:
                org_count += 1

        # Topics
        for topic in data.get("topics", []):
            if isinstance(topic, str):
                topic_name = topic.strip()
            else:
                continue
            if not topic_name or len(topic_name) < 3:
                continue
            eid = find_or_create_entity(db, topic_name, "topic", None, date)
            if eid:
                topic_count += 1

    db.commit()
    print(f"  People: {people_count} mentions")
    print(f"  Locations: {location_count} mentions")
    print(f"  Organizations: {org_count} mentions")
    print(f"  Topics: {topic_count} mentions")


def extract_from_speakers(db):
    """Extract person entities from transcript speaker maps."""
    import glob
    files = sorted(glob.glob(os.path.join(TRANSCRIPTS_DIR, "transcript-*.json")))

    speaker_count = 0
    for filepath in files:
        with open(filepath) as f:
            data = json.load(f)

        speaker_map = data.get("speaker_map", {})
        date = data.get("date", "")
        event_id = str(data.get("event_id", ""))

        for num, name in speaker_map.items():
            name = name.strip()
            if not name or name.startswith("Speaker") or len(name) < 3:
                continue

            eid = find_or_create_entity(db, name, "person", {"source": "speaker_map"}, date)
            if eid:
                speaker_count += 1

                # Create "spoke_at" relationship to this meeting
                # Find or create meeting entity
                title = data.get("title", f"Meeting {event_id}")
                meeting_eid = find_or_create_entity(db, f"{title} ({date})", "meeting", {
                    "event_id": event_id, "date": date
                }, date)
                if meeting_eid:
                    db.execute("""
                        INSERT OR IGNORE INTO relationships (source_id, target_id, type, context, doc_id)
                        VALUES (?, ?, 'spoke_at', ?, ?)
                    """, (eid, meeting_eid, f"Speaker in {title}", event_id))

    db.commit()
    print(f"  Speakers: {speaker_count} mentions from {len(files)} transcripts")


def link_entities_to_chunks(db):
    """Link entities to chunks where they're mentioned."""
    entities = db.execute("SELECT id, name, type FROM entities").fetchall()
    chunks = db.execute("SELECT id, content, speaker, doc_id FROM chunks").fetchall()

    link_count = 0
    for eid, name, etype in entities:
        if len(name) < 6:
            continue  # Skip short names to avoid false positive matches
        # Check speaker attribution
        if etype == "person":
            for cid, content, speaker, doc_id in chunks:
                if speaker and name.lower() in speaker.lower():
                    db.execute("""
                        INSERT OR IGNORE INTO entity_mentions (entity_id, chunk_id, role)
                        VALUES (?, ?, 'speaker')
                    """, (eid, cid))
                    link_count += 1
                elif name.lower() in content.lower():
                    db.execute("""
                        INSERT OR IGNORE INTO entity_mentions (entity_id, chunk_id, role)
                        VALUES (?, ?, 'mentioned')
                    """, (eid, cid))
                    link_count += 1
        else:
            # For non-person entities, just check content
            for cid, content, speaker, doc_id in chunks:
                if name.lower() in content.lower():
                    db.execute("""
                        INSERT OR IGNORE INTO entity_mentions (entity_id, chunk_id, role)
                        VALUES (?, ?, 'mentioned')
                    """, (eid, cid))
                    link_count += 1

        if link_count % 1000 == 0 and link_count > 0:
            print(f"    ... {link_count} links created")

    db.commit()
    print(f"  Total entity-chunk links: {link_count}")


def show_stats(db):
    """Show entity statistics."""
    total = db.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    by_type = db.execute(
        "SELECT type, COUNT(*) FROM entities GROUP BY type ORDER BY 2 DESC"
    ).fetchall()
    print(f"\nTotal entities: {total}")
    for etype, count in by_type:
        print(f"  {etype}: {count}")

    # Top entities by mention count
    top = db.execute("""
        SELECT name, type, mention_count FROM entities
        ORDER BY mention_count DESC LIMIT 15
    """).fetchall()
    print(f"\nTop entities:")
    for name, etype, count in top:
        print(f"  {name} ({etype}): {count} mentions")

    # Relationships
    rel_count = db.execute("SELECT COUNT(*) FROM relationships").fetchone()[0]
    print(f"\nRelationships: {rel_count}")

    # Entity-chunk links
    link_count = db.execute("SELECT COUNT(*) FROM entity_mentions").fetchone()[0]
    print(f"Entity-chunk links: {link_count}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    db = sqlite3.connect(RAG_DB)

    if cmd in ("index_json", "all"):
        print("=== Extracting from index_json ===")
        extract_from_index_json(db)

    if cmd in ("speakers", "all"):
        print("\n=== Extracting from speaker maps ===")
        extract_from_speakers(db)

    if cmd in ("link", "all"):
        print("\n=== Linking entities to chunks ===")
        link_entities_to_chunks(db)

    if cmd in ("stats", "all"):
        show_stats(db)

    db.close()


if __name__ == "__main__":
    main()
