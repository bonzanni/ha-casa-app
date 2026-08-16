"""Personality Phase A, Task 8: prompt compiler order/ceiling/determinism.

The compiler assembles the immutable per-surface resident system prompt from
the canonical role doctrine + bound persona pack, framed by the image-owned
platform-frame and safety-kernel. Section ORDER, the restricted-webhook
persona strip, admission-ceiling enforcement, and byte-for-byte determinism
are the load-bearing invariants proven here.
"""

from __future__ import annotations

import dataclasses

import pytest

from markdown_sections import sections
from persona_pack import PersonaManifest, PersonaPack
from personality_binding import materialize_image_default_binding
from prompt_compiler import _persona_body, compile_prompt_bundle
from role_slot import ResolvedModel, RoleSlot


@pytest.fixture
def role_factory():
    def make():
        resolved = ResolvedModel(
            source="ha_option", effective="haiku",
            sdk_model="claude-haiku-4-5", option="voice_agent_model",
        )
        normalized = {
            "api_version": "casa.role/v1", "id": "resident:butler", "kind": "resident",
            "slot": "butler", "mission": "Control the household.", "model": {
                "source": "ha_option", "option": "voice_agent_model", "default": "haiku",
                "allowed": ["opus", "sonnet", "haiku"],
            },
            "model_resolved": {"effective": "haiku", "sdk_model": "claude-haiku-4-5"},
            "response": {
                "text": {"register": "conversational",
                         "max_confirmation_sentences": 2,
                         "max_status_sentences": 3},
                "voice": {"register": "spoken",
                          "max_confirmation_sentences": 1,
                          "max_status_sentences": 2,
                          "first_clause_max_words": 8,
                          "first_clause_requires_early_punctuation": True},
                "restricted_webhook": {"register": "plain",
                                       "max_status_sentences": 2},
            },
        }
        return RoleSlot(
            role_id="resident:butler", kind="resident", slot="butler",
            mission="Control the household.", resolved_model=resolved, normalized=normalized,
            doctrine=(
                "# Core doctrine\n\nControl things.\n\n## Text projection\n\nBe brief.\n\n"
                "## Voice projection\n\nBe brief and spoken.\n\n"
                "## Restricted webhook projection\n\nBe plain.\n"
            ),
            checksum="sha256:" + "1" * 64,
        )
    return make


@pytest.fixture
def persona_factory():
    def make():
        return PersonaPack(
            persona_id="casa/tina", version="0.1.0", trait_schema_version=1,
            identity={"display_name": "Tina", "pronouns": {
                "subject": "she", "object": "her", "possessive_adjective": "her",
                "possessive_pronoun": "hers", "reflexive": "herself",
            }},
            relationship_posture="established", archetype="housekeeper",
            traits={"warmth": 3, "formality": 2, "candor": 4, "attunement": 4,
                    "curiosity": 3, "levity": 2, "social_energy": 3, "optimism": 3},
            quirks=(), markdown="# Core\n\nTina keeps the house running.\n\n## Negative space\n\nNever gossips.\n",
            examples=(),
            manifest=PersonaManifest(files=(), checksum="sha256:" + "3" * 64),
            checksum="sha256:" + "2" * 64,
        )
    return make


@pytest.fixture
def binding_factory():
    def make(role, persona):
        return materialize_image_default_binding(
            role=role, persona=persona, image_default_root=f"{persona.persona_id}@{persona.version}",
        )
    return make


