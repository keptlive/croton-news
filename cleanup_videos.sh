#!/bin/bash
# Delete videos older than 60 days
find /opt/croton-news/videos/ -name '*.mp4' -mtime +60 -delete
# Log what was done
echo "$(date): Video cleanup ran, deleted videos older than 60 days" >> /var/log/croton-cleanup.log 2>&1
