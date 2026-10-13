"""Keyword filters for California fatal road-accident collection."""

import re

from src.modules.source_collection.location import detect_location


# Road-related incident terms.
ROAD_EVENT_TERMS = [
    "crash",
    "collision",
    "car accident",
    "vehicle accident",
    "traffic accident",
    "motor vehicle accident",
    "motor vehicle crash",
    "fatal crash",
    "fatal collision",
    "deadly crash",
    "deadly collision",
    "road accident",
    "highway crash",
    "truck crash",
    "motorcycle crash",
    "motorcyclist",
    "pedestrian struck",
    "pedestrian killed",
    "cyclist killed",
    "vehicle rollover",
    "hit-and-run",
    "hit and run",
    "dui crash",
    "drunk driving crash",
]

# Terms that can indicate an unrelated fatal incident.
NON_ROAD_FATALITY_TERMS = [
    "shooting",
    "shot and killed",
    "stabbing",
    "stabbed to death",
    "homicide",
    "murder",
    "suicide",
    "overdose",
    "drowned",
    "drowning",
    "house fire",
    "structure fire",
    "workplace accident",
    "industrial accident",
]

ROAD_CONTEXT_TERMS = [
    "highway",
    "freeway",
    "interstate",
    "road",
    "street",
    "intersection",
    "vehicle",
    "motorcycle",
    "motorcyclist",
    "pedestrian",
    "driver",
    "passenger",
    "truck",
    "car",
    "traffic",
    "caltrans",
    "chp",
    "california highway patrol",
]

# Who was killed: road users. Combined with a road place below, these identify a road
# death even when the headline has no "crash" word ("Motorcycle rider dies on Highway 132").
ROAD_VICTIM_TERMS = [
    "motorcycle rider",
    "motorcyclist",
    "motorcycle",
    "pedestrian",
    "bicyclist",
    "cyclist",
    "bike rider",
    "e-bike",
    "scooter rider",
    "driver",
    "passenger",
    "struck by a vehicle",
    "struck by a car",
    "struck by a truck",
    "hit by a car",
    "hit by a vehicle",
    "run over",
]

ROAD_PLACE_TERMS = [
    "highway",
    "freeway",
    "interstate",
    "road",
    "roadway",
    "street",
    "avenue",
    "boulevard",
    "intersection",
    "route",
    "traffic",
    "crosswalk",
    "caltrans",
    "chp",
    "california highway patrol",
]

FATAL_TERMS = [
    "fatal",
    "fatally",
    "fatality",
    "dead",
    "deadly",
    "killed",
    "dies",
    "died",
    "death",
    "deaths",
    "succumbed",
    "pronounced dead",
]

def _normalize_text(value):
    """Convert a value to normalized lowercase text."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip().lower()


_TERM_PATTERNS = {}


def _contains_any(text, terms):
    """True if any term occurs as a whole word/phrase ("dies" won't match "studies",
    "car" won't match "card", "dead" won't match "deadline")."""
    key = tuple(terms)
    pattern = _TERM_PATTERNS.get(key)
    if pattern is None:
        alternation = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
        pattern = re.compile(rf"(?<![a-z0-9])(?:{alternation})(?![a-z0-9])")
        _TERM_PATTERNS[key] = pattern
    return pattern.search(text) is not None


def looks_relevant(record, summary=None):
    """
    Check whether a record appears relevant to a fatal road incident.

    Supports both interfaces:
        looks_relevant(record_dict)
        looks_relevant(title, summary)
    """
    if isinstance(record, dict):
        title = record.get("title", "")
        description = (
            record.get("description")
            or record.get("summary")
            or ""
        )
    else:
        title = record or ""
        description = summary or ""

    text = (
        f"{_normalize_text(title)} "
        f"{_normalize_text(description)}"
    ).strip()

    if not text:
        return False

    has_road_event = _contains_any(text, ROAD_EVENT_TERMS)
    has_road_context = _contains_any(text, ROAD_CONTEXT_TERMS)
    has_fatality = _contains_any(text, FATAL_TERMS)
    has_non_road_term = _contains_any(
        text, NON_ROAD_FATALITY_TERMS
    )
    # A road user dying in a road place counts even without a "crash" word.
    has_road_victim_in_road_place = (
        _contains_any(text, ROAD_VICTIM_TERMS)
        and _contains_any(text, ROAD_PLACE_TERMS)
    )

    if not has_fatality:
        return False
    if not (has_road_event or has_road_victim_in_road_place):
        return False

    # Require stronger road context when unrelated fatality terms appear.
    if has_non_road_term:
        return has_road_context

    return True


def mentions_accident(text):
    """Check whether text mentions a road-related incident."""
    normalized = _normalize_text(text)
    return _contains_any(normalized, ROAD_EVENT_TERMS)


def normalize_title(title):
    """Normalize headlines for duplicate comparison."""
    normalized = _normalize_text(title)
    normalized = re.sub(r"https?://\S+", " ", normalized)
    normalized = re.sub(r"[^a-z0-9\s]", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def location_status(title, summary=""):
    """
    California location status for a title/summary. Returns one of the
    statuses in location.py (california_confirmed, outside_california,
    location_unconfirmed) so every collector uses the same values.
    """
    return detect_location(title, summary)["location_status"]