def test_compiler_order_and_restricted_projection(role_factory, persona_factory, binding_factory) -> None:
    """Pins INV-PERS-004. Red case demonstrated: disabling the restricted_webhook persona omission fails this test."""
    role, persona = role_factory(), persona_factory()
    binding = binding_factory(role, persona)
    bundle = compile_prompt_bundle(
        role=role, persona=persona, binding=binding,
        platform_frame="Platform.\n", safety_kernel="Safety.\n",
    )
    text = bundle.text.system_prompt
    assert text.index("<platform_frame>") < text.index("<role_identity>")
    assert text.index("<role_identity>") < text.index("<persona>")
    assert text.index("<persona>") < text.index("<role_doctrine>")
    assert text.index("<role_doctrine>") < text.index("<response_shape>")
    assert text.index("<response_shape>") < text.index("<safety_kernel>")
    assert text.endswith("</safety_kernel>\n")
    assert "<persona>" not in bundle.restricted_webhook.system_prompt
    assert persona.identity["display_name"] not in bundle.restricted_webhook.system_prompt


def test_binding_digest_mismatch_is_rejected(role_factory, persona_factory, binding_factory) -> None:
    import dataclasses

    role, persona = role_factory(), persona_factory()
    binding = binding_factory(role, persona)
    tampered = dataclasses.replace(binding, binding_digest="sha256:" + "0" * 64)
    with pytest.raises(ValueError, match="binding"):
        compile_prompt_bundle(
            role=role, persona=persona, binding=tampered,
            platform_frame="Platform.\n", safety_kernel="Safety.\n",
        )


def test_a_role_checksum_mismatch_names_the_model_as_the_likely_cause(
        role_factory, persona_factory, binding_factory) -> None:
    """#568: the role checksum covers the RESOLVED model by design, so a model
    change — an operator flipping ``primary_agent_model``, or an alias being
    pointed at a new generation — invalidates every persisted binding compiled
    against the old one. The failure is loud and per-specialist, but it used to
    read as an opaque digest compare; it must name the field that moved and the
    remedy, or an operator has no way from the message to the fix."""
    import dataclasses

    role, persona = role_factory(), persona_factory()
    binding = binding_factory(role, persona)
    # NOT "1" * 64 — that is the role fixture's OWN stub checksum, so the
    # "stale" binding would have matched and this test would have asserted
    # nothing while appearing to pass.
    stale = dataclasses.replace(binding, role_checksum="sha256:" + "2" * 64)
    with pytest.raises(ValueError) as caught:
        compile_prompt_bundle(
            role=role, persona=persona, binding=stale,
            platform_frame="Platform.\n", safety_kernel="Safety.\n",
        )
    message = str(caught.value)
    assert "role_checksum" in message, message
    assert "resolved model" in message, message
    assert "re-install or upgrade" in message, message


def test_projection_doctrine_excludes_sibling_projections(
        role_factory, persona_factory, binding_factory) -> None:
    """#355: the shipped doctrines nest all three projection headings under
    '# Core doctrine', and a markdown section's body runs to the next
    SAME-level heading — selecting Core must not drag sibling surfaces'
    instructions into a projection, nor duplicate the selected surface."""
    role, persona = role_factory(), persona_factory()
    binding = binding_factory(role, persona)
    bundle = compile_prompt_bundle(
        role=role, persona=persona, binding=binding,
        platform_frame="Platform.\n", safety_kernel="Safety.\n",
    )
    rw = bundle.restricted_webhook.system_prompt
    assert "Be plain." in rw                      # its own surface
    assert "Control things." in rw                # shared core
    assert "Be brief and spoken." not in rw       # voice must not leak
    assert "Be brief.\n" not in rw                # text must not leak
    voice = bundle.voice.system_prompt
    assert voice.count("Be brief and spoken.") == 1   # not core-copy + own-copy
    assert "Be plain." not in voice
    text = bundle.text.system_prompt
    assert text.count("Be brief.") == 1
    assert "Be plain." not in text


