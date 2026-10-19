"""Stage: contact enrichment. For every qualified lead, find the deceased person in SearchBug
(date of birth, last address, phones) and their relatives (name, relationship, address, phones).

Input:  data/lead_verification/verified_leads.json  (verdict "qualifies")
        data/severity_screening/passed_leads.json   (article text: relationships named in the news)
Output: data/contact_enrichment/contacts.json         - short: one entry per deceased person
        data/contact_enrichment/contacts.csv          - the same as a table: one row per relative
                                                        (one row for a person with no relatives found)
        data/contact_enrichment/contacts_details.json - every candidate, match reasons, lookups made
        data/contact_enrichment/searchbug_errors.json - every failed SearchBug call

Matching never guesses: a record is used only when the name agrees and the age or a death record
agrees, and it is "matched" only when the person also lived in a reported city or has a death record.
Name and age alone -> "possible_match" (a person confirms; relatives not looked up). Several
candidates -> "ambiguous" (a person chooses). Every search too broad -> "too_many_results".
Nothing found -> "not_found".
A relationship comes from SearchBug's relationshipType or from the articles; otherwise it is
"relative (relationship not stated)". SearchBug results are leads, not verified relationships.

Every response is cached (data/state/searchbug_cache.json), so a rerun never pays twice, and at most
SEARCHBUG_MAX_LOOKUPS_PER_RUN paid lookups are made per run. Results are saved after every person.
"""

import hashlib
import json
import logging
from collections import Counter
from datetime import datetime, timezone

from src.core.logger import configure_logging
from src.core.outputs import read_json, write_json_atomic, write_stage_csv, write_stage_json
from src.modules.contact_enrichment import config
from src.modules.contact_enrichment.client import SearchBugClient, SearchBugError
from src.modules.contact_enrichment.match import AMBIGUOUS, MATCHED, NOT_FOUND, POSSIBLE, choose, relationship_from_articles
from src.modules.contact_enrichment.parse import find_people

logger = logging.getLogger(__name__)

SKIPPED_BUDGET = "skipped_lookup_limit"
TOO_MANY = "too_many_results"   # every search was too broad (common names): needs a narrower search
FAILED = "lookup_failed"
NO_NAME = "no_name"


def now():
    return datetime.now(timezone.utc).isoformat()


class BudgetExceeded(Exception):
    pass


class Lookups:
    """SearchBug calls with a cache and a per-run limit on paid lookups."""

    def __init__(self, client, limit):
        self.client, self.limit, self.paid = client, limit, 0
        self.cache = read_json(config.CACHE_FILE, default={}) or {}

    @staticmethod
    def key(fields):
        return hashlib.sha1(json.dumps(fields, sort_keys=True).encode()).hexdigest()

    def search(self, fields):
        key = self.key(fields)
        if key in self.cache:
            if self.cache[key].get("too_many_results"):
                raise SearchBugError("too_many_results", "SearchBug: too many results (cached)")
            return self.cache[key]["response"]
        if self.paid >= self.limit:
            raise BudgetExceeded()
        self.paid += 1
        try:
            response = self.client.search(fields)
        except SearchBugError as error:
            if error.kind == "too_many_results":  # remember it, so the same search is not sent again
                self.cache[key] = {"fields": fields, "at": now(), "too_many_results": True}
                write_json_atomic(config.CACHE_FILE, self.cache)
            raise
        self.cache[key] = {"fields": fields, "at": now(), "response": response}
        write_json_atomic(config.CACHE_FILE, self.cache)
        return response


def split_name(name):
    parts = (name or "").split()
    return (parts[0], parts[-1]) if len(parts) >= 2 else (None, None)


def city_of(text):
    """'Fortuna Avenue near Encina Avenue, Modesto' -> 'Modesto' (the last comma part without digits)."""
    parts = [p.strip() for p in (text or "").split(",") if p.strip()]
    parts = [p for p in parts if p.lower() not in ("california", "ca") and not any(ch.isdigit() for ch in p)]
    return parts[-1] if parts else None


def search_plans(target):
    """Searches to try, most specific first: name + city (residence, then crash city), then name + state."""
    first, last = split_name(target["name"])
    plans, seen = [], set()
    for city in target["cities"]:
        fields = {"FNAME": first, "LNAME": last, "CITY": city.upper(), "STATE": "CA"}
        if city.lower() not in seen:
            seen.add(city.lower())
            plans.append(fields)
    plans.append({"FNAME": first, "LNAME": last, "STATE": "CA"})
    return plans


