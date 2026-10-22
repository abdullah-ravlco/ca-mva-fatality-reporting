
"""Generic collector for official news listings (title links + a date per item), e.g. CHP."""

from collections import Counter
from urllib.parse import urldefrag, urljoin

import requests
from bs4 import BeautifulSoup

from src.core.date_utils import find_date_in_text, is_date_only, is_recent, iso, parse_date
from src.modules.source_collection.filters import mentions_accident
from src.core.http_client import error_status
from src.modules.source_collection.records import (
    DATE_CONFLICT_REVIEW, DATE_UNKNOWN, RECENT, RECENT_LISTING_DATE, RECENT_LISTING_ONLY,
    add_example, make_record, make_source_result,
)


TIME_SELECTORS = [
    "time[datetime]",
    'meta[property="article:published_time"]',
    'meta[property="og:published_time"]',
    'meta[name="pubdate"]',
    'meta[name="date"]',
    'meta[name="publish-date"]',
    'meta[itemprop="datePublished"]',
]
MAX_SIBLINGS_TO_SCAN = 6


def _date_in(node):
    """(datetime, date_only) from time/meta tags or visible text inside one node."""
    if node is None or not hasattr(node, "select_one"):
        return None, False
    # The node itself may be the <time> tag; select_one only searches descendants.
    candidates = [node] if node.name == "time" and node.get("datetime") else []
    candidates += [node.select_one(selector) for selector in TIME_SELECTORS]
    for found in candidates:
        if found is None:
            continue
        value = found.get("datetime") or found.get("content") or found.get_text(" ", strip=True)
        parsed = parse_date(value)
        if parsed:
            return parsed, is_date_only(value)
    parsed, matched = find_date_in_text(node.get_text(" ", strip=True))
    return parsed, bool(matched and is_date_only(matched))


def _link_count(node):
    return len(node.find_all("a", href=True)) if hasattr(node, "find_all") else 0


def date_near_link(link):
    """Find the date that belongs to this link's own listing item.

    Climbs only while the ancestor holds this single link (so a container listing
    many items can't lend its first date to every link), then scans the following
    siblings up to the next item's link. On CHP the <time> sits in a sibling <p>
    after the <h3> that holds the link.
    """
    item = link
    while item.parent is not None and _link_count(item.parent) == 1:
        item = item.parent

    parsed, date_only = _date_in(item)
    if parsed:
        return parsed, date_only

    for sibling in item.find_next_siblings(limit=MAX_SIBLINGS_TO_SCAN):
        if _link_count(sibling) > 0:
            break  # reached the next listing item
        parsed, date_only = _date_in(sibling)
        if parsed:
            return parsed, date_only

    return None, False


def extract_article_metadata(html):
    """Title, date, description and publisher from an article page."""

    soup = BeautifulSoup(html, "html.parser")

    def meta(prop):
        tag = soup.find("meta", property=prop)
        return tag["content"].strip() if tag and tag.get("content") else None

    title = meta("og:title") or (soup.title.get_text(" ", strip=True) if soup.title else None)

    published_at, date_only = None, False
    for selector in TIME_SELECTORS:
        tag = soup.select_one(selector)
        if not tag:
            continue
        value = tag.get("datetime") or tag.get("content") or tag.get_text(" ", strip=True)
        published_at = parse_date(value)
        if published_at:
            date_only = is_date_only(value)
            break

    return {
        "title": title,
        "publisher": meta("og:site_name"),
        "description": meta("og:description"),
        "published_at": published_at,
        "date_only": date_only,
    }


def collect_from_source(source, client, now, since):
    """Collect recent candidate articles from one listing page."""

    result = make_source_result(source["name"], source["url"], now, "official_listing")
    records = []

    try:
        response = client.get(source["url"])
    except requests.RequestException as error:
        result["http_status"] = error_status(error)
        result["error"] = str(error)
        return records, result

    result["http_status"] = response.status_code
    soup = BeautifulSoup(response.text, "html.parser")
    seen_urls = set()
    skipped = Counter()

    # With an item_selector only real listing entries are read; otherwise every link.
    selector = source.get("item_selector")
    links = soup.select(selector) if selector else soup.find_all("a", href=True)
    if selector and not links:
        result["error"] = f"no listing items matched '{selector}' (layout change?)"

    for link in links:
        link_title = link.get_text(" ", strip=True)
        article_url = urldefrag(urljoin(source["url"], link["href"])).url
        if not article_url.startswith(("http://", "https://")):
            continue
        if article_url.rstrip("/") == source["url"].rstrip("/") or article_url in seen_urls:
            continue
        seen_urls.add(article_url)
        result["items_seen"] += 1

        # Decided from the title alone: the article page is not fetched.
        if len(link_title) < 8 or not mentions_accident(link_title):
            skipped["not_road_crash_title"] += 1
            add_example(result, "not_road_crash_title", link_title, article_url)
            continue

        listing_date, listing_date_only = date_near_link(link)
        record = make_record(
            source_name=source["name"],
            source_type="official_listing",
            collection_method="html_listing",
            title=link_title,
            url=article_url,
            collected_at=now,
            listing_url=source["url"],
            publisher=source["name"],
            listing_published_at=iso(listing_date),
        )

        if listing_date is None:
            # Don't fetch an undated article just to discover its date.
            record["status"] = DATE_UNKNOWN
            records.append(record)
            continue

        if not is_recent(listing_date, since, now=now, date_only=listing_date_only):
            skipped["published_outside_window"] += 1  # known old: not fetched or saved
            add_example(result, "published_outside_window", link_title, article_url)
            continue

        try:
            metadata = extract_article_metadata(client.get(article_url).text)
        except requests.RequestException as error:
            # The listing date is recent, but the article page refused access.
            record.update(published_at=iso(listing_date), status=RECENT_LISTING_ONLY, fetch_error=str(error))
            records.append(record)
            continue

        record["title"] = metadata["title"] or link_title
        record["publisher"] = metadata["publisher"] or source["name"]
        record["description"] = metadata["description"]
        article_date = metadata["published_at"]

        if article_date is None:
            record.update(published_at=iso(listing_date), status=RECENT_LISTING_DATE)
        elif is_recent(article_date, since, now=now, date_only=metadata["date_only"]):
            record.update(published_at=iso(article_date), status=RECENT)
        else:
            # The article's own date contradicts the listing date: flag for review.
            record.update(published_at=iso(article_date), status=DATE_CONFLICT_REVIEW)

        records.append(record)

    result["candidate_count"] = len(records)
    result["skipped"] = dict(skipped)
    return records, result
