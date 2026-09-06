"""Regression guards on assistant/prompts/system.md content (Phase 5 / E-15).

Plain string-match assertions on the bundled Ellen system prompt. The
prompt isn't a code artifact — it's a YAML-resolved markdown file that
ships with the addon — but the wording is load-bearing for tool-routing
behavior. These tests catch accidental reverts.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest


def _system_md_path() -> Path:
    root = Path(__file__).resolve().parent.parent
    return root / (
        "casa/rootfs/opt/casa/defaults/agents/assistant/prompts/system.md"
    )


def _collapse_ws(text: str) -> str:
    """Collapse whitespace runs (incl. markdown line-wrap newlines) to a
    single space so a VERBATIM prose anchor can be matched regardless of
    where the source file happens to wrap it across lines."""
    return re.sub(r"\s+", " ", text)


@pytest.fixture(scope="module")
def system_md_text() -> str:
    return _system_md_path().read_text(encoding="utf-8")


def test_system_prompt_forbids_llm_arithmetic_for_finance(system_md_text):
    """Phase 6 / E-5: Ellen never performs financial arithmetic
    herself; she always delegates to Alex (finance role) and, when
    delegation fails, declines rather than producing an LLM-computed
    answer. Anchor strings are load-bearing — accidental rewording
    that breaks them is the regression this test catches."""
    text = system_md_text.lower()
    # Anchor 1: explicit "never compute" rule.
    assert "never compute" in text or "never perform arithmetic" in text, (
        "Ellen prompt must explicitly forbid LLM arithmetic for "
        "financial figures — anchor phrase missing."
    )
    # Anchor 2: route to Alex / finance.
    assert "alex" in text and "delegate" in text, (
        "Ellen prompt must point at Alex / delegation as the way to "
        "compute financial figures."
    )
    # Anchor 3: explicit decline behavior on delegation failure.
    assert "without alex" in text or "finance is reachable" in text, (
        "Ellen prompt must teach the user-facing decline shape on "
        "delegation failure — anchor phrase missing."
    )


def test_executors_yaml_lists_only_real_registered_executor_types():
    """F-6 (v0.32.0) doctrine drift guard.

    The 2026-05-02 exploration session caught Ellen narrating
    ``engagement`` as a third executor type because
    ``defaults/agents/assistant/executors.yaml`` listed it as one.
    There is no ``engagement`` executor in the registry —
    interactive-mode delegation to a specialist is a Tier 2 primitive
    (``delegate_to_agent(mode='interactive')``), conceptually different
    from a Tier 3 executor type.

    This test enumerates real executor directories under
    ``defaults/agents/executors/`` and asserts every ``executor_type``
    listed in Ellen's doctrine matches one of them. Catches future
    additions or removals on either side that drift apart.
    """
    import yaml

    # _system_md_path() = .../defaults/agents/assistant/prompts/system.md
    # parents: prompts → assistant → agents
    agents_dir = _system_md_path().parent.parent.parent
    executors_dir = agents_dir / "executors"
    yaml_path = agents_dir / "assistant" / "executors.yaml"

    real_executor_types = sorted(
        p.name for p in executors_dir.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )

    doctrine = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    doctrine_types = sorted(
        entry["executor_type"] for entry in doctrine.get("executors", [])
    )

    drift = set(doctrine_types) - set(real_executor_types)
    assert not drift, (
        f"Ellen's executors.yaml doctrine lists executor_type values "
        f"that don't exist in the registry: {sorted(drift)}. "
        f"Real executor directories: {real_executor_types}. "
        f"Either add the missing executor definition or remove the "
        f"doctrine entry."
    )


def test_system_prompt_teaches_sync_vs_interactive_delegation(system_md_text):
    """F-6 (v0.32.0): the engagement-as-executor doctrine drift was fixed
    by deleting that executors.yaml entry and folding its guidance into
    the system prompt. Ellen still needs to know when to use
    ``delegate_to_agent(mode='interactive')`` vs ``mode='sync'``.

    Anchors a small set of phrases so a future prose rewrite can't
    silently drop the distinction.
    """
    text = system_md_text.lower()
    assert "mode='interactive'" in text, (
        "system prompt must teach interactive-mode delegation."
    )
    assert "mode='sync'" in text, (
        "system prompt must teach sync-mode delegation."
    )
    assert "engagements supergroup" in text, (
        "system prompt must reference the Engagements supergroup as "
        "the destination for interactive delegations."
    )


def test_ellen_brief_doctrine_present(system_md_text):
    """W3/Sol B11 regression guard: Ellen's brief-envelope doctrine must be
    present VERBATIM in both executors.yaml cards + system.md, or a future
    edit could silently revert it back to a bare `task=` string — the exact
    failure mode that produced the invoice_reset mistranslation this
    release fixes (a process instruction like "discuss with me first"
    getting paraphrased into a feature requirement instead of landing in
    ``brief.process_requirements`` verbatim).
    """
    executors_path = (
        _system_md_path().parent.parent / "executors.yaml"
    )
    executors_text_raw = executors_path.read_text(encoding="utf-8")
    system_text = _collapse_ws(system_md_text)
    executors_text = _collapse_ws(executors_text_raw)

    doctrine_anchors = [
        "use the `brief` envelope on `engage_executor`",
        "into `brief.process_requirements` VERBATIM",
        "NEVER paraphrase a process instruction into a feature requirement",
        "Set `interaction_required: true` whenever the user asks for "
        "discussion/convergence/review",
        "Relay the executor's completion, which must account for each "
        "acceptance criterion",
    ]

    for anchor in doctrine_anchors:
        assert anchor in system_text, (
            f"system.md missing Ellen brief-envelope doctrine anchor: "
            f"{anchor!r}"
        )
        assert anchor in executors_text, (
            f"executors.yaml missing Ellen brief-envelope doctrine anchor: "
            f"{anchor!r}"
        )

    # Both executor cards must carry the doctrine — not just one card with
    # the other silently exempted from the process-fidelity requirement.
    assert executors_text.count(doctrine_anchors[0]) == 2, (
        "the brief-envelope doctrine must appear on BOTH executor cards "
        "(configurator + plugin-developer) in executors.yaml, found "
        f"{executors_text.count(doctrine_anchors[0])} occurrence(s)"
    )


def test_system_prompt_teaches_protected_tool_challenge_and_relay(system_md_text):
    """v0.77.0 [W2] doctrine anchor: Ellen's system prompt must carry the
    Sol-accepted protected-tool doctrine VERBATIM (a refused call posts a
    confirmation button; Ellen must NOT narrate/announce the approval
    prompt — PREFER ZERO narration, and if one sentence is unavoidable it
    must be timing-invariant, e.g. "I won't run this action without your
    approval."; then she ends her turn and retries with EXACTLY the same
    arguments on approval), plus her resident-specific relay/re-delegate
    paragraph for a delegated specialist's pending confirmation (same
    no-narration rule applies there too). A future prose rewrite that
    silently drops or paraphrases this text would leave Ellen either
    narrating a stale/pre-tap approval prompt or without the doctrine that
    keeps a re-tried protected call argument-identical (grants are
    argument-bound — see authz_grants.py)."""
    text = _collapse_ws(system_md_text)
    doctrine_anchors = [
        "your call will be refused and a confirmation button posted to the "
        "user",
        "Do not announce, describe, or explain the approval prompt",
        "Prefer zero narration",
        "I won't run this action without your approval.",
        "never phrasing like \"waiting for you\" or \"you'll receive a "
        "prompt\"",
        "END YOUR TURN",
        "retry the SAME call with EXACTLY the same arguments",
        "apply the same no-narration rule",
        "re-delegate the exact same action",
    ]
    for anchor in doctrine_anchors:
        assert anchor in text, (
            f"system.md missing v0.77.0 protected-tool doctrine anchor: "
            f"{anchor!r}"
        )


def test_butler_prompt_teaches_protected_tool_challenge_only():
    """v0.77.0 [W2] doctrine anchor: the butler prompt gets the
    protected-tool challenge/retry paragraph, including the no-narration
    rule and the timing-invariant fallback sentence (butler is a delegate
    target, same as Ellen). It does NOT get the relay/re-delegate
    paragraph — per design §3.8/W2 that paragraph is scoped to Ellen (the
    assistant), who is the one that delegates to specialists; butler's
    runtime.yaml carries no delegate_to_agent/engage_executor, so the
    relay guidance would be inert there."""
    agents_dir = _system_md_path().parent.parent.parent
    butler_path = agents_dir / "butler" / "prompts" / "system.md"
    text = _collapse_ws(butler_path.read_text(encoding="utf-8"))
    doctrine_anchors = [
        "your call will be refused and a confirmation button posted to the "
        "user",
        "Do not announce, describe, or explain the approval prompt",
        "Prefer zero narration",
        "I won't run this action without your approval.",
        "END YOUR TURN",
        "retry the SAME call with EXACTLY the same arguments",
    ]
    for anchor in doctrine_anchors:
        assert anchor in text, (
            f"butler system.md missing v0.77.0 protected-tool doctrine "
            f"anchor: {anchor!r}"
        )
    assert "re-delegate the exact same action" not in text, (
        "butler system.md should NOT carry the resident-only relay/"
        "re-delegate paragraph — butler never delegates (per design "
        "§3.8/W2, that paragraph is scoped to Ellen only)."
    )
    assert "apply the same no-narration rule" not in text, (
        "butler system.md should NOT carry the Ellen-only relay "
        "no-narration sentence — butler has no relay paragraph at all."
    )


# test_finance_specialist_prompt_teaches_protected_tool_challenge removed
# (Task N2 controller fix wave): Task N2's no-gap cutover deleted
# defaults/agents/specialists/finance/ (and its hand-authored
# prompts/system.md) from the image entirely — specialists now install
# from a component repository, so finance's specific doctrine-anchor
# content no longer lives in this repo to assert against. Unlike
# test_every_specialist_has_ask_user's FOR-ALL loop or
# test_real_shipped_role_artifact_loads' enumerated-real-dirs list, there
# is no synthetic equivalent to keep: this test audited one hand-authored
# file's exact prose, and that file's ownership moved to the finance
# component repo.


def _configurator_card() -> dict:
    """Parse executors.yaml and return the configurator executor card
    specifically (not a whole-file substring scan)."""
    import yaml

    executors_path = _system_md_path().parent.parent / "executors.yaml"
    doc = yaml.safe_load(executors_path.read_text(encoding="utf-8"))
    for entry in doc.get("executors", []):
        if entry.get("executor_type") == "configurator":
            return entry
    raise AssertionError("configurator executor card not found in executors.yaml")


def test_configurator_card_routes_repo_installs_with_correct_lifecycles():
    """v0.103.0 doctrine reconcile: the configurator card must (a) route
    repository installs of an existing component to ITSELF, (b) keep the
    read-only/factual-question non-dispatch guard, and (c) enumerate each
    component kind's OWN lifecycle verbs — critically, personas are
    install/apply/reset ONLY (no persona upgrade, no persona uninstall).
    Parses the configurator card and makes focused assertions on its
    purpose+when text rather than scanning the whole file."""
    card = _configurator_card()
    blob = _collapse_ws(f"{card['purpose']}\n{card['when']}")
    low = blob.lower()

    # (a) Repository install of all three component kinds is co-located IN the
    #     configurator card and routed to itself.
    for kind in ("specialist", "plugin", "persona"):
        assert kind in low, f"configurator card must mention the {kind} lifecycle"
    assert "repositor" in low and "install" in low, (
        "configurator card must route repository installs to itself"
    )

    # (b) The read-only/factual-question non-dispatch guard survives — Ellen
    #     must NOT engage the configurator for a factual config question.
    assert "read-only" in low and "answer directly" in low, (
        "configurator card must keep the read-only/factual-question guard"
    )
    assert "not yet supported" not in low, (
        "configurator card must not decline installs as 'not yet supported'"
    )

    # (c) Per-kind lifecycle verbs: specialists get the full four (incl.
    #     rollback), personas get apply/reset and NOTHING that implies
    #     upgrade/uninstall.
    assert "rollback" in low, "specialist lifecycle must include rollback"
    for verb in ("apply", "reset"):
        assert verb in low, f"persona lifecycle must include {verb}"
    assert re.search(r"no upgrade and no uninstall", low), (
        "configurator card must state personas have NO upgrade and NO "
        "uninstall (persona lifecycle is install/apply/reset only)"
    )

    # (d) reset is residents-only and restores the image default — the card
    #     must not imply a specialist reset or that reset is a persona-content
    #     operation (resident_persona_reset resets a resident to its default).
    assert re.search(r"reset[^.]*resident", low), (
        "card must scope persona reset to residents"
    )
    assert re.search(r"(residents-only|resident[- ]only)", low) or re.search(
        r"reset a resident", low
    ), "card must state reset is residents-only"

    # (e) no bare engage_executor(task=..., context=...) example — it would
    #     contradict the card's own mandatory `brief`-envelope rule.
    assert not re.search(r"task\s*=\s*['\"<.]", blob), (
        "configurator card must not show a bare task= engage_executor example "
        "in any quoted/placeholder form (it must use the brief envelope)"
    )
    assert "brief" in low, "configurator card must reference the brief envelope"


def test_create_vs_install_distinction_pinned(system_md_text):
    """The create-vs-install distinction must stay explicit in BOTH the
    configurator card and the system prompt: build a NEW plugin from scratch
    -> plugin-developer; install an EXISTING repository component (specialist/
    plugin/persona) -> configurator. Over-broadening (dropping the create
    side) would mis-route brand-new plugin builds; over-narrowing (dropping
    the install side) reintroduces the 'not yet supported' decline."""
    card = _configurator_card()
    # Strip markdown emphasis (**bold**) — it is not semantic and must not
    # break a literal "configurator job" match.
    card_low = _collapse_ws(f"{card['purpose']}\n{card['when']}").lower().replace("*", "")
    system_low = _collapse_ws(system_md_text).lower().replace("*", "")

    # Bind each routing CONCEPT to its TARGET within one CLAUSE. The bound
    # stops at BOTH periods and semicolons ([^.;]*) so the install clause's
    # target cannot leak from the adjacent create clause (the card separates
    # the two routes with a semicolon) — flipping either target fails.
    assert re.search(
        r"install[^.;]*existing[^.;]*(component|repositor)[^.;]*configurator", card_low
    ), "configurator card must route installing an EXISTING component to the configurator"
    assert re.search(
        r"(create|build)[^.;]*new[^.;]*plugin[^.;]*plugin-developer", card_low
    ), "configurator card must route CREATING a NEW plugin to plugin-developer"

    # system.md: the install section's routing sentence must bind the
    # existing-component install to the literal "configurator job" (not merely
    # any later 'configurator' mention), AND explicitly exclude plugin-developer.
    assert re.search(
        r"already-published component[^.]*configurator job", system_low
    ), "system.md install section must bind existing-component installs to a 'configurator job'"
    assert re.search(
        r"configurator job[^.]*not plugin-developer", system_low
    ), "system.md install section must explicitly exclude plugin-developer for installs"

    # system.md must not reintroduce the decline wording.
    assert "not yet supported" not in system_low, (
        "system.md must not decline installs as 'not yet supported'"
    )
    assert re.search(r"no upgrade and no uninstall", system_low), (
        "system.md persona bullet must state NO upgrade and NO uninstall"
    )


def test_system_prompt_forbids_engage_executor_context_bleed(system_md_text):
    """O-6 (v0.37.9): Ellen's ``engage_executor`` ``task=`` arg must
    carry ONLY the new task description — not the cumulative
    conversation context with prior tasks bleeding through.

    Live evidence: 2026-05-14 P27.2 cid ``093a02c7`` — Ellen's single
    turn spawned BOTH configurator AND plugin-developer engagements,
    and the configurator engagement received P27.1's rename task
    description instead of P27.2's repo creation task. Cause: Ellen's
    SDK conversation history carried the prior task into the new
    engage_executor call. This guard catches accidental revert of the
    prompt-side mitigation.
    """
    text = system_md_text.lower()
    # Anchor phrase from the v0.37.9 prompt fix — match the spirit of the
    # rule without over-binding the exact wording so editorial polish
    # remains possible.
    assert "only" in text and "engage_executor" in text, (
        "system prompt must include ONLY-the-new-task guidance for "
        "engage_executor calls."
    )
    assert (
        "do not carry" in text
        or "do not include" in text
        or "do not bleed" in text
        or "without bleeding" in text
    ), (
        "system prompt must forbid bleeding prior conversation context "
        "into engage_executor's task arg."
    )


# ---------------------------------------------------------------------------
# INV-TOOL-005 (#443) — no shipped prompt asserts an integration's liveness
# ---------------------------------------------------------------------------

def _shipped_setup_prompts() -> list[Path]:
    """Every bundled prompt/doctrine file that speaks about the plugin
    setup-tool hand-back — the three surfaces the #443 incident traversed."""
    root = Path(__file__).resolve().parent.parent
    base = root / "casa/rootfs/opt/casa/defaults/agents"
    paths = [
        base / "assistant/prompts/system.md",
        base / "executors/configurator/doctrine/recipes/plugin/add.md",
        base / "executors/configurator/doctrine/recipes/plugin/update.md",
    ]
    # A renamed or moved file must fail these tests LOUDLY rather than let them
    # pass over nothing (a vacuous guard is worse than no guard).
    missing = [p for p in paths if not p.is_file()]
    assert not missing, f"setup-prompt surfaces moved: {missing}"
    return paths


