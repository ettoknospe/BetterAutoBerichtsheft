import os
import tempfile
import uuid
from dataclasses import dataclass

from cryptography.fernet import Fernet
import pytest
from fastapi.testclient import TestClient

# Must happen before any `app.*` import: app/db.py computes DB_PATH from
# config.DATA_DIR once at import time, and app/config.py reads
# SECRET_ENCRYPTION_KEY/ADMIN_* from the environment at import time too.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="berichtsheft-test-")
os.environ["SECRET_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "test-admin-password"
os.environ.setdefault("SCRAPE_DAY", "off")

from app import db  # noqa: E402
from app import main  # noqa: E402

ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]
TEST_USER_PASSWORD = "test-user-password"


@dataclass
class TestUser:
    client: TestClient
    user_id: int
    username: str


@pytest.fixture(scope="session")
def app_client():
    # Only the context-manager form runs FastAPI's startup event, which is
    # where db.run_migrations()/db.bootstrap_admin_if_needed() live.
    with TestClient(main.app) as client:
        yield client


@pytest.fixture(scope="session")
def admin_client(app_client):
    # A separate TestClient (own cookie jar) so its session cookie never
    # collides with per-test user sessions. Startup already ran via
    # app_client, so instantiating without `with` is safe here.
    client = TestClient(main.app)
    r = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD})
    assert r.status_code == 200, r.text
    return client


@pytest.fixture
def new_user(admin_client):
    """Create + log in a fresh non-admin test user via the real HTTP API."""
    username = f"testuser-{uuid.uuid4().hex[:10]}"
    r = admin_client.post("/api/admin/users", json={"username": username, "password": TEST_USER_PASSWORD})
    assert r.status_code == 200, r.text
    user_id = r.json()["id"]

    client = TestClient(main.app)
    r = client.post("/api/auth/login", json={"username": username, "password": TEST_USER_PASSWORD})
    assert r.status_code == 200, r.text

    return TestUser(client=client, user_id=user_id, username=username)


@pytest.fixture
def unauth_client():
    """A client with no session cookie, for testing 401 behavior."""
    return TestClient(main.app)


@pytest.fixture
def user_settings(new_user):
    """A UserSettings for tests that call scraper/ihk_submitter/storage
    functions directly rather than through HTTP. Seeds dummy creds via the
    real PUT /api/me/settings so the settings row exists, then builds the
    UserSettings the same way main.py does per-request."""
    r = new_user.client.put("/api/me/settings", json={
        "untis_host": "le-bk-muenster.webuntis.com",
        "untis_school": "le-bk-muenster",
        "untis_user": "u",
        "untis_pass": "p",
        "ihk_host": "www.bildung-ihk-nordwestfalen.de",
        "ihk_user": "u",
        "ihk_pass": "p",
    })
    assert r.status_code == 200, r.text

    user_row = db.get_user_by_id(new_user.user_id)
    settings_row = db.get_user_settings(new_user.user_id)
    return db._row_to_settings(settings_row, user_row)
