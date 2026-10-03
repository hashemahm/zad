"""Builds the microlearning feed, mixing topics in proportion to their priority weight."""

import random
from collections import defaultdict
from collections.abc import Hashable, Sequence

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from app.models import READING, VIDEO, ContentItem, Preferences, Topic


def weighted_interleave(
    pools: dict[Hashable, list],
    weights: dict[Hashable, float],
    limit: int,
    rng: random.Random | None = None,
) -> list:
    """Draw up to `limit` items, choosing each slot's pool with probability ∝ its weight.

    Each pool must already be in best-first order. Exhausted pools drop out, so their share
    is redistributed among the remaining ones.
    """
    rng = rng or random.Random()
    queues = {key: list(items) for key, items in pools.items() if items}
    picked = []
    while queues and len(picked) < limit:
        keys = list(queues)
        key = rng.choices(keys, weights=[weights[k] for k in keys])[0]
        picked.append(queues[key].pop(0))
        if not queues[key]:
            del queues[key]
    return picked


def eligible_items_query(prefs: Preferences, content_type: str | None = None):
    types = []
    if prefs.include_videos and content_type in (None, VIDEO):
        types.append(VIDEO)
    if prefs.include_reading and content_type in (None, READING):
        types.append(READING)

    length_rules = []
    for kind, max_minutes in ((VIDEO, prefs.max_video_minutes), (READING, prefs.max_reading_minutes)):
        fits = ContentItem.duration_seconds <= max_minutes * 60
        if prefs.include_unknown_length:
            fits = or_(fits, ContentItem.duration_seconds.is_(None))
        length_rules.append((ContentItem.content_type == kind) & fits)

    return (
        select(ContentItem)
        .join(Topic)
        .options(joinedload(ContentItem.topic))
        .where(
            Topic.active.is_(True),
            ContentItem.content_type.in_(types),
            ContentItem.completed.is_(False),
            ContentItem.dismissed.is_(False),
            or_(*length_rules),
        )
    )


def _rank_key(item: ContentItem):
    # Unseen first, then the source's best results, then newest.
    published = item.published_at.timestamp() if item.published_at else 0
    return (item.view_count > 0, -item.relevance, -published)


def build_feed(
    db: Session,
    prefs: Preferences,
    priority_weights: dict[int, float],
    limit: int,
    content_type: str | None = None,
    rng: random.Random | None = None,
) -> list[ContentItem]:
    if not (prefs.include_videos or prefs.include_reading):
        return []
    items: Sequence[ContentItem] = db.scalars(eligible_items_query(prefs, content_type)).unique().all()

    pools: dict[int, list[ContentItem]] = defaultdict(list)
    weights: dict[int, float] = {}
    for item in items:
        pools[item.topic_id].append(item)
        weights[item.topic_id] = priority_weights[item.topic.priority]
    for pool in pools.values():
        pool.sort(key=_rank_key)

    return weighted_interleave(pools, weights, limit, rng)


def feed_shares(topics: Sequence[Topic], priority_weights: dict[int, float]) -> dict[int, float]:
    """Expected fraction of the feed each active topic gets (assuming enough content)."""
    active = [t for t in topics if t.active]
    total = sum(priority_weights[t.priority] for t in active)
    return {t.id: (priority_weights[t.priority] / total if total else 0.0) for t in active}

