from datetime import UTC, datetime

import httpx

from app.config import Settings
from app.crawlers.base import (
    CrawlContext,
    CrawledItem,
    count_words,
    html_to_text,
    is_youtube_url,
    matches_query,
    reading_seconds,
    sanitize_html,
    truncate,
)
from app.models import READING

SEARCH_URL = "https://hn.algolia.com/api/v1/search"


class HackerNewsCrawler:
    name = "hackernews"

    def __init__(self, settings: Settings) -> None:
        self.min_points = settings.hackernews_min_points
        self.wpm = settings.reading_words_per_minute

    def is_configured(self) -> tuple[bool, str | None]:
        return True, None

    async def search(self, client: httpx.AsyncClient, query: str, ctx: CrawlContext) -> list[CrawledItem]:
        response = await client.get(
            SEARCH_URL,
            params={
                "query": query,
                "tags": "story",
                "hitsPerPage": min(100, ctx.limit * 2),
                "numericFilters": f"points>={self.min_points}",
            },
        )
        response.raise_for_status()
        return self.parse(response.json(), ctx, query)

    def parse(self, payload: dict, ctx: CrawlContext, query: str = "") -> list[CrawledItem]:
        items: list[CrawledItem] = []
        for hit in payload.get("hits", []):
            story_id = hit.get("objectID") or hit.get("story_id")
            discussion = f"https://news.ycombinator.com/item?id={story_id}"
            url = hit.get("url") or discussion
            if is_youtube_url(url) or url.lower().endswith(".pdf"):
                continue
            story_html = hit.get("story_text")
            if query and not matches_query(query, hit.get("title"), html_to_text(story_html), url):
                continue
            duration = None
            if story_html and not hit.get("url"):
                duration = reading_seconds(count_words(html_to_text(story_html)), self.wpm)
                if duration and duration > ctx.max_reading_seconds:
                    continue
            items.append(
                CrawledItem(
                    source="hackernews",
                    content_type=READING,
                    external_id=str(story_id),
                    url=url,
                    title=hit.get("title") or "Untitled story",
                    author=hit.get("author"),
                    description=truncate(html_to_text(story_html)) or f"{hit.get('points', 0)} points · {hit.get('num_comments') or 0} comments on Hacker News",
                    body=sanitize_html(story_html) if not hit.get("url") else None,
                    published_at=datetime.fromtimestamp(hit["created_at_i"], UTC) if hit.get("created_at_i") else None,
                    duration_seconds=duration,
                    popularity=int(hit.get("points") or 0),
                )
            )
            if len(items) >= ctx.limit:
                break
        return items
