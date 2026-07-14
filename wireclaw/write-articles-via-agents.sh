#!/bin/bash
# Write articles via WireClaw agents (writer + editor)
# Uses batch-agent.js for reliable blocking execution.
# Called from enrich-transcripts.sh after enrichment completes.

LOG=/tmp/enrich-transcripts.log
CROTON="root@192.210.135.200"
SSH="ssh -i /root/.ssh/andy_vps_key -o StrictHostKeyChecking=no"
WIRECLAW_DIR=/root/wireclaw-cli
# venv python on croton — system python3 lacks project deps (this exact
# mismatch silently killed PDF extraction and publish_article before)
CPY=/opt/croton-news/venv/bin/python
FAIL=0

echo "$(date): === ARTICLE WRITING START ===" >> $LOG

# 1. Sync latest rag.db to WireClaw (enrichment may have updated it)
echo "$(date): Syncing latest rag.db..." >> $LOG
$SSH $CROTON "cat /opt/croton-news/rag/rag.db" > /root/croton-bot/data/rag.db 2>> $LOG
chmod 644 /root/croton-bot/data/rag.db

# 2. Find meetings that need articles
NEEDS_ARTICLES=$($SSH $CROTON "sqlite3 /opt/croton-news/rag/rag.db \"SELECT id, date, committee FROM meetings WHERE has_transcript = 1 AND (article IS NULL OR article = '') AND date > '2026-01-01' ORDER BY date DESC LIMIT 5;\"" 2>/dev/null)

if [ -z "$NEEDS_ARTICLES" ]; then
    echo "$(date): No meetings need articles" >> $LOG
    echo "$(date): === ARTICLE WRITING COMPLETE ===" >> $LOG
    exit 0
fi

echo "$(date): Meetings needing articles:" >> $LOG
echo "$NEEDS_ARTICLES" >> $LOG

