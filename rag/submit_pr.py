#!/usr/bin/env python3
"""
Press release submission tool for hyperlocal news sites.

Generates a press release from a site's latest article and submits to
PRLog and OpenPR. Reusable across all news sites.

Usage:
    python3 submit_pr.py --site clayny.news --db /opt/clay-news/rag/rag.db
    python3 submit_pr.py --site cranberrytownship.news --db /opt/cranberry-news/rag/rag.db
    python3 submit_pr.py --site albion.news --db /opt/albion-news/rag/rag.db
    python3 submit_pr.py --site croton.news --db /opt/croton-news/rag/rag.db
    python3 submit_pr.py --generate-only --site clayny.news --db /opt/clay-news/rag/rag.db
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Load env
for env_path in [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    "/opt/croton-news/rag/.env",
    "/opt/croton-news/.env",
]:
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())

# LLM for press release generation
ZAI_URL = "https://api.z.ai/api/anthropic/v1/messages"
ZAI_KEY = os.environ.get("ZAI_KEY", "")

# Site metadata
SITE_INFO = {
    "clayny.news": {
        "name": "clayny.news",
        "full_name": "clayny.news — Clay, NY Local News",
        "location": "Clay, NY",
        "region": "Central New York",
        "description": "AI-assisted local news covering Town of Clay government meetings, zoning decisions, and community affairs",
        "url": "https://clayny.news",
        "topics": "Town Board, Planning Board, Zoning Board, Micron development",
    },
    "cranberrytownship.news": {
        "name": "cranberrytownship.news",
        "full_name": "cranberrytownship.news — Cranberry Township, PA Local News",
        "location": "Cranberry Township, PA",
        "region": "Western Pennsylvania",
        "description": "AI-assisted local news covering Cranberry Township government meetings and community affairs",
        "url": "https://cranberrytownship.news",
        "topics": "Board of Supervisors, Planning Commission, Seneca Valley School Board",
    },
    "albion.news": {
        "name": "albion.news",
        "full_name": "albion.news — Albion, NY Local News",
        "location": "Albion, NY",
        "region": "Western New York",
        "description": "AI-assisted local news covering Village of Albion government meetings and Orleans County affairs",
        "url": "https://albion.news",
        "topics": "Board of Trustees, Planning Board, Board of Education, Orleans County Legislature",
    },
    "croton.news": {
        "name": "croton.news",
        "full_name": "croton.news — Croton-on-Hudson, NY Local News",
        "location": "Croton-on-Hudson, NY",
        "region": "Hudson Valley, New York",
        "description": "AI-assisted civic journalism covering Croton-on-Hudson village government from public records and meeting transcripts",
        "url": "https://croton.news",
        "topics": "Board of Trustees, Planning Board, Zoning Board, Board of Education",
    },
}


def get_latest_article(db_path):
    """Get the most recent article with a headline."""
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    row = db.execute("""
        SELECT id, date, committee, headline, quick_summary, article
        FROM meetings
        WHERE article IS NOT NULL AND headline IS NOT NULL
        ORDER BY date DESC LIMIT 1
    """).fetchone()
    db.close()
    return dict(row) if row else None


def generate_press_release(site_key, article):
    """Use LLM to convert an article into AP-style press release."""
    info = SITE_INFO[site_key]

    prompt = f"""Convert this local news article into a professional press release (AP style).

SITE: {info['full_name']}
ARTICLE HEADLINE: {article['headline']}
ARTICLE DATE: {article['date']}
ARTICLE SUMMARY: {article['quick_summary']}
ARTICLE TEXT:
{article['article'][:4000]}

PRESS RELEASE REQUIREMENTS:
- Dateline: {info['location'].upper()} —
- 300-500 words, AP style
- Lead paragraph: who, what, when, where, why
- Include 1-2 direct quotes from the article (attributed properly)
- Final paragraph: boilerplate about {info['name']}
- Professional tone, third person
- End with: "For more information, visit {info['url']}"

BOILERPLATE (use at end):
About {info['name']}: {info['description']}. Covering {info['topics']}. Free at {info['url']}.

