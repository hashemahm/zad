# Tech Microlearning

Pick topics, give each a priority, and get a feed of **short videos and readings** crawled from
YouTube, Reddit, Hacker News, DEV and Medium. Everything found is stored in a database, so you can
re-watch or re-read it later.

## Features

- **Topics with priority weights**: P1 (most important) to P5. Each topic's share of the feed is
  proportional to `PRIORITY_WEIGHTS` (default `16,8,4,2,1`). For example, Kubernetes P1 + Helm P2 +
  Slurm P5 gives a mix of about 64% / 32% / 4%.
- **Length limits**: a maximum video length and a maximum reading time, set in Settings. Videos use
  their real duration. Reading time comes from the source (DEV), the post's word count
  (Reddit/HN text posts), Medium's "N min read", or an estimate made by fetching the linked page.
- **Crawling**: runs when a topic is added, on demand, and on a schedule (`CRAWL_INTERVAL_MINUTES`).
- **Replay**: every item's URL and metadata are kept, plus the full text for text posts. Videos play
  embedded; text posts open in a built-in reader. History lists everything you opened. Library lets
  you search all stored content, including items marked done, saved or hidden.

## Quick start

```bash
uv sync                      # or: python -m venv .venv && pip install -e .
# edit .env (add YOUTUBE_API_KEY and, optionally, Reddit credentials)
uv run python -m app.main    # → http://127.0.0.1:8000
```

API docs: http://127.0.0.1:8000/docs

## Configuration (`.env`)

All settings live in `.env` (`.env.example` is a template you can commit). The main ones:

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | SQLAlchemy URL; defaults to SQLite in `data/` |
| `ENABLED_SOURCES` | `youtube,reddit,hackernews,devto,medium` |
| `YOUTUBE_API_KEY` | YouTube Data API v3 key. Videos come only from YouTube, so you need this for any videos |
| `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` | Reddit "script" app credentials (Reddit blocks anonymous API calls) |
| `PRIORITY_WEIGHTS` | Feed weight for P1..P5 |
| `CRAWL_INTERVAL_MINUTES` | Background re-crawl interval; `0` turns it off |
| `CRAWL_RESULTS_PER_SOURCE` | Results kept per source per crawl |
| `ESTIMATE_READING_TIME` | Fetch linked articles to estimate their reading time |
| `DEFAULT_MAX_VIDEO_MINUTES` / `DEFAULT_MAX_READING_MINUTES` | Starting values for the Settings screen |

Sources without credentials are skipped and shown as such on the Topics page. Hacker News, DEV
and Medium work without keys.

## How the feed is built

1. Candidates are content items from active topics that are not done or hidden, are of an allowed
   type, and fit the length limits. Unknown-length items count only if "include unknown length"
   is on.
2. Each topic's items are ordered unseen first, then by relevance (a source-normalised popularity
   rank), then by newest.
3. Each feed slot picks a topic at random, weighted by its priority weight, and takes that topic's
   next item. A topic that runs out drops out, and its share goes to the others.

## Project layout

```
app/
  config.py        settings from .env
  models.py        Topic, ContentItem, ViewEvent, Preferences
  crawlers/        one module per source + shared text helpers
  services/crawl.py   runs crawlers, estimates reading time, upserts into the DB
  services/feed.py    priority-weighted feed
  main.py          FastAPI routes, scheduler, static frontend
static/            single-page UI (vanilla JS)
tests/             pytest suite (offline; HTTP is mocked)
```

## Tests

```bash
uv run pytest
```

## Notes

- Single-user and has no authentication. Bind to `127.0.0.1` or put it behind auth before exposing it.
- YouTube search costs 100 quota units per call, and the default quota is 10,000 units a day.
  With 10 topics and a 6-hour interval the app uses about 4,000 units a day.
- Tables are created on startup. For schema changes on an existing database, add Alembic.
