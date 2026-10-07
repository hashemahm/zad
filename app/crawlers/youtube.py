import re
from datetime import datetime

import httpx

from app.config import Settings
from app.crawlers.base import (
    ChannelRef,
    CrawlContext,
    CrawledItem,
    SourceUnavailable,
    is_youtube_url,
    parse_iso8601_duration,
    parse_url,
    path_segments,
    truncate,
)
from app.models import VIDEO

SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
_CHANNEL_ID_RE = re.compile(r"^UC[\w-]{22}$")


def channel_ref(channel_id: str, title: str | None) -> ChannelRef:
    return ChannelRef(key=channel_id, name=title or channel_id, url=f"https://www.youtube.com/channel/{channel_id}")


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
        return await self._search(client, query, ctx)

    async def search_channel(self, client: httpx.AsyncClient, channel: ChannelRef, query: str,
                             ctx: CrawlContext) -> list[CrawledItem]:
        return await self._search(client, query, ctx, channel_id=channel.key)

    async def _search(self, client: httpx.AsyncClient, query: str, ctx: CrawlContext,
                      channel_id: str | None = None) -> list[CrawledItem]:
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
        if channel_id:
            params["channelId"] = channel_id

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

    async def resolve_channel(self, client: httpx.AsyncClient, text: str) -> ChannelRef:
        """Accept a channel URL (/channel/UC…, /@handle), an @handle or a channel id."""
        if not self.api_key:
            raise ValueError("Set YOUTUBE_API_KEY to add YouTube channels")
        text = text.strip()
        params = {"part": "snippet", "key": self.api_key}
        if _CHANNEL_ID_RE.match(text):
            params["id"] = text
        elif text.startswith("@"):
            params["forHandle"] = text
        else:
            url = parse_url(text)
            parts = path_segments(url) if url and is_youtube_url(str(url)) else []
            if len(parts) >= 2 and parts[0] == "channel":
                params["id"] = parts[1]
            elif parts and parts[0].startswith("@"):
                params["forHandle"] = parts[0]
            else:
                raise ValueError("Use a channel link like youtube.com/@name or youtube.com/channel/UC…")
        response = await client.get(CHANNELS_URL, params=params)
        response.raise_for_status()
        found = response.json().get("items") or []
        if not found:
            raise ValueError("YouTube channel not found")
        return channel_ref(found[0]["id"], found[0].get("snippet", {}).get("title"))

    async def channels_of_videos(self, client: httpx.AsyncClient, video_ids: list[str]) -> dict[str, ChannelRef]:
        """Look up the channel of already stored videos, 50 per request (1 quota unit each)."""
        result: dict[str, ChannelRef] = {}
        for start in range(0, len(video_ids), 50):
            response = await client.get(
                VIDEOS_URL,
                params={"part": "snippet", "id": ",".join(video_ids[start:start + 50]), "key": self.api_key},
            )
            response.raise_for_status()
            for video in response.json().get("items", []):
                snippet = video.get("snippet", {})
                if snippet.get("channelId"):
                    result[video["id"]] = channel_ref(snippet["channelId"], snippet.get("channelTitle"))
        return result

    @staticmethod
    def channel_from_stored(url: str, author: str | None) -> ChannelRef | None:
        return None  # only the API knows a video's channel id; see channels_of_videos

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
                    channel=channel_ref(snippet["channelId"], snippet.get("channelTitle"))
                    if snippet.get("channelId") else None,
                )
            )
            if len(items) >= ctx.limit:
                break
        return items
