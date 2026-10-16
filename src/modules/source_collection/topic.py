"""Broad topic check: is a headline + description about a deadly road crash at all?

Deliberately loose. It only removes clearly unrelated stories (crypto, politics, sports,
shootings...). It does not look at where the crash happened or when.

A story is on topic when it has road/vehicle context (or a vehicle-specific crash term such
as "head-on collision", or both a crash and a fatality term) AND either a crash term, or a
fatality term without a non-road cause (shooting, overdose, fire...). Plane, train and
statistics stories are rejected.
"""

import re

CRASH_TERMS = [
    "crash", "crashed", "crashes", "collision", "collided", "wreck", "wrecked", "pileup", "pile-up",
    "rollover", "rolled over", "overturned", "head-on", "t-bone", "t-boned", "sideswipe", "sideswiped",
    "rear-ended", "hit-and-run", "hit and run", "dui", "wrong-way", "wrong way", "ran off the road",
    "veered off", "struck by", "run over", "hit by a car", "hit by a vehicle", "hit by a truck",
    "car accident", "vehicle accident", "traffic accident", "motor vehicle", "road accident",
    "traffic fatality", "traffic fatalities", "road fatality",
]

# Crash terms that already mean a vehicle crash; "crash", "wreck" or "struck by" alone need road context.
VEHICLE_CRASH_TERMS = [
    "collision", "collided", "pileup", "pile-up", "rollover", "rolled over", "overturned", "head-on",
    "t-bone", "t-boned", "sideswipe", "sideswiped", "rear-ended", "hit-and-run", "hit and run", "dui",
    "wrong-way", "wrong way", "ran off the road", "car accident", "vehicle accident", "traffic accident",
    "motor vehicle", "road accident", "traffic fatality", "traffic fatalities", "road fatality",
]

FATAL_TERMS = [
    "fatal", "fatally", "fatality", "fatalities", "dead", "deadly", "killed", "kills", "killing",
    "dies", "died", "death", "deaths", "succumbed", "pronounced dead", "lost his life",
    "lost her life", "lives lost",
]

ROAD_CONTEXT_TERMS = [
    "highway", "highways", "freeway", "freeways", "interstate", "roadway", "roadways", "road", "roads", "street", "streets", "intersection", "crosswalk",
    "on-ramp", "off-ramp", "offramp", "onramp", "lane", "lanes", "traffic", "route", "boulevard", "avenue",
    "caltrans", "chp", "california highway patrol", "highway patrol",
    "vehicle", "vehicles", "car", "cars", "truck", "trucks", "suv", "sedan", "pickup", "van", "bus",
    "big rig", "semi", "semi-truck", "tractor-trailer", "motorcycle", "motorcyclist", "motorist",
    "bicyclist", "cyclist", "bike", "e-bike", "scooter", "pedestrian", "driver", "passenger",
    "rider", "wheel", "wheelchair",
]
ROUTE_PATTERN = r"(?:i|us|sr|ca|hwy|highway|route)[- ]?\d{1,3}"

# A "crash" in these stories is not a road crash.
NOT_A_ROAD_CRASH_TERMS = [
    "plane crash", "plane", "helicopter", "aircraft", "pilot", "airplane", "jet", "stock market",
    "market crash", "crypto", "bitcoin", "stocks", "roller coaster", "amusement park", "theme park",
    "derail", "derails", "derailed", "derailment", "ferry", "shipwreck",
]

# Statistics, studies and safety advice: about road deaths in general, not one incident.
NOT_AN_INCIDENT_TERMS = [
    "statistics", "statistic", "study finds", "study shows", "survey", "report finds", "report shows",
    "data shows", "data show", "deadliest roads", "deadliest highways", "deadliest intersections",
    "most dangerous", "road safety", "safety tips", "traffic deaths drop", "traffic deaths fall",
    "traffic deaths rise", "traffic deaths decline", "traffic deaths down", "traffic deaths up",
    "per capita", "per 100,000", "vision zero", "ranked", "ranking", "year in review",
]

NON_ROAD_CAUSE_TERMS = [
    "shooting", "shot and killed", "shot dead", "stabbing", "stabbed", "homicide", "murder",
    "suicide", "overdose", "drowned", "drowning", "house fire", "structure fire", "wildfire",
    "workplace accident", "industrial accident", "covid",
    "bus fire", "avalanche", "skier", "snowmobile", "boating", "boat crash", "train derailment",
    "forklift", "construction site", "heat stroke", "hot car",
]


def _pattern(terms):
    alternation = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
    return re.compile(rf"(?<![a-z0-9])(?:{alternation})(?![a-z0-9])")


_CRASH = _pattern(CRASH_TERMS)
_VEHICLE_CRASH = _pattern(VEHICLE_CRASH_TERMS)
_FATAL = _pattern(FATAL_TERMS)
_ROAD = _pattern(ROAD_CONTEXT_TERMS)
_ROUTE = re.compile(rf"(?<![a-z0-9]){ROUTE_PATTERN}(?![a-z0-9])")
_NON_ROAD = _pattern(NON_ROAD_CAUSE_TERMS)
_NOT_ROAD_CRASH = _pattern(NOT_A_ROAD_CRASH_TERMS)
_NOT_AN_INCIDENT = _pattern(NOT_AN_INCIDENT_TERMS)


def on_topic(title, description=""):
    text = re.sub(r"\s+", " ", f"{title or ''} {description or ''}").lower()
    if not text.strip():
        return False

    if _NOT_AN_INCIDENT.search(text) or _NOT_ROAD_CRASH.search(text):
        return False

    has_road = bool(_ROAD.search(text) or _ROUTE.search(text))
    has_crash = bool(_CRASH.search(text))
    has_fatal = bool(_FATAL.search(text))
    if not (has_road or _VEHICLE_CRASH.search(text) or (has_crash and has_fatal)):
        return False
    if has_crash:
        return True
    return has_fatal and not _NON_ROAD.search(text)
