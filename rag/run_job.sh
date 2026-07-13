#!/bin/bash
# run_job.sh — cron job wrapper with status tracking + failure alerts
#
# Usage: run_job.sh JOB_NAME [MANUAL_HINT] -- COMMAND [ARGS...]
#   JOB_NAME     short identifier (e.g. "boarddocs-sync")
#   MANUAL_HINT  optional text included in failure emails telling the
#                operator what to run by hand (quote it)
#
# Behavior:
#   - runs COMMAND, appends output to /var/log/croton-jobs/JOB_NAME.log
#   - records start/end/exit_code into rag/job_runs.db (sqlite)
#   - on non-zero exit: emails via notify.py (6h cooldown per job)
#
# Every pipeline cron entry should go through this wrapper so
# pipeline_watch.py can verify cadence from job_runs.db.

set -u
RAG=/opt/croton-news/rag
VENV=/opt/croton-news/venv/bin/python
LOGDIR=/var/log/croton-jobs
DB=$RAG/job_runs.db

JOB="$1"; shift
HINT=""
if [ "$1" != "--" ]; then HINT="$1"; shift; fi
[ "$1" == "--" ] && shift

mkdir -p "$LOGDIR"
LOG="$LOGDIR/$JOB.log"

# Run from rag/ so scripts resolving .env/paths via cwd behave like manual runs
cd "$RAG" || exit 1
export $(grep -v "^#" "$RAG/.env" | xargs) 2>/dev/null

sqlite3 "$DB" "CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job TEXT NOT NULL,
    started_at TEXT DEFAULT (datetime('now')),
    finished_at TEXT,
    exit_code INTEGER,
    log_tail TEXT
);"
RUN_ID=$(sqlite3 "$DB" "INSERT INTO job_runs (job) VALUES ('$JOB'); SELECT last_insert_rowid();")

echo "$(date '+%F %T') === $JOB start ===" >> "$LOG"
"$@" >> "$LOG" 2>&1
CODE=$?
echo "$(date '+%F %T') === $JOB exit $CODE ===" >> "$LOG"

TAIL=$(tail -c 4000 "$LOG")
sqlite3 "$DB" "UPDATE job_runs SET finished_at = datetime('now'), exit_code = $CODE,
    log_tail = '$(echo "$TAIL" | sed "s/'/''/g")' WHERE id = $RUN_ID;"

if [ $CODE -ne 0 ]; then
    BODY="Job: $JOB
Exit code: $CODE
Host: $(hostname)
Log: $LOG
$( [ -n "$HINT" ] && echo "Manual fix: $HINT" )

── last output ──
$(tail -n 30 "$LOG")"
    "$VENV" "$RAG/notify.py" --topic "job-$JOB" --cooldown 21600 \
        "job failed: $JOB (exit $CODE)" "$BODY"
fi

exit $CODE
