import asyncio
import random

import httpx
import pytest

from app.config import Settings
from app.crawlers.base import ChannelRef, CrawlContext, CrawledItem
from app.crawlers.devto import DevToCrawler
from app.crawlers.hackernews import HackerNewsCrawler
from app.crawlers.medium import MediumCrawler
from app.crawlers.reddit import RedditCrawler
from app.crawlers.substack import SubstackCrawler
from app.crawlers.youtube import YouTubeCrawler
from app.db import SessionLocal
from app.models import BLOCKED, PREFERRED, Channel, ContentItem, Preferences, Topic
from app.services import crawl as crawl_service
from app.services.channels import backfill_stored_items, detect_platform
from app.services.feed import build_feed

CTX = CrawlContext(limit=10, max_video_seconds=600, max_reading_seconds=480)
SETTINGS = Settings(_env_file=None, youtube_api_key="key", reddit_client_id="id", reddit_client_secret="secret")


def call(coro_fn, handler):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await coro_fn(client)
    return asyncio.run(go())


def no_http(request):
    raise AssertionError(f"unexpected request to {request.url}")


# --- resolving what the user typed ------------------------------------------------

@pytest.mark.parametrize("crawler, text, key", [
    (RedditCrawler(SETTINGS), "r/Kubernetes", "kubernetes"),
    (RedditCrawler(SETTINGS), "https://www.reddit.com/r/devops/top/", "devops"),
    (RedditCrawler(SETTINGS), "selfhosted", "selfhosted"),
    (HackerNewsCrawler(SETTINGS), "https://www.martinfowler.com/articles/x.html", "martinfowler.com"),
    (HackerNewsCrawler(SETTINGS), "github.com/kubernetes/kubernetes", "github.com/kubernetes"),
    (DevToCrawler(SETTINGS), "https://dev.to/aws-builders", "aws-builders"),
    (DevToCrawler(SETTINGS), "@ben", "ben"),
    (MediumCrawler(SETTINGS), "@Abonia", "medium.com/@abonia"),
    (MediumCrawler(SETTINGS), "https://medium.com/write-a-catalyst/some-post-123", "medium.com/write-a-catalyst"),
    (MediumCrawler(SETTINGS), "alexandrev.medium.com", "alexandrev.medium.com"),
    (SubstackCrawler(SETTINGS), "https://newsletter.pragmaticengineer.com/p/x", "newsletter.pragmaticengineer.com"),
    (SubstackCrawler(SETTINGS), "bytebytego", "bytebytego.substack.com"),
])
def test_resolve_channel(crawler, text, key):
    assert call(lambda c: crawler.resolve_channel(c, text), no_http).key == key


@pytest.mark.parametrize("crawler, text", [
    (RedditCrawler(SETTINGS), "https://example.com"),
    (SubstackCrawler(SETTINGS), "https://substack.com/@someone"),
    (MediumCrawler(SETTINGS), "https://medium.com/tag/kubernetes"),
])
def test_resolve_channel_rejects_bad_input(crawler, text):
    with pytest.raises(ValueError):
        call(lambda c: crawler.resolve_channel(c, text), no_http)


def test_youtube_resolves_handles_through_the_api():
    def handler(request):
        assert request.url.params["forHandle"] == "@fireship"
        return httpx.Response(200, json={"items": [{"id": "UCsBjURrPoezykLs9EqgamOA", "snippet": {"title": "Fireship"}}]})

    ref = call(lambda c: YouTubeCrawler(SETTINGS).resolve_channel(c, "https://www.youtube.com/@fireship/videos"), handler)
    assert (ref.key, ref.name) == ("UCsBjURrPoezykLs9EqgamOA", "Fireship")
    assert ref.url == "https://www.youtube.com/channel/UCsBjURrPoezykLs9EqgamOA"


def test_detect_platform():
    assert detect_platform("https://www.youtube.com/@fireship") == "youtube"
    assert detect_platform("r/kubernetes") == "reddit"
    assert detect_platform("dev.to/ben") == "devto"
    assert detect_platform("https://medium.com/@x") == "medium"
    assert detect_platform("bytebytego.substack.com") == "substack"
    assert detect_platform("martinfowler.com") is None  # a custom domain could be anything


# --- searching one channel ------------------------------------------------------------

