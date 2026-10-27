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
    assert p["phones"] == [{"number": "5595550101", "type": "Wireless", "last_seen": None}]
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
    assert john["phones"] == [{"number": "5595550199", "type": "Landline", "last_seen": None}] and john["lookup"] == "found"
    assert rosa["relationship"] == "mother"
    assert by_name["Sam Doe"]["match"] == "not_found"

    rows = list(csv.DictReader((sandbox / "contact_enrichment" / "contacts.csv").open(encoding="utf-8-sig")))
    assert [(r["First Name"], r["Last Name"]) for r in rows] == [("Maria", "Lopez"), ("Sam", "Doe")]  # one row each
    assert list(rows[0]) == [
        "Sr.#", "Case Id", "First Name", "Last Name", "Crash Date", "Address", "Town", "County", "ST", "Zip",
        "Phone1", "Phone2", "Age", "Incident Location", "Match Status",
        "Next of Kin 1 Name", "Next of Kin 1 Relationship", "Next of Kin 1 Address", "Next of Kin 1 Phones",
        "Next of Kin 2 Name", "Next of Kin 2 Relationship", "Next of Kin 2 Address", "Next of Kin 2 Phones"]
    maria_row = rows[0]
    assert maria_row["Sr.#"] == "1" and maria_row["Crash Date"] == "2026-09-12" and maria_row["Match Status"] == "matched"
    assert (maria_row["Address"], maria_row["Town"], maria_row["ST"], maria_row["Zip"]) == ("12 Elm St", "FRESNO", "CA", "93701")
    assert maria_row["Phone1"] == "5595550101 (Wireless)" and maria_row["Phone2"] == ""
    assert maria_row["Incident Location"] == "Highway 99, Fresno"
    assert (maria_row["Next of Kin 1 Name"], maria_row["Next of Kin 1 Relationship"]) == ("John Lopez", "husband")
    assert (maria_row["Next of Kin 2 Name"], maria_row["Next of Kin 2 Relationship"]) == ("Rosa Diaz", "mother")
    assert maria_row["Next of Kin 1 Phones"] == "5595550199 (Landline)"
    assert rows[1]["Match Status"] == "not_found" and rows[1]["Next of Kin 1 Name"] == "" and rows[1]["Address"] == ""

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


def test_relatives_are_ordered_spouse_children_parents_siblings_others():
    from src.modules.contact_enrichment.match import closeness
    from src.modules.contact_enrichment.pipeline import closest_relatives

    def rel(name, relationship):
        return {"names": [name], "relationship": relationship, "token": None, "addresses": [], "phones": []}

    found = [rel("Ann Lee", "In-Law"), rel("Bob Lopez", "Sibling"), rel("Carl Lopez", None), rel("Dora Lopez", "Parent"),
             rel("Eli Lopez", "Spouse"), rel("Fay Lopez", "Child")]
    ordered = closest_relatives(found, ["She is survived by her son, Carl Lopez."], limit=5)
    # spouse, the two children (Carl is a son per the article), parent, sibling; the in-law is cut off
    assert [r["names"][0] for r in ordered] == ["Eli Lopez", "Carl Lopez", "Fay Lopez", "Dora Lopez", "Bob Lopez"]
    assert closeness("Wife") == (0, "spouse") and closeness("Child") == (1, "child")
    assert closeness("brothers") == (3, "sibling") and closeness(None)[1] == "other"
    # by default only the 2 closest are kept: spouse, then the first child
    assert [r["names"][0] for r in closest_relatives(found, [], limit=2)] == ["Eli Lopez", "Fay Lopez"]


def test_only_the_two_most_recent_phones_are_kept():
    from src.modules.contact_enrichment.parse import most_recent_phones, phones

    found = phones({"phones": {"phone": [
        {"phoneNumber": "5550000001", "lastDate": {"day": "01", "month": "01", "year": "2019"}},
        {"phoneNumber": "5550000002", "lastDate": {"day": "18", "month": "06", "year": "2026"}},
        {"phoneNumber": "5550000003"},                                                     # no date: last
        {"phoneNumber": "5550000004", "lastDate": "03/02/2024"},
    ]}})
    assert [p["number"] for p in most_recent_phones(found, 2)] == ["5550000002", "5550000004"]


def test_possible_match_is_filled_in_but_keeps_its_status(sandbox):
    class Elsewhere(FakeSearchBug):
        def search(self, fields):
            self.calls.append(fields)
            if fields.get("LNAME") == "Lopez" and "CITY" not in fields:
                return {"people": [person(city="LONG BEACH", relatives=[relative("John", "Lopez", "tok-john", "Spouse")])]}
            if fields.get("reportToken") == "tok-john":
                return FakeSearchBug().search(fields)
            return {}

    ce.main(Elsewhere())
    rows = list(csv.DictReader((sandbox / "contact_enrichment" / "contacts.csv").open(encoding="utf-8-sig")))
    maria = next(r for r in rows if r["Last Name"] == "Lopez")
    assert maria["Match Status"] == "possible_match" and maria["Next of Kin 1 Name"] == "John Lopez"
    assert maria["Next of Kin 1 Phones"] == "5595550199 (Landline)"


def test_several_values_in_one_cell_are_separated_by_a_pipe():
    from src.modules.contact_enrichment.pipeline import _phones
    assert _phones([{"number": "5550000001", "type": "Mobile"}, {"number": "5550000002"}]) == "5550000001 (Mobile) | 5550000002"


