# Berichtsheft

Scrapes Lehrstoff (teaching content) from WebUntis via its JSON/REST API and
serves a small week-based viewer. One Docker container, runs on amd64 and
Raspberry Pi (arm64).

## Run

```bash
cp .env.example .env   # fill in UNTIS_USER / UNTIS_PASS (and IHK_USER / IHK_PASS if you want auto-submit)
docker compose up -d --build
```

Open http://localhost:8001 (on the Pi: http://<pi-ip>:8001).

- Arrows / dropdown switch weeks, weeks without data can be scraped on demand
  with the **Jetzt abrufen** button.
- Auto-scrape of the current **and previous** ISO week every Sunday 18:00
  (`SCRAPE_DAY` / `SCRAPE_TIME`) — catches Lehrstoff teachers enter after the week
  is already over.
- Data lives as one JSON per ISO week in `./data/` (bind-mounted volume).
- The "Berufsschule" block is the deduped, copy-paste-ready Berichtsheft text;
  **Text kopieren** copies it.

## IHK submission

**Bei IHK einreichen** submits the current week's Berufsschule text (plus two
optional manually-typed fields — "Betriebliche Tätigkeiten" and
"Unterweisungen, betrieblicher Unterricht, sonstige Schulungen") straight into
the real IHK apprenticeship logbook portal (tibrosBB), so you never have to
copy-paste it there by hand.

- **One-way only**: this app only ever *writes* to IHK, never reads content
  back to pre-fill its own UI — what you see here is always your own
  WebUntis data, never something echoed back from the portal.
- **Never auto-sends**: only ever clicks the portal's "Speichern" (save), never
  "Speichern & Senden" (send for approval) — that step is always yours to do
  on the real site.
- The button only appears for a week that can actually be submitted right
  now — either an existing entry that isn't locked, or exactly the next
  sequential week (the portal's "Neuer Eintrag" can't skip ahead). Weeks
  further back in a backlog show a disabled button explaining what to submit
  first, instead of failing after a click.
- A colored badge next to the week shows the real IHK status: *eingetragen*,
  *eingetragen und genehmigt*, *Warte auf Genehmigung* (sent, awaiting approval —
  yellow, locked), or *nicht genehmigt* (needs correction — red, still editable).
  Status is refreshed after every scrape and every submit, never on plain navigation.

### One-time history backfill

Genehmigt (locked) weeks can't be resubmitted, so it's safe to show their real
submitted text — run this once per user, on whichever machine holds your
`data/` volume (dev machine or Pi), to archive every existing IHK entry's
`ausbinhalt1`/`ausbinhalt2` into that user's encrypted IHK history. Without
it, locked weeks in the viewer only show the Berufsschule text, not the
archived entry:

```bash
docker compose run --rm --entrypoint python berichtsheft app/backfill_ihk_history.py <user_id>
```

## How scraping works

1. JSON-RPC `authenticate` → session + personId
2. `/WebUntis/api/token/new` → bearer token
3. JSON-RPC `getTimetable` → lessons of the week
4. Per lesson `GET /WebUntis/api/rest/view/v2/calendar-entry/detail` → `teachingContent`

On unexpected API responses raw payloads are dumped to `data/debug/` for diagnosis.

## Weeks with no lessons

Not every empty week means "not scraped yet" — the app tries to say why instead:

- **Schulferien** — confirmed via WebUntis's own `getHolidays`/`getSchoolyears` data (no
  hardcoded holiday dates).
- **Wahrscheinlich Schuljahrwechsel** — the week straddles two school years and WebUntis
  can't say for sure whether it's a holiday.
- **Alle Stunden abgesagt** — real lessons exist but every one is cancelled (e.g. the
  first week of a new school year before the schedule is active). Still submittable —
  the Berufsschule box just contains that exact text instead of a lesson list.
- **Kann noch nicht abgerufen werden** — the week is further in the future than WebUntis
  currently allows querying; try again later.

## API

- `GET /api/weeks` — available weeks + current ISO week
- `GET /api/weeks/2026-W29` — data for one week
- `POST /api/scrape` — body `{"week": "2026-W29"}` (optional, default current week)
- `GET /api/ihk-status` — last-synced `{week_id: {lfdnr, status, syncedAt}}` from IHK
- `POST /api/submit-ihk` — body `{"week": "2026-W29", "text": "...", "ausbinhalt1": null, "ausbinhalt2": null}`
  (the last two are optional; omit/`null` to leave whatever's already on IHK untouched)

## Tests

```bash
docker compose build
docker run --rm -v "$PWD/tests":/srv/tests -v "$PWD/pytest.ini":/srv/pytest.ini \
  --entrypoint bash bab2-berichtsheft \
  -c "pip install -q pytest==8.3.4 httpx==0.28.1 && cd /srv && pytest -q"
```
