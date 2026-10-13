"""Turn the LLM's claims into verified fields.

For every claim, the quote must really appear in the cited source's text;
otherwise the claim is dropped. Then each field gets a status:
  confirmed      - an official source, or 2+ independent (non-law-firm) sites agree
  single_source  - only one site states it
  conflict       - sources disagree (all values are kept)
  missing        - no source states it (value stays null; nothing is guessed)

Fault is reported as what the sources say, by statement type, and is never
inferred: being struck, being a passenger, or another driver's arrest does not
make a person "not at fault". Fault never affects an incident's evidence status
or whether it is in scope.
"""

import math
import re
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher

from src.core.date_utils import PACIFIC
from src.modules.lead_verification.evidence_llm import PersonEvidence
from src.modules.lead_verification.incidents import name_tokens
from src.modules.source_collection.location import CALIFORNIA_CONFIRMED, OUTSIDE_CALIFORNIA, detect_location
from src.modules.source_collection.sources import LOOKBACK_HOURS


# Fields where different values are a real disagreement.
STRICT_FIELDS = {"incident_date", "age", "outcome", "full_name", "role", "crash_type"}
MIN_FUZZY_QUOTE_CHARS = 25
FUZZY_MATCH_RATIO = 0.9
DATE_TOLERANCE_DAYS = 1

# fault summary statuses
EXPLICITLY_NOT_AT_FAULT = "explicitly_reported_not_at_fault"
OFFICIALLY_RESPONSIBLE = "officially_found_responsible"
RESPONSIBILITY_ALLEGED = "responsibility_alleged"
UNDER_INVESTIGATION = "under_investigation"
CONFLICTING = "conflicting"
UNDETERMINED = "undetermined"


# --------------------------------------------------
# Quote checks
# --------------------------------------------------

def _normalize(text):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def quote_in_text(quote, text):
    """True when the quote (ignoring case, punctuation and spacing) is in the text."""
    q, t = _normalize(quote), _normalize(text)
    if len(q) < 3:
        return False
    if q in t:
        return True
    if len(q) < MIN_FUZZY_QUOTE_CHARS:
        return False
    match = SequenceMatcher(None, q, t, autojunk=False).find_longest_match(0, len(q), 0, len(t))
    return match.size >= FUZZY_MATCH_RATIO * len(q)


def _source_for(claim, sources_by_id):
    return sources_by_id.get((claim.source or "").strip("[] ").upper())


def check_claims(claims, sources_by_id):
    """(valid claims with source details, number rejected)."""
    valid, rejected = [], 0
    for claim in claims or []:
        source = _source_for(claim, sources_by_id)
        if not claim.value or not claim.quote or source is None or not quote_in_text(claim.quote, source["text"]):
            rejected += 1
            continue
        valid.append({
            "value": claim.value,
            "quote": claim.quote,
            "source_id": source["id"],
            "source_url": source["url"],
            "host": source["host"],
            "official": source["official"],
            "law_firm": source["law_firm"],
        })
    return valid, rejected


# --------------------------------------------------
# Values
# --------------------------------------------------

def value_key(field, value):
    """Comparable form of a value, or None when the value is malformed."""
    value = (value or "").strip()
    if field == "incident_date":
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    if field == "age":
        match = re.search(r"\d{1,3}", value)
        return int(match.group(0)) if match and 0 < int(match.group(0)) <= 120 else None
    if field == "full_name":
        tokens = name_tokens(value)
        return (tokens[0], tokens[-1]) if tokens else None
    if field in ("outcome", "role", "crash_type"):
        return value.lower().replace(" ", "_")
    return _normalize(value) or None


def support(claims):
    """(has official source, number of independent non-law-firm sites)."""
    official = any(c["official"] for c in claims)
    hosts = {c["host"] for c in claims if not c["law_firm"]}
    return official, len(hosts)


def status_for(claims):
    official, independent = support(claims)
    return "confirmed" if official or independent >= 2 else "single_source"


def _evidence(claims):
    return [
        {"value": c["value"], "quote": c["quote"], "source_url": c["source_url"], "official": c["official"]}
        for c in claims
    ]


def _display_value(field, claims, key):
    if field == "incident_date":
        return key.isoformat()
    if field == "age":
        return key
    best = max(claims, key=lambda c: (c["official"], not c["law_firm"], len(c["value"])))
    return best["value"]


