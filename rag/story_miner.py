#!/usr/bin/env python3
"""
Story idea mining agent for croton.news.

Scans recent meeting transcripts and articles for potential feature stories,
then emails candidates to the site owner for editorial review.

Story Type: COMMUNITY SPOTLIGHT
Identifies positive community projects driven by an individual (not a government
official doing their job). The individual should be someone the editor could
contact for a standalone feature story.

MATCHES (examples from calibration):
  - Bruce Odland donating "Harmonic Landing" sound installation to Croton Landing Park
  - A new resident volunteering to research balcony solar systems for the community
  - Student officers creating a mentorship program bridging high school and elementary

DOES NOT MATCH:
  - Village budget adoption, tax levies, fiscal items (government business)
  - Zoning variances for home additions (individual but not community project)
  - Cannabis dispensary debates (controversial, not positive)
  - Police body cameras, parking rate changes (policy)
  - Infrastructure repairs, equipment purchases (routine operations)
  - Trustees/mayor/manager doing their official duties

Usage:
    python3 story_miner.py scan              # Scan recent meetings, print candidates
    python3 story_miner.py scan --email      # Scan and email results to editor
    python3 story_miner.py scan --since 2026-04-01  # Scan from specific date
    python3 story_miner.py history           # Show previously identified stories
"""

import json
import os
import smtplib
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# Load .env
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

# Also load parent .env (for SMTP)
_parent_env = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
if os.path.exists(_parent_env):
    with open(_parent_env) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RAG_DB = os.path.join(BASE_DIR, "rag.db")
TRANSCRIPTS_DIR = os.path.join(BASE_DIR, "transcripts")

# LLM
ZAI_URL = "https://api.z.ai/api/anthropic/v1/messages"
ZAI_KEY = os.environ.get("ZAI_KEY", "")

# Email
SMTP_HOST = "mail.cyberpersons.com"
SMTP_PORT = 587
SMTP_USER = os.environ.get("SMTP_USER", "smtp_1c45c43cd1597103")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = "editor@croton.news"
OWNER_EMAIL = "bpmatt@gmail.com"
EDITOR_EMAIL = "editor@croton.news"
AGENT_EMAIL = "croton-writer@agentwire.email"
SITE_URL = "https://croton.news"

# Track what we've already flagged
STORIES_DB = os.path.join(BASE_DIR, "story_ideas.db")


def get_rag_db():
    db = sqlite3.connect(RAG_DB)
    db.row_factory = sqlite3.Row
    return db


def get_stories_db():
    db = sqlite3.connect(STORIES_DB)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS story_ideas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT,
            meeting_date TEXT,
            committee TEXT,
            story_type TEXT DEFAULT 'community_spotlight',
            person_name TEXT,
            project_name TEXT,
            summary TEXT,
            transcript_excerpt TEXT,
            confidence TEXT,  -- high, medium
            status TEXT DEFAULT 'new',  -- new, emailed, dispatched, draft_ready, approved, rejected, written
            approval_token TEXT,
            draft_article TEXT,
            outreach_email_subject TEXT,
            outreach_email_body TEXT,
            contact_email TEXT,
            agent_dispatched_at TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(event_id, person_name)
        );
    """)
    return db


def scan_meeting(event_id, article_text, complete_summary, transcript_text, committee, date):
    """Use LLM to identify community spotlight story candidates in a meeting.

    Returns list of dicts or empty list.
    """
    # Build context: key actions + article excerpt + transcript excerpt
    # Keep total context under ~6000 chars to avoid API limits
    context_parts = []
    if complete_summary:
        context_parts.append(f"KEY ACTIONS:\n{complete_summary[:1500]}")
    if article_text:
        context_parts.append(f"ARTICLE EXCERPT:\n{article_text[:2500]}")
    if transcript_text:
        context_parts.append(f"TRANSCRIPT EXCERPT:\n{transcript_text[:2000]}")

    context = "\n\n".join(context_parts)

    prompt = f"""You are a local news editor scanning a {committee} meeting from {date} in Croton-on-Hudson, NY for potential feature stories.

