"""Browser executor §5.5 (Plan 3 C2, C3): run admission — the clean-context
reading of the operator's own words, the mechanical comparison, the one-tap
batch confirmation and its status."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import make_mocked_request

import operator_calls
import plugin_bindings
import plugin_continuations
import result_broker as rb
import run_admission
import tools as tools_mod
from authz_grants import GrantIdentity
from verdict_broker import BROKER

ARTIFACT = "1" * 64
ORIGIN = {"role": "assistant", "execution_role": "browser", "channel": "telegram",
          "chat_id": "42", "user_id": 100, "cid": "c", "message_type": "channel_in",
          "source": "telegram", "_operator_turn": True, "_delegation_id": "d-1",
          "delegation_depth": 1, "_handoff_chain": ("assistant", "browser")}


def _run(*sites, mode="account", digest="sha256:aa", kind="download invoice"):
    return {"spec_digest": digest, "mode": mode, "task_kind": kind, "task_text": "t",
            "sites": [{"site_id": s.lower(), "access": a, "name": s} for s, a in sites]}


RUNPOD = _run(("RunPod", "account"))


class _Channel:
    def __init__(self, deliver=True):
        self.deliver = deliver
        self.posted: list[tuple] = []
        self.edits: list[tuple] = []

    async def deliver_operator_question(self, chat_id, text, labels, rid):
        self.posted.append((chat_id, text, labels, rid))
        return 555 if self.deliver else None

    async def edit_dm_message(self, chat_id, message_id, text):
        self.edits.append(text)
        return True


@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setattr(operator_calls, "CALLS", operator_calls.OperatorCalls())
    monkeypatch.setattr(plugin_bindings, "BINDINGS",
                        plugin_bindings.BindingStore(str(tmp_path / "b.json")))
    monkeypatch.setattr(run_admission, "CONFIRMATIONS",
                        run_admission.Confirmations(str(tmp_path / "admissions.json")))
    monkeypatch.setattr(tools_mod, "_agent_role_map", {
        "assistant": SimpleNamespace(character=SimpleNamespace(name="Ellen"), kind="resident"),
        "finance": SimpleNamespace(character=SimpleNamespace(name="Alex"), kind="specialist"),
        "browser": SimpleNamespace(character=SimpleNamespace(name="Fido"), kind="specialist"),
    }, raising=False)
    channel = _Channel()
    monkeypatch.setattr(rb, "_telegram_channel", lambda: channel)
    continued: list = []

    class _Q:
        def request(self, binding, run_ref, reason):
            continued.append((binding.ref, run_ref, reason))
            return "queued"

    monkeypatch.setattr(plugin_continuations, "CONTINUATIONS", _Q())
    return channel, continued


def _call(words="download my latest RunPod invoice", chain=("assistant", "browser")):
    ident = GrantIdentity(operator_id=100, chat_id=42, enforcement_role="browser",
                          artifact_id=ARTIFACT, engagement_id="")
    origin = {**ORIGIN, "user_text": words or "", "_handoff_chain": chain}
    if words is None:
        origin["synthetic"] = "continuation"
    else:
        origin["_operator_words"] = words
    return operator_calls.CALLS.open(client_id="c1", tool_use_id="tu", tool_name="x",
                                     role="browser", plugin="browser",
                                     identity=ident, origin=origin)


def _model(reply):
    seen: list[str] = []

    async def _ask(words):
        seen.append(words)
        if isinstance(reply, Exception):
            raise reply
        return reply
    return _ask, seen


async def _tap(channel, index, actor=100):
    for _ in range(200):
        if channel.posted:
            break
        await asyncio.sleep(0)
    rid = channel.posted[-1][3]
    claim = BROKER.claim(namespace="resident_ask", scope="ask:42", request_id=rid,
                         option_index=index, actor_id=actor)
    assert not isinstance(claim, str), claim
    assert BROKER.commit(claim)
    for _ in range(200):
        await asyncio.sleep(0)


# -- reading and comparing ---------------------------------------------------------------

@pytest.mark.parametrize("reply,expected", [
    ('{"runs": [{"site": "runpod"}]}', ["runpod"]),
    ('Sure! {"runs": []}', []),
    ("I think they want RunPod", []),
    ('{"runs": [{"site": 7}]}', []),
    ('{"runs": "all"}', []),
])
def test_the_reading_is_parsed_strictly(reply, expected):
    assert run_admission.parse_extraction(reply) == expected


def test_comparison_is_mechanical():
    assert run_admission.run_matches(RUNPOD, ["Run Pod"])
    assert run_admission.run_matches(RUNPOD, ["runpod"])
    assert run_admission.run_matches(_run(("UniFi", "account")), ["UniFi"])
    # the same name, nothing less (review p3r2b)
    assert not run_admission.run_matches(RUNPOD, ["runpod.io"])
    assert not run_admission.run_matches(_run(("UniFi", "account")), ["my UniFi gateway"])
    assert not run_admission.run_matches(_run(("Chase", "account")), ["Chase Center"])
    assert not run_admission.run_matches(RUNPOD, ["LinkedIn"])
    assert not run_admission.run_matches(_run(("Fi", "account")), ["Fi"])      # under 3 chars
    # whole words only, and the whole label (review p3r1b)
    assert not run_admission.run_matches(_run(("ING", "account")), ["Booking.com"])
    assert not run_admission.run_matches(_run(("GitHub Enterprise", "account")), ["GitHub"])
    assert run_admission.run_matches(_run(("GitHub Enterprise", "account")),
                                     ["GitHub Enterprise"])
    # an account run with a public grant: only the account site must be named
    deck = _run(("Moxfield", "account"), ("Scryfall", "public"))
    assert run_admission.run_matches(deck, ["Moxfield"])
    assert not run_admission.run_matches(deck, ["Scryfall"])
    # an open-web run is named by any browsing his words ask for
    web = _run(mode="open_web")
    assert run_admission.run_matches(web, [""]) and run_admission.run_matches(web, ["PubMed"])
    assert not run_admission.run_matches(web, [])


@pytest.mark.parametrize("runs,err", [
    ([], "bad_runs"), ("x", "bad_runs"), ([{**RUNPOD, "extra": 1}], "bad_runs"),
    ([{**RUNPOD, "mode": "commit"}], "bad_mode"), ([{**RUNPOD, "spec_digest": ""}], "bad_digest"),
    ([{**RUNPOD, "sites": [{"site_id": "r", "access": "account"}]}], "bad_sites"),
    ([{**RUNPOD, "sites": [{"site_id": "r", "access": "public", "name": "R"}]}], "bad_sites"),
    ([{**RUNPOD, "mode": "open_web"}], "bad_sites"),          # an account site on the open web
    ([RUNPOD] * 9, "bad_runs"),
])
def test_run_list_shape(runs, err):
    assert run_admission.validate_runs(runs)[1] == err


# -- deciding ------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_operators_own_words_admit_with_no_prompt(wired, monkeypatch):
    channel, continued = wired
    ask, seen = _model('{"runs": [{"site": "RunPod"}]}')
    monkeypatch.setattr(run_admission, "_ask_model", ask)
    out = await run_admission.request_admission(_call(), [RUNPOD], "k1")
    assert out["status"] == "admitted"
    (adm,) = out["admissions"]
    assert adm["spec_digest"] == "sha256:aa" and adm["origin_kind"] == "operator_telegram"
    assert adm["origin_ref"].startswith("adm-")
    assert channel.posted == [] and continued == []
    assert seen == ["download my latest RunPod invoice"]      # nothing but his words


@pytest.mark.asyncio
async def test_a_context_reply_gets_one_card_for_the_whole_batch(wired, monkeypatch):
    channel, continued = wired
    ask, _ = _model('{"runs": []}')
    monkeypatch.setattr(run_admission, "_ask_model", ask)
    runs = [RUNPOD, _run(("Bergfreunde", "account"), digest="sha256:bb"),
            _run(("ChatGPT", "account"), digest="sha256:cc")]
    call = _call(words="collect them", chain=("assistant", "finance", "browser"))
    out = await run_admission.request_admission(call, runs, "k1")
    assert out["status"] == "confirmation_pending"
    cid = out["confirmation_id"]
    assert len(channel.posted) == 1
    _chat, text, labels, _ = channel.posted[0]
    assert labels == ["Start all", "Not now"]
    assert "Start these 3 browsing runs?" in text and "• Bergfreunde — download invoice" in text
    assert "Asked for by Alex." in text
    assert run_admission.confirmation_status(call, cid)["status"] == "pending"
    await _tap(channel, 0)
    status = run_admission.confirmation_status(call, cid)
    assert status["status"] == "admitted"
    assert [a["spec_digest"] for a in status["admissions"]] == [
        "sha256:aa", "sha256:bb", "sha256:cc"]
    binding = operator_calls.binding_for(call)
    assert continued == [(binding.ref, f"admission:{cid}", "operator_answer")]


@pytest.mark.asyncio
async def test_not_now_declines_and_still_continues_the_run(wired, monkeypatch):
    channel, continued = wired
    ask, _ = _model('{"runs": []}')
    monkeypatch.setattr(run_admission, "_ask_model", ask)
    call = _call(words="yes")
    out = await run_admission.request_admission(call, [RUNPOD], "k1")
    await _tap(channel, 1)
    assert run_admission.confirmation_status(call, out["confirmation_id"])[
        "status"] == "declined"
    assert len(continued) == 1


@pytest.mark.asyncio
async def test_a_turn_without_his_words_goes_to_the_card_without_a_reading(wired, monkeypatch):
    channel, _ = wired
    ask, seen = _model(AssertionError("no reading without his words"))
    monkeypatch.setattr(run_admission, "_ask_model", ask)
    out = await run_admission.request_admission(_call(words=None), [RUNPOD], "k1")
    assert out["status"] == "confirmation_pending" and seen == []


@pytest.mark.asyncio
async def test_one_run_his_words_do_not_name_puts_the_batch_to_the_card(wired, monkeypatch):
    channel, _ = wired
    ask, _ = _model('{"runs": [{"site": "RunPod"}]}')
    monkeypatch.setattr(run_admission, "_ask_model", ask)
    out = await run_admission.request_admission(
        _call(), [RUNPOD, _run(("LinkedIn", "account"), digest="sha256:dd")], "k1")
    assert out["status"] == "confirmation_pending"
    assert "LinkedIn" in channel.posted[0][1] and "RunPod" in channel.posted[0][1]


@pytest.mark.asyncio
async def test_a_failing_reading_falls_back_to_the_card(wired, monkeypatch):
    ask, _ = _model(RuntimeError("cli gone"))
    monkeypatch.setattr(run_admission, "_ask_model", ask)
    out = await run_admission.request_admission(_call(), [RUNPOD], "k1")
    assert out["status"] == "confirmation_pending"


@pytest.mark.asyncio
async def test_a_card_that_cannot_be_posted_is_refused(wired, monkeypatch):
    channel, continued = wired
    channel.deliver = False
    ask, _ = _model('{"runs": []}')
    monkeypatch.setattr(run_admission, "_ask_model", ask)
    out = await run_admission.request_admission(_call(), [RUNPOD], "k1")
    assert out["status"] == "refused" and out["reason"] == "not_delivered"
    # a replay of the key is the same answer, never a second card
    assert await run_admission.request_admission(_call(), [RUNPOD], "k1") == out
    assert len(channel.posted) == 1
    assert continued == []


@pytest.mark.asyncio
async def test_an_unanswered_card_expires_and_continues(wired, monkeypatch):
    channel, continued = wired
    monkeypatch.setattr(run_admission, "CONFIRM_TIMEOUT_S", 0.05)
    ask, _ = _model('{"runs": []}')
    monkeypatch.setattr(run_admission, "_ask_model", ask)
    call = _call()
    out = await run_admission.request_admission(call, [RUNPOD], "k1")
    for _ in range(100):
        await asyncio.sleep(0.01)
        if continued:
            break
    assert run_admission.confirmation_status(call, out["confirmation_id"])[
        "status"] == "expired"
    assert len(continued) == 1


@pytest.mark.asyncio
async def test_status_is_readable_only_by_its_plugin_and_operator(wired, monkeypatch):
    channel, _ = wired
    ask, _ = _model('{"runs": []}')
    monkeypatch.setattr(run_admission, "_ask_model", ask)
    out = await run_admission.request_admission(_call(), [RUNPOD], "k1")
    other = operator_calls.OperatorCall(
        client_id="c2", tool_use_id="t", tool_name="x", role="finance", plugin="bank",
        identity=GrantIdentity(operator_id=100, chat_id=42, enforcement_role="finance",
                               artifact_id=ARTIFACT, engagement_id=""),
        operator_words=None, chain=("assistant", "finance"), requester="assistant",
        turn_key="", binding_ref=None, origin={}, opened_at=0.0)
    assert run_admission.confirmation_status(other, out["confirmation_id"])[
        "status"] == "expired"
    assert run_admission.confirmation_status(other, "conf-unknown")["status"] == "expired"


@pytest.mark.asyncio
async def test_the_route_validates_then_decides(wired, monkeypatch):
    ask, _ = _model('{"runs": [{"site": "RunPod"}]}')
    monkeypatch.setattr(run_admission, "_ask_model", ask)
    _call()
    handler = operator_calls.build_run_admission_handler()

    async def _post(body):
        req = make_mocked_request("POST", "/x")

        async def _json():
            return body
        req.json = _json
        return json.loads((await handler(req)).body)

    assert await _post({"client": "c1", "request_key": "k", "runs": [{"spec_digest": "x"}]}) == {
        "error": "bad_mode"}
    assert await _post({"client": "c1", "runs": [RUNPOD]}) == {"error": "bad_request_key"}
    assert (await _post({"client": "c1", "request_key": "k", "runs": [RUNPOD]}))[
        "status"] == "admitted"


# -- one answer per request key, kept (P3-R1, P3-R2) ------------------------------------

@pytest.mark.asyncio
async def test_a_replayed_key_gets_the_same_answer_and_never_a_second_card(wired, monkeypatch):
    channel, _ = wired
    ask, seen = _model('{"runs": []}')
    monkeypatch.setattr(run_admission, "_ask_model", ask)
    call = _call(words="collect them")
    first = await run_admission.request_admission(call, [RUNPOD], "batch-1")
    again = await run_admission.request_admission(call, [RUNPOD], "batch-1")
    assert again == first and first["request_key"] == "batch-1"
    assert len(channel.posted) == 1 and len(seen) == 1
    await _tap(channel, 0)
    answered = await run_admission.request_admission(call, [RUNPOD], "batch-1")
    status = run_admission.confirmation_status(call, first["confirmation_id"])
    assert answered["status"] == status["status"] == "admitted"
    assert answered["admissions"] == status["admissions"]
    assert run_admission.confirmation_status(call, first["confirmation_id"]) == status
    assert len(channel.posted) == 1


@pytest.mark.asyncio
async def test_answers_survive_a_restart_and_a_dead_card_reads_expired(wired, monkeypatch, tmp_path):
    channel, _ = wired
    ask, _ = _model('{"runs": [{"site": "RunPod"}]}')
    monkeypatch.setattr(run_admission, "_ask_model", ask)
    words_call = _call()
    admitted = await run_admission.request_admission(words_call, [RUNPOD], "k-words")
    ask2, _ = _model('{"runs": []}')
    monkeypatch.setattr(run_admission, "_ask_model", ask2)
    pending = await run_admission.request_admission(_call(words="yes"), [RUNPOD], "k-card")
    assert pending["status"] == "confirmation_pending"
    # a new process reads the same file
    monkeypatch.setattr(run_admission, "_PROCESS", "another-process")
    monkeypatch.setattr(run_admission, "CONFIRMATIONS",
                        run_admission.Confirmations(str(tmp_path / "admissions.json")))
    assert await run_admission.request_admission(words_call, [RUNPOD], "k-words") == admitted
    assert run_admission.confirmation_status(words_call, admitted["confirmation_id"])[
        "admissions"] == admitted["admissions"]
    assert run_admission.confirmation_status(words_call, pending["confirmation_id"])[
        "status"] == "expired"
    assert len(channel.posted) == 1                        # replays post nothing


def test_answers_are_kept_thirty_days_after_the_answer(tmp_path):
    now = [1_000_000.0]
    store = run_admission.Confirmations(str(tmp_path / "a.json"), now=lambda: now[0])
    c = run_admission.Confirmation(cid="conf-1", plugin="browser", operator_id=100,
                                   binding_ref="b", request_key="k", status="admitted",
                                   admissions=[{"spec_digest": "d"}], created_at=now[0])
    store.add(c)
    now[0] += run_admission.ANSWER_KEEP_S - 60
    store.add(run_admission.Confirmation(cid="conf-2", plugin="browser", operator_id=100,
                                         binding_ref="b", request_key="k2", status="declined",
                                         admissions=[], created_at=now[0]))
    assert store.by_key(plugin="browser", operator_id=100, request_key="k") is not None
    now[0] += 120
    store.add(run_admission.Confirmation(cid="conf-3", plugin="browser", operator_id=100,
                                         binding_ref="b", request_key="k3", status="declined",
                                         admissions=[], created_at=now[0]))
    assert store.by_key(plugin="browser", operator_id=100, request_key="k") is None
    import os
    assert oct(os.stat(tmp_path / "a.json").st_mode & 0o777) == "0o600"


@pytest.mark.asyncio
async def test_overlapping_requests_with_one_key_are_decided_once(wired, monkeypatch):
    """p3r2b/x: a retry that overlaps the first request waits for it and gets
    its answer — one reading, one card, one confirmation."""
    channel, _ = wired
    release = asyncio.Event()
    readings: list[str] = []

    async def _slow(words):
        readings.append(words)
        await release.wait()
        return '{"runs": []}'

    monkeypatch.setattr(run_admission, "_ask_model", _slow)
    call = _call(words="collect them")
    first = asyncio.create_task(run_admission.request_admission(call, [RUNPOD], "same"))
    second = asyncio.create_task(run_admission.request_admission(call, [RUNPOD], "same"))
    for _ in range(20):
        await asyncio.sleep(0)
    release.set()
    a, b = await asyncio.gather(first, second)
    assert a == b and a["status"] == "confirmation_pending"
    assert len(readings) == 1 and len(channel.posted) == 1
