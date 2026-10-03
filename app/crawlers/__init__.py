from functools import lru_cache

from app.config import Settings, get_settings
from app.crawlers.base import Crawler
from app.crawlers.devto import DevToCrawler
from app.crawlers.hackernews import HackerNewsCrawler
from app.crawlers.medium import MediumCrawler
from app.crawlers.reddit import RedditCrawler
from app.crawlers.youtube import YouTubeCrawler

CRAWLER_CLASSES = {
    "youtube": YouTubeCrawler,
    "reddit": RedditCrawler,
    "hackernews": HackerNewsCrawler,
    "devto": DevToCrawler,
    "medium": MediumCrawler,
}


def build_crawlers(settings: Settings) -> list[Crawler]:
    return [CRAWLER_CLASSES[name](settings) for name in settings.sources]


@lru_cache
def get_crawlers() -> list[Crawler]:
    # Cached so stateful crawlers (e.g. Reddit's OAuth token) are reused across crawls.
    return build_crawlers(get_settings())
