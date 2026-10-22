from datetime import datetime, timezone

import pytest

from src.modules.source_collection.finalize import finalize
from src.modules.source_collection.topic import on_topic

NOW = datetime(2026, 10, 14, 18, 0, tzinfo=timezone.utc)
SINCE = datetime(2026, 10, 7, 18, 0, tzinfo=timezone.utc)  # the last run


def raw(url, title, description=None, published="2026-10-13T10:00:00+00:00"):
    return {"published_date": published, "source_name": "KTLA", "title": title, "url": url, "description": description}


@pytest.mark.parametrize("title", [
    "Motorcyclist killed in crash on 91 Freeway",
    "Pedestrian dies after being struck by a car in Fresno",
    "Driver dead after rollover on Highway 99, CHP says",
    "2 killed in head-on collision near Bakersfield",
    "Man killed in wrong-way crash on I-5",
])
def test_road_crash_stories_are_on_topic(title):
    assert on_topic(title)


@pytest.mark.parametrize("title", [
    "Bitcoin crash wipes out billions as crypto market falls",
    "Stock market crash fears grow",
    "Man killed in shooting on Main Street",
    "Plane crash kills two in Nevada",
    "City council approves new budget",
])
def test_unrelated_stories_are_not_on_topic(title):
    assert not on_topic(title)


def test_finalize_removes_same_link_off_topic_and_old():
    records = [
        raw("https://ktla.com/a", "Motorcyclist killed in crash on 91 Freeway"),
        raw("https://www.ktla.com/a/?utm_source=x", "Motorcyclist killed in crash on 91 Freeway", "Details"),
        raw("https://coin.com/x", "Bitcoin crash wipes out billions"),
        raw("https://ktla.com/old", "Driver killed in crash on I-5", published="2026-09-01T10:00:00+00:00"),
        raw("https://ktla.com/undated", "Driver killed in crash on I-5", published=None),
    ]

    kept, counts = finalize(records, NOW, SINCE)

    assert [r["url"] for r in kept] == ["https://ktla.com/a", "https://ktla.com/undated"]
    assert kept[0]["description"] == "Details"  # the fullest copy is kept
    assert counts == {"duplicate_links": 1, "not_on_topic": 1, "published_before_window": 1}


def test_history_entry_is_statistics_only():
    from src.modules.source_collection.history import build_entry

    entry = build_entry("t", 100, {"duplicate_links": 10, "not_on_topic": 30}, 55)

    assert entry["all_raw"] == 100 and entry["finalized_raw"] == 55
    assert entry["duplicate_links"] == 10 and entry["not_on_topic"] == 30 and entry["published_before_window"] == 0
    assert all(not isinstance(v, (list, dict)) for v in entry.values())


# ---- scope: outside California, statistics, low-value sources, Google links ----

from src.modules.source_collection.finalize import outside_california  # noqa: E402
from src.modules.source_collection.resolve_links import resolve_google_links  # noqa: E402
from src.modules.source_collection.source_quality import low_value_reason  # noqa: E402


def item(title, source="KTLA", description=None, url="https://example.com/x"):
    return {"published_date": None, "source_name": source, "title": title, "url": url, "description": description}


@pytest.mark.parametrize("title,source", [
    ("Calgary bus fire kills one", "CTV News"),
    ("SUV crashes into Pine Hills daycare, injuring 3 kids", "FOX 35 Orlando"),
    ("Car crashes into Central Florida daycare, hurting 3 children", "WKMG"),
    ("Speeding Driver Crashes Into Pillar on San Antonio North Side", "SuaraGarut.ID"),
    ("Crivitz man dies in Marinette County motorcycle crash", "Green Bay Press-Gazette"),
    ("Trooper hurt after crash on I-85 in Orange County", "cedarnews.net"),
])
def test_clearly_out_of_state_stories_are_outside(title, source):
    assert outside_california(item(title, source))


@pytest.mark.parametrize("title,source", [
    ("Fatal crash closes lanes on Highway 99", "KTLA"),                      # no location at all: kept
    ("Utah man identified in deadly motorcycle crash in Kern River Valley", "Yahoo"),
    ("Crash on Arizona Avenue in Santa Monica kills pedestrian", "KTLA"),
    ("Man killed in Orange County crash, CHP says", "WKMG"),                 # explicit California signal wins
    ("Texas driver killed in I-5 crash near Bakersfield", "KGET.com"),
    ("Killed In Fresno County Crash", "ABC30"),
])
def test_possible_california_stories_are_not_removed(title, source):
    assert not outside_california(item(title, source))