# The EXPLICIT prohibition each surface must carry. This is the load-bearing
# assertion: a phrase deny-list cannot fence an open namespace of ways to say
# "the integration is down" (a reviewer's "the service is unavailable until
# setup completes" defeats any such list), so what is pinned is that the
# instruction NOT to make the claim is present and reaches the model at all.
_REQUIRED_PROHIBITION = {
    "system.md": "Do not relay anyone else's verdict on whether a connection works",
    "add.md": "Make no claim of your own about whether the integration works, in either direction",
    "update.md": "Make no claim about the integration's state, in either direction",
}

# Known-bad wordings, kept as a secondary belt: each shipped in one of these
# files before this release, so a revert to any of them is caught by name. This
# list is NOT the guarantee — see above.
_FORBIDDEN_LIVENESS_CLAIMS = [
    "the integration is dead",
    "the integration is down",
    "the integration is not live",
    "integration is NOT live",
    "the integration goes live after",
]


@pytest.mark.parametrize("path", _shipped_setup_prompts(),
                         ids=lambda p: p.name)
def test_shipped_setup_prompt_carries_the_liveness_prohibition(path):
    """#443 RED CASE: the incident was a shipped prompt telling an agent to
    announce an integration dead — for a Gmail that was serving throughout.

    INV-TOOL-005 lives entirely in shipped prose, so these tests ARE its
    enforcement — there is no code path to assert against. Deleting the
    prohibition from any of these three surfaces fails here.
    """
    collapsed = _collapse_ws(path.read_text(encoding="utf-8"))
    required = _REQUIRED_PROHIBITION[path.name]
    assert required in collapsed, (
        f"{path.name} no longer instructs the agent to make no liveness claim "
        f"(expected: {required!r}). Casa cannot see the external side, and a "
        f"setup tool is not required to test what it provisioned "
        f"(INV-TOOL-005, #443)."
    )


