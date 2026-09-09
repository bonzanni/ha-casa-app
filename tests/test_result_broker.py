"""#792: the result broker — the store, the three hooks, the two routes, the
builders that carry them, and the plugin lifecycle that purges references.

Every guard here has a mutation that flips exactly one assertion. Counts and
values, never names. Async tests are marked individually (no module-level
asyncio mark — CC's hygiene guard)."""
from __future__ import annotations

import asyncio
import json
import re
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import make_mocked_request

import agent as agent_mod
import tools as tools_mod
import result_broker as rb
from authz_grants import GrantIdentity
from plugin_grants import PluginContract, ResultContractMap, ToolContract

ARTIFACT = "1" * 64
OTHER_ARTIFACT = "2" * 64
SENTINEL = "LIVE-CAPABILITY-VALUE-3f9a"
PLUGIN = "probe"
FETCH = "mcp__plugin_probe_api__fetch"
DONE = "mcp__plugin_probe_api__done"
SWAP = "mcp__plugin_probe_api__swap"      # consumes AND provides
LIST = "mcp__plugin_probe_api__list"
SETUP = "mcp__plugin_probe_api__setup_probe"
LEGACY = "mcp__plugin_legacy_srv__anything"
UNKNOWN = "mcp__plugin_nobody_x__y"


def _identity(**over) -> GrantIdentity:
    base = dict(operator_id=100, chat_id=42, enforcement_role="finance",
                artifact_id=ARTIFACT, engagement_id="")
    base.update(over)
    return GrantIdentity(**base)


def _map() -> ResultContractMap:
    tools = {
        FETCH: ToolContract(ARTIFACT, PLUGIN, "capability", ("link",), {}),
        DONE: ToolContract(ARTIFACT, PLUGIN, "safe", (), {"l": "link"}),
        SWAP: ToolContract(ARTIFACT, PLUGIN, "capability", ("token",), {"l": "link"}),
        LIST: ToolContract(ARTIFACT, PLUGIN, "safe", (), {}),
    }
    plugins = {
        PLUGIN: PluginContract(ARTIFACT, True, frozenset({SETUP})),
        "legacy": PluginContract(OTHER_ARTIFACT, False, frozenset()),
    }
    return ResultContractMap(tools=tools, plugins=plugins)


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _store():
    clock = _Clock()
    return rb.ReferenceStore(now=clock), clock


def _open(store, *, client="c1", call="call-1", identity=None, provides=("link",),
          artifact=ARTIFACT, tool=FETCH):
    store.open_call(client_id=client, artifact_id=artifact, tool_name=tool,
                    tool_use_id=call, identity=identity or _identity(),
                    provides=provides)


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------

def test_reference_shape_and_redaction():
    ref = rb.new_reference()
    assert re.fullmatch(r"casa-cap-[0-9a-f]{32}", ref)
    assert rb.is_reference(ref) and not rb.is_reference(ref + "x")
    from log_redact import redact
    assert redact(f"ref {ref} sent") == f"ref {ref} sent"


def test_ttl_is_the_challenge_plus_grant_window():
    from authz_grants import DEFAULT_GRANT_TTL_S, _CHALLENGE_TTL_S
    assert rb.reference_ttl_s() == _CHALLENGE_TTL_S + DEFAULT_GRANT_TTL_S


def test_deposit_binds_to_the_unique_in_flight_call():
    store, _ = _store()
    assert store.deposit(client_id="c1", slot="link", value=SENTINEL) == (None, "no_call_in_flight")
    _open(store)
    ref, err = store.deposit(client_id="c1", slot="link", value=SENTINEL)
    assert err is None and rb.is_reference(ref)
    assert store.deposit(client_id="c1", slot="link", value="again") == (None, "slot_already_deposited")
    assert store.deposit(client_id="c1", slot="other", value="x") == (None, "no_call_in_flight")
    assert store.deposit(client_id="c2", slot="link", value="x") == (None, "no_call_in_flight")
    assert store.reference_count() == 1
    assert SENTINEL not in repr(store._refs[ref])


