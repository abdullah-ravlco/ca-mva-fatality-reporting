"""Last-resort fallback: the Internet Archive's Wayback Machine copy of an article.

Used when the publisher's page is blocked (403, CAPTCHA), rate-limited or timing out.
Recent news articles are often archived within hours, but not always; when there is
no snapshot this fails with PageFetchError.
"""

import re
from urllib.parse import quote

from src.modules.link_extraction.page_fetcher import PageFetchError, fetch_page_with_url, http_get


AVAILABILITY_API = "https://archive.org/wayback/available?url={url}"
SNAPSHOT_TIMESTAMP_RE = re.compile(r"/web/(\d+)/")


def fetch_archived_article(url):
    """Return (html, snapshot_url) for the closest Wayback Machine snapshot."""
    api_url = AVAILABILITY_API.format(url=quote(url, safe=""))
    diagnostics = {"requested_url": url, "api_url": api_url}

    try:
        response = http_get(api_url, accept="application/json")
        data = response.json() if response.status_code < 400 else {}
    except Exception as exc:
        raise PageFetchError(f"Wayback lookup failed: {exc}", diagnostics) from exc

    snapshot = ((data or {}).get("archived_snapshots") or {}).get("closest") or {}
    if not snapshot.get("available") or not snapshot.get("url"):
        raise PageFetchError("No Wayback Machine snapshot of this article", diagnostics)

    # "id_" asks for the original page without the Wayback toolbar.
    snapshot_url = SNAPSHOT_TIMESTAMP_RE.sub(r"/web/\1id_/", snapshot["url"], count=1)
    diagnostics["snapshot_url"] = snapshot_url
    diagnostics["snapshot_timestamp"] = snapshot.get("timestamp")

    try:
        html, _final_url = fetch_page_with_url(snapshot_url)
    except PageFetchError as exc:
        raise PageFetchError(f"Wayback snapshot fetch failed: {exc}", dict(diagnostics, snapshot_error=exc.diagnostics)) from exc
    return html, snapshot_url
