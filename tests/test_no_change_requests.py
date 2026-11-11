"""An identical request that would change nothing needs no tap; everything
else asks.

These cases run the real inspect and lifecycle functions on a local
component, the real approval store and the real request tool; only the git
fetch and the card itself are doubles. An identical install is refused by
inspect as already installed; an identical upgrade (the active version, the
same effective settings, no secret names on either side) is ``not_needed``
with no record; anything else — another version, other settings, any secret
name, the same upgrade after a rollback or after a failed attempt — posts a
card."""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

import approval_records
import specialist_bundle_journal
import specialist_install
import specialist_install_consent
import specialist_receipt
import tools as tools_mod
from personality_binding import InstanceDir
from specialist_install import SpecialistInstallError
from test_consent_identity import (
    _approve, _component, _fresh_registry_snapshot, _inspect, _install, _installed_v1,  # noqa: F401
    _paths, _upgrade,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(approval_records, "PATH", str(tmp_path / "approval-records.json"))
    approval_records.reset()
    monkeypatch.setattr(tools_mod, "_PLUGIN_TOOLS_LOCK", asyncio.Lock())
    monkeypatch.setattr(tools_mod, "_SPECIALISTS_DIR", tmp_path / "specialists")
    real_load = specialist_receipt.load
    monkeypatch.setattr(specialist_receipt, "load",
                        lambda rid, receipts_dir=None: real_load(rid, tmp_path / "receipts"))
    cards: list = []

    def _card(**kw):
        cards.append(SimpleNamespace(record=kw["record"], submitted=kw.get("submitted_config")))
        return SimpleNamespace(refused=None)
    monkeypatch.setattr(specialist_install_consent, "prompt_specialist_install_consent", _card,
                        raising=False)
    monkeypatch.setattr(specialist_install_consent, "supersede_card", lambda r, **k: None,
                        raising=False)

    async def _posted(handle):
        return None
    monkeypatch.setattr(tools_mod, "_settle_install_consent_post", _posted)
    monkeypatch.setattr(tools_mod, "_casa_card_target", lambda: (
        SimpleNamespace(bot_id=777), SimpleNamespace(bot_id=777, chat_id=42), None))
    yield SimpleNamespace(cards=cards)
    approval_records.reset()


def _with_optional_secret(comp: Path) -> Path:
    """Declare the secret ``S`` (not required) beside the required ``A``,
    keeping the tree checksum-valid."""
    from specialist_component import compute_component_checksum
    (comp / "config-schema.json").write_text(
        json.dumps({"required": ["A"], "secret_names": ["S"]}), encoding="utf-8")
    manifest = json.loads((comp / "manifest.json").read_text(encoding="utf-8"))
    manifest["checksum"] = compute_component_checksum({
        name: (comp / name).read_bytes()
        for name in ("role/role.yaml", "role/doctrine.md", "config-schema.json")})
    (comp / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return comp


async def _ask(inspection, *, mode="upgrade", config=None, secrets=()):
    out = await tools_mod.specialist_install_request_approval.handler({
        "slug": "mtg", "root_digest": inspection.root_digest,
        "staged_dir": str(inspection.staged_dir), "receipt_id": inspection.receipt_id,
        "mode": mode, "config": dict(config or {}), "secret_names_provided": list(secrets)})
    return json.loads(out["content"][0]["text"])


def _snapshot(tmp_path) -> dict:
    """The installed tuples and settings (the content-addressed store aside,
    whose entries are written once per content and never change)."""
    root = tmp_path / "specialists"
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file() and p.relative_to(root).parts[0] != "store"}


def _current(inspection):
    return approval_records.current(f"{inspection.component_id}:{inspection.slug}")


def _records() -> list:
    approval_records.reset()
    store = approval_records._records  # noqa: SLF001 — what the store holds, read only
    with approval_records._LOCK:
        return list(store().values())


def _v1(tmp_path: Path) -> Path:
    """The component the installed v1 was inspected from."""
    return tmp_path / "v1" / "component"


# -- 1: identical install ---------------------------------------------------------------

async def test_an_identical_install_is_refused_by_inspect_as_already_installed(
        tmp_path, monkeypatch, env):
    _installed_v1(tmp_path, monkeypatch, config={"A": "1"})
    from specialist_registry import InstalledSpecialistIndex
    idx = InstalledSpecialistIndex(specialists_dir=str(tmp_path / "specialists"))
    idx.load()
    with pytest.raises(SpecialistInstallError) as raised:
        specialist_install.inspect_specialist_repo(
            "org/repo", "main", staging_root=tmp_path / "staging-again", installed_index=idx,
            receipts_dir=tmp_path / "receipts")
    assert raised.value.kind == "slug_collision"
    assert _records() == [] and env.cards == []


# -- 2: identical upgrade -----------------------------------------------------------------

async def test_an_identical_upgrade_with_no_secrets_needs_no_tap(tmp_path, monkeypatch, env):
    _installed_v1(tmp_path, monkeypatch, config={"A": "1"})
    again, _ = _inspect(tmp_path, monkeypatch, _v1(tmp_path), upgrade=True, sha="b" * 40)
    before = _snapshot(tmp_path)
    payload = await _ask(again, config={"A": "1"})
    assert payload == {"ok": True, "approval": "not_needed",
                       "detail": "already at 0.1.0 with these settings; nothing to change"}
    assert _records() == [] and env.cards == []
    assert _snapshot(tmp_path) == before
    # the settings it keeps count as the same settings
    assert (await _ask(again, config={}))["approval"] == "not_needed"


# -- 3: secret names --------------------------------------------------------------------

@pytest.mark.parametrize("installed, asked", [
    (["S"], ["S"]), ([], ["S"]), (["S"], [])], ids=["named-both", "new-name", "omitted"])
async def test_any_secret_name_involved_asks(tmp_path, monkeypatch, env, installed, asked):
    comp = _with_optional_secret(_component(tmp_path, "v1", required=["A"]))
    inspection, receipt = _inspect(tmp_path, monkeypatch, comp)
    values = {"config": {"A": "1"}, "secrets": installed}
    _install(tmp_path, inspection, receipt, _approve(tmp_path, inspection, values),
             config={"A": "1"}, approved=values, secrets=frozenset(installed))
    again, _ = _inspect(tmp_path, monkeypatch, comp, upgrade=True, sha="b" * 40)
    payload = await _ask(again, config={"A": "1"}, secrets=asked)
    assert payload["approval"] == "requested"
    (card,) = env.cards
    assert card.record.values == {"config": {"A": "1"}, "secrets": sorted(asked)}


# -- 4 and 5: one tap, one attempt ------------------------------------------------------

async def test_the_same_upgrade_after_a_rollback_asks(tmp_path, monkeypatch, env):
    _installed_v1(tmp_path, monkeypatch, config={"A": "1"})
    v2, receipt = _inspect(tmp_path, monkeypatch,
                           _component(tmp_path, "v2", required=["A"], version="0.2.0"),
                           upgrade=True, sha="b" * 40)
    values = {"config": {"A": "1"}, "secrets": []}
    _upgrade(tmp_path, v2, receipt, _approve(tmp_path, v2, values), config={},
             approved=values)
    instance, txn = specialist_install.rollback_specialist(
        slug="mtg", bundle=True, acks=specialist_install_consent.SpecialistInstallAckStore(
            path=tmp_path / "acks.json"), **_paths(tmp_path))
    specialist_bundle_journal.complete(txn.journal_path)
    assert specialist_install.parse_component_root(
        InstanceDir(tmp_path / "specialists" / "mtg").active().root)[1] == "0.1.0"
    again, _ = _inspect(tmp_path, monkeypatch,
                        _component(tmp_path, "v2b", required=["A"], version="0.2.0"),
                        upgrade=True, sha="c" * 40)
    assert (await _ask(again))["approval"] == "requested"
    assert len(env.cards) == 1


async def test_after_a_failed_attempt_asking_again_posts_a_new_card(tmp_path, monkeypatch, env):
    _installed_v1(tmp_path, monkeypatch, config={"A": "1"})
    v2, _ = _inspect(tmp_path, monkeypatch,
                     _component(tmp_path, "v2", required=["A"], version="0.2.0"),
                     upgrade=True, sha="b" * 40)
    first = await _ask(v2)
    assert first["approval"] == "requested"
    record = _current(v2)
    approval_records.approve(record.rid)
    async with tools_mod._PLUGIN_TOOLS_LOCK:          # its one attempt, which then failed
        assert approval_records.attempt(
            {"rid": record.rid, "generation": record.generation},
            kind="specialist_upgrade", consent_identity=record.consent_identity) is None
    second = await _ask(v2)
    assert second["approval"] == "requested" and len(env.cards) == 2
    assert _current(v2).rid != record.rid
    assert approval_records.get(record.rid).state == "used"


# -- 6: inherited settings ----------------------------------------------------------------

async def test_the_card_shows_the_settings_kept_from_the_current_install(
        tmp_path, monkeypatch, env):
    _installed_v1(tmp_path, monkeypatch, config={"A": "1"})
    v2, _ = _inspect(tmp_path, monkeypatch,
                     _component(tmp_path, "v2", required=["A"], version="0.2.0"),
                     upgrade=True, sha="b" * 40)
    assert (await _ask(v2, config={}))["approval"] == "requested"
    (card,) = env.cards
    assert card.record.values == {"config": {"A": "1"}, "secrets": []}
    assert card.submitted == {}
    # same version, other settings: a card too
    again, _ = _inspect(tmp_path, monkeypatch, _v1(tmp_path), upgrade=True, sha="c" * 40)
    assert (await _ask(again, config={"A": "2"}))["approval"] == "requested"
    assert env.cards[-1].record.values == {"config": {"A": "2"}, "secrets": []}


# -- 7: settings that changed after the approval ------------------------------------------

async def test_settings_changed_after_the_approval_refuse_at_the_library(
        tmp_path, monkeypatch, env):
    _installed_v1(tmp_path, monkeypatch, config={"A": "1"})
    v2, receipt = _inspect(tmp_path, monkeypatch,
                           _component(tmp_path, "v2", required=["A"], version="0.2.0"),
                           upgrade=True, sha="b" * 40)
    assert (await _ask(v2, config={}))["approval"] == "requested"
    approved = _current(v2).values                                 # {"A": "1"}, kept
    # the current install's settings change before the approved run commits
    v1b, r1b = _inspect(tmp_path, monkeypatch, _v1(tmp_path), upgrade=True, sha="d" * 40)
    changed = {"config": {"A": "3"}, "secrets": []}
    _upgrade(tmp_path, v1b, r1b, _approve(tmp_path, v1b, changed), config={"A": "3"},
             approved=changed)
    before = _snapshot(tmp_path)
    journals = sorted((tmp_path / "ops").iterdir()) if (tmp_path / "ops").exists() else []
    with pytest.raises(SpecialistInstallError) as raised:
        _upgrade(tmp_path, v2, receipt, _approve(tmp_path, v2, approved), config={},
                 approved=approved)
    assert raised.value.kind == "approval_values_differ"
    assert _snapshot(tmp_path) == before
    assert (sorted((tmp_path / "ops").iterdir()) if (tmp_path / "ops").exists() else []) == \
        journals
