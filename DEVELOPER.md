# Berichtsheft Developer Guide

This document explains how to develop and maintain Berichtsheft.

## About Berichtsheft

Berichtsheft is a multi-user web application. It gets teaching content from
WebUntis (a school scheduling system) and can send that content to the IHK
apprenticeship logbook portal (tibrosBB), saving you from copy-pasting it
there by hand.

Each user logs in with their own account and their own WebUntis/IHK
credentials, encrypted at rest under a per-user key. The app talks to both
external systems over plain HTTP and JSON-RPC — no browser automation (no
Playwright, no Selenium). All app data (scraped weeks, IHK history/status,
user settings) lives in one SQLite database. The app runs in a Docker
container, on amd64 and arm64 (Raspberry Pi) computers.

## Project Structure

```
berichtsheft/
├── app/
│   ├── main.py                # FastAPI app: all routes, scheduler, per-user locks
│   ├── auth.py                # Password hashing, session cookies, require_user/require_admin
│   ├── db.py                  # SQLite schema, migrations, all per-user CRUD
│   ├── crypto.py              # Fernet encryption primitives (master key + per-user DEK)
│   ├── settings.py            # UserSettings dataclass, built per-request from the DB
│   ├── config.py              # Runtime config read from the environment
│   ├── create_admin.py        # CLI: create the first admin account manually
│   ├── backfill_ihk_history.py # CLI: one-time archive of a user's existing IHK entries
│   ├── scraper.py             # Orchestration: scrape_week() ties the rest together
│   ├── untis_client.py        # WebUntis JSON-RPC + REST client (UntisClient, ScrapeError)
│   ├── time_utils.py          # Pure time/week-id helpers
│   ├── school_calendar.py     # Pure holiday/school-year gap detection
│   ├── storage.py             # Per-user encrypted CRUD (wraps db.py) + dev-only debug dumps
│   ├── ihk_client.py          # IHK tibrosBB portal client (IhkClient, IhkError)
│   └── ihk_submitter.py       # IHK orchestration: submit_week(), sync_status()
├── static/                    # HTML/CSS/JS, no build step, no framework
│   ├── login.html             # Login screen
│   ├── index.html             # Main week viewer + IHK submit UI
│   ├── settings.html          # Per-user settings, tabbed (WebUntis/Scraper/IHK/Account/Admin)
│   ├── bulkops.html           # Bulk scrape a date range + one-time IHK history backfill
│   └── help.html              # In-app German-language help/FAQ
├── tests/                     # pytest suite, see "Run Tests" below
├── data/                      # data/app.db (SQLite) + data/<user_id>/debug/ if DEBUG_DUMPS=true
├── Dockerfile                 # Container definition
├── compose.yaml                # Docker Compose configuration
├── requirements.txt            # Runtime Python dependencies
├── requirements-dev.txt        # + pytest/httpx for testing
└── pytest.ini                  # Test configuration
```

## Install and Run

### First-Time Setup

```bash
cp .env.example .env
```

Edit `.env` and set the two values a real deployment actually needs:

```
SECRET_ENCRYPTION_KEY=your-generated-key
ADMIN_PASSWORD=your-admin-password
```

Generate the key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

`.env.example` also lists `UNTIS_USER`/`UNTIS_PASS`/`IHK_USER`/`IHK_PASS` and
related vars — these are **legacy/test-only fallbacks**, not read on any real
request path. Real WebUntis/IHK credentials are entered per-user via the
Settings UI after login (see "REST API" and "Multi-User Operations" below).

Build and start:

```bash
docker compose build
docker compose up -d
```

The app starts on `http://localhost:8001`. Log in at
`http://localhost:8001/login.html`.

### Stop the Application

```bash
docker compose down
```

## First Admin Account

There is **no automatic migration** of old single-tenant data — a fresh
deployment always starts with an empty database, and a new account always
starts with empty settings. Two ways to get the first (admin) account:

