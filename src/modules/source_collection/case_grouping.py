
"""Group records into cases (one crash = one case).

Used twice: on feed records for sources_to_open.json (headline-level facts), and
on fetched articles by incident_grouping (full-text facts).

Two records are the same case only when nothing known about them conflicts
(crash date, place, county, case number) AND there is positive evidence they match:
similar headlines, the same place/road with the same crash date, the same
place/road with strongly overlapping headline words, or strongly overlapping story
text. When both records have article text, a shared place/road also needs some
overlap in that text.

Victim type and age are NOT conflicts: one crash can have several victims (a
driver and a passenger, two pedestrians), and different articles may describe
different victims of the same crash.

Records are added one by one (best evidence first); a record joins the first case
it matches against every member, otherwise it starts a new case. This avoids
chaining two different crashes together through a third record. The reason each
record joined is recorded.
"""

import hashlib
import re

from src.core.date_utils import parse_date
from src.modules.source_collection.deduplication import canonical_url, similar_headlines, title_tokens
from src.modules.source_collection.final_selection import CONSIDER
from src.modules.source_collection.location import CITY_RE, COUNTY_RE


MAX_DATE_GAP_DAYS = 1
MIN_TITLE_OVERLAP = 0.5   # with a shared place/road but a missing crash date
FACT_TEXT_CHARS = 2000    # story text used for victim/age/place facts
SIMILARITY_TEXT_CHARS = 1000
MIN_SIMILARITY_TOKENS = 40
MIN_TEXT_OVERLAP = 0.5            # story text alone is enough
CORROBORATING_TEXT_OVERLAP = 0.36  # with a shared place/road (calibrated on the 2026-10-13 run)

VICTIM_TYPES = [
    ("motorcyclist", re.compile(r"motorcycl\w*|motorbike", re.I)),
    ("bicyclist", re.compile(r"bicycl\w*|cyclist|\bbike\b|e-bike", re.I)),
    ("pedestrian", re.compile(r"pedestrian|\bwalking\b|crosswalk", re.I)),
    ("passenger", re.compile(r"\bpassenger\b", re.I)),
    ("driver", re.compile(r"\bdriver\b|\bmotorist\b", re.I)),
]
AGE_RE = re.compile(
    r"\b(\d{1,3})-year-old\b|,\s(\d{1,2}),\s|\bAge:\s*(\d{1,3})\b|\b(\d{1,3})\s+years?\s+old\b",
    re.I,
)
ROAD_NUMBER_RE = re.compile(r"\d{1,3}")
TEXT_TOKEN_RE = re.compile(r"[a-z0-9]+")
COMMON_WORDS = {
    "the", "a", "an", "and", "or", "in", "on", "at", "near", "of", "to", "for", "after",
    "with", "as", "is", "are", "was", "were", "that", "this", "by", "he", "she", "his",
    "her", "they", "their", "said", "says", "from", "who", "be", "been", "had", "has",
    "have", "it", "its", "not", "but", "about", "according",
}

# Doubtful stories that are about an earlier case's aftermath are not worth opening.
FOLLOW_UP_HEADLINE_RE = re.compile(
    r"\b(sentenced|sentencing|convicted|pleads?|pleaded|trial|verdict|lawsuit|sues|"
    r"settlement|anniversary|years after|remembered|memorial)\b",
    re.I,
)


# ---------------------------------------------------------------- record facts

def _text(record):
    return " ".join(
        part for part in (
            record.get("title"),
            record.get("description"),
            record.get("incident_date_evidence"),
            (record.get("text") or "")[:FACT_TEXT_CHARS],
        ) if part
    )


def victim_types(record):
    text = _text(record)
    return {name for name, pattern in VICTIM_TYPES if pattern.search(text)}


def victim_ages(record):
    ages = set()
    for match in AGE_RE.finditer(_text(record)):
        age = int(next(group for group in match.groups() if group))
        if 0 < age < 120:
            ages.add(age)
    return ages


def victim_type(record):
    """First victim type mentioned (summary field of a case)."""
    text = _text(record)
    for name, pattern in VICTIM_TYPES:
        if pattern.search(text):
            return name
    return None


