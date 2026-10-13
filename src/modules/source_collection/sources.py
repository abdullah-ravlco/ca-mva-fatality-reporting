
"""Configuration for California road-fatality source collection."""

from urllib.parse import quote_plus

from config.settings import (
    BROWSER_RETRY_ON_403,
    BROWSER_USER_AGENT,
    PUBLISHER_TIMEZONE,
    REQUEST_DELAY_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    TIMEZONE,
    USER_AGENT,
)

LOOKBACK_HOURS = 168

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
]

GOOGLE_NEWS_QUERIES = [
    '"California" fatal crash',
    '"California" killed in crash',
    '"California Highway Patrol" fatal collision',
    '"California" pedestrian killed crash',
    '"California" motorcyclist killed crash',
    '"California" deadly vehicle crash coroner',
]

GOOGLE_NEWS_RSS_URL = (
    "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
)


def google_news_feeds(hours=LOOKBACK_HOURS):
    """Build Google News RSS feeds for the configured lookback period."""
    days = max(1, -(-hours // 24))

    return [
        {
            "name": f"Google News: {query}",
            "url": GOOGLE_NEWS_RSS_URL.format(
                query=quote_plus(f"{query} when:{days}d")
            ),
            "kind": "google_news_rss",
            "query": query,
        }
        for query in GOOGLE_NEWS_QUERIES
    ]


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
