# Berichtsheft

Scrapes Lehrstoff (teaching content) from WebUntis via its JSON/REST API and
serves a small week-based viewer, with optional one-click submission to the
IHK apprenticeship logbook portal (tibrosBB). Multi-user: each person logs
in with their own account and their own WebUntis/IHK credentials. One Docker
container, runs on amd64 and Raspberry Pi (arm64).

## Run

```bash
cp .env.example .env
```

Fill in two values in `.env` — these are the only ones a real deployment
needs:

```
SECRET_ENCRYPTION_KEY=   # generate with the command below
ADMIN_PASSWORD=          # password for the first (admin) account
```

Generate the encryption key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Then:

```bash
docker compose up -d --build
```

Open `http://localhost:8001/login.html` (on the Pi: `http://<pi-ip>:8001/login.html`)
and log in as `admin` with the password you set.

WebUntis and IHK credentials are **not** set in `.env`. Each user enters
their own under **Settings** after logging in — see below.

- Arrows / dropdown switch weeks, weeks without data can be scraped on demand
  with the **Jetzt abrufen** button.
- Auto-scrape of the current **and previous** ISO week runs per-user, on each
  user's own schedule (`Settings → Scraper`, default Sunday 18:00) — catches
  Lehrstoff teachers enter after the week is already over.
- Data lives in a single SQLite database (`data/app.db`), encrypted per user
  (see [DEVELOPER.md](DEVELOPER.md) for the encryption model) — not as JSON
  files per week.

## Multi-user

- The first account is created automatically on first startup if
  `ADMIN_PASSWORD` is set (it becomes an admin). If you didn't set it, run
  `docker compose run --rm --entrypoint python berichtsheft -m app.create_admin`
  once to create it interactively.
- Admins create further accounts under **Settings → Admin → Benutzer
  verwalten** (or `POST /api/admin/users`).
- Each user sets their own WebUntis host/school/username/password and,
  optionally, IHK tibrosBB credentials under **Settings**. Both are encrypted
  at rest. The scheduler scrapes each user independently, on their own
  `scrape_day`/`scrape_time`.
- There is no automatic import of old single-tenant `.env`-based data — a new
  account always starts empty. The `UNTIS_*`/`IHK_*` variables in
  `.env.example` are legacy/test-only fallbacks, not read on any real request
  path once a user has logged in.

Full details, DB schema, and the encryption model: [DEVELOPER.md](DEVELOPER.md).

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
- A colored badge next to the week shows the real IHK status: *in
  Bearbeitung* (editable), *genehmigt* (approved, locked), *warten auf
  Genehmigung* (sent, awaiting approval, locked), or *abgelehnt* (needs
  correction, still editable). Status is refreshed after every scrape and
  every submit, never on plain navigation.

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

Or bulk-scrape a whole date range and run this backfill from the UI:
**Settings → Datenimport (Massenabruf)**.

## How scraping works

1. JSON-RPC `authenticate` → session + personId
2. `/WebUntis/api/token/new` → bearer token
3. JSON-RPC `getTimetable` → lessons of the week
4. Per lesson `GET /WebUntis/api/rest/view/v2/calendar-entry/detail` → `teachingContent`

On unexpected API responses, if `DEBUG_DUMPS=true` is set, raw payloads are
dumped (unencrypted) to `data/<user_id>/debug/` for diagnosis — off by
default, dev-only.

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

All endpoints except `POST /api/auth/login` require a valid `session` cookie
(set by login); admin-only endpoints additionally require the logged-in user
to be an admin.

| Method & path | Auth | Purpose |
|---|---|---|
| `POST /api/auth/login` | — | Log in, sets session cookie |
| `POST /api/auth/logout` | user | Clear session cookie |
| `GET /api/auth/whoami` | user | Current username + admin flag |
| `POST /api/admin/users` | admin | Create a user |
| `GET /api/admin/users` | admin | List all users |
| `GET /api/me/settings` | user | Get own WebUntis/IHK/scrape settings |
| `PUT /api/me/settings` | user | Update own settings (partial) |
| `PUT /api/me/password` | user | Change own password |
| `POST /api/test/untis` | user | Test WebUntis login without saving |
| `POST /api/test/ihk` | user | Test IHK login without saving |
| `GET /api/weeks` | user | Saved week ids + current week + configured start week |
| `GET /api/weeks/{week_id}` | user | Data for one week (404 if not scraped) |
| `POST /api/scrape` | user | Scrape a week (default: current) |
| `GET /api/ihk-status` | user | Last-synced IHK status + locally remembered fields |
| `GET /api/ihk-history` | user | Archived IHK entry history (from the one-time backfill) |
| `POST /api/submit-ihk` | user | Submit an entry to IHK |
| `POST /api/bulkops/scrape-weeks` | user | Scrape an inclusive week range |
| `GET /api/bulkops/scrape-progress` | user | Poll progress of an in-flight bulk scrape |
| `POST /api/bulkops/backfill-ihk` | user | Same as the CLI backfill, run via the UI |

Example bodies:

```
POST /api/scrape
{"week": "2026-W29"}   # optional, default current week

POST /api/submit-ihk
{"week": "2026-W29", "text": "...", "ausbinhalt1": null, "ausbinhalt2": null}
# the last two are optional; omit/null to leave whatever's already on IHK untouched
```

Full request/response shapes for every endpoint: [DEVELOPER.md](DEVELOPER.md).

## Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```

95 tests, all authenticating through the real HTTP login flow against an
isolated test database — see [DEVELOPER.md](DEVELOPER.md#run-tests) for
details and the Docker-based alternative.
