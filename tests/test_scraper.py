import datetime as dt
import json

import pytest

import scraper


def test_hm_formats_untis_time():
    assert scraper._hm(745) == "07:45"
    assert scraper._hm(1330) == "13:30"


def test_week_bounds_returns_monday_to_sunday():
    monday, sunday = scraper.week_bounds("2026-W29")
    assert monday.isocalendar()[:2] == (2026, 29)
    assert monday.weekday() == 0
    assert sunday == monday + dt.timedelta(days=6)


def test_current_week_id_matches_isocalendar():
    today = dt.date(2026, 1, 5)
    iso = today.isocalendar()
    assert scraper.current_week_id(today) == f"{iso.year}-W{iso.week:02d}"


def _period(date, subject, start, end, code=None, teacher="Teacher"):
    p = {
        "date": int(date.strftime("%Y%m%d")),
        "startTime": start,
        "endTime": end,
        "su": [{"name": subject, "longname": subject}],
        "te": [{"name": teacher, "longname": teacher}],
    }
    if code:
        p["code"] = code
    return p


@pytest.fixture
def fake_untis(monkeypatch, tmp_path):
    """Stub out network I/O in UntisClient; redirect DATA_DIR to tmp_path."""
    monkeypatch.setattr(scraper, "UNTIS_USER", "u")
    monkeypatch.setattr(scraper, "UNTIS_PASS", "p")
    monkeypatch.setattr(scraper, "DATA_DIR", tmp_path)
    monkeypatch.setattr(scraper, "SUBJECT_FILTER", [])
    monkeypatch.setattr(scraper.UntisClient, "login", lambda self: None)
    monkeypatch.setattr(scraper.UntisClient, "logout", lambda self: None)
    return tmp_path


def test_scrape_week_merges_consecutive_same_content(monkeypatch, fake_untis):
    monday, _ = scraper.week_bounds("2026-W29")
    periods = [
        _period(monday, "MATH", 800, 845),
        _period(monday, "MATH", 845, 930),  # merges with above: same subject+content
        _period(monday, "BIO", 930, 1015),  # different subject: not merged
        _period(monday, "MATH", 1100, 1145, code="cancelled"),  # skipped entirely
    ]
    monkeypatch.setattr(scraper.UntisClient, "timetable", lambda self, s, e: periods)

    def fake_teaching_content(self, date, start_hm, end_hm):
        return "Same content" if start_hm in ("08:00", "08:45") else "Bio content"

    monkeypatch.setattr(scraper.UntisClient, "teaching_content", fake_teaching_content)

    result = scraper.scrape_week("2026-W29")

    assert len(result["days"]) == 1
    lessons = result["days"][0]["lessons"]
    assert len(lessons) == 2
    assert lessons[0] == {
        "date": monday.isoformat(),
        "start": "08:00",
        "end": "09:30",
        "subject": "MATH",
        "subjectLong": "MATH",
        "teacher": "Teacher",
        "content": "Same content",
    }
    assert lessons[1]["subject"] == "BIO"
    assert lessons[1]["start"] == "09:30"
    assert lessons[1]["end"] == "10:15"

    saved = json.loads((fake_untis / "2026-W29.json").read_text())
    assert saved == result


def test_scrape_week_applies_subject_filter(monkeypatch, fake_untis):
    monday, _ = scraper.week_bounds("2026-W29")
    periods = [
        _period(monday, "MATH", 800, 845),
        _period(monday, "BIO", 930, 1015),
    ]
    monkeypatch.setattr(scraper.UntisClient, "timetable", lambda self, s, e: periods)
    monkeypatch.setattr(scraper.UntisClient, "teaching_content", lambda self, d, s, e: "")
    monkeypatch.setattr(scraper, "SUBJECT_FILTER", ["BIO"])

    result = scraper.scrape_week("2026-W29")

    lessons = result["days"][0]["lessons"]
    assert len(lessons) == 1
    assert lessons[0]["subject"] == "BIO"


def test_scrape_week_no_lessons_returns_empty_days_without_saving(monkeypatch, fake_untis):
    monkeypatch.setattr(scraper.UntisClient, "timetable", lambda self, s, e: [])
    monkeypatch.setattr(scraper.UntisClient, "holidays", lambda self: [])  # not a holiday either

    result = scraper.scrape_week("2026-W29")

    assert result["days"] == []
    assert "holiday" not in result
    assert not (fake_untis / "2026-W29.json").exists()


