import datetime as dt
import json

import pytest

from app import config
from app import scraper
from app import storage


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
def fake_untis(monkeypatch, user_settings):
    """Stub out network I/O in UntisClient; user_settings already carries
    real UNTIS_USER/PASS seeded via the real PUT /api/me/settings call."""
    monkeypatch.setattr(scraper.UntisClient, "login", lambda self: None)
    monkeypatch.setattr(scraper.UntisClient, "logout", lambda self: None)
    return user_settings


def _saved(user_settings, week_id):
    return storage.load_week_data(user_settings.user_id, week_id)


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

    result = scraper.scrape_week("2026-W29", settings=fake_untis)

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

    assert _saved(fake_untis, "2026-W29") == result


def test_scrape_week_no_lessons_returns_empty_days_without_saving(monkeypatch, fake_untis):
    monkeypatch.setattr(scraper.UntisClient, "timetable", lambda self, s, e: [])
    monkeypatch.setattr(scraper.UntisClient, "holidays", lambda self: [])  # not a holiday either
    monkeypatch.setattr(scraper.UntisClient, "school_years", lambda self: [])  # not a gap either

    result = scraper.scrape_week("2026-W29", settings=fake_untis)

    assert result["days"] == []
    assert "holiday" not in result
    assert _saved(fake_untis, "2026-W29") is None


def test_untis_client_requires_credentials(monkeypatch):
    monkeypatch.setattr(config, "UNTIS_USER", "")
    monkeypatch.setattr(config, "UNTIS_PASS", "")
    with pytest.raises(scraper.ScrapeError):
        scraper.UntisClient()


def test_holidays_returns_list(monkeypatch):
    monkeypatch.setattr(config, "UNTIS_USER", "u")
    monkeypatch.setattr(config, "UNTIS_PASS", "p")
    client = scraper.UntisClient()
    monkeypatch.setattr(client, "_rpc", lambda method, params: [{"name": "Sommerferien"}])
    assert client.holidays() == [{"name": "Sommerferien"}]


def test_holidays_raises_on_unexpected_shape(monkeypatch):
    monkeypatch.setattr(config, "UNTIS_USER", "u")
    monkeypatch.setattr(config, "UNTIS_PASS", "p")
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

    result = scraper.scrape_week("2026-W29", settings=fake_untis)

    assert result["days"] == []
    assert result["holiday"] is True
    assert _saved(fake_untis, "2026-W29") == result


def test_scrape_week_partial_holiday_not_flagged_and_not_saved(monkeypatch, fake_untis):
    monday, sunday = scraper.week_bounds("2026-W29")
    wednesday = monday + dt.timedelta(days=2)
    monkeypatch.setattr(scraper.UntisClient, "timetable", lambda self, s, e: [])
    monkeypatch.setattr(
        scraper.UntisClient,
        "holidays",
        lambda self: [{"startDate": int(monday.strftime("%Y%m%d")), "endDate": int(wednesday.strftime("%Y%m%d"))}],
    )
    monkeypatch.setattr(scraper.UntisClient, "school_years", lambda self: [])  # not a gap either

    result = scraper.scrape_week("2026-W29", settings=fake_untis)

    assert "holiday" not in result
    assert _saved(fake_untis, "2026-W29") is None


def test_scrape_week_does_not_call_holidays_when_periods_present(monkeypatch, fake_untis):
    monday, _ = scraper.week_bounds("2026-W29")
    periods = [_period(monday, "MATH", 800, 845)]
    monkeypatch.setattr(scraper.UntisClient, "timetable", lambda self, s, e: periods)
    monkeypatch.setattr(scraper.UntisClient, "teaching_content", lambda self, d, s, e: "content")
    calls = []
    monkeypatch.setattr(scraper.UntisClient, "holidays", lambda self: calls.append(1) or [])

    scraper.scrape_week("2026-W29", settings=fake_untis)

    assert calls == []


def _raise_boundary(self, start, end):
    raise scraper.ScrapeError("school year boundary", code=scraper.SCHOOL_YEAR_BOUNDARY_CODE)


def test_rpc_error_carries_webuntis_code(monkeypatch):
    monkeypatch.setattr(config, "UNTIS_USER", "u")
    monkeypatch.setattr(config, "UNTIS_PASS", "p")
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
    monkeypatch.setattr(scraper.UntisClient, "school_years", lambda self: [])  # can't confirm the gap either

    result = scraper.scrape_week("2026-W29", settings=fake_untis)

    assert result["schoolYearBoundary"] is True
    assert "holiday" not in result
    assert _saved(fake_untis, "2026-W29") == result


