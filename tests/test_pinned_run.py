"""S5 §5.2.4/§5.3/§5.4: the pinned run's controller — ``enter`` pins the CLI
and its descendants by pidfd; the pin allows exactly one call (the stored
tool, the stored canonical arguments, once; sealed ⇒ deny); the guard seals
entry and counts the S5 callbacks inside; ``terminate`` signals the pinned
fds, seals, abandons the SDK client, confirms exit by pidfd readability
under one deadline, drains the counter and discards the confirmed-dead CLI
from the SDK's reaper set — and reports an unconfirmed exit instead of
assuming it. The REAL broker callbacks run inside the guard: the pin is the
admission hook's first check, the capture is the result hook's last act with
its own effective result (INV-PROP-001, INV-PROP-002).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import types

import pytest

import pinned_run as pr
import result_broker as rb
import tools as tools_mod
from channels import DeliveryOutcome
from test_proposal_slot import (  # noqa: F401 — the env fixture rides along
    APPLY, ARTIFACT, GUARDED, LABEL, MORE, OFFER, PROTECTED, SEG, SLOT, _identity, _map, _open,
    _proposal, env,
)

CANON = '{"choice":"yes","match_id":17}'
ARGS = {"match_id": 17, "choice": "yes"}

CHILD = r"""
import subprocess, sys, time
g = subprocess.Popen([sys.executable, "-c", "import time\nwhile True: time.sleep(1)"])
print(g.pid, flush=True)
while True:
    time.sleep(1)
