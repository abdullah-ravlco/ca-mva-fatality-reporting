import json
from types import SimpleNamespace

import pytest

from src.modules.lead_verification import agent as agent_module
from src.modules.lead_verification.agent import check_quotes, run_agent
from src.modules.lead_verification.pipeline import incident_output
from src.modules.lead_verification.quotes import quote_in_text
from src.modules.lead_verification.qualification import assess
from src.modules.lead_verification.schema import AgentAssessment, Fact, IncidentFacts, LeadResult, Person

TEXT = "The driver who struck him was arrested on suspicion of DUI, the CHP said Sunday. Maria Lopez, 34, of Fresno died."


def fact(value, url="https://a.com/1", quote=None):
    return Fact(value=value, source_url=url, quote=quote or value)


def victim(**kw):
    base = dict(name=fact("Maria Lopez"), outcome="killed", role="pedestrian", at_fault="no", impaired="no")
    base.update(kw)
    return Person(**base)


def lead(people, **incident):
    facts = dict(
        date=fact("2026-10-12"), location=fact("Highway 99 in Fresno"), city="Fresno",
        crash_type="pedestrian_or_cyclist_struck", other_party_involved="yes",
        other_party_at_fault="yes", other_party_fault_basis="other_party_cited_or_arrested",
        other_party_fault_quote="The driver who struck him was arrested on suspicion of DUI", other_party_fault_source_url="https://a.com/1",
    )
    facts.update(incident)
    return LeadResult(
        incident=IncidentFacts(**facts), people=people,
        assessment=AgentAssessment(verdict="qualifies", reasons=[], explanation="x"),
    )


# ---- qualification rules ----

def test_pedestrian_killed_by_another_driver_qualifies():
    assert assess(lead([victim()]))["verdict"] == "qualifies"


def test_qualifies_with_open_items_while_fault_and_impairment_are_unknown():
    result = assess(lead([victim(at_fault="unknown", impaired="unknown")]))
    assert result["verdict"] == "qualifies"
    # the other party's fault is established, so only the victim's impairment is still open
    assert result["open_items"] == ["impairment not reported for Maria Lopez"]
    unknown_cause = assess(lead([victim(at_fault="unknown")], other_party_at_fault="unknown"))
    assert unknown_cause["verdict"] == "fault_unidentified" and "fault not yet reported for Maria Lopez" in unknown_cause["open_items"]


def test_unknown_placeholders_become_empty_values():
    assert Fact(value="unknown").value is None and Fact(value=" Not stated ").value is None
    assert Fact(value="Fresno").value == "Fresno"


def test_single_vehicle_crash_does_not_qualify():
    result = assess(lead([victim(role="driver")], crash_type="single_vehicle", other_party_involved="no"))
    assert result["verdict"] == "does_not_qualify" and "single_vehicle_no_other_party" in result["reasons"]


def test_impaired_deceased_driver_does_not_qualify():
    result = assess(lead([victim(role="driver", impaired="yes")]))
    assert result["verdict"] == "does_not_qualify" and result["reasons"] == ["deceased_impaired"]


def test_deceased_at_fault_does_not_qualify():
    result = assess(lead([victim(at_fault="yes")]))
    assert result["verdict"] == "does_not_qualify" and result["reasons"] == ["deceased_at_fault"]


def test_passenger_qualifies_even_when_the_deceased_driver_was_impaired():
    people = [victim(role="driver", impaired="yes", name=fact("Driver")), victim(role="passenger", name=fact("Pass"))]
    result = assess(lead(people))
    assert result["verdict"] == "qualifies"
    assert [p["can_be_claimant"] for p in result["per_person"]] == [False, True]


def test_no_death_does_not_qualify_and_no_people_needs_review():
    assert assess(lead([victim(outcome="injured")]))["reasons"] == ["no_death"]
    assert assess(lead([]))["verdict"] == "needs_review"


def test_outside_california_does_not_qualify_but_old_crash_is_not_cut_off():
    assert "outside_california" in assess(lead([victim()], location=fact("Interstate 35 in Austin, Texas")))["reasons"]
    assert assess(lead([victim()], date=fact("2026-08-01")))["verdict"] == "qualifies"  # no date cutoff


def test_missing_date_needs_review():
    result = assess(lead([victim()], date=Fact()))
    assert result["verdict"] == "needs_review" and "crash date" in result["missing"]


# ---- quotes ----

def test_quote_check_flags_quotes_missing_from_the_cited_page():
    assert quote_in_text("arrested on suspicion of DUI", TEXT)
    data = {"incident": {"date": {"value": "x", "source_url": "https://a.com/1", "quote": "arrested on suspicion of DUI"}},
            "people": [{"fault_source_url": "https://a.com/1", "fault_quote": "made up sentence about the crash"}]}
    check_quotes(data, {"https://a.com/1": TEXT})
    assert data["incident"]["date"]["quote_verified"] is True
    assert data["people"][0]["fault_quote_verified"] is False


