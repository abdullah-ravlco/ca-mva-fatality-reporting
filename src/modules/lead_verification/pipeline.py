"""Step 4: lead verification. An agent investigates every grouped incident.

For each incident in data/incident_grouping/incidents.json the model reads the full text of
its articles and decides whether to search the web (and read more pages) or to return the
structured result: the deceased person's name, age and residence, the crash date, time and
location, who was at fault, whether anyone was impaired, whether another party was involved,
and whether the incident qualifies (see qualification.py). See agent.py for the agent loop.

Output: data/lead_verification/verified_leads.json. An incident whose articles have not
changed since the last run is not analysed again.
"""

import logging
from collections import Counter
from datetime import datetime, timezone

from src.core.date_utils import PACIFIC, reference_time
from src.core.logger import configure_logging
from src.core.outputs import read_json, write_stage_json
from src.modules.lead_verification.agent import check_quotes, run_agent
from src.modules.lead_verification.config import MODULE, OUTPUT_FILE, OUTPUT_NAME
from src.modules.lead_verification.fetcher import Fetcher
from src.modules.lead_verification.incidents import existing_sources, input_fingerprint, load_incidents
from src.modules.lead_verification.qualification import assess
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

    data = check_quotes(result.model_dump(), store or {})
    output.update(
        qualification=assess(result, reference_time().astimezone(PACIFIC).date()),
        incident=data["incident"],
        people=data["people"],
        missing_information=data["missing_information"],
        pages_used=data["pages_used"],
        agent_log=log or [],
    )
    return output


def previous_results():
    """incident_id -> analysed record from the last output."""
    data = read_json(OUTPUT_FILE, default={}) or {}
    return {
        record["incident_id"]: record
        for record in data.get("incidents") or []
        if record.get("incident_id") and record.get("analysis_status") == ANALYSED
    }


def save(records):
    verdicts = Counter(r["qualification"]["verdict"] for r in records)
    reasons = Counter(reason for r in records for reason in r["qualification"].get("reasons") or [])
    return write_stage_json(MODULE, OUTPUT_NAME, {
        "run_at": now(),
        "rules": (
            "qualifies = a death in a California road crash with another party involved, and a "
            "deceased person who was not reported impaired or at fault. does_not_qualify = no death, "
            "no other party (single-vehicle), every deceased person impaired or at fault, not a road "
            "crash, outside California or older than the window. needs_review = facts missing. "
            "Fault and impairment are what the sources report, not a legal finding. Residence is "
            "only what a source states; home addresses are not searched for. quote_verified = the "
            "quote was found in the page it cites."
        ),
        "summary": {
            "incidents": len(records),
            "by_verdict": dict(verdicts),
            "by_reason": dict(reasons),
            "by_analysis_status": dict(Counter(r["analysis_status"] for r in records)),
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

    logger.info("Done. Output: %s", OUTPUT_FILE)
    if failures == len(pending):
        logger.error("Every incident analysis failed.")
        return False
    return True


if __name__ == "__main__":
    configure_logging()
    main()
