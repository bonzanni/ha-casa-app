"""INV-PERS-019: the three shipped residents can wear the public demo persona
packs, and only those beyond their own persona.

Runs the real ``persona_apply`` tool against the real shipped role artifacts
and the real compile proof. The packs themselves live in their own public
repositories, so what stands in here is a schema-valid pack declaring each
published ``id@version``: what this pins is the roles' admission, not a pack's
size. The widening is deliberately narrow, which is why the refusals are pinned
beside the admissions.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from test_persona_install import install_persona_for_apply

ROLES = Path(__file__).resolve().parent.parent / "casa/rootfs/opt/casa/defaults/roles"
RESIDENT_SLOTS = ("assistant", "butler", "concierge")
PUBLISHED_DEMO_PACKS = (
    ("demo/bruce", "0.3.0"),
    ("demo/gollum", "0.1.0"),
    ("demo/marvin", "0.1.0"),
    ("demo/terminator", "0.1.0"),
    ("demo/yoda", "0.1.0"),
)


def _apply(tmp_path, monkeypatch, *, slot: str, persona_id: str, version: str) -> dict:
    import tools

    monkeypatch.setenv("CASA_BINDINGS_DIR", str(tmp_path / "bindings"))
    install_persona_for_apply(
        tmp_path, monkeypatch, persona_id=persona_id, version=version)
    result = asyncio.run(tools.persona_apply.handler({
        "target_role_id": f"resident:{slot}",
        "persona_id": persona_id, "persona_version": version,
    }))
    return json.loads(result["content"][0]["text"])


@pytest.mark.parametrize("slot", RESIDENT_SLOTS)
@pytest.mark.parametrize(("persona_id", "version"), PUBLISHED_DEMO_PACKS)
def test_a_shipped_resident_can_wear_every_published_demo_pack(
        tmp_path: Path, monkeypatch, slot: str, persona_id: str, version: str) -> None:
    from personality_binding import InstanceDir

    out = _apply(tmp_path, monkeypatch, slot=slot, persona_id=persona_id, version=version)

    assert out["ok"] is True, out
    assert out["restart_required"] is True
    staged = InstanceDir(tmp_path / "bindings" / f"resident-{slot}").desired()
    assert (staged.binding.persona_id, staged.binding.persona_version) == (persona_id, version)


@pytest.mark.parametrize("slot", RESIDENT_SLOTS)
@pytest.mark.parametrize(("persona_id", "version"), [
    ("acme/bruce", "0.1.0"),   # another namespace: the widening is demo/ only
    ("demo/bruce", "1.0.0"),   # the range's ceiling is exclusive
    ("demo/bruce", "0.0.9"),   # and its floor is real
])
def test_a_shipped_resident_still_refuses_anything_outside_the_demo_range(
        tmp_path: Path, monkeypatch, slot: str, persona_id: str, version: str) -> None:
    out = _apply(tmp_path, monkeypatch, slot=slot, persona_id=persona_id, version=version)

    assert out["ok"] is False
    assert out["kind"] == "incompatible"
    assert list((tmp_path / "bindings").rglob("*.yaml")) == []


@pytest.mark.parametrize(("slot", "own", "other"), [
    ("assistant", "casa/ellen", "casa/tina"),
    ("butler", "casa/tina", "casa/gary"),
    ("concierge", "casa/gary", "casa/ellen"),
])
def test_a_shipped_resident_admits_its_own_persona_and_not_a_siblings(
        tmp_path: Path, monkeypatch, slot: str, own: str, other: str) -> None:
    assert _apply(tmp_path, monkeypatch, slot=slot, persona_id=own, version="0.2.0")["ok"] is True
    refused = _apply(tmp_path, monkeypatch, slot=slot, persona_id=other, version="0.2.0")
    assert refused["ok"] is False
    assert refused["kind"] == "incompatible"


def _role_before_the_demo_entry(tmp_path: Path, slot: str):
    """The shipped role as it stood before the demo entry was added."""
    import shutil

    from role_artifact import load_role_artifact
    from role_slot import materialize_role

    copy = tmp_path / "old-role" / slot
    shutil.copytree(ROLES / "resident" / slot, copy)
    role_yaml = copy / "role.yaml"
    text = role_yaml.read_text(encoding="utf-8")
    entry = ', "demo/*@>=0.1.0 <1.0.0"'
    assert entry in text
    role_yaml.write_text(text.replace(entry, ""), encoding="utf-8")
    return materialize_role(source=load_role_artifact(copy), options={})


@pytest.mark.parametrize("override", [False, True], ids=["image-default", "override"])
@pytest.mark.parametrize(("slot", "persona_id"), [
    ("assistant", "casa/ellen"), ("butler", "casa/tina"), ("concierge", "casa/gary"),
])
def test_the_entry_keeps_the_worn_persona_and_starts_conversations_fresh_once(
        tmp_path: Path, monkeypatch, slot: str, persona_id: str, override: bool) -> None:
    """The cost of the role edit, stated by the real reconcile and resume gate:
    what a resident wears is unchanged, its digest moves once, and a conversation
    held under the old binding starts fresh on every channel while one held under
    the new binding resumes."""
    from datetime import datetime, timezone

    from agent import _resume_decision
    from persona_install import apply_persona_override
    from persona_pack import load_persona_pack
    from personality_binding import InstanceDir, reconcile_resident_binding
    from role_artifact import load_role_artifact
    from role_slot import materialize_role

    def _image(ref: str):
        pid, _, version = ref.partition("@")
        root = ROLES.parent / "personas" / pid / version
        return load_persona_pack(root / "pack", root / "manifest.json")

    def _installed(ref: str):
        pid, _, version = ref.partition("@")
        root = tmp_path / "config-root" / "personas" / pid / version
        return load_persona_pack(root / "pack", root / "manifest.json")

    def _reconcile(role):
        return reconcile_resident_binding(
            role=role, image_default_persona_loader=_image,
            override_persona_loader=_installed, instance_dir=instance_dir,
            candidate_validator=lambda persona, binding: None,
        ).binding

    old_role = _role_before_the_demo_entry(tmp_path, slot)
    new_role = materialize_role(source=load_role_artifact(ROLES / "resident" / slot), options={})
    root = tmp_path / "bindings" / f"resident-{slot}"
    instance_dir = InstanceDir(root)
    if override:
        pack = install_persona_for_apply(
            tmp_path, monkeypatch, persona_id=persona_id, version="0.2.0")
        apply_persona_override(
            target_role_id=new_role.role_id, persona=pack, role=old_role,
            instance_dir_root=root, candidate_validator=lambda persona, binding: None,
        )
    before = _reconcile(old_role)
    after = _reconcile(new_role)

    assert (after.persona_id, after.persona_version, after.persona_checksum, after.mode) == (
        before.persona_id, before.persona_version, before.persona_checksum, before.mode)
    assert before.persona_id == persona_id
    assert before.mode == ("override" if override else "image-default")
    assert after.role_checksum != before.role_checksum
    assert after.binding_digest != before.binding_digest

    def _decide(channel: str, digest: str):
        now = datetime.now(timezone.utc)
        entry = {"agent": new_role.role_id, "sdk_session_id": "sid",
                 "last_active": now.isoformat(), "binding_digest": digest}
        return _resume_decision(channel, entry, now, role_id=new_role.role_id,
                                binding_digest=after.binding_digest)

    for channel in ("telegram", "voice"):
        assert _decide(channel, before.binding_digest).reason == "binding_mismatch"
        assert _decide(channel, after.binding_digest).action == "resume"
    assert _reconcile(new_role) == after
