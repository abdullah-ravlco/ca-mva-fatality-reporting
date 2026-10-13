"""Find more articles about a shortlisted incident with Google News RSS.

Queries use the person's full name (and the original headline), so follow-up
reports such as "victim identified", arrests or obituaries are found. A result
is kept only if its text names one of the incident's people.
"""

import re
from urllib.parse import quote_plus

import feedparser
import requests

from src.modules.lead_verification.config import (
    BLOCKED_HOST_RE,
    GOOGLE_NEWS_RSS_URL,
    MAX_FETCHES_PER_INCIDENT,
    MAX_NEW_SOURCES_PER_INCIDENT,
    MAX_QUERIES_PER_INCIDENT,
    MAX_RESULTS_PER_QUERY,
    REQUEST_USER_AGENT,
    SEARCH_WINDOW_DAYS,
)
from src.modules.lead_verification.incidents import host_of, mentions_any, name_tokens


# " - KTLA" / " | ABC7" suffixes are dropped from headlines used as queries.
TITLE_SUFFIX_RE = re.compile(r"\s+[-|–]\s+[^-|–]{2,40}$")


def build_queries(incident):
    queries = []
    for name in incident["names"]:
        words = name.split()
        if len(name_tokens(name)) >= 2:
            # "Jeorjina B. Espericueta" is often reported as "Jeorjina Espericueta".
            short = f"{words[0]} {words[-1]}"
            queries.append(f'"{short}"')
            queries.append(f'"{short}" crash')
    for person in incident["people"]:
        title = TITLE_SUFFIX_RE.sub("", person.get("article_title") or "").strip()
        if title:
            queries.append(title)
            break

    unique = []
    for query in queries:
        if query not in unique:
            unique.append(query)
    return unique[:MAX_QUERIES_PER_INCIDENT]


def google_news_items(query):
    """[(title, link, published)] for one Google News RSS search (never raises)."""
    url = GOOGLE_NEWS_RSS_URL.format(query=quote_plus(f"{query} when:{SEARCH_WINDOW_DAYS}d"))
    try:
        response = requests.get(url, headers={"User-Agent": REQUEST_USER_AGENT}, timeout=20)
        response.raise_for_status()
    except requests.RequestException:
        return []
    feed = feedparser.parse(response.content)
    return [
        (entry.get("title"), entry.get("link"), entry.get("published"))
        for entry in feed.entries[:MAX_RESULTS_PER_QUERY]
        if entry.get("link")
    ]


def search_followups(incident, known_urls, fetcher, log):
    """New source dicts for this incident; log gets queries and every fetch."""
    sources = []
    fetches = 0
    log["queries"] = build_queries(incident)
    log.setdefault("fetched", [])

    for query in log["queries"]:
        for title, link, published in google_news_items(query):
            if len(sources) >= MAX_NEW_SOURCES_PER_INCIDENT or fetches >= MAX_FETCHES_PER_INCIDENT:
                return sources
            if link in known_urls:
                continue
            known_urls.add(link)

            result = fetcher.fetch(link)
            if not result.get("cached"):
                fetches += 1
            url = result.get("url") or link
            entry = {"query": query, "title": title, "url": url}

            if url in known_urls and url != link:
                entry["result"] = "duplicate"
            elif BLOCKED_HOST_RE.search(host_of(url)):
                entry["result"] = "blocked_domain"
            elif not result.get("ok"):
                entry["result"] = "fetch_failed"
                entry["error"] = result.get("error")
            elif not mentions_any(result["text"], incident["names"]):
                entry["result"] = "does_not_name_person"
            else:
                entry["result"] = "added"
                sources.append({
                    "url": url,
                    "title": title,
                    "publisher": host_of(url),
                    "published_at": published,
                    "origin": "followup_search",
                    "text": result["text"],
                })
            known_urls.add(url)
            log["fetched"].append(entry)

    return sources
