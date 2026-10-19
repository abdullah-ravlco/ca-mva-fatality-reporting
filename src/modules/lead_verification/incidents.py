"""Load the leads that passed severity screening (passed_leads.json) and their already-fetched articles."""

import hashlib
import re
import unicodedata
from urllib.parse import urlsplit

from src.core.outputs import read_json
from src.modules.lead_verification.config import LAW_FIRM_HOST_RE, LEADS_FILE, OFFICIAL_HOST_RE


NAME_TITLE_RE = re.compile(r"^(officer|deputy|sgt\.?|sergeant|dr\.?|mr\.?|mrs\.?|ms\.?)\s+", re.I)


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

def load_incidents():
    """The leads in passed_leads.json, in the order severity screening wrote them."""
    data = read_json(LEADS_FILE)
    if not isinstance(data, dict) or not isinstance(data.get("leads"), list):
        raise FileNotFoundError(f"Leads file missing or invalid: {LEADS_FILE} (run severity_screening first)")

    incidents = []
    for lead in data["leads"]:
        articles = lead.get("articles") or []
        urls = {u for a in articles for u in (a.get("url"), a.get("rss_url"), a.get("final_url")) if u}
        incidents.append({
            "incident_key": lead["incident_id"],
            "names": [],
            "headlines": [a.get("title") for a in articles if a.get("title")],
            "source_urls_set": urls,
            "articles": articles,
            "grouping": lead.get("grouping") or {},
            "screening": lead.get("screening") or {},
        })
    return incidents


def existing_sources(incident):
    """Article texts that link_extraction already fetched for this incident (no new requests)."""
    found = []
    for article in incident["articles"]:
        if not article.get("text_available") or not article.get("text"):
            continue
        metadata = article.get("metadata") or {}
        found.append({
            "url": article.get("final_url") or article.get("url"),
            "title": article.get("title"),
            "publisher": article.get("publisher"),
            "published_at": metadata.get("date_published") or article.get("published_at"),
            "origin": "already_fetched",
            "text": article["text"],
        })
    return found


def input_fingerprint(incident):
    """Changes when the incident's articles change, so its analysis is redone."""
    urls = sorted(a.get("canonical_url") or a.get("url") or "" for a in incident["articles"])
    return hashlib.sha1("|".join(urls).encode()).hexdigest()[:16]
