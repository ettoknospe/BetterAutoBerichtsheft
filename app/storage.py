"""Data storage abstraction layer: per-user data lives in SQLite, encrypted
under each user's own data-encryption key (DEK, itself wrapped by the master
SECRET_ENCRYPTION_KEY - see db.get_user_dek / db._create_user_dek).

This is blast-radius reduction, not admin-exclusion: the scheduler runs
unattended and needs plaintext WebUntis/IHK credentials to scrape, and those
credentials are decrypted with the same master key an operator holds. See
crypto.py for the encryption primitives and db.py for the encrypted-blob
table CRUD - this module owns the "which key, what JSON shape" decisions and
never touches a filesystem Path for real user data.

Decryption failures raise rather than returning None/{} - a wrong or
rotated key must surface loudly, not silently look like "no data yet" and
risk an unattended scrape overwriting real history.
"""

import datetime as dt
import json
import logging
from pathlib import Path

from . import config, crypto, db

log = logging.getLogger("storage")


def _encrypt_json(user_id: int, data) -> bytes:
    dek = db.get_user_dek(user_id)
    return crypto.encrypt_with_key(dek, json.dumps(data, ensure_ascii=False))


def _decrypt_json(user_id: int, payload_enc: bytes):
    dek = db.get_user_dek(user_id)
    return json.loads(crypto.decrypt_with_key(dek, payload_enc))


# ===== Week data =====

def save_week_data(user_id: int, week_id: str, data: dict) -> None:
    """Save scraped week data, encrypted under the user's DEK."""
    db.save_week_data_row(user_id, week_id, _encrypt_json(user_id, data))


def load_week_data(user_id: int, week_id: str) -> dict | None:
    """Load scraped week data, or None if not found."""
    payload_enc = db.get_week_data_row(user_id, week_id)
    if payload_enc is None:
        return None
    return _decrypt_json(user_id, payload_enc)


def list_week_ids(user_id: int) -> list[str]:
    """List all saved week IDs for a user, sorted."""
    return db.list_week_ids_for_user(user_id)


# ===== IHK history / status / local fields =====

def save_ihk_history(user_id: int, history: dict) -> None:
    """Save IHK history archive."""
    db.save_ihk_history_row(user_id, _encrypt_json(user_id, history))


def load_ihk_history(user_id: int) -> dict:
    """Load IHK history, or empty dict if not found."""
    payload_enc = db.get_ihk_history_row(user_id)
    if payload_enc is None:
        return {}
    return _decrypt_json(user_id, payload_enc)


def save_ihk_status(user_id: int, status: dict) -> None:
    """Save last-synced IHK status map."""
    db.save_ihk_status_row(user_id, _encrypt_json(user_id, status))


def load_ihk_status(user_id: int) -> dict:
    """Load the last-synced status map, or {} if never synced."""
    payload_enc = db.get_ihk_status_row(user_id)
    if payload_enc is None:
        return {}
    return _decrypt_json(user_id, payload_enc)


def save_local_fields(user_id: int, fields: dict) -> None:
    """Save the locally-remembered ausbinhalt1/2 map."""
    db.save_local_fields_row(user_id, _encrypt_json(user_id, fields))


def load_local_fields(user_id: int) -> dict:
    """Read the locally-remembered ausbinhalt1/2 map, or {} if none saved yet."""
    payload_enc = db.get_local_fields_row(user_id)
    if payload_enc is None:
        return {}
    return _decrypt_json(user_id, payload_enc)


# ===== Utilities (debug, freshness checks) =====

def _dump_debug(name: str, payload, user_id: int | None = None):
    """Dev-only raw-response dump. Gated off unless config.DEBUG_DUMPS is set -
    these bypass all per-user encryption (plain filesystem JSON), so they
    must never run in production against real user data."""
    if not config.DEBUG_DUMPS:
        return
    data_dir = (config.DATA_DIR / str(user_id)) if user_id is not None else config.DATA_DIR
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
