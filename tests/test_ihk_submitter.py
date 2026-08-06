import pytest

from app import config
from app import ihk_submitter
from app import storage
from app.ihk_client import IhkClient, IhkError


@pytest.fixture
def fake_ihk(monkeypatch, user_settings):
    """Stub out network I/O in IhkClient; user_settings already carries
    real IHK_USER/PASS seeded via the real PUT /api/me/settings call."""
    monkeypatch.setattr(IhkClient, "login", lambda self: None)
    monkeypatch.setattr(IhkClient, "logout", lambda self: None)
    return user_settings


def test_untis_style_missing_credentials_raises(monkeypatch):
    monkeypatch.setattr(config, "IHK_USER", "")
    monkeypatch.setattr(config, "IHK_PASS", "")
    with pytest.raises(IhkError):
        IhkClient()


def test_submit_week_saves_into_existing_editable_entry(monkeypatch, fake_ihk):
    monkeypatch.setattr(
        IhkClient, "list_entries", lambda self: {"2026-W29": {"lfdnr": 42, "status": "in Bearbeitung bei Azubi"}}
    )
    saved = []
    monkeypatch.setattr(
        IhkClient, "save_entry", lambda self, lfdnr, text, a1=None, a2=None: saved.append((lfdnr, text, a1, a2))
    )
    monkeypatch.setattr(IhkClient, "create_next_entry", lambda self: pytest.fail("should not create a new entry"))

    ihk_submitter.submit_week("2026-W29", "the text", settings=fake_ihk)
    assert saved == [(42, "the text", None, None)]


def test_submit_week_refuses_locked_entry(monkeypatch, fake_ihk):
    monkeypatch.setattr(
        IhkClient, "list_entries", lambda self: {"2026-W29": {"lfdnr": 42, "status": "Nachweis genehmigt"}}
    )
    monkeypatch.setattr(IhkClient, "save_entry", lambda self, lfdnr, text: pytest.fail("should not save"))

    with pytest.raises(IhkError):
        ihk_submitter.submit_week("2026-W29", "the text", settings=fake_ihk)


def test_submit_week_creates_next_sequential_entry(monkeypatch, fake_ihk):
    # newest known entry is 2026-W28 -> 2026-W29 is the next sequential week
    monkeypatch.setattr(
        IhkClient, "list_entries", lambda self: {"2026-W28": {"lfdnr": 41, "status": "Nachweis genehmigt"}}
    )
    monkeypatch.setattr(IhkClient, "create_next_entry", lambda self: 42)
    saved = []
    monkeypatch.setattr(
        IhkClient, "save_entry", lambda self, lfdnr, text, a1=None, a2=None: saved.append((lfdnr, text, a1, a2))
    )

    ihk_submitter.submit_week("2026-W29", "the text", settings=fake_ihk)
    assert saved == [(42, "the text", None, None)]


def test_submit_week_refuses_to_skip_ahead(monkeypatch, fake_ihk):
    # newest known entry is 2026-W28, but 2026-W30 is requested - not the
    # immediate next week, "Neuer Eintrag" can't jump straight there.
    monkeypatch.setattr(
        IhkClient, "list_entries", lambda self: {"2026-W28": {"lfdnr": 41, "status": "Nachweis genehmigt"}}
    )
    monkeypatch.setattr(IhkClient, "create_next_entry", lambda self: pytest.fail("should not create an entry"))
    monkeypatch.setattr(IhkClient, "save_entry", lambda self, lfdnr, text: pytest.fail("should not save"))

    with pytest.raises(IhkError):
        ihk_submitter.submit_week("2026-W30", "the text", settings=fake_ihk)


def test_submit_week_with_no_existing_entries_requires_current_as_next(monkeypatch, fake_ihk):
    monkeypatch.setattr(IhkClient, "list_entries", lambda self: {})
    monkeypatch.setattr(IhkClient, "create_next_entry", lambda self: pytest.fail("should not create an entry"))

    with pytest.raises(IhkError):
        ihk_submitter.submit_week("2026-W29", "the text", settings=fake_ihk)


