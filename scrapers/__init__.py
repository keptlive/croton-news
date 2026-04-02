from .village_news import VillageNewsScraper
from .police_blotter import PoliceBlotterScraper
from .fire_dept import FireDeptScraper
from .school_calendar import SchoolCalendarScraper
from .cortlandt_news import CortlandtNewsScraper

ALL_SCRAPERS = [
    VillageNewsScraper,
    PoliceBlotterScraper,
    FireDeptScraper,
    SchoolCalendarScraper,
    CortlandtNewsScraper,
]