"""


@pytest.fixture
def tree():
    """A fixture process tree: a Python child that spawns a grandchild."""
    proc = subprocess.Popen([sys.executable, "-c", CHILD], stdout=subprocess.PIPE, text=True)
    grandchild = int(proc.stdout.readline().strip())
    yield types.SimpleNamespace(proc=proc, pid=proc.pid, grandchild=grandchild)
    for pid in (grandchild, proc.pid):
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        pass


class _Process:
    def __init__(self, pid):
        self.pid, self.returncode = pid, None


class _FakeClient:
    def __init__(self, pid, *, hang_exit=False, hang_enter=False):
        self.process = _Process(pid)
        self._transport = types.SimpleNamespace(_process=self.process)
        self.entered = self.exits = 0
        self.hang_exit = hang_exit
        self.hang_enter = hang_enter

    async def __aenter__(self):
        self.entered += 1
        if self.hang_enter:
            await asyncio.sleep(3600)                   # the CLI started; its initialisation hangs
        return self

    async def __aexit__(self, *a):
        self.exits += 1
        if self.hang_exit:
            await asyncio.sleep(3600)
        return False


def _owner(**over):
    kw = dict(run_id="run-1", runtime_name=APPLY, canonical=CANON, label="Yes",
              build_input=pr.BuildInput(cfg=None, resolution=None, withheld=(), protected={},
                                        contract_map=_map(), plan=None, target="specialist:finance"))
    kw.update(over)
    return pr.PinnedRun(**kw)


async def _enter(owner, tree, **client_kw):
    client = _FakeClient(tree.pid, **client_kw)
    got = await owner.enter(None, client_factory=lambda options: client)
    assert got is client and client.entered == 1
    return client


# --- enter: the pidfds ------------------------------------------------------------------

async def test_enter_pins_the_cli_and_every_descendant_with_a_pidfd_each(tree):
    owner = _owner()
    await _enter(owner, tree)
    assert owner.pinned_pids() == {tree.pid, tree.grandchild}
    assert owner.alive() is True
    assert all(fd >= 0 for fd in owner.pinned_fds())


def test_descendants_are_read_from_proc_by_parent_pid(tree):
    assert pr._descendants(tree.pid) == [tree.grandchild]
    assert pr._descendants(tree.grandchild) == []


async def test_an_extinct_pid_pins_nothing_and_an_unreadable_one_is_unconfirmed(monkeypatch):
    owner = _owner()
    client = _FakeClient(2 ** 22 - 1)                        # no such process
    await owner.enter(None, client_factory=lambda o: client)
    assert owner.pinned_pids() == set() and owner.alive() is False
    assert await owner.terminate() is True                    # nothing to confirm: confirmed

    def boom(pid):
        raise PermissionError("pidfd_open")
    monkeypatch.setattr(pr, "_pidfd_open", boom)
    owner2 = _owner()
    await owner2.enter(None, client_factory=lambda o: _FakeClient(os.getpid()))
    assert await owner2.terminate() is False                  # identity not established ⇒ unconfirmed


# --- the pin -----------------------------------------------------------------------------

def test_the_pin_allows_exactly_the_stored_call_once_and_denies_everything_else():
    owner = _owner()
    assert owner.pin("Bash", {"command": "ls"}) == "not the stored call"
    assert owner.pin(APPLY, {"choice": "yes", "match_id": 18}) == "not the stored arguments"
    assert owner.pin(APPLY, {"choice": "yes", "match_id": 17.0}) == "not the stored arguments"
    assert owner.fired is False
    assert owner.pin(APPLY, {"match_id": 17, "choice": "yes"}) is None     # key order is not a difference
    assert owner.fired is True
    assert owner.pin(APPLY, ARGS) == "the stored call already ran"
    owner.sealed = True
    assert owner.pin(APPLY, ARGS) == "the run is sealed"


async def test_the_pin_hook_for_other_tools_denies_in_the_cli_shape():
    owner = _owner()
    out = await owner.pin_hook({"tool_name": "Bash", "tool_input": {"command": "ls"}}, "t1", {})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == "not the stored call"
    assert await owner.pin_hook({"tool_name": APPLY, "tool_input": ARGS}, "t2", {}) == {}
    owner.sealed = True
    assert await asyncio.wait_for(owner.pin_hook({"tool_name": APPLY, "tool_input": ARGS}, "t3", {}), 1) == {}  # sealed: no effect


# --- the guard: the seal and the counter -----------------------------------------------

async def test_a_sealed_callback_returns_at_once_and_an_entered_one_is_counted_until_it_leaves():
    owner = _owner()
    ran, gate = [], asyncio.Event()

    async def body(input_data, tool_use_id, context):
        ran.append(tool_use_id)
        await gate.wait()
        return {"x": 1}
    guarded = owner.guard(body)
    task = asyncio.create_task(guarded({}, "t1", {}))
    await asyncio.sleep(0)
    assert owner.entered == 1
    owner.sealed = True
    assert await asyncio.wait_for(guarded({}, "t2", {}), 1) == {}   # enters after the seal: nothing, at once
    assert ran == ["t1"] and owner.entered == 1
    gate.set()
    assert await task == {"x": 1}
    assert owner.entered == 0


async def test_terminate_waits_for_an_entered_callback_to_leave_then_releases(monkeypatch):
    monkeypatch.setattr(pr.PinnedRun, "EXIT_WAIT_S", 2.0)
    owner = _owner()
    gate = asyncio.Event()

    async def body(input_data, tool_use_id, context):
        await gate.wait()
        owner.resolve(pr.Capture("receipt", "late but entered"))
        return {}
    task = asyncio.create_task(owner.guard(body)({}, "t1", {}))
    await asyncio.sleep(0)
    asyncio.get_running_loop().call_later(0.2, gate.set)
    t0 = time.monotonic()
    assert await owner.terminate() is True
    assert 0.15 <= time.monotonic() - t0 < 2.0
    assert owner.sealed and owner.entered == 0
    assert owner.captured == pr.Capture("receipt", "late but entered")   # still authoritative
    await task


async def test_a_callback_still_inside_at_the_deadline_is_an_unconfirmed_release(monkeypatch):
    monkeypatch.setattr(pr.PinnedRun, "EXIT_WAIT_S", 0.2)
    owner = _owner()
    gate = asyncio.Event()

    async def body(input_data, tool_use_id, context):
        await gate.wait()
        return {}
    task = asyncio.create_task(owner.guard(body)({}, "t1", {}))
    await asyncio.sleep(0)
    assert await owner.terminate() is False
    assert owner.entered == 1 and owner.captured is None
    gate.set()
    await task


# --- terminate: the processes ------------------------------------------------------------

async def test_terminate_kills_the_tree_confirms_by_pidfd_abandons_the_client_and_discards_the_reaper_entry(tree, monkeypatch):
    owner = _owner()
    client = await _enter(owner, tree, hang_exit=True)
    reaper = {client.process}
    monkeypatch.setattr(pr, "_active_children", lambda: reaper)

    async def execution():
        await asyncio.sleep(3600)
    task = asyncio.create_task(execution())
    await asyncio.sleep(0)
    t0 = time.monotonic()
    assert await owner.terminate(task) is True
    elapsed = time.monotonic() - t0
    assert elapsed < pr.PinnedRun.TASK_WAIT_S + pr.PinnedRun.GRACE_S + pr.PinnedRun.EXIT_WAIT_S
    assert task.cancelled() or task.done()
    assert owner.alive() is False and owner.sealed is True
    assert client.exits == 1                                 # the close was STARTED…
    assert owner.close_task is not None and not owner.close_task.done()   # …and never awaited
    assert reaper == set()                                   # the confirmed-dead CLI left the reaper set
    tree.proc.wait(timeout=5)
    assert tree.proc.returncode is not None
    owner.close_task.cancel()


async def test_a_pinned_process_that_does_not_die_is_reported_unconfirmed_within_the_bound(tree, monkeypatch):
    monkeypatch.setattr(pr.PinnedRun, "GRACE_S", 0.2)
    monkeypatch.setattr(pr.PinnedRun, "EXIT_WAIT_S", 0.3)
    monkeypatch.setattr(pr, "_send_signal", lambda fd, sig: None)   # the signals do not land
    owner = _owner()
    await _enter(owner, tree)
    t0 = time.monotonic()
    assert await owner.terminate() is False
    assert time.monotonic() - t0 < 2.0
    assert owner.alive() is True and owner.sealed is True


# --- the real broker callbacks inside the guard --------------------------------------------

def _pre(tool, tool_input):
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input}


def _post(tool, tool_input, response):
    return {"hook_event_name": "PostToolUse", "tool_name": tool, "tool_input": tool_input,
            "tool_response": response}


async def test_the_pin_is_the_admission_hooks_first_check_before_the_authorization_decision(env, monkeypatch):
    import plugin_erasure
    monkeypatch.setattr(plugin_erasure, "FENCE", plugin_erasure.EraseFence())   # no fence from another file
    authz_calls = []

    async def authz(input_data, tool_use_id, context):
        authz_calls.append(input_data["tool_name"])
        return rb._deny("challenge posted")
    owner = _owner()
    pinned = rb.make_plugin_admission_hook("finance", _map(), client_id="c1", authz_hook=authz,
                                           protected=PROTECTED, store=env.store, owner=owner)
    out = await pinned(_pre(GUARDED, {}), "t1", {})
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == "not the stored call"
    assert authz_calls == []                                  # denied BEFORE the authz hook
    assert await pinned(_pre(APPLY, ARGS), "t2", {}) == {}    # the one call passes on
    out = await pinned(_pre(APPLY, ARGS), "t3", {})
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == "the stored call already ran"
    # the unpinned positive control reaches the authorization decision
    plain = rb.make_plugin_admission_hook("finance", _map(), client_id="c1", authz_hook=authz,
                                          protected=PROTECTED, store=env.store)
    out = await plain(_pre(GUARDED, {}), "t4", {})
    assert authz_calls == [GUARDED] and out["hookSpecificOutput"]["permissionDecisionReason"] == "challenge posted"


async def test_a_safe_tools_response_is_captured_as_the_receipt_at_the_hooks_end(env):
    owner = _owner()
    hook = rb.make_result_hook(_map(), client_id="c1", store=env.store, owner=owner)
    assert await hook(_post(APPLY, ARGS, "applied match 17"), "t1", {}) == {}
    assert owner.captured == pr.Capture("receipt", "applied match 17", rewritten=False)
    # another tool's result never resolves the watch (one-shot, keyed on the stored tool)
    owner2 = _owner()
    hook2 = rb.make_result_hook(_map(), client_id="c1", store=env.store, owner=owner2)
    await hook2(_post(GUARDED, {}, "other"), "t2", {})
    assert owner2.captured is None


async def test_a_reported_input_differing_from_the_stored_canonical_is_told_and_logged_at_error(env, caplog):
    owner = _owner()
    hook = rb.make_result_hook(_map(), client_id="c1", store=env.store, owner=owner)
    with caplog.at_level(logging.ERROR, logger="result_broker"):
        await hook(_post(APPLY, {"choice": "yes", "match_id": 17, "__consentNonce": "n"}, "applied"), "t1", {})
    assert owner.captured == pr.Capture("receipt", "applied", rewritten=True)
    rec = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(rec) == 1
    msg = rec[0].getMessage()
    assert "run-1" in msg and APPLY in msg and CANON in msg and "__consentNonce" in msg


async def test_a_failure_captures_the_error_class_only(env):
    owner = _owner()
    hook = rb.make_failure_hook(_map(), client_id="c1", store=env.store, owner=owner)
    await hook({"hook_event_name": "PostToolUseFailure", "tool_name": APPLY, "tool_input": ARGS,
                "error": "match 17 is at revision 9 — the plugin's words"}, "t1", {})
    assert owner.captured == pr.Capture("error", "tool_error")


async def test_a_safe_json_response_uses_its_receipt_sentence(env):
    # #1200: a dict-returning tool reaches the hook as its JSON text; the operator's
    # receipt is the response's ``receipt`` sentence, not the JSON around it
    owner = _owner()
    hook = rb.make_result_hook(_map(), client_id="c1", store=env.store, owner=owner)
    response = json.dumps({"applied": True, "receipt": "applied yes to r-1 at revision 1"})
    assert await hook(_post(APPLY, ARGS, response), "t1", {}) == {}
    assert owner.captured == pr.Capture("receipt", "applied yes to r-1 at revision 1")


async def test_the_more_exceptions_capture_is_the_hooks_effective_result_never_the_raw_response(env):
    cases = []
    for name, response, expect in [
        ("delivered", None, pr.Capture("delivered")),
        ("withheld", json.dumps({SLOT: "casa-cap-" + "0" * 32}), None),
        ("no_post", json.dumps({SLOT: None, "note": "no more entries"}),
         pr.Capture("no_post", json.dumps({SLOT: None, "note": "no more entries"}))),
    ]:
        owner = _owner(runtime_name=MORE, canonical='{"page":2}')
        hook = rb.make_result_hook(_map(), client_id="c1", store=env.store, owner=owner)
        _open(env.store, call=f"call-{name}", tool=MORE)
        if name == "delivered":
            ref, err = env.store.deposit(client_id="c1", slot=SLOT, value=json.dumps(_proposal()))
            assert err is None
            response = json.dumps({SLOT: ref})
        out = await hook(_post(MORE, {"page": 2}, response), f"call-{name}", {})
        cases.append((name, out, owner.captured))
    name, out, cap = cases[0]
    assert cap == pr.Capture("delivered") and "casa_delivery" in out["hookSpecificOutput"]["updatedToolOutput"]
    assert len(env.rec.proposals) == 1
    name, out, cap = cases[1]
    assert cap.kind == "withheld" and cap.text and "casa_result_withheld" in out["hookSpecificOutput"]["updatedToolOutput"]
    name, out, cap = cases[2]
    assert out == {} and cap == pr.Capture("no_post", json.dumps({SLOT: None, "note": "no more entries"}))


class _NoticeRecorder:
    def __init__(self, rec):
        self._rec = rec
        self.notices = []

    def __getattr__(self, name):
        return getattr(self._rec, name)

    async def deliver_desk_notice(self, chat_id, text):
        self.notices.append((chat_id, text))
        return True


async def test_a_more_rewrite_tells_inside_the_next_proposal_when_it_fits_else_in_one_notice_after_it(env, monkeypatch):
    rec = _NoticeRecorder(env.rec)
    monkeypatch.setattr(tools_mod, "_channel_manager", types.SimpleNamespace(get=lambda n: rec))
    import specialist_desk as sd
    # fits one page: the tell line rides above the label, no separate notice
    owner = _owner(runtime_name=MORE, canonical='{"page":2}')
    hook = rb.make_result_hook(_map(), client_id="c1", store=env.store, owner=owner)
    _open(env.store, call="c-fit", tool=MORE)
    ref, _ = env.store.deposit(client_id="c1", slot=SLOT, value=json.dumps(_proposal()))
    await hook(_post(MORE, {"page": 2, "__consentNonce": "n"}, json.dumps({SLOT: ref})), "c-fit", {})
    assert owner.captured == pr.Capture("delivered", rewritten=True)
    (_, text, _, _, _), = env.rec.proposals
    assert text.startswith(sd.TELL_LINE + "\n" + LABEL + "\n")
    assert rec.notices == []
    # no longer fits: the proposal lands WITHOUT it and one labelled notice follows
    env.rec.proposals.clear()
    owner = _owner(runtime_name=MORE, canonical='{"page":2}')
    hook = rb.make_result_hook(_map(), client_id="c1", store=env.store, owner=owner)
    _open(env.store, call="c-full", tool=MORE)
    from text_util import utf16_len
    cap = 4096 - rb.PROPOSAL_SETTLE_RESERVE
    head_units, tell_units = utf16_len(LABEL + "\n"), utf16_len(sd.TELL_LINE + "\n")
    full = _proposal(text="🙂" * ((cap - head_units) // 2))   # the largest the deposit admits; not with the tell above it
    ref, err = env.store.deposit(client_id="c1", slot=SLOT, value=json.dumps(full))
    assert err is None
    await hook(_post(MORE, {"page": 2, "__consentNonce": "n"}, json.dumps({SLOT: ref})), "c-full", {})
    (_, text, _, _, _), = env.rec.proposals
    assert sd.TELL_LINE not in text and text.startswith(LABEL + "\n")
    assert rec.notices == [(42, f"{LABEL} {sd.TELL_LINE}")]
    assert owner.captured == pr.Capture("delivered", rewritten=True)
    # a proposal whose body plus the tell would fit Telegram's limit but NOT the
    # settlement reserve: the tell goes out as the notice, the message stays settleable
    env.rec.proposals.clear()
    rec.notices.clear()
    owner = _owner(runtime_name=MORE, canonical='{"page":2}')
    hook = rb.make_result_hook(_map(), client_id="c1", store=env.store, owner=owner)
    _open(env.store, call="c-edge", tool=MORE)
    edge = (cap - head_units - tell_units) // 2 + 1           # with the tell: over the reserve, under 4,096
    ref, err = env.store.deposit(client_id="c1", slot=SLOT, value=json.dumps(_proposal(text="🙂" * edge)))
    assert err is None
    assert cap < utf16_len(f"{sd.TELL_LINE}\n{LABEL}\n" + "🙂" * edge) <= 4096
    await hook(_post(MORE, {"page": 2, "__consentNonce": "n"}, json.dumps({SLOT: ref})), "c-edge", {})
    (_, text, _, _, _), = env.rec.proposals
    from text_util import utf16_len
    assert sd.TELL_LINE not in text and utf16_len(text) <= 4096 - rb.PROPOSAL_SETTLE_RESERVE
    assert rec.notices == [(42, f"{LABEL} {sd.TELL_LINE}")]


async def test_a_sealed_result_callback_has_no_effect(env):
    owner = _owner()
    hook = rb.make_result_hook(_map(), client_id="c1", store=env.store, owner=owner)
    owner.sealed = True
    assert await hook(_post(APPLY, ARGS, "applied"), "t1", {}) == {}
    assert owner.captured is None and owner.entered == 0


def test_broker_matchers_take_the_captured_contract_map_and_bind_the_owner(monkeypatch):
    import plugin_grants

    def boom(resolution):
        raise AssertionError("the contract map is captured, never rebuilt on a pinned turn")
    monkeypatch.setattr(plugin_grants, "result_contract_map", boom)
    owner = _owner()
    matchers = rb.broker_matchers("finance", None, client_id="c1", owner=owner)
    assert set(matchers) == {"PreToolUse", "PostToolUse", "PostToolUseFailure"}
    for event, lst in matchers.items():
        (m,) = lst
        (hook,) = m.hooks
        assert getattr(hook, "_casa_pinned", None) is owner, event


async def test_the_seal_is_set_before_the_exit_wait_so_a_callback_entering_during_it_has_no_effect(monkeypatch):
    owner = _owner()
    entered = []

    async def body(input_data, tool_use_id, context):
        entered.append(tool_use_id)
        return {"x": 1}
    guarded = owner.guard(body)
    seen = {}

    async def wait_exit(pinned, deadline):
        seen["sealed_during_wait"] = owner.sealed
        seen["callback_during_wait"] = await guarded({}, "late", {})   # scheduled by the dying CLI
        return True
    monkeypatch.setattr(owner, "_wait_exit", wait_exit)
    owner._cli = pr._Pinned(os.getpid(), os.open("/dev/null", os.O_RDONLY))   # readable at once: "exited"
    assert await owner.terminate() is True
    assert seen == {"sealed_during_wait": True, "callback_during_wait": {}}
    assert entered == []


async def test_the_catch_all_pin_hook_leaves_plugin_tools_to_the_admission_hook(env, monkeypatch):
    """Terra diff r1 S1: both matchers match a plugin tool (matcher=None matches
    every tool) and the CLI runs them concurrently; if both consumed the
    one-shot pin, the admission hook would deny the one stored call as
    already run and no tap could ever execute."""
    import plugin_erasure
    monkeypatch.setattr(plugin_erasure, "FENCE", plugin_erasure.EraseFence())
    owner = _owner()
    admission = rb.make_plugin_admission_hook("finance", _map(), client_id="c1", store=env.store, owner=owner)
    # the CLI's order is not fixed: catch-all first…
    assert await owner.pin_hook(_pre(APPLY, ARGS), "t1", {}) == {}
    assert owner.fired is False                                  # …it did not consume the pin
    assert await admission(_pre(APPLY, ARGS), "t1", {}) == {}     # the admission hook admits the one call
    assert owner.fired is True
    # …and admission first, catch-all after: still one allow, one no-op
    owner2 = _owner()
    admission2 = rb.make_plugin_admission_hook("finance", _map(), client_id="c1", store=env.store, owner=owner2)
    assert await admission2(_pre(APPLY, ARGS), "t2", {}) == {}
    assert await owner2.pin_hook(_pre(APPLY, ARGS), "t2", {}) == {}
    # another plugin tool is the admission hook's deny, the catch-all's no-op; a built-in is the catch-all's deny
    assert await owner2.pin_hook(_pre(GUARDED, {}), "t3", {}) == {}
    assert admission2 is not None
    out = await admission2(_pre(GUARDED, {}), "t3", {})
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == "not the stored call"
    out = await owner2.pin_hook({"tool_name": "Bash", "tool_input": {"command": "ls"}}, "t4", {})
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == "not the stored call"


# --- diff round 1 (Astra A2): ownership before the entry returns; an unprovable chain -------

async def test_a_ceiling_during_the_client_entry_still_pins_and_ends_the_cli(tree):
    owner = _owner()

    class _Hanging(_FakeClient):
        async def __aenter__(self):
            self.entered += 1
            await asyncio.sleep(3600)                   # the CLI started; its initialisation hangs
    client = _Hanging(tree.pid)
    task = asyncio.create_task(owner.enter(None, client_factory=lambda o: client))
    await asyncio.sleep(0.05)
    assert owner.pinned_pids() == set()                 # nothing pinned yet: the entry is pending
    assert await owner.terminate(task) is True          # the process is found at termination…
    assert owner.pinned_pids() == {tree.pid, tree.grandchild} and owner.alive() is False
    assert client.exits == 1                            # …and the client's close is started
    owner.close_task.cancel()


async def test_a_ceiling_during_an_entry_with_no_process_yet_cannot_be_confirmed():
    owner = _owner()

    class _NoProcess(_FakeClient):
        def __init__(self):
            super().__init__(None)
            self._transport = types.SimpleNamespace(_process=None)

        async def __aenter__(self):
            await asyncio.sleep(3600)
    task = asyncio.create_task(owner.enter(None, client_factory=lambda o: _NoProcess()))
    await asyncio.sleep(0.05)
    assert await owner.terminate(task) is False         # startup unresolved: never confirmed


async def test_an_entry_that_failed_before_any_process_is_confirmed_without_a_fault():
    owner = _owner()

    class _Failing(_FakeClient):
        def __init__(self):
            super().__init__(None)
            self._transport = types.SimpleNamespace(_process=None)

        async def __aenter__(self):
            raise RuntimeError("spawn failed")
    with pytest.raises(RuntimeError):
        await owner.enter(None, client_factory=lambda o: _Failing())
    assert await owner.finish() is True


async def test_a_descendant_whose_chain_cannot_be_proven_stays_pinned_and_is_unconfirmed(tree, monkeypatch):
    real = pr._ppid
    monkeypatch.setattr(pr, "_descendants", lambda pid: [tree.grandchild])   # listed while the CLI lived…

    def ppid(pid):
        return 1 if pid == tree.grandchild else real(pid)   # …then the CLI left; the server was reparented
    monkeypatch.setattr(pr, "_ppid", ppid)
    owner = _owner()
    owner._pin_tree(tree.pid, None)
    assert tree.grandchild in owner.pinned_pids()       # kept, not silently dropped
    assert await owner.finish() is False                # identity unprovable: never confirmed
    assert owner.alive() is True
    # even once every pinned process has exited, a run whose identity was never
    # established is not confirmed (the watched pid may not have been the server)
    for pid in (tree.grandchild, tree.pid):
        os.kill(pid, 9)
    tree.proc.wait(timeout=5)
    owner2 = _owner()
    monkeypatch.setattr(pr, "_ppid", lambda pid: None if pid == tree.pid else 1)
    owner2._pin_tree(tree.pid, None)                    # the CLI is gone: extinct, nothing pinned
    assert owner2.pinned_pids() == set()
    monkeypatch.setattr(pr.PinnedRun, "EXIT_WAIT_S", 0.5)
    assert owner.alive() is False
    assert await owner.finish() is False                # still unconfirmed: the flag, not a live process


# --- diff round 1 (Astra A1 S2): the capture reads Casa's own delivery status first ---------

@pytest.mark.parametrize("extra", [
    {"casa_result_withheld": True},                       # a plugin field that happens to carry Casa's key
    {"pad": "x" * (rb.MAX_RESPONSE_BYTES - 200)},         # a response at the raw-response ceiling
])
async def test_a_proven_proposal_delivery_is_captured_as_delivered_whatever_the_plugin_result_carries(env, extra):
    owner = _owner(runtime_name=MORE, canonical='{"page":2}')
    hook = rb.make_result_hook(_map(), client_id="c1", store=env.store, owner=owner)
    _open(env.store, call="c-x", tool=MORE)
    ref, err = env.store.deposit(client_id="c1", slot=SLOT, value=json.dumps(_proposal()))
    assert err is None
    response = json.dumps({SLOT: ref, **extra})
    assert len(response) <= rb.MAX_RESPONSE_BYTES
    out = await hook(_post(MORE, {"page": 2}, response), "c-x", {})
    assert "casa_delivery" in out["hookSpecificOutput"]["updatedToolOutput"]   # the post was proven
    assert len(env.rec.proposals) == 1
    assert owner.captured == pr.Capture("delivered")


async def test_a_descendant_is_dropped_as_extinct_only_on_its_own_pidfds_evidence(tree, monkeypatch):
    """Astra diff r2 S1: a failed /proc inspection (EMFILE, a permission
    error) is not an exit — only the retained pidfd's readability proves one.
    An unreadable child that is alive stays pinned, unprovable, unconfirmed."""
    monkeypatch.setattr(pr, "_descendants", lambda pid: [tree.grandchild])
    real = pr._ppid
    monkeypatch.setattr(pr, "_ppid", lambda pid: None if pid == tree.grandchild else real(pid))
    owner = _owner()
    owner._pin_tree(tree.pid, None)
    assert tree.grandchild in owner.pinned_pids()       # kept: alive, so not extinct
    monkeypatch.setattr(pr.PinnedRun, "EXIT_WAIT_S", 0.3)
    assert await owner.finish() is False                # unconfirmed, never released as sound
    # the same inspection failure on a child that HAS exited: extinct, dropped
    os.kill(tree.grandchild, 9)
    for _ in range(50):
        if pr._Pinned(tree.grandchild, pr._pidfd_open(tree.grandchild)).exited():
            break
        await asyncio.sleep(0.02)
    owner2 = _owner()
    owner2._pin_tree(tree.pid, None)
    assert owner2.pinned_pids() == {tree.pid}


async def test_a_late_delete_waits_for_every_pinned_exit_then_closes_the_fds(tree, monkeypatch):
    owner = _owner()
    owner._pin_tree(tree.pid, None)
    ran = []

    async def delete():
        ran.append(owner.alive())
    task = owner.schedule_after_exit(delete)
    await asyncio.sleep(0.2)
    assert ran == [] and not task.done()                    # the writer is alive: nothing yet
    for pid in (tree.grandchild, tree.pid):
        os.kill(pid, 9)
    await asyncio.wait_for(task, 5)
    assert ran == [False]                                   # ran once, after every exit
    for p in owner._all():
        with pytest.raises(OSError):
            os.fstat(p.fd)                                  # the fds were closed by the waiter


async def test_a_child_that_exited_before_validation_is_extinct_whatever_its_ancestry_reads(tree, monkeypatch):
    """Astra A2 diff r3 S2: a CLI and its server both gone before the chain
    walk — the server a reparented zombie — is extinction (its own pidfd is
    readable), never an unprovable identity: no fault for dead processes."""
    monkeypatch.setattr(pr, "_descendants", lambda pid: [tree.grandchild])
    real = pr._ppid
    monkeypatch.setattr(pr, "_ppid", lambda pid: 1 if pid == tree.grandchild else real(pid))
    for pid in (tree.grandchild, tree.pid):
        os.kill(pid, 9)
    tree.proc.wait(timeout=5)
    for _ in range(50):                                     # the grandchild's exit is visible on a pidfd
        try:
            probe = pr._Pinned(tree.grandchild, pr._pidfd_open(tree.grandchild))
        except ProcessLookupError:
            break
        if probe.exited():
            probe.close()
            break
        probe.close()
        await asyncio.sleep(0.02)
    owner = _owner()
    owner._pin_tree(tree.pid, None)
    assert tree.grandchild not in owner.pinned_pids()
    assert owner._unconfirmed_identity is False
    monkeypatch.setattr(pr.PinnedRun, "EXIT_WAIT_S", 0.5)
    assert await owner.finish() is True


async def test_a_child_exiting_between_the_fd_poll_and_the_proc_read_is_extinct_not_unprovable(tree, monkeypatch):
    """Astra A2 diff r4 S2: the child's own pidfd is re-read before any
    `unprovable` verdict — a child that left between the poll and the /proc
    read is extinct, and a dead run is never faulted."""
    monkeypatch.setattr(pr, "_descendants", lambda pid: [tree.grandchild])
    real = pr._ppid

    def ppid(pid):
        if pid == tree.grandchild:
            os.kill(tree.grandchild, 9)                      # it exits exactly here…
            for _ in range(200):
                probe = pr._Pinned(tree.grandchild, pr._pidfd_open(tree.grandchild))
                gone = probe.exited()
                probe.close()
                if gone:
                    break
                time.sleep(0.005)
            return None                                      # …and its /proc entry reads as nothing
        return real(pid)
    monkeypatch.setattr(pr, "_ppid", ppid)
    owner = _owner()
    owner._pin_tree(tree.pid, None)
    assert tree.grandchild not in owner.pinned_pids()
    assert owner._unconfirmed_identity is False


async def test_a_deferred_delete_with_nothing_pinned_waits_for_the_clients_process(monkeypatch):
    """Astra A1b diff r4 S2: when the CLI could not be pinned (pidfd_open failed)
    the pin set is empty, yet the writer may be alive — the deferred cleanup
    establishes its exit through the retained process object before deleting."""
    monkeypatch.setattr(pr.PinnedRun, "LATE_WAIT_S", 5.0)
    owner = _owner()
    proc = _Process(12345)                                   # the transport's process: still running
    client = _FakeClient(12345)
    client.process = proc
    client._transport = types.SimpleNamespace(_process=proc)
    owner._client = client
    owner._entry = "entered"
    owner._unconfirmed_identity = True                       # pinning failed; nothing in the pin set
    assert owner.pinned_pids() == set()
    ran = []

    async def delete():
        ran.append(proc.returncode)
    task = owner.schedule_after_exit(delete)
    await asyncio.sleep(0.2)
    assert ran == [] and not task.done()                     # the writer has not left
    proc.returncode = 0                                      # now it has
    await asyncio.wait_for(task, 5)
    assert ran == [0]


async def test_a_deferred_delete_with_no_process_at_all_runs_at_once():
    owner = _owner()                                         # the entry never produced a process
    ran = []

    async def delete():
        ran.append(True)
    await asyncio.wait_for(owner.schedule_after_exit(delete), 5)
    assert ran == [True]


async def test_a_deferred_delete_never_runs_while_the_writer_is_alive_even_past_the_cap(tree, monkeypatch):
    """Astra A1b diff r5 S2: the waiter's period expiring is NOT exit evidence;
    deleting then would let a later flush recreate the transcript for good.
    The obligation is kept until the exit is confirmed."""
    monkeypatch.setattr(pr.PinnedRun, "LATE_WAIT_S", 0.1)
    owner = _owner()
    owner._pin_tree(tree.pid, None)
    ran = []

    async def delete():
        ran.append(owner.alive())
    task = owner.schedule_after_exit(delete)
    await asyncio.sleep(0.5)                                # several periods past the cap
    assert ran == [] and not task.done()
    for pid in (tree.grandchild, tree.pid):
        os.kill(pid, 9)
    await asyncio.wait_for(task, 5)
    assert ran == [False]                                   # once, after the exit


# --- #1205: a child started AFTER enter (a tool call's shell-out) is pinned at terminate ------

CHILD_ON_DEMAND = r"""
import subprocess, sys, time
g = subprocess.Popen([sys.executable, "-c", "import time\nwhile True: time.sleep(1)"])
print(g.pid, flush=True)
for line in sys.stdin:                      # 'spawn' -> start another grandchild, print its pid
    if line.strip() == "spawn":
        late = subprocess.Popen([sys.executable, "-c", "import time\nwhile True: time.sleep(1)"])
        print(late.pid, flush=True)
