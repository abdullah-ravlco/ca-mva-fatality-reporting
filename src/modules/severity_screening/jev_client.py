"""Call JEV and report every failure precisely.

JEV is called in the OpenAI chat-completions format:
  POST {JEV_API_URL}/chat/completions   Authorization: Bearer {JEV_API_KEY}
  {"model": JEV_MODEL, "messages": [...], "temperature": 0, "response_format": {"type": "json_object"}}
and the reply text is read from choices[0].message.content. If JEV turns out to use another
format, only _payload() and _content() need to change.

Every failure raises JevError with: kind, HTTP status, a short excerpt of the response, the request
id (if JEV sends one), the number of attempts, the elapsed time, and a hint about what to check.
Rate limits (429), server errors (5xx), timeouts and dropped connections are retried with backoff;
other errors are not, because repeating the same request would fail the same way.
"""

import json
import logging
import re
import time

import requests
from pydantic import ValidationError

from src.modules.severity_screening import config
from src.modules.severity_screening.schema import JevResult

logger = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_BACKOFF_SECONDS = 60
EXCERPT_CHARS = 600
REQUEST_ID_HEADERS = ("x-request-id", "request-id", "x-amzn-requestid", "cf-ray")
CONTEXT_ERROR_RE = re.compile(r"context|too long|too many tokens|maximum.*tokens|token limit", re.I)

HINTS = {
    "config": "fill in JEV_API_URL, JEV_API_KEY and JEV_MODEL in .env",
    401: "authentication failed: check JEV_API_KEY",
    403: "access denied: check JEV_API_KEY and that it may use JEV_MODEL",
    404: "endpoint or model not found: check JEV_API_URL (base URL, e.g. https://host/v1) and JEV_MODEL",
    413: "request too large: set JEV_MAX_INPUT_CHARS in .env to limit the article text sent",
    422: "JEV rejected the request format: see response_excerpt",
    429: "rate limited by JEV even after retries: lower the run size or ask for a higher limit",
    "server": "JEV server error even after retries: JEV may be down; try again later",
    "timeout": "no answer within JEV_TIMEOUT_SECONDS: raise it in .env or check that JEV is up",
    "network": "could not connect to JEV: check JEV_API_URL and the network",
    "bad_response": "JEV answered, but not in the OpenAI chat-completions format: see response_excerpt",
    "invalid_json": "JEV's reply is not valid JSON: see response_excerpt",
    "schema": "JEV's JSON does not match the expected fields/values: see details",
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


def parse_json_reply(text):
    """The JSON object in a model reply (tolerates ```json fences and text around the object)."""
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in the reply")
    return json.loads(text[start:end + 1])


class JevClient:
    def __init__(self, url, api_key, model, timeout=120, max_retries=4, json_mode=True, session=None, sleep=time.sleep):
        self.endpoint = url.rstrip("/") if url.rstrip("/").endswith("/chat/completions") else url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.json_mode = json_mode
        self.session = session or requests.Session()
        self.sleep = sleep

    @classmethod
    def from_env(cls):
        missing = [name for name in ("JEV_API_URL", "JEV_API_KEY", "JEV_MODEL") if not getattr(config, name)]
        if missing:
            raise JevError("config", f"JEV is not configured: {', '.join(missing)} empty in .env")
        return cls(config.JEV_API_URL, config.JEV_API_KEY, config.JEV_MODEL, config.JEV_TIMEOUT_SECONDS,
                   config.JEV_MAX_RETRIES, config.JEV_JSON_MODE)

    # ---- request / response format (the only JEV-specific part)

    def _payload(self, system, user):
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0,
        }
        if self.json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    @staticmethod
    def _content(body):
        return body["choices"][0]["message"]["content"]

    # ---- calls

    def complete(self, system, user):
        """(reply_text, meta). Raises JevError."""
        started = time.monotonic()
        attempt = 0
        while True:
            attempt += 1
            elapsed = lambda: round(time.monotonic() - started, 1)  # noqa: E731
            try:
                response = self.session.post(
                    self.endpoint,
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json=self._payload(system, user),
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
                    content = self._content(response.json())
                except (ValueError, KeyError, IndexError, TypeError) as error:
                    raise JevError("bad_response", f"unexpected JEV response: {type(error).__name__}: {error}",
                                   status, _excerpt(response.text), request_id, attempt, elapsed()) from error
                if not isinstance(content, str) or not content.strip():
                    raise JevError("bad_response", "JEV returned an empty reply", status, _excerpt(response.text),
                                   request_id, attempt, elapsed())
                return content, {"attempts": attempt, "elapsed_seconds": elapsed(), "request_id": request_id,
                                 "json_mode": self.json_mode}

            body = response.text
            if status == 400 and self.json_mode and "response_format" in body:
                logger.warning("JEV does not accept response_format; retrying without JSON mode")
                self.json_mode = False  # the prompt already asks for JSON only
                attempt -= 1
                continue
            if status in RETRY_STATUSES and attempt <= self.max_retries:
                wait = _retry_after(response, attempt)
                logger.warning("JEV HTTP %s (attempt %s, request %s), retrying in %ss: %s",
                               status, attempt, request_id, wait, _excerpt(body)[:200])
                self.sleep(wait)
                continue

            kind = "server" if status >= 500 else ("context" if status in (400, 413) and CONTEXT_ERROR_RE.search(body) else "http")
            raise JevError(kind, f"JEV request failed", status, _excerpt(body), request_id, attempt, elapsed())

    def screen(self, system, user):
        """(JevResult, meta). Raises JevError for API failures and for replies that are not valid."""
        content, meta = self.complete(system, user)
        try:
            data = parse_json_reply(content)
        except ValueError as error:
            raise JevError("invalid_json", f"JEV reply is not valid JSON: {error}", 200, _excerpt(content),
                           meta["request_id"], meta["attempts"], meta["elapsed_seconds"]) from error
        try:
            return JevResult.model_validate(data), meta
        except ValidationError as error:
            details = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']} (got {e.get('input')!r})" for e in error.errors()][:10]
            raise JevError("schema", f"JEV reply has {error.error_count()} invalid field(s)", 200, _excerpt(content),
                           meta["request_id"], meta["attempts"], meta["elapsed_seconds"], details) from error
