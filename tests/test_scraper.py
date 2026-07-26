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

    result = scraper.scrape_week("2026-W29")

    assert result["days"] == []
    assert not (fake_untis / "2026-W29.json").exists()


def test_untis_client_requires_credentials(monkeypatch):
    monkeypatch.setattr(scraper, "UNTIS_USER", "")
    monkeypatch.setattr(scraper, "UNTIS_PASS", "")
    with pytest.raises(scraper.ScrapeError):
        scraper.UntisClient()