def find_deceased(lookups, target):
    """(status, chosen, candidates_summary, lookups_made)."""
    made, summary, too_broad = [], [], False
    for fields in search_plans(target):
        try:
            response = lookups.search(fields)
        except SearchBugError as error:
            if error.kind != "too_many_results":
                raise
            too_broad = True
            made.append({"fields": fields, "result": "too_many_results"})
            continue
        candidates = find_people(response)
        made.append({"fields": fields, "candidates": len(candidates)})
        status, chosen, scored = choose(target, candidates)
        summary = [
            {"names": c["names"][:3], "dobs": c["dobs"], "dods": c["dods"], "deceased_flag": c["deceased_flag"],
             "cities": [a["city"] for a in c["addresses"][:3]],
             "accepted": ok, "reasons": reasons, "token": c["token"]}
            for c, ok, reasons in scored
        ]
        if status in (MATCHED, AMBIGUOUS, POSSIBLE):
            return status, chosen, summary, made
    return (TOO_MANY if too_broad and not summary else NOT_FOUND), None, summary, made


def relative_contact(lookups, relative, texts):
    """One relative: relationship, and current address and phones from a follow-up lookup by token."""
    relationship, source, quote = relative["relationship"], "searchbug" if relative["relationship"] else None, None
    if not relationship:
        relationship, quote = relationship_from_articles(relative["names"], texts)
        source = "article" if relationship else None
    record = {
        "name": relative["names"][0] if relative["names"] else None,
        "relationship": relationship or "relative (relationship not stated)",
        "relationship_source": source,
        "relationship_quote": quote,
        "address": relative["addresses"][0]["address"] if relative["addresses"] else None,
        "phones": relative["phones"],
        "lookup": "not_needed",
    }
    if relative["token"]:
        try:
            people = find_people(lookups.search({"reportToken": relative["token"]}))
            if people:
                person = people[0]
                record["address"] = (person["addresses"][0]["address"] if person["addresses"] else record["address"])
                known = {p["number"] for p in record["phones"]}
                record["phones"] = record["phones"] + [p for p in person["phones"] if p["number"] not in known]
                record["emails"] = person["emails"]
                record["lookup"] = "found"
            else:
                record["lookup"] = "nothing_more_found"
        except BudgetExceeded:
            record["lookup"] = SKIPPED_BUDGET  # this relative's details wait for the next run
        except SearchBugError as error:
            record["lookup"] = FAILED
            record["lookup_error"] = error.to_dict()
    return record


def targets(leads):
    """One target per deceased person of a qualified lead."""
    out = []
    for lead in leads:
        if lead.get("verdict") not in config.VERDICTS_TO_ENRICH:
            continue
        for person in lead.get("deceased") or []:
            cities = [c for c in (person.get("residence"), city_of(lead.get("crash_location"))) if c]
            out.append({
                "incident_id": lead["incident_id"],
                "name": person.get("name"),
                "age": person.get("age"),
                "crash_date": lead.get("crash_date"),
                "cities": list(dict.fromkeys(cities)),
                "sources": lead.get("sources") or [],
            })
    return out


def article_texts(incident_id, passed):
    for lead in passed:
        if lead.get("incident_id") == incident_id:
            return [a.get("text") or "" for a in lead.get("articles") or []]
    return []


def enrich(lookups, target, texts):
    """(short_entry, detail_entry) for one deceased person."""
    base = {"incident_id": target["incident_id"], "name": target["name"], "age": target["age"],
            "crash_date": target["crash_date"]}
    if not target["name"] or not split_name(target["name"])[0]:
        return dict(base, match=NO_NAME, deceased=None, relatives=[]), dict(base, match=NO_NAME)

    status, chosen, candidates, made = find_deceased(lookups, target)
    detail = dict(base, match=status, searches=made, candidates=candidates)
    if status == POSSIBLE:
        # name and age agree but nothing else does: a person confirms before relatives are paid for
        possible = {"dob": next((d["date"] for d in chosen["dobs"] if d["date"]), None),
                    "last_address": chosen["addresses"][0]["address"] if chosen["addresses"] else None,
                    "searchbug_token": chosen["token"]}
        return dict(base, match=POSSIBLE, deceased=possible, relatives=[], candidates=1), detail
    if status != MATCHED:
        return dict(base, match=status, deceased=None, relatives=[],
                    candidates=len(candidates) if status == AMBIGUOUS else 0), detail

    relatives = [relative_contact(lookups, r, texts) for r in chosen["relatives"][:config.MAX_RELATIVES]]
    deceased = {
        "dob": next((d["date"] for d in chosen["dobs"] if d["date"]), None),
        "date_of_death": next((d["date"] for d in chosen["dods"] if d["date"]), None),
        "last_address": chosen["addresses"][0]["address"] if chosen["addresses"] else None,
        "phones": chosen["phones"],
        "searchbug_token": chosen["token"],
    }
    detail.update(chosen_record=chosen, relatives_found=len(chosen["relatives"]))
    return dict(base, match=MATCHED, deceased=deceased, relatives=relatives), detail