def test_response_shape_reaches_the_compiled_prompt(
        role_factory, persona_factory, binding_factory) -> None:
    """#549: role.yaml's `response:` block was declared and never read — the
    compiled bundle REPLACES the composed prompt for a persona-bound resident
    (INV-PERS-001), so every brevity rule written elsewhere was dead and
    editing the block had no effect and gave no warning. Red case: delete the
    `<response_shape>` append in compile_projection_set and this fails."""
    role, persona = role_factory(), persona_factory()
    bundle = compile_prompt_bundle(
        role=role, persona=persona, binding=binding_factory(role, persona),
        platform_frame="Platform.\n", safety_kernel="Safety.\n",
    )
    text = bundle.text.system_prompt
    assert "Register: conversational." in text
    assert "Confirmations: at most 2 sentences." in text
    assert "Status updates: at most 3 sentences." in text
    # Per-surface, not one shared block: voice is tighter and carries its own
    # first-clause rules; the webhook surface carries neither.
    voice = bundle.voice.system_prompt
    assert "Confirmations: at most 1 sentence." in voice
    assert "First clause: at most 8 words." in voice
    assert "Punctuate within the first clause." in voice
    assert "Confirmations: at most 2 sentences." not in voice  # text's limit
    assert "Status updates: at most 2 sentences." in voice     # its own
    rw = bundle.restricted_webhook.system_prompt
    assert "Register: plain." in rw
    assert "First clause" not in rw


def test_every_schema_response_key_is_rendered() -> None:
    """The anti-drift guard for #549, and the reason the compiler does NOT
    raise on an unknown key: a key added to role.v1.json that nobody teaches
    the renderer would otherwise become the next silently-dead declaration.
    Binding OUR tree in a test costs third-party bundles nothing, where a
    runtime rejection would block a schema-valid specialist install."""
    import json
    from pathlib import Path

    import prompt_compiler

    schema = json.loads(
        (Path(prompt_compiler.__file__).parent
         / "defaults/schema/role.v1.json").read_text(encoding="utf-8"))
    declared = set(
        schema["$defs"]["responseProjection"]["properties"])
    rendered = {key for key, _ in prompt_compiler._RESPONSE_RENDERERS}
    assert declared == rendered, (
        f"role.v1.json declares {declared - rendered} with no renderer, "
        f"and the renderer handles {rendered - declared} the schema rejects")


def test_response_shape_reserve_covers_the_largest_section() -> None:
    """Sol design r3 (S2): the section spends the same per-surface budget as
    everything else, so a projection that compiles today must still compile
    after it is added. Pins that the reserve added to each ceiling genuinely
    exceeds the biggest section the renderer can emit."""
    import prompt_compiler
    from canonical_bytes import canonical_json_bytes
    from role_slot import ResolvedModel, RoleSlot
    from trait_renderer import estimate_tokens_v1

    # The schema puts no upper bound on the integers, so the real ceiling is
    # the canonical-JSON safe-integer domain every role checksum goes through —
    # anything larger is rejected before it can reach the compiler (pinned
    # below, so this bound stays a fact rather than an assumption).
    widest_int = 2 ** 53 - 1
    with pytest.raises(Exception):
        canonical_json_bytes({"max_status_sentences": widest_int + 1})
    widest = {
        "register": "x" * (prompt_compiler._REGISTER_MAX_CHARS * 4),
        "max_confirmation_sentences": widest_int,
        "max_status_sentences": widest_int,
        "first_clause_max_words": widest_int,
        "first_clause_requires_early_punctuation": True,
    }
    role = RoleSlot(
        role_id="resident:butler", kind="resident", slot="butler",
        mission="m", resolved_model=ResolvedModel("fixed", "haiku",
                                                  "claude-haiku-4-5", None),
        normalized={"response": {"text": widest}}, doctrine="", checksum="c",
    )
    body = prompt_compiler._response_shape_body(role, "text")
    section = f"<response_shape>\n{body}\n</response_shape>\n"
    assert (estimate_tokens_v1(section)
            <= prompt_compiler._RESPONSE_SHAPE_TOKEN_RESERVE)


def test_recompiling_the_same_inputs_is_byte_identical(role_factory, persona_factory, binding_factory) -> None:
    role, persona = role_factory(), persona_factory()
    binding = binding_factory(role, persona)
    first = compile_prompt_bundle(
        role=role, persona=persona, binding=binding,
        platform_frame="Platform.\n", safety_kernel="Safety.\n",
    )
    second = compile_prompt_bundle(
        role=role, persona=persona, binding=binding,
        platform_frame="Platform.\n", safety_kernel="Safety.\n",
    )
    assert first == second