def test_ambiguous_deposit_is_refused():
    store, _ = _store()
    _open(store, call="a")
    _open(store, call="b")
    assert store.deposit(client_id="c1", slot="link", value=SENTINEL) == (None, "ambiguous_call")
    assert store.reference_count() == 0


def test_deposit_needs_an_identity_on_the_call():
    store, _ = _store()
    store.open_call(client_id="c1", artifact_id=ARTIFACT, tool_name=FETCH,
                    tool_use_id="x", identity=None, provides=("link",))
    assert store.deposit(client_id="c1", slot="link", value=SENTINEL) == (None, "no_identity")


def test_validate_result_requires_every_slot_with_its_own_reference():
    store, _ = _store()
    _open(store, provides=("link", "code"))
    r1, _ = store.deposit(client_id="c1", slot="link", value="v1")
    r2, _ = store.deposit(client_id="c1", slot="code", value="v2")
    call = store.close_call("c1", "call-1")
    assert store.validate_result(call, {"link": r1, "code": r2, "extra": 1}) is True
    assert store.validate_result(call, {"link": r1}) is False
    assert store.validate_result(call, {"link": r2, "code": r1}) is False
    assert store.validate_result(call, {"link": r1, "code": rb.new_reference()}) is False
    assert store.validate_result(call, ["not", "a", "dict"]) is False


def _minted(store, clock=None, **id_over):
    _open(store, identity=_identity(**id_over))
    ref, _ = store.deposit(client_id="c1", slot="link", value=SENTINEL)
    store.close_call("c1", "call-1")
    return ref


def test_arm_requires_same_identity_and_slot():
    store, _ = _store()
    ref = _minted(store)
    assert store.arm(reference=ref, identity=_identity(operator_id=7), slot="link",
                     client_id="c9", tool_use_id="d") is None
    assert store.arm(reference=ref, identity=_identity(chat_id=9), slot="link",
                     client_id="c9", tool_use_id="d") is None
    assert store.arm(reference=ref, identity=_identity(enforcement_role="other"), slot="link",
                     client_id="c9", tool_use_id="d") is None
    assert store.arm(reference=ref, identity=_identity(artifact_id=OTHER_ARTIFACT), slot="link",
                     client_id="c9", tool_use_id="d") is None
    assert store.arm(reference=ref, identity=_identity(engagement_id="eng"), slot="link",
                     client_id="c9", tool_use_id="d") is None
    assert store.arm(reference=ref, identity=_identity(), slot="code",
                     client_id="c9", tool_use_id="d") is None
    assert store.arm(reference="casa-cap-" + "f" * 32, identity=_identity(), slot="link",
                     client_id="c9", tool_use_id="d") is None
    ticket = store.arm(reference=ref, identity=_identity(), slot="link",
                       client_id="c9", tool_use_id="d")
    assert re.fullmatch(r"[0-9a-f]{32}", ticket)


def test_redeem_once_with_the_arming_call_in_flight():
    store, _ = _store()
    ref = _minted(store)
    # the consumer call must be in flight in the arming client
    _open(store, client="c9", call="d", provides=(), tool=DONE)
    ticket = store.arm(reference=ref, identity=_identity(), slot="link",
                       client_id="c9", tool_use_id="d")
    assert store.redeem(client_id="c9", reference=ref, ticket="0" * 32) == (None, "bad_ticket")
    assert store.redeem(client_id="c1", reference=ref, ticket=ticket) == (None, "wrong_client")
    assert store.redeem(client_id="c9", reference=ref, ticket=ticket) == (SENTINEL, None)
    assert store.redeem(client_id="c9", reference=ref, ticket=ticket) == (None, "unknown_or_used")
    assert store.reference_count() == 0


def test_redeem_refused_when_not_armed_or_call_closed():
    store, _ = _store()
    ref = _minted(store)
    assert store.redeem(client_id="c9", reference=ref, ticket="0" * 32) == (None, "not_armed")
    ticket = store.arm(reference=ref, identity=_identity(), slot="link",
                       client_id="c9", tool_use_id="d")   # no in-flight consumer call
    assert store.redeem(client_id="c9", reference=ref, ticket=ticket) == (None, "call_not_in_flight")
    assert SENTINEL not in json.dumps(store.redeem(client_id="c9", reference=ref, ticket=ticket))