def field_result(field, claims, sources_by_id):
    valid, rejected = check_claims(claims, sources_by_id)
    for claim in valid:
        claim["key"] = value_key(field, claim["value"])
    malformed = sum(1 for c in valid if c["key"] is None)
    valid = [c for c in valid if c["key"] is not None]

    result = {"value": None, "status": "missing", "evidence": [], "rejected_claims": rejected + malformed}
    if not valid:
        return result

    if field not in STRICT_FIELDS:
        # Descriptive fields (location, residence, time) differ in wording, not in fact.
        result.update(value=_display_value(field, valid, None), status=status_for(valid), evidence=_evidence(valid))
        return result

    clusters = {}
    for claim in valid:
        clusters.setdefault(claim["key"], []).append(claim)
    ranked = sorted(clusters.items(), key=lambda kv: (*support(kv[1]), len(kv[1])), reverse=True)
    best_key, best_claims = ranked[0]

    alternatives = []
    for key, members in ranked[1:]:
        if field == "incident_date" and abs((key - best_key).days) <= DATE_TOLERANCE_DAYS:
            best_claims = best_claims + members  # late-night crashes are reported a day apart
            continue
        alternatives.append({"value": _display_value(field, members, key), "evidence": _evidence(members)})

    result.update(
        value=_display_value(field, best_claims, best_key),
        status="conflict" if alternatives else status_for(best_claims),
        evidence=_evidence(best_claims),
    )
    if alternatives:
        result["alternatives"] = alternatives
    return result


# --------------------------------------------------
# Fault: what the sources say, never inferred
# --------------------------------------------------

def check_statements(statements, sources_by_id):
    """(quote-checked fault statements, number rejected)."""
    valid, rejected = [], 0
    for statement in statements or []:
        source = _source_for(statement, sources_by_id)
        if not statement.quote or source is None or not quote_in_text(statement.quote, source["text"]):
            rejected += 1
            continue
        valid.append({
            "statement": statement.value or statement.quote,
            "quote": statement.quote,
            "statement_type": statement.statement_type,
            "attributed_to": statement.attributed_to,
            "indicates": statement.indicates,
            "source_url": source["url"],
            "official": source["official"],
        })
    return valid, rejected


def fault_summary(statements, sources_by_id):
    """Summary of what the sources say about one person's responsibility.

    explicitly_reported_not_at_fault - a source says so about this person, and no
                                       source says the opposite
    officially_found_responsible     - an official finding of responsibility
    responsibility_alleged           - responsibility attributed to authorities,
                                       alleged, or an arrest/citation of this person
    conflicting                      - sources point both ways
    under_investigation              - sources say the cause is under investigation
    undetermined                     - nothing stated (the default)
    """
    valid, rejected = check_statements(statements, sources_by_id)
    responsibility = [s for s in valid if s["indicates"] == "responsibility"]
    no_responsibility = [s for s in valid if s["indicates"] == "no_responsibility"]
    citations = [s for s in valid if s["statement_type"] == "citation_or_arrest"]

    if (responsibility or citations) and no_responsibility:
        status = CONFLICTING
    elif no_responsibility:
        status = EXPLICITLY_NOT_AT_FAULT
    elif any(s["statement_type"] == "official_finding" for s in responsibility):
        status = OFFICIALLY_RESPONSIBLE
    elif responsibility or citations:
        status = RESPONSIBILITY_ALLEGED
    elif any(s["statement_type"] == "under_investigation" for s in valid):
        status = UNDER_INVESTIGATION
    else:
        status = UNDETERMINED

    return {
        "status": status,
        "statements": valid,
        "rejected_statements": rejected,
        "note": "Reported statements only; not a legal determination of fault.",
    }


def other_party_summary(crash_type, other_party):
    if other_party["value"]:
        return other_party["value"]
    if crash_type["value"] == "single_vehicle" and crash_type["status"] != "conflict":
        return "none (single-vehicle crash; no other vehicle involved)"
    return None


# --------------------------------------------------
# People
# --------------------------------------------------

PERSON_CLAIM_FIELDS = ("full_name", "age", "residence", "outcome", "role", "driver_of_vehicle", "fault_statements")


def merge_same_named(people, sources_by_id):
    """Merge LLM entries whose verified names are the same person (first + last name).

    Returns [(PersonEvidence, merge_note)].
    """
    groups, order = {}, []
    for index, person in enumerate(people):
        name = field_result("full_name", person.full_name, sources_by_id)
        key = value_key("full_name", name["value"]) if name["value"] else ("unnamed", index)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(person)

    merged = []
    for key in order:
        members = groups[key]
        if len(members) == 1:
            merged.append((members[0], None))
            continue
        combined = {field: [c for m in members for c in getattr(m, field)] for field in PERSON_CLAIM_FIELDS}
        person = PersonEvidence(
            person_ref=members[0].person_ref,
            descriptor=members[0].descriptor,
            **{field: [c.model_dump() for c in claims] for field, claims in combined.items()},
        )
        refs = ", ".join(m.person_ref or "?" for m in members)
        merged.append((person, f"entries {refs} name the same person and were merged"))
    return merged