def test_sync_status_writes_and_classifies(monkeypatch, fake_ihk):
    # sync_status is status/lfdnr metadata only - it must NOT call
    # fetch_entry or read back any content (one-way data flow: this site
    # -> IHK, never the reverse). No fetch_entry stub here on purpose;
    # if sync_status ever starts calling it, this test breaks loudly.
    monkeypatch.setattr(
        IhkClient,
        "list_entries",
        lambda self: {
            "2026-W28": {"lfdnr": 41, "status": "Nachweis genehmigt"},
            "2026-W29": {"lfdnr": 42, "status": "in Bearbeitung bei Azubi"},
            "2026-W30": {"lfdnr": 43, "status": "some future status IHK hasn't shown us yet"},
        },
    )

    status = ihk_submitter.sync_status(settings=fake_ihk)
    assert status["2026-W28"]["status"] == "genehmigt"
    assert status["2026-W29"]["status"] == "in_bearbeitung"
    assert status["2026-W30"]["status"] == "unknown"
    assert set(status["2026-W28"].keys()) == {"lfdnr", "status", "syncedAt"}
    assert storage.load_ihk_status(fake_ihk.user_id) == status


def test_load_status_round_trips(fake_ihk):
    assert ihk_submitter.load_status(settings=fake_ihk) == {}
    storage.save_ihk_status(fake_ihk.user_id, {"2026-W29": {"lfdnr": 42, "status": "genehmigt"}})
    assert ihk_submitter.load_status(settings=fake_ihk) == {"2026-W29": {"lfdnr": 42, "status": "genehmigt"}}


def test_save_local_fields_round_trips(fake_ihk):
    entry = ihk_submitter.save_local_fields("2026-W29", "worked on X", "training Y", settings=fake_ihk)
    assert entry["ausbinhalt1"] == "worked on X"
    assert entry["ausbinhalt2"] == "training Y"
    assert "savedAt" in entry
    assert ihk_submitter.load_local_fields(settings=fake_ihk)["2026-W29"]["ausbinhalt1"] == "worked on X"


def test_save_local_fields_preserves_field_when_omitted(fake_ihk):
    ihk_submitter.save_local_fields("2026-W29", "worked on X", "training Y", settings=fake_ihk)
    ihk_submitter.save_local_fields("2026-W29", "worked on X v2", None, settings=fake_ihk)
    entry = ihk_submitter.load_local_fields(settings=fake_ihk)["2026-W29"]
    assert entry["ausbinhalt1"] == "worked on X v2"
    assert entry["ausbinhalt2"] == "training Y"  # untouched, not wiped by the omitted arg


def test_save_local_fields_is_noop_when_both_none(fake_ihk):
    ihk_submitter.save_local_fields("2026-W29", None, None, settings=fake_ihk)
    assert storage.load_local_fields(fake_ihk.user_id) == {}


def test_load_local_fields_round_trips(fake_ihk):
    assert ihk_submitter.load_local_fields(settings=fake_ihk) == {}
    storage.save_local_fields(fake_ihk.user_id, {"2026-W29": {"ausbinhalt1": "a", "ausbinhalt2": "b"}})
    assert ihk_submitter.load_local_fields(settings=fake_ihk) == {"2026-W29": {"ausbinhalt1": "a", "ausbinhalt2": "b"}}


