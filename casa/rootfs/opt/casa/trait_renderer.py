from __future__ import annotations

import unicodedata
from typing import Mapping

RENDERER_VERSION = "casa-persona-renderer/1.0.0"

AXES = (
    "warmth", "formality", "candor", "attunement",
    "curiosity", "levity", "social_energy", "optimism",
)

TRAIT_PHRASE_V1 = {
    "warmth": {
        1: "coolly reserved", 2: "lightly warm", 3: "neutral",
        4: "openly warm", 5: "deeply affectionate without invented intimacy",
    },
    "formality": {
        1: "casual and relaxed", 2: "informal", 3: "neutral",
        4: "polished and formal", 5: "highly formal without stiffness",
    },
    "candor": {
        1: "highly tactful", 2: "diplomatic", 3: "neutral",
        4: "plainspoken", 5: "bracingly frank without rudeness",
    },
    "attunement": {
        1: "matter-of-fact", 2: "lightly responsive to emotion", 3: "neutral",
        4: "emotionally attentive",
        5: "deeply empathic without claiming feelings or facts",
    },
    "curiosity": {
        1: "self-contained", 2: "selectively curious", 3: "neutral",
        4: "inquisitive in what is noticed",
        5: "avidly curious in what is noticed",
    },
    "levity": {
        1: "earnest and serious", 2: "mostly straight-faced", 3: "neutral",
        4: "wryly humorous", 5: "freely comic without obscuring meaning",
    },
    "social_energy": {
        1: "quietly self-possessed", 2: "calm", 3: "neutral",
        4: "socially lively", 5: "buoyantly outgoing",
    },
    "optimism": {
        1: "sober and skeptical", 2: "pragmatic", 3: "neutral",
        4: "upbeat", 5: "buoyant without false reassurance",
    },
}

POSTURE_CLAUSE_V1 = {
    "new": "Keep new-contact social distance; do not imply prior familiarity.",
    "professional": "Keep professional ease; do not imply personal intimacy.",
    "established": (
        "An established posture means easy familiarity, never invented shared memory."
    ),
}

INVARIANT_V1 = (
    "These traits colour interpersonal wording only; they never change facts, "
    "permissions, action timing, tool choice, disclosure, response shape, or "
    "role doctrine."
)


def join_fragments(fragments: list[str]) -> str:
    if not fragments:
        return "remain neutral in interpersonal wording"
    if len(fragments) == 1:
        return f"be {fragments[0]}"
    if len(fragments) == 2:
        return f"be {fragments[0]} and {fragments[1]}"
    return "be " + ", ".join(fragments[:-1]) + ", and " + fragments[-1]


def render_v1(traits: Mapping[str, int], posture: str) -> str:
    if tuple(traits.keys()) != AXES and set(traits) != set(AXES):
        raise ValueError("traits must contain exactly the v1 axes")
    if posture not in POSTURE_CLAUSE_V1:
        raise ValueError("invalid relationship posture")
    fragments = []
    for axis in AXES:
        value = traits[axis]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
            raise ValueError(f"{axis} must be an integer from 1 through 5")
        if value != 3:
            fragments.append(TRAIT_PHRASE_V1[axis][value])
    return (
        "Interpersonal manner: " + join_fragments(fragments) + ". "
        + POSTURE_CLAUSE_V1[posture] + " " + INVARIANT_V1
    )


def estimate_tokens_v1(text: str) -> int:
    canonical = unicodedata.normalize("NFC", text)
    canonical = canonical.replace("\r\n", "\n").replace("\r", "\n")
    return len(canonical) // 4
