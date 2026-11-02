"""Task 3 (X4): the admission digest — RFC 8785 + SHA-256 over every RunSpec member except
``admission``, computed over the body as received. The vector file is the canonical shared
copy; Plan 2 keeps a byte-identical one."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import rfc8785

from casa_browser.protocol import canonical_json, spec_digest

from apihelpers import headers, paired_client, run_body, run_members

VECTORS = json.loads((Path(__file__).parent / "vectors" / "spec_digest.json").read_text("utf-8"))


@pytest.mark.parametrize("v", VECTORS, ids=[v["name"] for v in VECTORS])
def test_vectors(v):
    assert canonical_json(v["members"]).decode("utf-8") == v["canonical"]
    assert rfc8785.dumps(v["members"]).decode("utf-8") == v["canonical"]
    assert spec_digest(v["members"]) == v["digest"]
    assert v["digest"] == "sha256:" + hashlib.sha256(v["canonical"].encode("utf-8")).hexdigest()


@pytest.mark.parametrize("bad", [{"a": 1.5}, {"é": 1}, {"a": {"ü": 1}}, {"a": {1, 2}},
                                 {"a": 2**53}, {"a": -(2**53)}, {"a": [1.0]}, {"a": b"x"},
                                 {"a": "\ud800"}, {1: "x"}])
def test_refuses_floats_and_non_ascii_keys(bad):
    with pytest.raises(ValueError):
        spec_digest(bad)


async def test_digest_over_received_members(aiohttp_client, tmp_path):
    client, fake, secret = await paired_client(aiohttp_client, tmp_path)
    vector = VECTORS[0]
    members = dict(vector["members"])
    members["sites"] = [{"site_id": "site_a", "access": "account"}]
    digest = spec_digest(members)
    body = {**members, "admission": {"origin_kind": "operator_telegram", "origin_ref": "tg-1",
                                     "admitted_spec_digest": digest}}
    assert (await client.post("/v1/runs", json=body, headers=headers(secret))).status == 201
    # One changed member: refused, nothing queued.
    changed = {**body, "request_id": "req-changed", "task_text": body["task_text"] + "!"}
    changed["admission"] = dict(body["admission"])
    resp = await client.post("/v1/runs", json=changed, headers=headers(secret))
    assert (resp.status, await resp.json()) == (403, {"error": "blocked"})
    assert fake.store.list_runs(("queued", "running")) and all(
        r.spec.request_id != "req-changed" for r in fake.store.list_runs(("queued", "running")))
    # The same members in another order: accepted (idempotent repeat of the first run).
    reordered_text = "{" + ",".join(
        f"{json.dumps(k)}:{json.dumps(body[k])}" for k in reversed(list(body))) + "}"
    resp = await client.post("/v1/runs", data=reordered_text.encode(),
                             headers={**headers(secret), "Content-Type": "application/json"})
    assert resp.status == 200
    # A body missing a member: 400.
    missing = {k: v for k, v in body.items() if k != "result_expectation"}
    resp = await client.post("/v1/runs", json=missing, headers=headers(secret))
    assert (resp.status, await resp.json()) == (400, {"error": "unsupported"})


async def test_digest_received_not_reserialised(aiohttp_client, tmp_path):
    """The digest is over the members as received: a non-ASCII task text hashed by its
    UTF-8 bytes, not by an ASCII-escaped re-serialisation."""
    client, fake, secret = await paired_client(aiohttp_client, tmp_path)
    body = run_body("req-u", task_text="Scarica la fattura — €42 🧾")
    raw = json.dumps(body, ensure_ascii=True).encode()  # escaped on the wire
    resp = await client.post("/v1/runs", data=raw,
                             headers={**headers(secret), "Content-Type": "application/json"})
    assert resp.status == 201
    assert body["admission"]["admitted_spec_digest"] == "sha256:" + hashlib.sha256(
        rfc8785.dumps(run_members("req-u", task_text="Scarica la fattura — €42 🧾"))).hexdigest()
