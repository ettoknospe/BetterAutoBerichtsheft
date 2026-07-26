import os
import sys
from pathlib import Path

# main.py does `import scraper` (bare) — mirrors how uvicorn runs it in
# production via `--app-dir app`, so tests need app/ on sys.path the same way.
APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

# Must be set before `import main` (module-level thread start reads it).
os.environ.setdefault("SCRAPE_DAY", "off")
