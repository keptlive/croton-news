#!/bin/bash
# Daily backup of rag.db to /opt/croton-news/backups/
BACKUP_DIR=/opt/croton-news/backups
DB=/opt/croton-news/rag/rag.db
mkdir -p "$BACKUP_DIR"

# Use sqlite3 .backup for safe copy (handles WAL mode)
STAMP=$(date +%Y%m%d_%H%M)
sqlite3 "$DB" ".backup ${BACKUP_DIR}/rag-${STAMP}.db"

if [ $? -eq 0 ]; then
    echo "$(date): Backup created: rag-${STAMP}.db ($(du -h ${BACKUP_DIR}/rag-${STAMP}.db | cut -f1))"
    # Keep only last 7 backups
    ls -t ${BACKUP_DIR}/rag-*.db | tail -n +8 | xargs rm -f 2>/dev/null
else
    echo "$(date): BACKUP FAILED" >&2
    exit 1
fi
