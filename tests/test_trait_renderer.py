import json
from pathlib import Path

import pytest

from trait_renderer import (
    AXES,
    POSTURE_CLAUSE_V1,
    RENDERER_VERSION,
    estimate_tokens_v1,
    render_v1,
)

FIXTURE = Path(__file__).parent / "fixtures" / "trait_renderer_v1.json"


def test_fixture_versions_match_runtime_constants() -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["renderer"] == RENDERER_VERSION


@pytest.mark.parametrize("cardinality", range(9))
def test_selected_axis_cardinalities_are_exact_bytes(cardinality: int) -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    case = data["cardinalities"][str(cardinality)]
    assert render_v1(case["traits"], case["posture"]).encode("utf-8") == (
        case["expected"].encode("utf-8")
    )


@pytest.mark.parametrize("axis", AXES)
@pytest.mark.parametrize("level", (1, 2, 4, 5))
def test_every_non_neutral_phrase_mapping(axis: str, level: int) -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    traits = {name: 3 for name in AXES}
    traits[axis] = level
    expected = (
        "Interpersonal manner: be "
        + data["phrases"][axis][str(level)]
        + ". "
        + POSTURE_CLAUSE_V1["professional"]
        + " "
        + data["invariant"]
    )
    assert render_v1(traits, "professional") == expected


def test_estimator_normalizes_nfc_and_line_endings() -> None:
    assert estimate_tokens_v1("é\r\nabcd") == estimate_tokens_v1("é\nabcd")


def test_estimator_case_goldens() -> None:
    # Consumes every fixture estimator case so none is dead data. Case 0 uses a
    # DECOMPOSED "e" + U+0301 (combining acute) plus a CRLF: under the specified
    # NFC+LF normalization it composes/collapses to 6 scalars -> 6//4 == 1. An
    # estimator that skipped NFC would count 8 -> 2 and fail here, so this golden
    # actively guards the normalization step.
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for case in data["estimator_cases"]:
        assert estimate_tokens_v1(case["input"]) == case["expected"], case["input"]
