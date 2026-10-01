"""#1156 — a plugin webhook trigger may declare ``secret_owner: provider`` and
its own body cap (``max_body_kib``).

A provider-owned slot's VALUE is written by the plugin's setup tool, never by
Casa. What Casa owns is the slot's binding to the operator's approval: the
reconcile binds it at activation, retiring anything bound to another approval
first, and leaves the plugin unrouted when an older value cannot be removed.
So a value can reach a bound slot only after the bind — i.e. after the setup
that bind releases.

Every routing assertion drives the REAL reconcile into a REAL
``TriggerRegistry`` and, where a request is involved, the REAL
``/webhook/{name}`` handler over that registry.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import trigger_reconcile as tr
import webhook_auth
from plugin_triggers import ack_identity, parse_and_validate
from trigger_acks import TriggerAckStore
from trigger_consent import render_trigger_consent_message
from trigger_registry import TriggerRegistry

PLUGIN = "elevenlabs"
DECLARED = "postcall"
EFF = f"plg-{PLUGIN}--{DECLARED}"
HEADER = "X-Provider-Key"
PROVIDER_AUTH = {"mode": "static_header", "header": HEADER,
                 "tolerance_secs": 300, "secret_owner": "provider"}
CASA_AUTH = {**PROVIDER_AUTH, "secret_owner": "casa"}


def _trigger(owner="provider", **over):
    t = {"name": DECLARED, "type": "webhook", "target": "resident:assistant",
         "auth": {"mode": "static_header", "header": HEADER,
                  "secret_owner": owner}}
    t.update(over)
    return t


def _plugin(artifact_id="art-1", owner="provider", **over):
    return SimpleNamespace(
        name=PLUGIN, artifact_id=artifact_id, path=f"/store/{PLUGIN}",
        version="1.0.0",
        manifest={"name": PLUGIN,
                  "casa": {"setupTool": "setup_postcall",
                           "triggers": [_trigger(owner, **over)]}})


def _ack(acks, *, artifact_id="art-1", auth=PROVIDER_AUTH):
    ident = ack_identity(plugin=PLUGIN, artifact_id=artifact_id,
                         effective=EFF, target="resident:assistant", auth=auth)
    acks.record(identity=ident, plugin=PLUGIN, artifact_id=artifact_id,
                effective=EFF, target="resident:assistant", auth=auth)


def _resolver(plugin):
    def resolve(target):
        return SimpleNamespace(registry_valid=True, plugins=[plugin])
    return resolve


async def _reconcile(registry, plugin, acks, tmp_path):
    return await tr.reconcile_plugin_triggers(
        trigger_registry=registry,
        role_configs={"assistant": SimpleNamespace(channels=["webhook"])},
        channel_manager=None, acks=acks,
        secrets_dir=tmp_path / "webhook_secrets", prompt=False,
        resolver=_resolver(plugin), global_secret_ok=lambda: True)


def _write_value(tmp_path, value: bytes) -> None:
    """What the plugin's setup tool does: an atomic write into its slot."""
    d = tmp_path / "webhook_secrets"
    tmp = d / ".plugin-tmp"
    tmp.write_bytes(value)
    tmp.rename(d / EFF)


def _registry():
    return TriggerRegistry(scheduler=None, app=None, bus=None)


def _client(registry, tmp_path):
    from casa_core import _make_webhook_handler
    from rate_limit import RateLimiter

    bus = MagicMock()
    bus.send = AsyncMock()
    handler = _make_webhook_handler(
        webhook_rate_limiter=RateLimiter(capacity=0, window_s=60.0),
        webhook_secret="", trigger_registry=registry,
        default_role="assistant", bus=bus,
        secrets_dir=tmp_path / "webhook_secrets")
    app = web.Application()
    app.router.add_post("/webhook/{name}", handler)
    return TestClient(TestServer(app)), bus


# ---------------------------------------------------------------------------
# the slot binding
# ---------------------------------------------------------------------------


