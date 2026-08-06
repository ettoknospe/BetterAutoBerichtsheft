"""Data storage abstraction layer: isolates backend (file/DB) from business logic."""

import datetime as dt
import json
import logging
from pathlib import Path

from . import config

log = logging.getLogger("storage")


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
    except (json.JSONDecodeError, OSError) as e:
        log.exception("failed to load %s", path)
        return None


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


# ===== Utilities (debug, freshness checks) =====

def _dump_debug(name: str, payload, data_dir=None):
    if data_dir is None:
        data_dir = config.DATA_DIR
    debug_dir = data_dir / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    path = debug_dir / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{name}.json"
    try:
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        log.warning("raw response dumped to %s", path)
    except Exception:
        log.exception("could not write debug dump")


def _has_real_lessons(path: Path) -> bool:
    """True only if the saved file has actual lesson data — a previously saved
    placeholder guess (holiday/schoolYearBoundary, empty days) is safe to
    overwrite with a better-informed result later."""
    if not path.exists():
        return False
    try:
        return bool(json.loads(path.read_text()).get("days"))
    except (json.JSONDecodeError, OSError):
        return False
