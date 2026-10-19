
"""Configuration for California road-fatality source collection."""

from urllib.parse import quote_plus

from config.settings import (
    BROWSER_RETRY_ON_403,
    BROWSER_USER_AGENT,
    LOOKBACK_HOURS,
    PUBLISHER_TIMEZONE,
    REQUEST_DELAY_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    TIMEZONE,
    USER_AGENT,
)

# Article pages fetched per run to read crash dates/locations when the feed itself
# doesn't include the full story. Google News links can't be fetched (JS redirect).
MAX_ARTICLE_FETCHES = 40

SOURCES = [
    {
        "name": "San Bernardino County Coroner",
        "type": "sb_coroner",
        "url": (
            "https://wp.sbcounty.gov/sheriff/"
            "media-center/coroner-press-release/"
        ),
    },
    {
        "name": "Riverside County Sheriff-Coroner",
        "type": "riverside_coroner",
        "url": "https://www.riversidesheriff.org/m/newsflash?cat=6",
    },
    {
        "name": "California Highway Patrol",
        "type": "listing",
        "url": "https://www.chp.ca.gov/news-alerts/news-list/",
        # Each news item is <h3><a>title</a></h3> followed by <p><time>date</time></p>.
        # These are statewide press releases, so most items are filtered out as not
        # being about a fatal crash.
        "item_selector": "section.news-list h3 a[href]",
    },
]

NEWS_RSS_FEEDS = [
    {"name": "ABC7 Los Angeles", "url": "https://abc7.com/feed/"},
    {"name": "KTLA", "url": "https://ktla.com/feed/"},
    {"name": "KCRA Sacramento", "url": "https://www.kcra.com/topstories-rss"},
    {"name": "FOX 5 San Diego", "url": "https://fox5sandiego.com/feed/"},
    {"name": "KRON4", "url": "https://www.kron4.com/feed/"},
    {"name": "Victor Valley News", "url": "https://www.vvng.com/feed/"},
    {"name": "CBS News Bay Area", "url": "https://www.cbsnews.com/sanfrancisco/latest/rss/main"},
    {"name": "CBS News Los Angeles", "url": "https://www.cbsnews.com/losangeles/latest/rss/main"},
    {"name": "CBS News Sacramento", "url": "https://www.cbsnews.com/sacramento/latest/rss/main"},
    {"name": "NBC Bay Area", "url": "https://www.nbcbayarea.com/?rss=y"},
]

# Areas inside California searched one by one (counties and well-known regions), so local
# stories that a statewide query would rank too low are still found.
AREAS = [
    "Los Angeles", "San Diego", "Orange County", "Riverside", "San Bernardino",
    "Santa Clara", "Alameda", "Sacramento", "Contra Costa", "Fresno", "Kern",
    "San Francisco", "Ventura", "San Mateo", "San Joaquin", "Stanislaus", "Sonoma",
    "Tulare", "Santa Barbara", "Solano", "Monterey", "Placer", "San Luis Obispo",
    "Santa Cruz", "Merced", "Marin", "Butte", "Yolo", "El Dorado", "Imperial",
    "Shasta", "Humboldt", "Napa", "Kings County", "Madera", "Nevada County",
    "Mendocino", "Sutter", "Yuba", "Lake County", "Tehama", "Siskiyou", "Tuolumne",
    "Inland Empire", "Central Valley", "Bay Area", "High Desert", "Antelope Valley",
    "Coachella Valley", "Lake Tahoe", "Central Coast", "North Coast",
]

STATEWIDE_QUERIES = [
    '"California" fatal crash',
    '"California" killed in crash',
    '"California Highway Patrol" fatal collision',
    '"California" pedestrian killed crash',
    '"California" motorcyclist killed crash',
    '"California" bicyclist killed crash',
    '"California" deadly vehicle crash coroner',
    '"California" DUI crash killed',
    '"California" hit-and-run killed',
    '"California" wrong-way driver crash killed',
    '"California" big rig crash killed',
    '"California" head-on collision killed',
    '"California" freeway crash dies',
    '"California" CHP investigating fatal',
]
# Phrases searched for every area.
AREA_QUERY_TEMPLATES = [
    '"{area}" fatal crash',
    '"{area}" killed in crash',
    '"{area}" pedestrian OR motorcyclist OR bicyclist killed',
]
GOOGLE_NEWS_QUERIES = STATEWIDE_QUERIES + [t.format(area=area) for area in AREAS for t in AREA_QUERY_TEMPLATES]

GOOGLE_NEWS_RSS_URL = (
    "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
)


def google_news_feeds(hours=LOOKBACK_HOURS):
    """Build Google News RSS feeds for the configured lookback period."""
    days = max(1, -(-hours // 24))
    period = f"when:{days}d"

    return [
        {
            "name": f"Google News: {query}",
            "url": GOOGLE_NEWS_RSS_URL.format(
                query=quote_plus(f"{query} {period}")
            ),
            "kind": "google_news_rss",
            "query": query,
        }
        for query in GOOGLE_NEWS_QUERIES
    ]


# Tavily news search (needs TAVILY_API_KEY; skipped when it isn't set). Each query
# costs one API credit (two with raw content) per run.
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_MAX_RESULTS = 20  # API maximum per query

# Parallel collection. Google News tolerates a request every GOOGLE_NEWS_DELAY_SECONDS across
# all workers; publisher feeds are different hosts and are read side by side.
SEARCH_WORKERS = 6
TAVILY_WORKERS = 5
GOOGLE_NEWS_DELAY_SECONDS = 0.5
LINK_RESOLVE_WORKERS = 10
TAVILY_QUERIES = [
    "California fatal crash",
    "California Highway Patrol fatal collision",
    "California pedestrian killed by vehicle",
    "California motorcyclist killed crash",
    "California deadly freeway crash",
    "California bicyclist killed crash",
    "California DUI crash killed",
    "California hit-and-run killed",
] + [f"{area} California fatal crash" for area in AREAS]


FATAL_KEYWORDS = [
    "fatal", "fatality", "fatally", "deadly", "killed",
    "dies", "died", "dead", "death",
]

ACCIDENT_KEYWORDS = [
    "crash", "collision", "collided", "wreck", "hit-and-run",
    "hit and run", "struck", "pedestrian", "motorcycle",
    "motorcyclist", "bicyclist", "cyclist", "driver",
    "vehicle", "car", "truck", "freeway", "highway",
    "traffic", "rollover", "dui",
]
