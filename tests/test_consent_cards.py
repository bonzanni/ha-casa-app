"""Consent approvals show the same readable card as tool approvals.

Every consent question Casa asks (specialist install and update, persona
install, plugin trigger, callback and event consents, the uninstall's erase
question) is posted as a card a model writes from the request's facts, titled
"🔐 Allow <who> to …?" (the erase question has its own fixed title). When the
model does not answer, the card is the consent text Casa wrote before, kept
literally, its first line bold. The consent identities, acks and records do
not change: the card is display only.

The model is ``approval_card._ask``; tests stub it."""
from __future__ import annotations

import asyncio
import functools
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import approval_card
import approval_records as ar
import configurator_continue
import tools
from approval_records import values_digest
from authz_grants import ChallengeCoordinator
from channels.tg_address import TgAddress
from text_util import utf16_len

try:
    from tests.specialist_fixtures import write_bundled_plugin, write_minimal_component
except ImportError:
    from specialist_fixtures import write_bundled_plugin, write_minimal_component

SCOPE = "authz:111:100"
REPLY = {"action": "do the asked thing", "summary": "What it means, in plain words.",
         "lines": [{"label": "Checked", "value": "the one fact"},
                   {"label": "Also", "value": "a second fact"}]}
MODEL_LINES = "\n\nWhat it means, in plain words.\n\nChecked: the one fact\nAlso: a second fact"


class _Channel:
    """Casa's endpoint: bot 111, operator chat 100. Records what the card
    carries: its text and entities, and whether each edit was literal."""

    def __init__(self):
        self.bot_id = 111
        self.posts: list = []
        self.edits: list = []

    async def post_dm_keyboard(self, *, chat_id, request_id, text, options, entities=None):
        self.posts.append(SimpleNamespace(chat_id=chat_id, request_id=request_id, text=text,
                                          options=tuple(options), entities=entities))
        return 55

    async def edit_dm_message(self, chat_id, message_id, text, literal=False):
        self.edits.append(SimpleNamespace(text=text, literal=literal))
        return True


@pytest.fixture
def env(tmp_path, monkeypatch):
    import verdict_broker
    monkeypatch.setattr(ar, "PATH", str(tmp_path / "approval-records.json"))
    monkeypatch.setattr(tools, "_PLUGIN_TOOLS_LOCK", asyncio.Lock())
    ar.reset()
    broker = verdict_broker.VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", broker)
    starts: list = []
    monkeypatch.setattr(configurator_continue, "start",
                        lambda *a, **kw: starts.append(kw) or "started")
    asked: list = []

    async def _model(system, user):
        asked.append(json.loads(user))
        return json.dumps(REPLY)
    monkeypatch.setattr(approval_card, "_ask", _model)
    yield SimpleNamespace(broker=broker, coord=ChallengeCoordinator(), channel=_Channel(),
                          tmp=tmp_path, asked=asked, starts=starts)
    ar.reset()


def _hanging_model(monkeypatch, budget_s=0.05):
    """The model never answers; every card's budget is cut to *budget_s*."""
    async def _hang(system, user):
        await asyncio.Event().wait()
    monkeypatch.setattr(approval_card, "_ask", _hang)
    monkeypatch.setattr(approval_card, "CardRequest",
                        functools.partial(approval_card.CardRequest, budget_s=budget_s))


def _fallback_display(body: str) -> str:
    return approval_card.render_card(approval_card.text_fallback(body))[0]


def _tap(env, idx, *, post=0):
    request_id = env.channel.posts[post].request_id
    claim = env.broker.claim(namespace="resident_ask", scope=SCOPE, request_id=request_id,
                             option_index=idx, actor_id=100)
    assert not isinstance(claim, str), claim
    assert env.broker.commit(claim) is True
    step = claim.req.meta.get("on_commit_sync")
    if step is not None:
        step(idx)


async def _settle(n: int = 20):
    for _ in range(n):
        await asyncio.sleep(0)


# --- the six consent questions -------------------------------------------------------