def test_youtube_channel_search_restricts_to_channel_and_records_it():
    def handler(request):
        if request.url.path.endswith("/search"):
            assert request.url.params["channelId"] == "UCabc"
            return httpx.Response(200, json={"items": [{"id": {"videoId": "v"}}]})
        return httpx.Response(200, json={"items": [{
            "id": "v", "contentDetails": {"duration": "PT3M"}, "statistics": {},
            "snippet": {"title": "K8s", "channelId": "UCabc", "channelTitle": "Chan"}}]})

    channel = ChannelRef("UCabc", "Chan")
    items = call(lambda c: YouTubeCrawler(SETTINGS).search_channel(c, channel, "Kubernetes", CTX), handler)
    assert items[0].channel.key == "UCabc" and items[0].channel.name == "Chan"


def test_reddit_channel_search_uses_subreddit():
    def handler(request):
        if request.url.path == "/api/v1/access_token":
            return httpx.Response(200, json={"access_token": "tok"})
        assert request.url.path == "/r/kubernetes/search" and request.url.params["restrict_sr"] == "1"
        return httpx.Response(200, json={"data": {"children": [{"data": {
            "id": "p", "title": "Kubernetes upgrade notes", "score": 50, "subreddit": "kubernetes",
            "url": "https://blog/x", "permalink": "/r/kubernetes/p"}}]}})

    items = call(lambda c: RedditCrawler(SETTINGS).search_channel(c, ChannelRef("kubernetes", "r/kubernetes"),
                                                                 "Kubernetes", CTX), handler)
    assert [(i.external_id, i.channel.name) for i in items] == [("p", "r/kubernetes")]


def test_hackernews_channel_search_keeps_site_and_topic():
    def handler(request):
        assert request.url.params["restrictSearchableAttributes"] == "url"
        return httpx.Response(200, json={"hits": [
            {"objectID": "1", "title": "Kubernetes at scale", "url": "https://blog.example.com/k8s", "points": 50},
            {"objectID": "2", "title": "Unrelated post", "url": "https://example.com/other", "points": 50},
            {"objectID": "3", "title": "Kubernetes elsewhere", "url": "https://notexample.com/k8s", "points": 50},
        ]})

    items = call(lambda c: HackerNewsCrawler(SETTINGS).search_channel(
        c, ChannelRef("example.com", "example.com"), "Kubernetes", CTX), handler)
    assert [i.external_id for i in items] == ["1"]
    assert items[0].channel.key == "blog.example.com"


def test_devto_channel_search_filters_by_topic():
    def handler(request):
        assert request.url.params["username"] == "aws-builders"
        article = lambda i, title, tags: {"id": i, "title": title, "url": f"https://dev.to/aws-builders/{i}",  # noqa: E731
                                          "tag_list": tags, "reading_time_minutes": 3,
                                          "organization": {"username": "aws-builders", "name": "AWS Community Builders"},
                                          "user": {"username": "someone", "name": "Someone"}}
        return httpx.Response(200, json=[article(1, "EKS tips", ["kubernetes"]), article(2, "Lambda", ["serverless"])])

    items = call(lambda c: DevToCrawler(SETTINGS).search_channel(c, ChannelRef("aws-builders", "AWS"), "Kubernetes", CTX),
                 handler)
    assert [i.external_id for i in items] == ["1"]
    assert (items[0].channel.key, items[0].channel.name) == ("aws-builders", "AWS Community Builders")


def test_medium_channel_search_reads_the_profile_feed():
    rss = """<?xml version="1.0"?><rss xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0"><channel>
      <item><title>Helm tips</title><link>https://medium.com/@x/helm-tips-abc</link><dc:creator>X</dc:creator>
        <category>kubernetes</category><description>Charts</description></item>
      <item><title>Cooking</title><link>https://medium.com/@x/cooking-def</link><dc:creator>X</dc:creator>
        <description>Pasta</description></item>
    </channel></rss>"""

    def handler(request):
        assert str(request.url) == "https://medium.com/feed/@x"
        return httpx.Response(200, text=rss)

    items = call(lambda c: MediumCrawler(SETTINGS).search_channel(c, ChannelRef("medium.com/@x", "X"), "Kubernetes", CTX),
                 handler)
    assert [i.title for i in items] == ["Helm tips"]
    assert (items[0].channel.key, items[0].channel.name) == ("medium.com/@x", "X")


def test_substack_channel_search_uses_publication_archive():
    def handler(request):
        assert request.url.host == "ops.substack.com" and request.url.path == "/api/v1/archive"
        assert request.url.params["search"] == "Kubernetes"
        return httpx.Response(200, json=[{
            "id": 9, "type": "newsletter", "audience": "everyone", "title": "Kubernetes weekly", "wordcount": 400,
            "canonical_url": "https://ops.substack.com/p/k8s", "publishedBylines": [{"name": "Writer"}]}])

    channel = ChannelRef("ops.substack.com", "Ops Weekly")
    items = call(lambda c: SubstackCrawler(SETTINGS).search_channel(c, channel, "Kubernetes", CTX), handler)
    assert [(i.author, i.channel.key) for i in items] == [("Writer · Ops Weekly", "ops.substack.com")]


