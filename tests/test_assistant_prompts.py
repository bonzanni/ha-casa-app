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
import yaml


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


def test_system_prompt_teaches_talking_with_a_specialist(system_md_text):
    """Interactive specialist engagements are retired: Ellen must not be
    taught to open one, and a request to talk with a specialist is a sync
    delegation whose answer she tells the person herself; the person carries
    on by writing to her.

    Anchors a small set of phrases so a future prose rewrite can't
    silently drop the guidance.
    """
    text = " ".join(system_md_text.lower().split())
    assert "mode='interactive'" not in text, (
        "system prompt must not teach the retired interactive mode."
    )
    assert "mode='sync'" in text, (
        "system prompt must teach sync-mode delegation."
    )
    assert ("talk with a specialist" in text
            and "they carry on by writing to you" in text), (
        "system prompt must say how the person talks with a specialist."
    )
    assert "engagements supergroup" not in text and "a job lives in this chat" in text, (
        "system prompt must say a job lives in the direct chat, and name no "
        "supergroup as its destination."
    )


def test_system_prompt_teaches_protected_tool_challenge_and_relay(system_md_text):
    """v0.77.0 [W2] doctrine anchor: Ellen's system prompt must carry the
    Sol-accepted protected-tool doctrine VERBATIM (a refused call posts a
    confirmation button; Ellen must NOT narrate/announce the approval
    prompt — PREFER ZERO narration, never phrasing that assumes the tap has
    not happened yet; then she ends her turn and retries with EXACTLY the
    same arguments on approval; #1252 took out the one sentence it used to
    offer, since Casa now withholds every word written after the call),
    plus her resident-specific relay/re-delegate
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
    assert "I won't run this action without your approval." not in text


def test_butler_prompt_teaches_protected_tool_challenge_only():
    """v0.77.0 [W2] doctrine anchor: the butler prompt gets the
    protected-tool challenge/retry paragraph, including the no-narration
    rule, and no longer the fallback sentence #1252 removed (butler is a delegate
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
        "END YOUR TURN",
        "retry the SAME call with EXACTLY the same arguments",
    ]
    for anchor in doctrine_anchors:
        assert anchor in text, (
            f"butler system.md missing v0.77.0 protected-tool doctrine "
            f"anchor: {anchor!r}"
        )
    assert "I won't run this action without your approval." not in text
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
    """The configurator's entry in the assistant's delegates.yaml (since
    v0.351.0 the configurator is a delegate, and this card is what the
    `<delegates>` block serves a bundle-bound assistant)."""
    import yaml

    path = _system_md_path().parent.parent / "delegates.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    for entry in doc.get("delegates", []):
        if entry.get("agent") == "configurator":
            return entry
    raise AssertionError("configurator card not found in delegates.yaml")


def _system_md_section(text: str, heading: str) -> str:
    """The body of one `## <heading>` section of system.md (up to the next
    `## `), so a lifecycle claim can be pinned in the section that carries it
    rather than anywhere in the file."""
    marker = f"## {heading}\n"
    start = text.index(marker) + len(marker)
    end = text.find("\n## ", start)
    return text[start:] if end == -1 else text[start:end]


def _clauses(text: str) -> list[str]:
    """Lowercased, markdown-stripped clauses: a clause ends at a period or a
    semicolon, so a subject and its qualification must sit together and an
    unrelated mention elsewhere in the carrier cannot satisfy a predicate."""
    plain = _collapse_ws(text).lower().replace("*", "").replace("`", "")
    return re.findall(r"[^.;]+", plain)


def _n(text: str, *patterns: str) -> int:
    """Count the clauses of `text` matching EVERY pattern (#927 red case,
    specified by Astra: counts, not statuses)."""
    return sum(
        1 for clause in _clauses(text)
        if all(re.search(pat, clause) for pat in patterns)
    )


def _resident_apply_targets() -> tuple[str, ...]:
    """`resident:<slot>` for every fixed slot — derived from role_slot, never
    hand-listed, so a fourth resident widens the assertion automatically."""
    from role_slot import FIXED_RESIDENT_SLOTS
    return tuple(f"resident:{slot}" for slot in FIXED_RESIDENT_SLOTS)


PERSONA_VERBS = ("install", "apply", "reset", "list", "remove", "prune")


def _assert_persona_routing_current(text: str, carrier: str) -> None:
    """#927: every carrier of the assistant-facing persona routing sentence
    must state the configurator's REAL persona verb set — `persona_list`,
    `persona_remove` and `persona_prune` have been configurator tools since
    v0.203.0 — route LISTING installed personas to the configurator as a
    stated exception to the read-only guard (the assistant holds no persona
    tool and nothing that enumerates a directory), name every fixed resident
    slot and installed specialists as apply targets, and say image-default
    personas are never removable. Predicates are clause-bound (see
    `_clauses`); each is asserted as a count."""
    stale = len(re.findall(r"no upgrade and no uninstall",
                           _collapse_ws(text).lower()))
    assert stale == 0, (
        f"{carrier}: still claims personas have no upgrade and no uninstall "
        f"({stale} occurrence(s)); persona_remove/persona_prune are "
        "configurator tools"
    )
    assert _n(text, r"\bpersonas?\b",
              *(rf"\b{verb}\b" for verb in PERSONA_VERBS)) >= 1, (
        f"{carrier}: no single clause states the full persona lifecycle "
        f"{PERSONA_VERBS}"
    )
    assert _n(text, r"\bread-only\b", r"\banswer directly\b") >= 1, (
        f"{carrier}: the read-only/factual-question guard is missing"
    )
    assert _n(text, r"\bexcept(?:ion)?\b", r"\bread-only\b",
              r"\blist(?:ing)?\b", r"\binstalled personas\b",
              r"\bconfigurator\b") >= 1, (
        f"{carrier}: listing installed personas is not stated as an "
        "exception to the read-only guard routed to the configurator"
    )
    for target in _resident_apply_targets():
        assert _n(text, r"\bapply\b", r"\binstalled persona\b",
                  re.escape(target) + r"\b") >= 1, (
            f"{carrier}: {target} is not named as an apply target"
        )
    assert _n(text, r"\bapply\b", r"\binstalled persona\b",
              r"\binstalled specialists?\b") >= 1, (
        f"{carrier}: installed specialists are not named as an apply target"
    )
    assert _n(text, r"\bimage[- ](?:default|shipped) personas?\b",
              r"\bnever removable\b") >= 1, (
        f"{carrier}: does not say image-default personas are never removable"
    )
    assert _n(text, r"\bimage[- ](?:default|shipped) personas?\b",
              r"\b(?:can|may) be (?:removed|pruned)\b") == 0, (
        f"{carrier}: promises removal of an image-default persona"
    )