while True:
    time.sleep(1)
"""


@pytest.fixture
def tree_on_demand():
    """A fixture tree whose child spawns a further grandchild when told to."""
    proc = subprocess.Popen([sys.executable, "-c", CHILD_ON_DEMAND], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True)
    grandchild = int(proc.stdout.readline().strip())
    spawned: list[int] = []

    def spawn() -> int:
        proc.stdin.write("spawn\n")
        proc.stdin.flush()
        pid = int(proc.stdout.readline().strip())
        spawned.append(pid)
        return pid
    yield types.SimpleNamespace(proc=proc, pid=proc.pid, grandchild=grandchild, spawn=spawn)
    for pid in (*spawned, grandchild, proc.pid):
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        pass


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        fd = pr._pidfd_open(pid)
    except ProcessLookupError:
        return False                                   # gone (or reaped between the two calls)
    try:
        return not pr._Pinned(pid, fd).exited()
    finally:
        os.close(fd)


async def test_a_child_started_after_enter_is_pinned_killed_and_counted_at_terminate(tree_on_demand, monkeypatch):
    owner = _owner()
    await _enter(owner, tree_on_demand)
    assert tree_on_demand.grandchild in owner.pinned_pids()
    late = tree_on_demand.spawn()                       # the plugin server's tool call shells out
    assert late not in owner.pinned_pids()              # not known at enter — the #1205 gap
    monkeypatch.setattr(pr.PinnedRun, "EXIT_WAIT_S", 5.0)
    assert await owner.terminate() is True
    assert late in owner.pinned_pids()                  # re-walked at terminate, while the parents lived
    assert _alive(late) is False                        # killed and confirmed, not reparented to init
    assert owner.alive() is False
    pids = [p.pid for p in owner._all()]
    assert len(pids) == len(set(pids))                  # the re-walk pins each process once


# --- #1205 (Astra, diff round 1): the server EXITS during the cancellation wait --------------

SERVER_ON_DEMAND = r"""
import subprocess, sys, time
late = None
for line in sys.stdin:                      # 'spawn' -> a child; 'die' -> exit at once
    cmd = line.strip()
    if cmd == "spawn":
        late = subprocess.Popen([sys.executable, "-c", "import time\nwhile True: time.sleep(1)"])
        print(late.pid, flush=True)
    elif cmd == "die":
        sys.exit(0)
