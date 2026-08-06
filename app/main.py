import datetime as dt
import logging
import re
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Depends, Request, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, auth, scraper, ihk_submitter, untis_client, ihk_client, storage
from .settings import UserSettings
from .ihk_client import IhkError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("app")

# Multi-user locks (per-user scrape/submit concurrency control)
_locks = {}
_locks_meta_lock = threading.Lock()

def _get_lock(user_id: int, kind: str) -> threading.Lock:
    """Get or create a lock for a specific user and operation kind."""
    key = (user_id, kind)
    with _locks_meta_lock:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]

# Bulk-scrape progress, polled by the Datenimport page while a scrape runs.
# Absent/missing key means "nothing running" - the frontend treats a 404-ish
# empty response the same way, so no separate "not running" sentinel needed.
_bulk_scrape_progress = {}


def _iter_weeks(start: str, end: str):
    """Yield YYYY-Www week ids from start to end inclusive, same increment
    rule as bulkops_scrape_weeks() - kept in sync so the upfront total here
    matches what the loop actually iterates."""
    wk = start
    while wk <= end:
        yield wk
        y, w = wk.split("-W")
        w = int(w) + 1
        if w > 53:
            w = 1
            y = int(y) + 1
        wk = f"{y}-W{w:02d}"

# Constants
WEEK_RE = re.compile(r"^\d{4}-W\d{2}$")
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# Initialize app
app = FastAPI(title="Berichtsheft")

# ===== Request Models =====

class ScrapeRequest(BaseModel):
    week: str | None = None

class SubmitIhkRequest(BaseModel):
    week: str
    text: str
    ausbinhalt1: str | None = None
    ausbinhalt2: str | None = None
    ihk_abschnitt_override: str | None = None
    ihk_ausb_mail_override: str | None = None

class BulkScrapeRequest(BaseModel):
    startWeek: str
    endWeek: str

class BulkBackfillIhkRequest(BaseModel):
    pass

class LoginRequest(BaseModel):
    username: str
    password: str

class CreateUserRequest(BaseModel):
    username: str
    password: str
    is_admin: bool = False

class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str

class SettingsUpdateRequest(BaseModel):
    untis_host: str | None = None
    untis_school: str | None = None
    untis_user: str | None = None
    untis_pass: str | None = None
    scrape_day: str | None = None
    scrape_time: str | None = None
    ihk_host: str | None = None
    ihk_user: str | None = None
    ihk_pass: str | None = None
    ihk_ausbabschnitt: str | None = None
    ihk_ausb_mail: str | None = None
    ihk_use_settings_for_abschnitt: bool | None = None
    start_date: str | None = None

# ===== Auth Endpoints =====

