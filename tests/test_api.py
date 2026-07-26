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