def _specialist_inspection(**over):
    from specialist_install import DependencyResolution, InspectionResult
    base = dict(
        component_id="casa-test/mtg", version="0.2.0", slug="mtg",
        component_checksum="sha256:" + "1" * 64, root_digest="sha256:" + "4" * 64,
        mission="Answer test questions.",
        default_persona_ref="casa/judge@0.1.0", default_persona_checksum="sha256:" + "2" * 64,
        required_config_names=(), required_secret_names=(),
        dependencies=(DependencyResolution(kind="persona", identifier="casa/judge@0.1.0",
                                            digest="sha256:" + "2" * 64, available=True,
                                            detail=""),),
        staged_dir=Path("/config/specialists/.staging/x"))
    base.update(over)
    return InspectionResult(**base)


async def _specialist_record(kind="specialist_install", inspection=None,
                             values=None):
    from specialist_install_consent import identity_for_inspection
    values = values if values is not None else {"config": {"API_URL": "https://e.test"},
                                                "secrets": []}
    identity = identity_for_inspection(inspection or _specialist_inspection(), values=values)
    async with tools._plugin_tools_guard():
        return ar.request(kind, "casa-test/mtg:mtg", identity, values, asked="install mtg")


async def _post_specialist(env, *, kind="specialist_install", inspection=None, **kw):
    from specialist_install_consent import (SpecialistInstallAckStore,
                                            prompt_specialist_install_consent)
    inspection = inspection or _specialist_inspection()
    record, _, _ = await _specialist_record(kind, inspection)
    handle = prompt_specialist_install_consent(
        coordinator=env.coord, channel=env.channel, chat_id=100, operator_id=100,
        record=record, inspection_facts=inspection, continuation="carry on",
        acks=SpecialistInstallAckStore(path=env.tmp / "acks.json"),
        submitted_config={"API_URL": "x"}, **kw)
    return record, handle


async def _post_persona(env, **kw):
    from persona_install import PersonaInstallAckStore, persona_install_consent_identity
    from persona_install_consent import prompt_persona_install_consent
    inspection = SimpleNamespace(persona_id="judge", version="1.0.0",
                                 checksum="sha256:" + "a" * 64, display_name="MTG Judge")
    identity = persona_install_consent_identity(
        persona_id="judge", version="1.0.0", checksum=inspection.checksum,
        values_digest=values_digest({}))
    async with tools._plugin_tools_guard():
        record, _, _ = ar.request("persona_install", "judge", identity, {}, asked="add judge")
    return record, prompt_persona_install_consent(
        coordinator=env.coord, channel=env.channel, chat_id=100, operator_id=100,
        record=record, inspection=inspection, continuation="carry on",
        acks=PersonaInstallAckStore(path=env.tmp / "persona-acks.json"), **kw)


TRIGGER_AUTH = {"mode": "static_header", "header": "X-API-Key", "tolerance_secs": 300,
                "secret_owner": "casa"}


def _post_trigger(env):
    import trigger_consent
    from trigger_acks import TriggerAckStore
    return trigger_consent.prompt_trigger_consent(
        coordinator=env.coord, channel=env.channel, chat_id=100, operator_id=100,
        plugin="elevenlabs", artifact_id="art-1", effective="plg-elevenlabs--voicemail",
        target="resident:assistant", auth=TRIGGER_AUTH,
        acks=TriggerAckStore(path=env.tmp / "trigger_acks.json"))


def _post_callback(env):
    import callback_consent
    from callback_acks import CallbackAckStore
    from plugin_callbacks import declaration_digest
    return callback_consent.prompt_callback_consent(
        coordinator=env.coord, channel=env.channel, chat_id=100, operator_id=100,
        plugin="gmail", artifact_id="art-1", declared="authorize",
        effective="plg-gmail--authorize",
        declaration_digest=declaration_digest({"declared": "authorize",
                                               "effective": "plg-gmail--authorize"}),
        acks=CallbackAckStore(path=env.tmp / "callback_acks.json"))