@pytest.mark.parametrize("path", _shipped_setup_prompts(),
                         ids=lambda p: p.name)
def test_no_shipped_prompt_reverts_to_a_known_liveness_claim(path):
    """The secondary belt: the exact wordings that shipped before this release
    must not come back. Not exhaustive by construction."""
    collapsed = _collapse_ws(path.read_text(encoding="utf-8")).lower()
    offenders = [c for c in _FORBIDDEN_LIVENESS_CLAIMS
                 if c.lower() in collapsed]
    assert not offenders, (
        f"{path.name} asserts integration liveness Casa cannot observe: "
        f"{offenders} (INV-TOOL-005, #443)."
    )


def test_setup_handback_prompts_defer_to_the_tools_own_result():
    """The positive half: the recipes must point the engager at the TOOL's own
    result rather than at a verdict of Casa's — and must not overstate what that
    result covers, since provisioning is all the authoring contract requires."""
    add = _collapse_ws(_shipped_setup_prompts()[1].read_text(encoding="utf-8"))
    upd = _collapse_ws(_shipped_setup_prompts()[2].read_text(encoding="utf-8"))
    assert "its own result is what to go on" in add
    assert "its own result is what to go on" in upd
    # ...and must NOT promise the tool reports liveness: the contract requires
    # provisioning and does not REQUIRE a probe (Sol r3), which is not the same
    # as claiming no tool ever probes (Terra/Sol r4).
    assert "does NOT require the tool to test what it provisioned" in add
    assert "not *required* to test what it provisioned" in upd