@app.post("/api/auth/login")
async def login(req: LoginRequest, response: Response):
    """Authenticate user, set session cookie."""
    user = db.get_user_by_username(req.username)
    if not user or not auth.verify_password(req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    session_id = db.create_session(user["id"])
    response.set_cookie("session", session_id, httponly=True, samesite="lax", max_age=30*24*3600)
    return {"ok": True, "username": user["username"], "is_admin": bool(user["is_admin"])}

@app.post("/api/auth/logout")
async def logout(request: Request, user: auth.AuthedUser = Depends(auth.require_user), response: Response = None):
    """Clear session, both client-side (cookie) and server-side (DB row)."""
    session_id = request.cookies.get("session")
    if session_id:
        db.delete_session(session_id)
    response.delete_cookie("session")
    return {"ok": True}

@app.get("/api/auth/whoami")
async def whoami(user: auth.AuthedUser = Depends(auth.require_user)):
    """Get current user info."""
    return {"username": user.username, "is_admin": user.is_admin}

# ===== Admin Endpoints =====

@app.post("/api/admin/users")
async def create_admin_user(req: CreateUserRequest, admin: auth.AuthedUser = Depends(auth.require_admin)):
    """Create new user (admin only)."""
    if db.get_user_by_username(req.username):
        raise HTTPException(status_code=409, detail="Username already taken")

    try:
        user_id = db.create_user(req.username, auth.hash_password(req.password), req.is_admin)
        return {"ok": True, "id": user_id, "username": req.username}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/admin/users")
async def list_admin_users(admin: auth.AuthedUser = Depends(auth.require_admin)):
    """List all users (admin only)."""
    conn = db.get_connection()
    try:
        cursor = conn.execute("SELECT id, username, is_admin, created_at FROM users ORDER BY created_at DESC")
        users = [dict(row) for row in cursor.fetchall()]
        return users
    finally:
        conn.close()

# ===== Settings Endpoints =====

@app.get("/api/me/settings")
async def get_settings(user: auth.AuthedUser = Depends(auth.require_user)):
    """Get user's settings."""
    row = db.get_user_settings(user.id)
    if not row:
        raise HTTPException(status_code=404, detail="Settings not found")

    row_dict = dict(row)
    return {
        "untis_host": row_dict["untis_host"],
        "untis_school": row_dict["untis_school"],
        "untis_user": row_dict["untis_user"],
        "untis_pass_set": bool(row_dict["untis_pass_enc"]),
        "scrape_day": row_dict["scrape_day"],
        "scrape_time": row_dict["scrape_time"],
        "ihk_host": row_dict["ihk_host"],
        "ihk_user": row_dict["ihk_user"],
        "ihk_pass_set": bool(row_dict["ihk_pass_enc"]),
        "ihk_ausbabschnitt": row_dict["ihk_ausbabschnitt"],
        "ihk_ausb_mail": row_dict["ihk_ausb_mail"],
        "ihk_use_settings_for_abschnitt": bool(row_dict.get("ihk_use_settings_for_abschnitt", 1)),
        "start_date": row_dict.get("start_date") or "",
    }

@app.put("/api/me/settings")
async def update_settings(req: SettingsUpdateRequest, user: auth.AuthedUser = Depends(auth.require_user)):
    """Update user's settings."""
    if req.start_date:
        try:
            dt.date.fromisoformat(req.start_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="start_date must be YYYY-MM-DD")

    updates = {}
    for field in ["untis_host", "untis_school", "untis_user", "untis_pass",
                  "scrape_day", "scrape_time",
                  "ihk_host", "ihk_user", "ihk_pass", "ihk_ausbabschnitt", "ihk_ausb_mail",
                  "ihk_use_settings_for_abschnitt", "start_date"]:
        val = getattr(req, field, None)
        if val is not None:
            updates[field] = val

    try:
        db.update_user_settings(user.id, **updates)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/me/password")
async def change_password(req: PasswordChangeRequest, user: auth.AuthedUser = Depends(auth.require_user)):
    """Change user's password."""
    user_row = db.get_user_by_id(user.id)
    if not auth.verify_password(req.current_password, user_row["password_hash"]):
        raise HTTPException(status_code=401, detail="Current password incorrect")

    new_hash = auth.hash_password(req.new_password)
    conn = db.get_connection()
    try:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, user.id))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()

@app.post("/api/test/untis")
async def test_untis_connection(req: SettingsUpdateRequest, user: auth.AuthedUser = Depends(auth.require_user)):
    """Test WebUntis connection with provided credentials."""
    # Build settings from request (use provided values, fallback to user's saved settings)
    user_row = db.get_user_by_id(user.id)
    settings_row = db.get_user_settings(user.id)
    current_settings = db._row_to_settings(settings_row, user_row)

    # Merge provided values with current settings
    test_settings = UserSettings(
        UNTIS_HOST=req.untis_host or current_settings.UNTIS_HOST,
        UNTIS_SCHOOL=req.untis_school or current_settings.UNTIS_SCHOOL,
        UNTIS_USER=req.untis_user or current_settings.UNTIS_USER,
        UNTIS_PASS=req.untis_pass or current_settings.UNTIS_PASS,
        DATA_DIR=current_settings.DATA_DIR,
        IHK_HOST=current_settings.IHK_HOST,
        IHK_USER=current_settings.IHK_USER,
        IHK_PASS=current_settings.IHK_PASS,
        IHK_AUSBABSCHNITT=current_settings.IHK_AUSBABSCHNITT,
        IHK_AUSB_MAIL=current_settings.IHK_AUSB_MAIL,
        SCRAPE_DAY=current_settings.SCRAPE_DAY,
        SCRAPE_TIME=current_settings.SCRAPE_TIME,
        user_id=current_settings.user_id,
    )

    if not test_settings.UNTIS_USER or not test_settings.UNTIS_PASS:
        raise HTTPException(status_code=400, detail="UNTIS_USER and UNTIS_PASS required")

    try:
        client = untis_client.UntisClient(test_settings)
        client.login()
        return {"ok": True, "message": "WebUntis connection successful"}
    except untis_client.ScrapeError as e:
        raise HTTPException(status_code=502, detail=f"WebUntis error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Test failed: {e}")

