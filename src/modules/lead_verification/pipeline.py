"""Step 4: verify the shortlisted leads from article_analysis.

For each incident in shortlisted_articles.json:
  1. collect the article texts step 2 already fetched (no new requests);
  2. search Google News RSS for follow-up reports naming the people;
  3. LLM reads all sources and lists every fact with a source + exact quote;
  4. verifier.py keeps only quotes that really appear in the sources and
     sets each field to confirmed / single_source / conflict / missing;
  5. if key fields are still missing, the LLM web search proposes more URLs,
     which are fetched, checked, and steps 3-4 run again.

Output: data/verified_leads.json (final). data/fetch_cache.json only stores
downloaded pages so they are not fetched again.
"""

import json
import re
from datetime import date, datetime, timezone

from src.core.logger import configure_logging, get_logger
from src.modules.lead_verification.config import (
    DATA_DIR,
    KEY_INCIDENT_FIELDS,
    KEY_PERSON_FIELDS,
    LLM_WEB_SEARCH,
    MAX_WEB_SEARCHES_PER_RUN,
    OUTPUT_FILE,
)
from src.modules.lead_verification.evidence_llm import build_llm, extract_evidence, llm_model, rank_sources
from src.modules.lead_verification.fetcher import Fetcher
from src.modules.lead_verification.followup_search import search_followups
from src.modules.lead_verification.incidents import (
    existing_sources,
    group_incidents,
    host_of,
    is_law_firm,
    is_official,
    load_extracted_texts,
    load_shortlist,
    mentions_any,
    read_json,
)
from src.modules.lead_verification.llm_web_search import web_search_urls
from src.modules.lead_verification.verifier import verify_incident


DATA_DIR.mkdir(parents=True, exist_ok=True)
configure_logging()
logger = get_logger(__name__)

# Incidents with these results are not re-verified on the next run.
FINAL_STATUSES = {"verified", "rejected"}


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json_atomic(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


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


def missing_key_fields(leads):
    missing = set()
    for lead in leads:
        status = lead["field_status"]
        for field in KEY_PERSON_FIELDS + KEY_INCIDENT_FIELDS:
            key = "incident_date" if field == "date" else field
            if status.get(key) == "missing":
                missing.add(field)
        if lead["summary"]["at_fault"] in (None, "not_established"):
            missing.add("who caused the crash")
    return sorted(missing)


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

def verify_one(incident, extracted_records, client, fetcher, budget, today):
    """(leads, log). budget is a one-item list with the remaining web searches."""
    log = {"incident_key": incident["incident_key"], "names": incident["names"]}
    store = []

    log["already_fetched"] = add_sources(store, existing_sources(incident, extracted_records))

    known_urls = set(incident["source_urls_set"]) | {s["url"] for s in store}
    log["followup_added"] = add_sources(store, search_followups(incident, known_urls, fetcher, log))

    if not store:
        raise ValueError("No source text available for this incident.")

    sources = numbered(store)
    evidence = extract_evidence(client, incident, sources)
    leads = verify_incident(incident, evidence, sources, today)

    missing = missing_key_fields(leads)
    if LLM_WEB_SEARCH and missing and budget[0] > 0:
        budget[0] -= 1
        added = add_sources(store, web_search_sources(client, incident, store, fetcher, missing, log))
        log["web_search_added"] = added
        if added:
            sources = numbered(store)
            evidence = extract_evidence(client, incident, sources)
            leads = verify_incident(incident, evidence, sources, today)

    for lead, person in zip(leads, incident["people"]):
        lead.update({
            "lead_id": f"{incident['incident_key']}-{slug(person['person_name'])}",
            "incident_key": incident["incident_key"],
            "people_in_incident": incident["names"],
            "shortlist_entry": {
                key: person.get(key)
                for key in ("person_name", "person_age", "incident_date", "accident_location",
                            "lead_status", "lead_flags", "source_url", "case_ids")
            },
            "sources": public_sources(sources),
            "verified_at": now(),
        })
    log["sources_used"] = [s["url"] for s in sources]
    return leads, log


def failed_leads(incident, error):
    return [
        {
            "lead_id": f"{incident['incident_key']}-{slug(person['person_name'])}",
            "incident_key": incident["incident_key"],
            "verification_status": "verification_failed",
            "status_reasons": [str(error)],
            "summary": {"name": person["person_name"]},
            "shortlist_entry": {"person_name": person["person_name"], "source_url": person.get("source_url")},
            "verified_at": now(),
        }
        for person in incident["people"]
    ]


# --------------------------------------------------
# Run
# --------------------------------------------------

def previous_results():
    """incident_key -> leads, for incidents already verified or rejected."""
    data = read_json(OUTPUT_FILE, default={}) or {}
    by_incident = {}
    for lead in data.get("leads") or []:
        by_incident.setdefault(lead.get("incident_key"), []).append(lead)
    return {
        key: leads for key, leads in by_incident.items()
        if key and all(lead.get("verification_status") in FINAL_STATUSES for lead in leads)
    }


def save(results):
    leads = [lead for key in results for lead in results[key]]
    counts = {}
    for lead in leads:
        counts[lead["verification_status"]] = counts.get(lead["verification_status"], 0) + 1

    write_json_atomic(OUTPUT_FILE, {
        "run_at": now(),
        "rules": (
            "Every value comes from a quoted sentence that was found in the cited source. "
            "confirmed = official source or 2+ independent sites; single_source = one site; "
            "conflict = sources disagree; missing = not found (left null). "
            "verified = name, date and location confirmed and a source says the person was not at fault. "
            "Residence is only what the sources state; home addresses are not searched for."
        ),
        "status_counts": counts,
        "total_leads": len(leads),
        "leads": leads,
    })


def main():
    incidents = group_incidents(load_shortlist())
    extracted_records = load_extracted_texts()
    previous = previous_results()
    logger.info(
        "Shortlisted people: %s in %s incidents (crashes with several victims are verified together); "
        "%s incidents already final",
        sum(len(i["people"]) for i in incidents), len(incidents),
        sum(1 for i in incidents if i["incident_key"] in previous),
    )

    pending = [incident for incident in incidents if incident["incident_key"] not in previous]
    results = {incident["incident_key"]: previous[incident["incident_key"]]
               for incident in incidents if incident["incident_key"] in previous}

    if not pending:
        save(results)
        logger.info("Nothing new to verify.")
        return

    client = build_llm()
    fetcher = Fetcher()
    budget = [MAX_WEB_SEARCHES_PER_RUN]
    today = date.today()

    for index, incident in enumerate(pending, start=1):
        logger.info("[%s/%s] Verifying %s", index, len(pending), ", ".join(incident["names"]))
        try:
            leads, _log = verify_one(incident, extracted_records, client, fetcher, budget, today)
            for lead in leads:
                logger.info("  %s: %s %s", lead["summary"]["name"], lead["verification_status"],
                            "; ".join(lead["status_reasons"]))
        except Exception as error:
            logger.exception("  Verification failed (will be retried next run)")
            leads = failed_leads(incident, error)

        results[incident["incident_key"]] = leads
        fetcher.save()
        save(results)

    logger.info("Done. Output: %s", OUTPUT_FILE)


if __name__ == "__main__":
    main()
