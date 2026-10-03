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
    def __init__(self, pid, *, hang_exit=False):
        self.process = _Process(pid)
        self._transport = types.SimpleNamespace(_process=self.process)
        self.entered = self.exits = 0
        self.hang_exit = hang_exit

    async def __aenter__(self):
        self.entered += 1
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
