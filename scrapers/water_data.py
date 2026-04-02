"""USGS water data for Croton River and nearby waterways.

Source: USGS National Water Information System
- Croton River near Croton-on-Hudson (site 01375000 — New Croton Dam)
- Hudson River at Haverstraw (site 01376304)
"""

import json
import logging
from datetime import datetime, timezone
from .base import BaseScraper

logger = logging.getLogger(__name__)


class WaterDataScraper(BaseScraper):
    name = "water"
    category = "weather"

    # USGS Water Services API (no key needed)
    # Site 01375000 = Croton River at New Croton Dam
    CROTON_RIVER_URL = (
        "https://waterservices.usgs.gov/nwis/iv/"
        "?format=json&sites=01375000&parameterCd=00060,00065"
        "&siteStatus=all"
    )

    # Parameter codes:
    # 00060 = Discharge (cubic feet per second)
    # 00065 = Gage height (feet)

    def _scrape(self) -> list[dict]:
        articles = []

        try:
            data = self._fetch_usgs()
            if data:
                articles.append(data)
        except Exception as e:
            logger.debug(f"[water] USGS fetch failed: {e}")

        return articles

    def _fetch_usgs(self) -> dict | None:
        headers = {
            'User-Agent': 'croton.news/1.0 (andy@agentwire.email)',
            'Accept': 'application/json',
        }
        resp = self.session.get(self.CROTON_RIVER_URL, headers=headers, timeout=15)
        if resp.status_code != 200:
            return None

        data = resp.json()
        ts_list = data.get('value', {}).get('timeSeries', [])

        readings = {}
        for ts in ts_list:
            var_name = ts.get('variable', {}).get('variableName', '')
            values = ts.get('values', [{}])[0].get('value', [])
            if values:
                latest = values[-1]
                readings[var_name] = {
                    'value': latest.get('value'),
                    'timestamp': latest.get('dateTime'),
                }

        if not readings:
            return None

        # Build summary
        parts = []
        discharge = None
        gage_height = None

        for name, info in readings.items():
            val = info.get('value')
            if val is None:
                continue
            if 'discharge' in name.lower() or 'streamflow' in name.lower():
                discharge = val
                parts.append(f"Flow: {val} cfs")
            elif 'gage height' in name.lower():
                gage_height = val
                parts.append(f"Gage height: {val} ft")

        summary = ' | '.join(parts) if parts else 'Data available'

        water_data = {
            'site': '01375000',
            'site_name': 'Croton River at New Croton Dam',
            'discharge_cfs': discharge,
            'gage_height_ft': gage_height,
            'readings': readings,
        }

        return {
            'title': f"🌊 Croton River: {summary}",
            'summary': f"Croton River at New Croton Dam — {summary}",
            'content': json.dumps(water_data),
            'url': 'https://waterdata.usgs.gov/monitoring-location/01375000/',
            'source': 'USGS',
            'category': 'weather',
            'published_at': datetime.now(timezone.utc).isoformat(),
        }
