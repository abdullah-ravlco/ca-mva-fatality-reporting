import csv
import json
from types import SimpleNamespace

import pytest
import requests

import config.settings
import src.core.outputs
from src.modules.contact_enrichment import config as ce_config
from src.modules.contact_enrichment import pipeline as ce
from src.modules.contact_enrichment.client import SearchBugClient, SearchBugError
from src.modules.contact_enrichment.match import AMBIGUOUS, MATCHED, NOT_FOUND, choose, relationship_from_articles
from src.modules.contact_enrichment.parse import find_people


def person(first="Maria", last="Lopez", dob_year=1992, city="FRESNO", token="tok-maria", dod=None, relatives=()):
    record = {
        "reportToken": token,
        "names": {"name": [{"firstName": first, "lastName": last}]},
        "DOBs": {"DOB": {"year": dob_year, "month": 3, "day": 4, "age": 2026 - dob_year}},
        "addresses": {"address": [
            {"houseNumber": "12", "streetName": "Elm", "streetSuffix": "St", "city": city, "state": "CA",
             "zip": "93701", "lastDate": {"year": 2026, "month": 8}},
            {"line1": "9 Old Rd", "city": "MADERA", "state": "CA", "zip": "93637", "lastDate": {"year": 2019}},
        ]},
        "phones": {"phone": [{"phoneNumber": "(559) 555-0101", "phoneType": "Wireless"}]},
        "relatives": {"relative": list(relatives)},
    }
    if dod:
        record["DODs"] = {"deceasedRecord": {"DOD": dod, "ageAtDeath": 34}}
    return record


def relative(first, last, token, relationship=None):
    return {"reportToken": token, "relationshipType": relationship,
            "names": {"name": [{"firstName": first, "lastName": last}]}}


TARGET = {"incident_id": "inc-1", "name": "Maria Lopez", "age": 34, "crash_date": "2026-09-12", "cities": ["Fresno"]}


# ------------------------------------------------------------------ reading responses

def test_documented_fields_are_read():
    [p] = find_people({"people": {"person": [person(relatives=[relative("John", "Lopez", "tok-john", "Spouse")])]}})
    assert p["token"] == "tok-maria" and p["names"] == ["Maria Lopez"]
    assert p["dobs"] == [{"date": "1992-03-04", "age": 34}]
    assert p["addresses"][0]["address"] == "12 Elm St, FRESNO, CA 93701"      # most recent first
    assert p["phones"] == [{"number": "5595550101", "type": "Wireless"}]
    assert p["relatives"][0]["names"] == ["John Lopez"] and p["relatives"][0]["relationship"] == "Spouse"


def test_relatives_are_not_returned_as_separate_people_and_single_objects_work():
    one = person(relatives=[relative("John", "Lopez", "tok-john")])
    one["names"] = {"name": {"firstName": "Maria", "lastName": "Lopez"}}           # one object, not a list
    assert [p["token"] for p in find_people({"results": one})] == ["tok-maria"]


# ------------------------------------------------------------------ matching

def test_one_matching_record_is_chosen():
    status, chosen, _ = choose(TARGET, find_people([person(), person(first="Mario", token="tok-x")]))
    assert status == MATCHED and chosen["token"] == "tok-maria"


def test_wrong_age_is_not_a_match_but_a_death_record_is():
    older = find_people([person(dob_year=1950)])
    assert choose(TARGET, older)[0] == NOT_FOUND
    with_death = find_people([person(dob_year=1950, dod={"year": 2026, "month": 9, "day": 12})])
    assert choose(TARGET, with_death)[0] == MATCHED


def test_two_matching_records_are_ambiguous_never_picked():
    status, chosen, _ = choose(TARGET, find_people([person(token="a"), person(token="b")]))  # both strong
    assert status == AMBIGUOUS and chosen is None
    # the one that also lived in a reported city wins over one that only shares name and age
    status, chosen, _ = choose(TARGET, find_people([person(token="a"), person(token="b", city="CLOVIS")]))
    assert status == MATCHED and chosen["token"] == "a"


def test_relationship_only_from_the_articles_own_words():
    text = "Maria Lopez, 34, died at the scene. She is survived by her husband, John Lopez, and two children."
    assert relationship_from_articles(["John Lopez"], [text]) == (
        "husband", "She is survived by her husband, John Lopez, and two children.")
    assert relationship_from_articles(["Ann Smith"], [text]) == (None, None)
    # a relationship word elsewhere in the sentence does not count
    assert relationship_from_articles(["John Lopez"], ["Her son spoke. John Lopez, a neighbor, called 911."]) == (None, None)


# ------------------------------------------------------------------ client errors

def reply(status=200, body=None, text=None):
    return SimpleNamespace(status_code=status, text=text if text is not None else json.dumps(body or {}),
                           json=lambda: body if body is not None else json.loads(text))


