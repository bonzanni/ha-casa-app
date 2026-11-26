"""Run admission (C2, C3): every request is admitted at once, its status card
is posted without a notification, and a request key keeps its one answer."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import make_mocked_request

import operator_calls
import plugin_bindings
import plugin_continuations
import run_admission
import tools as tools_mod
from authz_grants import GrantIdentity
from channels.tg_address import TgAddress
from telegram_channel_double import make_transport
from two_endpoints import CASA_BOT

ARTIFACT = "1" * 64
ORIGIN = {"role": "assistant", "execution_role": "browser", "channel": "telegram",
          "chat_id": "42", "user_id": 100, "cid": "c", "message_type": "channel_in",
          "source": "telegram", "_operator_turn": True, "_delegation_id": "d-1",
          "delegation_depth": 1, "_handoff_chain": ("assistant", "browser"),
          "_tg_bot": CASA_BOT}


def _run(*sites, mode="account", digest="sha256:aa", kind="download invoice"):
    return {"spec_digest": digest, "mode": mode, "task_kind": kind, "task_text": "t",
            "sites": [{"site_id": s.lower(), "access": a, "name": s} for s, a in sites]}


RUNPOD = _run(("RunPod", "account"))
TWO_RUNS = [RUNPOD, _run(("LinkedIn", "account"), digest="sha256:dd")]
COLLEAGUE = ("assistant", "finance", "browser")


class _Channel:
    """Casa's Telegram endpoint (bot 111), as far as run admission uses it."""
    default_agent = "assistant"
    bot_id = CASA_BOT
    has_bot_id = True
    on_identity = None

    def __init__(self):
        self.posted: list[tuple] = []
        self.edits: list[str] = []
        self.cards: list[tuple] = []
        self.card_options: list[dict] = []
        self.card_lands = True
        self.lines: list[tuple] = []

    async def deliver_operator_question(self, chat_id, text, labels, rid, *, post=None, entities=None):
        self.posted.append((chat_id, text, labels, rid))
        return 555

    async def edit_dm_message(self, chat_id, message_id, text, buttons=None, danger=()):
        self.edits.append(text)
        return True

    async def deliver_run_card(self, chat_id, text, *, post=None, entities=None, **options):
        self.cards.append((chat_id, text))
        self.card_options.append(options)
        return 556 if self.card_lands else None

    async def send_reply_line(self, chat_id, text, reply_to, *, post=None, entities=None):
        self.lines.append((chat_id, text, reply_to))
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
    monkeypatch.setattr(tools_mod, "_channel_manager", make_transport(channel), raising=False)
    continued: list = []

    class _Q:
        def request(self, binding, run_ref, reason):
            continued.append((binding.ref, run_ref, reason))
            return "queued"

    monkeypatch.setattr(plugin_continuations, "CONTINUATIONS", _Q())
    return channel, continued


def _call(words="download my latest RunPod invoice", chain=("assistant", "browser"),
          **extra):
    ident = GrantIdentity(operator_id=100, chat_id=42, enforcement_role="browser",
                          artifact_id=ARTIFACT, engagement_id="", bot_id=CASA_BOT,
                          delivery=TgAddress(CASA_BOT, 42))
    origin = {**ORIGIN, "user_text": words or "", "_handoff_chain": chain, **extra}
    if words is None:
        origin["synthetic"] = "continuation"
    else:
        origin["_operator_words"] = words
    return operator_calls.CALLS.open(client_id="c1", tool_use_id="tu", tool_name="x",
                                     role="browser", plugin="browser",
                                     identity=ident, origin=origin)


async def _cards_settle():
    for _ in range(50):
        if not run_admission._CARD_TASKS:
            return
        await asyncio.sleep(0)


#: the requests that once waited for a Start tap, and the one that did not
REQUESTS = [
    pytest.param(dict(words=None), id="no-words-of-his"),
    pytest.param(dict(words="get my LinkedIn invoice and the ChatGPT one"),
                 id="his-words-name-another-site"),
    pytest.param(dict(words=None, chain=COLLEAGUE, _operator_task=True),
                 id="a-colleague-asked-under-his-task"),
]