STORY TYPE: Community Spotlight
You are looking for POSITIVE community projects driven by a NAMED INDIVIDUAL who is NOT a government official doing their regular job.

The individual should be someone the editor could contact for a standalone human-interest feature story about their project or contribution.

PERFECT EXAMPLES (from real meetings):
- Bruce Odland, a sound artist, donating a "Harmonic Landing" sound installation to Croton Landing Park — transforms highway/train noise into harmony, presented at MASS MoCA, artist donating at no cost
- A new resident volunteering to research "balcony solar" systems and report back to the Sustainability Committee
- Student officers who created "Leaders of Tomorrow," a mentorship program pairing high schoolers with elementary students

DOES NOT QUALIFY:
- Government officials doing their jobs (mayor, manager, trustees, superintendent presenting budgets, policies)
- Routine village business: budgets, tax levies, zoning variances, parking rates, equipment purchases
- Controversial or divisive issues (cannabis dispensaries, court consolidations)
- Anonymous community groups without a named point of contact
- Organizations making standard donations without a human story (corporate sponsors, generic charity)
- Infrastructure projects, road repairs, utility work

Meeting content:
{context}

If you find ANY stories matching this criteria, respond with a JSON array:
[
  {{
    "person_name": "Full Name",
    "project_name": "Short project title",
    "summary": "2-3 sentence pitch for why this is a feature story. Include what the person is doing, why it matters to the community, and what makes it human-interest worthy.",
    "confidence": "high or medium",
    "transcript_quote": "A key quote or detail from the transcript that captures the story"
  }}
]

If NO stories match the criteria, respond with an empty array: []

