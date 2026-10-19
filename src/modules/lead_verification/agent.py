"""The lead-verification agent: one run per incident.

The model gets the full text of the incident's articles and decides what to do next:
  - web_search (OpenAI's built-in tool) to find more reports, a coroner or CHP release,
    a victim identification, an arrest, an obituary;
  - fetch_page(url) to read a page in full (our fetcher, with the blocked-site list);
  - or stop and return the structured result (schema.LeadResult).
Tool calls are answered and the model is called again until it returns the structured result
or the step limit is reached; the last step has tools switched off so a result always comes back.

Every quote in the result is then checked against the text of the page it cites
(quote_verified). Pages that were only seen in search results are not verified.
"""

import json
import logging

from src.modules.lead_verification.config import (
    BLOCKED_HOST_RE,
    MAX_AGENT_STEPS,
    MAX_FETCHED_PAGE_CHARS,
    MAX_SOURCE_CHARS,
    MAX_SOURCES_PER_PROMPT,
    MAX_WEB_SEARCHES_PER_INCIDENT,
)
from src.modules.lead_verification.incidents import host_of
from src.modules.lead_verification.schema import LeadResult
from src.modules.lead_verification.quotes import quote_in_text

logger = logging.getLogger(__name__)

FETCH_TOOL = {
    "type": "function",
    "name": "fetch_page",
    "description": (
        "Read the full text of a web page (news article, coroner, CHP or police release). "
        "Use it on promising URLs from web_search before relying on them."
    ),
    "parameters": {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "Absolute http(s) URL"}},
        "required": ["url"],
        "additionalProperties": False,
    },
    "strict": True,
}

INSTRUCTIONS = """You verify one reported California road-crash death for a wrongful-death lead review.
You are given the full text of the articles about the incident. Work like an investigator:
1. Read the articles. Decide which required facts are missing or uncertain.
2. If anything important is missing (the deceased person's name, age, residence, the crash
   date/time/location, who was at fault, whether anyone was impaired, whether another party
   was involved), you MUST search for it before answering: call web_search with specific queries
   (crash location + victim description, "identified", "coroner", "CHP", the victim's name once
   known), then fetch_page on the best results. Try different queries when the first ones find
   nothing. Search as much as is useful; stop when you have what can be found or further searching
   is not turning up anything new.
3. Return the structured result.

Rules:
- Use only what the articles and pages you read say. Never guess or fill gaps. Leave fields
  empty/unknown when no source states them.
- Every value in a fact needs source_url (a URL you were given or fetched) and a short quote
  copied exactly from that page.
- Never use people-search, background-check, property-record or social-media sites, and never
  look for anyone's home address. Residence/address: only what a news or official source states
  (usually a city, e.g. "of Lamont"); a street address only if the source itself publishes it.
- One entry in people per real person involved (deceased, injured, uninjured drivers,
  passengers, pedestrians). Unnamed people are listed too, with name empty. Never invent names.
- incident.date is YYYY-MM-DD (the crash, not the arrest or funeral date). incident.location is
  where the crash happened.
- other_party_involved = "yes" if someone other than the victims and the driver of a victim's own
  vehicle was involved: another vehicle, or a driver who struck a pedestrian or cyclist. "no" when
  the sources establish nobody else was involved (a solo crash; a passenger killed in a crash their
  own driver caused). "unknown" when the sources do not say.
- at_fault, per person: "yes" if the sources report the person caused the crash or was cited,
  arrested or charged for it; "no" if the sources say another party was at fault or was cited,
  arrested or charged, or describe the person doing nothing wrong (a pedestrian in a crosswalk, a
  passenger, someone inside a building that a vehicle crashed into); "unknown" while it is under
  investigation or not reported. Give fault_basis and a quote. This is what the sources report,
  not a legal finding.
- other_party_at_fault (incident): did the other party cause the crash? "yes" only with a quote that
  says so or describes it (ran a red light, speeding and lost control, DUI, cited, arrested,
  charged). "unknown" when the cause is under investigation, not reported, or the driver had a
  medical emergency with no other cause stated. "no" only when the sources say the other party was
  not at fault.
- fault_basis must match its quote: "other_party_cited_or_arrested" / "deceased_cited_or_arrested"
  only when the quote itself says someone was cited, arrested, charged or booked;
  "stated_by_authorities" only when the quote attributes the cause to police/CHP/coroner;
  "circumstances_described" when the quote describes what the driver did; otherwise "none".
- impaired, per person: "yes" only when a source says alcohol, drugs or a DUI were involved for
  that person, "no" only when a source says they were not, otherwise "unknown".
- A road crash is any death caused by a motor vehicle in motion: vehicles colliding, a vehicle
  hitting a pedestrian, cyclist or scooter rider, or a vehicle leaving the road and striking a
  building, a restaurant or a person inside a home. That is true even when the crash ended off the
  road or the driver had a medical emergency. "not_a_road_crash" is ONLY for deaths no moving
  vehicle caused (illness, homicide, suicide, a fall, a fire with no crash).
- address (residence): only where the person lived, with a quote that says so ("of Lamont",
  "a Lamont resident", "lived in Lamont"). The crash location is NOT a residence.
- assessment.verdict: "does_not_qualify" when nobody died, nobody other than the deceased could
  be responsible (single-vehicle crash, no other party), the deceased driver was impaired or at
  fault, it was not a road crash (see above), or it is outside California; "qualifies" when
  someone died, another party is involved and at fault, and the deceased was not impaired or at
  fault; "fault_unidentified" when all of that holds except that the other party's fault is not
  established; "needs_review" when other facts needed to decide are missing. Put the codes in
  assessment.reasons and explain in one or two sentences.
- missing_information: what you could not find. pages_used: URLs you relied on."""


