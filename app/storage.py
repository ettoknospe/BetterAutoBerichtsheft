"""Data storage abstraction layer: isolates backend (file/DB) from business logic.

All per-user data access goes through here, keyed by user_id — callers never
touch a filesystem Path directly. This is the sole swap point for the
DB + per-user-encryption backend (see PAPERLESS.md-style migration notes).
"""

import datetime as dt
import json
import logging
import re
from pathlib import Path

from . import config

log = logging.getLogger("storage")

_WEEK_RE = re.compile(r"^\d{4}-W\d{2}$")


# ===== File-based backend (current, can swap later) =====

def _user_data_dir(user_id: int) -> Path:
    """Per-user data directory."""
    return config.DATA_DIR / str(user_id)


def save_week_data(user_id: int, week_id: str, data: dict) -> None:
    """Save scraped week data (abstraction point for backend swap)."""
    user_dir = _user_data_dir(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    path = user_dir / f"{week_id}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def load_week_data(user_id: int, week_id: str) -> dict | None:
    """Load scraped week data, or None if not found."""
    user_dir = _user_data_dir(user_id)
    path = user_dir / f"{week_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        log.exception("failed to load %s", path)
        return None


def list_week_ids(user_id: int) -> list[str]:
    """List all saved week IDs for a user, sorted."""
    user_dir = _user_data_dir(user_id)
    if not user_dir.exists():
        return []
    return sorted(p.stem for p in user_dir.glob("*.json") if _WEEK_RE.match(p.stem))


def save_ihk_history(user_id: int, history: dict) -> None:
    """Save IHK history archive."""
    user_dir = _user_data_dir(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    path = user_dir / "ihk_history.json"
    path.write_text(json.dumps(history, indent=2, ensure_ascii=False))


def load_ihk_history(user_id: int) -> dict:
    """Load IHK history, or empty dict if not found."""
    user_dir = _user_data_dir(user_id)
    path = user_dir / "ihk_history.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_ihk_status(user_id: int, status: dict) -> None:
    """Save last-synced IHK status map."""
    user_dir = _user_data_dir(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    path = user_dir / "ihk_status.json"
    path.write_text(json.dumps(status, indent=2, ensure_ascii=False))


def load_ihk_status(user_id: int) -> dict:
    """Load the last-synced status map, or {} if never synced."""
    user_dir = _user_data_dir(user_id)
    path = user_dir / "ihk_status.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_local_fields(user_id: int, fields: dict) -> None:
    """Save the locally-remembered ausbinhalt1/2 map."""
    user_dir = _user_data_dir(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    path = user_dir / "ihk_fields.json"
    path.write_text(json.dumps(fields, indent=2, ensure_ascii=False))


def load_local_fields(user_id: int) -> dict:
    """Read the locally-remembered ausbinhalt1/2 map, or {} if none saved yet."""
    user_dir = _user_data_dir(user_id)
    path = user_dir / "ihk_fields.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


# ===== Utilities (debug, freshness checks) =====

def _dump_debug(name: str, payload, user_id: int | None = None):
    """Dev-only raw-response dump. Gated off unless config.DEBUG_DUMPS is set -
    these bypass all per-user encryption, so they must never run in production
    against real user data."""
    if not config.DEBUG_DUMPS:
        return
    data_dir = _user_data_dir(user_id) if user_id is not None else config.DATA_DIR
    debug_dir = data_dir / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    path = debug_dir / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{name}.json"
    try:
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        log.warning("raw response dumped to %s", path)
    except Exception:
        log.exception("could not write debug dump")


def _has_real_lessons(user_id: int, week_id: str) -> bool:
    """True only if the saved week has actual lesson data — a previously saved
    placeholder guess (holiday/schoolYearBoundary, empty days) is safe to
    overwrite with a better-informed result later."""
    data = load_week_data(user_id, week_id)
    return bool(data and data.get("days"))
