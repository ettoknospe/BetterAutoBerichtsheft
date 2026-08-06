"""IHK tibrosBB portal client - plain HTTP, no browser.

Classic JSP/Tomcat webapp, not a REST API. Mechanics confirmed by hand
against the real portal (see plan doc for the debugging story):

- Login: POST azubiHome.jsp with login/pass/anmelden/old_url, session via
  a plain JSESSIONID cookie.
- Deep-linking into azubiHeftEditForm.jsp?lfdnr=... directly (without
  visiting azubiHeft.jsp first, in the same session) bounces back to a
  generic login page even though the session is authenticated -
  list_entries() (always called before any deep-link in every real flow)
  serves as the warm-up naturally.
- "Neuer Eintrag" does NOT create a persisted entry by itself. It returns
  a blank DRAFT form (hidden lfdnr='0') for what will become the next
  sequential week - the portal only assigns a real lfdnr once that draft
  is actually saved. save_entry() handles a lfdnr='0' draft specially:
  it diffs list_entries() before/after the save (not before/after "Neuer
  Eintrag") to discover the real lfdnr the portal just assigned.
- The submit buttons (`save`, `sent`, `neu`) have no value= attribute, so
  a real browser submits them as an EMPTY string, not their label text.
- ausbinhalt13 is not a static placeholder - it mirrors ausbinhalt3's
  CURRENT saved value and acts as an optimistic-concurrency check. A save
  with a stale ausbinhalt13 is silently rejected: HTTP 200, the submitted
  text echoed back in the response, but nothing persisted. Because of this,
  save_entry() always re-fetches and verifies after POSTing rather than
  trusting the response.
"""

import datetime as dt
import logging
import re

import requests
from bs4 import BeautifulSoup

from . import config
from .settings import UserSettings
from .time_utils import current_week_id

log = logging.getLogger("scraper")


