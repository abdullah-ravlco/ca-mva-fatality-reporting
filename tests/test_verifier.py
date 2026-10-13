from datetime import date

from src.modules.lead_verification.evidence_llm import IncidentEvidence
from src.modules.lead_verification.verifier import (
    EXPLICITLY_NOT_AT_FAULT,
    OFFICIALLY_RESPONSIBLE,
    RESPONSIBILITY_ALLEGED,
    UNDETERMINED,
    field_result,
    quote_in_text,
    verify_incident,
)

TODAY = date(2026, 10, 12)

S1_TEXT = (
    "A 25-year-old motorcyclist was killed Saturday, Oct. 10, 2026, on Highway 99 in Fresno. "
    "The driver who struck him was arrested on suspicion of DUI, the CHP said. "
    "A passenger in the pickup, 31, suffered minor injuries."
)
S2_TEXT = (
    "Officers said the motorcyclist, 25, died at the scene on Highway 99 in Fresno on Oct. 10. "
    "Police said the pickup driver was not at fault for the first impact."
)
S3_TEXT = "The motorcyclist was 27 years old, the coroner said. He died Oct. 10 on Highway 99."

SOURCES = [
    {"id": "S1", "url": "https://news-one.example/a", "text": S1_TEXT, "host": "news-one.example", "official": False, "law_firm": False},
    {"id": "S2", "url": "https://news-two.example/b", "text": S2_TEXT, "host": "news-two.example", "official": False, "law_firm": False},
    {"id": "S3", "url": "https://coroner.example.gov/c", "text": S3_TEXT, "host": "coroner.example.gov", "official": True, "law_firm": False},
]
BY_ID = {s["id"]: s for s in SOURCES}


def claim(value, source, quote):
    return {"value": value, "source": source, "quote": quote}


def evidence(people, incident_date="2026-10-10"):
    return IncidentEvidence.model_validate({
        "incident_date": [claim(incident_date, "S1", "killed Saturday, Oct. 10, 2026")],
        "location": [
            claim("Highway 99 in Fresno", "S1", "on Highway 99 in Fresno"),
            claim("Highway 99 in Fresno", "S2", "on Highway 99 in Fresno"),
        ],
        "people": people,
    })


MOTORCYCLIST = {
    "person_ref": "P1",
    "descriptor": "unnamed 25-year-old motorcyclist",
    "outcome": [claim("killed", "S1", "motorcyclist was killed")],
    "role": [claim("motorcyclist", "S1", "motorcyclist was killed")],
    "fault_statements": [],
}


def analyse(people, incident_date="2026-10-10"):
    return verify_incident({"articles": []}, evidence(people, incident_date), SOURCES, TODAY)


def person(result, ref):
    return next(p for p in result["people"] if p["person_ref"] == ref)


# ---------------------------------------------------------------- quotes and fields

def test_quote_in_text_exact_and_normalized():
    assert quote_in_text("The driver who struck him was ARRESTED on suspicion of DUI", S1_TEXT)
    assert quote_in_text("driver who struck him -- was arrested, on suspicion of DUI", S1_TEXT)


def test_quote_in_text_fuzzy_and_rejected():
    # A long quote may differ slightly at an edge (>= 90% contiguous match)...
    assert quote_in_text("The driver who struck him was arrested on suspicion of DUI, the CHP said Sunday", S1_TEXT)
    # ...but not in the middle, and an invented quote is rejected.
    assert not quote_in_text("The driver who struck him was released without charges, the CHP said", S1_TEXT)
    assert not quote_in_text("The motorcyclist ran a red light before the crash", S1_TEXT)


def test_field_result_confirmed_single_and_conflict():
    from src.modules.lead_verification.evidence_llm import Claim

    two_sites = [Claim(**claim("Highway 99", "S1", "on Highway 99 in Fresno")),
                 Claim(**claim("Highway 99", "S2", "on Highway 99 in Fresno"))]
    assert field_result("location", two_sites, BY_ID)["status"] == "confirmed"

    one_site = [Claim(**claim("25", "S2", "the motorcyclist, 25, died"))]
    assert field_result("age", one_site, BY_ID)["status"] == "single_source"

    ages = one_site + [Claim(**claim("27", "S3", "was 27 years old"))]
    result = field_result("age", ages, BY_ID)
    assert result["status"] == "conflict"
    assert result["value"] == 27  # the official source ranks first
    assert result["alternatives"][0]["value"] == 25

    invented = [Claim(**claim("35", "S1", "the rider was 35"))]
    assert field_result("age", invented, BY_ID)["status"] == "missing"


