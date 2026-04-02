"""Scraper for Town of Cortlandt news.

The Cortlandt site uses a custom ColdFusion CMS. The news page at
/cn/news/ renders a <section> containing a <ul> with <li> items.
Each <li> has an <a> link and a <span class="itemdate"> with the date.
"""

import logging
import re
from bs4 import BeautifulSoup
from .base import BaseScraper

logger = logging.getLogger(__name__)

BASE_URL = "https://www.townofcortlandtny.gov"


class CortlandtNewsScraper(BaseScraper):
    name = "cortlandt"
    category = "regional"
    source_url = f"{BASE_URL}/cn/news/"

    def _scrape(self) -> list[dict]:
        html = self.fetch()
        if not html:
            return []

        soup = BeautifulSoup(html, "lxml")
        articles = []

        # The Cortlandt news page structure:
        #   <article class="modnews">
        #     <section>
        #       <h2>Current News & Information</h2>
        #       <ul>
        #         <li>
        #           <a HREF="index.cfm?NID=58471&jump2=0">Title</a>
        #           &nbsp;<span class="itemdate">(Posted Wednesday, April 1, 2026)</span>
        #         </li>
        #         ...
        #       </ul>
        #     </section>
        #   </article>

        # Primary: find <li> elements inside the modnews article section
        section = soup.select_one("article.modnews section, section[aria-label='body-section']")
        if section:
            rows = section.select("li")
        else:
            # Fallback: any <li> inside #main-content that has a link
            rows = soup.select("#main-content li, .content-area li")

        for row in rows[:30]:
            try:
                article = self._parse_row(row)
                if article and article.get("title"):
                    articles.append(article)
            except Exception as e:
                logger.debug(f"[cortlandt] Failed to parse row: {e}")

        return articles

    def _parse_row(self, row) -> dict:
        link = row.select_one("a[href]")
        if not link:
            return {}

        title = link.get_text(strip=True)
        if not title or len(title) < 5:
            return {}

        href = link.get("href", "")
        if href and not href.startswith("http"):
            # Cortlandt uses relative hrefs like "index.cfm?NID=58471&jump2=0"
            if not href.startswith("/"):
                href = f"/cn/news/{href}"
            href = f"{BASE_URL}{href}"

        # Date: <span class="itemdate">(Posted Wednesday, April 1, 2026)</span>
        published = None
        date_el = row.select_one("span.itemdate")
        if date_el:
            date_text = date_el.get_text(strip=True)
            # Extract date from "(Posted Wednesday, April 1, 2026)" format
            m = re.search(
                r"(?:Posted\s+)?(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+"
                r"(\w+ \d{1,2},?\s*\d{4})",
                date_text,
            )
            if m:
                published = self._parse_date(m.group(1))

        return {
            "title": title,
            "url": href or self.source_url,
            "summary": "",
            "published_at": published,
        }
