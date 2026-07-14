#!/bin/bash
# Automated transcript enrichment + article writing via WireClaw agents
# Runs daily at 8 AM after croton discovery pipeline (6 AM)
# Pipeline: enrich transcripts → re-ingest chunks → write articles → fact-check → publish

LOG=/tmp/enrich-transcripts.log
CROTON="root@192.210.135.200"
SSH="ssh -i /root/.ssh/andy_vps_key -o StrictHostKeyChecking=no"
DATA=/root/croton-transcripts
WIRECLAW_DIR=/root/wireclaw-cli

echo "$(date): === ENRICHMENT PIPELINE START ===" >> $LOG

# 1. Sync transcripts + databases from croton
echo "$(date): Syncing data from croton..." >> $LOG
$SSH $CROTON "cd /opt/croton-news/rag && tar chzf - transcripts/transcript-*.json rag.db" | tar xzf - -C $DATA/ >> $LOG 2>&1
$SSH $CROTON "cat /opt/croton-news/scrapers/summaries.db" > $DATA/summaries.db 2>> $LOG
chmod 644 $DATA/transcripts/*.json $DATA/rag.db $DATA/summaries.db 2>/dev/null

# Also sync rag.db to writer/editor data path
cp $DATA/rag.db /root/croton-bot/data/rag.db
cp $DATA/code.db /root/croton-bot/data/code.db 2>/dev/null
chmod 644 /root/croton-bot/data/rag.db

# 2. Find transcripts needing deep enrichment (generic 'Speaker N' or
#    'Unknown Speaker' — the latter is what YouTube-caption BOE transcripts
#    have, and they were previously never picked up at all)
NEEDS_WORK=$(python3 -c "
import json, glob
for f in sorted(glob.glob('${DATA}/transcripts/transcript-*.json')):
    try:
        d = json.load(open(f))
        utts = d.get('utterances', [])
        if not utts: continue
        generic = sum(1 for u in utts
                      if u.get('speaker','').startswith(('Speaker ', 'Unknown')))
        if generic > 0:
            attempts = int(d.get('wireclaw_enrich_attempts', 0) or 0)
            if d.get('wireclaw_enriched'):
                attempts = max(attempts, 1)
            # Skip if nearly clean, or if we've tried 3+ times without
            # converging (e.g. 1160 kept 76 unmappable voices and would
            # otherwise re-enrich every day forever)
            if d.get('wireclaw_enriched') and generic <= 2:
                continue
            if attempts >= 3:
                continue
            import os; print(os.path.basename(f))
    except: pass
" 2>/dev/null)

if [ -z "$NEEDS_WORK" ]; then
    echo "$(date): No transcripts need enrichment" >> $LOG
else
    COUNT=$(echo "$NEEDS_WORK" | wc -l)
    echo "$(date): Found $COUNT transcripts needing enrichment" >> $LOG

    # 3. Process each transcript using batch-agent.js (blocks until done)
    for FILE in $NEEDS_WORK; do
        ID=$(echo "$FILE" | sed 's/transcript-//;s/\.json//')
        ENRICHED="${WIRECLAW_DIR}/groups/transcript-enricher/enriched-${ID}.json"
        echo "$(date): Processing $FILE..." >> $LOG

        # Remove old output and cache
        rm -f "$ENRICHED" 2>/dev/null

        # Run enricher agent via batch-agent.js (blocks until container completes)
        cd "$WIRECLAW_DIR"
        timeout 900 node dist/batch-agent.js transcript-enricher \
          "Enrich /workspace/extra/croton-data/transcripts/${FILE}. Do all 4 passes including diarization verification. SPLIT any merged utterances where two speakers are combined. Write to /workspace/group/enriched-${ID}.json." \
          < /dev/null >> $LOG 2>&1

        # Check if enriched file was created
        if [ -f "$ENRICHED" ]; then
            # Verify improvement
            GENERIC=$(python3 -c "
import json
d = json.load(open('${ENRICHED}'))
g = sum(1 for u in d['utterances'] if u.get('speaker','').startswith('Speaker '))
t = len(d['utterances'])
print(f'{g}/{t}')
" 2>/dev/null)
            echo "$(date): $FILE enriched (generic remaining: $GENERIC)" >> $LOG

            # Sync enriched transcript back to croton VPS
            cat "$ENRICHED" | $SSH $CROTON "cat > /opt/croton-news/rag/transcripts/${FILE}"
            echo "$(date): $FILE synced back to croton" >> $LOG

            # Re-ingest enriched chunks into RAG database with correct speaker names
            $SSH $CROTON "python3 -c \"
import json, sqlite3
with open('/opt/croton-news/rag/transcripts/${FILE}') as f:
    data = json.load(f)
event_id = str(data.get('event_id', '${ID}'))
db = sqlite3.connect('/opt/croton-news/rag/rag.db')
# Look up correct committee from meetings table (not transcript title)
row = db.execute('SELECT committee, date FROM meetings WHERE event_id = ? OR CAST(event_id AS TEXT) = ?', (event_id, event_id)).fetchone()
committee = row[0] if row else data.get('title','')
meeting_date = row[1] if row else data.get('date','')
# scope to transcript chunks only — an unscoped DELETE was wiping the
# minutes and article chunks for the doc on every re-enrichment
db.execute('DELETE FROM chunks WHERE doc_id = ? AND doc_type = ?', (event_id, 'transcript'))
idx = 0
for u in data.get('utterances', []):
    text = u.get('text','').strip()
    if len(text) < 30:
        continue  # Skip tiny chunks
    db.execute('INSERT INTO chunks (doc_id, doc_type, chunk_index, content, speaker, committee, date, start_time, end_time, char_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (event_id, 'transcript', idx, text, u.get('speaker','Unknown'), committee, meeting_date, u.get('start',0), u.get('end',0), len(text)))
    idx += 1
db.commit()
print(f'Re-ingested {idx} chunks for {event_id} (committee: {committee})')
\"" >> $LOG 2>&1
            echo "$(date): $FILE chunks re-ingested with enriched speakers" >> $LOG
        else
            echo "$(date): WARNING: $FILE enrichment failed (no output file)" >> $LOG
        fi

        # Stamp attempt count on the croton master copy (survives the next
        # run's sync) so non-converging transcripts stop re-enriching daily
        $SSH $CROTON "python3 -c \"
import json
p = '/opt/croton-news/rag/transcripts/${FILE}'
d = json.load(open(p))
d['wireclaw_enrich_attempts'] = int(d.get('wireclaw_enrich_attempts', 0) or 0) + 1
json.dump(d, open(p, 'w'))
print('attempts:', d['wireclaw_enrich_attempts'])
\"" >> $LOG 2>&1
    done
fi

echo "$(date): === ENRICHMENT PIPELINE COMPLETE ===" >> $LOG

# 4. Write articles for any meetings that need them (via WireClaw agents)
echo "$(date): Running article writing via WireClaw agents..." >> $LOG
/root/write-articles-via-agents.sh