# ---------------------------------------------------------------- fault rules

def test_victim_struck_by_arrested_driver_is_undetermined():
    driver = {
        "person_ref": "P2",
        "descriptor": "pickup driver",
        "role": [claim("driver", "S1", "The driver who struck him")],
        "fault_statements": [{
            "value": "arrested on suspicion of DUI",
            "source": "S1",
            "quote": "The driver who struck him was arrested on suspicion of DUI, the CHP said",
            "statement_type": "citation_or_arrest",
            "attributed_to": "CHP",
            "indicates": "responsibility",
        }],
    }
    result = analyse([MOTORCYCLIST, driver])

    assert person(result, "P1")["fault"]["status"] == UNDETERMINED
    # An arrest is an allegation, never an official finding.
    assert person(result, "P2")["fault"]["status"] == RESPONSIBILITY_ALLEGED
    assert person(result, "P2")["fault"]["status"] != OFFICIALLY_RESPONSIBLE


def test_passenger_without_statement_is_undetermined():
    passenger = {
        "person_ref": "P2",
        "descriptor": "31-year-old pickup passenger",
        "outcome": [claim("minor_injury", "S1", "suffered minor injuries")],
        "role": [claim("passenger", "S1", "A passenger in the pickup")],
    }
    result = analyse([MOTORCYCLIST, passenger])
    assert person(result, "P2")["fault"]["status"] == UNDETERMINED


def test_explicit_not_at_fault_statement():
    driver = {
        "person_ref": "P2",
        "descriptor": "pickup driver",
        "fault_statements": [{
            "value": "not at fault",
            "source": "S2",
            "quote": "Police said the pickup driver was not at fault for the first impact",
            "statement_type": "attributed_to_authorities",
            "attributed_to": "police",
            "indicates": "no_responsibility",
        }],
    }
    result = analyse([MOTORCYCLIST, driver])
    assert person(result, "P2")["fault"]["status"] == EXPLICITLY_NOT_AT_FAULT


def test_unquoted_fault_statement_is_rejected():
    victim = dict(MOTORCYCLIST, fault_statements=[{
        "value": "not at fault", "source": "S1", "quote": "the motorcyclist was not at fault",
        "statement_type": "attributed_to_authorities", "indicates": "no_responsibility",
    }])
    fault = person(analyse([victim]), "P1")["fault"]
    assert fault["status"] == UNDETERMINED
    assert fault["rejected_statements"] == 1


# ---------------------------------------------------------------- people and scope

def test_unnamed_person_killed_is_kept():
    result = analyse([MOTORCYCLIST])
    victim = person(result, "P1")

    assert victim["name_status"] == "unnamed"
    assert victim["display_name"] == "unnamed 25-year-old motorcyclist"
    assert result["scope_checks"]["fatality_established"] is True
    assert result["summary"]["people_killed"] == 1
    assert result["in_scope"] is True


def test_out_of_window_date_is_out_of_scope_but_kept():
    victim = dict(MOTORCYCLIST)
    result = verify_incident(
        {"articles": []},
        IncidentEvidence.model_validate({
            "incident_date": [claim("2026-01-01", "S1", "killed Saturday, Oct. 10, 2026")],
            "people": [victim],
        }),
        SOURCES,
        TODAY,
    )
    assert result["in_scope"] is False
    assert result["scope_checks"]["in_window"] is False
    assert result["people"]  # still reported


def test_same_named_entries_are_merged():
    named = [
        dict(MOTORCYCLIST, person_ref="P1", full_name=[claim("Juan Perez", "S1", "motorcyclist was killed")]),
    ]
    # Names must be quoted; build a source that names him in two places.
    sources = SOURCES + [{
        "id": "S4", "url": "https://news-three.example/d", "host": "news-three.example", "official": False,
        "law_firm": False, "text": "Juan Perez, 25, of Fresno was identified as the rider. Juan A. Perez loved motorcycles.",
    }]
    named = [
        dict(MOTORCYCLIST, person_ref="P1", full_name=[claim("Juan Perez", "S4", "Juan Perez, 25, of Fresno")]),
        {"person_ref": "P2", "descriptor": "rider", "full_name": [claim("Juan A. Perez", "S4", "Juan A. Perez loved")]},
    ]
    result = verify_incident({"articles": []}, evidence(named), sources, TODAY)

    assert len(result["people"]) == 1
    assert result["people"][0]["merge_note"]
