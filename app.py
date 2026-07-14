"""
croton.news — Hyperlocal news for Croton-on-Hudson, NY

Serves:
  - AI-generated news articles from meeting transcripts (rag.db)
  - Full-text document search via FTS5 (rag.db chunks_fts)
  - Community calendar (events.json)
  - Meeting index by committee
"""

import hmac
import json
import os
import sys
import sqlite3
import threading
from datetime import datetime
from functools import wraps
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from werkzeug.middleware.proxy_fix import ProxyFix
from flask import (
    Flask, Response, abort, g, jsonify, render_template, render_template_string, request,
    redirect, send_from_directory,
)

# ── Config ────────────────────────────────────────────────────────

# Load .env if present
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ECODE_DIR = os.path.join(BASE_DIR, "ecode360")
SUMMARIES_DB = os.path.join(ECODE_DIR, "summaries.db")
PHOTOS_DB = os.path.join(BASE_DIR, "photos.db")
# rag/ is sibling to site/ locally, or child of BASE_DIR on VPS
_rag_sibling = os.path.join(os.path.dirname(BASE_DIR), "rag")
_rag_child = os.path.join(BASE_DIR, "rag")
RAG_DIR = _rag_child if os.path.isdir(_rag_child) else _rag_sibling
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")

# Admin auth for mutating/admin routes (photos, comments, indexnow, /status, /review).
# Token lives in .env (mode 600). If unset, admin routes are locked entirely.
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")


def _is_admin():
    if not ADMIN_TOKEN:
        return False
    supplied = (
        request.headers.get("X-Admin-Token")
        or request.args.get("token")
        or request.cookies.get("admin_token")
        or ""
    )
    return hmac.compare_digest(supplied, ADMIN_TOKEN)


