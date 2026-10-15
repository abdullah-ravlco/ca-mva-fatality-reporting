
"""California location detection for news text.

Statuses:
    california_confirmed  - a California-specific indicator was found
    outside_california    - another state is named and nothing points to California
    location_unconfirmed  - no usable location indicator

Where someone is FROM is not where the crash happened: "Semi driver from California
dies in crash on I-35 in Olathe" and "California woman killed in Honolulu crash" are
not California crashes, so residence phrases are ignored when looking for California.
"""

import re


CALIFORNIA_CONFIRMED = "california_confirmed"
OUTSIDE_CALIFORNIA = "outside_california"
LOCATION_UNCONFIRMED = "location_unconfirmed"

CA_COUNTIES = [
    "Alameda", "Alpine", "Amador", "Butte", "Calaveras", "Colusa", "Contra Costa", "Del Norte",
    "El Dorado", "Fresno", "Glenn", "Humboldt", "Imperial", "Inyo", "Kern", "Kings", "Lake",
    "Lassen", "Los Angeles", "Madera", "Marin", "Mariposa", "Mendocino", "Merced", "Modoc",
    "Mono", "Monterey", "Napa", "Nevada", "Orange", "Placer", "Plumas", "Riverside",
    "Sacramento", "San Benito", "San Bernardino", "San Diego", "San Francisco", "San Joaquin",
    "San Luis Obispo", "San Mateo", "Santa Barbara", "Santa Clara", "Santa Cruz", "Shasta",
    "Sierra", "Siskiyou", "Solano", "Sonoma", "Stanislaus", "Sutter", "Tehama", "Trinity",
    "Tulare", "Tuolumne", "Ventura", "Yolo", "Yuba",
]

# Cities whose names are unlikely to refer to somewhere outside California.
# Ambiguous names (Ontario, Glendale, Pasadena, Corona, Long Beach, ...) are left out
# on purpose; they only count when the text also says "County"/"California"/"CHP".
CA_CITIES = [
    "Los Angeles", "San Diego", "San Francisco", "San Jose", "Sacramento", "Oakland",
    "Fresno", "Bakersfield", "Stockton", "Modesto", "Anaheim", "Santa Ana", "Irvine",
    "Chula Vista", "National City", "El Cajon", "Escondido", "Oceanside", "Carlsbad",
    "San Bernardino", "Victorville", "Hesperia", "Apple Valley", "Adelanto", "Barstow",
    "Fontana", "Rancho Cucamonga", "Rialto", "Redlands", "Yucaipa", "Upland", "Montclair",
    "Chino Hills", "Big Bear", "Twentynine Palms", "Yucca Valley", "Needles", "Moreno Valley",
    "Perris", "Hemet", "San Jacinto", "Temecula", "Murrieta", "Menifee", "Lake Elsinore",
    "Jurupa Valley", "Eastvale", "Norco", "Banning", "Beaumont", "Palm Springs",
    "Palm Desert", "Cathedral City", "Indio", "Coachella", "La Quinta", "Blythe",
    "Santa Clarita", "Lancaster", "Palmdale", "Torrance", "Inglewood", "Compton",
    "Pomona", "West Covina", "El Monte", "Downey", "Whittier", "Huntington Beach",
    "Costa Mesa", "Garden Grove", "Fullerton", "Oxnard", "Thousand Oaks", "Simi Valley",
    "Santa Barbara", "Santa Maria", "San Luis Obispo", "Santa Cruz",
    "Hayward", "Berkeley", "Walnut Creek", "Vallejo",
    "Santa Rosa", "Petaluma", "San Mateo", "Daly City", "Redwood City",
    "Sunnyvale", "Santa Clara", "Mountain View", "Elk Grove", "Roseville", "Folsom",
    "Citrus Heights", "Rancho Cordova", "Yuba City", "Chico",
    "Redding", "Turlock", "Visalia", "Hanford", "Porterville",
    "Lodi", "Manteca", "El Centro",
]
# Left out because they are common surnames or towns elsewhere (their "X County" form
# still counts): Davis, Salinas, Richmond, Concord, Ventura, Fremont, Clovis, Woodland,
# Madera, Merced, Tulare, Napa, Tracy, Ceres, Delano, Eureka.

# Interstates that exist only in California.
CA_ONLY_INTERSTATES = r"(?:105|110|210|238|380|505|605|680|710|780|805|880|980)"

OTHER_STATES = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "Colorado", "Connecticut", "Delaware",
    "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa", "Kansas",
    "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
    "Mississippi", "Missouri", "Montana", "Nebraska", "New Hampshire", "New Jersey",
    "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon",
    "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota", "Tennessee", "Texas",
    "Utah", "Vermont", "Virginia", "Washington state", "West Virginia", "Wisconsin", "Wyoming",
    # "Nevada" is also a California county/city, so only the clear forms count.
    "Las Vegas", "Reno, Nevada", "Reno, Nev",
]