def test_rearming_replaces_the_ticket():
    store, _ = _store()
    ref = _minted(store)
    _open(store, client="c9", call="d1", provides=(), tool=DONE)
    _open(store, client="c9", call="d2", provides=(), tool=DONE)
    t1 = store.arm(reference=ref, identity=_identity(), slot="link", client_id="c9", tool_use_id="d1")
    t2 = store.arm(reference=ref, identity=_identity(), slot="link", client_id="c9", tool_use_id="d2")
    assert t1 != t2
    assert store.redeem(client_id="c9", reference=ref, ticket=t1) == (None, "bad_ticket")
    assert store.redeem(client_id="c9", reference=ref, ticket=t2) == (SENTINEL, None)


def test_reference_expires_after_ttl_and_calls_after_cap():
    store, clock = _store()
    ref = _minted(store)
    clock.t += rb.reference_ttl_s() + 1
    assert store.reference_count() == 0
    assert store.arm(reference=ref, identity=_identity(), slot="link",
                     client_id="c9", tool_use_id="d") is None
    _open(store, call="late")
    clock.t += rb.INFLIGHT_CAP_S + 1
    assert store.inflight_count() == 0


def test_two_racing_redemptions_release_exactly_one_value():
    store, _ = _store()
    ref = _minted(store)
    _open(store, client="c9", call="d", provides=(), tool=DONE)
    ticket = store.arm(reference=ref, identity=_identity(), slot="link",
                       client_id="c9", tool_use_id="d")
    results = []
    barrier = threading.Barrier(2)

    def go():
        barrier.wait()
        results.append(store.redeem(client_id="c9", reference=ref, ticket=ticket))

    ts = [threading.Thread(target=go) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sum(1 for v, e in results if v == SENTINEL) == 1
    assert sum(1 for v, e in results if e) == 1


def test_purge_by_artifact_and_by_role_and_fresh_store_is_empty():
    store, _ = _store()
    _minted(store)
    _open(store, call="pending")
    assert store.reference_count() == 1 and store.inflight_count() == 1
    assert store.purge_artifact(OTHER_ARTIFACT) == 0
    assert store.purge_artifact(ARTIFACT) == 1
    assert store.reference_count() == 0 and store.inflight_count() == 0
    _minted(store)
    assert store.purge_role("other") == 0
    assert store.purge_role("finance") == 1
    assert rb.ReferenceStore().reference_count() == 0


# ---------------------------------------------------------------------------
# the hooks — driven directly, like the red case
# ---------------------------------------------------------------------------

class _Origin:
    """A dm/direct finance origin (role == execution_role), or none."""

    def __init__(self, origin, engagement=None):
        self._origin, self._eng = origin, engagement

    def __enter__(self):
        self._o = agent_mod.origin_var.set(self._origin)
        self._e = tools_mod.engagement_var.set(self._eng)

    def __exit__(self, *exc):
        agent_mod.origin_var.reset(self._o)
        tools_mod.engagement_var.reset(self._e)


DM = {"role": "finance", "channel": "telegram", "chat_id": "42", "user_id": 100,
      "cid": "abc", "message_type": "channel_in", "source": "telegram",
      "execution_role": "finance"}


def _pre(tool, tool_input=None):
    return {"hook_event_name": "PreToolUse", "tool_name": tool,
            "tool_input": dict(tool_input or {})}


def _post(tool, response, tool_input=None):
    return {"hook_event_name": "PostToolUse", "tool_name": tool,
            "tool_input": dict(tool_input or {}), "tool_response": response}


def _deny_reason(out):
    return out["hookSpecificOutput"]["permissionDecisionReason"]


def _withheld_body(out):
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse"
    return hso["updatedToolOutput"]


@pytest.mark.asyncio
async def test_admission_branches():
    store, _ = _store()
    hook = rb.make_plugin_admission_hook("finance", _map(), client_id="c1", store=store)
    assert await hook(_pre("Bash", {"command": "ls"}), "t", {}) == {}
    assert await hook(_pre(SETUP), "t", {}) == {}
    assert "not executed" in _deny_reason(await hook(_pre(UNKNOWN), "t", {}))
    assert "casa.resultContract" in _deny_reason(await hook(_pre(LEGACY, {"secret_arg": "S3CR3T"}), "t", {}))
    assert "S3CR3T" not in _deny_reason(await hook(_pre(LEGACY, {"secret_arg": "S3CR3T"}), "t", {}))
    assert "not declared" in _deny_reason(await hook(_pre("mcp__plugin_probe_api__undeclared"), "t", {}))
    assert await hook(_pre(LIST), "t", {}) == {}
    # a capability tool with no operator-bound turn: denied, nothing registered
    with _Origin(None):
        assert "operator" in _deny_reason(await hook(_pre(FETCH), "t", {}))
    assert store.inflight_count() == 0
    with _Origin(DM):
        assert await hook(_pre(FETCH), "call-1", {}) == {}
    assert store.inflight_count() == 1


@pytest.mark.asyncio
async def test_admission_arms_a_declared_consumer_and_denies_a_stale_reference():
    store, _ = _store()
    hook = rb.make_plugin_admission_hook("finance", _map(), client_id="c1", store=store)
    with _Origin(DM):
        await hook(_pre(FETCH), "call-1", {})
        ref, _ = store.deposit(client_id="c1", slot="link", value=SENTINEL)
        store.close_call("c1", "call-1")
        out = await hook(_pre(DONE, {"l": ref, "note": "x"}), "call-2", {})
        hso = out["hookSpecificOutput"]
        assert hso["hookEventName"] == "PreToolUse"
        assert hso["updatedInput"]["note"] == "x"
        ref2, ticket = hso["updatedInput"]["l"].split(":")
        assert ref2 == ref and re.fullmatch(r"[0-9a-f]{32}", ticket)
        assert store.redeem(client_id="c1", reference=ref, ticket=ticket) == (SENTINEL, None)
        # an inert string in the declared parameter is left alone
        assert await hook(_pre(DONE, {"l": "not-a-reference"}), "call-3", {}) == {}
        # a spent / unknown reference is a deny, never a pass
        assert "not redeemable" in _deny_reason(await hook(_pre(DONE, {"l": ref}), "call-4", {}))
        assert "not redeemable" in _deny_reason(
            await hook(_pre(DONE, {"l": "casa-cap-" + "9" * 32}), "call-5", {}))


@pytest.mark.asyncio
async def test_admission_refuses_a_reference_from_another_identity():
    store, _ = _store()
    hook = rb.make_plugin_admission_hook("finance", _map(), client_id="c1", store=store)
    ref = _minted(store, operator_id=999)         # minted for another operator
    with _Origin(DM):
        assert "not redeemable" in _deny_reason(await hook(_pre(DONE, {"l": ref}), "d", {}))
    assert store.redeem(client_id="c1", reference=ref, ticket="0" * 32) == (None, "not_armed")


@pytest.mark.asyncio
async def test_exchange_tool_registers_and_arms():
    store, _ = _store()
    hook = rb.make_plugin_admission_hook("finance", _map(), client_id="c1", store=store)
    ref = _minted(store)
    with _Origin(DM):
        out = await hook(_pre(SWAP, {"l": ref}), "swap-1", {})
    assert out["hookSpecificOutput"]["updatedInput"]["l"].startswith(ref + ":")
    assert store.inflight_count() == 1
    assert store.deposit(client_id="c1", slot="token", value="t")[1] is None


@pytest.mark.asyncio
async def test_authorization_runs_before_arming_and_its_deny_arms_nothing():
    store, _ = _store()
    calls = []

    async def authz_deny(input_data, tool_use_id, ctx):
        calls.append(tool_use_id)
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "permissionDecision": "deny",
                                       "permissionDecisionReason": "needs approval"}}

    async def authz_allow(input_data, tool_use_id, ctx):
        calls.append(tool_use_id)
        return {}

    ref = _minted(store)
    protected = {DONE: {"artifact_id": ARTIFACT, "summary": None}}
    deny_hook = rb.make_plugin_admission_hook(
        "finance", _map(), client_id="c1", store=store, authz_hook=authz_deny, protected=protected)
    with _Origin(DM):
        out = await deny_hook(_pre(DONE, {"l": ref}), "d1", {})
    assert _deny_reason(out) == "needs approval" and calls == ["d1"]
    assert store._refs[ref].armed is None                 # nothing armed
    allow_hook = rb.make_plugin_admission_hook(
        "finance", _map(), client_id="c1", store=store, authz_hook=authz_allow, protected=protected)
    with _Origin(DM):
        out = await allow_hook(_pre(DONE, {"l": ref}), "d2", {})
    assert calls == ["d1", "d2"] and "updatedInput" in out["hookSpecificOutput"]
    # a non-adopting plugin's tool is refused BEFORE authorization is consulted
    with _Origin(DM):
        await allow_hook(_pre(LEGACY), "d3", {})
    assert calls == ["d1", "d2"]
    # an unprotected tool never consults authorization
    with _Origin(DM):
        await allow_hook(_pre(LIST), "d4", {})
    assert calls == ["d1", "d2"]
    assert getattr(allow_hook, "_casa_authz_role") == "finance"


