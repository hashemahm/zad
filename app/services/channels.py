"""Channels (the "sources" a user can prefer, block or add): resolving, storing and backfilling."""

import logging

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crawlers import CRAWLER_CLASSES, get_crawlers
from app.crawlers.base import ChannelRef, bare_host, is_youtube_url, parse_url
from app.db import SessionLocal
from app.models import Channel, ContentItem

log = logging.getLogger(__name__)


def upsert_channel(db: Session, platform: str, ref: ChannelRef, cache: dict | None = None) -> Channel:
    """Get or create the channel, refreshing its name from the latest crawl."""
    cache = {} if cache is None else cache
    name = " ".join(ref.name.split())[:255] or ref.key
    channel = cache.get((platform, ref.key))
    if channel is None:
        channel = db.scalar(select(Channel).where(Channel.platform == platform, Channel.key == ref.key))
    if channel is None:
        channel = Channel(platform=platform, key=ref.key, name=name, url=ref.url)
        db.add(channel)
        db.flush()
    elif not channel.manual and name != ref.key:
        channel.name = name
    cache[(platform, ref.key)] = channel
    return channel


def detect_platform(text: str) -> str | None:
    """Guess the platform from what the user typed; None when it is ambiguous (e.g. a bare domain)."""
    text = text.strip()
    if text.lower().startswith(("r/", "/r/")):
        return "reddit"
    if is_youtube_url(str(parse_url(text) or "")) or text.startswith("UC") and len(text) == 24:
        return "youtube"
    host = bare_host(parse_url(text)) if "." in text else None
    if not host:
        return None
    if host == "reddit.com" or host.endswith(".reddit.com"):
        return "reddit"
    if host == "dev.to":
        return "devto"
    if host == "medium.com" or host.endswith(".medium.com"):
        return "medium"
    if host.endswith(".substack.com"):
        return "substack"
    if host == "news.ycombinator.com":
        return "hackernews"
    return None


async def resolve_channel(platform: str, text: str, client: httpx.AsyncClient) -> ChannelRef:
    crawler = next((c for c in get_crawlers() if c.name == platform), None)
    if crawler is None:
        raise ValueError(f"{platform} is not enabled (see ENABLED_SOURCES)")
    return await crawler.resolve_channel(client, text)


def backfill_stored_items() -> int:
    """Link items stored before channels existed, using what was saved about them."""
    linked = 0
    with SessionLocal() as db:
        items = db.scalars(
            select(ContentItem).where(ContentItem.channel_id.is_(None), ContentItem.source != "youtube")
        ).all()
        cache: dict = {}
        authors: dict[Channel, set[str]] = {}
        for item in items:
            crawler_class = CRAWLER_CLASSES.get(item.source)
            ref = crawler_class.channel_from_stored(item.url, item.author) if crawler_class else None
            if ref:
                item.channel = upsert_channel(db, item.source, ref, cache)
                authors.setdefault(item.channel, set()).add(item.author or "")
                linked += 1
        # A channel named after one author but holding several authors' posts is really an
        # organization or publication (e.g. dev.to/aws-builders); the next crawl names it properly.
        for channel, names in authors.items():
            if len(names) > 1 and channel.name in names and not channel.manual:
                channel.name = channel.key
        db.commit()
    return linked


async def backfill_youtube_channels(client: httpx.AsyncClient | None = None) -> int:
    """YouTube items only stored the channel title, so ask the API for their channel ids."""
    crawler = next((c for c in get_crawlers() if c.name == "youtube"), None)
    if crawler is None or not crawler.is_configured()[0]:
        return 0
    with SessionLocal() as db:
        video_ids = list(db.scalars(select(ContentItem.external_id).distinct().where(
            ContentItem.channel_id.is_(None), ContentItem.source == "youtube")))
    if not video_ids:
        return 0

    settings = get_settings()
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=settings.http_timeout_seconds)
    try:
        refs = await crawler.channels_of_videos(client, video_ids)
    finally:
        if owns_client:
            await client.aclose()

    with SessionLocal() as db:
        items = db.scalars(select(ContentItem).where(
            ContentItem.channel_id.is_(None), ContentItem.source == "youtube")).all()
        cache: dict = {}
        for item in items:
            if ref := refs.get(item.external_id):
                item.channel = upsert_channel(db, "youtube", ref, cache)
        db.commit()
    return len(refs)


async def backfill_channels() -> None:
    try:
        linked = backfill_stored_items()
        linked += await backfill_youtube_channels()
        if linked:
            log.info("Linked %d stored items to their sources", linked)
    except Exception:
        log.exception("Linking stored items to their sources failed")
