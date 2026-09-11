"""#931 red case — every resident persona STAGING entry point tells the
operator what the promoting restart costs (INV-PERS-018).

A resident's persona identity is an input of its `binding_digest`
(`personality_binding.compute_binding_digest`), and that digest is a resume
precondition: `agent._resume_decision` refuses a stored session whose digest
differs from the freshly loaded resident's, returning
`("new", retain_old=True, "binding_mismatch")` — after the role check and
BEFORE the freshness window, so the refusal is channel-agnostic
(`agent.py:519-520@ce4c4b53`). Telegram transcripts survive as recall because
`channel_policy._WRITABLE_CHANNELS` is telegram-only
(`channel_policy.py:23@ce4c4b53`) and `session_saver.save_session` returns
False without persisting for anything else
(`session_saver.py:190-191@ce4c4b53`). So the restart that promotes a staged
persona binding starts every one of that resident's conversations fresh, and a
voice conversation is lost outright rather than demoted to recall.

Staging is the LAST moment the operator can be told before ordering the
restart. This module pins that the telling exists on all seven surfaces the
operator can reach: the three staging tools' results, the three staging tools'
registered descriptions, and the apply recipe's step that orders the restart.

DECLARED (D34), not pinned from the base: nothing at `ce4c4b53` obliged any of
these surfaces to say this. The cited bytes above establish that the
consequence the notice NAMES is real; that the operator is TOLD is what this
change establishes, and the invariant is scoped to exactly those seven
surfaces. It asserts nothing about the resume decision (unchanged), nothing
about retention (voice stays non-bank-writable) and nothing about specialists,
which activate on `casa_reload` rather than a restart.

RED at the base for the intended reason: each surface exists and is reachable,
and each omits the notice — 7/7 surfaces, 28/28 clauses absent. Not an import,
filesystem or socket failure.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_persona_install import install_persona_for_apply

CASA = Path(__file__).resolve().parent.parent / "casa" / "rootfs" / "opt" / "casa"
ASSISTANT_ROLE_DIR = CASA / "defaults" / "roles" / "resident" / "assistant"
APPLY_RECIPE = (
    CASA / "defaults" / "agents" / "executors" / "configurator" / "doctrine"
    / "recipes" / "persona" / "apply.md"
)

# The four clauses the notice must carry, on every surface. Specified by Astra
# (`rounds-L/redcase-specify-astra.md`); asserted per clause per surface so one
# missing clause cannot hide behind another.
NOTICE_CLAUSES = (
    "on the restart that promotes this binding",
    "every conversation of this resident",
    "starts fresh on every channel",
    "voice history is not carried",
)


def assert_restart_notice(text: str, surface: str) -> None:
    """Every clause, counted — never a substring `in` on the whole notice."""
    normalized = " ".join((text or "").lower().split())
    present = [clause for clause in NOTICE_CLAUSES if clause in normalized]
    assert len(present) == len(NOTICE_CLAUSES), (
        f"{surface}: {len(NOTICE_CLAUSES) - len(present)} of "
        f"{len(NOTICE_CLAUSES)} notice clauses absent — missing "
        f"{[c for c in NOTICE_CLAUSES if c not in present]}"
    )


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def _assistant_role():
    from role_artifact import load_role_artifact
    from role_slot import materialize_role

    return materialize_role(source=load_role_artifact(ASSISTANT_ROLE_DIR), options={})


@pytest.fixture
def resident(tmp_path, monkeypatch):
    """A loaded `resident:assistant` with a private bindings root and an
    installed, role-compatible override pack — the arrangement all three
    staging entry points require in production.

    `casa/ellen@0.2.0` satisfies the assistant role artifact's declared
    compatibility (`casa/ellen@>=0.1.0 <1.0.0`) and differs from the image
    default `casa/ellen@0.1.0` that `resident_persona_reset` restores, so each
    of the three calls below stages a candidate that is not already active.
    """
    import agent as agent_mod

    monkeypatch.setenv("CASA_BINDINGS_DIR", str(tmp_path / "bindings-root"))
    pack = install_persona_for_apply(
        tmp_path, monkeypatch, persona_id="casa/ellen", version="0.2.0")
    role = _assistant_role()
    monkeypatch.setattr(
        agent_mod, "active_runtime",
        SimpleNamespace(role_slots={"resident:assistant": role}), raising=False)
    return SimpleNamespace(role=role, pack=pack)


def _count_stage_desired(monkeypatch) -> list[int]:
    """Wrap the real `InstanceDir.stage_desired` so the swap/reset cases can
    assert EXACTLY ONE staging write, not merely a truthy result."""
    from personality_binding import InstanceDir

    calls: list[int] = []
    original = InstanceDir.stage_desired

    def _counted(self, tuple_):
        calls.append(1)
        return original(self, tuple_)

    monkeypatch.setattr(InstanceDir, "stage_desired", _counted)
    return calls


# --- the three RESULT surfaces -------------------------------------------


@pytest.mark.asyncio
async def test_persona_apply_result_tells_the_resident_conversation_cost(
        resident, tmp_path, monkeypatch) -> None:
    """Surface 1/7 — `persona_apply`'s resident arm. The operator reaches this
    result through the configurator's apply recipe."""
    import persona_install
    from tools import persona_apply

    staged_calls: list[dict] = []

    def _capture(*, target_role_id, persona, role, instance_dir_root,
                 candidate_validator):
        # The envelope, not the staging mechanics, is what this pins — the
        # same capture seam `test_persona_apply_resident_branch_honors_
        # casa_bindings_dir` uses, so no compile proof runs here.
        staged_calls.append({"target_role_id": target_role_id})

        class _Committed:
            class binding:
                binding_digest = "sha256:" + "a" * 64

        return _Committed()

    monkeypatch.setattr(persona_install, "apply_persona_override", _capture)

    payload = _payload(await persona_apply.handler({
        "target_role_id": "resident:assistant",
        "persona_id": "casa/ellen", "persona_version": "0.2.0",
    }))

    assert len(staged_calls) == 1
    # The pre-existing envelope is intact: this key is ADDITIVE.
    assert payload["ok"] is True
    assert payload["target_role_id"] == "resident:assistant"
    assert payload["binding_digest"] == "sha256:" + "a" * 64
    assert payload["restart_required"] is True
    assert_restart_notice(
        payload.get("conversation_notice", ""), "persona_apply result")


