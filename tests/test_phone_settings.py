"""The phone options read from env, and the effective settings."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from phone import settings
from phone import store as pstore
from phone.store import PhoneStore


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    for name in ("PHONE_ENABLED", "PHONE_TRANSFER_NUMBER", "PHONE_OWNER_NAME",
                 "LIVE_VOICES", "LIVE_API_HOST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CONTACTS_DEFAULT_REGION", "NL")
    settings.reset()
    yield
    settings.reset()


def _fresh() -> settings.Options:
    settings.reset()
    return settings.options()


def test_defaults_when_nothing_is_set() -> None:
    opts = _fresh()
    assert opts == settings.Options(
        enabled=False, transfer_number="", owner_name="", seed_voices={},
        api_host="")


def test_enabled_only_for_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHONE_ENABLED", "true")
    assert _fresh().enabled is True
    monkeypatch.setenv("PHONE_ENABLED", "false")
    assert _fresh().enabled is False


def test_live_voices_skip_malformed_parts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIVE_VOICES", "nl=voice_a, it=voice_b,bad,en=marin")
    assert _fresh().seed_voices == {
        "nl": "voice_a", "it": "voice_b", "en": "marin"}


def test_voice_param() -> None:
    assert settings.voice_param("voice_a") == {"id": "voice_a"}
    assert settings.voice_param("marin") == "marin"


def test_transfer_number_is_normalised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("PHONE_TRANSFER_NUMBER", "06 12345678")
    assert _fresh().transfer_number == "+31612345678"
    monkeypatch.setenv("PHONE_TRANSFER_NUMBER", "garbage")
    with caplog.at_level(logging.WARNING):
        assert _fresh().transfer_number == ""
    assert len([r for r in caplog.records
                if r.levelno == logging.WARNING]) == 1
    assert "garbage" not in caplog.text


def test_owner_and_host_are_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHONE_OWNER_NAME", " Zoe ")
    monkeypatch.setenv("LIVE_API_HOST", " eu.api.openai.com ")
    opts = _fresh()
    assert (opts.owner_name, opts.api_host) == ("Zoe", "eu.api.openai.com")


@pytest.fixture
def opened(tmp_path: Path) -> PhoneStore:
    store = PhoneStore(str(tmp_path / "phone"))
    assert store.open() is True
    return store


def test_effective_stored_beats_seed_beats_default(
    opened: PhoneStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIVE_VOICES", "nl=voice_seed,en=marin")
    with opened.transaction(write=True) as conn:
        pstore.set_voice(conn, "nl", "voice_stored")
        pstore.set_setting(conn, "helper_model", "stored-model")
        pstore.set_grant(conn, "known", "transfer", "emergency")
    settings.reset()
    eff = settings.effective(opened)
    assert eff.voices == {"nl": "voice_stored", "en": "marin"}
    assert eff.models["helper_model"] == "stored-model"
    assert eff.models["voice_model"] == settings.DEFAULTS["voice_model"]
    assert eff.grants[("known", "transfer")] == "emergency"
    assert eff.grants[("priority", "transfer")] == "open"
    assert eff.grants[("unknown", "transfer")] == "emergency"


def test_unavailable_store_keeps_seeds_but_grants_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIVE_VOICES", "nl=voice_seed")
    store = PhoneStore(str(tmp_path / "phone"))
    assert store.open() is True
    with store.transaction(write=True) as conn:
        pstore.set_grant(conn, "known", "transfer", "off")
    broken = PhoneStore(str(tmp_path / "never-opened"))
    settings.reset()
    for candidate in (broken, None):
        eff = settings.effective(candidate)
        assert eff.voices == {"nl": "voice_seed"}
        assert eff.models == settings.DEFAULTS
        assert eff.grants == {key: "off"
                              for key in settings.DEFAULT_GRANTS}


def test_levels_ascend() -> None:
    assert settings.LEVELS == ("off", "emergency", "urgent", "open")


def test_bad_transfer_number_warns_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    opened: PhoneStore,
) -> None:
    monkeypatch.setenv("PHONE_TRANSFER_NUMBER", "garbage")
    settings.reset()
    with caplog.at_level(logging.WARNING):
        settings.options()
        settings.options()
        settings.effective(opened)
    assert len([r for r in caplog.records
                if r.levelno == logging.WARNING]) == 1


def test_unreadable_store_gives_seeds_and_no_grants(
    opened: PhoneStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIVE_VOICES", "nl=voice_seed")
    settings.reset()
    with opened.transaction(write=True) as conn:
        pstore.set_grant(conn, "known", "transfer", "off")
        pstore.set_voice(conn, "nl", "voice_stored")
        pstore.set_setting(conn, "helper_model", "stored-model")
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(opened.path + suffix).unlink()
        except FileNotFoundError:
            pass
    Path(opened.path).write_bytes(b"this is not a database" * 100)
    eff = settings.effective(opened)
    assert eff.voices == {"nl": "voice_seed"}
    assert eff.models == settings.DEFAULTS
    assert eff.grants == {key: "off" for key in settings.DEFAULT_GRANTS}
