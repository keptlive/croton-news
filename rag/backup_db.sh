#!/bin/bash
# Daily backup of all croton.news databases to /opt/croton-news/backups/
# Cron (wrapped): 0 5 * * * run_job.sh db-backup -- /opt/croton-news/rag/backup_db.sh
# Offsite: the WireClaw box pulls this directory nightly (see its crontab).
BASE=/opt/croton-news
BACKUP_DIR=$BASE/backups
mkdir -p "$BACKUP_DIR"
STAMP=$(date +%Y%m%d_%H%M)
FAIL=0

# name:path pairs — sqlite3 .backup is WAL-safe
DBS="
rag:$BASE/rag/rag.db
comments:$BASE/comments.db
tips:$BASE/tips.db
photos:$BASE/photos.db
ecode-summaries:$BASE/ecode360/summaries.db
code:$BASE/rag/code.db
history:$BASE/rag/history.db
ecode-search:$BASE/ecode360/search.db
scraped-news:$BASE/data/croton.db
"

for pair in $DBS; do
    name="${pair%%:*}"
    path="${pair#*:}"
    if [ ! -s "$path" ]; then
        echo "$(date): skip $name — missing or empty ($path)"
        continue
    fi
    out="$BACKUP_DIR/${name}-${STAMP}.db"
    if sqlite3 "$path" ".backup $out"; then
        echo "$(date): backup ok: ${name}-${STAMP}.db ($(du -h "$out" | cut -f1))"
    else
        echo "$(date): BACKUP FAILED: $name" >&2
        FAIL=1
    fi
    # keep last 7 per database
    ls -t "$BACKUP_DIR/${name}-"*.db 2>/dev/null | tail -n +8 | xargs rm -f 2>/dev/null
done

exit $FAIL