@app.post("/api/test/ihk")
async def test_ihk_connection(req: SettingsUpdateRequest, user: auth.AuthedUser = Depends(auth.require_user)):
    """Test IHK connection with provided credentials."""
    # Build settings from request (use provided values, fallback to user's saved settings)
    user_row = db.get_user_by_id(user.id)
    settings_row = db.get_user_settings(user.id)
    current_settings = db._row_to_settings(settings_row, user_row)

    # Merge provided values with current settings
    test_settings = UserSettings(
        UNTIS_HOST=current_settings.UNTIS_HOST,
        UNTIS_SCHOOL=current_settings.UNTIS_SCHOOL,
        UNTIS_USER=current_settings.UNTIS_USER,
        UNTIS_PASS=current_settings.UNTIS_PASS,
        DATA_DIR=current_settings.DATA_DIR,
        IHK_HOST=req.ihk_host or current_settings.IHK_HOST,
        IHK_USER=req.ihk_user or current_settings.IHK_USER,
        IHK_PASS=req.ihk_pass or current_settings.IHK_PASS,
        IHK_AUSBABSCHNITT=req.ihk_ausbabschnitt or current_settings.IHK_AUSBABSCHNITT,
        IHK_AUSB_MAIL=req.ihk_ausb_mail or current_settings.IHK_AUSB_MAIL,
        SCRAPE_DAY=current_settings.SCRAPE_DAY,
        SCRAPE_TIME=current_settings.SCRAPE_TIME,
        user_id=current_settings.user_id,
    )

    if not test_settings.IHK_USER or not test_settings.IHK_PASS:
        raise HTTPException(status_code=400, detail="IHK_USER and IHK_PASS required")

    try:
        client = ihk_client.IhkClient(test_settings)
        client.login()
        return {"ok": True, "message": "IHK connection successful"}
    except ihk_client.IhkError as e:
        raise HTTPException(status_code=502, detail=f"IHK error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Test failed: {e}")

# ===== Scraping Endpoints (per-user) =====

@app.get("/api/weeks")
def list_weeks(user: auth.AuthedUser = Depends(auth.require_user)):
    """List weeks available for current user."""
    # MANDATORY: settings= passed explicitly to scope DATA_DIR
    user_row = db.get_user_by_id(user.id)
    settings_row = db.get_user_settings(user.id)
    settings = db._row_to_settings(settings_row, user_row)
    weeks = storage.list_week_ids(settings.user_id)
    start_date = dict(settings_row).get("start_date") or None
    start_week = scraper.current_week_id(dt.date.fromisoformat(start_date)) if start_date else None
    return {"weeks": weeks, "current": scraper.current_week_id(), "startWeek": start_week}

@app.get("/api/weeks/{week_id}")
def get_week(week_id: str, user: auth.AuthedUser = Depends(auth.require_user)):
    """Get week data for current user."""
    if not WEEK_RE.match(week_id):
        raise HTTPException(status_code=400, detail="bad week id, expected YYYY-Www")

    user_row = db.get_user_by_id(user.id)
    settings_row = db.get_user_settings(user.id)
    settings = db._row_to_settings(settings_row, user_row)
    data = storage.load_week_data(settings.user_id, week_id)
    if data is None:
        raise HTTPException(status_code=404, detail="no data for this week")
    return data

@app.post("/api/scrape")
def scrape(req: ScrapeRequest, user: auth.AuthedUser = Depends(auth.require_user)):
    """Scrape current or specified week."""
    week_id = req.week or scraper.current_week_id()
    if not WEEK_RE.match(week_id):
        raise HTTPException(status_code=400, detail="bad week id, expected YYYY-Www")

    # Prevent scraping future weeks
    current_week = scraper.current_week_id()
    if week_id > current_week:
        raise HTTPException(status_code=400, detail="cannot scrape future weeks")

    lock = _get_lock(user.id, "scrape")
    if not lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="scrape already running")

    try:
        user_row = db.get_user_by_id(user.id)
        settings_row = db.get_user_settings(user.id)
        settings = db._row_to_settings(settings_row, user_row)
        # MANDATORY: settings= passed explicitly
        result = scraper.scrape_week(week_id, settings=settings)
        # Best effort: sync IHK status if credentials are configured
        try:
            ihk_submitter.sync_status(settings=settings)
        except Exception as e:
            log.warning("IHK status sync failed (non-fatal): %s", e)
        return result
    except scraper.ScrapeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        log.exception("scrape failed")
        raise HTTPException(status_code=500, detail=f"scrape failed: {e}")
    finally:
        lock.release()