def _post_event(env):
    import event_consent
    from event_acks import EventAckStore
    return event_consent.prompt_event_consent(
        coordinator=env.coord, channel=env.channel, chat_id=100, operator_id=100,
        subscriber="ledger", artifact_id="art-1", emitter="bank", event="payment",
        digest="sha256:" + "e" * 64, targets=["resident:assistant", "specialist:finance"],
        acks=EventAckStore(path=env.tmp / "event_acks.json"))


ERASERS = [("gmail", "erase_all", "Deletes every stored message")]


def _post_erase(env, *, card=True):
    import plugin_erase_consent as pec
    key = pec.EraseChoiceKey(operator_id=100, bot_id=111, chat_id=100,
                             subject="plugin:gmail", artifacts=("art-1",), question="q1")
    text = pec.render_erase_choice("the plugin gmail", ERASERS)

    async def _continue(choice):
        return True
    return pec.prompt_erase_choice(
        coordinator=env.coord, channel=env.channel, key=key, text=text,
        continue_cb=_continue,
        card_request=(pec.erase_card_request("the plugin gmail", ERASERS,
                                             requester_name="Ellen") if card else None))


async def test_install_card_has_the_uniform_title_and_the_models_lines(env):
    _, handle = await _post_specialist(env, requester_name="Ellen")
    assert await handle.settled_post() == "posted"
    [post] = env.channel.posts
    assert post.text == "🔐 Allow Ellen to do the asked thing?" + MODEL_LINES
    [facts] = env.asked
    assert (facts["requested_by"], facts["request"], facts["specialist"], facts["component"],
            facts["version"]) == ("Ellen", "install", "mtg", "casa-test/mtg", "0.2.0")
    assert facts["settings"] == {"API_URL": "https://e.test"}
    assert "sha256:" not in json.dumps(facts)       # no digest reaches the model


async def test_upgrade_card_names_both_versions(env):
    _, handle = await _post_specialist(env, kind="specialist_upgrade",
                                       requester_name="Ellen", from_version="0.1.0")
    assert await handle.settled_post() == "posted"
    assert env.channel.posts[0].text.startswith("🔐 Allow Ellen to do the asked thing?")
    [facts] = env.asked
    assert (facts["request"], facts["from_version"], facts["to_version"]) == (
        "update", "0.1.0", "0.2.0")
    assert "version" not in facts


async def test_persona_card_has_the_uniform_title_and_the_models_lines(env):
    _, handle = await _post_persona(env, requester_name="Ellen")
    assert await handle.settled_post() == "posted"
    assert env.channel.posts[0].text == "🔐 Allow Ellen to do the asked thing?" + MODEL_LINES
    assert env.asked == [{"requested_by": "Ellen", "request": "install a persona",
                          "persona": "judge", "version": "1.0.0",
                          "display_name": "MTG Judge"}]


@pytest.mark.parametrize("post, who", [(_post_trigger, "the elevenlabs plugin"),
                                       (_post_callback, "the gmail plugin"),
                                       (_post_event, "the ledger plugin")],
                         ids=["trigger", "callback", "event"])
async def test_plugin_consent_card_names_the_plugin(env, post, who):
    handle = post(env)
    assert await handle.settled_post() == "posted"
    [card] = env.channel.posts
    assert card.text == f"🔐 Allow {who} to do the asked thing?" + MODEL_LINES
    assert card.options == ("Approve", "Deny")
    assert env.asked[0]["requested_by"] == who


async def test_erase_card_has_its_own_title(env):
    handle = _post_erase(env)
    assert await handle.settled_post() == "posted"
    [card] = env.channel.posts
    assert card.text == ("🔐 Uninstall the plugin gmail: erase its data too?" + MODEL_LINES)
    assert card.options == ("Keep data", "Erase everything", "Cancel")
    [facts] = env.asked
    assert facts["erasers"] == [{"plugin": "gmail", "tool": "erase_all",
                                 "what_the_plugin_says_it_does": "Deletes every stored message"}]
    assert list(facts["choices"]) == ["Keep data", "Erase everything", "Cancel"]


# --- the fallback is today's text --------------------------------------------------

