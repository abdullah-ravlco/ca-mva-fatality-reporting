import json
from types import SimpleNamespace

import pytest
import requests

import config.settings
import src.core.outputs
from src.modules.severity_screening import config as screening_config
from src.modules.severity_screening import pipeline as screening
from src.modules.severity_screening.decision import decide
from src.modules.severity_screening.jev_client import JevClient, JevError, parse_json_reply
from src.modules.severity_screening.prompt import build_user_message
from src.modules.severity_screening.schema import JevResult

TEXT = "Maria Lopez, 34, of Fresno died at the scene. Police said the pickup driver ran a red light and was arrested."


def facts(victims=None, involved="yes", conflicts=(), date="2026-09-12", location="Highway 99, Fresno"):
    victim = dict(name="Maria Lopez", age=34, role="pedestrian", outcome="killed", injury_tier="catastrophic",
                  at_fault_party="other_party", impaired="no")
    return JevResult.model_validate({
        "crash_date": date, "crash_location": location, "collision_type": "vehicle_struck_pedestrian_or_cyclist",
        "victims": victims if victims is not None else [victim],
        "other_party": {"involved": involved}, "conflicts": list(conflicts), "case_status": "qualified",
    })


def victim(**changes):
    base = dict(name="Maria Lopez", age=34, role="pedestrian", outcome="killed", injury_tier="catastrophic",
                at_fault_party="other_party", impaired="no",
                fault_quote="Police said the pickup driver ran a red light and was arrested.")
    base.update(changes)
    return base


# ------------------------------------------------------------------ decision rules

@pytest.mark.parametrize("changes,involved,expected,codes", [
    ({}, "yes", "qualified", None),
    ({"name": None}, "yes", "victim_identity_unidentified", None),
    ({"at_fault_party": "unknown"}, "yes", "fault_unidentified", None),
    ({"injury_tier": "unknown"}, "yes", "qualified", None),                                     # a death is catastrophic
    ({"outcome": "injured", "injury_tier": "catastrophic"}, "yes", "qualified", None),          # e.g. paralysis
    ({"role": "passenger", "impaired": "yes", "impaired_quote": "He was arrested on suspicion of DUI."}, "yes", "qualified", None),                       # passenger did not drive
    # ruled out by the evidence
    ({}, "no", "dropped", ["no_other_party"]),
    ({"outcome": "injured", "injury_tier": "serious"}, "yes", "dropped", ["injury_not_catastrophic"]),
    ({"role": "passenger", "at_fault_party": "own_driver"}, "yes", "dropped", ["own_driver_at_fault"]),  # husband drove
    ({"role": "driver", "at_fault_party": "this_victim"}, "yes", "dropped", ["victim_at_fault"]),
    ({"role": "driver", "impaired": "yes", "impaired_quote": "He was arrested on suspicion of DUI."}, "yes", "dropped", ["victim_impaired_driver"]),
    # not enough to qualify
    ({}, "unclear", "dropped", ["other_party_unclear"]),
    ({"outcome": "injured", "injury_tier": "unknown"}, "yes", "dropped", ["injury_not_reported"]),
    ({"age": None}, "yes", "dropped", ["age_missing"]),
    ({"name": None, "at_fault_party": "unknown"}, "yes", "dropped", ["name_and_fault_unknown"]),
    ({"outcome": "injured", "injury_tier": "unknown", "age": None}, "unclear", "dropped",
     ["age_missing", "injury_not_reported", "other_party_unclear"]),                           # every reason listed
])
def test_case_status_rules(changes, involved, expected, codes):
    result = decide(facts([victim(**changes)], involved))
    assert result["status"] == expected
    assert (result["drop_reason"] or {}).get("codes") == codes


