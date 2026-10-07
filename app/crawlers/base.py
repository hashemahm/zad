import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from typing import Protocol

import httpx
import nh3

log = logging.getLogger(__name__)


@dataclass
class ChannelRef:
    """Where an item was published inside a platform: a YouTube channel, a subreddit, a site..."""

    key: str  # stable id within the platform, e.g. a YouTube channel id or "kubernetes" for r/kubernetes
    name: str
    url: str | None = None


@dataclass
class CrawledItem:
    source: str
    content_type: str  # "video" | "reading"
    external_id: str
    url: str
    title: str
    author: str | None = None
    description: str | None = None
    body: str | None = None
    thumbnail_url: str | None = None
    duration_seconds: int | None = None
    published_at: datetime | None = None
    popularity: int = 0
    relevance: float = 0.0
    channel: ChannelRef | None = None


@dataclass
class CrawlContext:
    """Learner preferences a crawler can use to ask the source for better-fitting results."""

    limit: int
    max_video_seconds: int
    max_reading_seconds: int


class SourceUnavailable(Exception):
    """Raised when a source cannot run, e.g. missing credentials."""


class Crawler(Protocol):
    name: str

    def is_configured(self) -> tuple[bool, str | None]:
        """Return (ready, reason-if-not-ready)."""

    async def search(
        self, client: httpx.AsyncClient, query: str, ctx: CrawlContext
    ) -> list[CrawledItem]: ...

    async def search_channel(
        self, client: httpx.AsyncClient, channel: ChannelRef, query: str, ctx: CrawlContext
    ) -> list[CrawledItem]:
        """Find content on `query` published by one channel (used for preferred sources)."""

    async def resolve_channel(self, client: httpx.AsyncClient, text: str) -> ChannelRef:
        """Turn what a user typed (a URL, handle or name) into a channel. Raises ValueError."""

    @staticmethod
    def channel_from_stored(url: str, author: str | None) -> ChannelRef | None:
        """Work out the channel of an item stored before channels were tracked."""


# --- text helpers -------------------------------------------------------------

_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)
_WORD_RE = re.compile(r"\w+", re.UNICODE)
_WS_RE = re.compile(r"\s+")

ALLOWED_TAGS = {
    "a", "b", "blockquote", "br", "code", "em", "h1", "h2", "h3", "h4", "h5", "h6",
    "hr", "i", "img", "li", "ol", "p", "pre", "strong", "table", "tbody", "td", "th",
    "thead", "tr", "ul",
}


def parse_iso8601_duration(value: str | None) -> int | None:
    """Parse a YouTube ISO-8601 duration like PT1H2M3S into seconds."""
    if not value:
        return None
    match = _DURATION_RE.match(value)
    if not match:
        return None
    parts = {k: int(v) for k, v in match.groupdict().items() if v}
    return (
        parts.get("days", 0) * 86400
        + parts.get("hours", 0) * 3600
        + parts.get("minutes", 0) * 60
        + parts.get("seconds", 0)
    )


def count_words(text: str | None) -> int:
    return len(_WORD_RE.findall(text or ""))


def reading_seconds(words: int, words_per_minute: int) -> int | None:
    if words <= 0:
        return None
    return max(60, math.ceil(words / words_per_minute) * 60)


def sanitize_html(html: str | None) -> str | None:
    if not html:
        return None
    cleaned = nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes={"a": {"href", "title"}, "img": {"src", "alt"}},
        url_schemes={"http", "https"},
        link_rel="noopener noreferrer nofollow",
    ).strip()
    return cleaned or None


class _TextExtractor(HTMLParser):
    """Collect readable text from an HTML page, skipping chrome like nav/footer/scripts."""

    SKIP = {"script", "style", "noscript", "nav", "footer", "header", "aside", "form", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.chunks: list[str] = []
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag == "meta":
            attr = dict(attrs)
            key = (attr.get("property") or attr.get("name") or "").lower()
            if key in {"og:image", "og:description", "description", "twitter:data1"} and attr.get("content"):
                self.meta.setdefault(key, attr["content"])

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth:
            self.chunks.append(data)


def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    parser = _TextExtractor()
    parser.feed(html)
    return _WS_RE.sub(" ", unescape(" ".join(parser.chunks))).strip()


def extract_page_info(html: str) -> tuple[int, dict[str, str]]:
    """Return (word count of readable text, selected <meta> values) for a page."""
    parser = _TextExtractor()
    parser.feed(html)
    return count_words(" ".join(parser.chunks)), parser.meta


def matches_query(query: str, *texts: str | None) -> bool:
    """True if every word of the query appears as a whole word in the given texts.

    Guards against fuzzy/typo-tolerant search backends (e.g. "Slurm" matching "slump").
    """
    haystack = " ".join(t for t in texts if t).lower()
    return all(re.search(rf"\b{re.escape(word)}", haystack) for word in query.lower().split())


def truncate(text: str | None, length: int = 400) -> str | None:
    if not text:
        return None
    text = _WS_RE.sub(" ", text).strip()
    return text if len(text) <= length else text[: length - 1].rstrip() + "…"


def assign_relevance(items: list[CrawledItem]) -> None:
    """Rank a single source's results by popularity into a 0..1 relevance score.

    Raw popularity is not comparable across sources (YouTube views vs HN points), so each
    source's best result gets 1.0 and the rest are spread evenly below it. The source's own
    search order breaks ties.
    """
    if not items:
        return
    order = sorted(range(len(items)), key=lambda i: (-items[i].popularity, i))
    n = len(items)
    for rank, index in enumerate(order):
        items[index].relevance = 1.0 - rank / n


def parse_url(text: str) -> httpx.URL | None:
    """Parse a URL a user typed, adding https:// when the scheme is missing."""
    text = text.strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", text, re.IGNORECASE):
        text = "https://" + text
    try:
        url = httpx.URL(text)
    except httpx.InvalidURL:
        return None
    return url if url.host and "." in url.host else None


def bare_host(url: str | httpx.URL | None) -> str | None:
    """'https://www.Example.com/x' -> 'example.com'."""
    if not url:
        return None
    try:
        host = (url if isinstance(url, httpx.URL) else httpx.URL(url)).host.lower()
    except httpx.InvalidURL:
        return None
    return host.removeprefix("www.") or None


def path_segments(url: httpx.URL) -> list[str]:
    return [part for part in url.path.split("/") if part]


YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "m.youtube.com", "www.youtube.com")


def is_youtube_url(url: str | None) -> bool:
    if not url:
        return False
    host = httpx.URL(url).host.lower()
    return any(host == h or host.endswith("." + h) for h in YOUTUBE_HOSTS)


@dataclass
class CrawlReport:
    source: str
    channel: str | None = None  # set when only one channel of the source was searched
    found: int = 0
    error: str | None = None
    skipped: str | None = None
    items: list[CrawledItem] = field(default_factory=list)
