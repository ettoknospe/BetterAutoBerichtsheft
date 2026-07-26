import datetime as dt
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import scraper

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("app")

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
SCRAPE_DAY = os.environ.get("SCRAPE_DAY", "sun").lower()  # mon..sun, or "off"
SCRAPE_TIME = os.environ.get("SCRAPE_TIME", "18:00")
WEEK_RE = re.compile(r"^\d{4}-W\d{2}$")
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

app = FastAPI(title="Berichtsheft")
scrape_lock = threading.Lock()


class ScrapeRequest(BaseModel):
    week: str | None = None


@app.get("/api/weeks")
def list_weeks():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    weeks = sorted(p.stem for p in DATA_DIR.glob("*-W*.json") if WEEK_RE.match(p.stem))
    return {"weeks": weeks, "current": scraper.current_week_id()}


@app.get("/api/weeks/{week_id}")
def get_week(week_id: str):
    if not WEEK_RE.match(week_id):
        raise HTTPException(400, "bad week id, expected YYYY-Www")
    path = DATA_DIR / f"{week_id}.json"
    if not path.exists():
        raise HTTPException(404, "no data for this week")
    return json.loads(path.read_text())


@app.post("/api/scrape")
def scrape(req: ScrapeRequest):
    week_id = req.week or scraper.current_week_id()
    if not WEEK_RE.match(week_id):
        raise HTTPException(400, "bad week id, expected YYYY-Www")
    if not scrape_lock.acquire(blocking=False):
        raise HTTPException(409, "scrape already running")
    try:
        return scraper.scrape_week(week_id)
    except scraper.ScrapeError as e:
        raise HTTPException(502, str(e))
    except Exception as e:
        log.exception("scrape failed")
        raise HTTPException(500, f"scrape failed: {e}")
    finally:
        scrape_lock.release()


def scheduler():
    """Scrape current week every SCRAPE_DAY at SCRAPE_TIME (container TZ)."""
    if SCRAPE_DAY not in DAYS:
        log.info("scheduler off (SCRAPE_DAY=%s)", SCRAPE_DAY)
        return
    hour, minute = (int(x) for x in SCRAPE_TIME.split(":"))
    last_run_date = None
    log.info("scheduler: every %s at %s", SCRAPE_DAY, SCRAPE_TIME)
    while True:
        now = dt.datetime.now()
        due = (
            DAYS[now.weekday()] == SCRAPE_DAY
            and (now.hour, now.minute) >= (hour, minute)
            and last_run_date != now.date()
        )
        if due and scrape_lock.acquire(blocking=False):
            try:
                last_run_date = now.date()
                scraper.scrape_week(scraper.current_week_id())
            except Exception:
                log.exception("scheduled scrape failed")
            finally:
                scrape_lock.release()
        time.sleep(60)


threading.Thread(target=scheduler, daemon=True).start()

app.mount("/", StaticFiles(directory=Path(__file__).parent.parent / "static", html=True), name="static")
