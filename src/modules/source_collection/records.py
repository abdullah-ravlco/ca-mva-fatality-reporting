
"""Builders for consistent source records, and the shared status rules."""

from src.core.date_utils import is_recent, iso, parse_date
from src.modules.source_collection.incident_dates import NOTE_EARLIER
from src.modules.source_collection.location import CALIFORNIA_CONFIRMED, OUTSIDE_CALIFORNIA

# Record statuses
RECENT = "recent"                                    # incident date verified inside the window
RECENT_LISTING_DATE = "recent_listing_date"          # official listing date recent, article undated
RECENT_LISTING_ONLY = "recent_listing_only"          # official listing date recent, article not fetched
DATE_UNKNOWN = "date_unknown"                        # official record without a usable date
DATE_CONFLICT_REVIEW = "date_conflict_review"        # listing and article dates disagree
INCIDENT_DATE_UNVERIFIED = "incident_date_unverified"  # story is recent, crash date not found
INCIDENT_OUTSIDE_WINDOW = "incident_outside_window"    # story is recent, crash is older
REVIEW_LOCATION = "review_location"                  # crash date fine, California not confirmed


def make_record(
    source_name,
    source_type,
    collection_method,
    title,
    url,
    collected_at,
    **fields,
):
    record = {
        "source_name": source_name,
        "source_type": source_type,
        "collection_method": collection_method,
        "title": title,
        "url": url,
        "listing_url": None,
        "feed_url": None,
        "publisher": None,
        "publisher_url": None,
        "description": None,
        "listing_published_at": None,
        "published_at": None,
        "incident_date": None,
        "incident_date_text": None,
        "incident_date_basis": None,      # explicit | relative_to_publication | relative_to_collection | official_record
        "incident_date_evidence": None,
        "case_number": None,
        "traffic_hint": None,
        "location_status": "location_unconfirmed",
        "location_evidence": None,
        "county": None,
        "place": None,
        "road": None,
        "location_text": None,
        "text_source": None,              # feed_content | article_page | none (news only)
        "status": None,
        "review_reasons": [],
        "also_reported_by": [],           # duplicates merged into this record
        "published_at_raw": None,         # publication date exactly as the feed gave it
        "published_local_date": None,     # publication date in the publisher's timezone
        "publisher_timezone": None,
        "incident_date_note": None,       # why no crash date was accepted, if relevant
        "incident_date_note_type": None,  # earlier | conflicting (see incident_dates.py)
        "page_extraction": None,          # see article_text.py (e.g. blocked, fetched)
        "fetch_error": None,
        "guid": None,                     # feed item id (Google News article id, WordPress guid, ...)
        "query": None,                    # search query of the feed that found this record
        "discovered_by_queries": [],      # every search query that returned this article
        "discovered_by_sources": [],      # every feed/source that returned this article
        "collected_at": iso(collected_at),
    }

    record.update(fields)
    if not record["discovered_by_sources"]:
        record["discovered_by_sources"] = [source_name]
    return record


REASON_NO_DATE = "crash date not found in story text"
REASON_OUTSIDE = "crash date is outside the lookback window"
REASON_LOCATION = "California location not confirmed"
BASE_REASONS = {REASON_NO_DATE, REASON_OUTSIDE, REASON_LOCATION}


def news_status(incident_date, location_status, now, hours):
    """(status, review_reasons) for a news record.

    Publication inside the window does not prove the crash was: the incident date
    found in the story text decides. Location problems are reported alongside.
    """
    reasons = []
    if incident_date is None:
        reasons.append(REASON_NO_DATE)
    elif not is_recent(incident_date, now=now, hours=hours, date_only=True):
        reasons.append(REASON_OUTSIDE)
    if location_status != CALIFORNIA_CONFIRMED:
        reasons.append(REASON_LOCATION)

    if incident_date is None:
        status = INCIDENT_DATE_UNVERIFIED
    elif REASON_OUTSIDE in reasons:
        status = INCIDENT_OUTSIDE_WINDOW
    elif location_status != CALIFORNIA_CONFIRMED:
        status = REVIEW_LOCATION
    else:
        status = RECENT
    return status, reasons


def refresh_news_status(record, now, hours):
    """Recompute a news record's status after deduplication filled in details.

    Extra reasons (blocked page, "crash happened earlier" notes) are kept. A record
    whose own text says the crash happened earlier never becomes recent, even if a
    duplicate supplied a date.
    """
    if record.get("source_type") not in ("rss", "google_news_rss"):
        return record
    says_earlier = record.get("incident_date_note_type") == NOTE_EARLIER
    incident_date = None if says_earlier else parse_date(record.get("incident_date"))
    status, reasons = news_status(incident_date, record.get("location_status"), now, hours)
    extra = [r for r in record.get("review_reasons") or [] if r not in BASE_REASONS]
    record["status"] = status
    record["review_reasons"] = reasons + extra
    return record


def is_excluded_location(location_status):
    return location_status == OUTSIDE_CALIFORNIA


def make_source_result(source_name, source_url, fetched_at, source_type):
    return {
        "source_name": source_name,
        "source_type": source_type,
        "source_url": source_url,
        "fetched_at": iso(fetched_at),
        "http_status": None,
        "error": None,
        # items_seen: entries found on the page/feed, before any filtering.
        # candidate_count: records this source produced after relevance, date and
        # location filters (before cross-source deduplication).
        # skipped (feeds): why the other items were dropped.
        "items_seen": 0,
        "candidate_count": 0,
        "skipped": {},
        "skipped_items": [],              # every item dropped by a filter, with its reason
    }


def add_example(source_result, reason, title, url=None):
    """Record one skipped item (reason, title, URL) so every filter decision is
    visible in fetched_sources.json."""
    source_result.setdefault("skipped_items", []).append({
        "reason": reason,
        "title": (title or "")[:200],
        "url": url,
    })
