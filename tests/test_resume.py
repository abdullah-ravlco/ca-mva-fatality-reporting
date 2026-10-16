"""An interrupted run leaves its results on disk and the next run continues from them."""

import json
import threading
from types import SimpleNamespace

import pytest

import config.settings
import src.core.outputs
from src.core.checkpoint import Checkpoint
from src.modules.link_extraction import pipeline as link
from src.modules.source_collection import raw_collection

TEXT = "A driver was killed in a crash on Highway 99 in Fresno, California, the CHP said on Oct. 13. " * 4


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Send every stage output and checkpoint into tmp_path, never the real data folder."""
    monkeypatch.setattr(config.settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(src.core.outputs, "DATA_DIR", tmp_path)
    monkeypatch.setenv("PIPELINE_RUN_ID", "test-run")
    monkeypatch.setattr(link, "CHECKPOINT_FILE", tmp_path / "link_extraction" / "progress.jsonl")
    monkeypatch.setattr(link, "PARTIAL_EVERY", 10)
    monkeypatch.setattr(link, "FETCH_WORKERS", 1)
    finalized = tmp_path / "finalized_raw.json"
    items = [
        {"published_date": "2026-10-13T10:00:00+00:00", "source_name": f"Outlet{i}", "title": f"Driver killed in crash {i}",
         "url": f"https://outlet{i}.com/story-{i}", "description": "CHP says"}
        for i in range(60)
    ]
    finalized.write_text(json.dumps({"items": items}))
    monkeypatch.setattr(link, "INPUT_FILE", finalized)
    return tmp_path


def ok(url):
    return {"ok": True, "fetch_status": "ok", "text": TEXT, "metadata": {}, "final_url": url, "method": "page"}


def test_stage2_files_exist_before_the_run_ends_and_a_rerun_continues(sandbox, monkeypatch):
    calls = []

    def dies_at_35(url):
        calls.append(url)
        if len(calls) == 35:
            raise KeyboardInterrupt  # the user stops the run
        return ok(url)

    monkeypatch.setattr(link, "attempt", dies_at_35)
    with pytest.raises(KeyboardInterrupt):
        link.main()

    # Nothing finished the run, yet the work done so far is on disk.
    progress = (sandbox / "link_extraction" / "progress.jsonl").read_text().splitlines()
    finished = len(progress) - 1                                     # header + one line per finished article
    assert 34 <= finished < 45                                       # stopped promptly, not run to the end
    partial = json.loads((sandbox / "link_extraction" / "articles.json").read_text())
    assert partial["complete"] is False and len(partial["articles"]) >= 30

    # Rerun: only the unfinished articles are fetched, and the real output is written.
    calls.clear()
    monkeypatch.setattr(link, "attempt", lambda url: (calls.append(url), ok(url))[1])
    link.main()

    assert len(calls) == 60 - finished
    final = json.loads((sandbox / "link_extraction" / "articles.json").read_text())
    assert final["complete"] is True and len(final["articles"]) == 60
    assert not (sandbox / "link_extraction" / "progress.jsonl").exists()


def test_stage1_finished_searches_are_saved_before_the_run_ends(tmp_path):
    rss = b"<rss><channel><title>T</title><item><title>Driver killed in crash</title><link>https://a.com/x</link></item></channel></rss>"
    lock, state = threading.Lock(), {"calls": 0}

    class Client:
        def get(self, url):
            with lock:
                state["calls"] += 1
                if state["calls"] > 20:
                    raise KeyboardInterrupt  # the run is stopped part-way through the searches
            return SimpleNamespace(content=rss)

    path = tmp_path / "collection_progress.jsonl"
    with pytest.raises(KeyboardInterrupt):
        raw_collection.collect_rss(Client(), Checkpoint(path, "sig"))

    saved = len(path.read_text().splitlines()) - 1
    assert 15 <= saved <= 20                                          # the finished searches are on disk

    seen = []

    class CountingClient:
        def get(self, url):
            seen.append(url)
            return SimpleNamespace(content=rss)

    raw_collection.collect_rss(CountingClient(), Checkpoint(path, "sig"))
    assert len(seen) == len(raw_collection.google_news_feeds()) + len(raw_collection.NEWS_RSS_FEEDS) - saved


def test_all_raw_is_written_while_the_searches_run(tmp_path, monkeypatch):
    from src.modules.source_collection import pipeline as stage1

    monkeypatch.setattr(config.settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(src.core.outputs, "DATA_DIR", tmp_path)
    monkeypatch.setenv("PIPELINE_RUN_ID", "test-run")
    monkeypatch.setattr(stage1, "PARTIAL_EVERY", 5)
    rss = b"<rss><channel><title>T</title><item><title>Driver killed in crash</title><link>https://a.com/x</link></item></channel></rss>"
    path = tmp_path / "source_collection" / "all_raw.json"
    sizes = []
    checkpoint = Checkpoint(tmp_path / "progress.jsonl", "sig")
    write_partial = stage1.partial_all_raw_writer("t", checkpoint)

    def on_progress():
        write_partial()
        if path.exists():
            data = json.loads(path.read_text())
            sizes.append((data["complete"], data["total_items"]))

    class Client:
        def get(self, url):
            return SimpleNamespace(content=rss)

    raw_collection.collect_rss(Client(), checkpoint, on_progress)

    assert sizes and all(complete is False for complete, _ in sizes)   # the file exists before the end
    assert [n for _, n in sizes] == sorted(n for _, n in sizes) and sizes[-1][1] > sizes[0][1]  # and grows
