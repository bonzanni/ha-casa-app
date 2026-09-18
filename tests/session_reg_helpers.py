"""Task 9: shared stub provenances for tests that call
``SessionRegistry.register`` but do not themselves exercise provenance content.

``register`` gained REQUIRED keyword-only ``binding_digest``/
``speaker_provenance``/``user_provenance`` params (an unset provenance is the
bug the personality plan closes — there is no back-compat default). Tests whose
subject is elsewhere (sweeper, reaper, reset hooks, save orchestration) pass
these honest stand-ins: an ``assistant`` resident identity and an anonymous
telegram user."""
from __future__ import annotations

from personality_types import SpeakerProvenance

# A valid resident executing-identity (mirrors the assistant resident used by
# most fixtures). Provenance validation requires persona_id/version/digest for
# a resident, so this is a fully-formed stand-in.
STUB_BINDING_DIGEST = "sha256:" + "a" * 64
STUB_SPEAKER_PROV = SpeakerProvenance(
    speaker_kind="resident",
    role_id="resident:assistant",
    persona_id="casa/tester",
    persona_version="0.1.0",
    display_name="Tester",
    binding_digest=STUB_BINDING_DIGEST,
)
# A valid anonymous user identity.
STUB_USER_PROV = SpeakerProvenance(speaker_kind="user", user_peer="tester")

# --- Per-role resident identity (for _make_agent-driven resume tests) --------
# A single fixed binding digest every synthetic resident config shares, so a
# seed entry written under a role's canonical id resumes against that config.
RESIDENT_DIGEST = "sha256:" + "b" * 64


def resident_role_id(slot: str) -> str:
    return f"resident:{slot}"


def resident_prov(slot: str) -> SpeakerProvenance:
    """A valid resident executing-identity for a role slot (e.g. ``butler``)."""
    return SpeakerProvenance(
        speaker_kind="resident",
        role_id=f"resident:{slot}",
        persona_id=f"casa/{slot}",
        persona_version="0.1.0",
        display_name=slot.capitalize(),
        binding_digest=RESIDENT_DIGEST,
    )


# --- #1029: the structural prompt-surface gate -------------------------------
def align_prompt_surface(registry, agent, channel: str, scope_id) -> str:
    """Stamp the seeded entry for ``(channel, agent.role, scope_id)`` with the
    prompt-surface digest THIS agent renders, and return it.

    ``_resume_decision`` retires a session whose stored structural surface
    (``<delegates>``/``<jobs>``/``<executors>``) no longer matches what would be
    rendered now, because the CLI pins a session's system prompt at creation and
    a resumed session cannot be corrected in place. An entry that never recorded
    a digest — every entry written before the gate, and every bare
    ``register(...)`` seed in these tests — is a MISMATCH by design: we cannot
    certify a prompt we never observed, and recording the current digest for it
    would assert a match that does not exist.

    So a test whose subject is resume behaviour (fault streaks, stale-resume
    recovery, two-pool authority) has to seed a surface as well as a session.
    Tests that assert a FRESH session must NOT call this — their unaligned seed
    is doing real work."""
    from agent import _live_agent_directory, _render_prompt_surface
    from session_registry import build_scoped_session_key

    digest = _render_prompt_surface(
        agent.config.delegates, agent._agent_registry,
        live_names=_live_agent_directory(),
        allowed_tools=agent.config.tools.allowed,
        executors=agent.config.executors,
    ).digest
    key = build_scoped_session_key(channel, agent.config.role, scope_id)
    entry = registry._data.get(key)
    if entry is not None:
        entry["prompt_surface_digest"] = digest
    return digest