async def test_provider_route_goes_live_bound_but_empty_and_refuses(tmp_path):
    """Approved → routed, with the slot bound to the approval and NO value:
    Casa mints nothing, every request is 401 until the setup tool writes."""
    registry, acks = _registry(), TriggerAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    assert await _reconcile(registry, p, acks, tmp_path) == []
    assert registry.get_webhook_target(EFF) == "assistant"
    d = tmp_path / "webhook_secrets"
    assert not (d / EFF).exists()
    assert (d / f"{EFF}.ident").exists()

    client, bus = _client(registry, tmp_path)
    async with client:
        r = await client.post(f"/webhook/{EFF}", json={},
                              headers={HEADER: "anything"})
        assert r.status == 401
    assert bus.send.await_count == 0


async def test_setup_gate_releases_on_the_bind_not_on_a_value(tmp_path):
    """The setup-dispatch gate's predicate: a provider route is backed once
    its slot is bound to THIS approval — the value comes from that setup."""
    registry, acks = _registry(), TriggerAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    desired = tr.compute_desired(
        role_configs={"assistant": SimpleNamespace(channels=["webhook"])},
        acks=acks, resolver=_resolver(p), global_secret_ok=lambda: True)
    before = tr.DesiredTriggers(overlay=dict(desired.overlay))
    tr.verify_minted_secrets(before, tmp_path / "webhook_secrets")
    assert EFF not in before.overlay          # unbound → held
    assert tr._needs_mint(desired, tmp_path / "webhook_secrets")

    await _reconcile(registry, p, acks, tmp_path)
    after = tr.DesiredTriggers(overlay=dict(desired.overlay))
    tr.verify_minted_secrets(after, tmp_path / "webhook_secrets")
    assert EFF in after.overlay               # bound, still no value
    assert not (tmp_path / "webhook_secrets" / EFF).exists()
    assert not tr._needs_mint(desired, tmp_path / "webhook_secrets")


async def test_value_written_after_the_bind_authenticates_and_survives_reconcile(
    tmp_path,
):
    registry, acks = _registry(), TriggerAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    await _reconcile(registry, p, acks, tmp_path)
    _write_value(tmp_path, b"wsec_provider_value_1")

    # A later pass under the SAME approval writes nothing and keeps the value.
    assert await _reconcile(registry, p, acks, tmp_path) == []
    assert (tmp_path / "webhook_secrets" / EFF).read_bytes() == \
        b"wsec_provider_value_1"

    client, bus = _client(registry, tmp_path)
    async with client:
        bad = await client.post(f"/webhook/{EFF}", json={},
                                headers={HEADER: "wrong"})
        good = await client.post(f"/webhook/{EFF}", json={"x": 1},
                                 headers={HEADER: "wsec_provider_value_1"})
        assert (bad.status, good.status) == (401, 200)
    assert bus.send.await_count == 1


async def test_replaced_artifact_never_inherits_a_value_whose_removal_failed(
    monkeypatch, tmp_path,
):
    """Design r1 S1 (both reviewers): artifact A's value survives a refused
    retirement; artifact B is approved. B must NOT route while A's value is
    on disk, and once it is removable B routes with an EMPTY slot."""
    registry, acks = _registry(), TriggerAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    await _reconcile(registry, _plugin("art-1"), acks, tmp_path)
    _write_value(tmp_path, b"value-of-artifact-A")

    real_unlink = webhook_auth._unlink_checked
    refuse = {"on": True}

    def unlink(path):
        if refuse["on"] and path.name == EFF:
            return False                        # the live file stays
        return real_unlink(path)

    monkeypatch.setattr(webhook_auth, "_unlink_checked", unlink)
    # the update's retirement fails...
    acks.revoke_artifact("art-1")
    webhook_auth.retire_secret(EFF, secrets_dir=tmp_path / "webhook_secrets")
    assert (tmp_path / "webhook_secrets" / EFF).exists()

    # ...and B is approved.
    _ack(acks, artifact_id="art-2")
    p2 = _plugin("art-2")
    issues = await _reconcile(registry, p2, acks, tmp_path)
    assert [i.reason_code for i in issues] == ["trigger_secret_missing"]
    assert registry.get_webhook_target(EFF) is None

    client, bus = _client(registry, tmp_path)
    async with client:
        r = await client.post(f"/webhook/{EFF}", json={},
                              headers={HEADER: "value-of-artifact-A"})
        assert r.status == 404

    refuse["on"] = False
    assert await _reconcile(registry, p2, acks, tmp_path) == []
    assert registry.get_webhook_target(EFF) == "assistant"
    assert not (tmp_path / "webhook_secrets" / EFF).exists()
    async with _client(registry, tmp_path)[0] as client:
        r = await client.post(f"/webhook/{EFF}", json={},
                              headers={HEADER: "value-of-artifact-A"})
        assert r.status == 401
    assert bus.send.await_count == 0


