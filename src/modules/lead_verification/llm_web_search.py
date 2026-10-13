"""Let the LLM search the web for more sources about one incident.

The model only proposes URLs (OpenAI web_search tool). Its own answer text is
never used as a fact: every URL is fetched by our code and must name the
person, and facts are then extracted and quote-checked like any other source.
"""

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from openai import BadRequestError

from src.modules.lead_verification.config import BLOCKED_HOST_RE, MAX_WEB_SEARCH_URLS
from src.modules.lead_verification.incidents import host_of


WEB_SEARCH_TOOLS = ("web_search", "web_search_preview")
URL_RE = re.compile(r"https?://[^\s)\]>\"']+")


def _clean_url(url):
    """Drop tracking parameters (OpenAI adds utm_source=openai) and trailing punctuation."""
    url = url.rstrip(".,;")
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith("utm_")]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _prompt(incident, missing):
    """incident["people"]: [{display_name, age, outcome}]; incident["known"]: what the sources say so far."""
    people = []
    for person in incident["people"]:
        bits = [person["display_name"]]
        if person.get("age"):
            bits.append(f"age {person['age']}")
        bits.append(person.get("outcome") or "outcome unknown")
        people.append(", ".join(str(b) for b in bits))
    known = incident.get("known") or {}
    return f"""Find published news articles and official releases (coroner, CHP,
police, sheriff, city) about this California traffic crash.

People: {"; ".join(people) or "not yet identified"}
Crash date (may be unknown): {known.get("incident_date") or "unknown"}
Crash location (may be unknown): {known.get("location") or "unknown"}
Known headline: {known.get("headline")}

We still need: {", ".join(missing)}.

Rules:
- Only pages about THIS crash and these people.
- Prefer coroner/CHP/police releases and local news follow-ups
  (victim identified, arrest, obituary).
- Do not use people-search, background-check, property-record or social
  media sites, and do not look for anyone's home address.
- Answer with the page URLs only, one per line."""


def web_search_urls(client, model, incident, missing, known_urls):
    """(urls, error). URLs are new, not blocked, at most MAX_WEB_SEARCH_URLS."""
    response, last_error = None, None
    for tool in WEB_SEARCH_TOOLS:
        try:
            response = client.responses.create(
                model=model,
                tools=[{"type": tool}],
                input=_prompt(incident, missing),
            )
            break
        except BadRequestError as error:  # older accounts/models only know the preview tool
            last_error = error
    if response is None:
        return [], f"LLM web search failed: {last_error}"

    found = []
    for item in getattr(response, "output", None) or []:
        for content in getattr(item, "content", None) or []:
            for annotation in getattr(content, "annotations", None) or []:
                if getattr(annotation, "type", "") == "url_citation" and getattr(annotation, "url", None):
                    found.append(annotation.url)
    found += URL_RE.findall(response.output_text or "")

    urls = []
    for url in found:
        url = _clean_url(url)
        if url in known_urls or url in urls or BLOCKED_HOST_RE.search(host_of(url)):
            continue
        urls.append(url)
    return urls[:MAX_WEB_SEARCH_URLS], None