@pytest.mark.asyncio
async def test_admission_exception_is_a_deny_never_a_pass():
    broken = SimpleNamespace(plugins=None, tools=None,
                             plugin_seg_of=lambda name: (_ for _ in ()).throw(RuntimeError("x")))
    hook = rb.make_plugin_admission_hook("finance", broken, client_id="c1", store=_store()[0])
    out = await hook(_pre(FETCH), "t", {})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.asyncio
async def test_result_hook_branches():
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    resp = json.dumps({"secret_field": SENTINEL})
    assert await hook(_post("Bash", "x"), "t", {}) == {}
    assert await hook(_post(SETUP, resp), "t", {}) == {}
    assert await hook(_post(LIST, resp), "t", {}) == {}
    for tool in (UNKNOWN, LEGACY, "mcp__plugin_probe_api__undeclared"):
        body = _withheld_body(await hook(_post(tool, resp), "t", {}))
        parsed = json.loads(body)
        assert parsed["casa_result_withheld"] is True
        assert body.count(SENTINEL) == 0 and body.count("secret_field") == 0
    assert json.loads(_withheld_body(await hook(_post(LEGACY, resp), "t", {})))["plugin"] == "legacy"


@pytest.mark.asyncio
async def test_capability_result_passes_only_with_its_own_references():
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    # a conforming producer: deposit during the call, return the reference
    _open(store)
    ref, _ = store.deposit(client_id="c1", slot="link", value=SENTINEL)
    assert await hook(_post(FETCH, json.dumps({"account": "ops", "link": ref})), "call-1", {}) == {}
    assert store.inflight_count() == 0 and store.reference_count() == 1
    # the list-of-blocks projection (measured for a dict-returning tool)
    _open(store, call="call-2")
    ref2, _ = store.deposit(client_id="c1", slot="link", value=SENTINEL)
    blocks = [{"type": "text", "text": json.dumps({"link": ref2})}]
    assert await hook(_post(FETCH, blocks), "call-2", {}) == {}
    # a result that omits the declared slot: withheld, its deposit dropped
    _open(store, call="call-3")
    ref3, _ = store.deposit(client_id="c1", slot="link", value=SENTINEL)
    body = _withheld_body(await hook(_post(FETCH, json.dumps({"account": "ops"})), "call-3", {}))
    assert body.count(SENTINEL) == 0 and ref3 not in store._refs
    # a foreign reference, a non-object result, a call never admitted
    _open(store, call="call-4")
    store.deposit(client_id="c1", slot="link", value=SENTINEL)
    _withheld_body(await hook(_post(FETCH, json.dumps({"link": rb.new_reference()})), "call-4", {}))
    _open(store, call="call-5")
    store.deposit(client_id="c1", slot="link", value=SENTINEL)
    _withheld_body(await hook(_post(FETCH, "just a string"), "call-5", {}))
    _withheld_body(await hook(_post(FETCH, json.dumps({"link": ref})), "never-opened", {}))
    _withheld_body(await hook(_post(FETCH, {"dict": "projection"}), "x", {}))


