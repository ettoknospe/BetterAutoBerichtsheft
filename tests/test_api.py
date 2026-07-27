import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient

import main
import scraper

client = TestClient(main.app)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    return tmp_path


def test_list_weeks_empty(data_dir):
    r = client.get("/api/weeks")
    assert r.status_code == 200
    assert r.json() == {"weeks": [], "current": scraper.current_week_id()}


def test_list_weeks_finds_week_files(data_dir):
    (data_dir / "2026-W29.json").write_text("{}")
    (data_dir / "not-a-week.json").write_text("{}")  # must be ignored
    r = client.get("/api/weeks")
    assert r.json()["weeks"] == ["2026-W29"]


def test_get_week_bad_id_format():
    r = client.get("/api/weeks/nonsense")
    assert r.status_code == 400


def test_get_week_missing(data_dir):
    r = client.get("/api/weeks/2026-W29")
    assert r.status_code == 404


def test_get_week_returns_saved_data(data_dir):
    payload = {"week": "2026-W29", "days": []}
    (data_dir / "2026-W29.json").write_text(json.dumps(payload))
    r = client.get("/api/weeks/2026-W29")
    assert r.status_code == 200
    assert r.json() == payload


def test_scrape_bad_week_format():
    r = client.post("/api/scrape", json={"week": "bad"})
    assert r.status_code == 400


def test_scrape_defaults_to_current_week(monkeypatch):
    monkeypatch.setattr(scraper, "scrape_week", lambda week_id: {"week": week_id})
    r = client.post("/api/scrape", json={})
    assert r.status_code == 200
    assert r.json() == {"week": scraper.current_week_id()}


def test_scrape_success_with_explicit_week(monkeypatch):
    calls = []
    monkeypatch.setattr(scraper, "scrape_week", lambda week_id: calls.append(week_id) or {"week": week_id})
    r = client.post("/api/scrape", json={"week": "2026-W29"})
    assert r.status_code == 200
    assert r.json() == {"week": "2026-W29"}
    assert calls == ["2026-W29"]


def test_scrape_error_maps_to_502(monkeypatch):
    def raiser(week_id):
        raise scraper.ScrapeError("webuntis said no")

    monkeypatch.setattr(scraper, "scrape_week", raiser)
    r = client.post("/api/scrape", json={"week": "2026-W29"})
    assert r.status_code == 502


def test_scrape_unexpected_exception_maps_to_500(monkeypatch):
    def raiser(week_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(scraper, "scrape_week", raiser)
    r = client.post("/api/scrape", json={"week": "2026-W29"})
    assert r.status_code == 500


def test_scrape_returns_409_when_already_running():
    assert main.scrape_lock.acquire(blocking=False)
    try:
        r = client.post("/api/scrape", json={"week": "2026-W29"})
        assert r.status_code == 409
    finally:
        main.scrape_lock.release()


@pytest.fixture
def sunday_1800(monkeypatch):
    """SCRAPE_DAY/_SCRAPE_HOUR/_SCRAPE_MINUTE default to 'off' under conftest's
    forced SCRAPE_DAY=off — set them explicitly here regardless of env."""
    monkeypatch.setattr(main, "SCRAPE_DAY", "sun")
    monkeypatch.setattr(main, "_SCRAPE_HOUR", 18)
    monkeypatch.setattr(main, "_SCRAPE_MINUTE", 0)


def test_scrape_due_on_sunday_at_scrape_time(sunday_1800):
    now = dt.datetime(2026, 8, 2, 18, 0)  # a Sunday
    assert main._scrape_due(now, last_run_date=None)


def test_scrape_due_on_sunday_after_scrape_time(sunday_1800):
    now = dt.datetime(2026, 8, 2, 23, 59)
    assert main._scrape_due(now, last_run_date=None)


def test_scrape_not_due_before_scrape_time(sunday_1800):
    now = dt.datetime(2026, 8, 2, 17, 59)
    assert not main._scrape_due(now, last_run_date=None)


def test_scrape_not_due_on_other_weekdays(sunday_1800):
    now = dt.datetime(2026, 8, 3, 18, 0)  # Monday
    assert not main._scrape_due(now, last_run_date=None)


def test_scrape_not_due_twice_same_day(sunday_1800):
    now = dt.datetime(2026, 8, 2, 20, 0)
    assert not main._scrape_due(now, last_run_date=now.date())


def test_scrape_due_again_next_week_after_last_run(sunday_1800):
    now = dt.datetime(2026, 8, 2, 18, 0)
    assert main._scrape_due(now, last_run_date=dt.date(2026, 7, 26))


def test_scheduler_off_when_scrape_day_invalid(monkeypatch):
    monkeypatch.setattr(main, "SCRAPE_DAY", "off")
    monkeypatch.setattr(main.scraper, "scrape_week", lambda week_id: pytest.fail("should not scrape"))
    # scheduler() returns immediately without looping when SCRAPE_DAY is invalid
    main.scheduler()


def test_weeks_to_scrape_returns_previous_and_current_week():
    today = dt.date(2026, 8, 2)  # a Sunday
    assert main._weeks_to_scrape(today) == ["2026-W30", "2026-W31"]


def test_scheduled_scrape_attempts_both_weeks(monkeypatch):
    calls = []
    monkeypatch.setattr(scraper, "scrape_week", lambda week_id: calls.append(week_id))
    main._scheduled_scrape(dt.date(2026, 8, 2))
    assert calls == ["2026-W30", "2026-W31"]


def test_scheduled_scrape_still_attempts_second_week_if_first_fails(monkeypatch):
    calls = []

    def fake_scrape_week(week_id):
        calls.append(week_id)
        if week_id == "2026-W30":
            raise scraper.ScrapeError("boom")

    monkeypatch.setattr(scraper, "scrape_week", fake_scrape_week)
    main._scheduled_scrape(dt.date(2026, 8, 2))  # must not raise
    assert calls == ["2026-W30", "2026-W31"]