# ---------------------------------------------------------------------------
# #611 — every authored persona section reaches the text projection EXACTLY
# once. `sections()` is flat across heading levels while a parent's body
# physically runs through its children, so walking every heading re-emits each
# nested subsection once per ancestor. Depth is the multiplier, not the number
# two. Each shape below is loader-admitted (persona_pack.py:207-219: exactly
# one level-1 `# Core` whose body is 300-500 chars, plus some level-2
# `## Negative space`); the shape is asserted as SETUP rather than assumed.
# ---------------------------------------------------------------------------

CORE_SENT = "CORESENT the validated core sentence."
NEG_SENT = "NEGSENT the negative space sentence."
GRAND_SENT = "GRANDSENT the grandchild sentence."
PREAMBLE_SENT = "PREAMBLESENT the house preamble sentence."
PREAMBLE_CORE_SENT = "PREAMBLECORESENT the core-named preamble sentence."
RULES_SENT = "RULESSENT the house rules sentence."
SUB_SENT = "SUBSENT the sub sentence."
NESTED_SENT = "NESTEDSENT the nested core sentence."


def _core_body(sentence: str = CORE_SENT) -> str:
    """A Core body inside the loader's 300-500 character gate."""
    body = f"{sentence} " + "The house stays calm and the answers stay short. " * 6
    body = body.strip()
    assert 300 <= len(body) <= 500, len(body)
    return body


def _shape(persona_factory, markdown: str, expected: list[tuple[int, str]]):
    """Return (text, voice) bodies, asserting the authored shape as setup."""
    assert [(level, name) for level, name, _ in sections(markdown)] == expected
    persona = dataclasses.replace(persona_factory(), markdown=markdown)
    return _persona_body(persona, "text"), _persona_body(persona, "voice")


_T1_MD = (f"# Core\n\n{_core_body()}\n\n## Negative space\n\n{NEG_SENT}\n\n"
          f"### Grandchild\n\n{GRAND_SENT}\n")
_T3_MD = (f"## House preamble\n\n{PREAMBLE_SENT}\n\n# Core\n\n{_core_body()}\n\n"
          f"## Negative space\n\n{NEG_SENT}\n")
_T4_MD = (f"# Core\n\n{_core_body()}\n\n## Negative space\n\n{NEG_SENT}\n\n"
          f"# House rules\n\n{RULES_SENT}\n\n## Sub\n\n{SUB_SENT}\n")
_T6_MD = (f"# Core\n\n{_core_body()}\n\n## Core\n\n{NESTED_SENT}\n\n"
          f"## Negative space\n\n{NEG_SENT}\n")
_T7_MD = (f"# Core\n\n{_core_body()}\n\n## Negative space\n\n{NEG_SENT}\n\n"
          f"# House rules\n\n{RULES_SENT}\n\n## Core\n\n{NESTED_SENT}\n")
_T8_MD = (f"## Core\n\n{PREAMBLE_CORE_SENT}\n\n# Core\n\n{_core_body()}\n\n"
          f"## Negative space\n\n{NEG_SENT}\n")


def test_a_nested_subsection_reaches_the_text_projection_exactly_once(persona_factory) -> None:
    """#611. Red case observed on the shipped tree: GRANDSENT x3, NEGSENT x2 —
    the grandchild rides inside Core's body, inside Negative space's body, and
    again as its own flat entry. Asserted as ==1, never as !=2, because depth
    is the multiplier."""
    text, _ = _shape(persona_factory, _T1_MD, [
        (1, "Core"), (2, "Negative space"), (3, "Grandchild")])
    assert text.count(GRAND_SENT) == 1
    assert text.count(NEG_SENT) == 1