Respond with ONLY the press release text, no preamble."""

    try:
        data = json.dumps({
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 1500,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()

        req = urllib.request.Request(ZAI_URL, data=data, headers={
            "Content-Type": "application/json",
            "x-api-key": ZAI_KEY,
            "anthropic-version": "2023-06-01",
        })
        resp = urllib.request.urlopen(req, timeout=30)
        result = json.loads(resp.read())
        return result.get("content", [{}])[0].get("text", "")
    except Exception as e:
        print(f"  LLM error: {e}")
        return None


def submit_prlog(title, body, site_key, email="bpmatt@gmail.com"):
    """Submit press release to PRLog.org via their form.

    PRLog uses a simple POST form. No JS rendering needed.
    Returns the submission URL or error.
    """
    info = SITE_INFO[site_key]

    # PRLog form fields
    form_data = urllib.parse.urlencode({
        "title": title,
        "body": body,
        "summary": body[:200],
        "tags": f"local news, {info['location']}, government, community",
        "category": "News",
        "country": "United States",
        "state": "New York" if "NY" in info["location"] else "Pennsylvania",
        "city": info["location"].split(",")[0].strip(),
        "url": info["url"],
        "email": email,
    }).encode()

    try:
        req = urllib.request.Request(
            "https://www.prlog.org/pub/submit.html",
            data=form_data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://www.prlog.org/pub/",
            }
        )
        resp = urllib.request.urlopen(req, timeout=30)
        result_url = resp.geturl()
        html = resp.read().decode("utf-8", errors="ignore")

        if "success" in html.lower() or "submitted" in html.lower() or "review" in html.lower():
            print(f"  PRLog: Submitted successfully → {result_url}")
            return result_url
        elif "login" in html.lower() or "sign in" in html.lower():
            print(f"  PRLog: Requires login — save PR text and submit manually")
            return "needs_login"
        else:
            print(f"  PRLog: Unknown response — check {result_url}")
            return result_url
    except Exception as e:
        print(f"  PRLog error: {e}")
        return None


def submit_openpr(title, body, site_key, email="bpmatt@gmail.com"):
    """Submit press release to OpenPR.com via their form."""
    info = SITE_INFO[site_key]

    form_data = urllib.parse.urlencode({
        "headline": title,
        "text": body,
        "contact_name": "Matt Broudy",
        "contact_email": email,
        "company": info["name"],
        "website": info["url"],
        "city": info["location"].split(",")[0].strip(),
        "country": "USA",
        "category": "Media & Telecommunications",
    }).encode()

    try:
        req = urllib.request.Request(
            "https://www.openpr.com/news/submit.html",
            data=form_data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://www.openpr.com/news/submit.html",
            }
        )
        resp = urllib.request.urlopen(req, timeout=30)
        result_url = resp.geturl()
        html = resp.read().decode("utf-8", errors="ignore")

        if "success" in html.lower() or "submitted" in html.lower() or "thank" in html.lower():
            print(f"  OpenPR: Submitted successfully → {result_url}")
            return result_url
        elif "login" in html.lower() or "register" in html.lower():
            print(f"  OpenPR: Requires login — save PR text and submit manually")
            return "needs_login"
        else:
            print(f"  OpenPR: Unknown response — check {result_url}")
            return result_url
    except Exception as e:
        print(f"  OpenPR error: {e}")
        return None


def dispatch_to_wireclaw(title, body, site_key, targets=None):
    """Send PR submission task to WireClaw agent for browser-based submission.

    The agent can handle CAPTCHAs via solvecaptcha and Cloudflare challenges.
    """
    if targets is None:
        targets = ["prlog", "openpr"]

    info = SITE_INFO[site_key]
    AGENTWIRE_API_KEY = "cmo1qs5n10001di15v02wle2z"

    task = f"""TASK: Submit this press release to {', '.join(targets)}.

PRESS RELEASE TITLE: {title}

PRESS RELEASE BODY:
{body}

SUBMISSION DETAILS:
- Contact Name: Matt Broudy
- Contact Email: bpmatt@gmail.com
- Company: {info['name']}
- Website: {info['url']}
- City: {info['location'].split(',')[0].strip()}
- Country: USA
- Category: Media & Telecommunications / News

INSTRUCTIONS FOR EACH TARGET:

{'PRLog (https://www.prlog.org/pub/):'  if 'prlog' in targets else ''}
{'1. Go to https://www.prlog.org/pub/' if 'prlog' in targets else ''}
{'2. If not logged in, create account with bpmatt@gmail.com' if 'prlog' in targets else ''}
{'3. Fill in the press release form with the title and body above' if 'prlog' in targets else ''}
{'4. Submit and note the URL' if 'prlog' in targets else ''}

