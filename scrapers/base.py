"""Base scraper class with common methods, caching, and error handling."""

import hashlib
import logging
import subprocess
import time
from datetime import datetime, timezone
from typing import Optional

import requests

try:
    import cloudscraper
except ImportError:
    cloudscraper = None

logger = logging.getLogger(__name__)


class BaseScraper:
    """Base class for all news scrapers."""

    name: str = "base"
    category: str = "general"
    source_url: str = ""
    cache_ttl: int = 1800  # 30 minutes

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

    def __init__(self):
        self._cache: dict = {}
        self._cache_time: float = 0
        # Try cloudscraper first (handles some Cloudflare challenges)
        if cloudscraper:
            self.session = cloudscraper.create_scraper()
        else:
            self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        })

    @staticmethod
    def _is_cloudflare_challenge(html: str) -> bool:
        """Detect if the response is a Cloudflare challenge page."""
        if not html:
            return False
        return (
            "Just a moment..." in html[:500]
            and "challenge-platform" in html
        ) or (
            "Enable JavaScript and cookies" in html
            and "_cf_chl" in html
        )

    def fetch(self, url: Optional[str] = None, timeout: int = 15) -> Optional[str]:
        """Fetch a URL with error handling, Cloudflare bypass, and timeouts."""
        url = url or self.source_url
        try:
            resp = self.session.get(url, timeout=timeout)
            resp.raise_for_status()
            html = resp.text
            if self._is_cloudflare_challenge(html):
                logger.warning(f"[{self.name}] Cloudflare challenge at {url}, trying browser fallback")
                html = self._fetch_via_browser(url)
            return html
        except requests.RequestException as e:
            logger.error(f"[{self.name}] Failed to fetch {url}: {e}")
            # On 403, try browser fallback
            if hasattr(e, "response") and e.response is not None and e.response.status_code == 403:
                logger.info(f"[{self.name}] Trying browser fallback for {url}")
                return self._fetch_via_browser(url)
            return None

    def _fetch_via_browser(self, url: str) -> Optional[str]:
        """Attempt to fetch via agent-browser CLI (handles JS challenges)."""
        try:
            # Open URL
            subprocess.run(
                ["agent-browser", "open", url],
                capture_output=True, text=True, timeout=15,
            )
            # Wait for page to potentially load past challenge
            time.sleep(8)
            # Try to get HTML
            result = subprocess.run(
                ["agent-browser", "get", "html", "body"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout:
                html = result.stdout
                if not self._is_cloudflare_challenge(html):
                    return html
            logger.warning(f"[{self.name}] Browser fallback also blocked by Cloudflare")
            return None
        except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
            logger.debug(f"[{self.name}] Browser fallback failed: {e}")
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
