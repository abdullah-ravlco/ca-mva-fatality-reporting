"""Step 4: analyse every grouped incident against its sources (one LLM call per incident).

For each incident in data/incident_grouping/incidents.json:
  1. use the article texts link_extraction already fetched (no new requests);
  2. LLM reads all of them and lists every fact and every person involved, each
     fact with a source + exact quote;
  3. verifier.py keeps only quotes that really appear in the sources and sets
     each field to confirmed / single_source / conflict / missing;
  4. once people are named, Google News RSS is searched for follow-up reports
     naming them; if new sources are found, steps 2-3 run again;
  5. if key fields are still missing, the LLM web search proposes more URLs,
     which are fetched, checked, and steps 2-3 run again.

Output: data/lead_verification/incident_analysis.json - every incident, with
scope checks (in window / in California / fatality established), an evidence
status, every person (named or unnamed) and what the sources say about fault.
Nothing is filtered out by fault or injury severity.
"""

import logging
from collections import Counter
from datetime import datetime, timezone

from src.core.logger import configure_logging
from src.core.outputs import read_json, write_stage_json
from src.modules.lead_verification.config import (
    KEY_INCIDENT_FIELDS,
    KEY_PERSON_FIELDS,
    LLM_WEB_SEARCH,
    MAX_WEB_SEARCHES_PER_RUN,
    MODULE,
    OUTPUT_FILE,
    OUTPUT_NAME,
)
from src.modules.lead_verification.evidence_llm import build_llm, extract_evidence, llm_model, rank_sources
from src.modules.lead_verification.fetcher import Fetcher
from src.modules.lead_verification.followup_search import search_followups
from src.modules.lead_verification.incidents import (
    existing_sources,
    host_of,
    input_fingerprint,
    is_law_firm,
    is_official,
    load_incidents,
    mentions_any,
)
from src.modules.lead_verification.llm_web_search import web_search_urls
from src.modules.lead_verification.verifier import verify_incident


logger = logging.getLogger(__name__)

ANALYSED = "analysed"
NO_TEXT = "no_text"          # no article text could be read; nothing for the LLM to check
FAILED = "failed"            # LLM or processing error; retried next run


def now():
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------
# Sources
# --------------------------------------------------

def add_sources(store, new_sources):
    """Add sources not already in the store (by URL). Returns how many were added."""
    known = {source["url"] for source in store}
    added = 0
    for source in new_sources:
        if not source.get("url") or source["url"] in known or not source.get("text"):
            continue
        source["host"] = host_of(source["url"])
        source["official"] = is_official(source["url"])
        source["law_firm"] = is_law_firm(source["url"])
        store.append(source)
        known.add(source["url"])
        added += 1
    return added


def numbered(sources):
    """Ranked sources with ids S1, S2, ... for one LLM call."""
    ranked = rank_sources(sources)
    for index, source in enumerate(ranked, start=1):
        source["id"] = f"S{index}"
    return ranked


def public_sources(sources):
    return [
        {key: source.get(key) for key in ("id", "url", "title", "publisher", "published_at", "origin", "official", "law_firm")}
        for source in sources
    ]


def missing_key_fields(result):
    """Fields still missing after verification (incident fields, and people reported killed)."""
    missing = {field for field in KEY_INCIDENT_FIELDS if result["field_status"].get(field) == "missing"}
    for person in result["people"]:
        if person["summary"]["outcome"] != "killed":
            continue
        if person["name_status"] == "unnamed":
            missing.add("name of the person killed")
        for field in KEY_PERSON_FIELDS:
            if person["field_status"].get(field) == "missing":
                missing.add(field)
    return sorted(missing)


def named_people(result):
    return [p["summary"]["name"] for p in result["people"] if p["summary"]["name"]]


def describe_for_search(incident, result):
    """Context the follow-up and web searches use."""
    incident["names"] = named_people(result)
    incident["people"] = [
        {"display_name": p["display_name"], "age": p["summary"]["age"], "outcome": p["summary"]["outcome"]}
        for p in result["people"]
    ]
    incident["known"] = {
        "incident_date": result["summary"]["incident_date"],
        "location": result["summary"]["location"],
        "headline": next(iter(incident.get("headlines") or []), None),
    }


def web_search_sources(client, incident, store, fetcher, missing, log):
    known = {source["url"] for source in store}
    urls, error = web_search_urls(client, llm_model(), incident, missing, known)
    log["web_search"] = {"missing": missing, "urls": urls, "error": error, "fetched": []}

    new_sources = []
    for url in urls:
        result = fetcher.fetch(url)
        entry = {"url": result.get("url") or url}
        if not result.get("ok"):
            entry.update(result="fetch_failed", error=result.get("error"))
        elif not mentions_any(result["text"], incident["names"]):
            entry["result"] = "does_not_name_person"
        else:
            entry["result"] = "added"
            new_sources.append({
                "url": entry["url"],
                "title": None,
                "publisher": host_of(entry["url"]),
                "published_at": None,
                "origin": "llm_web_search",
                "text": result["text"],
            })
        log["web_search"]["fetched"].append(entry)
    return new_sources


# --------------------------------------------------
# One incident
# --------------------------------------------------

def analyse(client, incident, store, log):
    sources = numbered(store)
    log["llm_calls"] = log.get("llm_calls", 0) + 1
    evidence = extract_evidence(client, incident, sources)
    return verify_incident(incident, evidence, sources), sources


