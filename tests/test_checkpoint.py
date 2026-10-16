import json
from datetime import datetime, timedelta, timezone

from src.core.checkpoint import Checkpoint


def test_finished_items_survive_a_restart(tmp_path):
    path = tmp_path / "progress.jsonl"
    first = Checkpoint(path, "sig")
    first.add("a", {"records": [1, 2]})
    first.add("b", {"records": []})

    second = Checkpoint(path, "sig")  # a new process opening the same file

    assert second.get("a") == {"records": [1, 2]} and second.get("b") == {"records": []}
    assert second.get("c") is None


def test_different_signature_starts_over(tmp_path):
    path = tmp_path / "progress.jsonl"
    Checkpoint(path, "window-A").add("a", {"x": 1})

    assert Checkpoint(path, "window-B").items == {}


def test_stale_checkpoint_starts_over_but_a_fresh_one_resumes(tmp_path):
    path = tmp_path / "progress.jsonl"
    Checkpoint(path, "sig").add("a", {"x": 1})
    assert Checkpoint(path, "sig", max_age_hours=6).items  # fresh

    old = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    lines = path.read_text().splitlines()
    lines[0] = json.dumps({"header": {"signature": "sig", "created": old}})
    path.write_text("\n".join(lines) + "\n")
    assert Checkpoint(path, "sig", max_age_hours=6).items == {}


def test_a_line_cut_off_by_a_crash_is_ignored(tmp_path):
    path = tmp_path / "progress.jsonl"
    Checkpoint(path, "sig").add("a", {"x": 1})
    with path.open("a") as file:
        file.write('{"key": "b", "payload": {"x"')  # process killed mid-write

    resumed = Checkpoint(path, "sig")
    assert resumed.items == {"a": {"x": 1}}


def test_clear_removes_the_file(tmp_path):
    path = tmp_path / "progress.jsonl"
    checkpoint = Checkpoint(path, "sig")
    checkpoint.clear()
    assert not path.exists()
    checkpoint.clear()  # clearing twice is fine
