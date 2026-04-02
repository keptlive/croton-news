"""Emergency alerts integrator for Croton-on-Hudson area.

Sources:
- NWS severe weather alerts (already in weather.py)
- Westchester County emergency notifications
- NY-Alert RSS feeds
"""

import logging
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from .base import BaseScraper

logger = logging.getLogger(__name__)


class EmergencyAlertsScraper(BaseScraper):
    name = "emergency"
    category = "municipal"

    # Westchester County emergency page
    WESTCHESTER_ALERTS = "https://www.westchestercountyny.gov/emergency-services"

    # NWS alerts (severe + extreme only, area already has moderate in weather.py)
    FEMA_CAP = "https://api.weather.gov/alerts/active?point=41.2087,-73.8912&severity=Extreme,Severe"

    # Google News fallback for Westchester emergency alerts
    GOOGLE_EMERGENCY_RSS = (
        "https://news.google.com/rss/search?"
        "q=%22croton+on+hudson%22+OR+%22westchester+county%22+emergency+OR+alert+OR+evacuation"
        "&hl=en-US&gl=US&ceid=US:en"
    )

    def _scrape(self) -> list[dict]:
        articles = []

        # FEMA/NWS severe alerts
        try:
            alerts = self._get_severe_alerts()
            articles.extend(alerts)
        except Exception as e:
            logger.debug(f"[emergency] FEMA alerts failed: {e}")

        # Westchester County alerts
        try:
            wc = self._scrape_westchester()
            articles.extend(wc)
        except Exception as e:
            logger.debug(f"[emergency] Westchester alerts failed: {e}")

        # Google News emergency fallback (when no official alerts active)
        if not articles:
            try:
                gn = self._scrape_google_emergency()
                articles.extend(gn)
            except Exception as e:
                logger.debug(f"[emergency] Google News fallback failed: {e}")

        return articles

    def _get_severe_alerts(self) -> list[dict]:
        """Get severe/extreme alerts from NWS for the area."""
        headers = {
            'User-Agent': '(croton.news, andy@agentwire.email)',
            'Accept': 'application/geo+json'
        }
        resp = self.session.get(self.FEMA_CAP, headers=headers, timeout=15)
        if resp.status_code != 200:
            return []

        data = resp.json()
        articles = []

        for feature in data.get('features', [])[:5]:
            props = feature.get('properties', {})
            event = props.get('event', 'Emergency Alert')
            headline = props.get('headline', event)
            description = props.get('description', '')
            severity = props.get('severity', 'Unknown')
            urgency = props.get('urgency', '')
            onset = props.get('onset', '')

            articles.append({
                'title': f"🚨 {headline}",
                'summary': f"Severity: {severity}. Urgency: {urgency}. {description[:300]}",
                'content': description,
                'url': props.get('id', ''),
                'source': 'NWS/FEMA',
                'category': 'municipal',
                'published_at': onset or datetime.now(timezone.utc).isoformat(),
            })

        return articles

    def _scrape_westchester(self) -> list[dict]:
        """Scrape Westchester County alerts page."""
        html = self.fetch(self.WESTCHESTER_ALERTS)
        if not html:
            return []

        soup = BeautifulSoup(html, "lxml")
        articles = []

        alert_items = soup.select(
            ".alert-item, .views-row, article, .news-item, "
            "[class*='alert'], [class*='emergency']"
        )

        for item in alert_items[:10]:
            title_el = item.select_one("h2, h3, h4, a, .title, strong")
            if not title_el:
                continue

            title = title_el.get_text(strip=True)
            if not title or len(title) < 5:
                continue

            href = ""
            link = title_el if title_el.name == "a" else item.select_one("a[href]")
            if link and link.get("href"):
                href = link["href"]
                if not href.startswith("http"):
                    href = f"https://www.westchestergov.com{href}"

            desc_el = item.select_one("p, .summary, .body, .description")
            summary = desc_el.get_text(strip=True) if desc_el else ""

            # Filter for relevance to Croton area
            text = f"{title} {summary}".lower()
            if any(kw in text for kw in ['croton', 'county-wide', 'countywide', 'all residents',
                                          'westchester', 'hudson', 'northern westchester']):
                articles.append({
                    'title': f"⚠️ Westchester: {title}",
                    'url': href or self.WESTCHESTER_ALERTS,
                    'summary': summary[:500],
                    'source': 'Westchester County',
                    'category': 'municipal',
                    'published_at': datetime.now(timezone.utc).isoformat(),
                })

        return articles

    def _scrape_google_emergency(self) -> list[dict]:
        """Google News fallback for emergency/alert news."""
        import re
        xml = self.fetch(self.GOOGLE_EMERGENCY_RSS)
        if not xml:
            return []

        soup = BeautifulSoup(xml, "lxml-xml")
        articles = []

        for item in soup.select("item")[:10]:
            title_el = item.select_one("title")
            if not title_el:
                continue

            title = title_el.get_text(strip=True)
            desc_el = item.select_one("description")
            desc = desc_el.get_text(strip=True) if desc_el else ""

            text = f"{title} {desc}".lower()
            if not re.search(r"croton|westchester", text, re.IGNORECASE):
                continue
            # Must be emergency-related
            if not any(kw in text for kw in ['emergency', 'alert', 'evacuation', 'outage',
                                              'flooding', 'storm', 'hazard', 'warning']):
                continue

            link_el = item.select_one("link")
            url = link_el.get_text(strip=True) if link_el else ""

            pubdate_el = item.select_one("pubDate")
            published = None
            if pubdate_el:
                published = self._parse_date(pubdate_el.get_text(strip=True))

            summary = ""
            if desc_el:
                desc_soup = BeautifulSoup(desc_el.get_text(), "html.parser")
                summary = desc_soup.get_text(strip=True)[:500]

            articles.append({
                'title': f"⚠️ {title}",
                'url': url,
                'summary': summary,
                'source': 'News',
                'category': 'municipal',
                'published_at': published,
            })

        return articles[:5]