def victim_age(record):
    """First age mentioned (summary field of a case)."""
    match = AGE_RE.search(_text(record))
    if not match:
        return None
    age = int(next(group for group in match.groups() if group))
    return age if 0 < age < 120 else None


def road_key(record):
    """'I-805', 'Interstate 805' and '805 Freeway' all become '805'."""
    match = ROAD_NUMBER_RE.search(record.get("road") or "")
    return match.group(0) if match else None


def _lower(value):
    return value.strip().lower() if value else None


def text_tokens(record):
    words = TEXT_TOKEN_RE.findall((record.get("text") or "")[:SIMILARITY_TEXT_CHARS].lower())
    return {word for word in words if word not in COMMON_WORDS and len(word) > 2}


def facts(record):
    text = _text(record)
    return {
        "case_number": record.get("case_number"),
        "date": parse_date(record.get("incident_date")),
        "place": _lower(record.get("place")),
        "county": _lower(record.get("county")),
        # Every place/county named, so a residence city mentioned first doesn't
        # count as a conflict with the crash city named by another article.
        "places": {_lower(m.group(1)) for m in CITY_RE.finditer(text)},
        "counties": {_lower(m.group(1)) for m in COUNTY_RE.finditer(text)},
        "road": road_key(record),
        "victims": victim_types(record),
        "ages": victim_ages(record),
        "tokens": title_tokens(record.get("title")),
        "text_tokens": text_tokens(record),
    }


# ---------------------------------------------------------------- matching

def _differs(a, b, key, all_key):
    """Both have a value, they differ, and neither record mentions the other's value."""
    return bool(a[key] and b[key] and a[key] != b[key] and a[key] not in b[all_key] and b[key] not in a[all_key])


def conflicts(a, b):
    """True if known facts show two records are different crashes."""
    if a["date"] and b["date"] and abs((a["date"] - b["date"]).days) > MAX_DATE_GAP_DAYS:
        return True
    if a["case_number"] and b["case_number"] and a["case_number"] != b["case_number"]:
        return True
    return _differs(a, b, "place", "places") or _differs(a, b, "county", "counties")


def _overlap(tokens_a, tokens_b):
    smaller = min(len(tokens_a), len(tokens_b))
    return len(tokens_a & tokens_b) / smaller if smaller else 0


def same_case_evidence(rec_a, a, rec_b, b):
    """Why two non-conflicting records describe the same crash, or None."""
    if similar_headlines(rec_a.get("title"), rec_b.get("title")):
        return "similar headlines"

    texts_comparable = min(len(a["text_tokens"]), len(b["text_tokens"])) >= MIN_SIMILARITY_TOKENS
    text_overlap = _overlap(a["text_tokens"], b["text_tokens"]) if texts_comparable else None

    both_dated = bool(a["date"] and b["date"])
    shared_location = (a["place"] and a["place"] == b["place"]) or (a["road"] and a["road"] == b["road"])
    # A city or a long highway on the same day is not enough when both stories can
    # be read (two crashes in Los Angeles on one day are common): their text must
    # also overlap. Headline-only records keep the location rule alone.
    corroborated = text_overlap is None or text_overlap >= CORROBORATING_TEXT_OVERLAP
    if shared_location and corroborated:
        if both_dated:
            return "same place/road and crash date"  # dates within a day, nothing conflicting
        if _overlap(a["tokens"], b["tokens"]) >= MIN_TITLE_OVERLAP:
            return "same place/road and overlapping headline words"

    if text_overlap is not None and text_overlap >= MIN_TEXT_OVERLAP:
        return "overlapping story text"

    return None


# ---------------------------------------------------------------- grouping

def _priority(record):
    """Order in which records seed cases: confirmed first, official sources first."""
    return (
        record.get("selection_bucket") != CONSIDER,
        record.get("source_type") != "coroner",
        record.get("source_type") == "google_news_rss",
        record.get("incident_date") is None,
    )


