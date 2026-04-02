"""Scraper for Croton-on-Hudson Police Department blotter.

The police department news page at /node/229/news is behind Cloudflare
Turnstile. This scraper tries the direct site first, then falls back to
Google News RSS for Croton-on-Hudson police news.
"""

import logging
import re
from bs4 import BeautifulSoup
from .base import BaseScraper

logger = logging.getLogger(__name__)

SITE_BASE = "https://www.crotononhudson-ny.gov"

# Common incident type keywords
INCIDENT_TYPES = [
    "arrest", "burglary", "theft", "larceny", "robbery", "assault",
    "accident", "collision", "DWI", "DUI", "disturbance", "noise complaint",
    "trespass", "vandalism", "suspicious", "domestic", "fraud", "harassment",
    "missing person", "fire", "medical", "alarm", "dispute", "traffic stop",
    "welfare check", "parking", "property damage",
]

# Street names common in Croton-on-Hudson
STREET_PATTERN = re.compile(
    r"\b\d*\s*(?:South|North|East|West|S\.|N\.|E\.|W\.)?\s*"
    r"(?:[A-Z][a-z]+(?:\s[A-Z][a-z]+)*)\s+"
    r"(?:Street|St\.?|Avenue|Ave\.?|Road|Rd\.?|Drive|Dr\.?|Boulevard|Blvd\.?|"
    r"Lane|Ln\.?|Place|Pl\.?|Court|Ct\.?|Way|Circle|Terrace|Ter\.?|Trail|"
    r"Route \d+|Rt\.? \d+)\b",
    re.IGNORECASE,
)


class PoliceBlotterScraper(BaseScraper):
    name = "police"
    category = "police"
    source_url = f"{SITE_BASE}/node/229/news"

    # Google News RSS for Croton-on-Hudson police news
    GOOGLE_NEWS_RSS = (
        "https://news.google.com/rss/search?"
        "q=%22croton+on+hudson%22+police+OR+arrest+OR+crime+OR+blotter"
        "&hl=en-US&gl=US&ceid=US:en"
    )

    def _scrape(self) -> list[dict]:
        # Try direct site first
        html = self.fetch()
        if html:
            articles = self._parse_site(html)
            if articles:
                return articles

        # Fallback: Google News RSS for police-related news
        logger.info("[police] Direct site unavailable, using Google News RSS fallback")
        return self._scrape_google_news()

    def _parse_site(self, html: str) -> list[dict]:
        """Parse the CivicPlus police news page."""
        soup = BeautifulSoup(html, "lxml")
        articles = []

        # Look for news items — multiple CivicPlus patterns
        rows = soup.select(
            ".view-content .views-row, .view-content .node, "
            ".views-table tbody tr, .field-content, "
            ".news-flash-item, .nf-item, "
            "article.node, main .content li, "
            "main article, #block-system-main .content li"
        )
        if not rows:
            rows = soup.select(
                "main .field-item, .region-content .field-item, "
                "main a[href*='/news/']"
            )

        # If still no rows, try parsing the page as unstructured narrative
        if not rows:
            main = soup.select_one("main, .region-content, #block-system-main")
            if main:
                articles.extend(self._parse_narrative(main.get_text()))
                return articles

        for row in rows[:30]:
            try:
                article = self._parse_row(row)
                if article and article.get("title"):
                    articles.append(article)
            except Exception as e:
                logger.debug(f"[police] Failed to parse row: {e}")

        return articles

    def _parse_row(self, row) -> dict:
        # If the row IS a link
        if row.name == "a":
            title = row.get_text(strip=True)
            href = row.get("href", "")
            if href and not href.startswith("http"):
                href = f"{SITE_BASE}{href}"
            if title and len(title) >= 5:
                return {"title": title, "url": href, "summary": "", "published_at": None}
            return {}

        title_el = row.select_one(
            "h2 a, h3 a, h4 a, .views-field-title a, "
            ".nf-title a, td a, a"
        )
        if not title_el:
            # Maybe the row itself contains narrative text
            text = row.get_text(strip=True)
            if len(text) > 30:
                return self._make_incident(text)
            return {}

        title = title_el.get_text(strip=True)
        if not title or len(title) < 5:
            return {}

        href = title_el.get("href", "")
        if href and not href.startswith("http"):
            href = f"{SITE_BASE}{href}"

        date_el = row.select_one(
            ".views-field-created, .date-display-single, time, "
            ".nf-date, .news-date, span.itemdate, [class*='date']"
        )
        published = None
        if date_el:
            date_text = date_el.get("datetime") or date_el.get_text(strip=True)
            published = self._parse_date(date_text)

        summary_el = row.select_one(
            ".views-field-body, .field-summary, .nf-body, p"
        )
        summary = summary_el.get_text(strip=True) if summary_el else ""

        return {
            "title": title,
            "url": href,
            "summary": summary[:500],
            "published_at": published,
        }

    def _parse_narrative(self, text: str) -> list[dict]:
        """Extract incident reports from unstructured narrative text."""
        articles = []
        paragraphs = re.split(r"\n{2,}|\r\n{2,}", text)

        for para in paragraphs:
            para = para.strip()
            if len(para) < 30:
                continue
            article = self._make_incident(para)
            if article:
                articles.append(article)

        return articles[:20]

    def _make_incident(self, text: str) -> dict:
        """Create an incident article from a block of text."""
        text = text.strip()
        if len(text) < 30:
            return {}

        incident_type = "Police Report"
        text_lower = text.lower()
        for itype in INCIDENT_TYPES:
            if itype.lower() in text_lower:
                incident_type = itype.title()
                break

        streets = STREET_PATTERN.findall(text)
        location = streets[0].strip() if streets else ""

        date_match = re.search(
            r"(\d{1,2}/\d{1,2}/\d{2,4}|\w+ \d{1,2},? \d{4})", text
        )
        published = None
        if date_match:
            published = self._parse_date(date_match.group(1))

        title = f"{incident_type}"
        if location:
            title += f" — {location}"

        return {
            "title": title[:200],
            "summary": text[:500],
            "url": self.source_url,
            "published_at": published,
        }

    def _scrape_google_news(self) -> list[dict]:
        """Fallback: scrape Google News RSS for Croton police news."""
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
            # Must mention Croton
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