def require_admin(f):
    """Gate a route behind ADMIN_TOKEN (header, ?token=, or cookie).

    Browser flow: visit /status?token=... once — a session cookie is set so
    subsequent visits work without the query param.
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not _is_admin():
            abort(403)
        from flask import make_response
        resp = make_response(f(*args, **kwargs))
        if request.args.get("token"):
            resp.set_cookie(
                "admin_token", ADMIN_TOKEN,
                httponly=True, secure=True, samesite="Lax",
                max_age=30 * 24 * 3600,
            )
        return resp
    return wrapper

COMMITTEES = {
    "Board Of Trustees": {"slug": "board-of-trustees", "icon": "🏛️", "color": "#1e40af"},
    "Planning Board": {"slug": "planning-board", "icon": "📐", "color": "#7c3aed"},
    "Zoning Board of Appeals": {"slug": "zba", "icon": "⚖️", "color": "#b45309"},
    "Sustainability Committee": {"slug": "sustainability", "icon": "🌿", "color": "#15803d"},
    "Recreation Advisory Committee": {"slug": "recreation", "icon": "🏞️", "color": "#0d9488"},
    "Conservation Advisory Council": {"slug": "conservation", "icon": "🦅", "color": "#166534"},
    "Bicycle and Pedestrian Committee": {"slug": "bike-ped", "icon": "🚲", "color": "#0284c7"},
    "Police Advisory Committee (PAC)": {"slug": "police", "icon": "🛡️", "color": "#dc2626"},
    "Fire Council": {"slug": "fire-council", "icon": "🚒", "color": "#ea580c"},
    "Waterfront Advisory Committee": {"slug": "waterfront", "icon": "⚓", "color": "#0369a1"},
    "IDEA Advisory Committee": {"slug": "idea", "icon": "💡", "color": "#7c3aed"},
    "Board of Education": {"slug": "board-of-education", "icon": "🎓", "color": "#7e22ce"},
    "Topics": {"slug": "topics-feature", "icon": "📰", "color": "#991b1b"},
}

SLUG_TO_COMMITTEE = {v["slug"]: k for k, v in COMMITTEES.items()}

COMMENTS_DB = os.path.join(BASE_DIR, "comments.db")

app = Flask(__name__, template_folder=TEMPLATE_DIR, static_folder=STATIC_DIR)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)


# Jinja filters
app.jinja_env.filters["from_json"] = lambda s: __import__("json").loads(s) if s else None

# Eastern Time offset — returns -04:00 (EDT) or -05:00 (EST) based on date
def _tz_offset(date_str):
    from zoneinfo import ZoneInfo
    try:
        parts = date_str[:10].split("-")
        from datetime import datetime as _dt
        dt = _dt(int(parts[0]), int(parts[1]), int(parts[2]), 19, 0, tzinfo=ZoneInfo("America/New_York"))
        off = dt.strftime("%z")  # e.g. -0400
        return off[:3] + ":" + off[3:]  # e.g. -04:00
    except Exception:
        return "-05:00"
app.jinja_env.filters["tz_offset"] = _tz_offset
# ── History Blueprint ─────────────────────────────────────────────
from history_bp import history_bp
app.register_blueprint(history_bp, url_prefix='/history')


import re as _re

def sanitize_fts5_query(query):
    """Escape user input for safe FTS5 MATCH queries."""
    # Remove FTS5 special characters: ( ) * : ^ "
    cleaned = _re.sub(r'[()\*:^"]+', ' ', query)
    # Split into words, wrap each in double-quotes to avoid FTS5 keyword issues
    terms = cleaned.split()
    if not terms:
        return None
    return ' '.join('"' + t + '"' for t in terms if t)

# ── Database helpers ──────────────────────────────────────────────

def get_summaries_db():
    if "summaries_db" not in g:
        g.summaries_db = sqlite3.connect(SUMMARIES_DB)
        g.summaries_db.row_factory = sqlite3.Row
    return g.summaries_db


def get_rag_db():
    if "rag_db" not in g:
        g.rag_db = sqlite3.connect(os.path.join(RAG_DIR, "rag.db"))
        g.rag_db.row_factory = sqlite3.Row
    return g.rag_db

def get_photos_db():
    if "photos_db" not in g:
        g.photos_db = sqlite3.connect(PHOTOS_DB)
        g.photos_db.row_factory = sqlite3.Row
        g.photos_db.execute("PRAGMA foreign_keys=ON")
    return g.photos_db

def get_comments_db():
    if "comments_db" not in g:
        g.comments_db = sqlite3.connect(COMMENTS_DB)
        g.comments_db.row_factory = sqlite3.Row
        g.comments_db.execute("PRAGMA journal_mode=WAL")
        g.comments_db.executescript("""
            CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id TEXT NOT NULL,
                name TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                approved INTEGER DEFAULT 1,
                ip TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_comments_article ON comments(article_id, approved);
        """)
    return g.comments_db

@app.teardown_appcontext
def close_dbs(exception):
    for key in ("summaries_db", "rag_db", "photos_db", "comments_db",
                "history_db", "mcdonald_db"):
        db = g.pop(key, None)
        if db:
            db.close()

@app.after_request
def add_cache_headers(response):
    path = request.path
    if path.startswith('/photos/') or path.startswith('/static/'):
        response.cache_control.public = True
        response.cache_control.max_age = 86400  # 1 day
    elif path.startswith('/api/'):
        response.cache_control.public = True
        response.cache_control.max_age = 300  # 5 min
    elif path in ('/feed', '/sitemap.xml', '/news-sitemap.xml', '/robots.txt', '/humans.txt'):
        response.cache_control.public = True
        response.cache_control.max_age = 3600  # 1 hour
    return response


# ── Template context ──────────────────────────────────────────────

import re

@app.template_filter("md_bold")
def md_bold_filter(text):
    """Convert **text** to <strong>text</strong>.

    NOTE: this was a body-less stub returning None for any non-empty input,
    which made Jinja render the literal string "None" for every Key Actions
    bullet site-wide (audit U4, fixed 2026-07-13).
    """
    if not text:
        return text
    return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)


@app.template_filter("strip_md")
def strip_md_filter(text):
    """Strip markdown formatting for use in meta descriptions."""
    if not text:
        return text
    t = text
    t = re.sub(r'\*\*([^*]+)\*\*', r'\1', t)
    t = re.sub(r'(?<!\w)\*([^*\n]+)\*(?!\w)', r'\1', t)
    t = re.sub(r'^#{1,6}\s+', '', t, flags=re.MULTILINE)
    t = re.sub(r'^>\s?', '', t, flags=re.MULTILINE)
    t = re.sub(r'^\*\s+', '', t, flags=re.MULTILINE)
    t = re.sub(r'^-\s+', '', t, flags=re.MULTILINE)
    t = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', t)
    t = re.sub(r'\{\{[^}]+\}\}', '', t)
    t = re.sub(r'^\*(?!\s)', '', t)  # lone leading asterisk (unclosed italic/bold)
    t = re.sub(r'\s+', ' ', t).strip()
    return t[:160]


@app.template_filter("meta_desc")
def meta_desc_filter(article_text, fallback=""):
    """SEO description: first real paragraph of the article body, else fallback.

    quick_summary is often a raw agenda concatenation ("Consider acknowledging
    receipt of Local Law Introductory No. 10...") that mismatches the headline
    and truncates mid-word in search snippets (audit U10).
    """
    src = ""
    for para in (article_text or "").split("\n\n"):
        p = para.strip()
        if not p or p.startswith(("#", "{{", "===", "|", "-", "*", ">", "!")):
            continue
        src = p
        break
    t = strip_md_filter(src or fallback or "") or ""
    if len(t) == 160 and " " in t:  # strip_md hard-cuts at 160 — end on a word
        t = t[:t.rfind(" ")].rstrip(",.;:") + "…"
    return t


@app.template_filter("process_photos")
def process_photos_filter(text):
    """Convert {{photo:EVENT_ID:SECONDS:CAPTION}} shortcodes to <img> tags.

    Also handles {{photo_static:FILENAME:CAPTION}} for non-video photos.
    """
    if not text or "{{photo" not in text:
        return text

    import re

    def replace_photo(m):
        event_id = m.group(1)
        seconds = m.group(2)
        caption = m.group(3) or ""
        src = f"/photos/{event_id}_t{seconds}.jpg"
        enhanced = f"/photos/{event_id}_t{seconds}_enhanced.jpg"
        return (
            f'<figure class="article-photo">'
            f'<img src="{enhanced}" alt="{caption}" loading="lazy" '
            f'onerror="this.src=\'{src}\'">'
            f'<figcaption>{caption}</figcaption>'
            f'</figure>'
        )

    def replace_static(m):
        filename = m.group(1)
        caption = m.group(2) or ""
        return (
            f'<figure class="article-photo">'
            f'<img src="/photos/{filename}" alt="{caption}" loading="lazy">'
            f'<figcaption>{caption}</figcaption>'
            f'</figure>'
        )

    # [\w-]+ not \w+: event ids can be yt-VIDEOID (audit doc-completeness #4)
    text = re.sub(r"\{\{photo:([\w-]+):(\d+):([^}]*)\}\}", replace_photo, text)
    text = re.sub(r"\{\{photo_static:([^:}]+):([^}]*)\}\}", replace_static, text)
    # Also clean up any documentation/template examples left in articles
    text = re.sub(r"\{\{photo:EVENT:SECONDS:CAPTION\}\}", "", text)
    text = re.sub(r"\{\{photo_static:FILENAME:CAPTION\}\}", "", text)
    return text


@app.template_filter("process_quotes")
def process_quotes_filter(text, event_id=""):
    """Convert {{quote:...}} shortcodes to [source]/[M:SS] link spans.

    Server-side mirror of the JS renderer in article.html (which stays as a
    no-op fallback for anything this misses). Forms:
      {{quote:SECONDS}}            — uses the article's event_id
      {{quote:EVENT:SECONDS}}      — explicit ChampDS event ID (4+ digits)
      {{quote:yt-VIDEO_ID:SECONDS}} — YouTube cross-meeting quotes
    Without server-side rendering, crawlers/RSS/no-JS clients saw raw tags
    (and Safari <16.4 choked on the JS entirely). See AUDIT-2026-07-13.md C4.
    """
    if not text or "{{quote" not in text:
        return text

    import re

    # Collapse accidental consecutive duplicate tags ({{quote:743}}{{quote:743}})
    text = re.sub(r"(\{\{quote:[^}]+\}\})(\s*\1)+", r"\1", text)

    link_style = (
        "text-decoration:none;color:var(--accent,#8b2500);opacity:0.65;"
        "border-bottom:1px dotted currentColor"
    )

    def render(qeid, ts):
        mins = ts // 60
        secs = str(ts % 60).zfill(2)
        if qeid.startswith("yt-"):
            video_href = f"https://www.youtube.com/watch?v={qeid[3:]}&t={ts}s"
        else:
            video_href = f"/videos/{qeid}.mp4#t={ts}"
        return (
            '<span class="quote-links" style="display:inline-flex;gap:6px;'
            'margin-left:4px;vertical-align:super;font-size:10px;line-height:1;'
            'font-family:var(--sans,sans-serif)">'
            f'<a href="/transcript/{qeid}#t={ts}" style="{link_style}" '
            f'title="View in transcript at {mins}:{secs}">[source]</a>'
            f'<a href="{video_href}" target="_blank" rel="noopener noreferrer" '
            f'style="{link_style}" title="Watch video at {mins}:{secs}">[{mins}:{secs}]</a>'
            "</span>"
        )

    text = re.sub(
        r"\{\{quote:(yt-[\w-]+):(\d+)\}\}",
        lambda m: render(m.group(1), int(m.group(2))), text)
    text = re.sub(
        r"\{\{quote:(\d{4,}):(\d+)\}\}",
        lambda m: render(m.group(1), int(m.group(2))), text)
    if event_id:
        text = re.sub(
            r"\{\{quote:(\d+)\}\}",
            lambda m: render(str(event_id), int(m.group(1))), text)
    else:
        text = re.sub(r"\{\{quote:\d+\}\}", "", text)
    return text

@app.template_filter("process_footnotes")
def process_footnotes_filter(text):
    """Transform [N] footnote markers and Footnotes section into styled HTML.

    Inline [1] becomes <sup> anchor links.
    The Footnotes section becomes a styled <aside> with numbered list.
    Articles without footnotes pass through unchanged.
    """
    if not text:
        return text

    # Quick check: does this text contain footnote markers?
    if not re.search(r'\[\d+\]', text):
        return text

    # Detect footnotes section separator
    separator_re = re.compile(
        r'\n\s*\*{0,2}(?:Footnotes|Source documents)\s*:?\s*\*{0,2}\s*\n',
        re.IGNORECASE
    )
    match = separator_re.search(text)
    if match:
        body = text[:match.start()]
        footnotes_raw = text[match.end():]
    else:
        # No separator found — don't process (might be false positives)
        return text

    # Parse footnotes: [N] at start of line followed by text
    footnotes = {}
    fn_pattern = re.compile(r'^\s*\[(\d+)\]\s*', re.MULTILINE)
    parts = fn_pattern.split(footnotes_raw)
    # parts: [preamble, num, text, num, text, ...]
    i = 1
    while i < len(parts) - 1:
        num = int(parts[i])
        fn_text = parts[i + 1].strip()
        # Convert markdown bold
        fn_text = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', fn_text)
        footnotes[num] = fn_text
        i += 2

    # Convert markdown links [text](url) in footnotes to HTML links
    for num in list(footnotes.keys()):
        fn = footnotes[num]
        fn = re.sub(
            r'\[([^\]]+)\]\((https?://[^)]+)\)',
            r'<a href="\2" class="fn-doc-link" target="_blank" rel="noopener">\1</a>',
            fn
        )
        footnotes[num] = fn

    # Replace inline [N] markers with superscript anchor links
    def replace_marker(m):
        num = m.group(1)
        return (
            f'<sup class="fn-ref">'
            f'<a href="#fn-{num}" id="fn-ref-{num}">{num}</a>'
            f'</sup>'
        )
    body = re.sub(r'\[(\d+)\]', replace_marker, body)

    # Build footnotes HTML
    if footnotes:
        fn_items = []
        for num in sorted(footnotes.keys()):
            fn_text = footnotes[num]
            fn_items.append(
                f'    <li id="fn-{num}" value="{num}">'
                f'<span class="fn-text">{fn_text}</span> '
                f'<a href="#fn-ref-{num}" class="fn-backref" '
                f'aria-label="Back to reference {num}" '
                f'title="Back to text">\u21a9</a></li>'
            )

        fn_html = (
            '\n<aside class="article-footnotes" role="note">\n'
            '  <h4 class="fn-heading">Sources</h4>\n'
            '  <ol class="fn-list">\n'
            + '\n'.join(fn_items) + '\n'
            '  </ol>\n'
            '</aside>\n'
        )
        return body + fn_html

    return body


@app.context_processor
def inject_globals():
    return {
        "now": datetime.now(tz=__import__("zoneinfo").ZoneInfo("America/New_York")),
        "committees": COMMITTEES,
    }


# ── Routes: Pages ─────────────────────────────────────────────────

@app.route("/")
def index():
    rag = get_rag_db()

    # Latest articles from meetings table
    articles = rag.execute("""
        SELECT id, committee, date, quick_summary, article, headline,
               event_id, has_transcript, has_video, article_model, agenda_json
        FROM meetings
        WHERE article IS NOT NULL AND article != ''
        ORDER BY date DESC LIMIT 20
    """).fetchall()

    # Committee meeting counts
    committee_counts = {}
    for row in rag.execute("SELECT committee, COUNT(*) as c FROM meetings GROUP BY committee ORDER BY c DESC"):
        committee_counts[row["committee"]] = row["c"]

    # Fire dept RSS
    fire_articles = []
    try:
        import requests as http
        import re as _re
        fr = http.get("https://www.crotonfd.org/apps/public/news/rss/",
                       timeout=8, allow_redirects=True)
        if fr.ok:
            items = _re.findall(r'<item>(.*?)</item>', fr.text, _re.DOTALL)
            for item in items[:5]:
                t = _re.search(r'<title>(.*?)</title>', item, _re.DOTALL)
                d = _re.search(r'<pubDate>(.*?)</pubDate>', item, _re.DOTALL)
                l = _re.search(r'<link>(.*?)</link>', item, _re.DOTALL)
                desc = _re.search(r'<description>(.*?)</description>', item, _re.DOTALL)
                if t:
                    from datetime import datetime as _dt
                    date_str = ""
                    try:
                        date_str = _dt.strptime(d.group(1).strip()[:25],
                                                '%a, %d %b %Y %H:%M:%S').strftime('%Y-%m-%d') if d else ""
                    except Exception:
                        pass
                    fire_articles.append({
                        "headline": t.group(1).strip(),
                        "short_summary": _re.sub(r'<[^>]+>', '', desc.group(1)).strip()[:200] if desc else "",
                        "date": date_str,
                        "committee": "Fire Department",
                        "doc_id": None,
                        "url": l.group(1).strip() if l else "",
                        "source": "fire",
                    })
    except Exception:
        pass

    # Split: preview articles (upcoming) vs real coverage (past)
    coverage = []
    previews = []
    today = datetime.now().strftime('%Y-%m-%d')
    for a in articles:
        model = a["article_model"] or ""
        if "packet" in model or "agenda" in model:
            previews.append(a)
        else:
            coverage.append(a)

    # Also grab upcoming meetings without articles (for the strip)
    upcoming_no_article = rag.execute("""
        SELECT id, committee, date, quick_summary, headline, agenda_json
        FROM meetings
        WHERE date >= ? AND agenda_json IS NOT NULL AND agenda_json != ''
        ORDER BY date ASC LIMIT 5
    """, (today,)).fetchall()

    # Combine previews + upcoming-no-article, only FUTURE dates, limit 2
    coming_up = []
    for p in previews:
        if p["date"] >= today and p["agenda_json"]:
            coming_up.append(dict(p))
    for u in upcoming_no_article:
        if not any(c["id"] == u["id"] for c in coming_up):
            coming_up.append(dict(u))
    coming_up.sort(key=lambda x: x["date"])
    coming_up = coming_up[:4]

    return render_template("index.html",
        articles=coverage,
        coming_up=coming_up,
        fire_articles=fire_articles,
        committee_counts=committee_counts,
    )


@app.route("/editorials")
def editorials():
    rag = get_rag_db()
    articles = rag.execute("""
        SELECT id, committee, date, quick_summary, article, headline,
               event_id, has_transcript, has_video, word_count
        FROM meetings
        WHERE article_model = 'claude-opus-4-feature'
        ORDER BY date DESC
    """).fetchall()
    return render_template("editorials.html", articles=articles)


@app.route("/api/weather")
def api_weather():
    """Fetch live weather + AQI + river for Croton-on-Hudson."""
    import requests as http
    data = {}

    # NWS forecast
    try:
        fr = http.get("https://api.weather.gov/gridpoints/OKX/34,58/forecast",
                       headers={"User-Agent": "croton.news"}, timeout=8)
        if fr.ok:
            periods = fr.json()["properties"]["periods"]
            data["weather"] = {
                "name": periods[0]["name"],
                "temp": periods[0]["temperature"],
                "unit": periods[0]["temperatureUnit"],
                "forecast": periods[0]["shortForecast"],
                "detail": periods[0]["detailedForecast"],
                "wind": periods[0]["windSpeed"],
                "icon": periods[0].get("icon", ""),
            }
            # Include tonight + tomorrow for "click for more"
            data["forecast"] = [{
                "name": p["name"],
                "temp": p["temperature"],
                "unit": p["temperatureUnit"],
                "forecast": p["shortForecast"],
                "detail": p["detailedForecast"],
                "wind": p["windSpeed"],
            } for p in periods[:6]]
    except Exception:
        pass

    # Open-Meteo AQI (free, no key)
    try:
        r = http.get("https://air-quality-api.open-meteo.com/v1/air-quality"
                      "?latitude=41.2087&longitude=-73.8912"
                      "&current=us_aqi,pm2_5,pm10"
                      "&timezone=America/New_York", timeout=5)
        if r.ok:
            cur = r.json().get("current", {})
            aqi_val = cur.get("us_aqi", 0)
            if aqi_val <= 50:
                cat = "Good"
            elif aqi_val <= 100:
                cat = "Moderate"
            elif aqi_val <= 150:
                cat = "Unhealthy for Sensitive Groups"
            else:
                cat = "Unhealthy"
            data["aqi"] = {
                "value": aqi_val,
                "category": cat,
                "pm25": cur.get("pm2_5"),
                "pm10": cur.get("pm10"),
            }
    except Exception:
        pass

    # Croton River at New Croton Dam (01375000) — discharge (cfs)
    # 6 years of daily data (2020-2026) for historical comparison
    try:
        # Current flow
        r = http.get("https://waterservices.usgs.gov/nwis/iv/"
                      "?format=json&sites=01375000&parameterCd=00060&period=PT2H",
                      timeout=5)
        if r.ok:
            ts = r.json()["value"]["timeSeries"]
            if ts and ts[0]["values"][0]["value"]:
                val = ts[0]["values"][0]["value"][-1]
                flow = float(val["value"])
                # Historical average for this day of year (2020-2025)
                from datetime import datetime as _dt
                mmdd = _dt.now().strftime("-%m-%d")
                try:
                    hr = http.get("https://waterservices.usgs.gov/nwis/dv/"
                                  "?format=json&sites=01375000&parameterCd=00060"
                                  "&startDT=2015-01-01&endDT=2025-12-31", timeout=8)
                    if hr.ok:
                        hvals = [float(v["value"]) for v in
                                 hr.json()["value"]["timeSeries"][0]["values"][0]["value"]
                                 if v["value"] and mmdd in v["dateTime"]]
                        avg = round(sum(hvals) / len(hvals), 1) if hvals else flow
                    else:
                        avg = flow
                except Exception:
                    avg = flow
                diff = round(flow - avg, 1)
                pct = round((diff / avg) * 100) if avg else 0
                data["river"] = {
                    "flow_cfs": flow,
                    "avg_cfs": avg,
                    "diff_cfs": diff,
                    "diff_pct": pct,
                    "status": "above" if pct > 15 else "below" if pct < -15 else "near",
                    "time": val["dateTime"],
                    "site": "Croton River at New Croton Dam",
                    "years": len(hvals) if 'hvals' in dir() else 0,
                }
    except Exception:
        pass

    return jsonify(data)


# Redirects for deleted articles that have backlinks
ARTICLE_REDIRECTS = {
    "59": "/article/58",  # High school → committee appointments editorial
    "60": "/article/61",  # Court study → court consolidation editorial
}

@app.route("/article/<doc_id>")
def article_page(doc_id):
    # Handle redirects for deleted articles
    if doc_id in ARTICLE_REDIRECTS:
        from flask import redirect
        return redirect(ARTICLE_REDIRECTS[doc_id], code=301)

    rag = get_rag_db()

    # Try meetings table first (by id or event_id)
    meeting = rag.execute(
        "SELECT * FROM meetings WHERE id = ? OR event_id = ?", (doc_id, doc_id)
    ).fetchone()

    if meeting:
        # Related meetings (same committee, nearby dates) — with full data for display
        related = rag.execute("""
            SELECT id, committee, date, quick_summary, headline, event_id,
                   has_transcript, has_video, has_audio, complete_summary
            FROM meetings
            WHERE committee = ? AND id != ?
            ORDER BY ABS(julianday(date) - julianday(?))
            LIMIT 6
        """, (meeting["committee"], meeting["id"], meeting["date"])).fetchall()
        related = [dict(r) for r in related]

        def _minutes_url(doc_ids_str):
            """Extract ecode360 minutes PDF URL from doc_ids."""
            for did in (doc_ids_str or "").split(","):
                did = did.strip()
                if did and not did.endswith("-transcript") and not did.endswith("-opus-news"):
                    return f"https://ecode360.com/CR0035/document/{did}.pdf"
            return None

        # Get doc_ids for PDF links
        pdf_url = _minutes_url(meeting["doc_ids"])

        # Add minutes_url to each related meeting
        rel_doc_ids = {r["id"]: r for r in related}
        if rel_doc_ids:
            for rm in rag.execute(
                f"SELECT id, doc_ids FROM meetings WHERE id IN ({','.join('?' * len(rel_doc_ids))})",
                list(rel_doc_ids.keys())
            ).fetchall():
                rel_doc_ids[rm["id"]]["minutes_url"] = _minutes_url(rm["doc_ids"])

        cdb = get_comments_db()
        comments = cdb.execute(
            "SELECT * FROM comments WHERE article_id = ? AND approved = 1 ORDER BY created_at ASC",
            (str(meeting["id"]),)
        ).fetchall()

        author = get_author_for_model(meeting["article_model"]) if meeting["article_model"] else None

        return render_template("article.html",
            article=meeting,
            author=author,
            index_data={},
            pdf_url=pdf_url,
            related=related,
            comments=comments,
        )

    # Fallback to old summaries table for legacy doc_ids
    try:
        db = get_summaries_db()
        article = db.execute(
            "SELECT * FROM summaries WHERE doc_id = ?", (doc_id,)
        ).fetchone()
    except Exception:
        article = None
    if not article:
        abort(404)

    index_data = {}
    try:
        index_data = json.loads(article["index_json"]) if article["index_json"] else {}
    except (json.JSONDecodeError, KeyError):
        pass

    pdf_url = f"https://ecode360.com/CR0035/document/{doc_id}.pdf"
    related = db.execute("""
        SELECT doc_id, committee, date, short_summary
        FROM summaries
        WHERE committee = ? AND doc_id != ?
        ORDER BY ABS(julianday(date) - julianday(?))
        LIMIT 4
    """, (article["committee"], doc_id, article["date"])).fetchall()

    cdb = get_comments_db()
    comments = cdb.execute(
        "SELECT * FROM comments WHERE article_id = ? AND approved = 1 ORDER BY created_at ASC",
        (doc_id,)
    ).fetchall()

    return render_template("article.html",
        article=article,
        index_data=index_data,
        pdf_url=pdf_url,
        related=related,
        comments=comments,
    )


@app.route("/meetings")
def meetings_index():
    rag = get_rag_db()
    committee_filter = request.args.get("committee", "")

    if committee_filter:
        full_name = SLUG_TO_COMMITTEE.get(committee_filter, committee_filter)
        mtgs = rag.execute("""
            SELECT id, committee, date, quick_summary, headline, event_id,
                   has_transcript, has_video, article, agenda_json, article_model
            FROM meetings
            WHERE committee = ?
            ORDER BY date DESC
        """, (full_name,)).fetchall()
    else:
        mtgs = rag.execute("""
            SELECT id, committee, date, quick_summary, headline, event_id,
                   has_transcript, has_video, article, agenda_json, article_model
            FROM meetings
            ORDER BY date DESC
        """).fetchall()

    # split recurring committees from one-off events (forums, webinars) so
    # the filter sidebar reads as a committee list, not a grab-bag (audit U8)
    committee_counts = {}
    one_off_counts = {}
    for row in rag.execute("SELECT committee, COUNT(*) as c FROM meetings GROUP BY committee ORDER BY c DESC"):
        name, c = row["committee"], row["c"]
        if name in COMMITTEES or c >= 2:
            committee_counts[name] = c
        else:
            one_off_counts[name] = c

    return render_template("meetings.html",
        meetings=mtgs,
        committee_filter=committee_filter,
        active_committee=SLUG_TO_COMMITTEE.get(committee_filter, committee_filter),
        committee_counts=committee_counts,
        one_off_counts=one_off_counts,
    )


@app.route("/calendar")
def calendar_page():
    return send_from_directory(TEMPLATE_DIR, "calendar.html")


@app.route("/documents")
def documents_page():
    db = get_rag_db()
    query = request.args.get("q", "").strip()

    results = []
    if query:
      try:
        safe_q = sanitize_fts5_query(query)
        if not safe_q:
            rows = []
        else:
            rows = db.execute("""
                SELECT c.doc_id, c.committee, c.date, c.content,
                       snippet(chunks_fts, 0, '<mark>', '</mark>', '...', 40) as snippet,
                       m.id as meeting_id
                FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.rowid
                LEFT JOIN meetings m ON c.doc_id = m.event_id
                WHERE chunks_fts MATCH ?
                ORDER BY rank
                LIMIT 30
            """, (safe_q,)).fetchall()
      except Exception:
        rows = []

      # NOTE: was indented under `except` — /documents search returned
      # zero results for every query (audit H5, fixed 2026-07-13)
      seen = set()
      for row in rows:
          if row["doc_id"] not in seen:
              seen.add(row["doc_id"])
              results.append(dict(row))

    # Committee list for browsing
    committees = db.execute("""
        SELECT committee, COUNT(DISTINCT doc_id) as c FROM chunks
        GROUP BY committee ORDER BY c DESC
    """).fetchall()

    return render_template("documents.html",
        query=query,
        results=results,
        doc_committees=committees,
    )


# ── Routes: Meeting Page ─────────────────────────────────────────

@app.route("/meeting/<int:meeting_id>")
def meeting_page(meeting_id):
    rag = get_rag_db()
    meeting = rag.execute(
        "SELECT * FROM meetings WHERE id = ?", (meeting_id,)
    ).fetchone()
    if not meeting:
        abort(404)

    meeting = dict(meeting)

    # Get topics linked to this meeting's chunks
    topics = []
    if meeting.get("event_id"):
        topics = rag.execute("""
            SELECT DISTINCT t.name, t.slug FROM topic_threads t
            JOIN topic_mentions tm ON tm.topic_id = t.id
            JOIN chunks c ON c.id = tm.chunk_id
            WHERE c.doc_id = ?
        """, (meeting["event_id"],)).fetchall()
        topics = [dict(t) for t in topics]

    # Get people entities linked to this meeting's chunks
    people = []
    if meeting.get("event_id"):
        people = rag.execute("""
            SELECT DISTINCT e.name, e.slug, e.metadata_json
            FROM entities e
            JOIN entity_mentions em ON em.entity_id = e.id
            JOIN chunks c ON c.id = em.chunk_id
            WHERE c.doc_id = ? AND e.type = 'person'
            ORDER BY e.mention_count DESC
            LIMIT 20
        """, (meeting["event_id"],)).fetchall()
        people_list = []
        for p in people:
            pd = dict(p)
            try:
                meta = json.loads(pd.get("metadata_json") or "{}")
                pd["role"] = meta.get("role", "")
            except (json.JSONDecodeError, TypeError):
                pd["role"] = ""
            people_list.append(pd)
        people = people_list

    # Related meetings
    related = rag.execute("""
        SELECT id, committee, date, headline, quick_summary
        FROM meetings
        WHERE committee = ? AND id != ?
        ORDER BY ABS(julianday(date) - julianday(?))
        LIMIT 4
    """, (meeting["committee"], meeting_id, meeting["date"])).fetchall()
    related = [dict(r) for r in related]

    # Get all source documents (packet PDFs) for this meeting
    source_docs = []
    if meeting.get("boarddocs_id"):
        source_docs = rag.execute("""
            SELECT nickname, source_url, pages, char_count, agenda_item_title
            FROM packet_pdfs
            WHERE event_id = ? AND source_url IS NOT NULL AND source_url != ''
            ORDER BY agenda_item_title, nickname
        """, (meeting["boarddocs_id"],)).fetchall()
        source_docs = [dict(d) for d in source_docs]

    return render_template("meeting.html",
        meeting=meeting,
        topics=topics,
        people=people,
        related=related,
        source_docs=source_docs,
    )



# ── Village Code Browser ──────────────────────────────────────────

_villagecode_toc_cache = None

def _get_villagecode_toc():
    """Build and cache the village code table of contents."""
    global _villagecode_toc_cache
    if _villagecode_toc_cache:
        return _villagecode_toc_cache

    chapter_dir = os.path.join(RAG_DIR, "croton-code", "chapters")
    if not os.path.isdir(chapter_dir):
        return {"part1": [], "part2": []}

    chapters = []
    for fname in sorted(os.listdir(chapter_dir)):
        m = re.match(r'Ch_(\d+)_(.+)\.txt', fname)
        if not m:
            # Handle DL (disposition list)
            if fname.startswith("Ch_DL"):
                continue
            continue
        num = int(m.group(1))
        slug = m.group(2)
        title = slug.replace('-', ' ').title()

        # Read frontmatter for better title
        fpath = os.path.join(chapter_dir, fname)
        try:
            with open(fpath) as f:
                header = f.read(500)
            tm = re.search(r'title:\s*"Ch_\d+\s+(.*?)"', header)
            if tm:
                title = tm.group(1)
        except Exception:
            pass

        chapters.append({"num": num, "title": title, "slug": slug, "file": fname})

    chapters.sort(key=lambda c: c["num"])
    part1 = [c for c in chapters if c["num"] <= 61]
    part2 = [c for c in chapters if c["num"] > 61]

    _villagecode_toc_cache = {"part1": part1, "part2": part2}
    return _villagecode_toc_cache


def _parse_chapter(chapter_num):
    """Parse a chapter text file into structured sections."""
    chapter_dir = os.path.join(RAG_DIR, "croton-code", "chapters")
    target = None

    for fname in os.listdir(chapter_dir):
        m = re.match(r'Ch_(\d+)_(.+)\.txt', fname)
        if m and int(m.group(1)) == chapter_num:
            target = os.path.join(chapter_dir, fname)
            break

    if not target:
        return None

    with open(target) as f:
        text = f.read()

    # Strip frontmatter
    title = f"Chapter {chapter_num}"
    if text.startswith('---'):
        parts = text.split('---', 2)
        if len(parts) >= 3:
            fm = parts[1]
            text = parts[2]
            tm = re.search(r'title:\s*"Ch_\d+\s+(.*?)"', fm)
            if tm:
                title = tm.group(1)

    # Extract history note
    history = None
    hm = re.search(r'\[HISTORY:.*?\]', text, re.DOTALL)
    if hm:
        history = hm.group(0)

    # Strip the PDF table of contents at the top of each chapter.
    # The TOC has lines like "§ 230-5.     Classes of districts.     § 230-20.1.  Purpose"
    # (two columns of section listings). Remove everything before the first body section.
    # The HISTORY note or GENERAL REFERENCES section marks the end of TOC.
    toc_end = None
    for marker in [r'\[HISTORY:', r'GENERAL REFERENCES', r'ARTICLE\s+I\b']:
        hm = re.search(marker, text)
        if hm:
            if toc_end is None or hm.start() < toc_end:
                toc_end = hm.start()
    if toc_end and toc_end > 50:
        text = text[toc_end:]

    # Clean PDF layout artifacts
    # Remove footnote superscript numbers (bare numbers like "41" mid-sentence)
    text = re.sub(r'(?<=\w)(\d{1,3})(?=\s+or\b|\s+and\b|\s+the\b|\s+of\b|\s+in\b|\s+to\b|\s+a\b|\s+for\b)', '', text)
    # Collapse runs of whitespace within lines (PDF column artifacts)
    text = re.sub(r'(?<!\n)[ \t]{4,}(?!\n)', '  ', text)
    # Remove page header artifacts that leaked through
    text = re.sub(r'\n\s*§\s*\d+[\-\.]\d+\s{10,}[A-Z][A-Z\s,\-]+\s{10,}§\s*\d+[\-\.]\d+\s*\n', '\n', text)

    # Check for reserved/empty chapters
    stripped = text.strip()
    if len(stripped) < 100 or 'reserved' in stripped.lower()[:200]:
        return {
            "num": chapter_num,
            "title": title,
            "history": history,
            "sections": [{"section_id": None, "anchor": "preamble", "title": "", "content": stripped or "This chapter is reserved."}],
            "reserved": True,
        }

    # Split into sections on § markers
    sections = []
    current = {"section_id": None, "anchor": "preamble", "title": "", "content": ""}

    for line in text.strip().split('\n'):
        sm = re.match(r'^(§\s*[\d][\d\-\.A-Za-z]*)', line)
        if sm:
            if current["content"].strip():
                sections.append(current)

            sid = sm.group(1).strip()
            sid_clean = re.sub(r'^§\s*', '', sid).rstrip('.')
            anchor = 's' + sid_clean.replace('.', '-')
            current = {
                "section_id": sid,
                "anchor": anchor,
                "title": "",
                "content": "",  # Don't include the § line itself — it's rendered as heading
            }
        else:
            current["content"] += '\n' + line

    if current["content"].strip():
        sections.append(current)

    # Filter: keep only sections with real body content (>2 non-empty lines)
    body_sections = []
    for s in sections:
        # Clean up the content
        c = s["content"]
        # Remove chapter title line and HISTORY from preamble (already in header)
        if s["section_id"] is None:
            c = re.sub(r'^\s*Chapter\s+\d+.*?\n', '', c)
            c = re.sub(r'\[HISTORY:.*?\]', '', c, flags=re.DOTALL)
        # Merge [Amended\n date\n ] into single line
        c = re.sub(r'\[Amended\s*\n\s*(.+?)\s*\n\s*\]', r'[Amended \1]', c)
        c = re.sub(r'\[Added\s*\n\s*(.+?)\s*\n\s*\]', r'[Added \1]', c)
        # Remove stray cross-reference fragments (bare number + comma + chapter name)
        c = re.sub(r'\n\d{1,3}\n,\s*\w[^\n]*\.?\n?', '\n', c)
        # Also catch at end of content
        c = re.sub(r'\n\d{1,3}\s*\n,\s*[A-Z][^\n]*\.?\s*$', '', c)
        # Remove duplicate § line at start of content (already shown as heading)
        c = re.sub(r'^\s*§\s*[\d][\d\-\.A-Za-z]*\s*\n', '', c)
        # Collapse excessive blank lines
        c = re.sub(r'\n{3,}', '\n\n', c)
        c = c.strip()

        # Extract section title (first non-empty line if it looks like a section name)
        section_title = ""
        if s["section_id"] and c:
            lines = c.split('\n')
            first = lines[0].strip() if lines else ""
            # Title: short phrase ending in period, not starting with articles/prepositions,
            # not an amendment note, not all-caps (definition term), not a sentence
            is_title = (first
                and len(first) < 80
                and not first.startswith('[')
                and not first.startswith('The ')
                and not first.startswith('No ')
                and not first.startswith('Any ')
                and not first.startswith('It ')
                and not first.startswith('A ')
                and not first.startswith('In ')
                and not first.isupper()  # skip ALL CAPS definition terms
                and not re.match(r'^\d', first)  # skip lines starting with numbers
                and (first.endswith('.') or first.endswith(';') or len(first) < 40)
            )
            if is_title:
                section_title = first
                c = '\n'.join(lines[1:]).strip()

        # Wrap [Amended/Added ...] notes in styled spans
        c = re.sub(r'\[(Amended[^\]]+)\]', r'<span class="vc-amended">[\1]</span>', c)
        c = re.sub(r'\[(Added[^\]]+)\]', r'<span class="vc-amended">[\1]</span>', c)

        s["content"] = c
        s["title"] = section_title

        content_lines = [l for l in c.split('\n') if l.strip()]
        # Keep all named sections (even short ones). Only filter preamble fragments.
        if s["section_id"] or len(content_lines) > 2:
            body_sections.append(s)

    # Deduplicate: chapter files have TOC (short) then body (long) for each §.
    # Keep the longest version of each section_id.
    seen = {}
    for s in body_sections:
        key = s.get("anchor", id(s))
        if key in seen:
            # Keep whichever has more content
            if len(s["content"]) > len(seen[key]["content"]):
                seen[key] = s
        else:
            seen[key] = s
    # Preserve order of first appearance but use the longer content
    deduped = []
    added = set()
    for s in body_sections:
        key = s.get("anchor", id(s))
        if key not in added:
            deduped.append(seen[key])
            added.add(key)
    body_sections = deduped

    return {
        "num": chapter_num,
        "title": title,
        "history": history,
        "sections": body_sections,
        "reserved": False,
    }


@app.route("/villagecode")
def villagecode_index():
    """Village Code table of contents."""
    toc = _get_villagecode_toc()
    return render_template("villagecode.html",
        toc_part1=toc["part1"],
        toc_part2=toc["part2"],
        chapter=None,
        active_chapter=None,
        sections=[],
    )


@app.route("/villagecode/chapter/<int:chapter_num>")
def villagecode_chapter(chapter_num):
    """Display a specific chapter with section anchors."""
    toc = _get_villagecode_toc()
    chapter = _parse_chapter(chapter_num)
    if not chapter:
        abort(404)

    return render_template("villagecode.html",
        toc_part1=toc["part1"],
        toc_part2=toc["part2"],
        chapter=chapter,
        active_chapter=chapter_num,
        sections=chapter["sections"],
    )


# ── Routes: Topics ───────────────────────────────────────────────

@app.route("/topics")
def topics_index():
    db = get_rag_db()
    rows = db.execute("""
        SELECT t.id, t.name, t.slug, t.description, t.status,
               t.first_date, t.last_date, t.meeting_count,
               COUNT(tm.chunk_id) as chunk_count
        FROM topic_threads t
        LEFT JOIN topic_mentions tm ON tm.topic_id = t.id
        GROUP BY t.id
        ORDER BY chunk_count DESC
    """).fetchall()

    topics = [dict(r) for r in rows]
    active_topics = [t for t in topics if t["status"] == "active"]
    resolved_topics = [t for t in topics if t["status"] != "active"]

    total_meetings = db.execute(
        "SELECT COUNT(DISTINCT doc_id) FROM chunks"
    ).fetchone()[0]

    return render_template("topics.html",
        topics=topics,
        active_topics=active_topics,
        resolved_topics=resolved_topics,
        total_meetings=total_meetings,
    )


@app.route("/topic/<slug>")
def topic_page(slug):
    db = get_rag_db()
    topic = db.execute(
        "SELECT * FROM topic_threads WHERE slug = ?", (slug,)
    ).fetchone()
    if not topic:
        abort(404)

    topic = dict(topic)

    # Get all chunks for this topic, grouped by meeting date
    chunks = db.execute("""
        SELECT c.id, c.doc_id, c.doc_type, c.committee, c.date, c.content,
               c.speaker, c.start_time, c.end_time, tm.relevance
        FROM topic_mentions tm
        JOIN chunks c ON c.id = tm.chunk_id
        WHERE tm.topic_id = ?
        ORDER BY c.date ASC, c.start_time ASC NULLS LAST
    """, (topic["id"],)).fetchall()

    # Group by date+committee
    from collections import OrderedDict
    meetings = OrderedDict()
    for chunk in chunks:
        chunk = dict(chunk)
        key = f"{chunk['date']}|{chunk['committee'] or 'N/A'}"
        if key not in meetings:
            meetings[key] = {
                "date": chunk["date"],
                "committee": chunk["committee"] or "N/A",
                "chunks": [],
            }
        meetings[key]["chunks"].append(chunk)

    return render_template("topic.html",
        topic=topic,
        meetings=list(meetings.values()),
    )


# ── SMTP Email ──────────────────────────────────────────────────

SMTP_HOST = "mail.cyberpersons.com"
SMTP_PORT = 587
SMTP_USER = os.environ.get("SMTP_USER", "smtp_1c45c43cd1597103")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = "editor@croton.news"
EDITOR_EMAIL = "editor@croton.news"


def send_email(to, subject, body_text, body_html=None):
    """Send email via SMTP in a background thread."""
    if not SMTP_PASS:
        print(f"SMTP not configured — would send to {to}: {subject}")
        return

    def _send():
        try:
            msg = MIMEMultipart("alternative")
            msg["From"] = f"croton.news <{SMTP_FROM}>"
            msg["To"] = to
            msg["Subject"] = subject
            msg.attach(MIMEText(body_text, "plain"))
            if body_html:
                msg.attach(MIMEText(body_html, "html"))
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASS)
                server.sendmail(SMTP_FROM, [to], msg.as_string())
            print(f"Email sent to {to}: {subject}")
        except Exception as e:
            print(f"Email failed: {e}")

    threading.Thread(target=_send, daemon=True).start()


# ── AI Author Pseudonyms ────────────────────────────────────────

AI_AUTHORS = {
    "glm-5-turbo": {
        "slug": "grant-mitchell",
        "name": "Grant Mitchell",
        "initials": "GM",
        "color": "#2d5a27",
        "role": "Staff Reporter",
        "bio_short": "Covers routine meeting business and committee updates",
        "bio": "Grant Mitchell handles the bulk of croton.news's day-to-day meeting coverage. Powered by a frontier language model, Grant specializes in concise, factual reporting on committee proceedings, budget discussions, and routine board actions. His articles prioritize clarity and completeness over narrative flair, ensuring every vote, motion, and public comment is captured in the record.",
        "style": "Straightforward news reporting. Short paragraphs, active voice, inverted pyramid structure. Focuses on who-said-what and what-was-decided. Minimal editorializing.",
        "model_id": "Frontier language model",
        "system_prompt": "You are a local news reporter covering Croton-on-Hudson village government. Write a news article from the meeting transcript provided. Include all key decisions, votes, and notable public comments. Use direct quotes with speaker attribution. Keep paragraphs short. Write in inverted pyramid style with the most newsworthy item first.",
    },
    "glm-5-turbo-styled": {
        "slug": "grant-mitchell",
        "name": "Grant Mitchell",
        "initials": "GM",
        "color": "#2d5a27",
        "role": "Staff Reporter",
        "bio_short": "Covers routine meeting business and committee updates",
        "bio": "Grant Mitchell handles the bulk of croton.news's day-to-day meeting coverage. Powered by a frontier language model, Grant specializes in concise, factual reporting on committee proceedings, budget discussions, and routine board actions. His articles prioritize clarity and completeness over narrative flair, ensuring every vote, motion, and public comment is captured in the record.",
        "style": "Straightforward news reporting with light narrative structure. Section headers organize complex meetings into digestible topics. Direct quotes linked to video timestamps.",
        "model_id": "Frontier language model",
        "system_prompt": "You are a local news reporter covering Croton-on-Hudson village government. Write a structured news article from the meeting transcript. Use section headers (##) to organize by topic. Include direct quotes with speaker attribution. Every factual claim should be traceable to the transcript. Write with authority but without editorial opinion.",
    },
    "claude-sonnet-4-20250514": {
        "slug": "claire-ashford",
        "name": "Claire Ashford",
        "initials": "CA",
        "color": "#8b2500",
        "role": "Senior Correspondent",
        "bio_short": "In-depth meeting analysis with context and cross-references",
        "bio": "Claire Ashford produces croton.news's most detailed meeting coverage. Powered by a frontier language model, Claire brings deeper analysis to complex topics — connecting current decisions to prior meetings, identifying patterns in board behavior, and providing context that helps readers understand why a vote matters. Her articles are longer and more layered than standard meeting coverage.",
        "style": "Analytical reporting with rich context. Weaves in cross-references to related meetings and policy history. Longer-form, magazine-style paragraphs. Sources quotes precisely with transcript timestamps.",
        "model_id": "Frontier language model",
        "system_prompt": "You are an experienced local government reporter covering Croton-on-Hudson. Write a comprehensive news article from the meeting transcript. Go beyond summarizing — analyze the implications of decisions, connect them to prior meetings and ongoing policy debates, and explain what matters to residents. Use the RAG search results to add cross-meeting context. Include direct quotes with timestamps. Write with narrative authority while maintaining strict factual accuracy.",
    },
    "opus": {
        "slug": "claire-ashford",
        "name": "Claire Ashford",
        "initials": "CA",
        "color": "#8b2500",
        "role": "Senior Correspondent",
        "bio_short": "In-depth meeting analysis",
        "bio": "Claire Ashford produces croton.news's most detailed meeting coverage.",
        "style": "Analytical reporting with rich context.",
        "model_id": "Frontier language model",
        "system_prompt": "Same as Senior Correspondent prompt.",
    },
    "claude-opus-4-feature": {
        "slug": "nora-caldwell",
        "name": "Nora Caldwell",
        "initials": "NC",
        "color": "#4a1259",
        "role": "Investigative Editor",
        "bio_short": "Deep-dive features and investigative editorials",
        "bio": "Nora Caldwell writes croton.news's long-form investigative features and editorials. Powered by a frontier language model, Nora synthesizes months of meeting transcripts, public records, and external research into narrative-driven stories that reveal the forces shaping village governance. Her pieces trace a single issue across multiple meetings, identify contradictions between public statements and actions, and give voice to residents whose testimony might otherwise be lost in the minutes.",
        "style": "Long-form investigative reporting. Literary narrative structure with scene-setting, character development, and dramatic pacing. Heavy use of direct quotes in dramatic context. Connects dots across multiple meetings to build a thesis. Asks the questions the board didn't ask themselves.",
        "model_id": "Frontier language model",
        "system_prompt": "You are an investigative journalist writing a long-form feature article for a hyperlocal news site. Your source material is multiple meeting transcripts, public records, and web research. Write a narrative-driven piece that tells the STORY behind the policy — who are the people, what are the stakes, why should readers care? Use scene-setting, direct quotes in dramatic context, and connect events across multiple meetings to reveal patterns. Be fair but don't be neutral — if the facts point to a conclusion, follow them. Every claim must be sourced to a specific public record or meeting timestamp.",
    },
}

def get_author_for_model(model_id):
    """Get author data for a model ID, with fallback."""
    if model_id in AI_AUTHORS:
        return AI_AUTHORS[model_id]
    return AI_AUTHORS.get("glm-5-turbo")  # default fallback


# ── Routes: Transparency Pages ──────────────────────────────────

@app.route("/author/<slug>")
def author_page(slug):
    # Find author by slug
    author = None
    for model_id, data in AI_AUTHORS.items():
        if data["slug"] == slug:
            author = dict(data)
            break
    if not author:
        abort(404)

    # Get articles by this author
    rag = get_rag_db()
    model_ids = [mid for mid, d in AI_AUTHORS.items() if d["slug"] == slug]
    placeholders = ",".join("?" * len(model_ids))
    articles = rag.execute(f"""
        SELECT id, headline, quick_summary, committee, date
        FROM meetings
        WHERE article IS NOT NULL AND article_model IN ({placeholders})
        ORDER BY date DESC
    """, model_ids).fetchall()
    articles = [dict(a) for a in articles]
    author["article_count"] = len(articles)

    return render_template("author.html", author=author, articles=articles)


@app.route("/staff")
def staff_page():
    """Staff directory — all AI authors."""
    rag = get_rag_db()
    seen = set()
    authors = []
    for model_id, data in AI_AUTHORS.items():
        if data["slug"] in seen:
            continue
        seen.add(data["slug"])
        model_ids = [mid for mid, d in AI_AUTHORS.items() if d["slug"] == data["slug"]]
        placeholders = ",".join("?" * len(model_ids))
        count = rag.execute(f"""
            SELECT COUNT(*) FROM meetings
            WHERE article IS NOT NULL AND article_model IN ({placeholders})
        """, model_ids).fetchone()[0]
        a = dict(data)
        a["article_count"] = count
        authors.append(a)
    return render_template("staff.html", authors=authors)


@app.route("/api/author-feedback", methods=["POST"])
def author_feedback():
    """Receive feedback about AI author output."""
    data = {
        "author": request.form.get("author", ""),
        "type": request.form.get("type", ""),
        "article_url": request.form.get("article_url", ""),
        "details": request.form.get("details", ""),
        "email": request.form.get("email", ""),
    }
    if not data["details"]:
        return "Details required", 400

    # Store feedback in comments.db
    cdb = get_comments_db()
    cdb.execute("""
        INSERT INTO comments (article_id, name, body, approved, created_at)
        VALUES (?, ?, ?, 0, datetime('now'))
    """, (
        f"author-feedback-{data['author']}",
        data["email"] or "Anonymous",
        f"[{data['type']}] {data['article_url']}\n\n{data['details']}",
    ))
    cdb.commit()

    # Notify editor
    send_email(
        EDITOR_EMAIL,
        f"[croton.news] Author feedback: {data['type']} — {data['author']}",
        f"Author: {data['author']}\nType: {data['type']}\nArticle: {data['article_url']}\nFrom: {data['email'] or 'Anonymous'}\n\n{data['details']}",
    )

    return redirect(f"/author/{data['author']}?feedback=sent")


@app.route("/about")
def about_page():
    rag = get_rag_db()
    transcript_count = rag.execute(
        "SELECT COUNT(*) FROM meetings WHERE has_transcript = 1"
    ).fetchone()[0]
    meeting_count = rag.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]
    return render_template("about.html",
        transcript_count=transcript_count, meeting_count=meeting_count)

@app.route("/contact")
def contact_page():
    return render_template("contact.html")

@app.route("/tips", methods=["GET", "POST"])
def tips_page():
    if request.method == "POST":
        import sqlite3 as _sql
        tips_db = os.path.join(BASE_DIR, "tips.db")
        db = _sql.connect(tips_db)
        db.execute("""CREATE TABLE IF NOT EXISTS tips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT (datetime('now')),
            topic TEXT, message TEXT NOT NULL,
            name TEXT, email TEXT, phone TEXT,
            status TEXT DEFAULT 'new',
            notes TEXT
        )""")
        # Honeypot — bots fill the hidden "website" field; pretend success
        if request.form.get("website"):
            db.close()
            return render_template("tips.html", submitted=True)

        # Rate limit: max 5 tips per IP per hour
        ip = request.remote_addr or ""
        db.execute("CREATE TABLE IF NOT EXISTS tip_ips (ip TEXT, created_at TEXT DEFAULT (datetime('now')))")
        recent = db.execute(
            "SELECT COUNT(*) FROM tip_ips WHERE ip = ? AND created_at > datetime('now', '-1 hour')",
            (ip,)
        ).fetchone()[0]
        if recent >= 5:
            db.close()
            return render_template("tips.html", submitted=False,
                error="Too many submissions. Please wait a bit and try again.")

        topic = request.form.get("topic", "").strip()[:200]
        message = request.form.get("message", "").strip()[:5000]
        name = request.form.get("name", "").strip()[:80]
        email = request.form.get("email", "").strip()[:120]
        phone = request.form.get("phone", "").strip()[:40]
        if message and len(message) >= 10:
            db.execute("INSERT INTO tip_ips (ip) VALUES (?)", (ip,))
            db.execute("INSERT INTO tips (topic, message, name, email, phone) VALUES (?,?,?,?,?)",
                       (topic, message, name or None, email or None, phone or None))
            db.commit()
            db.close()
            return render_template("tips.html", submitted=True)
        db.close()
        return render_template("tips.html", submitted=False, error="Please include a message (at least 10 characters).")
    return render_template("tips.html", submitted=False)

@app.route("/editorial-policy")
def editorial_policy_page():
    return render_template("editorial-policy.html")


# ── Routes: Entities ─────────────────────────────────────────────

@app.route("/entities")
def entities_index():
    db = get_rag_db()
    # default view: entities mentioned 3+ times (136 of 1,015) — the full
    # list was a 548KB page of one-mention noise (audit U7). ?all=1 shows all.
    show_all = request.args.get("all") == "1"
    min_mentions = 1 if show_all else 3
    entities = db.execute("""
        SELECT name, type, slug, mention_count, metadata_json FROM entities
        WHERE type != 'meeting' AND mention_count >= ?
        ORDER BY mention_count DESC
    """, (min_mentions,)).fetchall()
    total_entities = db.execute(
        "SELECT COUNT(*) FROM entities WHERE type != 'meeting'").fetchone()[0]

    grouped = {}
    for e in entities:
        t = e["type"]
        if t not in grouped:
            grouped[t] = []
        d = dict(e)
        try:
            d["metadata"] = json.loads(e["metadata_json"]) if e["metadata_json"] else {}
        except (json.JSONDecodeError, TypeError):
            d["metadata"] = {}
        grouped[t].append(d)

    # Sort types: person, location, organization, topic
    type_order = ["person", "location", "organization", "topic"]
    grouped = {k: grouped[k] for k in type_order if k in grouped}

    entity_count = len(entities)
    meeting_count = db.execute(
        "SELECT COUNT(DISTINCT doc_id) FROM chunks"
    ).fetchone()[0]

    return render_template("entities.html",
        grouped=grouped,
        entity_count=entity_count,
        meeting_count=meeting_count,
        show_all=show_all,
        total_entities=total_entities,
    )


@app.route("/entity/<slug>")
def entity_page(slug):
    db = get_rag_db()
    entity = db.execute(
        "SELECT * FROM entities WHERE slug = ?", (slug,)
    ).fetchone()
    if not entity:
        abort(404)

    entity_dict = dict(entity)
    try:
        entity_dict["metadata"] = json.loads(entity["metadata_json"]) if entity["metadata_json"] else {}
    except (json.JSONDecodeError, TypeError):
        entity_dict["metadata"] = {}

    # Get all chunks mentioning this entity
    mentions = db.execute("""
        SELECT c.id, c.doc_id, c.doc_type, c.committee, c.date, c.content,
               c.speaker, c.start_time, c.end_time, em.role
        FROM entity_mentions em
        JOIN chunks c ON c.id = em.chunk_id
        WHERE em.entity_id = ?
        ORDER BY c.date DESC, c.start_time
        LIMIT 50
    """, (entity["id"],)).fetchall()
    mentions = [dict(m) for m in mentions]

    # Find related entities (co-occurring in same chunks)
    related = db.execute("""
        SELECT DISTINCT e.name, e.slug, e.type
        FROM entity_mentions em1
        JOIN entity_mentions em2 ON em1.chunk_id = em2.chunk_id
        JOIN entities e ON e.id = em2.entity_id
        WHERE em1.entity_id = ? AND em2.entity_id != ?
          AND e.type != 'meeting'
        GROUP BY e.id
        ORDER BY COUNT(*) DESC
        LIMIT 15
    """, (entity["id"], entity["id"])).fetchall()
    related = [dict(r) for r in related]

    return render_template("entity.html",
        entity=entity_dict,
        mentions=mentions,
        related_entities=related,
    )


# ── Routes: RAG Search ───────────────────────────────────────────

# Add rag/ to path for import
sys.path.insert(0, RAG_DIR)

@app.route("/search")
def search_page():
    query = request.args.get("q", "").strip()
    corpus = request.args.get("db", "meetings")
    results = []
    ai_answer = None
    chunk_count = 0

    # Count chunks across all DBs
    db_counts = {}
    for name, fname in [("meetings", "rag.db"), ("history", "history.db"), ("code", "code.db")]:
        try:
            db_path = os.path.join(RAG_DIR, fname)
            tmp = sqlite3.connect(db_path)
            db_counts[name] = tmp.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            tmp.close()
        except Exception:
            db_counts[name] = 0
    chunk_count = sum(db_counts.values())

    if query:
        try:
            from multi_search import search as multi_search
            results = multi_search(query, corpus=corpus, limit=15)
        except Exception as e:
            app.logger.error(f"Search error: {e}")
            if corpus == "meetings":
                try:
                    from search import rag_search
                    results = rag_search(query, limit=20)
                except Exception:
                    pass

    # Results render immediately — AI answer loads async via /api/ask
    return render_template("search.html",
        query=query,
        corpus=corpus,
        results=results,
        ai_answer=None,
        chunk_count=f"{chunk_count:,}",
        db_counts=db_counts,
    )


@app.route("/api/ask")
def api_ask():
    """Async AI answer endpoint — called after search results render."""
    query = request.args.get("q", "").strip()
    corpus = request.args.get("db", "meetings")
    if not query:
        return jsonify({"answer": None})

    try:
        from multi_search import search as multi_search, ask_llm
        results = multi_search(query, corpus=corpus, limit=8)
        if results:
            answer = ask_llm(query, results, corpus)
            return jsonify({"answer": answer})
    except Exception as e:
        app.logger.error(f"AI answer error: {e}")

    return jsonify({"answer": None})


@app.route("/api/search")
def api_search():
    query = request.args.get("q", "").strip()
    limit = min(request.args.get("limit", 20, type=int) or 20, 50)
    doc_type = request.args.get("type")
    committee = request.args.get("committee")

    if not query:
        return jsonify([])

    try:
        from search import rag_search
        results = rag_search(query, limit=limit, doc_type=doc_type, committee=committee)
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Routes: API ───────────────────────────────────────────────────

@app.route("/transcript/<event_id>")
def transcript_page(event_id):
    # Try ecode360 first (ChampDS transcripts)
    transcript_path = os.path.join(ECODE_DIR, f"transcript-{event_id}.json")
    if not os.path.exists(transcript_path):
        # Try RAG transcripts dir (YouTube + other sources)
        transcript_path = os.path.join(RAG_DIR, "transcripts", f"transcript-{event_id}.json")
    if not os.path.exists(transcript_path):
        abort(404)
    with open(transcript_path) as f:
        data = json.load(f)
    return render_template("transcript.html", transcript=data)


@app.route("/watch/<event_id>")
def watch_page(event_id):
    """Dedicated video watch page with VideoObject structured data."""
    rag = get_rag_db()
    meeting = rag.execute(
        "SELECT * FROM meetings WHERE event_id = ?", (event_id,)
    ).fetchone()
    if not meeting:
        abort(404)

    # Verify video file exists
    video_dir = os.path.join(os.path.dirname(BASE_DIR), "videos")
    if not os.path.isdir(video_dir):
        video_dir = os.path.join(BASE_DIR, "videos")

    is_yt = event_id.startswith("yt-")
    if not is_yt:
        video_path = os.path.join(video_dir, f"{event_id}.mp4")
        if not os.path.exists(video_path):
            # Check boe subdirectory for YouTube-sourced videos
            video_path = os.path.join(video_dir, "boe", f"{event_id[3:]}.mp4") if is_yt else None
            if not video_path or not os.path.exists(video_path):
                abort(404)

    return render_template("watch.html", meeting=meeting)


# ── Media existence helpers ──────────────────────────────────────
# Video files are pruned weekly (cleanup_videos.sh), so templates must not
# link to /videos/<id>.mp4 unconditionally — audit U1: 16/16 sampled video
# links were 404. These globals let templates gate media links on reality.

def _media_dir(name):
    d = os.path.join(os.path.dirname(BASE_DIR), name)
    return d if os.path.isdir(d) else os.path.join(BASE_DIR, name)


@app.template_global("has_video")
def has_video(event_id):
    return bool(event_id) and os.path.exists(
        os.path.join(_media_dir("videos"), f"{event_id}.mp4"))


@app.template_global("has_audio")
def has_audio(event_id):
    return bool(event_id) and os.path.exists(
        os.path.join(_media_dir("audio"), f"{event_id}.mp3"))


@app.template_global("media_video_href")
def media_video_href(event_id, t=None):
    """Best video URL for a meeting: YouTube for yt- ids, local mp4 if the
    file exists (with #t= offset), else None (caller hides the link)."""
    if not event_id:
        return None
    eid = str(event_id)
    ts = int(t) if t else 0
    if eid.startswith("yt-"):
        return f"https://www.youtube.com/watch?v={eid[3:]}" + (f"&t={ts}s" if ts else "")
    if has_video(eid):
        return f"/videos/{eid}.mp4" + (f"#t={ts}" if ts else "")
    return None


@app.route("/videos/<path:filename>")
def serve_video(filename):
    video_dir = os.path.join(os.path.dirname(BASE_DIR), "videos")
    if not os.path.isdir(video_dir):
        video_dir = os.path.join(BASE_DIR, "videos")
    return send_from_directory(video_dir, filename, mimetype="video/mp4")


@app.route("/audio/<path:filename>")
def serve_audio(filename):
    audio_dir = os.path.join(os.path.dirname(BASE_DIR), "audio")
    if not os.path.isdir(audio_dir):
        audio_dir = os.path.join(BASE_DIR, "audio")
    return send_from_directory(audio_dir, filename, mimetype="audio/mpeg")


# ── Routes: On-demand photo extraction from meeting videos ───────

PHOTOS_CACHE_DIR = os.path.join(BASE_DIR, "photos")

@app.route("/photos/<path:filename>")
def serve_photo(filename):
    """Serve a cached photo, or extract it on-demand from video.

    URL formats:
        /photos/<event_id>_t<seconds>.jpg       — cropped inline photo
        /photos/<event_id>_t<seconds>_og.jpg    — full-frame 16:9 for og:image / Google News
        /photos/<anything>.jpg                  — static cached file
    """
    # Serve from cache if exists
    if os.path.isdir(PHOTOS_CACHE_DIR):
        cached = os.path.join(PHOTOS_CACHE_DIR, filename)
        if os.path.exists(cached):
            return send_from_directory(PHOTOS_CACHE_DIR, filename, mimetype="image/jpeg")

    # Parse filename — supports numeric ChampDS IDs and yt-VIDEO_ID format
    import re as _re
    m = _re.match(r'^([\w-]+)_t(\d+?)(?:_(og|enhanced))?\.(jpg|png)$', filename)
    if not m:
        abort(404)

    event_id = m.group(1)
    timestamp = int(m.group(2))
    suffix = m.group(3)
    is_og = suffix == "og"
    is_enhanced = suffix == "enhanced"
    crop = request.args.get("crop", "auto")

    # For _enhanced requests, fall back to non-enhanced cached photo
    if is_enhanced:
        base_filename = filename.replace('_enhanced', '')
        base_cached = os.path.join(PHOTOS_CACHE_DIR, base_filename)
        if os.path.exists(base_cached):
            return send_from_directory(PHOTOS_CACHE_DIR, base_filename, mimetype="image/jpeg")
        # If no cached version either, extract from video (as non-enhanced)
        filename = base_filename

    # Find video — YouTube videos stored in boe/ subdirectory
    video_dir = os.path.join(os.path.dirname(BASE_DIR), "videos")
    if not os.path.isdir(video_dir):
        video_dir = os.path.join(BASE_DIR, "videos")
    if event_id.startswith("yt-"):
        video_id = event_id[3:]
        video_path = os.path.join(video_dir, "boe", f"{video_id}.mp4")
    else:
        video_path = os.path.join(video_dir, f"{event_id}.mp4")
    if not os.path.exists(video_path):
        abort(404)

    try:
        sys.path.insert(0, RAG_DIR)
        from frame_extract import (
            extract_frame, detect_layout, crop_frame, sharpen,
            QUAD_SPLIT, PODIUM_VIEW, auto_face_crop,
        )
        try:
            from frame_extract import BOE_SPLIT
        except ImportError:
            BOE_SPLIT = {"main": (0, 300, 1280, 720), "speaker": (400, 300, 1280, 720)}
        from PIL import Image

        # Extract frame
        os.makedirs(PHOTOS_CACHE_DIR, exist_ok=True)
        raw_path = os.path.join(PHOTOS_CACHE_DIR, f"{event_id}_t{timestamp}_raw.png")
        extract_frame(video_path, timestamp, raw_path)
        img = Image.open(raw_path)

        if is_og:
            # OG image for Google News: 16:9, minimum 1200px wide
            # Use the inline cached photo as source if it exists (may be upscaled)
            inline_cached = os.path.join(PHOTOS_CACHE_DIR, f"{event_id}_t{timestamp}.jpg")
            if os.path.exists(inline_cached):
                img = Image.open(inline_cached)

            w, h = img.size
            # Crop to exactly 16:9 (trim top/bottom or left/right)
            target_ratio = 16 / 9
            current_ratio = w / h
            if current_ratio > target_ratio:
                # Too wide — crop sides
                new_w = int(h * target_ratio)
                left = (w - new_w) // 2
                img = img.crop((left, 0, left + new_w, h))
            elif current_ratio < target_ratio:
                # Too tall — crop top/bottom
                new_h = int(w / target_ratio)
                top = (h - new_h) // 2
                img = img.crop((0, top, w, top + new_h))

            # Scale to exactly 1200x675 (16:9)
            img = img.resize((1200, 675), Image.LANCZOS)

            img = sharpen(img, 1.2)
        else:
            # Inline photo: auto-detect layout and crop
            is_boe = event_id.startswith("yt-")
            if is_boe:
                # BOE meetings: split-screen with panoramic top, main camera bottom
                img = crop_frame(img, BOE_SPLIT["main"])
            elif crop == "auto":
                layout = detect_layout(img)
                if layout == "podium":
                    img = crop_frame(img, PODIUM_VIEW["podium"])
                elif layout == "quad":
                    img = crop_frame(img, QUAD_SPLIT["board"])
                elif layout == "closeup":
                    img = crop_frame(img, auto_face_crop(img))
                # wide: keep full frame
            elif crop in PODIUM_VIEW:
                img = crop_frame(img, PODIUM_VIEW[crop])
            elif crop in QUAD_SPLIT:
                img = crop_frame(img, QUAD_SPLIT[crop])
            img = sharpen(img, 1.3)

        # Save as JPEG
        out_path = os.path.join(PHOTOS_CACHE_DIR, filename)
        img.save(out_path, "JPEG", quality=88)

        # Clean up raw
        if os.path.exists(raw_path):
            os.remove(raw_path)

        return send_from_directory(PHOTOS_CACHE_DIR, filename, mimetype="image/jpeg")

    except ValueError as e:
        app.logger.warning(f"Photo skipped (no video stream): {e}")
        abort(404)
    except Exception as e:
        app.logger.error(f"Photo extraction error: {e}")
        abort(500)


@app.route("/api/openverse")
def api_openverse():
    """Search Openverse for CC-licensed images. Returns best landscape photo >= 1200px."""
    import urllib.request as _ur
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"url": None})
    try:
        params = _ur.quote(query)
        # Try wide+large first, fall back to any
        best = None
        for filters in ["&aspect_ratio=wide&size=large", "&size=large", ""]:
            url = (f"https://api.openverse.org/v1/images/?q={params}"
                   f"&license_type=commercial&page_size=10{filters}")
            req = _ur.Request(url, headers={"User-Agent": "croton.news/1.0 (hyperlocal news)"})
            resp = _ur.urlopen(req, timeout=10)
            data = json.loads(resp.read())
            results = data.get("results", [])
            # Pick best: landscape preferred, decent size
            for r in results:
                w = r.get("width", 0) or 0
                h = r.get("height", 0) or 0
                if not r.get("url"):
                    continue
                if w >= 800 and w >= h:
                    best = r
                    break
            if not best and results:
                # Take first with a URL
                for r in results:
                    if r.get("url"):
                        best = r
                        break
            if best:
                break
        if best:
            creator = best.get("creator", "Unknown")
            title = best.get("title", "")
            lic = best.get("license", "CC").upper().replace("_", "-")
            credit = f'{title} by {creator} ({lic}) via Openverse'
            return jsonify({
                "url": best.get("url", ""),
                "credit": credit,
                "source": best.get("foreign_landing_url", ""),
                "width": best.get("width", 0),
                "height": best.get("height", 0),
            })
    except Exception as e:
        app.logger.error(f"Openverse error: {e}")
    return jsonify({"url": None})


