# Berichtsheft

Scrapes Lehrstoff (teaching content) from WebUntis via its JSON API — no browser,
no Playwright — and serves a small week-based viewer. One Docker container,
runs on amd64 and Raspberry Pi (arm64).

## Run

```bash
cp .env.example .env   # fill in UNTIS_USER / UNTIS_PASS
docker compose up -d --build
```

Open http://localhost:8000 (on the Pi: http://<pi-ip>:8000).

- Arrows / dropdown switch weeks, weeks without data can be scraped on demand
  with the **Jetzt scrapen** button.
- Auto-scrape of the current week every Sunday 18:00 (`SCRAPE_DAY` / `SCRAPE_TIME`).
- Data lives as one JSON per ISO week in `./data/` (bind-mounted volume).
- The "Formatierter Text" block at the bottom is the deduped, copy-paste-ready
  Berichtsheft text; **Text kopieren** copies it.

## How scraping works

1. JSON-RPC `authenticate` → session + personId
2. `/WebUntis/api/token/new` → bearer token
3. JSON-RPC `getTimetable` → lessons of the week
4. Per lesson `GET /WebUntis/api/rest/view/v2/calendar-entry/detail` → `teachingContent`

On unexpected API responses raw payloads are dumped to `data/debug/` for diagnosis.

## API

- `GET /api/weeks` — available weeks + current ISO week
- `GET /api/weeks/2026-W29` — data for one week
- `POST /api/scrape` — body `{"week": "2026-W29"}` (optional, default current week)