@pytest.mark.asyncio
async def test_result_hook_exception_withholds():
    broken = SimpleNamespace(plugins=None, tools=None,
                             plugin_seg_of=lambda name: (_ for _ in ()).throw(RuntimeError("x")))
    hook = rb.make_result_hook(broken, client_id="c1", store=_store()[0])
    body = _withheld_body(await hook(_post(FETCH, SENTINEL), "t", {}))
    assert body.count(SENTINEL) == 0


@pytest.mark.asyncio
async def test_failure_hook_closes_the_call_and_drops_its_deposits():
    store, _ = _store()
    hook = rb.make_failure_hook(_map(), client_id="c1", store=store)
    _open(store)
    store.deposit(client_id="c1", slot="link", value=SENTINEL)
    out = await hook({"hook_event_name": "PostToolUseFailure", "tool_name": FETCH,
                      "tool_input": {}, "error": "Error executing tool: " + SENTINEL}, "call-1", {})
    assert out == {} and store.inflight_count() == 0 and store.reference_count() == 0
    assert await hook({"tool_name": "Bash"}, "t", {}) == {}


def test_matchers_and_env_shape():
    from claude_agent_sdk import HookMatcher
    res = SimpleNamespace(plugins=[])
    ms = rb.broker_matchers("finance", res, client_id="abc")
    assert sorted(ms) == ["PostToolUse", "PostToolUseFailure", "PreToolUse"]
    for event, lst in ms.items():
        assert len(lst) == 1 and isinstance(lst[0], HookMatcher)
        assert lst[0].matcher == "mcp__plugin_.*"
        assert re.fullmatch(lst[0].matcher, FETCH) and not re.fullmatch(lst[0].matcher, "Bash")
    assert ms["PreToolUse"][0].hooks[0]._casa_result_broker == "admission"
    assert ms["PreToolUse"][0].hooks[0]._casa_result_broker_client == "abc"
    assert rb.broker_env("abc") == {"CASA_BROKER_CLIENT": "abc",
                                    "CASA_BROKER_SOCKET": "/run/casa/internal.sock"}
    assert not any(k.startswith("CASA_PLUGIN_") for k in rb.broker_env("abc"))