async def test_consent_fallback_is_todays_body_literal(env, monkeypatch):
    """The model hangs: what is posted is the consent text as Casa renders it,
    character for character -- a markdown link in the mission stays literal --
    with only its first line bold."""
    from specialist_install_consent import render_install_consent_message
    _hanging_model(monkeypatch)
    inspection = _specialist_inspection(
        mission="See [the docs](https://example.test/x) and *this*.")
    record, handle = await _post_specialist(env, inspection=inspection)
    assert await handle.settled_post() == "posted"
    body = render_install_consent_message(inspection, values=record.values,
                                          submitted_config={"API_URL": "x"})
    [post] = env.channel.posts
    assert post.text == body
    assert "Mission: See [the docs](https://example.test/x) and *this*." in post.text
    first = body.split("\n", 1)[0]
    assert [(e.type, e.offset, e.length) for e in post.entities] == [
        ("bold", 0, utf16_len(first))]


@pytest.mark.parametrize("kind", ["persona", "trigger", "callback", "event", "erase"])
async def test_every_consent_falls_back_to_its_own_text(env, monkeypatch, kind):
    import callback_consent
    import event_consent
    import persona_install_consent
    import plugin_erase_consent as pec
    import trigger_consent
    _hanging_model(monkeypatch)
    if kind == "persona":
        _, handle = await _post_persona(env)
        body = persona_install_consent.render_persona_install_consent_message(
            SimpleNamespace(persona_id="judge", version="1.0.0",
                            checksum="sha256:" + "a" * 64, display_name="MTG Judge"))
    elif kind == "trigger":
        handle = _post_trigger(env)
        body = trigger_consent.render_trigger_consent_message(
            plugin="elevenlabs", effective="plg-elevenlabs--voicemail", role="assistant",
            auth=TRIGGER_AUTH)
    elif kind == "callback":
        handle = _post_callback(env)
        body = callback_consent.render_callback_consent_message(
            plugin="gmail", effective="plg-gmail--authorize")
    elif kind == "event":
        handle = _post_event(env)
        body = event_consent.render_event_consent_message(
            subscriber="ledger", emitter="bank", event="payment",
            targets=["resident:assistant", "specialist:finance"])
    else:
        handle = _post_erase(env)
        body = pec.render_erase_choice("the plugin gmail", ERASERS)
    assert await handle.settled_post() == "posted"
    [post] = env.channel.posts
    assert post.text == body
    assert [(e.offset, e.length) for e in post.entities] == [
        (0, utf16_len(body.split("\n", 1)[0]))]


# --- the card never changes what is approved ---------------------------------------

async def test_the_card_leaves_identity_ack_and_record_as_they_were(env):
    """Approve on a model-written install card acks exactly the record's
    identity and approves the record, as the plain text card did."""
    from specialist_install_consent import SpecialistInstallAckStore
    record, handle = await _post_specialist(env, requester_name="Ellen")
    await handle.settled_post()
    [key] = list(env.coord._entries)
    assert key.identity == record.consent_identity
    _tap(env, 0)
    await _settle()
    assert SpecialistInstallAckStore(path=env.tmp / "acks.json").is_acked(
        record.consent_identity)
    assert ar.get(record.rid).state == "approved"


@pytest.mark.parametrize("kind", ["install", "persona", "trigger", "callback", "event",
                                  "erase"])
@pytest.mark.parametrize("idx", [0, 1])
async def test_every_consent_edit_is_literal(env, kind, idx):
    """A settled consent card is edited as plain text: no edit is parsed for
    markup, whatever it says."""
    if kind == "install":
        _, handle = await _post_specialist(env)
    elif kind == "persona":
        _, handle = await _post_persona(env)
    else:
        handle = {"trigger": _post_trigger, "callback": _post_callback,
                  "event": _post_event, "erase": _post_erase}[kind](env)
    assert await handle.settled_post() == "posted"
    _tap(env, idx)
    await _settle(40)
    if kind in ("install", "persona") and idx == 0:
        await env.starts[0]["on_outcome"]("started")
    assert env.channel.edits
    assert all(e.literal is True for e in env.channel.edits)


