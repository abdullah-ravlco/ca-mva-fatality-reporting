"""Fallback for article pages that answer HTTP 401/403.

Many news sites (e.g. FOX 5 San Diego, KRON4, KTLA) run on WordPress, which also
publishes posts through its public REST API: /wp-json/wp/v2/posts?slug=<slug>.
That endpoint is sometimes not behind the same block as the HTML page. If the site
isn't WordPress, or the API is blocked too, this fails with PageFetchError.
"""

import html as html_lib
from urllib.parse import quote, urlsplit

from src.modules.link_extraction.page_fetcher import PageFetchError, http_get


def article_slug(url):
    """Last non-empty path segment: '/traffic/fatal-i-805-crash-national-city/' -> slug."""
    segments = [s for s in urlsplit(url).path.split("/") if s]
    return segments[-1] if segments else None


def fetch_wordpress_article(url, timeout=20):
    """Return (html, api_url) with the post's title and body as a small HTML page."""
    parts = urlsplit(url)
    slug = article_slug(url)
    if not slug:
        raise PageFetchError("WordPress fallback: URL has no slug", {"requested_url": url})

    api_url = (
        f"{parts.scheme}://{parts.netloc}/wp-json/wp/v2/posts"
        f"?slug={quote(slug)}&_fields=title,content,date,link"
    )
    diagnostics = {"requested_url": url, "api_url": api_url}

    try:
        # Same browser-impersonating client as page fetches (curl_cffi when installed).
        response = http_get(api_url, accept="application/json")
    except Exception as exc:
        raise PageFetchError(f"WordPress fallback failed: {exc}", diagnostics) from exc

    diagnostics.update({
        "status_code": response.status_code,
        "server": response.headers.get("Server"),
        "content_type": response.headers.get("Content-Type"),
    })
    if response.status_code >= 400:
        raise PageFetchError(f"WordPress fallback HTTP {response.status_code}", diagnostics)

    try:
        posts = response.json()
    except ValueError as exc:
        raise PageFetchError("WordPress fallback: response is not JSON (not a WordPress site?)", diagnostics) from exc

    if not isinstance(posts, list) or not posts:
        raise PageFetchError("WordPress fallback: no post found for this slug", diagnostics)

    post = posts[0]
    title = html_lib.escape(html_lib.unescape((post.get("title") or {}).get("rendered") or ""))
    body = (post.get("content") or {}).get("rendered") or ""
    page = f"<html><head><title>{title}</title></head><body><article><h1>{title}</h1>{body}</article></body></html>"
    return page, api_url