def test_scrape_week_school_year_boundary_but_confirmed_holiday_prefers_holiday_flag(monkeypatch, fake_untis):
    monday, sunday = scraper.week_bounds("2026-W29")
    monkeypatch.setattr(scraper.UntisClient, "timetable", _raise_boundary)
    monkeypatch.setattr(
        scraper.UntisClient,
        "holidays",
        lambda self: [{"startDate": int(monday.strftime("%Y%m%d")), "endDate": int(sunday.strftime("%Y%m%d"))}],
    )

    result = scraper.scrape_week("2026-W29", settings=fake_untis)

    assert result["holiday"] is True
    assert "schoolYearBoundary" not in result


def test_scrape_week_school_year_boundary_does_not_overwrite_existing_data(monkeypatch, fake_untis):
    existing = {"week": "2026-W29", "days": [{"date": "2026-07-13", "lessons": []}]}
    storage.save_week_data(fake_untis.user_id, "2026-W29", existing)
    monkeypatch.setattr(scraper.UntisClient, "timetable", _raise_boundary)
    monkeypatch.setattr(scraper.UntisClient, "holidays", lambda self: [])
    monkeypatch.setattr(scraper.UntisClient, "school_years", lambda self: [])

    result = scraper.scrape_week("2026-W29", settings=fake_untis)

    assert result["schoolYearBoundary"] is True
    assert _saved(fake_untis, "2026-W29") == existing  # untouched


def test_scrape_week_other_scrape_errors_still_propagate(monkeypatch, fake_untis):
    def raise_other(self, start, end):
        raise scraper.ScrapeError("some other failure", code=-1234)

    monkeypatch.setattr(scraper.UntisClient, "timetable", raise_other)

    with pytest.raises(scraper.ScrapeError):
        scraper.scrape_week("2026-W29", settings=fake_untis)


def _raise_holidays_error(self):
    raise scraper.ScrapeError("no current school year", code=-8998)


def test_scrape_week_boundary_saved_even_if_holidays_call_also_fails(monkeypatch, fake_untis):
    monkeypatch.setattr(scraper.UntisClient, "timetable", _raise_boundary)
    monkeypatch.setattr(scraper.UntisClient, "holidays", _raise_holidays_error)
    monkeypatch.setattr(scraper.UntisClient, "school_years", _raise_holidays_error)  # totally unconfirmable

    result = scraper.scrape_week("2026-W29", settings=fake_untis)

    assert result["schoolYearBoundary"] is True
    assert "holiday" not in result
    assert _saved(fake_untis, "2026-W29") == result


def test_scrape_week_plain_empty_week_not_saved_if_holidays_call_fails(monkeypatch, fake_untis):
    monkeypatch.setattr(scraper.UntisClient, "timetable", lambda self, s, e: [])
    monkeypatch.setattr(scraper.UntisClient, "holidays", _raise_holidays_error)
    monkeypatch.setattr(scraper.UntisClient, "school_years", lambda self: [])  # not a gap either

    result = scraper.scrape_week("2026-W29", settings=fake_untis)

    assert "holiday" not in result
    assert "schoolYearBoundary" not in result
    assert _saved(fake_untis, "2026-W29") is None


def test_school_years_returns_list(monkeypatch):
    monkeypatch.setattr(config, "UNTIS_USER", "u")
    monkeypatch.setattr(config, "UNTIS_PASS", "p")
    client = scraper.UntisClient()
    monkeypatch.setattr(client, "_rpc", lambda method, params: [{"name": "2025/2026"}])
    assert client.school_years() == [{"name": "2025/2026"}]


def test_school_years_raises_on_unexpected_shape(monkeypatch):
    monkeypatch.setattr(config, "UNTIS_USER", "u")
    monkeypatch.setattr(config, "UNTIS_PASS", "p")
    client = scraper.UntisClient()
    monkeypatch.setattr(client, "_rpc", lambda method, params: {"unexpected": True})
    with pytest.raises(scraper.ScrapeError):
        client.school_years()


def test_is_between_school_years_true_in_the_gap():
    monday, sunday = scraper.week_bounds("2026-W29")
    prev_year_end = monday - dt.timedelta(days=1)
    next_year_start = sunday + dt.timedelta(days=1)
    school_years = [
        {"startDate": 20250801, "endDate": int(prev_year_end.strftime("%Y%m%d"))},
        {"startDate": int(next_year_start.strftime("%Y%m%d")), "endDate": 20270731},
    ]
    assert scraper._is_between_school_years(monday, sunday, school_years)


def test_is_between_school_years_false_when_weekday_inside_a_school_year():
    monday, sunday = scraper.week_bounds("2026-W29")
    school_years = [{"startDate": 20250801, "endDate": int(sunday.strftime("%Y%m%d"))}]
    assert not scraper._is_between_school_years(monday, sunday, school_years)


def test_is_between_school_years_false_when_empty():
    monday, sunday = scraper.week_bounds("2026-W29")
    assert not scraper._is_between_school_years(monday, sunday, [])