class Session:
    def __init__(self, *responses):
        self.responses, self.forms = list(responses), []

    def post(self, url, data=None, timeout=None):
        self.forms.append(data)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client(*responses):
    return SearchBugClient("https://data.searchbug.com/api/search.aspx", "12345", "secret", max_retries=2,
                           session=Session(*responses), sleep=lambda s: None)


def test_request_form_and_retries():
    c = client(reply(503, text="busy"), requests.Timeout(), reply(body={"people": []}))
    assert c.search({"FNAME": "Maria", "LNAME": "Lopez", "STATE": "CA"}) == {"people": []}
    form = c.session.forms[0]
    assert (form["CO_CODE"], form["TYPE"], form["FORMAT"], form["FNAME"]) == ("12345", "api_ppl", "JSON", "Maria")


def test_token_search_sends_no_other_criteria():
    c = client(reply(body={}))
    c.search({"reportToken": "tok-john"})
    assert "LIMIT" not in c.session.forms[0] and "FNAME" not in c.session.forms[0]


@pytest.mark.parametrize("body,kind", [
    ({"Error": "Invalid CO_CODE or password"}, "auth"),
    ({"Error": "Insufficient balance"}, "balance"),
    ({"Error": "Something else broke"}, "api_error"),
])
def test_errors_inside_a_200_response_are_reported(body, kind):
    with pytest.raises(SearchBugError) as caught:
        client(reply(body=body)).search({"FNAME": "a", "LNAME": "b"})
    assert caught.value.kind == kind and "secret" not in json.dumps(caught.value.to_dict())


def test_no_records_found_is_an_empty_answer_not_an_error():
    assert client(reply(body={"Error": "No records found"})).search({"FNAME": "a", "LNAME": "b"}) == {}


def test_missing_settings_are_named(monkeypatch):
    monkeypatch.setattr(ce_config, "SEARCHBUG_CO_CODE", "")
    monkeypatch.setattr(ce_config, "SEARCHBUG_API_KEY", "")
    with pytest.raises(SearchBugError) as caught:
        SearchBugClient.from_env()
    assert "SEARCHBUG_CO_CODE, SEARCHBUG_API_KEY" in str(caught.value)


# ------------------------------------------------------------------ the stage

