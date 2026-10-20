"""The stage tables against a real Postgres. Skipped unless TEST_DATABASE_URL points at one, e.g.

    docker run -d --name mva-pg-test -e POSTGRES_PASSWORD=test -e POSTGRES_DB=mva_test -p 55432:5432 postgres:16
    TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/mva_test .venv/bin/python -m pytest tests/test_postgres_tables.py

Each test uses its own schema, dropped afterwards.
"""

import json
import os
import uuid

import pytest

import config.settings
import src.core.outputs as outputs
from src.core import db, db_sync

URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture
def database(tmp_path, monkeypatch):
    schema = f"test_{uuid.uuid4().hex[:10]}"
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("DB_SCHEMA", schema)
    monkeypatch.setenv("PIPELINE_RUN_ID", "run-1")
    monkeypatch.setattr(config.settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(outputs, "DATA_DIR", tmp_path)
    monkeypatch.setattr(outputs, "_copy_failed", [False])
    files = {stage: tmp_path / path.relative_to(path.parents[1]) for stage, path in db_sync.FILES.items()}
    monkeypatch.setattr(db_sync, "FILES", files)
    monkeypatch.setattr(db_sync, "CONTACT_DETAILS_FILE", tmp_path / "contact_enrichment" / "contacts_details.json")
    db.reset()
    yield tmp_path
    try:
        db.connection().execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    finally:
        db.reset()


def write(tmp_path, relative, data):
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


ARTICLE = {"article_id": "art-1", "url": "https://ktla.com/a", "canonical_url": "https://ktla.com/a",
           "title": "Woman killed in crash", "publisher": "KTLA", "published_at": "2026-10-08T07:00:00+00:00",
           "fetch_status": "ok", "text_available": True, "text": "Kathleen Erickson, 67, of Modesto died.",
           "filter_decision": "keep", "filter_reasons": [], "discovered_by_sources": ["Google News: x"]}


def sample_outputs(tmp_path):
    write(tmp_path, "source_collection/finalized_raw.json", {"items": [
        {"published_date": "2026-10-08T07:00:00+00:00", "source_name": "KTLA", "title": "Woman killed", "url": "https://ktla.com/a", "description": None}]})
    write(tmp_path, "link_extraction/articles.json", {"articles": [ARTICLE]})
    write(tmp_path, "incident_grouping/incidents.json", {"run_at": "2026-10-09T00:00:00+00:00", "incidents": [
        {"incident_id": "inc-1", "article_count": 1, "candidate_incident_dates": [{"date": "2026-10-08"}],
         "locations": [{"place": "Modesto"}], "conflicts": [], "grouping_rationale": [], "articles": [ARTICLE]}]})
    write(tmp_path, "severity_screening/screened_incidents.json", {"incidents": [
        {"incident_id": "inc-1", "status": "fault_unidentified", "tags": ["fault_unidentified"], "reasons": ["x"],
         "drop_reason": None, "facts": {"victims": []}, "screened_at": "2026-10-09T01:00:00+00:00"},
        {"incident_id": "inc-2", "status": "dropped", "tags": ["no_other_party"], "reasons": ["no other party was involved"],
         "drop_reason": {"codes": ["no_other_party"], "text": "no other party was involved"}, "facts": None}]})
    write(tmp_path, "lead_verification/verified_leads_details.json", {"incidents": [
        {"incident_id": "inc-1", "analysis_status": "analysed", "analysed_at": "2026-10-09T02:00:00+00:00",
         "qualification": {"verdict": "fault_unidentified", "reasons": [], "explanation": "cause under investigation",
                           "corrections": ["other party at fault -> unknown"]},
         "incident": {"date": {"value": "2026-10-08", "quote": "Thursday morning", "source_url": "https://ktla.com/a", "quote_verified": True},
                      "time": {"value": "08:30 AM"}, "location": {"value": "Fortuna Avenue, Modesto"}, "city": "Modesto",
                      "crash_type": "single_vehicle", "other_party_involved": "yes", "other_party_at_fault": "unknown"},
         "people": [
             {"name": {"value": "Kathleen Erickson", "quote": "Kathleen Erickson, 67, of Modesto died.", "source_url": "https://ktla.com/a", "quote_verified": True},
              "age": {"value": "67"}, "address": {"value": "Modesto"}, "outcome": "killed", "role": "other", "at_fault": "no",
              "fault_basis": "circumstances_described", "fault_explanation": "inside her home", "impaired": "unknown"},
             {"name": {"value": None}, "age": {"value": None}, "address": {"value": None}, "outcome": "uninjured", "role": "driver",
              "at_fault": "unknown", "impaired": "unknown"}],
         "missing_information": ["fault"]}]})
    write(tmp_path, "contact_enrichment/contacts.json", {"run_at": "2026-10-09T03:00:00+00:00", "people": [
        {"incident_id": "inc-1", "name": "Kathleen Erickson", "age": 67, "match": "matched", "relatives_on_record": 2,
         "deceased": {"dob": "1959-03-20", "street": "100 Main St", "town": "MODESTO", "county": "STANISLAUS", "state": "CA",
                      "zip": "95354", "aliases": ["KATHLEEN A ERICKSON"], "other_locations": ["MERCED, CA"],
                      "phones": [{"number": "2095550100", "type": "Mobile", "last_seen": "2026-06-18"}]},
         "relatives": [
             {"name": "SUSAN EBERLE", "relationship": "Parent", "relationship_source": "searchbug", "address": "1 Oak St",
              "phones": [{"number": "2095550111", "type": "Landline"}], "emails": ["s@example.com"], "lookup": "found"},
             {"name": "ROBERT ERICKSON", "relationship": "Parent", "relationship_source": "searchbug", "phones": []}]}]})
    write(tmp_path, "contact_enrichment/contacts_details.json", {"people": [
        {"incident_id": "inc-1", "name": "Kathleen Erickson", "chosen_record": {"token": "tok-k"}}]})


def counts():
    return {table: db._run(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("collected_items", "publishers", "incidents", "articles", "people", "next_of_kin",
                          "contact_points", "evidence")}


def test_every_stage_fills_its_tables_and_a_second_load_changes_nothing(database):
    sample_outputs(database)
    db_sync.sync_all()
    first = counts()
    assert first == {"collected_items": 1, "publishers": 1, "incidents": 2, "articles": 1, "people": 2,
                     "next_of_kin": 2, "contact_points": 3, "evidence": 7}
    db_sync.sync_all()
    assert counts() == first  # every load is an upsert: nothing is duplicated

    row = db._run(
        "SELECT i.screening_status, i.verification_verdict, i.crash_date::text, a.incident_id, p.age, p.dob::text, "
        "p.zip, p.match_status, p.searchbug_record->>'token' FROM incidents i JOIN articles a USING (incident_id) "
        "JOIN people p USING (incident_id) WHERE i.incident_id = 'inc-1' AND p.outcome = 'killed'").fetchone()
    assert row == ("fault_unidentified", "fault_unidentified", "2026-10-08", "inc-1", 67, "1959-03-20", "95354", "matched", "tok-k")

    kin = db._run("SELECT rank, full_name, relationship FROM next_of_kin ORDER BY rank").fetchall()
    assert kin == [(1, "SUSAN EBERLE", "Parent"), (2, "ROBERT ERICKSON", "Parent")]
    dropped = db._run("SELECT drop_reason_codes FROM incidents WHERE incident_id = 'inc-2'").fetchone()[0]
    assert dropped == ["no_other_party"]


def test_documents_copy_and_migrations_run_once(database):
    outputs.write_stage_json("severity_screening", "x.json", {"a": 1})
    assert db.get("severity_screening/x.json") == {"a": 1}
    assert json.loads((database / "severity_screening" / "x.json").read_text()) == {"a": 1}
    applied = db._run("SELECT name FROM schema_migrations").fetchall()
    db.reset()
    db.connection()                      # reconnect: migrations are not applied again
    assert db._run("SELECT name FROM schema_migrations").fetchall() == applied == [("001_initial.sql",)]
