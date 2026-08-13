"""Regression guards on assistant/prompts/system.md content (Phase 5 / E-15).

Plain string-match assertions on the bundled Ellen system prompt. The
prompt isn't a code artifact — it's a YAML-resolved markdown file that
ships with the addon — but the wording is load-bearing for tool-routing
behavior. These tests catch accidental reverts.
"""

from __future__ import annotations

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
