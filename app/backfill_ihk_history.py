"""One-time backfill: scrape ausbinhalt1/ausbinhalt2 text for every existing
IHK entry (locked or not) and save it to data/ihk_history.json.

Deliberately writes to its own file, NOT data/ihk_fields.json -
save_local_fields()'s docstring guarantees that file only ever holds text
typed into this app, never anything fetched back from IHK. Mixing scraped
content into it would break that invariant.

This is also a deliberate, explicit, one-time exception to the app's normal
one-way (this-app -> IHK, never back) data flow documented in AGENTS.md /
DEVELOPER.md - it exists purely to archive old entries' content locally
before that content becomes hard to reach, run by hand, once. It is not
wired into the running app, its API, or its scheduler in any way, and
doesn't change the live one-way-flow behavior described there.

Run once, not part of the deployed service - takes the target user's numeric
id explicitly (multi-user: there's no single "the" data dir anymore) and
reuses the same image/data volume as the real app, so this works identically
wherever the app is deployed (dev machine or rpi):

    docker compose run --rm --entrypoint python berichtsheft app/backfill_ihk_history.py <user_id>
"""

import datetime as dt
import logging
import sys

from . import db, storage
from .ihk_client import IhkClient

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("backfill")


def main():
    if len(sys.argv) != 2:
        print("usage: python -m app.backfill_ihk_history <user_id>", file=sys.stderr)
        raise SystemExit(1)
    user_id = int(sys.argv[1])

    user_row = db.get_user_by_id(user_id)
    if user_row is None:
        print(f"no such user_id: {user_id}", file=sys.stderr)
        raise SystemExit(1)
    settings_row = db.get_user_settings(user_id)
    settings = db._row_to_settings(settings_row, user_row)

    client = IhkClient(settings)
    client.login()
    try:
        entries = client.list_entries()
        log.info("found %d IHK entries", len(entries))

        history = {}
        now = dt.datetime.now().isoformat(timespec="seconds")
        for week_id, meta in sorted(entries.items()):
            lfdnr = meta["lfdnr"]
            if lfdnr is None:
                log.warning("skip %s - no lfdnr", week_id)
                continue
            form = client.fetch_entry(lfdnr)
            history[week_id] = {
                "lfdnr": lfdnr,
                "status": meta["status"],
                "ausbinhalt1": form["ausbinhalt1"],
                "ausbinhalt2": form["ausbinhalt2"],
                "scrapedAt": now,
            }
            log.info(
                "%s (lfdnr=%s, %s): ausbinhalt1=%d chars, ausbinhalt2=%d chars",
                week_id, lfdnr, meta["status"], len(form["ausbinhalt1"]), len(form["ausbinhalt2"]),
            )
    finally:
        client.logout()

    storage.save_ihk_history(user_id, history)
    log.info("wrote %d entries for user_id=%d", len(history), user_id)


if __name__ == "__main__":
    main()