def test_untis_client_requires_credentials(monkeypatch):
    monkeypatch.setattr(scraper, "UNTIS_USER", "")
    monkeypatch.setattr(scraper, "UNTIS_PASS", "")
    with pytest.raises(scraper.ScrapeError):
        scraper.UntisClient()


def test_holidays_returns_list(monkeypatch):
    monkeypatch.setattr(scraper, "UNTIS_USER", "u")
    monkeypatch.setattr(scraper, "UNTIS_PASS", "p")
    client = scraper.UntisClient()
    monkeypatch.setattr(client, "_rpc", lambda method, params: [{"name": "Sommerferien"}])
    assert client.holidays() == [{"name": "Sommerferien"}]


def test_holidays_raises_on_unexpected_shape(monkeypatch):
    monkeypatch.setattr(scraper, "UNTIS_USER", "u")
    monkeypatch.setattr(scraper, "UNTIS_PASS", "p")
    client = scraper.UntisClient()
    monkeypatch.setattr(client, "_rpc", lambda method, params: {"unexpected": True})
    with pytest.raises(scraper.ScrapeError):
        client.holidays()


def test_is_full_holiday_week_full_coverage():
    monday, sunday = scraper.week_bounds("2026-W29")
    periods = [{"startDate": int(monday.strftime("%Y%m%d")), "endDate": int(sunday.strftime("%Y%m%d"))}]
    assert scraper._is_full_holiday_week(monday, sunday, periods)


def test_is_full_holiday_week_partial_coverage_is_false():
    monday, sunday = scraper.week_bounds("2026-W29")
    wednesday = monday + dt.timedelta(days=2)  # only mon-wed covered
    periods = [{"startDate": int(monday.strftime("%Y%m%d")), "endDate": int(wednesday.strftime("%Y%m%d"))}]
    assert not scraper._is_full_holiday_week(monday, sunday, periods)


def test_is_full_holiday_week_two_periods_together_cover_week():
    monday, sunday = scraper.week_bounds("2026-W29")
    wednesday = monday + dt.timedelta(days=2)
    thursday = monday + dt.timedelta(days=3)
    friday = monday + dt.timedelta(days=4)
    periods = [
        {"startDate": int(monday.strftime("%Y%m%d")), "endDate": int(wednesday.strftime("%Y%m%d"))},
        {"startDate": int(thursday.strftime("%Y%m%d")), "endDate": int(friday.strftime("%Y%m%d"))},
    ]
    assert scraper._is_full_holiday_week(monday, sunday, periods)


def test_scrape_week_full_holiday_is_saved_and_flagged(monkeypatch, fake_untis):
    monday, sunday = scraper.week_bounds("2026-W29")
    monkeypatch.setattr(scraper.UntisClient, "timetable", lambda self, s, e: [])
    monkeypatch.setattr(
        scraper.UntisClient,
        "holidays",
        lambda self: [{"startDate": int(monday.strftime("%Y%m%d")), "endDate": int(sunday.strftime("%Y%m%d"))}],
    )

    result = scraper.scrape_week("2026-W29")

    assert result["days"] == []
    assert result["holiday"] is True
    saved = json.loads((fake_untis / "2026-W29.json").read_text())
    assert saved == result


def test_scrape_week_partial_holiday_not_flagged_and_not_saved(monkeypatch, fake_untis):
    monday, sunday = scraper.week_bounds("2026-W29")
    wednesday = monday + dt.timedelta(days=2)
    monkeypatch.setattr(scraper.UntisClient, "timetable", lambda self, s, e: [])
    monkeypatch.setattr(
        scraper.UntisClient,
        "holidays",
        lambda self: [{"startDate": int(monday.strftime("%Y%m%d")), "endDate": int(wednesday.strftime("%Y%m%d"))}],
    )

    result = scraper.scrape_week("2026-W29")

    assert "holiday" not in result
    assert not (fake_untis / "2026-W29.json").exists()


def test_scrape_week_does_not_call_holidays_when_periods_present(monkeypatch, fake_untis):
    monday, _ = scraper.week_bounds("2026-W29")
    periods = [_period(monday, "MATH", 800, 845)]
    monkeypatch.setattr(scraper.UntisClient, "timetable", lambda self, s, e: periods)
    monkeypatch.setattr(scraper.UntisClient, "teaching_content", lambda self, d, s, e: "content")
    calls = []
    monkeypatch.setattr(scraper.UntisClient, "holidays", lambda self: calls.append(1) or [])

    scraper.scrape_week("2026-W29")

    assert calls == []


