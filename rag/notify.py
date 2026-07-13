#!/usr/bin/env python3
"""Standalone alert mailer for the croton.news pipeline.

Usage:
    notify.py SUBJECT BODY
    echo "body" | notify.py SUBJECT -
    notify.py --topic boe-poll --cooldown 21600 SUBJECT BODY   # dedup window

Reads SMTP creds from /opt/croton-news/.env (SMTP_USER / SMTP_PASS).
Recipient defaults to ALERT_EMAIL from .env, falling back to the editor.
Cooldown: with --topic, a repeat alert inside the window is silently
dropped (stamp files in /opt/croton-news/rag/.alerts/).

Exit code 0 on send or cooldown-suppressed; 1 on failure to send
(failures also append to /var/log/croton-notify.log so a broken mailer
is still diagnosable).
"""
import os
import sys
import time
import smtplib
from email.mime.text import MIMEText

BASE = "/opt/croton-news"
ENV_PATH = os.path.join(BASE, ".env")
STAMP_DIR = os.path.join(BASE, "rag", ".alerts")
FALLBACK_LOG = "/var/log/croton-notify.log"

SMTP_HOST = "mail.cyberpersons.com"
SMTP_PORT = 587
SMTP_FROM = "editor@croton.news"


def load_env():
    env = {}
    try:
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    except OSError:
        pass
    return env


def log_fallback(msg):
    try:
        with open(FALLBACK_LOG, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def main():
    args = sys.argv[1:]
    topic, cooldown = None, 0
    while args and args[0].startswith("--"):
        if args[0] == "--topic":
            topic = args[1]
            args = args[2:]
        elif args[0] == "--cooldown":
            cooldown = int(args[1])
            args = args[2:]
        else:
            print(f"unknown flag {args[0]}", file=sys.stderr)
            return 1

    if len(args) < 2:
        print(__doc__, file=sys.stderr)
        return 1

    subject = args[0]
    body = sys.stdin.read() if args[1] == "-" else args[1]

    # Cooldown check
    if topic and cooldown:
        os.makedirs(STAMP_DIR, exist_ok=True)
        stamp = os.path.join(STAMP_DIR, f"{topic}.stamp")
        try:
            if time.time() - os.path.getmtime(stamp) < cooldown:
                return 0  # suppressed, still success for callers
        except OSError:
            pass

    env = load_env()
    user = env.get("SMTP_USER", "")
    password = env.get("SMTP_PASS", "")
    to = env.get("ALERT_EMAIL", "bpmatt@gmail.com")
    if not password:
        log_fallback(f"NO SMTP_PASS — dropped alert: {subject}")
        return 1

    msg = MIMEText(body)
    msg["Subject"] = f"[croton.news] {subject}"
    msg["From"] = SMTP_FROM
    msg["To"] = to

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.starttls()
            s.login(user, password)
            s.sendmail(SMTP_FROM, [to], msg.as_string())
    except Exception as e:  # noqa: BLE001 — any send failure must be logged, never raised
        log_fallback(f"SEND FAILED ({e}): {subject}")
        return 1

    if topic and cooldown:
        with open(os.path.join(STAMP_DIR, f"{topic}.stamp"), "w") as f:
            f.write(str(time.time()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