def test_conflicts_and_missing_date_are_dropped_with_the_reason():
    conflict = decide(facts(conflicts=["articles give ages 34 and 43"]))
    assert conflict["drop_reason"]["codes"] == ["conflicting_reports"]
    assert "ages 34 and 43" in conflict["drop_reason"]["text"]
    assert decide(facts(date=None))["drop_reason"]["codes"] == ["crash_details_missing"]


def test_best_victim_decides_and_every_victim_is_kept():
    result = decide(facts([victim(role="driver", impaired="yes", impaired_quote="DUI suspected", name="Driver"), victim(role="passenger", name="Pass")]))
    assert result["status"] == "qualified" and result["drop_reason"] is None
    assert [v["status"] for v in result["per_victim"]] == ["dropped", "qualified"]


def test_no_victim_reported_is_dropped_and_mismatch_with_jev_is_flagged():
    result = decide(facts(victims=[]))
    assert result["drop_reason"]["codes"] == ["no_victim_reported"]
    assert result["status_mismatch"] is True  # JEV said qualified


# ------------------------------------------------------------------ reply validation

def test_reply_values_are_normalized():
    result = JevResult.model_validate({
        "collision_type": "Multi-Vehicle", "case_status": "Fault Unidentified",
        "victims": [{"name": "Unidentified", "age": "34 years", "outcome": "Killed", "role": "DRIVER",
                     "fault_basis": None}],
        "other_party": {"involved": "unknown"},
    })
    v = result.victims[0]
    assert (v.name, v.age, v.outcome, v.role, v.fault_basis) == (None, 34, "killed", "driver", "not_reported")
    assert result.collision_type == "multi_vehicle" and result.case_status == "fault_unidentified"
    assert result.other_party.involved == "unclear"


def test_json_is_found_inside_fences_or_text():
    assert parse_json_reply('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_reply('Here you go: {"a": {"b": 2}} done') == {"a": {"b": 2}}
    with pytest.raises(ValueError):
        parse_json_reply("no json here")


# ------------------------------------------------------------------ JEV client errors

def reply(status=200, body=None, text=None, headers=None):
    payload = body if body is not None else {"choices": [{"message": {"content": json.dumps({"case_status": "dropped"})}}]}
    return SimpleNamespace(status_code=status, headers=headers or {}, text=text if text is not None else json.dumps(payload),
                           json=lambda: payload)


class FakeSession:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "json": json})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client(*responses, retries=2):
    return JevClient("https://jev.example/v1/", "key", "jev-1", max_retries=retries,
                     session=FakeSession(*responses), sleep=lambda s: None)


def test_endpoint_and_request_format():
    c = client(reply())
    result, meta = c.screen("system", "user")
    call = c.session.calls[0]
    assert call["url"] == "https://jev.example/v1/chat/completions"
    assert call["json"]["model"] == "jev-1" and call["json"]["response_format"] == {"type": "json_object"}
    assert result.case_status == "dropped" and meta["attempts"] == 1


def test_rate_limit_and_server_errors_are_retried():
    c = client(reply(429, text="slow down", headers={"Retry-After": "3"}), reply(503, text="busy"), reply())
    _, meta = c.screen("s", "u")
    assert meta["attempts"] == 3


def test_auth_error_is_not_retried_and_has_a_hint():
    c = client(reply(401, text='{"error": "bad key"}', headers={"x-request-id": "req-9"}))
    with pytest.raises(JevError) as caught:
        c.screen("s", "u")
    error = caught.value
    assert (error.kind, error.status, error.attempts, error.request_id) == ("http", 401, 1, "req-9")
    assert "JEV_API_KEY" in error.hint and "bad key" in error.response_excerpt


def test_timeouts_give_up_after_the_retry_limit():
    c = client(requests.Timeout("t1"), requests.Timeout("t2"), requests.Timeout("t3"))
    with pytest.raises(JevError) as caught:
        c.screen("s", "u")
    assert caught.value.kind == "timeout" and caught.value.attempts == 3