def group_with_reasons(records):
    """Return [(members, rationale)]; rationale lists why each later member joined."""
    cases = []  # list of (members, member_facts, rationale)

    for record in sorted(records, key=_priority):
        record_facts = facts(record)
        for members, member_facts, rationale in cases:
            if any(conflicts(record_facts, f) for f in member_facts):
                continue
            reason = next(
                (
                    (m, r) for m, f in zip(members, member_facts)
                    for r in [same_case_evidence(record, record_facts, m, f)] if r
                ),
                None,
            )
            if reason:
                matched, why = reason
                members.append(record)
                member_facts.append(record_facts)
                rationale.append({
                    "joined": record.get("url"),
                    "matched": matched.get("url"),
                    "reason": why,
                })
                break
        else:
            cases.append(([record], [record_facts], []))

    return [(members, rationale) for members, _, rationale in cases]


def group_into_cases(records):
    """Return a list of cases; each case is a list of records (best record first)."""
    return [members for members, _ in group_with_reasons(records)]


# ---------------------------------------------------------------- case entries

def _first(members, field):
    for record in members:
        if record.get(field):
            return record[field]
    return None


def _source_list(members):
    """Every URL for the case, once each (members and their merged duplicates)."""
    sources, seen = [], set()
    for record in members:
        refs = [{
            "url": record.get("url"),
            "title": record.get("title"),
            "description": record.get("description"),
            "publisher": record.get("publisher"),
            "source_type": record.get("source_type"),
            "published_at": record.get("published_at"),
            "guid": record.get("guid"),
            "discovered_by_queries": list(record.get("discovered_by_queries") or []),
            "discovered_by_sources": list(record.get("discovered_by_sources") or []),
        }] + [
            {
                "url": ref.get("url"),
                "title": ref.get("title"),
                "description": ref.get("description"),
                "publisher": ref.get("publisher"),
                "source_type": None,
                "published_at": ref.get("published_at"),
                "guid": ref.get("guid"),
                "discovered_by_queries": list(ref.get("discovered_by_queries") or []),
                "discovered_by_sources": [ref["source_name"]] if ref.get("source_name") else [],
            }
            for ref in record.get("also_reported_by") or []
        ]
        for ref in refs:
            key = canonical_url(ref["url"])
            if key and key not in seen:
                seen.add(key)
                sources.append(ref)
    return sources


def case_entry(members, rationale=None):
    """One entry of sources_to_open.json."""
    primary = members[0]
    confirmed = any(m.get("selection_bucket") == CONSIDER for m in members)

    return {
        "case_id": "case-" + hashlib.sha1(canonical_url(primary.get("url")).encode()).hexdigest()[:10],
        "confidence": "confirmed" if confirmed else "doubtful",
        "open_first": primary.get("url"),
        "title": primary.get("title"),
        "publisher": primary.get("publisher"),
        "incident_date": _first(members, "incident_date"),
        "incident_date_text": _first(members, "incident_date_text"),
        "incident_date_basis": _first(members, "incident_date_basis"),
        "incident_date_evidence": _first(members, "incident_date_evidence"),
        "location": {
            "county": _first(members, "county"),
            "place": _first(members, "place"),
            "road": _first(members, "road"),
            "location_text": _first(members, "location_text"),
            "location_status": "california_confirmed"
            if any(m.get("location_status") == "california_confirmed" for m in members)
            else primary.get("location_status"),
        },
        "victim_type": next((victim_type(m) for m in members if victim_type(m)), None),
        "victim_age": next((victim_age(m) for m in members if victim_age(m)), None),
        "case_number": _first(members, "case_number"),
        "why": primary.get("selection_reason"),
        "open_to_confirm": None if confirmed else (primary.get("review_reasons") or [primary.get("selection_reason")]),
        "grouping_rationale": rationale or [],
        "sources": _source_list(members),
    }


def build_cases(consider, review):
    """Return (confirmed_cases, doubtful_cases, dropped_counts_by_reason)."""
    confirmed, doubtful, dropped = [], [], {}

    for members, rationale in group_with_reasons(consider + review):
        entry = case_entry(members, rationale)
        if entry["confidence"] == "confirmed":
            confirmed.append(entry)
            continue
        if all(FOLLOW_UP_HEADLINE_RE.search(m.get("title") or "") for m in members):
            reason = "follow-up story (court/sentencing/lawsuit) about an earlier crash"
            dropped[reason] = dropped.get(reason, 0) + 1
            continue
        doubtful.append(entry)

    return confirmed, doubtful, dropped