{'OpenPR (https://www.openpr.com/news/submit.html):' if 'openpr' in targets else ''}
{'1. Go to https://www.openpr.com/news/submit.html' if 'openpr' in targets else ''}
{'2. If account needed, create with bpmatt@gmail.com' if 'openpr' in targets else ''}
{'3. Fill in the press release form' if 'openpr' in targets else ''}
{'4. Submit and note the URL' if 'openpr' in targets else ''}

If you encounter a CAPTCHA, use the solvecaptcha service (key in your .env).
If you encounter Cloudflare, wait and retry.

Reply to bpmatt@gmail.com with the submission URLs when done."""

    try:
        trigger_data = json.dumps({
            "message": task,
            "source": "pr-submission",
        }).encode()

        req = urllib.request.Request(
            f"https://agentwire.run/api/agents/croton-writer/trigger",
            data=trigger_data,
            headers={
                "Authorization": f"Bearer {AGENTWIRE_API_KEY}",
                "Content-Type": "application/json",
            }
        )
        resp = urllib.request.urlopen(req, timeout=15)
        result = json.loads(resp.read())
        print(f"  WireClaw agent dispatched (id={result.get('id', '?')})")
        return True
    except Exception as e:
        print(f"  WireClaw dispatch failed: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Submit press releases for hyperlocal news sites")
    parser.add_argument("--site", required=True, choices=list(SITE_INFO.keys()),
                        help="Which news site")
    parser.add_argument("--db", required=True, help="Path to rag.db")
    parser.add_argument("--generate-only", action="store_true",
                        help="Just generate and print the PR, don't submit")
    parser.add_argument("--agent", action="store_true",
                        help="Dispatch to WireClaw agent for browser submission")
    parser.add_argument("--title", help="Custom title (overrides auto-generated)")
    parser.add_argument("--launch", action="store_true",
                        help="Generate a site launch PR instead of article PR")
    args = parser.parse_args()

    info = SITE_INFO[args.site]
    print(f"\nPress Release Tool — {info['full_name']}")
    print("=" * 60)

    if args.launch:
        # Generate a site launch press release
        title = f"New AI-Powered Local News Site Launches for {info['location']}"
        body = f"""{info['location'].upper()} — {info['name']}, a new independent local news website, has launched to provide free, in-depth coverage of local government meetings and community affairs in {info['location']}.

The site uses AI-assisted journalism to transform official meeting minutes into readable, detailed articles that help residents stay informed about decisions affecting their community.

"Local government coverage has been declining for years, and many residents don't have the time to attend meetings or read through dense minutes," said Matt Broudy, editor of {info['name']}. "We're using technology to fill that gap and make local government more transparent."

{info['name']} covers {info['topics']}, turning dense public records into accessible journalism. The site is completely free and ad-free, with coverage available at {info['url']}.

{info['name']} is part of a growing network of hyperlocal news sites using AI-assisted journalism to serve communities that have lost local news coverage. The network currently serves Croton-on-Hudson, NY (croton.news); Clay, NY (clayny.news); Albion, NY (albion.news); and Cranberry Township, PA (cranberrytownship.news).

About {info['name']}: {info['description']}. Free at {info['url']}. Contact: editor@croton.news."""

    else:
        # Get latest article and generate PR from it
        article = get_latest_article(args.db)
        if not article:
            print("No articles found in database")
            sys.exit(1)

        print(f"Latest article: {article['headline']} ({article['date']})")
        print("Generating press release...")

        body = generate_press_release(args.site, article)
        if not body:
            print("Failed to generate press release")
            sys.exit(1)

        title = args.title or article["headline"]

    print(f"\n{'='*60}")
    print(f"TITLE: {title}")
    print(f"{'='*60}")
    print(body)
    print(f"{'='*60}")
    print(f"Length: {len(body)} chars, ~{len(body.split())} words")

    if args.generate_only:
        print("\n(generate-only mode — not submitting)")
        return

    if args.agent:
        print("\nDispatching to WireClaw agent for browser submission...")
        dispatch_to_wireclaw(title, body, args.site)
    else:
        print("\nAttempting direct submission...")
        print("\n[1/2] PRLog...")
        submit_prlog(title, body, args.site)
        time.sleep(3)
        print("\n[2/2] OpenPR...")
        submit_openpr(title, body, args.site)


if __name__ == "__main__":
    main()
