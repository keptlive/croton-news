"""Weather data integrator for Croton-on-Hudson.
Sources:
- NWS current observations (station KHPN - White Plains)
- NWS zone forecast (Northern Westchester)
- AirNow AQI (ZIP 10520)
"""
import json
import logging
from datetime import datetime, timezone
from scrapers.base import BaseScraper

logger = logging.getLogger('croton-news')


class WeatherIntegrator(BaseScraper):
    """Fetch weather data from free APIs."""

    name = 'weather'
    category = 'weather'

    # NWS API endpoints (no key needed)
    NWS_POINTS = 'https://api.weather.gov/points/41.2087,-73.8912'  # Croton-on-Hudson coords
    NWS_STATIONS_OBS = 'https://api.weather.gov/stations/KHPN/observations/latest'
    NWS_ALERTS = 'https://api.weather.gov/alerts/active?point=41.2087,-73.8912'

    # AirNow (free API key required)
    AIRNOW_URL = 'https://www.airnowapi.org/aq/observation/zipCode/current/'

    def _scrape(self):
        articles = []

        # 1. Current conditions from NWS
        try:
            obs = self._get_current_conditions()
            if obs:
                articles.append(obs)
        except Exception as e:
            logger.warning(f"Weather: NWS observations failed: {e}")

        # 2. Active weather alerts
        try:
            alerts = self._get_weather_alerts()
            articles.extend(alerts)
        except Exception as e:
            logger.warning(f"Weather: NWS alerts failed: {e}")

        # 3. Forecast
        try:
            forecast = self._get_forecast()
            if forecast:
                articles.append(forecast)
        except Exception as e:
            logger.warning(f"Weather: NWS forecast failed: {e}")

        return articles

    def _get_current_conditions(self):
        """Get current weather from NWS."""
        headers = {
            'User-Agent': '(croton.news, andy@agentwire.email)',
            'Accept': 'application/geo+json'
        }
        resp = self.session.get(self.NWS_STATIONS_OBS, headers=headers, timeout=15)
        if resp.status_code != 200:
            return None

        data = resp.json()
        props = data.get('properties', {})

        temp_c = props.get('temperature', {}).get('value')
        temp_f = round(temp_c * 9 / 5 + 32, 1) if temp_c is not None else None
        humidity = props.get('relativeHumidity', {}).get('value')
        wind_speed_kmh = props.get('windSpeed', {}).get('value')
        wind_speed_mph = round(wind_speed_kmh * 0.621371, 1) if wind_speed_kmh is not None else None
        wind_dir = props.get('windDirection', {}).get('value')
        description = props.get('textDescription', 'Unknown')
        timestamp = props.get('timestamp', '')

        summary_parts = []
        if temp_f is not None:
            summary_parts.append(f"{temp_f}°F")
        summary_parts.append(description)
        if humidity is not None:
            summary_parts.append(f"Humidity: {round(humidity)}%")
        if wind_speed_mph is not None:
            summary_parts.append(f"Wind: {wind_speed_mph} mph")

        content = ' | '.join(summary_parts)

        # Store raw data as JSON in content for the weather widget
        weather_data = {
            'temp_f': temp_f,
            'temp_c': round(temp_c, 1) if temp_c is not None else None,
            'humidity': round(humidity) if humidity is not None else None,
            'wind_speed_mph': wind_speed_mph,
            'wind_direction': wind_dir,
            'description': description,
            'timestamp': timestamp,
        }

        return {
            'title': f"Current Weather: {temp_f}°F, {description}" if temp_f else f"Current Weather: {description}",
            'summary': content,
            'content': json.dumps(weather_data),
            'url': 'https://forecast.weather.gov/MapClick.php?lat=41.2087&lon=-73.8912',
            'source': 'NWS',
            'category': 'weather',
            'published_at': timestamp or datetime.now(timezone.utc).isoformat(),
        }

    def _get_weather_alerts(self):
        """Get active weather alerts for the area."""
        headers = {
            'User-Agent': '(croton.news, andy@agentwire.email)',
            'Accept': 'application/geo+json'
        }
        resp = self.session.get(self.NWS_ALERTS, headers=headers, timeout=15)
        if resp.status_code != 200:
            return []

        data = resp.json()
        features = data.get('features', [])
        articles = []

        for feature in features[:5]:  # Limit to 5 most recent alerts
            props = feature.get('properties', {})
            event = props.get('event', 'Weather Alert')
            headline = props.get('headline', event)
            description = props.get('description', '')
            severity = props.get('severity', 'Unknown')
            onset = props.get('onset', '')
            expires = props.get('expires', '')

            articles.append({
                'title': f"⚠️ {headline}",
                'summary': f"Severity: {severity}. {description[:300]}...",
                'content': description,
                'url': props.get('id', ''),
                'source': 'NWS',
                'category': 'weather',
                'published_at': onset or datetime.now(timezone.utc).isoformat(),
            })

        return articles

    def _get_forecast(self):
        """Get 7-day forecast from NWS."""
        headers = {
            'User-Agent': '(croton.news, andy@agentwire.email)',
            'Accept': 'application/geo+json'
        }

        # First get the forecast URL for our location
        resp = self.session.get(self.NWS_POINTS, headers=headers, timeout=15)
        if resp.status_code != 200:
            return None

        points_data = resp.json()
        forecast_url = points_data.get('properties', {}).get('forecast')
        if not forecast_url:
            return None

        resp2 = self.session.get(forecast_url, headers=headers, timeout=15)
        if resp2.status_code != 200:
            return None

        forecast_data = resp2.json()
        periods = forecast_data.get('properties', {}).get('periods', [])

        if not periods:
            return None

        # Build forecast summary from first 4 periods (today + tonight + tomorrow + tomorrow night)
        forecast_parts = []
        for p in periods[:4]:
            name = p.get('name', '')
            temp = p.get('temperature', '')
            unit = p.get('temperatureUnit', 'F')
            short = p.get('shortForecast', '')
            forecast_parts.append(f"{name}: {temp}°{unit}, {short}")

        summary = ' | '.join(forecast_parts)

        # Store full forecast as JSON
        forecast_json = [{
            'name': p.get('name'),
            'temperature': p.get('temperature'),
            'temperatureUnit': p.get('temperatureUnit'),
            'windSpeed': p.get('windSpeed'),
            'windDirection': p.get('windDirection'),
            'shortForecast': p.get('shortForecast'),
            'detailedForecast': p.get('detailedForecast'),
            'icon': p.get('icon'),
        } for p in periods[:14]]

        return {
            'title': f"Forecast: {periods[0].get('shortForecast', 'N/A')}, {periods[0].get('temperature', '')}°F",
            'summary': summary,
            'content': json.dumps(forecast_json),
            'url': 'https://forecast.weather.gov/MapClick.php?lat=41.2087&lon=-73.8912',
            'source': 'NWS',
            'category': 'weather',
            'published_at': datetime.now(timezone.utc).isoformat(),
        }


def get_aqi(api_key, zip_code='10520'):
    """Get Air Quality Index from AirNow. Requires free API key."""
    if not api_key:
        return None
    import requests
    url = f"https://www.airnowapi.org/aq/observation/zipCode/current/?format=application/json&zipCode={zip_code}&API_KEY={api_key}"
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data:
                return data
    except Exception:
        pass
    return None
