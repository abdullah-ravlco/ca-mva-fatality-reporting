
"""RSS collection from California publishers and Google News RSS.

For each relevant, recently published item the story text (feed content or article
page) is read to find the crash date and the California location. If the article
page is blocked, the RSS evidence is kept and the block is recorded.
"""

import calendar
import logging
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

import feedparser
import requests
from bs4 import BeautifulSoup

from src.modules.source_collection.article_text import ArticleTextFetcher
from src.core.date_utils import is_recent, iso, parse_date
from src.modules.source_collection.filters import looks_relevant
from src.core.http_client import error_status
from src.modules.source_collection.incident_dates import find_incident_date
from src.modules.source_collection.location import detect_location
from src.modules.source_collection.records import add_example, is_excluded_location, make_record, make_source_result, news_status
from src.modules.source_collection.sources import LOOKBACK_HOURS, MAX_ARTICLE_FETCHES, NEWS_RSS_FEEDS, google_news_feeds


logger = logging.getLogger(__name__)

# Only the start of the story is used for location: later paragraphs often link to
# unrelated stories in other places.
LOCATION_TEXT_CHARS = 1500

# Feed-item fields kept verbatim in raw_feed_items.json (struct_time fields are left out).
RAW_ITEM_FIELDS = ("id", "title", "link", "published", "updated", "summary", "author")


def raw_item(item, feed):
    """JSON-safe copy of one feed item as the feed gave it, before any filter."""
    raw = {field: item.get(field) for field in RAW_ITEM_FIELDS if item.get(field) is not None}
    source_tag = item.get("source") or {}
    if source_tag:
        raw["source"] = {"title": source_tag.get("title"), "href": source_tag.get("href")}
    raw["feed_name"] = feed["name"]
    raw["feed_url"] = feed["url"]
    raw["query"] = feed.get("query")
    return raw


def normalize_link(link, feed_url):
    """Absolute https URL for a feed link.

    KCRA's feed uses protocol-relative links ("//www.kcra.com/article/..."), which
    requests rejects with "No scheme supplied"; relative paths are resolved too.
    """
    link = (link or "").strip()
    if not link:
        return ""
    if link.startswith("//"):
        return "https:" + link
    absolute = urljoin(feed_url, link)
    return absolute if urlsplit(absolute).scheme in ("http", "https") else ""


def parse_published_date(item):
    parsed = item.get("published_parsed") or item.get("updated_parsed")

    if parsed:
        return datetime.fromtimestamp(
            calendar.timegm(parsed), timezone.utc
        )

    return parse_date(item.get("published") or item.get("updated"))


def clean_html(value):
    return BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True)


def split_google_title(title, publisher):
    suffix = f" - {publisher}" if publisher else None

    if suffix and title.endswith(suffix):
        return title[:-len(suffix)].strip()

    return title


def records_from_feed(feed, content, now, hours, fetcher, result):
    """Parse one feed. Returns (records, items_seen, skipped_counts).

    Skip reasons and a few example titles per reason are written to `result`.
    Everything that can be decided from the feed item alone (topic, publication
    date, out-of-state place) is decided before the article page is fetched.
    """
    parsed = feedparser.parse(content)
    feed_title = parsed.feed.get("title") or feed["name"]
    is_google = feed.get("kind") == "google_news_rss"
    records = []
    skipped = Counter()

    def skip(reason, title):
        skipped[reason] += 1
        add_example(result, reason, title, url)

    query = feed.get("query")
    raw_items = result.setdefault("raw_items", [])

    for item in parsed.entries:
        raw_items.append(raw_item(item, feed))
        title = (item.get("title") or "").strip()
        url = normalize_link(item.get("link"), feed["url"])
        summary = clean_html(item.get("summary"))

        if not url or not title:
            skip("missing_link_or_title", title)
            continue

        source_tag = item.get("source") or {}
        publisher = source_tag.get("title") or feed_title
        publisher_url = source_tag.get("href")

        if is_google:
            title = split_google_title(title, source_tag.get("title"))

            if summary.startswith(title):
                summary = ""

        if not looks_relevant(title, summary):
            skip("not_fatal_road_story", title)
            continue

        published_at = parse_published_date(item)

        # A story published before the window can't describe a crash inside it.
        if published_at is None:
            skip("no_publication_date", title)
            continue
        if not is_recent(published_at, now=now, hours=hours):
            skip("published_outside_window", title)
            continue

        # Out-of-state from the headline/summary alone: no need to open the article.
        if is_excluded_location(detect_location(title, summary)["location_status"]):
            skip("outside_california", title)
            continue

        article = fetcher.text_for(item, url)
        # RSS evidence is always kept, even when the page itself was blocked.
        story = article["text"] or summary

        location = detect_location(title, summary, story[:LOCATION_TEXT_CHARS])
        if is_excluded_location(location["location_status"]):
            skip("outside_california", title)
            continue

        incident = find_incident_date(" ".join(p for p in (title, summary, story) if p), published_at)
        status, reasons = news_status(incident["incident_date"], location["location_status"], now, hours)
        if incident["incident_date_note"]:
            reasons.append(incident["incident_date_note"])
        if article["page_extraction"] in ("blocked", "failed"):
            reasons.append(f"article page unavailable ({article['fetch_error']}); RSS text used")

        records.append(make_record(
            source_name=feed["name"],
            source_type="google_news_rss" if is_google else "rss",
            collection_method="rss",
            title=title,
            url=url,
            collected_at=now,
            feed_url=feed["url"],
            publisher=publisher,
            publisher_url=publisher_url,
            description=summary or None,
            published_at=iso(published_at),
            published_at_raw=item.get("published") or item.get("updated"),
            published_local_date=incident["published_local_date"],
            publisher_timezone=incident["publisher_timezone"],
            incident_date=iso(incident["incident_date"]),
            incident_date_text=incident["incident_date_text"],
            incident_date_basis=incident["incident_date_basis"],
            incident_date_evidence=incident["incident_date_evidence"],
            incident_date_note=incident["incident_date_note"],
            incident_date_note_type=incident["incident_date_note_type"],
            text_source=article["text_source"],
            page_extraction=article["page_extraction"],
            fetch_error=article["fetch_error"],
            guid=item.get("id"),
            query=query,
            discovered_by_queries=[query] if query else [],
            status=status,
            review_reasons=reasons,
            **location,
        ))

    return records, len(parsed.entries), skipped


def collect_rss_sources(client, now, hours=LOOKBACK_HOURS):
    feeds = (
        [dict(feed, kind="rss") for feed in NEWS_RSS_FEEDS]
        + google_news_feeds(hours)
    )
    fetcher = ArticleTextFetcher(client, MAX_ARTICLE_FETCHES)

    all_records = []
    checked = []

    for feed in feeds:
        result = make_source_result(
            feed["name"], feed["url"], now, feed["kind"]
        )
        logger.info("Reading RSS feed: %s", feed["name"])

        try:
            response = client.get(feed["url"])
        except requests.RequestException as error:
            result["http_status"] = error_status(error)
            result["error"] = str(error)
            logger.warning("  failed: %s", error)
            checked.append(result)
            continue

        result["http_status"] = response.status_code

        records, items_seen, skipped = records_from_feed(
            feed, response.content, now, hours, fetcher, result
        )

        result["items_seen"] = items_seen
        result["candidate_count"] = len(records)
        result["skipped"] = dict(skipped)

        if items_seen == 0:
            result["error"] = "feed returned no items"

        all_records.extend(records)
        checked.append(result)

    return all_records, checked
