"""Scraper for Village of Croton-on-Hudson board meeting agendas.

Sources:
- Village Board agendas/minutes
- Planning Board
- Zoning Board of Appeals

The village uses CivicPlus which blocks bots, so we fall back to
Google search for recent agendas and meeting notices.
"""

import logging
import re
from bs4 import BeautifulSoup
from .base import BaseScraper

logger = logging.getLogger(__name__)

SITE_BASE = "https://www.crotononhudson-ny.gov"


class BoardAgendasScraper(BaseScraper):
    name = "boards"
    category = "municipal"

    # CivicPlus agenda pages (may 403)
    AGENDA_URLS = [
        f"{SITE_BASE}/AgendaCenter",
        f"{SITE_BASE}/AgendaCenter/Village-Board-1",
        f"{SITE_BASE}/AgendaCenter/Planning-Board-2",
        f"{SITE_BASE}/AgendaCenter/Zoning-Board-of-Appeals-3",
    ]

    # Google News RSS fallback for board meetings
    GOOGLE_NEWS_RSS = (
        "https://news.google.com/rss/search?"
        "q=%22croton+on+hudson%22+board+OR+agenda+OR+meeting+OR+zoning+OR+planning"
        "&hl=en-US&gl=US&ceid=US:en"
    )

    BOARDS = {
        "Village-Board-1": "Village Board",
        "Planning-Board-2": "Planning Board",
        "Zoning-Board-of-Appeals-3": "Zoning Board of Appeals",
    }

    def _scrape(self) -> list[dict]:
        articles = []

        # Try direct CivicPlus agenda center
        for url in self.AGENDA_URLS:
            html = self.fetch(url)
            if html:
                parsed = self._parse_agenda_center(html, url)
                if parsed:
                    articles.extend(parsed)

        if articles:
            return articles

        # Fallback: Google News RSS
        logger.info("[boards] Direct site unavailable, using Google News RSS")
        return self._scrape_google_news()

    def _parse_agenda_center(self, html: str, url: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        articles = []

        # CivicPlus AgendaCenter patterns
        rows = soup.select(
            ".agenda-item, .agendaRow, tr.catAgendaRow, "
            ".views-row, .meeting-item, [class*='agenda']"
        )

        for row in rows[:20]:
            title_el = row.select_one("a, .title, td:first-child")
            if not title_el:
                continue

            title = title_el.get_text(strip=True)
            if not title or len(title) < 3:
                continue

            href = ""
            link = title_el if title_el.name == "a" else row.select_one("a[href]")
            if link and link.get("href"):
                href = link["href"]
                if not href.startswith("http"):
                    href = f"{SITE_BASE}{href}"

            date_el = row.select_one("time, .date, [class*='date'], td:nth-child(2)")
            published = None
            if date_el:
                date_text = date_el.get("datetime") or date_el.get_text(strip=True)
                published = self._parse_date(date_text)

            # Determine which board
            board = "Board Meeting"
            for key, name in self.BOARDS.items():
                if key in url or name.lower() in title.lower():
                    board = name
                    break

            articles.append({
                "title": f"🏛️ {board}: {title}",
                "url": href or url,
                "summary": f"Agenda/minutes from {board}.",
                "published_at": published,
            })

        return articles

    def _scrape_google_news(self) -> list[dict]:
        xml = self.fetch(self.GOOGLE_NEWS_RSS)
        if not xml:
            return []

        soup = BeautifulSoup(xml, "lxml-xml")
        articles = []

        for item in soup.select("item")[:15]:
            title_el = item.select_one("title")
            link_el = item.select_one("link")
            pubdate_el = item.select_one("pubDate")
            desc_el = item.select_one("description")

            if not title_el:
                continue

            title = title_el.get_text(strip=True)
            # Must be relevant to Croton boards/meetings
            if not re.search(r"croton", title, re.IGNORECASE):
                desc_text = desc_el.get_text(strip=True) if desc_el else ""
                if not re.search(r"croton", desc_text, re.IGNORECASE):
                    continue

            url = link_el.get_text(strip=True) if link_el else ""
            published = None
            if pubdate_el:
                published = self._parse_date(pubdate_el.get_text(strip=True))

            summary = ""
            if desc_el:
                desc_soup = BeautifulSoup(desc_el.get_text(), "html.parser")
                summary = desc_soup.get_text(strip=True)[:500]

            articles.append({
                "title": title,
                "url": url,
                "summary": summary,
                "published_at": published,
            })

        return articles
