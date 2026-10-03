from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, field_validator


def _as_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    # SQLite drops tzinfo; every stored timestamp is UTC.
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


UtcDatetime = Annotated[datetime, PlainSerializer(_as_utc, return_type=str | None)]
Priority = Annotated[int, Field(ge=1, le=5, description="1 = P1 (highest) .. 5 = P5 (lowest)")]
ContentType = Literal["video", "reading"]


def _normalize_name(value: str) -> str:
    value = " ".join(value.split())
    if not value:
        raise ValueError("Topic name cannot be empty")
    return value


class TopicCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    priority: Priority = 3

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        return _normalize_name(value)


class TopicUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    priority: Priority | None = None
    active: bool | None = None

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str | None) -> str | None:
        return _normalize_name(value) if value is not None else None


class TopicOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    priority: int
    active: bool
    created_at: UtcDatetime
    last_crawled_at: UtcDatetime | None
    last_crawl_summary: dict | None = None
    item_count: int = 0
    unseen_count: int = 0
    feed_share: float = 0.0


class ContentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    topic_id: int
    topic_name: str
    topic_priority: int
    source: str
    content_type: str
    external_id: str
    url: str
    title: str
    author: str | None
    description: str | None
    has_body: bool
    thumbnail_url: str | None
    duration_seconds: int | None
    published_at: UtcDatetime | None
    popularity: int
    discovered_at: UtcDatetime
    view_count: int
    last_viewed_at: UtcDatetime | None
    completed: bool
    bookmarked: bool
    dismissed: bool


class ContentDetail(ContentOut):
    body: str | None


class ContentUpdate(BaseModel):
    completed: bool | None = None
    bookmarked: bool | None = None
    dismissed: bool | None = None


class ContentPage(BaseModel):
    total: int
    items: list[ContentOut]


class HistoryEntry(BaseModel):
    id: int
    viewed_at: UtcDatetime
    item: ContentOut


class PreferencesIO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    max_video_minutes: int = Field(ge=1, le=240)
    max_reading_minutes: int = Field(ge=1, le=240)
    include_videos: bool
    include_reading: bool
    include_unknown_length: bool


class SourceStatus(BaseModel):
    name: str
    ready: bool
    reason: str | None


class CrawlResultOut(BaseModel):
    topic_id: int
    topic: str
    added: int
    updated: int
    sources: list[dict]