# -- the run list ---------------------------------------------------------------------------

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
@pytest.mark.parametrize("request_facts", REQUESTS)
async def test_every_request_is_admitted_at_once(wired, request_facts):
    """Whoever asked and whatever his words say, the runs are admitted with no
    question: one admission per run, stored as an admitted request."""
    channel, continued = wired
    call = _call(**request_facts)
    out = await run_admission.request_admission(call, TWO_RUNS, "k1")
    await _cards_settle()
    assert out["status"] == "admitted"
    assert [a["spec_digest"] for a in out["admissions"]] == ["sha256:aa", "sha256:dd"]
    for adm in out["admissions"]:
        assert adm["origin_kind"] == "operator_telegram"
        assert adm["origin_ref"].startswith("adm-")
    assert channel.posted == [] and continued == []
    c = run_admission.CONFIRMATIONS.peek(out["confirmation_id"])
    assert c.status == "admitted" and c.admissions == out["admissions"]
    assert run_admission.confirmation_status(call, c.cid)["status"] == "admitted"
    assert len(channel.cards) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("request_facts", REQUESTS)
async def test_the_status_card_never_notifies(wired, request_facts):
    channel, _continued = wired
    out = await run_admission.request_admission(_call(**request_facts), [RUNPOD], "k1")
    await _cards_settle()
    assert out["status"] == "admitted" and len(channel.cards) == 1
    assert channel.card_options == [{"silent": True}]


@pytest.mark.asyncio
async def test_status_is_readable_only_by_its_plugin_and_operator(wired):
    out = await run_admission.request_admission(_call(), [RUNPOD], "k1")
    await _cards_settle()
    other = operator_calls.OperatorCall(
        client_id="c2", tool_use_id="t", tool_name="x", role="finance", plugin="bank",
        identity=GrantIdentity(operator_id=100, chat_id=42, enforcement_role="finance",
                               artifact_id=ARTIFACT, engagement_id="", bot_id=CASA_BOT),
        operator_words=None, chain=("assistant", "finance"), requester="assistant",
        turn_key="", binding_ref=None, origin={}, opened_at=0.0)
    assert run_admission.confirmation_status(other, out["confirmation_id"])[
        "status"] == "expired"
    assert run_admission.confirmation_status(other, "conf-unknown")["status"] == "expired"
    # the same plugin, another operator: the request is not his
    stranger = operator_calls.OperatorCall(
        client_id="c3", tool_use_id="t", tool_name="x", role="browser", plugin="browser",
        identity=GrantIdentity(operator_id=999, chat_id=999, enforcement_role="browser",
                               artifact_id=ARTIFACT, engagement_id="", bot_id=CASA_BOT),
        operator_words=None, chain=("assistant", "browser"), requester="assistant",
        turn_key="", binding_ref=None, origin={}, opened_at=0.0)
    seen = run_admission.confirmation_status(stranger, out["confirmation_id"])
    assert seen["status"] == "expired" and "admissions" not in seen


@pytest.mark.asyncio
async def test_the_route_validates_then_decides(wired):
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
    await _cards_settle()


# -- his words, kept per spec digest for the action check ---------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("chain,extra", [
    pytest.param(("assistant", "browser"), {}, id="the-resident-asked"),
    pytest.param(COLLEAGUE, {"_operator_task": True}, id="a-colleague-asked-under-his-task"),
])
async def test_words_are_kept_per_admitted_digest(wired, monkeypatch, chain, extra):
    import action_judgement
    monkeypatch.setattr(action_judgement, "WORDS", action_judgement.OperatorWords())
    runs = [RUNPOD, _run(("Bergfreunde", "account"), digest="sha256:bb")]
    out = await run_admission.request_admission(_call(chain=chain, **extra), runs, "k1")
    await _cards_settle()
    assert out["status"] == "admitted"
    for digest in ("sha256:aa", "sha256:bb"):
        assert action_judgement.WORDS.get(plugin="browser", bot_id=CASA_BOT, operator_id=100,
                                          spec_digest=digest) == (
            "download my latest RunPod invoice")
    assert action_judgement.WORDS.get(plugin="bank", bot_id=CASA_BOT, operator_id=100,
                                      spec_digest="sha256:aa") is None
    assert action_judgement.WORDS.get(plugin="browser", bot_id=CASA_BOT, operator_id=999,
                                      spec_digest="sha256:aa") is None


