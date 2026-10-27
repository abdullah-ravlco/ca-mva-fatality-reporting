import json

import pytest

from src.core import db, restore_from_postgres as restore


@pytest.fixture
def fake_db(tmp_path, monkeypatch):
    stored = {
        "source_collection/finalized_raw.json": {"items": [{"url": "https://a.com/1"}]},
        "history/run_history.json": [{"run_id": "r1"}],
        "runs/r1/source_collection/finalized_raw.json": {"items": []},
        "../outside.json": {"bad": True},
        "link_extraction/progress.jsonl": {"x": 1},
    }
    monkeypatch.setattr(restore, "DATA_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(db, "enabled", lambda: True)
    monkeypatch.setattr(db, "keys", lambda prefix="": sorted(stored))
    monkeypatch.setattr(db, "get", lambda key, default=None: stored.get(key, default))
    return tmp_path / "data"


def test_missing_files_are_restored_and_existing_ones_kept(fake_db):
    kept = fake_db / "history" / "run_history.json"
    kept.parent.mkdir()
    kept.write_text("[]")

    assert restore.main() is True
    assert json.loads((fake_db / "source_collection" / "finalized_raw.json").read_text())["items"][0]["url"] == "https://a.com/1"
    assert kept.read_text() == "[]"                                         # not overwritten
    assert not (fake_db / "runs").exists()                                  # run copies only on request
    assert not (fake_db.parent / "outside.json").exists()                   # a key cannot leave data/
    assert not (fake_db / "link_extraction" / "progress.jsonl").exists()    # only .json files


def test_overwrite_and_dry_run(fake_db):
    path = fake_db / "history" / "run_history.json"
    path.parent.mkdir()
    path.write_text("[]")

    restore.main(dry_run=True, overwrite=True)
    assert path.read_text() == "[]" and not (fake_db / "source_collection").exists()

    restore.main(overwrite=True, include_runs=True)
    assert json.loads(path.read_text()) == [{"run_id": "r1"}]
    assert (fake_db / "runs" / "r1" / "source_collection" / "finalized_raw.json").exists()