def test_assistant_refuses_to_relay_a_connection_verdict():
    """The assistant is where the false claim reached the operator, so its
    prompt must forbid relaying another party's verdict outright."""
    collapsed = _collapse_ws(_system_md_path().read_text(encoding="utf-8"))
    assert ("Do not relay anyone else's verdict on whether a connection works"
            in collapsed)


def test_liveness_prohibition_reaches_a_persona_bound_assistant():
    """The test above guards the COMPOSED prompt — which a persona-bound
    resident never reads, because the compiled bundle replaces it
    (INV-PERS-001). #443's prohibition therefore had no force on the normal
    configuration: the same defect #549 fixed for the response limits. It is
    now in the role doctrine's core section, so it reaches every projection.

    Asserted on the COMPILED text, not on the file, because selecting the core
    section is what actually carries it into the prompt."""
    from markdown_sections import select_markdown_sections

    doctrine = (Path(__file__).resolve().parents[1]
                / "casa/rootfs/opt/casa/defaults/roles/resident/assistant"
                / "doctrine.md").read_text(encoding="utf-8")
    sentence = "Do not relay anyone else's verdict on whether a connection works"
    assert sentence in _collapse_ws(doctrine)
    for surface in ("Text projection", "Voice projection",
                    "Restricted webhook projection"):
        selected = select_markdown_sections(
            doctrine, ("Core doctrine", surface),
            exclude=("Text projection", "Voice projection",
                     "Restricted webhook projection"))
        assert sentence in _collapse_ws(selected), surface


# ---------------------------------------------------------------------------
# #633 — a memory wipe nobody can perform must be REFUSED, not improvised
#
# D31 (operator ruling, #753): the capability is deliberately NOT built. What
# the operator is owed instead is a clear failure. There is no code surface
# that can deliver it — the tool is in no shipped agent's MCP server at all, so
# no handler ever runs — which is why the rule lives in the shipped prompt
# carriers and is pinned here.
#
# Pinned on the PRODUCTION compiler, not on the section selector: a
# persona-bound resident never reads the composed system prompt (INV-PERS-001),
# and `test_liveness_prohibition_reaches_a_persona_bound_assistant` above calls
# `select_markdown_sections` directly — so a compiler that dropped Core would
# leave it green while every real projection lost the rule.
# ---------------------------------------------------------------------------

WIPE_REFUSAL_ANCHORS = (
    "Only claim that you can wipe long-term memory when `wipe_memory` is "
    "actually present in your tools.",
    "If it is absent, say that this agent cannot perform the wipe.",
    "Do not delegate the request, route it through `ask_user`, or say that a "
    "confirmation is coming.",
    "Tell the operator to run `casactl memory-wipe --yes` in the add-on "
    "terminal, and state that the wipe is irreversible.",
)

def _resident_slots() -> tuple[str, ...]:
    """Derived, never hand-listed: a fourth resident slot must widen every
    carrier assertion below automatically, not silently leave it behind."""
    from role_slot import FIXED_RESIDENT_SLOTS
    return FIXED_RESIDENT_SLOTS


_RESIDENT_SLOTS = _resident_slots()


def _casa_root() -> Path:
    return Path(__file__).resolve().parents[1] / "casa/rootfs/opt/casa"


