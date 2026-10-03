from datetime import datetime

import httpx

from app.config import Settings
from app.crawlers.base import (
    CrawlContext,
    CrawledItem,
    SourceUnavailable,
    parse_iso8601_duration,
    truncate,
)
from app.models import VIDEO

SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


class YouTubeCrawler:
    name = "youtube"

    def __init__(self, settings: Settings) -> None:
        self.api_key = settings.youtube_api_key
        self.language = settings.youtube_relevance_language
        self.region = settings.youtube_region_code

    def is_configured(self) -> tuple[bool, str | None]:
        if not self.api_key:
            return False, "YOUTUBE_API_KEY is not set"
        return True, None

    async def search(self, client: httpx.AsyncClient, query: str, ctx: CrawlContext) -> list[CrawledItem]:
        if not self.api_key:
            raise SourceUnavailable("YOUTUBE_API_KEY is not set")

        params = {
            "part": "snippet",
            "q": query,
            "type": "video",
            "videoEmbeddable": "true",
            "safeSearch": "moderate",
            # Over-fetch: results longer than the learner's max are filtered out below.
            "maxResults": min(50, ctx.limit * 2),
            "key": self.api_key,
        }
        if self.language:
            params["relevanceLanguage"] = self.language
        if self.region:
            params["regionCode"] = self.region
        # YouTube's own buckets: short < 4 min, medium 4-20 min, long > 20 min.
        if ctx.max_video_seconds <= 4 * 60:
            params["videoDuration"] = "short"

        response = await client.get(SEARCH_URL, params=params)
        response.raise_for_status()
        ids = [
            entry["id"]["videoId"]
            for entry in response.json().get("items", [])
            if entry.get("id", {}).get("videoId")
        ]
        if not ids:
            return []

        response = await client.get(
            VIDEOS_URL,
            params={"part": "snippet,contentDetails,statistics", "id": ",".join(ids), "key": self.api_key},
        )
        response.raise_for_status()
        return self.parse_videos(response.json(), ctx)

    @staticmethod
    def parse_videos(payload: dict, ctx: CrawlContext) -> list[CrawledItem]:
        items: list[CrawledItem] = []
        for video in payload.get("items", []):
            snippet = video.get("snippet", {})
            if snippet.get("liveBroadcastContent", "none") != "none":
                continue
            duration = parse_iso8601_duration(video.get("contentDetails", {}).get("duration"))
            if not duration or duration > ctx.max_video_seconds:
                continue
            thumbs = snippet.get("thumbnails", {})
            thumb = (thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url")
            items.append(
                CrawledItem(
                    source="youtube",
                    content_type=VIDEO,
                    external_id=video["id"],
                    url=f"https://www.youtube.com/watch?v={video['id']}",
                    title=snippet.get("title", "Untitled video"),
                    author=snippet.get("channelTitle"),
                    description=truncate(snippet.get("description")),
                    thumbnail_url=thumb,
                    duration_seconds=duration,
                    published_at=_parse_dt(snippet.get("publishedAt")),
                    popularity=int(video.get("statistics", {}).get("viewCount", 0) or 0),
                )
            )
            if len(items) >= ctx.limit:
                break
        return items
