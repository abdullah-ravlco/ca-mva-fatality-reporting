"""Turn all_raw.json into finalized_raw.json.

1. Same link: records with the same canonical URL are merged into one.
2. Not on topic: stories that are not about a deadly road crash are removed
   (see topic.py). Where the crash happened is NOT checked.
3. Too old: a story published before the window start is removed. A story without a
   publication date is kept.
4. Low-value source: law-firm marketing, social media posts and unverifiable aggregators
   are removed (see source_quality.py).
5. Outside California: removed only when the text (headline, description, outlet name)
   names another state/city/country and nothing points to California. A story with no
   location at all is kept. See location.py.
"""

from collections import Counter

from src.core.date_utils import is_recent, parse_date
from src.modules.link_extraction.google_news_decoder import is_google_news_url
from src.modules.source_collection.deduplication import canonical_url
from src.modules.source_collection.filters import normalize_title
from src.modules.source_collection.location import (
    OUTSIDE_CALIFORNIA, detect_location, has_california_indicator, is_other_state_outlet,
)
from src.modules.source_collection.source_quality import low_value_reason
from src.modules.source_collection.topic import on_topic


def remove_same_link(records):
    """(unique_records, dropped) - dropped is a list of the removed records."""
    unique, index, dropped = [], {}, []
    for record in records:
        key = canonical_url(record.get("url"))
        if not key:
            dropped.append(record)
            continue
        if key not in index:
            index[key] = len(unique)
            unique.append(dict(record))
            continue
        kept = unique[index[key]]
        for field, value in record.items():  # keep the fullest copy
            if not kept.get(field) and value:
                kept[field] = value
        dropped.append(record)
    return unique, dropped


def drop_known(records, known):
    """(records, dropped_count): items whose link is already in `known` (the items of earlier runs).
    A Google News link can only be compared after it is resolved, so it passes here."""
    seen = {canonical_url(r.get("url")) for r in known}
    kept = [r for r in records if is_google_news_url(r.get("url")) or canonical_url(r.get("url")) not in seen]
    return kept, len(records) - len(kept)


def _story_key(record):
    outlet = (record.get("source_name") or "").lower().removeprefix("www.").split(".")[0]
    return normalize_title(record.get("title")), outlet


def drop_google_twins(records):
    """(records, dropped): a Google News item with the same headline and outlet as an item that
    already has the publisher's own link is the same article, so the Google copy is dropped
    before its link is looked up (each lookup takes seconds)."""
    direct = {_story_key(r) for r in records if not is_google_news_url(r.get("url"))}
    kept = [r for r in records if not is_google_news_url(r.get("url")) or _story_key(r) not in direct]
    return kept, len(records) - len(kept)


def outside_california(record):
    """True only with positive evidence of another place; unknown locations are kept."""
    text = f"{record.get('title') or ''} {record.get('description') or ''}"
    source = record.get("source_name")
    if detect_location(text, source, residents=False)["location_status"] == OUTSIDE_CALIFORNIA:
        return True
    return is_other_state_outlet(source) and not has_california_indicator(text)


def finalize(records, now, since):
    """(finalized_records, counts) after the same-link, source, topic, location and age filters."""
    unique, same_link = remove_same_link(records)
    counts = Counter(duplicate_links=len(same_link))

    kept = []
    for record in unique:
        reason = low_value_reason(record.get("source_name"), record.get("url"))
        if reason:
            counts[f"source_{reason}"] += 1
            continue
        if not on_topic(record.get("title"), record.get("description")):
            counts["not_on_topic"] += 1
            continue
        if outside_california(record):
            counts["outside_california"] += 1
            continue
        published = parse_date(record.get("published_date"))
        if published is not None and not is_recent(published, since, now=now):
            counts["published_before_window"] += 1
            continue
        kept.append(record)
    return kept, dict(counts)
