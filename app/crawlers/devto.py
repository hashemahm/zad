import re
from datetime import datetime

import httpx

from app.config import Settings
from app.crawlers.base import (
    ChannelRef,
    CrawlContext,
    CrawledItem,
    bare_host,
    matches_query,
    parse_url,
    path_segments,
    truncate,
)
from app.models import READING

ARTICLES_URL = "https://dev.to/api/articles"
SEARCH_URL = "https://dev.to/api/articles/search"
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{2,40}$")


def account_ref(username: str, name: str | None = None) -> ChannelRef:
    """A DEV user or organization; both publish under dev.to/<username>/."""
    return ChannelRef(key=username.lower(), name=name or username, url=f"https://dev.to/{username.lower()}")


def _account_from_url(url: str | None) -> str | None:
    parsed = parse_url(url) if url else None
    parts = path_segments(parsed) if parsed and bare_host(parsed) == "dev.to" else []
    return parts[0] if parts and _USERNAME_RE.match(parts[0]) else None


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

    async def search_channel(self, client: httpx.AsyncClient, channel: ChannelRef, query: str,
                             ctx: CrawlContext) -> list[CrawledItem]:
        # `username` works for users and organizations; keep the posts that are about the topic.
        response = await client.get(ARTICLES_URL, params={"username": channel.key, "per_page": 100})
        response.raise_for_status()
        return self.parse(response.json(), ctx, query)

    async def resolve_channel(self, client: httpx.AsyncClient, text: str) -> ChannelRef:
        """Accept a dev.to profile URL, @username or username."""
        text = text.strip().lstrip("@").strip("/")
        username = text if _USERNAME_RE.match(text) else _account_from_url(text)
        if not username:
            raise ValueError("Use a DEV profile like dev.to/username")
        return account_ref(username)

    @staticmethod
    def channel_from_stored(url: str, author: str | None) -> ChannelRef | None:
        username = _account_from_url(url)
        return account_ref(username, author) if username else None

    @staticmethod
    def parse(articles: list[dict], ctx: CrawlContext, query: str = "") -> list[CrawledItem]:
        items: list[CrawledItem] = []
        for article in articles:
            tags = article.get("tag_list") or article.get("tags") or ""
            if query and not matches_query(query, article.get("title"), article.get("description"),
                                           " ".join(tags) if isinstance(tags, list) else tags):
                continue
            minutes = article.get("reading_time_minutes")
            duration = int(minutes) * 60 if minutes else None
            if duration and duration > ctx.max_reading_seconds:
                continue
            published = article.get("published_at") or article.get("published_timestamp")
            org, user = article.get("organization") or {}, article.get("user") or {}
            account = (org.get("username") and account_ref(org["username"], org.get("name"))
                       or user.get("username") and account_ref(user["username"], user.get("name")))
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
                    channel=account or DevToCrawler.channel_from_stored(article["url"], user.get("name")),
                )
            )
            if len(items) >= ctx.limit:
                break
        return items
