"""Scraper for River Journal Online — local news covering Croton-on-Hudson.

Source: https://www.riverjournalonline.com
Covers: Tarrytown, Sleepy Hollow, Irvington, Ossining, Briarcliff Manor,
        Croton-on-Hudson, Cortlandt, and Peekskill.

WordPress site with standard RSS feed.
"""

import logging
import re
from bs4 import BeautifulSoup
from .base import BaseScraper

logger = logging.getLogger(__name__)


class RiverJournalScraper(BaseScraper):
    name = "riverjournal"
    category = "regional"
    source_url = "https://www.riverjournalonline.com"

    RSS_URLS = [
        "https://www.riverjournalonline.com/feed/",
        "https://www.riverjournalonline.com/?feed=rss2",
    ]

    GOOGLE_NEWS_RSS = (
        "https://news.google.com/rss/search?"
        "q=%22croton+on+hudson%22+OR+%22croton-on-hudson%22+OR+%22croton+harmon%22"
        "&hl=en-US&gl=US&ceid=US:en"
    )

    def _scrape(self) -> list[dict]:
        # Try RSS feeds first (WordPress standard)
        for rss_url in self.RSS_URLS:
            articles = self._scrape_rss(rss_url)
            if articles:
                return articles

        # Fallback to HTML
        articles = self._scrape_html()
        if articles:
            return articles

        # Last resort: Google News
        logger.info("[riverjournal] Direct site unavailable, using Google News fallback")
        return self._scrape_google_news()

    def _scrape_rss(self, url: str) -> list[dict]:
        xml = self.fetch(url)
        if not xml or '<rss' not in xml[:500]:
            return []

        soup = BeautifulSoup(xml, "lxml-xml")
        articles = []

        for item in soup.select("item")[:25]:
            title_el = item.select_one("title")
            if not title_el:
                continue

            title = title_el.get_text(strip=True)
            if not title:
                continue

            link_el = item.select_one("link")
            url = link_el.get_text(strip=True) if link_el else ""

            pubdate_el = item.select_one("pubDate")
            published = None
            if pubdate_el:
                published = self._parse_date(pubdate_el.get_text(strip=True))

            desc_el = item.select_one("description")
            summary = ""
            if desc_el:
                desc_soup = BeautifulSoup(desc_el.get_text(), "html.parser")
                summary = desc_soup.get_text(strip=True)[:500]

            # Include all articles (they cover Croton's region)
            articles.append({
                "title": title,
                "url": url,
                "summary": summary,
                "published_at": published,
                "source": "River Journal",
            })

        return articles

    def _scrape_html(self) -> list[dict]:
        html = self.fetch(self.source_url)
        if not html:
            return []

        soup = BeautifulSoup(html, "lxml")
        articles = []

        # WordPress article patterns
        items = soup.select(
            "article, .post, .entry, .hentry, "
            "[class*='post-'], [class*='article']"
        )

        seen = set()
        for item in items[:20]:
            title_el = item.select_one("h2 a, h3 a, .entry-title a, .post-title a")
            if not title_el:
                continue

            title = title_el.get_text(strip=True)
            if not title or len(title) < 10:
                continue

            href = title_el.get("href", "")
            if href in seen:
                continue
            seen.add(href)

            date_el = item.select_one("time, .entry-date, .post-date, [class*='date']")
            published = None
            if date_el:
                date_text = date_el.get("datetime") or date_el.get_text(strip=True)
                published = self._parse_date(date_text)

            desc_el = item.select_one(".entry-content, .entry-summary, .excerpt, p")
            summary = desc_el.get_text(strip=True)[:500] if desc_el else ""

            articles.append({
                "title": title,
                "url": href or self.source_url,
                "summary": summary,
                "published_at": published,
                "source": "River Journal",
            })

        return articles

    def _scrape_google_news(self) -> list[dict]:
        """Google News fallback for Croton-on-Hudson news."""
        xml = self.fetch(self.GOOGLE_NEWS_RSS)
        if not xml:
            return []

        soup = BeautifulSoup(xml, "lxml-xml")
        articles = []

        for item in soup.select("item")[:20]:
            title_el = item.select_one("title")
            if not title_el:
                continue

            title = title_el.get_text(strip=True)
            if not re.search(r"croton", title, re.IGNORECASE):
                desc_el = item.select_one("description")
                desc_text = desc_el.get_text(strip=True) if desc_el else ""
                if not re.search(r"croton", desc_text, re.IGNORECASE):
                    continue

            link_el = item.select_one("link")
            url = link_el.get_text(strip=True) if link_el else ""

            pubdate_el = item.select_one("pubDate")
            published = None
            if pubdate_el:
                published = self._parse_date(pubdate_el.get_text(strip=True))

            desc_el = item.select_one("description")
            summary = ""
            if desc_el:
                desc_soup = BeautifulSoup(desc_el.get_text(), "html.parser")
                summary = desc_soup.get_text(strip=True)[:500]

            articles.append({
                "title": title,
                "url": url,
                "summary": summary,
                "published_at": published,
                "source": "Google News",
            })

        return articles