def person_record(person, merge_note, sources_by_id):
    fields = {
        field: field_result(field, getattr(person, field), sources_by_id)
        for field in ("full_name", "age", "residence", "outcome", "role", "driver_of_vehicle")
    }
    fault = fault_summary(person.fault_statements, sources_by_id)
    named = fields["full_name"]["value"] is not None
    return {
        "person_ref": person.person_ref,
        "display_name": fields["full_name"]["value"] or person.descriptor or "unnamed person",
        "name_status": "named" if named else "unnamed",
        "descriptor": person.descriptor,
        "summary": {
            "name": fields["full_name"]["value"],
            "age": fields["age"]["value"],
            "residence": fields["residence"]["value"],
            "outcome": fields["outcome"]["value"],
            "role": fields["role"]["value"],
            "driver_of_vehicle": fields["driver_of_vehicle"]["value"] if fields["role"]["value"] == "passenger" else None,
            "fault_status": fault["status"],
        },
        "field_status": {name: field["status"] for name, field in fields.items()},
        "evidence": fields,
        "fault": fault,
        "merge_note": merge_note,
    }


# --------------------------------------------------
# Incident scope and evidence status (independent of fault)
# --------------------------------------------------

def _today():
    return datetime.now(PACIFIC).date()


def in_window(incident_date, today, hours=LOOKBACK_HOURS):
    """(True/False/None, reason) for the verified crash date."""
    if incident_date["value"] is None:
        return None, "crash date not established"
    if incident_date["status"] == "conflict":
        return None, "sources give conflicting crash dates"
    days = (today - value_key("incident_date", incident_date["value"])).days
    window_days = math.ceil(hours / 24)
    if days < 0:
        return False, f"crash date {incident_date['value']} is after today"
    if days > window_days:
        return False, f"crash was {days} days ago (window: {window_days} days)"
    return True, None


def in_california(location, incident):
    """(True/False/None, reason) from the verified location, then the articles' own checks."""
    if location["value"]:
        status = detect_location(location["value"])["location_status"]
        if status == CALIFORNIA_CONFIRMED:
            return True, None
        if status == OUTSIDE_CALIFORNIA:
            return False, f"location is outside California: {location['value']}"
    articles = incident.get("articles") or []
    if any(a.get("location_status") == CALIFORNIA_CONFIRMED for a in articles):
        return True, None
    return None, "California location not confirmed"


def fatality_established(people):
    """(True/False/None, reason): does any quote-checked source report a death?"""
    killed = [p for p in people if p["summary"]["outcome"] == "killed"]
    if killed:
        return True, None
    if any(p["field_status"]["outcome"] != "missing" for p in people):
        return False, "no source reports that anyone died"
    return None, "outcome of the people involved not stated"


def evidence_status(shared, people):
    killed = [p for p in people if p["summary"]["outcome"] == "killed"]
    statuses = [shared["incident_date"]["status"], shared["location"]["status"]]
    statuses += [p["field_status"]["outcome"] for p in killed]
    if "conflict" in statuses:
        return "conflicting"
    if not killed or "missing" in statuses:
        return "insufficient"
    if all(status == "confirmed" for status in statuses):
        return "corroborated"
    return "single_source"


def verify_incident(incident, evidence, sources, today=None):
    """One analysed incident: shared fields, every person, scope and evidence status."""
    today = today or _today()
    sources_by_id = {source["id"]: source for source in sources}

    shared = {
        "incident_date": field_result("incident_date", evidence.incident_date, sources_by_id),
        "incident_time": field_result("incident_time", evidence.incident_time, sources_by_id),
        "location": field_result("location", evidence.location, sources_by_id),
        "crash_type": field_result("crash_type", evidence.crash_type, sources_by_id),
        "other_party": field_result("other_party", evidence.other_party, sources_by_id),
    }
    description_urls = [
        sources_by_id[s.strip("[] ").upper()]["url"]
        for s in evidence.description_sources
        if s.strip("[] ").upper() in sources_by_id
    ]

    people = [person_record(p, note, sources_by_id) for p, note in merge_same_named(evidence.people, sources_by_id)]

    checks = {
        "in_window": in_window(shared["incident_date"], today),
        "in_california": in_california(shared["location"], incident),
        "fatality_established": fatality_established(people),
    }
    scope_reasons = [reason for value, reason in checks.values() if value is False]
    open_questions = [reason for value, reason in checks.values() if value is None]

    return {
        "in_scope": not scope_reasons,
        "scope_reasons": scope_reasons,
        "scope_checks": {name: value for name, (value, _) in checks.items()},
        "evidence_status": evidence_status(shared, people),
        "open_questions": open_questions,
        "summary": {
            "incident_date": shared["incident_date"]["value"],
            "incident_time": shared["incident_time"]["value"],
            "location": shared["location"]["value"],
            "crash_type": shared["crash_type"]["value"],
            "other_party": other_party_summary(shared["crash_type"], shared["other_party"]),
            "people_killed": sum(1 for p in people if p["summary"]["outcome"] == "killed"),
            "people_listed": len(people),
            "description": evidence.description if description_urls else None,
        },
        "field_status": {name: field["status"] for name, field in shared.items()},
        "evidence": shared,
        "description_sources": description_urls,
        "people": people,
    }
