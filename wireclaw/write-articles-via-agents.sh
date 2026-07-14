#!/bin/bash
# Write articles via WireClaw agents (writer + editor + publish gate)
# Uses batch-agent.js for reliable blocking execution.
# Called from enrich-transcripts.sh after enrichment completes.
#
# Per meeting: up to 2 GATE PASSES. Each pass = writer (2 attempts, 529s are
# transient) → editor (fact-check) → publish. publish_article.py runs the
# deterministic quality gate and exits 3 on violations — the violation
# report is fed back into the writer prompt and the whole pass retries
# immediately. Second gate block → FAIL (email via cron wrapper), meeting
# stays queued for the next run.

LOG=/tmp/enrich-transcripts.log
CROTON="root@192.210.135.200"
SSH="ssh -i /root/.ssh/andy_vps_key -o StrictHostKeyChecking=no"
WIRECLAW_DIR=/root/wireclaw-cli
# venv python on croton — system python3 lacks project deps
CPY=/opt/croton-news/venv/bin/python
FAIL=0

echo "$(date): === ARTICLE WRITING START ===" >> $LOG

# 1. Sync latest rag.db to WireClaw (enrichment may have updated it)
echo "$(date): Syncing latest rag.db..." >> $LOG
$SSH $CROTON "cat /opt/croton-news/rag/rag.db" > /root/croton-bot/data/rag.db 2>> $LOG
chmod 644 /root/croton-bot/data/rag.db

# 2. Find meetings that need articles — transcript-based OR minutes-based
# (minutes-only meetings like BOE sessions without processed video were
# invisible to the old has_transcript=1 filter; the writer prompt already
# supports minutes-based sourcing, and substantial minutes >2000 chars are
# article-worthy)
NEEDS_ARTICLES=$($SSH $CROTON "sqlite3 /opt/croton-news/rag/rag.db \"SELECT id, date, committee FROM meetings WHERE (has_transcript = 1 OR length(COALESCE(minutes_text,'')) > 2000) AND (article IS NULL OR article = '') AND date > '2026-01-01' AND date <= date('now') ORDER BY date DESC LIMIT 5;\"" 2>/dev/null)

if [ -z "$NEEDS_ARTICLES" ]; then
    echo "$(date): No meetings need articles" >> $LOG
    echo "$(date): === ARTICLE WRITING COMPLETE ===" >> $LOG
    exit 0
fi

echo "$(date): Meetings needing articles:" >> $LOG
echo "$NEEDS_ARTICLES" >> $LOG