def test_scrape_week_boundary_confirmed_by_school_year_gap_is_flagged_holiday(monkeypatch, fake_untis):
    monday, sunday = scraper.week_bounds("2026-W29")
    prev_year_end = monday - dt.timedelta(days=1)
    next_year_start = sunday + dt.timedelta(days=1)
    monkeypatch.setattr(scraper.UntisClient, "timetable", _raise_boundary)
    monkeypatch.setattr(scraper.UntisClient, "holidays", _raise_holidays_error)
    monkeypatch.setattr(
        scraper.UntisClient,
        "school_years",
        lambda self: [
            {"startDate": 20250801, "endDate": int(prev_year_end.strftime("%Y%m%d"))},
            {"startDate": int(next_year_start.strftime("%Y%m%d")), "endDate": 20270731},
        ],
    )

    result = scraper.scrape_week("2026-W29", settings=fake_untis)

    assert result["holiday"] is True
    assert "schoolYearBoundary" not in result
    assert _saved(fake_untis, "2026-W29") == result


def test_scrape_week_boundary_gap_confirmed_does_not_overwrite_existing_data(monkeypatch, fake_untis):
    monday, sunday = scraper.week_bounds("2026-W29")
    prev_year_end = monday - dt.timedelta(days=1)
    next_year_start = sunday + dt.timedelta(days=1)
    existing = {"week": "2026-W29", "days": [{"date": "2026-07-13", "lessons": []}]}
    storage.save_week_data(fake_untis.user_id, "2026-W29", existing)
    monkeypatch.setattr(scraper.UntisClient, "timetable", _raise_boundary)
    monkeypatch.setattr(scraper.UntisClient, "holidays", _raise_holidays_error)
    monkeypatch.setattr(
        scraper.UntisClient,
        "school_years",
        lambda self: [
            {"startDate": 20250801, "endDate": int(prev_year_end.strftime("%Y%m%d"))},
            {"startDate": int(next_year_start.strftime("%Y%m%d")), "endDate": 20270731},
        ],
    )

    result = scraper.scrape_week("2026-W29", settings=fake_untis)

    assert result["holiday"] is True
    assert _saved(fake_untis, "2026-W29") == existing  # untouched


def test_has_real_lessons_false_for_missing_file(user_settings):
    assert not storage._has_real_lessons(user_settings.user_id, "2026-W29")


def test_has_real_lessons_false_for_placeholder(user_settings):
    storage.save_week_data(user_settings.user_id, "2026-W29", {"days": [], "schoolYearBoundary": True})
    assert not storage._has_real_lessons(user_settings.user_id, "2026-W29")


def test_has_real_lessons_true_for_real_data(user_settings):
    storage.save_week_data(user_settings.user_id, "2026-W29", {"days": [{"date": "x", "lessons": []}]})
    assert storage._has_real_lessons(user_settings.user_id, "2026-W29")


def test_scrape_week_gap_confirmed_holiday_overwrites_stale_placeholder(monkeypatch, fake_untis):
    monday, sunday = scraper.week_bounds("2026-W29")
    prev_year_end = monday - dt.timedelta(days=1)
    next_year_start = sunday + dt.timedelta(days=1)
    stale = {
        "week": "2026-W29",
        "start": monday.isoformat(),
        "end": sunday.isoformat(),
        "scrapedAt": "2026-01-01T00:00:00",
        "days": [],
        "schoolYearBoundary": True,
    }
    storage.save_week_data(fake_untis.user_id, "2026-W29", stale)
    monkeypatch.setattr(scraper.UntisClient, "timetable", _raise_boundary)
    monkeypatch.setattr(scraper.UntisClient, "holidays", _raise_holidays_error)
    monkeypatch.setattr(
        scraper.UntisClient,
        "school_years",
        lambda self: [
            {"startDate": 20250801, "endDate": int(prev_year_end.strftime("%Y%m%d"))},
            {"startDate": int(next_year_start.strftime("%Y%m%d")), "endDate": 20270731},
        ],
    )

    result = scraper.scrape_week("2026-W29", settings=fake_untis)

    assert result["holiday"] is True
    assert _saved(fake_untis, "2026-W29") == result  # stale guess got replaced with the better-informed answer


def test_scrape_week_all_cancelled_periods_checks_holidays(monkeypatch, fake_untis):
    """WebUntis can return real (non-empty) periods that are all 'cancelled' —
    e.g. a holiday week where placeholder periods still exist. This must be
    detected the same way as a raw-empty timetable."""
    monday, sunday = scraper.week_bounds("2026-W29")
    periods = [
        _period(monday, "MATH", 800, 845, code="cancelled"),
        _period(monday, "BIO", 930, 1015, code="cancelled"),
    ]
    monkeypatch.setattr(scraper.UntisClient, "timetable", lambda self, s, e: periods)
    monkeypatch.setattr(
        scraper.UntisClient,
        "holidays",
        lambda self: [{"startDate": int(monday.strftime("%Y%m%d")), "endDate": int(sunday.strftime("%Y%m%d"))}],
    )

    result = scraper.scrape_week("2026-W29", settings=fake_untis)

    assert result["holiday"] is True
    assert result["days"] == []


