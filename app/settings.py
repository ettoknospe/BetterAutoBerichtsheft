"""Per-user settings, replacing the single-tenant config.py globals.

UserSettings is a 1:1 rename of config.py's module-level constants.
Production code builds these from the DB per-request and passes them
explicitly. Tests can still use UserSettings.from_config() to fall back
to the old monkeypatch.setattr(config, "X", ...) pattern.
"""

from dataclasses import dataclass
from pathlib import Path
from . import config as _config


@dataclass
class UserSettings:
    """Per-user configuration for WebUntis/IHK access and scraping behavior."""

    UNTIS_HOST: str
    UNTIS_SCHOOL: str
    UNTIS_USER: str
    UNTIS_PASS: str
    DATA_DIR: Path
    IHK_HOST: str
    IHK_USER: str
    IHK_PASS: str
    IHK_AUSBABSCHNITT: str
    IHK_AUSB_MAIL: str
    IHK_USE_SETTINGS_FOR_ABSCHNITT: bool = True
    SCRAPE_DAY: str = "off"
    SCRAPE_TIME: str = "18:00"
    user_id: int | None = None

    @classmethod
    def from_config(cls) -> "UserSettings":
        """Legacy fallback: read config.py module globals at call time.

        Used only by tests and the first-boot migration bootstrap step.
        NEVER used on a real per-user request path in main.py — those
        always pass settings= explicitly.

        Tests rely on this to keep monkeypatch.setattr(config, "X", ...)
        working unchanged — a core part of the adapter pattern.
        """
        c = _config
        return cls(
            UNTIS_HOST=c.UNTIS_HOST,
            UNTIS_SCHOOL=c.UNTIS_SCHOOL,
            UNTIS_USER=c.UNTIS_USER,
            UNTIS_PASS=c.UNTIS_PASS,
            DATA_DIR=c.DATA_DIR,
            IHK_HOST=c.IHK_HOST,
            IHK_USER=c.IHK_USER,
            IHK_PASS=c.IHK_PASS,
            IHK_AUSBABSCHNITT=c.IHK_AUSBABSCHNITT,
            IHK_AUSB_MAIL=c.IHK_AUSB_MAIL,
            IHK_USE_SETTINGS_FOR_ABSCHNITT=True,
            SCRAPE_DAY=c.SCRAPE_DAY,
            SCRAPE_TIME=c.SCRAPE_TIME,
        )
