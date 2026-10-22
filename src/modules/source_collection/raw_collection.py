"""Collect raw items from RSS feeds, Google News RSS and Tavily search.

Nothing is filtered here and no article page is opened. Every item becomes a record with
exactly these fields:
  published_date, source_name, title, url, description
"""

import logging

import feedparser
import requests

from config.settings import REQUEST_TIMEOUT_SECONDS, TAVILY_API_KEY
from src.core.date_utils import iso
from src.core.parallel import parallel_map
from src.modules.source_collection.collectors.rss import (
    clean_html, normalize_link, parse_published_date, split_google_title,
)
from src.modules.source_collection.sources import (
    NEWS_RSS_FEEDS, SEARCH_WORKERS, TAVILY_MAX_RESULTS, TAVILY_QUERIES, TAVILY_SEARCH_URL,
    TAVILY_WORKERS, google_news_feeds,
)

logger = logging.getLogger(__name__)

FIELDS = ("published_date", "source_name", "title", "url", "description")


def raw_record(published_date, source_name, title, url, description):
    return {
        "published_date": published_date,
        "source_name": source_name,
        "title": title,
        "url": url,
        "description": description,
    }


def read_feed(client, feed, checkpoint=None):
    """(records, error) for one feed. A feed finished in an earlier, interrupted run is not read again."""
    done = checkpoint.get(feed["name"]) if checkpoint else None
    if done is not None:
        return done["records"], None
    logger.info("Reading RSS feed: %s", feed["name"])
    try:
        response = client.get(feed["url"])
    except requests.RequestException as error:
        logger.warning("  failed (%s): %s", feed["name"], error)
        return [], {"source": feed["name"], "error": str(error)}

    parsed = feedparser.parse(response.content)
    feed_title = parsed.feed.get("title") or feed["name"]
    records = []
    for item in parsed.entries:
        url = normalize_link(item.get("link"), feed["url"])
        title = (item.get("title") or "").strip()
        if not url or not title:
            continue
        source = item.get("source") or {}
        source_name = source.get("title") or feed_title
        if feed["kind"] == "google_news_rss":
            title = split_google_title(title, source.get("title"))
        published = parse_published_date(item)
        records.append(raw_record(
            iso(published) if published else (item.get("published") or item.get("updated")),
            source_name, title, url, clean_html(item.get("summary")) or None,
        ))
    if checkpoint:
        checkpoint.add(feed["name"], {"records": records})
    return records, None


def collect_rss(client, since, now, checkpoint=None, on_progress=None):
    """(records, errors) from the publisher feeds and the Google News searches for the window since `since`.

    Feeds are read in parallel; the client spaces requests to the same host, so the many
    Google News searches share one polite request rate while publisher feeds overlap.
    """
    feeds = [dict(feed, kind="rss") for feed in NEWS_RSS_FEEDS] + google_news_feeds(since, now)
    results = []
    for result in parallel_map(lambda feed: read_feed(client, feed, checkpoint), feeds, SEARCH_WORKERS):
        results.append(result)
        if on_progress:
            on_progress()
    records = [record for items, _ in results for record in items]
    errors = [error for _, error in results if error]
    return records, errors


def search_tavily(query, api_key, since):
    response = requests.post(
        TAVILY_SEARCH_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "query": query,
            "topic": "news",
            "start_date": since.date().isoformat(),
            "max_results": TAVILY_MAX_RESULTS,
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json().get("results") or []


def tavily_query(query, api_key, since, checkpoint=None):
    """(records, error) for one Tavily query. A query finished in an interrupted run is not paid for again."""
    done = checkpoint.get(f"tavily:{query}") if checkpoint else None
    if done is not None:
        return done["records"], None
    logger.info("Tavily search: %s", query)
    try:
        results = search_tavily(query, api_key, since)
    except (requests.RequestException, ValueError) as error:
        logger.warning("  failed (%s): %s", query, error)
        return [], {"source": f"Tavily: {query}", "error": str(error)}

    records = []
    for result in results:
        url = normalize_link(result.get("url"), TAVILY_SEARCH_URL)
        title = (result.get("title") or "").strip()
        if not url or not title:
            continue
        host = url.split("/")[2].lower().removeprefix("www.")
        records.append(raw_record(result.get("published_date"), host, title, url, result.get("content") or None))
    if checkpoint:
        checkpoint.add(f"tavily:{query}", {"records": records})
    return records, None


def collect_tavily(since, api_key=TAVILY_API_KEY, checkpoint=None, on_progress=None):
    """(records, errors) from Tavily news search (queries run in parallel); skipped without a key."""
    if not api_key:
        logger.warning("TAVILY_API_KEY not set; Tavily search skipped.")
        return [], []

    results = []
    for result in parallel_map(
        lambda query: tavily_query(query, api_key, since, checkpoint), TAVILY_QUERIES, TAVILY_WORKERS
    ):
        results.append(result)
        if on_progress:
            on_progress()
    records = [record for items, _ in results for record in items]
    errors = [error for _, error in results if error]
    return records, errors
