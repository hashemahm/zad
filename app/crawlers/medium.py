import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import httpx

from app.config import Settings
from app.crawlers.base import CrawlContext, CrawledItem, html_to_text, truncate
from app.models import READING

FEED_URL = "https://medium.com/feed/tag/{tag}"
NS = {"dc": "http://purl.org/dc/elements/1.1/", "content": "http://purl.org/rss/1.0/modules/content/"}
_IMG_RE = re.compile(r'<img[^>]+src="([^"]+)"', re.IGNORECASE)


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

    @staticmethod
    def parse(xml_text: str, ctx: CrawlContext) -> list[CrawledItem]:
        root = ET.fromstring(xml_text)
        items: list[CrawledItem] = []
        for entry in root.iterfind("channel/item"):
            link = (entry.findtext("link") or "").split("?")[0]
            if not link:
                continue
            html = entry.findtext("content:encoded", namespaces=NS) or entry.findtext("description") or ""
            image = _IMG_RE.search(html)
            pub = entry.findtext("pubDate")
            guid = entry.findtext("guid") or link
            items.append(
                CrawledItem(
                    source="medium",
                    content_type=READING,
                    external_id=guid.rsplit("/", 1)[-1],
                    url=link,
                    title=entry.findtext("title") or "Untitled story",
                    author=entry.findtext("dc:creator", namespaces=NS),
                    description=truncate(html_to_text(html)),
                    thumbnail_url=image.group(1) if image else None,
                    published_at=parsedate_to_datetime(pub) if pub else None,
                )
            )
            if len(items) >= ctx.limit:
                break
        # The feed is newest-first and has no popularity signal; keep feed order.
        for rank, item in enumerate(items):
            item.popularity = len(items) - rank
        return items