Be VERY selective. Most meetings will have zero matches. Only flag stories where there is a clearly named non-government individual driving a positive community initiative. Quality over quantity."""

    try:
        req_data = json.dumps({
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()

        req = urllib.request.Request(ZAI_URL, data=req_data, headers={
            "Content-Type": "application/json",
            "x-api-key": ZAI_KEY,
            "anthropic-version": "2023-06-01",
        })
        resp = urllib.request.urlopen(req, timeout=60)
        raw = resp.read()
        if not raw:
            print(f"  LLM returned empty response for {event_id}")
            return []
        result = json.loads(raw)
        text = result.get("content", [{}])[0].get("text", "")

        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            text = text.rsplit("```", 1)[0]
        text = text.strip()

        stories = json.loads(text)
        return stories if isinstance(stories, list) else []

    except Exception as e:
        print(f"  LLM scan failed for {event_id}: {e}")
        return []


def load_transcript_text(event_id, max_minutes=None):
    """Load transcript as speaker-attributed text.

    If max_minutes is set, truncate to that duration. Otherwise returns full transcript.
    """
    path = os.path.join(TRANSCRIPTS_DIR, f"transcript-{event_id}.json")
    if not os.path.exists(path):
        return ""
    with open(path) as f:
        tx = json.load(f)
    utterances = tx.get("utterances", [])
    speaker_map = tx.get("speaker_map", {})
    lines = []
    for u in utterances:
        if max_minutes and u.get("start", 0) > max_minutes * 60:
            break
        speaker = u.get("speaker", "?")
        num = speaker.replace("Speaker ", "")
        if num in speaker_map:
            speaker = speaker_map[num]
        lines.append(f"[{int(u.get('start', 0))}s] {speaker}: {u.get('text', '')}")
    return "\n".join(lines)


def scan_recent(since_date=None, send_email=False):
    """Scan recent meetings for story candidates."""
    rag = get_rag_db()
    sdb = get_stories_db()

    if since_date is None:
        since_date = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")

    meetings = rag.execute("""
        SELECT event_id, date, committee, article, complete_summary
        FROM meetings
        WHERE date >= ?
        AND (article IS NOT NULL OR complete_summary IS NOT NULL)
        AND event_id IS NOT NULL
        ORDER BY date DESC
    """, (since_date,)).fetchall()

    print(f"Scanning {len(meetings)} meetings since {since_date}...")

    all_stories = []
    for m in meetings:
        eid = m["event_id"]

        # Skip if already scanned
        existing = sdb.execute(
            "SELECT id FROM story_ideas WHERE event_id = ?", (eid,)
        ).fetchone()
        if existing:
            continue

        print(f"  {m['date']} {m['committee']} (event {eid})...")

        # Load transcript text
        tx_text = load_transcript_text(eid)

        stories = scan_meeting(
            eid, m["article"], m["complete_summary"],
            tx_text, m["committee"], m["date"]
        )

        if stories:
            for s in stories:
                print(f"    -> {s['person_name']}: {s['project_name']} [{s['confidence']}]")
                # Save to stories DB
                try:
                    sdb.execute("""
                        INSERT OR IGNORE INTO story_ideas
                        (event_id, meeting_date, committee, person_name, project_name,
                         summary, transcript_excerpt, confidence)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (eid, m["date"], m["committee"],
                          s["person_name"], s["project_name"],
                          s["summary"], s.get("transcript_quote", ""),
                          s.get("confidence", "medium")))
                    sdb.commit()
                except Exception:
                    pass
                all_stories.append({**s, "date": m["date"], "committee": m["committee"], "event_id": eid})
        else:
            # Mark as scanned with no results
            try:
                sdb.execute("""
                    INSERT OR IGNORE INTO story_ideas
                    (event_id, meeting_date, committee, person_name, project_name,
                     summary, confidence, status)
                    VALUES (?, ?, ?, '', '(no stories found)', '', 'none', 'scanned')
                """, (eid, m["date"], m["committee"]))
                sdb.commit()
            except Exception:
                pass

        time.sleep(1)  # rate limit

    rag.close()

    print(f"\nFound {len(all_stories)} story candidates")

    if all_stories and send_email:
        # Email owner with story leads
        email_owner_notification(all_stories)

        # Dispatch each story to the GLM agent for research + writing
        for s in all_stories:
            eid = s["event_id"]
            tx_excerpt = load_transcript_text(eid)
            dispatch_to_agent(s, eid, s["committee"], s["date"], tx_excerpt)
            time.sleep(2)  # space out agent dispatches

    sdb.close()
    return all_stories


