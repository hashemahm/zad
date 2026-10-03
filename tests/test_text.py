from app.crawlers.base import (
    CrawledItem,
    assign_relevance,
    extract_page_info,
    is_youtube_url,
    matches_query,
    parse_iso8601_duration,
    reading_seconds,
    sanitize_html,
)


def test_parse_iso8601_duration():
    assert parse_iso8601_duration("PT4M13S") == 253
    assert parse_iso8601_duration("PT1H2M3S") == 3723
    assert parse_iso8601_duration("PT45S") == 45
    assert parse_iso8601_duration("P1DT1M") == 86460
    assert parse_iso8601_duration("P0D") == 0
    assert parse_iso8601_duration("garbage") is None
    assert parse_iso8601_duration(None) is None


def test_reading_seconds_rounds_up_to_whole_minutes():
    assert reading_seconds(0, 230) is None
    assert reading_seconds(10, 230) == 60
    assert reading_seconds(231, 230) == 120


def test_sanitize_html_strips_scripts_and_handlers():
    html = '<p onclick="x()">Hi <script>alert(1)</script><a href="javascript:alert(1)">bad</a><a href="https://ok">ok</a></p>'
    clean = sanitize_html(html)
    assert "script" not in clean and "onclick" not in clean and "javascript:" not in clean
    assert 'href="https://ok"' in clean


def test_extract_page_info_ignores_chrome_and_reads_meta():
    html = """<html><head><meta property="og:image" content="https://img/x.png"><style>.a{}</style></head>
    <body><nav>menu menu menu</nav><article><p>one two three four</p></article><script>var a=1</script></body></html>"""
    words, meta = extract_page_info(html)
    assert words == 4
    assert meta["og:image"] == "https://img/x.png"


def test_matches_query_rejects_typo_matches():
    assert matches_query("Slurm", "Running jobs with Slurm on HPC")
    assert not matches_query("Slurm", "The post-YC slump")
    assert matches_query("Conflict Resolution", "Conflict", "a guide to resolution")
    assert matches_query("LLM", "Self-hosting LLMs")


def test_is_youtube_url():
    assert is_youtube_url("https://www.youtube.com/watch?v=x")
    assert is_youtube_url("https://youtu.be/x")
    assert not is_youtube_url("https://notyoutube.com/x")


def test_assign_relevance_ranks_by_popularity():
    items = [CrawledItem("s", "reading", str(i), f"u{i}", "t", popularity=p) for i, p in enumerate([5, 50, 10])]
    assign_relevance(items)
    assert [round(i.relevance, 2) for i in items] == [0.33, 1.0, 0.67]
