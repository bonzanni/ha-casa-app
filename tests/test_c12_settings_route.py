"""#1139: changing an installed specialist's settings is a same-version
``specialist_upgrade`` (operator ruling, option 1), and the configurator's
recipes say so.

Two kinds of test live here:

- a regression pin, green at the base: the documented route WORKS -- a
  ``upgrade_specialist`` at the installed root with new ``config`` commits,
  keeps a setting the caller left out, and rotates the previous settings into
  the single retained prior (which is the rollback cost the recipe tells).
  Mutant it guards against: a same-root refusal;
- pins of the new recipe text (red at the base): the settings section sits
  before step 1 and tells both costs first; ``active_present`` names the
  install commit's refusal of an installed slug; the retired update stub stops
  calling model tier and memory budget settings and points plugin secrets at
  ``set_plugin_env_reference``; the tool description admits the installed
  version.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

import specialist_bundle_journal
import specialist_install
import specialist_receipt

from test_specialist_bundle_commit import _declare_config_schema, _subdir_stub

try:
    from tests.specialist_fixtures import write_minimal_component
except ImportError:
    from specialist_fixtures import write_minimal_component

REPO_ROOT = Path(__file__).resolve().parents[1]
RECIPES = (REPO_ROOT / "casa/rootfs/opt/casa/defaults/agents/executors/configurator"
           / "doctrine/recipes/specialist")


def _flat(name: str) -> str:
    return re.sub(r"\s+", " ", (RECIPES / name).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# The route works (regression, green at base)
# --------------------------------------------------------------------------

@pytest.fixture
def installed(tmp_path, monkeypatch):
    """`mtg` installed and active with two saved, non-secret settings."""
    import plugin_registry
    from specialist_install_consent import (SpecialistInstallAckStore,
                                            install_consent_identity)
    from specialist_registry import InstalledSpecialistIndex

    plugin_registry.reload_snapshot(registry_path=tmp_path / "snap-registry.json",
                                    store_root=tmp_path / "snap-store")
    comp, mpath = write_minimal_component(tmp_path, slug="mtg")
    _declare_config_schema(comp, mpath, required=["k", "j"], secret_names=[])
    monkeypatch.setattr(specialist_install, "resolve_and_fetch", _subdir_stub(comp))
    idx = InstalledSpecialistIndex(specialists_dir=str(tmp_path / "installed-index"))
    idx.load()
    insp = specialist_install.inspect_specialist_repo(
        "org/repo", "main", staging_root=tmp_path / "staging",
        installed_index=idx, receipts_dir=tmp_path / "receipts")
    receipt = specialist_receipt.load(insp.receipt_id, receipts_dir=tmp_path / "receipts")
    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    acks.record(
        identity=install_consent_identity(
            component_id=insp.component_id, version=insp.version,
            root_digest=insp.root_digest, slug=insp.slug,
            receipt_digest=insp.receipt_digest),
        component_id=insp.component_id, version=insp.version,
        component_checksum=insp.root_digest, slug=insp.slug,
        receipt_digest=insp.receipt_digest)
    common = dict(
        secret_names_provided=frozenset(), acks=acks,
        specialists_dir=tmp_path / "specialists",
        agents_specialists_dir=tmp_path / "agents",
        registry_path=tmp_path / "registry.json",
        plugin_store_root=tmp_path / "store", ops_dir=tmp_path / "ops")
    instance, txn = specialist_install.commit_specialist_install(
        inspection=insp, receipt=receipt, config={"k": "old", "j": "kept"}, **common)
    specialist_bundle_journal.complete(txn.journal_path)
    assert instance.state == "active", instance.last_activation_error
    return insp, receipt, common, tmp_path / "specialists" / "mtg"


def _tuple(slug_dir: Path, name: str) -> dict:
    return yaml.safe_load((slug_dir / name).read_text(encoding="utf-8"))


def test_same_version_upgrade_changes_settings_and_rotates_the_previous_ones(installed):
    insp, receipt, common, slug_dir = installed
    before = _tuple(slug_dir, "active.yaml")

    inst, txn = specialist_install.upgrade_specialist(
        slug="mtg", inspection=insp, receipt=receipt, config={"k": "new"}, **common)
    specialist_bundle_journal.complete(txn.journal_path)

    assert inst.state == "active", inst.last_activation_error
    active = _tuple(slug_dir, "active.yaml")
    prior = _tuple(slug_dir, "active.prior.yaml")
    # Same version, same root: a settings change, not a version change.
    assert active["root"] == before["root"] == prior["root"]
    # The passed value changed; the one left out kept its current value.
    assert active["config_snapshot"] == {"k": "new", "j": "kept"}
    # Rollback's single retained prior is now the previous SETTINGS at the
    # same version -- the earlier tuple is no longer retained.
    assert prior["config_snapshot"] == {"k": "old", "j": "kept"}


def test_the_install_commit_refuses_the_installed_slug_with_active_present(installed):
    insp, receipt, common, slug_dir = installed
    before = (slug_dir / "active.yaml").read_bytes()
    with pytest.raises(specialist_install.SpecialistInstallError) as ei:
        specialist_install.commit_specialist_install(
            inspection=insp, receipt=receipt, config={"k": "new", "j": "kept"}, **common)
    assert ei.value.kind == "active_present"
    assert (slug_dir / "active.yaml").read_bytes() == before


# --------------------------------------------------------------------------
# The recipe text (pins of the new structure, red at base)
# --------------------------------------------------------------------------

SECTION = "## Change an installed specialist's settings"


def test_upgrade_recipe_settings_section_tells_the_costs_before_step_1():
    text = (RECIPES / "upgrade.md").read_text(encoding="utf-8")
    assert text.count(SECTION) == 1
    start = text.index(SECTION)
    step1 = text.index("\n1. `specialist_install_inspect(")
    relay = text.index('kind: "open_conversations_unconfirmed"')
    assert start < step1 < relay
    section = re.sub(r"\s+", " ", text[start:step1])
    assert "`specialist_upgrade`" in section
    assert "to the version it already has" in section
    assert "may ask for their approval again" in section
    assert ('"rollback" restores the previous settings, not the previous version, and '
            "that previous version can no longer be rolled back to") in section
    # The two costs come before the section first mentions inspecting.
    assert section.index("rolled back to") < section.index("Then follow the steps below")
    # A settings change never ends "kept, not active yet" (same root, no
    # lossy capture), and rc20 keeps the kept-kind literal in one paragraph.
    assert "upgrade_kept_new_version" not in section
    assert "not active yet" not in section.lower()
    # The same-version route names the installed ref, never `latest`.
    assert "never `ref=\"latest\"`" in section
    assert "real upgrade, not a settings change" in section


def test_upgrade_recipe_steps_admit_the_same_version_route():
    text = _flat("upgrade.md")
    assert "a newer ref (for a settings change, the installed ref — see above)" in text
    assert "at the version already installed the approval may already be on record" in text


def test_recipes_name_active_present_for_the_install_commit_refusal():
    for name in ("install.md", "upgrade.md"):
        text = _flat(name)
        assert "concurrent_mutation" not in text, name
        assert "active_present" in text, name
    assert ("still-active old version makes `specialist_install_commit` refuse "
            "`active_present`") in _flat("upgrade.md")
    assert ('refuses any slug with an active tuple (`kind: "active_present"`)'
            in _flat("install.md"))


def test_retired_update_stub_routes_settings_and_secrets():
    raw = (RECIPES / "update.md").read_text(encoding="utf-8")
    assert raw.splitlines()[0].endswith("RETIRED")
    text = _flat("update.md")
    assert "specialist_install_commit" not in text
    assert "Change an installed specialist's settings" in text
    assert "`recipes/specialist/upgrade.md`" in text
    assert "Its model tier and memory budget are not settings" in text
    assert "(model tier, memory budget" not in text
    assert "`set_plugin_env_reference`" in text
    assert "`recipes/plugin/secrets.md`" in text


def test_specialist_upgrade_description_admits_the_installed_version():
    import tools
    desc = tools.specialist_upgrade.description
    assert "or to the version it already has, passing new config, to change its settings" in desc
    assert desc.count(tools.SPECIALIST_OPEN_CONVERSATION_NOTICE) == 1
    assert desc.endswith(tools._ORDINARY_CHANGE_TOOL_NOTE)
