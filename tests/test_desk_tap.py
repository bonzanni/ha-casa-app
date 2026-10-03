"""S5 §4.3/§5.2/§7/§10: the tap's desk use — the re-checks under the desk
lock on ONE captured build input, the permit after the lock, the pinned
run created directly (never the bounded wrapper), the receipt authoritative
over an aborted turn, the refusal notices, the tell line, the ``More``
cases, the exchange ``[tapped: <label>]`` and the body-free echo; a faulted
desk refusing every later use (INV-PROP-001, INV-PROP-002).

The runner is faked (the pin itself is ``test_pinned_run.py``'s): it records
what it was given — the captured resolution, the ``PinnedRun`` owner on its
ContextVar, the origin — and resolves the owner's watch as the real hooks
would.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

import agent as agent_mod
import pinned_run as pr
import plugin_erasure
import result_broker as rb
import specialist_desk as sd
import tools as tools_mod
from channels import DeliveryOutcome
from output_boundary import Admitted
from plugin_grants import PluginContract, ProfiledPlugin, ProfilePlan, ResultContractMap, ToolContract

OPERATOR = 42
LABEL = "📊 Finance"
ART = "7" * 64
RID = "f" * 32
SEG = "probe"
APPLY = "mcp__plugin_probe_probe__apply"
CANON = '{"choice":"yes"}'


# --- fakes ------------------------------------------------------------------------

class _Permit:
    def __init__(self, desk_holder):
        self.released = False
        self.released_under_lock = None
        self._desk = desk_holder

    def release(self):
        if not self.released:
            self.released = True
            self.released_under_lock = self._desk[0].lock.locked() if self._desk else None


class _Limiter:
    def __init__(self, refuse=False):
        self.refuse = refuse
        self.permits = []
        self.scopes = []
        self.desk_holder = []

    def try_acquire(self, scope):
        self.scopes.append(scope)
        if self.refuse:
            return None
        p = _Permit(self.desk_holder)
        self.permits.append(p)
        return p


class _Channel:
    def __init__(self, fail_send=False):
        self.replies, self.notices, self.released, self.marks = [], [], [], []
        self.fail_send = fail_send

    async def send_response(self, message, context):
        assert isinstance(message, Admitted)
        self.replies.append((message, context))
        if self.fail_send:
            raise RuntimeError("send failed")
        return DeliveryOutcome.DELIVERED

    async def deliver_desk_notice(self, chat_id, text):
        self.notices.append((chat_id, text))
        return True

    async def mark_proposal(self, meta, line):
        self.marks.append(line)

    def _release_typing(self, context, chat_id):
        self.released.append((chat_id, context.get("cid")))


class _Fence:
    def __init__(self):
        self.names = set()

    def fenced(self, name):
        return name in self.names


def _tool(name, kind="safe", *, provides=(), consumes=None, delivers=None, transport="stdio"):
    return ToolContract(ART, SEG, kind, tuple(provides), dict(consumes or {}),
                        delivers=dict(delivers or {}), servers=("probe",), wire_name=name,
                        transport=transport)


def _map(tools, *, setup=()):
    return ResultContractMap(
        tools={f"mcp__plugin_probe_probe__{t.wire_name}": t for t in tools},
        plugins={SEG: PluginContract(ART, True, frozenset(setup), name="probe")})


def _build(*, tools=None, artifact=ART, plan=None, protected=None, plugins=None):
    rp = SimpleNamespace(name="probe", manifest_name="", artifact_id=artifact, path="/p/probe",
                         version="1", manifest={})
    resolution = SimpleNamespace(plugins=[rp] if plugins is None else list(plugins))
    return pr.BuildInput(
        cfg=tools_mod._agent_role_map["finance"], resolution=resolution, withheld=(),
        protected=dict(protected or {}),
        contract_map=_map([_tool("apply"), _tool("more", "capability", provides=("proposal",),
                                                   delivers={"proposal": "operator_proposal"})]
                          if tools is None else tools),
        plan=plan or ProfilePlan(loaded=("probe",)), target="specialist:finance")


def _meta(**over):
    base = {"deadline": 10_000.0, "chat_id": OPERATOR, "operator_id": OPERATOR, "role": "finance",
            "artifact_id": ART, "plugin_seg": SEG, "label": LABEL,
            "text": f"{LABEL}\nPair 17?", "options": ["Yes", "More"], "revision": "",
            "message_id": 501, "owner": "d-1", "_scope": f"proposal:{OPERATOR}",
            "calls": [{"server": "probe", "wire_name": "apply", "runtime_name": APPLY,
                       "proposal": False, "arguments": {"choice": "yes"}, "canonical": CANON},
                      {"server": "probe", "wire_name": "more",
                       "runtime_name": "mcp__plugin_probe_probe__more",
                       "proposal": True, "arguments": {"page": 2}, "canonical": '{"page":2}'}]}
    base.update(over)
    return base


@pytest.fixture
def env(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(sd, "DESKS", sd.DeskRegistry(now=lambda: clock["t"]))
    monkeypatch.setattr(sd, "DESK_ECHO", rb.PostLedger(max_events=64))
    monkeypatch.setattr(rb, "POSTS", rb.PostLedger())
    monkeypatch.setattr(rb, "POST_MAP", rb.PostMap())
    finance = SimpleNamespace(role="finance", kind="specialist", delegates=(),
                              character=SimpleNamespace(name="Finance"))
    assistant = SimpleNamespace(role="assistant", kind="resident",
                                delegates=(SimpleNamespace(agent="finance"),),
                                character=SimpleNamespace(name="Ellen"))
    monkeypatch.setattr(tools_mod, "_agent_role_map", {"assistant": assistant, "finance": finance})
    limiter = _Limiter()
    monkeypatch.setattr(tools_mod, "_specialist_limiter", limiter)
    fence = _Fence()
    monkeypatch.setattr(plugin_erasure, "FENCE", fence)
    calls, captures = [], []
    env = SimpleNamespace(clock=clock, limiter=limiter, calls=calls, captures=captures,
                          fence=fence, assistant=assistant, terminated=[], confirm=True)
    env.build = _build()

    def capture(cfg):
        captures.append(cfg)
        return env.build
    monkeypatch.setattr(tools_mod, "_capture_build_input", capture, raising=False)

    async def runner(cfg, task_text, context_text, resolution=None, output_format=None,
                     tool_counts=None):
        call = SimpleNamespace(cfg=cfg, task=task_text, context=context_text, resolution=resolution,
                               owner=tools_mod._pinned_run.get(),
                               origin=dict(agent_mod.origin_var.get(None) or {}),
                               turn_id=tools_mod._delegation_quota_key.get(),
                               lock_held=sd.DESKS.get(OPERATOR, "finance").lock.locked())
        calls.append(call)
        return await env.respond(call)

    async def _default(call):
        call.owner.resolve(pr.Capture("receipt", "applied match 17"))
        return tools_mod.DelegatedOutput(text="model prose that must not leak")
    env.respond = _default
    monkeypatch.setattr(tools_mod, "_run_delegated_agent", runner)

    async def bounded(*a, **k):
        raise AssertionError("the bounded wrapper is never on the pinned path")
    monkeypatch.setattr(tools_mod, "_run_delegated_agent_bounded", bounded)

    async def terminate(self, task=None):
        env.terminated.append(self)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=1)
        self.sealed = True                                   # as the real one does
        return env.confirm
    monkeypatch.setattr(pr.PinnedRun, "terminate", terminate)
    env.refreshes = []

    async def refresh():
        env.refreshes.append(True)
    monkeypatch.setattr(tools_mod, "_refresh_plugin_health_live", refresh, raising=False)
    env.channel = _Channel()
    env.desk = sd.DESKS.get_or_create(OPERATOR, "finance")
    limiter.desk_holder.append(env.desk)
    return env


async def _tap(env, idx=0, meta=None, reservation=None, **over):
    m = _meta(**(meta or {}))
    if "deadline" not in (meta or {}):
        m["deadline"] = asyncio.get_running_loop().time() + 3600
    kw = dict(channel=env.channel, resident_role="assistant", chat_id=OPERATOR, user_id=OPERATOR,
              cid="cid-1", desk_role="finance", meta=m, idx=idx, request_id=RID,
              reservation=reservation if reservation is not None else env.desk.reserve())
    kw.update(over)
    await sd.handle_tap(**kw)


def _echo():
    return sd.drain_echo_lines(OPERATOR)


# --- the happy path: one pinned run, the receipt, the exchange, the echo -----------

async def test_a_tap_runs_one_pinned_turn_on_the_captured_build_input_and_posts_the_receipt(env):
    await _tap(env)
    (call,) = env.calls
    assert env.captures == [tools_mod._agent_role_map["finance"]]        # captured ONCE
    assert call.cfg is tools_mod._agent_role_map["finance"]
    assert call.resolution is env.build.resolution                        # passed UNCHANGED
    assert call.owner is not None and call.owner.build_input is env.build
    assert (call.owner.runtime_name, call.owner.canonical, call.owner.label) == (APPLY, CANON, "Yes")
    assert call.lock_held is True
    # the prompt (§5.2.3) and the desk block as context
    assert call.task.startswith('[casa stored call] The operator tapped "Yes" on your proposal.')
    assert f"`{APPLY}`" in call.task and CANON in call.task and "Then stop." in call.task
    assert call.context.startswith(sd.turn_frame("Ellen"))
    # the origin: a desk turn's plus the reserved stored_call marker keyed on the run id
    assert call.origin["desk"] == {"role": "finance", "chat_id": OPERATOR}
    assert call.origin["execution_role"] == "finance" and call.origin["_operator_turn"] is True
    sc = call.origin["stored_call"]
    assert sc == {"run_id": call.turn_id, "runtime_name": APPLY, "canonical": CANON, "label": "Yes"}
    assert call.owner.run_id == call.turn_id
    assert "stored_call" in call.origin["turn_scope"].markers      # reserved: a chat message cannot carry it
    # the receipt, labelled, admitted, filed as a receipt post
    (message, context), = env.channel.replies
    assert str(message) == f"{LABEL}\napplied match 17"
    post = context["_post"]
    assert (post.role, post.operator_id, post.kind, post.owner) == ("finance", OPERATOR, "receipt", call.turn_id)
    assert env.channel.notices == [] and env.channel.marks == []
    assert [(e.who, e.text) for e in env.desk.log] == [("operator", "[tapped: Yes]"),
                                                       ("specialist", "applied match 17")]
    assert env.desk.last_used == 1000.0 and env.desk.waiting == 0
    assert _echo() == [f"{LABEL} applied your tap (Yes)."]
    assert env.channel.released == [(str(OPERATOR), "cid-1")]
    assert not env.desk.lock.locked()
    # the permit: taken after the lock, released inside it
    assert env.limiter.scopes == [f"{OPERATOR}:finance"]
    (permit,) = env.limiter.permits
    assert permit.released and permit.released_under_lock is True


async def test_the_models_own_text_never_reaches_the_operator_the_desk_or_the_resident(env):
    async def respond(call):
        call.owner.resolve(pr.Capture("receipt", "applied"))
        return tools_mod.DelegatedOutput(text="SECRET-MODEL-TEXT")
    env.respond = respond
    await _tap(env)
    everything = ([str(m) for m, _ in env.channel.replies] + [t for _, t in env.channel.notices]
                  + [e.text for e in env.desk.log] + _echo())
    assert everything and not any("SECRET" in s for s in everything)


async def test_the_receipt_is_bounded_and_its_first_line_is_the_exchange(env):
    async def respond(call):
        call.owner.resolve(pr.Capture("receipt", "line one\n" + "x" * 5000))
        return tools_mod.DelegatedOutput(text="")
    env.respond = respond
    await _tap(env)
    (message, _), = env.channel.replies
    body = str(message).split("\n", 1)[1]
    assert len(body) <= sd.STORED_CALL_RECEIPT_CHARS + len(sd.CLIP)
    assert env.desk.log[-1].text.startswith("line one")


# --- the re-checks, under the lock, each its own refusal ------------------------------

def _refused(env, reason):
    assert env.calls == [], "no turn runs on a refusal"
    assert env.channel.marks == [f"✖ {reason}"]
    assert env.channel.notices == [(OPERATOR, f"{LABEL} could not apply your tap ({reason}).")]
    assert env.desk.log == [] and env.desk.last_used is None       # a refused tap is not a use
    assert _echo() == [f"{LABEL} refused your tap (Yes): {reason}."]
    assert env.limiter.permits == []                                # refused BEFORE the permit
    assert env.desk.waiting == 0 and not env.desk.lock.locked()
    assert env.channel.released == [(str(OPERATOR), "cid-1")]


async def test_a_resident_that_no_longer_declares_the_specialist_is_refused(env):
    env.assistant.delegates = ()
    await _tap(env)
    _refused(env, "not delegable")


async def test_a_plugin_no_longer_assigned_to_the_role_is_refused(env):
    env.build = _build(plugins=[])
    await _tap(env)
    _refused(env, "plugin unassigned")


async def test_a_replaced_artifact_is_refused(env):
    env.build = _build(artifact="8" * 64)
    await _tap(env)
    _refused(env, "plugin changed")


async def test_a_profile_narrowed_past_the_tool_is_refused(env):
    narrow = ProfiledPlugin(name="probe", profile="narrow", prefixes=("mcp__plugin_probe_probe__",),
                            allowed_names=frozenset({"mcp__plugin_probe_probe__more"}),
                            declared_excluded=frozenset())
    env.build = _build(plan=ProfilePlan(entries=(narrow,), loaded=("probe",)))
    await _tap(env)
    _refused(env, "profile")


async def test_a_profile_that_still_allows_the_tool_runs(env):
    wide = ProfiledPlugin(name="probe", profile="wide", prefixes=("mcp__plugin_probe_probe__",),
                          allowed_names=frozenset({APPLY}), declared_excluded=frozenset())
    env.build = _build(plan=ProfilePlan(entries=(wide,), loaded=("probe",)))
    await _tap(env)
    assert len(env.calls) == 1 and env.channel.marks == []


async def test_a_plugin_whose_erasure_is_running_is_refused(env):
    env.fence.names.add("probe")
    await _tap(env)
    _refused(env, "plugin erasing")


@pytest.mark.parametrize("build, reason", [
    (lambda: _build(tools=[]), "undeclared"),
    (lambda: _build(protected={APPLY: {"artifact_id": ART}}), "protected"),
    (lambda: _build(tools=[_tool("apply", transport="http")]), "transport"),
])
async def test_a_stored_call_the_live_maps_no_longer_admit_is_refused_with_its_reason(env, build, reason):
    env.build = build()
    await _tap(env)
    _refused(env, reason)


async def test_a_deadline_passed_while_waiting_executes_nothing_with_no_notice_and_no_exchange(env):
    await _tap(env, meta={"deadline": asyncio.get_running_loop().time() - 1})
    assert env.calls == [] and env.channel.notices == [] and env.desk.log == []
    assert env.channel.marks == ["⌛ expired"]
    assert _echo() == []
    assert env.channel.released == [(str(OPERATOR), "cid-1")]


async def test_the_re_checks_read_the_world_after_the_desk_wait_not_before_it(env):
    await env.desk.lock.acquire()                     # a desk turn is running
    reservation = env.desk.reserve()
    task = asyncio.create_task(_tap(env, reservation=reservation))
    for _ in range(5):
        await asyncio.sleep(0)
    assert env.captures == []                         # nothing captured while waiting
    env.build = _build(plugins=[])                    # the assignment is removed meanwhile
    env.desk.lock.release()
    await task
    _refused(env, "plugin unassigned")


# --- the permit, after the lock ---------------------------------------------------------

async def test_a_refused_permit_is_the_busy_line_and_notice_with_no_turn(env):
    env.limiter.refuse = True
    await _tap(env)
    assert env.calls == []
    assert env.channel.marks == ["✖ busy"]
    assert env.channel.notices == [
        (OPERATOR, f"{LABEL} is busy; the specialist will propose again, or type your verdict.")]
    assert env.desk.log == []
    assert _echo() == [f"{LABEL} refused your tap (Yes): busy."]
    assert env.channel.released == [(str(OPERATOR), "cid-1")]


# --- the outcomes -------------------------------------------------------------------------

async def test_a_validated_capture_is_authoritative_over_an_aborted_turn(env):
    async def respond(call):
        call.owner.resolve(pr.Capture("receipt", "applied"))
        return tools_mod.DelegatedOutput(text="", run_subtype="error_max_turns")
    env.respond = respond
    await _tap(env)
    (message, _), = env.channel.replies
    assert str(message) == f"{LABEL}\napplied"
    assert env.channel.notices == [] and env.channel.marks == []
    assert env.desk.log[-1].text == "applied"
    assert _echo() == [f"{LABEL} applied your tap (Yes)."]


async def test_a_validated_capture_is_authoritative_over_a_runner_exception(env):
    async def respond(call):
        call.owner.resolve(pr.Capture("receipt", "applied"))
        raise RuntimeError("the CLI died after the call")
    env.respond = respond
    await _tap(env)
    assert [str(m) for m, _ in env.channel.replies] == [f"{LABEL}\napplied"]
    assert env.channel.notices == []


def _failed(env, kind):
    assert env.channel.replies == []
    assert env.channel.marks == ["✖ failed"]
    assert env.channel.notices == [(OPERATOR, f"{LABEL} could not apply your tap ({kind}).")]
    assert [(e.who, e.text) for e in env.desk.log] == [("operator", "[tapped: Yes]"),
                                                       ("specialist", sd.NO_RECEIPT)]
    assert _echo() == [f"{LABEL} refused your tap (Yes): {kind}."]
    (permit,) = env.limiter.permits
    assert permit.released and not env.desk.lock.locked() and env.desk.waiting == 0


async def test_a_turn_that_executed_no_call_is_the_refusal_notice_with_no_retry(env):
    async def respond(call):
        return tools_mod.DelegatedOutput(text="I could not do that")
    env.respond = respond
    await _tap(env)
    _failed(env, "no_call")
    assert len(env.calls) == 1


async def test_an_aborted_turn_without_a_capture_names_the_abort_kind(env):
    async def respond(call):
        return tools_mod.DelegatedOutput(text="", run_subtype="error_max_turns")
    env.respond = respond
    await _tap(env)
    _failed(env, tools_mod._run_abort_kind("error_max_turns"))


async def test_a_runner_exception_without_a_capture_names_the_error_class(env):
    async def respond(call):
        raise RuntimeError("boom")
    env.respond = respond
    await _tap(env)
    _failed(env, tools_mod._classify_error(RuntimeError("boom")).value)


async def test_a_failure_capture_names_the_error_class_never_a_receipt(env):
    async def respond(call):
        call.owner.resolve(pr.Capture("error", "McpError"))
        return tools_mod.DelegatedOutput(text="")
    env.respond = respond
    await _tap(env)
    _failed(env, "McpError")


async def test_the_tell_line_rides_above_the_receipt_and_in_the_echo(env):
    async def respond(call):
        call.owner.resolve(pr.Capture("receipt", "applied", rewritten=True))
        return tools_mod.DelegatedOutput(text="")
    env.respond = respond
    await _tap(env)
    (message, _), = env.channel.replies
    assert str(message) == f"{LABEL}\n{sd.TELL_LINE}\napplied"
    assert _echo() == [f"{LABEL} applied your tap (Yes) — the CLI reported arguments "
                       "changed by an installed hook."]


async def test_a_more_proposal_that_landed_is_the_sole_receipt(env):
    async def respond(call):
        call.owner.resolve(pr.Capture("delivered"))
        return tools_mod.DelegatedOutput(text="")
    env.respond = respond
    await _tap(env, idx=1)
    assert env.channel.replies == [] and env.channel.notices == [] and env.channel.marks == []
    assert [(e.who, e.text) for e in env.desk.log] == [("operator", "[tapped: More]"),
                                                       ("specialist", sd.POSTED_PROPOSAL)]
    assert _echo() == [f"{LABEL} applied your tap (More)."]
    (call,) = env.calls
    assert call.owner.runtime_name == "mcp__plugin_probe_probe__more"


async def test_a_more_proposal_that_landed_with_a_rewrite_tells_once_in_the_echo(env):
    async def respond(call):
        call.owner.resolve(pr.Capture("delivered", rewritten=True))
        return tools_mod.DelegatedOutput(text="")
    env.respond = respond
    await _tap(env, idx=1)
    assert env.channel.replies == [] and env.channel.notices == []   # the hook told, in the message
    assert _echo() == [f"{LABEL} applied your tap (More) — the CLI reported arguments "
                       "changed by an installed hook."]


async def test_a_more_proposal_withheld_or_not_delivered_is_the_refusal_notice(env):
    async def respond(call):
        call.owner.resolve(pr.Capture("withheld", "not delivered"))
        return tools_mod.DelegatedOutput(text="")
    env.respond = respond
    await _tap(env, idx=1)
    assert env.channel.replies == []
    assert env.channel.marks == ["✖ failed"]
    assert env.channel.notices == [(OPERATOR, f"{LABEL} could not apply your tap (not delivered).")]
    assert env.desk.log[-1].text == sd.NO_RECEIPT
    assert _echo() == [f"{LABEL} refused your tap (More): not delivered."]


async def test_a_more_tool_that_posted_nothing_has_its_own_text_as_the_receipt(env):
    async def respond(call):
        call.owner.resolve(pr.Capture("no_post", '{"proposal": null, "note": "no more entries"}'))
        return tools_mod.DelegatedOutput(text="")
    env.respond = respond
    await _tap(env, idx=1)
    (message, context), = env.channel.replies
    assert str(message) == f'{LABEL}\n{{"proposal": null, "note": "no more entries"}}'
    assert context["_post"].kind == "receipt"
    assert env.channel.notices == [] and env.channel.marks == []
    assert _echo() == [f"{LABEL} applied your tap (More)."]


async def test_a_receipt_whose_send_fails_is_told_as_applied_without_the_receipt(env):
    env.channel = _Channel(fail_send=True)
    await _tap(env)
    assert len(env.channel.replies) == 1
    assert env.channel.notices == [(OPERATOR, f"{LABEL} applied your tap; the receipt did not go out.")]
    assert env.channel.marks == []
    assert env.desk.log[-1].text == "applied match 17"
    assert _echo() == [f"{LABEL} applied your tap (Yes)."]


# --- the ceiling, the termination path, the faulted desk ---------------------------------

async def test_the_ceiling_sends_the_notice_at_once_terminates_and_settles_timed_out(env, monkeypatch):
    monkeypatch.setattr(tools_mod, "_DELEGATION_CEILING_S", 0.05)

    async def respond(call):
        await asyncio.sleep(30)
    env.respond = respond
    await asyncio.wait_for(_tap(env), 5)
    (owner,) = env.terminated
    assert owner is env.calls[0].owner
    _failed(env, "timed out")
    assert env.desk.faulted is None


async def test_a_capture_landing_during_the_hold_is_still_the_receipt(env, monkeypatch):
    monkeypatch.setattr(tools_mod, "_DELEGATION_CEILING_S", 0.05)

    async def respond(call):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            call.owner.resolve(pr.Capture("receipt", "applied late"))   # an entered callback finishing
            raise
    env.respond = respond
    await asyncio.wait_for(_tap(env), 5)
    assert len(env.terminated) == 1
    # the ceiling notice went out at once; the receipt is posted when the hold ends
    assert env.channel.notices == [(OPERATOR, f"{LABEL} could not apply your tap (timed out).")]
    assert [str(m) for m, _ in env.channel.replies] == [f"{LABEL}\napplied late"]
    assert env.desk.log[-1].text == "applied late"
    assert _echo() == [f"{LABEL} refused your tap (Yes): timed out.", f"{LABEL} applied your tap (Yes)."]


async def test_an_unconfirmed_termination_faults_the_desk_releases_the_permit_and_tells(env, monkeypatch, caplog):
    monkeypatch.setattr(tools_mod, "_DELEGATION_CEILING_S", 0.05)
    env.confirm = False

    async def respond(call):
        await asyncio.sleep(30)
    env.respond = respond
    with caplog.at_level(logging.ERROR, logger="specialist_desk"):
        await asyncio.wait_for(_tap(env), 5)
    assert env.desk.faulted
    assert any(r.levelno == logging.ERROR and "faulted" in r.getMessage() for r in caplog.records)
    faulted_line = f"{LABEL}'s desk is faulted; a Casa restart clears it."
    assert env.channel.notices == [(OPERATOR, f"{LABEL} could not apply your tap (timed out)."),
                                   (OPERATOR, faulted_line)]
    assert faulted_line in _echo()
    (permit,) = env.limiter.permits
    assert permit.released                              # a process that cannot be killed holds no capacity
    assert not env.desk.lock.locked() and env.desk.waiting == 0


async def test_a_faulted_desk_refuses_a_tap_a_reply_and_a_queued_reply_at_once(env):
    env.desk.faulted = "unconfirmed termination of run abc"
    faulted_line = f"{LABEL}'s desk is faulted; a Casa restart clears it."
    await _tap(env)
    assert env.calls == [] and env.channel.marks == ["✖ faulted"]
    assert env.channel.notices == [(OPERATOR, faulted_line)]
    assert env.desk.log == []
    # a reply that takes its own queue place
    env.channel = _Channel()
    await sd.handle_reply(channel=env.channel, resident_role="assistant", chat_id=OPERATOR,
                          user_id=OPERATOR, user_name="N", message_id=70, cid="cid-2", text="hi",
                          quoted_text=None, record=None, desk_role="finance")
    assert env.channel.notices == [(OPERATOR, faulted_line)]
    assert env.calls == [] and env.channel.released == [(str(OPERATOR), "cid-2")]
    # a reply QUEUED before the fault: it finds the fault after the lock
    env.channel = _Channel()
    env.desk.faulted = None
    await env.desk.lock.acquire()
    reservation = env.desk.reserve()
    task = asyncio.create_task(sd.handle_reply(
        channel=env.channel, resident_role="assistant", chat_id=OPERATOR, user_id=OPERATOR,
        user_name="N", message_id=71, cid="cid-3", text="hi", quoted_text=None, record=None,
        desk_role="finance", reservation=reservation))
    for _ in range(5):
        await asyncio.sleep(0)
    env.desk.faulted = "unconfirmed termination of run abc"
    env.desk.lock.release()
    await task
    assert env.channel.notices == [(OPERATOR, faulted_line)]
    assert env.calls == [] and env.desk.log == [] and env.limiter.permits == []
    assert env.desk.waiting == 0 and not env.desk.lock.locked()


async def test_a_delegation_queued_before_the_fault_is_refused_under_the_lock(env):
    async def run(cfg, task_text, context_text, resolution=None, output_format=None):
        raise AssertionError("a faulted desk runs nothing")
    await env.desk.lock.acquire()
    reservation = env.desk.reserve()
    task = asyncio.create_task(sd.delegation_use(
        env.desk, reservation=reservation, run=run, cfg=tools_mod._agent_role_map["finance"],
        task_text="t", context_text="c", scope=f"{OPERATOR}:finance"))
    for _ in range(5):
        await asyncio.sleep(0)
    env.desk.faulted = "unconfirmed termination of run abc"
    env.desk.lock.release()
    with pytest.raises(sd.DeskFaulted):
        await task
    assert env.limiter.permits == [] and env.desk.log == []
    assert env.desk.waiting == 0 and not env.desk.lock.locked()


# --- cleanup on cancel ----------------------------------------------------------------------

async def test_a_cancel_while_the_run_is_in_flight_releases_everything(env):
    started = asyncio.Event()

    async def respond(call):
        started.set()
        await asyncio.sleep(30)
    env.respond = respond
    task = asyncio.create_task(_tap(env))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (permit,) = env.limiter.permits
    assert permit.released and env.desk.waiting == 0 and not env.desk.lock.locked()
    assert env.channel.released == [(str(OPERATOR), "cid-1")]


async def test_a_cancel_while_waiting_for_the_desk_releases_the_place(env):
    await env.desk.lock.acquire()
    reservation = env.desk.reserve()
    task = asyncio.create_task(_tap(env, reservation=reservation))
    for _ in range(3):
        await asyncio.sleep(0)
    assert env.desk.waiting == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    env.desk.lock.release()
    assert env.desk.waiting == 0 and env.calls == []
    assert env.channel.released == [(str(OPERATOR), "cid-1")]


# --- the controller's close and the transcript, after the run ------------------------------

@pytest.fixture
def deleter(monkeypatch):
    deletes = []

    async def fake_delete(sid, directory, role):
        deletes.append((sid, directory, role, list(env_terminated)))
    env_terminated = []
    monkeypatch.setattr(tools_mod, "_delete_own_delegated_transcript", fake_delete)
    return SimpleNamespace(deletes=deletes, terminated=env_terminated)


async def test_the_desk_deletes_the_pinned_runs_transcript_after_the_controller_finished(env, deleter):
    async def respond(call):
        call.owner.transcript = ("sid-1", "/agent-home/finance")
        call.owner.resolve(pr.Capture("receipt", "applied"))
        return tools_mod.DelegatedOutput(text="")
    env.respond = respond
    await _tap(env)
    assert deleter.deletes == [("sid-1", "/agent-home/finance", "finance", [])]
    assert env.calls[0].owner.sealed is True            # finish(): sealed and drained


async def test_the_transcript_is_deleted_only_after_termination_on_the_ceiling(env, deleter, monkeypatch):
    monkeypatch.setattr(tools_mod, "_DELEGATION_CEILING_S", 0.05)

    async def respond(call):
        call.owner.transcript = ("sid-2", "/agent-home/finance")
        await asyncio.sleep(30)
    env.respond = respond
    real_terminate = pr.PinnedRun.terminate

    async def terminate(self, task=None):
        deleter.terminated.append(self.run_id)
        return await real_terminate(self, task)
    monkeypatch.setattr(pr.PinnedRun, "terminate", terminate)
    await asyncio.wait_for(_tap(env), 5)
    (delete,) = deleter.deletes
    assert delete[:3] == ("sid-2", "/agent-home/finance", "finance")
    assert delete[3] == [env.calls[0].owner.run_id]      # terminate ran BEFORE the delete


async def test_processes_still_alive_after_an_orderly_close_enter_the_termination_path(env, monkeypatch):
    async def finish(self):
        return False                                       # the orderly close did not end them
    monkeypatch.setattr(pr.PinnedRun, "finish", finish)

    async def respond(call):
        return tools_mod.DelegatedOutput(text="")
    env.respond = respond
    await _tap(env)
    (owner,) = env.terminated
    assert owner is env.calls[0].owner
    _failed(env, "processes alive")
    assert env.desk.faulted is None


# --- the health surface: a faulted desk is reported until restart ---------------------------

async def test_a_faulted_desk_is_listed_with_its_plugin_and_lands_in_the_health_report(env, monkeypatch, tmp_path):
    import plugin_health
    import plugin_registry
    monkeypatch.setattr(tools_mod, "_DELEGATION_CEILING_S", 0.05)
    env.confirm = False

    async def respond(call):
        await asyncio.sleep(30)
    env.respond = respond
    await asyncio.wait_for(_tap(env), 5)
    (desk,) = sd.DESKS.faulted()
    assert desk is env.desk and desk.fault.plugin == "probe" and desk.fault.since == 1000.0
    assert env.refreshes == [True]                           # the report is regenerated at once
    (issue,) = sd.faulted_desk_issues()
    assert (issue.name, issue.target, issue.reason_code) == ("probe", "specialist:finance", "desk_faulted")
    assert str(OPERATOR) in str(issue.detail)
    # the regeneration carries the row, and the operator phrasing never shows the code
    import callback_reconcile
    import event_reconcile
    import trigger_reconcile
    monkeypatch.setattr(tools_mod, "_PLUGIN_HEALTH_PATH", tmp_path / "health.json")
    for mod in (trigger_reconcile, callback_reconcile, event_reconcile):
        monkeypatch.setattr(mod, "current_issues", lambda: [])
    monkeypatch.setattr(plugin_registry, "resolve_all", lambda: SimpleNamespace(issues=[], warnings=[]))
    monkeypatch.setattr(plugin_registry, "load_registry", lambda *a, **k: SimpleNamespace(valid=False, entries=[]))
    written = {}
    monkeypatch.setattr(plugin_health, "write_report", lambda **kw: written.update(kw))
    tools_mod._regenerate_plugin_health([])
    rows = [i for i in written["issues"] if getattr(i, "reason_code", None) == "desk_faulted"]
    assert len(rows) == 1 and rows[0].name == "probe"
    line = plugin_health.describe_issue({"name": "probe", "reason_code": "desk_faulted", "detail": issue.detail})
    assert "desk_faulted" not in line and "restart" in line and "probe" in line


def test_without_a_fault_nothing_is_listed(env):
    assert sd.DESKS.faulted() == [] and sd.faulted_desk_issues() == []


async def test_a_slow_but_normal_cli_exit_within_the_deadline_produces_no_notice(env, monkeypatch):
    """BRAIN 2026-10-03: the normal-end path (PinnedRun.finish) is new code the
    design never reviewed — a CLI that takes a moment to leave after the SDK's
    close must not be told as a failure, nor enter the termination path."""
    import subprocess
    import sys
    cli = subprocess.Popen([sys.executable, "-c", "import time\nwhile True: time.sleep(1)"])

    class _SlowClient:
        async def __aexit__(self, *a):
            await asyncio.sleep(0.4)                 # the SDK's disconnect takes a moment…
            cli.kill()                               # …and then the CLI leaves
            return False

    async def respond(call):
        owner = call.owner
        owner._pin_tree(cli.pid, None)               # as enter() pins the real CLI
        owner._client = _SlowClient()
        owner.resolve(pr.Capture("receipt", "applied"))
        return tools_mod.DelegatedOutput(text="")
    env.respond = respond
    try:
        await asyncio.wait_for(_tap(env), 5)
        assert env.terminated == [] and env.channel.notices == [] and env.channel.marks == []
        assert [str(m) for m, _ in env.channel.replies] == [f"{LABEL}\napplied"]
        owner = env.calls[0].owner
        assert owner.alive() is False and owner.sealed is True and env.desk.faulted is None
        assert _echo() == [f"{LABEL} applied your tap (Yes)."]
    finally:
        try:
            cli.kill()
        except ProcessLookupError:
            pass
        cli.wait(timeout=5)


# --- diff round 1 (Astra A2 / Astra B S1): the release condition holds on cancellation too ----

async def _cancel_in_flight(env):
    started = asyncio.Event()

    async def respond(call):
        started.set()
        await asyncio.sleep(30)
    env.respond = respond
    task = asyncio.create_task(_tap(env))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)


async def test_a_cancel_in_flight_runs_the_bounded_termination_before_the_desk_is_released(env):
    await _cancel_in_flight(env)
    (owner,) = env.terminated                               # a channel stop still terminates, bounded
    assert owner is env.calls[0].owner and owner.sealed
    (permit,) = env.limiter.permits
    assert permit.released and env.desk.waiting == 0 and not env.desk.lock.locked()
    assert env.desk.faulted is None


async def test_a_cancel_whose_termination_is_unconfirmed_faults_the_desk(env, caplog):
    env.confirm = False
    with caplog.at_level(logging.ERROR, logger="specialist_desk"):
        await _cancel_in_flight(env)
    assert env.terminated and env.desk.faulted
    assert any("faulted" in r.getMessage() for r in caplog.records if r.levelno == logging.ERROR)
    (permit,) = env.limiter.permits
    assert permit.released and not env.desk.lock.locked()


async def test_a_second_cancellation_as_the_termination_completes_still_runs_the_cleanup(env, monkeypatch, deleter):
    """Terra diff r2 S2: a cancellation arriving exactly when the shielded
    termination finishes must not skip the fault decision, the transcript
    delete and the fd close."""
    closed = []
    monkeypatch.setattr(pr.PinnedRun, "close_fds", lambda self: closed.append(self))
    env.confirm = False
    holder = {}

    async def terminate(self, task=None):
        env.terminated.append(self)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=1)
        self.sealed = True
        holder["tap"].cancel()                               # the second cancellation lands here
        return env.confirm
    monkeypatch.setattr(pr.PinnedRun, "terminate", terminate)
    started = asyncio.Event()

    async def respond(call):
        call.owner.transcript = ("sid-c", "/agent-home/finance")
        started.set()
        await asyncio.sleep(30)
    env.respond = respond
    holder["tap"] = asyncio.create_task(_tap(env))
    await started.wait()
    holder["tap"].cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(holder["tap"], 5)
    assert env.desk.faulted                                  # the fault decision ran
    assert [d[:3] for d in deleter.deletes] == [("sid-c", "/agent-home/finance", "finance")]
    assert closed == [env.calls[0].owner]
    assert not env.desk.lock.locked() and env.desk.waiting == 0


# --- the one release path (BRAIN, diff round 2: "same shape twice: cut the mechanism") -------

def _calls_in(node):
    """Every call as ``receiver.attr`` (``owner.finish``) or a bare name."""
    import ast
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                out.append(f"{f.value.id}.{f.attr}")
            elif isinstance(f, ast.Name):
                out.append(f.id)
    return out


def test_the_pinned_runs_release_is_reachable_only_through_the_one_settle_function():
    """Structural pin: every act that ends a pinned run's hold — the
    confirmation (`finish`/`terminate`), the fault decision, the transcript
    delete, the fd close and the permit release — lives in ONE function,
    `_settle_pinned_run`, and `handle_tap` reaches it on every path only
    through `_shielded`; nothing else in `handle_tap` performs any of them."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(sd))
    fns = {n.name: n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)}
    settle, tap = fns["_settle_pinned_run"], fns["handle_tap"]
    RELEASING = {"owner.finish", "owner.terminate", "owner.close_fds", "permit.release",
                 "tools_mod._delete_own_delegated_transcript"}
    in_settle = set(_calls_in(settle))
    assert RELEASING <= in_settle, RELEASING - in_settle
    in_tap = [c for c in _calls_in(tap) if c in RELEASING]
    assert in_tap == [], in_tap                     # none of them anywhere else in handle_tap
    # every call of the settle function in handle_tap is wrapped in _shielded
    shielded_args = [a for n in ast.walk(tap) if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Name) and n.func.id == "_shielded" for a in n.args]
    settle_calls = [n for n in ast.walk(tap) if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name) and n.func.id == "_settle_pinned_run"]
    assert settle_calls and all(any(c is a for a in shielded_args) for c in settle_calls)
    # the fault decision is settle's alone
    faults = [n for n in ast.walk(tap) if isinstance(n, ast.Attribute) and n.attr == "fault"
              and isinstance(n.ctx, ast.Store)]
    assert faults == []
    assert any(isinstance(n, ast.Attribute) and n.attr == "fault" and isinstance(n.ctx, ast.Store)
               for n in ast.walk(settle))


