import json
from datetime import datetime, timezone

from src.modules.link_extraction import pipeline
from src.modules.link_extraction.full_text_filter import KEEP

NOW = datetime(2026, 10, 14, 18, 0, tzinfo=timezone.utc)
TEXT = "A motorcyclist was killed in a crash on Highway 99 in Fresno, California, the CHP said on Oct. 13. " * 4


def write_finalized(tmp_path, monkeypatch, items):
    path = tmp_path / "finalized_raw.json"
    path.write_text(json.dumps({"items": items}))
    monkeypatch.setattr(pipeline, "INPUT_FILE", path)


def test_each_finalized_item_becomes_one_article(tmp_path, monkeypatch):
    write_finalized(tmp_path, monkeypatch, [
        {"published_date": "2026-10-13T10:00:00+00:00", "source_name": "KTLA", "title": "Motorcyclist killed in Fresno crash",
         "url": "https://ktla.com/a", "description": "CHP says"},
        {"published_date": None, "source_name": "ABC7", "title": "Other crash", "url": "//abc7.com/b", "description": None},
        {"title": "no url"},
    ])
    monkeypatch.setattr(pipeline, "attempt", lambda url: {
        "ok": True, "fetch_status": "ok", "text": TEXT, "metadata": {}, "final_url": url, "method": "page",
    })

    cases = pipeline.load_cases()
    assert [c["open_first"] for c in cases] == ["https://ktla.com/a", "https://abc7.com/b"]

    articles, errors = pipeline.process_case(cases[0], {}, {}, NOW)
    assert not errors and len(articles) == 1
    article = articles[0]
    assert article["publisher"] == "KTLA" and article["published_at"] == "2026-10-13T10:00:00+00:00"
    assert article["text_available"] and article["filter_decision"] == KEEP
    assert article["county"] is None or article["location_status"] == "california_confirmed"