@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(config.settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(src.core.outputs, "DATA_DIR", tmp_path)
    monkeypatch.setenv("PIPELINE_RUN_ID", "test-run")
    leads = tmp_path / "verified_leads.json"
    leads.write_text(json.dumps({"leads": [
        {"incident_id": "inc-1", "verdict": "qualifies", "crash_date": "2026-09-12", "crash_location": "Highway 99, Fresno",
         "deceased": [{"name": "Maria Lopez", "age": 34, "residence": "Fresno"}], "sources": ["https://a.com/1"]},
        {"incident_id": "inc-2", "verdict": "does_not_qualify", "deceased": [{"name": "Not Looked Up", "age": 50}]},
        {"incident_id": "inc-3", "verdict": "qualifies", "crash_date": "2026-09-20", "crash_location": "Oakland",
         "deceased": [{"name": "Sam Doe", "age": 40}]},
    ]}))
    passed = tmp_path / "passed_leads.json"
    passed.write_text(json.dumps({"leads": [{"incident_id": "inc-1", "articles": [
        {"text": "She is survived by her husband, John Lopez, and her mother, Rosa Diaz."}]}]}))
    monkeypatch.setattr(ce_config, "LEADS_FILE", leads)
    monkeypatch.setattr(ce_config, "PASSED_LEADS_FILE", passed)
    monkeypatch.setattr(ce_config, "CACHE_FILE", tmp_path / "state" / "searchbug_cache.json")
    return tmp_path


class FakeSearchBug:
    def __init__(self):
        self.calls = []

    def search(self, fields):
        self.calls.append(fields)
        if fields.get("reportToken") == "tok-john":
            return {"person": {"reportToken": "tok-john", "names": {"name": {"firstName": "John", "lastName": "Lopez"}},
                               "addresses": {"address": {"line1": "12 Elm St", "city": "FRESNO", "state": "CA"}},
                               "phones": {"phone": {"phoneNumber": "5595550199", "phoneType": "Landline"}}}}
        if fields.get("reportToken"):
            return {}
        if fields.get("LNAME") == "Lopez":
            return {"people": [person(relatives=[relative("John", "Lopez", "tok-john"), relative("Rosa", "Diaz", "tok-rosa")])]}
        return {}


def test_stage_writes_json_and_csv_and_reuses_the_cache(sandbox):
    fake = FakeSearchBug()
    assert ce.main(fake) is True

    out = json.loads((sandbox / "contact_enrichment" / "contacts.json").read_text())
    by_name = {p["name"]: p for p in out["people"]}
    assert set(by_name) == {"Maria Lopez", "Sam Doe"}                    # does_not_qualify is not looked up
    maria = by_name["Maria Lopez"]
    assert maria["match"] == "matched" and maria["deceased"]["dob"] == "1992-03-04"
    john, rosa = maria["relatives"]
    assert (john["name"], john["relationship"], john["relationship_source"]) == ("John Lopez", "husband", "article")
    assert john["phones"] == [{"number": "5595550199", "type": "Landline"}] and john["lookup"] == "found"
    assert rosa["relationship"] == "mother"
    assert by_name["Sam Doe"]["match"] == "not_found"

    rows = list(csv.DictReader((sandbox / "contact_enrichment" / "contacts.csv").open(encoding="utf-8-sig")))
    assert [r["relative_name"] for r in rows] == ["John Lopez", "Rosa Diaz", ""]   # one row per relative
    assert rows[0]["deceased_phones"] == "5595550101 (Wireless)" and rows[0]["relationship"] == "husband"
    assert rows[2]["deceased_name"] == "Sam Doe" and rows[2]["match_status"] == "not_found"

    paid = len(fake.calls)
    again = FakeSearchBug()
    ce.main(again)
    assert again.calls == []  # everything answered from the cache: nothing paid twice
    assert paid > 0


def test_lookup_limit_stops_paying_and_says_so(sandbox, monkeypatch):
    monkeypatch.setattr(ce_config, "MAX_LOOKUPS_PER_RUN", 1)
    ce.main(FakeSearchBug())
    out = json.loads((sandbox / "contact_enrichment" / "contacts.json").read_text())
    maria = next(p for p in out["people"] if p["name"] == "Maria Lopez")
    assert maria["match"] == "matched" and {r["lookup"] for r in maria["relatives"]} == {"skipped_lookup_limit"}
    assert next(p for p in out["people"] if p["name"] == "Sam Doe")["match"] == "skipped_lookup_limit"


# ------------------------------------------------------------------ live response format

LIVE = {"rows": "2", "people": {"person": [{
    "reportToken": "tok-k", "deceased": "No", "deceasedRecords": None,
    "names": {"name": [{"firstName": "KATHLEEN", "middleName": "A", "lastName": "ERICKSON"}]},
    "DOBs": {"DOB": "03/20/1959"},
    "addresses": {"address": [{"fullStreet": "100 MAIN ST", "apt": None, "city": "MODESTO", "state": "CA",
                               "zip": "95350", "plusFour": "1703", "lastDate": "06/18/2026"}]},
    "phones": {"phone": [{"phoneNumber": "2095550100", "phoneType": "Mobile",
                          "lastDate": {"day": "18", "month": "06", "year": "2026"}}]},
    "relationships": {"relationship": [{"relationshipType": "Sibling", "name": {"firstName": "ANNA", "lastName": "ERICKSON"},
                                        "DOB": "01/02/1962", "currentAge": 64, "reportToken": "tok-a"}]},
}]}}


def test_live_response_format_is_read():
    [p] = find_people(LIVE)
    assert p["dobs"] == [{"date": "1959-03-20", "age": None}] and p["deceased_flag"] is False
    assert p["addresses"][0]["address"] == "100 MAIN ST, MODESTO, CA 95350"
    assert p["relatives"][0]["names"] == ["ANNA ERICKSON"] and p["relatives"][0]["relationship"] == "Sibling"
    assert p["relatives"][0]["token"] == "tok-a" and p["relatives"][0]["age"] == 64
    target = {"name": "Kathleen Erickson", "age": 67, "crash_date": "2026-10-08", "cities": ["Modesto"]}
    assert choose(target, [p])[0] == MATCHED


def test_name_and_age_alone_is_only_a_possible_match():
    from src.modules.contact_enrichment.match import POSSIBLE
    [p] = find_people(LIVE)
    elsewhere = {"name": "Kathleen Erickson", "age": 67, "crash_date": "2026-10-08", "cities": ["Whittier"]}
    assert choose(elsewhere, [p])[0] == POSSIBLE


def test_too_many_results_tries_the_next_search_and_is_not_a_failure(sandbox):
    class TooBroad(FakeSearchBug):
        def search(self, fields):
            self.calls.append(fields)
            raise SearchBugError("too_many_results", "SearchBug: ERR P001: Too Many Results Found")

    assert ce.main(TooBroad()) is True
    out = json.loads((sandbox / "contact_enrichment" / "contacts.json").read_text())
    assert {p["match"] for p in out["people"]} == {"too_many_results"}
    assert json.loads((sandbox / "contact_enrichment" / "searchbug_errors.json").read_text()) == []
    again = TooBroad()
    ce.main(again)
    assert again.calls == []  # the too-broad searches are remembered, not paid for again


def test_p001_from_the_api_is_recognized():
    with pytest.raises(SearchBugError) as caught:
        client(reply(body={"error": "ERR P001: Too Many Results Found"})).search({"FNAME": "a", "LNAME": "b"})
    assert caught.value.kind == "too_many_results"
