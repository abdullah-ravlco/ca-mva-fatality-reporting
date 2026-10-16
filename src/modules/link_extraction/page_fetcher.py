"""Fetch article pages.

Uses curl_cffi (libcurl with browser impersonation: real Chrome/Safari TLS and HTTP/2
fingerprints) when it is installed, otherwise plain requests. Handles:
- HTTP 401/403: retried once with a second browser profile.
- HTTP 429: waits for Retry-After (up to MAX_RETRY_AFTER_SECONDS) and retries once.
- Read timeouts: retried once with a longer timeout.
- CAPTCHA / bot-verification pages: detected and reported (never solved).
"""

import re
import threading
import time
from collections import Counter
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests

try:
    from curl_cffi import requests as cffi_requests
    HAVE_CURL_CFFI = True
except ImportError:  # pip install curl_cffi
    cffi_requests = None
    HAVE_CURL_CFFI = False


IMPERSONATE_PROFILES = ("chrome", "safari")
READ_TIMEOUT_SECONDS = 30
RETRY_READ_TIMEOUT_SECONDS = 60
MIN_SECONDS_PER_HOST = 2.0
MAX_RETRY_AFTER_SECONDS = 60

# Used only when curl_cffi is not installed (curl_cffi sets matching browser headers itself).
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Pages that ask for human verification instead of serving the article.
BLOCK_PAGE_MARKERS = [
    ("perimeterx_captcha", re.compile(r"px-captcha|_pxCaptcha|perimeterx", re.I)),
    ("cloudflare_challenge", re.compile(
        r"cf-chl-|challenge-platform|<title>\s*Just a moment\.\.\.|Attention Required! \| Cloudflare", re.I)),
    ("datadome_captcha", re.compile(r"captcha-delivery\.com|datadome", re.I)),
    ("captcha", re.compile(
        r"g-recaptcha|h-captcha|hcaptcha\.com|verify (?:that )?you are (?:a )?human|are you a robot|"
        r"robot verification|bot verification|access to this page has been denied",
        re.I,
    )),
]

_next_slot_by_host = {}
_slot_lock = threading.Lock()

# After this many timeouts / HTTP 429s from one host in a run, stop spending time on it:
# no longer timeout, no retry, no Retry-After wait.
HOST_FAILURE_LIMIT = 2
FAST_READ_TIMEOUT_SECONDS = 12
_host_timeouts = Counter()
_host_rate_limits = Counter()


class PageFetchError(Exception):
    def __init__(self, message, diagnostics=None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


def _wait_for_host(url):
    """Space requests to one host MIN_SECONDS_PER_HOST apart; safe across threads (different
    hosts never wait for each other)."""
    host = urlsplit(url).netloc
    with _slot_lock:
        now = time.monotonic()
        slot = max(now, _next_slot_by_host.get(host, 0.0))
        _next_slot_by_host[host] = slot + MIN_SECONDS_PER_HOST
    if slot > now:
        time.sleep(slot - now)


def http_get(url, timeout=READ_TIMEOUT_SECONDS, profile="chrome", accept=None):
    """One GET request. Browser impersonation via curl_cffi when available."""
    _wait_for_host(url)
    if HAVE_CURL_CFFI:
        headers = {"Accept-Language": "en-US,en;q=0.9"}
        if accept:
            headers["Accept"] = accept
        return cffi_requests.get(url, headers=headers, timeout=timeout, allow_redirects=True, impersonate=profile)
    headers = dict(HEADERS, Accept=accept) if accept else HEADERS
    return requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)


def _is_timeout(exc):
    return "timeout" in type(exc).__name__.lower() or "timed out" in str(exc).lower()


def _retry_after_seconds(response):
    value = (response.headers.get("Retry-After") or "").strip()
    if not value:
        return 10
    if value.isdigit():
        return int(value)
    try:
        return max(0, int(parsedate_to_datetime(value).timestamp() - time.time()))
    except (TypeError, ValueError, IndexError, OverflowError):
        return 10


SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript)\b.*?</\1>", re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")
# Challenge pages carry almost no readable text; real articles that merely embed a
# reCAPTCHA (comment/newsletter forms) or a bot-detection script have much more.
MAX_BLOCK_PAGE_TEXT_CHARS = 2000


def visible_text_length(html):
    text = TAG_RE.sub(" ", SCRIPT_STYLE_RE.sub(" ", html or ""))
    return len(" ".join(text.split()))


