"""Scraper for Village of Croton-on-Hudson official news (CivicPlus CMS).

The village site at crotononhudson-ny.gov is behind Cloudflare Turnstile,
which blocks automated access. This scraper tries the direct site first,
then falls back to Google News RSS as an alternative source.

Known URL patterns:
  - News listing: /news
  - Individual articles: /home/news/{slug}
  - Police dept news: /node/229/news
  - Dept-specific: /node/{id}/news
"""

import logging
import re
from bs4 import BeautifulSoup
from .base import BaseScraper

logger = logging.getLogger(__name__)

SITE_BASE = "https://www.crotononhudson-ny.gov"


class VillageNewsScraper(BaseScraper):
    name = "village"
    category = "municipal"
    source_url = f"{SITE_BASE}/news"

    # Google News RSS feed for Croton-on-Hudson village news
    GOOGLE_NEWS_RSS = (
        "https://news.google.com/rss/search?"
        "q=%22croton+on+hudson%22+village+OR+municipal+OR+board+OR+meeting"
        "&hl=en-US&gl=US&ceid=US:en"
    )

    def _scrape(self) -> list[dict]:
        # Try direct site first
        html = self.fetch()
        if html:
            articles = self._parse_civicplus(html)
            if articles:
                return articles

        # Fallback: Google News RSS
        logger.info("[village] Direct site unavailable, using Google News RSS fallback")
        return self._scrape_google_news()

    def _parse_civicplus(self, html: str) -> list[dict]:
        """Parse the CivicPlus news listing page."""
        soup = BeautifulSoup(html, "lxml")
        articles = []

        # CivicPlus uses various patterns depending on version:
        # - .views-row (Drupal Views)
        # - .news-flash-item, .nf-item (News Flash widget)
        # - .node--type-news (Drupal node)
        # - Simple <li> or <article> lists
        rows = soup.select(
            ".view-content .views-row, "
            ".news-flash-item, .nf-item, "
            ".node--type-news, "
            "article.node, "
            ".view-content .node, "
            ".views-table tbody tr, "
            "#block-system-main .content li, "
            "main article, main .content li"
        )

        if not rows:
            # Broader fallback for restructured pages
            rows = soup.select(
                "main a[href*='/news/'], "
                ".region-content a[href*='/news/'], "
                "#block-system-main a[href*='/news/']"
            )

        for row in rows[:30]:
            try:
                article = self._parse_row(row)
                if article and article.get("title"):
                    articles.append(article)
            except Exception as e:
                logger.debug(f"[village] Failed to parse row: {e}")
                continue

        return articles

    def _parse_row(self, row) -> dict:
        """Parse a single news item from the CivicPlus page."""
        # If the row IS a link (from the fallback selector)
        if row.name == "a":
            title = row.get_text(strip=True)
            href = row.get("href", "")
            if href and not href.startswith("http"):
                href = f"{SITE_BASE}{href}"
            if title and len(title) >= 5:
                return {"title": title, "url": href, "summary": "", "published_at": None}
            return {}

        # Try structured CivicPlus layout
        title_el = (
            row.select_one(
                "h2 a, h3 a, h4 a, "
                ".views-field-title a, .field-title a, "
                ".nf-title a, td a"
            )
            or row.select_one("a")
        )
        if not title_el:
            return {}

        title = title_el.get_text(strip=True)
        if not title or len(title) < 5:
            return {}

        href = title_el.get("href", "")
        if href and not href.startswith("http"):
            href = f"{SITE_BASE}{href}"

        # Date — try multiple CivicPlus patterns
        date_el = row.select_one(
            ".views-field-created, .field-created, .date-display-single, "
            ".views-field-field-date, time, .nf-date, .news-date, "
            "span.itemdate, [class*='date']"
        )
        published = None
        if date_el:
            date_text = date_el.get("datetime") or date_el.get_text(strip=True)
            published = self._parse_date(date_text)

        # Summary
        summary_el = row.select_one(
            ".views-field-body, .field-summary, .views-field-field-summary, "
            ".views-field-nothing, .nf-body, p"
        )
        summary = summary_el.get_text(strip=True) if summary_el else ""

        return {
            "title": title,
            "url": href,
            "summary": summary[:500],
            "published_at": published,
        }

    def _scrape_google_news(self) -> list[dict]:
        """Fallback: scrape Google News RSS for Croton-on-Hudson news."""
        xml = self.fetch(self.GOOGLE_NEWS_RSS)
        if not xml:
            return []

        soup = BeautifulSoup(xml, "lxml-xml")
        articles = []

        for item in soup.select("item")[:20]:
            title_el = item.select_one("title")
            link_el = item.select_one("link")
            pubdate_el = item.select_one("pubDate")
            desc_el = item.select_one("description")

            if not title_el:
                continue

            title = title_el.get_text(strip=True)
            # Skip items that aren't really about Croton-on-Hudson
            if not re.search(r"croton", title, re.IGNORECASE):
                # Check description too
                desc_text = desc_el.get_text(strip=True) if desc_el else ""
                if not re.search(r"croton", desc_text, re.IGNORECASE):
                    continue

            url = link_el.get_text(strip=True) if link_el else ""
            published = None
            if pubdate_el:
                published = self._parse_date(pubdate_el.get_text(strip=True))

            summary = ""
            if desc_el:
                # Clean HTML from description
                desc_soup = BeautifulSoup(desc_el.get_text(), "html.parser")
                summary = desc_soup.get_text(strip=True)[:500]

            articles.append({
                "title": title,
                "url": url,
                "summary": summary,
                "published_at": published,
            })

        return articles
