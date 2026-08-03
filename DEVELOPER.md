# Berichtsheft Developer Guide

This document explains how to develop and maintain Berichtsheft.

## About Berichtsheft

Berichtsheft is a web application. It gets teaching content from WebUntis. WebUntis is a school scheduling system.

The app uses only HTTP and JSON-RPC. The app stores data as JSON files. One file stores data for each ISO week.

The app runs in a Docker container. It works on amd64 and arm64 computers.

## Project Structure

```
berichtsheft/
├── app/
│   ├── main.py              # FastAPI application and scheduler
│   ├── scraper.py           # Orchestration: scrape_week() ties the rest together
│   ├── config.py            # Runtime config read from the environment
│   ├── untis_client.py      # WebUntis JSON-RPC + REST client (UntisClient, ScrapeError)
│   ├── time_utils.py        # Pure time/week-id helpers
│   ├── school_calendar.py   # Pure holiday/school-year gap detection
│   └── storage.py           # Local file I/O: debug dumps, saved-week checks
├── static/                  # HTML, CSS, JavaScript
├── tests/                   # Test files
├── data/                    # JSON data (one file per week)
├── Dockerfile               # Container definition
├── compose.yaml             # Docker Compose configuration
├── requirements.txt         # Python package dependencies
└── pytest.ini              # Test configuration
```

## Install and Run

### First-Time Setup

1. Copy the example configuration:

```bash
cp .env.example .env
```

2. Edit `.env`. Add your WebUntis username and password:

```
UNTIS_USER=your-username
UNTIS_PASS=your-password
```

3. Build the Docker image:

```bash
docker compose build
```

### Start the Application

```bash
docker compose up -d
```

The app starts on `http://localhost:8001`.

Access the app at:
- `http://localhost:8001/` — web interface
- `http://localhost:8001/api/weeks` — API endpoint for week list

### Stop the Application

```bash
docker compose down
```

## How It Works

The scraper gets teaching content from WebUntis in four steps.

### Step 1: Login

The app sends JSON-RPC `authenticate` to WebUntis. The response contains a session cookie and a `personId`.

### Step 2: Get Token

The app sends HTTP GET to `/WebUntis/api/token/new`. The response contains a bearer token. This token is needed for REST API calls.

### Step 3: Get Timetable

The app sends JSON-RPC `getTimetable` to get all lessons for the week. The response is a list of lessons. Each lesson has a date, time, subject, and teacher.

### Step 4: Get Teaching Content

For each lesson, the app sends HTTP GET to `/WebUntis/api/rest/view/v2/calendar-entry/detail`. This endpoint returns the teaching content for that lesson.

### Handle Errors

If WebUntis returns an unexpected response, the app saves the raw response to `data/debug/`. This helps you diagnose problems.

## Configuration

Set these values in `.env` or as environment variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `UNTIS_HOST` | `le-bk-muenster.webuntis.com` | WebUntis server name |
| `UNTIS_SCHOOL` | `le-bk-muenster` | School ID in WebUntis |
| `UNTIS_USER` | (required) | Your WebUntis username |
| `UNTIS_PASS` | (required) | Your WebUntis password |
| `DATA_DIR` | `/data` | Where to save JSON files |
| `SCRAPE_DAY` | `sun` | Day to auto-scrape (mon-sun, or `off`) |
| `SCRAPE_TIME` | `18:00` | Time to auto-scrape (HH:MM format) |
| `SUBJECT_FILTER` | (empty) | Comma-separated subject names to include. If empty, include all subjects. |

### Use Subject Filter

To include only certain subjects, set `SUBJECT_FILTER`:

```
SUBJECT_FILTER=German, English, Math
```

Only lessons with these subject names will be saved.

## REST API

The app exposes these endpoints:

### List Available Weeks

**Request:**
```
GET /api/weeks
```

**Response:**
```json
{
  "weeks": ["2026-W28", "2026-W29"],
  "current": "2026-W29"
}
```

This returns all weeks that have saved data. It also returns the current ISO week.

### Get Data for One Week

**Request:**
```
GET /api/weeks/2026-W29
```

**Response:**
```json
{
  "week": "2026-W29",
  "start": "2026-07-20",
  "end": "2026-07-26",
  "scrapedAt": "2026-07-28T17:30:00",
  "days": [
    {
      "date": "2026-07-20",
      "lessons": [
        {
          "date": "2026-07-20",
          "start": "08:00",
          "end": "09:30",
          "subject": "DE",
          "subjectLong": "German",
          "teacher": "Mr. Schmidt",
          "content": "Chapter 5: Grammar rules"
        }
      ]
    }
  ]
}
```

If no data exists for the week, return 404.

### Request a Scrape

**Request:**
```
POST /api/scrape
Content-Type: application/json

{
  "week": "2026-W29"
}
```

If you do not send a `week` value, the app scrapes the current week.

**Response:**
```json
{
  "week": "2026-W29",
  "start": "2026-07-20",
  "end": "2026-07-26",
  "scrapedAt": "2026-07-28T17:30:00",
  "days": [...]
}
```

Only one scrape can run at a time. If a scrape is already running, return 409.

## Data Format

The app saves each week as a JSON file:

```
data/2026-W29.json
```

The file has this structure:

