from src.modules.source_collection.final_selection import CONSIDER, DISCARD, REVIEW, classify
from src.modules.source_collection.incident_dates import NOTE_CONFLICTING, NOTE_EARLIER
from src.modules.source_collection.records import INCIDENT_DATE_UNVERIFIED, INCIDENT_TOO_OLD, RECENT


def record(note_type):
    return {
        "source_type": "google_news_rss",
        "status": INCIDENT_DATE_UNVERIFIED,
        "incident_date_note": "note",
        "incident_date_note_type": note_type,
        "review_reasons": ["crash date not found in story text"],
    }


def test_conflicting_dates_go_to_review():
    assert classify(record(NOTE_CONFLICTING))[0] == REVIEW


def test_follow_up_story_about_an_earlier_crash_goes_to_review():
    # incident grouping decides whether the crash is already known
    assert classify(record(NOTE_EARLIER))[0] == REVIEW


def test_crash_older_than_the_age_limit_is_discarded_and_a_recent_one_considered():
    assert classify({"source_type": "google_news_rss", "status": INCIDENT_TOO_OLD})[0] == DISCARD
    assert classify({"source_type": "google_news_rss", "status": RECENT})[0] == CONSIDER
