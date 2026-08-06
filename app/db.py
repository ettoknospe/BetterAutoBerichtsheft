"""SQLite database layer with migrations and per-user CRUD helpers.

Connection pragmas, schema definition, migration runner, and bootstrap logic
for first-boot multi-user migration (single-tenant .env → multi-user DB).
"""

import sqlite3
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone

from . import config, crypto
from .settings import UserSettings

log = logging.getLogger("app")

DB_PATH = config.DATA_DIR / "app.db"

# Schema migrations: (version, SQL statement)
MIGRATIONS = [
    (1, """
    CREATE TABLE IF NOT EXISTS users (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        username      TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        is_admin      INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT NOT NULL
    )
    """),
    (2, """
    CREATE TABLE IF NOT EXISTS user_settings (
        user_id            INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        untis_host         TEXT NOT NULL DEFAULT '',
        untis_school       TEXT NOT NULL DEFAULT '',
        untis_user         TEXT NOT NULL DEFAULT '',
        untis_pass_enc     BLOB,
        scrape_day         TEXT NOT NULL DEFAULT 'off',
        scrape_time        TEXT NOT NULL DEFAULT '18:00',
        ihk_host           TEXT NOT NULL DEFAULT '',
        ihk_user           TEXT NOT NULL DEFAULT '',
        ihk_pass_enc       BLOB,
        ihk_ausbabschnitt  TEXT NOT NULL DEFAULT '',
        ihk_ausb_mail      TEXT NOT NULL DEFAULT '',
        updated_at         TEXT NOT NULL
    )
    """),
    (3, """
    CREATE TABLE IF NOT EXISTS sessions (
        session_id  TEXT PRIMARY KEY,
        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at  TEXT NOT NULL,
        expires_at  TEXT NOT NULL
    )
    """),
    (4, """
    CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id)
    """),
    (5, """
    CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at)
    """),
    (6, """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version    INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL
    )
    """),
    (7, """
    ALTER TABLE user_settings ADD COLUMN ihk_use_settings_for_abschnitt INTEGER NOT NULL DEFAULT 1
    """),
    (8, """
    CREATE TABLE IF NOT EXISTS week_data (
        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        week_id     TEXT NOT NULL,
        payload_enc BLOB NOT NULL,
        updated_at  TEXT NOT NULL,
        PRIMARY KEY (user_id, week_id)
    )
    """),
    (9, """
    CREATE TABLE IF NOT EXISTS ihk_history (
        user_id     INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        payload_enc BLOB NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """),
    (10, """
    CREATE TABLE IF NOT EXISTS ihk_status (
        user_id     INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        payload_enc BLOB NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """),
    (11, """
    CREATE TABLE IF NOT EXISTS local_fields (
        user_id     INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        payload_enc BLOB NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """),
    (12, """
    CREATE TABLE IF NOT EXISTS user_keys (
        user_id     INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        dek_wrapped BLOB NOT NULL,
        created_at  TEXT NOT NULL
    )
    """),
    (13, """
    ALTER TABLE user_settings ADD COLUMN start_date TEXT NOT NULL DEFAULT ''
    """),
]