async def test_an_expired_consent_card_is_edited_literally(env):
    handle = _post_trigger(env)
    await handle.settled_post()
    env.broker._on_timeout(("resident_ask", SCOPE, env.channel.posts[0].request_id))
    await _settle()
    assert [e.literal for e in env.channel.edits] == [True]


async def test_supersede_edits_an_approved_card_literally(env):
    from specialist_install_consent import supersede_card
    record, handle = await _post_specialist(env)
    await handle.settled_post()
    _tap(env, 0)
    await _settle()
    _b, _, prev = await _specialist_record(values={"config": {"API_URL": "https://b.test"},
                                                   "secrets": []})
    assert prev.rid == record.rid
    await supersede_card(prev, channel=env.channel)
    assert env.channel.edits[-1].text == "Replaced by a newer request; nothing was installed."
    assert env.channel.edits[-1].literal is True


async def test_superseded_install_while_writing_posts_nothing(env, monkeypatch):
    """A newer request replaces this one while its card is still being
    written (the record has no card yet): the card is never posted."""
    release = asyncio.Event()

    async def _slow(system, user):
        await release.wait()
        return json.dumps(REPLY)
    monkeypatch.setattr(approval_card, "_ask", _slow)
    from specialist_install_consent import (SpecialistInstallAckStore,
                                            prompt_specialist_install_consent)
    monkeypatch.setattr(tools, "CHALLENGES", env.coord)
    monkeypatch.setattr(tools, "_casa_card_target",
                        lambda: (env.channel, TgAddress(111, 100), None))
    inspection = _specialist_inspection()
    record, _, _ = await _specialist_record(inspection=inspection)

    def _post(**kw):
        return prompt_specialist_install_consent(
            inspection_facts=inspection, submitted_config={"API_URL": "x"},
            acks=SpecialistInstallAckStore(path=env.tmp / "acks.json"), **kw)
    # The older run, posting its card through the request tool's own poster.
    older = asyncio.create_task(tools._post_approval_card(
        record, _post, what="specialist:mtg", continuation="carry on"))
    await _settle()
    _b, outcome, prev = await _specialist_record(
        values={"config": {"API_URL": "https://b.test"}, "secrets": []})
    assert (outcome, prev.rid, prev.card) == ("superseded_previous", record.rid, None)
    await tools._supersede_card(prev)
    release.set()
    result = await older
    await _settle()
    assert env.channel.posts == []
    assert env.channel.edits == []
    assert ar.get(record.rid).state == "superseded"
    # The older run hears it was replaced, not that its card expired.
    assert result["kind"] == "approval_superseded", result


# --- the governed request tool -----------------------------------------------------

GRANTS = ["mcp__casa-framework__search_memory", "mcp__casa-framework__send_media"]
PROTECTED = [{"name": "danger_tool", "summary": "Deletes the deck {deck}"}, "plain_tool"]
ELLEN = {"role": "assistant", "execution_role": "configurator", "channel": "telegram",
         "chat_id": 100, "cid": "c", "user_text": "install mtg"}