def test_every_authored_heading_line_survives_exactly_once(persona_factory) -> None:
    """The ONLY guard on heading preservation — every other case counts prose.
    A name-keyed `exclude=` fix drops '## Negative space' to 0, because
    select_markdown_sections cuts from heading_start (markdown_sections.py:87-90)
    while these bodies are heading-exclusive. No assertion here expects a ROOT's
    own heading line: `sections()` bodies start at match.end(), so '# Core' and
    '# House rules' reach no projection today or after."""
    text, _ = _shape(persona_factory, _T1_MD, [
        (1, "Core"), (2, "Negative space"), (3, "Grandchild")])
    assert text.count("## Negative space") == 1
    assert text.count("### Grandchild") == 1


def test_a_section_authored_above_core_is_emitted_once_and_only_once(persona_factory) -> None:
    """The loader constrains no ordering and no depth, so a level-2 section
    ABOVE the first level-1 heading is admitted. The NEG half is red today
    (x2); the PREAMBLE half is GREEN today and is the anti-regression half —
    a `level == 1` filter on the extras, the fix this issue originally
    prescribed, silently deletes it (x0)."""
    text, _ = _shape(persona_factory, _T3_MD, [
        (2, "House preamble"), (1, "Core"), (2, "Negative space")])
    assert {"preamble": text.count(PREAMBLE_SENT),
            "negative": text.count(NEG_SENT)} == {"preamble": 1, "negative": 1}


def test_a_second_root_and_its_subsection_each_reach_text_once(persona_factory) -> None:
    """LOAD-BEARING: the extras loop is DEAD for all three shipped packs
    (roots == [(1, 'Core')] for tina, ellen and gary), so without a second-root
    shape a 'delete the extras loop' mutation ships byte-identical output for
    the whole image and passes everything else."""
    text, _ = _shape(persona_factory, _T4_MD, [
        (1, "Core"), (2, "Negative space"), (1, "House rules"), (2, "Sub")])
    assert {"rules": text.count(RULES_SENT), "sub": text.count(SUB_SENT),
            "negative": text.count(NEG_SENT)} == {"rules": 1, "sub": 1, "negative": 1}


def test_a_second_roots_prose_never_reaches_voice(persona_factory) -> None:
    """NOT red today — a mutation-only guard, owed because the fix rewrites the
    body of the `if surface == "text":` branch and so makes that branch a site.
    Deleting the guard and dedenting the extras extend puts RULESSENT on voice,
    whose persona ceiling is 400 tokens against text's 2000."""
    text, voice = _shape(persona_factory, _T4_MD, [
        (1, "Core"), (2, "Negative space"), (1, "House rules"), (2, "Sub")])
    assert text.count(RULES_SENT) == 1, "premise: the marker is on text"
    assert {"rules": voice.count(RULES_SENT),
            "sub": voice.count(SUB_SENT)} == {"rules": 0, "sub": 0}


def test_a_core_nested_under_the_core_root_reaches_voice_exactly_once(persona_factory) -> None:
    """Red today on VOICE (x2), which the issue does not name at all:
    select_markdown_sections matches names at ANY level
    (markdown_sections.py:92-93), so it returns the level-1 body — which
    already contains the nested subtree — concatenated with the level-2 body.
    This is the sole proof the fix reached the core half, not only the
    extras half."""
    text, voice = _shape(persona_factory, _T6_MD, [
        (1, "Core"), (2, "Core"), (2, "Negative space")])
    assert voice.count(NESTED_SENT) == 1
    assert text.count(NESTED_SENT) == 1


def test_a_core_under_a_second_root_never_reaches_voice(persona_factory) -> None:
    """A DECLARED BEHAVIOUR CHANGE, not a de-duplication: this prose stops
    reaching voice and stays on text. Today voice carries it (x1) because the
    core selection is level-blind. `personality.md` already publishes the
    contract this restores — "voice carries only the persona core" and "voice
    behavior written only outside the core never reaches the voice surface".
    A level-blind but root-scoped predicate removes it too (the section is not
    a root; `# House rules` contains it), so the change is not avoidable by
    predicate choice. This test exists so a later tidy cannot silently flip it
    back."""
    text, voice = _shape(persona_factory, _T7_MD, [
        (1, "Core"), (2, "Negative space"), (1, "House rules"), (2, "Core")])
    assert voice.count(NESTED_SENT) == 0
    assert text.count(NESTED_SENT) == 1