def test_scrape_week_all_cancelled_not_confirmed_holiday_is_flagged_all_cancelled(monkeypatch, fake_untis):
    """Real case hit live: a week sitting inside a school year (not a gap)
    where every period is still 'cancelled' — e.g. the first week of a new
    school year before the real schedule is active. Not a confirmable
    holiday, but shouldn't silently look like nothing was ever scraped."""
    monday, _ = scraper.week_bounds("2026-W29")
    periods = [_period(monday, "MATH", 800, 845, code="cancelled")]
    monkeypatch.setattr(scraper.UntisClient, "timetable", lambda self, s, e: periods)
    monkeypatch.setattr(scraper.UntisClient, "holidays", lambda self: [])
    monkeypatch.setattr(scraper.UntisClient, "school_years", lambda self: [])  # not a gap either

    result = scraper.scrape_week("2026-W29", settings=fake_untis)

    assert "holiday" not in result
    assert result["allCancelled"] is True
    assert _saved(fake_untis, "2026-W29") == result


def test_scrape_week_all_cancelled_does_not_overwrite_existing_real_data(monkeypatch, fake_untis):
    existing = {"week": "2026-W29", "days": [{"date": "2026-07-13", "lessons": []}]}
    storage.save_week_data(fake_untis.user_id, "2026-W29", existing)
    monday, _ = scraper.week_bounds("2026-W29")
    periods = [_period(monday, "MATH", 800, 845, code="cancelled")]
    monkeypatch.setattr(scraper.UntisClient, "timetable", lambda self, s, e: periods)
    monkeypatch.setattr(scraper.UntisClient, "holidays", lambda self: [])
    monkeypatch.setattr(scraper.UntisClient, "school_years", lambda self: [])

    result = scraper.scrape_week("2026-W29", settings=fake_untis)

    assert result["allCancelled"] is True
    assert _saved(fake_untis, "2026-W29") == existing  # untouched


def test_scrape_week_mixed_cancelled_and_real_periods_does_not_check_holidays(monkeypatch, fake_untis):
    monday, _ = scraper.week_bounds("2026-W29")
    periods = [
        _period(monday, "MATH", 800, 845, code="cancelled"),
        _period(monday, "BIO", 930, 1015),  # one real lesson
    ]
    monkeypatch.setattr(scraper.UntisClient, "timetable", lambda self, s, e: periods)
    monkeypatch.setattr(scraper.UntisClient, "teaching_content", lambda self, d, s, e: "content")
    calls = []
    monkeypatch.setattr(scraper.UntisClient, "holidays", lambda self: calls.append(1) or [])

    result = scraper.scrape_week("2026-W29", settings=fake_untis)

    assert calls == []
    assert len(result["days"][0]["lessons"]) == 1


def _raise_no_allowed_date(self, start, end):
    raise scraper.ScrapeError("no allowed date", code=scraper.NO_ALLOWED_DATE_CODE)


def test_scrape_week_no_allowed_date_is_saved_as_unavailable(monkeypatch, fake_untis):
    monkeypatch.setattr(scraper.UntisClient, "timetable", _raise_no_allowed_date)

    result = scraper.scrape_week("2026-W38", settings=fake_untis)

    assert result["unavailable"] is True
    assert result["days"] == []
    assert "holiday" not in result
    assert "schoolYearBoundary" not in result
    assert _saved(fake_untis, "2026-W38") == result


def test_scrape_week_no_allowed_date_does_not_overwrite_existing_data(monkeypatch, fake_untis):
    existing = {"week": "2026-W38", "days": [{"date": "2026-09-14", "lessons": []}]}
    storage.save_week_data(fake_untis.user_id, "2026-W38", existing)
    monkeypatch.setattr(scraper.UntisClient, "timetable", _raise_no_allowed_date)

    result = scraper.scrape_week("2026-W38", settings=fake_untis)

    assert result["unavailable"] is True
    assert _saved(fake_untis, "2026-W38") == existing  # untouched


def test_scrape_week_no_allowed_date_does_not_call_holidays_or_school_years(monkeypatch, fake_untis):
    monkeypatch.setattr(scraper.UntisClient, "timetable", _raise_no_allowed_date)
    calls = []
    monkeypatch.setattr(scraper.UntisClient, "holidays", lambda self: calls.append("holidays") or [])
    monkeypatch.setattr(scraper.UntisClient, "school_years", lambda self: calls.append("school_years") or [])

    scraper.scrape_week("2026-W38", settings=fake_untis)

    assert calls == []
