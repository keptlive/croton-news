import json, sqlite3, sys

RAG_DB = "/opt/croton-news/rag/rag.db"

data = json.loads(sys.stdin.read())

conn = sqlite3.connect(RAG_DB)
conn.execute("""INSERT OR REPLACE INTO packet_pdfs
    (event_id, media_file, nickname, agenda_item_title, kind, size_bytes, pages, char_count, text, source_url)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
    (data["event_id"], data["media_file"], data["nickname"], data["title"],
     data["kind"], data["size"], data["pages"], data["char_count"],
     data["text"], data.get("source_url", "")))
conn.commit()
conn.close()
print(f"Stored: {data['nickname']}")
