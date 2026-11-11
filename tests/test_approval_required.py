"""Every approval-governed mutation needs its run's own approval.

An ordinary delegation from Ellen to the configurator carries no approval:
there an install, an upgrade or a persona install is refused
``approval_required`` before anything reaches the library, whatever acks the
ledger still holds from earlier approvals and whatever newer request is
pending. Rollback, uninstall and persona apply are not governed and behave as
they always did."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import approval_records
import tools as tools_mod
from specialist_install_consent import identity_for_inspection
from test_approval_claim_under_lease import ARGS, SLUG, VALUES, lib  # noqa: F401 — fixture
from test_configurator_continue import _settled, _ordinary_delegation, env  # noqa: F401

pytestmark = pytest.mark.asyncio


def _inside_the_ordinary_run(env, call):
    results: list = []

    async def _hook(origin):
        assert approval_records.APPROVAL_KEY not in origin
        results.append(await call())
    env.runs.hook = _hook
    return results


async def _payload(tool, args):
    out = await tool.handler(dict(args))
    return json.loads(out["content"][0]["text"])


@pytest.mark.parametrize("tool", ["specialist_install_commit", "specialist_upgrade"])
async def test_a_governed_specialist_call_without_its_runs_approval_is_refused(
        env, lib, tool):
    # the ledger acked A long ago; a newer request B is pending now
    async with tools_mod._PLUGIN_TOOLS_LOCK:
        approval_records.request(
            "specialist_upgrade" if tool == "specialist_upgrade" else "specialist_install",
            f"weather:{SLUG}", identity_for_inspection(lib.inspection,
                                                       values={"config": {"A": "2"},
                                                               "secrets": []}),
            {"config": {"A": "2"}, "secrets": []})
    results = _inside_the_ordinary_run(
        env, lambda: _payload(getattr(tools_mod, tool), ARGS))
    assert (await _ordinary_delegation())["status"] == "pending"
    await _settled(env)
    (payload,) = results
    assert payload == {"ok": False, "kind": "approval_required"}
    assert lib.calls == []                                     # nothing reached the library


async def test_a_persona_install_without_its_runs_approval_is_refused(env, monkeypatch):
    import persona_install
    import persona_pack
    pack = SimpleNamespace(persona_id="casa/judge", version="0.2.0", checksum="sha256:" + "e" * 64,
                           identity={"display_name": "Judge"})
    monkeypatch.setattr(persona_pack, "load_persona_pack", lambda *a, **k: pack)
    called: list = []
    monkeypatch.setattr(persona_install, "commit_persona_install",
                        lambda **kw: called.append(kw) or pack)
    results = _inside_the_ordinary_run(env, lambda: _payload(
        tools_mod.persona_install_commit,
        {"persona_id": pack.persona_id, "version": pack.version, "checksum": pack.checksum,
         "staged_dir": "/staged/judge"}))
    await _ordinary_delegation()
    await _settled(env)
    (payload,) = results
    assert payload == {"ok": False, "kind": "approval_required"}
    assert called == []


async def test_uninstall_and_rollback_need_no_approval(env, monkeypatch):
    import specialist_bundle_journal
    import specialist_install
    calls: list = []

    def _txn(**kw):
        calls.append(kw["slug"])
        return SimpleNamespace(slug=kw["slug"], removed_artifact_ids=(), new_artifact_ids=(),
                               journal_path="", removed_owned_names=())
    monkeypatch.setattr(specialist_install, "uninstall_specialist", _txn)
    monkeypatch.setattr(specialist_install, "rollback_specialist",
                        lambda **kw: (SimpleNamespace(slug=kw["slug"], state="active"), _txn(**kw)))

    async def _seq(slug, *, removed_artifact_ids, targets_removed):
        return {"ok": True, "reloaded": [], "verify": {}}

    async def _nothing(*a, **k):
        return True
    monkeypatch.setattr(tools_mod, "_bundle_reload_and_verify", _seq)
    monkeypatch.setattr(tools_mod, "_own_chat_hook", _nothing)
    monkeypatch.setattr(tools_mod, "_open_specialist_engagements", lambda slug: [])
    monkeypatch.setattr(tools_mod, "_bound_persona", lambda slug: None)
    monkeypatch.setattr(specialist_bundle_journal, "complete", lambda p: None)

    async def _both():
        return [await _payload(tools_mod.specialist_rollback, {"slug": SLUG}),
                await _payload(tools_mod.specialist_uninstall, {"slug": SLUG})]
    results = _inside_the_ordinary_run(env, _both)
    await _ordinary_delegation()
    await _settled(env)
    (pair,) = results
    assert [p["ok"] for p in pair] == [True, True]
    assert calls == [SLUG, SLUG]


async def test_persona_apply_needs_no_approval(env):
    results = _inside_the_ordinary_run(env, lambda: _payload(
        tools_mod.persona_apply,
        {"target_role_id": "specialist:../x", "persona_id": "casa/judge",
         "persona_version": "0.2.0"}))
    await _ordinary_delegation()
    await _settled(env)
    (payload,) = results
    assert payload.get("kind") != "approval_required"
    assert "approval" not in json.dumps(payload)
