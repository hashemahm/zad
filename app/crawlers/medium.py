import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import httpx

from app.config import Settings
from app.crawlers.base import (
    ChannelRef,
    CrawlContext,
    CrawledItem,
    bare_host,
    html_to_text,
    matches_query,
    parse_url,
    path_segments,
    truncate,
)
from app.models import READING

FEED_URL = "https://medium.com/feed/tag/{tag}"
NS = {"dc": "http://purl.org/dc/elements/1.1/", "content": "http://purl.org/rss/1.0/modules/content/"}
_IMG_RE = re.compile(r'<img[^>]+src="([^"]+)"', re.IGNORECASE)
_HANDLE_RE = re.compile(r"^@[A-Za-z0-9_.-]+$")


def channel_from_url(url: str | None, author: str | None = None) -> ChannelRef | None:
    """medium.com/@user/… -> the user, medium.com/<publication>/… -> the publication,
    <user>.medium.com or a custom domain -> that host."""
    parsed = parse_url(url) if url else None
    host = bare_host(parsed) if parsed else None
    if not host:
        return None
    if host == "medium.com":
        parts = path_segments(parsed)
        if not parts or parts[0] in {"p", "feed", "tag", "m"}:
            return None
        segment = parts[0].lower()
        name = author if segment.startswith("@") and author else parts[0].replace("-", " ").title()
        return ChannelRef(key=f"medium.com/{segment}", name=name, url=f"https://medium.com/{segment}")
    name = author if host.endswith(".medium.com") and author else host
    return ChannelRef(key=host, name=name, url=f"https://{host}")


def feed_url(channel: ChannelRef) -> str:
    if channel.key.startswith("medium.com/"):
        return "https://medium.com/feed/" + channel.key.removeprefix("medium.com/")
    return f"https://{channel.key}/feed"


def topic_to_slug(topic: str) -> str:
    """Medium tag slugs are hyphenated, e.g. 'Conflict Resolution' -> 'conflict-resolution'."""
    return re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")


class MediumCrawler:
    """Reads Medium's public tag RSS feed. Feeds only carry snippets, so reading time is
    estimated later from the article page."""

    name = "medium"

    def __init__(self, settings: Settings) -> None:
        pass

    def is_configured(self) -> tuple[bool, str | None]:
        return True, None

    async def search(self, client: httpx.AsyncClient, query: str, ctx: CrawlContext) -> list[CrawledItem]:
        response = await client.get(FEED_URL.format(tag=topic_to_slug(query)))
        if response.status_code == 404:
            return []
        response.raise_for_status()
        return self.parse(response.text, ctx)

    async def search_channel(self, client: httpx.AsyncClient, channel: ChannelRef, query: str,
                             ctx: CrawlContext) -> list[CrawledItem]:
        # A writer's or publication's feed holds their latest posts; keep those about the topic.
        response = await client.get(feed_url(channel))
        if response.status_code == 404:
            return []
        response.raise_for_status()
        return self.parse(response.text, ctx, query)

    async def resolve_channel(self, client: httpx.AsyncClient, text: str) -> ChannelRef:
        """Accept @user, a medium.com/@user or publication URL, or a Medium custom domain."""
        text = text.strip().strip("/")
        if _HANDLE_RE.match(text):
            text = f"medium.com/{text}"
        ref = channel_from_url(text)
        if ref is None:
            raise ValueError("Use a Medium profile like medium.com/@name")
        return ref

    @staticmethod
    def channel_from_stored(url: str, author: str | None) -> ChannelRef | None:
        return channel_from_url(url, author)

    @staticmethod
    def parse(xml_text: str, ctx: CrawlContext, query: str = "") -> list[CrawledItem]:
        root = ET.fromstring(xml_text)
        items: list[CrawledItem] = []
        for entry in root.iterfind("channel/item"):
            link = (entry.findtext("link") or "").split("?")[0]
            if not link:
                continue
            html = entry.findtext("content:encoded", namespaces=NS) or entry.findtext("description") or ""
            image = _IMG_RE.search(html)
            title = entry.findtext("title") or "Untitled story"
            text = html_to_text(html)
            categories = " ".join(c.text or "" for c in entry.iterfind("category"))
            if query and not matches_query(query, title, text, categories):
                continue
            pub = entry.findtext("pubDate")
            guid = entry.findtext("guid") or link
            author = entry.findtext("dc:creator", namespaces=NS)
            items.append(
                CrawledItem(
                    source="medium",
                    content_type=READING,
                    external_id=guid.rsplit("/", 1)[-1],
                    url=link,
                    title=title,
                    author=author,
                    description=truncate(text),
                    thumbnail_url=image.group(1) if image else None,
                    published_at=parsedate_to_datetime(pub) if pub else None,
                    channel=channel_from_url(link, author),
                )
            )
            if len(items) >= ctx.limit:
                break
        # The feed is newest-first and has no popularity signal; keep feed order.
        for rank, item in enumerate(items):
            item.popularity = len(items) - rank
        return items