# Interstates that exist in California. Any other interstate number (I-35, I-94, I-95)
# points to another state.
CA_INTERSTATE_NUMBERS = {
    5, 8, 10, 15, 40, 80, 105, 110, 205, 210, 215, 238, 280, 305, 380, 405, 505, 580, 605, 680,
    710, 780, 805, 880, 980,
}
INTERSTATE_RE = re.compile(r"\b(?:I-|Interstate\s+)(\d{1,3})\b")

# Other places that headlines name without "in <State>".
OTHER_PLACES = ["Honolulu", "Olathe", "Kansas City", "Salt Lake City", "Boise", "Portland", "Seattle", "Phoenix"]
STATE_ABBREVIATIONS = r"N\.J\.|N\.Y\.|Pa\.|Mass\.|Conn\.|Md\.|Va\.|Fla\.|Ga\.|Tenn\.|Mich\.|Wis\.|Minn\.|Ill\."
RESIDENT_NOUNS = (
    r"man|men|woman|women|driver|motorist|resident|residents|teen|boy|girl|couple|family|"
    r"motorcyclist|bicyclist|pedestrian|trucker|native|father|mother|student|worker|veteran|tourist"
)


def _alternation(names):
    return "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))


CA_STRONG_RE = re.compile(
    r"\bcalifornia\b|\bcalif\.|,\s*CA\b|\bCHP\b|\bcaltrans\b"
    rf"|\b(?:I-|Interstate\s+){CA_ONLY_INTERSTATES}\b",
    re.IGNORECASE,
)
COUNTY_RE = re.compile(rf"\b({_alternation(CA_COUNTIES)})\s+County\b")
CITY_RE = re.compile(rf"\b({_alternation(CA_CITIES)})\b")
ROAD_RE = re.compile(
    r"\b(?:I-\d{1,3}|Interstate\s+\d{1,3}|(?:State\s+)?(?:Route|Highway|Hwy\.?)\s+\d{1,3}"
    r"|SR-?\d{1,3}|US-?\d{1,3}|U\.S\.\s+\d{1,3}|(?:\d{1,3}|[A-Z][a-z]+)\s+Freeway)\b"
)
# Another state only counts when used as a place: "in Arizona", "near Las Vegas", ", Texas",
# "western Kansas", "Kansas man", "N.J. motorcyclist", "Honolulu parking garage".
OTHER_STATE_RE = re.compile(
    rf"(?:\bin|\bnear|,)\s+({_alternation(OTHER_STATES)})\b"
    rf"|\b(?:northern|southern|eastern|western|central)\s+({_alternation(OTHER_STATES)})\b"
    rf"|\b({_alternation(OTHER_STATES)})\s+(?:{RESIDENT_NOUNS})\b"
    rf"|(?<!\w)({STATE_ABBREVIATIONS})"
    rf"|\b({_alternation(OTHER_PLACES)})\b",
)
# "from California", "California woman", "California-based": where someone lives. A region
# ("Southern California woman") still counts as a place.
RESIDENCE_RE = re.compile(
    rf"\b(?:from|of|out\s+of)\s+(?:(?:northern|southern|central)\s+)?california\b"
    rf"|\bcalifornia(?:-based|\s+(?:{RESIDENT_NOUNS})\b)",
    re.IGNORECASE,
)


def other_state_evidence(text):
    """The other-state place named in the text, or None."""
    match = OTHER_STATE_RE.search(text)
    if match:
        return next(group for group in match.groups() if group)
    for number in INTERSTATE_RE.finditer(text):
        if int(number.group(1)) not in CA_INTERSTATE_NUMBERS:
            return number.group(0)
    return None


def detect_location(*texts):
    """Return location_status plus county / place / road / evidence found in the texts."""

    text = " ".join(t for t in texts if t)
    county = COUNTY_RE.search(text)
    city = CITY_RE.search(text)
    road = ROAD_RE.search(text)
    strong = CA_STRONG_RE.search(RESIDENCE_RE.sub(" ", text))
    other_state = other_state_evidence(text)

    evidence = None
    if strong:
        status, evidence = CALIFORNIA_CONFIRMED, strong.group(0)
    elif county:
        status, evidence = CALIFORNIA_CONFIRMED, county.group(0)
    elif city and not other_state:
        status, evidence = CALIFORNIA_CONFIRMED, city.group(0)
    elif other_state:
        status, evidence = OUTSIDE_CALIFORNIA, other_state
    else:
        status = LOCATION_UNCONFIRMED

    return {
        "location_status": status,
        "location_evidence": evidence,
        "county": county.group(1) if county else None,
        "place": city.group(1) if city else None,
        "road": road.group(0) if road else None,
    }
