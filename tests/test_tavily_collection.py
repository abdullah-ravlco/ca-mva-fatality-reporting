from datetime import datetime, timezone

from src.modules.source_collection.article_text import ArticleTextFetcher
from src.modules.source_collection.collectors.rss import records_from_items
from src.modules.source_collection.collectors.tavily import collect_tavily_sources, search_feeds, to_item
from src.modules.source_collection.records import make_source_result

NOW = datetime(2026, 10, 14, 18, 0, tzinfo=timezone.utc)
FEED = search_feeds()[0]
STORY = (
    "A pedestrian was struck and killed in a crash on Oct. 12 in Fresno, California, "
    "the California Highway Patrol said. " * 10
)


def test_tavily_result_becomes_a_news_record():
    item = to_item({
        "title": "Pedestrian killed in crash in Fresno",
        "url": "https://www.abc30.com/post/fresno-pedestrian-killed/123/",
        "content": "A pedestrian was killed in a crash in Fresno.",
        "published_date": "Mon, 13 Oct 2026 15:00:00 GMT",
        "raw_content": STORY,
    })
    result = make_source_result(FEED["name"], FEED["url"], NOW, FEED["kind"])
    # No client and no fetch budget: raw_content alone must be enough.
    records, seen, skipped = records_from_items(FEED, [item], "Tavily", NOW, 168, ArticleTextFetcher(None, 0), result)

    assert seen == 1 and not skipped
    record = records[0]
    assert record["source_type"] == "tavily_search"
    assert record["collection_method"] == "tavily_search"
    assert record["publisher"] == "abc30.com"
    assert record["query"] == FEED["query"]
    assert record["published_at"].startswith("2026-10-13")
    assert record["incident_date"].startswith("2026-10-12")
    assert record["text_source"] == "feed_content"
    assert record["status"] == "recent"


def test_missing_api_key_skips_tavily():
    assert collect_tavily_sources(client=None, now=NOW, api_key=None) == ([], [])