"""

CLI_WITH_SERVER = r"""
import subprocess, sys
srv = subprocess.Popen([sys.executable, "-c", %r], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                       text=True)
print(srv.pid, flush=True)
for line in sys.stdin:                      # relay 'spawn' / 'die' to the server; survive it
    try:
        srv.stdin.write(line); srv.stdin.flush()
    except (BrokenPipeError, OSError):
        pass
    if line.strip() == "spawn":
        print(srv.stdout.readline().strip(), flush=True)
    elif line.strip() == "die":
        srv.wait()                          # the CLI reaps its server and stays alive
while True:
    import time; time.sleep(1)
""" % SERVER_ON_DEMAND


@pytest.fixture
def tree_with_server():
    """CLI -> server -> (late child on command); the server exits on 'die'."""
    proc = subprocess.Popen([sys.executable, "-c", CLI_WITH_SERVER], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True)
    server = int(proc.stdout.readline().strip())
    spawned: list[int] = []

    def spawn() -> int:
        proc.stdin.write("spawn\n"); proc.stdin.flush()
        pid = int(proc.stdout.readline().strip()); spawned.append(pid); return pid

    def kill_server() -> None:
        proc.stdin.write("die\n"); proc.stdin.flush()
        for _ in range(50):
            if not _alive(server):
                return
            time.sleep(0.05)
    yield types.SimpleNamespace(proc=proc, pid=proc.pid, server=server, spawn=spawn,
                                kill_server=kill_server)
    for pid in (*spawned, server, proc.pid):
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        pass


async def test_a_late_child_whose_server_exits_during_the_cancellation_wait_is_still_pinned_and_killed(tree_with_server, monkeypatch):
    owner = _owner()
    await _enter(owner, tree_with_server)
    assert tree_with_server.server in owner.pinned_pids()
    late = tree_with_server.spawn()                    # started by the server, after enter
    monkeypatch.setattr(pr.PinnedRun, "EXIT_WAIT_S", 5.0)

    async def execution():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            tree_with_server.kill_server()             # the server dies while (a) waits for the task
            raise
    task = asyncio.create_task(execution())
    await asyncio.sleep(0)
    # the walk before the cancel pinned the child, so it is killed with the rest — but the
    # server died under a live CLI without Casa signalling it, so the scan cannot be known
    # complete: the run is UNCONFIRMED (faulted, told), never a silent release (round 2 ruling)
    assert await owner.terminate(task) is False
    assert late in owner.pinned_pids()                 # pinned before the cancel, while its server lived
    assert _alive(late) is False                       # and killed — not left to init


async def test_a_child_started_during_the_cancellation_wait_is_pinned_by_the_walk_before_the_signals(tree_with_server, monkeypatch):
    owner = _owner()
    await _enter(owner, tree_with_server)
    monkeypatch.setattr(pr.PinnedRun, "EXIT_WAIT_S", 5.0)
    born: list[int] = []

    async def execution():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            born.append(tree_with_server.spawn())          # the tool call shells out while (a) waits
            raise
    task = asyncio.create_task(execution())
    await asyncio.sleep(0)
    assert await owner.terminate(task) is True
    assert born and born[0] in owner.pinned_pids()       # the walk just before the signals found it
    assert _alive(born[0]) is False


# --- #1205 (round 2): a scan during which a pinned ancestor dies is INCOMPLETE ---------------

async def test_a_server_that_dies_during_the_walks_enumeration_leaves_the_run_unconfirmed(tree_with_server, monkeypatch):
    """Astra, diff round 2: the server exits while /proc is being enumerated, before the
    late child's parent is read — no walk can find the child; Casa must not confirm."""
    owner = _owner()
    await _enter(owner, tree_with_server)
    late = tree_with_server.spawn()
    monkeypatch.setattr(pr.PinnedRun, "GRACE_S", 0.2)
    monkeypatch.setattr(pr.PinnedRun, "EXIT_WAIT_S", 0.5)
    real_descendants = pr._descendants
    fired = []

    def dying_walk(pid):
        out = real_descendants(pid)
        if not fired:                                  # the server dies mid-enumeration, once
            fired.append(1)
            tree_with_server.kill_server()
            return [p for p in out if p != late]       # …so the child's parent was never read
        return out
    monkeypatch.setattr(pr, "_descendants", dying_walk)
    assert await owner.terminate() is False            # an incomplete scan is not a confirmation
    assert late not in owner.pinned_pids()             # it was indeed never seen
    assert _alive(late)                                # and it survived — hence the fault, told


