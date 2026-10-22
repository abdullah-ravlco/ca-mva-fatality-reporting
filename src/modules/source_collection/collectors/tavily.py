"""Tavily news search for California road-fatality stories.

Each search result is turned into a feed-style item and goes through the same
relevance, date and location filters as the RSS items (collectors/rss.py). Tavily
returns the page text (raw_content), so most results need no article fetch.
Skipped entirely when TAVILY_API_KEY is not set.
"""

import logging
from urllib.parse import urlsplit

import requests

from config.settings import REQUEST_TIMEOUT_SECONDS, TAVILY_API_KEY
from src.core.http_client import error_status
from src.modules.source_collection.article_text import ArticleTextFetcher
from src.modules.source_collection.collectors.rss import records_from_items
from src.modules.source_collection.records import make_source_result
from src.modules.source_collection.sources import (
    MAX_ARTICLE_FETCHES, TAVILY_MAX_RESULTS, TAVILY_QUERIES, TAVILY_SEARCH_URL,
)


logger = logging.getLogger(__name__)


def search_feeds():
    return [
        {
            "name": f"Tavily: {query}",
            "url": TAVILY_SEARCH_URL,
            "kind": "tavily_search",
            "collection_method": "tavily_search",
            "text_label": "Tavily",
            "query": query,
        }
        for query in TAVILY_QUERIES
    ]


def search(query, api_key, since):
    """Raw Tavily results for one query, published since the window start."""
    response = requests.post(
        TAVILY_SEARCH_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "query": query,
            "topic": "news",
            "start_date": since.date().isoformat(),
            "max_results": TAVILY_MAX_RESULTS,
            "include_raw_content": "text",
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json().get("results") or []


def to_item(result):
    """Tavily result -> dict with the feedparser entry keys records_from_items reads."""
    host = urlsplit(result.get("url") or "").netloc.lower().removeprefix("www.")
    item = {
        "id": result.get("url"),
        "title": result.get("title"),
        "link": result.get("url"),
        "summary": result.get("content"),
        "source": {"title": host, "href": f"https://{host}"} if host else {},
    }
    if result.get("published_date"):
        item["published"] = result["published_date"]
    if result.get("raw_content"):
        item["content"] = [{"value": result["raw_content"]}]
    return item


def collect_tavily_sources(client, now, since, api_key=TAVILY_API_KEY):
    if not api_key:
        logger.warning("TAVILY_API_KEY not set; Tavily search skipped.")
        return [], []

    fetcher = ArticleTextFetcher(client, MAX_ARTICLE_FETCHES)
    all_records = []
    checked = []

    for feed in search_feeds():
        result = make_source_result(feed["name"], feed["url"], now, feed["kind"])
        logger.info("Tavily search: %s", feed["query"])

        try:
            items = [to_item(r) for r in search(feed["query"], api_key, since)]
        except (requests.RequestException, ValueError) as error:
            result["http_status"] = error_status(error)
            result["error"] = str(error)
            logger.warning("  failed: %s", error)
            checked.append(result)
            continue

        result["http_status"] = 200
        records, items_seen, skipped = records_from_items(
            feed, items, "Tavily", now, since, fetcher, result
        )

        result["items_seen"] = items_seen
        result["candidate_count"] = len(records)
        result["skipped"] = dict(skipped)

        if items_seen == 0:
            result["error"] = "search returned no results"

        all_records.extend(records)
        checked.append(result)

    return all_records, checked