def test_mailer_helpers():
    from src.modules.contact_enrichment.pipeline import accident_notes, age_range, first_last

    assert first_last("Lucian Michael Piane III") == ("Lucian", "Piane")
    assert age_range(46) == "40-49" and age_range(None) is None
    notes = accident_notes({"role": "passenger", "at_fault": "no", "match": "matched",
                            "fault_explanation": "No public source assigns fault; the rollover cause remained under investigation."})
    assert notes == ("Manner: Accident (MVA). Role: passenger. Not at fault (No public source assigns fault; "
                     "the rollover cause remained under investigation.).")


@pytest.mark.parametrize("name", [None, "", "Maria", "Not yet identified", "Unidentified woman", "unknown",
                                  "(named in articles)", "Name not released"])
def test_a_missing_or_placeholder_name_is_not_a_name(name):
    assert not ce.is_named(name)


@pytest.mark.parametrize("name", ["Maria Lopez", "Sam Doe", "Mary Ann O'Neil", "Juan Carlos de la Cruz Jr."])
def test_real_names_are_names(name):
    assert ce.is_named(name)


def test_unnamed_people_are_not_looked_up_and_go_to_their_own_csv(sandbox):
    entry, _ = ce.enrich(FakeSearchBug(), dict(TARGET, name="Not yet identified"), [])
    assert entry["match"] == "no_name" and entry["relatives"] == []

    fake = FakeSearchBug()
    ce.save([ce.enrich(fake, TARGET, [])[0], entry], [], [])
    assert fake.calls                                                    # Maria was searched, the unnamed one was not
    folder = sandbox / "contact_enrichment"
    named = list(csv.DictReader((folder / "contacts.csv").open(encoding="utf-8-sig")))
    unnamed = list(csv.DictReader((folder / "unnamed_people.csv").open(encoding="utf-8-sig")))
    assert [r["First Name"] for r in named] == ["Maria"]
    assert [r["#"] for r in unnamed] == ["1"] and unnamed[0]["Match Status"] == "no_name"
    assert "Name not reported" in unnamed[0]["Notes on accident and/or mailer recipient"]


# ------------------------------------------------------------------ Google Sheet

class FakeResponse:
    def __init__(self, body=None, status=200, text=""):
        self.body, self.status_code, self.text = body, status, text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        if self.body is None:
            raise ValueError("not json")
        return self.body


@pytest.fixture
def script(monkeypatch):
    from src.modules.contact_enrichment import sheets
    monkeypatch.setattr(ce_config, "GOOGLE_SCRIPT_URL", "https://script.google.com/macros/s/x/exec")
    monkeypatch.setattr(ce_config, "GOOGLE_SCRIPT_TOKEN", "secret")
    sent = []

    def use(response):
        monkeypatch.setattr(sheets.requests, "post", lambda url, json, timeout: sent.append((url, json)) or response)
        return sent
    return use


def test_new_rows_are_posted_in_column_order_with_the_token(script):
    from src.modules.contact_enrichment import sheets
    sent = script(FakeResponse({"status": "success", "added": 1, "skipped": 1}))
    answer = sheets.publish("Leads", ["A", "B"], [{"A": "x", "B": None}, {"A": 5, "B": "(559) 555-0101"}])
    assert answer["added"] == 1
    url, body = sent[0]
    assert url.endswith("/exec") and body["token"] == "secret" and body["tab"] == "Leads"
    assert body["columns"] == ["A", "B"] and body["rows"] == [["x", ""], ["5", "(559) 555-0101"]]



@pytest.mark.parametrize("response, message", [
    (FakeResponse({"status": "error", "message": "bad token"}), "bad token"),
    (FakeResponse(None, text="<html>Sign in</html>"), "JSON"),
    (FakeResponse(None, status=500), "500"),
])
def test_a_refused_or_broken_answer_raises(script, response, message):
    from src.modules.contact_enrichment import sheets
    script(response)
    with pytest.raises(Exception, match=message):
        sheets.publish("Leads", ["A"], [])


def test_sheet_needs_url_and_token(monkeypatch):
    from src.modules.contact_enrichment import sheets
    monkeypatch.setattr(ce_config, "GOOGLE_SCRIPT_URL", "https://x/exec")
    monkeypatch.setattr(ce_config, "GOOGLE_SCRIPT_TOKEN", "")
    assert not sheets.enabled()
    monkeypatch.setattr(ce_config, "GOOGLE_SCRIPT_TOKEN", "secret")
    assert sheets.enabled()


def test_stage_sends_named_people_and_a_sheet_failure_never_stops_it(sandbox, monkeypatch):
    from src.modules.contact_enrichment import sheets
    sent = []
    monkeypatch.setattr(sheets, "enabled", lambda: True)
    monkeypatch.setattr(sheets, "publish", lambda tab, columns, rows: sent.append((tab, [r["First Name"] for r in rows])) or {})
    assert ce.main(FakeSearchBug()) is True
    assert sent == [(ce_config.GOOGLE_SHEET_TAB, ["Maria", "Sam"])]

    def broken(*args):
        raise RuntimeError("quota exceeded")
    monkeypatch.setattr(sheets, "publish", broken)
    assert ce.main(FakeSearchBug()) is True
