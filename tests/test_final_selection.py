from src.modules.source_collection.final_selection import DISCARD, REVIEW, classify
from src.modules.source_collection.incident_dates import NOTE_CONFLICTING, NOTE_EARLIER
from src.modules.source_collection.records import INCIDENT_DATE_UNVERIFIED


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


def test_earlier_crash_is_discarded():
    assert classify(record(NOTE_EARLIER))[0] == DISCARD
