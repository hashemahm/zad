from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

VIDEO = "video"
READING = "reading"


def utcnow() -> datetime:
    return datetime.now(UTC)


class Topic(Base):
    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    # 1 (P1, most important) .. 5 (P5, least important)
    priority: Mapped[int] = mapped_column(Integer, default=3)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_crawled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_crawl_summary: Mapped[str | None] = mapped_column(Text)

    items: Mapped[list["ContentItem"]] = relationship(
        back_populates="topic", cascade="all, delete-orphan", passive_deletes=True
    )


class ContentItem(Base):
    """A video or reading discovered by a crawler. Kept so it can be replayed later."""

    __tablename__ = "content_items"
    __table_args__ = (UniqueConstraint("topic_id", "url", name="uq_topic_url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    content_type: Mapped[str] = mapped_column(String(16), index=True)
    external_id: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(String(2048))
    title: Mapped[str] = mapped_column(String(512))
    author: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    # Sanitized HTML of the content itself when the source provides it (e.g. Reddit/HN text posts).
    body: Mapped[str | None] = mapped_column(Text)
    thumbnail_url: Mapped[str | None] = mapped_column(String(2048))
    # Video length, or estimated reading time for readings. None = unknown.
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    popularity: Mapped[int] = mapped_column(Integer, default=0)
    # 0..1 rank of the item within its source's results for this topic.
    relevance: Mapped[float] = mapped_column(Float, default=0.0)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Learner state
    view_count: Mapped[int] = mapped_column(Integer, default=0)
    last_viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    bookmarked: Mapped[bool] = mapped_column(Boolean, default=False)
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False)

    topic: Mapped[Topic] = relationship(back_populates="items")
    views: Mapped[list["ViewEvent"]] = relationship(
        back_populates="item", cascade="all, delete-orphan", passive_deletes=True
    )


class ViewEvent(Base):
    __tablename__ = "view_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    content_id: Mapped[int] = mapped_column(
        ForeignKey("content_items.id", ondelete="CASCADE"), index=True
    )
    viewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    item: Mapped[ContentItem] = relationship(back_populates="views")


class Preferences(Base):
    """Single-row table (id=1) with the learner's feed preferences."""

    __tablename__ = "preferences"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    max_video_minutes: Mapped[int] = mapped_column(Integer)
    max_reading_minutes: Mapped[int] = mapped_column(Integer)
    include_videos: Mapped[bool] = mapped_column(Boolean, default=True)
    include_reading: Mapped[bool] = mapped_column(Boolean, default=True)
    # Show items whose length could not be determined.
    include_unknown_length: Mapped[bool] = mapped_column(Boolean, default=False)
