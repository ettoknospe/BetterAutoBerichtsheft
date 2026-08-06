"""WebUntis JSON-RPC + REST client.

Flow:
  1. JSON-RPC authenticate -> session cookie + personId
  2. /WebUntis/api/token/new -> bearer token for REST endpoints
  3. JSON-RPC getTimetable -> periods of the week
  4. per period: REST calendar-entry/detail -> teachingContent (Lehrstoff)

On unexpected API responses the raw payload is dumped to DATA_DIR/debug/
(see storage.py) so a failing first run can be diagnosed without
re-running blind.
"""

import datetime as dt
import logging

import requests

from . import config
from .settings import UserSettings
from .storage import _dump_debug

log = logging.getLogger("scraper")

STUDENT_TYPE = 5  # WebUntis element type for students
SCHOOL_YEAR_BOUNDARY_CODE = -8507  # getTimetable error when start/end span two school years
NO_ALLOWED_DATE_CODE = -7004  # getTimetable error when the date is beyond WebUntis's publish horizon


class ScrapeError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class UntisClient:
    def __init__(self, settings: UserSettings | None = None):
        self.cfg = settings or UserSettings.from_config()
        if not self.cfg.UNTIS_USER or not self.cfg.UNTIS_PASS:
            raise ScrapeError("UNTIS_USER / UNTIS_PASS not set")
        self.base = f"https://{self.cfg.UNTIS_HOST}"
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        self.person_id = None
        self.token = None

    def _rpc(self, method, params):
        r = self.s.post(
            f"{self.base}/WebUntis/jsonrpc.do",
            params={"school": self.cfg.UNTIS_SCHOOL},
            json={"id": "bab", "jsonrpc": "2.0", "method": method, "params": params},
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            _dump_debug(f"rpc-{method}", data, user_id=self.cfg.user_id)
            err = data["error"]
            code = err.get("code") if isinstance(err, dict) else None
            raise ScrapeError(f"WebUntis RPC {method} failed: {err}", code=code)
        return data.get("result")

    def login(self):
        result = self._rpc(
            "authenticate", {"user": self.cfg.UNTIS_USER, "password": self.cfg.UNTIS_PASS, "client": "berichtsheft"}
        )
        self.person_id = result.get("personId")
        if not self.person_id:
            _dump_debug("authenticate", result, user_id=self.cfg.user_id)
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
            _dump_debug("getTimetable", result, user_id=self.cfg.user_id)
            raise ScrapeError("unexpected getTimetable response")
        return result

    def holidays(self):
        result = self._rpc("getHolidays", {})
        if not isinstance(result, list):
            _dump_debug("getHolidays", result, user_id=self.cfg.user_id)
            raise ScrapeError("unexpected getHolidays response")
        return result

    def school_years(self):
        result = self._rpc("getSchoolyears", {})
        if not isinstance(result, list):
            _dump_debug("getSchoolyears", result, user_id=self.cfg.user_id)
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
            timeout=60,
        )
        if not r.ok:
            log.warning("calendar-entry/detail %s for %s %s", r.status_code, date, start_hm)
            if r.status_code not in (404,):
                _dump_debug("detail-error", {"status": r.status_code, "body": r.text[:2000], "params": params}, user_id=self.cfg.user_id)
            return ""
        try:
            data = r.json()
        except ValueError:
            _dump_debug("detail-nonjson", {"body": r.text[:2000], "params": params}, user_id=self.cfg.user_id)
            return ""
        texts = []
        for entry in data.get("calendarEntries", []):
            tc = entry.get("teachingContent")
            if tc:
                texts.append(str(tc).strip())
        return "\n".join(t for t in texts if t)
