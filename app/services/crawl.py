"""Runs the crawlers for a topic and stores what they find."""

import asyncio
import json
import logging
from dataclasses import dataclass, field

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.crawlers import get_crawlers
from app.crawlers.base import (
    ChannelRef,
    CrawlContext,
    CrawledItem,
    CrawlReport,
    SourceUnavailable,
    assign_relevance,
    extract_page_info,
    reading_seconds,
)
from app.db import SessionLocal
from app.models import BLOCKED, PREFERRED, READING, Channel, ContentItem, Topic, utcnow
from app.services.channels import upsert_channel
from app.services.preferences import get_preferences

log = logging.getLogger(__name__)

MAX_PAGE_BYTES = 2_000_000
_topic_locks: dict[int, asyncio.Lock] = {}


@dataclass
class CrawlResult:
    topic_id: int
    topic: str
    added: int = 0
    updated: int = 0
    sources: list[dict] = field(default_factory=list)


async def _run_source(crawler, client: httpx.AsyncClient, query: str, ctx: CrawlContext,
                      channel: ChannelRef | None = None) -> CrawlReport:
    """Search a whole source, or only one of its channels when `channel` is given."""
    report = CrawlReport(source=crawler.name, channel=channel.name if channel else None)
    ready, reason = crawler.is_configured()
    if not ready:
        report.skipped = reason
        return report
    try:
        if channel:
            report.items = await crawler.search_channel(client, channel, query, ctx)
        else:
            report.items = await crawler.search(client, query, ctx)
        report.found = len(report.items)
        assign_relevance(report.items)
    except SourceUnavailable as exc:
        report.skipped = str(exc)
    except httpx.HTTPStatusError as exc:
        report.error = f"HTTP {exc.response.status_code} from {exc.request.url.host}"
        log.warning("%s crawl for %r failed: %s", crawler.name, query, report.error)
    except httpx.TransportError as exc:
        report.error = f"Network error: {exc or type(exc).__name__}"
        log.warning("%s crawl for %r failed: %s", crawler.name, query, report.error)
    except Exception as exc:  # one broken source must not fail the whole crawl
        report.error = f"{type(exc).__name__}: {exc}"
        log.exception("%s crawl for %r failed", crawler.name, query)
    return report


async def _estimate_page(client: httpx.AsyncClient, item: CrawledItem, wpm: int, sem: asyncio.Semaphore) -> None:
    """Fetch a linked article and estimate its reading time from its word count."""
    async with sem:
        try:
            async with client.stream("GET", item.url) as response:
                if response.status_code != 200 or "html" not in response.headers.get("content-type", ""):
                    return
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > MAX_PAGE_BYTES:
                        break
                html = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
        except (httpx.HTTPError, UnicodeDecodeError) as exc:
            log.debug("Could not fetch %s: %s", item.url, exc)
            return
    words, meta = extract_page_info(html)
    # Medium exposes "N min read" in twitter:data1, which is more accurate than our count.
    label = meta.get("twitter:data1", "")
    if label.endswith("min read") and label.split()[0].isdigit():
        item.duration_seconds = int(label.split()[0]) * 60
    else:
        item.duration_seconds = reading_seconds(words, wpm)
    if not item.thumbnail_url and meta.get("og:image", "").startswith("http"):
        item.thumbnail_url = meta["og:image"]
    if not item.description and meta.get("og:description"):
        item.description = meta["og:description"][:400]


def _dedupe(items: list[CrawledItem]) -> list[CrawledItem]:
    seen: dict[str, CrawledItem] = {}
    for item in items:
        existing = seen.get(item.url)
        if existing is None or item.relevance > existing.relevance:
            seen[item.url] = item
    return list(seen.values())


async def crawl_topic(topic_id: int, client: httpx.AsyncClient | None = None,
                      channel_ids: set[int] | None = None) -> CrawlResult:
    """Crawl every source plus the preferred channels, or only `channel_ids` when given."""
    lock = _topic_locks.setdefault(topic_id, asyncio.Lock())
    async with lock:
        return await _crawl_topic(topic_id, client, channel_ids)