@pytest.fixture
def governed(env, monkeypatch):
    """The real request tool over a really staged component: a role granting
    two Casa tools and a bundled plugin with two protected tools, one of them
    summarised. Casa's card goes to the env channel; the registration's text
    is recorded."""
    import plugin_registry
    import specialist_install
    import specialist_receipt
    from specialist_registry import InstalledSpecialistIndex
    tmp = env.tmp
    plugin_registry.reload_snapshot(registry_path=tmp / "registry.json",
                                    store_root=tmp / "store")
    component_dir, manifest_path = write_minimal_component(tmp, slug="mtg",
                                                           tools_allowed=GRANTS)
    digest = write_bundled_plugin(component_dir, "mtg", protected_tools=PROTECTED)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["dependencies"].append({
        "kind": "plugin/implementation", "identifier": "mtg", "digest": digest,
        "source": {"type": "bundled", "path": "plugins/mtg"}})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def _fetch(repo, ref, subdir, dest, *, expected_revision=None):
        import shutil
        shutil.copytree(component_dir, dest)
        return "a" * 40
    monkeypatch.setattr(specialist_install, "resolve_and_fetch", _fetch)
    index = InstalledSpecialistIndex(specialists_dir=str(tmp / "specialists"))
    index.load()
    result = specialist_install.inspect_specialist_repo(
        "org/repo", "main", staging_root=tmp / "staging", installed_index=index,
        receipts_dir=tmp / "receipts")
    real_load = specialist_receipt.load
    monkeypatch.setattr(specialist_receipt, "load",
                        lambda rid, receipts_dir=None: real_load(rid, tmp / "receipts"))
    monkeypatch.setattr(tools, "_SPECIALISTS_DIR", tmp / "specialists")
    monkeypatch.setattr(tools, "CHALLENGES", env.coord)
    monkeypatch.setattr(tools, "_casa_card_target",
                        lambda: (env.channel, TgAddress(111, 100), None))
    monkeypatch.setattr(tools, "_agent_role_map", {
        "assistant": SimpleNamespace(character=SimpleNamespace(name="Ellen"))})
    registered: list = []
    real_register = env.coord.register_challenge

    def _register(key, **kw):
        registered.append(kw)
        return real_register(key, **kw)
    monkeypatch.setattr(env.coord, "register_challenge", _register)
    return SimpleNamespace(result=result, registered=registered)


async def _request_approval(governed, config):
    from test_delegate_to_agent import _with_origin
    r = governed.result
    out = await _with_origin(tools.specialist_install_request_approval.handler({
        "slug": r.slug, "root_digest": r.root_digest, "staged_dir": str(r.staged_dir),
        "receipt_id": r.receipt_id, "mode": "install", "config": config}), dict(ELLEN))
    return json.loads(out["content"][0]["text"])


def _todays_body(governed, config):
    """The whole consent text of the inspection rebuilt from the checked
    staged bytes, before any cut to the size limit."""
    import specialist_install
    from specialist_install_consent import render_install_consent_message
    r = governed.result
    checked = specialist_install.validate_resume_inputs(
        staged_dir=r.staged_dir, receipt_id=r.receipt_id, root_digest=r.root_digest)
    inspection = tools._inspection_from_checked(checked, Path(r.staged_dir))
    return render_install_consent_message(
        inspection, mode="install", values={"config": config, "secrets": []},
        submitted_config=config)


async def test_install_title_names_the_requesting_agent(env, governed):
    out = await _request_approval(governed, {"API_URL": "https://e.test"})
    assert out["approval"] == "requested", out
    [post] = env.channel.posts
    assert post.text.startswith("🔐 Allow Ellen to do the asked thing?")
    assert env.asked[0]["requested_by"] == "Ellen"


async def test_governed_install_request_carries_role_grants_and_tool_summaries(env, governed):
    config = {"API_URL": "https://e.test"}
    out = await _request_approval(governed, config)
    assert out["approval"] == "requested", out
    [facts] = env.asked
    assert facts["casa_tools_granted"] == GRANTS
    [plugin] = facts["bundled_plugins"]
    assert plugin["name"] == "mtg.mtg"
    assert plugin["protected_tools"] == [
        {"name": "danger_tool", "summary": "Deletes the deck {deck}"},
        {"name": "plain_tool", "summary": None}]
    # #1543: the consent text, which registration measures and the fallback
    # shows, names the Casa tools too.
    [reg] = governed.registered
    assert reg["challenge_text"] == _todays_body(governed, config)
    assert reg["challenge_text"].count("Casa tools: " + ", ".join(GRANTS) + "\n") == 1
    assert reg["card_request"].fallback == approval_card.text_fallback(reg["challenge_text"])