# ---------------------------------------------------------------------------
# the routes — resolved without a listening socket
# ---------------------------------------------------------------------------

async def _call(handler, body):
    from aiohttp import web
    app = web.Application()
    app.router.add_post("/x", handler)
    payload = body if isinstance(body, bytes) else json.dumps(body).encode()
    import aiohttp
    from unittest.mock import MagicMock
    stream = aiohttp.streams.StreamReader(MagicMock(), 2 ** 16, loop=asyncio.get_running_loop())
    stream.feed_data(payload)
    stream.feed_eof()
    req = make_mocked_request("POST", "/x", payload=stream, app=app,
                              headers={"Content-Type": "application/json"})
    resp = await handler(req)
    return resp.status, json.loads(resp.body)


@pytest.mark.asyncio
async def test_deposit_and_redeem_routes():
    store, _ = _store()
    dep = rb.build_broker_deposit_handler(store)
    red = rb.build_broker_redeem_handler(store)
    assert await _call(dep, b"{") == (200, {"error": "bad_json"})
    assert await _call(dep, ["x"]) == (200, {"error": "bad_json"})
    assert await _call(dep, {"slot": "link", "value": "v"}) == (200, {"error": "bad_client"})
    assert await _call(dep, {"client": "c1", "value": "v"}) == (200, {"error": "bad_slot"})
    assert await _call(dep, {"client": "c1", "slot": "link"}) == (200, {"error": "bad_value"})
    assert await _call(dep, {"client": "c1", "slot": "link", "value": "x" * (rb.MAX_VALUE_BYTES + 1)}) == (
        200, {"error": "value_too_large"})
    assert await _call(dep, {"client": "c1", "slot": "link", "value": SENTINEL}) == (
        200, {"error": "no_call_in_flight"})
    _open(store)
    status, body = await _call(dep, {"client": "c1", "slot": "link", "value": SENTINEL})
    assert status == 200 and rb.is_reference(body["reference"])
    ref = body["reference"]
    store.close_call("c1", "call-1")
    _open(store, client="c9", call="d", provides=(), tool=DONE)
    ticket = store.arm(reference=ref, identity=_identity(), slot="link", client_id="c9", tool_use_id="d")
    assert await _call(red, {"client": "c9", "reference": "nope", "ticket": ticket}) == (200, {"error": "bad_reference"})
    assert await _call(red, {"client": "c9", "reference": ref, "ticket": "zz"}) == (200, {"error": "bad_ticket"})
    assert await _call(red, {"reference": ref, "ticket": ticket}) == (200, {"error": "bad_client"})
    assert await _call(red, {"client": "c1", "reference": ref, "ticket": ticket}) == (200, {"error": "wrong_client"})
    assert await _call(red, {"client": "c9", "reference": ref, "ticket": ticket}) == (200, {"value": SENTINEL})
    assert await _call(red, {"client": "c9", "reference": ref, "ticket": ticket}) == (200, {"error": "unknown_or_used"})


