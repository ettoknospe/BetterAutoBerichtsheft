# AGENTS.md — Berichtsheft

## What this is
Single-container app. Scrapes "Lehrstoff" (teaching content) from WebUntis JSON/REST API and serves a small week-based viewer + auto-generates copy-paste Berichtsheft text. Runs amd64 + arm64 (Pi).

## Layout
- `app/scraper.py` — orchestration only. `scrape_week()` ties together the HTTP client, pure date helpers, and file I/O below to produce and persist one ISO week's lesson data; re-exports names from the split modules for backward compat (tests/`main.py` still reach them as `scraper.X`).
  - **Week-status detection**: when a week has no usable (non-cancelled) lessons, `scrape_week()` tries to explain why instead of silently doing nothing, and persists a `{"days": [], ...}` placeholder with one of four flags (see Gotchas below for the exact flag names/priority). This is the one situation where an empty-lessons week still gets written to disk — every other path only saves when there's real lesson data.
- `app/config.py` — runtime config read from the environment (`UNTIS_HOST`/`SCHOOL`/`USER`/`PASS`, `SUBJECT_FILTER`, `DATA_DIR`). Standalone leaf module, no dependents of its own; other modules do `import config` and read `config.X` at call time (not `from config import X`) so `monkeypatch.setattr(config, "UNTIS_USER", ...)` in tests reaches the code that uses it.
- `app/untis_client.py` — `UntisClient` + `ScrapeError`. Login flow: JSON-RPC `authenticate` → session cookie + personId → `/WebUntis/api/token/new` → bearer token → JSON-RPC `getTimetable` → per-lesson REST `calendar-entry/detail` for `teachingContent`. Debug dumps to `data/debug/` on unexpected API shapes (via `storage.py`).
- `app/time_utils.py` — pure time/week-id helpers: `_hm`, `week_bounds`, `current_week_id`.
- `app/school_calendar.py` — pure holiday/school-year gap detection: `_is_full_holiday_week`, `_is_between_school_years`. No hardcoded dates, derived entirely from WebUntis's own data.
- `app/storage.py` — local file I/O: `_dump_debug` (raw API dumps to `data/debug/`) and `_has_real_lessons(path)`, which guards every placeholder write — a previous placeholder guess is always safe to overwrite with a better-informed one later (e.g. once `getSchoolyears` data becomes available), but a file with real saved lessons is never overwritten by a placeholder.
- `app/ihk_client.py` — `IhkClient` + `IhkError`. Plain HTTP against the IHK tibrosBB portal (classic JSP/Tomcat, not a REST API). `login()`/`logout()`, `list_entries()` (parses `azubiHeft.jsp`'s week list into `{week_id: {lfdnr, status}}` — the *only* source of truth for which weeks exist/are locked), `create_next_entry()` (POSTs "Neuer Eintrag" — returns an unpersisted DRAFT dict, `lfdnr='0'`), `fetch_entry(lfdnr)`, `save_entry(entry, ausbinhalt3, ausbinhalt1=None, ausbinhalt2=None)` (always re-fetches after POSTing to independently verify the save actually persisted, never trusts the response — see Gotchas). `_parse_entry_form()` is the shared BeautifulSoup field-extraction helper for both an existing entry and a draft.
- `app/ihk_submitter.py` — IHK orchestration, separate from `scraper.py` (different external system, no shared logic). `submit_week(week_id, formatted_text, ausbinhalt1=None, ausbinhalt2=None)` finds-or-creates the right entry and saves into it; refuses a `genehmigt` (locked) week and refuses to skip ahead of the next sequential week. `sync_status()` refreshes `data/ihk_status.json` (status/lfdnr metadata only — deliberately never fetches content, see Gotchas); `load_status()` reads it back, `{}` if never synced.
- `app/main.py` — FastAPI app. Routes: `GET /api/weeks`, `GET /api/weeks/{week_id}`, `POST /api/scrape`, `GET /api/ihk-status`, `POST /api/submit-ihk`. Background thread `scheduler()` does weekly auto-scrape (`SCRAPE_DAY`/`SCRAPE_TIME` env, default Sunday 18:00 container TZ), scraping **current + previous ISO week** each run (`_weeks_to_scrape()`/`_scheduled_scrape()`) — catches Lehrstoff teachers add late for the week that just ended. Each week is scraped independently (`_scheduled_scrape`); one failing doesn't block the other. Due-check logic (`_scrape_due()`) is a pure function, unit tested in `tests/test_api.py` rather than only verified by waiting for a real Sunday. `_sync_ihk_status_best_effort()` runs after every scrape (manual and scheduled) and every submit — a sync failure never breaks the response it's piggybacked on. `submit_lock` is a separate `threading.Lock()` from `scrape_lock`, same 409-on-concurrent pattern. Serves `static/` at `/`.
- `static/index.html` — single-page vanilla JS/CSS viewer, no build step, no framework. `renderWeek()` builds one merged card: two writable textareas (`ausbinhalt1`/`ausbinhalt2`, always blank on load — one-way flow, never pre-filled from IHK) + the read-only "Berufsschule" text. The IHK button is tri-state (`enabled`/`disabled` with a `title` reason/`hidden`), computed from `ihkStatus` + `nextSubmittableWeek()`; also force-hidden for any week after `currentWeek` (nothing to submit yet). `IHK_BADGE_LABELS` maps the 4 known statuses to German label + CSS class.
- `data/*.json` — one file per ISO week (`YYYY-Www.json`), bind-mounted volume, gitignored.
- `data/ihk_status.json` — `{week_id: {lfdnr, status, syncedAt}}`, refreshed only after a scrape or submit (never on navigation). Status only, no content fields — see one-way-flow gotcha below.
- `data/debug/` — raw API dumps on scrape failure/unexpected shape.
- `compose.yaml` — single service, builds from `Dockerfile`, binds `127.0.0.1:8001:8000` (host-side localhost-only), `.env` for secrets.
- `Dockerfile` — python:3.12-slim, installs `requirements.txt`, copies `app/` + `static/`, runs uvicorn on 8000 internal.
- `.env` (gitignored, real creds) / `.env.example` (template) — `UNTIS_USER`, `UNTIS_PASS`, `UNTIS_HOST`, `UNTIS_SCHOOL`, `SUBJECT_FILTER`, `SCRAPE_DAY`, `SCRAPE_TIME`, `IHK_USER`, `IHK_PASS`, `IHK_HOST`, `IHK_AUSBABSCHNITT`, `IHK_AUSB_MAIL`.

## Conventions
- No linter config. Keep it that way unless asked — this is a small personal-use tool, don't over-engineer.
- **Tests are required for further changes to pass.** `tests/test_scraper.py` (40 tests) covers pure scraper logic (`_hm`, `week_bounds`, `current_week_id`, merging/filtering in `scrape_week`, credential check) and all four empty-week flags (`unavailable`/`holiday`/`schoolYearBoundary`/`allCancelled`, including priority order and the `_has_real_lessons` overwrite guard) with WebUntis network calls stubbed via monkeypatch. `tests/test_api.py` (32 tests) covers all five routes (`/api/weeks`, `/api/weeks/{id}`, `/api/scrape`, `/api/ihk-status`, `/api/submit-ihk`) via FastAPI `TestClient`, with `scraper.scrape_week`/`ihk_submitter.submit_week`/`sync_status` monkeypatched so no real network calls happen. `tests/test_ihk_submitter.py` (15 tests) covers `IhkClient`/`ihk_submitter` at the method-stub level (`fake_ihk` fixture mirrors `fake_untis`) — mostly regression tests for real bugs hit live against the portal (silent-save, field-clobber, draft/lfdnr diff), see Gotchas. 87 tests total. Run any change through this suite before considering it done, and add cases for new behavior.
- Week id format everywhere: `YYYY-Www` (e.g. `2026-W29`), validated via `WEEK_RE = r"^\d{4}-W\d{2}$"` in both scraper and API layer — keep both in sync if changed.
- Errors from WebUntis surface as `scraper.ScrapeError`; `main.py` maps that to HTTP 502, unexpected exceptions to 500. Errors from the IHK portal surface as `ihk_client.IhkError`, mapped the same way.
- Debug dumps (`_dump_debug`) are the primary diagnostic tool when WebUntis changes its API shape — check `data/debug/` before assuming code bugs on scrape failures.

## Running / verifying changes
```bash
cd /Users/tim/claude/BAB2
docker compose up -d --build
curl -s http://127.0.0.1:8001/api/weeks
```
No `.env` secrets should ever be committed — already gitignored, double check before any commit touching it.

## Running tests
Tests run inside the real project container (not an ad-hoc image), so they exercise the exact same base image/deps as prod:
```bash
docker compose build
docker run --rm \
  -v "$PWD/tests":/srv/tests \
  -v "$PWD/pytest.ini":/srv/pytest.ini \
  --entrypoint bash \
  bab2-berichtsheft \
  -c "pip install -q pytest==8.3.4 httpx==0.28.1 && cd /srv && pytest -q"
```
`requirements-dev.txt` documents the pinned dev deps (pytest, httpx) if you want a persistent test image/venv instead of installing ad-hoc. `pytest.ini` sets `testpaths = tests`; `tests/conftest.py` puts `app/` on `sys.path` (mirrors uvicorn's `--app-dir app`) and forces `SCRAPE_DAY=off` before `main.py`'s import-time background thread starts.

## Gotchas
- WebUntis bearer token endpoint (`/WebUntis/api/token/new`) can silently fail (non-200 or oversized body) — code already guards this (`teaching_content` returns `""` if no token), don't remove that guard.
- `SUBJECT_FILTER` empty string vs unset both mean "no filter" — comma-split-and-strip already handles both.
- Port is bound to `127.0.0.1:8001` on host, not `0.0.0.0` — intentional, not a bug, don't "fix" without asking.
- **Four distinct empty-week flags**, checked in this priority order in `scrape_week()` (each shown as its own placeholder in `static/index.html`'s `renderWeek()`):
  1. `"unavailable": true` — `getTimetable` error `-7004` ("no allowed date"): the week is beyond WebUntis's publish horizon (too far in the future). Not fixable now, no further checks attempted, just surfaced plainly. Re-scraping later will pick up real data once WebUntis opens that window.
  2. `"holiday": true` — confirmed via `getHolidays` (`UntisClient.holidays()` + `_is_full_holiday_week()`), or as a second attempt via `getSchoolyears` (`UntisClient.school_years()` + `_is_between_school_years()`, no hardcoded dates — derived entirely from WebUntis's own school-year boundaries) when the week sits in the gap between two school years. Either path shows plain "Schulferien".
  3. `"schoolYearBoundary": true` — `getTimetable` error `-8507` ("start/end not within a single school year") fired, but neither `getHolidays` nor `getSchoolyears` could confirm it either way (both can themselves fail right at the exact boundary moment — observed live as error `-8998`, "sy is null"). Last-resort honest guess, shown as "Wahrscheinlich Schuljahrwechsel".
  4. `"allCancelled": true` — `getTimetable` succeeded and returned real periods, but every one had `code: "cancelled"` (e.g. first week of a new school year before the real schedule is active) and it's not a confirmed holiday/gap either. Unlike the other three flags, this one still flows through `renderWeek()`'s normal text/submit path (`noSchedule` in the frontend) rather than a dead-end placeholder — the Berufsschule box just shows "Alle Stunden abgesagt" and stays submittable. Real case hit live: 2026-W36, the literal first day of the 2026/2027 school year.
  - If none of these apply (e.g. `getTimetable` returns a genuinely empty list with no explanation available), nothing is saved, so a later scrape can pick it up once WebUntis has more to say about it.

## IHK submission — key mechanics

`app/ihk_client.py`'s module docstring has the full debugging story; the load-bearing points:

- **Data flows one way only: this app → IHK, never the reverse.** `sync_status()` deliberately fetches status/lfdnr metadata only, never `ausbinhalt1/2/3` content. The UI's two writable textareas always start blank, never pre-filled from a live fetch — this was a real feature that got built and then fully reverted per an explicit user requirement. Don't reintroduce a read-back/pre-fill path without asking.
- **Never click "Speichern & Senden" (`sent`) automatically** — only ever `save`. Sending for approval is a one-way step toward the locked `genehmigt` state and must stay a manual action on the real site.
- **`<button>` elements with no `value=` attribute submit an empty string**, not their label text, when a real browser clicks them (`save`, `sent`, `neu` all lack `value=`). Check this pattern before wiring any new IHK form field.
- **"Neuer Eintrag" does not persist anything by itself.** It returns a blank DRAFT (`lfdnr='0'`) for what will become the next sequential week; the portal only assigns a real `lfdnr` once that draft is actually saved. `save_entry()` diffs `list_entries()` before/after the *save* POST (not around "Neuer Eintrag") to discover the assigned `lfdnr` — the opposite ordering caused a real production bug (`"expected exactly one new entry ... got set()"`, 2026-W30).
- **`ausbinhalt13` is an optimistic-concurrency field**, not a static placeholder — it mirrors `ausbinhalt3`'s current saved value. A stale one gets silently rejected (HTTP 200, text echoed back, nothing persisted) — always re-scrape it fresh immediately before every save, same as `token`.
- **`save_entry()` defaults `ausbinhalt1`/`ausbinhalt2` to the entry's current value** when the caller passes `None` — never hardcode `""`, that silently wipes manually-entered content on the real site (a real bug, now regression-tested).
- **Never trust a POST response as proof of success** — `save_entry()` always independently re-fetches and compares afterward.
- Only two Berichtsheft-relevant statuses were seen live for a long time (`Nachweis genehmigt`→`genehmigt`, `in Bearbeitung bei Azubi`→`in_bearbeitung`); `Warten auf Genehmigung`→`warten_auf_genehmigung` was confirmed live 2026-08-05 (2026-W29, after a real "Speichern & Senden" on the portal). `abgelehnt` is still an unconfirmed guess at the portal's own filter-checkbox label.
