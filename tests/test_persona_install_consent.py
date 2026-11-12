"""The persona install card: it asks about one approval record, binds the
record's consent identity, and on Approve approves the record and has Casa
carry the configurator on. A newer request replaces it.

``configurator_continue.start`` is replaced by a recorder here; the real
launcher is exercised with these cards where both are built.
"""
import asyncio
from types import SimpleNamespace

import pytest

import approval_records as ar
import configurator_continue
import tools
from approval_records import values_digest
from authz_grants import ChallengeCoordinator
from channels.tg_address import TgAddress
from persona_install import PersonaInstallAckStore, persona_install_consent_identity
from persona_install_consent import prompt_persona_install_consent, supersede_card

KEY = "judge"
SCOPE = "authz:111:100"
CONTINUATION = "The operator approved installing persona judge. Call persona_install_commit."


def _inspection() -> SimpleNamespace:
    return SimpleNamespace(
        persona_id="judge", version="1.0.0",
        checksum="sha256:" + "a" * 64, display_name="MTG Judge",
    )


def _identity(inspection=None) -> str:
    i = inspection or _inspection()
    return persona_install_consent_identity(
        persona_id=i.persona_id, version=i.version, checksum=i.checksum,
        values_digest=values_digest({}))


class _Channel:
    def __init__(self):
        self.bot_id = 111
        self.posts = []
        self.edits = []

    async def post_dm_keyboard(self, *, chat_id, request_id, text, options,
                               entities=None):
        self.posts.append((chat_id, request_id, text, tuple(options)))
        return 55

    async def edit_dm_message(self, chat_id, message_id, text, literal=False):
        self.edits.append((chat_id, message_id, text))
        return True


@pytest.fixture
def env(tmp_path, monkeypatch):
    import verdict_broker
    monkeypatch.setattr(ar, "PATH", str(tmp_path / "approval-records.json"))
    monkeypatch.setattr(tools, "_PLUGIN_TOOLS_LOCK", asyncio.Lock())
    ar.reset()
    broker = verdict_broker.VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", broker)
    starts = []

    def _start(continuation, *, key, approval=None, asked="", on_outcome=None):
        starts.append(SimpleNamespace(continuation=continuation, key=key, approval=approval,
                                      asked=asked, on_outcome=on_outcome))
        return "started"

    monkeypatch.setattr(configurator_continue, "start", _start)
    yield SimpleNamespace(broker=broker, coord=ChallengeCoordinator(), channel=_Channel(),
                          acks=PersonaInstallAckStore(tmp_path / "persona-acks.json"),
                          starts=starts)
    ar.reset()


async def _request(identity=None):
    async with tools._plugin_tools_guard():
        return ar.request("persona_install", KEY, identity or _identity(), {},
                          asked="install the judge persona")


def _prompt(env, record):
    return prompt_persona_install_consent(
        coordinator=env.coord, channel=env.channel, chat_id=100, operator_id=100,
        record=record, inspection=_inspection(), continuation=CONTINUATION, acks=env.acks)


def _tap(env, idx):
    claim = env.broker.claim(namespace="resident_ask", scope=SCOPE,
                             request_id=env.channel.posts[0][1], option_index=idx,
                             actor_id=100)
    if isinstance(claim, str):
        return claim
    assert env.broker.commit(claim) is True
    claim.req.meta["on_commit_sync"](idx)
    return None


async def _settle(n: int = 10):
    for _ in range(n):
        await asyncio.sleep(0)


async def test_approve_acks_approves_and_carries_the_configurator_on(env):
    record, _, _ = await _request()
    await _prompt(env, record).settled_post()
    assert ar.get(record.rid).card == TgAddress(111, 100, 55)
    assert _tap(env, 0) is None
    await _settle()
    assert env.acks.is_acked(record.consent_identity) is True
    assert ar.get(record.rid).state == "approved"
    [s] = env.starts
    assert (s.continuation, s.key, s.approval.state, s.asked) == (
        CONTINUATION, record.rid, "approved", "install the judge persona")
    await s.on_outcome("started")
    assert env.channel.edits[-1][2] == "✅ Approved — the Configurator is carrying on with 'judge'."
    await s.on_outcome("busy")
    assert env.channel.edits[-1][2] == (
        "⚠️ Approved and saved — but the configurator could not be started. "
        "Ask Ellen again; the approval is kept.")


