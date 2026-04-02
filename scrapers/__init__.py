from .village_news import VillageNewsScraper
from .police_blotter import PoliceBlotterScraper
from .fire_dept import FireDeptScraper
from .school_calendar import SchoolCalendarScraper
from .cortlandt_news import CortlandtNewsScraper
from .weather import WeatherIntegrator
from .transit import TransitIntegrator
from .library import LibraryScraper
from .board_agendas import BoardAgendasScraper
from .emergency import EmergencyAlertsScraper
from .water_data import WaterDataScraper

ALL_SCRAPERS = [
    VillageNewsScraper,
    PoliceBlotterScraper,
    FireDeptScraper,
    SchoolCalendarScraper,
    CortlandtNewsScraper,
    WeatherIntegrator,
    TransitIntegrator,
    LibraryScraper,
    BoardAgendasScraper,
    EmergencyAlertsScraper,
    WaterDataScraper,
]