# ---------------------------------------------------------------------------
# the builders
# ---------------------------------------------------------------------------

def _broker_hooks(opts):
    out = {}
    for event, ms in (opts.hooks or {}).items():
        for m in ms:
            for h in m.hooks:
                tag = getattr(h, "_casa_result_broker", None)
                if tag:
                    out.setdefault(event, []).append(h)
    return out


def test_executor_builder_carries_no_broker(tmp_path, monkeypatch):
    """Executors of both drivers are outside the contract (#923):
    INV-PLUG-006 stays exactly true — plugins, no grants, no callback, and
    now no broker matchers and no broker env either."""
    from plugin_registry import reload_snapshot
    from plugin_fixtures import entry, mk_artifact, mk_registry
    from test_agent_plugin_binding import _exec_defn
    store = tmp_path / "store"
    e = entry("execplug", ["executor:probe-exec"])
    mk_artifact(store, "execplug", e["artifact_id"], mcp_servers={"execplug": {}})
    reload_snapshot(registry_path=mk_registry(tmp_path, [e]), store_root=store)
    monkeypatch.setattr(tools_mod, "_mcp_registry", None, raising=False)
    opts = tools_mod._build_executor_options(_exec_defn(), executor_type="probe-exec")
    assert len(opts.plugins) == 1
    assert _broker_hooks(opts) == {}
    assert not any(k.startswith("CASA_BROKER_") for k in (opts.env or {}))


def test_resident_and_specialist_builders_carry_one_of_each(tmp_path, monkeypatch):
    from plugin_registry import reload_snapshot
    from plugin_fixtures import entry, mk_artifact, mk_registry
    from test_agent_plugin_binding import _make_agent, _spec_cfg
    store = tmp_path / "store"
    e = entry("probe", ["resident:assistant", "specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"], mcp_servers={"api": {}},
                extra_manifest={"casa": {"protectedTools": ["danger"]}})
    reload_snapshot(registry_path=mk_registry(tmp_path, [e]), store_root=store)
    monkeypatch.setattr(tools_mod, "_mcp_registry", None, raising=False)
    monkeypatch.setattr(tools_mod, "_agent_registry", None, raising=False)

    async def run():
        return await _make_agent(tmp_path)._build_options(
            channel="telegram", channel_key="k", is_fresh=True, resume_sid=None, user_text="hi")

    for opts in (asyncio.run(run()), tools_mod._build_specialist_options(_spec_cfg("finance"))):
        hooks = _broker_hooks(opts)
        assert {k: len(v) for k, v in hooks.items()} == {
            "PreToolUse": 1, "PostToolUse": 1, "PostToolUseFailure": 1}
        admission = hooks["PreToolUse"][0]
        assert opts.env["CASA_BROKER_CLIENT"] == admission._casa_result_broker_client
        # the protected plugin's authz decision rides on the composite, and
        # the standalone authz matcher is gone
        assert getattr(admission, "_casa_authz_role", None) is not None
        standalone = [h for m in opts.hooks["PreToolUse"] for h in m.hooks
                      if getattr(h, "_casa_authz_role", None) is not None
                      and getattr(h, "_casa_result_broker", None) is None]
        assert standalone == []


