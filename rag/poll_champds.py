#!/usr/bin/env python3
"""
Poll ChampDS for video-publication state of specific events.
Logs one CSV row per check to /var/log/champds_poll.csv:
    ts_utc, event_id, media_type, has_video, title, event_date
Usage: poll_champds.py 1144 1147 [more...]
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

LOG = "/var/log/champds_poll.csv"


def fetch(eid):
    url = f"https://playapi.champds.com/crotononhudsonny/event/{eid}"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"_err": str(e)}


def csv_safe(s):
    return (s or "").replace(",", " ").replace("\n", " ").replace("\r", " ")


def main():
    ids = sys.argv[1:] or ["1144", "1147"]
    new_file = not os.path.exists(LOG)
    with open(LOG, "a") as f:
        if new_file:
            f.write("ts_utc,event_id,media_type,has_video,title,event_date\n")
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for eid in ids:
            d = fetch(eid)
            if "_err" in d:
                f.write(f"{ts},{eid},ERROR,0,{csv_safe(d['_err'])[:80]},\n")
                continue
            ev = d.get("Event", {}) or {}
            mi = d.get("MediaInfo", {}) or {}
            media_type = csv_safe(mi.get("MediaType") or "")
            has_video = 1 if mi.get("MediaPath") else 0
            title = csv_safe(ev.get("EventTitle") or "")
            date = (ev.get("EventDateTimeCustomerLocal") or "")[:10]
            f.write(f"{ts},{eid},{media_type},{has_video},{title},{date}\n")
    print(f"logged to {LOG}")


if __name__ == "__main__":
    main()
