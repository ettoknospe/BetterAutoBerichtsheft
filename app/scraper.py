"""WebUntis scraper — plain HTTP, no browser.

Flow:
  1. JSON-RPC authenticate -> session cookie + personId
  2. /WebUntis/api/token/new -> bearer token for REST endpoints
  3. JSON-RPC getTimetable -> periods of the week
  4. per period: REST calendar-entry/detail -> teachingContent (Lehrstoff)

On unexpected API responses the raw payload is dumped to DATA_DIR/debug/
so a failing first run can be diagnosed without re-running blind.
"""

import datetime as dt
import json
import logging
import os
from pathlib import Path

import requests

log = logging.getLogger("scraper")

UNTIS_HOST = os.environ.get("UNTIS_HOST", "le-bk-muenster.webuntis.com")
UNTIS_SCHOOL = os.environ.get("UNTIS_SCHOOL", "le-bk-muenster")
UNTIS_USER = os.environ.get("UNTIS_USER", "")
UNTIS_PASS = os.environ.get("UNTIS_PASS", "")
SUBJECT_FILTER = [s.strip() for s in os.environ.get("SUBJECT_FILTER", "").split(",") if s.strip()]
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))

STUDENT_TYPE = 5  # WebUntis element type for students
SCHOOL_YEAR_BOUNDARY_CODE = -8507  # getTimetable error when start/end span two school years
NO_ALLOWED_DATE_CODE = -7004  # getTimetable error when the date is beyond WebUntis's publish horizon


class ScrapeError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def _dump_debug(name: str, payload):
    debug_dir = DATA_DIR / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    path = debug_dir / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{name}.json"
    try:
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        log.warning("raw response dumped to %s", path)
    except Exception:
        log.exception("could not write debug dump")