def email_stories(stories):
    """Email story candidates to the editor."""
    if not SMTP_PASS:
        print("SMTP not configured — printing instead:")
        for s in stories:
            print(f"\n  {s['person_name']}: {s['project_name']}")
            print(f"  {s['summary']}")
        return

    # Build email body
    subject = f"croton.news: {len(stories)} story idea{'s' if len(stories) != 1 else ''} from recent meetings"

    text_parts = [
        "Community Spotlight Story Ideas",
        "=" * 40,
        "",
        f"Found {len(stories)} potential feature stories from recent meetings.",
        "Each involves a named community member driving a positive project.",
        "",
    ]

    html_parts = [
        "<html><body>",
        "<h2 style='color:#8b2500;font-family:Georgia,serif'>Community Spotlight Story Ideas</h2>",
        f"<p style='color:#666'>Found {len(stories)} potential feature {'stories' if len(stories) != 1 else 'story'} from recent meetings. "
        "Each involves a named community member driving a positive project.</p>",
        "<hr style='border:1px solid #eee'>",
    ]

    for i, s in enumerate(stories, 1):
        confidence_color = "#059669" if s.get("confidence") == "high" else "#d97706"
        confidence_label = s.get("confidence", "medium").upper()

        text_parts.extend([
            f"--- Story {i} ---",
            f"Person: {s['person_name']}",
            f"Project: {s['project_name']}",
            f"Meeting: {s['date']} {s['committee']}",
            f"Confidence: {confidence_label}",
            f"",
            f"{s['summary']}",
            "",
            f"Key detail: \"{s.get('transcript_quote', 'N/A')}\"",
            "",
        ])

        html_parts.append(f"""
        <div style="margin:20px 0;padding:16px;border-left:4px solid {confidence_color};background:#fafafa;border-radius:0 6px 6px 0">
            <div style="display:flex;justify-content:space-between;align-items:center">
                <h3 style="margin:0;color:#1a1a1a;font-family:Georgia,serif">{s['person_name']}</h3>
                <span style="font-size:11px;padding:2px 8px;background:{confidence_color};color:white;border-radius:10px;font-weight:600">{confidence_label}</span>
            </div>
            <div style="font-size:14px;color:#8b2500;font-weight:600;margin:4px 0">{s['project_name']}</div>
            <div style="font-size:12px;color:#888;margin-bottom:8px">{s['date']} &mdash; {s['committee']}</div>
            <p style="margin:8px 0;font-size:14px;line-height:1.5;color:#333">{s['summary']}</p>
            <blockquote style="margin:8px 0;padding:8px 12px;background:#f0f0f0;border-radius:4px;font-size:13px;color:#555;font-style:italic">
                "{s.get('transcript_quote', 'N/A')}"
            </blockquote>
        </div>
        """)

    text_parts.extend([
        "---",
        "Reply to this email to approve, reject, or request changes.",
        "Stories will not be written until you approve them.",
    ])

    html_parts.extend([
        "<hr style='border:1px solid #eee'>",
        "<p style='font-size:12px;color:#999'>Reply to approve, reject, or request changes. "
        "Stories will not be written until you approve them.</p>",
        "</body></html>",
    ])

    body_text = "\n".join(text_parts)
    body_html = "\n".join(html_parts)

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = f"croton.news Story Miner <{SMTP_FROM}>"
        msg["To"] = EDITOR_EMAIL
        msg["Subject"] = subject
        msg.attach(MIMEText(body_text, "plain"))
        msg.attach(MIMEText(body_html, "html"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_FROM, [EDITOR_EMAIL], msg.as_string())

        print(f"Email sent to {EDITOR_EMAIL}: {subject}")
    except Exception as e:
        print(f"Email failed: {e}")
        print("Stories found but could not be emailed. Saved to story_ideas.db.")


def dispatch_to_agent(story, event_id, committee, date, transcript_excerpt):
    """Send a research+write task to the GLM-5-Turbo agent via email.

    The agent will research the person, write a draft article, draft an outreach
    email, and reply to editor@croton.news with the results.
    """
    if not SMTP_PASS:
        print(f"  SMTP not configured — cannot dispatch to agent")
        return

    person = story["person_name"]
    project = story["project_name"]
    summary = story["summary"]

    # Generate approval token
    token = str(uuid.uuid4())

    # Save token to DB
    sdb = get_stories_db()
    sdb.execute("""
        UPDATE story_ideas SET approval_token = ?, status = 'dispatched',
        agent_dispatched_at = datetime('now')
        WHERE event_id = ? AND person_name = ?
    """, (token, event_id, person))
    sdb.commit()
    sdb.close()

    subject = f"TASK: Write Community Spotlight — {person}, {project}"

    task_body = f"""You are a local news journalist for croton.news, a hyperlocal news site covering Croton-on-Hudson, NY.

TASK: Research and write a Community Spotlight feature article with cited sources, plus draft an outreach email.

STORY CANDIDATE:
- Person: {person}
- Project: {project}
- Meeting: {committee}, {date}
- Summary: {summary}

FULL TRANSCRIPT (from the meeting — use this as your primary source):
{transcript_excerpt}

INSTRUCTIONS:

1. RESEARCH (CRITICAL — do this thoroughly):
   Use web search to find independent information about {person} and {project}. You MUST cite your sources.

   Search for:
   - "{person}" — their website, portfolio, bio, social media
   - "{project}" — the project's history, prior installations, press coverage
   - "{person} {project}" — any articles or mentions connecting them
   - "{person} artist" or "{person} Croton" — relevant context
   - Similar projects in other communities for comparison
   - Contact information (email, website) if publicly available

   For EVERY fact you include that doesn't come from the meeting transcript, you MUST note the source.
   If you cannot verify a fact independently, say so or omit it.

2. WRITE ARTICLE (~800-1000 words): Write an engaging, well-sourced feature article:

   Structure:
   - Lead with what makes this person/project compelling for Croton residents
   - Background on the person from your research (WITH SOURCE CITATIONS)
   - Context from the meeting: what was presented, how the board reacted
   - At least one direct quote from the meeting transcript
   - Independent context: similar projects, the person's other work (WITH SOURCES)
   - What comes next: timeline, how residents can learn more or get involved

   Citation style: Use inline links or footnotes. Example:
   "Odland's Harmonic Bridge installation at MASS MoCA drew national attention ([New York Times, 2019](url))."

   Tone: positive, community-focused, but real journalism — verify claims, cite sources.
   NO AI disclaimers. Write as a human journalist would.

3. SOURCES SECTION: After the article, list all sources used:
   SOURCES:
   [1] Source title — URL
   [2] Source title — URL
   [3] Meeting transcript, {committee}, {date} — croton.news

4. DRAFT OUTREACH EMAIL to {person}:
   - Subject line referencing their project
   - Greeting using their name
   - Introduce croton.news as a local news site covering village government
   - Mention their project was discussed at the {committee} meeting on {date}
   - Say you'd love to publish a feature article about their work
   - Before publishing, you wanted to reach out for their input:
     * Share any comments or additional context
     * Provide supplementary materials (photos, documents) as attachments
     * Or let us know if they'd prefer the article not be published — no hard feelings
   - They can simply reply to this email
   - Sign off as "croton.news editorial team, editor@croton.news"

5. REPLY FORMAT — send your reply via email to bpmatt@gmail.com (NOT editor@croton.news).

Include this review link at the TOP of your email so the editor can approve/tweak/rewrite/deny:
REVIEW: {SITE_URL}/api/story/review/{token}

Then use this exact structure:

CONTACT_EMAIL: [their email if found publicly, or "not found"]
CONTACT_WEBSITE: [their website if found, or "not found"]

---ARTICLE---
HEADLINE: [your headline]

[your full article text with inline source citations]

SOURCES:
[1] ...
[2] ...

---OUTREACH_EMAIL---
SUBJECT: [outreach email subject]

[outreach email body]

---END---

APPROVAL_TOKEN: {token}"""

    # Dispatch via AgentWire trigger API (more reliable than email→SSE chain)
    AGENTWIRE_API_KEY = "cmo1qs5n10001di15v02wle2z"
    AGENTWIRE_URL = "https://agentwire.run"
    AGENT_HANDLE = "croton-writer"  # GLM-5 via z.ai (free)

    try:
        trigger_data = json.dumps({
            "message": task_body,
            "source": "story-miner",
            "replyTo": OWNER_EMAIL,
        }).encode()

        req = urllib.request.Request(
            f"{AGENTWIRE_URL}/api/agents/{AGENT_HANDLE}/trigger",
            data=trigger_data,
            headers={
                "Authorization": f"Bearer {AGENTWIRE_API_KEY}",
                "Content-Type": "application/json",
            }
        )
        resp = urllib.request.urlopen(req, timeout=30)
        result = json.loads(resp.read())
        print(f"  Agent task dispatched via trigger API: {person} / {project} (id={result.get('id', '?')})")
    except Exception as e:
        print(f"  Trigger API failed, falling back to email: {e}")
        # Fallback to email
        try:
            msg = MIMEMultipart("alternative")
            msg["From"] = f"croton.news Story Pipeline <{SMTP_FROM}>"
            msg["To"] = AGENT_EMAIL
            msg["Reply-To"] = EDITOR_EMAIL
            msg["Subject"] = subject
            msg.attach(MIMEText(task_body, "plain"))

            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASS)
                server.sendmail(SMTP_FROM, [AGENT_EMAIL], msg.as_string())

            print(f"  Agent task dispatched via email to {AGENT_EMAIL}")
        except Exception as e2:
            print(f"  Agent dispatch failed completely: {e2}")


