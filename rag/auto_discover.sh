#!/bin/bash
# croton.news — full daily pipeline
# Cron (via run_job.sh wrapper):
#   0 6 * * * /opt/croton-news/rag/run_job.sh daily-pipeline -- /opt/croton-news/rag/auto_discover.sh
#
# Uses the project venv (system python3 lacks pymupdf/deepgram/etc — that
# mismatch silently killed PDF extraction once already). A stage failure
# is recorded but later stages still run; overall exit is non-zero if any
# stage failed, so run_job.sh sends an alert email.
cd /opt/croton-news/rag || exit 1
export $(grep -v "^#" .env | xargs) 2>/dev/null
PY=/opt/croton-news/venv/bin/python
LOG=/var/log/croton-pipeline.log
FAIL=0

stage() {
    local name="$1"; shift
    echo "$(date '+%F %T'): -- stage: $name" >> "$LOG"
    "$@" >> "$LOG" 2>&1
    local code=$?
    if [ $code -ne 0 ]; then
        echo "$(date '+%F %T'): !! stage $name FAILED (exit $code)" >> "$LOG"
        FAIL=1
    fi
}

echo "$(date '+%F %T'): === DAILY PIPELINE START ===" >> "$LOG"

# 1. Discover new meetings
stage discover        "$PY" pipeline.py discover

# 2. Match orphan meetings to ChampDS events
stage match-orphans   "$PY" pipeline.py match-orphans

# 3. Refresh agendas + check for new video
stage refresh-agendas "$PY" pipeline.py refresh-agendas

# 4. Extract minutes from agenda approval PDFs
stage extract-minutes "$PY" pipeline.py extract-minutes

# 5. Download, transcribe, enrich, ingest new videos
stage process-videos  "$PY" process_videos.py

# 6. Write articles + fact-check via z.ai writer+editor
# DISABLED: articles now written by WireClaw agents after enrichment
# (see enrich-transcripts.sh on WireClaw VPS, 8:00 daily)

# 7. Polish upcoming meeting summaries
stage gen-summaries   "$PY" gen_summaries.py

# 8. Index: minutes → chunks, FTS rebuild, embed anything new.
# (chunks_fts is external-content with no triggers — without this stage,
#  freshly ingested chunks are invisible to keyword AND vector search)
stage ingest-minutes  "$PY" ingest_minutes.py
stage embed-new       "$PY" embeddings.py
# entity spellings re-verified against minutes/agendas/packets daily;
# unverified people show "(sp?)" + a reader-correction form on the site
stage verify-entities "$PY" verify_entities.py

echo "$(date '+%F %T'): === DAILY PIPELINE COMPLETE (fail=$FAIL) ===" >> "$LOG"
exit $FAIL
