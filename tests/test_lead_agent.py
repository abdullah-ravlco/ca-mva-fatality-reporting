import json
from datetime import date
from types import SimpleNamespace

import pytest

from src.modules.lead_verification import agent as agent_module
from src.modules.lead_verification.agent import check_quotes, run_agent
from src.modules.lead_verification.pipeline import incident_output
from src.modules.lead_verification.quotes import quote_in_text
from src.modules.lead_verification.qualification import assess
from src.modules.lead_verification.schema import AgentAssessment, Fact, IncidentFacts, LeadResult, Person

TODAY = date(2026, 10, 14)
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
    )
    facts.update(incident)
    return LeadResult(
        incident=IncidentFacts(**facts), people=people,
        assessment=AgentAssessment(verdict="qualifies", reasons=[], explanation="x"),
    )


# ---- qualification rules ----

def test_pedestrian_killed_by_another_driver_qualifies():
    assert assess(lead([victim()]), TODAY)["verdict"] == "qualifies"


def test_qualifies_with_open_items_while_fault_and_impairment_are_unknown():
    result = assess(lead([victim(at_fault="unknown", impaired="unknown")]), TODAY)
    assert result["verdict"] == "qualifies"
    assert result["open_items"] == ["fault not yet reported for Maria Lopez", "impairment not reported for Maria Lopez"]


def test_unknown_placeholders_become_empty_values():
    assert Fact(value="unknown").value is None and Fact(value=" Not stated ").value is None
    assert Fact(value="Fresno").value == "Fresno"


def test_single_vehicle_crash_does_not_qualify():
    result = assess(lead([victim(role="driver")], crash_type="single_vehicle", other_party_involved="no"), TODAY)
    assert result["verdict"] == "does_not_qualify" and "single_vehicle_no_other_party" in result["reasons"]


def test_impaired_deceased_driver_does_not_qualify():
    result = assess(lead([victim(role="driver", impaired="yes")]), TODAY)
    assert result["verdict"] == "does_not_qualify" and result["reasons"] == ["deceased_impaired"]


def test_deceased_at_fault_does_not_qualify():
    result = assess(lead([victim(at_fault="yes")]), TODAY)
    assert result["verdict"] == "does_not_qualify" and result["reasons"] == ["deceased_at_fault"]


def test_passenger_qualifies_even_when_the_deceased_driver_was_impaired():
    people = [victim(role="driver", impaired="yes", name=fact("Driver")), victim(role="passenger", name=fact("Pass"))]
    result = assess(lead(people), TODAY)
    assert result["verdict"] == "qualifies"
    assert [p["can_be_claimant"] for p in result["per_person"]] == [False, True]


def test_no_death_does_not_qualify_and_no_people_needs_review():
    assert assess(lead([victim(outcome="injured")]), TODAY)["reasons"] == ["no_death"]
    assert assess(lead([]), TODAY)["verdict"] == "needs_review"


def test_outside_california_and_old_crash_do_not_qualify():
    assert "outside_california" in assess(lead([victim()], location=fact("Interstate 35 in Austin, Texas")), TODAY)["reasons"]
    assert "outside_window" in assess(lead([victim()], date=fact("2026-08-01")), TODAY)["reasons"]


def test_missing_date_needs_review():
    result = assess(lead([victim()], date=Fact()), TODAY)
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