async def test_grants_are_told_when_the_extra_facts_cannot_be_read(env, governed,
                                                                   monkeypatch):
    """The grants come from the checked component itself, so the model is
    told them even when the card's extra facts cannot be read at all."""
    def _fails(*a, **kw):
        raise OSError("staged tree gone")
    monkeypatch.setattr(tools, "_install_card_facts", _fails)
    out = await _request_approval(governed, {})
    assert out["approval"] == "requested", out
    [facts] = env.asked
    assert facts["casa_tools_granted"] == GRANTS
    # The plugin's tools still come from the checked receipt, by name.
    assert [t["name"] for t in facts["bundled_plugins"][0]["protected_tools"]] == [
        "danger_tool", "plain_tool"]


async def test_unreadable_plugin_manifest_lists_its_tools_by_name(env, governed, caplog,
                                                                   monkeypatch):
    import logging
    manifest = (Path(governed.result.staged_dir) / "plugins" / "mtg" / ".claude-plugin"
                / "plugin.json")
    real_read = Path.read_text

    def _read(self, *a, **kw):
        if self == manifest:
            raise PermissionError("unreadable")
        return real_read(self, *a, **kw)
    # The staged bytes are checked first; only the card facts' later read of
    # the plugin's manifest fails.
    import specialist_install
    real_validate = specialist_install.validate_resume_inputs

    def _validate(**kw):
        checked = real_validate(**kw)
        monkeypatch.setattr(Path, "read_text", _read)
        return checked
    monkeypatch.setattr(specialist_install, "validate_resume_inputs", _validate)
    with caplog.at_level(logging.INFO, logger="tools"):
        out = await _request_approval(governed, {})
    monkeypatch.setattr(Path, "read_text", real_read)
    assert out["approval"] == "requested", out
    [plugin] = env.asked[0]["bundled_plugins"]
    assert plugin["protected_tools"] == [{"name": "danger_tool", "summary": None},
                                         {"name": "plain_tool", "summary": None}]
    assert any("protected tool summaries of mtg.mtg unreadable" in r.getMessage()
               for r in caplog.records)


async def test_an_install_over_the_size_limit_is_asked_with_its_text_cut(env, governed):
    """A consent text over the 3 900-unit limit is not refused (#1541): what
    registration measures, and the fallback shows, is its leading whole lines
    and how many were left out. The model is still told every fact."""
    base = utf16_len(_todays_body(governed, {"NOTE": "x"}))
    config = {"NOTE": "x" * (1 + 3950 - base)}
    body = _todays_body(governed, config)
    assert utf16_len(body) == 3950
    out = await _request_approval(governed, config)
    assert out["approval"] == "requested", out
    [reg] = governed.registered
    text = reg["challenge_text"]
    assert utf16_len(text) <= 3900
    *kept, closing = text.split("\n")
    assert kept == body.split("\n")[:len(kept)]
    assert kept[0].startswith("🔐 Install ")
    assert ("Casa tools: " + ", ".join(GRANTS)) in kept
    left = len(body.split("\n")) - len(kept)
    assert left > 0 and closing == f"…and {left} more line{'' if left == 1 else 's'}"
    assert reg["card_request"].fallback == approval_card.text_fallback(text)
    assert len(env.channel.posts) == 1
    assert env.asked[0]["settings"] == config


async def test_upgrade_facts_carry_the_installed_version(monkeypatch, tmp_path):
    import personality_binding
    from specialist_install import component_root_string
    active = SimpleNamespace(root=component_root_string(
        component_id="casa-test/mtg", version="0.1.0", component_checksum="sha256:" + "5" * 64))
    monkeypatch.setattr(personality_binding, "InstanceDir",
                        lambda path: SimpleNamespace(active=lambda: active))
    checked = SimpleNamespace(
        component=SimpleNamespace(role=SimpleNamespace(role={}), dependencies=()),
        receipt=SimpleNamespace(plugins=()))
    facts = tools._install_card_facts(checked, tmp_path, slug="mtg", mode="upgrade")
    assert facts == {"from_version": "0.1.0", "protected_tools": {}}
    monkeypatch.setattr(personality_binding, "InstanceDir",
                        lambda path: SimpleNamespace(active=lambda: None))
    assert "from_version" not in tools._install_card_facts(
        checked, tmp_path, slug="mtg", mode="upgrade")
