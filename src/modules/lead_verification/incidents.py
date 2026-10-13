"""Load the shortlist and group shortlisted people into incidents (one crash each)."""

import hashlib
import json
import re
import unicodedata
from urllib.parse import urlsplit

from src.modules.lead_verification.config import EXTRACTED_TEXT_FILE, LAW_FIRM_HOST_RE, OFFICIAL_HOST_RE, SHORTLIST_FILE


NAME_TITLE_RE = re.compile(r"^(officer|deputy|sgt\.?|sergeant|dr\.?|mr\.?|mrs\.?|ms\.?)\s+", re.I)


def read_json(path, default=None):
    """File contents, or default when the file is missing, empty or not valid JSON."""
    if not path.exists():
        return default
    try:
        text = path.read_text(encoding="utf-8")
        return json.loads(text) if text.strip() else default
    except (OSError, ValueError):
        return default


# --------------------------------------------------
# Names
# --------------------------------------------------

def name_tokens(text):
    """Lower-case ASCII word tokens: 'José Ivan' -> ['jose', 'ivan']."""
    text = NAME_TITLE_RE.sub("", text or "")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return [token for token in re.split(r"[^a-z]+", text.lower()) if token]


def mentions_person(words, name):
    """True when the text's word set contains the person's first and last name."""
    tokens = name_tokens(name)
    if not tokens:
        return False
    if len(tokens) == 1:
        return tokens[0] in words
    return tokens[0] in words and tokens[-1] in words


def mentions_any(text, names):
    words = set(name_tokens(text))
    return any(mentions_person(words, name) for name in names)


# --------------------------------------------------
# Hosts
# --------------------------------------------------

def host_of(url):
    host = urlsplit(url or "").netloc.lower()
    return host[4:] if host.startswith("www.") else host


def is_official(url):
    return bool(OFFICIAL_HOST_RE.search(host_of(url)))


def is_law_firm(url):
    return bool(LAW_FIRM_HOST_RE.search(host_of(url)))


# --------------------------------------------------
# Incidents
# --------------------------------------------------

def load_shortlist():
    data = read_json(SHORTLIST_FILE)
    if data is None:
        raise FileNotFoundError(f"Shortlist not found: {SHORTLIST_FILE}")
    people = data.get("people") if isinstance(data, dict) else data
    return [person for person in people or [] if isinstance(person, dict) and person.get("person_name")]


def _person_keys(person):
    case_ids = set(person.get("case_ids") or [])
    if person.get("case_id"):
        case_ids.add(person["case_id"])
    urls = {source.get("source_url") for source in person.get("sources") or [] if source.get("source_url")}
    if person.get("source_url"):
        urls.add(person["source_url"])
    return case_ids, urls


def group_incidents(people):
    """People who share a case_id or a source URL were in the same crash."""
    incidents = []

    for person in people:
        case_ids, urls = _person_keys(person)
        match = next(
            (incident for incident in incidents
             if incident["case_ids"] & case_ids or incident["source_urls_set"] & urls),
            None,
        )
        if match is None:
            match = {"people": [], "case_ids": set(), "source_urls_set": set(), "source_urls": []}
            incidents.append(match)

        match["people"].append(person)
        match["case_ids"] |= case_ids
        for url in [person.get("source_url")] + [s.get("source_url") for s in person.get("sources") or []]:
            if url and url not in match["source_urls_set"]:
                match["source_urls_set"].add(url)
                match["source_urls"].append(url)

    for incident in incidents:
        names = sorted(name_tokens(p["person_name"])[-1] if name_tokens(p["person_name"]) else ""
                       for p in incident["people"])
        seed = "|".join(sorted(incident["case_ids"]) or incident["source_urls"]) + "|" + ",".join(names)
        incident["incident_key"] = "inc-" + hashlib.sha1(seed.encode()).hexdigest()[:10]
        incident["names"] = [p["person_name"] for p in incident["people"]]

    return incidents


def published_dates(incident):
    """source_url -> published_date from the shortlist entries."""
    dates = {}
    for person in incident["people"]:
        for source in person.get("sources") or []:
            if source.get("source_url") and source.get("published_date"):
                dates[source["source_url"]] = source["published_date"]
    return dates


def load_extracted_texts():
    records = read_json(EXTRACTED_TEXT_FILE, default=[]) or []
    return [record for record in records if isinstance(record, dict) and record.get("text")]


def existing_sources(incident, extracted_records):
    """Article texts that step 2 already fetched for this incident (no new requests)."""
    dates = published_dates(incident)
    found = []

    for record in extracted_records:
        urls = {record.get("link"), record.get("final_url")}
        same_url = bool(urls & incident["source_urls_set"])
        same_case = record.get("case_id") in incident["case_ids"]
        if not (same_url or (same_case and mentions_any(record["text"], incident["names"]))):
            continue
        url = record.get("link") or record.get("final_url")
        found.append({
            "url": url,
            "title": record.get("title"),
            "publisher": record.get("publisher"),
            "published_at": dates.get(url) or dates.get(record.get("final_url")),
            "origin": "already_fetched",
            "text": record["text"],
        })
    return found
