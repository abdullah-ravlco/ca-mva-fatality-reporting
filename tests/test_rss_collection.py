from datetime import datetime, timezone

from src.modules.source_collection.collectors.rss import records_from_feed
from src.modules.source_collection.records import INCIDENT_DATE_UNVERIFIED, make_source_result

NOW = datetime(2026, 10, 14, 18, 0, tzinfo=timezone.utc)
SINCE = datetime(2026, 10, 7, 18, 0, tzinfo=timezone.utc)  # the last run
FEED = {"name": "Test Feed", "url": "https://news.example/feed", "kind": "rss"}


class NoPageFetcher:
    def text_for(self, item, url):
        return {"text": "", "text_source": "none", "page_extraction": "blocked", "fetch_error": "403"}


def rss(*items):
    body = "".join(items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>Test</title>{body}</channel></rss>'.encode()


def item(title, pub_date=None, description=""):
    date = f"<pubDate>{pub_date}</pubDate>" if pub_date else ""
    return (f"<item><title>{title}</title><link>https://news.example/{abs(hash(title))}</link>"
            f"<description>{description}</description>{date}</item>")


def collect(content):
    result = make_source_result("Test Feed", FEED["url"], NOW, "rss")
    records, _, skipped = records_from_feed(FEED, content, NOW, SINCE, NoPageFetcher(), result)
    return records, skipped


def test_item_without_publication_date_is_kept_for_review():
    records, skipped = collect(rss(item("Motorcyclist killed in crash on Highway 132 in Stanislaus County, CHP says")))

    assert len(records) == 1 and not skipped
    record = records[0]
    assert record["published_at"] is None
    assert record["status"] == INCIDENT_DATE_UNVERIFIED
    assert any("publication date missing" in reason for reason in record["review_reasons"])


def test_undated_item_with_a_written_out_crash_date_is_confirmed():
    records, _ = collect(rss(item(
        "Pedestrian killed in crash in Fresno",
        description="The pedestrian was struck and killed in a crash on Oct. 12 in Fresno, California.",
    )))

    assert records[0]["incident_date"].startswith("2026-10-12")
    assert records[0]["status"] == "recent"


def test_undated_item_does_not_resolve_weekday_phrases():
    records, _ = collect(rss(item(
        "Driver killed in crash in Fresno",
        description="A driver was killed in a crash Saturday night in Fresno, California.",
    )))

    assert records[0]["incident_date"] is None


def test_item_published_before_the_window_is_still_skipped():
    records, skipped = collect(rss(item(
        "Motorcyclist killed in crash on Highway 132 in Stanislaus County, CHP says",
        pub_date="Mon, 01 Sep 2026 12:00:00 GMT",
    )))

    assert not records
    assert skipped["published_outside_window"] == 1
