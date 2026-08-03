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
SUBJECT_FILTER = [s.strip() for s in os.environ.get("SUBJECT_FILTER", "").split(",") if s.strip()]
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
