"""Each stage keeps what earlier runs produced and works only on what is new."""

import json

import pytest

import config.settings
import src.core.outputs
from src.modules.contact_enrichment import config as ce_config
from src.modules.contact_enrichment import pipeline as ce
from src.modules.incident_grouping.pipeline import drop_duplicate_articles, match_earlier
from src.modules.link_extraction import pipeline as link
from src.modules.source_collection import output_files
from src.modules.source_collection.finalize import drop_known

TEXT = "A driver was killed in a crash on Highway 99 in Fresno, California, the CHP said on Oct. 13. " * 4


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(config.settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(src.core.outputs, "DATA_DIR", tmp_path)
    monkeypatch.setenv("PIPELINE_RUN_ID", "test-run")
    return tmp_path


def item(n):
    return {"published_date": "2026-10-13T10:00:00+00:00", "source_name": f"Outlet{n}", "title": f"Driver killed in crash {n}",
            "url": f"https://outlet{n}.com/story-{n}", "description": "CHP says"}


# ------------------------------------------------------------------ stage 1

def test_finalized_raw_grows_and_never_repeats_a_link():
    earlier = [item(1), item(2)]
    merged = output_files.merge_finalized(earlier, [item(2), item(3)])
    assert [i["source_name"] for i in merged] == ["Outlet1", "Outlet2", "Outlet3"]
    kept, dropped = drop_known([item(2), item(3), dict(item(4), url="https://news.google.com/rss/articles/x")], earlier)
    assert [i["source_name"] for i in kept] == ["Outlet3", "Outlet4"] and dropped == 1  # a Google link is compared later


# ------------------------------------------------------------------ stage 2

def test_stage2_opens_only_the_items_it_has_not_read(sandbox, monkeypatch):
    finalized = sandbox / "finalized_raw.json"
    monkeypatch.setattr(link, "INPUT_FILE", finalized)
    monkeypatch.setattr(link, "CHECKPOINT_FILE", sandbox / "link_extraction" / "progress.jsonl")
    monkeypatch.setattr(link, "FETCH_WORKERS", 1)
    opened = []
    monkeypatch.setattr(link, "attempt", lambda url: (opened.append(url), {
        "ok": True, "fetch_status": "ok", "text": TEXT, "metadata": {}, "final_url": url, "method": "page"})[1])

    finalized.write_text(json.dumps({"window_start": "2026-10-01T00:00:00+00:00", "items": [item(1), item(2)]}))
    assert link.main() is True
    assert len(opened) == 2

    opened.clear()
    finalized.write_text(json.dumps({"window_start": "2026-10-08T00:00:00+00:00", "items": [item(1), item(2), item(3)]}))
    assert link.main() is True
    assert opened == ["https://outlet3.com/story-3"]                       # earlier items are not opened again

    out = json.loads((sandbox / "link_extraction" / "articles.json").read_text())
    assert [a["publisher"] for a in out["articles"]] == ["Outlet1", "Outlet2", "Outlet3"]
    assert out["summary"]["new_articles"] == 1 and out["summary"]["articles"] == 3

    opened.clear()
    assert link.main() is True and opened == []                             # nothing new: nothing opened


# ------------------------------------------------------------------ stage 3

def article(n, **fields):
    return {"article_id": f"a{n}", "canonical_url": f"https://x.com/{n}", "url": f"https://x.com/{n}",
            "title": f"Crash {n}", "text_available": True, **fields}


def test_an_incident_that_gains_an_article_is_matched_to_its_earlier_id():
    earlier = [{"incident_id": "inc-old", "articles": [article(1), article(2)]},
               {"incident_id": "inc-other", "articles": [article(9)]}]
    groups = [([article(1), article(2), article(3)], "grew"), ([article(7)], "new crash"), ([article(9)], "same")]
    matches = match_earlier(groups, earlier)
    assert [m and m["incident_id"] for m in matches] == ["inc-old", None, "inc-other"]


def test_a_new_article_with_nothing_new_is_discarded_as_a_duplicate():
    old = {"incident_id": "inc-old", "articles": [article(1, incident_date="2026-10-12", place="Fresno")]}
    repeat = article(2, incident_date="2026-10-12", place="Fresno")
    members, discarded = drop_duplicate_articles([old["articles"][0], repeat], old)
    assert [m["article_id"] for m in members] == ["a1"]
    assert discarded[0]["article_id"] == "a2" and discarded[0]["incident_id"] == "inc-old"


def test_a_new_article_that_names_the_victim_is_kept():
    old = {"incident_id": "inc-old", "articles": [article(1, incident_date="2026-10-12", place="Fresno",
                                                          title="Motorcyclist killed in Fresno crash")]}
    named = article(2, incident_date="2026-10-12", place="Fresno", title="Maria Lopez killed in Fresno crash")
    members, discarded = drop_duplicate_articles([old["articles"][0], named], old)
    assert [m["article_id"] for m in members] == ["a1", "a2"] and discarded == []


# ------------------------------------------------------------------ stage 6

class Counting:
    def __init__(self):
        self.calls = []

    def search(self, fields):
        self.calls.append(fields)
        return {}


def test_stage6_searches_only_new_people(sandbox, monkeypatch):
    leads = sandbox / "verified_leads.json"
    monkeypatch.setattr(ce_config, "LEADS_FILE", leads)
    monkeypatch.setattr(ce_config, "PASSED_LEADS_FILE", sandbox / "passed_leads.json")
    monkeypatch.setattr(ce_config, "CACHE_FILE", sandbox / "state" / "searchbug_cache.json")

    def lead(n, name):
        return {"incident_id": f"inc-{n}", "verdict": "qualifies", "crash_date": "2026-09-12", "crash_location": "Fresno",
                "deceased": [{"name": name, "age": 40}]}

    leads.write_text(json.dumps({"leads": [lead(1, "Maria Lopez")]}))
    first = Counting()
    assert ce.main(first) is True and first.calls

    leads.write_text(json.dumps({"leads": [lead(1, "Maria Lopez"), lead(2, "Sam Doe")]}))
    second = Counting()
    assert ce.main(second) is True
    assert second.calls and all(c.get("LNAME") == "Doe" for c in second.calls)    # Maria is not searched again

    out = json.loads((sandbox / "contact_enrichment" / "contacts.json").read_text())
    assert [p["name"] for p in out["people"]] == ["Maria Lopez", "Sam Doe"]       # both kept, in lead order

    third = Counting()
    assert ce.main(third) is True and third.calls == []


def test_stage3_second_run_keeps_the_incident_unchanged_when_the_new_article_adds_nothing(sandbox, monkeypatch):
    from src.modules.incident_grouping import pipeline as grouping

    articles_file = sandbox / "articles.json"
    monkeypatch.setattr(grouping, "INPUT_FILE", articles_file)
    monkeypatch.setattr(grouping, "group_with_reasons", lambda articles: [(articles, ["same crash"])])

    def write(*numbers):
        articles_file.write_text(json.dumps({"articles": [
            dict(article(n, incident_date="2026-10-12", place="Fresno", filter_decision="keep", case_id=f"c{n}"))
            for n in numbers]}))

    def incidents():
        return json.loads((sandbox / "incident_grouping" / "incidents.json").read_text())

    write(1)
    assert grouping.main() is True
    first = incidents()["incidents"][0]

    write(1, 2)                                   # a second outlet repeats the same facts
    assert grouping.main() is True
    out = incidents()
    assert [i["incident_id"] for i in out["incidents"]] == [first["incident_id"]]
    assert [a["article_id"] for a in out["incidents"][0]["articles"]] == ["a1"]
    assert [d["article_id"] for d in out["discarded_duplicates"]] == ["a2"]
    assert out["summary"]["duplicate_articles_discarded"] == 1


def test_stage3_a_late_story_about_an_older_crash_joins_a_known_incident_or_makes_a_new_one(sandbox, monkeypatch):
    # run 2 collects stories published after run 1 started; they describe crashes from before it
    from src.modules.incident_grouping import pipeline as grouping

    articles_file = sandbox / "articles.json"
    monkeypatch.setattr(grouping, "INPUT_FILE", articles_file)
    known = article(1, incident_date="2026-10-12", place="Fresno", road="Highway 99", filter_decision="keep",
                    case_id="c1", title="Driver killed in crash on Highway 99 in Fresno",
                    text="A driver was killed in a crash on Highway 99 in Fresno on Oct. 12, the CHP said.")
    same_crash = dict(known, article_id="a2", canonical_url="https://x.com/2", url="https://x.com/2", case_id="c2",
                      title="Fresno Highway 99 crash leaves driver dead")
    unknown_crash = article(3, incident_date="2026-10-01", place="Bakersfield", road="Ming Avenue", filter_decision="keep",
                            case_id="c3", title="Pedestrian killed on Ming Avenue in Bakersfield",
                            text="A pedestrian was struck and killed on Ming Avenue in Bakersfield on Oct. 1, police said.")

    def run(*records):
        articles_file.write_text(json.dumps({"articles": list(records)}))
        assert grouping.main() is True
        return json.loads((sandbox / "incident_grouping" / "incidents.json").read_text())

    first = run(known)["incidents"][0]["incident_id"]
    out = run(known, same_crash, unknown_crash)
    by_article = {a["article_id"]: i["incident_id"] for i in out["incidents"] for a in i["articles"]}
    assert by_article["a1"] == first and "a2" not in by_article
    assert [d["incident_id"] for d in out["discarded_duplicates"]] == [first]
    assert by_article["a3"] != first