# --- crawl, store and feed ------------------------------------------------------------

class FakeCrawler:
    name = "devto"

    def __init__(self):
        self.channel_searches = []

    def is_configured(self):
        return True, None

    @staticmethod
    def _item(n, account):
        return CrawledItem(source="devto", content_type="reading", external_id=n, url=f"https://dev.to/{account}/{n}",
                           title=n, duration_seconds=120, channel=ChannelRef(account, account.title()))

    async def search(self, client, query, ctx):
        return [self._item("good", "alice"), self._item("spam", "spammer")]

    async def search_channel(self, client, channel, query, ctx):
        self.channel_searches.append(channel.key)
        return [self._item(f"extra-{channel.key}", channel.key)]


def test_crawl_skips_blocked_and_searches_preferred_sources(client, monkeypatch):
    fake = FakeCrawler()
    monkeypatch.setattr(crawl_service, "get_crawlers", lambda: [fake])
    with SessionLocal() as db:
        topic = Topic(name="Kubernetes", priority=1)
        db.add_all([topic, Channel(platform="devto", key="spammer", name="Spammer", status=BLOCKED),
                    Channel(platform="devto", key="bob", name="Bob", status=PREFERRED)])
        db.commit()
        topic_id = topic.id

    result = asyncio.run(crawl_service.crawl_topic(topic_id, client=httpx.AsyncClient()))
    assert fake.channel_searches == ["bob"]
    assert {s.get("channel") for s in result.sources} == {None, "Bob"}
    with SessionLocal() as db:
        stored = {i.title: i.channel.key for i in db.query(ContentItem)}
    assert stored == {"good": "alice", "extra-bob": "bob"}

    # Crawling only a newly added source does not run the general search.
    fake.channel_searches.clear()
    bob_id = client.get("/api/channels?status=preferred").json()["items"][0]["id"]
    asyncio.run(crawl_service.crawl_topic(topic_id, client=httpx.AsyncClient(), channel_ids={bob_id}))
    assert fake.channel_searches == ["bob"]


def _item(topic, channel, n, relevance=0.5):
    return ContentItem(topic_id=topic.id, channel=channel, source="devto", content_type="reading", external_id=n,
                       url=f"https://x/{n}", title=n, duration_seconds=120, relevance=relevance)


def test_feed_drops_blocked_and_ranks_preferred_first(client):
    with SessionLocal() as db:
        topic = Topic(name="Kubernetes", priority=1)
        liked = Channel(platform="devto", key="liked", name="Liked", status=PREFERRED)
        meh = Channel(platform="devto", key="meh", name="Meh")
        bad = Channel(platform="devto", key="bad", name="Bad", status=BLOCKED)
        db.add_all([topic, liked, meh, bad])
        db.flush()
        db.add_all([_item(topic, meh, "popular", relevance=1.0), _item(topic, liked, "liked", relevance=0.2),
                    _item(topic, bad, "blocked", relevance=1.0), _item(topic, None, "no-channel", relevance=0.1)])
        db.commit()
        prefs = Preferences(id=1, max_video_minutes=10, max_reading_minutes=8, include_videos=True,
                            include_reading=True, include_unknown_length=False)
        titles = [i.title for i in build_feed(db, prefs, {1: 1, 2: 1, 3: 1, 4: 1, 5: 1}, 10, rng=random.Random(0))]
    assert titles == ["liked", "popular", "no-channel"]

    assert {i["title"] for i in client.get("/api/content").json()["items"]} == {"liked", "popular", "no-channel"}
    blocked_id = client.get("/api/channels?status=blocked").json()["items"][0]["id"]
    assert [i["title"] for i in client.get(f"/api/content?channel_id={blocked_id}").json()["items"]] == ["blocked"]


