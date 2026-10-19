"""Call SearchBug's Enhanced People Search and report every failure precisely.

  POST https://data.searchbug.com/api/search.aspx   (form fields)
  CO_CODE=<account>  PASS=<api key>  TYPE=api_ppl  FORMAT=JSON  + search fields

Search fields: FNAME, MNAME, LNAME, CITY, STATE, ZIP, ADDRESS, or reportToken alone (a person or
relative from an earlier search). Every failure raises SearchBugError with the HTTP status, a
response excerpt, attempts, elapsed time and a hint. 429, 5xx, timeouts and dropped connections are
retried; other errors are not. Credentials are never written to logs or error files.
"""

import json
import logging
import time

import requests

from src.modules.contact_enrichment import config

logger = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}
EXCERPT_CHARS = 600
# Keys SearchBug-style APIs use for an error inside a 200 response.
ERROR_KEYS = ("Error", "error", "ErrorMessage", "errorMessage", "errors")
AUTH_ERROR_WORDS = ("password", "co_code", "account", "auth", "credential", "api key", "apikey")
BALANCE_ERROR_WORDS = ("balance", "funds", "credit", "insufficient")
NO_RESULT_WORDS = ("no record", "no result", "not found", "no match", "no data", "noresults")
TOO_MANY_WORDS = ("too many results", "p001")

HINTS = {
    "config": "fill in SEARCHBUG_CO_CODE and SEARCHBUG_API_KEY in .env",
    "auth": "SearchBug rejected the credentials: check SEARCHBUG_CO_CODE and SEARCHBUG_API_KEY",
    "balance": "SearchBug account balance is too low: add funds to the prepaid balance",
    401: "authentication failed: check SEARCHBUG_CO_CODE and SEARCHBUG_API_KEY",
    403: "access denied: check the account and that it has access to api_ppl",
    404: "endpoint not found: check SEARCHBUG_API_URL",
    429: "rate limited by SearchBug even after retries: try again later",
    "server": "SearchBug server error even after retries: try again later",
    "timeout": "no answer within SEARCHBUG_TIMEOUT_SECONDS: raise it in .env or try again later",
    "network": "could not connect to SearchBug: check SEARCHBUG_API_URL and the network",
    "invalid_json": "SearchBug did not return JSON: see response_excerpt (is FORMAT=JSON accepted?)",
    "api_error": "SearchBug returned an error message: see message and response_excerpt",
    "too_many_results": "the search matched too many people: a narrower search (city) is needed",
}


class SearchBugError(Exception):
    def __init__(self, kind, message, status=None, response_excerpt=None, attempts=0, elapsed_seconds=None):
        super().__init__(message)
        self.kind, self.status = kind, status
        self.response_excerpt, self.attempts, self.elapsed_seconds = response_excerpt, attempts, elapsed_seconds
        self.hint = HINTS.get(status) or HINTS.get(kind)

    def to_dict(self):
        return {"kind": self.kind, "message": super().__str__(), "http_status": self.status, "hint": self.hint,
                "attempts": self.attempts, "elapsed_seconds": self.elapsed_seconds,
                "response_excerpt": self.response_excerpt}

    def __str__(self):
        parts = [super().__str__()] + ([f"HTTP {self.status}"] if self.status else []) + ([f"hint: {self.hint}"] if self.hint else [])
        return " | ".join(parts)


def _excerpt(text):
    text = (text or "").strip()
    return text[:EXCERPT_CHARS] + ("..." if len(text) > EXCERPT_CHARS else "")


def api_error_message(data):
    """An error message carried inside a 200 JSON response, or None."""
    if not isinstance(data, dict):
        return None
    for key in ERROR_KEYS:
        value = data.get(key)
        if value:
            return value if isinstance(value, str) else json.dumps(value)[:300]
    return None


class SearchBugClient:
    def __init__(self, url, co_code, api_key, timeout=60, max_retries=3, session=None, sleep=time.sleep):
        self.url, self.co_code, self.api_key = url, co_code, api_key
        self.timeout, self.max_retries = timeout, max_retries
        self.session = session or requests.Session()
        self.sleep = sleep

    @classmethod
    def from_env(cls):
        missing = [name for name in ("SEARCHBUG_CO_CODE", "SEARCHBUG_API_KEY") if not getattr(config, name)]
        if missing:
            raise SearchBugError("config", f"SearchBug is not configured: {', '.join(missing)} empty in .env")
        return cls(config.SEARCHBUG_API_URL, config.SEARCHBUG_CO_CODE, config.SEARCHBUG_API_KEY,
                   config.SEARCHBUG_TIMEOUT_SECONDS, config.SEARCHBUG_MAX_RETRIES)

    def search(self, fields):
        """The parsed JSON for one api_ppl search (fields: search inputs only, no credentials)."""
        form = {"CO_CODE": self.co_code, "PASS": self.api_key, "TYPE": "api_ppl", "FORMAT": "JSON",
                "REF": config.REFERENCE, **fields}
        if "reportToken" not in fields:
            form.setdefault("LIMIT", config.SEARCHBUG_RESULT_LIMIT)
        started, attempt = time.monotonic(), 0
        while True:
            attempt += 1
            elapsed = round(time.monotonic() - started, 1)
            try:
                response = self.session.post(self.url, data=form, timeout=self.timeout)
            except requests.Timeout as error:
                if attempt <= self.max_retries:
                    logger.warning("SearchBug timeout (attempt %s), retrying", attempt)
                    self.sleep(min(2 ** attempt, 30))
                    continue
                raise SearchBugError("timeout", f"SearchBug did not answer in {self.timeout}s", attempts=attempt, elapsed_seconds=elapsed) from error
            except requests.RequestException as error:
                if attempt <= self.max_retries:
                    logger.warning("SearchBug connection error (attempt %s), retrying: %s", attempt, type(error).__name__)
                    self.sleep(min(2 ** attempt, 30))
                    continue
                raise SearchBugError("network", f"could not reach SearchBug: {type(error).__name__}", attempts=attempt, elapsed_seconds=elapsed) from error

            status = response.status_code
            if status in RETRY_STATUSES and attempt <= self.max_retries:
                logger.warning("SearchBug HTTP %s (attempt %s), retrying", status, attempt)
                self.sleep(min(2 ** attempt, 30))
                continue
            if status != 200:
                kind = "server" if status >= 500 else "http"
                raise SearchBugError(kind, "SearchBug request failed", status, _excerpt(response.text), attempt, elapsed)
            try:
                data = response.json()
            except ValueError as error:
                raise SearchBugError("invalid_json", "SearchBug response is not JSON", status, _excerpt(response.text), attempt, elapsed) from error
            message = api_error_message(data)
            if message:
                lowered = message.lower()
                if any(w in lowered for w in TOO_MANY_WORDS):
                    raise SearchBugError("too_many_results", f"SearchBug: {message}", status, _excerpt(response.text), attempt, elapsed)
                if any(w in lowered for w in NO_RESULT_WORDS):
                    return {}  # "nothing found" is an answer, not a failure
                kind = ("auth" if any(w in lowered for w in AUTH_ERROR_WORDS)
                        else "balance" if any(w in lowered for w in BALANCE_ERROR_WORDS) else "api_error")
                raise SearchBugError(kind, f"SearchBug error: {message}", status, _excerpt(response.text), attempt, elapsed)
            return data