def _compiled_resident_carriers() -> list[tuple[str, str]]:
    """Every shipped resident role compiled through the REAL prompt compiler,
    bound to its image-default persona: three roles x three projections."""
    from persona_pack import load_persona_pack
    from personality_binding import IMAGE_DEFAULT_PERSONA_BY_SLOT
    from prompt_compiler import compile_projection_set
    from role_artifact import load_role_artifact
    from role_slot import materialize_role

    root = _casa_root()
    platform_frame = (root / "defaults/personality/platform-frame.md").read_text(
        encoding="utf-8")
    safety_kernel = (root / "defaults/personality/safety-kernel.md").read_text(
        encoding="utf-8")

    carriers: list[tuple[str, str]] = []
    for slot in _RESIDENT_SLOTS:
        role = materialize_role(
            source=load_role_artifact(root / "defaults/roles/resident" / slot),
            options={},
        )
        persona_id, _, version = IMAGE_DEFAULT_PERSONA_BY_SLOT[slot].partition("@")
        persona_dir = root / "defaults/personas" / persona_id / version
        persona = load_persona_pack(
            persona_dir / "pack", persona_dir / "manifest.json")
        projections = compile_projection_set(
            role=role, persona=persona, platform_frame=platform_frame,
            safety_kernel=safety_kernel,
        )
        for surface, compiled in sorted(projections.items()):
            carriers.append((f"{slot}:{surface}", compiled.system_prompt))
    return carriers


def _legacy_prompt_carriers() -> list[tuple[str, str]]:
    """The composed-prompt configuration's carriers, one per resident."""
    root = _casa_root()
    return [
        (slot, (root / "defaults/agents" / slot / "prompts/system.md").read_text(
            encoding="utf-8"))
        for slot in _RESIDENT_SLOTS
    ]


def test_every_shipped_resident_refuses_an_unavailable_memory_wipe_honestly():
    """RED pre-fix (#633): asking any shipped resident to wipe long-term memory
    reaches a resident with no wipe tool and no instruction about it, so what
    the operator gets is improvised — the stranding the issue reports.

    Counts, and over EVERY carrier: the published claim is "no shipped agent",
    so a pin scoped to the assistant would let the claim outrun its evidence."""
    compiled = _compiled_resident_carriers()
    legacy = _legacy_prompt_carriers()
    assert len(compiled) == 9
    assert len(legacy) == 3

    for anchor in WIPE_REFUSAL_ANCHORS:
        assert sum(_collapse_ws(text).count(_collapse_ws(anchor)) == 1
                   for _name, text in compiled) == 9
        assert sum(_collapse_ws(text).count(_collapse_ws(anchor)) != 1
                   for _name, text in compiled) == 0
        assert sum(_collapse_ws(text).count(_collapse_ws(anchor)) == 1
                   for _name, text in legacy) == 3
        assert sum(_collapse_ws(text).count(_collapse_ws(anchor)) != 1
                   for _name, text in legacy) == 0


# ---------------------------------------------------------------------------
# #652 red case — INV-PERS-015.
#
# The resident's live prompt IS the compiled bundle, and at the base that
# bundle carries no credential-handling rule and no disclosure section at all:
# `policies.render_disclosure_section` has exactly one caller,
# `agent_loader._compose_prompt`, whose output no bundle-bound agent is served.
# That is WHY the disclosure policy's "credentials" category — which in any
# case sits at `required_trust: authenticated`, i.e. permits the operator's own
# DM — never constrained the resident.
#
# The rule therefore has to live on a surface that is actually compiled.
# `defaults/personality/safety-kernel.md` is the only image-owned input carried
# into every resident projection AND every bound specialist's, and it is not an
# input to `role_slot.compute_role_checksum`, so stating it there moves every
# projection digest without invalidating a single persisted binding.
#
# Specified by the red-case reviewer (Terra), 2026-08-30, before any production
# change. This DECLARES an invariant (D34) rather than pinning a prior one: it
# asserts presence in the served prompt, and asserts nothing about enforcement.
# ---------------------------------------------------------------------------

CREDENTIAL_RULE_ANCHORS = (
    "A credential-bearing artifact a tool returns is a capability",
    "An earlier agreement to fetch or send is not authority for the next one",
)


@pytest.mark.parametrize("anchor", CREDENTIAL_RULE_ANCHORS)
def test_every_resident_projection_carries_the_credential_rule_exactly_once(
    anchor,
) -> None:
    """Pins INV-PERS-015.

    RED pre-fix: both anchors occur zero times in the safety kernel, so the
    tuple is (0, 0, 9) — the rule is absent from every one of the nine served
    projections. That, not a fixture or import problem, is the intended
    failure.

    Exactly-once rather than at-least-once: `compile_projection_set` selects
    doctrine sections by containment and appends the kernel per surface, so a
    duplicate would be evidence the assembly changed shape.
    """
    compiled = _compiled_resident_carriers()
    assert len(compiled) == 9

    needle = _collapse_ws(anchor)
    counts = [_collapse_ws(text).count(needle) for _name, text in compiled]
    kernel = (_casa_root() / "defaults/personality/safety-kernel.md").read_text(
        encoding="utf-8")

    assert (
        _collapse_ws(kernel).count(needle),
        sum(1 for c in counts if c == 1),
        sum(1 for c in counts if c != 1),
    ) == (1, 9, 0)