def get_connection():
    """Get a DB connection with proper pragmas set."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def run_migrations():
    """Apply pending schema migrations."""
    conn = get_connection()
    try:
        # Ensure schema_migrations table exists (it's the 6th migration)
        try:
            conn.execute(MIGRATIONS[5][1])  # Create schema_migrations table
            conn.commit()
        except Exception:
            pass  # Table already exists

        # Get current schema version
        cursor = conn.execute("SELECT MAX(version) FROM schema_migrations")
        result = cursor.fetchone()
        current_version = result[0] if result[0] is not None else 0

        # Apply pending migrations (all except schema_migrations table)
        for version, sql in MIGRATIONS:
            if version == 6:  # Skip schema_migrations table, already created
                continue
            if version > current_version:
                try:
                    conn.execute(sql)
                    conn.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                               (version, datetime.now(timezone.utc).isoformat(timespec="seconds")))
                    conn.commit()
                    log.info("Applied migration %d", version)
                except sqlite3.OperationalError as e:
                    if "duplicate column" in str(e) or "already exists" in str(e):
                        # Column or index already exists, mark as applied
                        try:
                            conn.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                                       (version, datetime.now(timezone.utc).isoformat(timespec="seconds")))
                            conn.commit()
                            log.info("Migration %d already applied", version)
                        except:
                            conn.rollback()
                    else:
                        conn.rollback()
                        log.exception("Migration %d failed", version)
                except Exception as e:
                    conn.rollback()
                    log.exception("Migration %d failed", version)
                    log.error("Migration %d failed: %s", version, e)
                    raise
    finally:
        conn.close()


def _create_user_dek(conn, user_id: int) -> None:
    """Generate a random per-user data-encryption key, wrap it with the
    master SECRET_ENCRYPTION_KEY, and store it. Called inside the same
    transaction as user creation - a user must never exist without a DEK,
    or their week/IHK data has nothing to encrypt under."""
    dek = crypto.generate_dek()
    wrapped = crypto.encrypt(dek.decode())
    conn.execute(
        "INSERT INTO user_keys (user_id, dek_wrapped, created_at) VALUES (?, ?, ?)",
        (user_id, wrapped, datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )


def get_user_dek(user_id: int) -> bytes:
    """Fetch and unwrap a user's data-encryption key. Raises if missing -
    silently falling back to no encryption would be worse than a loud error."""
    conn = get_connection()
    try:
        row = conn.execute("SELECT dek_wrapped FROM user_keys WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            raise RuntimeError(f"no data-encryption key for user_id={user_id}")
        return crypto.decrypt(row["dek_wrapped"]).encode()
    finally:
        conn.close()


def bootstrap_admin_if_needed():
    """On first boot, create admin account if ADMIN_PASSWORD is set.

    Validates encryption key early (fail fast). Legacy data migration
    can be done separately later via a migration tool.
    """
    conn = get_connection()
    try:
        cursor = conn.execute("SELECT COUNT(*) FROM users")
        if cursor.fetchone()[0] > 0:
            # Already initialized
            return

        # Check if we should bootstrap an admin
        if not config.ADMIN_PASSWORD:
            log.info("No ADMIN_PASSWORD set — run 'python -m app.create_admin' to create first admin")
            return

        # Validate encryption key early (fail fast before any DB changes)
        if config.SECRET_ENCRYPTION_KEY:
            try:
                crypto.encrypt("test")
            except RuntimeError as e:
                log.error("Encryption key validation failed: %s", e)
                raise RuntimeError(f"Cannot bootstrap — encryption key is invalid: {e}")
        else:
            log.error("SECRET_ENCRYPTION_KEY not set")
            raise RuntimeError("SECRET_ENCRYPTION_KEY must be set in .env for bootstrap")

        from .auth import hash_password

        admin_username = config.ADMIN_USERNAME or "admin"
        password_hash = hash_password(config.ADMIN_PASSWORD)
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

        # Insert admin user and empty settings
        cursor = conn.execute(
            "INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (?, ?, 1, ?)",
            (admin_username, password_hash, now_iso)
        )
        admin_id = cursor.lastrowid

        cursor = conn.execute("""
            INSERT INTO user_settings (user_id, updated_at) VALUES (?, ?)
        """, (admin_id, now_iso))

        _create_user_dek(conn, admin_id)

        conn.commit()
        log.info("Bootstrapped admin account '%s' (id=%d)", admin_username, admin_id)

    except Exception as e:
        conn.rollback()
        log.error("Bootstrap failed: %s", e)
        raise
    finally:
        conn.close()


def create_user(username: str, password_hash: str, is_admin: bool = False) -> int:
    """Create a new user. Returns user ID."""
    from .auth import hash_password
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?)",
            (username, password_hash, 1 if is_admin else 0,
             datetime.now(timezone.utc).isoformat(timespec="seconds"))
        )
        user_id = cursor.lastrowid

        # Create empty user_settings
        cursor = conn.execute("""
            INSERT INTO user_settings (user_id, updated_at) VALUES (?, ?)
        """, (user_id, datetime.now(timezone.utc).isoformat(timespec="seconds")))

        _create_user_dek(conn, user_id)

        conn.commit()
        return user_id
    finally:
        conn.close()


def get_user_by_username(username: str):
    """Fetch user by username, or None."""
    conn = get_connection()
    try:
        cursor = conn.execute("SELECT * FROM users WHERE username = ?", (username,))
        return cursor.fetchone()
    finally:
        conn.close()


def get_user_by_id(user_id: int):
    """Fetch user by ID, or None."""
    conn = get_connection()
    try:
        cursor = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        return cursor.fetchone()
    finally:
        conn.close()


def get_user_settings(user_id: int):
    """Fetch user_settings row (or None if not set up)."""
    conn = get_connection()
    try:
        cursor = conn.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,))
        return cursor.fetchone()
    finally:
        conn.close()


def update_user_settings(user_id: int, **kwargs) -> UserSettings:
    """Update user_settings fields and return rebuilt UserSettings object.

    Supported kwargs: untis_host, untis_school, untis_user, untis_pass,
    scrape_day, scrape_time, ihk_host, ihk_user,
    ihk_pass, ihk_ausbabschnitt, ihk_ausb_mail.

    Passwords are encrypted before storage; None/empty = leave unchanged.
    """
    conn = get_connection()
    try:
        # Load current settings
        row = conn.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            raise ValueError(f"User {user_id} has no settings row")

        updates = {}

        # Handle encrypted fields specially
        if "untis_pass" in kwargs:
            val = kwargs.pop("untis_pass")
            if val:
                updates["untis_pass_enc"] = crypto.encrypt(val)

        if "ihk_pass" in kwargs:
            val = kwargs.pop("ihk_pass")
            if val:
                updates["ihk_pass_enc"] = crypto.encrypt(val)

        # Everything else passes through
        updates.update(kwargs)
        updates["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

        # Build SET clause
        set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
        values = list(updates.values()) + [user_id]

        conn.execute(f"UPDATE user_settings SET {set_clause} WHERE user_id = ?", values)
        conn.commit()

        # Rebuild UserSettings from updated row
        updated_row = conn.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
        user_row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _row_to_settings(updated_row, user_row)

    finally:
        conn.close()


def _row_to_settings(settings_row, user_row) -> UserSettings:
    """Convert DB row to UserSettings object, decrypting passwords."""
    if not settings_row:
        raise ValueError("No settings row")

    row_dict = dict(settings_row)

    untis_pass = ""
    if row_dict["untis_pass_enc"]:
        try:
            untis_pass = crypto.decrypt(row_dict["untis_pass_enc"])
        except RuntimeError:
            log.warning("Could not decrypt UNTIS_PASS for user %s", user_row["id"])

    ihk_pass = ""
    if row_dict["ihk_pass_enc"]:
        try:
            ihk_pass = crypto.decrypt(row_dict["ihk_pass_enc"])
        except RuntimeError:
            log.warning("Could not decrypt IHK_PASS for user %s", user_row["id"])

    return UserSettings(
        UNTIS_HOST=row_dict["untis_host"] or "",
        UNTIS_SCHOOL=row_dict["untis_school"] or "",
        UNTIS_USER=row_dict["untis_user"] or "",
        UNTIS_PASS=untis_pass,
        DATA_DIR=config.DATA_DIR / str(user_row["id"]),
        IHK_HOST=row_dict["ihk_host"] or "",
        IHK_USER=row_dict["ihk_user"] or "",
        IHK_PASS=ihk_pass,
        IHK_AUSBABSCHNITT=row_dict["ihk_ausbabschnitt"] or "",
        IHK_AUSB_MAIL=row_dict["ihk_ausb_mail"] or "",
        IHK_USE_SETTINGS_FOR_ABSCHNITT=bool(row_dict.get("ihk_use_settings_for_abschnitt", 1)),
        SCRAPE_DAY=row_dict["scrape_day"],
        SCRAPE_TIME=row_dict["scrape_time"],
        user_id=user_row["id"],
    )


def create_session(user_id: int, expires_days: int = 30) -> str:
    """Create a new session and return the session_id."""
    import secrets
    session_id = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(days=expires_days)).isoformat(timespec="seconds")

    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO sessions (session_id, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (session_id, user_id, now.isoformat(timespec="seconds"), expires_at)
        )
        conn.commit()
        return session_id
    finally:
        conn.close()


def get_session(session_id: str):
    """Fetch a session if it exists and is not expired."""
    conn = get_connection()
    try:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cursor = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ? AND expires_at > ?",
            (session_id, now)
        )
        return cursor.fetchone()
    finally:
        conn.close()


def delete_session(session_id: str):
    """Delete a session."""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        conn.commit()
    finally:
        conn.close()


# ===== Encrypted per-user blob storage (week data, IHK history/status/fields) =====
# All payloads are opaque ciphertext to this layer - encryption/decryption with
# the user's DEK happens in storage.py, not here.

def save_week_data_row(user_id: int, week_id: str, payload_enc: bytes) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO week_data (user_id, week_id, payload_enc, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (user_id, week_id) DO UPDATE SET payload_enc = excluded.payload_enc, updated_at = excluded.updated_at",
            (user_id, week_id, payload_enc, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        conn.commit()
    finally:
        conn.close()


def get_week_data_row(user_id: int, week_id: str) -> bytes | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT payload_enc FROM week_data WHERE user_id = ? AND week_id = ?", (user_id, week_id)
        ).fetchone()
        return row["payload_enc"] if row else None
    finally:
        conn.close()


def list_week_ids_for_user(user_id: int) -> list[str]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT week_id FROM week_data WHERE user_id = ? ORDER BY week_id", (user_id,)
        ).fetchall()
        return [r["week_id"] for r in rows]
    finally:
        conn.close()


def _save_single_blob(table: str, user_id: int, payload_enc: bytes) -> None:
    conn = get_connection()
    try:
        conn.execute(
            f"INSERT INTO {table} (user_id, payload_enc, updated_at) VALUES (?, ?, ?) "
            f"ON CONFLICT (user_id) DO UPDATE SET payload_enc = excluded.payload_enc, updated_at = excluded.updated_at",
            (user_id, payload_enc, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        conn.commit()
    finally:
        conn.close()


def _get_single_blob(table: str, user_id: int) -> bytes | None:
    conn = get_connection()
    try:
        row = conn.execute(f"SELECT payload_enc FROM {table} WHERE user_id = ?", (user_id,)).fetchone()
        return row["payload_enc"] if row else None
    finally:
        conn.close()


def save_ihk_history_row(user_id: int, payload_enc: bytes) -> None:
    _save_single_blob("ihk_history", user_id, payload_enc)


def get_ihk_history_row(user_id: int) -> bytes | None:
    return _get_single_blob("ihk_history", user_id)


def save_ihk_status_row(user_id: int, payload_enc: bytes) -> None:
    _save_single_blob("ihk_status", user_id, payload_enc)


def get_ihk_status_row(user_id: int) -> bytes | None:
    return _get_single_blob("ihk_status", user_id)


def save_local_fields_row(user_id: int, payload_enc: bytes) -> None:
    _save_single_blob("local_fields", user_id, payload_enc)


def get_local_fields_row(user_id: int) -> bytes | None:
    return _get_single_blob("local_fields", user_id)
