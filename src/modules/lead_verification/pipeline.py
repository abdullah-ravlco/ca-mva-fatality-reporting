"""Step 4: lead verification. An agent investigates every grouped incident.

For each lead in data/severity_screening/passed_leads.json (only incidents that passed severity
screening), one at a time, the model reads the full text of its articles and what screening found, and decides whether to search the web (and read more pages) or to return the
structured result: the deceased person's name, age and residence, the crash date, time and
location, who was at fault, whether anyone was impaired, whether another party was involved,
and whether the incident qualifies (see qualification.py). See agent.py for the agent loop.

Output: data/lead_verification/verified_leads.json         - one short entry per lead, required fields only
        data/lead_verification/verified_leads_details.json - the full record (every person, quotes, agent log)
An incident whose articles have not changed since the last run is not analysed again.
"""

import logging
from collections import Counter
from datetime import datetime, timezone

from src.core.logger import configure_logging
from src.core.outputs import read_json, write_stage_json
from src.modules.lead_verification.agent import check_quotes, run_agent
from src.modules.lead_verification.checks import apply_checks
from src.modules.lead_verification.config import DETAILS_FILE, DETAILS_NAME, MODULE, OUTPUT_FILE, OUTPUT_NAME
from src.modules.lead_verification.fetcher import Fetcher
from src.modules.lead_verification.incidents import existing_sources, input_fingerprint, load_incidents
from src.modules.lead_verification.qualification import assess
from src.modules.lead_verification.schema import LeadResult
from src.core.llm_client import build_llm, llm_model

logger = logging.getLogger(__name__)

ANALYSED = "analysed"
NO_TEXT = "no_text"   # no article text could be read; nothing to give the model
FAILED = "failed"     # model or processing error; retried next run


def now():
    return datetime.now(timezone.utc).isoformat()


def incident_output(incident, status, result=None, store=None, log=None, error=None):
    output = {
        "incident_id": incident["incident_key"],
        "input_fingerprint": input_fingerprint(incident),
        "analysis_status": status,
        "analysed_at": now(),
        "error": error,
        "grouping": incident["grouping"],
        "screening": {key: (incident.get("screening") or {}).get(key) for key in ("status", "tags", "reasons")},
        "articles": [
            {key: article.get(key) for key in ("article_id", "url", "title", "publisher", "published_at", "fetch_status")}
            for article in incident["articles"]
        ],
    }
    if result is None:
        output["qualification"] = {
            "verdict": "needs_review",
            "reasons": ["insufficient_information"],
            "explanation": "no article text could be read" if status == NO_TEXT else "analysis failed",
        }
        return output

    corrections = apply_checks(result)  # remove claims the quotes do not support, before deciding
    data = check_quotes(result.model_dump(), store or {})
    output.update(
        qualification=dict(assess(result), corrections=corrections),
        incident=data["incident"],
        people=data["people"],
        missing_information=data["missing_information"],
        pages_used=data["pages_used"],
        agent_log=log or [],
    )
    return output


def previous_results():
    """incident_id -> analysed record from the last output."""
    data = read_json(DETAILS_FILE, default={}) or {}
    return {
        record["incident_id"]: record
        for record in data.get("incidents") or []
        if record.get("incident_id") and record.get("analysis_status") == ANALYSED
    }


def _age(value):
    text = str(value or "")
    return int(text) if text.isdigit() else (value or None)


def _value(fact):
    return (fact or {}).get("value")


def compact(record):
    """The short entry of verified_leads.json: only what is needed to act on the lead."""
    qualification = record.get("qualification") or {}
    incident = record.get("incident") or {}
    people = record.get("people") or []
    deceased = [p for p in people if p.get("outcome") == "killed"]
    verdict = qualification.get("verdict")
    if verdict == "does_not_qualify":
        reason = ", ".join(qualification.get("reasons") or []) or qualification.get("explanation")
    elif record.get("analysis_status") != ANALYSED:
        reason = record.get("error") or qualification.get("explanation")
    else:
        reason = qualification.get("explanation")
        agent = qualification.get("agent_verdict")
        if agent and agent != verdict:  # the rules decided; say so, the model's text argues otherwise
            reason = f"{verdict} by the rules (the model said {agent}): {reason}"
    return {
        "incident_id": record["incident_id"],
        "verdict": verdict,
        "reason": reason,
        "crash_date": _value(incident.get("date")),
        "crash_time": _value(incident.get("time")),
        "crash_location": _value(incident.get("location")),
        "other_party": incident.get("other_party_description"),
        "other_party_at_fault": incident.get("other_party_at_fault"),
        "other_party_fault_source_url": incident.get("other_party_fault_source_url"),
        "deceased": [
            {
                "name": _value(p.get("name")),
                "age": _age(_value(p.get("age"))),
                "residence": _value(p.get("address")),
                "role": p.get("role"),
                "at_fault": p.get("at_fault"),
                "fault_basis": p.get("fault_basis"),
                "fault_explanation": p.get("fault_explanation"),
                "fault_source_url": p.get("fault_source_url"),
                "impaired": p.get("impaired"),
            }
            for p in deceased
        ],
        "missing": (record.get("missing_information") or []) + (qualification.get("open_items") or []),
        "corrections": qualification.get("corrections") or [],
        "sources": record.get("pages_used") or [a["url"] for a in record.get("articles") or [] if a.get("url")],
    }


