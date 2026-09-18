"""#1029: a resumed SDK session keeps the system prompt it was created with.

The CLI pins a session's system prompt at creation. Casa re-renders a current
prompt on every cold connect and hands it to a resumed session, which does not
take it — so a plugin that declares a new background job never reaches a
conversation that was already open, for the life of that session, silently.

The gate here is the sibling of the existing ``binding_digest`` gate: a session
whose STRUCTURAL prompt surface (``<delegates>``/``<jobs>``/``<executors>``) no
longer matches what Casa would render is not resumed; a fresh one is started and
the old conversation is retained.

Design round 1 (Astra + Terra, both SHIP WITH FIXES) reproduced the one way this
can go wrong: the surface is read at the resume decision and again, after an
await, in ``_build_options`` — a reload landing between them makes the stored
digest describe a prompt the session never had. So the surface is rendered ONCE
per turn and carried; ``test_surface_is_rendered_once_and_carried`` is the pin
for that, and it is the test that must survive a mutation of the carry.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import agent as agent_mod
from agent import _resume_decision

pytestmark = [pytest.mark.unit]

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
BINDING = "sha256:" + "1" * 64


def _stored_entry(**overrides) -> dict:
    base = {
        "agent": "resident:assistant", "sdk_session_id": "sid-1",
        "last_active": datetime.now(timezone.utc).isoformat(),
        "binding_digest": BINDING,
        "prompt_surface_digest": DIGEST_A,
        "speaker_provenance": {
            "speaker_kind": "resident", "role_id": "resident:assistant",
            "persona_id": "casa/ellen", "persona_version": "0.1.0",
            "display_name": "Ellen", "binding_digest": BINDING,
            "user_peer": None, "user_id": None,
        },
        "user_provenance": {
            "speaker_kind": "user", "role_id": None, "persona_id": None,
            "persona_version": None, "display_name": None,
            "binding_digest": None, "user_peer": "telegram_1", "user_id": "1",
        },
    }
    base.update(overrides)
    return base


def _decide(entry, digest):
    return _resume_decision(
        "telegram", entry, datetime.now(timezone.utc),
        role_id="resident:assistant", binding_digest=BINDING,
        prompt_surface_digest=digest,
    )


def test_matching_surface_still_resumes() -> None:
    """The gate must not cost continuity when nothing structural changed."""
    decision = _decide(_stored_entry(), DIGEST_A)
    assert decision.action == "resume"
    assert decision.resume_sid == "sid-1"
    assert decision.reason == "fresh"


def test_changed_surface_starts_a_fresh_session_and_retains_the_old() -> None:
    """A newly declared job changes the surface: the pinned session must go."""
    decision = _decide(_stored_entry(), DIGEST_B)
    assert decision.action == "new"
    assert decision.resume_sid is None
    assert decision.reason == "prompt_surface_changed"
    # Retained, not dropped — the operator's prior conversation is saved.
    assert decision.retain_old is True
    assert decision.old is not None
    assert decision.old.sdk_session_id == "sid-1"


def test_entry_written_before_this_gate_is_a_mismatch_not_consent() -> None:
    """Absence is not consent. An entry with no stored digest predates the
    gate and may be carrying exactly the stale prompt this fixes, so it is
    retired once — which is what makes an already-stuck conversation recover
    on deploy rather than needing a hand-edited session file."""
    entry = _stored_entry()
    del entry["prompt_surface_digest"]
    decision = _decide(entry, DIGEST_A)
    assert decision.action == "new"
    assert decision.reason == "prompt_surface_changed"
    assert decision.retain_old is True


def test_binding_gate_is_checked_before_the_surface_gate() -> None:
    """Ordering pin: a binding mismatch must keep reporting itself, so the
    new reason cannot mask an identity change in the logs."""
    decision = _resume_decision(
        "telegram", _stored_entry(), datetime.now(timezone.utc),
        role_id="resident:assistant", binding_digest="sha256:" + "9" * 64,
        prompt_surface_digest=DIGEST_B,
    )
    assert decision.reason == "binding_mismatch"


class _Delegate:
    def __init__(self, agent_name: str) -> None:
        self.agent = agent_name
        self.purpose = "money"
        self.when = "ask about money"


def test_digest_moves_when_a_declared_job_appears(monkeypatch) -> None:
    """The digest must actually track the thing that broke: a job declaration
    appearing for a delegate that was already present under the same name."""
    delegates = [_Delegate("finance")]
    names = {"finance": "Alex"}
    allowed = ["mcp__casa-framework__start_job"]

    # NB: ``_render_jobs_block`` imports ``startable_jobs`` from
    # ``background_jobs`` inside the function, so the patch has to land on that
    # module — patching it on ``agent`` reaches nothing and leaves the jobs
    # block empty in BOTH renders, which would pin nothing at all.
    import background_jobs

    monkeypatch.setattr(agent_mod, "_live_agent_directory", lambda: names)
    monkeypatch.setattr(background_jobs, "startable_jobs", lambda roles: [])

    before = agent_mod._render_prompt_surface(
        delegates, None, live_names=names, allowed_tools=allowed,
    )

    class _Decl:
        qualified_name = "tx-classifier:classify-transactions"
        title = "Classify transactions"
        summary = "Tag untagged transactions in batches"

    monkeypatch.setattr(
        background_jobs, "startable_jobs",
        lambda roles: [("finance", _Decl())],
    )
    after = agent_mod._render_prompt_surface(
        delegates, None, live_names=names, allowed_tools=allowed,
    )

    assert before.digest != after.digest, (
        "a new job declaration must move the digest, or the gate never fires"
    )
    assert "classify-transactions" in after.jobs
    assert "classify-transactions" not in before.jobs
    # Stable for identical inputs — otherwise every turn retires the session.
    again = agent_mod._render_prompt_surface(
        delegates, None, live_names=names, allowed_tools=allowed,
    )
    assert again.digest == after.digest


@pytest.mark.asyncio
async def test_build_options_splices_the_armed_surface_without_re_rendering(
    tmp_path,
) -> None:
    """The design-round finding, pinned at the PRODUCTION call site.

    ``_build_options`` must splice the surface the turn already rendered — the
    one the resume decision gated on and the one the registration stores — and
    must NOT render a second time. A second render is reachable: it happens
    after the awaited memory load, and both design reviewers reproduced a
    reload landing in that window and changing the result, which would store a
    digest describing a prompt the session never had.

    Pinning "the blocks came out the other side" alone is not enough — a
    re-render of an unchanged surface produces identical text and would pass.
    So this also asserts the render count, which is what a broken carry moves.
    """
    from agent import Agent
    from agent_registry import AgentRegistry
    from channels import ChannelManager
    from config import (
        AgentConfig, CharacterConfig, MemoryConfig, ToolsConfig,
    )
    from mcp_registry import McpServerRegistry
    from session_registry import SessionRegistry

    try:
        from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
    except ImportError:
        from role_artifact_stub import STUB_ROLE_ARTIFACT
    try:
        from tests.session_reg_helpers import RESIDENT_DIGEST, resident_prov
        from tests.session_reg_helpers import resident_role_id
    except ImportError:
        from session_reg_helpers import RESIDENT_DIGEST, resident_prov
        from session_reg_helpers import resident_role_id

    class _FakeMemory:
        async def profile(self, *a, **kw):
            return None

        async def recall(self, *a, **kw):
            return []

        def __getattr__(self, _name):
            async def _noop(*a, **kw):
                return None
            return _noop

    cfg = AgentConfig(
        role_artifact=STUB_ROLE_ARTIFACT, role="assistant",
        model="claude-sonnet-4-6", system_prompt="You are helpful.",
        character=CharacterConfig(name="Test"),
        tools=ToolsConfig(allowed=["Read"], permission_mode="acceptEdits"),
        memory=MemoryConfig(token_budget=1000, read_strategy="per_turn"),
        role_id=resident_role_id("assistant"), kind="resident",
        binding_digest=RESIDENT_DIGEST,
        speaker_provenance=resident_prov("assistant"),
    )
    agent = Agent(
        config=cfg,
        session_registry=SessionRegistry(str(tmp_path / "sessions.json")),
        mcp_registry=McpServerRegistry(),
        channel_manager=ChannelManager(),
        semantic_memory=_FakeMemory(),
    )

    sentinel = agent_mod.PromptSurface(
        delegates="<delegates>\n- SENTINEL-DELEGATE\n</delegates>",
        jobs="<jobs>\n- SENTINEL-JOB\n</jobs>",
        executors="<executors>\n- SENTINEL-EXECUTOR\n</executors>",
        digest="sha256:" + "c" * 64,
    )

    renders = {"n": 0}
    real = agent_mod._render_prompt_surface

    def _counting(*args, **kwargs):
        renders["n"] += 1
        return real(*args, **kwargs)

    monkeypatch_token = agent_mod._prompt_surface_var.set(sentinel)
    original = agent_mod._render_prompt_surface
    agent_mod._render_prompt_surface = _counting
    try:
        opts = await agent._build_options(
            channel="telegram", channel_key="k", is_fresh=True,
            resume_sid=None, user_text="hi",
        )
    finally:
        agent_mod._render_prompt_surface = original
        agent_mod._prompt_surface_var.reset(monkeypatch_token)

    prompt = opts.system_prompt or ""
    assert "SENTINEL-DELEGATE" in prompt
    assert "SENTINEL-JOB" in prompt
    assert "SENTINEL-EXECUTOR" in prompt
    assert renders["n"] == 0, (
        "_build_options re-rendered the surface instead of splicing the armed "
        "one — the drift both design reviewers reproduced is back"
    )


@pytest.mark.asyncio
async def test_carrier_falls_back_to_a_fresh_render_when_unarmed() -> None:
    """A direct call with no carrier armed renders once, consistently."""
    renders = {"n": 0}
    real = agent_mod._render_prompt_surface

    def _counting(*args, **kwargs):
        renders["n"] += 1
        return real(*args, **kwargs)

    original = agent_mod._render_prompt_surface
    agent_mod._render_prompt_surface = _counting
    try:
        fresh = agent_mod._carried_prompt_surface(
            [], None, live_names={}, allowed_tools=[],
        )
    finally:
        agent_mod._render_prompt_surface = original
    assert renders["n"] == 1
    assert fresh.digest == real([], None, live_names={}, allowed_tools=[]).digest