def email_owner_notification(stories):
    """Email the site owner about story candidates and that agents are working on them."""
    if not SMTP_PASS:
        print("  SMTP not configured")
        return

    subject = f"croton.news: {len(stories)} story lead{'s' if len(stories) != 1 else ''} — agents researching"

    text_parts = [
        "Community Spotlight — New Story Leads",
        "=" * 40,
        "",
        f"Found {len(stories)} potential feature stories. GLM agents have been dispatched to",
        "research and write drafts. You'll receive the drafts with approve/reject links.",
        "",
    ]

    html_parts = [
        "<html><body style='font-family:Georgia,serif;max-width:600px;margin:0 auto'>",
        "<h2 style='color:#8b2500'>Community Spotlight — New Leads</h2>",
        f"<p style='color:#666'>Found {len(stories)} potential feature {'stories' if len(stories) != 1 else 'story'}. "
        "GLM agents have been dispatched to research and write drafts. "
        "You'll receive the drafts with approve/reject links shortly.</p>",
        "<hr style='border:1px solid #eee'>",
    ]

    for i, s in enumerate(stories, 1):
        confidence_color = "#059669" if s.get("confidence") == "high" else "#d97706"

        text_parts.extend([
            f"Story {i}: {s['person_name']}",
            f"  Project: {s['project_name']}",
            f"  Meeting: {s['date']} {s['committee']}",
            f"  {s['summary']}",
            "",
        ])

        html_parts.append(f"""
        <div style="margin:16px 0;padding:14px;border-left:4px solid {confidence_color};background:#fafafa;border-radius:0 6px 6px 0">
            <h3 style="margin:0;color:#1a1a1a">{s['person_name']}</h3>
            <div style="font-size:14px;color:#8b2500;font-weight:600">{s['project_name']}</div>
            <div style="font-size:12px;color:#888;margin:4px 0">{s['date']} — {s['committee']}</div>
            <p style="font-size:14px;line-height:1.5;color:#333">{s['summary']}</p>
        </div>
        """)

    html_parts.append("</body></html>")

    body_text = "\n".join(text_parts)
    body_html = "\n".join(html_parts)

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = f"croton.news <{SMTP_FROM}>"
        msg["To"] = OWNER_EMAIL
        msg["Subject"] = subject
        msg.attach(MIMEText(body_text, "plain"))
        msg.attach(MIMEText(body_html, "html"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_FROM, [OWNER_EMAIL], msg.as_string())

        print(f"  Owner notification sent to {OWNER_EMAIL}")
    except Exception as e:
        print(f"  Owner notification failed: {e}")


def show_history():
    """Show previously identified story ideas."""
    sdb = get_stories_db()
    rows = sdb.execute("""
        SELECT * FROM story_ideas
        WHERE person_name != '' AND status != 'scanned'
        ORDER BY created_at DESC
    """).fetchall()

    if not rows:
        print("No story ideas found yet.")
        return

    for r in rows:
        status_icon = {"new": "?", "emailed": ">>", "approved": "+",
                       "rejected": "x", "written": "*"}.get(r["status"], "?")
        print(f"  [{status_icon}] {r['meeting_date']} {r['committee']}")
        print(f"      {r['person_name']}: {r['project_name']} [{r['confidence']}]")
        print(f"      {r['summary'][:120]}")
        print()

    sdb.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "scan":
        since = None
        send = "--email" in sys.argv
        for arg in sys.argv:
            if arg.startswith("--since"):
                idx = sys.argv.index(arg)
                if idx + 1 < len(sys.argv):
                    since = sys.argv[idx + 1]
        scan_recent(since_date=since, send_email=send)

    elif cmd == "history":
        show_history()

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)
