import json
from datetime import datetime, timedelta, timezone

import pytest

import config.settings
import src.core.outputs as outputs
from src.core import db, migrate_to_postgres


class FakeDb:
    """In-memory stand-in for Postgres: same put/get/keys as src/core/db.py."""

    def __init__(self):
        self.rows = {}
        self.saved_at = datetime.now(timezone.utc) + timedelta(seconds=60)  # newer than any file written in the test

    def put(self, key, data):
        self.rows[key] = json.loads(json.dumps(data))

    def get(self, key, default=None):
        return self.rows.get(key, default)

    def get_with_time(self, key):
        return (self.rows[key], self.saved_at) if key in self.rows else None


@pytest.fixture
def fake(tmp_path, monkeypatch):
    store = FakeDb()
    monkeypatch.setattr(config.settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(outputs, "DATA_DIR", tmp_path)
    monkeypatch.setattr(migrate_to_postgres, "DATA_DIR", tmp_path)
    monkeypatch.setenv("PIPELINE_RUN_ID", "run-1")
    monkeypatch.setenv("DATABASE_URL", "postgresql://example/mva")
    monkeypatch.setattr(db, "put", store.put)
    monkeypatch.setattr(db, "get", store.get)
    monkeypatch.setattr(db, "get_with_time", store.get_with_time)
    monkeypatch.setattr(outputs, "_copy_failed", [False])
    return store


def test_stage_output_is_written_to_files_and_copied_to_postgres(fake, tmp_path):
    path = outputs.write_stage_json("severity_screening", "out.json", {"a": 1})
    assert fake.rows == {
        "runs/run-1/severity_screening/out.json": {"a": 1},   # run history
        "severity_screening/out.json": {"a": 1},              # latest
    }
    assert json.loads(path.read_text()) == {"a": 1}             # the JSON file is still written
    assert outputs.read_json(path) == {"a": 1}


def test_stages_read_the_database_copy_not_the_file(fake, tmp_path):
    path = outputs.write_stage_json("m", "a.json", {"from": "run"})
    path.write_text(json.dumps({"from": "file"}))                  # the file differs (older than the copy)
    assert outputs.read_json(path) == {"from": "run"}              # the database wins
    path.unlink()                                                  # data/ deleted: nothing is lost
    assert outputs.read_json(path) == {"from": "run"}


def test_the_file_is_read_when_the_database_has_no_copy_or_an_older_one(fake, tmp_path):
    path = tmp_path / "m" / "b.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"only": "file"}))
    assert outputs.read_json(path) == {"only": "file"}             # no copy in the database yet
    fake.rows["m/b.json"] = {"only": "old copy"}
    fake.saved_at = datetime.now(timezone.utc) - timedelta(hours=1)
    assert outputs.read_json(path) == {"only": "file"}             # the file is newer: a run could not reach the database


def test_an_unreachable_database_falls_back_to_the_files(fake, tmp_path, monkeypatch):
    path = tmp_path / "m" / "c.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"x": 1}))

    def down(key):
        raise OSError("connection refused")

    monkeypatch.setattr(db, "get_with_time", down)
    assert outputs.read_json(path) == {"x": 1} and outputs._copy_failed == [True]


def test_a_database_failure_never_stops_writing_files(fake, tmp_path, monkeypatch):
    def down(key, data):
        raise OSError("connection refused")

    monkeypatch.setattr(db, "put", down)
    monkeypatch.setattr(outputs, "_copy_failed", [False])
    path = outputs.write_stage_json("m", "a.json", {"x": 1})
    outputs.write_stage_json("m", "b.json", {"y": 2})          # copying is off after the first failure
    assert json.loads(path.read_text()) == {"x": 1} and outputs._copy_failed == [True]


def test_missing_files_give_the_default(fake, tmp_path):
    assert outputs.read_json(tmp_path / "nothing.json", default={"x": 1}) == {"x": 1}


def test_paths_outside_data_stay_files(fake, tmp_path_factory):
    other = tmp_path_factory.mktemp("elsewhere") / "x.json"
    outputs.write_json_atomic(other, {"b": 2})
    assert fake.rows == {} and outputs.read_json(other) == {"b": 2}


def test_without_database_url_files_are_used(tmp_path, monkeypatch):
    monkeypatch.setattr(outputs, "DATA_DIR", tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    outputs.write_json_atomic(tmp_path / "m" / "f.json", {"c": 3})
    assert json.loads((tmp_path / "m" / "f.json").read_text()) == {"c": 3}


def test_migration_copies_existing_json_files(fake, tmp_path, monkeypatch):
    (tmp_path / "incident_grouping").mkdir()
    (tmp_path / "incident_grouping" / "incidents.json").write_text('{"incidents": []}')
    (tmp_path / "fetch_cache.json").write_text("{}")
    (tmp_path / "broken.json").write_text("{nope")
    monkeypatch.setattr(migrate_to_postgres, "check", lambda: True)
    from src.core import db_sync
    monkeypatch.setattr(db_sync, "sync_all", lambda: {})
    assert migrate_to_postgres.main() is True
    assert fake.rows == {"incident_grouping/incidents.json": {"incidents": []}}


def test_connection_settings_from_url_or_libpq_variables(monkeypatch):
    for name in ("DATABASE_URL", "PGHOST", "PGDATABASE", "PGSERVICE", "DB_SCHEMA"):
        monkeypatch.delenv(name, raising=False)
    assert db.enabled() is False
    monkeypatch.setenv("PGHOST", "db.example.com")
    assert db.enabled() is False                      # a host alone is not enough
    monkeypatch.setenv("PGDATABASE", "mva")
    assert db.enabled() is True
    monkeypatch.setenv("DATABASE_URL", "postgresql://ann:s3cret@db.example.com:6543/mva?sslmode=require")
    assert "s3cret" not in db.describe() and "ann@db.example.com:6543/mva" in db.describe()
    monkeypatch.setenv("DB_SCHEMA", "leads; drop table x")
    with pytest.raises(ValueError):
        db.schema_name()


def test_a_database_problem_after_a_stage_never_stops_the_run(monkeypatch, caplog):
    import logging

    import src.main as pipeline_main
    from src.core import db_sync

    caplog.set_level(logging.INFO)
    calls = []
    monkeypatch.setattr(db, "enabled", lambda: True)
    monkeypatch.setattr(db, "reset", lambda: calls.append("reset"))
    monkeypatch.setattr(db, "describe", lambda: "db")

    def broken(stage):
        raise OSError("server closed the connection")

    monkeypatch.setattr(db_sync, "sync_stage", broken)
    monkeypatch.setattr(db_sync, "record_run", lambda *args: calls.append("run"))
    pipeline_main.save_to_database("incident_grouping", None, True)    # must not raise
    assert calls == ["reset"] and "could not save incident_grouping" in caplog.text

    monkeypatch.setattr(db_sync, "sync_stage", lambda stage: {"incidents": 3})
    pipeline_main.save_to_database("incident_grouping", None, True)
    assert calls[-1] == "run" and "saved {'incidents': 3}" in caplog.text


def test_no_database_configured_means_no_database_work(monkeypatch):
    import src.main as pipeline_main

    monkeypatch.setattr(db, "enabled", lambda: False)
    monkeypatch.setattr(db, "connection", lambda: pytest.fail("must not connect"))
    pipeline_main.save_to_database("incident_grouping", None, True)


def test_a_repeated_variable_name_or_quotes_in_the_url_are_tolerated(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", 'DATABASE_URL="postgresql://u:p@localhost:5432/mva"')
    assert db.database_url() == "postgresql://u:p@localhost:5432/mva"


def test_a_url_without_its_scheme_gets_one(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres:secret@127.0.0.1:5432/mva_reporting")
    assert db.database_url() == "postgresql://postgres:secret@127.0.0.1:5432/mva_reporting"
    monkeypatch.setenv("DATABASE_URL", "host=localhost dbname=mva")          # key=value form is left alone
    assert db.database_url() == "host=localhost dbname=mva"
