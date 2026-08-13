# casa/rootfs/opt/casa/prompt_compiler.py
"""Personality Phase A, Task 8: the resident prompt compiler.

Assembles the immutable per-surface (text / voice / restricted_webhook)
system prompt for a persona-bearing resident from three image-owned inputs —
the platform frame, the canonical role doctrine, and the bound persona pack —
plus the image-owned safety kernel, in a FIXED section order:

    <platform_frame> <role_identity> <persona> <role_doctrine>
    <response_shape> <safety_kernel>

The restricted-webhook surface strips the persona entirely (an untrusted
webhook origin must never see the resident's persona identity). Each surface
enforces an admission ceiling (persona token budget + total token budget) and
is byte-for-byte deterministic given identical inputs, so a recompile of the
same role+persona+binding produces an identical prompt and digest.

The compiled bundle is only returned when the supplied ``BindingRecord``'s
``binding_digest`` recomputes to the digest of the exact role+persona pair the
caller is compiling — a loaded binding that does not match the role/persona it
claims to bind is rejected as tampered/stale.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from canonical_bytes import canonical_text, checksum_bytes
from markdown_sections import sections, select_markdown_sections
from personality_binding import BindingRecord, compute_binding_digest
from role_slot import RoleSlot
from trait_renderer import RENDERER_VERSION, estimate_tokens_v1, render_v1

# #549: the response-shape section is rendered from role.yaml's `response:`
# block, whose only bounded-length field is `register` (role.v1.json admits any
# non-empty string). Capping what we render bounds the whole section, which is
# what lets the ceilings below carry an exact reserve for it.
_REGISTER_MAX_CHARS = 64

# #549 (Sol design r3, S2): the section consumes the SAME per-surface budget as
# everything else, so adding it would have pushed a projection that compiles
# today past an unchanged ceiling — an installed, schema-valid specialist would
# become unloadable for no reason but our new section. Each total ceiling is
# raised by a reserve that provably exceeds the largest section we can emit
# (five short lines plus a register capped at _REGISTER_MAX_CHARS), so every
# projection admitted before this change is still admitted after it. The
# persona ceilings are untouched — the section is not persona prose.
_RESPONSE_SHAPE_TOKEN_RESERVE = 128

# Per-surface (persona_token_ceiling, total_token_ceiling). The persona budget
# bounds how much authored persona prose can dominate the prompt; the total
# budget bounds the assembled projection. voice is tighter than text (a spoken
# turn is latency-bound); restricted_webhook admits no persona at all.
_LIMITS = {
    "text": (2000, 12000 + _RESPONSE_SHAPE_TOKEN_RESERVE),
    "voice": (400, 6000 + _RESPONSE_SHAPE_TOKEN_RESERVE),
    "restricted_webhook": (0, 4000 + _RESPONSE_SHAPE_TOKEN_RESERVE),
}

# #355: every projection heading is excluded from any OTHER selected
# section's subtree (see compile_projection_set) — the surface's own section
# is always selected explicitly, never inherited through Core's nesting.
_PROJECTION_HEADINGS = (
    "Text projection", "Voice projection", "Restricted webhook projection",
)


@dataclass(frozen=True, slots=True)
class CompiledProjection:
    system_prompt: str
    digest: str
    estimated_tokens: int


@dataclass(frozen=True, slots=True)
class CompiledPromptBundle:
    role_id: str
    resolved_model: str
    text: CompiledProjection
    voice: CompiledProjection
    restricted_webhook: CompiledProjection
    binding_digest: str


def _section(tag: str, body: str) -> str:
    return f"<{tag}>\n{body.rstrip(chr(10))}\n</{tag}>\n"


def _projection(parts: list[tuple[str, str]]) -> CompiledProjection:
    prompt = "\n".join(_section(tag, body).rstrip("\n") for tag, body in parts) + "\n"
    return CompiledProjection(
        system_prompt=prompt, digest=checksum_bytes(prompt.encode("utf-8")),
        estimated_tokens=estimate_tokens_v1(prompt),
    )


def _persona_body(persona, surface: str) -> str:
    if surface == "restricted_webhook":
        return ""
    identity = persona.identity
    pronouns = identity["pronouns"]
    core = select_markdown_sections(persona.markdown, ("Core",))
    body = [
        f"Display name: {identity['display_name']}",
        (f"Pronouns: {pronouns['subject']}/{pronouns['object']}/"
         f"{pronouns['possessive_adjective']}/{pronouns['possessive_pronoun']}/{pronouns['reflexive']}"),
        render_v1(persona.traits, persona.relationship_posture),
        core.rstrip("\n"),
    ]
    if surface == "text":
        body.extend(
            body_part.rstrip("\n") for _, name, body_part in sections(persona.markdown) if name != "Core"
        )
        body.extend(f"Quirk ({q['frequency']}): when {q['context']}, {q['tendency']}." for q in persona.quirks)
        body.extend(
            f"Example user: {e['user']}\nGood: {e['good']}\nBad: {e['bad']}"
            for e in persona.examples if e["surface"] in {"text", "any"}
        )
    else:
        body.extend(f"Quirk ({q['frequency']}): when {q['context']}, {q['tendency']}." for q in persona.quirks[:2])
    return "\n\n".join(v for v in body if v)


# #549: role.yaml's `response:` block was declared and never read — the compiled
# bundle REPLACES the composed prompt for a persona-bound resident (INV-PERS-001),
# so `defaults/agents/*/response_shape.yaml` and the composed system prompt's
# brevity rules never reached the model, and editing the block had no effect and
# gave no warning. Rendering it here is what makes the declaration live.
#
# One renderer per key, in a FIXED order so the projection stays byte-for-byte
# deterministic. A key absent from the block renders nothing. A value of the
# wrong type is SKIPPED rather than raising: `defaults/schema/role.v1.json`
# ($defs/responseProjection) is closed and typed, and every role.yaml — image
# and specialist-bundle alike — is validated against it in role_artifact.py
# before it can become a RoleSlot, so a raise here could only fire where the
# loader already rejected, while being able to block a legitimate install
# (Terra design r1, S2). The drift this guards against is OUR OWN — a schema key
# nobody taught the renderer — and that is pinned by a test which reads the
# schema, not by a runtime rejection third-party bundles would pay for.
def _sentences(value: object, label: str) -> str | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return f"{label}: at most {value} sentence{'' if value == 1 else 's'}."


_RESPONSE_RENDERERS: tuple[tuple[str, "object"], ...] = (
    ("register", lambda v: (
        f"Register: {v.strip()[:_REGISTER_MAX_CHARS]}."
        if isinstance(v, str) and v.strip() else None)),
    ("max_confirmation_sentences",
     lambda v: _sentences(v, "Confirmations")),
    ("max_status_sentences",
     lambda v: _sentences(v, "Status updates")),
    ("first_clause_max_words", lambda v: (
        f"First clause: at most {v} words."
        if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None)),
    ("first_clause_requires_early_punctuation", lambda v: (
        "Punctuate within the first clause." if v is True else None)),
)


def _response_shape_body(role: RoleSlot, surface: str) -> str:
    """The `response:` block for *surface*, as deterministic prose. Empty when
    the role declares none (role.v1.json requires the block, so this is the
    defensive path for a RoleSlot built by something other than the loader)."""
    response = role.normalized.get("response")
    if not isinstance(response, Mapping):
        return ""
    block = response.get(surface)
    if not isinstance(block, Mapping):
        return ""
    lines = []
    for key, render in _RESPONSE_RENDERERS:
        if key in block:
            line = render(block[key])
            if line is not None:
                lines.append(line)
    return "\n".join(lines)


def compile_projection_set(
    *, role: RoleSlot, persona, platform_frame: str, safety_kernel: str,
) -> dict[str, CompiledProjection]:
    projections: dict[str, CompiledProjection] = {}
    for surface in ("text", "voice", "restricted_webhook"):
        persona_body = _persona_body(persona, surface)
        # #355: the shipped doctrines nest all three projection headings
        # under "# Core doctrine", so Core's body would otherwise carry every
        # surface's instructions into every projection (and the selected
        # surface twice). Excluding ALL projection subtrees scopes Core to
        # the shared text; the surface's own section is selected separately.
        doctrine = select_markdown_sections(role.doctrine, {
            "text": ("Core doctrine", "Text projection"),
            "voice": ("Core doctrine", "Voice projection"),
            "restricted_webhook": ("Core doctrine", "Restricted webhook projection"),
        }[surface], exclude=_PROJECTION_HEADINGS)
        parts = [
            ("platform_frame", canonical_text(platform_frame)),
            ("role_identity", f"id: {role.role_id}\nkind: {role.kind}\nmission: {role.mission}\n"),
        ]
        if persona_body:
            parts.append(("persona", persona_body))
        parts.append(("role_doctrine", doctrine))
        response_shape = _response_shape_body(role, surface)
        if response_shape:
            parts.append(("response_shape", response_shape))
        parts.append(("safety_kernel", canonical_text(safety_kernel)))
        projection = _projection(parts)
        persona_tokens = estimate_tokens_v1(persona_body)
        persona_limit, total_limit = _LIMITS[surface]
        if persona_tokens > persona_limit or projection.estimated_tokens > total_limit:
            raise ValueError(f"{surface} prompt exceeds admission ceiling for role {role.role_id}")
        projections[surface] = projection
    return projections


def compile_prompt_bundle(
    *, role: RoleSlot, persona, binding: BindingRecord, platform_frame: str, safety_kernel: str,
) -> CompiledPromptBundle:
    projections = compile_projection_set(
        role=role, persona=persona, platform_frame=platform_frame, safety_kernel=safety_kernel,
    )
    expected_digest = compute_binding_digest(
        stable_agent_id=role.role_id, role_checksum=role.checksum,
        persona_id=persona.persona_id, persona_version=persona.version,
        persona_checksum=persona.checksum, compiler_schema_version=RENDERER_VERSION,
        dependency_digests=binding.dependency_digests,
        effective_config_digest=binding.effective_config_digest,
    )
    if (binding.binding_digest != expected_digest or binding.stable_agent_id != role.role_id
            or binding.role_checksum != role.checksum or binding.persona_id != persona.persona_id
            or binding.persona_version != persona.version or binding.persona_checksum != persona.checksum):
        raise ValueError(f"loaded binding for {role.role_id} does not match the compiled role+persona")
    return CompiledPromptBundle(
        role_id=role.role_id, resolved_model=role.resolved_model.effective,
        text=projections["text"], voice=projections["voice"],
        restricted_webhook=projections["restricted_webhook"], binding_digest=binding.binding_digest,
    )


def projection_for(bundle: CompiledPromptBundle, *, channel: str, origin_route: str | None) -> CompiledProjection:
    if origin_route == "webhook_trigger":
        return bundle.restricted_webhook
    if channel == "voice":
        return bundle.voice
    return bundle.text
