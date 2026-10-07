import asyncio

import httpx

from app.config import Settings
from app.crawlers.base import CrawlContext
from app.crawlers.devto import DevToCrawler, topic_to_tag
from app.crawlers.hackernews import HackerNewsCrawler
from app.crawlers.medium import MediumCrawler, topic_to_slug
from app.crawlers.reddit import RedditCrawler
from app.crawlers.substack import SubstackCrawler
from app.crawlers.youtube import YouTubeCrawler

CTX = CrawlContext(limit=10, max_video_seconds=600, max_reading_seconds=480)
SETTINGS = Settings(_env_file=None, youtube_api_key="key", reddit_client_id="id", reddit_client_secret="secret")


def run(crawler, handler, query="Kubernetes"):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await crawler.search(client, query, CTX)
    return asyncio.run(go())


def test_youtube_filters_long_and_live_videos():
    def handler(request: httpx.Request):
        if request.url.path.endswith("/search"):
            assert request.url.params["q"] == "Kubernetes"
            return httpx.Response(200, json={"items": [{"id": {"videoId": v}} for v in ("a", "b", "c")]})
        assert request.url.params["id"] == "a,b,c"
        video = lambda vid, dur, live="none": {  # noqa: E731
            "id": vid,
            "snippet": {"title": f"Video {vid}", "channelTitle": "Chan", "publishedAt": "2026-01-01T00:00:00Z",
                        "liveBroadcastContent": live, "thumbnails": {"high": {"url": f"https://i/{vid}.jpg"}}},
            "contentDetails": {"duration": dur},
            "statistics": {"viewCount": "1000"},
        }
        return httpx.Response(200, json={"items": [video("a", "PT5M"), video("b", "PT45M"), video("c", "PT3M", "live")]})

    items = run(YouTubeCrawler(SETTINGS), handler)
    assert [i.external_id for i in items] == ["a"]
    assert items[0].duration_seconds == 300
    assert items[0].url == "https://www.youtube.com/watch?v=a"
    assert items[0].content_type == "video"


def test_youtube_without_key_is_not_configured():
    ready, reason = YouTubeCrawler(Settings(_env_file=None, youtube_api_key="")).is_configured()
    assert not ready and "YOUTUBE_API_KEY" in reason


def test_hackernews_parses_links_and_text_posts():
    def handler(request):
        return httpx.Response(200, json={"hits": [
            {"objectID": "1", "title": "Kubernetes networking explained", "url": "https://blog/k8s", "points": 300, "author": "a", "created_at_i": 1700000000},
            {"objectID": "2", "title": "Ask HN: Kubernetes at home?", "story_text": "<p>" + "word " * 100 + "</p>", "points": 50},
            {"objectID": "3", "title": "Kubernetes talk", "url": "https://youtube.com/watch?v=x", "points": 99},
            {"objectID": "4", "title": "Unrelated typo match: Kubernets", "url": "https://x", "points": 99},
        ]})

    items = run(HackerNewsCrawler(SETTINGS), handler)
    assert [i.external_id for i in items] == ["1", "2"]
    assert items[0].duration_seconds is None  # estimated later from the page
    assert items[1].url == "https://news.ycombinator.com/item?id=2"
    assert items[1].body and items[1].duration_seconds == 60


def test_devto_falls_back_to_search_and_respects_reading_limit():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/api/articles":
            assert request.url.params["tag"] == "conflictresolution"
            return httpx.Response(200, json=[])
        article = lambda i, minutes: {"id": i, "title": f"A{i}", "url": f"https://dev.to/a{i}", "reading_time_minutes": minutes,  # noqa: E731
                                      "published_at": "2026-02-01T10:00:00Z", "public_reactions_count": 3, "user": {"name": "N"}}
        return httpx.Response(200, json=[article(1, 4), article(2, 20)])

    items = run(DevToCrawler(SETTINGS), handler, query="Conflict Resolution")
    assert calls == ["/api/articles", "/api/articles/search"]
    assert [i.external_id for i in items] == ["1"]
    assert items[0].duration_seconds == 240


def test_medium_rss():
    rss = """<?xml version="1.0"?><rss xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0"><channel>
      <item><title>Helm tips</title><link>https://medium.com/@x/helm-tips-abc?source=rss</link>
        <guid>https://medium.com/p/abc</guid><dc:creator>Writer</dc:creator><pubDate>Fri, 02 Oct 2026 15:17:17 GMT</pubDate>
        <description>&lt;img src="https://cdn/img.png"&gt;&lt;p&gt;Short intro&lt;/p&gt;</description></item>
    </channel></rss>"""

    def handler(request):
        assert request.url.path == "/feed/tag/helm-charts"
        return httpx.Response(200, text=rss)

    items = run(MediumCrawler(SETTINGS), handler, query="Helm Charts")
    assert len(items) == 1
    item = items[0]
    assert item.url == "https://medium.com/@x/helm-tips-abc"
    assert item.external_id == "abc" and item.author == "Writer"
    assert item.thumbnail_url == "https://cdn/img.png"
    assert item.description == "Short intro"


