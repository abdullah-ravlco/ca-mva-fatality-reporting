from datetime import date, datetime, timezone

from src.modules.source_collection.incident_dates import (
    LOCAL_TZ,
    NOTE_CONFLICTING,
    NOTE_EARLIER,
    find_incident_date,
)

# Monday, Oct. 12, 2026 in California.
PUBLISHED = datetime(2026, 10, 12, 18, 0, tzinfo=timezone.utc)


def local_day(result):
    return result["incident_date"].astimezone(LOCAL_TZ).date()


def test_weekday_resolved_against_publication_date():
    result = find_incident_date("A man was killed in a crash Saturday night on Highway 99.", PUBLISHED)
    assert local_day(result) == date(2026, 10, 10)
    assert result["incident_date_basis"] == "relative_to_publication"


def test_explicit_date():
    result = find_incident_date("The crash happened on Oct. 9, 2026, when a driver struck a pedestrian.", PUBLISHED)
    assert local_day(result) == date(2026, 10, 9)
    assert result["incident_date_basis"] == "explicit"


def test_earlier_crash_note_type():
    result = find_incident_date("The crash that killed him happened in February.", PUBLISHED)
    assert result["incident_date"] is None
    assert result["incident_date_note_type"] == NOTE_EARLIER


def test_conflicting_dates_note_type():
    text = "A driver was killed in a crash on Oct. 1. Another man died in a collision on Oct. 9."
    result = find_incident_date(text, PUBLISHED)
    assert result["incident_date"] is None
    assert result["incident_date_note_type"] == NOTE_CONFLICTING


def test_arrest_sentence_is_not_the_crash_date():
    text = "A pedestrian was killed in a crash Friday. The driver who hit her was arrested Sunday."
    result = find_incident_date(text, PUBLISHED)
    assert local_day(result) == date(2026, 10, 9)
