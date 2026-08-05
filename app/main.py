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

import ihk_submitter
import scraper
from ihk_client import IhkError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("app")

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
SCRAPE_DAY = os.environ.get("SCRAPE_DAY", "sun").lower()  # mon..sun, or "off"
SCRAPE_TIME = os.environ.get("SCRAPE_TIME", "18:00")
WEEK_RE = re.compile(r"^\d{4}-W\d{2}$")
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_SCRAPE_HOUR, _SCRAPE_MINUTE = (int(x) for x in SCRAPE_TIME.split(":"))

app = FastAPI(title="Berichtsheft")
scrape_lock = threading.Lock()
submit_lock = threading.Lock()


class ScrapeRequest(BaseModel):
    week: str | None = None


class SubmitIhkRequest(BaseModel):
    week: str
    text: str
    ausbinhalt1: str | None = None
    ausbinhalt2: str | None = None


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
        result = scraper.scrape_week(week_id)
        _sync_ihk_status_best_effort()
        return result
    except scraper.ScrapeError as e:
        raise HTTPException(502, str(e))
    except Exception as e:
        log.exception("scrape failed")
        raise HTTPException(500, f"scrape failed: {e}")
    finally:
        scrape_lock.release()


@app.get("/api/ihk-status")
def ihk_status():
    return ihk_submitter.load_status()


@app.post("/api/submit-ihk")
def submit_ihk(req: SubmitIhkRequest):
    if not WEEK_RE.match(req.week):
        raise HTTPException(400, "bad week id, expected YYYY-Www")
    if not req.text.strip():
        raise HTTPException(400, "text is empty")
    if not submit_lock.acquire(blocking=False):
        raise HTTPException(409, "submit already running")
    try:
        ihk_submitter.submit_week(req.week, req.text, req.ausbinhalt1, req.ausbinhalt2)
        _sync_ihk_status_best_effort()
        return {"ok": True}
    except IhkError as e:
        raise HTTPException(502, str(e))
    except Exception as e:
        log.exception("IHK submit failed")
        raise HTTPException(500, f"IHK submit failed: {e}")
    finally:
        submit_lock.release()


def _sync_ihk_status_best_effort():
    try:
        ihk_submitter.sync_status()
    except Exception:
        log.exception("IHK status sync failed (non-fatal)")


def _scrape_due(now: dt.datetime, last_run_date) -> bool:
    return (
        DAYS[now.weekday()] == SCRAPE_DAY
        and (now.hour, now.minute) >= (_SCRAPE_HOUR, _SCRAPE_MINUTE)
        and last_run_date != now.date()
    )


def _weeks_to_scrape(today=None):
    """Current week plus the previous one — teachers sometimes add Lehrstoff
    for a week after it's over, so re-checking last week catches that."""
    today = today or dt.date.today()
    return [scraper.current_week_id(today - dt.timedelta(days=7)), scraper.current_week_id(today)]


def _scheduled_scrape(today=None):
    for week_id in _weeks_to_scrape(today):
        try:
            scraper.scrape_week(week_id)
        except Exception:
            log.exception("scheduled scrape of %s failed", week_id)
    _sync_ihk_status_best_effort()


def scheduler():
    """Scrape current + previous week every SCRAPE_DAY at SCRAPE_TIME (container TZ)."""
    if SCRAPE_DAY not in DAYS:
        log.info("scheduler off (SCRAPE_DAY=%s)", SCRAPE_DAY)
        return
    last_run_date = None
    log.info("scheduler: every %s at %s", SCRAPE_DAY, SCRAPE_TIME)
    while True:
        now = dt.datetime.now()
        if _scrape_due(now, last_run_date) and scrape_lock.acquire(blocking=False):
            try:
                last_run_date = now.date()
                _scheduled_scrape()
            finally:
                scrape_lock.release()
        time.sleep(60)


threading.Thread(target=scheduler, daemon=True).start()

app.mount("/", StaticFiles(directory=Path(__file__).parent.parent / "static", html=True), name="static")