# 3. For each meeting, run writer → editor → publish
# Use here-string (not pipe) to avoid stdin consumption by child processes
while IFS='|' read -r MID MDATE MCOMMITTEE; do
    [ -z "$MID" ] && continue
    echo "$(date): Writing article for meeting $MID ($MCOMMITTEE, $MDATE)..." >> $LOG

    # Writer agent — blocks until container completes
    WRITER_OUTPUT="${WIRECLAW_DIR}/groups/croton-article-writer/article-${MID}.json"
    rm -f "$WRITER_OUTPUT" 2>/dev/null

    cd "$WIRECLAW_DIR"
    WRITER_PROMPT="Write an article for meeting ID ${MID} (${MCOMMITTEE}, ${MDATE}). Query the database at /workspace/extra/croton-data/rag.db for the full transcript and minutes. Cross-reference names against the entities table. Write the article and save as JSON to /workspace/group/article-${MID}.json with keys: meeting_id, headline, quick_summary, key_actions, article, article_model, validation."
    # up to 2 attempts — model-API 529s are transient, and batch-agent exits 0
    # even when the API call failed (the output file is the only proof of work)
    for ATTEMPT in 1 2; do
        echo "$(date): Starting writer agent for meeting $MID (attempt $ATTEMPT)..." >> $LOG
        timeout 900 node dist/batch-agent.js croton-article-writer "$WRITER_PROMPT" \
          < /dev/null >> $LOG 2>&1
        WRITER_EXIT=$?
        echo "$(date): Writer agent exited with code $WRITER_EXIT for meeting $MID (attempt $ATTEMPT)" >> $LOG
        [ -f "$WRITER_OUTPUT" ] && break
        [ "$ATTEMPT" = "1" ] && { echo "$(date): No output — retrying writer for $MID in 60s..." >> $LOG; sleep 60; }
    done

    # Check for output file
    if [ ! -f "$WRITER_OUTPUT" ]; then
        echo "$(date): WARNING: Writer produced no file for meeting $MID (exit: $WRITER_EXIT)" >> $LOG
        FAIL=1
        continue
    fi

    echo "$(date): Writer produced article file for meeting $MID" >> $LOG

    # Editor/fact-checker agent — blocks until container completes
    EDITOR_OUTPUT="${WIRECLAW_DIR}/groups/croton-article-editor/checked-${MID}.json"
    rm -f "$EDITOR_OUTPUT" 2>/dev/null

    echo "$(date): Starting editor agent for meeting $MID..." >> $LOG
    # 1800s: the old 900s cap timed out mid-verification on a 10K-char article
    # (meeting 153, 2026-07-14) and blocked publication
    timeout 1800 node dist/batch-agent.js croton-article-editor \
      "Fact-check the article at /workspace/extra/writer-output/article-${MID}.json for meeting ID ${MID} (${MCOMMITTEE}, ${MDATE}). Load the source transcript from /workspace/extra/croton-data/rag.db. Check the entities table for correct names/titles. Save your result to /workspace/group/checked-${MID}.json with keys: meeting_id, headline, article, editor_result (PASS/CORRECTED/REJECT), corrections." \
      < /dev/null >> $LOG 2>&1

    EDITOR_EXIT=$?
    echo "$(date): Editor agent exited with code $EDITOR_EXIT for meeting $MID" >> $LOG

    # Determine which output to publish
    if [ -f "$EDITOR_OUTPUT" ]; then
        echo "$(date): Editor produced checked output for meeting $MID" >> $LOG
        FINAL="$EDITOR_OUTPUT"
        RESULT=$(python3 -c "import json; d=json.load(open('${EDITOR_OUTPUT}')); print(d.get('editor_result','CHECKED'))" 2>/dev/null || echo "CHECKED")
    else
        echo "$(date): BLOCKED: Editor produced no file for meeting $MID — refusing to publish unchecked article" >> $LOG
        FAIL=1
        continue
    fi

    # Block REJECT results
    if [ "$RESULT" = "REJECT" ]; then
        echo "$(date): BLOCKED: Editor REJECTED article for meeting $MID — not publishing" >> $LOG
        continue
    fi

    # Validate JSON before publishing
    if ! python3 -c "import json; d=json.load(open('${FINAL}')); assert d.get('headline'), 'no headline'; assert d.get('article'), 'no article'" 2>> $LOG; then
        echo "$(date): WARNING: Invalid JSON in $FINAL for meeting $MID — skipping" >> $LOG
        FAIL=1
        continue
    fi

    # 4. Publish to croton VPS
    echo "$(date): Publishing article for meeting $MID (editor: $RESULT)..." >> $LOG
    # Use a heredoc-based publish script to avoid quoting hell
    cat "$FINAL" | $SSH $CROTON "cat > /tmp/article-${MID}.json" 2>> $LOG
    $SSH $CROTON "$CPY /opt/croton-news/rag/publish_article.py /tmp/article-${MID}.json ${MID} wireclaw-agent-${RESULT}" >> $LOG 2>&1

    PUBLISH_EXIT=$?
    if [ $PUBLISH_EXIT -eq 0 ]; then
        echo "$(date): Successfully published article for meeting $MID (editor: $RESULT)" >> $LOG

        # Insert photos from video frames
        EVENT_ID=$($SSH $CROTON "sqlite3 /opt/croton-news/rag/rag.db \"SELECT event_id FROM meetings WHERE id=$MID;\"" 2>/dev/null)
        if [ -n "$EVENT_ID" ]; then
            echo "$(date): Inserting photos for meeting $MID (event $EVENT_ID)..." >> $LOG
            $SSH $CROTON "cd /opt/croton-news/rag && $CPY insert_photos.py $EVENT_ID" >> $LOG 2>&1
            PHOTO_EXIT=$?
            if [ $PHOTO_EXIT -eq 0 ]; then
                echo "$(date): Photos inserted for meeting $MID" >> $LOG
            else
                echo "$(date): WARNING: Photo insertion failed for meeting $MID (exit: $PHOTO_EXIT)" >> $LOG
            fi
        fi
    else
        echo "$(date): WARNING: Publish failed for meeting $MID (exit: $PUBLISH_EXIT)" >> $LOG
        FAIL=1
    fi

done <<< "$NEEDS_ARTICLES"

# 5. Index: chunk + FTS-rebuild + embed the new articles so they are
# searchable immediately (previously waited for the next 6:00 pipeline)
echo "$(date): Indexing new articles (chunks/FTS/embeddings)..." >> $LOG
$SSH $CROTON "cd /opt/croton-news/rag && export \$(grep -v '^#' .env | xargs) 2>/dev/null && $CPY ingest_minutes.py && $CPY embeddings.py" >> $LOG 2>&1 || FAIL=1

echo "$(date): === ARTICLE WRITING COMPLETE (fail=$FAIL) ===" >> $LOG
exit $FAIL
