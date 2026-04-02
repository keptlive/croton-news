"""Croton Free Library events scraper.
Source: https://www.crotonfreelibrary.org/events/upcoming
"""
import logging
import re
from datetime import datetime, timezone
from scrapers.base import BaseScraper

logger = logging.getLogger('croton-news')


class LibraryScraper(BaseScraper):
    """Scrape Croton Free Library events."""

    name = 'library'
    category = 'events'
    BASE_URL = 'https://www.crotonfreelibrary.org'
    EVENTS_URL = 'https://www.crotonfreelibrary.org/events/upcoming'

    def _scrape(self):
        from bs4 import BeautifulSoup

        html = self.fetch(self.EVENTS_URL)
        if not html:
            return []

        soup = BeautifulSoup(html, 'html.parser')
        articles = []

        # Library sites often use event listing structures
        # Try multiple selector strategies
        event_selectors = [
            '.event-item', '.views-row', '.event-card', '.event',
            '.node--type-event', 'article.event', '.eventlist-event',
            '.field-content', '.view-content .views-row',
            'li.event', '.event-listing', '.calendar-event',
        ]

        events = []
        for selector in event_selectors:
            events = soup.select(selector)
            if events:
                break

        if not events:
            # Fallback: try to find any list of events by looking for date + title patterns
            # Look for links that might be event links
            links = soup.select('a[href*="/event"], a[href*="/node/"], a[href*="/calendar"]')
            for link in links[:20]:
                title = link.get_text(strip=True)
                href = link.get('href', '')
                if title and len(title) > 5 and href:
                    url = href if href.startswith('http') else f"{self.BASE_URL}{href}"
                    articles.append({
                        'title': f"📚 Library: {title}",
                        'summary': f"Event at Croton Free Library. Visit {url} for details.",
                        'content': '',
                        'url': url,
                        'source': 'Croton Free Library',
                        'category': 'events',
                        'published_at': datetime.now(timezone.utc).isoformat(),
                    })

            return articles[:15]

        for event in events[:20]:
            title_elem = event.select_one('h2, h3, h4, .title, .event-title, .field--name-title, a')
            if not title_elem:
                continue

            title = title_elem.get_text(strip=True)
            if not title or len(title) < 3:
                continue

            # Get link
            link_elem = event.select_one('a[href]') or title_elem if title_elem.name == 'a' else None
            url = ''
            if link_elem and link_elem.get('href'):
                href = link_elem['href']
                url = href if href.startswith('http') else f"{self.BASE_URL}{href}"

            # Get date
            date_elem = event.select_one('.date, .event-date, time, .field--name-field-date')
            date_str = date_elem.get_text(strip=True) if date_elem else ''

            # Get description
            desc_elem = event.select_one('.description, .summary, .field--name-body, .teaser, p')
            description = desc_elem.get_text(strip=True) if desc_elem else ''

            # Get audience tag if available
            audience_elem = event.select_one('.audience, .tag, .category, .event-audience')
            audience = audience_elem.get_text(strip=True) if audience_elem else ''

            summary = f"{date_str}. {description}" if date_str else description
            if audience:
                summary = f"[{audience}] {summary}"

            articles.append({
                'title': f"📚 Library: {title}",
                'summary': summary[:500] if summary else f"Event at Croton Free Library.",
                'content': description,
                'url': url or self.EVENTS_URL,
                'source': 'Croton Free Library',
                'category': 'events',
                'published_at': datetime.now(timezone.utc).isoformat(),
            })

        return articles
