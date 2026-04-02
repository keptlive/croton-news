"""Scraper for Croton-on-Hudson Police Department blotter."""

import logging
import re
from bs4 import BeautifulSoup
from .base import BaseScraper

logger = logging.getLogger(__name__)

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
    source_url = "https://www.crotononhudson-ny.gov/node/229/news"

    def _scrape(self) -> list[dict]:
        html = self.fetch()
        if not html:
            return []

        soup = BeautifulSoup(html, "lxml")
        articles = []

        # Look for news items
        rows = soup.select(
            ".view-content .views-row, .view-content .node, "
            ".views-table tbody tr, .field-content"
        )
        if not rows:
            rows = soup.select("main .field-item, .region-content .field-item")

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
        title_el = row.select_one("h2 a, h3 a, .views-field-title a, a")
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
            href = f"https://www.crotononhudson-ny.gov{href}"

        date_el = row.select_one(
            ".views-field-created, .date-display-single, time"
        )
        published = None
        if date_el:
            date_text = date_el.get("datetime") or date_el.get_text(strip=True)
            published = self._parse_date(date_text)

        summary_el = row.select_one(".views-field-body, .field-summary")
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
        # Split on common delimiters: dates, bullet points, paragraph breaks
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

        # Detect incident type
        incident_type = "Police Report"
        text_lower = text.lower()
        for itype in INCIDENT_TYPES:
            if itype.lower() in text_lower:
                incident_type = itype.title()
                break

        # Extract street names
        streets = STREET_PATTERN.findall(text)
        location = streets[0].strip() if streets else ""

        # Extract date if present
        date_match = re.search(
            r"(\d{1,2}/\d{1,2}/\d{2,4}|\w+ \d{1,2},? \d{4})", text
        )
        published = None
        if date_match:
            published = self._parse_date(date_match.group(1))

        # Build title
        title = f"{incident_type}"
        if location:
            title += f" — {location}"

        return {
            "title": title[:200],
            "summary": text[:500],
            "url": self.source_url,
            "published_at": published,
        }
