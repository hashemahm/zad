from app.db import SessionLocal
from app.models import ContentItem


def test_topic_crud(client):
    r = client.post("/api/topics?crawl=false", json={"name": "  Kubernetes  ", "priority": 1})
    assert r.status_code == 201
    topic = r.json()
    assert topic["name"] == "Kubernetes" and topic["priority"] == 1 and topic["feed_share"] == 1.0

    assert client.post("/api/topics?crawl=false", json={"name": "kubernetes"}).status_code == 409
    assert client.post("/api/topics?crawl=false", json={"name": "Helm", "priority": 6}).status_code == 422

    client.post("/api/topics?crawl=false", json={"name": "Slurm", "priority": 5})
    shares = {t["name"]: t["feed_share"] for t in client.get("/api/topics").json()}
    assert shares == {"Kubernetes": 16 / 17, "Slurm": 1 / 17}

    r = client.patch(f"/api/topics/{topic['id']}", json={"priority": 2, "active": False})
    assert r.json()["priority"] == 2 and r.json()["active"] is False

    assert client.delete(f"/api/topics/{topic['id']}").status_code == 204
    assert [t["name"] for t in client.get("/api/topics").json()] == ["Slurm"]


def test_preferences_round_trip(client):
    prefs = client.get("/api/preferences").json()
    assert prefs["max_video_minutes"] == 10 and prefs["max_reading_minutes"] == 8
    prefs.update(max_video_minutes=5, include_unknown_length=True)
    assert client.put("/api/preferences", json=prefs).json() == prefs
    assert client.put("/api/preferences", json={**prefs, "max_video_minutes": 0}).status_code == 422


def test_feed_view_history_and_replay(client):
    topic = client.post("/api/topics?crawl=false", json={"name": "Helm", "priority": 2}).json()
    with SessionLocal() as db:
        db.add(ContentItem(topic_id=topic["id"], source="devto", content_type="reading", external_id="1",
                           url="https://dev.to/helm", title="Helm in 5 minutes", duration_seconds=300,
                           body="<p>Charts!</p>"))
        db.commit()

    feed = client.get("/api/feed").json()
    assert [i["title"] for i in feed] == ["Helm in 5 minutes"]
    item = feed[0]
    assert item["topic_priority"] == 2 and item["has_body"] is True

    assert client.post(f"/api/content/{item['id']}/view").json()["view_count"] == 1
    client.post(f"/api/content/{item['id']}/view")
    history = client.get("/api/history").json()
    assert len(history) == 2 and history[0]["item"]["id"] == item["id"]
    assert history[0]["viewed_at"].endswith("+00:00")

    assert client.get(f"/api/content/{item['id']}").json()["body"] == "<p>Charts!</p>"

    client.patch(f"/api/content/{item['id']}", json={"completed": True, "bookmarked": True})
    assert client.get("/api/feed").json() == []  # done items leave the feed...
    saved = client.get("/api/content?status=bookmarked").json()
    assert saved["total"] == 1  # ...but stay in the library for replay
    assert client.get("/api/content?q=helm").json()["total"] == 1
    assert client.get("/api/content?q=nothing").json()["total"] == 0


def test_crawl_with_no_sources_records_summary(client):
    topic = client.post("/api/topics?crawl=false", json={"name": "LLM", "priority": 3}).json()
    result = client.post(f"/api/topics/{topic['id']}/crawl").json()
    assert result == {"topic_id": topic["id"], "topic": "LLM", "added": 0, "updated": 0, "sources": []}
    listed = client.get("/api/topics").json()[0]
    assert listed["last_crawled_at"] and listed["last_crawl_summary"]["added"] == 0
    assert client.post("/api/topics/999/crawl").status_code == 404


def test_frontend_served(client):
    assert "Microlearning" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
