"""Runtime config, read from the environment.

A standalone leaf module - no imports from scraper/untis_client/storage, so
none of them need to import each other back just to see a patched value.
Tests do `monkeypatch.setattr(config, "UNTIS_USER", ...)`; consumers read
`config.X` at call time (not `from config import X`) so those patches reach
the code that actually uses it.
"""

import os
from pathlib import Path

UNTIS_HOST = os.environ.get("UNTIS_HOST", "le-bk-muenster.webuntis.com")
UNTIS_SCHOOL = os.environ.get("UNTIS_SCHOOL", "le-bk-muenster")
UNTIS_USER = os.environ.get("UNTIS_USER", "")
UNTIS_PASS = os.environ.get("UNTIS_PASS", "")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
IHK_HOST = os.environ.get("IHK_HOST", "www.bildung-ihk-nordwestfalen.de")
IHK_USER = os.environ.get("IHK_USER", "")
IHK_PASS = os.environ.get("IHK_PASS", "")
IHK_AUSBABSCHNITT = os.environ.get("IHK_AUSBABSCHNITT", "")
IHK_AUSB_MAIL = os.environ.get("IHK_AUSB_MAIL", "")
SCRAPE_DAY = os.environ.get("SCRAPE_DAY", "sun").lower()
SCRAPE_TIME = os.environ.get("SCRAPE_TIME", "18:00")

# Multi-user auth additions (new)
SECRET_ENCRYPTION_KEY = os.environ.get("SECRET_ENCRYPTION_KEY", "")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# Raw WebUntis/IHK response dumps bypass per-user storage entirely (plaintext,
# unencrypted) - dev-only, must stay off in production.
DEBUG_DUMPS = os.environ.get("DEBUG_DUMPS", "").lower() in ("1", "true", "yes")

# Set the `Secure` flag on the session cookie. Default off because the app is
# also reachable directly over plain HTTP on the LAN (0.0.0.0:8001); set to
# true when the app is only reached through the TLS-terminating reverse proxy.
SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "").lower() in ("1", "true", "yes")

# Expose the interactive API docs (/docs, /redoc, /openapi.json). Off by
# default so the schema isn't handed to unauthenticated visitors in production.
ENABLE_DOCS = os.environ.get("ENABLE_DOCS", "").lower() in ("1", "true", "yes")
