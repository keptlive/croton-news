"""Base scraper class with common methods, caching, and error handling."""

import hashlib
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class BaseScraper:
    """Base class for all news scrapers."""

    name: str = "base"
    category: str = "general"
    source_url: str = ""
    cache_ttl: int = 1800  # 30 minutes

    USER_AGENT = (
        "CrotonNewsBot/1.0 (+https://croton.news; "
        "local-news-aggregator; contact@croton.news)"
    )

    def __init__(self):
        self._cache: dict = {}
        self._cache_time: float = 0
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        })

    def fetch(self, url: Optional[str] = None, timeout: int = 15) -> Optional[str]:
        """Fetch a URL with error handling and timeouts."""
        url = url or self.source_url
        try:
            resp = self.session.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            logger.error(f"[{self.name}] Failed to fetch {url}: {e}")
            return None

    def scrape(self) -> list[dict]:
        """Run the scraper with caching. Returns list of article dicts."""
        now = time.time()
        if self._cache and (now - self._cache_time) < self.cache_ttl:
            logger.debug(f"[{self.name}] Returning cached results")
            return self._cache.get("articles", [])

        try:
            articles = self._scrape()
            # Normalize articles
            for art in articles:
                art.setdefault("source", self.name)
                art.setdefault("category", self.category)
                art.setdefault("scraped_at", datetime.now(timezone.utc).isoformat())
                if not art.get("id"):
                    art["id"] = self._make_id(art.get("url", "") or art.get("title", ""))
            self._cache = {"articles": articles}
            self._cache_time = now
            logger.info(f"[{self.name}] Scraped {len(articles)} articles")
            return articles
        except Exception as e:
            logger.error(f"[{self.name}] Scrape failed: {e}", exc_info=True)
            return self._cache.get("articles", [])

    def _scrape(self) -> list[dict]:
        """Override in subclasses. Return list of article dicts."""
        raise NotImplementedError

    @staticmethod
    def _make_id(text: str) -> str:
        return hashlib.md5(text.encode()).hexdigest()[:12]

    @staticmethod
    def _parse_date(date_str: str) -> Optional[str]:
        """Try multiple date formats and return ISO string."""
        formats = [
            "%B %d, %Y",
            "%b %d, %Y",
            "%Y-%m-%d",
            "%m/%d/%Y",
            "%m/%d/%y",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%SZ",
            "%a, %d %b %Y %H:%M:%S %z",
            "%a, %d %b %Y %H:%M:%S %Z",
        ]
        date_str = date_str.strip()
        for fmt in formats:
            try:
                dt = datetime.strptime(date_str, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.isoformat()
            except ValueError:
                continue
        return None