async def test_reapproval_after_revoke_clears_the_value(tmp_path):
    """Same artifact, new approval generation: the value written under the
    old approval is retired at activation; the re-armed setup writes anew."""
    registry, acks = _registry(), TriggerAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    await _reconcile(registry, p, acks, tmp_path)
    _write_value(tmp_path, b"old-generation")
    acks.revoke_plugin(PLUGIN)          # nothing retired on disk
    _ack(acks)
    assert await _reconcile(registry, p, acks, tmp_path) == []
    assert not (tmp_path / "webhook_secrets" / EFF).exists()


async def test_owner_flip_casa_to_provider_retires_the_casa_token(tmp_path):
    registry, acks = _registry(), TriggerAckStore(path=tmp_path / "acks.json")
    _ack(acks, auth=CASA_AUTH)
    await _reconcile(registry, _plugin(owner="casa"), acks, tmp_path)
    casa_token = (tmp_path / "webhook_secrets" / EFF).read_bytes()
    assert len(casa_token) == 43

    _ack(acks, artifact_id="art-2")
    assert await _reconcile(registry, _plugin("art-2"), acks, tmp_path) == []
    assert not (tmp_path / "webhook_secrets" / EFF).exists()


async def test_owner_flip_provider_to_casa_mints_fresh(tmp_path):
    registry, acks = _registry(), TriggerAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    await _reconcile(registry, _plugin(), acks, tmp_path)
    _write_value(tmp_path, b"provider-value")
    _ack(acks, artifact_id="art-2", auth=CASA_AUTH)
    assert await _reconcile(
        registry, _plugin("art-2", owner="casa"), acks, tmp_path) == []
    minted = (tmp_path / "webhook_secrets" / EFF).read_bytes()
    assert minted != b"provider-value" and len(minted) == 43


async def test_the_key_is_read_with_the_route_not_after_the_body(tmp_path):
    """Diff r1 S1 (Astra): the route is read before the body, so a key read
    AFTER a slow body could belong to an approval that replaced the route
    mid-request. The key is snapshotted with the route: a slot rewritten
    while the body streams does not change what this request verifies with."""
    import asyncio

    registry, acks = _registry(), TriggerAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    await _reconcile(registry, _plugin(), acks, tmp_path)
    _write_value(tmp_path, b"key-at-route-time")
    client, bus = _client(registry, tmp_path)
    started, release = asyncio.Event(), asyncio.Event()

    async def paused_body():
        yield b'{"a":'
        started.set()
        await release.wait()
        yield b' 1}'

    async with client:
        req = asyncio.ensure_future(client.post(
            f"/webhook/{EFF}", data=paused_body(),
            headers={HEADER: "key-written-mid-request"}))
        await asyncio.wait_for(started.wait(), 5)
        await asyncio.sleep(0.05)       # the handler is now awaiting the body
        _write_value(tmp_path, b"key-written-mid-request")
        release.set()
        r = await asyncio.wait_for(req, 5)
        assert r.status == 401
    assert bus.send.await_count == 0


