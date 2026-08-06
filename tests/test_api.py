import datetime as dt

import pytest

from app import ihk_submitter
from app import main
from app import scraper
from app import storage
from app.ihk_client import IhkError


def test_list_weeks_empty(new_user):
    r = new_user.client.get("/api/weeks")
    assert r.status_code == 200
    assert r.json() == {"weeks": [], "current": scraper.current_week_id(), "startWeek": None}


def test_list_weeks_finds_week_files(new_user):
    storage.save_week_data(new_user.user_id, "2026-W29", {})
    r = new_user.client.get("/api/weeks")
    assert r.json()["weeks"] == ["2026-W29"]


def test_get_week_bad_id_format(new_user):
    r = new_user.client.get("/api/weeks/nonsense")
    assert r.status_code == 400


def test_get_week_missing(new_user):
    r = new_user.client.get("/api/weeks/2026-W29")
    assert r.status_code == 404


def test_get_week_returns_saved_data(new_user):
    payload = {"week": "2026-W29", "days": []}
    storage.save_week_data(new_user.user_id, "2026-W29", payload)
    r = new_user.client.get("/api/weeks/2026-W29")
    assert r.status_code == 200
    assert r.json() == payload


def test_scrape_bad_week_format(new_user):
    r = new_user.client.post("/api/scrape", json={"week": "bad"})
    assert r.status_code == 400


def test_scrape_defaults_to_current_week(new_user, monkeypatch):
    monkeypatch.setattr(scraper, "scrape_week", lambda week_id, settings=None: {"week": week_id})
    r = new_user.client.post("/api/scrape", json={})
    assert r.status_code == 200
    assert r.json() == {"week": scraper.current_week_id()}


def test_scrape_success_with_explicit_week(new_user, monkeypatch):
    calls = []
    monkeypatch.setattr(
        scraper, "scrape_week", lambda week_id, settings=None: calls.append(week_id) or {"week": week_id}
    )
    r = new_user.client.post("/api/scrape", json={"week": "2026-W29"})
    assert r.status_code == 200
    assert r.json() == {"week": "2026-W29"}
    assert calls == ["2026-W29"]


def test_scrape_error_maps_to_502(new_user, monkeypatch):
    def raiser(week_id, settings=None):
        raise scraper.ScrapeError("webuntis said no")

    monkeypatch.setattr(scraper, "scrape_week", raiser)
    r = new_user.client.post("/api/scrape", json={"week": "2026-W29"})
    assert r.status_code == 502