def test_channels_api(client, monkeypatch):
    monkeypatch.setattr("app.services.channels.get_crawlers", lambda: [RedditCrawler(SETTINGS)])
    crawled = []

    async def fake_crawl(channel_ids=None):
        crawled.append(channel_ids)
        return []
    monkeypatch.setattr("app.main.crawl_all_active", fake_crawl)

    r = client.post("/api/channels", json={"value": "https://www.reddit.com/r/Kubernetes/"})
    assert r.status_code == 201
    channel = r.json()
    assert channel["platform"] == "reddit" and channel["name"] == "r/Kubernetes"
    assert channel["status"] == "preferred" and channel["manual"] is True and channel["item_count"] == 0
    assert crawled == [{channel["id"]}]  # fetched right away for every topic

    # Adding it again just updates the status.
    again = client.post("/api/channels", json={"value": "r/kubernetes", "status": "blocked"}).json()
    assert again["id"] == channel["id"] and again["status"] == "blocked"

    assert client.post("/api/channels", json={"value": "martinfowler.com"}).status_code == 422  # platform unknown
    assert client.post("/api/channels", json={"value": "x", "platform": "devto"}).status_code == 422  # not enabled

    r = client.patch(f"/api/channels/{channel['id']}", json={"status": "preferred"})
    assert r.json()["status"] == "preferred" and len(crawled) == 2
    assert client.patch(f"/api/channels/{channel['id']}", json={"status": "nope"}).status_code == 422

    listed = client.get("/api/channels?platform=reddit&q=kube").json()
    assert listed["total"] == 1 and listed["items"][0]["name"] == "r/Kubernetes"

    assert client.delete(f"/api/channels/{channel['id']}").status_code == 204
    assert client.get("/api/channels").json()["total"] == 0
    assert client.patch(f"/api/channels/{channel['id']}", json={"status": "neutral"}).status_code == 404


def test_deleting_a_source_with_items_resets_it(client):
    with SessionLocal() as db:
        topic = Topic(name="Helm", priority=1)
        channel = Channel(platform="devto", key="a", name="A", status=PREFERRED, manual=True)
        db.add_all([topic, channel])
        db.flush()
        db.add(_item(topic, channel, "x"))
        db.commit()
        channel_id = channel.id
    client.delete(f"/api/channels/{channel_id}")
    listed = client.get("/api/channels").json()["items"]
    assert [(c["status"], c["manual"], c["item_count"]) for c in listed] == [("neutral", False, 1)]
    assert client.get("/api/feed").json()[0]["channel_name"] == "A"


def test_backfill_links_items_stored_before_channels(client):
    with SessionLocal() as db:
        topic = Topic(name="Helm", priority=1)
        db.add(topic)
        db.flush()
        stored = [("devto", "https://dev.to/aws-builders/a-1", "Ann"), ("devto", "https://dev.to/aws-builders/b-2", "Bo"),
                  ("devto", "https://dev.to/carl/c-3", "Carl"), ("reddit", "https://blog/x", "u/me in r/devops"),
                  ("hackernews", "https://news.ycombinator.com/item?id=5", "pg"),
                  ("substack", "https://ops.substack.com/p/x", "Writer · Ops Weekly"),
                  ("youtube", "https://www.youtube.com/watch?v=v", "Chan")]
        db.add_all([ContentItem(topic_id=topic.id, source=src, content_type="reading", external_id=url, url=url,
                                title=url, author=author) for src, url, author in stored])
        db.commit()

    assert backfill_stored_items() == 6  # YouTube needs the API
    names = {(c["platform"], c["name"]) for c in client.get("/api/channels").json()["items"]}
    assert names == {("devto", "aws-builders"), ("devto", "Carl"), ("reddit", "r/devops"),
                     ("hackernews", "Hacker News (text posts)"), ("substack", "Ops Weekly")}
    assert backfill_stored_items() == 0


def test_backfill_youtube_channels_via_api(client, monkeypatch):
    from app.services.channels import backfill_youtube_channels
    monkeypatch.setattr("app.services.channels.get_crawlers", lambda: [YouTubeCrawler(SETTINGS)])
    with SessionLocal() as db:
        topic = Topic(name="Helm", priority=1)
        db.add(topic)
        db.flush()
        db.add_all([ContentItem(topic_id=topic.id, source="youtube", content_type="video", external_id=v,
                                url=f"https://www.youtube.com/watch?v={v}", title=v) for v in ("a", "b")])
        db.commit()

    def handler(request):
        assert request.url.params["id"] == "a,b"
        return httpx.Response(200, json={"items": [
            {"id": v, "snippet": {"channelId": "UCx", "channelTitle": "Chan"}} for v in ("a", "b")]})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await backfill_youtube_channels(http)
    assert asyncio.run(go()) == 2
    listed = client.get("/api/channels").json()["items"]
    assert [(c["key"], c["name"], c["item_count"]) for c in listed] == [("UCx", "Chan", 2)]
