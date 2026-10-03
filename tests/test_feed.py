import random
from collections import Counter

from app.db import SessionLocal
from app.models import ContentItem, Preferences, Topic
from app.services.feed import build_feed, feed_shares, weighted_interleave

WEIGHTS = {1: 16, 2: 8, 3: 4, 4: 2, 5: 1}


def test_weighted_interleave_matches_weights():
    rng = random.Random(42)
    counts = Counter()
    for _ in range(5000):
        pools = {"k8s": ["k"], "helm": ["h"], "slurm": ["s"]}
        counts[weighted_interleave(pools, {"k8s": 16, "helm": 8, "slurm": 1}, limit=1, rng=rng)[0]] += 1
    assert abs(counts["k"] / 5000 - 16 / 25) < 0.03
    assert abs(counts["h"] / 5000 - 8 / 25) < 0.03
    assert abs(counts["s"] / 5000 - 1 / 25) < 0.015


def test_weighted_interleave_redistributes_when_a_pool_runs_out():
    picked = weighted_interleave({"a": [1], "b": [2, 3, 4]}, {"a": 100, "b": 1}, limit=10, rng=random.Random(1))
    assert sorted(picked) == [1, 2, 3, 4]
    assert picked[1:] == [2, 3, 4]  # each pool keeps its best-first order


def _seed(db):
    k8s = Topic(name="Kubernetes", priority=1)
    slurm = Topic(name="Slurm", priority=5)
    paused = Topic(name="Paused", priority=1, active=False)
    db.add_all([k8s, slurm, paused])
    db.flush()

    def item(topic, n, kind="reading", duration=120, **kw):
        return ContentItem(topic_id=topic.id, source="devto", content_type=kind, external_id=n, url=f"https://x/{n}",
                           title=n, duration_seconds=duration, **kw)

    db.add_all([
        item(k8s, "short-read"),
        item(k8s, "long-read", duration=30 * 60),
        item(k8s, "unknown-read", duration=None),
        item(k8s, "short-video", kind="video", duration=5 * 60),
        item(k8s, "long-video", kind="video", duration=40 * 60),
        item(k8s, "done", completed=True),
        item(k8s, "hidden", dismissed=True),
        item(slurm, "slurm-read"),
        item(paused, "paused-read"),
    ])
    db.commit()


def test_build_feed_applies_preferences(client):
    with SessionLocal() as db:
        _seed(db)
        prefs = Preferences(id=1, max_video_minutes=10, max_reading_minutes=8, include_videos=True,
                            include_reading=True, include_unknown_length=False)
        titles = {i.title for i in build_feed(db, prefs, WEIGHTS, limit=50)}
        assert titles == {"short-read", "short-video", "slurm-read"}

        prefs.include_unknown_length = True
        assert "unknown-read" in {i.title for i in build_feed(db, prefs, WEIGHTS, limit=50)}

        prefs.include_videos = False
        assert {i.content_type for i in build_feed(db, prefs, WEIGHTS, limit=50)} == {"reading"}

        prefs.include_videos, prefs.max_video_minutes = True, 60
        videos = {i.title for i in build_feed(db, prefs, WEIGHTS, limit=50, content_type="video")}
        assert videos == {"short-video", "long-video"}


def test_feed_shares():
    topics = [Topic(id=1, name="a", priority=1, active=True), Topic(id=2, name="b", priority=5, active=True),
              Topic(id=3, name="c", priority=1, active=False)]
    shares = feed_shares(topics, WEIGHTS)
    assert shares == {1: 16 / 17, 2: 1 / 17}
