from datetime import datetime, timezone

from src.modules.link_extraction.full_text_filter import DROP, KEEP, apply_full_text_filter

NOW = datetime(2026, 10, 12, 20, 0, tzinfo=timezone.utc)
PUBLISHED = "2026-10-12T15:00:00+00:00"


def article(title, text, description=None):
    return {
        "title": title,
        "text": text,
        "description": description,
        "published_at": PUBLISHED,
        "source_type": "google_news_rss",
        "metadata": {},
    }


def test_story_published_now_about_an_older_crash_is_kept():
    # the last run started Oct. 5; a story published Oct. 12 about a Sept. 1 crash is kept for grouping
    result = apply_full_text_filter(article(
        "Driver killed in Fresno crash",
        "A driver was killed in a crash on Sept. 1, 2026 on Highway 99 in Fresno. Police said the investigation continues.",
    ), NOW)
    assert result["filter_decision"] == KEEP
    assert result["incident_date"].startswith("2026-09-01")


def test_crash_older_than_the_age_limit_is_dropped():
    result = apply_full_text_filter(article(
        "Driver killed in Fresno crash",
        "A driver was killed in a crash on Sept. 1, 2023 on Highway 99 in Fresno. Police said the investigation continues.",
    ), NOW)
    assert result["filter_decision"] == DROP
    assert result["incident_date"].startswith("2023-09-01")


def test_follow_up_story_about_an_earlier_crash_is_kept():
    result = apply_full_text_filter(article(
        "Arrest made in Fresno crash",
        "Police arrested a driver Monday in connection with last month's crash that killed a pedestrian on Highway 99 in Fresno.",
    ), NOW)
    assert result["filter_decision"] == KEEP


def test_out_of_state_crash_is_dropped():
    result = apply_full_text_filter(article(
        "Pedestrian killed in crash",
        "A pedestrian was killed in a crash Saturday in Phoenix, Arizona, police said.",
    ), NOW)
    assert result["filter_decision"] == DROP
    assert result["location_status"] == "outside_california"


def test_undetermined_article_is_kept():
    result = apply_full_text_filter(article(
        "Motorcyclist killed in crash",
        "A motorcyclist was killed in a crash on a rural road, police said.",
    ), NOW)
    assert result["filter_decision"] == KEEP
    assert result["filter_basis"] == "article_text"


def test_article_without_text_is_judged_on_rss_metadata_and_kept():
    result = apply_full_text_filter(article(
        "Motorcyclist killed in crash", None, description="A motorcyclist died after a collision.",
    ), NOW)
    assert result["filter_decision"] == KEEP
    assert result["filter_basis"] == "rss_metadata"
