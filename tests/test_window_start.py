import json
from datetime import datetime, timezone

import pytest

from src.modules.source_collection import history
from src.modules.source_collection.sources import google_news_feeds


@pytest.fixture
def run_history(tmp_path, monkeypatch):
    def write(entries):
        path = tmp_path / "history" / "run_history.json"
        path.parent.mkdir()
        path.write_text(json.dumps(entries))
        monkeypatch.setattr(history, "HISTORY_FILE", path)

    monkeypatch.setattr(history, "HISTORY_FILE", tmp_path / "history" / "run_history.json")  # none yet
    return write


def test_window_starts_at_the_last_run_that_collected_something(run_history):
    run_history([
        {"run_at": "2026-10-01T08:00:00+00:00", "all_raw": 400},
        {"run_at": "2026-10-08T08:00:00+00:00", "all_raw": 380},
        {"run_at": "2026-10-09T08:00:00+00:00", "all_raw": 0},  # every source failed: its gap is collected next time
    ])
    assert history.window_start() == datetime(2026, 10, 8, 8, 0, tzinfo=timezone.utc)


def test_first_run_needs_a_start_date(run_history):
    with pytest.raises(ValueError, match="--since"):
        history.window_start()


def test_since_overrides_the_history_and_must_be_a_date(run_history):
    run_history([{"run_at": "2026-10-08T08:00:00+00:00", "all_raw": 380}])
    assert history.window_start("2026-10-01").date().isoformat() == "2026-10-01"
    with pytest.raises(ValueError):
        history.window_start("last week")


def test_google_news_period_covers_the_window_in_whole_days():
    since, now = datetime(2026, 10, 8, 8, 0, tzinfo=timezone.utc), datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)
    assert all("when%3A7d" in feed["url"] for feed in google_news_feeds(since, now))
    assert all("when%3A1d" in feed["url"] for feed in google_news_feeds(now, now))