async def test_a_proven_child_already_dead_at_terminates_entry_before_any_signal_leaves_the_run_unconfirmed(tree_with_server, monkeypatch):
    """Terra, diff round 2: the server exits in the instant before the first walk; Casa
    cannot vouch for that server's subtree and must not confirm."""
    owner = _owner()
    await _enter(owner, tree_with_server)
    late = tree_with_server.spawn()
    tree_with_server.kill_server()                     # dead before terminate() is even called
    monkeypatch.setattr(pr.PinnedRun, "GRACE_S", 0.2)
    monkeypatch.setattr(pr.PinnedRun, "EXIT_WAIT_S", 0.5)
    assert await owner.terminate() is False
    assert _alive(late)


async def test_a_server_that_exits_because_casa_signalled_the_cli_does_not_trip_the_incomplete_scan_flag(tree_with_server, monkeypatch):
    """The coordinator's negative (round 2 ruling): the flag is for deaths the run did not
    cause; servers dying after Casa's SIGTERM to the CLI are the normal kill, confirmed."""
    owner = _owner()
    await _enter(owner, tree_with_server, hang_exit=True)
    tree_with_server.spawn()
    monkeypatch.setattr(pr, "_active_children", lambda: set())
    monkeypatch.setattr(pr.PinnedRun, "EXIT_WAIT_S", 5.0)
    assert await owner.terminate() is True
    assert owner.alive() is False
    owner.close_task.cancel()