@pytest.mark.asyncio
async def test_no_words_keeps_nothing(wired, monkeypatch):
    import action_judgement
    monkeypatch.setattr(action_judgement, "WORDS", action_judgement.OperatorWords())
    out = await run_admission.request_admission(_call(words=None), [RUNPOD], "k1")
    await _cards_settle()
    assert out["status"] == "admitted"
    assert action_judgement.WORDS.get(plugin="browser", bot_id=CASA_BOT, operator_id=100,
                                      spec_digest="sha256:aa") is None


# -- one answer per request key, kept (P3-R1, P3-R2) ------------------------------------

@pytest.mark.asyncio
async def test_a_replayed_key_gets_the_same_answer_and_never_a_second_card(wired):
    channel, _ = wired
    call = _call(words="collect them")
    first = await run_admission.request_admission(call, [RUNPOD], "batch-1")
    await _cards_settle()
    again = await run_admission.request_admission(call, [RUNPOD], "batch-1")
    await _cards_settle()
    assert again == first and first["request_key"] == "batch-1"
    assert first["status"] == "admitted"
    status = run_admission.confirmation_status(call, first["confirmation_id"])
    assert status["admissions"] == first["admissions"]
    assert run_admission.confirmation_status(call, first["confirmation_id"]) == status
    assert channel.posted == [] and len(channel.cards) == 1


@pytest.mark.asyncio
async def test_answers_survive_a_restart_and_a_dead_card_reads_expired(wired, monkeypatch, tmp_path):
    """An admitted request reads the same after a restart; a request stored pending by an
    earlier Casa (one that still put a Start question) reads ``pending`` as it did, and
    ``expired`` once read by another process."""
    channel, _ = wired
    call = _call()
    admitted = await run_admission.request_admission(call, [RUNPOD], "k-words")
    await _cards_settle()
    old = run_admission.Confirmation(
        cid="conf-" + "e" * 32, plugin="browser", bot_id=CASA_BOT, operator_id=100,
        binding_ref=operator_calls.binding_for(call).ref, request_key="k-card",
        status="pending", admissions=[], created_at=run_admission.CONFIRMATIONS._now())
    run_admission.CONFIRMATIONS.add(old)
    assert run_admission.confirmation_status(call, old.cid)["status"] == "pending"
    assert (await run_admission.request_admission(call, [RUNPOD], "k-card"))[
        "status"] == "confirmation_pending"
    # a new process reads the same file
    monkeypatch.setattr(run_admission, "_PROCESS", "another-process")
    monkeypatch.setattr(run_admission, "CONFIRMATIONS",
                        run_admission.Confirmations(str(tmp_path / "admissions.json")))
    assert await run_admission.request_admission(call, [RUNPOD], "k-words") == admitted
    assert run_admission.confirmation_status(call, admitted["confirmation_id"])[
        "admissions"] == admitted["admissions"]
    assert run_admission.confirmation_status(call, old.cid)["status"] == "expired"
    assert (await run_admission.request_admission(call, [RUNPOD], "k-card"))[
        "status"] == "expired"
    await _cards_settle()
    assert channel.posted == [] and len(channel.cards) == 1   # replays post nothing


def test_answers_are_kept_thirty_days_after_the_answer(tmp_path):
    now = [1_000_000.0]
    store = run_admission.Confirmations(str(tmp_path / "a.json"), now=lambda: now[0])
    c = run_admission.Confirmation(cid="conf-1", plugin="browser", bot_id=CASA_BOT, operator_id=100,
                                   binding_ref="b", request_key="k", status="admitted",
                                   admissions=[{"spec_digest": "d"}], created_at=now[0])
    store.add(c)
    now[0] += run_admission.ANSWER_KEEP_S - 60
    store.add(run_admission.Confirmation(cid="conf-2", plugin="browser", bot_id=CASA_BOT, operator_id=100,
                                         binding_ref="b", request_key="k2", status="declined",
                                         admissions=[], created_at=now[0]))
    assert store.by_key(plugin="browser", bot_id=CASA_BOT, operator_id=100, request_key="k") is not None
    now[0] += 120
    store.add(run_admission.Confirmation(cid="conf-3", plugin="browser", bot_id=CASA_BOT, operator_id=100,
                                         binding_ref="b", request_key="k3", status="declined",
                                         admissions=[], created_at=now[0]))
    assert store.by_key(plugin="browser", bot_id=CASA_BOT, operator_id=100, request_key="k") is None
    import os
    assert oct(os.stat(tmp_path / "a.json").st_mode & 0o777) == "0o600"


