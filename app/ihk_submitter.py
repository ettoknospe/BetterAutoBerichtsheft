"""IHK tibrosBB submission orchestration - separate from scraper.py since
it's a different external system with no shared logic.

submit_week() finds or creates the right week's entry and saves text into
it. sync_status() refreshes data/ihk_status.json, a local record of which
weeks are still editable vs already locked (genehmigt) - "Neuer Eintrag"
only ever creates the next sequential missing week, so the UI needs to
know in advance which weeks can actually be submitted before offering the
button (see AGENTS.md-style rationale in the plan doc).
"""

import datetime as dt
import json
import logging

import config
from ihk_client import IhkClient, IhkError
from time_utils import current_week_id, week_bounds

log = logging.getLogger("scraper")

# "Nachweis genehmigt", "in Bearbeitung bei Azubi", and "Warten auf
# Genehmigung" have all been seen on a real entry (2026-W29, after the user
# hit "Speichern & Senden" on the actual portal). "abgelehnt" is still only
# a guess at the portal's filter-checkbox label - never confirmed live.
_STATUS_MAP = {
    "Nachweis genehmigt": "genehmigt",
    "in Bearbeitung bei Azubi": "in_bearbeitung",
    "Warten auf Genehmigung": "warten_auf_genehmigung",
    "abgelehnt": "abgelehnt",
}


def _classify(raw_status: str) -> str:
    return _STATUS_MAP.get(raw_status, "unknown")


def _next_week_id(week_id: str) -> str:
    _, sunday = week_bounds(week_id)
    return current_week_id(sunday + dt.timedelta(days=1))


def sync_status():
    """Refresh data/ihk_status.json from the live portal - the ONLY place
    that logs into IHK on a routine basis (piggybacked on scrape/submit,
    never on plain UI navigation - see main.py). Only status/lfdnr
    (lock-state metadata, needed to know when to hide the submit button) -
    deliberately does NOT read back ausbinhalt1/2/3 content. Data flows
    one-way, own site -> IHK; the UI never shows/pre-fills text pulled
    back from the portal (see save_entry's preserve-if-omitted default for
    how a blank field in the UI still avoids clobbering real content on
    IHK without needing to fetch and display it first).
    Best-effort: a failure here must not break whatever it's piggybacked
    onto."""
    client = IhkClient()
    client.login()
    try:
        entries = client.list_entries()
    finally:
        client.logout()

    now = dt.datetime.now().isoformat(timespec="seconds")
    status = {
        week_id: {"lfdnr": e["lfdnr"], "status": _classify(e["status"]), "syncedAt": now}
        for week_id, e in entries.items()
    }

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = config.DATA_DIR / "ihk_status.json"
    out.write_text(json.dumps(status, indent=2, ensure_ascii=False))
    log.info("synced IHK status for %d weeks", len(status))
    return status


def load_status() -> dict:
    """Read the last-synced status map, or {} if never synced."""
    path = config.DATA_DIR / "ihk_status.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def load_history() -> dict:
    """Read the one-time archived ausbinhalt1/2 content per week, or {} if
    the backfill has never been run. See backfill_ihk_history.py - this is a
    static snapshot, not kept in sync automatically."""
    path = config.DATA_DIR / "ihk_history.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def submit_week(week_id: str, formatted_text: str, ausbinhalt1: str | None = None, ausbinhalt2: str | None = None):
    """Find (or create, if it's the next sequential missing one) the IHK
    entry for week_id, and save formatted_text into its "Berufsschule"
    field. ausbinhalt1/ausbinhalt2 ("Betriebliche Tätigkeiten"/
    "Unterweisungen...") are optional - omit to preserve whatever's
    already on the portal (see IhkClient.save_entry)."""
    client = IhkClient()
    client.login()
    try:
        entries = client.list_entries()

        entry = entries.get(week_id)
        if entry is not None:
            if _classify(entry["status"]) == "genehmigt":
                raise IhkError(f"{week_id} is already genehmigt (locked) - cannot submit")
            client.save_entry(entry["lfdnr"], formatted_text, ausbinhalt1, ausbinhalt2)
            return

        # week_id has no entry yet - only allowed if it's exactly the next
        # sequential week after the newest existing one (Neuer Eintrag
        # can't target an arbitrary week or skip ahead).
        if entries:
            newest_week = max(entries.keys())
        else:
            newest_week = None

        if newest_week is None or _next_week_id(newest_week) != week_id:
            raise IhkError(
                f"{week_id} has no entry and is not the next sequential week "
                f"after {newest_week!r} - cannot skip ahead with 'Neuer Eintrag'"
            )

        draft = client.create_next_entry()
        client.save_entry(draft, formatted_text, ausbinhalt1, ausbinhalt2)
    finally:
        client.logout()
