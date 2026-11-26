"""The store module's copy works with the packages and the Python of this app."""
from __future__ import annotations

import stat

import pytest

from casa_browser.vault_store import DATABASE_NAME, KEY_NAME, Field, KeyProblem, Store


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _login_fields(password: str, websites: str) -> list[Field]:
    return [
        Field(id="", label="username", type="STRING", value="ellen@example.com"),
        Field(id="", label="password", type="CONCEALED", value=password),
        Field(id="", label="websites", type="URL", value=websites),
        Field(id="", label="one-time password", type="OTP",
              value="otpauth://totp/Example?secret=JBSWY3DPEHPK3PXP"),
        Field(id="", label="version", type="STRING", value="1"),
    ]


def test_the_copy_opens_writes_and_reopens_on_this_python(tmp_path):
    directory = tmp_path / "logins"
    store = Store.open(directory, None)
    item = store.create(title="example.com", category="LOGIN", tags=[],
                        fields=_login_fields("first", "https://example.com"),
                        writer="live_view")
    store.replace(item.id, title=None, category=None, tags=None,
                  fields=_login_fields("second",
                                       "https://example.com\nhttps://login.example.com"),
                  writer="live_view")

    again = Store.open(directory, None).get(item.id)
    assert again.title == "example.com"
    assert again.category == "LOGIN"
    assert again.writer == "live_view"
    assert [(f.label, f.type, f.value) for f in again.fields] == [
        (f.label, f.type, f.value)
        for f in _login_fields("second", "https://example.com\nhttps://login.example.com")
    ]
    assert _mode(directory) == 0o700
    assert _mode(directory / DATABASE_NAME) == 0o600
    assert _mode(directory / KEY_NAME) == 0o600


def test_a_missing_key_leaves_the_file_alone(tmp_path):
    directory = tmp_path / "logins"
    Store.open(directory, None).create(title="example.com", category="LOGIN", tags=[],
                                       fields=_login_fields("first", "https://example.com"),
                                       writer="live_view")
    database = directory / DATABASE_NAME
    before = database.read_bytes()
    (directory / KEY_NAME).unlink()

    with pytest.raises(KeyProblem):
        Store.open(directory, None)
    assert database.read_bytes() == before