# ---------------------------------------------------------------------------
# #658 red case — the authorization-link formatting rule.
#
# An authorization or consent page a household member has to open reaches them
# as a bare several-hundred-character address, because no shipped prompt
# carrier says anything about how to write such a link. No code composes that
# message: the address arrives as a plugin tool result the model is handed
# directly, so the instruction IS the fix.
#
# This test DECLARES rather than pins (D34). It asserts PRESENCE and PROVENANCE
# in the served prompt and nothing else — not model compliance, not what a
# transport then does with the labelled form, and not enforcement, of which
# there is none and of which this change adds none. Deliberately no invariant
# id: one paragraph, one test, retirable without manifest surgery.
#
# Specified by sol, 2026-09-02, before any production change; accepted by
# terra. The intended failure at the base is ABSENCE — the paragraph occurs
# zero times in all nine compiled resident carriers — not an import, fixture or
# helper error.
#
# The guard is PARAGRAPH IDENTITY, not clause sampling, and that is a
# deliberate replacement rather than a first guess: a presence-anchor pin was
# specified, then defeated twice by reproduction — once by deleting a clause,
# once by ADDITIVE broadening of the governed class ("...or any other link they
# must visit..."), which no existential anchor can ever see. The per-sentence
# counts below are additional, not a return to sampling: they close a partial
# leak (one sentence copied into a Voice section) that a whole-paragraph count
# cannot see. Sections are selected ALONE because Core+Text is CONCATENATED
# before the whitespace collapse, so a paragraph split across that boundary
# reconstructs itself in the combined selection while its prefix ships in all
# nine projections.
# ---------------------------------------------------------------------------

_LINK_RULE_SENTENCES = (
    "When a person has to open a link themselves — an authorization or consent "
    "page they must visit to grant a connection access — write it as a labelled "
    "link, `[action (destination-domain)](url)`: name the action and the real "
    "destination domain in the label, rather than leaving the address standing "
    "bare.",
    "The one exception is a message sent with the message tool, which is not "
    "rendered: put the plain address there.",
    "Opening that page is what the link is for, so handing it to the person who "
    "must open it is passing it to its intended consumer; labelling changes only "
    "the shape of a link you were already going to hand over, never whether you "
    "may hand it over.",
)

LINK_RULE_PARAGRAPH = " ".join(_LINK_RULE_SENTENCES)


def test_the_authorization_link_rule_reaches_only_the_resident_text_projections():
    """#658: every resident's SERVED text projection carries the
    authorization-link paragraph exactly once, sourced from that role's
    doctrine `## Text projection` section, and no other served projection
    carries any part of it.

    RED pre-fix: the paragraph occurs zero times in all nine compiled
    carriers and zero times in every doctrine section. That absence, not a
    fixture or import problem, is the intended failure.
    """
    from markdown_sections import select_markdown_sections
    from prompt_compiler import _PROJECTION_HEADINGS

    compiled = _compiled_resident_carriers()
    assert len(compiled) == 9

    paragraph = _collapse_ws(LINK_RULE_PARAGRAPH)
    text_counts = [_collapse_ws(t).count(paragraph)
                   for name, t in compiled if name.endswith(":text")]
    assert text_counts == [1, 1, 1]

    # Per sentence: exactly once on every text carrier, and NOT PRESENT AT ALL
    # on the six others — a whole-paragraph count cannot see one sentence
    # copied into a Voice or restricted-webhook section.
    for sentence in _LINK_RULE_SENTENCES:
        needle = _collapse_ws(sentence)
        on_text = [_collapse_ws(t).count(needle)
                   for name, t in compiled if name.endswith(":text")]
        elsewhere = [_collapse_ws(t).count(needle)
                     for name, t in compiled if not name.endswith(":text")]
        assert (on_text, elsewhere) == ([1, 1, 1], [0, 0, 0, 0, 0, 0]), sentence

    # Provenance: the doctrine's own Text section, selected ALONE, and never
    # the Core section that every projection inherits.
    for slot in _RESIDENT_SLOTS:
        doctrine = (_casa_root() / "defaults/roles/resident" / slot
                    / "doctrine.md").read_text(encoding="utf-8")
        text_only = select_markdown_sections(
            doctrine, ("Text projection",), exclude=_PROJECTION_HEADINGS)
        core_only = select_markdown_sections(
            doctrine, ("Core doctrine",), exclude=_PROJECTION_HEADINGS)
        assert (_collapse_ws(text_only).count(paragraph),
                _collapse_ws(core_only).count(paragraph)) == (1, 0), slot


# --------------------------------------------------------------------------
# #517 arm B — the channel-fit table rule. Same carrier discipline as the
# #658 authorization-link pin above: paragraph identity, per-sentence counts,
# and provenance. A SEPARATE paragraph; the #658 sentences are untouched.
# --------------------------------------------------------------------------

_TABLE_SHAPE_SENTENCES = (
    "A table is the right shape only for a small, tidy grid: at most three "
    "columns, every cell a short value.",
    "When the material is wider than that, or a cell would carry a sentence, "
    "write one `Field: value` line per item instead, with a blank line "
    "between items.",
    "A single fact or a two-row comparison is a sentence, not a table.",
)

TABLE_SHAPE_PARAGRAPH = " ".join(_TABLE_SHAPE_SENTENCES)


def test_the_table_shape_rule_reaches_only_the_resident_text_projections():
    """#517: every resident's served text projection carries the channel-fit
    table paragraph exactly once, sourced from that role's doctrine
    `## Text projection` section, and no other served projection carries any
    part of it.

    RED pre-fix: the paragraph occurs zero times in all nine compiled
    carriers and zero times in every doctrine section. That absence, not a
    fixture or import problem, is the intended failure.
    """
    from markdown_sections import select_markdown_sections
    from prompt_compiler import _PROJECTION_HEADINGS

    compiled = _compiled_resident_carriers()
    assert len(compiled) == 9

    paragraph = _collapse_ws(TABLE_SHAPE_PARAGRAPH)
    text_counts = [_collapse_ws(t).count(paragraph)
                   for name, t in compiled if name.endswith(":text")]
    other_counts = [_collapse_ws(t).count(paragraph)
                    for name, t in compiled if not name.endswith(":text")]
    assert text_counts == [1, 1, 1]
    assert other_counts == [0, 0, 0, 0, 0, 0]

    # Per sentence, because a whole-paragraph count cannot see one sentence
    # copied into a Voice or restricted-webhook section.
    for sentence in _TABLE_SHAPE_SENTENCES:
        needle = _collapse_ws(sentence)
        on_text = [_collapse_ws(t).count(needle)
                   for name, t in compiled if name.endswith(":text")]
        elsewhere = [_collapse_ws(t).count(needle)
                     for name, t in compiled if not name.endswith(":text")]
        assert (on_text, elsewhere) == ([1, 1, 1], [0, 0, 0, 0, 0, 0]), sentence

    # Provenance: the doctrine's own Text section, selected ALONE, and never
    # the Core section that every projection inherits.
    for slot in _RESIDENT_SLOTS:
        doctrine = (_casa_root() / "defaults/roles/resident" / slot
                    / "doctrine.md").read_text(encoding="utf-8")
        text_only = select_markdown_sections(
            doctrine, ("Text projection",), exclude=_PROJECTION_HEADINGS)
        core_only = select_markdown_sections(
            doctrine, ("Core doctrine",), exclude=_PROJECTION_HEADINGS)
        assert (_collapse_ws(text_only).count(paragraph),
                _collapse_ws(core_only).count(paragraph)) == (1, 0), slot