def test_a_core_authored_above_the_core_root_is_text_prose_not_voice_core(persona_factory) -> None:
    """Pins the LOADER's predicate against the level-blind root-scoped
    alternative: here the level-2 `## Core` IS a root, which is what separates
    the two. Red today on the voice half — prose that never passed the loader's
    300-500 char Core gate (which validates core_bodies[0] ALONE,
    persona_pack.py:214-216) is in the voice prompt today."""
    text, voice = _shape(persona_factory, _T8_MD, [
        (2, "Core"), (1, "Core"), (2, "Negative space")])
    assert voice.count(PREAMBLE_CORE_SENT) == 0
    assert text.count(PREAMBLE_CORE_SENT) == 1


def test_the_text_projection_puts_the_core_root_before_the_other_roots(persona_factory) -> None:
    """Declares the reordering the fix introduces: it changes within-persona
    ORDER for 266 of 1389 loader-admitted shapes, content preserved throughout.
    Today the core is every Core-NAMED body in document order, so a level-2
    preamble Core precedes the validated level-1 Core prose. The three shipped
    packs are unaffected (one root each), and
    test_compiler_order_and_restricted_projection pins only <tag> order, never
    the order within the persona block — which is why this is owed."""
    text, _ = _shape(persona_factory, _T8_MD, [
        (2, "Core"), (1, "Core"), (2, "Negative space")])
    assert text.count(CORE_SENT) == 1
    assert text.count(PREAMBLE_CORE_SENT) == 1
    assert text.index(CORE_SENT) < text.index(PREAMBLE_CORE_SENT)


# The only shape in this file where the fix costs bytes rather than saving
# them. Negative space and the core-named section are BOTH roots here, so
# nothing is nested and no duplicate is removed — the sole difference is which
# separator joins the core-named section to the rest.
_T10_MD = (f"## Negative space\n\n{NEG_SENT}\n\n## Core\n\n{PREAMBLE_CORE_SENT}\n\n"
           f"# Core\n\n{_core_body()}\n")


def test_a_core_that_is_not_the_core_root_costs_one_byte_on_text(persona_factory) -> None:
    """Declares the ONE way this fix can make a projection bigger, so the
    release does not claim admission ceilings only move down.

    The section that costs a byte is narrower than "any Core-named section":
    it must be a ROOT (contained by nothing) that is not the validated level-1
    Core. Today such a section is joined into the core element by a SINGLE
    newline; after the fix it is its own extras element joined by a BLANK
    LINE. Measured on this shape: text 812 -> 813 on this fixture, while voice
    falls 774 -> 723. (Against the real shipped tina pack, whose traits render
    differently, the same shape measures 1117 -> 1118 and 1079 -> 1028 — the
    +1 is the same byte.)

    A Core-named section that is NESTED goes the other way, because the
    de-duplication dominates: measured 902 -> 827 and 939 -> 864 on the two
    nested shapes above. Their number is not independently limited, though
    persona.md is capped at MAX_PERSONA_MD_BYTES (262144), which bounds it.

    So "no pack can become inadmissible" is FALSE on text and TRUE on voice. A
    pack sitting exactly on the 2000-token text persona ceiling with such a
    section can be refused after this change — loudly, by the existing
    ValueError in compile_projection_set, never silently."""
    text, voice = _shape(persona_factory, _T10_MD, [
        (2, "Negative space"), (2, "Core"), (1, "Core")])
    assert text.count(PREAMBLE_CORE_SENT) == 1
    assert voice.count(PREAMBLE_CORE_SENT) == 0
    assert len(text.encode("utf-8")) == 813