def analyse_incident(incident, client, fetcher, budget):
    """(result, sources, log) for one incident with article text."""
    log = {"already_fetched": 0, "followup_added": 0, "web_search_added": 0}
    store = []
    log["already_fetched"] = add_sources(store, existing_sources(incident))

    result, sources = analyse(client, incident, store, log)
    describe_for_search(incident, result)

    # Follow-up reports can only be matched to this incident by a person's name.
    if incident["names"]:
        known_urls = set(incident["source_urls_set"]) | {s["url"] for s in store}
        log["followup_added"] = add_sources(store, search_followups(incident, known_urls, fetcher, log))
        if log["followup_added"]:
            result, sources = analyse(client, incident, store, log)
            describe_for_search(incident, result)

    missing = missing_key_fields(result)
    if LLM_WEB_SEARCH and missing and incident["names"] and budget[0] > 0:
        budget[0] -= 1
        log["web_search_added"] = add_sources(store, web_search_sources(client, incident, store, fetcher, missing, log))
        if log["web_search_added"]:
            result, sources = analyse(client, incident, store, log)

    log["sources_used"] = [s["url"] for s in sources]
    return result, sources, log


def incident_output(incident, status, result=None, sources=None, log=None, error=None):
    output = {
        "incident_id": incident["incident_key"],
        "input_fingerprint": input_fingerprint(incident),
        "analysis_status": status,
        "analysed_at": now(),
        "error": error,
        "grouping": incident["grouping"],
        "articles": [
            {key: article.get(key) for key in ("article_id", "url", "title", "publisher", "published_at", "fetch_status")}
            for article in incident["articles"]
        ],
        "sources": public_sources(sources or []),
        "analysis_log": log or {},
    }
    if result:
        output.update(result)
    else:
        output.update(
            in_scope=None,
            scope_reasons=[],
            evidence_status="insufficient",
            open_questions=["no article text could be read" if status == NO_TEXT else "analysis failed"],
            people=[],
        )
    return output


# --------------------------------------------------
# Run
# --------------------------------------------------

def previous_results():
    """incident_id -> analysed record from the last output."""
    data = read_json(OUTPUT_FILE, default={}) or {}
    return {
        record["incident_id"]: record
        for record in data.get("incidents") or []
        if record.get("incident_id") and record.get("analysis_status") == ANALYSED
    }


def save(records):
    statuses = Counter(r["analysis_status"] for r in records)
    evidence = Counter(r.get("evidence_status") for r in records if r["analysis_status"] == ANALYSED)
    return write_stage_json(MODULE, OUTPUT_NAME, {
        "run_at": now(),
        "rules": (
            "Every value comes from a quoted sentence found in the cited source. "
            "confirmed = official source or 2+ independent sites; single_source = one site; "
            "conflict = sources disagree; missing = not found (left null). "
            "Fault is reported as the sources state it, by statement type, and is never inferred "
            "from being struck, being a passenger, or another person's arrest or citation; "
            "it is not a legal determination. in_scope = no check failed (crash date in window, "
            "in California, a death reported). Residence is only what the sources state; "
            "home addresses are not searched for."
        ),
        "summary": {
            "incidents": len(records),
            "by_analysis_status": dict(statuses),
            "by_evidence_status": dict(evidence),
            "in_scope": sum(1 for r in records if r.get("in_scope") is True),
            "people_killed": sum(r.get("summary", {}).get("people_killed", 0) for r in records if r.get("summary")),
        },
        "incidents": records,
    })


def main():
    incidents = load_incidents()
    previous = previous_results()
    records, pending = [], []

    for incident in incidents:
        old = previous.get(incident["incident_key"])
        if old and old.get("input_fingerprint") == input_fingerprint(incident):
            records.append(old)
        elif not existing_sources(incident):
            records.append(incident_output(incident, NO_TEXT))
        else:
            pending.append(incident)

    logger.info(
        "Incidents: %s (%s unchanged since the last analysis, %s without text, %s to analyse)",
        len(incidents), len(records) - sum(1 for r in records if r["analysis_status"] == NO_TEXT),
        sum(1 for r in records if r["analysis_status"] == NO_TEXT), len(pending),
    )
    if not pending:
        save(records)
        logger.info("Nothing new to analyse.")
        return True

    client = build_llm()  # raises when OPENAI_API_KEY is missing: the run stops here
    fetcher = Fetcher()
    budget = [MAX_WEB_SEARCHES_PER_RUN]
    failures = 0

    for index, incident in enumerate(pending, start=1):
        logger.info("[%s/%s] Analysing %s (%s articles)", index, len(pending), incident["incident_key"], len(incident["articles"]))
        try:
            result, sources, log = analyse_incident(incident, client, fetcher, budget)
            records.append(incident_output(incident, ANALYSED, result, sources, log))
            logger.info(
                "  in_scope=%s evidence=%s people=%s killed=%s",
                result["in_scope"], result["evidence_status"], len(result["people"]), result["summary"]["people_killed"],
            )
        except Exception as error:
            failures += 1
            logger.exception("  Analysis failed (will be retried next run)")
            records.append(incident_output(incident, FAILED, error=f"{type(error).__name__}: {error}"))

        fetcher.save()
        save(records)

    logger.info("Done. Output: %s", OUTPUT_FILE)
    if failures == len(pending):
        logger.error("Every incident analysis failed.")
        return False
    return True


if __name__ == "__main__":
    configure_logging()
    main()
