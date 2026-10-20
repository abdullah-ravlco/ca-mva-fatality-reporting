"""Call JEV (TypeSafe System One) and report every failure precisely.

  POST {JEV_API_URL}/systemone   Authorization: Bearer {JEV_API_KEY}
  {"model": JEV_MODEL, "state": "<articles>", "questions": {name: {"type": "choice"|"noul"|"score", ...}}}
  -> {"model": ..., "answers": {name: {"type": ..., "choice"/"noul"/"score": ..., "confidence": ...}}, "usage": ...}
JEV classifies; it does not extract free text. The questions and the mapping of its answers to the
screening facts are in questions.py. Only _payload() and _answers() know the request/response format.

Every failure raises JevError with: kind, HTTP status, a short excerpt of the response, the request
id (if JEV sends one), the number of attempts, the elapsed time, and a hint about what to check.
Rate limits (429), server errors (5xx), timeouts and dropped connections are retried with backoff;
other errors are not, because repeating the same request would fail the same way.
"""

import logging
import re
import time

import requests

from src.modules.severity_screening import config

logger = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_BACKOFF_SECONDS = 60
EXCERPT_CHARS = 600
REQUEST_ID_HEADERS = ("x-request-id", "request-id", "x-amzn-requestid", "cf-ray")
CONTEXT_ERROR_RE = re.compile(r"context|too long|too many tokens|maximum.*tokens|token limit", re.I)

HINTS = {
    "config": "fill in JEV_API_KEY in .env",
    401: "authentication failed: check JEV_API_KEY",
    403: "access denied: check JEV_API_KEY and that it may use JEV_MODEL",
    404: "endpoint not found: JEV_API_URL should be https://api.typesafe.ai/v1 (the client adds /systemone)",
    413: "request too large: set JEV_MAX_INPUT_CHARS in .env to limit the article text sent",
    422: "JEV rejected the request (state or questions): see response_excerpt",
    429: "rate limited by JEV even after retries: lower the run size or ask for a higher limit",
    "server": "JEV server error even after retries: JEV may be down; try again later",
    "timeout": "no answer within JEV_TIMEOUT_SECONDS: raise it in .env or check that JEV is up",
    "network": "could not connect to JEV: check JEV_API_URL and the network",
    "bad_response": "JEV answered, but not in the System One format (answers for every question): see response_excerpt",
    "context": "input too long for JEV's context window: set JEV_MAX_INPUT_CHARS in .env",
}


class JevError(Exception):
    def __init__(self, kind, message, status=None, response_excerpt=None, request_id=None,
                 attempts=0, elapsed_seconds=None, details=None):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.response_excerpt = response_excerpt
        self.request_id = request_id
        self.attempts = attempts
        self.elapsed_seconds = elapsed_seconds
        self.details = details
        self.hint = HINTS.get(status) or HINTS.get(kind)

    def to_dict(self):
        return {
            "kind": self.kind,
            "message": str(self),
            "http_status": self.status,
            "hint": self.hint,
            "request_id": self.request_id,
            "attempts": self.attempts,
            "elapsed_seconds": self.elapsed_seconds,
            "response_excerpt": self.response_excerpt,
            "details": self.details,
        }

    def __str__(self):
        parts = [super().__str__()]
        if self.status:
            parts.append(f"HTTP {self.status}")
        if self.hint:
            parts.append(f"hint: {self.hint}")
        return " | ".join(parts)


def _excerpt(text):
    text = (text or "").strip()
    return text[:EXCERPT_CHARS] + ("..." if len(text) > EXCERPT_CHARS else "")


def _request_id(response):
    for header in REQUEST_ID_HEADERS:
        if response.headers.get(header):
            return response.headers[header]
    return None


def _retry_after(response, attempt):
    value = (response.headers.get("Retry-After") or "").strip() if response is not None else ""
    if value.isdigit():
        return min(int(value), MAX_BACKOFF_SECONDS)
    return min(2 ** attempt, MAX_BACKOFF_SECONDS)


class JevClient:
    def __init__(self, url, api_key, model, timeout=120, max_retries=4, long_context=True, session=None, sleep=time.sleep):
        url = url.rstrip("/")
        self.endpoint = url if url.endswith("/systemone") else url + "/systemone"
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.long_context = long_context
        self.session = session or requests.Session()
        self.sleep = sleep

    @classmethod
    def from_env(cls):
        if not config.JEV_API_KEY:
            raise JevError("config", "JEV is not configured: JEV_API_KEY empty in .env")
        return cls(config.JEV_API_URL, config.JEV_API_KEY, config.JEV_MODEL, config.JEV_TIMEOUT_SECONDS,
                   config.JEV_MAX_RETRIES, config.JEV_LONG_CONTEXT)

    # ---- request / response format (the only JEV-specific part)

    def _headers(self):
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        if self.long_context:
            headers["TypeSafe-Danger-Zone-Long-Context"] = "true"  # the articles are sent in full
        return headers

    def _payload(self, state, questions):
        return {"model": self.model, "state": state, "questions": questions}

    @staticmethod
    def _answers(body, questions):
        answers = body["answers"]
        missing = [name for name in questions if not isinstance(answers.get(name), dict)]
        if missing:
            raise KeyError(f"no answer for {', '.join(missing)}")
        return answers

    # ---- calls

    def ask(self, state, questions):
        """(answers, meta): answers is {question name: answer dict}. Raises JevError."""
        started = time.monotonic()
        attempt = 0
        while True:
            attempt += 1
            elapsed = lambda: round(time.monotonic() - started, 1)  # noqa: E731
            try:
                response = self.session.post(
                    self.endpoint,
                    headers=self._headers(),
                    json=self._payload(state, questions),
                    timeout=self.timeout,
                )
            except requests.Timeout as error:
                if attempt <= self.max_retries:
                    logger.warning("JEV timeout (attempt %s), retrying: %s", attempt, error)
                    self.sleep(_retry_after(None, attempt))
                    continue
                raise JevError("timeout", f"JEV did not answer in {self.timeout}s", attempts=attempt, elapsed_seconds=elapsed()) from error
            except requests.RequestException as error:
                if attempt <= self.max_retries:
                    logger.warning("JEV connection error (attempt %s), retrying: %s", attempt, error)
                    self.sleep(_retry_after(None, attempt))
                    continue
                raise JevError("network", f"could not reach JEV: {error}", attempts=attempt, elapsed_seconds=elapsed()) from error

            status, request_id = response.status_code, _request_id(response)
            if status == 200:
                try:
                    answers = self._answers(response.json(), questions)
                except (ValueError, KeyError, TypeError, AttributeError) as error:
                    raise JevError("bad_response", f"unexpected JEV response: {type(error).__name__}: {error}",
                                   status, _excerpt(response.text), request_id, attempt, elapsed()) from error
                return answers, {"attempts": attempt, "elapsed_seconds": elapsed(), "request_id": request_id}

            body = response.text
            if status in RETRY_STATUSES and attempt <= self.max_retries:
                wait = _retry_after(response, attempt)
                logger.warning("JEV HTTP %s (attempt %s, request %s), retrying in %ss: %s",
                               status, attempt, request_id, wait, _excerpt(body)[:200])
                self.sleep(wait)
                continue

            kind = "server" if status >= 500 else ("context" if status in (400, 413) and CONTEXT_ERROR_RE.search(body) else "http")
            raise JevError(kind, "JEV request failed", status, _excerpt(body), request_id, attempt, elapsed())