async def test_a_termination_cancelled_by_the_loops_shutdown_still_faults_and_cleans_up(env, monkeypatch, deleter):
    """Astra B diff r2 S1: when the teardown task is itself cancelled (every
    task cancelled, as a loop shutdown does), the run is conservatively
    unconfirmed: the desk is faulted and the fds and permit still released."""
    closed = []
    monkeypatch.setattr(pr.PinnedRun, "close_fds", lambda self: closed.append(self))

    async def terminate(self, task=None):
        env.terminated.append(self)
        if task is not None and not task.done():
            task.cancel()
        raise asyncio.CancelledError()                       # the teardown task was cancelled too
    monkeypatch.setattr(pr.PinnedRun, "terminate", terminate)
    await _cancel_in_flight(env)
    assert env.desk.faulted and closed == [env.calls[0].owner]
    (permit,) = env.limiter.permits
    assert permit.released and not env.desk.lock.locked() and env.desk.waiting == 0


async def test_a_cancellation_during_the_receipts_send_is_disclosed_not_silent(env, caplog):
    """Astra B diff r2 S1: a captured receipt whose send is cancelled is still
    an executed call — the exchange is logged, the resident's line records the
    applied tap, and an ERROR names the run."""
    gate = asyncio.Event()

    async def slow_send(message, context):
        gate.set()
        await asyncio.sleep(30)
    env.channel.send_response = slow_send
    task = asyncio.create_task(_tap(env))
    await gate.wait()
    with caplog.at_level(logging.ERROR, logger="specialist_desk"):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
    assert any("receipt" in r.getMessage() and "cancel" in r.getMessage().lower()
               for r in caplog.records if r.levelno == logging.ERROR)
    assert [(e.who, e.text) for e in env.desk.log] == [("operator", "[tapped: Yes]"),
                                                       ("specialist", "applied match 17")]
    assert _echo() == [f"{LABEL} applied your tap (Yes)."]
    (permit,) = env.limiter.permits
    assert permit.released and not env.desk.lock.locked() and env.desk.waiting == 0