def test_context_too_long_is_explained():
    c = client(reply(400, text="This model's maximum context length is 8192 tokens"))
    with pytest.raises(JevError) as caught:
        c.screen("s", "u")
    assert caught.value.kind == "context" and "JEV_MAX_INPUT_CHARS" in caught.value.hint


def test_json_mode_is_dropped_when_jev_does_not_support_it():
    c = client(reply(400, text="unknown parameter: response_format"), reply())
    c.screen("s", "u")
    assert "response_format" not in c.session.calls[1]["json"] and c.json_mode is False


@pytest.mark.parametrize("content,kind", [("not json at all", "invalid_json"), ('{"victims": [{"outcome": "exploded"}]}', "schema")])
def test_bad_replies_are_reported(content, kind):
    c = client(reply(body={"choices": [{"message": {"content": content}}]}))
    with pytest.raises(JevError) as caught:
        c.screen("s", "u")
    assert caught.value.kind == kind and caught.value.response_excerpt


def test_wrong_response_shape_is_reported():
    c = client(reply(body={"output": "something else"}))
    with pytest.raises(JevError) as caught:
        c.screen("s", "u")
    assert caught.value.kind == "bad_response"


def test_missing_configuration_names_the_settings(monkeypatch):
    monkeypatch.setattr(screening_config, "JEV_API_URL", "")
    monkeypatch.setattr(screening_config, "JEV_API_KEY", "")
    monkeypatch.setattr(screening_config, "JEV_MODEL", "jev")
    with pytest.raises(JevError) as caught:
        JevClient.from_env()
    assert "JEV_API_URL, JEV_API_KEY" in str(caught.value)


# ------------------------------------------------------------------ prompt input

def test_full_article_text_is_sent_unless_a_cap_is_set():
    long_text = "word " * 5000
    incident = {"incident_id": "inc-1", "articles": [{"title": "t", "url": "https://a.com", "text": long_text}]}
    assert long_text.strip() in build_user_message(incident, max_chars=0)
    assert len(build_user_message(incident, max_chars=2000)) < 3000


# ------------------------------------------------------------------ the stage

