"""Scraper for Croton-Harmon Union Free School District calendar."""

import logging
import re
from bs4 import BeautifulSoup
from .base import BaseScraper

logger = logging.getLogger(__name__)


class SchoolCalendarScraper(BaseScraper):
    name = "schools"
    category = "schools"
    source_url = "https://www.chufsd.org/calendar"
    # Also try the news page
    news_url = "https://www.chufsd.org/news"

    def _scrape(self) -> list[dict]:
        articles = []

        # Try calendar page
        cal_articles = self._scrape_calendar()
        articles.extend(cal_articles)

        # Try news page
        news_articles = self._scrape_news()
        articles.extend(news_articles)

        return articles

    def _scrape_calendar(self) -> list[dict]:
        html = self.fetch(self.source_url)
        if not html:
            return []

        soup = BeautifulSoup(html, "lxml")
        articles = []

        # Finalsite / Blackboard CMS patterns
        events = soup.select(
            ".fsCalendarEvent, .calendar-event, .event-item, "
            ".cal-event, .vevent, [class*='event'], [class*='calendar']"
        )

        for event in events[:30]:
            try:
                title_el = event.select_one(
                    ".fsCalendarEventTitle, .event-title, .summary, "
                    "h3, h4, a, .title"
                )
                if not title_el:
                    continue

                title = title_el.get_text(strip=True)
                if not title or len(title) < 3:
                    continue

                href = ""
                link = title_el if title_el.name == "a" else title_el.select_one("a")
                if link and link.get("href"):
                    href = link["href"]
                    if href and not href.startswith("http"):
                        href = f"https://www.chufsd.org{href}"

                date_el = event.select_one(
                    ".fsCalendarDate, .event-date, .dtstart, time, "
                    "[class*='date']"
                )
                published = None
                if date_el:
                    date_text = date_el.get("datetime") or date_el.get_text(strip=True)
                    published = self._parse_date(date_text)

                desc_el = event.select_one(
                    ".fsCalendarEventDescription, .event-description, "
                    ".description, p"
                )
                summary = desc_el.get_text(strip=True) if desc_el else ""

                articles.append({
                    "title": f"School Event: {title}",
                    "url": href or self.source_url,
                    "summary": summary[:500],
                    "published_at": published,
                })
            except Exception as e:
                logger.debug(f"[schools] Failed to parse event: {e}")

        # If no structured events found, try extracting from text
        if not articles:
            articles = self._parse_text_calendar(soup)

        return articles

    def _scrape_news(self) -> list[dict]:
        html = self.fetch(self.news_url)
        if not html:
            return []

        soup = BeautifulSoup(html, "lxml")
        articles = []

        items = soup.select(
            ".fsNewsItem, .news-item, article, .post, "
            "[class*='news'] a, .view-content .views-row"
        )

        for item in items[:20]:
            try:
                title_el = item.select_one("h2, h3, h4, a, .title")
                if not title_el:
                    continue

                title = title_el.get_text(strip=True)
                if not title or len(title) < 5:
                    continue

                href = ""
                link = title_el if title_el.name == "a" else title_el.select_one("a")
                if link and link.get("href"):
                    href = link["href"]
                    if href and not href.startswith("http"):
                        href = f"https://www.chufsd.org{href}"

                date_el = item.select_one("time, .date, [class*='date']")
                published = None
                if date_el:
                    date_text = date_el.get("datetime") or date_el.get_text(strip=True)
                    published = self._parse_date(date_text)

                desc_el = item.select_one("p, .summary, .description, .body")
                summary = desc_el.get_text(strip=True) if desc_el else ""

                articles.append({
                    "title": title,
                    "url": href or self.news_url,
                    "summary": summary[:500],
                    "published_at": published,
                })
            except Exception as e:
                logger.debug(f"[schools] Failed to parse news item: {e}")

        return articles

    def _parse_text_calendar(self, soup) -> list[dict]:
        """Fallback: extract events from plain text on the page."""
        main = soup.select_one("main, #content, .region-content, body")
        if not main:
            return []

        text = main.get_text()
        articles = []

        # Look for date-event patterns
        pattern = re.compile(
            r"(\w+\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{2,4})"
            r"\s*[-:–]\s*(.+?)(?=\n|\r|$)"
        )
        for match in pattern.finditer(text):
            date_str, event_title = match.group(1), match.group(2).strip()
            if len(event_title) < 5:
                continue
            published = self._parse_date(date_str)
            articles.append({
                "title": f"School: {event_title}",
                "url": self.source_url,
                "summary": "",
                "published_at": published,
            })

        return articles[:20]