# ---------------------------------------------------------------------------
# the body cap
# ---------------------------------------------------------------------------


async def _routed_with_cap(tmp_path, kib):
    registry, acks = _registry(), TriggerAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    await _reconcile(registry, _plugin(max_body_kib=kib), acks, tmp_path)
    _write_value(tmp_path, b"k")
    return registry


def _body(n: int) -> bytes:
    """Valid JSON of exactly ``n`` bytes."""
    return b'"' + b"a" * (n - 2) + b'"'


async def test_route_record_carries_the_declared_cap(tmp_path):
    registry = await _routed_with_cap(tmp_path, 512)
    assert registry.webhook_route(EFF)["max_body"] == 512 * 1024


async def test_body_up_to_the_declared_cap_is_accepted(tmp_path):
    registry = await _routed_with_cap(tmp_path, 128)
    client, bus = _client(registry, tmp_path)
    async with client:
        r = await client.post(f"/webhook/{EFF}", data=_body(128 * 1024),
                              headers={HEADER: "k"})
        assert r.status == 200
    assert bus.send.await_count == 1


async def test_body_over_the_declared_cap_is_413(tmp_path):
    registry = await _routed_with_cap(tmp_path, 128)
    client, bus = _client(registry, tmp_path)

    async def chunked():
        for _ in range(129):
            yield b"a" * 1024

    async with client:
        sized = await client.post(f"/webhook/{EFF}",
                                  data=_body(128 * 1024 + 1),
                                  headers={HEADER: "k"})
        streamed = await client.post(f"/webhook/{EFF}", data=chunked(),
                                     headers={HEADER: "k"})
        assert (sized.status, streamed.status) == (413, 413)
    assert bus.send.await_count == 0


async def test_undeclared_cap_stays_64_kib(tmp_path):
    registry, acks = _registry(), TriggerAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    await _reconcile(registry, _plugin(), acks, tmp_path)
    _write_value(tmp_path, b"k")
    assert registry.webhook_route(EFF)["max_body"] == 64 * 1024
    client, _ = _client(registry, tmp_path)
    async with client:
        ok = await client.post(f"/webhook/{EFF}", data=_body(64 * 1024),
                               headers={HEADER: "k"})
        over = await client.post(f"/webhook/{EFF}", data=_body(64 * 1024 + 1),
                                 headers={HEADER: "k"})
        assert (ok.status, over.status) == (200, 413)


async def test_unknown_name_is_404_whatever_the_body_size(tmp_path):
    client, _ = _client(_registry(), tmp_path)
    async with client:
        r = await client.post("/webhook/plg-nobody--x", data=_body(2 << 20))
        assert r.status == 404


def test_resident_route_reads_the_default_cap():
    registry = _registry()
    registry._webhook_routes["res"] = {
        "role": "assistant", "clearance": "public",
        "auth": {"mode": "hmac_body"}}
    assert registry.webhook_route("res")["max_body"] == 64 * 1024


# ---------------------------------------------------------------------------
# the consent prompt names both
# ---------------------------------------------------------------------------


def test_consent_prompt_names_provider_owner_and_raised_cap():
    trig, errs = parse_and_validate(PLUGIN, {"casa": {
        "setupTool": "setup_postcall", "triggers": [
            _trigger(max_body_kib=1024)]}})
    assert errs == []
    text = render_trigger_consent_message(
        plugin=PLUGIN, effective=EFF, role="assistant", auth=trig[0]["auth"],
        max_body_kib=trig[0]["max_body_kib"])
    assert "generated by the provider" in text
    assert "up to 1024 KiB" in text


def test_consent_prompt_unchanged_for_casa_owner_and_default_cap():
    text = render_trigger_consent_message(
        plugin=PLUGIN, effective=EFF, role="assistant", auth=CASA_AUTH)
    assert "provider" not in text
    assert "KiB" not in text
