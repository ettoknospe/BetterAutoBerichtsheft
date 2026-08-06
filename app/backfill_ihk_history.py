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

Run once, not part of the deployed service - reuses the same image/creds/
data volume as the real app, so this works identically wherever the app is
deployed (dev machine or rpi):

    docker compose run --rm --entrypoint python berichtsheft app/backfill_ihk_history.py
"""

import datetime as dt
import json
import logging

from . import config
from .ihk_client import IhkClient

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("backfill")


def main():
    client = IhkClient()
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

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = config.DATA_DIR / "ihk_history.json"
    out.write_text(json.dumps(history, indent=2, ensure_ascii=False))
    log.info("wrote %d entries to %s", len(history), out)


if __name__ == "__main__":
    main()
