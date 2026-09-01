"""#674 — `specialist_rollback` is the way back from a specialist's FIRST persona
override, and the shipped doctrine has to say so.

`persona_apply` on a specialist stages and commits an override tuple through
the same `commit_desired_to_active` every upgrade uses, so the component-default
binding is rotated into `active.prior.yaml` — exactly the tuple `_rollback_core`
restores. The mechanism existed; every surface an executor reads framed the tool
as a version operation, so the configurator told operators the undo did not
exist. These tests pin (t1) the tool description, (t2) the real handler's
behaviour — including the disclosure of the owned-plugin generation it
republishes, and the fact that a rollback EXCHANGES the active tuple with the
single retained prior rather than consuming it — and (t3) the recipe content.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_DOCTRINE = (Path(__file__).resolve().parent.parent
             / "casa/rootfs/opt/casa/defaults/agents/executors/configurator/doctrine")
_DISCLOSURE_KEYS = ("plugin_data_plugins", "plugin_data_may_remain",
                    "provider_revocation_performed", "plugin_data_note")


def _read(rel: str) -> str:
    return (_DOCTRINE / rel).read_text(encoding="utf-8")


def test_specialist_rollback_description_names_both_prior_tuple_sources() -> None:
    """t1 — the description is the only surface an agent reads BEFORE calling.
    RED at the base: it names the prior VERSION and only the upgrade case."""
    from tools import specialist_rollback

    text = specialist_rollback.description
    lower = text.lower()
    assert "retained prior tuple" in lower
    assert "upgrade or persona override" in lower
    assert "no_prior_tuple" in text
    assert "nothing was retained" in lower


@pytest.mark.asyncio
async def test_specialist_rollback_handler_exchanges_a_persona_override_with_component_default(
        tmp_path: Path, monkeypatch) -> None:
    """t2 — the real handler over the real cores: a first override is undone by
    one rollback, whose owned-plugin republish is DISCLOSED in the envelope
    (the override apply never rotated the sidecar, so the retained owned
    generation is the pre-install one — empty); and a second rollback swaps
    the override back, because a rollback exchanges active and prior. Green at
    the base — it pins the mechanism the doctrine now names."""
    import plugin_registry
    import specialist_bundle_journal
    import specialist_install
    import specialist_install_consent
    from persona_install import apply_persona_override
    from personality_binding import InstanceDir, load_instance_tuple
    from test_specialist_bundle_commit import _owned, _prep_multi
    from test_tools_specialist_install import _payload, _stub_bundle_sequencer
    from test_wholebranch_security_fixes import (
        _load_specialist_persona_role, _publish_installed_copy,
    )
    from tools import specialist_rollback

    plugin_registry.reload_snapshot(
        registry_path=tmp_path / "snap-registry.json", store_root=tmp_path / "snap-store")
    ctx = _prep_multi(tmp_path, monkeypatch, ["mtg"])
    _instance, install_txn = specialist_install.commit_specialist_install(**ctx.kw)
    specialist_bundle_journal.complete(install_txn.journal_path)
    specialists_dir = ctx.kw["specialists_dir"]
    registry_path = ctx.kw["registry_path"]
    slug_dir = specialists_dir / "mtg"

    installed = InstanceDir(slug_dir).active()
    assert installed.binding.mode == "component-default"
    original_root = installed.root
    assert [e["name"] for e in _owned(registry_path, "mtg")] == ["mtg.mtg"]

    # The first override, through the real apply core.
    persona, role = _load_specialist_persona_role(specialists_dir, "mtg")
    _publish_installed_copy(persona, specialists_dir, "mtg", tmp_path, monkeypatch)
    overridden = apply_persona_override(
        target_role_id="specialist:mtg", persona=persona, role=role,
        instance_dir_root=slug_dir, candidate_validator=lambda p, b: None)
    assert overridden.binding.mode == "override"
    assert overridden.root == original_root
    override_source = overridden.binding.override_source
    assert [e["name"] for e in _owned(registry_path, "mtg")] == ["mtg.mtg"]
    assert load_instance_tuple(slug_dir / "active.prior.yaml").binding.mode == "component-default"

    # The handler resolves its core by module attribute at call time; bind the
    # real core to this test's trees and record every call.
    real_rollback = specialist_install.rollback_specialist
    rollback_calls: list[str] = []

    def _bound(*, slug, bundle=False, acks=None, **_ignored):
        rollback_calls.append(slug)
        assert bundle is True
        return real_rollback(
            slug=slug, bundle=True, acks=ctx.acks, specialists_dir=specialists_dir,
            agents_specialists_dir=ctx.kw["agents_specialists_dir"],
            registry_path=registry_path, plugin_store_root=ctx.kw["plugin_store_root"],
            ops_dir=ctx.kw["ops_dir"])

    monkeypatch.setattr(specialist_install, "rollback_specialist", _bound)
    monkeypatch.setattr(specialist_install_consent, "SpecialistInstallAckStore",
                        lambda: ctx.acks)
    _stub_bundle_sequencer(monkeypatch)

    first = _payload(await specialist_rollback.handler({"slug": "mtg"}))
    assert first["ok"] is True
    assert first["state"] == "active"
    assert first["plugin_data_plugins"] == ["mtg.mtg"]
    assert sum(k in first for k in _DISCLOSURE_KEYS) == 4
    active = InstanceDir(slug_dir).active()
    assert active.binding.mode == "component-default"
    assert active.root == original_root
    assert [e["name"] for e in _owned(registry_path, "mtg")] == []

    second = _payload(await specialist_rollback.handler({"slug": "mtg"}))
    assert second["ok"] is True
    assert second["state"] == "active"
    assert sum(k in second for k in _DISCLOSURE_KEYS) == 0
    active = InstanceDir(slug_dir).active()
    assert active.binding.mode == "override"
    assert active.binding.override_source == override_source
    assert active.root == original_root
    assert [e["name"] for e in _owned(registry_path, "mtg")] == ["mtg.mtg"]
    assert len(rollback_calls) == 2


def test_apply_recipe_routes_a_specialists_first_override_undo_through_the_rollback_recipe() -> None:
    """t3a — the SPECIALIST arm of the apply recipe names the way back and
    routes it through the rollback recipe (which carries the owned-plugin
    relay). RED at the base: the recipe has no undo at all."""
    text = _read("recipes/persona/apply.md")
    step_6 = text.split("\n6. ", 1)[1].split("\n## Common mistakes", 1)[0]
    assert "SPECIALIST" in step_6
    assert "`specialist_rollback`" in step_6
    assert "`recipes/specialist/rollback.md`" in step_6
    lower = step_6.lower()
    assert "first" in lower
    assert "override" in lower
    assert "component-default" in lower
    assert "retained prior" in lower
    assert "later override or an upgrade" in lower
    assert "`persona_apply`" in step_6
    assert "`resident_persona_reset`" in step_6
    assert "residents-only" in lower


def test_rollback_recipe_names_the_persona_override_and_states_the_exchange() -> None:
    """t3b — the rollback recipe opens on both retained-prior producers, warns
    about the owned-plugin generation before the call, and states what a
    second call does. RED at the base: it names only upgrades and says a second
    call returns `no_prior_tuple`, which t2 measures to be false."""
    text = _read("recipes/specialist/rollback.md")
    lower = text.lower()
    assert "bad upgrade" in lower
    assert "first persona override" in lower
    assert "component-default" in lower
    assert "retained prior" in lower
    assert "immediately-prior tuple" in lower
    assert "pre-override binding" in lower
    assert "owned-plugin generation" in lower
    assert "older or absent" in lower
    assert "confirm" in lower
    assert "`plugin_data_note`" in text
    assert "`plugin_list()`" in text
    mistakes = text.split("## Common mistakes", 1)[1].lower()
    assert "exchanges" in mistakes
    assert "second call" in mistakes
    assert "swaps them back" in mistakes
    assert "no_prior_tuple" in mistakes
    assert "nothing was ever retained" in mistakes
