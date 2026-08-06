import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient

from app import config
from app import ihk_submitter
from app import main
from app import scraper
from app.ihk_client import IhkError

client = TestClient(main.app)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    # ihk_submitter reads/writes via config.DATA_DIR, a separate variable -
    # patch both so any real (non-monkeypatched) ihk_submitter call in a
    # test (e.g. save_local_fields, piggybacked on a successful submit)
    # never touches the real /data on disk.
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
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


def test_ihk_status_returns_load_status(monkeypatch):
    monkeypatch.setattr(ihk_submitter, "load_status", lambda: {"2026-W29": {"lfdnr": 42, "status": "genehmigt"}})
    monkeypatch.setattr(ihk_submitter, "load_local_fields", lambda: {"2026-W29": {"ausbinhalt1": "x"}})
    r = client.get("/api/ihk-status")
    assert r.status_code == 200
    assert r.json() == {
        "status": {"2026-W29": {"lfdnr": 42, "status": "genehmigt"}},
        "fields": {"2026-W29": {"ausbinhalt1": "x"}},
    }


def test_submit_ihk_bad_week_format():
    r = client.post("/api/submit-ihk", json={"week": "bad", "text": "hi"})
    assert r.status_code == 400


def test_submit_ihk_rejects_empty_text():
    r = client.post("/api/submit-ihk", json={"week": "2026-W29", "text": "   "})
    assert r.status_code == 400


def test_submit_ihk_success(monkeypatch, data_dir):
    calls = []
    monkeypatch.setattr(
        ihk_submitter,
        "submit_week",
        lambda week_id, text, ausbinhalt1=None, ausbinhalt2=None: calls.append(
            (week_id, text, ausbinhalt1, ausbinhalt2)
        ),
    )
    r = client.post("/api/submit-ihk", json={"week": "2026-W29", "text": "the text"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert calls == [("2026-W29", "the text", None, None)]


def test_submit_ihk_passes_through_ausbinhalt1_and_2(monkeypatch, data_dir):
    calls = []
    monkeypatch.setattr(
        ihk_submitter,
        "submit_week",
        lambda week_id, text, ausbinhalt1=None, ausbinhalt2=None: calls.append(
            (week_id, text, ausbinhalt1, ausbinhalt2)
        ),
    )
    r = client.post(
        "/api/submit-ihk",
        json={"week": "2026-W29", "text": "the text", "ausbinhalt1": "worked on X", "ausbinhalt2": "training Y"},
    )
    assert r.status_code == 200
    assert calls == [("2026-W29", "the text", "worked on X", "training Y")]


def test_submit_ihk_error_maps_to_502(monkeypatch):
    def raiser(week_id, text, ausbinhalt1=None, ausbinhalt2=None):
        raise IhkError("ihk said no")

    monkeypatch.setattr(ihk_submitter, "submit_week", raiser)
    r = client.post("/api/submit-ihk", json={"week": "2026-W29", "text": "the text"})
    assert r.status_code == 502


def test_submit_ihk_unexpected_exception_maps_to_500(monkeypatch):
    def raiser(week_id, text, ausbinhalt1=None, ausbinhalt2=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(ihk_submitter, "submit_week", raiser)
    r = client.post("/api/submit-ihk", json={"week": "2026-W29", "text": "the text"})
    assert r.status_code == 500


def test_submit_ihk_returns_409_when_already_running():
    assert main.submit_lock.acquire(blocking=False)
    try:
        r = client.post("/api/submit-ihk", json={"week": "2026-W29", "text": "the text"})
        assert r.status_code == 409
    finally:
        main.submit_lock.release()


def test_scrape_success_also_syncs_ihk_status_best_effort(monkeypatch):
    monkeypatch.setattr(scraper, "scrape_week", lambda week_id: {"week": week_id})
    synced = []
    monkeypatch.setattr(ihk_submitter, "sync_status", lambda: synced.append(True))
    r = client.post("/api/scrape", json={"week": "2026-W29"})
    assert r.status_code == 200
    assert synced == [True]


def test_scrape_succeeds_even_if_ihk_status_sync_fails(monkeypatch):
    monkeypatch.setattr(scraper, "scrape_week", lambda week_id: {"week": week_id})

    def raiser():
        raise RuntimeError("ihk portal unreachable")

    monkeypatch.setattr(ihk_submitter, "sync_status", raiser)
    r = client.post("/api/scrape", json={"week": "2026-W29"})
    assert r.status_code == 200  # best-effort: sync failure must not break the scrape response



def test_submit_ihk_success_also_syncs_ihk_status_best_effort(monkeypatch, data_dir):
    monkeypatch.setattr(
        ihk_submitter, "submit_week", lambda week_id, text, ausbinhalt1=None, ausbinhalt2=None: None
    )
    synced = []
    monkeypatch.setattr(ihk_submitter, "sync_status", lambda: synced.append(True))
    r = client.post("/api/submit-ihk", json={"week": "2026-W29", "text": "the text"})
    assert r.status_code == 200
    assert synced == [True]


def test_submit_ihk_success_also_saves_local_fields_best_effort(monkeypatch, data_dir):
    monkeypatch.setattr(
        ihk_submitter, "submit_week", lambda week_id, text, ausbinhalt1=None, ausbinhalt2=None: None
    )
    monkeypatch.setattr(ihk_submitter, "sync_status", lambda: None)
    saved = []
    monkeypatch.setattr(
        ihk_submitter,
        "save_local_fields",
        lambda week_id, ausbinhalt1=None, ausbinhalt2=None: saved.append((week_id, ausbinhalt1, ausbinhalt2)),
    )
    r = client.post(
        "/api/submit-ihk",
        json={"week": "2026-W29", "text": "the text", "ausbinhalt1": "worked on X", "ausbinhalt2": "training Y"},
    )
    assert r.status_code == 200
    assert saved == [("2026-W29", "worked on X", "training Y")]


def test_submit_ihk_succeeds_even_if_local_fields_save_fails(monkeypatch, data_dir):
    monkeypatch.setattr(
        ihk_submitter, "submit_week", lambda week_id, text, ausbinhalt1=None, ausbinhalt2=None: None
    )
    monkeypatch.setattr(ihk_submitter, "sync_status", lambda: None)

    def raiser(week_id, ausbinhalt1=None, ausbinhalt2=None):
        raise RuntimeError("disk full")

    monkeypatch.setattr(ihk_submitter, "save_local_fields", raiser)
    r = client.post("/api/submit-ihk", json={"week": "2026-W29", "text": "the text"})
    assert r.status_code == 200  # best-effort: local-save failure must not break the submit response


def test_submit_ihk_then_ihk_status_reflects_local_fields(monkeypatch, data_dir):
    monkeypatch.setattr(
        ihk_submitter, "submit_week", lambda week_id, text, ausbinhalt1=None, ausbinhalt2=None: None
    )
    monkeypatch.setattr(ihk_submitter, "sync_status", lambda: None)
    r = client.post(
        "/api/submit-ihk",
        json={"week": "2026-W29", "text": "the text", "ausbinhalt1": "worked on X", "ausbinhalt2": "training Y"},
    )
    assert r.status_code == 200

    r = client.get("/api/ihk-status")
    assert r.json()["fields"]["2026-W29"]["ausbinhalt1"] == "worked on X"
    assert r.json()["fields"]["2026-W29"]["ausbinhalt2"] == "training Y"
