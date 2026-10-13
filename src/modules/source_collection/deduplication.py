
"""Remove duplicate ARTICLES (the same story found more than once).

Order of checks:
1. Canonical URL (tracking parameters and fragments removed) - exact duplicates, e.g.
   the same Google News item returned by several queries.
2. Similar headlines from the SAME publisher - e.g. the feed copy and the Google News
   copy of one story - only when the incident details found so far (crash date,
   county, city) do not contradict each other.

Different publishers reporting the same crash are NOT merged here: that is
incident-level grouping (case_grouping.py), which keeps every article.

Every query and source that found an article is kept in discovered_by_queries /
discovered_by_sources; other URLs of a merged duplicate are listed in "also_reported_by".
"""

from datetime import timedelta
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from src.core.date_utils import parse_date
from src.modules.source_collection.filters import normalize_title
from src.modules.source_collection.incident_dates import NOTE_EARLIER


TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "oc",        # Google News adds ?oc=5 to article links
    "ocid",
    "ref",
    "referrer",
    "taid",
    "cmpid",
}

# Fields copied from a duplicate when the kept record lacks them.
FILLABLE_FIELDS = [
    "incident_date", "incident_date_text", "incident_date_basis", "incident_date_evidence",
    "county", "place", "road", "location_text", "location_evidence", "description",
]
MAX_INCIDENT_DATE_GAP = timedelta(days=1)


def canonical_url(url):
    """Remove fragments and common tracking parameters from URLs."""
    if not url:
        return ""

    try:
        parts = urlsplit(url.strip())

        query = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_")
            and key.lower() not in TRACKING_PARAMS
        ]

        netloc = parts.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]

        return urlunsplit((
            parts.scheme.lower(),
            netloc,
            parts.path.rstrip("/"),
            urlencode(query),
            "",
        ))
    except (TypeError, ValueError):
        return url.strip()


def title_tokens(title):
    """Return meaningful normalized words from a headline."""
    ignored = {
        "the", "a", "an", "and", "or", "in", "on", "at", "near",
        "of", "to", "for", "after", "with", "as", "is", "are",
        "was", "were", "that", "this", "by",
    }

    return {
        word
        for word in normalize_title(title).split()
        if word not in ignored and len(word) > 1
    }


def similar_headlines(title_a, title_b):
    """
    Conservative cross-publisher headline comparison.

    Requiring several shared words helps avoid merging unrelated stories
    with short or generic headlines.
    """
    a = normalize_title(title_a)
    b = normalize_title(title_b)

    if not a or not b:
        return False

    if a == b:
        return True

    tokens_a = title_tokens(a)
    tokens_b = title_tokens(b)

    if min(len(tokens_a), len(tokens_b)) < 4:
        return False

    shared = len(tokens_a & tokens_b)
    overlap = shared / min(len(tokens_a), len(tokens_b))
    sequence_similarity = SequenceMatcher(None, a, b).ratio()

    return overlap >= 0.80 or (
        overlap >= 0.65 and sequence_similarity >= 0.82
    )


def details_conflict(record_a, record_b):
    """True when known incident details show two stories are different crashes."""
    date_a = parse_date(record_a.get("incident_date"))
    date_b = parse_date(record_b.get("incident_date"))
    if date_a and date_b and abs(date_a - date_b) > MAX_INCIDENT_DATE_GAP:
        return True

    for field in ("county", "place"):
        value_a, value_b = record_a.get(field), record_b.get(field)
        if value_a and value_b and value_a.lower() != value_b.lower():
            return True

    return False


def is_news_record(record):
    return record.get("source_type") in {"rss", "google_news_rss"}


def publisher_key(record):
    """Normalized publisher name, or the publisher's host when no name is known."""
    name = (record.get("publisher") or "").strip().lower()
    if name:
        return name
    for field in ("publisher_url", "url"):
        host = urlsplit(record.get(field) or "").netloc.lower()
        if host and host != "news.google.com":
            return host[4:] if host.startswith("www.") else host
    return None


