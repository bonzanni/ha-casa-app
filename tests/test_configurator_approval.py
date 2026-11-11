"""The configurator commits exactly what the approval card showed.

The request tool freezes the effective values on an approval record and posts
the card; Approve (here, what the card's Approve does: ``approve`` then
``configurator_continue.start`` with the card's continuation) starts one
delegation whose task carries the frozen values; its commit stages exactly
those. A newer request replaces an older one; one tap is one attempt.

The approval store, the request tool, the launcher, the lane, the lease and
the governed tools are real; the library is a double that applies the
library's own comparison of the settings it would stage with the approved
ones."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import approval_records
import configurator_continue as cc
import configurator_run
import specialist_install
import tools as tools_mod
from test_approval_claim_under_lease import ARGS, SLUG, _request_args, lib  # noqa: F401
from test_configurator_continue import _settled, _until, env  # noqa: F401
from test_delegate_to_agent import _with_origin

pytestmark = pytest.mark.asyncio

ELLEN = {"role": "assistant", "execution_role": "configurator", "channel": "telegram",
         "chat_id": 42, "cid": "c", "user_text": "install weather with A=1"}


@pytest.fixture
def comparing(lib, monkeypatch):
    """The library double refuses, as the library does, settings that differ
    from the approved ones."""
    def _commit(**kw):
        lib.calls.append(("commit_specialist_install", kw.get("config")))
        specialist_install._require_approved_values(
            kw["approved_values"], config=kw.get("config") or {},
            secret_names_provided=kw.get("secret_names_provided") or ())
        return (SimpleNamespace(state="active", slug=SLUG, last_activation_error=None),
                SimpleNamespace(slug=SLUG, removed_artifact_ids=[], journal_path="",
                                removed_owned_names=()))
    monkeypatch.setattr(specialist_install, "commit_specialist_install", _commit)
    return lib


async def _ask(**over):
    """The request tool, asked from an ordinary run (no approval of its own)."""
    out = await _with_origin(tools_mod.specialist_install_request_approval.handler(
        {**_request_args(), **over}), dict(ELLEN))
    return json.loads(out["content"][0]["text"])


def _tap_approve(lib, record, on_outcome=None):
    """What the card's Approve does: approve, then start the continuation."""
    r = approval_records.approve(record.rid)
    if r is None:
        return "replaced"
    return cc.start(lib.continuations[record.rid], key=r.rid, approval=r, asked=r.asked,
                    on_outcome=on_outcome)


@pytest.fixture
def cards(lib, monkeypatch):
    import specialist_install_consent
    lib.continuations = {}

    def _card(**kw):
        lib.cards.append(kw["record"])
        lib.continuations[kw["record"].rid] = kw["continuation"]
        return SimpleNamespace(refused=None)
    monkeypatch.setattr(specialist_install_consent, "prompt_specialist_install_consent", _card,
                        raising=False)
    return lib


def _committing_run(env, config_from_task=True, override=None):
    """The configurator: commits with the values its task names."""
    results: list = []
    real = env.runs.__call__

    async def _run(cfg, task_text, context_text, **kw):
        await real(cfg, task_text, context_text, **kw)
        frozen = json.loads(task_text.split(cc.APPROVED_VALUES_LINE, 1)[1])
        args = {**ARGS, "config": override if override is not None else frozen["config"],
                "secret_names_provided": frozen["secrets"]}
        payload = await tools_mod.specialist_install_commit.handler(args)
        results.append(json.loads(payload["content"][0]["text"]))
        return tools_mod.DelegatedOutput(text="done")
    tools_mod._run_delegated_agent = _run
    return results


# -- what the card showed is what is committed ----------------------------------------

@pytest.mark.parametrize("override, ok", [(None, True), ({"A": "2"}, False)],
                         ids=["the-frozen-values", "other-values"])
async def test_approve_commits_exactly_the_frozen_values(env, comparing, cards, override, ok):
    assert (await _ask(config={"A": "1"}))["approval"] == "requested"
    (record,) = cards.cards
    assert record.values == {"config": {"A": "1"}, "secrets": []}
    results = _committing_run(env, override=override)
    try:
        assert _tap_approve(cards, record) == "started"
        await _settled(env)
    finally:
        tools_mod._run_delegated_agent = env.runs
    (payload,) = results
    if ok:
        assert payload["ok"] is True
        assert [c for c in comparing.calls if c[0] == "commit_specialist_install"] == [
            ("commit_specialist_install", {"A": "1"})]
    else:
        assert payload["kind"] == "approval_values_differ" and "(A)" in payload["detail"]


# -- a newer request replaces the older one ------------------------------------------