async def _crawl_topic(topic_id: int, client: httpx.AsyncClient | None,
                       channel_ids: set[int] | None) -> CrawlResult:
    settings = get_settings()
    with SessionLocal() as db:
        topic = db.get(Topic, topic_id)
        if topic is None:
            raise LookupError(f"Topic {topic_id} not found")
        prefs = get_preferences(db)
        query = topic.name
        ctx = CrawlContext(
            limit=settings.crawl_results_per_source,
            max_video_seconds=prefs.max_video_minutes * 60,
            max_reading_seconds=prefs.max_reading_minutes * 60,
        )
        known_urls = set(db.scalars(select(ContentItem.url).where(ContentItem.topic_id == topic_id)))
        blocked = {(platform, key) for platform, key in
                   db.execute(select(Channel.platform, Channel.key).where(Channel.status == BLOCKED))}
        if channel_ids is not None:
            wanted = select(Channel).where(Channel.id.in_(channel_ids), Channel.status != BLOCKED)
        elif settings.preferred_source_results > 0:
            wanted = select(Channel).where(Channel.status == PREFERRED)
        else:
            wanted = None
        channels = [(c.platform, ChannelRef(c.key, c.name, c.url))
                    for c in (db.scalars(wanted).all() if wanted is not None else [])]

    result = CrawlResult(topic_id=topic_id, topic=query)
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(
            timeout=settings.http_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": settings.http_user_agent},
        )
    try:
        crawlers = {c.name: c for c in get_crawlers()}
        channel_ctx = CrawlContext(limit=max(1, settings.preferred_source_results),
                                   max_video_seconds=ctx.max_video_seconds,
                                   max_reading_seconds=ctx.max_reading_seconds)
        runs = [] if channel_ids is not None else [_run_source(c, client, query, ctx) for c in crawlers.values()]
        runs += [_run_source(crawlers[platform], client, query, channel_ctx, ref)
                 for platform, ref in channels if platform in crawlers]
        reports = await asyncio.gather(*runs)
        items = _dedupe([
            item for report in reports for item in report.items
            if not (item.channel and (item.source, item.channel.key) in blocked)
        ])

        if settings.estimate_reading_time:
            sem = asyncio.Semaphore(settings.max_concurrent_page_fetches)
            pending = [
                i for i in items
                if i.content_type == READING and i.duration_seconds is None and i.url not in known_urls
            ]
            await asyncio.gather(*(_estimate_page(client, i, settings.reading_words_per_minute, sem) for i in pending))
            # Drop readings that turned out to be longer than the learner wants.
            items = [
                i for i in items
                if not (i.content_type == READING and i.duration_seconds and i.duration_seconds > ctx.max_reading_seconds)
            ]
    finally:
        if owns_client:
            await client.aclose()

    result.sources = [
        {"source": r.source, "found": r.found, "error": r.error, "skipped": r.skipped}
        | ({"channel": r.channel} if r.channel else {})
        for r in reports
    ]
    _store(topic_id, items, result, record_crawl=channel_ids is None)
    return result


def _store(topic_id: int, items: list[CrawledItem], result: CrawlResult, record_crawl: bool = True) -> None:
    with SessionLocal() as db:
        topic = db.get(Topic, topic_id)
        if topic is None:
            return  # deleted while the crawl was running
        existing = {
            row.url: row
            for row in db.scalars(select(ContentItem).where(ContentItem.topic_id == topic_id))
        }
        channels: dict = {}
        for item in items:
            fields = {k: v for k, v in item.__dict__.items() if k != "channel"}
            channel = upsert_channel(db, item.source, item.channel, channels) if item.channel else None
            if channel is not None and channel.status == BLOCKED:
                continue  # blocked while the crawl was running
            row = existing.get(item.url)
            if row is None:
                db.add(ContentItem(topic_id=topic_id, channel=channel, **fields))
                result.added += 1
            else:
                row.channel = channel or row.channel
                # Refresh source metadata but keep the learner's state (views, completed, ...).
                row.title = item.title
                row.popularity = item.popularity
                row.relevance = item.relevance
                row.description = item.description or row.description
                row.body = item.body or row.body
                row.thumbnail_url = item.thumbnail_url or row.thumbnail_url
                row.duration_seconds = item.duration_seconds or row.duration_seconds
                result.updated += 1

        if record_crawl:  # a crawl of only some channels is not a full crawl of the topic
            topic.last_crawled_at = utcnow()
            topic.last_crawl_summary = json.dumps(
                {"added": result.added, "updated": result.updated, "sources": result.sources}
            )
        db.commit()


async def crawl_all_active(channel_ids: set[int] | None = None) -> list[CrawlResult]:
    with SessionLocal() as db:
        topic_ids = list(
            db.scalars(select(Topic.id).where(Topic.active.is_(True)).order_by(Topic.priority, Topic.id))
        )
    results = []
    for topic_id in topic_ids:
        try:
            results.append(await crawl_topic(topic_id, channel_ids=channel_ids))
        except LookupError:
            continue  # deleted while the crawl was running
    return results