```json
{
  "week": "2026-W29",
  "start": "2026-07-20",
  "end": "2026-07-26",
  "scrapedAt": "2026-07-28T17:30:00",
  "days": [
    {
      "date": "2026-07-20",
      "lessons": [
        {
          "date": "2026-07-20",
          "start": "08:00",
          "end": "09:30",
          "subject": "DE",
          "subjectLong": "German",
          "teacher": "Mr. Schmidt",
          "content": "Chapter 5: Grammar rules"
        }
      ]
    }
  ]
}
```

### Status Fields

These fields appear in the JSON when there are no lessons:

| Field | Meaning |
|-------|---------|
| `"holiday": true` | This week is a school holiday |
| `"unavailable": true` | The week is too far in the future. WebUntis has not published data for this week yet. |
| `"schoolYearBoundary": true` | The week spans two school years. The app could not get data. |
| `"allCancelled": true` | All lessons in the week were cancelled. |

### Merging Lessons

The app combines two lessons if:
- They are on the same day
- They have the same subject
- They have the same teaching content

When lessons merge, the `end` time updates to match the later lesson.

## Automatic Scheduling

The app has a background scheduler thread. This thread runs every minute to check if it is time to scrape.

### Schedule Configuration

Use `SCRAPE_DAY` and `SCRAPE_TIME`:

```
SCRAPE_DAY=sun
SCRAPE_TIME=18:00
```

This means: scrape every Sunday at 18:00.

The timezone must match the container timezone. Set the timezone in `compose.yaml`:

```yaml
environment:
  TZ: Europe/Berlin
```

### What Gets Scraped

The scheduler scrapes two weeks:
- The current week
- The previous week (one week earlier)

Teachers sometimes add teaching content after the week has ended. Re-scraping the previous week catches these changes.

### Disable Scheduling

Set `SCRAPE_DAY=off` to disable automatic scraping:

```
SCRAPE_DAY=off
```

When disabled, only manual scrape requests work.

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
| `test_scraper.py` | Scraper functions and error handling |
| `test_api.py` | REST API endpoints |

## Write New Code

### Code Style

Follow these rules:

- Use type hints for function arguments and returns
- Keep functions short (under 30 lines when possible)
- Use descriptive variable names
- Add comments only for complex logic

### Add a New API Endpoint

1. Import the needed modules at the top of `app/main.py`
2. Define a Pydantic model for the request body (if needed)
3. Add the endpoint function with the `@app.get()` or `@app.post()` decorator
4. Return a dict or Pydantic model
5. Add tests in `tests/test_api.py`

Example:

```python
@app.get("/api/health")
def health():
    return {"status": "ok"}
```

### Add Scraper Logic

1. Add the function to `app/scraper.py` (orchestration) or `app/untis_client.py` (a new WebUntis API call)
2. Use the `UntisClient` class (`app/untis_client.py`) to make WebUntis API calls
3. Handle `ScrapeError` exceptions
4. Add tests in `tests/test_scraper.py`

## Common Problems

### Problem: "UNTIS_USER / UNTIS_PASS not set"

**Cause:** You did not set username and password.

**Fix:** Edit `.env` and set `UNTIS_USER` and `UNTIS_PASS`.

### Problem: "WebUntis RPC authenticate failed"

**Cause:** The username or password is wrong.

**Fix:** Check your `.env` file. Make sure the username and password are correct.

### Problem: "no data for this week"

**Cause:** You have not scraped this week yet.

**Fix:** Use the web UI to click "Jetzt scrapen" (Scrape Now). Or send a POST request to `/api/scrape`.

### Problem: Debug files in `data/debug/`

**Cause:** WebUntis returned an unexpected response.

**Fix:** Look at the JSON file in `data/debug/`. It shows the raw response. Find out why the response is wrong. Common causes:
- WebUntis API changed
- Server error on the WebUntis side
- Network problem

Check the WebUntis status page. If the server is down, wait and try again later.

### Problem: Scheduler does not run scrape

**Cause:** `SCRAPE_DAY` is wrong or set to `off`.

**Fix:** Check your `.env`. Set `SCRAPE_DAY` to `sun` (or another day). Make sure `SCRAPE_TIME` is correct.

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

Static files are HTML, CSS, and JavaScript in the `static/` directory.

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

If the scraper failed, look in `data/debug/`:

```bash
ls -la data/debug/
cat data/debug/20260728-173000-rpc-getTimetable.json
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

5. Test the API:
   ```bash
   curl http://localhost:8001/api/weeks
   ```

## Performance

The app is designed to be lightweight:

- Scraping one week takes about 5-10 seconds
- API responses are very fast (under 100ms)
- The app uses minimal CPU and memory
- Data is stored as plain JSON files (no database needed)

## Security Notes

- The WebUntis password is stored in `.env`. Protect this file.
- The app does not use authentication. Run it only on a private network or behind a VPN.
- Do not share `.env` files in version control systems

## Contact and Support

For questions or problems:

1. Check the `data/debug/` directory for error details
2. Review the logs with `docker compose logs`
3. Test the WebUntis API connection
4. Verify that `.env` has correct credentials

## Related Documentation

- `README.md` — Quick start guide
- `AGENTS.md` — How to use Claude agents to work on this project
- WebUntis API — Official documentation from your school