@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(config.settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(src.core.outputs, "DATA_DIR", tmp_path)
    monkeypatch.setenv("PIPELINE_RUN_ID", "test-run")
    incidents = tmp_path / "incidents.json"
    incidents.write_text(json.dumps({"incidents": [
        {"incident_id": f"inc-{i}", "articles": [{"url": f"https://a.com/{i}", "title": "Pedestrian killed", "text": TEXT}]}
        for i in range(3)
    ]}))
    monkeypatch.setattr(screening_config, "INCIDENTS_FILE", incidents)
    monkeypatch.setattr(screening_config, "OUTPUT_FILE", tmp_path / "severity_screening" / "screened_incidents.json")
    return tmp_path


class FakeJev:
    model = "fake-jev"

    def __init__(self, fail_on=()):
        self.fail_on, self.calls = set(fail_on), []

    def screen(self, system, user):
        self.calls.append(user)
        if any(f"Incident {i}:" in user for i in self.fail_on):
            raise JevError("http", "JEV request failed", 500, "boom", "req-1", 3, 4.2)
        result = facts([victim(injury_quote="Maria Lopez, 34, of Fresno died at the scene.", injury_source_url="https://a.com/0")])
        return result, {"attempts": 1, "elapsed_seconds": 0.5, "request_id": "r"}


def test_stage_screens_saves_errors_and_retries_only_failures(sandbox):
    jev = FakeJev(fail_on={"inc-1"})
    assert screening.main(jev) is True

    out = json.loads((sandbox / "severity_screening" / "screened_incidents.json").read_text())
    statuses = {r["incident_id"]: r["status"] for r in out["incidents"]}
    assert statuses == {"inc-0": "qualified", "inc-1": "screening_failed", "inc-2": "qualified"}
    first = next(r for r in out["incidents"] if r["incident_id"] == "inc-0")
    assert first["facts"]["victims"][0]["injury_quote_verified"] is True

    errors = json.loads((sandbox / "severity_screening" / "screening_errors.json").read_text())
    assert errors[0]["incident_id"] == "inc-1" and errors[0]["http_status"] == 500 and errors[0]["request_id"] == "req-1"

    # Next run: only the failed incident goes to JEV again.
    again = FakeJev()
    screening.main(again)
    assert len(again.calls) == 1 and "Incident inc-1:" in again.calls[0]


def test_passed_leads_file_holds_only_passed_incidents_with_full_content(sandbox):
    class Mixed(FakeJev):
        def screen(self, system, user):
            result, meta = super().screen(system, user)
            if "Incident inc-2:" in user:
                result.other_party.involved = "no"  # inc-2 is dropped
            return result, meta

    screening.main(Mixed())
    leads = json.loads((sandbox / "severity_screening" / "passed_leads.json").read_text())

    assert [lead["incident_id"] for lead in leads["leads"]] == ["inc-0", "inc-1"]
    first = leads["leads"][0]
    assert first["screening"]["status"] == "qualified" and first["screening"]["facts"]["victims"]
    assert first["articles"][0]["text"] == TEXT  # the full article content, not a summary


def test_lead_verification_reads_the_passed_leads_one_by_one(sandbox, monkeypatch):
    from src.modules.lead_verification import incidents as lead_incidents
    from src.modules.lead_verification.agent import build_input

    path = sandbox / "passed_leads.json"
    path.write_text(json.dumps({"leads": [
        {"incident_id": "a", "screening": {"status": "fault_unidentified", "facts": {"crash_date": "2026-09-12"}},
         "grouping": {}, "articles": [{"url": "https://a.com/1", "title": "t", "text": TEXT, "text_available": True}]},
    ]}))
    monkeypatch.setattr(lead_incidents, "LEADS_FILE", path)

    loaded = lead_incidents.load_incidents()
    assert [i["incident_key"] for i in loaded] == ["a"] and loaded[0]["articles"][0]["text"] == TEXT
    prompt = build_input(loaded[0], lead_incidents.existing_sources(loaded[0]))
    assert "fault_unidentified" in prompt and "search for it first" in prompt and "2026-09-12" in prompt


def test_dropped_cases_say_why():
    single = decide(facts([victim(role="driver", impaired="yes", impaired_quote="DUI suspected")]))
    assert single["drop_reason"] == {"codes": ["victim_impaired_driver"], "text": "the victim was an impaired driver"}

    two = decide(facts([victim(role="driver", at_fault_party="this_victim", name="Ann"),
                        victim(role="passenger", at_fault_party="own_driver", name=None)]))
    assert two["status"] == "dropped"
    assert two["drop_reason"]["codes"] == ["own_driver_at_fault", "victim_at_fault"]
    assert two["drop_reason"]["text"] == (
        "Ann: the victim caused the crash; "
        "unnamed victim: the driver of the vehicle this passenger rode in caused the crash"
    )

    assert decide(facts())["drop_reason"] is None


def test_fault_claims_need_a_quote_that_shows_them():
    medical = ("Police said the driver experienced a medical emergency, although the exact circumstances "
               "leading to the collision remain under investigation.")
    no_quote = decide(facts([victim(fault_quote=None)]))
    assert no_quote["status"] == "fault_unidentified" and no_quote["corrections"]
    uncertain = decide(facts([victim(fault_quote=medical)]))
    assert uncertain["status"] == "fault_unidentified"
    shown = decide(facts([victim(fault_quote="the driver was speeding when he lost control and hit West")]))
    assert shown["status"] == "qualified" and shown["corrections"] == []
    # an unquoted claim against the victim does not drop the case either
    assert decide(facts([victim(role="driver", at_fault_party="this_victim", fault_quote=None)]))["status"] != "dropped"
