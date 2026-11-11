"""A task's delivery is decided once, at its birth, and every call of the task
carries it as it was decided — ``None`` included — never recomputed.

``delivery_for``: the resident's own work goes to its chat; an agent with its
own chat goes there only when that chat is open at the task's birth; anything
else (an agent behind the resident, an own chat not open) posts nothing.
``GrantIdentity.delivery``: a task's call copies its birth-time delivery (the
engagement record's, or the origin's reserved key); any other turn's is its
own chat."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import agent as agent_mod
import own_voice
import provenance
import task_model
import tools as tools_mod
from authz_grants import resolve_grant_identity
from channels.tg_address import TgAddress
from task_model import Requester
from two_endpoints import CASA_BOT, FINANCE_BOT, OPERATOR, FakeEndpoint, build_two_endpoints


@pytest.fixture
def world(monkeypatch):
    eps = build_two_endpoints()
    monkeypatch.setattr(tools_mod, "_channel_manager", eps.manager, raising=False)
    states = {"finance": "open"}

    def state(role):
        return states.get(role, "not_declared")

    def endpoint(role):
        return eps.finance if role == "finance" else None
    monkeypatch.setattr(own_voice, "own_chat_state", state)
    monkeypatch.setattr(own_voice, "endpoint_for", endpoint)
    return SimpleNamespace(eps=eps, states=states)


# --- delivery_for ----------------------------------------------------------------------------

@pytest.mark.parametrize("route", [{"_tg_bot": CASA_BOT}, {"_tg_bot": "casa"}, {},
                                   {"_tg_bot": FINANCE_BOT}])
def test_the_residents_own_work_is_delivered_in_its_chat(world, route):
    assert task_model.delivery_for("assistant", route) == TgAddress(CASA_BOT, OPERATOR)


def test_an_agent_with_an_open_own_chat_is_delivered_there(world):
    assert task_model.delivery_for("finance", {"_tg_bot": CASA_BOT}) == TgAddress(FINANCE_BOT, OPERATOR)


@pytest.mark.parametrize("state", ["no_bot", "not_open", "dormant"])
def test_an_own_chat_that_is_not_open_at_birth_delivers_nothing(world, state):
    world.states["finance"] = state
    assert task_model.delivery_for("finance", {"_tg_bot": CASA_BOT}) is None


def test_an_agent_behind_the_resident_delivers_nothing(world):
    assert task_model.delivery_for("browser", {"_tg_bot": CASA_BOT}) is None


def test_an_open_chat_without_a_bound_endpoint_delivers_nothing(world, monkeypatch):
    monkeypatch.setattr(own_voice, "endpoint_for", lambda role: None)
    assert task_model.delivery_for("finance", {"_tg_bot": CASA_BOT}) is None


def test_without_telegram_nothing_is_delivered(monkeypatch):
    monkeypatch.setattr(tools_mod, "_channel_manager", None, raising=False)
    assert task_model.delivery_for("assistant", {"_tg_bot": CASA_BOT}) is None


def test_the_resident_born_before_its_bot_has_an_id_delivers_nothing(monkeypatch):
    eps = build_two_endpoints(casa=FakeEndpoint("assistant", bot_id=None, is_casa=True))
    monkeypatch.setattr(tools_mod, "_channel_manager", eps.manager, raising=False)
    assert task_model.delivery_for("assistant", {"_tg_bot": "casa"}) is None


# --- the requester ---------------------------------------------------------------------------

def test_a_requester_round_trips_and_a_malformed_one_is_refused():
    r = Requester("assistant", {"_tg_bot": CASA_BOT})
    assert Requester.from_json(r.to_json()) == r
    assert Requester.from_json({"role": "finance", "route": {"_tg_bot": "casa"}}).route == {"_tg_bot": "casa"}
    for bad in ({"role": "", "route": {}}, {"role": "a"}, {"role": "a", "route": {"_tg_bot": 0}},
                {"role": "a", "route": {"chat_id": 1}}, ["a", {}], {"role": "a", "route": {}, "x": 1}):
        with pytest.raises(ValueError):
            Requester.from_json(bad)


# --- the reserved key ------------------------------------------------------------------------

def test_the_task_delivery_is_reserved_and_carried_as_it_is():
    assert provenance.TASK_DELIVERY_KEY in provenance.RESERVED_CONTEXT_KEYS
    assert provenance.sanitize_external_context({"_task_delivery": [1, 2, None], "a": 1}) == {"a": 1}
    stamped = provenance.task_delivery_marker(TgAddress(FINANCE_BOT, OPERATOR))
    assert stamped == {"_task_delivery": [FINANCE_BOT, OPERATOR, None]}
    assert provenance.carry_task_delivery({**stamped, "x": 1}) == stamped
    assert provenance.carry_task_delivery(provenance.task_delivery_marker(None)) == {"_task_delivery": None}
    assert provenance.carry_task_delivery({"x": 1}) == {}
    assert provenance.task_delivery(stamped) == (True, TgAddress(FINANCE_BOT, OPERATOR))
    assert provenance.task_delivery({"_task_delivery": None}) == (True, None)
    assert provenance.task_delivery({"_task_delivery": "garbage"}) == (True, None)
    assert provenance.task_delivery({}) == (False, None)


# --- GrantIdentity.delivery --------------------------------------------------------------------

DM = {"role": "assistant", "channel": "telegram", "chat_id": str(OPERATOR), "user_id": OPERATOR,
      "cid": "c", "message_type": "channel_in", "source": "telegram",
      "execution_role": "assistant", "_tg_bot": CASA_BOT}
DELEGATED = {**DM, "execution_role": "finance", "delegation_depth": 1}


def _identity(origin, role, engagement=None):
    o = agent_mod.origin_var.set(origin)
    e = tools_mod.engagement_var.set(engagement)
    try:
        return resolve_grant_identity(role)
    finally:
        agent_mod.origin_var.reset(o)
        tools_mod.engagement_var.reset(e)


def test_a_turn_that_is_not_a_task_delivers_in_its_own_chat(world):
    identity, why = _identity(DM, "assistant")
    assert why is None and identity.delivery == TgAddress(CASA_BOT, OPERATOR)


def test_a_task_born_without_a_delivery_stays_without_one_even_once_the_chat_opens(world, monkeypatch):
    """Born while Alex's chat was not open: the delivery is None. The chat opens
    mid-run; the call still carries None — nothing recomputes it."""
    world.states["finance"] = "not_open"
    birth = task_model.delivery_for("finance", {"_tg_bot": CASA_BOT})
    assert birth is None
    origin = {**DELEGATED, **provenance.task_delivery_marker(birth)}
    world.states["finance"] = "open"

    def never(*a, **k):
        raise AssertionError("a task's delivery is never recomputed")
    monkeypatch.setattr(own_voice, "own_chat_state", never)
    monkeypatch.setattr(own_voice, "endpoint_for", never)
    monkeypatch.setattr(task_model, "delivery_for", never)
    identity, why = _identity(origin, "finance")
    assert why is None and identity.delivery is None
    # the identity itself is unchanged by the delivery (advisory, like the target role)
    plain, _ = _identity({**DELEGATED, **provenance.task_delivery_marker(TgAddress(FINANCE_BOT, OPERATOR))},
                         "finance")
    assert identity == plain and plain.delivery == TgAddress(FINANCE_BOT, OPERATOR)


def test_a_task_born_with_a_delivery_carries_it(world):
    birth = task_model.delivery_for("finance", {"_tg_bot": CASA_BOT})
    identity, why = _identity({**DELEGATED, **provenance.task_delivery_marker(birth)}, "finance")
    assert why is None and identity.delivery == TgAddress(FINANCE_BOT, OPERATOR)
    assert identity.bot_id == CASA_BOT                  # the turn's own route is not the delivery


@pytest.mark.parametrize("delivery", [None, TgAddress(FINANCE_BOT, OPERATOR)])
def test_a_jobs_call_copies_the_jobs_delivery(world, delivery):
    rec = SimpleNamespace(id="eng-1", kind="specialist", status="active", role_or_type="finance",
                          origin={"user_id": OPERATOR, "chat_id": OPERATOR, "_tg_bot": CASA_BOT},
                          delivery=delivery)
    identity, why = _identity({**DELEGATED}, "finance", engagement=rec)
    assert why is None and identity.engagement_id == "eng-1"
    assert identity.delivery == delivery


# --- a task's read check, consulted at each Read ------------------------------------------------

async def test_a_tasks_read_check_is_consulted_live_after_the_prefixes_deny(tmp_path):
    from hooks import make_path_scope_hook_v2
    import agent_inbox
    granted: set = set()
    hook = make_path_scope_hook_v2(readable=[str(tmp_path / "ready")],
                                   extra_read_check=lambda p: p in granted)
    target = tmp_path / "inbox" / "a.pdf"

    async def read(path):
        out = await hook({"tool_name": "Read", "tool_input": {"file_path": str(path)}}, None, {})
        return out == {}
    assert await read(tmp_path / "ready" / "x.txt")           # the static prefixes still allow
    assert not await read(target)
    granted.add(target)                                       # granted after the hook was built
    assert await read(target)
    assert not await read(tmp_path / "inbox" / "b.pdf")
    # without a check, today's hook
    plain = make_path_scope_hook_v2(readable=[str(tmp_path / "ready")])
    out = await plain({"tool_name": "Read", "tool_input": {"file_path": str(target)}}, None, {})
    assert out != {}
    # the builder's grants: a check bound to the task only when a task is named
    cfg = SimpleNamespace(role="nobody", kind="specialist")
    assert agent_inbox.delegated_build_grants(cfg, engagement=True) == agent_inbox.Grants((), (), None)
    bound = agent_inbox.delegated_build_grants(cfg, engagement=True, task_id="t-1")
    assert (bound.prefixes, bound.tools) == ((), ()) and bound.read_check(target) is False
