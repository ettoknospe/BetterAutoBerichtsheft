# AGENTS.md — Berichtsheft

## What this is
Single-container app. Scrapes "Lehrstoff" (teaching content) from WebUntis JSON/REST API (no browser, no Playwright) and serves a small week-based viewer + auto-generates copy-paste Berichtsheft text. Runs amd64 + arm64 (Pi).

## Layout
- `app/scraper.py` — WebUntis client. Login flow: JSON-RPC `authenticate` → session cookie + personId → `/WebUntis/api/token/new` → bearer token → JSON-RPC `getTimetable` → per-lesson REST `calendar-entry/detail` for `teachingContent`. Handles merging consecutive same-subject/same-content lessons, subject filtering, debug dumps to `data/debug/` on unexpected API shapes. If `getTimetable` returns zero periods, calls JSON-RPC `getHolidays` (`UntisClient.holidays()`) and checks `_is_full_holiday_week()` — if the whole Mon–Fri span is covered by a holiday period, persists `{"days": [], "holiday": true}` (this is the one case where an empty-lessons week still gets saved to disk).
- `app/main.py` — FastAPI app. Routes: `GET /api/weeks`, `GET /api/weeks/{week_id}`, `POST /api/scrape`. Background thread `scheduler()` does weekly auto-scrape (`SCRAPE_DAY`/`SCRAPE_TIME` env). Serves `static/` at `/`.
- `static/index.html` — single-page vanilla JS/CSS viewer, no build step, no framework.
- `data/*.json` — one file per ISO week (`YYYY-Www.json`), bind-mounted volume, gitignored.
- `data/debug/` — raw API dumps on scrape failure/unexpected shape.
- `compose.yaml` — single service, builds from `Dockerfile`, binds `127.0.0.1:8001:8000` (host-side localhost-only), `.env` for secrets.
- `Dockerfile` — python:3.12-slim, installs `requirements.txt`, copies `app/` + `static/`, runs uvicorn on 8000 internal.
- `.env` (gitignored, real creds) / `.env.example` (template) — `UNTIS_USER`, `UNTIS_PASS`, `UNTIS_HOST`, `UNTIS_SCHOOL`, `SUBJECT_FILTER`, `SCRAPE_DAY`, `SCRAPE_TIME`.

## Conventions
- No linter config. Keep it that way unless asked — this is a small personal-use tool, don't over-engineer.
- **Tests are required for further changes to pass.** `tests/test_scraper.py` covers pure scraper logic (`_hm`, `week_bounds`, `current_week_id`, merging/filtering in `scrape_week`, credential check, holiday detection via `holidays()`/`_is_full_holiday_week()`) with WebUntis network calls stubbed via monkeypatch. `tests/test_api.py` covers all three routes (`/api/weeks`, `/api/weeks/{id}`, `/api/scrape`) via FastAPI `TestClient`, with `scraper.scrape_week` monkeypatched so no real WebUntis calls happen. Run any change through this suite before considering it done, and add cases for new behavior.
- Week id format everywhere: `YYYY-Www` (e.g. `2026-W29`), validated via `WEEK_RE = r"^\d{4}-W\d{2}$"` in both scraper and API layer — keep both in sync if changed.
- Errors from WebUntis surface as `scraper.ScrapeError`; `main.py` maps that to HTTP 502, unexpected exceptions to 500.
- Debug dumps (`_dump_debug`) are the primary diagnostic tool when WebUntis changes its API shape — check `data/debug/` before assuming code bugs on scrape failures.
- Two on-disk copies of this project exist: `/Users/tim/BAB2/berichtsheft` (no `.git`) and `/Users/tim/claude/BAB2/berichtsheft` (has `.git`, remote `github.com/ettoknospe/BetterAutoBerichtsheft`). **This one (`/Users/tim/claude/BAB2/berichtsheft`) is canonical** — make changes here.

## Running / verifying changes
```bash
cd /Users/tim/claude/BAB2/berichtsheft
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
  berichtsheft-berichtsheft \
  -c "pip install -q pytest==8.3.4 httpx==0.28.1 && cd /srv && pytest -q"
```
`requirements-dev.txt` documents the pinned dev deps (pytest, httpx) if you want a persistent test image/venv instead of installing ad-hoc. `pytest.ini` sets `testpaths = tests`; `tests/conftest.py` puts `app/` on `sys.path` (mirrors uvicorn's `--app-dir app`) and forces `SCRAPE_DAY=off` before `main.py`'s import-time background thread starts.

## Gotchas
- WebUntis bearer token endpoint (`/WebUntis/api/token/new`) can silently fail (non-200 or oversized body) — code already guards this (`teaching_content` returns `""` if no token), don't remove that guard.
- `SUBJECT_FILTER` empty string vs unset both mean "no filter" — comma-split-and-strip already handles both.
- Port is bound to `127.0.0.1:8001` on host, not `0.0.0.0` — intentional, not a bug, don't "fix" without asking.
