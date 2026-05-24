#!/usr/bin/env python3
"""Run all croton.news scrapers and insert new articles into the database."""
import sqlite3
import sys
sys.path.insert(0, '/opt/croton-news')

from scrapers import ALL_SCRAPERS
from datetime import datetime, timezone

db = sqlite3.connect('/opt/croton-news/data/croton.db')
cursor = db.cursor()

total_new = 0
for ScraperClass in ALL_SCRAPERS:
    s = ScraperClass()
    try:
        items = s.scrape()
        if not items:
            continue
        new_count = 0
        for item in items:
            cursor.execute("SELECT id FROM articles WHERE title = ? AND source = ?", (item.get("title", ""), s.name))
            if cursor.fetchone():
                continue
            cursor.execute(
                "INSERT INTO articles (title, url, source, category, summary, content, published_at, scraped_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.get("title", ""),
                    item.get("url", ""),
                    s.name,
                    item.get("category", s.category),
                    item.get("summary", ""),
                    item.get("content", ""),
                    item.get("published_at", datetime.now(timezone.utc).isoformat()),
                    datetime.now(timezone.utc).isoformat(),
                )
            )
            new_count += 1
        total_new += new_count
        if new_count:
            print(f"{s.name}: {new_count} new")
    except Exception as e:
        print(f"{s.name}: ERROR - {e}")

db.commit()
db.close()
print(f"Total: {total_new} new articles")
