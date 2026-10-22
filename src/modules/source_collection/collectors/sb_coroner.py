"""San Bernardino County Coroner press releases.

The page has no per-case links: each case is one <li> of plain text, e.g.
"702606682: On Saturday, 10/03/2026, at 09:39 p.m., the Montclair Police Department
responded to ... for a traffic collision. Pedestrian ..., a 67-year-old ...
(Supervising Deputy Coroner S. Hill, 10/05/2026)"
"""

import re
from collections import Counter
from datetime import timedelta, timezone

import requests
from bs4 import BeautifulSoup

from src.core.date_utils import is_recent, iso, parse_date, utc_now
from src.core.http_client import error_status
from src.modules.source_collection.records import DATE_UNKNOWN, RECENT, add_example, make_record, make_source_result


CASE_RE = re.compile(r"^\s*(\d{6,})\s*:")
INCIDENT_DATE_RE = re.compile(
    r"\bOn\s+(?:[A-Za-z]+day,?\s+)?"
    r"(\d{1,2}/\d{1,2}/\d{2,4}|[A-Z][a-z]+\.?\s+\d{1,2},\s+\d{4})"
)
SIGNOFF_DATE_RE = re.compile(r"\(([^()]*?),\s*(\d{1,2}/\d{1,2}/\d{2,4})\s*\)\s*$")
TRAFFIC_RE = re.compile(
    r"traffic collision|vehicle|pedestrian|motorcyclist|bicyclist|cyclist|\bdriver\b",
    re.IGNORECASE,
)
NOT_ROAD_RE = re.compile(r"airplane|aircraft|train|railroad", re.IGNORECASE)
# "...responded to <location> [for a traffic collision]. <Next sentence>". The sentence
# end requires a lowercase letter/digit before the period so "N. D Street" isn't cut.
LOCATION_RE = re.compile(
    r"responded to\s+(?:the\s+)?(?:area of\s+)?(?P<location>.+?)"
    r"(?:\s+for an?\s+[a-z ]*?collision)?(?<=[a-z0-9])\.\s+(?=[A-Z])"
)
CITY_RE = re.compile(r"\s+in\s+(?P<city>[A-Z][A-Za-z .'\-]+?)\s*$")

# The SB Coroner page occasionally carries a future date (typo, or a release
# posted before the crash date is finalized). Accept dates up to this many days
# into the future so a single bad date doesn't hide a real case.
FUTURE_TOLERANCE_DAYS = 2


def is_recent_or_near_future(value, since, now=None, tolerance_days=FUTURE_TOLERANCE_DAYS):
    """Like date_utils.is_recent(), but tolerates dates a couple of days into the future.

    Local to this collector: the SB Coroner page is the only source that
    needs this tolerance.
    """
    if value is None:
        return False

    now = now or utc_now()

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    value = value.astimezone(timezone.utc)
    now = now.astimezone(timezone.utc)

    if value > now + timedelta(days=tolerance_days):
        return False

    return value >= since.astimezone(timezone.utc)


def parse_location(text):
    """(location_text, place) from the 'responded to ...' sentence."""
    match = LOCATION_RE.search(text)
    if not match:
        return None, None
    location = match.group("location").strip()
    city = CITY_RE.search(location)
    return location, city.group("city").strip() if city else None


def parse_entries(html):
    """Return one dict per case: case_number, text, incident_date, published_at."""

    soup = BeautifulSoup(html, "html.parser")

    # The coroner releases are one <ul class="wp-block-list"> inside the page's
    # <article id="post-156">. Scope to that exact list so nav/sidebar/related
    # <ul> can never leak in, and so a second <article> elsewhere on the page
    # can't be picked up.
    article = soup.find("article", id="post-156") or soup.find("article")

    list_container = None
    if article is not None:
        list_container = article.find("ul", class_="wp-block-list")

    if list_container is None:
        # Fallback: any <ul> whose first <li> starts with a case number.
        for candidate in soup.find_all("ul"):
            first_li = candidate.find("li")
            if first_li and CASE_RE.match(first_li.get_text(" ", strip=True)):
                list_container = candidate
                break

    if list_container is None:
        return []

    entries = []

    for item in list_container.find_all("li", recursive=False):
        if item.find("strong") is None:
            continue

        text = " ".join(item.get_text(" ", strip=True).split()).replace(" :", ":")
        case_match = CASE_RE.match(text)
        if not case_match:
            continue

        incident_match = INCIDENT_DATE_RE.search(text)
        signoff_match = SIGNOFF_DATE_RE.search(text)

        entries.append({
            "case_number": case_match.group(1),
            "text": text,
            # A typo such as "09/10/206" parses to None; it is never guessed.
            "incident_date": parse_date(incident_match.group(1)) if incident_match else None,
            "incident_date_text": incident_match.group(1) if incident_match else None,
            "published_at": parse_date(signoff_match.group(2)) if signoff_match else None,
        })

    return entries


def traffic_hint(text):
    """True if the release reads like a road collision, False if clearly not, else None."""
    if NOT_ROAD_RE.search(text):
        return False
    if TRAFFIC_RE.search(text):
        return True
    return None


def collect_sb_coroner(source, client, now, since):
    result = make_source_result(source["name"], source["url"], now, "coroner")
    records = []

    try:
        response = client.get(source["url"])
    except requests.RequestException as error:
        result["http_status"] = error_status(error)
        result["error"] = str(error)
        return records, result

    result["http_status"] = response.status_code
    entries = parse_entries(response.text)
    result["items_seen"] = len(entries)
    if not entries:
        result["error"] = "page fetched but no case entries parsed (layout change?)"

    skipped = Counter()
    for entry in entries:
        incident = entry["incident_date"]
        published = entry["published_at"]
        label = f"case {entry['case_number']}"

        if incident is not None:
            if not is_recent_or_near_future(incident, since, now=now):
                skipped["incident_outside_window"] += 1
                continue
            status = RECENT
        elif published is not None and is_recent(published, since, now=now, date_only=True):
            # Recent release whose incident date couldn't be read: keep for review.
            status = DATE_UNKNOWN
        else:
            skipped["incident_outside_window"] += 1
            continue

        hint = traffic_hint(entry["text"])
        if hint is False:
            # Aircraft/rail deaths etc.: decided from the release text, not needed.
            skipped["not_road_death"] += 1
            add_example(result, "not_road_death", f"{label}: {entry['text'][:120]}", f"{source['url']}#case-{entry['case_number']}")
            continue

        location_text, place = parse_location(entry["text"])
        records.append(make_record(
            source_name=source["name"],
            source_type="coroner",
            collection_method="html_listing",
            title=f"SB County Coroner case {entry['case_number']}",
            url=f"{source['url']}#case-{entry['case_number']}",
            collected_at=now,
            listing_url=source["url"],
            publisher=source["name"],
            description=entry["text"],
            listing_published_at=iso(published),
            published_at=iso(published),
            incident_date=iso(incident),
            incident_date_text=entry["incident_date_text"],
            incident_date_basis="official_record" if incident else None,
            case_number=entry["case_number"],
            traffic_hint=hint,
            # A county coroner release is a California record by definition.
            location_status="california_confirmed",
            location_evidence="San Bernardino County Coroner release",
            county="San Bernardino",
            place=place,
            location_text=location_text,
            status=status,
            review_reasons=[] if incident else ["incident date not readable in release"],
        ))

    result["candidate_count"] = len(records)
    result["skipped"] = dict(skipped)
    return records, result