async def test_a_record_for_another_persona_version_is_refused(env):
    other = SimpleNamespace(persona_id="judge", version="2.0.0",
                            checksum="sha256:" + "b" * 64, display_name="MTG Judge")
    record, _, _ = await _request(identity=_identity(other))
    with pytest.raises(ValueError):
        _prompt(env, record)


async def test_approve_on_a_replaced_request_starts_nothing(env):
    record, _, _ = await _request()
    await _prompt(env, record).settled_post()
    async with tools._plugin_tools_guard():
        ar.request("persona_install", KEY, "another-identity", {"x": 1})
    _tap(env, 0)
    await _settle()
    assert env.starts == []
    assert env.channel.edits[-1][2] == "Replaced by a newer request; nothing was installed."


async def test_deny_denies_the_record(env):
    record, _, _ = await _request()
    await _prompt(env, record).settled_post()
    _tap(env, 1)
    await _settle()
    assert ar.get(record.rid).state == "denied"
    assert env.channel.edits[-1][2] == "❌ Denied — 'judge' was not installed"


async def test_a_card_replaced_before_it_was_posted_is_edited_replaced(env):
    a, _, _ = await _request()
    async with tools._plugin_tools_guard():
        ar.request("persona_install", KEY, "another-identity", {"x": 1})
    await _prompt(env, a).settled_post()
    await _settle()
    assert env.channel.edits == [(100, 55, "Replaced by a newer request; nothing was installed.")]
    assert env.broker.pending(namespace="resident_ask", scope=SCOPE) == []
    assert _tap(env, 0) == "stale"
    assert env.starts == []


async def test_supersede_card_replaces_an_open_persona_card(env):
    a, _, _ = await _request()
    await _prompt(env, a).settled_post()
    async with tools._plugin_tools_guard():
        _b, _, prev = ar.request("persona_install", KEY, "another-identity", {"x": 1})
    await supersede_card(prev, channel=env.channel)
    await _settle()
    assert env.channel.edits == [(100, 55, "Replaced by a newer request; nothing was installed.")]
    assert ar.get(a.rid).state == "superseded"


@pytest.mark.parametrize("reason", [
    "challenge_cancelled", "unrecognised_red_case_reason",
])
async def test_933_a_withdrawn_consent_does_not_claim_it_expired(env, reason) -> None:
    """#933 — the authz half of the family, in the module the issue named.

    `ChallengeCoordinator.cancel_matching` retires a persona consent with
    `challenge_cancelled` when the persona is removed or its ack revoked. The
    hook sees that reason and today throws it away, so an operator who has just
    removed a persona is told the request timed out instead.

    The authz half composes a headline rather than a body suffix, so it gets
    its own unknown-reason row: the fallback has to be truthful in BOTH
    composers, not only in the one the scheduled pin reaches.
    """
    class _Coordinator:
        def register_challenge(self, key, **kwargs):
            self.on_commit_sync = kwargs["on_commit_sync"]
            self.finish_factory = kwargs["finish_factory"]
            return SimpleNamespace(created=True)

    class _Acks:
        def __init__(self):
            self.records = []

        def revocation_generations(self, *, persona_id, version):
            return (0, 0)

        def record(self, **kwargs):
            self.records.append(kwargs)
            return True

    coordinator = _Coordinator()
    channel = _Channel()
    record, _, _ = await _request()
    prompt_persona_install_consent(
        coordinator=coordinator, channel=channel, chat_id=701, operator_id=701,
        record=record, inspection=_inspection(), continuation=CONTINUATION, acks=_Acks(),
    )
    finish = coordinator.finish_factory(88, SimpleNamespace(meta={}))
    await finish({"outcome": "cancelled", "reason": reason})

    assert len(channel.edits) == 1
    text = channel.edits[0][2]
    lowered = text.casefold()
    assert sum("expired" in t for t in [lowered]) == 0
    # Still says which consent it was, and still says nothing was installed.
    assert sum("'judge'" in t for t in [text]) == 1
    assert sum("nothing was installed" in t for t in [lowered]) == 1
