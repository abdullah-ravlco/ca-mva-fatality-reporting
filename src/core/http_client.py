
"""Shared HTTP client: one session, retries for temporary errors, per-host delay."""

import threading
import time
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config.settings import BROWSER_USER_AGENT, REQUEST_DELAY_SECONDS, REQUEST_TIMEOUT_SECONDS, USER_AGENT


class HttpClient:
    def __init__(self, delay_seconds=REQUEST_DELAY_SECONDS, timeout=REQUEST_TIMEOUT_SECONDS, host_delays=None):
        self.delay_seconds = delay_seconds
        self.host_delays = host_delays or {}   # host -> delay, for hosts that tolerate faster requests
        self.timeout = timeout
        self._next_slot = {}
        self._lock = threading.Lock()
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.8",
        })
        retry = Retry(
            total=3,
            backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _wait_for_host(self, url):
        """Reserve the next request slot for the host, then wait for it. Safe across threads:
        threads asking for the same host are spaced `delay` apart, different hosts do not wait."""
        host = urlsplit(url).netloc
        delay = self.host_delays.get(host, self.delay_seconds)
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot.get(host, 0.0))
            self._next_slot[host] = slot + delay
        if slot > now:
            time.sleep(slot - now)

    def get(self, url, headers=None):
        """GET a URL. Raises requests.RequestException (incl. HTTPError for 4xx/5xx).

        headers: optional per-request headers that override the session defaults.
        """
        self._wait_for_host(url)
        response = self.session.get(url, timeout=self.timeout, headers=headers)
        response.raise_for_status()
        return response


def browser_headers(url):
    """Headers a normal desktop browser sends when opening an article from the site's
    homepage. Used only for one retry after an HTTP 403 (see BROWSER_RETRY_ON_403)."""
    parts = urlsplit(url)
    return {
        "User-Agent": BROWSER_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"{parts.scheme}://{parts.netloc}/",
        "Upgrade-Insecure-Requests": "1",
    }


def error_status(error):
    """HTTP status code from a requests exception, if it has one."""
    response = getattr(error, "response", None)
    return response.status_code if response is not None else None
