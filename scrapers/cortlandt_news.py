"""Scraper for Town of Cortlandt news."""

import logging
from bs4 import BeautifulSoup
from .base import BaseScraper

logger = logging.getLogger(__name__)


class CortlandtNewsScraper(BaseScraper):
    name = "cortlandt"
    category = "regional"
    source_url = "https://www.townofcortlandtny.gov/cn/news/"

    def _scrape(self) -> list[dict]:
        html = self.fetch()
        if not html:
            return []

        soup = BeautifulSoup(html, "lxml")
        articles = []

        # Try common CMS patterns
        rows = soup.select(
            ".news-item, .post, article, .views-row, .entry, "
            ".news-list-item, .list-item, .cn-news-item, "
            ".view-content .node, tbody tr"
        )

        if not rows:
            # Broad fallback — look for heading+link combos
            rows = soup.select("main h2, main h3, #content h2, #content h3")

        for row in rows[:30]:
            try:
                article = self._parse_row(row)
                if article and article.get("title"):
                    articles.append(article)
            except Exception as e:
                logger.debug(f"[cortlandt] Failed to parse row: {e}")

        # If still empty, try links approach
        if not articles:
            articles = self._fallback_links(soup)

        return articles

    def _parse_row(self, row) -> dict:
        title_el = row.select_one("h2 a, h3 a, a, .title a")
        if not title_el:
            # Row might be the heading itself
            if row.name in ("h2", "h3"):
                link = row.select_one("a")
                if link:
                    title_el = link
                else:
                    return {
                        "title": row.get_text(strip=True),
                        "url": self.source_url,
                        "summary": "",
                        "published_at": None,
                    }
            else:
                return {}

        title = title_el.get_text(strip=True)
        if not title or len(title) < 5:
            return {}

        href = title_el.get("href", "")
        if href and not href.startswith("http"):
            if not href.startswith("/"):
                href = f"/{href}"
            href = f"https://www.townofcortlandtny.gov{href}"

        date_el = row.select_one("time, .date, .created, [class*='date']")
        published = None
        if date_el:
            date_text = date_el.get("datetime") or date_el.get_text(strip=True)
            published = self._parse_date(date_text)

        summary_el = row.select_one("p, .summary, .body, .description, .teaser")
        summary = summary_el.get_text(strip=True) if summary_el else ""

        return {
            "title": title,
            "url": href or self.source_url,
            "summary": summary[:500],
            "published_at": published,
        }

    def _fallback_links(self, soup) -> list[dict]:
        """Last resort: extract any meaningful links from main content."""
        main = soup.select_one("main, #content, .region-content")
        if not main:
            main = soup
        links = main.select("a[href]")
        articles = []
        seen = set()

        for link in links[:40]:
            href = link.get("href", "")
            title = link.get_text(strip=True)

            if not title or len(title) < 10 or title in seen:
                continue
            # Skip navigation/footer links
            if any(w in title.lower() for w in [
                "home", "contact", "login", "search", "menu", "skip",
                "facebook", "twitter", "instagram",
            ]):
                continue

            seen.add(title)
            if href and not href.startswith("http"):
                if not href.startswith("/"):
                    href = f"/{href}"
                href = f"https://www.townofcortlandtny.gov{href}"

            articles.append({
                "title": title,
                "url": href,
                "summary": "",
                "published_at": None,
            })

        return articles[:15]
