"""Local file I/O: debug dumps and the saved-week freshness check.

_dump_debug reads DATA_DIR off the `scraper` module at call time (not at
import time) so tests that do `monkeypatch.setattr(scraper, "DATA_DIR", ...)`
keep working unchanged - see app/scraper.py for why this module and scraper
import each other.
"""

import datetime as dt
import json
import logging
from pathlib import Path

import scraper as _scraper

log = logging.getLogger("scraper")


def _dump_debug(name: str, payload):
    debug_dir = _scraper.DATA_DIR / "debug"
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
