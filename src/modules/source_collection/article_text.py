
"""Get readable story text for a feed item: the feed's own full content first,
then the article page (bounded by MAX_ARTICLE_FETCHES).

A blocked or failed page fetch never discards the RSS evidence: the feed text is
kept and "page_extraction" records why the page itself is unavailable.
"""

import requests
from bs4 import BeautifulSoup

from src.core.http_client import browser_headers, error_status
from config.settings import BROWSER_RETRY_ON_403


MAX_TEXT_CHARS = 15000
MIN_PARAGRAPH_CHARS = 40
ENOUGH_TEXT_CHARS = 500  # feed content at least this long is used without fetching the page
NOISE_TAGS = ["script", "style", "noscript", "nav", "footer", "header", "aside", "form", "figure", "iframe"]

# Aggregator links that redirect via JavaScript; fetching them returns no article text.
UNFETCHABLE_HOSTS = ("news.google.com",)

# page_extraction values
NOT_NEEDED = "not_needed_feed_has_full_text"
FETCHED = "fetched"
FETCHED_BROWSER = "fetched_with_browser_headers"
BLOCKED = "blocked"                   # HTTP 401/403 even after the browser-header retry
FAILED = "failed"                     # other HTTP error or network error
UNAVAILABLE_REDIRECT = "unavailable_aggregator_redirect"
SKIPPED_BUDGET = "skipped_fetch_budget"
NO_TEXT = "page_had_no_article_text"


def html_to_text(html):
    """Paragraph text from HTML, without navigation/script noise."""
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(NOISE_TAGS):
        tag.decompose()
    root = soup.find("article") or soup.body or soup
    paragraphs = [" ".join(p.get_text(" ", strip=True).split()) for p in root.find_all("p")]
    paragraphs = [p for p in paragraphs if len(p) >= MIN_PARAGRAPH_CHARS]
    text = "\n".join(paragraphs) if paragraphs else " ".join(root.get_text(" ", strip=True).split())
    return text[:MAX_TEXT_CHARS]


def feed_item_text(item):
    """Full story text when the feed includes it (WordPress feeds use content:encoded)."""
    for content in item.get("content") or []:
        text = html_to_text(content.get("value"))
        if text:
            return text
    return ""


def _result(text, text_source, page_extraction, fetch_error=None):
    return {
        "text": text,
        "text_source": text_source,          # feed_content | article_page | rss_summary_only
        "page_extraction": page_extraction,
        "fetch_error": fetch_error,
    }


class ArticleTextFetcher:
    """Fetches article pages, up to a fixed number per run."""

    def __init__(self, client, max_fetches):
        self.client = client
        self.remaining = max_fetches

    def _fetch(self, url):
        """(page_text, page_extraction, fetch_error) for one article URL."""
        try:
            return html_to_text(self.client.get(url).text), FETCHED, None
        except requests.RequestException as error:
            status = error_status(error)
            if status not in (401, 403):
                return "", FAILED, f"HTTP {status}" if status else str(error)
            if not BROWSER_RETRY_ON_403:
                return "", BLOCKED, f"HTTP {status}"

        try:
            response = self.client.get(url, headers=browser_headers(url))
            return html_to_text(response.text), FETCHED_BROWSER, None
        except requests.RequestException as error:
            status = error_status(error)
            return "", BLOCKED, f"HTTP {status} (also with browser headers)" if status else str(error)

    def text_for(self, item, url):
        """Best available story text for one feed item, plus how it was obtained."""
        feed_text = feed_item_text(item)
        fallback_source = "feed_content" if feed_text else "rss_summary_only"

        if len(feed_text) >= ENOUGH_TEXT_CHARS:
            return _result(feed_text, "feed_content", NOT_NEEDED)
        if any(host in url for host in UNFETCHABLE_HOSTS):
            return _result(feed_text, fallback_source, UNAVAILABLE_REDIRECT)
        if self.remaining <= 0:
            return _result(feed_text, fallback_source, SKIPPED_BUDGET, "article fetch budget used up")

        self.remaining -= 1
        page_text, page_extraction, fetch_error = self._fetch(url)

        if page_text and len(page_text) > len(feed_text):
            return _result(page_text, "article_page", page_extraction)
        if page_extraction in (FETCHED, FETCHED_BROWSER):
            page_extraction = NO_TEXT
        return _result(feed_text, fallback_source, page_extraction, fetch_error)