class UntisClient:
    def __init__(self):
        if not UNTIS_USER or not UNTIS_PASS:
            raise ScrapeError("UNTIS_USER / UNTIS_PASS not set")
        self.base = f"https://{UNTIS_HOST}"
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "berichtsheft/1.0"
        self.person_id = None
        self.token = None

    def _rpc(self, method, params):
        r = self.s.post(
            f"{self.base}/WebUntis/jsonrpc.do",
            params={"school": UNTIS_SCHOOL},
            json={"id": "bab", "jsonrpc": "2.0", "method": method, "params": params},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            _dump_debug(f"rpc-{method}", data)
            err = data["error"]
            code = err.get("code") if isinstance(err, dict) else None
            raise ScrapeError(f"WebUntis RPC {method} failed: {err}", code=code)
        return data.get("result")

    def login(self):
        result = self._rpc("authenticate", {"user": UNTIS_USER, "password": UNTIS_PASS, "client": "berichtsheft"})
        self.person_id = result.get("personId")
        if not self.person_id:
            _dump_debug("authenticate", result)
            raise ScrapeError("login ok but no personId in response")
        # bearer token used by the REST endpoints of the new frontend
        r = self.s.get(f"{self.base}/WebUntis/api/token/new", timeout=30)
        if r.ok and r.text and len(r.text) < 4096:
            self.token = r.text.strip()
        else:
            log.warning("token/new failed (%s) — detail endpoint may not work", r.status_code)
        log.info("logged in, personId=%s", self.person_id)

    def logout(self):
        try:
            self._rpc("logout", {})
        except Exception:
            pass

    def timetable(self, start: dt.date, end: dt.date):
        result = self._rpc(
            "getTimetable",
            {
                "options": {
                    "element": {"id": self.person_id, "type": STUDENT_TYPE},
                    "startDate": int(start.strftime("%Y%m%d")),
                    "endDate": int(end.strftime("%Y%m%d")),
                    "showSubstText": True,
                    "showLsText": True,
                    "showInfo": True,
                    "subjectFields": ["name", "longname"],
                    "teacherFields": ["name", "longname"],
                }
            },
        )
        if not isinstance(result, list):
            _dump_debug("getTimetable", result)
            raise ScrapeError("unexpected getTimetable response")
        return result

    def holidays(self):
        result = self._rpc("getHolidays", {})
        if not isinstance(result, list):
            _dump_debug("getHolidays", result)
            raise ScrapeError("unexpected getHolidays response")
        return result

    def school_years(self):
        result = self._rpc("getSchoolyears", {})
        if not isinstance(result, list):
            _dump_debug("getSchoolyears", result)
            raise ScrapeError("unexpected getSchoolyears response")
        return result

    def teaching_content(self, date: dt.date, start_hm: str, end_hm: str):
        """Fetch Lehrstoff via the calendar-entry detail endpoint (same call the
        WebUntis frontend makes when a lesson modal opens)."""
        if not self.token:
            return ""
        params = {
            "elementId": self.person_id,
            "elementType": STUDENT_TYPE,
            "startDateTime": f"{date:%Y-%m-%d}T{start_hm}:00",
            "endDateTime": f"{date:%Y-%m-%d}T{end_hm}:00",
            "homeworkOption": "DUE",
        }
        r = self.s.get(
            f"{self.base}/WebUntis/api/rest/view/v2/calendar-entry/detail",
            params=params,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=30,
        )
        if not r.ok:
            log.warning("calendar-entry/detail %s for %s %s", r.status_code, date, start_hm)
            if r.status_code not in (404,):
                _dump_debug("detail-error", {"status": r.status_code, "body": r.text[:2000], "params": params})
            return ""
        try:
            data = r.json()
        except ValueError:
            _dump_debug("detail-nonjson", {"body": r.text[:2000], "params": params})
            return ""
        texts = []
        for entry in data.get("calendarEntries", []):
            tc = entry.get("teachingContent")
            if tc:
                texts.append(str(tc).strip())
        return "\n".join(t for t in texts if t)


def _hm(untis_time: int) -> str:
    """745 -> 07:45, 1330 -> 13:30"""
    return f"{untis_time // 100:02d}:{untis_time % 100:02d}"


def week_bounds(week_id: str):
    """'2026-W29' -> (monday, sunday)"""
    year, wk = week_id.split("-W")
    monday = dt.date.fromisocalendar(int(year), int(wk), 1)
    return monday, monday + dt.timedelta(days=6)


def current_week_id(today=None) -> str:
    today = today or dt.date.today()
    iso = today.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _is_full_holiday_week(monday: dt.date, sunday: dt.date, holiday_periods: list) -> bool:
    """True if every Mon-Fri date of the week falls inside some holiday period."""
    weekdays = [monday + dt.timedelta(days=i) for i in range(5)]
    ranges = []
    for h in holiday_periods:
        start = dt.datetime.strptime(str(h["startDate"]), "%Y%m%d").date()
        end = dt.datetime.strptime(str(h["endDate"]), "%Y%m%d").date()
        ranges.append((start, end))
    return all(any(start <= d <= end for start, end in ranges) for d in weekdays)


def _is_between_school_years(monday: dt.date, sunday: dt.date, school_years: list) -> bool:
    """True if every Mon-Fri date of the week falls in the gap between two
    consecutive school years — i.e. after one ends and before the next
    starts. No calendar dates hardcoded: derived entirely from WebUntis's
    own `getSchoolyears` data."""
    ranges = sorted(
        (
            dt.datetime.strptime(str(sy["startDate"]), "%Y%m%d").date(),
            dt.datetime.strptime(str(sy["endDate"]), "%Y%m%d").date(),
        )
        for sy in school_years
    )
    if not ranges:
        return False
    weekdays = [monday + dt.timedelta(days=i) for i in range(5)]
    if any(any(start <= d <= end for start, end in ranges) for d in weekdays):
        return False  # at least one weekday is inside a school year, not between them
    return ranges[0][0] <= weekdays[0] and weekdays[-1] <= ranges[-1][1]


def _has_real_lessons(path: Path) -> bool:
    """True only if the saved file has actual lesson data — a previously saved
    placeholder guess (holiday/schoolYearBoundary, empty days) is safe to
    overwrite with a better-informed result later."""
    if not path.exists():
        return False
    try:
        return bool(json.loads(path.read_text()).get("days"))
    except (json.JSONDecodeError, OSError):
        return False


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

        if SUBJECT_FILTER:
            lessons = [l for l in lessons if l["subject"] in SUBJECT_FILTER]

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
            out = DATA_DIR / f"{week_id}.json"
            if _has_real_lessons(out):
                log.info("%s already has real lesson data — not overwriting with unavailable marker", week_id)
            else:
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
                log.info("saved %s (beyond WebUntis publish horizon)", out)
            return result
        if holiday_periods and _is_full_holiday_week(monday, sunday, holiday_periods):
            result["holiday"] = True
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            out = DATA_DIR / f"{week_id}.json"
            out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
            log.info("saved %s (holiday week)", out)
            return result
        if school_years_list and _is_between_school_years(monday, sunday, school_years_list):
            result["holiday"] = True
            out = DATA_DIR / f"{week_id}.json"
            if _has_real_lessons(out):
                log.info("%s already has real lesson data — not overwriting", week_id)
            else:
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
                log.info("saved %s (between school years, treated as holiday)", out)
            return result
        if school_year_boundary:
            result["schoolYearBoundary"] = True
            out = DATA_DIR / f"{week_id}.json"
            if _has_real_lessons(out):
                log.info("%s already has real lesson data — not overwriting with school-year-boundary marker", week_id)
            else:
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
                log.info("saved %s (school-year boundary, not a confirmed holiday)", out)
            return result
        if periods:
            # WebUntis returned real periods but every one was cancelled, and
            # neither getHolidays nor getSchoolyears confirmed a holiday —
            # still worth surfacing honestly instead of pretending nothing
            # was ever scraped.
            result["allCancelled"] = True
            out = DATA_DIR / f"{week_id}.json"
            if _has_real_lessons(out):
                log.info("%s already has real lesson data — not overwriting with allCancelled marker", week_id)
            else:
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
                log.info("saved %s (all periods cancelled, not a confirmed holiday)", out)
            return result
        log.info("no lessons in %s — nothing saved", week_id)
        return result

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = DATA_DIR / f"{week_id}.json"
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    log.info("saved %s (%d lessons)", out, sum(len(d["lessons"]) for d in days))
    return result
