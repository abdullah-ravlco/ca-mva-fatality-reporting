"""Turn a Google News RSS link into the publisher's article URL.

Google News article links (https://news.google.com/rss/articles/CBMi...) no longer
redirect with HTTP; the browser is sent on by JavaScript, so a plain GET never
reaches the article.

Two formats exist:
- Old IDs: base64 data that contains the article URL directly (decoded locally).
- New IDs ("AU_yqL..."): Google's own page holds a signature and timestamp, which
  are sent to Google's batchexecute endpoint to get the URL back.

The new-format method uses an undocumented Google endpoint (the same approach as the
open-source "googlenewsdecoder" package). It can stop working if Google changes it;
failures are reported, never guessed.
"""

import base64
import json
import re
import threading
import time
from urllib.parse import quote, urlsplit

import requests
from bs4 import BeautifulSoup


BATCH_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
MIN_SECONDS_BETWEEN_CALLS = 0.4  # starting spacing between calls; Google answers HTTP 429 when it is too fast
MAX_SECONDS_BETWEEN_CALLS = 6.0
COOLDOWN_AFTER_429_SECONDS = 20   # every thread pauses this long after a 429
MAX_ATTEMPTS = 5
CALMER_AFTER_OK_CALLS = 30        # this many calls in a row without a 429 speed the pace back up
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
URL_IN_BYTES_RE = re.compile(rb"https?://[\x21-\x7e]+")

_throttle_lock = threading.Lock()
_pace = {"next": 0.0, "interval": MIN_SECONDS_BETWEEN_CALLS, "ok_streak": 0, "slowed_at": -1e9}


class GoogleNewsDecodeError(Exception):
    pass


def is_google_news_url(url):
    parts = urlsplit(url or "")
    return parts.netloc == "news.google.com" and "/articles/" in parts.path


def article_id(url):
    path = urlsplit(url).path
    return path.split("/articles/", 1)[1].split("/")[0]


def _throttle():
    """Space calls to Google apart, across all threads. The spacing adapts: see _note_result."""
    with _throttle_lock:
        now = time.monotonic()
        slot = max(now, _pace["next"])
        _pace["next"] = slot + _pace["interval"]
    if slot > now:
        time.sleep(slot - now)


def _note_result(status_code):
    """Slow every thread down when Google says 429, and speed up again after a calm stretch."""
    with _throttle_lock:
        if status_code == 429:
            now = time.monotonic()
            # Calls already in flight all get a 429 together: slow down once per cooldown, not once each.
            if now - _pace["slowed_at"] > COOLDOWN_AFTER_429_SECONDS:
                _pace["interval"] = min(_pace["interval"] * 2, MAX_SECONDS_BETWEEN_CALLS)
                _pace["slowed_at"] = now
            _pace["ok_streak"] = 0
            _pace["next"] = max(_pace["next"], now + COOLDOWN_AFTER_429_SECONDS)
        else:
            _pace["ok_streak"] += 1
            if _pace["ok_streak"] >= CALMER_AFTER_OK_CALLS:
                _pace["interval"] = max(MIN_SECONDS_BETWEEN_CALLS, _pace["interval"] * 0.8)
                _pace["ok_streak"] = 0


def _send(send):
    """Call send() (a requests call) until Google stops answering 429, up to MAX_ATTEMPTS times."""
    for attempt in range(MAX_ATTEMPTS):
        _throttle()
        response = send()
        _note_result(response.status_code)
        if response.status_code != 429:
            break
    return response


def _get(page, timeout):
    """GET that backs off when Google answers 429."""
    return _send(lambda: requests.get(page, headers=HEADERS, timeout=timeout))


def _decode_old_format(gn_id):
    """Old-style IDs carry the URL inside the base64 data. Returns None for new IDs."""
    try:
        raw = base64.urlsafe_b64decode(gn_id + "=" * (-len(gn_id) % 4))
    except (ValueError, TypeError):
        return None
    if b"AU_yqL" in raw:
        return None  # new format: needs the batchexecute call
    match = URL_IN_BYTES_RE.search(raw)
    return match.group(0).decode("ascii", "ignore") if match else None


def _signature_and_timestamp(gn_id, timeout):
    """Read data-n-a-sg / data-n-a-ts from Google's article page."""
    for page in (f"https://news.google.com/articles/{gn_id}", f"https://news.google.com/rss/articles/{gn_id}"):
        try:
            response = _get(page, timeout)
        except requests.RequestException:
            continue
        if not response.ok:
            continue
        node = BeautifulSoup(response.text, "html.parser").select_one("[data-n-a-sg][data-n-a-ts]")
        if node:
            return node["data-n-a-sg"], node["data-n-a-ts"]
    raise GoogleNewsDecodeError("signature not found on Google News page (layout change or consent page)")


def _batchexecute(gn_id, signature, timestamp, timeout):
    inner = [
        "garturlreq",
        [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1],
         "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
        gn_id,
        int(timestamp),
        signature,
    ]
    request_body = [[["Fbv4je", json.dumps(inner, separators=(",", ":"))]]]
    payload = "f.req=" + quote(json.dumps(request_body, separators=(",", ":")))

    headers = dict(HEADERS, **{
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Referer": "https://news.google.com/",
    })
    try:
        response = _send(lambda: requests.post(BATCH_URL, data=payload, headers=headers, timeout=timeout))
    except requests.RequestException as exc:
        raise GoogleNewsDecodeError(f"batchexecute request failed: {exc}") from exc
    if not response.ok:
        raise GoogleNewsDecodeError(f"batchexecute HTTP {response.status_code}")

    # Response: ")]}'" line, blank line, then a JSON array of envelopes.
    try:
        envelopes = json.loads(response.text.split("\n\n", 1)[1])
        for envelope in envelopes:
            if envelope and envelope[0] == "wrb.fr" and envelope[1] == "Fbv4je" and envelope[2]:
                decoded = json.loads(envelope[2])[1]
                if isinstance(decoded, str) and decoded.startswith(("http://", "https://")):
                    return decoded
    except (IndexError, ValueError, TypeError) as exc:
        raise GoogleNewsDecodeError(f"unexpected batchexecute response: {exc}") from exc
    raise GoogleNewsDecodeError("batchexecute response did not contain a URL")


def decode_google_news_url(url, timeout=20):
    """Publisher article URL for a Google News link. Raises GoogleNewsDecodeError."""
    if not is_google_news_url(url):
        return url
    gn_id = article_id(url)
    old = _decode_old_format(gn_id)
    if old:
        return old
    signature, timestamp = _signature_and_timestamp(gn_id, timeout)
    return _batchexecute(gn_id, signature, timestamp, timeout)
