from __future__ import annotations

import base64
import json
import re
import time
import urllib.request
from pathlib import Path
from typing import Mapping

PREFIX = "casa-source-v1."


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def decode_contract_tag(tag: str) -> dict[str, object]:
    assert tag.startswith(PREFIX)
    payload = tag[len(PREFIX):]
    assert payload and "=" not in payload
    raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    parsed = json.loads(raw)
    assert raw == canonical_json(parsed)
    assert set(parsed) == {
        "speaker_kind", "role_id", "persona_id", "persona_version",
        "display_name", "binding_digest", "user_peer", "user_id",
    }
    # Guard the checksum well-formedness the fixture goldens must obey: a
    # non-null binding_digest is exactly "sha256:" + 64 lowercase hex chars.
    # This runs on every decoded fixture tag and every live-recalled tag, so a
    # malformed digest (e.g. the 61-hex slip caught during the entry gate)
    # fails deterministically instead of only under a manual pass.
    digest = parsed.get("binding_digest")
    assert digest is None or (
        isinstance(digest, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is not None
    ), "binding_digest must be null or 'sha256:' + 64 lowercase hex chars"
    return parsed


def _request(
    base_url: str,
    method: str,
    path: str,
    body: object | None = None,
    *,
    timeout: float = 300.0,
) -> tuple[int, object]:
    data = None if body is None else canonical_json(body)
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        return response.status, json.loads(raw) if raw.strip() else {}


def assert_contract(report: Mapping[str, object]) -> None:
    if report.get("require_maximum_cases", True):
        maximum_cases = {
            item["case_id"] for item in report["items"]
            if item.get("maximum_length") is True
        }
        assert maximum_cases == {"max-user-unicode", "max-agent-unicode"}
    for item in report["items"]:
        expected = item["expected_tag"]
        recalled_source = [
            tag for tag in item["recall_tags"]
            if isinstance(tag, str) and tag.startswith("casa-source-")
        ]
        assert recalled_source == [expected], "reserved tag changed or disappeared"
        recalled_tiers = [
            tag for tag in item["recall_tags"]
            if tag in {"public", "friends", "family", "private"}
        ]
        assert recalled_tiers == [item["expected_sensitivity"]], (
            "recalled item must carry exactly its one expected sensitivity tag"
        )
        decoded = decode_contract_tag(recalled_source[0])
        assert decoded == item["expected_provenance"]
        assert len(canonical_json(decoded)) == item["expected_payload_bytes"]
        assert len(recalled_source[0].encode("ascii")) == item["expected_tag_bytes"]
        assert item["expected_payload_bytes"] <= 2048
        assert item["expected_tag_bytes"] <= 2746
    for check in report["tier_checks"]:
        assert check["actual_ids"] == check["expected_ids"]
    # #349: the context round-trip probe was recorded but never enforced — a
    # backend that drops or rewrites retained context must fail the contract.
    assert report["context_probe"]["byte_equal"] is True, (
        "retained context must round-trip byte-equal through recall"
    )


def run_contract(
    base_url: str,
    *,
    bank: str,
    expected_version: str,
    record_path: Path | None = None,
) -> dict[str, object]:
    fixture_path = Path(__file__).resolve().parents[4] / (
        "tests/fixtures/hindsight_provenance_contract_input.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    _, openapi = _request(base_url, "GET", "/openapi.json")
    backend_version = openapi.get("info", {}).get("version")
    if not isinstance(backend_version, str) or not backend_version.strip():
        raise AssertionError("backend did not report info.version at runtime")
    if backend_version != expected_version:
        raise AssertionError(
            f"backend version {backend_version!r} != deployed supported "
            f"version {expected_version!r}"
        )
    retain_body = {"async": False, "items": fixture["retain_items"]}
    retain_status, retain_response = _request(
        base_url,
        "POST",
        f"/v1/default/banks/{bank}/memories",
        retain_body,
    )
    time.sleep(12)

    # Recall once per sensitivity tier. Tag filtering deterministically surfaces
    # every retained item under its own tier, independent of semantic ranking or
    # budget truncation, so `observed` reliably contains all contract cases.
    recall_envelopes = []
    observed = {}
    for tier in ("public", "friends", "family", "private"):
        _, envelope = _request(
            base_url,
            "POST",
            f"/v1/default/banks/{bank}/memories/recall",
            {
                "query": fixture["query"],
                "tags": [tier],
                "tags_match": "any",
                "max_tokens": 8192,
                "types": ["world", "experience", "observation"],
                "budget": "high",
            },
            timeout=90,
        )
        recall_envelopes.append({"tier": tier, "response": envelope})
        for result in envelope.get("results", []):
            for tag in result.get("tags", []):
                if isinstance(tag, str) and tag.startswith("contract-id-"):
                    observed[tag] = result

    items = []
    get_envelopes = []
    for case in fixture["items"]:
        result = observed[case["ordinary_tag"]]
        backend_id = result.get("id")
        if isinstance(backend_id, str) and backend_id:
            status, envelope = _request(
                base_url,
                "GET",
                f"/v1/default/banks/{bank}/memories/{backend_id}",
            )
            get_envelopes.append(
                {"backend_id": backend_id, "status": status, "response": envelope}
            )
        items.append(
            {
                "case_id": case["case_id"],
                "maximum_length": case["maximum_length"],
                "expected_tag": case["provenance_tag"],
                "expected_sensitivity": case["sensitivity"],
                "recall_tags": result.get("tags", []),
                "expected_provenance": case["provenance"],
                "expected_payload_bytes": case["canonical_payload_bytes"],
                "expected_tag_bytes": case["encoded_tag_bytes"],
            }
        )

    tier_checks = []
    for check in fixture["tier_queries"]:
        _, envelope = _request(
            base_url,
            "POST",
            f"/v1/default/banks/{bank}/memories/recall",
            {
                "query": fixture["query"],
                "tags": check["tags"],
                "tags_match": "any",
                "max_tokens": 8192,
                "types": ["world", "experience", "observation"],
                "budget": "high",
            },
            timeout=90,
        )
        # One document yields several extracted facts that all inherit its tags,
        # so a tier can return the same contract id more than once. The contract
        # is about which ids appear under a tier (set semantics), matching the
        # deduplicated expected_ids, so collapse duplicates here.
        actual_ids = sorted(
            {
                tag
                for result in envelope.get("results", [])
                for tag in result.get("tags", [])
                if isinstance(tag, str) and tag.startswith("contract-id-")
            }
        )
        tier_checks.append(
            {
                "tags": check["tags"],
                "expected_ids": sorted(check["expected_ids"]),
                "actual_ids": actual_ids,
                "response": envelope,
            }
        )

    diagnostic = next(item for item in fixture["items"] if item["diagnostic"])
    diagnostic_result = observed[diagnostic["ordinary_tag"]]
    context_equal = diagnostic_result.get("context") == diagnostic["context"]
    report = {
        "backend_hindsight_version": backend_version,
        "expected_hindsight_version": expected_version,
        "bank": bank,
        "retain": {"status": retain_status, "request": retain_body,
                   "response": retain_response},
        "recalls": recall_envelopes,
        "per_memory_get": get_envelopes,
        "items": items,
        "tier_checks": tier_checks,
        "context_probe": {
            "sent": diagnostic["context"],
            "recalled": diagnostic_result.get("context"),
            "byte_equal": context_equal,
        },
    }
    assert_contract(report)
    if record_path is not None:
        record_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return report