def _sources_block(sources):
    blocks = []
    for index, source in enumerate(sources, start=1):
        blocks.append(
            f"[Article {index}] {source.get('title') or ''}\n"
            f"URL: {source['url']}\n"
            f"Publisher: {source.get('publisher') or 'unknown'}\n"
            f"Published: {source.get('published_at') or 'unknown'}\n"
            f"TEXT:\n{source['text'][:MAX_SOURCE_CHARS]}"
        )
    return "\n\n-----\n\n".join(blocks)


SCREENING_FOCUS = {
    "victim_identity_unidentified": "The victim's name has not been reported yet: search for it first.",
    "fault_unidentified": "Who was at fault has not been established yet: search for it first.",
    "qualified": "Screening found everything needed: confirm each fact, and look for anything that contradicts it.",
}


def _screening_block(screening):
    """What severity screening found (unverified) and what to focus on."""
    if not screening:
        return ""
    facts = screening.get("facts") or {}
    victims = [
        {key: v.get(key) for key in ("name", "age", "role", "outcome", "injury_tier", "at_fault_party", "impaired")}
        for v in facts.get("victims") or []
    ]
    known = {
        "crash_date": facts.get("crash_date"),
        "crash_location": facts.get("crash_location"),
        "other_party": (facts.get("other_party") or {}).get("description"),
        "victims": victims,
    }
    return (
        f"Severity screening result: {screening.get('status')}. "
        f"{SCREENING_FOCUS.get(screening.get('status'), '')}\n"
        f"Facts screening extracted from these articles (treat as unverified, check them): {json.dumps(known)}\n"
    )


def build_input(incident, sources):
    grouping = incident.get("grouping") or {}
    return (
        "Incident grouped from these articles (they should all be about the same crash; "
        "tell me if they are not).\n"
        f"{_screening_block(incident.get('screening'))}"
        f"Candidate dates: {json.dumps(grouping.get('candidate_incident_dates'))}\n"
        f"Candidate locations: {json.dumps(grouping.get('locations'))}\n\n"
        f"{_sources_block(sources[:MAX_SOURCES_PER_PROMPT])}"
    )


def missing_key_facts(result):
    """What the model has not found yet (used to push it to search before it answers)."""
    missing = []
    if not result.incident.date.value:
        missing.append("crash date")
    if not result.incident.location.value:
        missing.append("crash location")
    for person in result.people:
        if person.outcome != "killed":
            continue
        for field in ("name", "age", "address"):
            if not getattr(person, field).value:
                missing.append(f"deceased person's {field}")
        if person.at_fault == "unknown":
            missing.append("who was at fault")
    return sorted(set(missing))