async def test_a_server_that_dies_during_the_second_walks_enumeration_leaves_the_run_unconfirmed(tree_with_server, monkeypatch):
    """The walk just before the signals: a death during ITS enumeration is caught by that
    walk's own before/after comparison (nothing runs after it but the signals)."""
    owner = _owner()
    await _enter(owner, tree_with_server)
    late = tree_with_server.spawn()
    monkeypatch.setattr(pr.PinnedRun, "GRACE_S", 0.2)
    monkeypatch.setattr(pr.PinnedRun, "EXIT_WAIT_S", 0.5)
    real_descendants = pr._descendants
    calls = []

    def dying_on_second_walk(pid):
        out = real_descendants(pid)
        calls.append(pid)
        if calls.count(tree_with_server.pid) == 2:        # the CLI's second walk
            tree_with_server.kill_server()
            return [p for p in out if p != late]
        return out
    monkeypatch.setattr(pr, "_descendants", dying_on_second_walk)
    assert await owner.terminate() is False


async def test_a_server_stopped_by_casas_own_sdk_close_does_not_fault_the_terminated_desk(tree_with_server, monkeypatch):
    """Astra, diff round 3: the normal end starts the SDK close, which stops the servers while
    the CLI lingers; the fallback terminate() must not read those deaths as an anomaly."""
    owner = _owner()
    client = await _enter(owner, tree_with_server, hang_exit=True)
    monkeypatch.setattr(pr, "_active_children", lambda: set())
    monkeypatch.setattr(pr.PinnedRun, "EXIT_WAIT_S", 0.3)
    assert await owner.finish() is False                       # the CLI (and server) linger: unconfirmed end
    assert owner.close_task is not None                        # Casa's close has begun
    tree_with_server.kill_server()                             # …and it stops the server, CLI still alive
    monkeypatch.setattr(pr.PinnedRun, "EXIT_WAIT_S", 5.0)
    assert await owner.terminate() is True                     # terminated cleanly, nothing survives
    assert owner.alive() is False
    owner.close_task.cancel()