@pytest.mark.parametrize("title", [
    "Traffic deaths rise statewide, new report shows",
    "California's deadliest roads ranked",
    "Roller coaster rider's death puts spotlight on ride safety",
    "Six killed in Wisconsin plane crash",
    "Passenger train derails in Pakistan, killing at least 4",
])
def test_statistics_and_other_deaths_are_off_topic(title):
    assert not on_topic(title)


def test_fatal_crash_without_road_word_is_on_topic():
    assert on_topic("Two killed in rural Davis crashes")
    assert on_topic("LASD deputy hospitalized after fatal Commerce crash")


@pytest.mark.parametrize("name,url,reason", [
    ("Pacific Attorney Group", "https://news.google.com/rss/articles/x", "law_firm"),
    ("contreras-law.com", "https://contreras-law.com/a", "law_firm"),
    ("Culver Legal", "https://news.google.com/rss/articles/x", "law_firm"),
    ("Facebook", "https://news.google.com/rss/articles/x", "social_media"),
    ("instagram.com", "https://www.instagram.com/p/1", "social_media"),
    ("NewsBreak: Local News & Alerts", "https://news.google.com/rss/articles/x", "aggregator"),
    ("KTLA", "https://ktla.com/a", None),
    ("Yahoo", "https://news.google.com/rss/articles/x", None),
    ("Lawndale Gazette", "https://lawndalegazette.com/a", None),
])
def test_source_quality(name, url, reason):
    assert low_value_reason(name, url) == reason


def test_google_links_are_resolved_and_failures_keep_the_original(tmp_path, monkeypatch):
    from src.modules.link_extraction.google_news_decoder import GoogleNewsDecodeError
    from src.modules.source_collection import resolve_links

    monkeypatch.setattr(resolve_links, "CACHE_FILE", tmp_path / "google_links.json")
    ok = item("a", url="https://news.google.com/rss/articles/OK?oc=5")
    bad = item("b", url="https://news.google.com/rss/articles/BAD?oc=5")
    direct = item("c", url="https://ktla.com/c")

    def decode(url):
        if "BAD" in url:
            raise GoogleNewsDecodeError("nope")
        return "https://ktla.com/ok-story"

    assert resolve_google_links([ok, bad, direct], decode=decode) == (1, 1)
    assert ok["url"] == "https://ktla.com/ok-story"
    assert bad["url"].startswith("https://news.google.com/")
    assert direct["url"] == "https://ktla.com/c"

    # second run: answered from the cache, no decoding
    again = item("a", url="https://news.google.com/rss/articles/OK?oc=5")
    resolve_google_links([again], decode=lambda url: pytest.fail("should use the cache"))
    assert again["url"] == "https://ktla.com/ok-story"


def test_finalize_keeps_only_stories_inside_the_window():
    end = datetime(2026, 10, 1, 7, 0, tzinfo=timezone.utc)  # the window ends here
    records = [
        raw("https://a.com/sept", "Driver killed in crash on I-5", published="2026-09-15T10:00:00+00:00"),
        raw("https://a.com/aug", "Driver killed in crash on I-5", published="2026-08-20T10:00:00+00:00"),
        raw("https://a.com/oct", "Driver killed in crash on I-5", published="2026-10-05T10:00:00+00:00"),
    ]
    kept, counts = finalize(records, end, datetime(2026, 9, 1, 7, 0, tzinfo=timezone.utc))
    assert [r["url"] for r in kept] == ["https://a.com/sept"]
    assert counts["published_before_window"] == 2  # before the period, or after it


def test_google_copy_of_a_story_that_has_a_direct_link_is_dropped_before_resolving():
    from src.modules.source_collection.finalize import drop_google_twins

    google = "https://news.google.com/rss/articles/CBMiX"
    records = [
        item("2 killed in wrong-way crash", "KTLA", url="https://ktla.com/a"),
        item("2 killed in wrong-way crash", "KTLA", url=google),                 # same story, same outlet
        item("2 killed in wrong-way crash", "ABC7", url=google + "2"),           # other outlet: kept
        item("Another crash", "KTLA", url=google + "3"),                         # no direct copy: kept
    ]
    kept, dropped = drop_google_twins(records)
    assert dropped == 1 and [r["url"] for r in kept] == ["https://ktla.com/a", google + "2", google + "3"]
