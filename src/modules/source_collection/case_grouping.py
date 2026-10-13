
"""Group records into cases (one crash = one case) for sources_to_open.json.

Two records are the same case only when nothing known about them conflicts
(crash date, place, county, victim type, victim age) AND there is positive
evidence they match: similar headlines, or the same place/road with the same
crash date, or the same place/road with strongly overlapping headline words.

Records are added one by one (best evidence first); a record joins the first case
it matches against every member, otherwise it starts a new case. This avoids
chaining two different crashes together through a third record.
"""

import hashlib
import re

from src.core.date_utils import parse_date
from src.modules.source_collection.deduplication import canonical_url, similar_headlines, title_tokens
from src.modules.source_collection.final_selection import CONSIDER


MAX_DATE_GAP_DAYS = 1
MIN_TITLE_OVERLAP = 0.5   # with a shared place/road but a missing crash date

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
        ) if part
    )


def victim_type(record):
    text = _text(record)
    for name, pattern in VICTIM_TYPES:
        if pattern.search(text):
            return name
    return None


def victim_age(record):
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


def facts(record):
    return {
        "case_number": record.get("case_number"),
        "date": parse_date(record.get("incident_date")),
        "place": _lower(record.get("place")),
        "county": _lower(record.get("county")),
        "road": road_key(record),
        "victim": victim_type(record),
        "age": victim_age(record),
        "tokens": title_tokens(record.get("title")),
    }


# ---------------------------------------------------------------- matching

def conflicts(a, b):
    """True if known facts show two records are different crashes or victims."""
    if a["date"] and b["date"] and abs((a["date"] - b["date"]).days) > MAX_DATE_GAP_DAYS:
        return True
    for key in ("case_number", "place", "county", "victim"):
        if a[key] and b[key] and a[key] != b[key]:
            return True
    if a["age"] and b["age"] and abs(a["age"] - b["age"]) > 1:
        return True
    return False


def same_case_evidence(rec_a, a, rec_b, b):
    """Positive evidence that two non-conflicting records describe the same crash."""
    if similar_headlines(rec_a.get("title"), rec_b.get("title")):
        return True

    shared_location = (a["place"] and a["place"] == b["place"]) or (a["road"] and a["road"] == b["road"])
    if not shared_location:
        return False

    if a["date"] and b["date"]:
        return True  # same place/road, crash dates within a day, nothing conflicting

    smaller = min(len(a["tokens"]), len(b["tokens"]))
    overlap = len(a["tokens"] & b["tokens"]) / smaller if smaller else 0
    return overlap >= MIN_TITLE_OVERLAP


# ---------------------------------------------------------------- grouping

def _priority(record):
    """Order in which records seed cases: confirmed first, official sources first."""
    return (
        record.get("selection_bucket") != CONSIDER,
        record.get("source_type") != "coroner",
        record.get("source_type") == "google_news_rss",
        record.get("incident_date") is None,
    )


def group_into_cases(records):
    """Return a list of cases; each case is a list of records (best record first)."""
    cases = []  # list of (members, member_facts)

    for record in sorted(records, key=_priority):
        record_facts = facts(record)
        for members, member_facts in cases:
            if any(conflicts(record_facts, f) for f in member_facts):
                continue
            if any(same_case_evidence(record, record_facts, m, f) for m, f in zip(members, member_facts)):
                members.append(record)
                member_facts.append(record_facts)
                break
        else:
            cases.append(([record], [record_facts]))

    return [members for members, _ in cases]


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
            "publisher": record.get("publisher"),
            "source_type": record.get("source_type"),
            "published_at": record.get("published_at"),
        }] + [
            {
                "url": ref.get("url"),
                "title": None,
                "publisher": ref.get("publisher"),
                "source_type": None,
                "published_at": ref.get("published_at"),
            }
            for ref in record.get("also_reported_by") or []
        ]
        for ref in refs:
            key = canonical_url(ref["url"])
            if key and key not in seen:
                seen.add(key)
                sources.append(ref)
    return sources


def case_entry(members):
    """One entry of sources_to_open.json."""
    primary = members[0]
    member_facts = [facts(m) for m in members]
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
        "victim_type": next((f["victim"] for f in member_facts if f["victim"]), None),
        "victim_age": next((f["age"] for f in member_facts if f["age"]), None),
        "case_number": _first(members, "case_number"),
        "why": primary.get("selection_reason"),
        "open_to_confirm": None if confirmed else (primary.get("review_reasons") or [primary.get("selection_reason")]),
        "sources": _source_list(members),
    }


def build_cases(consider, review):
    """Return (confirmed_cases, doubtful_cases, dropped_counts_by_reason)."""
    confirmed, doubtful, dropped = [], [], {}

    for members in group_into_cases(consider + review):
        entry = case_entry(members)
        if entry["confidence"] == "confirmed":
            confirmed.append(entry)
            continue
        if all(FOLLOW_UP_HEADLINE_RE.search(m.get("title") or "") for m in members):
            reason = "follow-up story (court/sentencing/lawsuit) about an earlier crash"
            dropped[reason] = dropped.get(reason, 0) + 1
            continue
        doubtful.append(entry)

    return confirmed, doubtful, dropped
