"""#945 red case — `resident_persona_reset` resolves the in-image default
persona from the IMAGE-DEFAULTS root alone, the root boot actually reads.

DECLARED (D34), not pinned from the base. Nothing at `0b31b835` obliged the
reset to resolve anywhere in particular; what the cited bytes below establish is
the CONSEQUENCE that makes the image root the only correct answer, and the
declaration is scoped to exactly what a one-call-site change can guarantee.

**The cited evidence, all at `0b31b835efc3770dc9014cede01fd188f04e2c94`.**
`agent_loader` binds `personas_root` to the module-relative image-defaults tree
(`agent_loader.py:1530`); `_load_default` probes that root and no other
(`agent_loader.py:1561-1570`); it is wired as `image_default_persona_loader=`
into boot's reconcile (`agent_loader.py:1615-1620`); and reconcile's
non-override arm re-materialises the candidate from that loader, ignoring the
staged tuple's persona bytes entirely (`personality_binding.py:1388-1394`). So
for a non-override selection the pack a resident actually serves comes from the
image root — always, whatever is staged.

**The declaration, scoped.** For a loaded resident whose slot's in-image default
pack is present, valid and role-compatible, `resident_persona_reset` validates
and stages the IMAGE pack for that reference: the pack it resolves is
byte-identical to the one boot's non-override arm will re-materialise from the
same image tree and the same role, and a pack installed at that same reference
under `$CASA_CONFIG_DIR/personas` — readable or not — neither changes what the
reset resolves nor makes it refuse.

Scope stated explicitly, on Astra's specification: this asserts equality with
boot's selection *for the image contents and role as they stand*, not across an
intervening image upgrade or role change, and it does NOT make the reset
universally available — a reset whose own image-default pack is absent,
invalid or role-incompatible still refuses, by the same contract as before.
It asserts nothing about `resident_persona_swap` or `persona_apply`, whose
caller-supplied refs keep resolving installed-first, and nothing about the
restart notice beyond its continuing to arrive whole.

RED at the base for the intended reason, both arms measured in-worktree at
`0b31b835` (2 passed, scratch file since removed):
  - readable shadow: the tool validated and staged the SHADOW's identity
    (`sha256:e74fe229bf41411a…`) where the image pack is
    `sha256:15b91873c08b1b70…`;
  - invalid shadow: the tool performed ZERO validations and ZERO staging
    writes, returning `{"ok": false, "kind": "incompatible_or_missing_persona",
    "detail": "persona pack file set is invalid"}` while the image default
    loaded perfectly well.
Neither is an import, filesystem or socket failure.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_persona_install import install_persona_for_apply

CASA = Path(__file__).resolve().parent.parent / "casa" / "rootfs" / "opt" / "casa"
ASSISTANT_ROLE_DIR = CASA / "defaults" / "roles" / "resident" / "assistant"


def _assistant_role():
    from role_artifact import load_role_artifact
    from role_slot import materialize_role

    return materialize_role(source=load_role_artifact(ASSISTANT_ROLE_DIR), options={})


def _image_pack(ref: str):
    """Load the image-default pack through the root `agent_loader` itself binds
    — derived from `agent_loader.SCHEMA_DIR`, never a literal path, so this test
    reads the same tree boot does."""
    import agent_loader
    from persona_pack import load_persona_pack

    root = Path(agent_loader.SCHEMA_DIR).parent / "personas"
    persona_id, _, version = ref.partition("@")
    return load_persona_pack(root / persona_id / version / "pack",
                             root / persona_id / version / "manifest.json")


@pytest.fixture
def resident(tmp_path, monkeypatch):
    """A loaded `resident:assistant` with a private bindings root and a private
    `$CASA_CONFIG_DIR`, and NOTHING installed under it yet — each case installs
    its own shadow at the image-default ref."""
    import agent as agent_mod

    monkeypatch.setenv("CASA_BINDINGS_DIR", str(tmp_path / "bindings-root"))
    monkeypatch.setenv("CASA_CONFIG_DIR", str(tmp_path / "config-root"))
    role = _assistant_role()
    monkeypatch.setattr(
        agent_mod, "active_runtime",
        SimpleNamespace(role_slots={"resident:assistant": role}), raising=False)
    return SimpleNamespace(role=role, tmp_path=tmp_path)


def _instrument(monkeypatch) -> tuple[list, list]:
    """Wrap — never replace — the two seams whose INPUT is the defect.

    `check_persona_requirements` and `InstanceDir.stage_desired` both keep their
    real behaviour; the wrappers only record what they were handed, so the
    assertions can be counts and identities rather than a status. The reset
    imports `check_persona_requirements` inside its own body, so the module
    attribute is what it resolves at call time."""
    import personality_binding as pb

    validated: list = []
    staged: list = []

    real_check = pb.check_persona_requirements
    real_stage = pb.InstanceDir.stage_desired

    def _check(role_normalized, persona):
        validated.append(persona)
        return real_check(role_normalized, persona)

    def _stage(self, tuple_):
        staged.append(tuple_)
        return real_stage(self, tuple_)

    monkeypatch.setattr(pb, "check_persona_requirements", _check)
    monkeypatch.setattr(pb.InstanceDir, "stage_desired", _stage)
    return validated, staged


async def _reset() -> dict:
    from tools import resident_persona_reset

    result = await resident_persona_reset.handler({"role": "resident:assistant"})
    return json.loads(result["content"][0]["text"])


def _staged_on_disk():
    """Re-read the staged tuple from the bindings root the tool actually wrote
    to, through the same seam the tool resolves it with."""
    import agent_loader
    from personality_binding import InstanceDir

    return InstanceDir(
        agent_loader._resident_bindings_root(None) / "resident-assistant").desired()


def _assert_image_binding(resident, staged_tuple, payload, image_pack, ref) -> None:
    """The shared terminus of both cases: what was staged is the binding boot's
    non-override arm would itself materialise from the image pack."""
    import tools as tools_mod
    from personality_binding import materialize_image_default_binding

    expected = materialize_image_default_binding(
        role=resident.role, persona=image_pack, image_default_root=ref)

    assert staged_tuple.binding.persona_checksum == image_pack.checksum
    assert staged_tuple.binding.binding_digest == expected.binding_digest
    assert staged_tuple.binding.image_default_root == ref
    assert staged_tuple.binding.mode == expected.mode
    assert _staged_on_disk().binding.binding_digest == expected.binding_digest

    # The refusal contract and the envelope are untouched by this change.
    assert payload["ok"] is True
    assert payload["role"] == "resident:assistant"
    assert payload["persona"] == ref
    assert payload["activation"] == "restart_required"
    assert payload["conversation_notice"] == tools_mod.RESIDENT_CONVERSATION_RESET_NOTICE


@pytest.mark.asyncio
async def test_reset_stages_image_pack_despite_readable_shadow(
        resident, monkeypatch) -> None:
    """ARM 1 — a READABLE pack installed at the image-default ref.

    At the base the tool validated and staged the shadow's identity, so the
    `desired.yaml` on disk carried a `binding_digest` no boot would ever
    promote. Counts, not statuses: exactly one validation and exactly one
    staging write, and the pack handed to BOTH is the image pack."""
    from personality_binding import IMAGE_DEFAULT_PERSONA_BY_SLOT

    ref = IMAGE_DEFAULT_PERSONA_BY_SLOT["assistant"]
    persona_id, _, version = ref.partition("@")
    shadow = install_persona_for_apply(
        resident.tmp_path, monkeypatch, persona_id=persona_id, version=version)
    image_pack = _image_pack(ref)
    # The state this case describes only exists if the two genuinely differ.
    assert shadow.checksum != image_pack.checksum

    validated, staged = _instrument(monkeypatch)
    payload = await _reset()

    assert len(validated) == 1
    assert len(staged) == 1
    assert validated[0].checksum == image_pack.checksum
    _assert_image_binding(resident, staged[0], payload, image_pack, ref)


@pytest.mark.asyncio
async def test_reset_stages_image_pack_despite_invalid_shadow(
        resident, monkeypatch) -> None:
    """ARM 2 — an UNREADABLE pack installed at the image-default ref, which is
    the operator-visible defect: the always-available reset REFUSED because of
    the very thing the operator was trying to recover from, while the image
    default it should have fallen back to was perfectly intact.

    The shadow is broken by deleting one admitted file from its pack (the
    manifest then no longer matches the admitted file set), leaving `pack/` and
    `manifest.json` in place — the directory still ANSWERS for the ref, which is
    what makes the installed-first resolver load it and fail. No permission bit
    is touched: the candidate gate materializes this tree read-only, and a mode
    experiment there would measure the materialization rather than the tool."""
    from persona_pack import PersonaPackError, load_persona_pack
    from personality_binding import IMAGE_DEFAULT_PERSONA_BY_SLOT

    ref = IMAGE_DEFAULT_PERSONA_BY_SLOT["assistant"]
    persona_id, _, version = ref.partition("@")
    install_persona_for_apply(
        resident.tmp_path, monkeypatch, persona_id=persona_id, version=version)
    shadow_dir = (resident.tmp_path / "config-root" / "personas"
                  / persona_id / version)
    (shadow_dir / "pack" / "persona.md").unlink()

    # The premise, measured here rather than assumed: the shadow does not load,
    # and the image default does.
    with pytest.raises(PersonaPackError):
        load_persona_pack(shadow_dir / "pack", shadow_dir / "manifest.json")
    image_pack = _image_pack(ref)

    validated, staged = _instrument(monkeypatch)
    payload = await _reset()

    assert len(validated) == 1
    assert len(staged) == 1
    assert validated[0].checksum == image_pack.checksum
    _assert_image_binding(resident, staged[0], payload, image_pack, ref)