async def test_a_tap_on_a_replaced_card_starts_nothing(env, cards):
    await _ask(config={"A": "1"})
    await _ask(config={"A": "2"})
    first, second = cards.cards
    assert first.rid != second.rid
    assert approval_records.get(first.rid).state == "superseded"
    assert _tap_approve(cards, first) == "replaced"
    await asyncio.sleep(0.05)
    assert env.runs.seen == [] and env.bus.sent == []
    # after an approval, other values are a new request with its own card
    assert _tap_approve(cards, second) == "started"
    await _settled(env)
    assert (await _ask(config={"A": "3"}))["approval"] == "requested"
    assert len(cards.cards) == 3


async def test_a_newer_request_before_the_approved_run_starts_stops_it(env, cards):
    await _ask(config={"A": "1"})
    (a,) = cards.cards
    held = await configurator_run.acquire(run_key="other", via="delegation", task="t")
    assert _tap_approve(cards, a) == "started"
    await asyncio.sleep(0.05)
    await _ask(config={"A": "2"})                            # B, while A waits
    held.release()
    await _settled(env)
    assert env.runs.seen == []
    (msg,) = env.bus.sent                                    # Ellen told once
    assert msg.content.kind == "approval_superseded"
    b = cards.cards[1]
    assert approval_records.approve(b.rid) is not None       # B's card is answerable


async def test_a_newer_request_inside_the_approved_run_before_its_attempt_writes_nothing(
        env, comparing, cards):
    await _ask(config={"A": "1"})
    (a,) = cards.cards
    results: list = []
    real = env.runs.__call__

    async def _run(cfg, task_text, context_text, **kw):
        await real(cfg, task_text, context_text, **kw)
        await tools_mod.specialist_install_request_approval.handler(
            {**_request_args(), "config": {"A": "2"}})
        payload = await tools_mod.specialist_install_commit.handler(dict(ARGS))
        results.append(json.loads(payload["content"][0]["text"]))
        return tools_mod.DelegatedOutput(text="done")
    tools_mod._run_delegated_agent = _run
    try:
        _tap_approve(cards, a)
        await _settled(env)
    finally:
        tools_mod._run_delegated_agent = env.runs
    assert results[0]["kind"] == "approval_superseded"
    assert comparing.calls == []


# -- one tap starts one run, and is one attempt ---------------------------------------

async def test_approve_starts_exactly_one_delegation(env, cards):
    await _ask()
    (record,) = cards.cards
    assert _tap_approve(cards, record) == "started"
    await _settled(env)
    await asyncio.sleep(0.05)
    assert len(env.runs.seen) == 1 and len(env.reg.job_registry.all()) == 1


async def test_a_start_that_failed_runs_on_asking_again_without_a_new_card(
        env, cards, monkeypatch):
    await _ask()
    (record,) = cards.cards
    real = env.reg.register_delegation
    calls = {"n": 0}

    async def _once(rec):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk full")
        await real(rec)
    monkeypatch.setattr(env.reg, "register_delegation", _once)
    told: list = []

    async def _on(outcome):
        told.append(outcome)
    _tap_approve(cards, record, on_outcome=_on)
    await _until(lambda: told)
    assert told == ["failed"] and env.runs.seen == []
    assert (await _ask())["approval"] == "already_approved"
    await _settled(env)
    (run,) = env.runs.seen
    assert run.task.endswith(cc.values_line(record.values))
    assert len(cards.cards) == 1


async def test_busy_past_the_deadline_tells_once_and_asking_again_runs_it(
        env, cards, monkeypatch):
    monkeypatch.setattr(configurator_run, "LEASE_WAIT_S", 0.1)
    await _ask()
    (record,) = cards.cards
    permit = tools_mod._specialist_limiter.try_acquire("42:configurator")
    told: list = []

    async def _on(outcome):
        told.append(outcome)
    _tap_approve(cards, record, on_outcome=_on)
    await _until(lambda: told)
    await asyncio.sleep(0.05)
    assert told == ["busy"]
    assert approval_records.get(record.rid).state == "approved"
    permit.release()
    assert (await _ask())["approval"] == "already_approved"
    await _settled(env)
    assert len(env.runs.seen) == 1 and len(cards.cards) == 1


async def test_an_attempt_that_failed_means_a_new_card(env, comparing, cards):
    await _ask()
    (record,) = cards.cards
    comparing.verify_fails = True
    results = _committing_run(env)
    try:
        _tap_approve(cards, record)
        await _settled(env)
    finally:
        tools_mod._run_delegated_agent = env.runs
    assert results[0]["ok"] is False
    assert approval_records.get(record.rid).state == "used"
    assert (await _ask())["approval"] == "requested"
    assert len(cards.cards) == 2