@app.route("/api/articles")
def api_articles():
    db = get_rag_db()
    limit = min(request.args.get("limit", 20, type=int) or 20, 100)
    offset = request.args.get("offset", 0, type=int) or 0
    committee = request.args.get("committee", "")

    if committee:
        full_name = SLUG_TO_COMMITTEE.get(committee, committee)
        rows = db.execute("""
            SELECT id, committee, date, quick_summary, article, headline
            FROM meetings WHERE committee = ?
            AND article IS NOT NULL AND article != ''
            ORDER BY date DESC LIMIT ? OFFSET ?
        """, (full_name, limit, offset)).fetchall()
    else:
        rows = db.execute("""
            SELECT id, committee, date, quick_summary, article, headline
            FROM meetings
            WHERE article IS NOT NULL AND article != ''
            ORDER BY date DESC LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()

    return jsonify([dict(r) for r in rows])


@app.route("/api/calendar/events")
def api_calendar_events():
    return send_from_directory(STATIC_DIR, "events.json")


@app.route("/api/search/documents")
def api_search_documents():
    db = get_rag_db()
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify([])

    safe_q = sanitize_fts5_query(query)
    if not safe_q:
        return jsonify([])
    try:
        rows = db.execute("""
            SELECT c.doc_id, c.committee, c.date,
                   snippet(chunks_fts, 0, '<mark>', '</mark>', '...', 40) as snippet
            FROM chunks_fts
            JOIN chunks c ON c.id = chunks_fts.rowid
            WHERE chunks_fts MATCH ?
            ORDER BY rank LIMIT 20
        """, (safe_q,)).fetchall()
    except Exception:
        rows = []

    return jsonify([dict(r) for r in rows])


@app.route("/api/health")
def api_health():
    db = get_rag_db()
    article_count = db.execute(
        "SELECT COUNT(*) FROM meetings WHERE article IS NOT NULL AND article != ''"
    ).fetchone()[0]
    chunk_count = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    meeting_count = db.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]
    return jsonify({
        "status": "ok",
        "articles": article_count,
        "meetings": meeting_count,
        "chunks": chunk_count,
        "timestamp": datetime.now().isoformat(),
    })


@app.route("/api/indexnow", methods=["POST"])
@require_admin
def api_indexnow():
    """Submit all article URLs to IndexNow for Bing/Yandex indexing."""
    db = get_rag_db()
    articles = db.execute("""
        SELECT id FROM meetings
        WHERE article IS NOT NULL AND article != ''
        ORDER BY date DESC
    """).fetchall()
    urls = [f"https://croton.news/article/{a['id']}" for a in articles]
    urls.extend(["https://croton.news/", "https://croton.news/meetings",
                 "https://croton.news/topics", "https://croton.news/entities"])
    notify_indexnow(urls)
    return jsonify({"submitted": len(urls)})


# ── Routes: Photo Gallery ────────────────────────────────────────

@app.route("/gallery")
def gallery():
    db = get_photos_db()
    category = request.args.get("category", "")
    sort = request.args.get("sort", "quality")
    search = request.args.get("search", "").strip()
    page = max(request.args.get("page", 1, type=int) or 1, 1)
    PER_PAGE = 120  # unpaginated page was 4.8MB HTML / 1,179 imgs (audit U7)

    # Sort mapping
    order_map = {
        "quality": "p.quality_score DESC",
        "newest": "p.harvested_at DESC",
        "title": "p.title ASC",
    }
    order = order_map.get(sort, "p.quality_score DESC")

    # Build query
    where = ["p.deleted = 0", "p.downloaded = 1"]
    params = []
    if category:
        where.append("p.category = ?")
        params.append(category)
    if search:
        where.append("(p.title LIKE ? OR p.section LIKE ? OR p.creator LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like])

    where_sql = " AND ".join(where)

    filtered_total = db.execute(
        f"SELECT COUNT(*) FROM photos p WHERE {where_sql}", params
    ).fetchone()[0]
    total_pages = max((filtered_total + PER_PAGE - 1) // PER_PAGE, 1)
    page = min(page, total_pages)

    photos = db.execute(f"""
        SELECT p.*, GROUP_CONCAT(pt.tag_id) as tag_ids
        FROM photos p
        LEFT JOIN photo_tags pt ON pt.photo_id = p.id
        WHERE {where_sql}
        GROUP BY p.id
        ORDER BY p.section, {order}
        LIMIT ? OFFSET ?
    """, params + [PER_PAGE, (page - 1) * PER_PAGE]).fetchall()

    # Get all tags
    all_tags = db.execute("SELECT * FROM tags ORDER BY name").fetchall()

    # Category counts
    categories = db.execute("""
        SELECT category as name, COUNT(*) as count
        FROM photos WHERE deleted = 0 AND downloaded = 1
        GROUP BY category ORDER BY count DESC
    """).fetchall()

    # Stats
    total_photos = db.execute(
        "SELECT COUNT(*) FROM photos WHERE deleted = 0 AND downloaded = 1"
    ).fetchone()[0]
    featured_count = db.execute(
        "SELECT COUNT(*) FROM photos WHERE featured = 1 AND deleted = 0"
    ).fetchone()[0]
    tagged_count = db.execute("""
        SELECT COUNT(DISTINCT pt.photo_id) FROM photo_tags pt
        JOIN photos p ON p.id = pt.photo_id WHERE p.deleted = 0
    """).fetchone()[0]

    # Group by section
    sections = []
    current_section = None
    for photo in photos:
        p = dict(photo)
        p["tag_ids_set"] = set(
            int(x) for x in (p["tag_ids"] or "").split(",") if x
        )
        section_name = p["section"] or p["category"] or "Other"
        if not current_section or current_section["name"] != section_name:
            current_section = {"name": section_name, "photos": []}
            sections.append(current_section)
        current_section["photos"].append(p)

    # JSON data for lightbox
    photo_data = [
        {
            "id": p["id"], "title": p["title"], "local_path": p["local_path"],
            "creator": p["creator"], "license": p["license"],
            "license_version": p["license_version"],
            "width": p["width"], "height": p["height"],
            "foreign_landing_url": p["foreign_landing_url"],
        }
        for s in sections for p in s["photos"]
    ]

    return render_template("gallery.html",
        sections=sections,
        all_tags=all_tags,
        categories=categories,
        category=category,
        sort=sort,
        search=search,
        total_photos=total_photos,
        featured_count=featured_count,
        tagged_count=tagged_count,
        photo_data_json=json.dumps(photo_data),
        page=page,
        total_pages=total_pages,
        filtered_total=filtered_total,
    )


@app.route("/api/photos/<int:photo_id>", methods=["DELETE"])
@require_admin
def api_delete_photo(photo_id):
    db = get_photos_db()
    photo = db.execute(
        "SELECT local_path FROM photos WHERE id = ?", (photo_id,)
    ).fetchone()
    if photo and photo["local_path"]:
        filepath = os.path.join(STATIC_DIR, photo["local_path"])
        try:
            os.remove(filepath)
        except OSError:
            pass
    db.execute("DELETE FROM photo_tags WHERE photo_id = ?", (photo_id,))
    db.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/photos/<int:photo_id>/feature", methods=["POST"])
@require_admin
def api_feature_photo(photo_id):
    db = get_photos_db()
    data = request.get_json()
    featured = 1 if data.get("featured") else 0
    db.execute("UPDATE photos SET featured = ? WHERE id = ?", (featured, photo_id))
    db.commit()
    return jsonify({"ok": True, "featured": featured})


@app.route("/api/photos/<int:photo_id>/tags", methods=["POST"])
@require_admin
def api_toggle_tag(photo_id):
    db = get_photos_db()
    data = request.get_json()
    tag_id = data.get("tag_id")
    action = data.get("action", "add")

    if action == "add":
        db.execute(
            "INSERT OR IGNORE INTO photo_tags (photo_id, tag_id) VALUES (?, ?)",
            (photo_id, tag_id)
        )
    else:
        db.execute(
            "DELETE FROM photo_tags WHERE photo_id = ? AND tag_id = ?",
            (photo_id, tag_id)
        )
    db.commit()

    tag = db.execute("SELECT color FROM tags WHERE id = ?", (tag_id,)).fetchone()
    return jsonify({"ok": True, "color": tag["color"] if tag else "#6b7280"})


@app.route("/api/comments", methods=["POST"])
def api_post_comment():
    data = request.get_json()
    if not data:
        return jsonify({"ok": False, "error": "Invalid request"}), 400

    # Honeypot - if website field is filled, it's a bot
    if data.get("website"):
        return jsonify({"ok": True, "id": 0})

    article_id = str(data.get("article_id", "")).strip()
    name = str(data.get("name", "")).strip()[:80]
    body = str(data.get("body", "")).strip()[:2000]

    if not article_id or not name or not body:
        return jsonify({"ok": False, "error": "Name and comment are required"}), 400
    if len(name) < 2:
        return jsonify({"ok": False, "error": "Name too short"}), 400
    if len(body) < 3:
        return jsonify({"ok": False, "error": "Comment too short"}), 400

    # Basic rate limiting: max 5 comments per IP per hour
    ip = request.remote_addr or ""
    cdb = get_comments_db()
    recent = cdb.execute(
        "SELECT COUNT(*) FROM comments WHERE ip = ? AND created_at > datetime('now', '-1 hour')",
        (ip,)
    ).fetchone()[0]
    if recent >= 5:
        return jsonify({"ok": False, "error": "Too many comments. Please wait a bit."}), 429

    # Sanitize: strip HTML tags
    import re as _re
    body = _re.sub(r'<[^>]+>', '', body)
    name = _re.sub(r'<[^>]+>', '', name)

    cdb.execute(
        "INSERT INTO comments (article_id, name, body, ip) VALUES (?, ?, ?, ?)",
        (article_id, name, body, ip)
    )
    cdb.commit()
    return jsonify({"ok": True})


@app.route("/api/comments/<int:comment_id>", methods=["DELETE"])
@require_admin
def api_delete_comment(comment_id):
    cdb = get_comments_db()
    cdb.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
    cdb.commit()
    return jsonify({"ok": True})


@app.route("/api/photos/stats")
def api_photos_stats():
    db = get_photos_db()
    total = db.execute("SELECT COUNT(*) FROM photos WHERE deleted = 0").fetchone()[0]
    downloaded = db.execute("SELECT COUNT(*) FROM photos WHERE downloaded = 1 AND deleted = 0").fetchone()[0]
    featured = db.execute("SELECT COUNT(*) FROM photos WHERE featured = 1 AND deleted = 0").fetchone()[0]
    categories = db.execute("""
        SELECT category, COUNT(*) as c FROM photos
        WHERE deleted = 0 GROUP BY category ORDER BY c DESC
    """).fetchall()
    return jsonify({
        "total": total,
        "downloaded": downloaded,
        "featured": featured,
        "categories": [dict(r) for r in categories],
    })


# ── Routes: Feeds & Static ───────────────────────────────────────

@app.route("/feed")
def rss_feed():
    """RSS 2.0 feed with proper article URLs and metadata."""
    rag = get_rag_db()
    articles = rag.execute("""
        SELECT id, committee, date, headline, quick_summary, event_id
        FROM meetings
        WHERE article IS NOT NULL AND article != ''
        ORDER BY date DESC LIMIT 20
    """).fetchall()
    articles = [dict(a) for a in articles]

    from email.utils import formatdate
    from time import mktime
    import datetime as _dt

    rss = '<?xml version="1.0" encoding="UTF-8"?>\n'
    rss += '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:media="http://search.yahoo.com/mrss/">\n'
    rss += '<channel>\n'
    rss += '  <title>croton.news</title>\n'
    rss += '  <link>https://croton.news</link>\n'
    rss += '  <description>AI-assisted civic information covering Croton-on-Hudson, NY village government from public records</description>\n'
    rss += '  <language>en-us</language>\n'
    rss += f'  <lastBuildDate>{formatdate(localtime=True)}</lastBuildDate>\n'
    rss += '  <copyright>2026 croton.news</copyright>\n'
    rss += '  <managingEditor>editor@croton.news (Matthew Broudy)</managingEditor>\n'
    rss += '  <atom:link href="https://croton.news/feed" rel="self" type="application/rss+xml"/>\n'
    rss += '  <image><url>https://croton.news/static/favicon.svg</url><title>croton.news</title><link>https://croton.news</link></image>\n'

    for a in articles:
        from zoneinfo import ZoneInfo
        eastern = ZoneInfo("America/New_York")
        dt = _dt.datetime.strptime(a["date"], "%Y-%m-%d").replace(hour=19, tzinfo=eastern)
        pub_date = formatdate(dt.timestamp(), localtime=False, usegmt=False)
        # formatdate with UTC timestamp; manually append offset
        off = dt.strftime("%z")  # e.g. -0400
        pub_date = dt.strftime("%a, %d %b %Y %H:%M:%S ") + off[:3] + off[3:]
        eid = a.get("event_id") or ""
        og_img = f"https://croton.news/photos/{eid}_t60_og.jpg" if eid else "https://croton.news/photos/placeholder_og.jpg"
        title = (a["headline"] or a["quick_summary"] or "").replace("&", "&amp;").replace("<", "&lt;")
        desc = (a["quick_summary"] or "").replace("&", "&amp;").replace("<", "&lt;")

        rss += '  <item>\n'
        rss += f'    <title>{title}</title>\n'
        rss += f'    <link>https://croton.news/article/{a["id"]}</link>\n'
        rss += f'    <guid isPermaLink="true">https://croton.news/article/{a["id"]}</guid>\n'
        rss += f'    <pubDate>{pub_date}</pubDate>\n'
        rss += f'    <dc:creator>Matthew Broudy</dc:creator>\n'
        rss += f'    <description><![CDATA[{desc}]]></description>\n'
        rss += f'    <category>{a["committee"]}</category>\n'
        rss += f'    <media:content url="{og_img}" medium="image" type="image/jpeg" width="1200" height="675"/>\n'
        rss += '  </item>\n'

    rss += '</channel>\n</rss>'
    return Response(rss, mimetype="application/rss+xml")


@app.route("/feeds/<path:filename>")
def feeds(filename):
    feeds_dir = os.path.join(STATIC_DIR, "feeds")
    mimetype = "text/calendar" if filename.endswith(".ics") else "application/json"
    return send_from_directory(feeds_dir, filename, mimetype=mimetype)


INDEXNOW_KEY = "d0b7fffbe494bf18af45048af4bddaef"

@app.route("/site.webmanifest")
def webmanifest():
    return send_from_directory("static", "site.webmanifest", mimetype="application/manifest+json")

@app.route("/robots.txt")
def robots():
    return Response(
        "User-agent: *\n"
        "Allow: /\n\n"
        "User-agent: Googlebot-News\n"
        "Allow: /\n\n"
        "Sitemap: https://croton.news/sitemap.xml\n"
        "Sitemap: https://croton.news/news-sitemap.xml\n\n"
        "# LLM-friendly site description\n"
        "# https://croton.news/llms.txt\n"
        "\n"
        "# See https://llmstxt.org/\n"
        "llms.txt: https://croton.news/llms.txt\n"
        "llms-full.txt: https://croton.news/llms-full.txt\n",
        mimetype="text/plain",
    )

@app.route("/humans.txt")
def humans_txt_file():
    db = get_rag_db()
    latest = db.execute("SELECT MAX(date) FROM meetings WHERE article IS NOT NULL").fetchone()[0] or "unknown"
    return Response(
        "/* TEAM */\n"
        "Creator: Andy\n"
        "Contact: andy@agentwire.email\n"
        "Site: https://croton.news\n"
        "Description: Local news and meeting coverage for Croton-on-Hudson, NY\n"
        "\n"
        "/* SITE */\n"
        f"Last update: {latest}\n"
        "Standards: HTML5, CSS3, JavaScript\n"
        "Software: Flask, Nginx, Python, SQLite\n"
        "Hosting: Self-hosted VPS\n",
        mimetype="text/plain",
    )

@app.route("/llms.txt")
def llms_txt():
    db = get_rag_db()
    article_count = db.execute(
        "SELECT COUNT(*) FROM meetings WHERE article IS NOT NULL"
    ).fetchone()[0]
    meeting_count = db.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]
    committees = [r[0] for r in db.execute(
        "SELECT DISTINCT committee FROM meetings ORDER BY committee"
    ).fetchall()]
    recent = db.execute(
        "SELECT id, committee, date, headline FROM meetings "
        "WHERE article IS NOT NULL AND headline IS NOT NULL "
        "ORDER BY date DESC LIMIT 10"
    ).fetchall()

    lines = [
        "# croton.news \u2014 Croton-on-Hudson, NY Local News",
        "> For the full version, see https://croton.news/llms-full.txt",
        "",
        "## About",
        "croton.news is an independent, AI-assisted civic information project",
        "covering the Village of Croton-on-Hudson, New York. We publish news",
        "articles and meeting coverage drawn from public records, official",
        f"meeting transcripts, and government documents. {article_count} articles",
        f"generated from {meeting_count} public meetings.",
        "",
        "## Pages",
        "- Homepage: https://croton.news/",
        "- Meetings: https://croton.news/meetings",
        "- Editorials: https://croton.news/editorials",
        "- Calendar: https://croton.news/calendar",
        "- Topics: https://croton.news/topics",
        "- Entities: https://croton.news/entities",
        "- Documents: https://croton.news/documents",
        "- Search: https://croton.news/search",
        "- About: https://croton.news/about",
        "",
        "## Committees Covered",
    ]
    for c in committees:
        if c != "Topics":
            lines.append(f"- {c}")
    lines.append("")
    lines.append("## Recent Articles")
    for r in recent:
        title = r["headline"] or f"{r['committee']} \u2014 {r['date']}"
        lines.append(f"- {title}: https://croton.news/article/{r['id']}")
    lines.append("")
    lines.append("## History Archive")
    lines.append("- History Home: https://croton.news/history")
    lines.append("- Timeline: https://croton.news/history/timeline")
    lines.append("- Historical Maps: https://croton.news/history/maps")
    lines.append("- Historical Documents: https://croton.news/history/documents")
    lines.append("- Stories: https://croton.news/history/stories")
    lines.append("- McDonald Interviews (232 transcripts): https://croton.news/history/mcdonald")
    lines.append("")
    lines.append("## APIs")
    lines.append("- GET https://croton.news/api/articles?limit=20 \u2014 Recent articles (JSON)")
    lines.append("- GET https://croton.news/api/articles?committee=board-of-trustees \u2014 Filter by committee")
    lines.append("- GET https://croton.news/api/search?q=QUERY \u2014 Full-text search")
    lines.append("- GET https://croton.news/feed \u2014 RSS feed")
    lines.append("")
    lines.append("## Data")
    lines.append("- Sitemap: https://croton.news/sitemap.xml")
    lines.append("- News Sitemap: https://croton.news/news-sitemap.xml")
    lines.append("- Robots: https://croton.news/robots.txt")

    return Response("\n".join(lines) + "\n", mimetype="text/plain")


@app.route(f"/{INDEXNOW_KEY}.txt")
def indexnow_key():
    return Response(INDEXNOW_KEY, mimetype="text/plain")

def notify_indexnow(urls):
    """Notify Bing/Yandex of new or updated URLs via IndexNow."""
    import urllib.request
    if isinstance(urls, str):
        urls = [urls]
    payload = json.dumps({
        "host": "croton.news",
        "key": INDEXNOW_KEY,
        "keyLocation": f"https://croton.news/{INDEXNOW_KEY}.txt",
        "urlList": urls,
    }).encode()
    def _send():
        for endpoint in ("https://api.indexnow.org/indexnow",
                         "https://www.bing.com/indexnow"):
            try:
                req = urllib.request.Request(
                    endpoint, data=payload,
                    headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=10)
            except Exception:
                pass
    threading.Thread(target=_send, daemon=True).start()


@app.route("/news-sitemap.xml")
def news_sitemap():
    """Google News sitemap — articles from last 48 hours (Google requirement)."""
    rag = get_rag_db()
    articles = rag.execute("""
        SELECT id, date, committee, headline, quick_summary
        FROM meetings
        WHERE article IS NOT NULL AND article != ''
          AND date >= date('now', '-2 days')
        ORDER BY date DESC
    """).fetchall()
    # If no recent articles, include last 10 for discovery
    if not articles:
        articles = rag.execute("""
            SELECT id, date, committee, headline, quick_summary
            FROM meetings
            WHERE article IS NOT NULL AND article != ''
            ORDER BY date DESC LIMIT 10
        """).fetchall()

    xml = '<?xml version="1.0" encoding="UTF-8"?>\n'
    xml += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"\n'
    xml += '        xmlns:news="http://www.google.com/schemas/sitemap-news/0.9"\n'
    xml += '        xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">\n'
    for a in articles:
        a = dict(a)
        eid = rag.execute("SELECT event_id FROM meetings WHERE id = ?", (a["id"],)).fetchone()
        eid_val = eid["event_id"] if eid and eid["event_id"] else ""
        og_img = f"https://croton.news/photos/{eid_val}_t60_og.jpg" if eid_val else "https://croton.news/photos/placeholder_og.jpg"
        xml += '  <url>\n'
        xml += f'    <loc>https://croton.news/article/{a["id"]}</loc>\n'
        xml += '    <news:news>\n'
        xml += '      <news:publication>\n'
        xml += '        <news:name>croton.news</news:name>\n'
        xml += '        <news:language>en</news:language>\n'
        xml += '      </news:publication>\n'
        xml += f'      <news:publication_date>{a["date"]}T19:00:00{_tz_offset(a["date"])}</news:publication_date>\n'
        xml += f'      <news:title>{a["headline"] or a["quick_summary"] or ""}</news:title>\n'
        xml += '    </news:news>\n'
        xml += '    <image:image>\n'
        xml += f'      <image:loc>{og_img}</image:loc>\n'
        xml += f'      <image:caption>{a["headline"] or ""}</image:caption>\n'
        xml += '    </image:image>\n'
        xml += '  </url>\n'
    xml += '</urlset>'
    return Response(xml, mimetype="application/xml")


@app.route("/sitemap.xml")
def sitemap():
    rag = get_rag_db()
    meetings = rag.execute("""
        SELECT id, date, article_model FROM meetings
        WHERE article IS NOT NULL AND article != ''
        ORDER BY date DESC
    """).fetchall()

    # Also pull legacy summaries that aren't in meetings table
    try:
        db = get_summaries_db()
        legacy = db.execute("SELECT doc_id, date FROM summaries ORDER BY date DESC").fetchall()
    except Exception:
        legacy = []

    # Meeting hub pages (all meetings, not just those with articles)
    all_meetings = rag.execute("""
        SELECT id, date, event_id, has_transcript FROM meetings ORDER BY date DESC
    """).fetchall()

    # Topics and entities for full coverage
    topics = rag.execute("SELECT slug, last_date FROM topic_threads WHERE status = 'active' ORDER BY last_date DESC").fetchall()
    entities = rag.execute("SELECT slug, last_seen_date FROM entities WHERE mention_count >= 3 ORDER BY mention_count DESC LIMIT 200").fetchall()

    # Build set of event_ids that have video files on disk
    video_dir = os.path.join(os.path.dirname(BASE_DIR), "videos")
    if not os.path.isdir(video_dir):
        video_dir = os.path.join(BASE_DIR, "videos")
    video_event_ids = set()
    if os.path.isdir(video_dir):
        for fname in os.listdir(video_dir):
            if fname.endswith(".mp4"):
                video_event_ids.add(fname[:-4])  # e.g. "1109"
        boe_dir = os.path.join(video_dir, "boe")
        if os.path.isdir(boe_dir):
            for fname in os.listdir(boe_dir):
                if fname.endswith(".mp4"):
                    video_event_ids.add("yt-" + fname[:-4])  # e.g. "yt-abc123"

    # History section — stories and McDonald interviews
    history_stories = []
    stories_dir = os.path.join(os.path.dirname(BASE_DIR), "rag", "history", "stories")
    if not os.path.isdir(stories_dir):
        stories_dir = os.path.join(BASE_DIR, "rag", "history", "stories")
    if os.path.isdir(stories_dir):
        for fname in sorted(os.listdir(stories_dir)):
            if fname.endswith(".md") and not fname.endswith("_long.md"):
                slug = fname.replace(".md", "")
                history_stories.append(slug)

    mcdonald_slugs = []
    mcdonald_db_path = os.path.join(os.path.dirname(BASE_DIR), "rag", "history", "mcdonald.db")
    if not os.path.exists(mcdonald_db_path):
        mcdonald_db_path = os.path.join(BASE_DIR, "rag", "history", "mcdonald.db")
    if os.path.exists(mcdonald_db_path):
        try:
            mdb = sqlite3.connect(mcdonald_db_path)
            mdb.row_factory = sqlite3.Row
            mcdonald_slugs = [r["slug"] for r in mdb.execute("SELECT slug FROM interviews ORDER BY slug").fetchall()]
            mdb.close()
        except Exception:
            pass

    return Response(
        render_template("sitemap.xml",
            meetings=meetings, legacy=legacy, all_meetings=all_meetings,
            topics=topics, entities=entities, video_event_ids=video_event_ids,
            history_stories=history_stories, mcdonald_slugs=mcdonald_slugs),
        mimetype="application/xml",
    )


# ── Main ──────────────────────────────────────────────────────────



# -- Status Page -------------------------------------------------------

# /status runs subprocess tails + external HTTP probes — cache the rendered
# page for 60s so reloads don't hammer the worker (audit M8)
_status_cache = {"t": 0.0, "html": None}


@app.route("/status")
@require_admin
def status_page():
    import subprocess
    import time as _time
    import urllib.request as _urllib_req
    from datetime import datetime, timedelta

    if _status_cache["html"] is not None and _time.time() - _status_cache["t"] < 60:
        return _status_cache["html"]

    rag = get_rag_db()
    today = datetime.now().date()

    # Stats
    total = rag.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]
    with_articles = rag.execute("SELECT COUNT(*) FROM meetings WHERE article IS NOT NULL AND article != ''").fetchone()[0]
    last_row = rag.execute("SELECT date, committee FROM meetings ORDER BY date DESC LIMIT 1").fetchone()
    last_meeting_date = last_row["date"] if last_row else "None"
    last_meeting_committee = last_row["committee"] if last_row else ""
    try:
        days_since_last = (today - datetime.strptime(last_meeting_date, "%Y-%m-%d").date()).days
    except Exception:
        days_since_last = -1

    max_ev = rag.execute(
        "SELECT MAX(CAST(event_id AS INTEGER)) as eid, date FROM meetings WHERE event_id IS NOT NULL AND event_id NOT LIKE 'yt-%'"
    ).fetchone()
    max_event_id = max_ev["eid"] or 0
    max_event_date = max_ev["date"] or ""

    stats = dict(total_meetings=total, with_articles=with_articles,
                 last_meeting_date=last_meeting_date, last_meeting_committee=last_meeting_committee,
                 days_since_last=days_since_last, max_event_id=max_event_id, max_event_date=max_event_date)

    # ChampDS check
    champds = {"api_live": False, "portal_live": False, "next_event_exists": False, "scan_ceiling": max_event_id + 20}
    try:
        with _urllib_req.urlopen(f"https://playapi.champds.com/crotononhudsonny/event/{max_event_id}", timeout=8) as r:
            champds["api_live"] = "Event" in json.loads(r.read())
    except Exception:
        pass
    try:
        req = _urllib_req.Request("https://play.champds.com/crotononhudsonny", method="HEAD")
        with _urllib_req.urlopen(req, timeout=8) as r:
            champds["portal_live"] = r.status < 400
    except Exception:
        pass

    # Cron jobs
    cron_jobs = []
    job_defs = [
        {"name": "Pipeline (discover+process)", "script": "pipeline.py process-new", "schedule": "Daily 7:00 AM",
         "log": "/var/log/croton-pipeline.log", "cron_pattern": "pipeline.py"},
        {"name": "BOE YouTube Poller", "script": "poll_boe.py --write", "schedule": "Daily 7:30 AM",
         "log": "/var/log/croton-boe.log", "cron_pattern": "poll_boe.py"},
        {"name": "BoardDocs Sync", "script": "boarddocs.py sync", "schedule": "Daily 7:15 AM",
         "log": "/var/log/croton-boarddocs.log", "cron_pattern": "boarddocs.py"},
        {"name": "Story Miner", "script": "story_miner.py scan --email", "schedule": "Daily 8:00 AM",
         "log": "/var/log/croton-stories.log", "cron_pattern": "story_miner.py"},
        {"name": "Minutes-to-Article", "script": "write_from_minutes.py", "schedule": "Daily 7:30 AM",
         "log": "/var/log/croton-minutes-articles.log", "cron_pattern": "write_from_minutes.py"},
        {"name": "Auto Pipeline (upcoming+previews)", "script": "auto_pipeline.py", "schedule": "Hourly",
         "log": "/var/log/auto_pipeline.log", "cron_pattern": "auto_pipeline.py"},
    ]
    try:
        crontab_output = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        crontab_output = ""

    for jd in job_defs:
        in_crontab = jd["cron_pattern"] in crontab_output
        last_run = None
        last_output = ""
        try:
            mtime = os.path.getmtime(jd["log"])
            last_run = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
            hours_ago = (datetime.now() - datetime.fromtimestamp(mtime)).total_seconds() / 3600
        except Exception:
            hours_ago = 9999
        try:
            result = subprocess.run(["tail", "-3", jd["log"]], capture_output=True, text=True, timeout=5)
            last_output = result.stdout.strip()
        except Exception:
            last_output = ""
        if not in_crontab:
            status = "dead"
        elif hours_ago > 48:
            status = "error"
        elif "ERR" in last_output or "failed" in last_output.lower() or "error" in last_output.lower():
            status = "warn"
        else:
            status = "ok"
        cron_jobs.append(dict(name=jd["name"], script=jd["script"],
            schedule=jd["schedule"] if in_crontab else jd["schedule"] + " -- NOT IN CRONTAB",
            last_run=last_run, status=status, last_output=last_output))

    # Logs
    log_files = [("/var/log/croton-pipeline.log", "Pipeline"), ("/var/log/croton-boe.log", "BOE Poller"),
                 ("/var/log/croton-boarddocs.log", "BoardDocs"), ("/var/log/croton-stories.log", "Story Miner"),
                 ("/var/log/auto_pipeline.log", "Auto Pipeline")]
    logs = []
    for lpath, lname in log_files:
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(lpath)).strftime("%Y-%m-%d %H:%M")
            result = subprocess.run(["tail", "-20", lpath], capture_output=True, text=True, timeout=5)
            logs.append(dict(name=lname, modified=mtime, tail=result.stdout.strip()))
        except Exception:
            logs.append(dict(name=lname, modified="N/A", tail="Log file not found"))

    # Expected schedule (confirmed from village calendar + BoardDocs)
    confirmed_schedule = [
        ("2026-04-01", "Board Of Trustees"),
        ("2026-04-08", "Board Of Trustees"),
        ("2026-04-14", "Planning Board Meeting"),
        ("2026-04-15", "Board Of Trustees"),
        ("2026-04-21", "Zoning Board of Appeals"),
        ("2026-04-22", "Board Of Trustees"),
        ("2026-05-05", "Board Of Trustees"),
        ("2026-05-06", "Board Of Trustees"),
        ("2026-05-07", "Board of Education"),
        ("2026-05-19", "Zoning Board of Appeals"),
        ("2026-05-19", "Board of Education"),
        ("2026-05-20", "Board Of Trustees"),
        ("2026-05-26", "Planning Board"),
        ("2026-05-27", "Conservation Advisory Council"),
        ("2026-06-03", "Board Of Trustees"),
        ("2026-06-04", "Board of Education"),
        ("2026-06-17", "Zoning Board of Appeals"),
        ("2026-06-18", "Board Of Trustees"),
        ("2026-06-23", "Planning Board"),
    ]
    expected_meetings = []
    known_dates = {(r[0], r[1]) for r in rag.execute("SELECT date, committee FROM meetings").fetchall()}
    for date_str, committee in confirmed_schedule:
        try:
            date_val = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if date_val < today - timedelta(days=45) or date_val > today + timedelta(days=60):
            continue
        if (date_str, committee) in known_dates:
            st = "covered"
        elif date_val > today:
            st = "upcoming"
        else:
            st = "missing"
        expected_meetings.append(dict(date=date_str, committee=committee, status=st))
    expected_meetings.sort(key=lambda x: x["date"])

    # Phone relay actions
    relay_actions = []
    try:
        import xml.etree.ElementTree as _ET
        _rss_req = _urllib_req.Request(
            "https://www.youtube.com/feeds/videos.xml?channel_id=UC8PPKYkTdJs0GJ10k-lABXQ",
            headers={"User-Agent": "Mozilla/5.0 (compatible; croton.news/1.0)"})
        with _urllib_req.urlopen(_rss_req, timeout=10) as _resp:
            _rss_root = _ET.fromstring(_resp.read())
        _rss_ns = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
        _yt_known = {row["event_id"].replace("yt-", "") for row in
                     rag.execute("SELECT event_id FROM meetings WHERE event_id LIKE 'yt-%'").fetchall()}
        _today_str = today.strftime("%Y-%m-%d")
        for _entry in _rss_root.findall("atom:entry", _rss_ns):
            _vid = _entry.find("yt:videoId", _rss_ns).text
            _title = _entry.find("atom:title", _rss_ns).text
            _pub = _entry.find("atom:published", _rss_ns).text[:10]
            import re as _re
            _dm = _re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", _title)
            if _dm:
                _m, _d, _y = int(_dm.group(1)), int(_dm.group(2)), int(_dm.group(3))
                if _y < 100: _y += 2000
                if f"{_y}-{_m:02d}-{_d:02d}" > _today_str:
                    continue
            if _vid not in _yt_known and _pub <= _today_str:
                relay_actions.append(dict(action="boe-fetch", description="New YouTube video: " + _title,
                                         command="boe-fetch " + _vid, priority="medium"))
    except Exception:
        pass

    bd_missing = rag.execute(
        "SELECT date, committee, boarddocs_id, has_minutes, "
        "agenda_json IS NOT NULL AND agenda_json != '' as has_agenda "
        "FROM meetings WHERE committee LIKE '%Education%' "
        "AND date >= date('now', '-45 days') AND date < date('now') "
        "AND (article_model IS NULL OR article_model != 'skipped')"
    ).fetchall()
    for m in bd_missing:
        needs = []
        if not m["has_agenda"]: needs.append("agenda")
        if not m["has_minutes"]: needs.append("minutes")
        if needs:
            relay_actions.append(dict(action="boarddocs-fetch",
                description="BOE " + m["date"] + " -- needs " + " + ".join(needs),
                command="boarddocs-fetch " + m["date"] + " " + (m["boarddocs_id"] or "unknown"),
                priority="high" if "minutes" in needs else "medium"))

    if days_since_last > 7:
        relay_actions.append(dict(action="investigate",
            description="ChampDS has not posted new events in " + str(days_since_last) + " days",
            command="# Check if Village has switched platforms or is just delayed",
            priority="high"))



    # Photo enhancement pipeline check
    photo_pipeline = {}
    # Check ffmpeg
    try:
        ffmpeg_result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=5)
        photo_pipeline["ffmpeg"] = "ok" if ffmpeg_result.returncode == 0 else "missing"
        photo_pipeline["ffmpeg_version"] = ffmpeg_result.stdout.split("\n")[0][:60] if ffmpeg_result.returncode == 0 else ""
    except Exception:
        photo_pipeline["ffmpeg"] = "missing"
        photo_pipeline["ffmpeg_version"] = ""

    # Check Replicate token
    # Check .env file for Replicate token (pipeline reads it at runtime)
    _replicate_set = bool(os.environ.get("REPLICATE_API_TOKEN", ""))
    if not _replicate_set:
        try:
            with open("/opt/croton-news/rag/.env") as _ef:
                _replicate_set = "REPLICATE_API_TOKEN=" in _ef.read()
        except Exception:
            pass
    photo_pipeline["replicate_token"] = _replicate_set

    # Check Pillow
    try:
        result = subprocess.run(
            ["/opt/croton-news/venv/bin/python", "-c", "from PIL import Image; print('ok')"],
            capture_output=True, text=True, timeout=5, cwd="/opt/croton-news/rag"
        )
        photo_pipeline["pillow"] = result.returncode == 0
    except Exception:
        photo_pipeline["pillow"] = False

    # Check frame_extract module
    try:
        result = subprocess.run(
            ["/opt/croton-news/venv/bin/python", "-c", "from frame_extract import extract_frame; print('ok')"],
            capture_output=True, text=True, timeout=5, cwd="/opt/croton-news/rag"
        )
        photo_pipeline["frame_extract"] = result.returncode == 0
    except Exception:
        photo_pipeline["frame_extract"] = False

    # Articles with/without photos
    total_articles = rag.execute("SELECT COUNT(*) FROM meetings WHERE article IS NOT NULL AND article != ''").fetchone()[0]
    articles_with_photos = rag.execute("SELECT COUNT(*) FROM meetings WHERE article LIKE '%{{photo:%'").fetchone()[0]
    photo_pipeline["total_articles"] = total_articles
    photo_pipeline["with_photos"] = articles_with_photos

    # Recent articles missing photos (last 30 days, has video)
    missing_photos = rag.execute("""
        SELECT id, event_id, date, committee FROM meetings
        WHERE article IS NOT NULL AND article != ''
        AND article NOT LIKE '%{{photo:%'
        AND has_video = 1
        AND date >= date('now', '-30 days')
        ORDER BY date DESC
    """).fetchall()
    photo_pipeline["missing_photos"] = [dict(r) for r in missing_photos]


    # Minutes-to-article pipeline check
    minutes_pipeline = {}
    # Meetings with minutes but no article
    minutes_pending = rag.execute("""
        SELECT id, date, committee, length(minutes_text) as min_len
        FROM meetings
        WHERE has_minutes = 1 AND minutes_text IS NOT NULL AND length(minutes_text) > 1000
        AND (article IS NULL OR article = '')
        AND date < date('now')
        ORDER BY date DESC
    """).fetchall()
    minutes_pipeline["pending"] = [dict(r) for r in minutes_pending]

    # Articles generated from minutes
    minutes_articles = rag.execute("""
        SELECT COUNT(*) FROM meetings WHERE article_model = 'claude-sonnet-4-minutes'
    """).fetchone()[0]
    minutes_pipeline["articles_generated"] = minutes_articles

    # Total meetings with minutes
    total_with_minutes = rag.execute("""
        SELECT COUNT(*) FROM meetings WHERE has_minutes = 1
    """).fetchone()[0]
    minutes_pipeline["total_with_minutes"] = total_with_minutes

    # Check if script exists
    minutes_pipeline["script_exists"] = os.path.exists("/opt/croton-news/rag/write_from_minutes.py")

    # Check OpenRouter key
    _or_key = bool(os.environ.get("OPENROUTER_API_KEY", ""))
    if not _or_key:
        try:
            with open("/opt/croton-news/rag/.env") as _ef:
                _or_key = "OPENROUTER_API_KEY=" in _ef.read()
        except Exception:
            pass
    minutes_pipeline["openrouter_key"] = _or_key

    # Dependency health check
    dep_checks = []
    critical_deps = [
        ("pymupdf", "PDF text extraction for agenda packets"),
        ("deepgram", "Audio transcription"),
        ("google.generativeai", "Gemini API (calendar, embeddings)"),
        ("openai", "OpenAI/OpenRouter API (article writing)"),
        ("bs4", "HTML scraping (BeautifulSoup)"),
        ("requests", "HTTP requests"),
    ]
    for mod_name, purpose in critical_deps:
        try:
            result = subprocess.run(
                ["/opt/croton-news/venv/bin/python", "-c", f"import {mod_name}; print(getattr({mod_name}, '__version__', 'installed'))"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                dep_checks.append(dict(name=mod_name, purpose=purpose, status="ok", version=result.stdout.strip()))
            else:
                dep_checks.append(dict(name=mod_name, purpose=purpose, status="error", version="NOT INSTALLED"))
        except Exception as e:
            dep_checks.append(dict(name=mod_name, purpose=purpose, status="error", version=str(e)[:50]))

    # Article Quality Pipeline stats
    article_quality = {}
    # Count articles by model type
    model_counts = rag.execute("""
        SELECT article_model, COUNT(*) as c FROM meetings
        WHERE article IS NOT NULL AND article != ''
        GROUP BY article_model ORDER BY c DESC
    """).fetchall()
    article_quality["model_counts"] = [dict(r) for r in model_counts]

    # Count articles with quote tags
    article_quality["with_quotes"] = rag.execute(
        "SELECT COUNT(*) FROM meetings WHERE article LIKE '%{{quote:%'"
    ).fetchone()[0]

    # Count articles with photo tags
    article_quality["with_photos"] = rag.execute(
        "SELECT COUNT(*) FROM meetings WHERE article LIKE '%{{photo:%'"
    ).fetchone()[0]

    # Count articles with footnotes
    article_quality["with_footnotes"] = rag.execute(
        "SELECT COUNT(*) FROM meetings WHERE article LIKE '%**Footnotes:**%'"
    ).fetchone()[0]

    # Preview articles still needing upgrade
    article_quality["pending_upgrades"] = rag.execute("""
        SELECT COUNT(*) FROM meetings
        WHERE has_transcript = 1
        AND (article_model LIKE '%packet%' OR article_model LIKE '%agenda%' OR article_model LIKE '%preview%')
    """).fetchone()[0]

    # Recent articles for spot check
    recent_articles = rag.execute("""
        SELECT id, date, committee, headline, article_model, length(article) as chars,
               CASE WHEN article LIKE '%{{quote:%' THEN 1 ELSE 0 END as has_quotes,
               CASE WHEN article LIKE '%{{photo:%' THEN 1 ELSE 0 END as has_photos,
               CASE WHEN article LIKE '%**Footnotes:**%' THEN 1 ELSE 0 END as has_footnotes
        FROM meetings
        WHERE article IS NOT NULL AND article != ''
        ORDER BY article_generated_at DESC LIMIT 10
    """).fetchall()
    article_quality["recent"] = [dict(r) for r in recent_articles]

    # Canonical names count
    try:
        import sys as _sys
        _sys.path.insert(0, os.path.join(os.path.dirname(__file__), "rag"))
        from enrich_transcript import _load_canonical_names
        all_names, _ = _load_canonical_names()
        article_quality["canonical_names"] = len(all_names)
    except Exception:
        article_quality["canonical_names"] = 0

    html = render_template("status.html",
        stats=stats, champds=champds, cron_jobs=cron_jobs, logs=logs,
        expected_meetings=expected_meetings, relay_actions=relay_actions,
        dep_checks=dep_checks, photo_pipeline=photo_pipeline,
        minutes_pipeline=minutes_pipeline, article_quality=article_quality)
    import time as _time
    _status_cache["html"] = html
    _status_cache["t"] = _time.time()
    return html




# -- Review Page -------------------------------------------------------

@app.route("/review")
@app.route("/review/<slug>")
@require_admin
def review_page(slug=None):
    """Editorial review page for draft articles before publication."""
    review_dir = os.path.join(os.path.dirname(__file__), "rag", "saved_articles")
    if not os.path.isdir(review_dir):
        return "No articles in review", 404

    review_files = sorted(
        [f for f in os.listdir(review_dir) if f.endswith("-verified.json")],
        reverse=True
    )

    if not slug:
        articles = []
        for fname in review_files:
            try:
                with open(os.path.join(review_dir, fname)) as f:
                    data = json.load(f)
                articles.append({
                    "slug": fname.replace(".json", ""),
                    "headline": data.get("headline", fname),
                    "model": data.get("model", "unknown"),
                    "generated_at": data.get("generated_at", ""),
                    "verification": data.get("verification", ""),
                })
            except Exception:
                pass

        return render_template_string(REVIEW_INDEX_HTML, articles=articles)

    fpath = os.path.join(review_dir, slug + ".json")
    if not os.path.exists(fpath):
        return "Article not found", 404

    with open(fpath) as f:
        data = json.load(f)

    article_md = data.get("article", "")
    headline = data.get("headline", "")

    # Markdown to HTML
    import re as _re
    h = article_md
    h = _re.sub(r'^#{3}\s+(.+)$', r'<h3>\1</h3>', h, flags=_re.MULTILINE)
    h = _re.sub(r'^#{2}\s+(.+)$', r'<h2>\1</h2>', h, flags=_re.MULTILINE)
    h = _re.sub(r'^#{1}\s+(.+)$', r'<h1>\1</h1>', h, flags=_re.MULTILINE)
    h = _re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', h)
    h = _re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<em>\1</em>', h)
    h = _re.sub(r'\n\n+', '</p>\n<p>', h)
    h = '<p>' + h + '</p>'
    # Convert (at MM:SS) timestamps to clickable video links
    def _ts_to_link(m):
        mm, ss = int(m.group(1)), int(m.group(2))
        secs = mm * 60 + ss
        vid = data.get('source_transcript', '').replace('transcript-yt-', '').replace('.json', '')
        if vid:
            return f'(<a href="https://www.youtube.com/watch?v={vid}&t={secs}s" target="_blank" style="color:var(--blue-link);font-size:0.82em;">at {mm}:{ss:02d}</a>)'
        return m.group(0)
    h = _re.sub(r'\(at (\d+):(\d+)\)', _ts_to_link, h)
    article_html = h

    meta_html = (
        f"Model: {data.get('model','')} | "
        f"Source: {data.get('source_transcript','')} | "
        f"Verification: {data.get('verification','')} | "
        f"Generated: {data.get('generated_at','')}"
    )

    return render_template_string(
        REVIEW_ARTICLE_HTML,
        headline=headline,
        meta=meta_html,
        article_html=article_html,
        video_url="https://www.youtube.com/watch?v=_3mId9iaSps",
        check=data.get("editorial_check"),
    )


REVIEW_INDEX_HTML = """
{% extends "base.html" %}
{% block title %}Editorial Review{% endblock %}
{% block content %}
<div style="max-width:800px;margin:2rem auto;padding:0 1rem;">
  <h1 style="font-family:var(--serif-display);">Editorial Review</h1>
  <p style="color:var(--ink-muted);margin-bottom:2rem;">Draft articles pending human review before publication.</p>
  {% for a in articles %}
  <div style="border:1px solid var(--rule);padding:1rem;margin-bottom:1rem;border-radius:4px;">
    <h3><a href="/review/{{ a.slug }}">{{ a.headline }}</a></h3>
    <p style="font-size:0.85rem;color:var(--ink-muted);">
      Model: {{ a.model }} | Generated: {{ a.generated_at }} | Verification: {{ a.verification }}
    </p>
  </div>
  {% endfor %}
  {% if not articles %}
  <p>No articles pending review.</p>
  {% endif %}
</div>
{% endblock %}
"""

REVIEW_ARTICLE_HTML = """
{% extends "base.html" %}
{% block title %}REVIEW: {{ headline }}{% endblock %}
{% block content %}
<div style="max-width:800px;margin:2rem auto;padding:0 1rem;">
  <div style="background:#fff3cd;border:1px solid #ffc107;padding:1rem;border-radius:4px;margin-bottom:2rem;">
    <strong>EDITORIAL REVIEW</strong> &mdash; This article has not been published.
    Every quote must be verified by a human editor before publication.
    <br><small>{{ meta }}</small>
  </div>

  <article style="font-family:var(--serif-body);line-height:1.8;">
    {{ article_html | safe }}
  </article>

  <div style="margin-top:3rem;padding:1rem;background:var(--paper-warm);border-radius:4px;">
    <h3>Editorial Verification Results</h3>
    {% if check %}
    <table style="width:100%;border-collapse:collapse;font-size:0.88rem;margin-bottom:1rem;">
      <tr><td style="padding:4px 8px;">Quotes verified</td><td><strong>{{ check.verified_quotes }}/{{ check.total_quotes }}</strong></td></tr>
      <tr><td style="padding:4px 8px;">Name issues</td><td><strong>{{ check.name_issues }}</strong></td></tr>
      <tr><td style="padding:4px 8px;">Facts checked</td><td><strong>{{ check.factual_claims_checked }}</strong></td></tr>
      <tr><td style="padding:4px 8px;">Status</td><td><strong>{{ check.status }}</strong></td></tr>
    </table>
    {% if check.failed_quote_timestamps %}
    <details><summary style="cursor:pointer;font-weight:600;">{{ check.failed_quotes }} quotes need fixing</summary>
    <ul style="font-size:0.82rem;margin-top:0.5rem;">
    {% for fq in check.failed_quote_timestamps %}
      <li><strong>[{{ fq.ts }}]</strong> {{ fq.issue }}</li>
    {% endfor %}
    </ul>
    </details>
    {% endif %}
    {% if check.notes %}<p style="font-size:0.82rem;color:var(--ink-secondary);margin-top:0.5rem;">{{ check.notes }}</p>{% endif %}
    {% else %}
    <p style="color:var(--ink-muted);">No editorial check has been run yet.</p>
    {% endif %}
    <h4 style="margin-top:1rem;">Manual Checklist</h4>
    <ul style="list-style:none;padding:0;">
      <li>&#9744; Every direct quote verified against source recording</li>
      <li>&#9744; Speaker attributions correct</li>
      <li>&#9744; Names spelled correctly</li>
      <li>&#9744; Dates and facts accurate</li>
      <li>&#9744; No fabricated content</li>
      <li>&#9744; Timestamps match quotes</li>
    </ul>
    <p><a href="{{ video_url }}" target="_blank">Source video</a></p>
  </div>
</div>
{% endblock %}
"""


@app.route("/llms-full.txt")
def llms_full_txt():
    db = get_rag_db()
    article_count = db.execute(
        "SELECT COUNT(*) FROM meetings WHERE article IS NOT NULL"
    ).fetchone()[0]
    meeting_count = db.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]
    committees = [r[0] for r in db.execute(
        "SELECT DISTINCT committee FROM meetings ORDER BY committee"
    ).fetchall()]
    recent = db.execute(
        "SELECT id, committee, date, headline, quick_summary FROM meetings "
        "WHERE article IS NOT NULL AND headline IS NOT NULL "
        "ORDER BY date DESC LIMIT 20"
    ).fetchall()
    try:
        topics = db.execute(
            "SELECT slug, name, COUNT(DISTINCT meeting_id) as cnt "
            "FROM topic_threads tt JOIN topic_mentions tm ON tt.id = tm.thread_id "
            "GROUP BY tt.id ORDER BY cnt DESC LIMIT 30"
        ).fetchall()
    except Exception:
        topics = []
    try:
        entity_count = db.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    except Exception:
        entity_count = 0

    lines = []
    lines.append("# croton.news \u2014 Croton-on-Hudson, NY Local News")
    lines.append("")
    lines.append("> Comprehensive LLM-readable documentation for croton.news")
    lines.append("> For the shorter version, see https://croton.news/llms.txt")
    lines.append("")
    lines.append("## About")
    lines.append("")
    lines.append("croton.news is an independent, AI-assisted civic information project covering the Village of")
    lines.append("Croton-on-Hudson, New York (population ~8,200, Westchester County, 35 miles north of Manhattan).")
    lines.append("The site publishes news articles and meeting coverage generated from public records, official")
    lines.append("meeting transcripts, and government documents using AI-assisted journalism workflows.")
    lines.append("")
    lines.append("- {} published articles generated from {} public meetings".format(article_count, meeting_count))
    lines.append("- {} tracked entities (people, organizations, places)".format(entity_count))
    lines.append("- {} government committees and boards covered".format(len(committees)))
    lines.append("- Coverage period: September 2024 to present")
    lines.append("- Updated daily as new meetings are transcribed and published")
    lines.append("")
    lines.append("## How It Works")
    lines.append("")
    lines.append("1. Public meetings are recorded and transcribed (YouTube recordings, audio files)")
    lines.append("2. Transcripts are chunked and indexed in a RAG (Retrieval-Augmented Generation) database")
    lines.append("3. AI generates long-form articles from transcript chunks, with citations to specific timestamps")
    lines.append("4. Articles include quick summaries, full narrative coverage, and links to source transcripts")
    lines.append("5. Named entities (people, organizations, places) are automatically extracted and cross-linked")
    lines.append("6. Topics are tracked across multiple meetings to show policy threads over time")
    lines.append("")
    lines.append("## Site Pages")
    lines.append("")
    lines.append("### Main Navigation")
    lines.append("- Homepage: https://croton.news/ \u2014 Latest articles, weather, upcoming meetings")
    lines.append("- Meetings: https://croton.news/meetings \u2014 All covered meetings by committee and date")
    lines.append("- Editorials: https://croton.news/editorials \u2014 AI-generated long-form investigative pieces")
    lines.append("- Calendar: https://croton.news/calendar \u2014 Upcoming village meetings and events")
    lines.append("- Topics: https://croton.news/topics \u2014 Policy topics tracked across meetings")
    lines.append("- Entities: https://croton.news/entities \u2014 People, organizations, and places mentioned in coverage")
    lines.append("- Documents: https://croton.news/documents \u2014 Meeting packets and source PDFs")
    lines.append("- Search: https://croton.news/search \u2014 Full-text search across all articles and transcripts")
    lines.append("- Gallery: https://croton.news/gallery \u2014 Community photo gallery")
    lines.append("- About: https://croton.news/about \u2014 Project description and methodology")
    lines.append("- Contact: https://croton.news/contact \u2014 Contact form")
    lines.append("- Editorial Policy: https://croton.news/editorial-policy \u2014 Transparency and sourcing standards")
    lines.append("")
    lines.append("### History Archive")
    lines.append("A curated historical section covering Croton-on-Hudson from pre-colonial times to the 20th century:")
    lines.append("- History Home: https://croton.news/history \u2014 Overview and featured stories")
    lines.append("- Timeline: https://croton.news/history/timeline \u2014 Interactive chronological timeline")
    lines.append("- Historical Maps: https://croton.news/history/maps \u2014 Annotated historical maps")
    lines.append("- Historical Documents: https://croton.news/history/documents \u2014 Primary source documents")
    lines.append("- Stories: https://croton.news/history/stories \u2014 27 narrative essays on local history")
    lines.append("- McDonald Interviews: https://croton.news/history/mcdonald \u2014 232 oral history transcripts")
    lines.append("- Search: https://croton.news/history/search \u2014 Search across all historical content")
    lines.append("")
    lines.append("### History Stories (27 essays)")
    stories = [
        ("01_cannon_tellers_point", "The Cannon at Teller's Point"),
        ("02_fifteen_year_revenge", "The Fifteen-Year Revenge"),
        ("03_nimham_last_stand", "Nimham's Last Stand"),
        ("04_grape_king_senasqua", "The Grape King of Senasqua"),
        ("05_little_italy_dam", "Little Italy and the Dam"),
        ("06_westchester_tea_party", "The Westchester Tea Party"),
        ("07_slavery_patriots_manor", "Slavery at the Patriots' Manor"),
        ("08_other_harmon", "The Other Harmon"),
        ("09_five_lives_croton_point", "Five Lives of Croton Point"),
        ("10_prohibitions_wild_croton", "Prohibition's Wild Croton"),
        ("11_croton_poems", "Croton in Poetry"),
        ("12_brinton_brook", "Brinton Brook Sanctuary"),
        ("13_croton_gorge_park", "Croton Gorge Park"),
        ("14_teatown", "Teatown Lake Reservation"),
        ("15_oscawana", "Oscawana Island"),
        ("16_blue_mountain", "Blue Mountain Reservation"),
        ("17_croton_landing", "Croton Landing Park"),
        ("18_georges_island", "George's Island Park"),
        ("19_aqueduct_trail", "Old Croton Aqueduct Trail"),
        ("20_original_research", "Original Research Collection"),
        ("21_tea_captain", "The Tea Captain"),
        ("22_pines_bridge", "The Sacrifice at Pines Bridge"),
        ("23_mosiers_fight", "Mosier's Fight"),
        ("24_crompond_burning", "The Burning of Crompond"),
        ("25_bearmore", "Bearmore the Bear"),
        ("26_tim_knapp", "Tim Knapp's Croton"),
        ("27_cornell_dam_strike", "The Cornell Dam Strike"),
    ]
    for slug, title in stories:
        lines.append("- {}: https://croton.news/history/story/{}".format(title, slug))
    lines.append("")
    lines.append("## Committees Covered")
    lines.append("")
    for c in committees:
        if c != "Topics":
            lines.append("- {}".format(c))
    lines.append("")
    lines.append("## Recent Articles")
    lines.append("")
    for r in recent:
        title = r["headline"] or "{} \u2014 {}".format(r["committee"], r["date"])
        summary = r["quick_summary"] or ""
        lines.append("### {}".format(title))
        lines.append("- URL: https://croton.news/article/{}".format(r["id"]))
        lines.append("- Committee: {}".format(r["committee"]))
        lines.append("- Date: {}".format(r["date"]))
        if summary:
            lines.append("- Summary: {}".format(summary))
        lines.append("")
    lines.append("## Topics")
    lines.append("")
    lines.append("Policy topics are tracked across multiple meetings to show how issues evolve over time.")
    lines.append("")
    if topics:
        for t in topics:
            lines.append("- {} ({} meetings): https://croton.news/topic/{}".format(t["name"], t["cnt"], t["slug"]))
    lines.append("")
    lines.append("## APIs")
    lines.append("")
    lines.append("### GET /api/articles")
    lines.append("Returns recent articles as JSON array.")
    lines.append("- Parameters:")
    lines.append("  - limit (int, default 20): Number of articles to return")
    lines.append("  - committee (string): Filter by committee slug (e.g., board-of-trustees)")
    lines.append("- Response: Array of objects with id, headline, quick_summary, committee, date, article (full markdown)")
    lines.append("- Example: GET https://croton.news/api/articles?limit=5")
    lines.append("")
    lines.append("### GET /api/search")
    lines.append("Full-text search across meeting transcripts.")
    lines.append("- Parameters:")
    lines.append("  - q (string, required): Search query")
    lines.append("- Response: Array of transcript chunks with content, speaker, timestamps, committee, date, doc_id")
    lines.append("- Example: GET https://croton.news/api/search?q=water+infrastructure")
    lines.append("")
    lines.append("### GET /api/calendar/events")
    lines.append("Returns upcoming village meetings and events.")
    lines.append("- Response: Array of event objects")
    lines.append("")
    lines.append("### GET /api/search/documents")
    lines.append("Search across meeting packet documents and PDFs.")
    lines.append("- Parameters:")
    lines.append("  - q (string, required): Search query")
    lines.append("- Response: Array of matching document sections")
    lines.append("")
    lines.append("### GET /api/weather")
    lines.append("Returns current weather for Croton-on-Hudson.")
    lines.append("")
    lines.append("### GET /api/health")
    lines.append("Service health check endpoint.")
    lines.append("- Response: JSON with status, article count, latest article date")
    lines.append("")
    lines.append("### GET /feed")
    lines.append("RSS 2.0 feed of recent articles.")
    lines.append("- Content-Type: application/rss+xml")
    lines.append("")
    lines.append("## Data & Feeds")
    lines.append("")
    lines.append("- RSS Feed: https://croton.news/feed")
    lines.append("- Sitemap: https://croton.news/sitemap.xml")
    lines.append("- News Sitemap: https://croton.news/news-sitemap.xml (Google News format)")
    lines.append("- Robots: https://croton.news/robots.txt")
    lines.append("- LLMs.txt: https://croton.news/llms.txt")
    lines.append("- LLMs-full.txt: https://croton.news/llms-full.txt")
    lines.append("")
    lines.append("## Article Structure")
    lines.append("")
    lines.append("Each article page includes:")
    lines.append("- Headline and quick summary (1-3 sentences)")
    lines.append("- Full narrative article (typically 1,000-5,000 words)")
    lines.append("- Committee name and meeting date")
    lines.append("- Links to related articles from the same committee")
    lines.append("- Entity mentions linked to entity profile pages")
    lines.append("- Topic tags linked to topic thread pages")
    lines.append("- Source transcript with timestamps (when available)")
    lines.append("- Meeting packet documents (when available)")
    lines.append("- NewsArticle JSON-LD structured data")
    lines.append("")
    lines.append("## Technical Details")
    lines.append("")
    lines.append("- Stack: Python/Flask, SQLite (RAG database), Jinja2 templates")
    lines.append("- Hosting: Self-hosted on VPS behind nginx reverse proxy with SSL")
    lines.append("- Search: SQLite FTS5 full-text search over transcript chunks")
    lines.append("- NLP: Named entity recognition for people, organizations, and places")
    lines.append("- AI: Article generation from transcripts using LLM pipelines")
    lines.append("- Design: Newspaper-style layout with serif typography, warm color palette")
    lines.append("")
    lines.append("## FAQ")
    lines.append("")
    lines.append("### Is croton.news affiliated with the Village of Croton-on-Hudson?")
    lines.append("No. croton.news is an independent project. All content is derived from publicly available records.")
    lines.append("")
    lines.append("### How are articles generated?")
    lines.append("Articles are generated by AI from official meeting transcripts. Each article cites specific")
    lines.append("timestamps and speakers from the source recording. Human editorial review ensures accuracy.")
    lines.append("")
    lines.append("### How often is the site updated?")
    lines.append("New articles are published within days of public meetings being held. The site covers meetings")
    lines.append("from the Board of Trustees, Planning Board, Zoning Board, and 20+ other committees.")
    lines.append("")
    lines.append("### Can I use the API?")
    lines.append("Yes. The articles API and search API are free and open. No authentication required.")
    lines.append("Please be respectful with request frequency.")
    lines.append("")
    lines.append("### What is the McDonald Interview collection?")
    lines.append("232 oral history transcripts collected by local historian Mary McDonald, documenting")
    lines.append("personal stories and memories of Croton-on-Hudson residents across decades.")
    lines.append("")
    lines.append("### What topics does the History section cover?")
    lines.append("27 narrative essays covering the American Revolution in Croton, the Croton Dam construction,")
    lines.append("Prohibition-era bootlegging, Native American history, local parks and landmarks,")
    lines.append("and lesser-known episodes in the village's history.")
    lines.append("")
    lines.append("## Related Sites")
    lines.append("")
    lines.append("croton.news is part of a network of free tools and services:")
    lines.append("- helloandy.net \u2014 Free developer tools (regex, cron, API tester, CLAUDE.md writer)")
    lines.append("- launch.pics \u2014 AI image processing API and browser tools (250+ tools)")
    lines.append("- everyone.food \u2014 Recipe collection, kitchen tools, and calorie API")
    lines.append("- contextwire.dev \u2014 Search API and MCP server for AI applications")
    lines.append("- qrmcp.dev \u2014 QR code generator with MCP integration")
    lines.append("- mcp.vin \u2014 VIN decoder and vehicle data API")
    lines.append("- webmcplist.com \u2014 WebMCP protocol directory")
    lines.append("- qrcode.host \u2014 AI microsite builder with QR codes")
    lines.append("- stockandflow.live \u2014 Systems dynamics simulator")
    lines.append("- stockandflow.org \u2014 Systems dynamics model library (1,000+ models)")

    return Response("\n".join(lines) + "\n", mimetype="text/plain")





@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3260))
    app.run(host="0.0.0.0", port=port, debug=False)

