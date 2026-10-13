
"""Riverside County Sheriff-Coroner newsflash (category 6 = Coroner).

Each list item's preview holds the full labeled release ("Last Name:", "Age:",
"Date of Injury:", "Location of Injury:", ...) and a "Posted on <date>" footer.
"""

import re
from collections import Counter
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from src.core.date_utils import is_recent, iso, parse_date
from src.core.http_client import error_status
from src.modules.source_collection.records import DATE_UNKNOWN, RECENT, make_record, make_source_result


LABELS = {
    "Coroner’s File #", "Coroner's File #", "Last Name", "First Name", "Gender", "Age",
    "City of Residence", "Next of Kin Notified", "Date of Death", "Time of Death",
    "Date of Injury", "Time of Injury", "Location of Death", "Location of Injury",
    "Agency Investigating", "Investigation #", "Comments", "Initial, Update or Final",
}
# Not stored: residence details are not needed for source collection.
SKIPPED_LABELS = {"City of Residence"}

POSTED_RE = re.compile(r"Posted on\s+([A-Z][a-z]+\.?\s+\d{1,2},\s+\d{4})")
ROAD_LOCATION_RE = re.compile(r"^\s*(roadway|freeway|highway|intersection|street)\b", re.IGNORECASE)
TRAFFIC_WORDS_RE = re.compile(r"traffic collision|vehicle|pedestrian|motorcycle|crash", re.IGNORECASE)
# House number at the start of an address segment or after "of"/"at" ("In Front of 1425 Pleasant View").
HOUSE_NUMBER_RE = re.compile(r"((?:^|,\s+|\bof\s+|\bat\s+))\d{1,6}\s+(?!block\b)(?=[A-Z])")


def parse_labeled_fields(tokens):
    """["Age:", "26 Years", "Gender:", "Male"] -> {"Age": "26 Years", "Gender": "Male"}."""
    fields = {}
    i = 0
    while i < len(tokens):
        token = tokens[i].strip()
        label = token[:-1].strip() if token.endswith(":") else None
        if label in LABELS:
            value = ""
            next_token = tokens[i + 1].strip() if i + 1 < len(tokens) else ""
            if next_token and next_token.rstrip(":") not in LABELS:
                value = next_token
                i += 1
            key = "Coroner's File #" if label.startswith("Coroner") else label
            fields.setdefault(key, value)
        i += 1
    return fields


LOCATION_PREFIX_RE = re.compile(r"^\s*(?:roadway|freeway|highway|street)\s*[:,]\s*", re.IGNORECASE)
ZIP_RE = re.compile(r"\s*\b9\d{4}(?:-\d{4})?\b")
CITY_RE = re.compile(r"(?:,|\bin)\s*([A-Z][A-Za-z .'\-]+?),\s*CA\b")


def strip_house_number(location):
    return HOUSE_NUMBER_RE.sub(r"\1", location) if location else location


def parse_location(fields):
    """(location_text, place) from 'Location of Injury', without house numbers or ZIP."""
    raw = fields.get("Location of Injury") or ""
    if not raw:
        return None, None
    cities = CITY_RE.findall(raw)
    text = ZIP_RE.sub("", strip_house_number(LOCATION_PREFIX_RE.sub("", raw))).strip(" ,")
    return text or None, cities[-1].strip() if cities else None


def parse_items(html, page_url):
    """Return one dict per release: url, file_number, title, fields, published_at."""

    soup = BeautifulSoup(html, "html.parser")
    items = []

    for item in soup.select('li.list-group-item[id^="list-articles-category-"]'):
        link = item.select_one("a.article-title-link")
        preview = item.select_one(".article-preview")
        if link is None or preview is None:
            continue

        tokens = [t for t in preview.get_text("\n", strip=True).split("\n") if t.strip()]
        fields = parse_labeled_fields(tokens)
        if not fields:
            continue

        posted = POSTED_RE.search(item.get_text(" ", strip=True))
        items.append({
            "url": urljoin(page_url, link.get("href", "")),
            "title": link.get_text(strip=True),
            "fields": fields,
            "published_at": parse_date(posted.group(1)) if posted else None,
        })

    return items


def traffic_hint(fields):
    """True if the release reads like a road death, else None (the release states no cause)."""
    if ROAD_LOCATION_RE.search(fields.get("Location of Injury", "")):
        return True
    if TRAFFIC_WORDS_RE.search(fields.get("Comments", "")):
        return True
    return None


def describe(fields):
    """Readable one-line description of the release, without residence details."""
    clean = dict(fields)
    for key in ("Location of Injury", "Location of Death"):
        if key in clean:
            clean[key] = strip_house_number(clean[key])
    return " | ".join(f"{k}: {v}" for k, v in clean.items() if k not in SKIPPED_LABELS)


def collect_riverside_coroner(source, client, now, hours):
    result = make_source_result(source["name"], source["url"], now, "coroner")
    records = []

    try:
        response = client.get(source["url"])
    except requests.RequestException as error:
        result["http_status"] = error_status(error)
        result["error"] = str(error)
        return records, result

    result["http_status"] = response.status_code
    items = parse_items(response.text, source["url"])
    result["items_seen"] = len(items)
    if not items:
        result["error"] = "page fetched but no releases parsed (layout change?)"

    skipped = Counter()
    for item in items:
        fields = item["fields"]
        incident = parse_date(fields.get("Date of Injury", ""))
        published = item["published_at"]

        if incident is not None:
            if not is_recent(incident, now=now, hours=hours, date_only=True):
                skipped["incident_outside_window"] += 1
                continue
            status = RECENT
        elif published is not None and is_recent(published, now=now, hours=hours, date_only=True):
            status = DATE_UNKNOWN  # e.g. "Date of Injury: Under Investigation"
        else:
            skipped["incident_outside_window"] += 1
            continue

        file_number = fields.get("Coroner's File #") or item["title"]
        location_text, place = parse_location(fields)
        records.append(make_record(
            source_name=source["name"],
            source_type="coroner",
            collection_method="html_listing",
            title=f"Riverside County Coroner file {item['title']}",
            url=item["url"],
            collected_at=now,
            listing_url=source["url"],
            publisher=source["name"],
            description=describe(fields),
            listing_published_at=iso(published),
            published_at=iso(published),
            incident_date=iso(incident),
            incident_date_text=fields.get("Date of Injury") or None,
            incident_date_basis="official_record" if incident else None,
            case_number=file_number,
            traffic_hint=traffic_hint(fields),
            # A county coroner release is a California record by definition.
            location_status="california_confirmed",
            location_evidence="Riverside County Coroner release",
            county="Riverside",
            place=place,
            location_text=location_text,
            status=status,
            review_reasons=[] if incident else ["date of injury not given in release"],
            # The site sometimes shows one file number in the title and another in the body.
            file_number_mismatch=bool(fields.get("Coroner's File #")) and fields.get("Coroner's File #") != item["title"],
        ))

    result["candidate_count"] = len(records)
    result["skipped"] = dict(skipped)
    return records, result
