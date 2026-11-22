"""The finance plugin's own `op` code, unchanged, against the built-in store.

The plugin lives in another repository. These tests load its `opvault.py`
(every `op` command it runs is in that one module) and its key loader from
a checkout named by `CASA_FINANCE_CHECKOUT`, by default a
`casa-specialist-finance` directory beside this repository, and are skipped
where there is none. The same command lines are held against the real CLI's
recorded answers in `test_vault_op.py`, which needs no checkout.
"""

import fcntl
import importlib.util
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import vault_drop_off
from vault_harness import builtin_op  # noqa: F401

REPOSITORY = Path(__file__).resolve().parents[1]
CHECKOUT = (REPOSITORY / os.environ.get("CASA_FINANCE_CHECKOUT",
                                        "../casa-specialist-finance")).resolve()
SERVER = CHECKOUT / "plugins" / "bank-feed" / "server"

pytestmark = pytest.mark.skipif(
    not (SERVER / "opvault.py").is_file(),
    reason=f"no finance plugin checkout at {CHECKOUT} (CASA_FINANCE_CHECKOUT names one)")

VAULT = "Casa"
ELSEWHERE = "Elsewhere"


@pytest.fixture
def plugin(builtin_op, tmp_path, monkeypatch):
    """The plugin's modules, set up the way its server process has them."""
    data = tmp_path / "plugin-data"
    data.mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data))
    monkeypatch.delenv("BANKFEED_OP_VAULT", raising=False)
    monkeypatch.delenv("BANKFEED_EB_ENVIRONMENT", raising=False)
    # The checkout is read, never written: importing a module would otherwise
    # leave a bytecode file beside it.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)

    def load(name):
        # By path and under the plugin's own module names, which is how they
        # import each other; nothing of the plugin goes on the import path.
        spec = importlib.util.spec_from_file_location(name, SERVER / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        return module

    load("ebmode")
    opvault, jwtsign = load("opvault"), load("jwtsign")
    # The plugin's server hands every `op` it starts the descriptor of the
    # lock its current call holds.
    lock = os.open(data / "lifecycle.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock, fcntl.LOCK_SH)
    monkeypatch.setattr(opvault, "INHERIT_FDS", (lock,))
    try:
        yield SimpleNamespace(opvault=opvault, jwtsign=jwtsign, store=builtin_op)
    finally:
        os.close(lock)


def values(item):
    return {field.label: field.value for field in item.fields}


def test_status_is_usable(plugin):
    assert plugin.opvault.status() is None
    assert plugin.opvault.VAULT == VAULT


def test_read_of_a_missing_item_is_not_found(plugin):
    opvault = plugin.opvault
    for reference in (opvault.REF_PRIVATE_KEY, opvault.REF_REFRESH_TOKEN, opvault.REF_EMAIL):
        with pytest.raises(opvault.OpError) as raised:
            opvault.read(reference)
        assert raised.value.not_found is True


def test_item_exists_is_false_then_true(plugin):
    opvault = plugin.opvault
    assert opvault.item_exists(opvault.CRED_ITEM, VAULT) is False
    opvault.upsert_field(opvault.CRED_ITEM, VAULT, "refresh token", "refresh-one")
    assert opvault.item_exists(opvault.CRED_ITEM, VAULT) is True


def test_upsert_field_creates_then_edits_without_a_second_item(plugin):
    opvault = plugin.opvault
    opvault.upsert_field(opvault.CRED_ITEM, VAULT, "refresh token", "refresh-one")
    (item,) = plugin.store.items()
    assert (item.title, item.category) == ("EnableBanking", "API_CREDENTIAL")
    assert opvault.read(opvault.REF_REFRESH_TOKEN) == "refresh-one"

    opvault.upsert_field(opvault.CRED_ITEM, VAULT, "refresh token", "refresh-two")
    opvault.upsert_field(opvault.CRED_ITEM, VAULT, "username", "resident@example.org",
                         concealed=False)
    (edited,) = plugin.store.items()
    assert edited.id == item.id
    assert values(edited) == {"refresh token": "refresh-two",
                              "username": "resident@example.org"}
    assert opvault.read(opvault.REF_REFRESH_TOKEN) == "refresh-two"
    assert opvault.read(opvault.REF_EMAIL) == "resident@example.org"


def test_a_field_not_stored_yet_reads_as_not_found(plugin):
    # 1Password gives an API credential an empty `username` of its own; the
    # store keeps only the fields that were written. Before the account
    # email is first stored, the plugin's read of it must come out as
    # "absent", never as a fault.
    opvault = plugin.opvault
    opvault.upsert_field(opvault.CRED_ITEM, VAULT, "refresh token", "refresh-one")
    with pytest.raises(opvault.OpError) as raised:
        opvault.read(opvault.REF_EMAIL)
    assert raised.value.not_found is True
    assert "does not have a field 'username'" in str(raised.value)


def test_set_field_on_a_missing_item_is_not_found(plugin):
    opvault = plugin.opvault
    with pytest.raises(opvault.OpError) as raised:
        opvault.set_field(opvault.CRED_ITEM, VAULT, "refresh token", "s3cr3t-value")
    assert raised.value.not_found is True
    assert "s3cr3t-value" not in str(raised.value)
    assert plugin.store.items() == []


def test_a_wrong_vault_name_is_a_fault_not_an_absence(plugin):
    opvault = plugin.opvault
    opvault.upsert_field(opvault.CRED_ITEM, VAULT, "refresh token", "refresh-one")
    before = plugin.store.items()
    attempts = [
        lambda: opvault.read(f"op://{ELSEWHERE}/{opvault.CRED_ITEM}/refresh token"),
        lambda: opvault.item_exists(opvault.CRED_ITEM, ELSEWHERE),
        lambda: opvault.set_field(opvault.CRED_ITEM, ELSEWHERE, "refresh token", "never-stored"),
        # An "absent" here would make the plugin create a second item.
        lambda: opvault.upsert_field(opvault.CRED_ITEM, ELSEWHERE, "refresh token", "never-stored"),
    ]
    for attempt in attempts:
        with pytest.raises(opvault.OpError) as raised:
            attempt()
        assert raised.value.not_found is False
        assert "isn't a vault" in str(raised.value)
    assert plugin.store.items() == before


@pytest.mark.slow
def test_the_generated_ssh_key_loads_with_the_plugins_own_loader(plugin):
    # Slow: the plugin asks for a 4096-bit key, which takes seconds to make.
    opvault = plugin.opvault
    opvault.create_ssh_key(opvault.KEY_ITEM, VAULT)
    assert opvault.item_exists(opvault.KEY_ITEM, VAULT) is True

    pem = opvault.read(opvault.REF_PRIVATE_KEY)
    key = plugin.jwtsign.load_pkcs8(pem)
    assert key.n.bit_length() == 4096
    signature = plugin.jwtsign.sign(b"signed with the stored key", key)
    assert len(signature) == key.size_bytes
    (item,) = plugin.store.items()
    assert (item.title, item.category) == ("EnableBanking Key", "SSH_KEY")


def test_take_drop_off_reads_and_deletes(plugin):
    opvault = plugin.opvault
    assert opvault.take_drop_off("signin_link") is None

    link = "https://signin.example.test/magic?code=abc==&x=[y]"
    stored_at = time.time()
    assert vault_drop_off.store(opvault.DROP_OFF_PLUGIN, "signin_link", link) == {
        "status": "ok"}
    value, created = opvault.take_drop_off("signin_link")
    assert value == link
    assert stored_at - 2 <= created <= time.time() + 2
    assert plugin.store.items() == []
    assert opvault.take_drop_off("signin_link") is None


def test_the_erasure_deletes_the_items_the_plugin_created_and_no_other(plugin):
    opvault = plugin.opvault
    assert vault_drop_off.store("another-plugin", "code", "kept") == {"status": "ok"}
    opvault.upsert_field(opvault.CRED_ITEM, VAULT, "refresh token", "refresh-one")
    assert len(plugin.store.items()) == 2

    assert opvault.erase_recorded() == (["EnableBanking"], [])
    assert [item.title for item in plugin.store.items()] == [
        vault_drop_off.item_title("another-plugin", "code")]