async def test_a_server_dying_between_two_polls_before_the_walk_is_still_an_incomplete_scan(tree_with_server, monkeypatch):
    """Astra, diff round 3: alive at one poll, dead at the next, before the enumeration — the
    check runs once, after the walk, over every proven pin."""
    owner = _owner()
    await _enter(owner, tree_with_server)
    late = tree_with_server.spawn()
    monkeypatch.setattr(pr.PinnedRun, "GRACE_S", 0.2)
    monkeypatch.setattr(pr.PinnedRun, "EXIT_WAIT_S", 0.5)
    real = pr._Pinned.exited
    seen = []

    def exited(self):
        out = real(self)
        if self.pid == tree_with_server.server and not seen:
            seen.append(1)
            tree_with_server.kill_server()                     # dies right after being polled alive
            # the walk that follows will not find the (now reparented) late child
        return out
    monkeypatch.setattr(pr._Pinned, "exited", exited)
    confirmed = await owner.terminate()
    # either the walk still caught the child (killed) or the scan was judged incomplete
    # (unconfirmed): what must never happen is a confirmed end with the child alive
    assert not (confirmed and _alive(late))
    assert confirmed is False or not _alive(late)


# --- #1205 (round 4): ACCEPTED RESIDUES, not fixed. These cases describe what the declined
# generalisation of "incomplete" would have required (anything in the tree when Casa first acts
# ends accounted for). By the operator's ruling the windows are documented under §14.8 instead;
# the cases are expected failures and may pass by timing ------------------------------------------

