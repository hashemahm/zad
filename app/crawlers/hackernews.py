from datetime import UTC, datetime

import httpx

from app.config import Settings
from app.crawlers.base import (
    ChannelRef,
    CrawlContext,
    CrawledItem,
    bare_host,
    path_segments,
    count_words,
    html_to_text,
    is_youtube_url,
    matches_query,
    parse_url,
    reading_seconds,
    sanitize_html,
    truncate,
)
from app.models import READING

SEARCH_URL = "https://hn.algolia.com/api/v1/search"
HN_HOST = "news.ycombinator.com"
# Sites hosting many unrelated authors; their source is the first path segment (the owner).
SHARED_HOSTS = {"github.com", "gitlab.com", "medium.com", "dev.to", "substack.com", "x.com", "twitter.com"}


def site_ref(url: str | None) -> ChannelRef | None:
    """HN links point at other sites, so the site is the source. Text posts belong to HN itself."""
    parsed = parse_url(url) if url else None
    host = bare_host(parsed) if parsed else HN_HOST if url is None else None
    if not host:
        return None
    if host == HN_HOST:
        return ChannelRef(key=HN_HOST, name="Hacker News (text posts)", url=f"https://{HN_HOST}")
    parts = path_segments(parsed) if host in SHARED_HOSTS else []
    key = f"{host}/{parts[0].lower()}" if parts else host
    return ChannelRef(key=key, name=key, url=f"https://{key}")


def _on_site(url: str | None, site: str) -> bool:
    ref = site_ref(url)
    return bool(ref) and (ref.key == site or ref.key.endswith("." + site) or ref.key.startswith(site + "/"))


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

    async def search_channel(self, client: httpx.AsyncClient, channel: ChannelRef, query: str,
                             ctx: CrawlContext) -> list[CrawledItem]:
        if channel.key == HN_HOST:
            params = {"query": query, "tags": "story"}
        else:
            # Find the site's stories by URL; the topic filter is applied in parse().
            params = {"query": channel.key, "restrictSearchableAttributes": "url", "tags": "story"}
        response = await client.get(
            SEARCH_URL,
            params={**params, "hitsPerPage": 100, "numericFilters": f"points>={self.min_points}"},
        )
        response.raise_for_status()
        payload = response.json()
        payload["hits"] = [hit for hit in payload.get("hits", []) if _on_site(hit.get("url"), channel.key)]
        return self.parse(payload, ctx, query)

    async def resolve_channel(self, client: httpx.AsyncClient, text: str) -> ChannelRef:
        """Accept a site's domain or any URL on it, e.g. martinfowler.com."""
        url = parse_url(text)
        ref = site_ref(str(url)) if url else None
        if ref is None:
            raise ValueError("Use a website domain like martinfowler.com")
        return ref

    @staticmethod
    def channel_from_stored(url: str, author: str | None) -> ChannelRef | None:
        return site_ref(None if url.startswith(f"https://{HN_HOST}/item") else url)

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
                    channel=site_ref(hit.get("url")),
                )
            )
            if len(items) >= ctx.limit:
                break
        return items
