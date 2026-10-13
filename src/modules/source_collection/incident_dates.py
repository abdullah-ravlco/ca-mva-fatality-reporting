
"""Find when a crash happened, as opposed to when the story was published.

Rules:
- Only sentences that describe the crash are searched. Sentences about arrests,
  charges or court dates are skipped: "arrested Thursday" is not the crash date.
- Relative phrases ("Saturday night", "yesterday") are resolved against the
  publication timestamp converted to the publisher's local timezone, never "now".
- If a crash sentence points to an earlier time ("in February", "last year",
  "months ago"), or crash sentences give different dates, no date is returned and
  the reason is recorded. Nothing is guessed.
"""

import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from config.settings import PUBLISHER_TIMEZONE


LOCAL_TZ = ZoneInfo(PUBLISHER_TIMEZONE)
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
MONTH_NAMES = (
    "january|february|march|april|may|june|july|august|september|october|november|december"
)

DATE_PHRASE_RE = re.compile(
    r"\b(?:"
    # Oct. 9 / October 9, 2026 / Sept. 30
    r"(?P<month>jan|feb|mar|apr|may|jun|jul|aug|sept?|oct|nov|dec)[a-z]*\.?\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(?P<year>\d{4}))?"
    # 10/09/2026
    r"|(?P<num_month>\d{1,2})/(?P<num_day>\d{1,2})/(?P<num_year>\d{4})"
    # early Saturday / Friday night / Sunday
    r"|(?:early\s+|late\s+)?(?P<weekday>monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"(?:\s+(?:morning|afternoon|evening|night))?"
    # yesterday / last night / this morning
    r"|(?P<relative>yesterday|last\s+night|this\s+morning|this\s+afternoon|earlier\s+today|today|tonight)"
    r")\b",
    re.IGNORECASE,
)

# A sentence must describe the crash itself for its date to count.
CRASH_SENTENCE_RE = re.compile(
    r"\b(crash\w*|collision|collided|struck|hit|killed|died|dies|fatal\w*|wreck|"
    r"overturned|rolled|pronounced dead)\b",
    re.IGNORECASE,
)
# Dates in these sentences are about the investigation, not the crash.
FOLLOW_UP_SENTENCE_RE = re.compile(
    r"\b(arrest\w*|charged|charges|booked|arraign\w*|court|sentenc\w*|pleaded|plea|"
    r"convicted|trial|hearing|vigil|funeral|memorial|lawsuit)\b",
    re.IGNORECASE,
)
# Signs that the crash happened well before publication.
EARLIER_CRASH_RE = re.compile(
    rf"\b(?:in|since|back in|earlier this|last)\s+(?:{MONTH_NAMES})\b(?!\s+\d)"
    r"|\b(?:last|earlier this)\s+(?:year|month|spring|summer|fall|winter)\b"
    r"|\b\d+\s+(?:weeks?|months?|years?)\s+(?:ago|earlier|after)\b"
    r"|\b(?:nearly|almost|more than|about)\s+(?:a|one|two|three|\w+)\s+(?:months?|years?)\s+(?:ago|after|later)\b"
    r"|\bin\s+20\d\d\b",
    re.IGNORECASE,
)
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'])")
MAX_CANDIDATE_GAP_DAYS = 1

# incident_date_note_type values. Only "earlier" means the crash is known to be old;
# "conflicting" means the date is uncertain and the story still needs review.
NOTE_EARLIER = "earlier"
NOTE_CONFLICTING = "conflicting"


def _safe_date(year, month, day):
    try:
        return datetime(year, month, day).date()
    except ValueError:
        return None


def _resolve(match, published_day):
    """(date, basis) for one phrase match, or None."""
    groups = match.groupdict()

    if groups["month"]:
        month = MONTHS[groups["month"].lower()[:3]]
        day = int(groups["day"])
        if groups["year"]:
            return _safe_date(int(groups["year"]), month, day), "explicit"
        if published_day is None:
            return None
        candidate = _safe_date(published_day.year, month, day)
        if candidate and candidate > published_day:
            candidate = _safe_date(published_day.year - 1, month, day)
        return candidate, "relative_to_publication"

    if groups["num_month"]:
        return _safe_date(int(groups["num_year"]), int(groups["num_month"]), int(groups["num_day"])), "explicit"

    if published_day is None:
        return None

    if groups["weekday"]:
        delta = (published_day.weekday() - WEEKDAYS.index(groups["weekday"].lower())) % 7
        return published_day - timedelta(days=delta), "relative_to_publication"

    relative = groups["relative"].lower()
    if relative in ("yesterday", "last night"):
        return published_day - timedelta(days=1), "relative_to_publication"
    return published_day, "relative_to_publication"


def _empty(published_local=None, note=None, note_type=None):
    return {
        "incident_date": None,
        "incident_date_text": None,
        "incident_date_basis": None,
        "incident_date_evidence": None,
        "incident_date_note": note,
        "incident_date_note_type": note_type,
        "published_local_date": published_local.isoformat() if published_local else None,
        "publisher_timezone": PUBLISHER_TIMEZONE,
    }


def find_incident_date(text, published_at):
    """Crash date found in the story text, with the phrase and sentence used as evidence.

    incident_date is a UTC datetime for local midnight of the crash day, or None.
    incident_date_note explains why no date was accepted, when relevant.
    """
    published_local = published_at.astimezone(LOCAL_TZ).date() if published_at else None
    if not text:
        return _empty(published_local)

    candidates = []
    for sentence in SENTENCE_SPLIT_RE.split(text):
        if not CRASH_SENTENCE_RE.search(sentence):
            continue

        earlier = EARLIER_CRASH_RE.search(sentence)
        if earlier:
            return _empty(
                published_local,
                f"story places the crash earlier: '{earlier.group(0)}' in: {sentence.strip()[:300]}",
                NOTE_EARLIER,
            )

        if FOLLOW_UP_SENTENCE_RE.search(sentence):
            continue

        for match in DATE_PHRASE_RE.finditer(sentence):
            resolved = _resolve(match, published_local)
            if not resolved or resolved[0] is None:
                continue
            day, basis = resolved
            # A crash can't happen after the story about it was published.
            if published_local is not None and day > published_local:
                continue
            candidates.append((day, basis, match.group(0), sentence.strip()[:400]))

    if not candidates:
        return _empty(published_local)

    days = sorted({c[0] for c in candidates})
    if (days[-1] - days[0]).days > MAX_CANDIDATE_GAP_DAYS:
        found = ", ".join(f"'{c[2]}' -> {c[0].isoformat()}" for c in candidates[:4])
        return _empty(published_local, f"conflicting crash dates in story text: {found}", NOTE_CONFLICTING)

    day, basis, phrase, sentence = candidates[0]
    result = _empty(published_local)
    result.update({
        "incident_date": datetime.combine(day, time(0, 0), tzinfo=LOCAL_TZ).astimezone(timezone.utc),
        "incident_date_text": phrase,
        "incident_date_basis": basis,
        "incident_date_evidence": sentence,
    })
    return result
