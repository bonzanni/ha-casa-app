"""Personality Phase A, Task 8: prompt compiler order/ceiling/determinism.

The compiler assembles the immutable per-surface resident system prompt from
the canonical role doctrine + bound persona pack, framed by the image-owned
platform-frame and safety-kernel. Section ORDER, the restricted-webhook
persona strip, admission-ceiling enforcement, and byte-for-byte determinism
are the load-bearing invariants proven here.
"""

from __future__ import annotations

import pytest

from persona_pack import PersonaManifest, PersonaPack
from personality_binding import materialize_image_default_binding
from prompt_compiler import compile_prompt_bundle
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