def same_publisher(record_a, record_b):
    key_a, key_b = publisher_key(record_a), publisher_key(record_b)
    return key_a is not None and key_a == key_b


def record_quality(record):
    """
    Prefer a direct publisher URL over a Google News redirect, then records
    with a verified crash date and confirmed location.
    """
    url = (record.get("url") or "").lower()
    google_redirect = "news.google.com" in url

    return (
        not google_redirect,
        record.get("incident_date") is not None,
        record.get("location_status") == "california_confirmed",
        bool(record.get("description")),
        bool(record.get("publisher")),
    )


def _reference(record):
    return {
        "url": record.get("url"),
        "title": record.get("title"),
        "description": record.get("description"),
        "publisher": record.get("publisher"),
        "source_name": record.get("source_name"),
        "published_at": record.get("published_at"),
        "guid": record.get("guid"),
        "discovered_by_queries": list(record.get("discovered_by_queries") or []),
    }


def _union(kept, dropped, field):
    values = kept.setdefault(field, [])
    for value in dropped.get(field) or []:
        if value not in values:
            values.append(value)


def merge_into(kept, dropped):
    """Record the dropped duplicate on the kept record and fill missing details from it."""
    # Provenance is kept even when both copies have the same URL.
    _union(kept, dropped, "discovered_by_queries")
    _union(kept, dropped, "discovered_by_sources")

    references = kept.setdefault("also_reported_by", [])
    known_urls = {kept.get("url")} | {ref.get("url") for ref in references}

    for ref in [_reference(dropped)] + list(dropped.get("also_reported_by") or []):
        if ref.get("url") not in known_urls:
            references.append(ref)
            known_urls.add(ref.get("url"))

    for field in FILLABLE_FIELDS:
        # Don't give a date to a story whose own text says the crash happened earlier.
        if field.startswith("incident_date") and kept.get("incident_date_note_type") == NOTE_EARLIER:
            continue
        if kept.get(field) in (None, "") and dropped.get(field) not in (None, ""):
            kept[field] = dropped[field]

    if kept.get("location_status") != "california_confirmed" and dropped.get("location_status") == "california_confirmed":
        kept["location_status"] = "california_confirmed"
    # Status is recomputed after deduplication (records.refresh_news_status).
    return kept


def deduplicate_records(records):
    """
    Remove exact URL duplicates and near-duplicate copies from the same publisher.

    Similar headlines from different publishers are left alone: they are separate
    articles, grouped into one incident later by case_grouping.py.
    """
    unique = []
    seen_urls = {}
    news_indexes = []

    for record in records:
        url_key = canonical_url(record.get("url"))

        # Ignore records without a usable URL.
        if not url_key:
            continue

        # First remove exact/canonical URL duplicates.
        if url_key in seen_urls:
            index = seen_urls[url_key]
            unique[index] = _keep_better(unique[index], record)
            continue

        duplicate_index = None

        # Near-duplicates: news records from the same publisher only (not official
        # records such as distinct coroner case entries).
        if is_news_record(record):
            for index in news_indexes:
                existing = unique[index]

                if (
                    same_publisher(record, existing)
                    and similar_headlines(record.get("title"), existing.get("title"))
                    and not details_conflict(record, existing)
                ):
                    duplicate_index = index
                    break

        if duplicate_index is not None:
            kept = _keep_better(unique[duplicate_index], record)
            unique[duplicate_index] = kept
            seen_urls[url_key] = duplicate_index
            continue

        new_index = len(unique)
        unique.append(record)
        seen_urls[url_key] = new_index

        if is_news_record(record):
            news_indexes.append(new_index)

    return unique


def _keep_better(existing, candidate):
    """Return the better of two duplicates, with the other merged into it."""
    if record_quality(candidate) > record_quality(existing):
        return merge_into(candidate, existing)
    return merge_into(existing, candidate)