def save(records):
    verdicts = Counter(r["qualification"]["verdict"] for r in records)
    reasons = Counter(reason for r in records for reason in r["qualification"].get("reasons") or [])
    summary = {
        "leads": len(records),
        "by_verdict": dict(verdicts),
        "by_reason": dict(reasons),
        "by_analysis_status": dict(Counter(r["analysis_status"] for r in records)),
    }
    write_stage_json(MODULE, OUTPUT_NAME, {
        "run_at": now(),
        "summary": summary,
        "details_file": DETAILS_NAME,
        "leads": [compact(record) for record in records],
    })
    return write_stage_json(MODULE, DETAILS_NAME, {
        "run_at": now(),
        "rules": (
            "qualifies = a death in a California road crash with another party involved and at fault, "
            "and a deceased person who was not reported impaired or at fault. fault_unidentified = the "
            "same, except the other party's fault is not established. does_not_qualify = no death, "
            "no other party (single-vehicle), every deceased person impaired or at fault, not a road "
            "crash, outside California or older than the window. needs_review = facts missing. "
            "Fault and impairment are what the sources report, not a legal finding. Residence is "
            "only what a source states; home addresses are not searched for. quote_verified = the "
            "quote was found in the page it cites."
        ),
        "summary": summary,
        "incidents": records,
    })


def result_from_record(record):
    """Rebuild a LeadResult from a saved detailed record (to re-decide without calling the model).

    Records saved before other_party_at_fault existed take it from the deceased person's fault
    statement: the deceased "not at fault" with a quote describing the other party."""
    incident = dict(record.get("incident") or {})
    people = [{k: v for k, v in p.items() if not k.endswith("_verified")} for p in record.get("people") or []]
    for fact in ("date", "time", "location"):
        incident[fact] = {k: v for k, v in (incident.get(fact) or {}).items() if k != "quote_verified"}
    for person in people:
        for fact in ("name", "age", "address"):
            person[fact] = {k: v for k, v in (person.get(fact) or {}).items() if k != "quote_verified"}
    if "other_party_at_fault" not in incident:
        source = next((p for p in people if p.get("outcome") == "killed" and p.get("at_fault") == "no"
                       and p.get("fault_quote") and p.get("fault_basis") != "none"), None)
        if source:
            incident.update(other_party_at_fault="yes", other_party_fault_basis=source["fault_basis"],
                            other_party_fault_quote=source["fault_quote"], other_party_fault_source_url=source["fault_source_url"])
    qualification = record.get("qualification") or {}
    agent_reasons = [r for r in qualification.get("reasons") or [] if r == "not_a_road_crash"]
    return LeadResult.model_validate({
        "incident": incident,
        "people": people,
        "assessment": {"verdict": qualification.get("agent_verdict") or "needs_review",
                       "reasons": agent_reasons, "explanation": qualification.get("explanation")},
        "missing_information": record.get("missing_information") or [],
        "pages_used": record.get("pages_used") or [],
    })


def reassess():
    """Re-decide every saved lead with the current rules and checks, without calling the model."""
    data = read_json(DETAILS_FILE, default={}) or {}
    incidents = {i["incident_key"]: i for i in load_incidents()}
    records = []
    for record in data.get("incidents") or []:
        incident = incidents.get(record["incident_id"])
        if record.get("analysis_status") != ANALYSED or incident is None:
            records.append(record)
            continue
        store = {a.get("url"): a.get("text") for a in incident["articles"] if a.get("text")}
        new = incident_output(incident, ANALYSED, result_from_record(record), store, record.get("agent_log"))
        new["analysed_at"] = record.get("analysed_at")
        # keep corrections made by earlier passes (their claims are already gone from the record)
        earlier = (record.get("qualification") or {}).get("corrections") or []
        new["qualification"]["corrections"] = list(dict.fromkeys(earlier + new["qualification"]["corrections"]))
        records.append(new)
        logger.info("%s: %s -> %s %s", record["incident_id"], (record.get("qualification") or {}).get("verdict"),
                    new["qualification"]["verdict"], new["qualification"]["corrections"])
    save(records)
    return records


def main():
    incidents = load_incidents()  # only the leads that passed severity screening
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

    logger.info("Incidents: %s (%s to analyse, the rest unchanged or without text)", len(incidents), len(pending))
    if not pending:
        save(records)
        logger.info("Nothing new to analyse.")
        return True

    client = build_llm()  # raises when OPENAI_API_KEY is missing: the run stops here
    model = llm_model()
    fetcher = Fetcher()
    failures = 0

    for index, incident in enumerate(pending, start=1):
        logger.info("[%s/%s] Investigating %s (%s articles)", index, len(pending), incident["incident_key"], len(incident["articles"]))
        try:
            result, store, log = run_agent(client, model, incident, existing_sources(incident), fetcher)
            record = incident_output(incident, ANALYSED, result, store, log)
            records.append(record)
            logger.info(
                "  verdict=%s reasons=%s tool_calls=%s",
                record["qualification"]["verdict"], record["qualification"]["reasons"], len(log),
            )
        except Exception as error:
            failures += 1
            logger.exception("  Analysis failed (will be retried next run)")
            records.append(incident_output(incident, FAILED, error=f"{type(error).__name__}: {error}"))

        fetcher.save()
        save(records)

    logger.info("Done. Leads: %s (full details: %s)", OUTPUT_FILE, DETAILS_FILE)
    if failures == len(pending):
        logger.error("Every incident analysis failed.")
        return False
    return True


if __name__ == "__main__":
    import sys

    configure_logging()
    reassess() if "--reassess" in sys.argv else main()