CSV_COLUMNS = [
    "incident_id", "crash_date", "match_status",
    "deceased_name", "deceased_age", "deceased_dob", "deceased_date_of_death", "deceased_last_address",
    "deceased_phones",
    "relative_name", "relationship", "relationship_source", "relative_address", "relative_phones",
    "relative_emails", "relative_lookup",
]


def _phones(phones):
    return "; ".join(f"{p['number']} ({p['type']})" if p.get("type") else p["number"] for p in phones or [])


def csv_rows(entries):
    """One row per relative; a deceased person with no relatives (or no match) still gets one row."""
    rows = []
    for entry in entries:
        deceased = entry.get("deceased") or {}
        person = {
            "incident_id": entry.get("incident_id"),
            "crash_date": entry.get("crash_date"),
            "match_status": entry.get("match"),
            "deceased_name": entry.get("name"),
            "deceased_age": entry.get("age"),
            "deceased_dob": deceased.get("dob"),
            "deceased_date_of_death": deceased.get("date_of_death"),
            "deceased_last_address": deceased.get("last_address"),
            "deceased_phones": _phones(deceased.get("phones")),
        }
        relatives = entry.get("relatives") or []
        if not relatives:
            rows.append(person)
        for relative in relatives:
            rows.append(dict(
                person,
                relative_name=relative.get("name"),
                relationship=relative.get("relationship"),
                relationship_source=relative.get("relationship_source"),
                relative_address=relative.get("address"),
                relative_phones=_phones(relative.get("phones")),
                relative_emails="; ".join(relative.get("emails") or []),
                relative_lookup=relative.get("lookup"),
            ))
    return rows


def save(entries, details, errors):
    write_stage_json(config.MODULE, config.ERRORS_NAME, errors)
    write_stage_csv(config.MODULE, config.CSV_NAME, CSV_COLUMNS, csv_rows(entries))
    write_stage_json(config.MODULE, config.DETAILS_NAME, {"run_at": now(), "people": details})
    return write_stage_json(config.MODULE, config.OUTPUT_NAME, {
        "run_at": now(),
        "note": ("SearchBug results are leads, not verified identities or relationships. Confirm identity and "
                 "relationship before any contact, and follow the rules that apply to contacting families."),
        "summary": {
            "people": len(entries),
            "by_match": dict(Counter(e["match"] for e in entries)),
            "relatives": sum(len(e.get("relatives") or []) for e in entries),
            "errors": len(errors),
        },
        "people": entries,
    })


def main(client=None):
    data = read_json(config.LEADS_FILE)
    if not isinstance(data, dict) or not isinstance(data.get("leads"), list):
        raise FileNotFoundError(f"Leads file missing or invalid: {config.LEADS_FILE} (run lead_verification first)")
    passed = (read_json(config.PASSED_LEADS_FILE, default={}) or {}).get("leads") or []
    people = targets(data["leads"])
    logger.info("Deceased people in qualified leads: %s", len(people))
    if not people:
        save([], [], [])
        return True

    try:
        client = client or SearchBugClient.from_env()
    except SearchBugError as error:
        logger.error("Contact enrichment cannot run: %s", error)
        return False

    lookups = Lookups(client, config.MAX_LOOKUPS_PER_RUN)
    entries, details, errors = [], [], []
    for index, target in enumerate(people, start=1):
        logger.info("[%s/%s] %s (%s)", index, len(people), target["name"] or "unnamed", target["incident_id"])
        try:
            entry, detail = enrich(lookups, target, article_texts(target["incident_id"], passed))
            logger.info("  match=%s relatives=%s", entry["match"], len(entry.get("relatives") or []))
        except BudgetExceeded:
            logger.warning("  paid lookup limit (%s) reached; the rest wait for the next run", config.MAX_LOOKUPS_PER_RUN)
            entry = detail = {"incident_id": target["incident_id"], "name": target["name"], "match": SKIPPED_BUDGET}
        except SearchBugError as error:
            logger.error("  SearchBug failed: %s", error)
            entry = detail = {"incident_id": target["incident_id"], "name": target["name"], "match": FAILED,
                              "error": error.to_dict()}
            errors.append(dict(error.to_dict(), incident_id=target["incident_id"], name=target["name"], at=now()))
        entries.append(entry)
        details.append(detail)
        save(entries, details, errors)

    logger.info("Contact enrichment done: %s (paid lookups this run: %s)",
                dict(Counter(e["match"] for e in entries)), lookups.paid)
    if errors and len(errors) == len(people):
        logger.error("Every SearchBug lookup failed; details in %s", config.ERRORS_NAME)
        return False
    return True


if __name__ == "__main__":
    configure_logging()
    main()