# 3. For each meeting: gate-pass loop of (writer → editor → publish)
while IFS='|' read -r MID MDATE MCOMMITTEE; do
    [ -z "$MID" ] && continue
    echo "$(date): Writing article for meeting $MID ($MCOMMITTEE, $MDATE)..." >> $LOG

    WRITER_OUTPUT="${WIRECLAW_DIR}/groups/croton-article-writer/article-${MID}.json"
    EDITOR_OUTPUT="${WIRECLAW_DIR}/groups/croton-article-editor/checked-${MID}.json"
    # preload violations from a prior run's gate block so the first pass of
    # THIS run already knows what failed last time
    GATE_FEEDBACK=$($SSH $CROTON "cat /opt/croton-news/rag/validation/article-${MID}-report.json 2>/dev/null" < /dev/null | python3 -c "import json,sys
try:
    d = json.load(sys.stdin)
    if not d.get('passed', True):
        print(json.dumps(d['violations']))
except Exception: pass" 2>/dev/null)
    MEETING_DONE=0

    for GATE_PASS in 1 2; do
        rm -f "$WRITER_OUTPUT" "$EDITOR_OUTPUT" 2>/dev/null
        cd "$WIRECLAW_DIR"

        WRITER_PROMPT="Write an article for meeting ID ${MID} (${MCOMMITTEE}, ${MDATE}). Query the database at /workspace/extra/croton-data/rag.db for the full transcript and minutes. Cross-reference names against the entities table. Write the article and save as JSON to /workspace/group/article-${MID}.json with keys: meeting_id, headline, quick_summary, key_actions, article, article_model, validation."
        if [ -n "$GATE_FEEDBACK" ]; then
            WRITER_PROMPT="$WRITER_PROMPT

IMPORTANT — your previous draft was BLOCKED by the automated publish gate for the violations below. Fix every one: quote attributions must match the transcript speaker at the timestamp, quoted text must be verbatim, and every name and dollar figure must appear in a source document. If a fact cannot be sourced, remove it.
$GATE_FEEDBACK"
        fi

        # writer: up to 2 attempts (model-API 529s are transient; batch-agent
        # exits 0 even on API failure — the output file is the proof of work)
        for ATTEMPT in 1 2; do
            echo "$(date): Starting writer agent for meeting $MID (pass $GATE_PASS, attempt $ATTEMPT)..." >> $LOG
            timeout 900 node dist/batch-agent.js croton-article-writer "$WRITER_PROMPT" \
              < /dev/null >> $LOG 2>&1
            WRITER_EXIT=$?
            echo "$(date): Writer agent exited with code $WRITER_EXIT for meeting $MID (pass $GATE_PASS, attempt $ATTEMPT)" >> $LOG
            [ -f "$WRITER_OUTPUT" ] && break
            if [ "$ATTEMPT" = "1" ]; then
            # 529-aware backoff: provider overload/auth issues need longer than a blip
            if tail -40 $LOG | grep -q "API Error: 529"; then
                echo "$(date): API 529 detected — retrying writer for $MID in 300s..." >> $LOG; sleep 300
            else
                echo "$(date): No output — retrying writer for $MID in 60s..." >> $LOG; sleep 60
            fi
        fi
        done

        if [ ! -f "$WRITER_OUTPUT" ]; then
            echo "$(date): WARNING: Writer produced no file for meeting $MID (exit: $WRITER_EXIT)" >> $LOG
            FAIL=1
            break
        fi
        echo "$(date): Writer produced article file for meeting $MID" >> $LOG

        # editor/fact-checker (1800s: 900s once timed out mid-verification)
        echo "$(date): Starting editor agent for meeting $MID..." >> $LOG
        timeout 1800 node dist/batch-agent.js croton-article-editor \
          "Fact-check the article at /workspace/extra/writer-output/article-${MID}.json for meeting ID ${MID} (${MCOMMITTEE}, ${MDATE}). Load the source transcript from /workspace/extra/croton-data/rag.db. Check the entities table for correct names/titles. Save your result to /workspace/group/checked-${MID}.json with keys: meeting_id, headline, article, editor_result (PASS/CORRECTED/REJECT), corrections." \
          < /dev/null >> $LOG 2>&1
        EDITOR_EXIT=$?
        echo "$(date): Editor agent exited with code $EDITOR_EXIT for meeting $MID" >> $LOG

        if [ -f "$EDITOR_OUTPUT" ]; then
            FINAL="$EDITOR_OUTPUT"
            RESULT=$(python3 -c "import json; d=json.load(open('${EDITOR_OUTPUT}'), strict=False); print(d.get('editor_result','CHECKED'))" 2>/dev/null || echo "CHECKED")
        else
            echo "$(date): BLOCKED: Editor produced no file for meeting $MID — refusing to publish unchecked article" >> $LOG
            FAIL=1
            break
        fi

        if [ "$RESULT" = "REJECT" ]; then
            echo "$(date): BLOCKED: Editor REJECTED article for meeting $MID — not publishing" >> $LOG
            break
        fi

        if ! python3 -c "import json; d=json.load(open('${FINAL}'), strict=False); assert d.get('headline'), 'no headline'; assert d.get('article'), 'no article'" 2>> $LOG; then
            echo "$(date): WARNING: Invalid JSON in $FINAL for meeting $MID — skipping" >> $LOG
            FAIL=1
            break
        fi

        # publish (runs the deterministic quality gate on croton)
        echo "$(date): Publishing article for meeting $MID (pass $GATE_PASS, editor: $RESULT)..." >> $LOG
        cat "$FINAL" | $SSH $CROTON "cat > /tmp/article-${MID}.json" 2>> $LOG
        $SSH $CROTON "$CPY /opt/croton-news/rag/publish_article.py /tmp/article-${MID}.json ${MID} wireclaw-agent-${RESULT}" < /dev/null >> $LOG 2>&1
        PUBLISH_EXIT=$?

        if [ $PUBLISH_EXIT -eq 0 ]; then
            echo "$(date): Successfully published article for meeting $MID (pass $GATE_PASS, editor: $RESULT)" >> $LOG
            MEETING_DONE=1

            EVENT_ID=$($SSH $CROTON "sqlite3 /opt/croton-news/rag/rag.db \"SELECT event_id FROM meetings WHERE id=$MID;\"" < /dev/null 2>/dev/null)
            if [ -n "$EVENT_ID" ]; then
                echo "$(date): Inserting photos for meeting $MID (event $EVENT_ID)..." >> $LOG
                $SSH $CROTON "cd /opt/croton-news/rag && $CPY insert_photos.py $EVENT_ID" < /dev/null >> $LOG 2>&1 \
                    || echo "$(date): WARNING: Photo insertion failed for meeting $MID" >> $LOG
            fi
            break
        elif [ $PUBLISH_EXIT -eq 3 ]; then
            echo "$(date): GATE BLOCKED article for meeting $MID (pass $GATE_PASS)" >> $LOG
            GATE_FEEDBACK=$($SSH $CROTON "cat /opt/croton-news/rag/validation/article-${MID}-report.json" < /dev/null 2>/dev/null)
            if [ "$GATE_PASS" = "2" ]; then
                echo "$(date): Gate blocked twice for meeting $MID — giving up this run" >> $LOG
                FAIL=1
            else
                echo "$(date): Retrying immediately with violation feedback..." >> $LOG
            fi
        else
            echo "$(date): WARNING: Publish failed for meeting $MID (exit: $PUBLISH_EXIT)" >> $LOG
            FAIL=1
            break
        fi
    done

    [ "$MEETING_DONE" = "1" ] || echo "$(date): Meeting $MID NOT published this run" >> $LOG

done <<< "$NEEDS_ARTICLES"

# 5. Index: chunk + FTS-rebuild + embed the new articles so they are
# searchable immediately (previously waited for the next 6:00 pipeline)
echo "$(date): Indexing new articles (chunks/FTS/embeddings)..." >> $LOG
$SSH $CROTON "cd /opt/croton-news/rag && export \$(grep -v '^#' .env | xargs) 2>/dev/null && $CPY ingest_minutes.py && $CPY embeddings.py" >> $LOG 2>&1 || FAIL=1

echo "$(date): === ARTICLE WRITING COMPLETE (fail=$FAIL) ===" >> $LOG
exit $FAIL