def test_reddit_uses_oauth_and_filters_posts():
    def handler(request):
        if request.url.path == "/api/v1/access_token":
            assert request.headers["authorization"].startswith("Basic ")
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer tok"
        post = lambda pid, **kw: {"data": {"id": pid, "title": "Kubernetes " + pid, "score": 100, "author": "me",  # noqa: E731
                                           "subreddit": "kubernetes", "permalink": f"/r/kubernetes/{pid}",
                                           "created_utc": 1700000000, **kw}}
        return httpx.Response(200, json={"data": {"children": [
            post("self", is_self=True, selftext="word " * 300, selftext_html="&lt;p&gt;hello&lt;/p&gt;"),
            post("short", is_self=True, selftext="how?"),
            post("link", is_self=False, url="https://blog.example/k8s"),
            post("yt", is_self=False, url="https://youtu.be/x"),
            post("nsfw", is_self=False, url="https://x", over_18=True),
            post("low", is_self=False, url="https://y", score=1),
        ]}})

    items = run(RedditCrawler(SETTINGS), handler)
    assert [i.external_id for i in items] == ["self", "link"]
    assert items[0].body == "<p>hello</p>" and items[0].duration_seconds == 120
    assert items[1].url == "https://blog.example/k8s"


def _substack_post(pid, words=500, audience="everyone", kind="newsletter"):
    return {
        "type": "post",
        "context": {"users": [{"name": "Writer"}]},
        "publication": {"name": "Ops Weekly"},
        "post": {"id": pid, "type": kind, "audience": audience, "title": f" Kubernetes post {pid} ", "wordcount": words,
                 "canonical_url": f"https://ops.substack.com/p/{pid}", "subtitle": "Sub", "reaction_count": 10,
                 "restacks": 2, "post_date": "2026-05-12T12:22:04.658Z", "cover_image": "https://cdn/c.png"},
    }


def test_substack_skips_paid_long_and_non_posts_and_follows_cursor():
    pages = []

    def handler(request):
        pages.append(request.url.params.get("cursor"))
        if "cursor" not in request.url.params:
            return httpx.Response(200, json={"nextCursor": "c1", "items": [
                {"type": "profileSearchResults", "results": []},
                _substack_post(1),
                _substack_post(2, audience="only_paid"),
                _substack_post(3, words=5000),  # ~22 min > 8 min limit
                _substack_post(4, kind="podcast"),
                {"type": "comment", "post": {}},
            ]})
        off_topic = _substack_post(6)
        off_topic["post"]["title"], off_topic["post"]["subtitle"] = "Weekly trade recap", "Markets"
        return httpx.Response(200, json={"nextCursor": None, "items": [_substack_post(1), _substack_post(5, words=100), off_topic]})

    crawler = SubstackCrawler(SETTINGS)
    crawler.page_delay = 0
    items = run(crawler, handler)
    assert pages == [None, "c1"]
    assert [i.external_id for i in items] == ["1", "5"]
    first = items[0]
    assert first.url == "https://ops.substack.com/p/1" and first.title == "Kubernetes post 1"
    assert first.author == "Writer · Ops Weekly" and first.duration_seconds == 180
    assert first.popularity == 12 and first.content_type == "reading"


def test_substack_keeps_first_page_when_rate_limited():
    def handler(request):
        if "cursor" in request.url.params:
            return httpx.Response(429)
        return httpx.Response(200, json={"nextCursor": "c1", "items": [_substack_post(1)]})

    crawler = SubstackCrawler(SETTINGS)
    crawler.page_delay = 0
    assert [i.external_id for i in run(crawler, handler)] == ["1"]


def test_substack_can_include_paid_posts():
    crawler = SubstackCrawler(Settings(_env_file=None, substack_include_paid=True))
    items = crawler.parse({"items": [_substack_post(2, audience="only_paid")]}, CTX)
    assert [i.external_id for i in items] == ["2"]


def test_slug_helpers():
    assert topic_to_tag("Conflict Resolution") == "conflictresolution"
    assert topic_to_slug("Conflict Resolution") == "conflict-resolution"
    assert topic_to_slug("C++ / Rust") == "c-rust"
