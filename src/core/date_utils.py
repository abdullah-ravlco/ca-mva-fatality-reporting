
"""Date parsing and rolling lookback-window checks."""

import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

from config.settings import TIMEZONE

PACIFIC = ZoneInfo(TIMEZONE)

DATE_FORMATS = [
    "%B %d, %Y",
    "%b %d, %Y",
    "%b. %d, %Y",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%Y-%m-%d",
]

DATE_PATTERNS = [
    r"\b[A-Z][a-z]{2,8}\.? \d{1,2}, \d{4}\b",
    r"\b\d{1,2}/\d{1,2}/\d{4}\b",
    r"\b\d{4}-\d{2}-\d{2}\b",
]


def utc_now():
    return datetime.now(timezone.utc)


def reference_time():
    """The moment the lookback window is measured back from: now."""
    return utc_now()


def is_date_only(value):
    if not value:
        return False

    for fmt in DATE_FORMATS:
        try:
            datetime.strptime(value.strip(), fmt)
            return True
        except ValueError:
            continue

    return False


def parse_date(value):
    """Parse a date string into UTC, or return None."""
    if not value:
        return None

    value = str(value).strip()

    for fmt in DATE_FORMATS:
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed.replace(
                tzinfo=PACIFIC
            ).astimezone(timezone.utc)
        except ValueError:
            continue

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=PACIFIC)

        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        pass

    try:
        parsed = parsedate_to_datetime(value)

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError, IndexError):
        return None


def find_date_in_text(text):
    """Return (datetime, matched_text), or (None, None)."""
    for pattern in DATE_PATTERNS:
        match = re.search(pattern, text or "")

        if match:
            parsed = parse_date(match.group())

            if parsed:
                return parsed, match.group()

    return None, None


def is_recent(published_at, now=None, hours=96, date_only=False):
    """Check whether a timestamp falls within the lookback window."""
    if published_at is None:
        return False

    now = now or utc_now()

    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)

    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    now = now.astimezone(timezone.utc)
    published_at = published_at.astimezone(timezone.utc)
    cutoff = now - timedelta(hours=hours)

    if date_only:
        local_day = published_at.astimezone(PACIFIC).date()
        return (
            cutoff.astimezone(PACIFIC).date()
            <= local_day
            <= now.astimezone(PACIFIC).date()
        )

    return cutoff <= published_at <= now


def iso(value):
    return value.isoformat() if value else None