def test_517_does_not_disturb_the_658_authorization_link_paragraph():
    """The #658 paragraph is pinned BY IDENTITY; a neighbouring paragraph
    must not change it. Mutation check — PASSES at the base commit.
    """
    compiled = _compiled_resident_carriers()
    needle = _collapse_ws(LINK_RULE_PARAGRAPH)
    assert [_collapse_ws(t).count(needle)
            for name, t in compiled if name.endswith(":text")] == [1, 1, 1]


# ---------------------------------------------------------------------------
# #651 red case — the Telegram retention telling.
#
# At the base every shipped resident projection is SILENT about retention while
# `channel_policy.writes_to_bank("telegram")` is True and `save_session` banks
# the transcript on `/new`, on the freshness sweep and on a superseded session.
# The only memory tool the assistant is granted is read-side `recall_memory`,
# and the kernel and frame push the model toward disowning memory ("never
# convert them into first-person recollection"), so what the operator gets is
# the disclaimer the issue reports — while the conversation is banked anyway.
#
# This DECLARES rather than pins (D34): it asserts PRESENCE and SCOPE in the
# served prompt, and nothing about model compliance or about whether any
# particular retain succeeded. Deliberately no invariant id, following #658
# above: one paragraph, one test, retirable without manifest surgery.
#
# Specified by astra, 2026-09-06, before any production change; accepted by
# terra. The intended failure at the base is `assistant:text: 0, expected 1` —
# an ABSENCE, not an import, fixture or helper error.
#
# SCOPE IS THE WHOLE POINT, and placement alone does not deliver it.
# `projection_for` sends a `/invoke` turn (channel "webhook") to the TEXT
# projection while `writes_to_bank("webhook")` is False, so a paragraph scoped
# only by its section would be served to a route it is false on. The prose
# therefore names Telegram in every affirmative clause, and this test asserts
# the negative on the other eight carriers as hard as the positive on the one.
#
# SCOPE IS ALSO WHAT THE RESIDUAL PIN (assertion 6) IS FOR. Counting this
# paragraph proves it is present where it belongs and absent elsewhere; it
# says nothing about a DIFFERENT sentence, added later, that makes the
# prohibited claim in other words. Two reviewers each defeated a
# vocabulary-based residual with one such sentence, so the residual is pinned
# by content digest per carrier instead.
# ---------------------------------------------------------------------------

_RETENTION_SENTENCES = (
    "On Telegram, an ending conversation is meant to be kept, not dropped: "
    "when one ends — the person starts a fresh one with `/new`, or it goes "
    "quiet long enough to be swept up — Casa retains the exchange to "
    "long-term memory, and whatever is retained can be recalled later, "
    "subject to the clearance of whoever is asking.",
    "Do not tell anyone on Telegram that what they say cannot be kept, or "
    "that it will be gone by next week, merely because you hold no "
    "memory-writing tool: the retention is the system's and does not pass "
    "through you.",
    "That is Telegram's policy and nothing more — it says nothing about a "
    "voice or webhook conversation, an `/invoke` turn, or a delegation "
    "originating on one of those; a save can also fail, so never report a "
    "particular exchange as now being in memory.",
)

RETENTION_PARAGRAPH = " ".join(_RETENTION_SENTENCES)

# The compiled content of every carrier as it stands at the base, with this
# paragraph stripped from the one carrier that may hold it. Per-carrier rather
# than one digest over all nine, so a failure NAMES the surface that moved.
#
# A vocabulary deny-list stood here first and was defeated twice by
# reproduction — by "The same retention policy applies to webhook
# conversations" (astra) and then, after the list was measured against that,
# by "Webhook conversations are kept after they end." (terra), which says the
# prohibited thing using none of the listed words. Widening the list is
# sharpening a mechanism that has now failed twice in the same shape; a
# residual pin is the generalisation, and both reviewers asked for one.
#
# WHEN THIS FIRES because of an unrelated prompt change: that is the intended
# behaviour, not a stale fixture. Re-read this paragraph's scope against the
# carrier that moved — a retention claim on any other surface, or a broadened
# one here, is exactly what it exists to catch — and only then regenerate the
# digest for that carrier. Never regenerate the map wholesale from the
# candidate; that is the one move which turns this assertion back into nothing.
_RESIDUAL_DIGESTS = {
    "assistant:restricted_webhook":
        "2dd2928a69e7218e312d351a11e2f7a133ea93171db28dd864b5cb52e546b0ef",
    "assistant:text":
        "62d7824d006638e101817422aa3ae0ece973c16c112a58bbeec2d02b61ecb08f",
    "assistant:voice":
        "33063c98973890148cfbad89b537e3bde6634e6ddc37e8313c10b413e7dfd5ed",
    "butler:restricted_webhook":
        "63f746c67fa33c396267c125c11a7d6948d897e579d5cf0021626dd7d616501f",
    "butler:text":
        "45c1aa348c67c243f7f1a4deeb7b44375f7bb6cf848e1cf3ee3c5e9a4af2ad38",
    "butler:voice":
        "6325dfcea3037b1d15cea941e8f73e8000afd638191606196b6fc212e293666b",
    "concierge:restricted_webhook":
        "4baf96c7443a5688e5c952532a0275eda8e906a8ef04548b54dd511d153d9124",
    "concierge:text":
        "0128cf952c45901582c75b4acfbc5647f4499ba240442dc75674fc9d62446108",
    "concierge:voice":
        "319a61259c3a302df90cc6554da15e962eedcbcc2c239aaadcab012c145a1cb2",
}


