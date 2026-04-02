"""Scraper for Village of Croton-on-Hudson official news (CivicPlus CMS)."""

import logging
from bs4 import BeautifulSoup
from .base import BaseScraper

logger = logging.getLogger(__name__)


class VillageNewsScraper(BaseScraper):
    name = "village"
    category = "municipal"
    source_url = "https://www.crotononhudson-ny.gov/node/all/news"

    def _scrape(self) -> list[dict]:
        html = self.fetch()
        if not html:
            return []

        soup = BeautifulSoup(html, "lxml")
        articles = []

        # CivicPlus CMS typically uses .views-row or .node--type-news
        rows = soup.select(".view-content .views-row, .view-content .node, .views-table tbody tr")
        if not rows:
            # Fallback: look for any list of links in main content
            rows = soup.select("main a, .region-content a, #block-system-main a")

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
        # Try structured CivicPlus layout
        title_el = (
            row.select_one("h2 a, h3 a, .views-field-title a, .field-title a, td a")
            or row.select_one("a")
        )
        if not title_el:
            return {}

        title = title_el.get_text(strip=True)
        if not title or len(title) < 5:
            return {}

        href = title_el.get("href", "")
        if href and not href.startswith("http"):
            href = f"https://www.crotononhudson-ny.gov{href}"

        # Date
        date_el = row.select_one(
            ".views-field-created, .field-created, .date-display-single, "
            ".views-field-field-date, time"
        )
        published = None
        if date_el:
            date_text = date_el.get("datetime") or date_el.get_text(strip=True)
            published = self._parse_date(date_text)

        # Summary
        summary_el = row.select_one(
            ".views-field-body, .field-summary, .views-field-field-summary, "
            ".views-field-nothing"
        )
        summary = summary_el.get_text(strip=True) if summary_el else ""

        return {
            "title": title,
            "url": href,
            "summary": summary[:500],
            "published_at": published,
        }
