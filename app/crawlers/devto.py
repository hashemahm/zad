import re
from datetime import datetime

import httpx

from app.config import Settings
from app.crawlers.base import CrawlContext, CrawledItem, truncate
from app.models import READING

ARTICLES_URL = "https://dev.to/api/articles"
SEARCH_URL = "https://dev.to/api/articles/search"


def topic_to_tag(topic: str) -> str:
    """DEV tags are lowercase alphanumerics, e.g. 'Conflict Resolution' -> 'conflictresolution'."""
    return re.sub(r"[^a-z0-9]", "", topic.lower())


class DevToCrawler:
    name = "devto"

    def __init__(self, settings: Settings) -> None:
        pass

    def is_configured(self) -> tuple[bool, str | None]:
        return True, None

    async def search(self, client: httpx.AsyncClient, query: str, ctx: CrawlContext) -> list[CrawledItem]:
        per_page = min(100, ctx.limit * 2)
        # Top articles for the matching tag give the best quality; fall back to full-text search
        # for topics that are not a DEV tag.
        response = await client.get(
            ARTICLES_URL, params={"tag": topic_to_tag(query), "top": 365, "per_page": per_page}
        )
        response.raise_for_status()
        articles = response.json()
        if not articles:
            response = await client.get(SEARCH_URL, params={"q": query, "per_page": per_page})
            response.raise_for_status()
            articles = response.json()
        return self.parse(articles, ctx)

    @staticmethod
    def parse(articles: list[dict], ctx: CrawlContext) -> list[CrawledItem]:
        items: list[CrawledItem] = []
        for article in articles:
            minutes = article.get("reading_time_minutes")
            duration = int(minutes) * 60 if minutes else None
            if duration and duration > ctx.max_reading_seconds:
                continue
            published = article.get("published_at") or article.get("published_timestamp")
            items.append(
                CrawledItem(
                    source="devto",
                    content_type=READING,
                    external_id=str(article["id"]),
                    url=article["url"],
                    title=article.get("title", "Untitled article"),
                    author=(article.get("user") or {}).get("name"),
                    description=truncate(article.get("description")),
                    thumbnail_url=article.get("cover_image") or article.get("social_image"),
                    duration_seconds=duration,
                    published_at=datetime.fromisoformat(published.replace("Z", "+00:00")) if published else None,
                    popularity=int(article.get("public_reactions_count") or article.get("positive_reactions_count") or 0),
                )
            )
            if len(items) >= ctx.limit:
                break
        return items
