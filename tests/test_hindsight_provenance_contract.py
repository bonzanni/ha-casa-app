import json
from pathlib import Path

from hindsight_provenance_contract import (
    assert_contract,
    canonical_json,
    decode_contract_tag,
)

FIXTURE = Path(__file__).parent / "fixtures" / "hindsight_provenance_contract_input.json"


def test_fixture_tags_decode_to_exact_canonical_snapshots() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for case in raw["items"]:
        decoded = decode_contract_tag(case["provenance_tag"])
        assert decoded == case["provenance"]
        payload = case["provenance_tag"].split(".", 1)[1]
        assert "=" not in payload
        assert canonical_json(decoded) == canonical_json(case["provenance"])
        assert len(canonical_json(decoded)) == case["canonical_payload_bytes"]
        assert len(case["provenance_tag"].encode("ascii")) == case["encoded_tag_bytes"]
        assert case["canonical_payload_bytes"] <= 2048
        assert case["encoded_tag_bytes"] <= 2746


def test_contract_rejects_altered_or_missing_reserved_tags() -> None:
    report = {
        "require_maximum_cases": False,
        "items": [
            {
                "expected_tag": "casa-source-v1.expected",
                "expected_sensitivity": "friends",
                "recall_tags": ["friends", "private", "casa-source-v1.changed"],
                "expected_provenance": {},
                "expected_payload_bytes": 2,
                "expected_tag_bytes": 23,
            }
        ],
        "tier_checks": [],
        "context_probe": {"sent": None, "recalled": None, "byte_equal": True},
    }
    try:
        assert_contract(report)
    except AssertionError as exc:
        assert "reserved tag changed" in str(exc)
    else:
        raise AssertionError("altered reserved tag was accepted")


def test_contract_rejects_context_probe_mismatch() -> None:
    """#349: `run_contract` records the context round-trip probe but
    `assert_contract` never enforced it — a backend that drops or rewrites
    retained context must FAIL the contract."""
    report = {
        "require_maximum_cases": False,
        "items": [],
        "tier_checks": [],
        "context_probe": {
            "sent": {"k": "v"},
            "recalled": {"k": "rewritten"},
            "byte_equal": False,
        },
    }
    try:
        assert_contract(report)
    except AssertionError as exc:
        assert "context" in str(exc)
    else:
        raise AssertionError("a dropped/rewritten context was accepted")


def test_contract_requires_context_probe_present() -> None:
    """A report missing the probe entirely must not pass silently."""
    report = {
        "require_maximum_cases": False,
        "items": [],
        "tier_checks": [],
    }
    import pytest
    with pytest.raises((AssertionError, KeyError)):
        assert_contract(report)


def test_fixture_contains_unicode_and_maximum_length_cases() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    cases = {case["case_id"]: case for case in raw["items"]}
    assert {"max-user-unicode", "max-agent-unicode"} <= cases.keys()
    for case_id in ("max-user-unicode", "max-agent-unicode"):
        case = cases[case_id]
        assert case["maximum_length"] is True
        assert "\U0001f4a9" in json.dumps(
            case["provenance"], ensure_ascii=False
        )
        assert case["canonical_payload_bytes"] <= 2048
        assert case["encoded_tag_bytes"] <= 2746