def test_plugin_free_session_carries_no_broker(tmp_path, monkeypatch):
    from plugin_registry import reload_snapshot
    from plugin_fixtures import mk_registry
    from test_agent_plugin_binding import _spec_cfg
    reload_snapshot(registry_path=mk_registry(tmp_path, []), store_root=tmp_path / "store")
    monkeypatch.setattr(tools_mod, "_mcp_registry", None, raising=False)
    monkeypatch.setattr(tools_mod, "_agent_registry", None, raising=False)
    opts = tools_mod._build_specialist_options(_spec_cfg("finance"))
    assert opts.plugins == [] and _broker_hooks(opts) == {}
    assert not any(k.startswith("CASA_BROKER_") for k in (opts.env or {}))


# ---------------------------------------------------------------------------
# the plugin lifecycle — the REAL update / remove / unassign paths
# ---------------------------------------------------------------------------

def _seed(artifact, role):
    _open(rb.STORE, client=f"c-{artifact[:4]}-{role}", call="call",
          identity=_identity(artifact_id=artifact, enforcement_role=role), artifact=artifact)
    ref, err = rb.STORE.deposit(client_id=f"c-{artifact[:4]}-{role}", slot="link", value=SENTINEL)
    assert err is None
    rb.STORE.close_call(f"c-{artifact[:4]}-{role}", "call")
    return ref


@pytest.fixture
def clean_store(monkeypatch):
    fresh = rb.ReferenceStore()
    monkeypatch.setattr(rb, "STORE", fresh)
    return fresh


@pytest.mark.asyncio
async def test_plugin_update_purges_the_old_artifact_references(monkeypatch, tmp_path, clean_store):
    from test_plugin_tools import _State, _entry, _wire, _pr
    st = _State()
    st.raw["plugins"].append(_entry())
    tools_mod2 = _wire(monkeypatch, tmp_path, st, publish=_pr(version="2.0.0"))
    old_id = st.raw["plugins"][0]["artifact_id"]
    _seed(old_id, "assistant")
    _seed(OTHER_ARTIFACT, "assistant")
    assert clean_store.reference_count() == 2
    r = await tools_mod2.plugin_update.handler({"name": "probe", "new_ref": "v2"})
    assert r.get("is_error") is not True
    assert clean_store.reference_count() == 1
    assert clean_store.purge_artifact(old_id) == 0


@pytest.mark.asyncio
async def test_plugin_remove_purges_artifact_and_role_references(monkeypatch, tmp_path, clean_store):
    from test_plugin_tools import _State, _entry, _wire, _pr
    st = _State()
    st.raw["plugins"].append(_entry())
    tools_mod2 = _wire(monkeypatch, tmp_path, st, publish=_pr())
    old_id = st.raw["plugins"][0]["artifact_id"]
    _seed(old_id, "assistant")
    _seed(OTHER_ARTIFACT, "assistant")        # same role, other artifact
    _seed(OTHER_ARTIFACT, "finance")          # neither
    assert clean_store.reference_count() == 3
    r = await tools_mod2.plugin_remove.handler({"name": "probe"})
    assert r.get("is_error") is not True
    assert clean_store.reference_count() == 1


@pytest.mark.asyncio
async def test_plugin_unassign_purges_the_role_references(monkeypatch, tmp_path, clean_store):
    from test_plugin_tools import _State, _entry, _wire, _pr
    st = _State()
    st.raw["plugins"].append(_entry())
    tools_mod2 = _wire(monkeypatch, tmp_path, st, publish=_pr())
    _seed(ARTIFACT, "assistant")
    _seed(ARTIFACT, "finance")
    assert clean_store.reference_count() == 2
    r = await tools_mod2.plugin_unassign.handler({"name": "probe", "target": "resident:assistant"})
    assert r.get("is_error") is not True
    assert clean_store.reference_count() == 1