async def test_the_shield_hands_back_the_works_own_cancellation_without_spinning():
    """A settle task cancelled by the loop itself (every task cancelled at
    shutdown) ends `_shielded` with that cancellation at once — never a spin
    on a finished task, never a swallowed cancellation."""
    async def work():
        asyncio.current_task().cancel()
        await asyncio.sleep(0)
    t = asyncio.create_task(sd._shielded(work()))
    done, _ = await asyncio.wait({t}, timeout=1)
    assert t in done and t.cancelled()

    async def late():
        await asyncio.sleep(0.05)
        return "settled"
    t2 = asyncio.create_task(sd._shielded(late()))
    await asyncio.sleep(0)
    t2.cancel()                                           # the caller is cancelled mid-wait…
    assert await asyncio.wait_for(t2, 1) == ("settled", True)   # …still gets the result, and knows it was cancelled


async def test_an_unconfirmed_teardown_deletes_the_transcript_only_after_the_writer_left(env, monkeypatch, deleter):
    """Astra A1b diff r3 S2: a CLI whose exit is unconfirmed may still flush its
    transcript; the delete is owned by a detached waiter that runs after the
    pinned fds report exit — the desk's bounded hold is not extended."""
    monkeypatch.setattr(tools_mod, "_DELEGATION_CEILING_S", 0.05)
    env.confirm = False
    waiters = []
    real = pr.PinnedRun.schedule_after_exit

    def schedule(self, coro_factory):
        t = real(self, coro_factory)
        waiters.append(t)
        return t
    monkeypatch.setattr(pr.PinnedRun, "schedule_after_exit", schedule)

    import subprocess
    import sys
    cli = subprocess.Popen([sys.executable, "-c", "import time\nwhile True: time.sleep(1)"])

    async def respond(call):
        call.owner._pin_tree(cli.pid, None)                 # the writer, still alive after the hold
        call.owner.transcript = ("sid-late", "/agent-home/finance")
        await asyncio.sleep(30)
    env.respond = respond
    try:
        await asyncio.wait_for(_tap(env), 5)
        assert env.desk.faulted and env.terminated
        assert deleter.deletes == []                         # not while the writer may be alive
        (waiter,) = waiters
        await asyncio.sleep(0.1)
        assert not waiter.done()
        cli.kill()
        await asyncio.wait_for(waiter, 5)                    # the writer left: the delete runs
        assert [d[:3] for d in deleter.deletes] == [("sid-late", "/agent-home/finance", "finance")]
    finally:
        try:
            cli.kill()
        except ProcessLookupError:
            pass
        cli.wait(timeout=5)


