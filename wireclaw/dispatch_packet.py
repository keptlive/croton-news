#!/usr/bin/env python3
"""Insert a one-shot task for croton-packet-writer via parameterized SQL."""
import sqlite3
import sys
import time
from datetime import datetime, timezone

eid = sys.argv[1] if len(sys.argv) > 1 else "1147"

prompt = f"""Write the preliminary packet article for event {eid}.

Follow the workflow in your CLAUDE.md:
1. meeting_info {eid} (bail if transcript_available is true)
2. fetch_agenda_packet {eid} (get all PDF text)
3. search_references for each substantive topic in the packet
4. Draft with the required Editor's note, no direct quotes, no /photos/ images
5. save_article {eid} with model=glm-5-turbo-packet
6. End the article body with a "Source documents" section linking each PDF URL from the attachment records

Report back: meeting_id, headline, pdf count used, reference count.
"""

now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
# include event id so multiple dispatches in the same second don't collide
# (UNIQUE constraint on scheduled_tasks.id — bit us 2026-07-13)
task_id = f"task-packet-{int(time.time())}-{eid}-manual"

db = sqlite3.connect("/root/wireclaw-cli/store/messages.db")
db.execute(
    """INSERT INTO scheduled_tasks
       (id, group_folder, chat_jid, prompt, schedule_type, schedule_value,
        next_run, status, created_at, context_mode)
       VALUES (?, 'croton-packet-writer', 'agentwire:croton-packet-writer', ?,
               'once', ?, ?, 'active', ?, 'isolated')""",
    (task_id, prompt, now, now, now),
)
db.commit()
db.close()
print(f"inserted task {task_id} for event {eid}")
print(f"fires at {now}")
