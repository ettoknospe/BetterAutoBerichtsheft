"""WebUntis scraper - plain HTTP, no browser.

Flow:
  1. JSON-RPC authenticate -> session cookie + personId
  2. /WebUntis/api/token/new -> bearer token for REST endpoints
  3. JSON-RPC getTimetable -> periods of the week
  4. per period: REST calendar-entry/detail -> teachingContent (Lehrstoff)

On unexpected API responses the raw payload is dumped to DATA_DIR/debug/
so a failing first run can be diagnosed without re-running blind.

Module layout: this file owns orchestration (scrape_week). Runtime config
lives in config.py; the HTTP/RPC client is in untis_client.py; pure date
helpers are in time_utils.py and school_calendar.py; local file I/O is in
storage.py. Each of those is a one-way import of config.py - no module here
imports another back, so there's no import-order fragility.
"""

import datetime as dt
import json
import logging

import config
from time_utils import _hm, week_bounds, current_week_id
from school_calendar import _is_full_holiday_week, _is_between_school_years
from storage import _dump_debug, _has_real_lessons
from untis_client import (
    ScrapeError,
    UntisClient,
    SCHOOL_YEAR_BOUNDARY_CODE,
    NO_ALLOWED_DATE_CODE,
)

log = logging.getLogger("scraper")


def scrape_week(week_id: str) -> dict:
    monday, sunday = week_bounds(week_id)
    log.info("scraping %s (%s .. %s)", week_id, monday, sunday)

    client = UntisClient()
    client.login()
    holiday_periods = None
    school_years_list = None
    school_year_boundary = False
    unavailable = False
    try:
        try:
            periods = client.timetable(monday, sunday)
        except ScrapeError as e:
            if e.code == SCHOOL_YEAR_BOUNDARY_CODE:
                # week straddles a school-year boundary — WebUntis refuses the
                # query outright. Not a real error, treat like an empty week
                # and check below whether it's also an actual holiday.
                periods = []
                school_year_boundary = True
            elif e.code == NO_ALLOWED_DATE_CODE:
                # date is beyond WebUntis's publish horizon — nothing to check,
                # not fixable right now, just surface it plainly.
                periods = []
                unavailable = True
            else:
                raise

        # group double lessons: one entry per (date, subject, start) after sort
        lessons = []
        for p in periods:
            if p.get("code") == "cancelled":
                continue
            date = dt.datetime.strptime(str(p["date"]), "%Y%m%d").date()
            subjects = p.get("su") or []
            subj = subjects[0].get("name", "?") if subjects else "?"
            subj_long = subjects[0].get("longname", "") if subjects else ""
            teachers = p.get("te") or []
            teacher = teachers[0].get("longname") or teachers[0].get("name", "") if teachers else ""
            lessons.append(
                {
                    "date": date.isoformat(),
                    "start": _hm(p["startTime"]),
                    "end": _hm(p["endTime"]),
                    "subject": subj,
                    "subjectLong": subj_long,
                    "teacher": teacher,
                    "content": "",
                }
            )
        lessons.sort(key=lambda l: (l["date"], l["start"]))

        if not lessons and not unavailable:
            # no usable (non-cancelled) periods at all — either a real holiday
            # (WebUntis returns cancelled placeholder periods through it) or a
            # school-year-boundary week. Check holidays before logging out,
            # while the session is still valid.
            try:
                holiday_periods = client.holidays()
            except ScrapeError:
                # can also fail right at a school-year boundary (WebUntis has no
                # "current" school year yet) — leave unconfirmed, not fatal.
                log.warning("getHolidays failed for %s, leaving holiday status unconfirmed", week_id)

        confirmed_holiday = holiday_periods and _is_full_holiday_week(monday, sunday, holiday_periods)
        if not lessons and not unavailable and not confirmed_holiday:
            # getHolidays can be unconfirmed or fail entirely right at a
            # school-year transition — fall back to checking whether the week
            # sits in the gap between two school years (derived from WebUntis
            # data, no hardcoded dates).
            try:
                school_years_list = client.school_years()
            except ScrapeError:
                log.warning("getSchoolyears failed for %s", week_id)

        if config.SUBJECT_FILTER:
            lessons = [l for l in lessons if l["subject"] in config.SUBJECT_FILTER]

        for lesson in lessons:
            date = dt.date.fromisoformat(lesson["date"])
            lesson["content"] = client.teaching_content(date, lesson["start"], lesson["end"])
    finally:
        client.logout()

    # merge consecutive periods of same subject+day with identical content
    merged = []
    for lesson in lessons:
        prev = merged[-1] if merged else None
        if (
            prev
            and prev["date"] == lesson["date"]
            and prev["subject"] == lesson["subject"]
            and prev["content"] == lesson["content"]
        ):
            prev["end"] = lesson["end"]
        else:
            merged.append(lesson)

    days = []
    for offset in range(7):
        date = (monday + dt.timedelta(days=offset)).isoformat()
        day_lessons = [l for l in merged if l["date"] == date]
        if day_lessons:
            days.append({"date": date, "lessons": day_lessons})

    result = {
        "week": week_id,
        "start": monday.isoformat(),
        "end": sunday.isoformat(),
        "scrapedAt": dt.datetime.now().isoformat(timespec="seconds"),
        "days": days,
    }

    if not days:
        if unavailable:
            result["unavailable"] = True
            out = config.DATA_DIR / f"{week_id}.json"
            if _has_real_lessons(out):
                log.info("%s already has real lesson data — not overwriting with unavailable marker", week_id)
            else:
                config.DATA_DIR.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
                log.info("saved %s (beyond WebUntis publish horizon)", out)
            return result
        if holiday_periods and _is_full_holiday_week(monday, sunday, holiday_periods):
            result["holiday"] = True
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            out = config.DATA_DIR / f"{week_id}.json"
            out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
            log.info("saved %s (holiday week)", out)
            return result
        if school_years_list and _is_between_school_years(monday, sunday, school_years_list):
            result["holiday"] = True
            out = config.DATA_DIR / f"{week_id}.json"
            if _has_real_lessons(out):
                log.info("%s already has real lesson data — not overwriting", week_id)
            else:
                config.DATA_DIR.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
                log.info("saved %s (between school years, treated as holiday)", out)
            return result
        if school_year_boundary:
            result["schoolYearBoundary"] = True
            out = config.DATA_DIR / f"{week_id}.json"
            if _has_real_lessons(out):
                log.info("%s already has real lesson data — not overwriting with school-year-boundary marker", week_id)
            else:
                config.DATA_DIR.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
                log.info("saved %s (school-year boundary, not a confirmed holiday)", out)
            return result
        if periods:
            # WebUntis returned real periods but every one was cancelled, and
            # neither getHolidays nor getSchoolyears confirmed a holiday —
            # still worth surfacing honestly instead of pretending nothing
            # was ever scraped.
            result["allCancelled"] = True
            out = config.DATA_DIR / f"{week_id}.json"
            if _has_real_lessons(out):
                log.info("%s already has real lesson data — not overwriting with allCancelled marker", week_id)
            else:
                config.DATA_DIR.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
                log.info("saved %s (all periods cancelled, not a confirmed holiday)", out)
            return result
        log.info("no lessons in %s — nothing saved", week_id)
        return result

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = config.DATA_DIR / f"{week_id}.json"
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    log.info("saved %s (%d lessons)", out, sum(len(d["lessons"]) for d in days))
    return result
