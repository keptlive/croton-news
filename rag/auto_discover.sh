#!/bin/bash
# croton.news — full daily pipeline
# Cron: 0 6 * * * /opt/croton-news/rag/auto_discover.sh
cd /opt/croton-news/rag
export $(grep -v "^#" .env | xargs) 2>/dev/null
LOG=/tmp/croton-discover.log

echo "$(date): === DAILY PIPELINE START ===" >> $LOG

# 1. Discover new meetings
python3 pipeline.py discover >> $LOG 2>&1

# 2. Match orphan meetings to ChampDS events
python3 pipeline.py match-orphans >> $LOG 2>&1

# 3. Refresh agendas + check for new video
python3 pipeline.py refresh-agendas >> $LOG 2>&1

# 4. Extract minutes from agenda approval PDFs
python3 pipeline.py extract-minutes >> $LOG 2>&1

# 5. Download, transcribe, enrich, ingest new videos
python3 process_videos.py >> $LOG 2>&1

# 6. Write articles + fact-check via z.ai writer+editor
python3 write_and_check.py >> $LOG 2>&1

# 7. Polish upcoming meeting summaries
python3 gen_summaries.py >> $LOG 2>&1

echo "$(date): === DAILY PIPELINE COMPLETE ===" >> $LOG
