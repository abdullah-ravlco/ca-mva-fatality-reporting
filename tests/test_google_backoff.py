from types import SimpleNamespace

from src.modules.link_extraction import google_news_decoder as g


def reset(monkeypatch):
    monkeypatch.setattr(g, "_pace", {"next": 0.0, "interval": g.MIN_SECONDS_BETWEEN_CALLS, "ok_streak": 0, "slowed_at": -1e9})
    monkeypatch.setattr(g.time, "sleep", lambda seconds: None)  # no real waiting in tests


def test_a_429_is_retried_and_slows_the_pace(monkeypatch):
    reset(monkeypatch)
    statuses = iter([429, 429, 200])
    response = g._send(lambda: SimpleNamespace(status_code=next(statuses)))

    assert response.status_code == 200
    # two 429s together count as one slowdown (calls in flight fail at the same time)
    assert g._pace["interval"] == g.MIN_SECONDS_BETWEEN_CALLS * 2


def test_it_gives_up_after_the_attempt_limit_and_the_interval_is_capped(monkeypatch):
    reset(monkeypatch)
    calls = []
    response = g._send(lambda: (calls.append(1), SimpleNamespace(status_code=429))[1])

    assert response.status_code == 429 and len(calls) == g.MAX_ATTEMPTS
    assert g._pace["interval"] <= g.MAX_SECONDS_BETWEEN_CALLS


def test_pace_recovers_after_a_calm_stretch(monkeypatch):
    reset(monkeypatch)
    g._note_result(429)
    slowed = g._pace["interval"]
    for _ in range(g.CALMER_AFTER_OK_CALLS):
        g._note_result(200)

    assert g._pace["interval"] < slowed
