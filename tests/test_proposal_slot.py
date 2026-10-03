"""S5 §2–§3: the ``operator_proposal`` delivered slot — a JSON-encoded proposal
object judged at deposit by one predicate (text, buttons, the stored calls
resolved against the depositing call's own plugin and server, the argument
grammar, the one-page rule), posted labelled with a keyboard under the
broker's ``proposal`` namespace, registered BEFORE the post and unregistered
when the post is not proven, bounded in number and superseded by revision
(INV-PROP-003).

Driven like the S3 delivery suite: the real hooks and store, a recorder
channel where the hook looks for it, a fresh broker per test.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import result_broker as rb
import stored_calls as sc
import tools as tools_mod
import verdict_broker as vb
from authz_grants import GrantIdentity
from channels import DeliveryOutcome
from plugin_grants import PluginContract, ResultContractMap, ToolContract

ARTIFACT = "6" * 64
SEG = "probe"
SRV = "api"
OFFER = f"mcp__plugin_{SEG}_{SRV}__offer"          # capability, delivers operator_proposal
APPLY = f"mcp__plugin_{SEG}_{SRV}__apply"          # safe, no consumes — the ordinary stored call
MORE = f"mcp__plugin_{SEG}_{SRV}__more"            # the More exception
GUARDED = f"mcp__plugin_{SEG}_{SRV}__guarded"      # safe but PROTECTED
SLOT = "proposal"
LABEL = "📊 Finance"


def _identity(**over) -> GrantIdentity:
    base = dict(operator_id=42, chat_id=42, enforcement_role="finance",
                artifact_id=ARTIFACT, engagement_id="", delegation_id="d-1")
    base.update(over)
    return GrantIdentity(**base)


def _tool(name, kind="safe", provides=(), delivers=None, consumes=None):
    return ToolContract(ARTIFACT, SEG, kind, tuple(provides), dict(consumes or {}),
                        delivers=dict(delivers or {}), servers=(SRV,), wire_name=name,
                        transport="stdio")


def _map() -> ResultContractMap:
    tools = {
        OFFER: _tool("offer", "capability", (SLOT,), {SLOT: "operator_proposal"}),
        APPLY: _tool("apply"),
        MORE: _tool("more", "capability", (SLOT,), {SLOT: "operator_proposal"}),
        GUARDED: _tool("guarded"),
    }
    return ResultContractMap(tools=tools,
                             plugins={SEG: PluginContract(ARTIFACT, True, frozenset(), name="probe")})


PROTECTED = {GUARDED: object()}


def _proposal(**over):
    base = {"text": "Pair invoice 17 with the Adobe payment?",
            "buttons": [{"label": "Yes", "call": {"tool": "apply", "arguments": {"choice": "yes", "match_id": 17}}},
                        {"label": "No", "call": {"tool": "apply", "arguments": {"choice": "no", "match_id": 17}}}],
            "revision": "r-9"}
    base.update(over)
    return base


class _Recorder:
    def __init__(self, outcome=DeliveryOutcome.DELIVERED, message_id=501):
        self.outcome, self.message_id = outcome, message_id
        self.proposals: list[tuple] = []
        self.other: list[tuple] = []
        self.chat_id = "42"

    async def deliver_operator_proposal(self, chat_id, text, labels, rid, *, post=None):
        self.proposals.append((chat_id, text, list(labels), rid, post))
        return self.message_id if self.outcome is DeliveryOutcome.DELIVERED else None

    async def deliver_operator_message(self, *a, **kw):
        self.other.append(("message", a)); return DeliveryOutcome.DELIVERED

    async def send_response(self, message, context):
        self.other.append(("send_response", message)); return DeliveryOutcome.DELIVERED


class _Manager:
    def __init__(self, ch): self._ch = ch
    def get(self, name): return self._ch if name == "telegram" else None


@pytest.fixture
def env(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(tools_mod, "_channel_manager", _Manager(rec), raising=False)
    monkeypatch.setattr(tools_mod, "_agent_role_map", {
        "finance": SimpleNamespace(character=SimpleNamespace(name="Finance"))})
    monkeypatch.setattr(vb, "BROKER", vb.VerdictBroker())
    monkeypatch.setattr(rb, "POSTS", rb.PostLedger())
    store = rb.ReferenceStore(now=lambda: 1000.0)
    return SimpleNamespace(rec=rec, store=store, broker=vb.BROKER)


def _open(store, *, call="call-1", identity=None, tool=OFFER):
    store.open_call(client_id="c1", artifact_id=ARTIFACT, tool_name=tool,
                    tool_use_id=call, identity=identity or _identity(),
                    provides=(SLOT,), delivers={SLOT: "operator_proposal"},
                    contract_map=_map(), protected=PROTECTED, entry=_map().tools[tool])


def _post(response, tool=OFFER):
    return {"hook_event_name": "PostToolUse", "tool_name": tool, "tool_input": {},
            "tool_response": response}


# --- the deposit --------------------------------------------------------------------

@pytest.mark.parametrize("value, reason", [
    ("not json", "bad_proposal"),
    (json.dumps([1]), "bad_proposal"),
    (json.dumps(_proposal(text="")), "bad_proposal"),
    (json.dumps(_proposal(text="x" * 4001)), "bad_proposal"),
    (json.dumps(_proposal(buttons=[])), "bad_proposal"),
    (json.dumps(_proposal(buttons=[{"label": "L", "call": {"tool": "apply", "arguments": {}}}] * 7)), "bad_proposal"),
    (json.dumps(_proposal(buttons=[{"label": "", "call": {"tool": "apply", "arguments": {}}}])), "bad_proposal"),
    (json.dumps(_proposal(buttons=[{"label": "x" * 33, "call": {"tool": "apply", "arguments": {}}}])), "bad_proposal"),
    (json.dumps(_proposal(buttons=[{"label": "L", "call": {"tool": "mcp__plugin_other_x__y", "arguments": {}}}])), "bad_proposal"),
    (json.dumps(_proposal(buttons=[{"label": "L", "call": {"tool": "nope", "arguments": {}}}])), "bad_proposal"),
    (json.dumps(_proposal(buttons=[{"label": "L", "call": {"tool": "guarded", "arguments": {}}}])), "bad_proposal"),
    (json.dumps(_proposal(buttons=[{"label": "L", "call": {"tool": "apply", "arguments": {"n": 1.5}}}])), "bad_proposal"),
    (json.dumps(_proposal(buttons=[{"label": "L", "call": {"tool": "apply", "arguments": {"s": "a<b"}}}])), "bad_proposal"),
    (json.dumps(_proposal(revision="r" * 65)), "bad_proposal"),
    (json.dumps(_proposal(text="😀" * 2100)), "bad_proposal"),      # one page in UTF-16 units
], ids=["not-json", "not-object", "empty-text", "long-text", "no-buttons", "seven-buttons",
        "empty-label", "long-label", "qualified-tool", "undeclared-tool", "protected-tool",
        "float-argument", "angle-bracket", "long-revision", "two-pages"])
def test_the_deposit_refuses_a_bad_proposal(env, value, reason):
    _open(env.store)
    assert env.store.deposit(client_id="c1", slot=SLOT, value=value) == (None, reason)
    assert env.store._refs == {}


def test_the_deposit_keeps_the_parsed_proposal_with_its_resolved_calls(env):
    _open(env.store)
    ref, err = env.store.deposit(client_id="c1", slot=SLOT, value=json.dumps(_proposal()))
    assert err is None and rb.is_reference(ref)
    r = env.store._refs[ref]
    kept = r.proposal
    assert kept["text"].startswith("Pair invoice") and kept["revision"] == "r-9"
    assert [b["label"] for b in kept["buttons"]] == ["Yes", "No"]
    call = kept["buttons"][0]["call"]
    assert (call["server"], call["wire_name"], call["runtime_name"], call["proposal"]) == (SRV, "apply", APPLY, False)
    assert call["canonical"] == sc.canonical_json({"choice": "yes", "match_id": 17})
    assert call["arguments"] == {"choice": "yes", "match_id": 17}


def test_the_more_exception_is_a_valid_button_target(env):
    _open(env.store)
    value = json.dumps(_proposal(buttons=[{"label": "More", "call": {"tool": "more", "arguments": {"page": 2}}}]))
    ref, err = env.store.deposit(client_id="c1", slot=SLOT, value=value)
    assert err is None and env.store._refs[ref].proposal["buttons"][0]["call"]["proposal"] is True


def test_the_kind_is_declared_to_the_manifest_and_the_broker():
    import plugin_store
    assert "operator_proposal" in plugin_store._RC_DELIVERS_KINDS
    assert "proposal" in vb._VALID_NAMESPACES
    assert rb.OPERATOR_PROPOSAL == "operator_proposal"


# --- the post ------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_proven_proposal_registers_before_posting_posts_once_and_returns_the_receipt(env):
    hook = rb.make_result_hook(_map(), client_id="c1", store=env.store)
    _open(env.store, identity=_identity(chat_id=4242, operator_id=4242))
    ref, _ = env.store.deposit(client_id="c1", slot=SLOT, value=json.dumps(_proposal()))
    out = await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    (chat_id, text, labels, rid, post), = env.rec.proposals
    assert chat_id == 4242 and text == LABEL + "\nPair invoice 17 with the Adobe payment?"
    assert labels == ["Yes", "No"] and len(rid) == 32
    assert post.kind == "operator_proposal" and post.role == "finance" and post.owner == "d-1"
    meta = env.broker.get_meta(namespace="proposal", scope="proposal:4242", request_id=rid)
    assert meta["chat_id"] == 4242 and meta["operator_id"] == 4242 and meta["role"] == "finance"
    assert meta["artifact_id"] == ARTIFACT and meta["plugin_seg"] == SEG and meta["revision"] == "r-9"
    assert meta["message_id"] == 501 and meta["label"] == LABEL and meta["owner"] == "d-1"
    assert [c["runtime_name"] for c in meta["calls"]] == [APPLY, APPLY]
    assert meta["deadline"] > asyncio.get_running_loop().time() + rb.PROPOSAL_TTL_S - 5
    receipt = json.loads(out["hookSpecificOutput"]["updatedToolOutput"])
    assert receipt == {SLOT: ref, "casa_delivery": {"slot": SLOT, "status": "delivered",
                                                   "to": "operator_chat", "proposal_id": rid,
                                                   "buttons": 2}}
    assert rb.echo_lines(rb.POSTS.drain("d-1")) == [LABEL + " posted a proposal to your chat (2 buttons)."]
    assert rb.PROPOSAL_TTL_S == 3600 and rb.PROPOSAL_MAX_LIVE == 32


@pytest.mark.asyncio
async def test_a_post_that_is_not_proven_unregisters_and_withholds(env):
    env.rec.outcome = DeliveryOutcome.NOT_DELIVERED
    hook = rb.make_result_hook(_map(), client_id="c1", store=env.store)
    _open(env.store)
    ref, _ = env.store.deposit(client_id="c1", slot=SLOT, value=json.dumps(_proposal()))
    out = await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    parsed = json.loads(out["hookSpecificOutput"]["updatedToolOutput"])
    assert parsed["casa_result_withheld"] is True and "proposal" in parsed["reason"]
    assert env.broker.pending(namespace="proposal", scope="proposal:42") == []
    assert ref not in env.store._refs and rb.POSTS.drain("d-1") == []


@pytest.mark.asyncio
async def test_the_thirty_third_live_proposal_in_a_chat_is_withheld(env):
    hook = rb.make_result_hook(_map(), client_id="c1", store=env.store)
    for i in range(rb.PROPOSAL_MAX_LIVE):
        _open(env.store, call=f"call-{i}")
        ref, _ = env.store.deposit(client_id="c1", slot=SLOT, value=json.dumps(_proposal(revision=f"r{i}")))
        await hook(_post(json.dumps({SLOT: ref})), f"call-{i}", {})
    assert len(env.rec.proposals) == rb.PROPOSAL_MAX_LIVE
    _open(env.store, call="call-x")
    ref, _ = env.store.deposit(client_id="c1", slot=SLOT, value=json.dumps(_proposal(revision="rx")))
    out = await hook(_post(json.dumps({SLOT: ref})), "call-x", {})
    assert json.loads(out["hookSpecificOutput"]["updatedToolOutput"])["casa_result_withheld"] is True
    assert len(env.rec.proposals) == rb.PROPOSAL_MAX_LIVE
    assert len(env.broker.pending(namespace="proposal", scope="proposal:42")) == rb.PROPOSAL_MAX_LIVE


@pytest.mark.asyncio
async def test_a_same_revision_proposal_supersedes_only_its_own_predecessor(env):
    hook = rb.make_result_hook(_map(), client_id="c1", store=env.store)
    rids = []
    for i, rev in enumerate(["r1", "r2", "", "r1"]):
        _open(env.store, call=f"call-{i}")
        ref, _ = env.store.deposit(client_id="c1", slot=SLOT, value=json.dumps(_proposal(revision=rev)))
        await hook(_post(json.dumps({SLOT: ref})), f"call-{i}", {})
        rids.append(env.rec.proposals[-1][3])
    live = env.broker.pending(namespace="proposal", scope="proposal:42")
    assert rids[0] not in live                      # superseded by the fourth (same r1)
    assert rids[1] in live and rids[2] in live and rids[3] in live
    # another role's or plugin's same revision is never superseded
    _open(env.store, call="call-z", identity=_identity(enforcement_role="hr"))
    ref, _ = env.store.deposit(client_id="c1", slot=SLOT, value=json.dumps(_proposal(revision="r2")))
    await hook(_post(json.dumps({SLOT: ref})), "call-z", {})
    assert rids[1] in env.broker.pending(namespace="proposal", scope="proposal:42")