# ---- agent loop with a fake model ----

class FakeResponses:
    def __init__(self, steps):
        self.steps, self.calls = list(steps), []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return self.steps.pop(0)


def response(output=(), parsed=None, rid="r"):
    return SimpleNamespace(id=rid, output=list(output), output_parsed=parsed)


def function_call(url):
    return SimpleNamespace(type="function_call", name="fetch_page", call_id="c1", arguments=json.dumps({"url": url}))


class FakeFetcher:
    def fetch(self, url):
        return {"ok": True, "url": url, "text": TEXT}


INCIDENT = {"incident_key": "inc-1", "grouping": {}}
SOURCES = [{"url": "https://a.com/1", "title": "t", "publisher": "p", "published_at": None, "text": TEXT}]


def test_agent_searches_fetches_then_returns_structured_result():
    search = SimpleNamespace(type="web_search_call", action=SimpleNamespace(query="Maria Lopez Fresno crash"))
    final = lead([victim()])
    client = SimpleNamespace(responses=FakeResponses([
        response([search, function_call("https://news.example.com/story")], rid="r1"),
        response([SimpleNamespace(type="message")], parsed=final, rid="r2"),
    ]))

    result, store, log = run_agent(client, "m", INCIDENT, SOURCES, FakeFetcher())

    assert result is final
    assert [entry["tool"] for entry in log] == ["web_search", "fetch_page"]
    assert "https://news.example.com/story" in store
    second = client.responses.calls[1]
    assert second["previous_response_id"] == "r1"
    assert second["input"][0]["type"] == "function_call_output"
    assert client.responses.calls[0]["tool_choice"] == "auto"


def test_agent_blocks_disallowed_sites_and_forces_an_answer_on_the_last_step(monkeypatch):
    monkeypatch.setattr(agent_module, "MAX_AGENT_STEPS", 2)
    client = SimpleNamespace(responses=FakeResponses([
        response([function_call("https://www.spokeo.com/Maria-Lopez")], rid="r1"),
        response(parsed=lead([victim()]), rid="r2"),
    ]))

    _, store, log = run_agent(client, "m", INCIDENT, SOURCES, FakeFetcher())

    assert log[0]["result"] == "blocked_domain" and "https://www.spokeo.com/Maria-Lopez" not in store
    assert client.responses.calls[1]["tool_choice"] == "none"


def test_agent_that_never_answers_raises(monkeypatch):
    monkeypatch.setattr(agent_module, "MAX_AGENT_STEPS", 1)
    client = SimpleNamespace(responses=FakeResponses([response([function_call("https://a.com/x")])]))
    with pytest.raises(ValueError):
        run_agent(client, "m", INCIDENT, SOURCES, FakeFetcher())


def test_incident_output_has_verdict_people_and_verified_quotes():
    incident = {"incident_key": "inc-1", "grouping": {}, "articles": [{"article_id": "a", "url": "https://a.com/1", "text_available": True, "text": TEXT}]}
    record = incident_output(incident, "analysed", lead([victim(fault_source_url="https://a.com/1", fault_quote="arrested on suspicion of DUI")]),
                             {"https://a.com/1": TEXT, }, [{"tool": "web_search"}])
    assert record["qualification"]["verdict"] == "qualifies"
    assert record["people"][0]["name"]["value"] == "Maria Lopez"
    assert record["people"][0]["fault_quote_verified"] is True
    assert record["agent_log"] == [{"tool": "web_search"}]


def test_short_output_has_only_the_required_fields():
    from src.modules.lead_verification.pipeline import compact

    incident = {"incident_key": "inc-1", "grouping": {"locations": ["lots of grouping detail"]},
                "articles": [{"article_id": "a", "url": "https://a.com/1", "text_available": True, "text": TEXT}]}
    people = [victim(age=fact("34"), address=fact("Fresno", quote="Maria Lopez, 34, of Fresno died.")),
              Person(outcome="injured", role="driver")]
    record = incident_output(incident, "analysed", lead(people), {"https://a.com/1": TEXT}, [{"tool": "web_search"}])
    record["pages_used"] = ["https://a.com/1"]

    short = compact(record)

    assert set(short) == {"incident_id", "verdict", "reason", "crash_date", "crash_time", "crash_location",
                          "other_party", "other_party_at_fault", "deceased", "missing", "corrections", "sources"}
    assert short["verdict"] == "qualifies" and short["crash_date"] == "2026-10-12"
    assert short["deceased"] == [{"name": "Maria Lopez", "age": 34, "residence": "Fresno", "role": "pedestrian",
                                  "at_fault": "no", "fault_basis": "none", "fault_explanation": None,
                                  "impaired": "no"}]  # the injured driver is left out
    assert short["sources"] == ["https://a.com/1"]