@pytest.mark.asyncio
async def test_overlapping_requests_with_one_key_are_decided_once(wired):
    """A retry that overlaps the first request gets its answer: one admission, one card."""
    channel, _ = wired
    call = _call(words="collect them")
    a, b = await asyncio.gather(run_admission.request_admission(call, [RUNPOD], "same"),
                                run_admission.request_admission(call, [RUNPOD], "same"))
    await _cards_settle()
    assert a == b and a["status"] == "admitted"
    assert channel.posted == [] and len(channel.cards) == 1
    assert len(run_admission.CONFIRMATIONS._items) == 1


# -- the status card ------------------------------------------------------------------------

def test_a_run_the_agent_asked_for_itself_names_no_requester():
    """The card speaks as the browsing agent: who asked it, or nobody when the
    run is its own."""
    two = [RUNPOD, _run(("Bergfreunde", "account"), digest="sha256:bb")]
    assert run_admission.compose_status([RUNPOD], None).splitlines()[0] == "On it:"
    assert run_admission.compose_status(two, None).splitlines()[0] == "On it (2 runs):"
    assert run_admission.compose_status(two, "Alex").splitlines()[0] == "On it for Alex (2 runs):"
    assert len(run_admission.compose_status(two, None).splitlines()) == 3   # head + one per run


@pytest.mark.asyncio
async def test_a_status_card_for_the_agents_own_run_names_no_requester(wired):
    channel, _continued = wired
    out = await run_admission.request_admission(_call(chain=("browser",)), [RUNPOD], "k1")
    await _cards_settle()
    assert out["status"] == "admitted"
    (_chat, text), = channel.cards
    assert "On it:\n• RunPod — download invoice" in text


@pytest.mark.asyncio
async def test_a_status_card_carries_no_stop_button(wired):
    channel, _continued = wired
    out = await run_admission.request_admission(_call(), [RUNPOD], "k1")
    await _cards_settle()
    assert out["status"] == "admitted" and channel.posted == []
    (card,) = channel.cards
    cid = out["confirmation_id"]
    # the card is its text alone: no button, nothing to raise one
    assert channel.card_options == [{"silent": True}]
    assert "On it for Ellen:\n• RunPod — download invoice" in card[1]
    assert "Asked for by" not in card[1]
    assert channel.lines == [] and channel.edits == []
    c = run_admission.CONFIRMATIONS.peek(cid)
    assert c.card_address() == TgAddress(CASA_BOT, 42, 556) and c.card_text == card[1]
    assert c.title == "download invoice"            # stored with the request, as before


def test_a_runs_title_is_its_first_task_and_never_a_site():
    run = {"mode": "sites", "task_kind": "download  invoice", "task_text": "",
           "sites": [{"name": "Acme — Billing"}]}
    assert run_admission.key_title([run]) == "download invoice"
    assert run_admission.key_title([run, run, run]) == "download invoice +2"
    assert run_admission.key_title([{**run, "task_kind": "", "task_text": "get the PDF"}]) \
        == "get the PDF"
    assert run_admission.key_title([{**run, "task_kind": ""}]) == "browsing"
    long = run_admission.key_title([{**run, "task_kind": "x" * 60}, run])
    assert long == "x" * 36 + "… +1" and len(long) == run_admission.TITLE_CHARS


@pytest.mark.asyncio
async def test_a_status_card_that_fails_to_post_leaves_the_admission(wired):
    channel, _continued = wired
    channel.card_lands = False
    out = await run_admission.request_admission(_call(), [RUNPOD], "k1")
    await _cards_settle()
    assert out["status"] == "admitted" and len(channel.cards) == 1
    assert run_admission.CONFIRMATIONS.peek(out["confirmation_id"]).card is None
