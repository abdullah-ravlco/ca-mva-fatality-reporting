"""Stage: severity screening. JEV reads every grouped incident and only catastrophic-injury cases
caused by another party move on to lead_verification.

Input:  data/incident_grouping/incidents.json (full text of every article of each incident)
Output: data/severity_screening/screened_incidents.json - every incident with its status, tags,
          reasons, the facts mapped from JEV's answers (JEV's raw answers are under jev.answers)
        data/severity_screening/passed_leads.json       - only the incidents that passed, each with its
          screening result and the full content of its articles: the input of lead_verification
        data/severity_screening/screening_errors.json   - every failed JEV call, with the details

Statuses: qualified, victim_identity_unidentified, fault_unidentified (passed on to
lead_verification), dropped (with drop_reason: codes and text; not passed on), screening_failed
(JEV call failed; retried next run).
JEV answers classification questions (questions.py); the status is decided in decision.py from them.

Results are saved after every incident. An incident whose articles and prompt version have not
changed since it was screened is not sent to JEV again.
"""

import hashlib
import logging
from collections import Counter
from datetime import datetime, timezone

from src.core.logger import configure_logging
from src.core.outputs import read_json, write_stage_json
from src.modules.lead_verification.quotes import quote_in_text
from src.modules.severity_screening import config
from src.modules.severity_screening.decision import decide
from src.modules.severity_screening.jev_client import JevClient, JevError
from src.modules.severity_screening.prompt import build_user_message
from src.modules.severity_screening.questions import QUESTIONS, to_result
from src.modules.severity_screening.schema import JevResult

logger = logging.getLogger(__name__)

FAILED = "screening_failed"


def now():
    return datetime.now(timezone.utc).isoformat()


def load_incidents():
    data = read_json(config.INCIDENTS_FILE)
    if not isinstance(data, dict) or not isinstance(data.get("incidents"), list):
        raise FileNotFoundError(f"Incidents file missing or invalid: {config.INCIDENTS_FILE}")
    return data["incidents"]


def fingerprint(incident):
    """Changes when the incident's articles or the prompt/rules change."""
    urls = sorted(a.get("canonical_url") or a.get("url") or "" for a in incident.get("articles") or [])
    return hashlib.sha1("|".join([config.PROMPT_VERSION] + urls).encode()).hexdigest()[:16]


def check_quotes(facts, incident):
    """Add *_verified flags: is each quote really in the article it cites (or, failing that, any article)?"""
    texts = {}
    for article in incident.get("articles") or []:
        text = " ".join(p for p in (article.get("title"), article.get("description"), article.get("text")) if p)
        for url in (article.get("url"), article.get("final_url"), article.get("rss_url"), article.get("canonical_url")):
            if url:
                texts[url] = text
    everything = " ".join(texts.values())

    def verified(quote, url):
        if not quote:
            return None
        return quote_in_text(quote, texts.get(url or "") or everything)

    for victim in facts["victims"]:
        for prefix in ("injury", "fault", "impaired"):
            victim[f"{prefix}_quote_verified"] = verified(victim.get(f"{prefix}_quote"), victim.get(f"{prefix}_source_url"))
    party = facts["other_party"]
    party["quote_verified"] = verified(party.get("quote"), party.get("source_url"))
    return facts


def article_summary(incident):
    return [
        {key: article.get(key) for key in ("article_id", "url", "title", "publisher", "published_at", "text_available")}
        for article in incident.get("articles") or []
    ]


def screened_record(incident, result, meta, model):
    decision = decide(result)
    return {
        "incident_id": incident["incident_id"],
        "input_fingerprint": fingerprint(incident),
        "screened_at": now(),
        "status": decision["status"],
        "drop_reason": decision["drop_reason"],
        "tags": decision["tags"],
        "reasons": decision["reasons"],
        "corrections": decision["corrections"],
        "jev_status": decision["jev_status"],
        "jev_tags": decision["jev_tags"],
        "jev_drop_reasons": result.drop_reasons,
        "status_mismatch": decision["status_mismatch"],
        "jev_decision_reason": result.decision_reason,
        "jev_notes": result.notes,
        "per_victim": decision["per_victim"],
        "facts": check_quotes(
            result.model_dump(exclude={"case_status", "drop_reasons", "tags", "decision_reason", "notes"}), incident
        ),
        "articles": article_summary(incident),
        "jev": dict(meta, model=model, prompt_version=config.PROMPT_VERSION),
        "error": None,
    }


def failed_record(incident, error):
    return {
        "incident_id": incident["incident_id"],
        "input_fingerprint": fingerprint(incident),
        "screened_at": now(),
        "status": FAILED,
        "tags": [],
        "reasons": [f"JEV call failed ({error.kind}); retried next run"],
        "articles": article_summary(incident),
        "error": error.to_dict(),
    }


def previous_results():
    data = read_json(config.OUTPUT_FILE, default={}) or {}
    return {r["incident_id"]: r for r in data.get("incidents") or [] if r.get("incident_id") and r.get("status") != FAILED}


GROUPING_FIELDS = ("source_case_ids", "candidate_incident_dates", "locations", "conflicts",
                   "uncertainties", "grouping_rationale", "publishers")


