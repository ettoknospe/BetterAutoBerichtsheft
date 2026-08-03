# Berichtsheft

Scrapes Lehrstoff (teaching content) from WebUntis via its JSON/REST API and
serves a small week-based viewer. One Docker container, runs on amd64 and
Raspberry Pi (arm64).

## Run

```bash
cp .env.example .env   # fill in UNTIS_USER / UNTIS_PASS
docker compose up -d --build
```

Open http://localhost:8001 (on the Pi: http://<pi-ip>:8001).

- Arrows / dropdown switch weeks, weeks without data can be scraped on demand
  with the **Jetzt scrapen** button.
- Auto-scrape of the current **and previous** ISO week every Sunday 18:00
  (`SCRAPE_DAY` / `SCRAPE_TIME`) — catches Lehrstoff teachers enter after the week
  is already over.
- Data lives as one JSON per ISO week in `./data/` (bind-mounted volume).
- The "Formatierter Text" block at the bottom is the deduped, copy-paste-ready
  Berichtsheft text; **Text kopieren** copies it.

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
- **Alle Stunden in dieser Woche wurden abgesagt** — real lessons exist but every one is
  cancelled (e.g. the first week of a new school year before the schedule is active).
- **Kann noch nicht abgerufen werden** — the week is further in the future than WebUntis
  currently allows querying; try again later.

## API

- `GET /api/weeks` — available weeks + current ISO week
- `GET /api/weeks/2026-W29` — data for one week
- `POST /api/scrape` — body `{"week": "2026-W29"}` (optional, default current week)

## Tests

```bash
docker compose build
docker run --rm -v "$PWD/tests":/srv/tests -v "$PWD/pytest.ini":/srv/pytest.ini \
  --entrypoint bash bab2-berichtsheft \
  -c "pip install -q pytest==8.3.4 httpx==0.28.1 && cd /srv && pytest -q"
```
