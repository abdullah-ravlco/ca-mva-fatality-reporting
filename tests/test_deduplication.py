from datetime import datetime, timezone

from src.modules.source_collection.deduplication import canonical_url, deduplicate_records
from src.modules.source_collection.records import make_record

NOW = datetime(2026, 10, 12, 20, 0, tzinfo=timezone.utc)
TITLE = "Motorcyclist killed in crash on 91 Freeway in Riverside"


def news(url, publisher, title=TITLE, queries=(), source_name="feed"):
    return make_record(
        source_name=source_name,
        source_type="google_news_rss",
        collection_method="rss",
        title=title,
        url=url,
        collected_at=NOW,
        publisher=publisher,
        discovered_by_queries=list(queries),
    )


def test_canonical_url_strips_tracking_and_normalizes():
    url = "https://www.KTLA.com/news/crash/?utm_source=x&fbclid=1&oc=5&id=3#comments"
    assert canonical_url(url) == "https://ktla.com/news/crash?id=3"


def test_same_url_from_two_queries_keeps_both_queries():
    a = news("https://news.google.com/rss/articles/ABC?oc=5", "KTLA", queries=["q1"], source_name="Google News: q1")
    b = news("https://news.google.com/rss/articles/ABC", "KTLA", queries=["q2"], source_name="Google News: q2")

    unique = deduplicate_records([a, b])

    assert len(unique) == 1
    assert unique[0]["discovered_by_queries"] == ["q1", "q2"]
    assert set(unique[0]["discovered_by_sources"]) == {"Google News: q1", "Google News: q2"}


def test_similar_headlines_from_different_publishers_are_not_merged():
    a = news("https://ktla.com/a", "KTLA")
    b = news("https://abc7.com/b", "ABC7", title=TITLE + ", CHP says")

    assert len(deduplicate_records([a, b])) == 2


def test_same_publisher_near_duplicate_is_merged_and_referenced():
    a = news("https://ktla.com/a", "KTLA", queries=["q1"])
    b = news("https://news.google.com/rss/articles/XYZ", "KTLA", title=TITLE + ", CHP says", queries=["q2"])

    unique = deduplicate_records([a, b])

    assert len(unique) == 1
    assert unique[0]["url"] == "https://ktla.com/a"  # direct URL preferred over Google redirect
    assert [ref["url"] for ref in unique[0]["also_reported_by"]] == ["https://news.google.com/rss/articles/XYZ"]
    assert unique[0]["discovered_by_queries"] == ["q1", "q2"]
