import asyncio
import json
import logging
from contextlib import asynccontextmanager, suppress
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Response, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.config import BASE_DIR, get_settings
from app.crawlers import get_crawlers
from app.db import get_db, init_db
from app.models import ContentItem, Topic, ViewEvent, utcnow
from app.schemas import (
    ContentDetail,
    ContentOut,
    ContentPage,
    ContentType,
    ContentUpdate,
    CrawlResultOut,
    HistoryEntry,
    PreferencesIO,
    SourceStatus,
    TopicCreate,
    TopicOut,
    TopicUpdate,
)
from app.services.crawl import crawl_all_active, crawl_topic
from app.services.feed import build_feed, feed_shares
from app.services.preferences import get_preferences

settings = get_settings()
logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("microlearning")

STATIC_DIR = BASE_DIR / "static"
DbSession = Annotated[Session, Depends(get_db)]


async def _scheduler() -> None:
    if settings.crawl_on_startup:
        await _safe_crawl_all()
    while True:
        await asyncio.sleep(settings.crawl_interval_minutes * 60)
        await _safe_crawl_all()


async def _safe_crawl_all() -> None:
    try:
        results = await crawl_all_active()
        log.info("Scheduled crawl finished: %d topics, %d new items",
                 len(results), sum(r.added for r in results))
    except Exception:
        log.exception("Scheduled crawl failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    task = asyncio.create_task(_scheduler()) if settings.crawl_interval_minutes > 0 else None
    yield
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title=settings.app_name, lifespan=lifespan)


# --- helpers ------------------------------------------------------------------

def _content_out(item: ContentItem, detail: bool = False) -> ContentOut:
    data = {
        **{c: getattr(item, c) for c in ContentOut.model_fields if hasattr(item, c)},
        "topic_name": item.topic.name,
        "topic_priority": item.topic.priority,
        "has_body": bool(item.body),
    }
    if detail:
        return ContentDetail(**data, body=item.body)
    return ContentOut(**data)


def _topic_out(db: Session, topic: Topic, shares: dict[int, float] | None = None) -> TopicOut:
    counts = db.execute(
        select(
            func.count(ContentItem.id),
            func.count(ContentItem.id).filter(ContentItem.view_count == 0, ContentItem.dismissed.is_(False)),
        ).where(ContentItem.topic_id == topic.id)
    ).one()
    if shares is None:
        shares = feed_shares(db.scalars(select(Topic)).all(), settings.weights)
    return TopicOut(
        id=topic.id,
        name=topic.name,
        priority=topic.priority,
        active=topic.active,
        created_at=topic.created_at,
        last_crawled_at=topic.last_crawled_at,
        last_crawl_summary=json.loads(topic.last_crawl_summary) if topic.last_crawl_summary else None,
        item_count=counts[0],
        unseen_count=counts[1],
        feed_share=shares.get(topic.id, 0.0),
    )


def _get_topic(db: Session, topic_id: int) -> Topic:
    topic = db.get(Topic, topic_id)
    if topic is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Topic not found")
    return topic


def _get_item(db: Session, content_id: int) -> ContentItem:
    item = db.get(ContentItem, content_id, options=[joinedload(ContentItem.topic)])
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Content not found")
    return item


async def _background_crawl(topic_id: int) -> None:
    try:
        await crawl_topic(topic_id)
    except LookupError:
        pass
    except Exception:
        log.exception("Background crawl for topic %s failed", topic_id)


# --- topics -------------------------------------------------------------------

@app.get("/api/topics", response_model=list[TopicOut])
def list_topics(db: DbSession):
    topics = db.scalars(select(Topic).order_by(Topic.priority, Topic.name)).all()
    shares = feed_shares(topics, settings.weights)
    return [_topic_out(db, t, shares) for t in topics]


@app.post("/api/topics", response_model=TopicOut, status_code=status.HTTP_201_CREATED)
def create_topic(payload: TopicCreate, db: DbSession, background: BackgroundTasks,
                 crawl: bool = Query(True, description="Start crawling the new topic right away")):
    if db.scalar(select(Topic).where(func.lower(Topic.name) == payload.name.lower())):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Topic '{payload.name}' already exists")
    topic = Topic(name=payload.name, priority=payload.priority)
    db.add(topic)
    db.commit()
    if crawl:
        background.add_task(_background_crawl, topic.id)
    return _topic_out(db, topic)


