"""Croton News — Hyperlocal news aggregator for Croton-on-Hudson, NY."""

import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import (
    Flask, Response, abort, g, jsonify, render_template, request, send_from_directory,
)

from scrapers import ALL_SCRAPERS

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data" / "croton.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# Tiered scrape intervals (seconds)
SCRAPE_TIERS = {
    "fast": 300,     # 5 min — weather, transit
    "medium": 3600,  # 1 hr — news, police, fire, events
    "slow": 21600,   # 6 hr — schools, regional, library
}
SCRAPER_TIER = {
    "weather": "fast",
    "transit": "fast",
    "water": "fast",
    "emergency": "fast",
    "village": "medium",
    "police": "medium",
    "fire": "medium",
    "tides": "fast",
    "riverjournal": "medium",
    "boards": "slow",
    "schools": "slow",
    "cortlandt": "slow",
    "library": "slow",
}
TICK_INTERVAL = 60  # check every 60s
CATEGORIES = {
    "municipal": {"label": "Village News", "icon": "🏛️", "color": "#2563eb"},
    "police": {"label": "Police Blotter", "icon": "🚔", "color": "#dc2626"},
    "fire": {"label": "Fire Department", "icon": "🚒", "color": "#ea580c"},
    "schools": {"label": "Schools", "icon": "🎓", "color": "#16a34a"},
    "regional": {"label": "Regional", "icon": "🗺️", "color": "#7c3aed"},
    "weather": {"label": "Weather", "icon": "🌤️", "color": "#0891b2"},
    "transit": {"label": "Transit", "icon": "🚂", "color": "#ca8a04"},
    "events": {"label": "Events", "icon": "📅", "color": "#9333ea"},
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("croton-news")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(str(DB_PATH))
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db


def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS articles (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            summary TEXT DEFAULT '',
            content TEXT DEFAULT '',
            source TEXT NOT NULL,
            category TEXT NOT NULL,
            url TEXT DEFAULT '',
            published_at TEXT,
            scraped_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_articles_category ON articles(category);
        CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_at DESC);
        CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source);
    """)
    conn.close()


def upsert_articles(articles: list[dict]):
    conn = sqlite3.connect(str(DB_PATH))
    try:
        for art in articles:
            conn.execute("""
                INSERT INTO articles (id, title, summary, content, source, category, url, published_at, scraped_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    summary=excluded.summary,
                    source=excluded.source,
                    category=excluded.category,
                    url=excluded.url,
                    published_at=COALESCE(excluded.published_at, articles.published_at),
                    scraped_at=excluded.scraped_at
            """, (
                art["id"],
                art["title"],
                art.get("summary", ""),
                art.get("content", ""),
                art["source"],
                art["category"],
                art.get("url", ""),
                art.get("published_at"),
                art.get("scraped_at", datetime.now(timezone.utc).isoformat()),
            ))
        conn.commit()
    finally:
        conn.close()


def query_articles(category=None, search=None, limit=50, offset=0) -> list[dict]:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    clauses = []
    params = []

    if category:
        clauses.append("category = ?")
        params.append(category)
    if search:
        clauses.append("(title LIKE ? OR summary LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM articles {where} ORDER BY "
        f"COALESCE(published_at, scraped_at) DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_article(article_id: str) -> dict | None:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def count_articles(category=None) -> int:
    conn = sqlite3.connect(str(DB_PATH))
    clause = "WHERE category = ?" if category else ""
    params = [category] if category else []
    count = conn.execute(
        f"SELECT COUNT(*) FROM articles {clause}", params
    ).fetchone()[0]
    conn.close()
    return count


def category_counts() -> dict:
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        "SELECT category, COUNT(*) as cnt FROM articles GROUP BY category"
    ).fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows}


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

scraper_instances = [cls() for cls in ALL_SCRAPERS]
_last_scrape: dict[str, float] = {}  # scraper_name -> last run timestamp


def run_scrapers(force_all: bool = False):
    """Run scrapers that are due based on their tier interval."""
    now = time.time()
    total = 0
    for scraper in scraper_instances:
        tier = SCRAPER_TIER.get(scraper.name, "medium")
        interval = SCRAPE_TIERS[tier]
        last = _last_scrape.get(scraper.name, 0)
        if not force_all and (now - last) < interval:
            continue
        try:
            articles = scraper.scrape()
            if articles:
                upsert_articles(articles)
                total += len(articles)
            _last_scrape[scraper.name] = now
        except Exception as e:
            logger.error(f"Scraper {scraper.name} failed: {e}")
    if total:
        logger.info(f"Scrape tick — {total} articles processed")


def scrape_loop():
    """Background thread that scrapes on tiered intervals."""
    # First run: scrape everything
    logger.info("Initial scrape — all sources...")
    run_scrapers(force_all=True)
    logger.info("Initial scrape complete")
    while True:
        try:
            run_scrapers()
        except Exception as e:
            logger.error(f"Scrape loop error: {e}")
        time.sleep(TICK_INTERVAL)


# ---------------------------------------------------------------------------
# Flask App
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 3600


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _ctx():
    """Common template context."""
    return {
        "categories": CATEGORIES,
        "cat_counts": category_counts(),
        "total_count": count_articles(),
        "now": datetime.now(timezone.utc),
    }


# --- HTML Routes ---

@app.route("/")
def index():
    search = request.args.get("q", "").strip()
    articles = query_articles(search=search if search else None, limit=40)
    return render_template("index.html", articles=articles, search=search, **_ctx())


@app.route("/documents")
def documents_page():
    """Municipal documents search page."""
    q = request.args.get("q", "").strip()
    results = []
    if q and ECODE360_DB.exists():
        conn = sqlite3.connect(str(ECODE360_DB))
        c = conn.cursor()
        c.execute(
            "SELECT doc_id, committee, date, snippet(chunks, 4, '<b>', '</b>', '…', 40), rank "
            "FROM chunks WHERE chunks MATCH ? ORDER BY rank LIMIT 30",
            (q,),
        )
        results = [
            {"doc_id": r[0], "committee": r[1], "date": r[2], "snippet": r[3], "score": round(-r[4], 2)}
            for r in c.fetchall()
        ]
        conn.close()
    return render_template("documents.html", query=q, results=results, **_ctx())


@app.route("/category/<name>")
def category_page(name):
    if name not in CATEGORIES:
        abort(404)
    articles = query_articles(category=name, limit=40)
    return render_template(
        "category.html", articles=articles,
        current_category=name, **_ctx(),
    )


@app.route("/article/<article_id>")
def article_page(article_id):
    article = get_article(article_id)
    if not article:
        abort(404)
    return render_template("article.html", article=article, **_ctx())


# --- API Routes ---

@app.route("/api/articles")
def api_articles():
    category = request.args.get("category")
    search = request.args.get("q")
    limit = min(int(request.args.get("limit", 50)), 200)
    offset = int(request.args.get("offset", 0))
    articles = query_articles(category=category, search=search, limit=limit, offset=offset)
    return jsonify({"articles": articles, "count": len(articles)})


# --- Document Search (ecode360 minutes/resolutions) ---

ECODE360_DB = BASE_DIR / "ecode360" / "search.db"


@app.route("/api/search/documents")
def api_search_documents():
    """Full-text search across municipal meeting minutes and resolutions."""
    q = request.args.get("q", "").strip()
    committee = request.args.get("committee")
    limit = min(int(request.args.get("limit", 20)), 100)
    if not q:
        return jsonify({"error": "Missing 'q' parameter"}), 400
    if not ECODE360_DB.exists():
        return jsonify({"error": "Search index not built yet"}), 503
    conn = sqlite3.connect(str(ECODE360_DB))
    c = conn.cursor()
    try:
        if committee:
            c.execute(
                "SELECT doc_id, committee, date, snippet(chunks, 4, '<b>', '</b>', '…', 40), rank "
                "FROM chunks WHERE chunks MATCH ? AND committee = ? ORDER BY rank LIMIT ?",
                (q, committee, limit),
            )
        else:
            c.execute(
                "SELECT doc_id, committee, date, snippet(chunks, 4, '<b>', '</b>', '…', 40), rank "
                "FROM chunks WHERE chunks MATCH ? ORDER BY rank LIMIT ?",
                (q, limit),
            )
        results = [
            {"doc_id": r[0], "committee": r[1], "date": r[2], "snippet": r[3], "score": round(-r[4], 2)}
            for r in c.fetchall()
        ]
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 400
    conn.close()
    return jsonify({"query": q, "results": results, "count": len(results)})


@app.route("/api/documents")
def api_documents():
    """List all indexed municipal documents."""
    if not ECODE360_DB.exists():
        return jsonify({"error": "Search index not built yet"}), 503
    conn = sqlite3.connect(str(ECODE360_DB))
    c = conn.cursor()
    c.execute("SELECT doc_id, committee, date, type, text_size, preview FROM documents ORDER BY committee, date")
    docs = [
        {"doc_id": r[0], "committee": r[1], "date": r[2], "type": r[3], "text_size": r[4], "preview": r[5]}
        for r in c.fetchall()
    ]
    conn.close()
    return jsonify({"documents": docs, "count": len(docs)})


@app.route("/api/health")
def api_health():
    return jsonify({
        "status": "ok",
        "total_articles": count_articles(),
        "categories": category_counts(),
        "last_scrape": {
            name: datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            for name, ts in _last_scrape.items()
        } if _last_scrape else None,
    })


# --- RSS Feed ---

@app.route("/feed")
def rss_feed():
    articles = query_articles(limit=30)
    xml = render_template("feed.xml", articles=articles)
    return Response(xml, mimetype="application/rss+xml")


# --- SEO ---

@app.route("/robots.txt")
def robots():
    return Response(
        "User-agent: *\nAllow: /\nSitemap: https://croton.news/sitemap.xml\n",
        mimetype="text/plain",
    )


@app.route("/sitemap.xml")
def sitemap():
    articles = query_articles(limit=500)
    xml = ['<?xml version="1.0" encoding="UTF-8"?>']
    xml.append('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')
    xml.append(f'  <url><loc>https://croton.news/</loc><changefreq>hourly</changefreq><priority>1.0</priority></url>')
    for cat in CATEGORIES:
        xml.append(f'  <url><loc>https://croton.news/category/{cat}</loc><changefreq>hourly</changefreq><priority>0.8</priority></url>')
    for art in articles:
        xml.append(f'  <url><loc>https://croton.news/article/{art["id"]}</loc><priority>0.6</priority></url>')
    xml.append('</urlset>')
    return Response("\n".join(xml), mimetype="application/xml")


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------



# --- Calendar ---

@app.route("/calendar")
def calendar_page():
    return send_from_directory(app.template_folder, "calendar.html")

@app.route("/api/calendar/events")
def api_calendar_events():
    return send_from_directory(app.static_folder, "events.json")

@app.route("/feeds/<path:filename>")
def feeds(filename):
    mimetype = "text/calendar" if filename.endswith(".ics") else "application/json"
    return send_from_directory(os.path.join(app.static_folder, "feeds"), filename, mimetype=mimetype)

if __name__ == "__main__":
    init_db()
    # Run initial scrape in background
    t = threading.Thread(target=scrape_loop, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 3260))
    app.run(host="0.0.0.0", port=port, debug=False)