@pytest.mark.xfail(strict=False, reason="#1205 round 4: an accepted §14.8 residue by operator ruling (2026-10-03) — a timing window around the termination-time walks, so the case can pass by timing; documented, not fixed")
async def test_terminate_during_a_pending_entry_pins_the_tree_before_the_cancel_can_orphan_it(tree_with_server, monkeypatch):
    """Astra, round 4 (1): the entry still pending at terminate — the first walk must see the
    tree (late_pin first), or the cancel reaps the server and the child escapes."""
    owner = _owner()
    client = _FakeClient(tree_with_server.pid, hang_enter=True)
    enter = asyncio.create_task(owner.enter(None, client_factory=lambda options: client))
    await asyncio.sleep(0.05)
    late = tree_with_server.spawn()
    monkeypatch.setattr(pr.PinnedRun, "EXIT_WAIT_S", 5.0)

    async def execution():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            tree_with_server.kill_server()                     # the cancel takes the server down
            raise
    task = asyncio.create_task(execution())
    await asyncio.sleep(0)
    confirmed = await owner.terminate(task)
    assert not (confirmed and _alive(late))                    # never a confirmed end over a survivor
    assert late in owner.pinned_pids() or confirmed is False
    enter.cancel()
    try:
        await enter
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


@pytest.mark.xfail(strict=False, reason="#1205 round 4: an accepted §14.8 residue by operator ruling (2026-10-03) — a timing window around the termination-time walks, so the case can pass by timing; documented, not fixed")
async def test_a_server_dead_before_casas_close_began_is_not_hidden_by_the_close_exemption(tree_with_server, monkeypatch):
    """Astra, round 4 (2): the audit runs before Casa's first own action — a death that
    preceded finish()'s close is recorded, and the fallback terminate() stays unconfirmed."""
    owner = _owner()
    client = await _enter(owner, tree_with_server, hang_exit=True)
    late = tree_with_server.spawn()
    tree_with_server.kill_server()                             # dies before Casa does anything
    monkeypatch.setattr(pr, "_active_children", lambda: set())
    monkeypatch.setattr(pr.PinnedRun, "EXIT_WAIT_S", 0.3)
    assert await owner.finish() is False
    monkeypatch.setattr(pr.PinnedRun, "GRACE_S", 0.2)
    confirmed = await owner.terminate()
    assert confirmed is False                                  # the pre-close death was recorded
    assert _alive(late)                                        # …which is why: it escaped the walks
    owner.close_task.cancel()


@pytest.mark.xfail(strict=False, reason="#1205 round 4: an accepted §14.8 residue by operator ruling (2026-10-03) — a timing window around the termination-time walks, so the case can pass by timing; documented, not fixed")
async def test_a_listed_descendant_that_vanishes_before_its_pidfd_leaves_a_termination_walk_unconfirmed(tree_with_server, monkeypatch):
    """Terra, round 4: listed by /proc, gone before pidfd_open — exit or reparent, Casa cannot
    tell at termination time, so the scan is incomplete (enter-time handling unchanged)."""
    owner = _owner()
    await _enter(owner, tree_with_server)
    late = tree_with_server.spawn()
    monkeypatch.setattr(pr.PinnedRun, "GRACE_S", 0.2)
    monkeypatch.setattr(pr.PinnedRun, "EXIT_WAIT_S", 0.5)
    real_open = pr._pidfd_open
    hit = []

    def vanishing_open(pid):
        if pid == late and not hit:
            hit.append(1)
            raise ProcessLookupError(pid)                      # "extinct" as far as the walk can see
        return real_open(pid)
    monkeypatch.setattr(pr, "_pidfd_open", vanishing_open)
    assert await owner.terminate() is False