def test_configurator_card_routes_repo_installs_with_correct_lifecycles():
    """v0.103.0 doctrine reconcile: the configurator card must (a) route
    repository installs of an existing component to ITSELF, (b) keep the
    read-only/factual-question non-dispatch guard, and (c) enumerate each
    component kind's OWN lifecycle verbs — for personas, the configurator's
    real set: install/apply/reset/list/remove/prune (#927; the old
    "no upgrade and no uninstall" wording predates v0.203.0's removal tools).
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
    # #927: the card is the ONLY persona routing text a bundle-bound
    # assistant is served (rendered verbatim into <executors>), and it used
    # to say personas have NO upgrade and NO uninstall — false since
    # v0.203.0 gave the configurator persona_list/persona_remove/
    # persona_prune. The full verb set, the listing exception, the apply
    # targets and the image-default exclusion are pinned here.
    _assert_persona_routing_current(f"{card['purpose']}\n{card['when']}",
                                    "configurator card")

    # Naming the residents as apply targets must NOT have been done by
    # granting delegation or a persona tool: delegates.yaml is the delegation
    # ACL (INV-ENG-007) and the assistant ROUTES persona work, it holds none
    # of the persona tools. Green at the base; regression guards.
    import yaml
    delegates_doc = yaml.safe_load(
        (_system_md_path().parent.parent / "delegates.yaml")
        .read_text(encoding="utf-8"))
    assert [entry["agent"] for entry in delegates_doc["delegates"]] == [
        "butler", "configurator"]
    assistant_role = yaml.safe_load(
        (_system_md_path().parents[3] / "roles/resident/assistant/role.yaml")
        .read_text(encoding="utf-8"))
    assert sum("persona_" in tool
               for tool in assistant_role["tools"]["allowed"]) == 0

    # (d) reset is residents-only and restores the image default — the card
    #     must not imply a specialist reset or that reset is a persona-content
    #     operation (resident_persona_reset resets a resident to its default).
    assert re.search(r"reset[^.]*resident", low), (
        "card must scope persona reset to residents"
    )
    assert re.search(r"(residents-only|resident[- ]only)", low) or re.search(
        r"reset a resident", low
    ), "card must state reset is residents-only"

    # (e) the card hands the person's words on, process instructions verbatim,
    #     and says the answer comes back to be told in the resident's own words.
    assert "verbatim" in low
    assert "tell the person what it says, in your own words" in low
    assert "relay_answer" not in low




def _shipped_setup_prompts() -> list[Path]:
    """Every bundled prompt/doctrine file that speaks about the plugin
    setup-tool hand-back — the three surfaces the #443 incident traversed."""
    root = Path(__file__).resolve().parent.parent
    base = root / "casa/rootfs/opt/casa/defaults"
    paths = [
        base / "agents/assistant/prompts/system.md",
        base / "configurator/doctrine/recipes/plugin/add.md",
        base / "configurator/doctrine/recipes/plugin/update.md",
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

def test_create_vs_install_distinction_pinned(system_md_text):
    """The create-vs-install distinction must stay explicit in BOTH the
    configurator card and the system prompt: a NEW plugin from scratch is not
    something Casa builds; install an EXISTING repository component (specialist/
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
        r"does not build[^.;]*new[^.;]*plugin", card_low
    ), "configurator card must say Casa does not build a NEW plugin"
    assert "plugin-developer" not in card_low

    # system.md: the install section's routing sentence must bind the
    # existing-component install to the literal "configurator job" (not merely
    # any later 'configurator' mention); building a new plugin is declined.
    assert re.search(
        r"already-published component[^.]*configurator job", system_low
    ), "system.md install section must bind existing-component installs to a 'configurator job'"
    assert re.search(
        r"(create|build)[^.;]*new plugin[^.;]*not something casa does", system_low
    ), "system.md must say building a NEW plugin is not something Casa does"
    assert "plugin-developer" not in system_low

    # system.md must not reintroduce the decline wording.
    assert "not yet supported" not in system_low, (
        "system.md must not decline installs as 'not yet supported'"
    )
    # #927: system.md is the composed fallback (not served to a bundle-bound
    # resident, INV-PERS-012) and the configurator definition's description
    # has no runtime reader — both are pinned so the three carriers keep
    # saying the same thing as the served card.
    _assert_persona_routing_current(system_md_text, "system.md")
    # The full lifecycle must be stated in BOTH system.md sections that route
    # persona work, not once somewhere in the file.
    for heading in ("Configuration requests",
                    "Installing an existing component from a repository"):
        section = _system_md_section(system_md_text, heading)
        assert _n(section, r"\bpersonas?\b",
                  *(rf"\b{verb}\b" for verb in PERSONA_VERBS)) >= 1, (
            f"system.md section {heading!r} does not state the full persona "
            f"lifecycle {PERSONA_VERBS}"
        )


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
            if surface == "guest":
                continue  # a specialist-only surface; no resident is served it
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
#
# MOVED 2026-09-10 (#934), the three `assistant:*` carriers ONLY. #934 adds one
# sentence to the assistant's Core doctrine about answering a "why is this
# plugin not working" question from the standing entries before the setup
# history. Regenerated per carrier after measuring the residual diff, not from
# the candidate: `difflib` over base-vs-new residual text reports exactly ONE
# `insert` on each of the three, and the inserted span is that sentence and
# nothing else; the six butler and concierge carriers are byte-identical. The
# sentence makes no retention claim on any surface, which is what this pin
# guards.
#
# MOVED 2026-09-15 (plugin-install flow, batch 1), the three `assistant:*`
# carriers ONLY. The Core doctrine gains one paragraph: briefing an install
# never makes an unwired secret the operator's job, and relaying a completion
# never states a fact the completion did not state. Measured with `difflib`
# over base-vs-new residual text: exactly ONE `insert` of 75 words on each of
# the three assistant carriers, that paragraph and nothing else; the six
# butler and concierge carriers byte-identical. No retention claim anywhere.
#
# MOVED 2026-09-17 (#1021), the three `assistant:*` carriers ONLY. The Core
# doctrine gains one sentence: a setup tool's result that names values for the
# configurator to wire is handed to the configurator, and the setup run does
# not wire them itself. Measured with `difflib` over base-vs-new compiled text,
# word by word: exactly ONE `insert` on each of the three assistant carriers,
# that sentence and nothing else; the six butler and concierge carriers
# byte-identical. No retention claim anywhere.
# MOVED 2026-09-17 (background jobs), the three `assistant:*` carriers ONLY.
# (Re-measured against v0.320.0 after the merge; same two inserts.)
# Core gains one paragraph (33 words): a failed delegation whose message lists
# the tools the specialist called is never reported as having changed nothing.
# Text gains one paragraph (81 words): a request matching a listed background
# job is started with `start_job`. Measured with `difflib` over base-vs-new
# residual text: exactly those inserts (one on restricted_webhook and voice,
# two on text) and nothing else; the six butler and concierge carriers
# byte-identical. No retention claim anywhere.
# MOVED 2026-09-26 (#1048), the three `assistant:*` carriers ONLY. Core gains
# one paragraph: the credential rule covers what a tool returns (a mailbox, a
# tool result, a completion stay under it), so a sign-in link a person pasted
# is passed to the operation that consumes it. Text gains
# one paragraph: a topic in the Engagements supergroup is named only when a
# call returned an engagement for it. Measured: stripping exactly those two
# paragraphs from the new compiled text gives the base text on every carrier
# (one insert on restricted_webhook and voice, two on text); the six butler
# and concierge carriers byte-identical. No retention claim anywhere.
# MOVED 2026-09-27 (#1047), ALL NINE carriers. The kernel's credential line
# gains one clause naming a plugin-declared vault drop-off as a consumer, on
# every resident; the assistant's Core paragraph from #1048 is REPLACED by the
# drop-off paragraph (a pasted link no longer rides in a brief). Measured:
# replacing the new paragraph with the #1048 one and removing the clause gives
# the base text on every carrier, byte for byte. No retention claim anywhere.
# MOVED 2026-09-30 (#1116), the `assistant:text` carrier ONLY. The Text
# projection's beat-per-step paragraph gains one sentence: when a turn's own
# instructions say it is a scheduled or background check, or that nothing is
# seen until the turn ends, give no beats and deliver only what those
# instructions ask for. Measured with `difflib` over base-vs-new compiled text,
# word by word: exactly ONE `insert` on `assistant:text`, that sentence and
# nothing else; the other eight carriers byte-identical. No retention claim
# anywhere.
# MOVED 2026-09-30 (#1117), the three `assistant:*` carriers ONLY. The Core
# doctrine paragraph gains one sentence after "trust live tool results over
# stale memory": before stating changeable household or device state as
# current, read it live in this turn or say it has not been checked; a recalled
# memory is history, not verification. Measured with `difflib` over base-vs-new
# compiled text, word by word: exactly ONE `insert` per assistant carrier,
# that sentence and nothing else; the six butler and concierge carriers
# byte-identical. No retention claim anywhere.
# MOVED 2026-10-03 (S7a, B2), the `assistant:text` carrier ONLY. The Text
# projection gains one paragraph: delegate what a delegate owns; a result or a
# completion carrying Casa's post lines is not retold, and one that says
# nothing beyond them ends silent as Casa's note under them says. Measured with
# `difflib` over base-vs-new compiled text, word by word: exactly ONE `insert`
# on `assistant:text`, that paragraph and nothing else; the other eight
# carriers byte-identical. No retention claim anywhere.
# MOVED 2026-10-04 (#1228), `assistant:text` and `assistant:voice` ONLY. The
# Text projection's jobs paragraph sends a job listed as a delegate's own to
# that delegate by a sync delegation, and its last rule is narrowed to jobs that
# are not a delegate's own; the topic paragraph admits a delegate's reported job
# start; the Voice projection gains one paragraph: no background job starts on a
# call. Measured with `difflib` over base-vs-new compiled text, word by word:
# five non-equal opcodes on `assistant:text`, all inside those two paragraphs,
# one `insert` on `assistant:voice`, that paragraph and nothing else; the other
# seven carriers byte-identical. No retention claim anywhere.
# MOVED 2026-10-04 (#1071), the six `assistant:*` and `butler:*` carriers ONLY.
# The Core doctrine of both gains one paragraph: a protected-tool denial refuses
# the one call it names, not the tool; a later operator request is made or
# delegated and follows Casa's authorization result. Measured with `difflib`
# over base-vs-new compiled text, word by word: exactly ONE `insert` per
# assistant and butler carrier, that paragraph and nothing else; the three
# concierge carriers byte-identical. No retention claim anywhere.
# MOVED 2026-10-07 (#1315), the `assistant:text` carrier ONLY. The Text
# projection gains one paragraph: a message meant for something a delegate owns
# is delegated in the person's own words, quoted exactly in `task=`, before any
# clarifying question the delegate could settle. Measured with `difflib` over
# base-vs-new compiled text, word by word: exactly ONE `insert` on
# `assistant:text`, that paragraph and nothing else; the other eight carriers
# byte-identical. No retention claim anywhere.
# MOVED 2026-10-08 (#1332), the `assistant:text` carrier ONLY. The Text
# projection gains one paragraph, after the already-posted rule: a delegate's
# answer is the whole reply, passed on in its own words and attributed by name,
# with nothing of the resident's own around it. Measured with `difflib` over
# base-vs-new compiled text, word by word: exactly ONE `insert` on
# `assistant:text`, that paragraph and nothing else; the other eight carriers
# byte-identical. No retention claim anywhere.
# MOVED 2026-10-08 (#1348), the `assistant:text` carrier ONLY. The Text
# projection gains one paragraph, after the #1332 one: a late answer is passed
# on the same way, a pending delegation gets one short line, and nothing is
# written about a question `ask_user` posted. Measured with `difflib` over
# base-vs-new compiled text, word by word: exactly ONE `insert` on
# `assistant:text`, that paragraph and nothing else; the other eight carriers
# byte-identical. No retention claim anywhere.
# MOVED 2026-10-08 (#1353), the `assistant:text` carrier ONLY. The #1348
# paragraph's holding line gains "with nothing added: not the task again, not
# what you have done, not what will happen next" (Opus 5.5 added a promise or
# restated the task). Measured with `difflib` over base-vs-new compiled text,
# word by word: exactly ONE `replace` on `assistant:text` ("it." -> "it.",
# plus that clause) and nothing else; the other eight carriers byte-identical.
# MOVED 2026-10-09 (#1373), the `assistant:text` carrier ONLY. The Text
# projection gains one paragraph, before the retention one: asked to remove all
# the closed topics, the assistant runs the full purge herself after a dry run,
# the count in an `ask_user` question and the person's confirmation. Measured
# with `difflib` over base-vs-new compiled text, word by word: exactly ONE
# `insert` on `assistant:text`, that paragraph and nothing else; the other
# eight carriers byte-identical. No retention claim anywhere.
# MOVED 2026-10-09 (#1373 r1), the three `assistant:*` carriers. The Core
# doctrine gains one paragraph: the all_terminal purge runs only in a Telegram
# text conversation; elsewhere the person is asked to make the request in
# Telegram. Measured with `difflib` over base-vs-new compiled text, word by
# word: exactly ONE `insert` on `assistant:voice` and on
# `assistant:restricted_webhook`, that paragraph; on `assistant:text` two
# inserts, that paragraph and the one above; the six other carriers
# byte-identical. No retention claim anywhere.
# MOVED 2026-10-09 (#1386), `assistant:text` only: the purge paragraph's
# confirm sentence gains the one-line "Deleting N topics…" notice, and its
# unrecorded-topics clause is said only when asked. One `replace` inside that
# paragraph; the eight other carriers byte-identical. No retention claim.
# MOVED 2026-10-10 (#1429), `assistant:text` only: the late-answer paragraph
# gains one sentence after "is passed on.": a delegate's line above Casa's post
# lines that only announces, points to or describes those posts is not passed
# on. Measured with `difflib` over base-vs-new compiled text, word by word:
# exactly ONE `insert` on `assistant:text`, that sentence; the eight other
# carriers byte-identical. No retention claim.
# MOVED 2026-10-14 (#1437), the three `assistant:*` carriers. The Core
# doctrine's completion-relay paragraph gains one sentence: never offer or
# propose to have a plugin assigned to you unless the person asked for that
# plugin's tools. Measured with `difflib` over base-vs-new compiled text, word
# by word: exactly ONE `insert` on each `assistant:*` carrier, that sentence;
# the six other carriers byte-identical. No retention claim.
# MOVED 2026-10-18 (browser executor Plan 3), `assistant:text` only: the Text
# projection gains one paragraph before the Telegram-retention paragraph — web
# pages are read only by the browsing specialist, open-web delegations carry
# only what the task needs and never contact details, runs start on the
# person's words or Casa's question, specialists settle between themselves
# first, say what was chosen. Exactly ONE `insert` on `assistant:text`; the
# eight other carriers byte-identical. No retention claim.
# MOVED 2026-10-31 (browser executor slice 5h), `assistant:text` only: that
# browsing paragraph gains one sentence — a browsing task is one `sync`
# delegation, also when the person asks to be asked first, since the browsing
# specialist asks them itself in this chat. Measured with `difflib` over
# base-vs-new compiled text, word by word: exactly ONE `insert` on
# `assistant:text`, that sentence; the eight other carriers byte-identical. No
# retention claim.
# MOVED 2026-11-01 (browser executor slice 5l), `assistant:text` only: the
# #1332 relay paragraph is rewritten — a delegate's answer is passed on with
# relay_answer (Casa posts it under the delegate's name), passing it on by hand
# ("Alex: …") only when relay_answer cannot post it. ONE `replace` on
# `assistant:text`, that paragraph; the eight other carriers byte-identical. No
# retention claim.
# MOVED (memory serving), the three `assistant:*` carriers. The Core doctrine
# gains three paragraphs, one block: his health, money and family are his to
# bring up; a current value in memory is used without asking him to confirm
# it; a day's saved conversation can be read with `browse_memory_day`.
# Measured with `difflib` over base-vs-new compiled text, word by word:
# exactly ONE `insert` on each `assistant:*` carrier, that block; the six
# other carriers byte-identical. No retention claim.
# MOVED (memory serving, butler), the three `butler:*` carriers. The butler's
# Core doctrine gains two paragraphs, one block: a current value in memory is
# used rather than asking the person to repeat it; the part of a day's saved
# conversation the butler is cleared to see can be read with
# `browse_memory_day`. Measured with `difflib` over base-vs-new compiled text,
# word by word: exactly ONE `insert` of 67 words on each `butler:*` carrier,
# that block; the six other carriers byte-identical. No retention claim.
# MOVED (v0.347.1), the three `assistant:*` carriers. The Core paragraph on
# a failed delegation keeps "never say nothing happened or nothing changed"
# and now asks for plain words: that it stopped before finishing, what it may
# already have done that matters to the person, and what they can do now; the
# list of tools it called is for the assistant to judge by, never passed on
# (on casa-test the assistant passed on "read pages about 28 times, done
# around 21 searches"). Measured with `difflib` over base-vs-new compiled
# text, word by word: one insert and two replaces inside that paragraph on
# each `assistant:*` carrier and nothing else; the six other carriers
# byte-identical. No retention claim.
# MOVED 2026-11-03 (the Stop), `assistant:text` only: one paragraph before the
# relay paragraph — to stop or cancel work a specialist carries on by itself
# (browsing), call stop_work yourself and say it in one plain line, never
# delegate a stop, and ask which one when several run and his words do not say.
# ONE `insert` on `assistant:text`, that paragraph; the eight other carriers
# byte-identical. No retention claim.
# MOVED (v0.347.3), `assistant:text` only: that stop paragraph now says to
# ask which one first when several run and his words do not say, and otherwise
# write nothing to the person until the work is stopped, then one plain line
# of what stopped, never how it was found or what else runs (on casa-test the
# assistant posted "Only one thing running, so that's the one." between its
# two stop_work calls). One paragraph changed on `assistant:text`; the eight
# other carriers byte-identical. No retention claim.
# MOVED (interactive specialist engagements retired), the three `assistant:*`
# carriers. Core's sign-in-link paragraph: with no drop-off, the person gives
# the link to the specialist by replying to one of its messages in this chat,
# not in an interactive delegation's topic (on every `assistant:*` carrier).
# `assistant:text` also: a delegate's own job is asked for in `sync` mode (the
# "never in `interactive`" clause goes); the unopened-topic paragraph says a
# delegation opens no topic and that talking with a specialist is a `sync`
# delegation relayed with relay_answer, carried on by replying to its message;
# the browsing paragraph loses its "never a reason for an `interactive`
# delegation" clause. Measured with `difflib` over base-vs-new compiled text,
# word by word: one insert and one replace inside the sign-in paragraph on each
# `assistant:*` carrier, and on `assistant:text` the changes inside those three
# paragraphs and nothing else; the six other carriers byte-identical. No
# retention claim.
# MOVED (v0.351.0, the configurator becomes a delegate), the
# three `assistant:*` carriers, core doctrine only: "task-bounded executors"
# becomes "the configurator"; "the executor reported" becomes "the delegate
# reported"; the setup-report rule says "delegate that report to the
# configurator" instead of "engage the configurator with that report". The six
# other carriers byte-identical. No retention claim.
# MOVED (a job in the direct chat), `assistant:text` only: the job paragraph's
# "progress appears in the specialist's topic; they can write there between
# batches or /cancel it" becomes: its questions and its result come to this
# chat, progress appears in the topic, the person steers it by replying here
# to the job's messages and stops it with its Stop key at the keyboard
# or by asking (`cancel_engagement`). One paragraph changed on
# `assistant:text`; the eight other carriers byte-identical. No retention
# claim.
# MOVED (a job is one long session), `assistant:text` only: "A delegation does
# run that job, its batches included" becomes "A delegation does run that whole
# job". One sentence changed on `assistant:text`; the eight other carriers
# byte-identical. No retention claim.
# MOVED (agent logs; jobs without the forum group), `assistant:text` only: the
# Text projection's first line drops "topic" from "never invent Telegram topic
# links"; the job paragraph says progress appears in the agent's log when the
# result's message says so, not in the specialist's topic; the unopened-topic
# paragraph becomes "A job lives in this chat: its questions, its result and
# its Stop key come here." followed by the unchanged talk-with-a-specialist
# sentences, its topic sentences gone. Measured with `difflib` over
# base-vs-new compiled text, word by word: eight non-equal opcodes on
# `assistant:text`, all inside those three paragraphs; the eight other carriers
# byte-identical. No retention claim.
# REMOVED (the topic cleanup tool is gone), all three `assistant` carriers:
# the core paragraph limiting the purge to a Telegram text conversation goes
# from every assistant projection, and the text projection also loses the
# purge paragraph. Nothing added; the six other carriers byte-identical. No
# retention claim.
# MOVED (the resident tells a delegate's answer herself), `assistant:text`
# only, three paragraphs: the relay paragraph now says to tell the answer in
# her own words, keeping every fact, amount, date, name and question exactly,
# never under the delegate's name; the late-answer paragraph loses its sentence
# on a connection report passed on under the delegate's name; the
# talk-with-a-specialist sentence says to tell its answer in her own words and
# that the person carries on by writing to her. Measured with `difflib` over
# base-vs-new compiled text, word by word: 19 non-equal opcodes on
# `assistant:text`, all inside those three paragraphs; the eight other carriers
# byte-identical. No retention claim.
# MOVED 2026-11-11 (the resident closes the loop on a task shown in a
# delegate's own chat), `assistant:text` only: two sentences after the
# already-posted paragraph's last one ("…relay or narrate the outcome as
# before."). When Casa's note says the posts went to the person in the
# delegate's own chat, she closes the loop herself in a short line and stays
# silent only when nothing is worth saying; when the answer is already in that
# chat, she closes in a line or two without retelling it. Measured with
# `difflib` over base-vs-new compiled text, word by word: exactly ONE `insert`
# on `assistant:text`, those two sentences; the eight other carriers
# byte-identical. No retention claim.
# MOVED (a reply in the resident's chat is her own turn), the three
# `assistant:*` carriers, core doctrine only: the sign-in paragraph's last
# sentence no longer sends the person to reply to the specialist's message in
# this chat. With no drop-off declared, the step cannot be finished through
# her: a specialist with its own chat gets the link there, in that agent's own
# chat; one that works behind her needs a vault drop-off, and without one she
# says it cannot be done safely. Measured with `difflib` over base-vs-new
# compiled text, word by word: five non-equal opcodes on each `assistant:*`
# carrier, all inside that sentence; the six other carriers byte-identical. No
# retention claim.
# MOVED (a delegate asked about its own job), `assistant:text` only: the
# background-jobs paragraph ends with one sentence telling her to put what she
# already knows of the job's state (whether it ended, how, and when) in the
# delegation's `context=`, since the delegate starts fresh and a job's end is
# told to her. Measured with `difflib` over base-vs-new compiled text, word by
# word: exactly ONE `insert` on `assistant:text`, that sentence; the eight
# other carriers byte-identical. No retention claim.
# MOVED (a pending delegation the person can watch), `assistant:text` only:
# the pending-delegation sentence is followed by one sentence saying the
# "still on it" line is only for work the person cannot see, and that when
# Casa's note says they are watching it in the delegate's own chat she writes
# nothing about it. Measured with `difflib` over base-vs-new compiled text,
# word by word: exactly ONE `insert` on `assistant:text`, that sentence; the
# eight other carriers byte-identical. No retention claim.
# MOVED (a specialist's handle), `assistant:text` only: one paragraph before
# the retention paragraph says she chooses a specialist's bot handle with
# `offer_own_chat`, can give it a different handle (check, then replace), and
# should do so even if earlier in the conversation it looked as if she could
# not. The eight other carriers byte-identical. No retention claim.
# MOVED (a specialist moved back, a bot renamed), `assistant:text` only: in
# that same paragraph, before its last sentence, two sentences say a handle
# the check reports `used_before` moves the specialist back to a bot it had,
# and that the `rename` mode changes the name at the top of its chat. The
# eight other carriers byte-identical. No retention claim.
# MOVED (the Stop key at the keyboard), `assistant:text` only: the job
# paragraph says the person stops a job "with its Stop key at the keyboard"
# where it said "with the Cancel button on its start line", and the
# job-lives-here paragraph says "its Stop key" where it said "its Cancel
# button". The eight other carriers byte-identical. No retention claim.
_RESIDUAL_DIGESTS = {
    "assistant:restricted_webhook":
        "f7f8d0dcbbc197ff0349557cbebe0f95e5e360377030dacd09f38cf61ab7e335",
    "assistant:text":
        "100940f2e893f33779a8547a9fe36c0e639606789aa6cb0505ff7ad3791d5148",
    "assistant:voice":
        "71805d1c8fd3a8ef47a7de718e60db1123752388d8203a66e358eedac7b007d1",
    "butler:restricted_webhook":
        "817241ec5da254b2fef07d2d0e6c7db0073bf1965a28be8d9b6818e59bc7f60b",
    "butler:text":
        "01472c7a4afeedb6aad34d2cff103440486c015062c348cb85b605a0b360d632",
    "butler:voice":
        "512f53954dae8711a83ad25b2aaec3263b71a5916f53dc59e8938925992c4c9f",
    "concierge:restricted_webhook":
        "20eaa4bac29d6149a4623b3c6b162d7645ab2e15f4e633f703aab615a4df3f12",
    "concierge:text":
        "ddbea3a48cae0d0dab3266c2a51cd962e5d5f091c7222bed6cf214dd24751456",
    "concierge:voice":
        "21900b3787b24545667d9ca63cbf37c0ade418b8d29d6c819ae091b8dc760220",
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
        guest=CompiledProjection(system_prompt="stub guest", digest="stub-guest",
                                 estimated_tokens=0),
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


# ---------------------------------------------------------------------------
# #934 red case — the assistant is handed the blocking fact FIRST and is told
# nothing about selecting it.
#
# `plugin_status` returns `{ok, standing, history}` and the standing order is
# STRUCTURAL: `res.issues` (carrying the verify-stage row) precede the merged
# setup-episode rows (`tools.py:12057-12061`), and `plugin_health.describe_issue`
# renders each row with the detail its own comment calls "the one actionable
# fact in the row". So `standing[0]` already held the answer. What the operator
# got instead was a paraphrase of the LAST sentence — the setup history — and
# "the cause is unknown", because no shipped carrier says how to choose: the
# tool's description led with the setup-episode narrative and the assistant's
# Core doctrine said nothing about plugins at all.
#
# This DECLARES an invariant (D34) rather than pinning a prior one. It asserts
# PRESENCE and ORDER of the telling in the served carriers, and asserts nothing
# about whether the model then selects correctly — an answer-selection defect is
# not measurable at unit level, and the playbook's plugin-status play is the
# measurement.
#
# Scoped to the ASSISTANT: `plugin_status` is granted to the assistant alone
# (`defaults/agents/assistant/runtime.yaml:25`), so this imposes nothing on the
# butler or the concierge.
#
# The telling names what the plugin is WAITING FOR, never the identifier naming
# it, because the Text projection this Core sentence is carried into already
# forbids putting environment-variable names and "raw fields copied out of a
# tool result" in front of a household member (doctrine.md:14) and asks for "the
# thing they have, not the thing you lack" (:16) — and the row to lead from is
# literally `fx-setup is waiting for a setup-provided value to be wired in — CASA_PLUGIN_FX_BIN`.
#
# Specified by the red-case reviewer (Astra), 2026-09-10, before any production
# change. Round 1's strings were RETURNED by this stream and re-specified: they
# described `standing` as only what a plugin is "waiting for", which is a strict
# subset of its phrase table (`plugin_health.py:324-361` renders "could not be
# loaded", "clashes with another plugin's name", an open suffix family and the
# "is not working" fallback) and of the corpus claim that the tool reports "the
# whole standing set unfiltered" (`docs/architecture/plugin-health.md:86`).
# ---------------------------------------------------------------------------

_PLUGIN_STANDING_DOCTRINE = (
    "When someone asks why a plugin is not working, answer first with what is "
    "standing in that plugin's way now — the value or approval it is waiting "
    "for, or what it could not do — and only then with what its automatic "
    "setup already tried."
)

_PLUGIN_STATUS_SENTENCES = (
    "Answer an operator asking why a plugin is not working from `standing` "
    "first: it reports what is currently wrong with each installed plugin — "
    "what is blocking it right now, such as a value it is still waiting for.",
    "`history` comes after, and says what happened during each plugin's "
    "automatic setup, including the error a failed setup last reported.",
    "Read-only: it changes nothing, and it answers the question without "
    "engaging the configurator.",
)


def _plugin_telling_pattern(sentence):
    plain = _collapse_ws(sentence).strip().lower()
    plain = plain.replace("*", "").replace("`", "")
    return re.escape(plain.removesuffix("."))


def test_plugin_standing_telling_reaches_assistant_core_and_projections():
    """RED pre-fix: the telling occurs zero times in Core and in all three
    compiled assistant projections.

    Asserted on `_compiled_resident_carriers()` — the PRODUCTION compiler —
    because a compiler that dropped Core would leave a `select_markdown_sections`
    pin green while every real projection lost the rule. Core is asserted too,
    so a telling smuggled into one projection's own section cannot pass."""
    from markdown_sections import select_markdown_sections
    from prompt_compiler import _PROJECTION_HEADINGS

    carriers = [
        (name, body)
        for name, body in _compiled_resident_carriers()
        if name.startswith("assistant:")
    ]
    # Check multiplicity before constructing any dictionary.
    assert sorted(name for name, _ in carriers) == [
        "assistant:restricted_webhook",
        "assistant:text",
        "assistant:voice",
    ]

    doctrine = (
        _casa_root()
        / "defaults/roles/resident/assistant/doctrine.md"
    ).read_text(encoding="utf-8")
    core = select_markdown_sections(
        doctrine, ("Core doctrine",), exclude=_PROJECTION_HEADINGS
    )

    pattern = _plugin_telling_pattern(_PLUGIN_STANDING_DOCTRINE)
    counts = [
        _n(body, pattern) for _, body in sorted(carriers)
    ] + [_n(core, pattern)]
    assert counts == [1, 1, 1, 1]


def test_plugin_status_description_leads_with_standing():
    """RED pre-fix: the description leads with the setup-episode narrative.

    The full-description equality is what makes this a pin rather than a
    presence check: counts alone accept reordering, and a first-sentence pin
    alone permits changes that narrow or remove the later promises (the setup
    history, the last reported error, read-only, no configurator)."""
    import tools

    description = _collapse_ws(tools.plugin_status.description).strip()
    sentences = [
        _collapse_ws(sentence).strip()
        for sentence in _PLUGIN_STATUS_SENTENCES
    ]

    counts = [
        _n(description, _plugin_telling_pattern(sentence))
        for sentence in sentences
    ]
    assert counts == [1, 1, 1]
    assert description.startswith(sentences[0])
    assert description == " ".join(sentences)


# ---------------------------------------------------------------------------
# #1048 red case — a sign-in link the operator pasted by hand, and a topic
# that was never opened.
#
# The safety kernel's credential rule (`safety-kernel.md:7`) governs "a
# credential-bearing artifact a tool returns". Nothing in a shipped assistant
# carrier says what it does NOT govern, so the assistant applied it to a link
# the operator pasted into the DM and refused to hand it to the specialist's
# sign-in tool — the one route a sync delegation leaves open. Every assistant
# sentence that names an Engagements topic is conditioned on an
# engagement-creating call, and none says a sync delegation opens no topic, so
# it twice sent the operator to a topic that did not exist.
#
# This DECLARES the two tellings; it asserts PRESENCE and PROVENANCE in the
# served carriers and nothing about the model obeying them.
# ---------------------------------------------------------------------------

# #1047 supersedes #1048's telling (operator ruling on #1047): a pasted link
# no longer rides in a delegation brief either — every sign-in link goes to
# the consuming plugin's vault drop-off, and only the fact that it is waiting
# reaches the specialist.
_DROP_OFF_DOCTRINE = (
    "A sign-in link or one-time code is under the rule on credential-bearing "
    "artifacts wherever it came from: a tool result, a mailbox, a completion, "
    "or a person's own message to you. It never goes into a reply, a "
    "delegation brief or a completion summary. When the plugin that consumes "
    "it declares a vault drop-off, store it there with `vault_drop_off`, then "
    "tell that plugin's specialist only that it is waiting and ask it to run "
    "its sign-in step. Read a mailbox for such a link only when the operator "
    "asks, in this conversation, for that specific email. When no drop-off is "
    "declared, the step cannot be finished through you. If that specialist "
    "has its own chat, tell the person to give it the link there, in that "
    "agent's own chat. If it works behind you, the link needs a vault "
    "drop-off; without one, tell the person it cannot be done safely."
)

_KERNEL_DROP_OFF_CLAUSE = (
    "Pass it only to the operation designated to consume it — a tool that "
    "takes it, or a vault drop-off its consuming plugin declares; if there is "
    "none, stop and report that the step could not be completed."
)

_UNOPENED_TOPIC_DOCTRINE = (
    "A job lives in this chat: its questions, its result and its Stop key "
    "come here. When the person wants to talk with a specialist themselves, or "
    "a step needs them to, delegate their words to it in `sync` mode and tell "
    "them its answer in your own words; they carry on by writing to you, and "
    "you delegate their next words the same way."
)


def _assistant_doctrine_sections() -> tuple[str, str]:
    from markdown_sections import select_markdown_sections
    from prompt_compiler import _PROJECTION_HEADINGS

    doctrine = (_casa_root() / "defaults/roles/resident/assistant"
                / "doctrine.md").read_text(encoding="utf-8")
    core = select_markdown_sections(
        doctrine, ("Core doctrine",), exclude=_PROJECTION_HEADINGS)
    text = select_markdown_sections(
        doctrine, ("Text projection",), exclude=_PROJECTION_HEADINGS)
    return core, text


def test_a_signin_link_goes_to_the_drop_off_on_every_assistant_surface():
    """#1047: the telling is on every assistant carrier (Core, so all three
    projections, and the legacy prompt) and on no other resident's; #1048's
    brief-forwarding sentence is gone from every carrier."""
    needle = _collapse_ws(_DROP_OFF_DOCTRINE)
    compiled = _compiled_resident_carriers()
    assert {name: _collapse_ws(body).count(needle)
            for name, body in compiled} == {
                f"{slot}:{surface}": int(slot == "assistant")
                for slot in _RESIDENT_SLOTS
                for surface in ("text", "voice", "restricted_webhook")}

    core, text = _assistant_doctrine_sections()
    assert (_collapse_ws(core).count(needle),
            _collapse_ws(text).count(needle)) == (1, 0)
    legacy = dict(_legacy_prompt_carriers())
    assert _collapse_ws(legacy["assistant"]).count(needle) == 1

    superseded = "in the brief of a delegation to that specialist"
    for name, body in [*compiled, *legacy.items()]:
        assert superseded not in _collapse_ws(body), name


def test_the_kernel_names_a_declared_drop_off_as_a_consumer():
    """#1047: the kernel's "operation designated to consume it" names a
    plugin-declared drop-off, on every resident carrier."""
    needle = _collapse_ws(_KERNEL_DROP_OFF_CLAUSE)
    assert {name: _collapse_ws(body).count(needle)
            for name, body in _compiled_resident_carriers()} == {
                f"{slot}:{surface}": 1
                for slot in _RESIDENT_SLOTS
                for surface in ("text", "voice", "restricted_webhook")}


def test_the_assistant_never_points_to_an_unopened_engagement_topic():
    """RED pre-fix: the telling occurs zero times on every carrier."""
    needle = _collapse_ws(_UNOPENED_TOPIC_DOCTRINE)
    compiled = _compiled_resident_carriers()
    assert {name: _collapse_ws(body).count(needle)
            for name, body in compiled} == {
                f"{slot}:{surface}": int(slot == "assistant"
                                         and surface == "text")
                for slot in _RESIDENT_SLOTS
                for surface in ("text", "voice", "restricted_webhook")}

    core, text = _assistant_doctrine_sections()
    assert (_collapse_ws(core).count(needle),
            _collapse_ws(text).count(needle)) == (0, 1)
    legacy = dict(_legacy_prompt_carriers())
    assert _collapse_ws(legacy["assistant"]).count(needle) == 1


# ---------------------------------------------------------------------------
# #1116 red case (b): the served assistant text doctrine's beat rule has an
# exception keyed on what the model can SEE in the turn.
#
# The runtime gives the model no buffered-turn marker; what it reads is the
# turn's own instructions, and Casa's buffered turns say what they are there
# (the heartbeat and briefing: nothing is seen until the turn ends; an event
# wake: "this is a background wake"). At the base the served `assistant:text`
# projection asked for one beat per step with no exception, and on a buffered
# turn that closes without a send that narration is delivered (#1075 row 3).
# Pinned through the REAL compiler; this pins the text served, not obedience.
# ---------------------------------------------------------------------------

_BACKGROUND_EXCEPTION = (
    "When a turn's own instructions say it is a scheduled or background "
    "check, or that nothing you write is seen until the turn ends, nobody "
    "is watching the steps: give no beats and do not announce what you "
    "are checking; deliver only what those instructions ask for."
)

_INTERACTIVE_BEAT = (
    "While a multi-step job is running, give one short beat per step; "
    "save the detail for whoever asks, and offer it rather than "
    "volunteering it."
)


def test_background_exception_reaches_only_assistant_text() -> None:
    carriers = _compiled_resident_carriers()
    expected_names = sorted(
        f"{role}:{surface}"
        for role in ("assistant", "butler", "concierge")
        for surface in ("text", "voice", "restricted_webhook")
    )
    assert len(carriers) == 9
    assert sorted(name for name, _ in carriers) == expected_names
    assert [
        _collapse_ws(body).count(_collapse_ws(_BACKGROUND_EXCEPTION))
        for _name, body in carriers
    ] == [int(name == "assistant:text") for name, _body in carriers]
    assert _collapse_ws(dict(carriers)["assistant:text"]).count(
        _collapse_ws(_INTERACTIVE_BEAT)) == 1


# ---------------------------------------------------------------------------
# S7a (B2): a result or completion carrying Casa's post lines is not retold.
# The rule reaches only the assistant's text projection (and the legacy
# carrier); the sentinel itself lives only in Casa's note under the lines,
# because a compiled doctrine may not carry markup.
# ---------------------------------------------------------------------------

_POSTED_DOCTRINE = (
    "The person has already seen what those lines name: do not retell it, and "
    "when the result says nothing beyond those posts, stay silent exactly as "
    "Casa's note under them says."
)


def test_the_already_posted_rule_reaches_only_the_assistant_text_projection():
    needle = _collapse_ws(_POSTED_DOCTRINE)
    compiled = _compiled_resident_carriers()
    assert {name: _collapse_ws(body).count(needle) for name, body in compiled} == {
        f"{slot}:{surface}": int(slot == "assistant" and surface == "text")
        for slot in _RESIDENT_SLOTS
        for surface in ("text", "voice", "restricted_webhook")}
    legacy = dict(_legacy_prompt_carriers())
    assert _collapse_ws(legacy["assistant"]).count(needle) == 1


# A task the person sees in a delegate's own chat was still asked of the
# resident: she closes the loop herself, briefly, instead of staying silent or
# retelling what the person already read there. The rule sits right after the
# already-posted rule, on the assistant's text projection and the legacy
# carrier only. No markup, because a compiled doctrine may not carry it.
_OWN_CHAT_CLOSE_DOCTRINE = (
    "When Casa's note says those posts went to the person in the delegate's "
    "own chat, the person still asked you here: close the loop yourself, "
    "usually in one short line on what came of it, and stay silent only when "
    "nothing at all is worth saying. When the answer is already in the "
    "delegate's own chat, close in a line or two; do not retell it."
)

_POSTED_PARAGRAPH_END = "Without such lines, relay or narrate the outcome as before."


def test_the_own_chat_close_rule_reaches_only_the_assistant_text_projection():
    needle = _collapse_ws(_OWN_CHAT_CLOSE_DOCTRINE)
    compiled = _compiled_resident_carriers()
    assert {name: _collapse_ws(body).count(needle) for name, body in compiled} == {
        f"{slot}:{surface}": int(slot == "assistant" and surface == "text")
        for slot in _RESIDENT_SLOTS
        for surface in ("text", "voice", "restricted_webhook")}
    assert {slot: _collapse_ws(body).count(needle)
            for slot, body in _legacy_prompt_carriers()} == {
        slot: int(slot == "assistant") for slot in _RESIDENT_SLOTS}


def test_the_own_chat_close_rule_ends_the_already_posted_paragraph():
    compiled = dict(_compiled_resident_carriers())
    legacy = dict(_legacy_prompt_carriers())
    for body in (compiled["assistant:text"], legacy["assistant"]):
        flat = _collapse_ws(body)
        anchor = _collapse_ws(_POSTED_DOCTRINE) + " " + _POSTED_PARAGRAPH_END
        assert flat.count(
            anchor + " " + _collapse_ws(_OWN_CHAT_CLOSE_DOCTRINE)) == 1


def test_casas_note_under_the_post_lines_names_the_silence_sentinel():
    import tools
    from output_boundary import SILENCE_SENTINEL
    assert tools.POST_ECHO_SILENCE_NOTE.count(SILENCE_SENTINEL) == 1
    assert "Do not retell them." in tools.POST_ECHO_SILENCE_NOTE


# ---------------------------------------------------------------------------
# #1315: a message meant for a delegate reaches it in the person's own words,
# delegated before any clarifying question. The rule reaches only the
# assistant's text projection and the legacy carrier, once each. Its live
# effect is measured by test-local/eval/ellen_delegation_fidelity.py.
# ---------------------------------------------------------------------------

_OWN_WORDS_DOCTRINE = (
    "When a message is meant for something a delegate owns — an answer to a "
    "question its plugin posted, a phrase its plugin asked the person to send, "
    "or a short request in its area — delegate it with the person's own words "
    "quoted exactly in `task=`, and put your reading of them, if any, in "
    "`context=`. Do not turn their words into a different action. When you "
    "cannot tell which of a delegate's actions they mean, delegate their words "
    "and let the delegate settle it; ask the person to choose only when the "
    "delegate's result asks, or when no delegate owns the request. A sign-in "
    "link or one-time code still never goes into a brief."
)


def test_the_own_words_rule_reaches_only_the_assistant_text_projection():
    needle = _collapse_ws(_OWN_WORDS_DOCTRINE)
    compiled = _compiled_resident_carriers()
    assert {name: _collapse_ws(body).count(needle) for name, body in compiled} == {
        f"{slot}:{surface}": int(slot == "assistant" and surface == "text")
        for slot in _RESIDENT_SLOTS
        for surface in ("text", "voice", "restricted_webhook")}
    legacy = dict(_legacy_prompt_carriers())
    assert _collapse_ws(legacy["assistant"]).count(needle) == 1


def test_the_eval_measures_the_shipped_rule():
    import ast
    path = Path(__file__).resolve().parents[1] / "test-local/eval/ellen_delegation_fidelity.py"
    text = path.read_text(encoding="utf-8")
    start = text.index("DOCTRINE_PARAGRAPH = (") + len("DOCTRINE_PARAGRAPH = ")
    shipped = ast.literal_eval(text[start:text.index("\n)\n", start) + 2])
    assert _collapse_ws(shipped) == _collapse_ws(_OWN_WORDS_DOCTRINE)


# ---------------------------------------------------------------------------
# A delegate's answer is told by the resident herself, in her own words: the
# delegate's "I" becomes its name, every fact, amount, date, name and question
# is kept exactly, and nothing goes out under the delegate's name. The rule
# reaches only the assistant's text projection and the legacy carrier, once
# each. Its live effect is measured by test-local/eval/ellen_own_words.py,
# whose LLM judge decides fidelity; no offline judge is pinned here.
# ---------------------------------------------------------------------------

CASA_ROOT = Path(__file__).resolve().parents[1] / "casa/rootfs/opt/casa"

RELAY_TEXT_SITES = ["defaults/agents/assistant/prompts/system.md",
                    "defaults/roles/resident/assistant/doctrine.md",
                    "defaults/agents/assistant/delegates.yaml",
                    "plugin_setup_episodes.py", "tools.py"]

_RELAY_DOCTRINE = (
    "When a delegate's result answers the person, tell them that answer "
    "yourself, in your own words and as yourself: the delegate's \"I\" becomes "
    "its name (\"Alex found €420 in October…\"). Keep every fact, amount, date, "
    "name and question it gives them exactly as it gave them; leave out only "
    "its account of how it worked. Never write it under the delegate's name or "
    "as a quote introduced by its name, and add no question, offer or next step "
    "it did not make, and no note on what you will do. Say it in as many words "
    "as the answer needs and no more. A result meant for you rather than the "
    "person is not passed on, except that a question or request it asks you to "
    "put to them is put to them."
)


def test_no_model_facing_text_mentions_relay_answer():
    for rel in RELAY_TEXT_SITES:
        assert "relay_answer" not in (CASA_ROOT / rel).read_text(encoding="utf-8"), rel


def test_ellen_tells_answers_in_her_own_words():
    for rel in RELAY_TEXT_SITES[:2]:
        text = _collapse_ws((CASA_ROOT / rel).read_text(encoding="utf-8"))
        assert "tell them that answer yourself, in your own words and as yourself" in text
        assert "Never write it under the delegate's name" in text
        assert "Keep every fact, amount, date, name and question" in text


def test_the_connection_verdict_rule_is_untouched():
    for rel in RELAY_TEXT_SITES[:2]:
        text = _collapse_ws((CASA_ROOT / rel).read_text(encoding="utf-8"))
        assert "Do not relay anyone else's verdict on whether a connection works" in text
        assert "stays its own report" not in text


def test_the_configurator_answer_is_told_not_relayed():
    text = _collapse_ws(
        (CASA_ROOT / "defaults/agents/assistant/delegates.yaml").read_text(encoding="utf-8"))
    assert "tell the person what it says, in your own words" in text


def test_the_relay_rule_reaches_only_the_assistant_text_projection():
    needle = _collapse_ws(_RELAY_DOCTRINE)
    compiled = _compiled_resident_carriers()
    assert {name: _collapse_ws(body).count(needle) for name, body in compiled} == {
        f"{slot}:{surface}": int(slot == "assistant" and surface == "text")
        for slot in _RESIDENT_SLOTS
        for surface in ("text", "voice", "restricted_webhook")}
    assert {slot: _collapse_ws(body).count(needle)
            for slot, body in _legacy_prompt_carriers()} == {
        slot: int(slot == "assistant") for slot in _RESIDENT_SLOTS}


def test_the_relay_rule_follows_the_already_posted_rule():
    """The eval inserts its copy right after the "When a request falls within"
    paragraph; the shipped text sits in the same place on both carriers."""
    compiled = dict(_compiled_resident_carriers())
    legacy = dict(_legacy_prompt_carriers())
    for body in (compiled["assistant:text"], legacy["assistant"]):
        flat = _collapse_ws(body)
        assert flat.index(_collapse_ws(_POSTED_DOCTRINE)) < flat.index(
            _collapse_ws(_RELAY_DOCTRINE)) < flat.index(_collapse_ws(_OWN_WORDS_DOCTRINE))


def test_the_own_words_eval_measures_the_shipped_rule():
    import ast
    path = Path(__file__).resolve().parents[1] / "test-local/eval/ellen_own_words.py"
    text = path.read_text(encoding="utf-8")
    start = text.index("DOCTRINE_PARAGRAPH = (") + len("DOCTRINE_PARAGRAPH = ")
    shipped = ast.literal_eval(text[start:text.index("\n)\n", start) + 2])
    assert _collapse_ws(shipped) == _collapse_ws(_RELAY_DOCTRINE)


# ---------------------------------------------------------------------------
# #1348: the two paths #1332 left — a late answer (a sync delegation past its
# 60 s wait, back as a notification) goes out the same way, a pending
# delegation gets one short line, and nothing narrates a question `ask_user`
# posted. The rule reaches only the assistant's text projection and the legacy
# carrier, once each. Its live effect is measured by
# test-local/eval/ellen_late_answer_and_ask.py.
# ---------------------------------------------------------------------------

_LATE_ANSWER_DOCTRINE = (
    "A delegate's answer that comes back later, in a notification, is passed "
    "on the same way. Casa's lines about a delegate's posts cover only those "
    "posts: an answer it wrote above them is not among them, and is passed on. "
    "A line above them that only announces those posts, points to them or says "
    "what they hold or propose, such as \"I'm posting its card now\" or \"it's "
    "on the card I just posted\", is not an answer: the posts have already "
    "reached the person and speak for themselves, so that line is not passed on. "
    "When a delegation comes back pending, the person would otherwise wait in "
    "silence: say only that it is still running, in one short line such as "
    "\"Alex is still on it.\", with nothing added: not the task again, not "
    "what you have done, not what will happen next. "
    "That line is only for work the person cannot see: when Casa's note "
    "says they are watching it in the delegate's own chat, write nothing "
    "about it. When `ask_user` has "
    "posted a question, the "
    "person already sees it with its buttons: write nothing about it, and "
    "when you have nothing else for them, stay silent exactly as Casa's note "
    "in its result says."
)
_LATE_EVAL = "test-local/eval/ellen_late_answer_and_ask.py"


def _late_eval():
    import importlib.util
    path = Path(__file__).resolve().parents[1] / _LATE_EVAL
    spec = importlib.util.spec_from_file_location("ellen_late_answer_and_ask", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_late_answer_rule_reaches_only_the_assistant_text_projection():
    needle = _collapse_ws(_LATE_ANSWER_DOCTRINE)
    compiled = _compiled_resident_carriers()
    assert {name: _collapse_ws(body).count(needle) for name, body in compiled} == {
        f"{slot}:{surface}": int(slot == "assistant" and surface == "text")
        for slot in _RESIDENT_SLOTS
        for surface in ("text", "voice", "restricted_webhook")}
    assert {slot: _collapse_ws(body).count(needle)
            for slot, body in _legacy_prompt_carriers()} == {
        slot: int(slot == "assistant") for slot in _RESIDENT_SLOTS}


def test_the_late_answer_rule_follows_the_relay_rule():
    """The eval inserts its copy right after the #1332 paragraph; the shipped
    text sits in the same place on both carriers."""
    compiled = dict(_compiled_resident_carriers())
    legacy = dict(_legacy_prompt_carriers())
    for body in (compiled["assistant:text"], legacy["assistant"]):
        flat = _collapse_ws(body)
        relay = flat.index(_collapse_ws(_RELAY_DOCTRINE))
        assert flat.index(_collapse_ws(_LATE_ANSWER_DOCTRINE)) == (
            relay + len(_collapse_ws(_RELAY_DOCTRINE)) + 1)


def test_the_late_answer_eval_measures_the_shipped_rule():
    module = _late_eval()
    assert _collapse_ws(module.DOCTRINE_PARAGRAPH) == _collapse_ws(_LATE_ANSWER_DOCTRINE)
    assert module.ANCHOR in _RELAY_DOCTRINE


def test_the_late_answer_eval_fakes_the_production_pending_note():
    """The eval's fake answers ``pending`` with the note the degraded sync
    wait returns, so it measures the turn the resident really gets."""
    source = (_casa_root() / "tools.py").read_text(encoding="utf-8")
    note = _late_eval().PENDING_NOTE
    assert note == ("Delegation continues in background; you will receive a "
                    "NOTIFICATION when complete.")
    assert '"Delegation continues in background; you will receive a "' in source
    assert '"NOTIFICATION when complete."' in source


def test_the_late_answer_eval_fakes_the_production_ask_note():
    """The eval's ask fake answers with the note a DM turn's live ask_user
    result carries (#1348), so a deployed image that predates it is measured
    with the shipped note. The note carries the one sentinel the output
    boundary turns into no message — in Casa's text, never in doctrine."""
    import tools
    from output_boundary import strips_to_silence
    module = _late_eval()
    assert module.ASK_NOTE == tools.ASK_USER_SILENCE_NOTE
    assert module.AWAITING["note"] == tools.ASK_USER_SILENCE_NOTE
    assert tools.ASK_USER_SILENCE_NOTE.count("<silent/>") == 1
    assert strips_to_silence("<silent/>")


@pytest.mark.parametrize("reply, ok", [
    ("", True),
    ("<silent/>", True),
    ("Alex is still on it.", True),
    ("Asked him — he's taking a moment, I'll bring his question back as soon as "
     "it lands.", False),
    ("Alex is still on it.\nI'll pass it on.", False),
    ("Still running — want me to check back?", False),
    ("Alex is still working on it.", True),
    ("Alex's still on it", True),
    # the reviewers' counterexamples (#1348 x2): short, one line, no question,
    # and not a holding line
    ("Alex has finished; no further answer is coming.", False),
    ("Your account balance is €500.", False),
    ("Alex couldn't do it.", False),
])
def test_the_late_answer_eval_judges_the_holding_line(reply, ok):
    assert _late_eval().judge_holding(reply)[0] is ok


@pytest.mark.parametrize("after, ok", [
    ("", True),
    ("<silent/>", True),
    ("Asked — tap whichever you fancy.", False),
    ("Asked — buttons are in your chat.", False),
])
def test_the_late_answer_eval_judges_text_after_an_ask(after, ok):
    assert _late_eval().judge_after_ask(after)[0] is ok


# ---------------------------------------------------------------------------
# No carrier names the removed topic cleanup tool any more.
# ---------------------------------------------------------------------------


def test_no_carrier_names_the_removed_topic_cleanup_tool():
    for _name, body in [*_compiled_resident_carriers(), *_legacy_prompt_carriers()]:
        assert "cleanup_engagement_topics" not in body
        assert "configurator-only" not in body


# ---------------------------------------------------------------------------
# Slice 5h (phone round 7): "ask me before you submit" made the assistant open a
# topic for a browsing task, where the specialist asked in plain text and its own
# question card never came. A browsing task is one sync delegation. Pinned on the
# compiled text projection (once there, nowhere else), the carrier a persona-bound
# assistant actually reads.
# ---------------------------------------------------------------------------

ASK_FIRST_IS_SYNC_SENTENCES = (
    "A browsing task is one delegation in `sync` mode, also when the person asks to be asked "
    "first (\"ask me before you submit\"): the browsing specialist asks them itself, in this "
    "chat (a question with buttons, or a link to take over).",
)


def test_asking_first_is_a_sync_delegation_on_the_assistants_text_projection():
    compiled = _compiled_resident_carriers()
    for sentence in ASK_FIRST_IS_SYNC_SENTENCES:
        needle = _collapse_ws(sentence)
        assert {name: _collapse_ws(body).count(needle) for name, body in compiled} == {
            name: int(name == "assistant:text") for name, _ in compiled}, sentence


_FAILED_DELEGATION_RULE = (
    "A failed delegation may already have changed things. If its message lists "
    "tools the specialist called before stopping, never say nothing happened or "
    "nothing changed: say in plain words that it stopped before finishing, what it "
    "may already have done that matters to the person (a message sent, a file "
    "saved, an order placed), and what they can do now. That list is for you to "
    "judge this by; never pass on its tool names or counts.")


def test_a_failed_delegation_is_told_in_plain_words_not_its_tool_list():
    """casa-test 2026-11-03: a browsing delegation hit its turn limit and its
    notice listed the calls it made ("run_act ×28, run_observe ×28, run_search
    ×21, …"); the assistant, told to "say it stopped partway and what it
    called", told the operator it had "opened and read pages about 28 times,
    done around 21 searches". The rule now asks for what happened in the
    person's terms and keeps the list for the assistant to judge by. Every
    assistant carrier and the fallback prompt carry it once; no other resident
    does; the old instruction is gone."""
    rule = _collapse_ws(_FAILED_DELEGATION_RULE)
    compiled = _compiled_resident_carriers()
    assert {name: _collapse_ws(body).count(rule) for name, body in compiled} == {
        name: int(name.startswith("assistant:")) for name, _ in compiled}
    assert all("say it stopped partway and what it called" not in _collapse_ws(body)
               for _, body in compiled)
    fallback = (_casa_root() / "defaults/agents/assistant/prompts/system.md").read_text(
        encoding="utf-8")
    assert _collapse_ws(fallback).count(rule) == 1


def test_the_stop_paragraph_asks_first_and_otherwise_says_only_what_stopped(system_md_text):
    """v0.347.3 (casa-test 2026-11-03): "Only one thing running, so that's the one." was posted
    between the two stop_work calls. The paragraph says: ask which one when several run and his
    words do not say; otherwise nothing until it is stopped, then one plain line of what stopped.
    The asking comes first, so the silence never forbids it (r1, Astra + Terra)."""
    text = _collapse_ws(system_md_text)
    ask = ("When its list holds more than one and the person's words do not make clear which he "
           "means, ask him which, naming each by its task, before stopping any.")
    quiet = ("Otherwise write nothing to him until it is stopped, then say in one plain line what "
             "stopped (\"Stopped the Hacker News summaries.\"), never how you found it or what "
             "else is or is not running.")
    assert text.count(ask) == 1 and text.count(quiet) == 1
    assert text.index(ask) < text.index(quiet)
    import tools as tools_mod
    desc = _collapse_ws(tools_mod.stop_work.description)
    assert ("Unless you must ask him which one, write nothing to the person between the two "
            "calls.") in desc


def test_the_configurator_is_a_delegate_and_no_executor_card_ships():
    """Since v0.351.0 the configurator is reached with
    delegate_to_agent; executors.yaml and engage_executor are gone."""
    agents_dir = _system_md_path().parent.parent.parent
    assert not (agents_dir / "assistant" / "executors.yaml").exists()
    assert not (agents_dir / "executors").exists()
    assert _configurator_card()["agent"] == "configurator"


def test_system_prompt_forbids_task_context_bleed(system_md_text):
    """O-6 (v0.37.9): a delegation's ``task=`` carries ONLY the new task —
    not the cumulative conversation with prior tasks bleeding through."""
    text = system_md_text.lower()
    assert "engage_executor" not in text
    assert "pass only the" in text and "do not carry" in text


def test_the_text_doctrine_says_she_can_change_a_specialists_handle():
    """A conversation that began before the handle change existed must not
    keep her saying it cannot be done: the text projection says she can, and
    that this holds even if earlier in the conversation it looked otherwise."""
    from pathlib import Path
    doctrine = (Path(__file__).resolve().parent.parent / "casa/rootfs/opt/casa/defaults"
                / "roles/resident/assistant/doctrine.md").read_text(encoding="utf-8")
    text = " ".join(doctrine.split("## Text projection", 1)[1]
                    .split("## Voice projection", 1)[0].split())
    assert "a different handle for a specialist" in text
    assert "`replace`" in text and "`check`" in text
    assert "even if earlier in the conversation it looked as if you could not" in text