@app.get("/api/ihk-status")
def ihk_status(user: auth.AuthedUser = Depends(auth.require_user)):
    """Get current IHK status."""
    user_row = db.get_user_by_id(user.id)
    settings_row = db.get_user_settings(user.id)
    settings = db._row_to_settings(settings_row, user_row)
    # MANDATORY: settings= passed explicitly
    return {"status": ihk_submitter.load_status(settings=settings), "fields": ihk_submitter.load_local_fields(settings=settings)}

@app.get("/api/ihk-history")
def ihk_history(user: auth.AuthedUser = Depends(auth.require_user)):
    """Get archived IHK entry history."""
    user_row = db.get_user_by_id(user.id)
    settings_row = db.get_user_settings(user.id)
    settings = db._row_to_settings(settings_row, user_row)
    return ihk_submitter.load_history(settings=settings)

@app.post("/api/submit-ihk")
def submit_ihk(req: SubmitIhkRequest, user: auth.AuthedUser = Depends(auth.require_user)):
    """Submit entry to IHK."""
    if not WEEK_RE.match(req.week):
        raise HTTPException(status_code=400, detail="bad week id, expected YYYY-Www")
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="text is empty")

    lock = _get_lock(user.id, "submit")
    if not lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="submit already running")

    try:
        user_row = db.get_user_by_id(user.id)
        settings_row = db.get_user_settings(user.id)
        settings = db._row_to_settings(settings_row, user_row)

        # Use override values if provided, otherwise use settings
        ihk_abschnitt = req.ihk_abschnitt_override or settings.IHK_AUSBABSCHNITT
        ihk_ausb_mail = req.ihk_ausb_mail_override or settings.IHK_AUSB_MAIL

        # Temporarily update settings with per-submission values
        settings.IHK_AUSBABSCHNITT = ihk_abschnitt
        settings.IHK_AUSB_MAIL = ihk_ausb_mail

        # MANDATORY: settings= passed explicitly
        ihk_submitter.submit_week(req.week, req.text, req.ausbinhalt1, req.ausbinhalt2, settings=settings)
        # Best effort: remembering the submitted text locally and refreshing
        # status must not turn an otherwise-successful submit into an error.
        try:
            ihk_submitter.save_local_fields(req.week, req.ausbinhalt1, req.ausbinhalt2, settings=settings)
        except Exception as e:
            log.warning("saving local IHK fields failed (non-fatal): %s", e)
        try:
            ihk_submitter.sync_status(settings=settings)
        except Exception as e:
            log.warning("IHK status sync failed (non-fatal): %s", e)
        return {"ok": True}
    except IhkError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        log.exception("IHK submit failed")
        raise HTTPException(status_code=500, detail=f"IHK submit failed: {e}")
    finally:
        lock.release()

# ===== Bulk Operations =====

@app.post("/api/bulkops/scrape-weeks")
def bulkops_scrape_weeks(req: BulkScrapeRequest, user: auth.AuthedUser = Depends(auth.require_user)):
    """Bulk scrape WebUntis for week range [startWeek, endWeek] inclusive."""
    if not WEEK_RE.match(req.startWeek) or not WEEK_RE.match(req.endWeek):
        raise HTTPException(status_code=400, detail="week must be YYYY-Www format")
    if req.startWeek > req.endWeek:
        raise HTTPException(status_code=400, detail="startWeek must be <= endWeek")

    user_row = db.get_user_by_id(user.id)
    settings_row = db.get_user_settings(user.id)
    settings = db._row_to_settings(settings_row, user_row)

    weeks_list = list(_iter_weeks(req.startWeek, req.endWeek))
    total = len(weeks_list)
    weeks_scraped = 0
    try:
        for i, wk in enumerate(weeks_list, start=1):
            _bulk_scrape_progress[user.id] = {"current": i, "total": total, "week": wk}
            try:
                lock = _get_lock(user.id, "scrape")
                if not lock.acquire(blocking=False):
                    raise HTTPException(status_code=409, detail="scrape already running")
                try:
                    scraper.scrape_week(wk, settings=settings)
                    weeks_scraped += 1
                finally:
                    lock.release()
            except Exception as e:
                log.warning("failed to scrape %s: %s", wk, e)
        return {"weeks_scraped": weeks_scraped}
    finally:
        _bulk_scrape_progress.pop(user.id, None)

@app.get("/api/bulkops/scrape-progress")
def bulkops_scrape_progress(user: auth.AuthedUser = Depends(auth.require_user)):
    """Polled by the Datenimport page while a bulk scrape is in flight."""
    return _bulk_scrape_progress.get(user.id) or {"current": 0, "total": 0, "week": None}

