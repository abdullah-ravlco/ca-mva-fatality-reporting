"""Turn the LLM's claims into verified fields.

For every claim, the quote must really appear in the cited source's text;
otherwise the claim is dropped. Then each field gets a status:
  confirmed      - an official source, or 2+ independent (non-law-firm) sites agree
  single_source  - only one site states it
  conflict       - sources disagree (all values are kept)
  missing        - no source states it (value stays null; nothing is guessed)
"""

import re
import unicodedata
from datetime import date
from difflib import SequenceMatcher

from src.modules.lead_verification.config import MAX_DAYS
from src.modules.lead_verification.incidents import name_tokens


# Fields where different values are a real disagreement.
STRICT_FIELDS = {"incident_date", "age", "outcome", "full_name", "role", "crash_type"}
FAULT_VALUES = ("at_fault", "not_at_fault", "not_established")
MIN_FUZZY_QUOTE_CHARS = 25
FUZZY_MATCH_RATIO = 0.9
DATE_TOLERANCE_DAYS = 1


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


def check_claims(claims, sources_by_id):
    """(valid claims with source details, number rejected)."""
    valid, rejected = [], 0
    for claim in claims or []:
        source = sources_by_id.get((claim.source or "").strip("[] ").upper())
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
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    if field == "age":
        match = re.search(r"\d{1,3}", value)
        return int(match.group(0)) if match and 0 < int(match.group(0)) <= 120 else None
    if field == "full_name":
        tokens = name_tokens(value)
        return (tokens[0], tokens[-1]) if tokens else None
    if field in ("outcome", "role", "fault", "crash_type"):
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


def fault_result(claims, sources_by_id):
    """at_fault wins over not_at_fault wins over not_established; disagreement is a conflict."""
    valid, rejected = check_claims(claims, sources_by_id)
    valid = [c for c in valid if value_key("fault", c["value"]) in FAULT_VALUES]
    result = {"value": None, "status": "missing", "evidence": [], "rejected_claims": rejected}
    if not valid:
        return result

    by_value = {}
    for claim in valid:
        by_value.setdefault(value_key("fault", claim["value"]), []).append(claim)
    for value in FAULT_VALUES:
        if value in by_value:
            claims_for_value = by_value[value]
            break

    result.update(value=value, evidence=_evidence(claims_for_value), status=status_for(claims_for_value))
    if value == "at_fault" and "not_at_fault" in by_value:
        result["status"] = "conflict"
        result["alternatives"] = [{"value": "not_at_fault", "evidence": _evidence(by_value["not_at_fault"])}]
    return result


def passenger_fault(fault, role):
    """A passenger was not driving, so cannot have caused the crash by driving.

    Applied only when the sources (quote-checked) show the person was a
    passenger and no source says this person was at fault.
    """
    if role["value"] != "passenger" or role["status"] == "conflict" or fault["value"] == "at_fault":
        return fault
    return {
        "value": "not_at_fault",
        "status": role["status"],
        "basis": "passenger: was not driving the vehicle",
        "evidence": role["evidence"] + fault.get("evidence", []),
        "rejected_claims": fault.get("rejected_claims", 0),
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

def match_person_evidence(person, evidence):
    tokens = name_tokens(person["person_name"])
    for item in evidence.people:
        other = name_tokens(item.shortlist_name or "")
        if other and tokens and other[0] == tokens[0] and other[-1] == tokens[-1]:
            return item
    for item in evidence.people:
        for claim in item.full_name:
            other = name_tokens(claim.value or "")
            if other and tokens and other[0] == tokens[0] and other[-1] == tokens[-1]:
                return item
    return None


def lead_status(person, fields, today):
    """(verification_status, reasons)."""
    reasons = []

    fault = fields["fault"]
    if fault["value"] == "at_fault" and fault["status"] != "conflict":
        return "rejected", ["Sources say this person caused the crash."]

    outcome = fields["outcome"]
    if outcome["value"] == "minor_injury" and outcome["status"] != "conflict":
        return "rejected", ["Sources describe only minor injuries."]

    incident_date = fields["incident_date"]
    if incident_date["value"] and incident_date["status"] != "conflict":
        age_days = (today - date.fromisoformat(incident_date["value"])).days
        if age_days > MAX_DAYS:
            return "rejected", [f"Crash was {age_days} days ago (more than {MAX_DAYS})."]

    for field in ("full_name", "incident_date", "location"):
        status = fields[field]["status"]
        if status == "missing":
            reasons.append(f"{field}: not found in any source")
        elif status != "confirmed":
            reasons.append(f"{field}: {status}")

    if fault["value"] != "not_at_fault":
        reasons.append(
            "fault: conflicting reports" if fault["status"] == "conflict"
            else "fault: sources do not say who caused the crash" if fault["value"] in (None, "not_established")
            else f"fault: {fault['value']}"
        )
    elif fault["status"] != "confirmed":
        reasons.append("fault: not_at_fault from a single source")

    for field in ("age", "residence"):
        if fields[field]["status"] == "missing":
            reasons.append(f"{field}: not found in any source")

    blocking = [r for r in reasons if not r.startswith(("age:", "residence:", "fault: not_at_fault from"))]
    return ("verified" if not blocking else "partially_verified"), reasons


FAULT_SUMMARY = {"at_fault": "yes", "not_at_fault": "no", "not_established": "not_established"}


def verify_incident(incident, evidence, sources, today=None):
    """One verified-lead dict per shortlisted person in the incident."""
    today = today or date.today()
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
    description = evidence.description if description_urls else None

    leads = []
    for person in incident["people"]:
        person_evidence = match_person_evidence(person, evidence)
        get = (lambda name: getattr(person_evidence, name)) if person_evidence else (lambda name: [])

        fields = dict(shared)
        fields["full_name"] = field_result("full_name", get("full_name"), sources_by_id)
        fields["age"] = field_result("age", get("age"), sources_by_id)
        fields["residence"] = field_result("residence", get("residence"), sources_by_id)
        fields["outcome"] = field_result("outcome", get("outcome"), sources_by_id)
        fields["role"] = field_result("role", get("role"), sources_by_id)
        fields["driver_of_vehicle"] = field_result("driver_of_vehicle", get("driver_of_vehicle"), sources_by_id)
        fields["fault"] = passenger_fault(fault_result(get("fault"), sources_by_id), fields["role"])

        status, reasons = lead_status(person, fields, today)
        leads.append({
            "verification_status": status,
            "status_reasons": reasons,
            "summary": {
                "name": fields["full_name"]["value"] or person["person_name"],
                "age": fields["age"]["value"],
                "residence": fields["residence"]["value"],
                "outcome": fields["outcome"]["value"],
                "role": fields["role"]["value"],
                "incident_date": fields["incident_date"]["value"],
                "incident_time": fields["incident_time"]["value"],
                "location": fields["location"]["value"],
                "at_fault": FAULT_SUMMARY.get(fields["fault"]["value"]),
                "at_fault_basis": fields["fault"].get("basis"),
                "crash_type": fields["crash_type"]["value"],
                "driver_of_vehicle": (
                    fields["driver_of_vehicle"]["value"] if fields["role"]["value"] == "passenger" else None
                ),
                "other_party": other_party_summary(fields["crash_type"], fields["other_party"]),
                "description": description,
            },
            "field_status": {name: field["status"] for name, field in fields.items()},
            "evidence": fields,
            "description_sources": description_urls,
        })
    return leads