def test_resident_persona_swap_result_tells_the_resident_conversation_cost(
        resident, monkeypatch) -> None:
    """Surface 2/7 — `resident_persona_swap`. It is routed through NO recipe
    (`configurator/prompt.md:36` indexes install/apply/remove/prune only), so
    its own result and description are the only things that can tell."""
    from tools import resident_persona_swap

    calls = _count_stage_desired(monkeypatch)
    payload = _payload(asyncio.run(resident_persona_swap.handler({
        "role": "resident:assistant", "persona_ref": "casa/ellen@0.2.0",
    })))

    assert len(calls) == 1
    assert payload["ok"] is True
    assert payload["role"] == "resident:assistant"
    assert payload["persona"] == "casa/ellen@0.2.0"
    assert payload["activation"] == "restart_required"
    assert_restart_notice(
        payload.get("conversation_notice", ""), "resident_persona_swap result")


def test_resident_persona_reset_result_tells_the_resident_conversation_cost(
        resident, monkeypatch) -> None:
    """Surface 3/7 — `resident_persona_reset`, the other un-recipe'd entry
    point. Reset moves the digest exactly as an apply does, so it costs the
    resident's conversations exactly as much."""
    from tools import resident_persona_reset

    calls = _count_stage_desired(monkeypatch)
    payload = _payload(asyncio.run(resident_persona_reset.handler({
        "role": "resident:assistant",
    })))

    assert len(calls) == 1
    assert payload["ok"] is True
    assert payload["role"] == "resident:assistant"
    assert payload["persona"] == "casa/ellen@0.1.0"
    assert payload["activation"] == "restart_required"
    assert_restart_notice(
        payload.get("conversation_notice", ""), "resident_persona_reset result")


# --- the three DESCRIPTION surfaces --------------------------------------


@pytest.mark.parametrize("tool_name", [
    "persona_apply", "resident_persona_swap", "resident_persona_reset",
])
def test_staging_tool_description_tells_the_resident_conversation_cost(
        tool_name: str) -> None:
    """Surfaces 4-6/7 — the registered descriptions. The description is what
    the configurator model reads BEFORE it calls anything, so it is the only
    surface that can make the model warn the operator rather than report a
    cost already incurred."""
    import tools

    assert_restart_notice(
        getattr(tools, tool_name).description, f"{tool_name} description")