@app.patch("/api/topics/{topic_id}", response_model=TopicOut)
def update_topic(topic_id: int, payload: TopicUpdate, db: DbSession):
    topic = _get_topic(db, topic_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(topic, field, value)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Another topic already has that name")
    return _topic_out(db, topic)


@app.delete("/api/topics/{topic_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_topic(topic_id: int, db: DbSession):
    db.delete(_get_topic(db, topic_id))
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/api/topics/{topic_id}/crawl", response_model=CrawlResultOut)
async def crawl_one(topic_id: int):
    try:
        return (await crawl_topic(topic_id)).__dict__
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Topic not found")


@app.post("/api/crawl", status_code=status.HTTP_202_ACCEPTED)
async def crawl_everything(background: BackgroundTasks):
    background.add_task(_safe_crawl_all)
    return {"status": "started"}


@app.get("/api/sources", response_model=list[SourceStatus])
def list_sources():
    statuses = []
    for crawler in get_crawlers():
        ready, reason = crawler.is_configured()
        statuses.append(SourceStatus(name=crawler.name, ready=ready, reason=reason))
    return statuses


# --- preferences --------------------------------------------------------------

@app.get("/api/preferences", response_model=PreferencesIO)
def read_preferences(db: DbSession):
    return get_preferences(db)


@app.put("/api/preferences", response_model=PreferencesIO)
def write_preferences(payload: PreferencesIO, db: DbSession):
    prefs = get_preferences(db)
    for field, value in payload.model_dump().items():
        setattr(prefs, field, value)
    db.commit()
    return prefs


# --- feed & content -----------------------------------------------------------

@app.get("/api/feed", response_model=list[ContentOut])
def feed(db: DbSession,
         limit: int | None = Query(None, ge=1, le=100),
         content_type: ContentType | None = None):
    items = build_feed(db, get_preferences(db), settings.weights,
                       limit or settings.feed_default_size, content_type)
    return [_content_out(i) for i in items]


@app.get("/api/content", response_model=ContentPage)
def library(db: DbSession,
            topic_id: int | None = None,
            content_type: ContentType | None = None,
            status_filter: str = Query("all", alias="status",
                                       pattern="^(all|unseen|viewed|bookmarked|completed|dismissed)$"),
            q: str | None = Query(None, max_length=200),
            limit: int = Query(30, ge=1, le=200),
            offset: int = Query(0, ge=0)):
    query = select(ContentItem).options(joinedload(ContentItem.topic))
    if topic_id is not None:
        query = query.where(ContentItem.topic_id == topic_id)
    if content_type:
        query = query.where(ContentItem.content_type == content_type)
    if q:
        like = f"%{q}%"
        query = query.where(or_(ContentItem.title.ilike(like), ContentItem.description.ilike(like),
                                ContentItem.author.ilike(like)))
    query = query.where({
        "all": ContentItem.dismissed.is_(False),
        "unseen": (ContentItem.view_count == 0) & ContentItem.dismissed.is_(False),
        "viewed": ContentItem.view_count > 0,
        "bookmarked": ContentItem.bookmarked.is_(True),
        "completed": ContentItem.completed.is_(True),
        "dismissed": ContentItem.dismissed.is_(True),
    }[status_filter])

    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.scalars(query.order_by(ContentItem.discovered_at.desc(), ContentItem.id.desc())
                      .limit(limit).offset(offset)).all()
    return ContentPage(total=total, items=[_content_out(i) for i in rows])


@app.get("/api/content/{content_id}", response_model=ContentDetail)
def content_detail(content_id: int, db: DbSession):
    return _content_out(_get_item(db, content_id), detail=True)


@app.post("/api/content/{content_id}/view", response_model=ContentOut)
def record_view(content_id: int, db: DbSession):
    item = _get_item(db, content_id)
    now = utcnow()
    item.view_count += 1
    item.last_viewed_at = now
    db.add(ViewEvent(content_id=item.id, viewed_at=now))
    db.commit()
    return _content_out(item)


@app.patch("/api/content/{content_id}", response_model=ContentOut)
def update_content(content_id: int, payload: ContentUpdate, db: DbSession):
    item = _get_item(db, content_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(item, field, value)
    db.commit()
    return _content_out(item)


@app.get("/api/history", response_model=list[HistoryEntry])
def history(db: DbSession, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    events = db.scalars(
        select(ViewEvent)
        .options(joinedload(ViewEvent.item).joinedload(ContentItem.topic))
        .order_by(ViewEvent.viewed_at.desc(), ViewEvent.id.desc())
        .limit(limit).offset(offset)
    ).all()
    return [HistoryEntry(id=e.id, viewed_at=e.viewed_at, item=_content_out(e.item)) for e in events]


@app.get("/api/health")
def health():
    return {"status": "ok"}


# --- frontend -----------------------------------------------------------------

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host=settings.app_host, port=settings.app_port)


if __name__ == "__main__":
    run()