def test_a_lead_that_does_not_qualify_gives_its_reasons():
    from src.modules.lead_verification.pipeline import compact

    incident = {"incident_key": "inc-2", "grouping": {}, "articles": []}
    record = incident_output(incident, "analysed", lead([victim(role="driver", impaired="yes", impaired_quote="DUI")]), {}, [])
    assert compact(record)["reason"] == "deceased_impaired"


def test_reason_says_when_the_rules_overrode_the_model():
    from src.modules.lead_verification.pipeline import compact

    result = lead([victim()])
    result.assessment.verdict = "needs_review"
    record = incident_output({"incident_key": "inc-3", "grouping": {}, "articles": []}, "analysed", result, {}, [])
    assert compact(record)["reason"].startswith("qualifies by the rules (the model said needs_review): ")


# ---- fault_unidentified and the checks on the model's claims ----

from src.modules.lead_verification.checks import apply_checks  # noqa: E402

MODESTO = "Police said the driver experienced a medical emergency, although the exact circumstances leading to the collision remain under investigation."


def test_other_party_fault_not_established_is_fault_unidentified():
    result = lead([victim()], other_party_at_fault="unknown", other_party_fault_quote=None)
    assert assess(result)["verdict"] == "fault_unidentified"


def test_medical_emergency_or_investigation_is_not_proof_of_fault():
    result = lead([victim(role="other", fault_basis="other_party_cited_or_arrested", fault_quote=MODESTO)],
                  other_party_fault_basis="circumstances_described", other_party_fault_quote=MODESTO)
    notes = apply_checks(result)
    assert result.incident.other_party_at_fault == "unknown"
    assert result.people[0].fault_basis == "circumstances_described"  # nobody was cited or arrested
    assert any("uncertain" in n for n in notes)
    assert assess(result)["verdict"] == "fault_unidentified"


def test_a_vehicle_crash_is_never_not_a_road_crash():
    result = lead([victim()])  # a car struck her (pedestrian_or_cyclist_struck, other party involved)
    result.assessment.reasons = ["not_a_road_crash"]
    result.assessment.verdict = "does_not_qualify"
    notes = apply_checks(result)
    assert "not_a_road_crash" not in result.assessment.reasons and notes
    assert assess(result)["verdict"] != "does_not_qualify" or "not_a_road_crash" not in assess(result)["reasons"]


def test_a_real_non_crash_death_keeps_its_reason():
    result = lead([victim()], crash_type="unknown", other_party_involved="no")
    result.assessment.reasons = ["not_a_road_crash"]
    assert apply_checks(result) == [] and result.assessment.reasons == ["not_a_road_crash"]


def test_residence_needs_a_quote_saying_where_they_lived():
    crash_place = victim(address=fact("Whittier", quote="The crash happened at Slauson Avenue and Norwalk Boulevard in Whittier."))
    lived = victim(address=fact("Fresno", quote="Maria Lopez, 34, of Fresno died."))
    result = lead([crash_place, lived])
    apply_checks(result)
    assert result.people[0].address.value is None and result.people[1].address.value == "Fresno"


def test_unquoted_blame_on_the_victim_is_removed():
    result = lead([victim(role="driver", at_fault="yes", impaired="yes")])
    apply_checks(result)
    assert (result.people[0].at_fault, result.people[0].impaired) == ("unknown", "unknown")


def test_old_records_are_re_decided_without_the_model():
    from src.modules.lead_verification.pipeline import result_from_record

    whittier_quote = "the driver was speeding through the intersection to make the light before it changed while turning left when he lost control and hit West"
    record = {
        "incident": {"date": {"value": "2026-10-12"}, "location": {"value": "Slauson Avenue and Norwalk Boulevard, Whittier"},
                     "crash_type": "multi_vehicle", "other_party_involved": "yes"},
        "people": [{"name": {"value": "Zenia Chantal West"}, "outcome": "killed", "role": "pedestrian", "at_fault": "no",
                    "fault_basis": "circumstances_described", "fault_quote": whittier_quote, "fault_source_url": "https://a"}],
        "qualification": {"agent_verdict": "does_not_qualify", "reasons": ["not_a_road_crash"]},
    }
    result = result_from_record(record)
    apply_checks(result)
    assert result.incident.other_party_at_fault == "yes"           # taken from the deceased's fault quote
    assert assess(result)["verdict"] == "qualifies"