def passed_lead(record, incident):
    """One entry of passed_leads.json: the screening result plus everything lead_verification reads."""
    return {
        "incident_id": record["incident_id"],
        "screening": {key: record.get(key) for key in ("status", "tags", "reasons", "per_victim", "facts")},
        "grouping": {key: incident.get(key) for key in GROUPING_FIELDS},
        "articles": incident.get("articles") or [],
    }


def save(records, errors, incidents_by_id):
    write_stage_json(config.MODULE, config.ERRORS_NAME, errors)
    leads = [
        passed_lead(record, incidents_by_id[record["incident_id"]])
        for record in records
        if record["status"] in config.PASS_ON_STATUSES and record["incident_id"] in incidents_by_id
    ]
    write_stage_json(config.MODULE, config.PASSED_LEADS_NAME, {
        "run_at": now(),
        "statuses": list(config.PASS_ON_STATUSES),
        "count": len(leads),
        "leads": leads,
    })
    return write_stage_json(config.MODULE, config.OUTPUT_NAME, {
        "run_at": now(),
        "prompt_version": config.PROMPT_VERSION,
        "summary": {
            "incidents": len(records),
            "by_status": dict(Counter(r["status"] for r in records)),
            "dropped_by_reason": dict(Counter(
                code for r in records if r["status"] == "dropped" for code in (r.get("drop_reason") or {}).get("codes", [])
            )),
            "passed_on_to_lead_verification": sum(1 for r in records if r["status"] in config.PASS_ON_STATUSES),
            "status_mismatch_with_jev": sum(1 for r in records if r.get("status_mismatch")),
            "errors": len(errors),
        },
        "incidents": records,
    })


def redecide():
    """Re-decide every saved screening result with the current rules, without calling JEV."""
    incidents = load_incidents()
    by_id = {incident["incident_id"]: incident for incident in incidents}
    data = read_json(config.OUTPUT_FILE, default={}) or {}
    records = []
    for record in data.get("incidents") or []:
        if record.get("status") == FAILED or record["incident_id"] not in by_id:
            records.append(record)
            continue
        facts = record["facts"]
        clean = {
            **{k: v for k, v in facts.items() if k not in ("victims", "other_party")},
            "victims": [{k: v for k, v in victim.items() if not k.endswith("_verified")} for victim in facts["victims"]],
            "other_party": {k: v for k, v in facts["other_party"].items() if k != "quote_verified"},
            "case_status": record.get("jev_status"), "tags": record.get("jev_tags") or [],
            "drop_reasons": record.get("jev_drop_reasons") or [],
            "decision_reason": record.get("jev_decision_reason"), "notes": record.get("jev_notes"),
        }
        result = JevResult.model_validate(clean)
        new = screened_record(by_id[record["incident_id"]], result, record.get("jev") or {}, (record.get("jev") or {}).get("model"))
        new["screened_at"], new["jev"] = record.get("screened_at"), record.get("jev")
        new["corrections"] = list(dict.fromkeys((record.get("corrections") or []) + new["corrections"]))
        logger.info("%s: %s -> %s %s", record["incident_id"], record.get("status"), new["status"], new["corrections"])
        records.append(new)
    save(records, read_json(module_errors_file(), default=[]) or [], by_id)
    return records


def module_errors_file():
    return config.OUTPUT_FILE.parent / config.ERRORS_NAME


def main(client=None):
    incidents = load_incidents()
    by_id = {incident["incident_id"]: incident for incident in incidents}
    previous = previous_results()
    records, pending = [], []
    for incident in incidents:
        old = previous.get(incident["incident_id"])
        if old and old.get("input_fingerprint") == fingerprint(incident):
            records.append(old)
        else:
            pending.append(incident)
    logger.info("Incidents: %s (%s to screen, %s unchanged)", len(incidents), len(pending), len(records))

    errors = []
    if not pending:
        save(records, errors, by_id)
        return True

    try:
        client = client or JevClient.from_env()
    except JevError as error:
        logger.error("Severity screening cannot run: %s", error)
        return False

    for index, incident in enumerate(pending, start=1):
        incident_id = incident["incident_id"]
        logger.info("[%s/%s] Screening %s (%s articles)", index, len(pending), incident_id, len(incident.get("articles") or []))
        try:
            answers, meta = client.ask(build_user_message(incident), QUESTIONS)
            result = to_result(answers, incident)
            meta = dict(meta, answers=answers)
            record = screened_record(incident, result, meta, client.model)
            logger.info("  status=%s%s tags=%s%s", record["status"],
                        f" ({record['drop_reason']['text']})" if record["drop_reason"] else "", record["tags"],
                        f" (JEV said {record['jev_status']})" if record["status_mismatch"] else "")
        except JevError as error:
            logger.error("  JEV failed for %s: %s", incident_id, error)
            if error.response_excerpt:
                logger.error("  response: %s", error.response_excerpt[:300])
            record = failed_record(incident, error)
            errors.append(dict(error.to_dict(), incident_id=incident_id, at=now()))
        records.append(record)
        save(records, errors, by_id)  # after every incident, so a stopped run keeps what it did

    path = save(records, errors, by_id)
    logger.info("Screening complete: %s -> %s", dict(Counter(r["status"] for r in records)), path)
    if errors:
        logger.warning("%s JEV call(s) failed; details in %s", len(errors), config.ERRORS_NAME)
    if len(errors) == len(pending):
        logger.error("Every JEV call failed.")
        return False
    return True


if __name__ == "__main__":
    import sys

    configure_logging()
    redecide() if "--redecide" in sys.argv else main()
