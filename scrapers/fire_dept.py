"""Scraper for Croton-on-Hudson Fire Department RSS feed."""

import logging
from .base import BaseScraper

logger = logging.getLogger(__name__)


class FireDeptScraper(BaseScraper):
    name = "fire"
    category = "fire"
    source_url = "http://crotonfd.org/apps/public/news/rss/"

    def _scrape(self) -> list[dict]:
        import feedparser

        html = self.fetch()
        if not html:
            return []

        feed = feedparser.parse(html)
        articles = []

        for entry in feed.entries[:30]:
            try:
                published = None
                if hasattr(entry, "published"):
                    published = self._parse_date(entry.published)
                elif hasattr(entry, "updated"):
                    published = self._parse_date(entry.updated)

                summary = ""
                if hasattr(entry, "summary"):
                    summary = entry.summary
                elif hasattr(entry, "description"):
                    summary = entry.description

                # Strip HTML from summary
                if "<" in summary:
                    from bs4 import BeautifulSoup
                    summary = BeautifulSoup(summary, "lxml").get_text(strip=True)

                articles.append({
                    "title": entry.get("title", "Fire Department Update"),
                    "url": entry.get("link", self.source_url),
                    "summary": summary[:500],
                    "published_at": published,
                })
            except Exception as e:
                logger.debug(f"[fire] Failed to parse entry: {e}")

        return articles