def _retention_expectation() -> dict[str, int]:
    """DERIVED, never hand-listed (see `_resident_slots`): a fourth resident
    slot, or a fourth surface, must widen this map automatically."""
    return {
        f"{slot}:{surface}": int(slot == "assistant" and surface == "text")
        for slot in _RESIDENT_SLOTS
        for surface in ("text", "voice", "restricted_webhook")
    }


def test_the_telegram_retention_telling_reaches_only_the_assistant_text_projection():
    """#651: the assistant's SERVED text projection says a Telegram
    conversation is saved when it ends, and no other shipped resident
    projection says anything of the kind.

    RED pre-fix: the paragraph occurs zero times on all nine compiled
    carriers, so `assistant:text` is 0 where 1 is expected. That absence, not
    a fixture or import problem, is the intended failure.
    """
    from channel_policy import _WRITABLE_CHANNELS, writes_to_bank
    from markdown_sections import select_markdown_sections
    from prompt_compiler import _PROJECTION_HEADINGS

    compiled = _compiled_resident_carriers()
    expected = _retention_expectation()

    # 1. Exact carrier inventory INCLUDING multiplicity — asserted on the list
    #    before any dict is built, or a duplicated carrier name would vanish.
    assert sorted(name for name, _ in compiled) == sorted(expected)

    # 2. Whole paragraph, then each sentence on its own. The #658 comment
    #    above records why both: a whole-paragraph count cannot see one
    #    sentence copied into a Voice section, and per-sentence anchors alone
    #    were defeated by clause deletion and by additive broadening.
    paragraph = _collapse_ws(RETENTION_PARAGRAPH)
    assert {name: _collapse_ws(body).count(paragraph)
            for name, body in compiled} == expected
    for sentence in _RETENTION_SENTENCES:
        needle = _collapse_ws(sentence)
        assert {name: _collapse_ws(body).count(needle)
                for name, body in compiled} == expected, sentence[:48]

    # 3. Provenance: the assistant doctrine's own Text section, selected
    #    ALONE, and never the Core section every projection inherits.
    text_sections: dict[str, str] = {}
    for slot in _RESIDENT_SLOTS:
        doctrine = (_casa_root() / "defaults/roles/resident" / slot
                    / "doctrine.md").read_text(encoding="utf-8")
        text_only = select_markdown_sections(
            doctrine, ("Text projection",), exclude=_PROJECTION_HEADINGS)
        core_only = select_markdown_sections(
            doctrine, ("Core doctrine",), exclude=_PROJECTION_HEADINGS)
        text_sections[slot] = text_only
        assert (_collapse_ws(text_only).count(paragraph),
                _collapse_ws(core_only).count(paragraph)) == (
                    (1, 0) if slot == "assistant" else (0, 0)), slot

    # 4. The paragraph is a STANDALONE block, and the last one in the section.
    #    A count survives insertion INSIDE another sentence; this does not.
    blocks = re.split(r"\n[ \t]*\n", text_sections["assistant"].strip())
    assert _collapse_ws(blocks[-1]).strip() == paragraph

    # 5. "long-term memory" is said exactly twice on every carrier at the base
    #    (the kernel's attributed-evidence rule and Core's wipe refusal); this
    #    paragraph is the third occurrence, on one carrier.
    assert {name: _collapse_ws(body).lower().count("long-term memory")
            for name, body in compiled} == {
                name: 2 + n for name, n in expected.items()}

    # 6. Residual content: strip this paragraph from the one carrier that may
    #    carry it, and every carrier is byte-for-byte what it was before this
    #    change. This is what catches a SECOND sentence making the prohibited
    #    claim in words no anchor list contains — read the note on
    #    `_RESIDUAL_DIGESTS` before regenerating anything.
    residual = {}
    for name, body in compiled:
        collapsed = _collapse_ws(body)
        if expected[name]:
            collapsed = _collapse_ws(collapsed.replace(paragraph, "", 1))
        residual[name] = hashlib.sha256(
            collapsed.strip().encode("utf-8")).hexdigest()
    assert residual == _RESIDUAL_DIGESTS

    # 7. The policy the prose depends on. If Telegram ever loses write-trust,
    #    or another channel gains it, this paragraph must be re-read — so the
    #    pin fails rather than the prompt quietly becoming false.
    assert _WRITABLE_CHANNELS == frozenset({"telegram"})
    assert [writes_to_bank(c) for c in (
        "telegram", "ha_voice", "voice", "webhook", "webhook_trigger",
        "no-such-channel")] == [True, False, False, False, False, False]


def test_the_retention_telling_is_scoped_by_channel_because_invoke_gets_text():
    """The routing fact that makes assertion 6's negatives necessary: a
    `/invoke` turn is served the TEXT projection while its channel is not
    write-trusted, so the paragraph's Telegram scoping is what keeps it true
    there. Reproduced through the production selector, not asserted in prose.
    """
    from prompt_compiler import CompiledPromptBundle, CompiledProjection, projection_for

    bodies = dict(_compiled_resident_carriers())

    def _proj(surface: str) -> CompiledProjection:
        return CompiledProjection(
            system_prompt=bodies[f"assistant:{surface}"],
            digest=f"stub-{surface}", estimated_tokens=0,
        )

    bundle = CompiledPromptBundle(
        role_id="resident:assistant", resolved_model="opus",
        text=_proj("text"), voice=_proj("voice"),
        restricted_webhook=_proj("restricted_webhook"),
        binding_digest="stub-binding-digest",
    )
    routed = {
        ("telegram", "telegram"): "text",
        ("webhook", "invoke"): "text",
        ("webhook", "webhook_trigger"): "restricted_webhook",
        ("voice", None): "voice",
        ("voice", "webhook_trigger"): "restricted_webhook",
        ("no-such-channel", None): "text",
    }
    for (channel, origin_route), surface in routed.items():
        got = projection_for(bundle, channel=channel, origin_route=origin_route)
        assert got.system_prompt == bodies[f"assistant:{surface}"], (
            channel, origin_route)
