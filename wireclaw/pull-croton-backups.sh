#!/bin/bash
# Offsite pull of croton.news DB backups (runs on WireClaw box, cron 5:40 UTC).
# Pulls only the NEWEST backup of each database (WireClaw disk is tight),
# gzips it, keeps 3 generations. Alerts by email (via croton's notify.py)
# if the pull fails or the newest rag backup is stale.
set -u
DEST=/root/croton-backups
SSH="ssh -i /root/.ssh/andy_vps_key -o StrictHostKeyChecking=no -o BatchMode=yes"
HOST=root@192.210.135.200
NOTIFY="/opt/croton-news/venv/bin/python /opt/croton-news/rag/notify.py"
mkdir -p "$DEST"

fail() {
    $SSH $HOST "$NOTIFY --topic offsite-backup --cooldown 43200 'offsite backup pull failed' '$1 (on WireClaw box, /root/pull-croton-backups.sh)'"
    echo "$(date '+%F %T') FAIL: $1"
    exit 1
}

for name in rag comments tips photos ecode-summaries; do
    latest=$($SSH $HOST "ls -t /opt/croton-news/backups/${name}-*.db 2>/dev/null | head -1")
    [ -z "$latest" ] && { [ "$name" = "rag" ] && fail "no rag backups found on croton"; continue; }
    base=$(basename "$latest")
    [ -f "$DEST/$base.gz" ] && continue  # already pulled
    $SSH $HOST "cat $latest" | gzip > "$DEST/$base.gz.tmp" || fail "pull of $base failed"
    mv "$DEST/$base.gz.tmp" "$DEST/$base.gz"
    echo "$(date '+%F %T') pulled $base ($(du -h "$DEST/$base.gz" | cut -f1))"
    # keep last 3 per database
    ls -t "$DEST/${name}-"*.gz 2>/dev/null | tail -n +4 | xargs rm -f 2>/dev/null
done

# staleness guard: newest local rag copy must be <30h old
newest=$(ls -t "$DEST"/rag-*.gz 2>/dev/null | head -1)
if [ -n "$newest" ]; then
    age_h=$(( ($(date +%s) - $(stat -c %Y "$newest")) / 3600 ))
    [ "$age_h" -gt 30 ] && fail "newest offsite rag backup is ${age_h}h old"
fi
exit 0