def _run_tool(call, fetcher, store, log):
    """Output string for one function call from the model."""
    try:
        arguments = json.loads(call.arguments or "{}")
    except ValueError:
        arguments = {}
    if call.name != "fetch_page":
        return json.dumps({"error": f"unknown tool {call.name}"})

    url = (arguments.get("url") or "").strip()
    entry = {"tool": "fetch_page", "url": url}
    log.append(entry)
    if not url.startswith(("http://", "https://")):
        entry["result"] = "invalid_url"
        return json.dumps({"error": "url must start with http:// or https://"})
    if BLOCKED_HOST_RE.search(host_of(url)):
        entry["result"] = "blocked_domain"
        return json.dumps({"error": "this site type is not allowed as a source"})

    result = fetcher.fetch(url)
    final = result.get("url") or url
    if not result.get("ok"):
        entry.update(result="fetch_failed", error=result.get("error"))
        return json.dumps({"error": result.get("error") or "could not fetch the page"})

    text = result["text"][:MAX_FETCHED_PAGE_CHARS]
    store[final] = text
    store.setdefault(url, text)
    entry["result"] = "ok"
    return json.dumps({"url": final, "text": text})


def run_agent(client, model, incident, sources, fetcher):
    """(LeadResult, page_texts, log). Raises ValueError if the model never returns a result."""
    store = {source["url"]: source["text"] for source in sources}
    log = []
    tools = [{"type": "web_search"}, FETCH_TOOL]
    next_input = build_input(incident, sources)
    previous_id = None
    nudged = False

    for step in range(1, MAX_AGENT_STEPS + 1):
        last = step == MAX_AGENT_STEPS
        response = client.responses.parse(
            model=model,
            instructions=INSTRUCTIONS,
            input=next_input,
            tools=tools,
            tool_choice="none" if last else "auto",
            max_tool_calls=MAX_WEB_SEARCHES_PER_INCIDENT,
            previous_response_id=previous_id,
            text_format=LeadResult,
        )
        for item in response.output:
            if getattr(item, "type", "") == "web_search_call":
                action = getattr(item, "action", None)
                log.append({"tool": "web_search", "query": getattr(action, "query", None), "step": step})

        calls = [item for item in response.output if getattr(item, "type", "") == "function_call"]
        if not calls:
            result = response.output_parsed
            if result is None:
                raise ValueError(f"{model} returned no structured result for {incident['incident_key']}")
            missing = missing_key_facts(result)
            searched = any(entry["tool"] == "web_search" for entry in log)
            if missing and not searched and not nudged and step < MAX_AGENT_STEPS - 1:
                # Models often answer straight away; one push to search for what is missing.
                nudged = True
                log.append({"tool": "nudge", "missing": missing, "step": step})
                next_input = (
                    "You have not searched yet. Still missing: " + ", ".join(missing) + ". "
                    "Use web_search (and fetch_page on the best results) to look for it, then answer again."
                )
                previous_id = response.id
                continue
            return result, store, log

        next_input = [
            {"type": "function_call_output", "call_id": call.call_id, "output": _run_tool(call, fetcher, store, log)}
            for call in calls
        ]
        previous_id = response.id

    raise ValueError(f"agent did not finish within {MAX_AGENT_STEPS} steps for {incident['incident_key']}")


def check_quotes(node, store):
    """Add quote_verified to every {source_url, quote} pair (and the *_source_url/*_quote pairs)."""
    if isinstance(node, list):
        for item in node:
            check_quotes(item, store)
    elif isinstance(node, dict):
        pairs = [("source_url", "quote", "quote_verified")]
        for key in list(node):
            if key.endswith("_quote"):
                prefix = key[: -len("_quote")]
                pairs.append((f"{prefix}_source_url", key, f"{prefix}_quote_verified"))
        for url_key, quote_key, flag in pairs:
            if node.get(quote_key):
                text = store.get(node.get(url_key) or "")
                node[flag] = bool(text) and quote_in_text(node[quote_key], text)
        for value in node.values():
            check_quotes(value, store)
    return node