class IhkError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class IhkClient:
    def __init__(self, settings: UserSettings | None = None):
        self.cfg = settings or UserSettings.from_config()
        if not self.cfg.IHK_USER or not self.cfg.IHK_PASS:
            raise IhkError("IHK_USER / IHK_PASS not set")
        self.base = f"https://{self.cfg.IHK_HOST}/tibrosBB"
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "berichtsheft/1.0"

    def login(self):
        self.s.get(f"{self.base}/BB_auszubildende.jsp", timeout=30)
        r = self.s.post(
            f"{self.base}/azubiHome.jsp",
            data={"login": self.cfg.IHK_USER, "pass": self.cfg.IHK_PASS, "anmelden": "Login", "old_url": "null"},
            timeout=30,
        )
        r.raise_for_status()
        if "angemeldet als" not in r.text:
            raise IhkError("IHK login failed")
        # No warm-up GET of azubiHeft.jsp here on purpose - every current
        # caller (sync_status, submit_week) calls list_entries() before any
        # deep-link (fetch_entry/create_next_entry), and list_entries()
        # itself fetches azubiHeft.jsp, which satisfies the same
        # "visited the list page this session" requirement.
        log.info("logged in to IHK portal")

    def logout(self):
        try:
            self.s.get(f"{self.base}/logout.jsp", timeout=30)
        except Exception:
            pass

    def list_entries(self):
        """Every week that already has a PERSISTED IHK entry, keyed by
        week_id. Weeks with no entry yet simply don't appear here -
        "Neuer Eintrag" never lets you skip ahead, so a missing week is
        always the gap right after the newest one returned.
        """
        r = self.s.get(f"{self.base}/azubiHeft.jsp", timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        entries = {}
        for label in soup.find_all("div", class_="col-md-4"):
            if label.get_text(strip=True) != "Zeitraum:":
                continue
            row = label.find_parent("div", class_="row")
            block = row.parent
            fields = {}
            for r2 in block.find_all("div", class_="row", recursive=False):
                cols = r2.find_all("div", recursive=False)
                if len(cols) != 2:
                    continue
                key = cols[0].get_text(strip=True).rstrip(":")
                fields[key] = cols[1]

            zeitraum = fields.get("Zeitraum")
            status = fields.get("Status")
            if zeitraum is None or status is None:
                continue
            start_str = zeitraum.get_text(strip=True).split(" - ")[0]
            start_date = dt.datetime.strptime(start_str, "%d.%m.%Y").date()
            week_id = current_week_id(start_date)

            link = block.find("a", href=lambda h: h and "lfdnr=" in h)
            lfdnr = None
            if link:
                m = re.search(r"lfdnr=(\d+)", link["href"])
                lfdnr = int(m.group(1)) if m else None

            entries[week_id] = {"lfdnr": lfdnr, "status": status.get_text(strip=True)}

        return entries

    def _parse_entry_form(self, html):
        """Shared field extraction for both an existing entry's edit page
        and a fresh "Neuer Eintrag" draft - same form shape either way,
        just lfdnr='0' and blank content fields for a draft. lfdnr is read
        FROM the HTML itself (not trusted from the caller), since for a
        draft it's the only way to know it's '0' and not yet persisted."""
        if "Anmeldung am Azubiportal" in html:
            raise IhkError("bounced to login")
        soup = BeautifulSoup(html, "html.parser")

        def val(name):
            el = soup.find(attrs={"name": name})
            if el is None:
                return None
            return el.get_text() if el.name == "textarea" else el.get("value")

        return {
            "lfdnr": val("lfdnr"),
            "token": val("token"),
            "edtvon": val("edtvon"),
            "edtbis": val("edtbis"),
            "ausbabschnitt": val("ausbabschnitt"),
            "ausbMail": val("ausbMail"),
            "ausbinhalt1": val("ausbinhalt1") or "",
            "ausbinhalt2": val("ausbinhalt2") or "",
            "ausbinhalt3": val("ausbinhalt3") or "",
            "ausbinhalt13": val("ausbinhalt13") or "",
        }

    def create_next_entry(self):
        """POST the "Neuer Eintrag" form. Doesn't persist anything by
        itself - returns a blank draft dict (lfdnr='0') for what will
        become the next sequential week once actually saved (pass this
        straight to save_entry(); see module docstring)."""
        r = self.s.post(f"{self.base}/azubiHeftEditForm.jsp", data={"neu": ""}, timeout=30)
        r.raise_for_status()
        draft = self._parse_entry_form(r.text)
        if not draft["token"]:
            raise IhkError("could not read token from 'Neuer Eintrag' draft")
        return draft

    def fetch_entry(self, lfdnr):
        r = self.s.get(f"{self.base}/azubiHeftEditForm.jsp", params={"lfdnr": lfdnr}, timeout=30)
        r.raise_for_status()
        return self._parse_entry_form(r.text)

    def save_entry(self, entry, ausbinhalt3, ausbinhalt1=None, ausbinhalt2=None):
        """Save text into an entry's three content fields and return its
        real lfdnr. `entry` is either an existing entry's lfdnr (fetched
        fresh here), or an already-parsed draft dict from
        create_next_entry() (lfdnr='0', not yet persisted).

        ausbinhalt1/ausbinhalt2 ("Betriebliche Tätigkeiten"/
        "Unterweisungen...") default to whatever is CURRENTLY on the
        entry so a school-content-only save never wipes them (used to
        hardcode "", which silently blanked any manually-entered content
        on the real site - #bug). Always re-fetches afterward to
        independently verify the save actually persisted - see module
        docstring on why the POST response alone can't be trusted."""
        current = entry if isinstance(entry, dict) else self.fetch_entry(entry)
        if not current["token"]:
            raise IhkError(f"could not read token for entry {current.get('lfdnr', entry)}")

        is_new_draft = current["lfdnr"] in ("0", 0, None)

        if ausbinhalt1 is None:
            ausbinhalt1 = current["ausbinhalt1"]
        if ausbinhalt2 is None:
            ausbinhalt2 = current["ausbinhalt2"]

        ausb_mail = self.cfg.IHK_AUSB_MAIL or current["ausbMail"] or ""
        ausbabschnitt = self.cfg.IHK_AUSBABSCHNITT or current["ausbabschnitt"] or ""

        payload = {
            "token": current["token"],
            "lfdnr": str(current["lfdnr"]),
            "gap": "false",
            "gapNr": "0",
            "edtvon": current["edtvon"],
            "edtbis": current["edtbis"],
            "ausbabschnitt": ausbabschnitt,
            "ausbMail": current["ausbMail"] or ausb_mail,
            "ausbMail2": ausb_mail,
            "ausbinhalt1": ausbinhalt1,
            "stdMo": "0", "stdDi": "0", "stdMi": "0", "stdDo": "0", "stdFr": "0", "stdSa": "0", "stdSo": "0",
            "ausbinhalt2": ausbinhalt2,
            "ausbinhalt12": "",
            "ausbinhalt3": ausbinhalt3,
            "ausbinhalt13": current["ausbinhalt13"],
            "save": "",
        }

        # A brand-new draft (lfdnr='0') doesn't exist in list_entries()
        # yet - THIS save is what creates it, so the before/after diff to
        # discover the real lfdnr belongs here, not around "Neuer
        # Eintrag" (which never actually changes the list - that was the
        # original bug).
        before = set(e["lfdnr"] for e in self.list_entries().values() if e["lfdnr"]) if is_new_draft else None

        r = self.s.post(f"{self.base}/azubiHeftAdd.jsp", data=payload, files={"file": ("", b"")}, timeout=30)
        r.raise_for_status()
        # bonus signal: a real save redirects to the list page; a
        # rejected one redirects back to the same edit form. Not proof by
        # itself - the re-fetch below is what actually matters.
        looked_ok = "azubiHeft.jsp" in r.url and "azubiHeftEditForm.jsp" not in r.url

        if is_new_draft:
            after = self.list_entries()
            new_lfdnrs = set(e["lfdnr"] for e in after.values() if e["lfdnr"]) - before
            if len(new_lfdnrs) != 1:
                raise IhkError(
                    f"expected exactly one new entry after saving the draft, got {new_lfdnrs} "
                    f"(redirect looked {'ok' if looked_ok else 'like a rejection'})"
                )
            real_lfdnr = new_lfdnrs.pop()
        else:
            real_lfdnr = current["lfdnr"]

        verify = self.fetch_entry(real_lfdnr)
        mismatches = [
            name
            for name, expected in (("ausbinhalt1", ausbinhalt1), ("ausbinhalt2", ausbinhalt2), ("ausbinhalt3", ausbinhalt3))
            if verify[name] != expected
        ]
        if mismatches:
            raise IhkError(
                f"save for entry {real_lfdnr} did not persist ({', '.join(mismatches)} mismatched; "
                f"redirect looked {'ok' if looked_ok else 'like a rejection'})"
            )
        log.info("saved entry %s", real_lfdnr)
        return real_lfdnr
