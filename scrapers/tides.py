"""NOAA tidal data for Croton-on-Hudson area.

Source: NOAA CO-OPS Tides & Currents API
- Station 8518750: The Battery, NY (closest major tide station)
- Station 8531680: Sandy Hook, NJ (reference station)

Note: There's no NOAA tide station at Croton directly, but Hudson River
tidal influence extends well past Croton-Harmon. Battery data gives
a reasonable proxy for tidal timing (add ~2-3 hours for Croton).
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from .base import BaseScraper

logger = logging.getLogger(__name__)


class TidesScraper(BaseScraper):
    name = "tides"
    category = "weather"

    # NOAA CO-OPS API (no key needed)
    # The Battery, NYC — closest major station with predictions
    NOAA_PREDICTIONS_URL = (
        "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
        "?station=8518750"
        "&product=predictions"
        "&datum=MLLW"
        "&time_zone=lst_ldt"
        "&units=english"
        "&interval=hilo"
        "&format=json"
        "&application=croton.news"
    )

    NOAA_WATER_LEVEL_URL = (
        "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
        "?station=8518750"
        "&product=water_level"
        "&datum=MLLW"
        "&time_zone=lst_ldt"
        "&units=english"
        "&format=json"
        "&application=croton.news"
    )

    def _scrape(self) -> list[dict]:
        articles = []

        try:
            tide = self._get_tide_predictions()
            if tide:
                articles.append(tide)
        except Exception as e:
            logger.debug(f"[tides] Predictions failed: {e}")

        return articles

    def _get_tide_predictions(self) -> dict | None:
        now = datetime.now()
        begin = now.strftime("%Y%m%d")
        end = (now + timedelta(days=2)).strftime("%Y%m%d")

        url = f"{self.NOAA_PREDICTIONS_URL}&begin_date={begin}&end_date={end}"
        resp = self.session.get(url, timeout=15)
        if resp.status_code != 200:
            return None

        data = resp.json()
        predictions = data.get("predictions", [])
        if not predictions:
            return None

        # Format tide predictions
        parts = []
        for p in predictions[:8]:  # Next 8 high/low tides
            t = p.get("t", "")
            v = p.get("v", "")
            typ = "High" if p.get("type") == "H" else "Low"
            parts.append(f"{typ}: {v}ft @ {t}")

        summary = " | ".join(parts[:4])

        tide_data = {
            "station": "8518750",
            "station_name": "The Battery, NY",
            "note": "Add ~2-3 hours for Croton-on-Hudson timing",
            "predictions": predictions[:8],
        }

        return {
            "title": f"🌊 Tide Forecast: {parts[0] if parts else 'Data available'}",
            "summary": f"Hudson River tides (Battery station, +2-3hr for Croton): {summary}",
            "content": json.dumps(tide_data),
            "url": "https://tidesandcurrents.noaa.gov/noaatidepredictions.html?id=8518750",
            "source": "NOAA",
            "category": "weather",
            "published_at": datetime.now(timezone.utc).isoformat(),
        }
