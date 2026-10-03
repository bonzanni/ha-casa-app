"""S4 §5/§9: the desk turn — one specialist run on the operator's exact
words, under the desk lock from the log read to the exchange's commit, the
permit acquired after the lock and released inside it; the reply as the
specialist's admitted, labelled, bounded text through the resident reply
path with its pages filed in the post map; one labelled Casa notice whenever
the turn has no proven operator-visible outcome; the resident's body-free
echo (INV-DESK-002, INV-DESK-003).
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

import agent as agent_mod
import result_broker as rb
import specialist_desk as sd
import tools as tools_mod
from channels import DeliveryOutcome
from output_boundary import Admitted

pytestmark = pytest.mark.asyncio

# the real bounded runner, captured before any fixture replaces it (#1197)
_REAL_BOUNDED = tools_mod._run_delegated_agent_bounded

OPERATOR = 42
LABEL = "📊 Finance"


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
        self.replies = []
        self.notices = []
        self.released = []
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

    def _release_typing(self, context, chat_id):
        self.released.append((chat_id, context.get("cid")))


@pytest.fixture
def env(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(sd, "DESKS", sd.DeskRegistry(now=lambda: clock["t"]))
    monkeypatch.setattr(sd, "DESK_ECHO", rb.PostLedger(max_events=64))
    monkeypatch.setattr(rb, "POSTS", rb.PostLedger())
    monkeypatch.setattr(rb, "POST_MAP", rb.PostMap())
    finance = SimpleNamespace(role="finance", kind="specialist", delegates=(),
                              character=SimpleNamespace(name="Finance"))
    monkeypatch.setattr(tools_mod, "_agent_role_map", {
        "assistant": SimpleNamespace(role="assistant", kind="resident",
                                     delegates=(SimpleNamespace(agent="finance"),),
                                     character=SimpleNamespace(name="Ellen")),
        "finance": finance})
    limiter = _Limiter()
    monkeypatch.setattr(tools_mod, "_specialist_limiter", limiter)
    calls = []

    async def runner(cfg, task_text, context_text, resolution=None, output_format=None):
        calls.append(SimpleNamespace(cfg=cfg, task=task_text, context=context_text,
                                     origin=dict(agent_mod.origin_var.get(None) or {}),
                                     turn_id=tools_mod._delegation_quota_key.get()))
        return await env.respond(calls[-1])

    async def _default(call):
        return tools_mod.DelegatedOutput(text="Here you go: **42**")
    env = SimpleNamespace(clock=clock, limiter=limiter, calls=calls, respond=_default)
    monkeypatch.setattr(tools_mod, "_run_delegated_agent_bounded", runner)
    env.channel = _Channel()
    env.desk = sd.DESKS.get_or_create(OPERATOR, "finance")
    limiter.desk_holder.append(env.desk)
    return env


def _record():
    return rb.PostRecord(role="finance", operator_id=OPERATOR, plugin="probe", slot="report",
                         tool_use_id="call-1", owner="d-1", posted_at=900.0)


async def _reply(env, text="  more detail please ", **over):
    kw = dict(channel=env.channel, resident_role="assistant", chat_id=OPERATOR,
              user_id=OPERATOR, user_name="Nicola", message_id=70, cid="cid-1",
              text=text, quoted_text="📊 Finance\nQ3 report", record=_record(),
              desk_role="finance", continuation=False)
    kw.update(over)
    await sd.handle_reply(**kw)


# --- the happy path -------------------------------------------------------------

async def test_a_reply_runs_one_specialist_turn_and_posts_the_labelled_admitted_reply(env):
    await _reply(env)
    (call,) = env.calls
    assert call.cfg is tools_mod._agent_role_map["finance"]
    assert call.task == "  more detail please "                       # exact words
    assert "<desk>" not in call.context                               # first use: no log
    assert call.context.startswith(sd.turn_frame("Ellen"))            # #1192: the same operator, now
    assert "The operator replied to your post" in call.context
    assert "📊 Finance\nQ3 report" in call.context
    (message, context), = env.channel.replies
    assert str(message) == LABEL + "\nHere you go: **42**"
    assert context["chat_id"] == str(OPERATOR) and context["cid"] == "cid-1"
    post = context["_post"]
    assert (post.role, post.operator_id, post.slot, post.owner) == ("finance", OPERATOR, "desk", call.turn_id)
    assert [(e.who, e.text) for e in env.desk.log] == [
        ("operator", "  more detail please "), ("specialist", "Here you go: **42**")]
    assert env.desk.last_used == 1000.0 and env.desk.waiting == 0
    assert env.channel.notices == []
    assert sd.prompt_prefix(OPERATOR) == "(front desk) 📊 Finance answered your reply (1 page).\n\n"
    assert env.channel.released == [(str(OPERATOR), "cid-1")]
    assert not env.desk.lock.locked()


async def test_the_desk_origin_classifies_as_a_delegated_dm_turn_with_a_desk_identity(env):
    from authz_grants import resolve_grant_identity
    from provenance import turn_provenance
    seen = {}

    async def respond(call):
        prov = turn_provenance()
        seen["prov"] = (prov.transport, prov.execution)
        seen["identity"] = resolve_grant_identity("finance", artifact_id="1" * 64)
        seen["origin"] = call.origin
        return tools_mod.DelegatedOutput(text="ok")
    env.respond = respond
    await _reply(env)
    assert seen["prov"] == ("dm", "delegated")
    identity, why = seen["identity"]
    assert why is None
    assert (identity.operator_id, identity.chat_id, identity.enforcement_role) == (OPERATOR, OPERATOR, "finance")
    assert identity.target_role == "assistant" and identity.desk_role == "finance"
    assert identity.delegation_id == seen["origin"]["_delegation_id"] == env.calls[0].turn_id
    o = seen["origin"]
    assert (o["channel"], o["source"], o["message_type"]) == ("telegram", "telegram", "channel_in")
    assert (o["role"], o["execution_role"], o["delegation_depth"]) == ("assistant", "finance", 1)
    assert o["user_text"] == "  more detail please " and o["_operator_turn"] is True
    assert o["desk"] == {"role": "finance", "chat_id": OPERATOR}
    assert o["turn_scope"].display_name == "Finance" and o["turn_scope"].role == "assistant"
    assert env.limiter.scopes == [f"{OPERATOR}:finance"]


async def test_turns_serialise_and_the_second_sees_the_first_exchange(env):
    gate = asyncio.Event()
    active = {"n": 0, "max": 0}

    async def respond(call):
        active["n"] += 1
        active["max"] = max(active["max"], active["n"])
        if len(env.calls) == 1:
            await gate.wait()
        active["n"] -= 1
        return tools_mod.DelegatedOutput(text=f"answer {len(env.calls)}")
    env.respond = respond
    t1 = asyncio.create_task(_reply(env, text="first"))
    await asyncio.sleep(0)
    t2 = asyncio.create_task(_reply(env, text="second"))
    await asyncio.sleep(0.01)
    assert env.desk.waiting == 1 and len(env.calls) == 1
    gate.set()
    await asyncio.gather(t1, t2)
    assert active["max"] == 1
    assert "] the operator: first" in env.calls[1].context
    assert "] you: answer 1" in env.calls[1].context
    assert "answer 1" in env.calls[1].context
    assert [e.text for e in env.desk.log] == ["first", "answer 1", "second", "answer 2"]


async def test_the_permit_is_acquired_after_the_lock_and_released_inside_it(env):
    await _reply(env)
    (permit,) = env.limiter.permits
    assert permit.released is True and permit.released_under_lock is True


async def test_the_reply_is_bounded_like_a_sync_answer(env):
    from specialist_limits import _MAX_OUTPUT_CHARS

    async def respond(call):
        return tools_mod.DelegatedOutput(text="x" * (_MAX_OUTPUT_CHARS + 5000))
    env.respond = respond
    await _reply(env)
    (message, _), = env.channel.replies
    assert len(str(message)) <= len(LABEL) + 1 + _MAX_OUTPUT_CHARS + 200


async def test_a_continuation_turn_has_no_quote_and_joins_the_same_desk(env):
    env.desk.append("operator", "earlier", now=990.0)
    await _reply(env, text="[authorization approved]: call it", quoted_text=None, record=None,
                 continuation=True)
    (call,) = env.calls
    assert "The operator replied" not in call.context and "earlier" in call.context
    assert call.task == "[authorization approved]: call it"
    # #1192: an approval continuation's task is Casa's note of the operator's
    # decision, not the operator's own words — its frame says so
    assert call.context.startswith(sd.turn_frame("Ellen", continuation=True))
    assert sd.turn_frame("Ellen") not in call.context


# --- #1198: a reply starting with "/" is text, not a command --------------------------

def _seed_prior_exchange(env):
    env.desk.append("operator", "earlier question", now=990.0)
    env.desk.append("specialist", "earlier answer", now=990.0)


def _base_reply_context(log):
    """The base's context for a reply on `_record()` quoting "📊 Finance\\nQ3 report",
    frozen independently of `_compose_context`."""
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(900.0))
    return (sd.turn_frame("Ellen") + "\n\n" + sd.render_block(log, resident_name="Ellen")
            + "\n\n" + f"The operator replied to your post (slot report, posted {when}) "
            "which read:\n📊 Finance\nQ3 report")


@pytest.mark.parametrize("text", ["/new", "/new, start over", "  /new"])
async def test_slash_reply_explains_no_command_or_reset(env, text):
    _seed_prior_exchange(env)

    await _reply(env, text=text, continuation=False)

    assert len(env.calls) == 1
    call = env.calls[0]
    assert call.task == text

    assert call.context.count("<desk>") == 1
    assert call.context.count("</desk>") == 1
    block = call.context.split("<desk>", 1)[1].split("</desk>", 1)[0]
    assert block.count("earlier question") == 1
    assert block.count("earlier answer") == 1

    assert len(env.channel.notices) == 0
    assert len(env.channel.replies) == 1
    assert [entry.text for entry in env.desk.log] == [
        "earlier question", "earlier answer",
        text, "Here you go: **42**",
    ]

    context = call.context.lower()
    assert (
        "not" in context
        and ("command" in context or "executed" in context)
        and "reset" in context
    ), "slash reply context must explain no command execution and no reset"


async def test_a_non_slash_reply_context_is_the_base_context_byte_for_byte(env):
    _seed_prior_exchange(env)
    log = list(env.desk.log)
    await _reply(env, text="more detail please")
    (call,) = env.calls
    assert call.context == _base_reply_context(log)


async def test_a_slash_continuation_context_is_the_base_context_byte_for_byte(env):
    _seed_prior_exchange(env)
    log = list(env.desk.log)
    await _reply(env, text="/new", quoted_text=None, record=None, continuation=True)
    (call,) = env.calls
    assert call.context == (sd.turn_frame("Ellen", continuation=True) + "\n\n"
                            + sd.render_block(log, resident_name="Ellen"))


# --- no proven outcome ⇒ one labelled notice -----------------------------------------

async def test_an_aborted_run_posts_no_text_and_one_notice(env):
    async def respond(call):
        return tools_mod.DelegatedOutput(text="partial…", run_subtype="error_max_turns",
                                         result_message_seen=True)
    env.respond = respond
    await _reply(env)
    assert env.channel.replies == []
    assert env.channel.notices == [(OPERATOR, LABEL + " could not handle your reply (specialist_turn_limit).")]
    assert [(e.who, e.text) for e in env.desk.log] == [("operator", "  more detail please "), ("specialist", sd.NO_REPLY)]
    assert "could not handle your reply" in sd.prompt_prefix(OPERATOR)
    assert env.channel.released and not env.desk.lock.locked()


async def test_a_raising_run_is_a_notice_with_its_kind(env):
    async def respond(call):
        raise RuntimeError("boom " + "secret-7f3e")
    env.respond = respond
    await _reply(env)
    assert env.channel.replies == []
    ((_, text),) = env.channel.notices
    assert text.startswith(LABEL + " could not handle your reply (") and "secret-7f3e" not in text
    assert env.limiter.permits[0].released is True


async def test_a_silent_turn_whose_plugin_posted_is_silent_and_logged_as_a_view(env):
    async def respond(call):
        rb.POSTS.record(call.turn_id, rb.PostEvent("c", "probe", "report", LABEL, 2, None))
        return tools_mod.DelegatedOutput(text="<silent/>")
    env.respond = respond
    await _reply(env)
    assert env.channel.replies == [] and env.channel.notices == []
    assert [e.text for e in env.desk.log] == ["  more detail please ", sd.POSTED_VIEW]
    prefix = sd.prompt_prefix(OPERATOR)
    assert "posted to your chat (2 pages)" in prefix and "answered" not in prefix


async def test_a_silent_turn_with_no_outcome_is_a_notice(env):
    async def respond(call):
        return tools_mod.DelegatedOutput(text="")
    env.respond = respond
    await _reply(env)
    assert env.channel.notices == [(OPERATOR, LABEL + " had nothing to add.")]
    assert [e.text for e in env.desk.log] == ["  more detail please ", sd.NO_REPLY]


async def test_a_failed_reply_send_is_logged_and_noticed(env):
    env.channel.fail_send = True
    await _reply(env)
    assert len(env.channel.replies) == 1
    assert env.channel.notices == [(OPERATOR, LABEL + " answered; complete delivery could not be confirmed.")]
    assert [e.text for e in env.desk.log] == ["  more detail please ", "Here you go: **42**"]
    assert "could not be confirmed" in sd.prompt_prefix(OPERATOR)


async def test_a_refused_permit_is_a_busy_notice_and_runs_nothing(env):
    env.limiter.refuse = True
    await _reply(env)
    assert env.calls == [] and env.channel.replies == []
    assert env.channel.notices == [(OPERATOR, LABEL + " was at its concurrent-work limit when the turn was attempted.")]
    assert env.desk.log == [] and not env.desk.lock.locked() and env.desk.waiting == 0


async def test_a_full_queue_is_refused_before_any_run(env):
    env.desk.waiting = sd.DESK_QUEUE_MAX
    await _reply(env)
    assert env.calls == [] and env.channel.notices == [(OPERATOR, LABEL + "'s desk was full when the place was requested.")]
    assert env.desk.waiting == sd.DESK_QUEUE_MAX and env.channel.released == [(str(OPERATOR), "cid-1")]


async def test_an_idle_desk_starts_fresh(env):
    env.desk.append("operator", "yesterday", now=1000.0 - sd.DESK_IDLE_S - 5)
    await _reply(env)
    assert "yesterday" not in env.calls[0].context
    assert [e.text for e in env.desk.log] == ["  more detail please ", "Here you go: **42**"]


# --- round 1 folds -------------------------------------------------------------------

async def test_a_cancelled_waiting_reply_releases_its_reservation(env):
    await env.desk.lock.acquire()                               # a turn is running
    task = asyncio.create_task(_reply(env, text="queued"))
    await asyncio.sleep(0.01)
    assert env.desk.waiting == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert env.desk.waiting == 0 and env.calls == []
    assert env.channel.released == [(str(OPERATOR), "cid-1")]
    env.desk.lock.release()


async def test_idle_is_measured_from_the_turns_completion_not_its_start(env):
    async def respond(call):
        env.clock["t"] += 500.0                                 # a long run
        return tools_mod.DelegatedOutput(text="slow answer")
    env.respond = respond
    await _reply(env, text="first")
    assert env.desk.last_used == 1500.0 and env.desk.log[-1].at == 1500.0
    env.respond = None
    env.clock["t"] = 1500.0 + 3200.0                            # idle 3,200 s since completion
    await _reply(env, text="second")
    assert "slow answer" in env.calls[1].context                # not reset


async def test_a_full_queue_is_refused_without_reserving_twice(env):
    """The channel reserves before spawning (§4); the turn is handed the
    reservation and must not take a second one."""
    place = env.desk.reserve()
    assert place is not None
    await _reply(env, text="handed", reservation=place)
    assert len(env.calls) == 1 and env.desk.waiting == 0 and place.released


async def test_the_exchange_is_stamped_after_delivery_settles(env):
    async def slow_send(message, context):
        env.clock["t"] += 20.0                                  # a slow reply delivery
        env.channel.replies.append((message, context))
        return DeliveryOutcome.DELIVERED
    env.channel.send_response = slow_send
    await _reply(env)
    assert env.desk.last_used == 1020.0 and all(e.at == 1020.0 for e in env.desk.log)


async def test_a_silent_turn_whose_only_outcome_was_a_delivered_file_still_echoes(env):
    """A specialist that sent a file through send_media and then stayed
    silent: no reply, no notice — but the resident still learns of it."""
    async def respond(call):
        scope = agent_mod.origin_var.get()["turn_scope"]
        scope.open_send("media").delivered = True
        return tools_mod.DelegatedOutput(text="<silent/>")
    env.respond = respond
    await _reply(env)
    assert env.channel.replies == [] and env.channel.notices == []
    assert [e.text for e in env.desk.log] == ["  more detail please ", sd.POSTED_VIEW]
    prefix = sd.prompt_prefix(OPERATOR)
    assert prefix == "(front desk) 📊 Finance sent you a file.\n\n"


async def test_a_silent_turns_echo_names_what_was_sent_not_always_a_file(env):
    """Round 5: the echo line is derived from the intents of the delivered
    sends (the boundary's own taxonomy) — a keyboard is a question, a
    discrete message is a message, media is a file; one line per kind."""
    async def respond(call):
        scope = agent_mod.origin_var.get()["turn_scope"]
        scope.open_send("keyboard").delivered = True
        scope.open_send("discrete").delivered = True
        scope.open_send("keyboard").delivered = True
        return tools_mod.DelegatedOutput(text="<silent/>")
    env.respond = respond
    await _reply(env)
    assert env.channel.replies == [] and env.channel.notices == []
    assert env.desk.log[-1].text == sd.POSTED_VIEW
    assert sd.prompt_prefix(OPERATOR) == (
        "(front desk) 📊 Finance asked you a question.\n"
        "(front desk) 📊 Finance sent you a message.\n\n")


async def test_a_silent_turn_after_a_delivered_link_is_a_view_with_a_casa_line(env, monkeypatch):
    """Round 6: a proven outcome is anything that LANDED for this turn — the
    post map by owner — or a delivered send; an operator_link records no
    S3 echo event, so the desk composes its own body-free line for it."""
    pm = rb.PostMap()
    monkeypatch.setattr(rb, "POST_MAP", pm)
    async def respond(call):
        pm.record(OPERATOR, 501, rb.PostRecord(
            role="finance", operator_id=OPERATOR, plugin="probe", slot="open",
            tool_use_id="c-link", owner=call.turn_id, posted_at=1.0, kind=rb.OPERATOR_LINK))
        return tools_mod.DelegatedOutput(text="<silent/>")
    env.respond = respond
    await _reply(env)
    assert env.channel.replies == [] and env.channel.notices == []
    assert [e.text for e in env.desk.log] == ["  more detail please ", sd.POSTED_VIEW]
    assert sd.prompt_prefix(OPERATOR) == "(front desk) 📊 Finance posted a link to your chat.\n\n"


async def test_a_landed_message_post_echoes_exactly_its_s3_line(env, monkeypatch):
    """A message post lands in the map AND records its S3 event: one line,
    the S3 one (with its page count), never a second Casa line for the
    same post."""
    pm = rb.PostMap()
    monkeypatch.setattr(rb, "POST_MAP", pm)
    async def respond(call):
        rb.POSTS.record(call.turn_id, rb.PostEvent("c", "probe", "report", LABEL, 2, None))
        for mid in (601, 602):
            pm.record(OPERATOR, mid, rb.PostRecord(
                role="finance", operator_id=OPERATOR, plugin="probe", slot="report",
                tool_use_id="c", owner=call.turn_id, posted_at=1.0, kind=rb.OPERATOR_MESSAGE))
        return tools_mod.DelegatedOutput(text="<silent/>")
    env.respond = respond
    await _reply(env)
    assert env.channel.notices == []
    assert sd.prompt_prefix(OPERATOR) == "(front desk) 📊 Finance posted to your chat (2 pages).\n\n"


async def test_one_delivered_send_is_an_outcome_even_when_a_later_send_failed(env):
    """Round 8: a send that landed is what the operator saw; a later send
    that failed does not erase it (§5.6: silence after a delivered outcome
    stays silent). Only the delivered sends are echoed."""
    async def respond(call):
        scope = agent_mod.origin_var.get()["turn_scope"]
        scope.open_send("media").delivered = True
        scope.open_send("keyboard")                       # opened, never confirmed
        return tools_mod.DelegatedOutput(text="<silent/>")
    env.respond = respond
    await _reply(env)
    assert env.channel.replies == [] and env.channel.notices == []
    assert env.desk.log[-1].text == sd.POSTED_VIEW
    assert sd.prompt_prefix(OPERATOR) == "(front desk) 📊 Finance sent you a file.\n\n"


# --- #1197: a run cut off past its teardown bound keeps the desk ----------------

class _Survivor:
    """`tools._run_delegated_agent` whose FIRST run keeps going after it is
    cancelled, until ``release`` is set — an unwind that outlives the bounded
    runner's teardown bound. Later runs answer at once. Counts runs started
    and the most ever running together."""

    def __init__(self):
        self.release = asyncio.Event()
        self.starts = 0
        self.active = 0
        self.peak = 0
        self.tasks = []

    async def run(self, cfg, task_text, context_text, resolution=None,
                  output_format=None, tool_counts=None):
        self.starts += 1
        first = self.starts == 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.tasks.append(asyncio.current_task())
        try:
            while first and not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    pass                      # still unwinding
            return tools_mod.DelegatedOutput(text="late" if first else "answer")
        finally:
            self.active -= 1

    async def started(self):
        for _ in range(200):
            if self.starts:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("the first run never started")

    async def end_first(self):
        self.release.set()
        await asyncio.gather(*self.tasks, return_exceptions=True)


def _survive(monkeypatch):
    survivor = _Survivor()
    monkeypatch.setattr(tools_mod, "_run_delegated_agent_bounded", _REAL_BOUNDED)
    monkeypatch.setattr(tools_mod, "_run_delegated_agent", survivor.run)
    monkeypatch.setattr(tools_mod, "_DELEGATION_CEILING_S", 0.05)
    monkeypatch.setattr(tools_mod, "_CEILING_TEARDOWN_BOUND_S", 0.05)
    return survivor


async def _no_second_run_until_the_first_has_ended(env, survivor):
    """The first reply has returned while its run is still executing: a
    second reply must not start a run beside it (it waits, or is refused);
    once the first run has ended, the next reply runs."""
    assert survivor.starts == 1 and survivor.active == 1      # still unwinding
    faulted = (OPERATOR, sd.faulted_line(sd.label_for("finance")))
    second = asyncio.create_task(_reply(env, text="second"))
    try:
        await asyncio.wait({second}, timeout=0.3)
        assert survivor.starts == 1 and survivor.peak == 1
        refused = second.done()
        if refused:                     # refused: the labelled faulted notice, once
            assert env.channel.notices.count(faulted) == 1
    finally:
        await survivor.end_first()
    await asyncio.wait_for(second, 5)
    if not refused:                     # it waited: it runs once the first has ended
        assert survivor.starts == 2 and env.channel.notices.count(faulted) == 0
    before = survivor.starts
    await asyncio.wait_for(_reply(env, text="third"), 5)
    assert survivor.starts == before + 1 and survivor.peak == 1
    assert not env.desk.lock.locked() and not env.desk.faulted


async def test_a_reply_cut_off_at_the_ceiling_keeps_its_desk_until_its_run_has_ended(env, monkeypatch):
    survivor = _survive(monkeypatch)
    try:
        await asyncio.wait_for(_reply(env), 5)
        timeouts = [n for n in env.channel.notices if "(timeout)" in n[1]]
        assert len(timeouts) == 1                               # INV-DESK-002's one notice
        assert [e.text for e in env.desk.log][-1] == sd.NO_REPLY
        await _no_second_run_until_the_first_has_ended(env, survivor)
    finally:
        await survivor.end_first()


async def test_a_cancelled_reply_keeps_its_desk_until_its_run_has_ended(env, monkeypatch):
    survivor = _survive(monkeypatch)
    try:
        first = asyncio.create_task(_reply(env))
        await survivor.started()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first, 5)
        await _no_second_run_until_the_first_has_ended(env, survivor)
    finally:
        await survivor.end_first()


async def test_a_sticky_fault_set_while_a_cut_off_run_unwinds_outlives_that_run(env, monkeypatch):
    """#1197 must never clear S5's sticky fault: a desk faulted ("termination
    unconfirmed", refused until restart) while a cut-off reply's run is still
    unwinding stays faulted, with S5's own reason and health row, after that
    run has ended — and the next reply is refused and starts no run."""
    survivor = _survive(monkeypatch)
    try:
        await asyncio.wait_for(_reply(env), 5)
        assert survivor.active == 1                              # still unwinding
        assert env.desk.faulted == sd.UNWINDING and env.desk.fault is None
        assert sd.DESKS.faulted() == []                          # unwinding: no health row
        fault = sd.DeskFault(run_id="r-1", plugin="probe", since=1000.0,
                             reason="run r-1: termination unconfirmed (normal)")
        env.desk.fault = fault
        await survivor.end_first()
        assert survivor.active == 0                              # the cut-off run has ended
        assert env.desk.fault is fault and env.desk.faulted == fault.reason
        assert sd.DESKS.faulted() == [env.desk]
        faulted = (OPERATOR, sd.faulted_line(sd.label_for("finance")))
        before = survivor.starts
        await asyncio.wait_for(_reply(env, text="after the run ended"), 5)
        assert survivor.starts == before                         # refused: nothing ran
        assert env.channel.notices.count(faulted) == 1
        assert env.desk.fault is fault and not env.desk.lock.locked()
    finally:
        await survivor.end_first()