1. **Automatic, at startup** (`db.bootstrap_admin_if_needed()`, called from
   FastAPI's startup event): if the `users` table is empty and
   `ADMIN_PASSWORD` is set in `.env`, an admin account named `ADMIN_USERNAME`
   (default `admin`) is created automatically. Requires `SECRET_ENCRYPTION_KEY`
   to be set to a valid Fernet key — the app fails fast at startup if it
   isn't (encryption is validated before any admin row is written).
2. **Manual CLI**, if you didn't set `ADMIN_PASSWORD`:
   ```bash
   docker compose run --rm --entrypoint python berichtsheft -m app.create_admin
   ```
   Prompts for username/password interactively (or accepts `--username`/
   `--password` flags). Refuses to run if any user already exists.

Once logged in as admin, create further accounts under **Settings → Admin →
Benutzer verwalten**, or via `POST /api/admin/users`.

## Encryption Model

Two-tier Fernet encryption:

- **Master key** (`SECRET_ENCRYPTION_KEY`, from `.env`) directly encrypts two
  things: (a) each user's random per-user data-encryption key (DEK), stored
  wrapped in `user_keys.dek_wrapped`; (b) the `untis_pass_enc`/`ihk_pass_enc`
  columns in `user_settings` — **these two password columns are encrypted
  with the master key directly, not with the per-user DEK.** Easy to get
  backwards; the asymmetry is intentional (see `storage.py`'s docstring: the
  scheduler runs unattended and needs the same master key an operator holds
  to decrypt WebUntis/IHK login credentials for scraping).
- **Per-user DEK** encrypts everything else: scraped week data, IHK history,
  IHK status, and locally-remembered IHK fields — all stored as encrypted
  BLOBs in SQLite, decrypted only after fetching and unwrapping that user's
  DEK with the master key.

A user cannot exist without a DEK — `db.create_user()` always creates one in
the same transaction. Decryption failures **raise** (`RuntimeError`) rather
than silently returning empty data — a wrong or rotated key must surface
loudly, not look like "no data yet."

`config.DEBUG_DUMPS=true` (default off) enables `storage._dump_debug()`,
called from several places in `untis_client.py` when WebUntis returns an
unexpected shape. This writes **plaintext, unencrypted** JSON to
`data/<user_id>/debug/` — bypasses all encryption entirely, dev-only, must
stay off in production.

## Database Schema

Single SQLite file (`DATA_DIR/app.db`), WAL mode, foreign keys on. Schema is
versioned via an in-code migration list (`app/db.py`), applied automatically
at startup.

| Table | Columns | Purpose |
|---|---|---|
| `users` | `id, username (unique), password_hash, is_admin, created_at` | Accounts |
| `user_settings` | `user_id (PK), untis_host/school/user, untis_pass_enc, scrape_day, scrape_time, ihk_host/user, ihk_pass_enc, ihk_ausbabschnitt, ihk_ausb_mail, ihk_use_settings_for_abschnitt, start_date, updated_at` | Per-user config; both `*_pass_enc` columns are master-key-encrypted |
| `sessions` | `session_id (PK), user_id, created_at, expires_at` | Login sessions, 30-day expiry |
| `schema_migrations` | `version (PK), applied_at` | Migration bookkeeping |
| `week_data` | `user_id, week_id, payload_enc, updated_at` (PK: user_id+week_id) | Scraped week JSON, DEK-encrypted |
| `ihk_history` | `user_id (PK), payload_enc, updated_at` | One-time backfilled IHK entry archive, DEK-encrypted |
| `ihk_status` | `user_id (PK), payload_enc, updated_at` | Last-synced IHK status/lfdnr map, DEK-encrypted |
| `local_fields` | `user_id (PK), payload_enc, updated_at` | Locally-remembered ausbinhalt1/2 text, DEK-encrypted |
| `user_keys` | `user_id (PK), dek_wrapped, created_at` | Each user's DEK, master-key-wrapped |

`app/storage.py` is the only module allowed to read/write these encrypted
tables for real user data (via `app/db.py`'s `*_row` functions) — it never
touches a filesystem path for real data.

## How Scraping Works

1. JSON-RPC `authenticate` → session cookie + `personId`
2. `GET /WebUntis/api/token/new` → bearer token for REST calls (non-fatal if
   this fails — `teaching_content()` just returns `""` for every lesson
   without a token)
3. JSON-RPC `getTimetable` → all lessons for the requested week
4. Per lesson, `GET /WebUntis/api/rest/view/v2/calendar-entry/detail` →
   `teachingContent`

On an unexpected response shape at any of these steps, if
`DEBUG_DUMPS=true`, the raw payload is dumped to `data/<user_id>/debug/` for
diagnosis (see Encryption Model above — off by default, unencrypted when on).

### Lesson merging

`scrape_week()` merges two consecutive lessons when they're on the same day,
same subject, **and have the same teaching content** — the merged lesson's
`end` time extends to the later one's. (`scraper.py`'s own module docstring
describes the merge key as `(date, subject, start)`; the actual code merges
on `(date, subject, content)`, with no comparison against `start` at all —
trust the code, the docstring is stale.) Periods with `code: "cancelled"`
are dropped before any merging happens.

### Four empty-week flags

When a week has no usable (non-cancelled) lessons, `scrape_week()` tries to
say why, in this order (each returns immediately, so they're mutually
exclusive in practice):

1. **`unavailable`** — `getTimetable` error `-7004`: the week is beyond
   WebUntis's publish horizon. Saved unless real lesson data already exists
   for that week (never overwrites a better answer with a worse guess).
2. **`holiday`** — confirmed via `getHolidays` (`_is_full_holiday_week`), or
   via `getSchoolyears` when the week sits in the gap between two school
   years (`_is_between_school_years`) — both pure functions, no hardcoded
   dates, derived entirely from WebUntis's own data. The `getHolidays` path
   is saved unconditionally; the `getSchoolyears` path is saved unless real
   lesson data already exists.
3. **`schoolYearBoundary`** — `getTimetable` error `-8507` fired, but
   neither holiday check above could confirm it either way (both can
   themselves fail right at the exact boundary, observed live as error
   `-8998`). A last-resort honest guess. Saved unless real data exists.
4. **`allCancelled`** — `getTimetable` returned real periods, but every one
   was `cancelled` (e.g. the first week of a new school year before the real
   schedule is active), and it's not a confirmed holiday/gap either. Unlike
   the other three, this still flows through the frontend's normal
   text/submit path — the Berufsschule box just shows "Alle Stunden
   abgesagt" and stays submittable. Saved unless real data exists.

If none apply (a genuinely empty response with no explanation available),
nothing is saved — a later scrape can pick it up once WebUntis has more to
say. `_has_real_lessons(user_id, week_id)` is the guard used throughout: a
previous placeholder guess is always safe to overwrite with a
better-informed one, but real saved lesson data is never overwritten by a
placeholder.

## How IHK Submission Works

The IHK tibrosBB portal is an old-style website (JSP/Tomcat), not a REST
API. The app talks to it with plain HTTP requests, like a browser would, but
without running any JavaScript.

1. **Login** — POST username/password, portal replies with a session cookie.
2. **List existing entries** — the portal's own week-list page tells the app
   which weeks already have an entry and whether each is locked ("genehmigt"
   = approved). This must always happen before touching one specific entry —
   skipping it makes the portal reject the next request even though the
   session is still valid.
3. **Find or create the right entry** — if the week already has an
   unlocked entry, reuse it. If not, the portal can only ever create the
   **next** sequential week ("Neuer Eintrag" has no "create week X" option —
   it can't skip ahead). Clicking it doesn't save anything by itself; it
   only opens a blank draft form (`lfdnr='0'`) that isn't real until saved.
4. **Save** — fills in the form and submits with the portal's "Speichern"
   button. Never clicks "Speichern & Senden" (send for approval) — that step
   is always done by hand on the real site. After saving, the app
   independently re-fetches the entry and verifies the text actually
   changed: a save can report HTTP 200 while silently doing nothing (a stale
   `ausbinhalt13` optimistic-concurrency field causes a silent server-side
   rejection), so this check always runs rather than trusting the response.

Key implementation details worth knowing before touching `ihk_client.py`:

- The portal's submit buttons (`save`, `sent`, `neu`) have no `value=`
  attribute, so a real browser — and this client — submits them as an empty
  string, not their label text.
- `save_entry()` defaults `ausbinhalt1`/`ausbinhalt2` to the entry's current
  value when the caller passes `None`, never to `""` — hardcoding `""` used
  to silently wipe manually-entered content on the real site (a real
  production bug, now regression-tested).
- For a brand-new draft, the newly-assigned `lfdnr` is discovered by diffing
  `list_entries()` immediately before and after the **save** POST — not
  around "Neuer Eintrag" itself, which never changes the list on its own.

## Configuration

Set these in `.env` or as environment variables.

**Live — actually read on a real request path:**

| Variable | Default | Purpose |
|---|---|---|
| `SECRET_ENCRYPTION_KEY` | (required) | Master Fernet key; wraps every per-user DEK and encrypts saved WebUntis/IHK passwords |
| `ADMIN_USERNAME` | `admin` | Username for the auto-bootstrapped first admin |
| `ADMIN_PASSWORD` | (empty) | If set, gates automatic admin-account creation at startup |
| `DATA_DIR` | `/data` | Where `app.db` (and, if enabled, debug dumps) live |
| `DEBUG_DUMPS` | off | Dev-only: dump raw WebUntis API responses to disk, unencrypted |

**Legacy/test-only — module-level defaults, only used as a fallback when a
function is called with `settings=None` (which no route in `main.py` ever
does; every request builds and passes real per-user settings). In practice
only exercised by the test suite and by `UserSettings.from_config()`:**

`UNTIS_HOST`, `UNTIS_SCHOOL`, `UNTIS_USER`, `UNTIS_PASS`, `IHK_HOST`,
`IHK_USER`, `IHK_PASS`, `IHK_AUSBABSCHNITT`, `IHK_AUSB_MAIL`, `SCRAPE_DAY`,
`SCRAPE_TIME`.

There is no `SUBJECT_FILTER` variable in the current codebase — an earlier
version had one; it was never carried over and isn't read anywhere in
`config.py` today.

## REST API

All endpoints except `POST /api/auth/login` require a valid `session` cookie
(httponly, samesite=lax, 30-day expiry, set by login). Admin endpoints
additionally require `is_admin`. Errors from WebUntis surface as
`scraper.ScrapeError` → HTTP 502; errors from the IHK portal as
`ihk_client.IhkError` → HTTP 502; anything else unexpected → 500.

### Auth

**`POST /api/auth/login`** — `{username, password}` → `{ok, username,
is_admin}`, sets the session cookie. 401 on bad credentials.

**`POST /api/auth/logout`** — clears the session cookie and deletes the
session row server-side (`db.delete_session()`), so the session id can't be
replayed after logout.

**`GET /api/auth/whoami`** → `{username, is_admin}`.

### Admin

**`POST /api/admin/users`** (admin) — `{username, password, is_admin?}` →
`{ok, id, username}`. 409 if the username is taken.

**`GET /api/admin/users`** (admin) → list of `{id, username, is_admin,
created_at}`.

### Settings

**`GET /api/me/settings`** → all settings fields; passwords are never
returned in plaintext, only as `untis_pass_set`/`ihk_pass_set` booleans.

**`PUT /api/me/settings`** — any subset of: `untis_host`, `untis_school`,
`untis_user`, `untis_pass`, `scrape_day`, `scrape_time`, `ihk_host`,
`ihk_user`, `ihk_pass`, `ihk_ausbabschnitt`, `ihk_ausb_mail`,
`ihk_use_settings_for_abschnitt`, `start_date` (validated as `YYYY-MM-DD`).
Only non-null fields are updated → `{ok:true}`.

**`PUT /api/me/password`** — `{current_password, new_password}` → `{ok:true}`;
401 if `current_password` is wrong.

**`POST /api/test/untis`** / **`POST /api/test/ihk`** — same body shape as
`PUT /api/me/settings`; tests a connection using the provided values merged
over the user's saved settings, without persisting anything → `{ok,
message}` or 400/502/500.

### Weeks & scraping

**`GET /api/weeks`** → `{weeks: [...], current: "2026-W29", startWeek:
"2026-W10" | null}` — saved week ids, the current ISO week, and the
user's configured Berichtsheft start week (from `start_date`), if any.

**`GET /api/weeks/{week_id}`** → the saved week JSON (see shape below); 400
on a malformed id, 404 if not scraped yet.

**`POST /api/scrape`** — `{week?: "2026-W29"}` (default: current week) →
the scraped week JSON. Per-user lock (`"scrape"` kind); 409 if a scrape for
this user is already running. 400 if the week is in the future. After a
successful scrape, best-effort syncs IHK status too (failure there doesn't
fail the response).

Week JSON shape:
```json
{
  "week": "2026-W29", "start": "2026-07-20", "end": "2026-07-26",
  "scrapedAt": "2026-07-28T17:30:00",
  "days": [{"date": "2026-07-20", "lessons": [
    {"date": "2026-07-20", "start": "08:00", "end": "09:30",
     "subject": "DE", "subjectLong": "German", "teacher": "Mr. Schmidt",
     "content": "Chapter 5: Grammar rules"}
  ]}]
}
```
Plus, when applicable, one of `"holiday": true`, `"unavailable": true`,
`"schoolYearBoundary": true`, or `"allCancelled": true` (see "Four empty-week
flags" above), instead of a populated `days` list.

### IHK

**`GET /api/ihk-status`** → `{status: {week_id: {lfdnr, status, syncedAt}}, fields: {week_id: {ausbinhalt1, ausbinhalt2, savedAt}}}`.
`status` is one of `in_bearbeitung` (editable), `genehmigt` (approved,
locked), `warten_auf_genehmigung` (sent, awaiting approval, locked), or
`abgelehnt` (needs correction, editable). Cached snapshot, refreshed only
after a scrape or submit, never a live IHK request. Of these four, three
(`genehmigt`, `in_bearbeitung`, `warten_auf_genehmigung`) have been
confirmed live against the real portal; `abgelehnt` is still an unconfirmed
guess at the portal's own status label.

**`GET /api/ihk-history`** → the one-time-backfilled archive (see
`backfill_ihk_history.py` below) — `{}` if never run. Not kept in sync
automatically.

**`POST /api/submit-ihk`** — `{week, text, ausbinhalt1?, ausbinhalt2?,
ihk_abschnitt_override?, ihk_ausb_mail_override?}` → `{ok:true}`. 400 on a
malformed week id or empty text. Per-user lock (`"submit"` kind); 409 if a
submit for this user is already running. 502 if the week is already
`genehmigt`, or has no entry and isn't the next sequential week. On success,
also best-effort saves the two fields locally and re-syncs IHK status
(neither failure fails the response).

### Bulk operations

**`POST /api/bulkops/scrape-weeks`** — `{startWeek, endWeek}` (inclusive
range) → `{weeks_scraped}`. Scrapes each week in the range sequentially,
using the same per-user `"scrape"` lock per iteration; a failure on one week
is logged and skipped, doesn't abort the rest.

**`GET /api/bulkops/scrape-progress`** → `{current, total, week}` (all zero/
null if nothing is running) — polled by the UI while a bulk scrape is in
flight.

**`POST /api/bulkops/backfill-ihk`** — empty body → `{entries_scraped}`.
Fetches every existing IHK entry (status + full `ausbinhalt1`/`ausbinhalt2`
text) and overwrites the user's entire `ihk_history` row. Same operation as
the CLI (`app/backfill_ihk_history.py`), runnable from the UI.

## Automatic Scheduling

A single background thread (`scheduler()`, started at app startup) loops
every 60 seconds. Each iteration:

1. Queries all users whose `scrape_day != 'off'`.
2. For each, checks `_scrape_due(now, last_run_date, scrape_day,
   scrape_hour, scrape_minute)` — a pure function, unit-tested directly
   rather than only verified by waiting for a real Sunday.
3. If due, acquires that user's `"scrape"` lock non-blocking (via
   `_get_lock(user_id, "scrape")` — a per-`(user_id, kind)` dict of
   `threading.Lock`s, replacing what used to be one global `scrape_lock`/
   `submit_lock` pair) and scrapes the current **and previous** ISO week
   (`_scheduled_scrape`) — catches Lehrstoff teachers add late for a week
   that's already over. Each week is scraped independently; one failing
   doesn't block the other. Also best-effort syncs IHK status afterward.

**Caveat**: the "already ran today" tracking (`last_run: {user_id: date}`)
is an in-memory dict, **not persisted**. A container restart right around
the scheduled time can cause a duplicate scrape that same day. Harmless
(re-scraping is idempotent) but worth knowing.

Timezone comes from the container (`TZ` in `compose.yaml`, default
`Europe/Berlin`) — must match what users expect their `scrape_day`/
`scrape_time` to mean.

## Run Tests

### Build for Testing

```bash
docker compose build
```

### Run Tests

```bash
docker run --rm \
  -v "$PWD/tests":/srv/tests \
  -v "$PWD/pytest.ini":/srv/pytest.ini \
  --entrypoint bash \
  bab2-berichtsheft \
  -c "pip install -q pytest==8.3.4 httpx==0.28.1 && cd /srv && pytest -q"
```

(The image only installs `requirements.txt`, not `requirements-dev.txt`, so
pytest isn't present in the built image by default — this command installs
it ad-hoc into the throwaway test container.)

Or use a simpler method on your computer:

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```

### Test Files

Tests are in the `tests/` directory:

| File | Tests |
|------|-------|
| `conftest.py` | Shared test setup and fixtures |
| `test_scraper.py` | Scraper functions and error handling (39 tests) |
| `test_api.py` | REST API endpoints, including IHK routes (38 tests) |
| `test_ihk_submitter.py` | IHK client and submit logic (18 tests) |

95 tests total.

### How Tests Authenticate

Tests log in through the real HTTP auth flow, not a test-only bypass.
`conftest.py` sets `SECRET_ENCRYPTION_KEY`/`DATA_DIR`/`ADMIN_*` env vars to
isolated test values before `app.main` is imported, then:

- `admin_client` logs in as the bootstrapped admin (`db.bootstrap_admin_if_needed()`,
  triggered by FastAPI's startup event) via the real `POST /api/auth/login`.
- `new_user` creates a fresh, uniquely-named non-admin user via the real
  `POST /api/admin/users` (using `admin_client`), then logs it in the same way.
  Use this fixture for anything that hits the HTTP API as a normal user.
- `user_settings` (built on `new_user`) seeds dummy WebUntis/IHK creds via the real
  `PUT /api/me/settings` and returns a `UserSettings` for tests that call
  `scraper`/`ihk_submitter`/`storage` functions directly instead of through HTTP.
- `unauth_client` is a session-less client, for testing 401 behavior.

There are no test-only endpoints and no fixtures that reach into the DB to bypass
auth or password hashing.

## Write New Code

### Code Style

- Use type hints for function arguments and returns
- Keep functions short (under 30 lines when possible)
- Use descriptive variable names
- Add comments only for complex logic

### Add a New API Endpoint

1. Import the needed modules at the top of `app/main.py`
2. Define a Pydantic model for the request body (if needed)
3. Add the endpoint function with `Depends(auth.require_user)` (or
   `require_admin`) unless it's genuinely public
4. Build a per-request `UserSettings` via `db._row_to_settings(...)` if the
   endpoint touches scraping/IHK/storage — never call anything with
   `settings=None` on a real request path
5. Add tests in `tests/test_api.py`, using the `new_user`/`unauth_client`
   fixtures from `conftest.py`

### Add Scraper Logic

1. Add the function to `app/scraper.py` (orchestration) or `app/untis_client.py` (a new WebUntis API call)
2. Use the `UntisClient` class (`app/untis_client.py`) to make WebUntis API calls
3. Handle `ScrapeError` exceptions
4. Add tests in `tests/test_scraper.py`

### Add IHK Logic

1. Add the function to `app/ihk_submitter.py` (orchestration) or `app/ihk_client.py` (a new portal call)
2. Use the `IhkClient` class (`app/ihk_client.py`) to make IHK portal calls
3. Handle `IhkError` exceptions
4. Never call the portal's "Speichern & Senden" (send for approval) automatically — only "Speichern" (save)
5. Never read entry content back from the portal to show it in this app's own UI — this app only ever writes to IHK
6. Add tests in `tests/test_ihk_submitter.py`

## Common Problems

### Problem: A user's WebUntis/IHK scraping fails with a credentials error

**Cause:** That user hasn't set WebUntis/IHK username+password.

**Fix:** Log in as that user, go to **Settings**, fill in and save the
relevant tab. (Not a `.env` change — credentials are per-user now.)

### Problem: "WebUntis RPC authenticate failed"

**Cause:** The username or password is wrong for that user's WebUntis account.

**Fix:** Check and re-save the WebUntis credentials in **Settings**.

### Problem: "no data for this week"

**Cause:** That week hasn't been scraped yet for this user.

**Fix:** Click "Jetzt abrufen" in the UI, or `POST /api/scrape`.

### Problem: Debug files in `data/<user_id>/debug/`

**Cause:** WebUntis returned an unexpected response, and `DEBUG_DUMPS=true`
is set.

**Fix:** Look at the JSON file — it shows the raw response. Common causes:
WebUntis API changed, a WebUntis-side server error, or a network problem.
Check the WebUntis status page if the server seems down.

### Problem: Scheduler does not run scrape for a user

**Cause:** That user's `scrape_day` (Settings → Scraper) is `off`, or the
container restarted right around the scheduled time (see the `last_run`
caveat under "Automatic Scheduling").

**Fix:** Check and set `scrape_day`/`scrape_time` in Settings.

### Problem: "IHK_USER / IHK_PASS not set"

**Cause:** That user hasn't set IHK tibrosBB credentials.

**Fix:** Settings → IHK tab. Without them, "Bei IHK einreichen" won't work
for that user.

### Problem: "is already genehmigt (locked) - cannot submit"

**Cause:** That week's IHK entry was already approved. Approved entries cannot be changed.

**Fix:** Nothing to fix — this is expected. Pick a different week.

### Problem: "has no entry and is not the next sequential week"

**Cause:** You tried to submit a week further ahead than the next one IHK expects. The portal only ever lets you create the next week in order — it cannot skip ahead.

**Fix:** Submit the earlier missing weeks first, one at a time, in order. The web UI already disables the button for a week you cannot submit yet and explains which week to submit first.

### Problem: IHK button is missing for a week

**Cause:** Either the week is already `genehmigt` (locked), or it is further in the future than the current week (nothing to report yet).

**Fix:** Nothing to fix — this is expected. Check the status badge next to the week.

### Problem: Wrong timezone for scheduling

**Cause:** Container timezone does not match your timezone.

**Fix:** Edit `compose.yaml` and set the correct `TZ` value:

```yaml
environment:
  TZ: Europe/Berlin
```

Common values:
- `Europe/Berlin` — Germany
- `Europe/London` — UK
- `America/New_York` — US Eastern
- `America/Los_Angeles` — US Pacific
- `Asia/Tokyo` — Japan

## Make Changes

### Edit Python Code

1. Edit the file in your text editor
2. Build the Docker image: `docker compose build`
3. Restart the app: `docker compose restart`
4. Test your changes manually in the web UI or with curl
5. Run the test suite

### Edit Configuration

1. Edit `.env`
2. Restart the app: `docker compose restart`

Configuration changes take effect immediately.

### Edit Static Files

Static files are HTML, CSS, and JavaScript in the `static/` directory. Each
page (`login.html`, `index.html`, `settings.html`, `bulkops.html`) is
self-contained — there is no shared `api.js` or common fetch wrapper; each
duplicates its own `fetch()` calls.

1. Edit the file
2. Refresh the web page in your browser
3. No rebuild needed

If you change HTML structure, test thoroughly in your browser.

## Debug

### View Application Logs

```bash
docker compose logs -f berichtsheft
```

The `-f` flag shows new logs as they happen.

### Check Debug Dumps

If the scraper failed and `DEBUG_DUMPS=true` is set, look in `data/<user_id>/debug/`:

```bash
ls -la data/<user_id>/debug/
cat data/<user_id>/debug/20260728-173000-rpc-getTimetable.json
```

Debug files show the raw API response. They help you understand what went wrong.

### Test a Single Function

Use Python interactively:

```bash
docker compose exec berichtsheft python
```

Then:

```python
from app import scraper
week_id = scraper.current_week_id()
print(week_id)
```

Exit with `exit()`.

## Deploy

The app runs in Docker. Follow these steps:

1. Build the image:
   ```bash
   docker compose build
   ```

2. Start the app:
   ```bash
   docker compose up -d
   ```

3. Check the logs:
   ```bash
   docker compose logs -f
   ```

4. Wait for the app to start (about 5 seconds)

5. Test the API (will 401 without a session cookie — that's expected):
   ```bash
   curl -i http://localhost:8001/api/weeks
   ```

`compose.yaml` binds the container's port 8000 to `0.0.0.0:8001` on the
host — reachable from other machines on the network, not just localhost.

## Performance

The app is designed to be lightweight:

- Scraping one week takes about 5-10 seconds
- API responses are very fast (under 100ms)
- The app uses minimal CPU and memory
- Data is stored in a single SQLite file — no separate database service needed

## Security Notes

- Sessions: opaque random tokens (`secrets.token_urlsafe(32)`), stored
  server-side in the `sessions` table, set as an httponly, samesite=lax
  cookie, 30-day expiry.
- Passwords: PBKDF2-HMAC-SHA256, 260,000 iterations, random 32-byte salt per
  password.
- Per-user data (scraped weeks, IHK history/status/fields, WebUntis/IHK
  passwords) is encrypted at rest — see "Encryption Model" above.
- `auth.verify_password` uses `hmac.compare_digest` for the hash comparison
  (constant-time, avoids a timing side-channel). `POST /api/auth/logout`
  invalidates the session server-side, not just the client-side cookie.
- Protect `.env` — it holds `SECRET_ENCRYPTION_KEY` and `ADMIN_PASSWORD`.
  Never commit it; it's already gitignored.
- `DEBUG_DUMPS=true` writes plaintext, unencrypted API responses to disk —
  never enable it in production.

## Multi-User Operations

### Creating Additional User Accounts

Once logged in as admin:

1. Go to **Settings** (top nav) → **Admin** tab → **Benutzer verwalten**
2. Fill in username, password, optionally mark as admin
3. Click "Erstellen" (Create)

This tab is only visible to admins (`GET /api/auth/whoami`'s `is_admin`
gates it client-side; the endpoints themselves are also admin-only
server-side). There's no edit or delete UI for users currently — only create
and list.

Or via CLI:
```bash
docker compose run --rm --entrypoint python berichtsheft -m app.create_admin
```

### Setting WebUntis/IHK Credentials Per-User

Each user logs in with their own app login, then:

1. Go to **Settings**
2. **WebUntis tab**: host, username, password (password field left blank on
   reload to keep the existing saved value — only overwritten if you type a
   new one)
3. **IHK tab**: host, username, password (same blank-keeps-existing
   behavior), optionally `ihk_ausbabschnitt`/`ihk_ausb_mail` overrides for a
   brand-new entry, and a checkbox to always use these values instead of
   being asked per submission
4. **Scraper tab**: `scrape_day`/`scrape_time`, and `start_date` (weeks
   before this date won't be shown or scrapable)
5. Click "Speichern" (Save) on each tab independently

Each user's credentials are encrypted at rest with `SECRET_ENCRYPTION_KEY`
(see Encryption Model). The scheduler scrapes for each user independently
based on their own `scrape_day`/`scrape_time` settings.

## Contact and Support

For questions or problems:

1. Check `data/<user_id>/debug/` for error details (if `DEBUG_DUMPS=true`)
2. Review the logs with `docker compose logs`
3. Test the WebUntis/IHK connection from **Settings** (the "Test Connection" buttons)
4. Verify `.env` has `SECRET_ENCRYPTION_KEY` set and valid

## Related Documentation

- `README.md` — Quick start guide
- `AGENTS.md` — Dense reference for AI agents working on this codebase
- WebUntis API — Official documentation from your school