async def test_a_cancellation_during_the_settlement_still_propagates_after_it(env, monkeypatch):
    """Terra diff r4 S2: a cancellation that lands while the settle function
    runs (the normal-end path) must not be swallowed — the settle completes,
    the captured receipt is disclosed, and the cancellation propagates; no
    receipt is posted as if the tap had not been cancelled."""
    gate = asyncio.Event()
    holder = {}

    async def finish(self):
        gate.set()
        await asyncio.sleep(0.2)                            # the orderly close takes a moment…
        return True
    monkeypatch.setattr(pr.PinnedRun, "finish", finish)

    async def respond(call):
        call.owner.resolve(pr.Capture("receipt", "applied"))
        return tools_mod.DelegatedOutput(text="")
    env.respond = respond
    holder["tap"] = asyncio.create_task(_tap(env))
    await gate.wait()
    holder["tap"].cancel()                                  # …during which the channel stops
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(holder["tap"], 5)
    assert env.channel.replies == []                        # nothing posted after a cancellation
    assert [(e.who, e.text) for e in env.desk.log] == [("operator", "[tapped: Yes]"), ("specialist", "applied")]
    assert _echo() == [f"{LABEL} applied your tap (Yes)."]
    (permit,) = env.limiter.permits
    assert permit.released and not env.desk.lock.locked() and env.desk.waiting == 0