# --- the RECIPE surface ---------------------------------------------------


def test_apply_recipe_step_five_tells_the_resident_conversation_cost() -> None:
    """Surface 7/7 — the apply recipe's resident step, which is the step that
    orders `casa_restart_supervised`. The notice must sit WITH that order, not
    merely somewhere in the file: a sentence the model reads after it has
    already told the operator "it takes effect on the next restart" is too
    late to be a warning.

    The structure `tests/test_specialist_rollback_persona_override.py` splits
    on is asserted here too, so growing step 5 cannot silently break it.
    """
    text = APPLY_RECIPE.read_text()
    assert text.count("\n6. ") == 1
    assert text.count("\n## Common mistakes") == 1

    step_5 = text.split("\n5. ", 1)[1].split("\n6. ", 1)[0]
    assert "casa_restart_supervised" in step_5
    assert_restart_notice(step_5, "recipes/persona/apply.md step 5")


# --- the no-op stage: nothing is promoted, so nothing is claimed ----------
#
# Diff-review round 1 (Terra, S2) against the fix above: the notice was
# unconditional, and a stage whose candidate EQUALS the already-active binding
# is DISCARDED by boot reconciliation rather than promoted
# (`personality_binding` returns the active tuple untouched when
# `active.binding.binding_digest == candidate_binding.binding_digest`). The
# digest never moves, the session resumes, and the notice was a confidently
# worded FALSE warning on a reachable path.
#
# These are regression tests in this change's diff, NOT red cases: at the base
# `conversation_notice` does not exist at all, so `.get(...)` is None and both
# would pass there for the wrong reason. They pin the fix, and they are
# reviewed like any other test rather than carrying a red-case receipt.


def test_a_no_op_reset_claims_nothing_because_nothing_is_promoted(
        resident, monkeypatch) -> None:
    """The resident is ALREADY active on the binding the reset restores, so
    reconciliation discards the staged candidate and the conversations survive.
    The result must not warn about a loss that will not happen."""
    from personality_binding import (
        InstanceDir, materialize_image_default_binding, make_instance_tuple)
    from tools import resident_persona_reset
    import agent_loader
    import tools as tools_mod

    # Make the image default the ACTIVE binding, exactly as a boot reconcile
    # would leave it — commit, do not merely stage.
    persona = tools_mod._resolve_local_persona("casa/ellen@0.1.0")
    binding = materialize_image_default_binding(
        role=resident.role, persona=persona, image_default_root="casa/ellen@0.1.0")
    instance_dir = InstanceDir(
        agent_loader._resident_bindings_root(None) / "resident-assistant")
    instance_dir.stage_desired(
        make_instance_tuple(root="casa/ellen@0.1.0", binding=binding, config_snapshot={}))
    instance_dir.commit_desired_to_active()
    active = instance_dir.active()
    assert active is not None
    assert active.binding.binding_digest == binding.binding_digest

    payload = _payload(asyncio.run(resident_persona_reset.handler({
        "role": "resident:assistant",
    })))

    assert payload["ok"] is True
    # The staged candidate equals the active binding, so reconciliation will
    # discard it: no digest movement, no reset, and nothing to warn about.
    assert payload["conversation_notice"] is None


def test_a_reset_that_does_move_the_binding_still_tells(
        resident, monkeypatch) -> None:
    """The mutation guard for the case above: with a DIFFERENT active binding,
    the same call must still carry the notice. Without this, returning None
    unconditionally would pass the no-op test."""
    from personality_binding import (
        InstanceDir, materialize_override_binding, make_instance_tuple)
    from tools import resident_persona_reset
    import agent_loader

    binding = materialize_override_binding(
        role=resident.role, persona=resident.pack,
        override_source="operator:casa/ellen@0.2.0")
    instance_dir = InstanceDir(
        agent_loader._resident_bindings_root(None) / "resident-assistant")
    instance_dir.stage_desired(
        make_instance_tuple(root="operator:casa/ellen@0.2.0", binding=binding,
                            config_snapshot={}))
    instance_dir.commit_desired_to_active()

    payload = _payload(asyncio.run(resident_persona_reset.handler({
        "role": "resident:assistant",
    })))

    assert payload["ok"] is True
    assert_restart_notice(
        payload.get("conversation_notice", ""),
        "resident_persona_reset result (binding actually moves)")
