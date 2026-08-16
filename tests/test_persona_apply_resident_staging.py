"""#607: `persona_apply` must not make a resident binding active, and must not
write anything at all for a persona that cannot compile.

The shipped behaviour these pin against: `apply_persona_override`'s resident
branch staged AND committed in one step, with only a presence check between
them. A persona that is schema-valid and namespace-compatible but exceeds a
compile admission ceiling therefore became the ACTIVE binding, and every
subsequent boot died on it — while the tool returned
`{ok: true, restart_required: true}`, which the recipe relays to the operator as
"staged, takes effect on the next restart". The #339 guard that exists for
exactly this cannot fire, because `reconcile_resident_binding` returns early
when the candidate equals the already-active binding, and `persona_apply` had
just made the bad binding the active one.

Two properties, pinned separately (a combined assertion would let either half
carry the other):

1. a candidate that fails the compile proof leaves the InstanceDir untouched —
   no `desired.yaml`, no `active.yaml`, no `desired.error.yaml`;
2. a candidate that passes is STAGED and not promoted, so `restart_required`
   is true rather than a description of something that already happened.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from test_persona_install import install_persona_for_apply

ROLE_DIR = (
    Path(__file__).resolve().parent.parent
    / "casa/rootfs/opt/casa/defaults/roles/resident/assistant"
)


def _assistant_role():
    from role_artifact import load_role_artifact
    from role_slot import materialize_role

    return materialize_role(source=load_role_artifact(ROLE_DIR), options={})


def _apply(tmp_path, monkeypatch, *, validator):
    """Apply casa/ellen@0.1.0 to resident:assistant, returning the InstanceDir."""
    from persona_install import apply_persona_override
    from personality_binding import InstanceDir

    role = _assistant_role()
    persona = install_persona_for_apply(
        tmp_path, monkeypatch, persona_id="casa/ellen", version="0.1.0")
    root = tmp_path / "bindings" / "resident-assistant"
    apply_persona_override(
        target_role_id="resident:assistant", persona=persona, role=role,
        instance_dir_root=root, candidate_validator=validator,
    )
    return InstanceDir(root)


def test_resident_apply_stages_and_does_not_promote(tmp_path: Path, monkeypatch) -> None:
    """The staged tuple is the override; `active.yaml` is never written, so the
    running resident keeps its binding until the restart the tool promises."""
    instance_dir = _apply(tmp_path, monkeypatch, validator=lambda persona, binding: None)

    desired = instance_dir.desired()
    assert desired is not None, "the override must be staged"
    assert desired.binding.mode == "override"
    assert desired.binding.persona_id == "casa/ellen"
    assert instance_dir.active() is None, (
        "persona_apply must not promote a resident binding — the tool reports "
        "restart_required=true and the recipe tells the operator it is staged"
    )


def test_resident_apply_returns_the_staged_tuple(tmp_path: Path, monkeypatch) -> None:
    """`tools.persona_apply` reads `binding.binding_digest` off the return
    value; stage-only must still hand back the tuple it staged."""
    from persona_install import apply_persona_override

    role = _assistant_role()
    persona = install_persona_for_apply(
        tmp_path, monkeypatch, persona_id="casa/ellen", version="0.1.0")
    root = tmp_path / "bindings" / "resident-assistant"

    staged = apply_persona_override(
        target_role_id="resident:assistant", persona=persona, role=role,
        instance_dir_root=root, candidate_validator=lambda p, b: None,
    )
    assert staged.binding.binding_digest


def test_resident_apply_writes_nothing_when_the_candidate_cannot_compile(
    tmp_path: Path, monkeypatch,
) -> None:
    """The reported defect. A validator that raises stands in for the admission
    ceiling `compile_prompt_bundle` enforces; nothing may reach disk."""
    from persona_install import apply_persona_override

    role = _assistant_role()
    persona = install_persona_for_apply(
        tmp_path, monkeypatch, persona_id="casa/ellen", version="0.1.0")
    root = tmp_path / "bindings" / "resident-assistant"

    def _refuse(persona, binding):
        raise ValueError(
            "voice prompt exceeds admission ceiling for role resident:assistant")

    with pytest.raises(ValueError, match="admission ceiling"):
        apply_persona_override(
            target_role_id="resident:assistant", persona=persona, role=role,
            instance_dir_root=root, candidate_validator=_refuse,
        )

    # Not "no active" — NOTHING. A staged tuple would be promoted by the next
    # reconcile, and a desired.error.yaml would claim an attempt was recorded.
    for name in ("desired.yaml", "active.yaml", "active.prior.yaml",
                 "desired.error.yaml"):
        assert not (root / name).exists(), f"{name} must not be written"


def test_resident_apply_requires_a_candidate_validator(tmp_path: Path, monkeypatch) -> None:
    """The proof is a REQUIRED argument, not a defaulted one.

    A validator that defaults to "no check" is off for exactly the caller that
    forgets to pass it — the failure mode this issue is about. Making it
    required means a new call site cannot silently skip the compile proof.
    """
    from persona_install import apply_persona_override

    role = _assistant_role()
    persona = install_persona_for_apply(
        tmp_path, monkeypatch, persona_id="casa/ellen", version="0.1.0")

    with pytest.raises(TypeError):
        apply_persona_override(
            target_role_id="resident:assistant", persona=persona, role=role,
            instance_dir_root=tmp_path / "bindings" / "resident-assistant",
        )


class _OversizePersona:
    """A persona whose VOICE projection exceeds the admission ceiling while
    every structural rule still holds — the shape #607 reproduced with
    `casa/tina@0.9.1` (463 voice tokens against the 400 ceiling).

    Built from the schema's own maxima rather than from arbitrary bulk: the
    `# Core` body is capped at 500 characters and a quirk's context and
    tendency at 240 each, so a Core at its limit plus the two quirks the voice
    surface renders is the largest voice body a *valid* pack can produce. It
    measures 485 tokens, which is why the ceiling is reachable at all — a pack
    cannot simply be padded past it.
    """

    persona_id = "casa/ellen"
    version = "0.9.1"
    checksum = "sha256:" + "0" * 64
    identity = {"display_name": "Ellen", "pronouns": {
        "subject": "she", "object": "her", "possessive_adjective": "her",
        "possessive_pronoun": "hers", "reflexive": "herself"}}
    traits = {"warmth": 3, "formality": 3, "candor": 4, "attunement": 3,
              "curiosity": 5, "levity": 2, "social_energy": 3, "optimism": 3}
    relationship_posture = "established"
    markdown = f"# Core\n\n{'Y' * 500}\n\n## Negative space\n\nNever condescends.\n"
    quirks = [{"frequency": "common", "context": "C" * 240, "tendency": "T" * 240},
              {"frequency": "rare", "context": "D" * 240, "tendency": "U" * 240}]
    examples = ()


def test_the_shared_proof_refuses_a_persona_over_the_admission_ceiling() -> None:
    """The red case. A validator mutated to a no-op must fail HERE.

    `agent_loader.make_candidate_compile_validator` is one definition shared by
    boot reconciliation and `persona_apply` — two copies of "does this candidate
    compile" drift, and the copy that drifts is the one that admits a binding
    the loader then rejects. So the factory is driven with a persona that is
    structurally valid and namespace-compatible, and oversized on exactly one
    surface, and must raise.
    """
    import agent_loader
    from prompt_compiler import _LIMITS, _persona_body, estimate_tokens_v1

    role = _assistant_role()
    validator = agent_loader.make_candidate_compile_validator(role)

    # State the premise as a measurement, not an assumption: if a schema change
    # ever made this body fit, the test below would pass for the wrong reason.
    tokens = estimate_tokens_v1(_persona_body(_OversizePersona, "voice"))
    assert tokens > _LIMITS["voice"][0], (
        f"fixture no longer exceeds the voice ceiling ({tokens} <= "
        f"{_LIMITS['voice'][0]}) — this test would pass vacuously")

    from personality_binding import materialize_override_binding

    binding = materialize_override_binding(
        role=role, persona=_OversizePersona,
        override_source="casa/ellen@0.9.1")
    with pytest.raises(ValueError, match="voice prompt exceeds admission ceiling"):
        validator(_OversizePersona, binding)


def test_the_shared_proof_admits_a_persona_that_fits(tmp_path: Path, monkeypatch) -> None:
    """The other half, separately — so the test above cannot be satisfied by a
    validator that simply refuses everything."""
    import agent_loader
    from personality_binding import materialize_override_binding

    role = _assistant_role()
    persona = install_persona_for_apply(
        tmp_path, monkeypatch, persona_id="casa/ellen", version="0.1.0")
    binding = materialize_override_binding(
        role=role, persona=persona, override_source="casa/ellen@0.1.0")

    agent_loader.make_candidate_compile_validator(role)(persona, binding)


def test_an_oversize_persona_is_refused_end_to_end_with_nothing_written(
    tmp_path: Path, monkeypatch,
) -> None:
    """#607's actual reproduction, through the function the tool calls: the
    real compile proof, an oversized persona, and an untouched binding store."""
    import agent_loader
    from persona_install import apply_persona_override

    role = _assistant_role()
    # Resolve the pack the presence check will look for; only the projection
    # is oversized, which is exactly what made this reachable.
    install_persona_for_apply(
        tmp_path, monkeypatch, persona_id="casa/ellen", version="0.1.0")
    root = tmp_path / "bindings" / "resident-assistant"

    with pytest.raises(ValueError, match="admission ceiling"):
        apply_persona_override(
            target_role_id="resident:assistant", persona=_OversizePersona,
            role=role, instance_dir_root=root,
            candidate_validator=agent_loader.make_candidate_compile_validator(role))

    # The strong form: not "no tuple files" but "the directory was never
    # created". `InstanceDir.__init__` does not mkdir, so a refused apply
    # touches nothing at all — an empty-but-present directory would mean some
    # write path ran and is worth failing on.
    assert not root.exists(), (
        "a refused apply must leave the binding store untouched")