@app.post("/api/bulkops/backfill-ihk")
def bulkops_backfill_ihk(req: BulkBackfillIhkRequest, user: auth.AuthedUser = Depends(auth.require_user)):
    """Backfill IHK history: fetch all existing IHK entries and archive locally."""
    user_row = db.get_user_by_id(user.id)
    settings_row = db.get_user_settings(user.id)
    settings = db._row_to_settings(settings_row, user_row)

    try:
        client = ihk_client.IhkClient(settings=settings)
        client.login()
        try:
            entries = client.list_entries()
            history = {}
            now = dt.datetime.now().isoformat(timespec="seconds")
            for week_id, meta in sorted(entries.items()):
                lfdnr = meta.get("lfdnr")
                if lfdnr is None:
                    log.warning("skip %s - no lfdnr", week_id)
                    continue
                form = client.fetch_entry(lfdnr)
                history[week_id] = {
                    "lfdnr": lfdnr,
                    "status": meta.get("status"),
                    "ausbinhalt1": form.get("ausbinhalt1", ""),
                    "ausbinhalt2": form.get("ausbinhalt2", ""),
                    "scrapedAt": now,
                }
                log.info("%s (lfdnr=%s): fetched", week_id, lfdnr)
        finally:
            client.logout()

        from . import storage
        storage.save_ihk_history(user.id, history)
        return {"entries_scraped": len(history)}
    except IhkError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        log.exception("backfill failed")
        raise HTTPException(status_code=500, detail=f"backfill failed: {e}")

# ===== Scheduler (multi-user) =====

def _scrape_due(now: dt.datetime, last_run_date, scrape_day: str, scrape_hour: int, scrape_minute: int) -> bool:
    """Check if scrape is due (parametrized, no module globals)."""
    return (
        DAYS[now.weekday()] == scrape_day
        and (now.hour, now.minute) >= (scrape_hour, scrape_minute)
        and last_run_date != now.date()
    )

def _weeks_to_scrape(today=None):
    """Current week plus the previous one."""
    today = today or dt.date.today()
    return [scraper.current_week_id(today - dt.timedelta(days=7)), scraper.current_week_id(today)]

def _scheduled_scrape(user_id: int, today=None):
    """Scrape current+previous week for a user."""
    user_row = db.get_user_by_id(user_id)
    settings_row = db.get_user_settings(user_id)
    settings = db._row_to_settings(settings_row, user_row)
    for week_id in _weeks_to_scrape(today):
        try:
            # MANDATORY: settings= passed explicitly
            scraper.scrape_week(week_id, settings=settings)
        except Exception:
            log.exception("scheduled scrape of %s failed for user %d", week_id, user_id)
    try:
        ihk_submitter.sync_status(settings=settings)
    except Exception:
        log.exception("IHK status sync failed for user %d (non-fatal)", user_id)

def scheduler():
    """Scrape for all users on their individual schedules."""
    last_run = {}  # user_id -> date
    log.info("scheduler started")
    while True:
        try:
            now = dt.datetime.now()
            conn = db.get_connection()
            try:
                cursor = conn.execute("SELECT id, username FROM users WHERE id IN (SELECT user_id FROM user_settings WHERE scrape_day != 'off')")
                users = cursor.fetchall()
            finally:
                conn.close()

            for user_row in users:
                user_id = user_row["id"]
                settings_row = db.get_user_settings(user_id)
                settings = db._row_to_settings(settings_row, user_row)

                # Parse scrape time
                try:
                    scrape_hour, scrape_minute = map(int, settings.SCRAPE_TIME.split(":"))
                except Exception:
                    log.warning("Invalid SCRAPE_TIME for user %d: %s", user_id, settings.SCRAPE_TIME)
                    continue

                if _scrape_due(now, last_run.get(user_id), settings.SCRAPE_DAY, scrape_hour, scrape_minute):
                    lock = _get_lock(user_id, "scrape")
                    if lock.acquire(blocking=False):
                        try:
                            last_run[user_id] = now.date()
                            _scheduled_scrape(user_id)
                        finally:
                            lock.release()

            time.sleep(60)
        except Exception:
            log.exception("scheduler loop error")
            time.sleep(60)

# ===== Startup =====

@app.on_event("startup")
def startup():
    """Initialize DB and start scheduler."""
    db.run_migrations()
    db.bootstrap_admin_if_needed()
    threading.Thread(target=scheduler, daemon=True).start()

# ===== Static Files =====

app.mount("/", StaticFiles(directory=Path(__file__).parent.parent / "static", html=True), name="static")
