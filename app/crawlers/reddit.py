import time
from datetime import UTC, datetime
from html import unescape

import httpx

from app.config import Settings
from app.crawlers.base import (
    CrawlContext,
    CrawledItem,
    SourceUnavailable,
    count_words,
    is_youtube_url,
    matches_query,
    reading_seconds,
    sanitize_html,
    truncate,
)
from app.models import READING

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
SEARCH_URL = "https://oauth.reddit.com/search"


class RedditCrawler:
    """Searches Reddit with an app-only OAuth token (anonymous access is blocked by Reddit)."""

    name = "reddit"

    def __init__(self, settings: Settings) -> None:
        self.client_id = settings.reddit_client_id
        self.client_secret = settings.reddit_client_secret
        self.user_agent = settings.reddit_user_agent
        self.min_score = settings.reddit_min_score
        self.wpm = settings.reading_words_per_minute
        self._token: str | None = None
        self._token_expires = 0.0

    def is_configured(self) -> tuple[bool, str | None]:
        if not (self.client_id and self.client_secret):
            return False, "REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET are not set"
        return True, None

    async def _get_token(self, client: httpx.AsyncClient) -> str:
        if self._token and time.monotonic() < self._token_expires:
            return self._token
        response = await client.post(
            TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(self.client_id, self.client_secret),
            headers={"User-Agent": self.user_agent},
        )
        response.raise_for_status()
        payload = response.json()
        self._token = payload["access_token"]
        self._token_expires = time.monotonic() + int(payload.get("expires_in", 3600)) - 60
        return self._token

    async def search(self, client: httpx.AsyncClient, query: str, ctx: CrawlContext) -> list[CrawledItem]:
        ready, reason = self.is_configured()
        if not ready:
            raise SourceUnavailable(reason)
        token = await self._get_token(client)
        response = await client.get(
            SEARCH_URL,
            params={
                "q": query,
                "sort": "relevance",
                "t": "year",
                "type": "link",
                "limit": min(100, ctx.limit * 3),
                "raw_json": 1,
            },
            headers={"Authorization": f"Bearer {token}", "User-Agent": self.user_agent},
        )
        response.raise_for_status()
        return self.parse(response.json(), ctx, query)

    def parse(self, payload: dict, ctx: CrawlContext, query: str = "") -> list[CrawledItem]:
        items: list[CrawledItem] = []
        for child in payload.get("data", {}).get("children", []):
            post = child.get("data", {})
            if post.get("over_18") or post.get("stickied") or post.get("score", 0) < self.min_score:
                continue
            permalink = "https://www.reddit.com" + post.get("permalink", "")
            selftext = post.get("selftext") or ""
            if query and not matches_query(query, post.get("title"), selftext):
                continue
            if post.get("is_self"):
                words = count_words(selftext)
                if words < 80:  # skip one-line questions; they are not learning material
                    continue
                url = permalink
                body = sanitize_html(unescape(post.get("selftext_html") or ""))
                duration = reading_seconds(words, self.wpm)
                if duration and duration > ctx.max_reading_seconds:
                    continue
            else:
                url = post.get("url_overridden_by_dest") or post.get("url")
                if not url or is_youtube_url(url) or post.get("is_video"):
                    continue  # videos come from the YouTube crawler
                body = None
                duration = None  # estimated later from the linked page

            thumb = post.get("thumbnail")
            items.append(
                CrawledItem(
                    source="reddit",
                    content_type=READING,
                    external_id=post.get("id", url),
                    url=url,
                    title=post.get("title", "Untitled post"),
                    author=f"u/{post['author']} in r/{post.get('subreddit', '')}" if post.get("author") else None,
                    description=truncate(selftext) or f"Discussion: {permalink}",
                    body=body,
                    thumbnail_url=thumb if thumb and thumb.startswith("http") else None,
                    duration_seconds=duration,
                    published_at=datetime.fromtimestamp(post["created_utc"], UTC) if post.get("created_utc") else None,
                    popularity=int(post.get("score", 0)),
                )
            )
            if len(items) >= ctx.limit:
                break
        return items