def test_save_entry_raises_if_save_does_not_actually_persist(monkeypatch, user_settings):
    """Regression test for the real bug found while building this feature:
    a save can return HTTP 200 and echo the submitted text back without
    actually persisting it (stale ausbinhalt13 causes a silent server-side
    rejection). save_entry must always re-fetch and compare, not trust the
    POST response."""
    client = IhkClient(settings=user_settings)
    client.base = "https://example.invalid/tibrosBB"

    class FakeResponse:
        url = "https://example.invalid/tibrosBB/azubiHeftEditForm.jsp?token=x"

        def raise_for_status(self):
            pass

    class FakeSession:
        def post(self, *a, **k):
            return FakeResponse()

    client.s = FakeSession()

    fetch_results = [
        {
            "lfdnr": 1, "token": "T1", "edtvon": "01.01.2026", "edtbis": "07.01.2026",
            "ausbabschnitt": "18.42", "ausbMail": "a@a.com",
            "ausbinhalt1": "", "ausbinhalt2": "",
            "ausbinhalt3": "old content", "ausbinhalt13": "old content",
        },
        {
            # unchanged after the "save" - simulates the real silent-rejection bug
            "lfdnr": 1, "token": "T2", "edtvon": "01.01.2026", "edtbis": "07.01.2026",
            "ausbabschnitt": "18.42", "ausbMail": "a@a.com",
            "ausbinhalt1": "", "ausbinhalt2": "",
            "ausbinhalt3": "old content", "ausbinhalt13": "old content",
        },
    ]
    monkeypatch.setattr(client, "fetch_entry", lambda lfdnr: fetch_results.pop(0))

    with pytest.raises(IhkError):
        client.save_entry(1, "new content")


def test_save_entry_preserves_existing_ausbinhalt1_and_2_when_not_specified(monkeypatch, user_settings):
    """Regression test: save_entry() used to hardcode ausbinhalt1/2 as ""
    on every save, silently wiping any manually-entered content on the
    real IHK site. It must now preserve whatever's currently there when
    the caller doesn't explicitly pass a value."""
    client = IhkClient(settings=user_settings)
    client.base = "https://example.invalid/tibrosBB"

    class FakeResponse:
        url = "https://example.invalid/tibrosBB/azubiHeft.jsp"

        def raise_for_status(self):
            pass

    captured_payloads = []

    class FakeSession:
        def post(self, url, data=None, files=None, timeout=None):
            captured_payloads.append(data)
            return FakeResponse()

    client.s = FakeSession()

    current_entry = {
        "lfdnr": 1, "token": "T1", "edtvon": "01.01.2026", "edtbis": "07.01.2026",
        "ausbabschnitt": "18.42", "ausbMail": "a@a.com",
        "ausbinhalt1": "existing work log entry",
        "ausbinhalt2": "existing training entry",
        "ausbinhalt3": "new school content",
        "ausbinhalt13": "old school content",
    }
    # fetch_entry is called twice: once to build the payload, once to verify
    monkeypatch.setattr(client, "fetch_entry", lambda lfdnr: dict(current_entry))

    client.save_entry(1, "new school content")  # ausbinhalt1/2 not specified

    assert captured_payloads[0]["ausbinhalt1"] == "existing work log entry"
    assert captured_payloads[0]["ausbinhalt2"] == "existing training entry"


def test_create_next_entry_parses_draft_response():
    """'Neuer Eintrag' doesn't persist anything - it returns a blank draft
    (lfdnr='0') for the week it would become once saved."""
    client = IhkClient.__new__(IhkClient)
    client.base = "https://example.invalid/tibrosBB"

    draft_html = """
    <input type=hidden name=token value='DRAFTTOK'/>
    <input type=hidden name=lfdnr value='0'/>
    <input class='form-control' type=text name='edtvon' value='20.07.2026' disabled='disabled' />
    <input class='form-control' type=text name='edtbis' value='26.07.2026' />
    <input class='form-control' type='text' name='ausbabschnitt' value='18.42' >
    <input class='form-control' type=text name='ausbMail' value='betreuer@example.com' />
    <textarea name='ausbinhalt1'></textarea>
    <textarea name='ausbinhalt2'></textarea>
    <textarea name='ausbinhalt3'>text from school goes here</textarea>
    <input type='hidden' name='ausbinhalt13' value='text from school goes here' />
    """

    class FakeResponse:
        text = draft_html

        def raise_for_status(self):
            pass

    class FakeSession:
        def post(self, url, data=None, timeout=None):
            assert data == {"neu": ""}
            return FakeResponse()

    client.s = FakeSession()

    draft = client.create_next_entry()
    assert draft["lfdnr"] == "0"
    assert draft["token"] == "DRAFTTOK"
    assert draft["edtvon"] == "20.07.2026"
    assert draft["edtbis"] == "26.07.2026"


