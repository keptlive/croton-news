"""
Topic thread detection and management for croton.news RAG.

Usage:
    python3 topics.py seed         # Seed known topic threads
    python3 topics.py discover     # Discover new threads via clustering
    python3 topics.py stats        # Show topic stats
"""

import os
import re
import sqlite3
import sys
import unicodedata

RAG_DB = os.path.join(os.path.dirname(__file__), "rag.db")

# Known recurring topics with search terms for matching
KNOWN_TOPICS = [
    {
        "name": "Court Consolidation",
        "description": "Proposal to merge Croton's village court with another municipality. Studied, debated, and ultimately tabled after public opposition.",
        "terms": ["court consolidation", "court study", "village court", "court merger"],
        "status": "resolved",
    },
    {
        "name": "Gouveia Park",
        "description": "Restoration and development of Gouveia Park including driveway, drainage, lighting, fencing, and the historic Gouveia house.",
        "terms": ["gouveia park", "gouveia house", "gouveia"],
        "status": "active",
    },
    {
        "name": "Solar & Battery Storage",
        "description": "Village solar canopy projects and battery energy storage system (BESS) incentives for green energy.",
        "terms": ["solar canopy", "solar panel", "battery storage", "BESS", "battery incentive", "5 MW battery"],
        "status": "active",
    },
    {
        "name": "Affordable Housing (Lot A)",
        "description": "100-unit affordable housing development on Lot A, the village's largest housing initiative.",
        "terms": ["affordable housing", "lot a housing", "maple commons", "100-unit"],
        "status": "active",
    },
    {
        "name": "Police Body Cameras",
        "description": "Acquisition and deployment of Axon body-worn cameras for the Croton police department.",
        "terms": ["body camera", "body cam", "body-worn camera", "axon", "axon enterprise"],
        "status": "active",
    },
    {
        "name": "Electric Vehicles & Green Fleet",
        "description": "Transition of DPW fleet to renewable diesel and electric vehicles.",
        "terms": ["electric vehicle", "renewable diesel", "green fleet", "EV charging"],
        "status": "active",
    },
    {
        "name": "Rental Registry",
        "description": "New rental registry law requiring landlords to register rental properties with the village.",
        "terms": ["rental registry", "rental registration", "landlord registry"],
        "status": "resolved",
    },
    {
        "name": "Backyard Chicken Regulations",
        "description": "Zoning amendments regulating residential fowl keeping — number of chickens, setback distances, coop requirements.",
        "terms": ["chicken", "fowl", "backyard chicken", "chicken coop", "fowl regulation"],
        "status": "active",
    },
    {
        "name": "Mount Airy Road Subdivision",
        "description": "Controversial subdivision proposal at 52 Mount Airy Road requiring multiple area variances.",
        "terms": ["mount airy", "52 mount airy", "mount airy subdivision"],
        "status": "active",
    },
    {
        "name": "Village Budget 2026-2027",
        "description": "Annual budget process including tax cap override, department work sessions, and capital plan.",
        "terms": ["budget 2026", "budget 2027", "tax cap override", "budget work session", "capital plan"],
        "status": "active",
    },
    {
        "name": "Downtown Paving",
        "description": "Long-stalled $1.03M downtown paving project on Old Post Road and surrounding streets.",
        "terms": ["downtown paving", "old post road paving", "street paving"],
        "status": "active",
    },
    {
        "name": "Short-Term Rental Tax",
        "description": "3% occupancy tax on short-term rentals (Airbnb) taking effect April 2026.",
        "terms": ["short-term rental", "airbnb tax", "occupancy tax", "short term rental tax"],
        "status": "resolved",
    },
    {
        "name": "Harmon Parking",
        "description": "Expansion of parking permits at the Harmon train station after a two-year pilot program.",
        "terms": ["harmon parking", "harmon permit", "train station parking"],
        "status": "resolved",
    },
    {
        "name": "Water Infrastructure",
        "description": "Aging water mains, cement lining projects, leak detection, and emergency water main breaks.",
        "terms": ["water main", "water break", "cement lining", "water meter", "water infrastructure"],
        "status": "active",
    },
]


def slugify(text):
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[-\s]+", "-", text).strip("-")
    return text


def seed_topics(db):
    """Seed known topic threads and link to matching chunks."""
    for topic in KNOWN_TOPICS:
        slug = slugify(topic["name"])

        # Skip if already exists
        existing = db.execute("SELECT id FROM topic_threads WHERE slug = ?", (slug,)).fetchone()
        if existing:
            print(f"  Skip {topic['name']} (already exists)")
            continue

        # Find matching chunks via keyword search
        matching_chunk_ids = set()
        for term in topic["terms"]:
            # Case-insensitive content search
            rows = db.execute(
                "SELECT id FROM chunks WHERE LOWER(content) LIKE ?",
                (f"%{term.lower()}%",)
            ).fetchall()
            for r in rows:
                matching_chunk_ids.add(r[0])

        if not matching_chunk_ids:
            print(f"  {topic['name']}: 0 chunks (skipping)")
            continue

        # Get date range from matching chunks
        dates = db.execute(
            f"SELECT MIN(date), MAX(date) FROM chunks WHERE id IN ({','.join('?' * len(matching_chunk_ids))})",
            list(matching_chunk_ids)
        ).fetchone()

        # Count distinct meetings
        meetings = db.execute(
            f"SELECT COUNT(DISTINCT doc_id) FROM chunks WHERE id IN ({','.join('?' * len(matching_chunk_ids))})",
            list(matching_chunk_ids)
        ).fetchone()[0]

        # Insert topic thread
        db.execute("""
            INSERT INTO topic_threads (name, slug, description, first_date, last_date, meeting_count, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (topic["name"], slug, topic["description"], dates[0], dates[1], meetings, topic["status"]))

        topic_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Link chunks to topic
        for chunk_id in matching_chunk_ids:
            db.execute(
                "INSERT OR IGNORE INTO topic_mentions (topic_id, chunk_id, relevance) VALUES (?, ?, 1.0)",
                (topic_id, chunk_id)
            )

        print(f"  {topic['name']}: {len(matching_chunk_ids)} chunks, {meetings} meetings ({dates[0]} to {dates[1]}) [{topic['status']}]")

    db.commit()


def show_stats(db):
    """Show topic thread statistics."""
    threads = db.execute("""
        SELECT t.name, t.slug, t.status, t.first_date, t.last_date, t.meeting_count,
               COUNT(tm.chunk_id) as chunk_count
        FROM topic_threads t
        LEFT JOIN topic_mentions tm ON tm.topic_id = t.id
        GROUP BY t.id
        ORDER BY chunk_count DESC
    """).fetchall()

    print(f"\nTopic Threads: {len(threads)}")
    print(f"{'Name':<35} {'Status':<10} {'Chunks':>6} {'Mtgs':>5} {'First':>12} {'Last':>12}")
    print("-" * 90)
    for name, slug, status, first, last, mtgs, chunks in threads:
        print(f"{name:<35} {status:<10} {chunks:>6} {mtgs:>5} {first or 'N/A':>12} {last or 'N/A':>12}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "seed"
    db = sqlite3.connect(RAG_DB)

    if cmd == "seed":
        print("=== Seeding known topic threads ===")
        seed_topics(db)
        show_stats(db)
    elif cmd == "stats":
        show_stats(db)
    elif cmd == "discover":
        print("=== Discovering topic threads via clustering ===")
        discover_topics(db)
        show_stats(db)
    else:
        print(f"Unknown command: {cmd}")

    db.close()


if __name__ == "__main__":
    main()
