"""Transit data integrator for Croton-on-Hudson.
Sources:
- MTA Metro-North service alerts (HTML scraping)
- MTA service status API
"""
import logging
import re
from datetime import datetime, timezone
from scrapers.base import BaseScraper

logger = logging.getLogger('croton-news')


class TransitIntegrator(BaseScraper):
    """Fetch transit data from MTA and related sources."""

    name = 'transit'
    category = 'transit'

    # MTA service status
    MTA_STATUS_URL = 'https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/camsys%2Fsubway-status.json'
    MTA_ALERTS_URL = 'https://www.mta.info/alerts'

    # Metro-North specific
    MN_STATUS_URL = 'https://new.mta.info/alerts'

    def _scrape(self):
        articles = []

        # Scrape MTA service alerts page for Metro-North Hudson Line
        try:
            alerts = self._scrape_mta_alerts()
            articles.extend(alerts)
        except Exception as e:
            logger.warning(f"Transit: MTA alerts scraping failed: {e}")

        # Generate a transit status summary article
        try:
            status = self._get_metro_north_status()
            if status:
                articles.append(status)
        except Exception as e:
            logger.warning(f"Transit: Metro-North status failed: {e}")

        return articles

    def _scrape_mta_alerts(self):
        """Scrape MTA alerts page for Metro-North Hudson Line alerts."""
        from bs4 import BeautifulSoup

        # Try the MTA alerts page
        html = self.fetch('https://www.mta.info/alerts')
        if not html:
            return []

        soup = BeautifulSoup(html, 'html.parser')
        articles = []

        # Look for alert items mentioning Hudson Line or Metro-North
        alert_elements = soup.select('.alert-item, .service-alert, [class*="alert"], [class*="Alert"]')

        for elem in alert_elements:
            text = elem.get_text(strip=True)
            # Filter for Hudson Line related alerts
            if any(kw in text.lower() for kw in ['hudson', 'metro-north', 'croton', 'harmon', 'mnr']):
                title_elem = elem.select_one('h2, h3, h4, .title, .alert-title, strong')
                title = title_elem.get_text(strip=True) if title_elem else text[:100]

                articles.append({
                    'title': f"🚂 Metro-North: {title}",
                    'summary': text[:500],
                    'content': text,
                    'url': 'https://www.mta.info/alerts',
                    'source': 'MTA',
                    'category': 'transit',
                    'published_at': datetime.now(timezone.utc).isoformat(),
                })

        return articles[:5]

    def _get_metro_north_status(self):
        """Get Metro-North Hudson Line status."""
        # Try to get service status from MTA
        try:
            headers = {
                'User-Agent': 'croton.news/1.0 (andy@agentwire.email)',
                'Accept': 'text/html,application/json'
            }
            resp = self.session.get(
                'https://new.mta.info/system_generated/latest-service-status.json',
                headers=headers,
                timeout=15
            )
            if resp.status_code == 200:
                data = resp.json()
                # Look for Metro-North Hudson Line status
                for line in data.get('routeDetails', data.get('lines', [])):
                    name = line.get('name', line.get('route', ''))
                    if 'hudson' in name.lower():
                        status = line.get('status', line.get('statusSummary', 'Unknown'))
                        details = line.get('statusDetails', line.get('alertText', ''))
                        return {
                            'title': f"🚂 Hudson Line Status: {status}",
                            'summary': details[:500] if details else f"Metro-North Hudson Line is currently: {status}",
                            'content': details or status,
                            'url': 'https://new.mta.info/',
                            'source': 'MTA',
                            'category': 'transit',
                            'published_at': datetime.now(timezone.utc).isoformat(),
                        }
        except Exception as e:
            logger.debug(f"MTA JSON status failed: {e}")

        # Fallback: generate a basic status article
        return {
            'title': '🚂 Metro-North Hudson Line — Croton-Harmon Station',
            'summary': 'Check mta.info for current Metro-North Hudson Line service status and alerts affecting Croton-Harmon station.',
            'content': 'Visit https://new.mta.info/ for real-time Metro-North service alerts. Croton-Harmon station is a major stop on the Hudson Line.',
            'url': 'https://new.mta.info/',
            'source': 'MTA',
            'category': 'transit',
            'published_at': datetime.now(timezone.utc).isoformat(),
        }