def test_create_next_entry_raises_if_no_token_in_response():
    client = IhkClient.__new__(IhkClient)
    client.base = "https://example.invalid/tibrosBB"

    class FakeResponse:
        text = "<html><body>nothing useful here</body></html>"

        def raise_for_status(self):
            pass

    class FakeSession:
        def post(self, url, data=None, timeout=None):
            return FakeResponse()

    client.s = FakeSession()
    with pytest.raises(IhkError):
        client.create_next_entry()


_DRAFT = {
    "lfdnr": "0", "token": "DRAFTTOK", "edtvon": "20.07.2026", "edtbis": "26.07.2026",
    "ausbabschnitt": "18.42", "ausbMail": "betreuer@example.com",
    "ausbinhalt1": "", "ausbinhalt2": "", "ausbinhalt3": "", "ausbinhalt13": "text from school goes here",
}


def test_save_entry_with_draft_diffs_list_entries_around_the_save(user_settings):
    """Regression test for the actual live bug: create_next_entry() alone
    never changes list_entries() - the portal only persists on save - so
    the before/after diff to discover the newly-assigned lfdnr must
    happen around the SAVE POST, not around 'Neuer Eintrag' itself
    (that's what produced "expected exactly one new entry ... got set()"
    in production)."""
    client = IhkClient(settings=user_settings)
    client.base = "https://example.invalid/tibrosBB"

    class FakeResponse:
        url = "https://example.invalid/tibrosBB/azubiHeft.jsp"

        def raise_for_status(self):
            pass

    class FakeSession:
        def post(self, url, data=None, files=None, timeout=None):
            return FakeResponse()

    client.s = FakeSession()

    call_count = []

    def fake_list_entries(self):
        call_count.append(1)
        base = {"2026-W29": {"lfdnr": 2774528, "status": "in Bearbeitung bei Azubi"}}
        if len(call_count) == 1:
            return base  # before the save - draft week not there yet
        return {**base, "2026-W30": {"lfdnr": 9999999, "status": "in Bearbeitung bei Azubi"}}

    from app import ihk_client as ihk_client_module

    original = ihk_client_module.IhkClient.list_entries
    ihk_client_module.IhkClient.list_entries = fake_list_entries
    client.fetch_entry = lambda lfdnr: {**_DRAFT, "lfdnr": lfdnr, "ausbinhalt3": "new content"}
    try:
        real_lfdnr = client.save_entry(dict(_DRAFT), "new content")
    finally:
        ihk_client_module.IhkClient.list_entries = original

    assert real_lfdnr == 9999999
    assert len(call_count) == 2  # once before, once after the save POST


def test_save_entry_raises_if_draft_save_creates_no_new_entry(user_settings):
    """Same scenario, but the diff genuinely finds nothing new - must
    fail loudly rather than guess a lfdnr."""
    client = IhkClient(settings=user_settings)
    client.base = "https://example.invalid/tibrosBB"

    class FakeResponse:
        url = "https://example.invalid/tibrosBB/azubiHeftEditForm.jsp?token=x"

        def raise_for_status(self):
            pass

    class FakeSession:
        def post(self, url, data=None, files=None, timeout=None):
            return FakeResponse()

    client.s = FakeSession()

    same_entries = {"2026-W29": {"lfdnr": 2774528, "status": "in Bearbeitung bei Azubi"}}

    from app import ihk_client as ihk_client_module

    original = ihk_client_module.IhkClient.list_entries
    ihk_client_module.IhkClient.list_entries = lambda self: dict(same_entries)
    try:
        with pytest.raises(IhkError):
            client.save_entry(dict(_DRAFT), "new content")
    finally:
        ihk_client_module.IhkClient.list_entries = original
