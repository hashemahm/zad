import asyncio
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
    reading_seconds,
    truncate,
)
from app.models import READING

# Substack has no official public API; this is the endpoint behind substack.com's search box.
SEARCH_URL = "https://substack.com/api/v1/top/search"
# Substack rate-limits bursts of search requests (HTTP 429), so space out result pages.
PAGE_DELAY_SECONDS = 1.5
# Each publication (including custom domains) serves its own archive API.
ARCHIVE_URL = "https://{host}/api/v1/archive"
_SUBDOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


def publication_ref(url: str | None, name: str | None = None) -> ChannelRef | None:
    host = bare_host(url)
    if not host or host == "substack.com":
        return None
    return ChannelRef(key=host, name=name or host, url=f"https://{host}")


class SubstackCrawler:
    name = "substack"

    def __init__(self, settings: Settings) -> None:
        self.include_paid = settings.substack_include_paid
        self.max_pages = settings.substack_max_pages
        self.wpm = settings.reading_words_per_minute
        self.page_delay = PAGE_DELAY_SECONDS

    def is_configured(self) -> tuple[bool, str | None]:
        return True, None

    async def search(self, client: httpx.AsyncClient, query: str, ctx: CrawlContext) -> list[CrawledItem]:
        items: list[CrawledItem] = []
        seen: set[str] = set()
        cursor = None
        for page in range(self.max_pages):
            params = {"query": query}
            if cursor:
                params["cursor"] = cursor
                await asyncio.sleep(self.page_delay)
            response = await client.get(SEARCH_URL, params=params)
            if response.status_code == 429 and page > 0:
                break  # rate-limited mid-crawl: keep what earlier pages returned
            response.raise_for_status()
            payload = response.json()
            for item in self.parse(payload, ctx, query):
                if item.url not in seen:
                    seen.add(item.url)
                    items.append(item)
            cursor = payload.get("nextCursor")
            if len(items) >= ctx.limit or not cursor:
                break
        return items[: ctx.limit]

    async def search_channel(self, client: httpx.AsyncClient, channel: ChannelRef, query: str,
                             ctx: CrawlContext) -> list[CrawledItem]:
        response = await client.get(ARCHIVE_URL.format(host=channel.key),
                                    params={"sort": "new", "search": query, "limit": 50})
        if response.status_code == 404:
            return []
        response.raise_for_status()
        items = []
        for post in response.json():
            bylines = post.get("publishedBylines") or []
            author = bylines[0].get("name") if bylines else None
            item = self.parse_post(post, ctx, query, author, channel.name)
            if item:
                items.append(item)
        return items[: ctx.limit]

    async def resolve_channel(self, client: httpx.AsyncClient, text: str) -> ChannelRef:
        """Accept a publication URL (name.substack.com or a custom domain) or its subdomain."""
        text = text.strip().lower()
        if _SUBDOMAIN_RE.match(text):
            text = f"{text}.substack.com"
        url = parse_url(text)
        ref = publication_ref(str(url)) if url else None
        if ref is None:
            raise ValueError("Use a publication link like name.substack.com")
        return ref

    @staticmethod
    def channel_from_stored(url: str, author: str | None) -> ChannelRef | None:
        # Stored authors look like "Writer · Publication".
        publication = author.rsplit(" · ", 1)[-1] if author else None
        return publication_ref(url, publication)

    def parse(self, payload: dict, ctx: CrawlContext, query: str = "") -> list[CrawledItem]:
        items: list[CrawledItem] = []
        for entry in payload.get("items", []):
            if entry.get("type") != "post":
                continue  # profiles, comments
            users = (entry.get("context") or {}).get("users") or []
            author = users[0]["name"] if users and users[0].get("name") else None
            publication = (entry.get("publication") or {}).get("name")
            item = self.parse_post(entry.get("post") or {}, ctx, query, author, publication)
            if item:
                items.append(item)
        return items

    def parse_post(self, post: dict, ctx: CrawlContext, query: str, author: str | None,
                   publication: str | None) -> CrawledItem | None:
        if post.get("type") != "newsletter":
            return None  # podcasts, threads, video posts
        if post.get("audience") != "everyone" and not self.include_paid:
            return None  # paywalled
        url = post.get("canonical_url")
        if not url:
            return None
        # The search is semantic and drifts off-topic ("Helm" -> trading recaps), so require
        # the topic's words in what the post says about itself.
        tags = " ".join(t.get("name", "") for t in post.get("postTags") or [])
        if query and not matches_query(query, post.get("title"), post.get("subtitle"),
                                       post.get("description"), post.get("truncated_body_text"), tags):
            return None
        duration = reading_seconds(int(post.get("wordcount") or 0), self.wpm)
        if duration and duration > ctx.max_reading_seconds:
            return None

        byline = f"{author} · {publication}" if author and publication else author or publication
        published = post.get("post_date")
        return CrawledItem(
            source="substack",
            content_type=READING,
            external_id=str(post.get("id", url)),
            url=url,
            title=(post.get("title") or "Untitled post").strip(),
            author=byline,
            description=truncate(post.get("subtitle") or post.get("description") or post.get("truncated_body_text")),
            thumbnail_url=post.get("cover_image"),
            duration_seconds=duration,
            published_at=datetime.fromisoformat(published.replace("Z", "+00:00")) if published else None,
            popularity=int(post.get("reaction_count") or 0) + int(post.get("restacks") or 0),
            channel=publication_ref(url, publication),
        )
