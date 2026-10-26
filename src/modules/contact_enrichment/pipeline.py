"""Stage: contact enrichment. For every qualified lead, find the deceased person in SearchBug
(date of birth, last address, phones) and their relatives (name, relationship, address, phones).

Input:  data/lead_verification/verified_leads.json  (verdict "qualifies")
        data/severity_screening/passed_leads.json   (article text: relationships named in the news)
Output: data/contact_enrichment/contacts.json         - short: one entry per deceased person
        data/contact_enrichment/contacts.csv          - the team's mailer layout: one row per deceased person
                                                        (name, death date, aliases, address, phones, age,
                                                        locations, accident notes), then match status and
                                                        up to 2 next of kin, closest first
        data/contact_enrichment/contacts_details.json - every candidate, match reasons, lookups made
        data/contact_enrichment/searchbug_errors.json - every failed SearchBug call

Matching never guesses: a record is used only when the name agrees and the age or a death record
agrees, and it is "matched" only when the person also lived in a reported city or has a death record.
Name and age alone -> "possible_match" (details and next of kin are still fetched, but a person must
confirm it is the right person before any contact). Several
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
import re
from collections import Counter
from datetime import datetime, timezone

from src.core.logger import configure_logging
from src.core.outputs import read_json, write_json_atomic, write_stage_csv, write_stage_json
from src.modules.contact_enrichment import config, sheets
from src.modules.contact_enrichment.client import SearchBugClient, SearchBugError
from src.modules.contact_enrichment.match import (
    AMBIGUOUS, MATCHED, NOT_FOUND, POSSIBLE, choose, closeness, relationship_from_articles,
)
from src.modules.contact_enrichment.parse import find_people, most_recent_phones

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


# A name that says there is no name: "Not yet identified", "Unidentified woman", "(named in articles)".
PLACEHOLDER_NAME_RE = re.compile(
    r"\b(unidentified|identified|unknown|unnamed|undisclosed|withheld|released|reported|available|"
    r"anonymous|victim|pedestrian)\b|^\(|\bno name\b|\bn/a\b", re.IGNORECASE)


def is_named(name):
    """True when name is a real first and last name, not empty or a placeholder."""
    return bool(split_name(name)[0]) and not PLACEHOLDER_NAME_RE.search(name)


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


def relationship_of(relative, texts):
    """(relationship, source, quote): SearchBug's relationshipType, else the articles' own words."""
    if relative["relationship"]:
        return relative["relationship"], "searchbug", None
    relationship, quote = relationship_from_articles(relative["names"], texts)
    return relationship, ("article" if relationship else None), quote


def closest_relatives(relatives, texts, limit):
    """The `limit` closest relatives (spouse, children, parents, siblings, others), closest first.
    Ranked before any lookup, so the paid lookups go to the closest family."""
    ranked = sorted(enumerate(relatives), key=lambda pair: (closeness(relationship_of(pair[1], texts)[0])[0], pair[0]))
    return [relative for _, relative in ranked[:limit]]


def relative_contact(lookups, relative, texts):
    """One relative: relationship, and current address and phones from a follow-up lookup by token."""
    relationship, source, quote = relationship_of(relative, texts)
    record = {
        "name": relative["names"][0] if relative["names"] else None,
        "relationship": relationship or "relative (relationship not stated)",
        "closeness": closeness(relationship)[1],
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
    record["phones"] = most_recent_phones(record["phones"], config.MAX_PHONES)
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
                # from lead verification, for the mailer notes
                "residence": person.get("residence"),
                "crash_location": lead.get("crash_location"),
                "role": person.get("role"),
                "at_fault": person.get("at_fault"),
                "fault_explanation": person.get("fault_explanation"),
            })
    return out


def article_texts(incident_id, passed):
    for lead in passed:
        if lead.get("incident_id") == incident_id:
            return [a.get("text") or "" for a in lead.get("articles") or []]
    return []


def aliases(names, reported_name):
    """Other names SearchBug has for the person (not the reported name itself)."""
    reported = " ".join((reported_name or "").lower().split())
    return [n for n in dict.fromkeys(names) if " ".join(n.lower().split()) != reported]


def other_locations(addresses, limit=3):
    """'Town, ST' of earlier addresses (not the current one), most recent first."""
    current = (addresses[0].get("city"), addresses[0].get("state")) if addresses else None
    places = []
    for address in addresses[1:]:
        place = (address.get("city"), address.get("state"))
        if address.get("city") and place != current and place not in places:
            places.append(place)
    return [", ".join(p for p in place if p) for place in places[:limit]]


def enrich(lookups, target, texts):
    """(short_entry, detail_entry) for one deceased person."""
    base = {key: target.get(key) for key in ("incident_id", "name", "age", "crash_date", "residence",
                                             "crash_location", "role", "at_fault", "fault_explanation")}
    if not is_named(target["name"]):
        return dict(base, match=NO_NAME, deceased=None, relatives=[]), dict(base, match=NO_NAME)

    status, chosen, candidates, made = find_deceased(lookups, target)
    detail = dict(base, match=status, searches=made, candidates=candidates)
    if status not in (MATCHED, POSSIBLE):
        return dict(base, match=status, deceased=None, relatives=[],
                    candidates=len(candidates) if status == AMBIGUOUS else 0), detail

    relatives = [relative_contact(lookups, r, texts)
                 for r in closest_relatives(chosen["relatives"], texts, config.MAX_RELATIVES)]
    current = chosen["addresses"][0] if chosen["addresses"] else {}
    deceased = {
        "dob": next((d["date"] for d in chosen["dobs"] if d["date"]), None),
        "date_of_death": next((d["date"] for d in chosen["dods"] if d["date"]), None),
        "last_address": current.get("address"),
        "street": current.get("street"),
        "town": current.get("city"),
        "county": current.get("county"),
        "state": current.get("state"),
        "zip": current.get("zip"),
        "other_locations": other_locations(chosen["addresses"]),
        "aliases": aliases(chosen["names"], target["name"]),
        "gender": chosen.get("gender"),
        "phones": most_recent_phones(chosen["phones"], config.MAX_PHONES),
        "searchbug_token": chosen["token"],
    }
    detail.update(chosen_record=chosen, relatives_found=len(chosen["relatives"]))
    # A possible match (name and age only) is filled in like a match, but keeps its status:
    # a person must confirm it is the right person before anyone is contacted.
    return dict(base, match=status, deceased=deceased, relatives_on_record=len(chosen["relatives"]),
                relatives=relatives), detail


# The team's mailer layout (one row per deceased person). Columns we have no source for yet
# (Mailing Date, Children, Homeowner, Married, Net Worth, Income Range, Death Location, Been
# Verified) stay empty for the team to fill. Our own columns follow, so every row can be traced.
MAILER_COLUMNS = [
    "#", "Mailing Date", "First Name", "Last Name", "Death Date", "Aliases", "Address", "Town", "County",
    "ST", "Zip", "Phone1", "Phone2", "Phone3", "Age", "Age Range", "Children", "Gender", "Homeowner",
    "Married", "Net Worth", "Income Range", "Residence", "Death Location", "Incident Location",
    "Other Locations", "Notes on accident and/or mailer recipient", "Been Verified",
]
NEXT_OF_KIN_FIELDS = ["Name", "Relationship", "Address", "Phones"]
EXTRA_COLUMNS = ["Match Status", "Incident ID", "DOB", "Relatives on Record"] + [
    f"Next of Kin {i} {field}" for i in range(1, config.MAX_RELATIVES + 1) for field in NEXT_OF_KIN_FIELDS
]
CSV_COLUMNS = MAILER_COLUMNS + EXTRA_COLUMNS
# People with no reported name: what is known about the crash, nothing to mail yet.
UNNAMED_COLUMNS = ["#", "Death Date", "Age", "Age Range", "Residence", "Incident Location",
                   "Notes on accident and/or mailer recipient", "Match Status", "Incident ID"]
CELL_SEPARATOR = " | "  # between several values in one cell
NAME_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v"}
ROLE_WORDS = {"driver": "driver", "passenger": "passenger", "pedestrian": "pedestrian", "cyclist": "cyclist",
              "motorcyclist": "motorcyclist", "other": "other", "unknown": "unknown"}


def _phones(phones):
    return CELL_SEPARATOR.join(f"{p['number']} ({p['type']})" if p.get("type") else p["number"] for p in phones or [])


def first_last(name):
    words = (name or "").split()
    while len(words) > 2 and words[-1].lower().strip(",") in NAME_SUFFIXES:
        words = words[:-1]
    return (words[0], words[-1]) if len(words) >= 2 else ((words[0], None) if words else (None, None))


def age_range(age):
    """10-year bands: 46 -> '40-49'."""
    if not isinstance(age, int):
        return None
    low = age // 10 * 10
    return f"{low}-{low + 9}"


def gender_letter(value):
    letter = (value or "").strip()[:1].upper()
    return letter if letter in ("M", "F") else None


def accident_notes(entry):
    """'Manner: Accident (MVA). Role: passenger. Not at fault (reason).' plus a warning for an
    unconfirmed identity."""
    fault = {"no": "Not at fault", "yes": "At fault"}.get(entry.get("at_fault"), "Fault not established")
    explanation = (entry.get("fault_explanation") or "").strip().rstrip(".")
    notes = f"Manner: Accident (MVA). Role: {ROLE_WORDS.get(entry.get('role'), 'unknown')}. {fault}"
    notes += f" ({explanation}.)." if explanation else "."
    if entry.get("match") == NO_NAME:
        notes += " Name not reported; not looked up."
    elif entry.get("match") == POSSIBLE:
        notes += " Identity not confirmed: SearchBug record matches name and age only; check before mailing."
    elif entry.get("match") == AMBIGUOUS:
        notes += " Several SearchBug records match; choose the right one before mailing."
    return notes


def csv_rows(entries):
    """One row per deceased person in the team's mailer layout, then match status, incident id,
    date of birth and the next of kin (closest first)."""
    rows = []
    for number, entry in enumerate(entries, start=1):
        deceased = entry.get("deceased") or {}
        phones = deceased.get("phones") or []
        first, last = first_last(entry.get("name"))
        row = {
            "#": number,
            "First Name": first,
            "Last Name": last,
            "Death Date": deceased.get("date_of_death") or entry.get("crash_date"),
            "Aliases": "; ".join(deceased.get("aliases") or []),
            "Address": deceased.get("street"),
            "Town": deceased.get("town"),
            "County": deceased.get("county"),
            "ST": deceased.get("state"),
            "Zip": deceased.get("zip"),
            "Age": entry.get("age"),
            "Age Range": age_range(entry.get("age")),
            "Gender": gender_letter(deceased.get("gender")),
            "Residence": entry.get("residence"),
            "Incident Location": entry.get("crash_location"),
            "Other Locations": "; ".join(deceased.get("other_locations") or []),
            "Notes on accident and/or mailer recipient": accident_notes(entry),
            "Match Status": entry.get("match"),
            "Incident ID": entry.get("incident_id"),
            "DOB": deceased.get("dob"),
            "Relatives on Record": entry.get("relatives_on_record", len(entry.get("relatives") or [])),
        }
        for i, phone in enumerate(phones[:3], start=1):
            row[f"Phone{i}"] = f"{phone['number']} ({phone['type']})" if phone.get("type") else phone["number"]
        for i, relative in enumerate((entry.get("relatives") or [])[:config.MAX_RELATIVES], start=1):
            row.update({
                f"Next of Kin {i} Name": relative.get("name"),
                f"Next of Kin {i} Relationship": relative.get("relationship"),
                f"Next of Kin {i} Address": relative.get("address"),
                f"Next of Kin {i} Phones": _phones(relative.get("phones")),
            })
        rows.append(row)
    return rows


def publish_to_sheet(entries):
    """Append the contacts that are not in the Google Sheet yet (when one is configured).
    A sheet problem is logged and never stops the run: the CSV is complete."""
    if not sheets.enabled():
        return
    named = [e for e in entries if e.get("match") != NO_NAME]
    try:
        answer = sheets.publish(config.GOOGLE_SHEET_TAB, CSV_COLUMNS, csv_rows(named))
        logger.info("Google Sheet: tab %s, %s new rows added, %s already there", config.GOOGLE_SHEET_TAB,
                    answer.get("added"), answer.get("skipped"))
    except Exception as error:
        logger.error("Google Sheet: could not save contacts: %s: %s. contacts.csv is complete; "
                     "import it by hand.", type(error).__name__, error)


def save(entries, details, errors):
    write_stage_json(config.MODULE, config.ERRORS_NAME, errors)
    named = [e for e in entries if e.get("match") != NO_NAME]
    unnamed = [e for e in entries if e.get("match") == NO_NAME]
    write_stage_csv(config.MODULE, config.CSV_NAME, CSV_COLUMNS, csv_rows(named))
    write_stage_csv(config.MODULE, config.UNNAMED_CSV_NAME, UNNAMED_COLUMNS, csv_rows(unnamed))
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
        publish_to_sheet([])
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
    publish_to_sheet(entries)
    if errors and len(errors) == len(people):
        logger.error("Every SearchBug lookup failed; details in %s", config.ERRORS_NAME)
        return False
    return True


if __name__ == "__main__":
    configure_logging()
    main()
