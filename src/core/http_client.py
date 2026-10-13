
"""Shared HTTP client: one session, retries for temporary errors, per-host delay."""

import time
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config.settings import BROWSER_USER_AGENT, REQUEST_DELAY_SECONDS, REQUEST_TIMEOUT_SECONDS, USER_AGENT


class HttpClient:
    def __init__(self, delay_seconds=REQUEST_DELAY_SECONDS, timeout=REQUEST_TIMEOUT_SECONDS):
        self.delay_seconds = delay_seconds
        self.timeout = timeout
        self._last_request = {}
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
        host = urlsplit(url).netloc
        last = self._last_request.get(host)
        if last is not None:
            remaining = self.delay_seconds - (time.monotonic() - last)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request[host] = time.monotonic()

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
