
"""Split collected records into the final list to consider, a short review list,
and discarded items (counted by reason, not saved).

consider  - in-window California road death backed by an official record or a
            crash date found in the story text
review    - possibly relevant but not yet verified (crash date or cause unknown)
discard   - known not to be needed (old crash, not a road death)
"""

from collections import Counter

from src.modules.source_collection.incident_dates import NOTE_EARLIER
from src.modules.source_collection.records import (
    DATE_CONFLICT_REVIEW, DATE_UNKNOWN, INCIDENT_DATE_UNVERIFIED, INCIDENT_OUTSIDE_WINDOW,
    RECENT, RECENT_LISTING_DATE, RECENT_LISTING_ONLY, REVIEW_LOCATION,
)


CONSIDER = "consider"
REVIEW = "review"
DISCARD = "discard"


def classify(record):
    """(bucket, reason) for one record."""
    source_type = record.get("source_type")
    status = record.get("status")

    if source_type == "coroner":
        if record.get("traffic_hint") is False:
            return DISCARD, "coroner case is not a road death"
        if status == RECENT and record.get("traffic_hint"):
            return CONSIDER, "official coroner record of a road death in the window"
        if status == RECENT:
            return REVIEW, "coroner record in the window; cause of death not stated"
        return REVIEW, "coroner record; incident date not readable"

    if source_type == "official_listing":
        if status in (RECENT, RECENT_LISTING_DATE, RECENT_LISTING_ONLY):
            return CONSIDER, "official crash release published in the window"
        return REVIEW, f"official release: {status}"

    # News (publisher RSS / Google News)
    if status == RECENT:
        return CONSIDER, "crash date in story text is inside the window"
    if status == INCIDENT_OUTSIDE_WINDOW:
        return DISCARD, "crash happened before the window"
    if status == INCIDENT_DATE_UNVERIFIED and record.get("incident_date_note_type") == NOTE_EARLIER:
        # e.g. "arrest made in February crash": the story itself says it's old.
        # Conflicting dates in the story are uncertain, not old: those go to review.
        return DISCARD, "story says the crash happened earlier"
    if status in (INCIDENT_DATE_UNVERIFIED, REVIEW_LOCATION, DATE_UNKNOWN, DATE_CONFLICT_REVIEW):
        return REVIEW, ", ".join(record.get("review_reasons") or [status])
    return REVIEW, f"unrecognized status: {status}"


def select_records(records):
    """Return (consider, review, discarded_counts_by_reason)."""
    consider, review = [], []
    discarded = Counter()

    for record in records:
        bucket, reason = classify(record)
        record["selection_bucket"] = bucket
        record["selection_reason"] = reason
        if bucket == CONSIDER:
            consider.append(record)
        elif bucket == REVIEW:
            review.append(record)
        else:
            discarded[reason] += 1

    return consider, review, dict(discarded)