def _raise_boundary(self, start, end):
    raise scraper.ScrapeError("school year boundary", code=scraper.SCHOOL_YEAR_BOUNDARY_CODE)


def test_rpc_error_carries_webuntis_code(monkeypatch):
    monkeypatch.setattr(scraper, "UNTIS_USER", "u")
    monkeypatch.setattr(scraper, "UNTIS_PASS", "p")
    client = scraper.UntisClient()

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"error": {"code": -8507, "message": "boom"}}

    monkeypatch.setattr(client, "s", type("S", (), {"post": staticmethod(lambda *a, **k: FakeResponse())})())

    with pytest.raises(scraper.ScrapeError) as exc_info:
        client._rpc("getTimetable", {})
    assert exc_info.value.code == -8507


def test_scrape_week_school_year_boundary_not_confirmed_holiday_is_saved(monkeypatch, fake_untis):
    monkeypatch.setattr(scraper.UntisClient, "timetable", _raise_boundary)
    monkeypatch.setattr(scraper.UntisClient, "holidays", lambda self: [])  # not actually a holiday

    result = scraper.scrape_week("2026-W29")

    assert result["schoolYearBoundary"] is True
    assert "holiday" not in result
    saved = json.loads((fake_untis / "2026-W29.json").read_text())
    assert saved == result


def test_scrape_week_school_year_boundary_but_confirmed_holiday_prefers_holiday_flag(monkeypatch, fake_untis):
    monday, sunday = scraper.week_bounds("2026-W29")
    monkeypatch.setattr(scraper.UntisClient, "timetable", _raise_boundary)
    monkeypatch.setattr(
        scraper.UntisClient,
        "holidays",
        lambda self: [{"startDate": int(monday.strftime("%Y%m%d")), "endDate": int(sunday.strftime("%Y%m%d"))}],
    )

    result = scraper.scrape_week("2026-W29")

    assert result["holiday"] is True
    assert "schoolYearBoundary" not in result


def test_scrape_week_school_year_boundary_does_not_overwrite_existing_data(monkeypatch, fake_untis):
    existing = {"week": "2026-W29", "days": [{"date": "2026-07-13", "lessons": []}]}
    (fake_untis / "2026-W29.json").write_text(json.dumps(existing))
    monkeypatch.setattr(scraper.UntisClient, "timetable", _raise_boundary)
    monkeypatch.setattr(scraper.UntisClient, "holidays", lambda self: [])

    result = scraper.scrape_week("2026-W29")

    assert result["schoolYearBoundary"] is True
    saved = json.loads((fake_untis / "2026-W29.json").read_text())
    assert saved == existing  # untouched on disk


def test_scrape_week_other_scrape_errors_still_propagate(monkeypatch, fake_untis):
    def raise_other(self, start, end):
        raise scraper.ScrapeError("some other failure", code=-1234)

    monkeypatch.setattr(scraper.UntisClient, "timetable", raise_other)

    with pytest.raises(scraper.ScrapeError):
        scraper.scrape_week("2026-W29")


def _raise_holidays_error(self):
    raise scraper.ScrapeError("no current school year", code=-8998)


def test_scrape_week_boundary_saved_even_if_holidays_call_also_fails(monkeypatch, fake_untis):
    monkeypatch.setattr(scraper.UntisClient, "timetable", _raise_boundary)
    monkeypatch.setattr(scraper.UntisClient, "holidays", _raise_holidays_error)

    result = scraper.scrape_week("2026-W29")

    assert result["schoolYearBoundary"] is True
    assert "holiday" not in result
    saved = json.loads((fake_untis / "2026-W29.json").read_text())
    assert saved == result


def test_scrape_week_plain_empty_week_not_saved_if_holidays_call_fails(monkeypatch, fake_untis):
    monkeypatch.setattr(scraper.UntisClient, "timetable", lambda self, s, e: [])
    monkeypatch.setattr(scraper.UntisClient, "holidays", _raise_holidays_error)

    result = scraper.scrape_week("2026-W29")

    assert "holiday" not in result
    assert "schoolYearBoundary" not in result
    assert not (fake_untis / "2026-W29.json").exists()