def detect_block_page(html):
    """Name of the verification system if the page is a CAPTCHA/challenge, else None."""
    for name, pattern in BLOCK_PAGE_MARKERS:
        if pattern.search(html or ""):
            if visible_text_length(html) <= MAX_BLOCK_PAGE_TEXT_CHARS:
                return name
            return None
    return None


def _protection(response, html):
    server = (response.headers.get("Server") or "").lower()
    if "cloudflare" in server or response.headers.get("CF-Ray"):
        return "cloudflare"
    return detect_block_page(html) or (server or None)


def _diagnostics(url, response, profile):
    try:
        body = response.text
    except Exception:
        body = ""
    return {
        "requested_url": url,
        "final_url": str(response.url),
        "status_code": response.status_code,
        "reason": getattr(response, "reason", None),
        "client": f"curl_cffi:{profile}" if HAVE_CURL_CFFI else "requests",
        "content_type": response.headers.get("Content-Type"),
        "server": response.headers.get("Server"),
        "via": response.headers.get("Via"),
        "cf_ray": response.headers.get("CF-Ray"),
        "retry_after": response.headers.get("Retry-After"),
        "protection": _protection(response, body),
        "response_body_sample": body[:1500],
    }


def _get_with_retries(url, profile):
    """GET with one longer-timeout retry and one Retry-After wait on HTTP 429."""
    host = urlsplit(url).netloc
    waited_for_429 = _host_rate_limits[host] >= HOST_FAILURE_LIMIT
    slow_host = _host_timeouts[host] >= HOST_FAILURE_LIMIT
    timeout = FAST_READ_TIMEOUT_SECONDS if slow_host else READ_TIMEOUT_SECONDS

    while True:
        try:
            response = http_get(url, timeout=timeout, profile=profile)
        except Exception as exc:
            if _is_timeout(exc):
                _host_timeouts[host] += 1
                if not slow_host and timeout != RETRY_READ_TIMEOUT_SECONDS:
                    timeout = RETRY_READ_TIMEOUT_SECONDS
                    continue
            raise PageFetchError(
                f"{'Read timeout' if _is_timeout(exc) else 'Request failed'}: {exc}",
                {
                    "requested_url": url,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                    "timeout": "timeout" if _is_timeout(exc) else None,
                    "client": f"curl_cffi:{profile}" if HAVE_CURL_CFFI else "requests",
                },
            ) from exc

        if response.status_code == 429:
            _host_rate_limits[host] += 1
        if response.status_code == 429 and not waited_for_429:
            delay = _retry_after_seconds(response)
            if delay <= MAX_RETRY_AFTER_SECONDS:
                waited_for_429 = True
                time.sleep(delay)
                continue
        return response


def fetch_page(url: str, timeout: int = 20) -> str:
    """Return the page HTML (unchanged behavior)."""
    html, _final_url = fetch_page_with_url(url, timeout)
    return html


def fetch_page_with_url(url: str, timeout: int = 20):
    """Return (html, final_url). The final URL shows where redirects ended, e.g.
    whether a Google News link actually reached the publisher's article.

    `timeout` is kept for compatibility; READ_TIMEOUT_SECONDS / RETRY_READ_TIMEOUT_SECONDS apply.
    """
    profiles = IMPERSONATE_PROFILES if HAVE_CURL_CFFI else ("requests",)
    last_error = None

    for position, profile in enumerate(profiles):
        is_last_profile = position == len(profiles) - 1
        response = _get_with_retries(url, profile)
        diagnostics = _diagnostics(url, response, profile)

        if response.status_code in (401, 403):
            last_error = PageFetchError(f"HTTP {response.status_code}: {diagnostics['reason'] or 'Forbidden'}", diagnostics)
            if not is_last_profile:
                continue  # another browser fingerprint sometimes passes
            raise last_error

        if response.status_code >= 400:
            raise PageFetchError(f"HTTP {response.status_code}: {diagnostics['reason'] or ''}".strip(), diagnostics)

        content_type = (response.headers.get("Content-Type") or "").lower()
        if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
            raise PageFetchError(f"Unexpected content type: {content_type}", diagnostics)

        html = response.text
        block = detect_block_page(html)
        if block:
            diagnostics["block_type"] = block
            last_error = PageFetchError(f"Bot verification page returned ({block}); not solved", diagnostics)
            if not is_last_profile:
                continue
            raise last_error

        return html, str(response.url)

    raise last_error or PageFetchError("Request failed", {"requested_url": url})