def test_scrape_unexpected_exception_maps_to_500(new_user, monkeypatch):
    def raiser(week_id, settings=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(scraper, "scrape_week", raiser)
    r = new_user.client.post("/api/scrape", json={"week": "2026-W29"})
    assert r.status_code == 500


def test_scrape_returns_409_when_already_running(new_user):
    lock = main._get_lock(new_user.user_id, "scrape")
    assert lock.acquire(blocking=False)
    try:
        r = new_user.client.post("/api/scrape", json={"week": "2026-W29"})
        assert r.status_code == 409
    finally:
        lock.release()


def test_scrape_due_on_sunday_at_scrape_time():
    now = dt.datetime(2026, 8, 2, 18, 0)  # a Sunday
    assert main._scrape_due(now, None, "sun", 18, 0)


def test_scrape_due_on_sunday_after_scrape_time():
    now = dt.datetime(2026, 8, 2, 23, 59)
    assert main._scrape_due(now, None, "sun", 18, 0)


def test_scrape_not_due_before_scrape_time():
    now = dt.datetime(2026, 8, 2, 17, 59)
    assert not main._scrape_due(now, None, "sun", 18, 0)


def test_scrape_not_due_on_other_weekdays():
    now = dt.datetime(2026, 8, 3, 18, 0)  # Monday
    assert not main._scrape_due(now, None, "sun", 18, 0)


def test_scrape_not_due_twice_same_day():
    now = dt.datetime(2026, 8, 2, 20, 0)
    assert not main._scrape_due(now, now.date(), "sun", 18, 0)


def test_scrape_due_again_next_week_after_last_run():
    now = dt.datetime(2026, 8, 2, 18, 0)
    assert main._scrape_due(now, dt.date(2026, 7, 26), "sun", 18, 0)


def test_weeks_to_scrape_returns_previous_and_current_week():
    today = dt.date(2026, 8, 2)  # a Sunday
    assert main._weeks_to_scrape(today) == ["2026-W30", "2026-W31"]


def test_scheduled_scrape_attempts_both_weeks(new_user, monkeypatch):
    calls = []
    monkeypatch.setattr(scraper, "scrape_week", lambda week_id, settings=None: calls.append(week_id))
    main._scheduled_scrape(new_user.user_id, dt.date(2026, 8, 2))
    assert calls == ["2026-W30", "2026-W31"]


def test_scheduled_scrape_still_attempts_second_week_if_first_fails(new_user, monkeypatch):
    calls = []

    def fake_scrape_week(week_id, settings=None):
        calls.append(week_id)
        if week_id == "2026-W30":
            raise scraper.ScrapeError("boom")

    monkeypatch.setattr(scraper, "scrape_week", fake_scrape_week)
    main._scheduled_scrape(new_user.user_id, dt.date(2026, 8, 2))  # must not raise
    assert calls == ["2026-W30", "2026-W31"]


def test_ihk_status_returns_load_status(new_user, monkeypatch):
    monkeypatch.setattr(
        ihk_submitter, "load_status", lambda settings=None: {"2026-W29": {"lfdnr": 42, "status": "genehmigt"}}
    )
    monkeypatch.setattr(
        ihk_submitter, "load_local_fields", lambda settings=None: {"2026-W29": {"ausbinhalt1": "x"}}
    )
    r = new_user.client.get("/api/ihk-status")
    assert r.status_code == 200
    assert r.json() == {
        "status": {"2026-W29": {"lfdnr": 42, "status": "genehmigt"}},
        "fields": {"2026-W29": {"ausbinhalt1": "x"}},
    }


def test_submit_ihk_bad_week_format(new_user):
    r = new_user.client.post("/api/submit-ihk", json={"week": "bad", "text": "hi"})
    assert r.status_code == 400


def test_submit_ihk_rejects_empty_text(new_user):
    r = new_user.client.post("/api/submit-ihk", json={"week": "2026-W29", "text": "   "})
    assert r.status_code == 400


def test_submit_ihk_success(new_user, monkeypatch):
    calls = []
    monkeypatch.setattr(
        ihk_submitter,
        "submit_week",
        lambda week_id, text, ausbinhalt1=None, ausbinhalt2=None, settings=None: calls.append(
            (week_id, text, ausbinhalt1, ausbinhalt2)
        ),
    )
    monkeypatch.setattr(ihk_submitter, "save_local_fields", lambda week_id, a1=None, a2=None, settings=None: None)
    monkeypatch.setattr(ihk_submitter, "sync_status", lambda settings=None: None)
    r = new_user.client.post("/api/submit-ihk", json={"week": "2026-W29", "text": "the text"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert calls == [("2026-W29", "the text", None, None)]


def test_submit_ihk_passes_through_ausbinhalt1_and_2(new_user, monkeypatch):
    calls = []
    monkeypatch.setattr(
        ihk_submitter,
        "submit_week",
        lambda week_id, text, ausbinhalt1=None, ausbinhalt2=None, settings=None: calls.append(
            (week_id, text, ausbinhalt1, ausbinhalt2)
        ),
    )
    monkeypatch.setattr(ihk_submitter, "save_local_fields", lambda week_id, a1=None, a2=None, settings=None: None)
    monkeypatch.setattr(ihk_submitter, "sync_status", lambda settings=None: None)
    r = new_user.client.post(
        "/api/submit-ihk",
        json={"week": "2026-W29", "text": "the text", "ausbinhalt1": "worked on X", "ausbinhalt2": "training Y"},
    )
    assert r.status_code == 200
    assert calls == [("2026-W29", "the text", "worked on X", "training Y")]


def test_submit_ihk_error_maps_to_502(new_user, monkeypatch):
    def raiser(week_id, text, ausbinhalt1=None, ausbinhalt2=None, settings=None):
        raise IhkError("ihk said no")

    monkeypatch.setattr(ihk_submitter, "submit_week", raiser)
    r = new_user.client.post("/api/submit-ihk", json={"week": "2026-W29", "text": "the text"})
    assert r.status_code == 502


def test_submit_ihk_unexpected_exception_maps_to_500(new_user, monkeypatch):
    def raiser(week_id, text, ausbinhalt1=None, ausbinhalt2=None, settings=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(ihk_submitter, "submit_week", raiser)
    r = new_user.client.post("/api/submit-ihk", json={"week": "2026-W29", "text": "the text"})
    assert r.status_code == 500


def test_submit_ihk_returns_409_when_already_running(new_user):
    lock = main._get_lock(new_user.user_id, "submit")
    assert lock.acquire(blocking=False)
    try:
        r = new_user.client.post("/api/submit-ihk", json={"week": "2026-W29", "text": "the text"})
        assert r.status_code == 409
    finally:
        lock.release()


def test_scrape_success_also_syncs_ihk_status_best_effort(new_user, monkeypatch):
    monkeypatch.setattr(scraper, "scrape_week", lambda week_id, settings=None: {"week": week_id})
    synced = []
    monkeypatch.setattr(ihk_submitter, "sync_status", lambda settings=None: synced.append(True))
    r = new_user.client.post("/api/scrape", json={"week": "2026-W29"})
    assert r.status_code == 200
    assert synced == [True]


def test_scrape_succeeds_even_if_ihk_status_sync_fails(new_user, monkeypatch):
    monkeypatch.setattr(scraper, "scrape_week", lambda week_id, settings=None: {"week": week_id})

    def raiser(settings=None):
        raise RuntimeError("ihk portal unreachable")

    monkeypatch.setattr(ihk_submitter, "sync_status", raiser)
    r = new_user.client.post("/api/scrape", json={"week": "2026-W29"})
    assert r.status_code == 200  # best-effort: sync failure must not break the scrape response


def test_submit_ihk_success_also_syncs_ihk_status_best_effort(new_user, monkeypatch):
    monkeypatch.setattr(
        ihk_submitter,
        "submit_week",
        lambda week_id, text, ausbinhalt1=None, ausbinhalt2=None, settings=None: None,
    )
    monkeypatch.setattr(ihk_submitter, "save_local_fields", lambda week_id, a1=None, a2=None, settings=None: None)
    synced = []
    monkeypatch.setattr(ihk_submitter, "sync_status", lambda settings=None: synced.append(True))
    r = new_user.client.post("/api/submit-ihk", json={"week": "2026-W29", "text": "the text"})
    assert r.status_code == 200
    assert synced == [True]


def test_submit_ihk_success_also_saves_local_fields_best_effort(new_user, monkeypatch):
    monkeypatch.setattr(
        ihk_submitter,
        "submit_week",
        lambda week_id, text, ausbinhalt1=None, ausbinhalt2=None, settings=None: None,
    )
    monkeypatch.setattr(ihk_submitter, "sync_status", lambda settings=None: None)
    saved = []
    monkeypatch.setattr(
        ihk_submitter,
        "save_local_fields",
        lambda week_id, ausbinhalt1=None, ausbinhalt2=None, settings=None: saved.append(
            (week_id, ausbinhalt1, ausbinhalt2)
        ),
    )
    r = new_user.client.post(
        "/api/submit-ihk",
        json={"week": "2026-W29", "text": "the text", "ausbinhalt1": "worked on X", "ausbinhalt2": "training Y"},
    )
    assert r.status_code == 200
    assert saved == [("2026-W29", "worked on X", "training Y")]


def test_submit_ihk_succeeds_even_if_local_fields_save_fails(new_user, monkeypatch):
    monkeypatch.setattr(
        ihk_submitter,
        "submit_week",
        lambda week_id, text, ausbinhalt1=None, ausbinhalt2=None, settings=None: None,
    )
    monkeypatch.setattr(ihk_submitter, "sync_status", lambda settings=None: None)

    def raiser(week_id, ausbinhalt1=None, ausbinhalt2=None, settings=None):
        raise RuntimeError("disk full")

    monkeypatch.setattr(ihk_submitter, "save_local_fields", raiser)
    r = new_user.client.post("/api/submit-ihk", json={"week": "2026-W29", "text": "the text"})
    assert r.status_code == 200  # best-effort: local-save failure must not break the submit response


def test_submit_ihk_then_ihk_status_reflects_local_fields(new_user, monkeypatch):
    monkeypatch.setattr(
        ihk_submitter,
        "submit_week",
        lambda week_id, text, ausbinhalt1=None, ausbinhalt2=None, settings=None: None,
    )
    monkeypatch.setattr(ihk_submitter, "sync_status", lambda settings=None: None)
    r = new_user.client.post(
        "/api/submit-ihk",
        json={"week": "2026-W29", "text": "the text", "ausbinhalt1": "worked on X", "ausbinhalt2": "training Y"},
    )
    assert r.status_code == 200

    r = new_user.client.get("/api/ihk-status")
    assert r.json()["fields"]["2026-W29"]["ausbinhalt1"] == "worked on X"
    assert r.json()["fields"]["2026-W29"]["ausbinhalt2"] == "training Y"


# ===== Auth / multi-user isolation =====

def test_protected_route_401_without_session_cookie(unauth_client):
    r = unauth_client.get("/api/weeks")
    assert r.status_code == 401


def test_create_user_403_for_non_admin(new_user):
    r = new_user.client.post("/api/admin/users", json={"username": "someone-else", "password": "x"})
    assert r.status_code == 403


def test_week_data_is_isolated_per_user(new_user, admin_client):
    storage.save_week_data(new_user.user_id, "2026-W29", {"week": "2026-W29", "days": []})

    r = admin_client.get("/api/weeks/2026-W29")
    assert r.status_code == 404  # admin's own (empty) data, not new_user's


def test_logout_invalidates_session_server_side(new_user):
    session_id = new_user.client.cookies.get("session")
    assert session_id

    r = new_user.client.post("/api/auth/logout")
    assert r.status_code == 200

    # Replay the exact session id post-logout (bypassing the client's own
    # cookie jar, which already dropped it) - must be rejected server-side,
    # not just cleared client-side.
    from fastapi.testclient import TestClient
    replay_client = TestClient(main.app)
    replay_client.cookies.set("session", session_id)
    r = replay_client.get("/api/auth/whoami")
    assert r.status_code == 401
