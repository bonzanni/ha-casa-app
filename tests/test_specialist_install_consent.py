import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import approval_records as ar
import configurator_continue
import tools
from approval_records import values_digest
from authz_grants import ChallengeCoordinator
from channels.tg_address import TgAddress
from specialist_install import DependencyResolution, InspectionResult
from specialist_install_consent import (
    SpecialistInstallAckStore,
    identity_for_inspection,
    install_consent_identity,
    prompt_specialist_install_consent,
    render_install_consent_message,
    supersede_card,
)

#: the values digest of a fresh install with no settings supplied
VD = values_digest({"config": {}, "secrets": []})


def _inspection(**overrides) -> InspectionResult:
    base = dict(
        component_id="casa-test/mtg", version="0.1.0", slug="mtg",
        component_checksum="sha256:" + "1" * 64, root_digest="sha256:" + "4" * 64,
        mission="Answer test questions.",
        default_persona_ref="casa/judge@0.1.0", default_persona_checksum="sha256:" + "2" * 64,
        required_config_names=(), required_secret_names=(),
        dependencies=(DependencyResolution(kind="persona", identifier="casa/judge@0.1.0",
                                            digest="sha256:" + "2" * 64, available=True, detail=""),),
        staged_dir=Path("/config/specialists/.staging/x"),
    )
    base.update(overrides)
    return InspectionResult(**base)


def test_identity_is_stable_for_the_same_inputs() -> None:
    a = install_consent_identity(component_id="casa-test/mtg", version="0.1.0",
                                  root_digest="sha256:" + "1" * 64, slug="mtg", values_digest=VD)
    b = install_consent_identity(component_id="casa-test/mtg", version="0.1.0",
                                  root_digest="sha256:" + "1" * 64, slug="mtg", values_digest=VD)
    assert a == b


def test_identity_changes_when_component_checksum_changes() -> None:
    a = install_consent_identity(component_id="casa-test/mtg", version="0.1.0",
                                  root_digest="sha256:" + "1" * 64, slug="mtg", values_digest=VD)
    b = install_consent_identity(component_id="casa-test/mtg", version="0.1.0",
                                  root_digest="sha256:" + "9" * 64, slug="mtg", values_digest=VD)
    assert a != b


def test_ack_store_is_unacked_until_recorded(tmp_path: Path) -> None:
    store = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    identity = install_consent_identity(component_id="casa-test/mtg", version="0.1.0",
                                         root_digest="sha256:" + "1" * 64, slug="mtg", values_digest=VD)
    assert store.is_acked(identity) is False
    store.record(identity=identity, component_id="casa-test/mtg", version="0.1.0",
                 component_checksum="sha256:" + "1" * 64, slug="mtg", values_digest=VD)
    assert store.is_acked(identity) is True


def test_ack_store_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "acks.json"
    identity = install_consent_identity(component_id="casa-test/mtg", version="0.1.0",
                                         root_digest="sha256:" + "1" * 64, slug="mtg", values_digest=VD)
    SpecialistInstallAckStore(path=path).record(
        identity=identity, component_id="casa-test/mtg", version="0.1.0",
        component_checksum="sha256:" + "1" * 64, slug="mtg", values_digest=VD)
    reopened = SpecialistInstallAckStore(path=path)
    assert reopened.is_acked(identity) is True


def test_ack_store_fails_closed_on_a_hand_edited_key(tmp_path: Path) -> None:
    import json

    path = tmp_path / "acks.json"
    identity = install_consent_identity(component_id="casa-test/mtg", version="0.1.0",
                                         root_digest="sha256:" + "1" * 64, slug="mtg", values_digest=VD)
    SpecialistInstallAckStore(path=path).record(
        identity=identity, component_id="casa-test/mtg", version="0.1.0",
        component_checksum="sha256:" + "1" * 64, slug="mtg", values_digest=VD)
    raw = json.loads(path.read_text(encoding="utf-8"))
    # Tamper: change the recorded component_checksum without updating the key.
    list(raw["acks"].values())[0]["component_checksum"] = "sha256:" + "0" * 64
    path.write_text(json.dumps(raw), encoding="utf-8")
    tampered = SpecialistInstallAckStore(path=path)
    assert tampered.is_acked(identity) is False  # whole-store fail-closed, never partial trust


def test_record_aborts_rather_than_wiping_on_transient_read_failure(
    tmp_path: Path, monkeypatch,
) -> None:
    """#310 (Sol r1): a transient ledger read failure (an OSError that is not
    file-missing) during a mutation's read-modify-write must abort — never
    persist the fail-closed empty view over previously recorded acks."""
    path = tmp_path / "acks.json"
    store = SpecialistInstallAckStore(path=path)
    ident_a = install_consent_identity(component_id="casa-test/a", version="0.1.0",
                                       root_digest="sha256:" + "1" * 64, slug="a", values_digest=VD)
    ident_b = install_consent_identity(component_id="casa-test/b", version="0.1.0",
                                       root_digest="sha256:" + "2" * 64, slug="b", values_digest=VD)
    store.record(identity=ident_a, component_id="casa-test/a", version="0.1.0",
                 component_checksum="sha256:" + "1" * 64, slug="a",
                 values_digest=VD)

    real_read_text = Path.read_text

    def _flaky(self, *args, **kwargs):
        if self == path:
            raise PermissionError("transient read failure")
        return real_read_text(self, *args, **kwargs)

    with monkeypatch.context() as mp:
        mp.setattr(Path, "read_text", _flaky)
        with pytest.raises(OSError):
            store.record(identity=ident_b, component_id="casa-test/b", version="0.1.0",
                         component_checksum="sha256:" + "2" * 64, slug="b",
                         values_digest=VD)

    assert SpecialistInstallAckStore(path=path).is_acked(ident_a)


def test_revoke_removes_an_ack(tmp_path: Path) -> None:
    store = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    identity = install_consent_identity(component_id="casa-test/mtg", version="0.1.0",
                                         root_digest="sha256:" + "1" * 64, slug="mtg", values_digest=VD)
    store.record(identity=identity, component_id="casa-test/mtg", version="0.1.0",
                 component_checksum="sha256:" + "1" * 64, slug="mtg", values_digest=VD)
    assert store.revoke(identity) is True
    assert store.is_acked(identity) is False


def test_identity_changes_when_the_values_digest_changes() -> None:
    a = install_consent_identity(component_id="casa-test/mtg", version="0.1.0",
                                  root_digest="sha256:" + "1" * 64, slug="mtg", values_digest=VD)
    b = install_consent_identity(component_id="casa-test/mtg", version="0.1.0",
                                  root_digest="sha256:" + "1" * 64, slug="mtg",
                                  values_digest=values_digest({"config": {"A": "1"},
                                                               "secrets": []}))
    assert a != b


def test_identity_needs_a_values_digest() -> None:
    with pytest.raises(TypeError):
        install_consent_identity(component_id="casa-test/mtg", version="0.1.0",  # type: ignore[call-arg]
                                 root_digest="sha256:" + "1" * 64, slug="mtg")
    with pytest.raises(ValueError):
        install_consent_identity(component_id="casa-test/mtg", version="0.1.0",
                                 root_digest="sha256:" + "1" * 64, slug="mtg", values_digest="")


def test_identity_for_inspection_is_the_identity_of_its_fields_and_values() -> None:
    insp = _inspection(receipt_digest="sha256:" + "7" * 64)
    values = {"config": {"A": "1"}, "secrets": ["S"]}
    assert identity_for_inspection(insp, values=values) == install_consent_identity(
        component_id=insp.component_id, version=insp.version, root_digest=insp.root_digest,
        slug=insp.slug, receipt_digest=insp.receipt_digest,
        values_digest=values_digest(values))
    assert identity_for_inspection(insp, values=values) != identity_for_inspection(
        insp, values={"config": {"A": "2"}, "secrets": ["S"]})


def test_the_ack_record_persists_its_values_digest(tmp_path: Path) -> None:
    import json

    path = tmp_path / "acks.json"
    identity = install_consent_identity(component_id="casa-test/mtg", version="0.1.0",
                                         root_digest="sha256:" + "1" * 64, slug="mtg",
                                         values_digest=VD)
    SpecialistInstallAckStore(path=path).record(
        identity=identity, component_id="casa-test/mtg", version="0.1.0",
        component_checksum="sha256:" + "1" * 64, slug="mtg", values_digest=VD)
    assert json.loads(path.read_text(encoding="utf-8"))["acks"][identity]["values_digest"] == VD


def test_a_record_without_a_values_digest_is_an_identity_mismatch(tmp_path: Path) -> None:
    """An entry written before approvals carried their values recomputes to no
    key: the store reads it as it reads any mismatch, failing closed."""
    import json

    path = tmp_path / "acks.json"
    legacy_fields = {"component_id": "casa-test/mtg", "version": "0.1.0",
                     "component_checksum": "sha256:" + "1" * 64, "slug": "mtg", "ts": 1}
    from canonical_bytes import checksum_json
    legacy_key = checksum_json({k: legacy_fields[k] for k in
                                ("component_id", "version", "component_checksum", "slug")})
    path.write_text(json.dumps({"schema_version": 1, "acks": {legacy_key: legacy_fields}}),
                    encoding="utf-8")
    store = SpecialistInstallAckStore(path=path)
    assert store.is_acked(legacy_key) is False
    assert store.get(legacy_key) is None
    assert store.snapshot_slug("mtg") == []


def test_restoring_a_journalled_record_recomputes_its_identity_with_its_values_digest(
        tmp_path: Path) -> None:
    store = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    identity = install_consent_identity(component_id="casa-test/mtg", version="0.1.0",
                                         root_digest="sha256:" + "1" * 64, slug="mtg",
                                         values_digest=VD)
    store.record(identity=identity, component_id="casa-test/mtg", version="0.1.0",
                 component_checksum="sha256:" + "1" * 64, slug="mtg", values_digest=VD)
    removed = store.retire_slug("mtg")
    assert store.is_acked(identity) is False
    store.restore_records(removed)
    assert store.is_acked(identity) is True


def test_render_message_names_slug_and_dependencies() -> None:
    text = render_install_consent_message(_inspection())
    assert "mtg" in text
    assert "casa/judge@0.1.0" in text


def test_render_message_discloses_role_casa_tool_grants() -> None:
    # #541: the operator approving the install must see the casa-framework
    # powers the specialist arrives with.
    text = render_install_consent_message(_inspection(role_tool_grants=(
        "mcp__casa-framework__search_memory",
        "mcp__casa-framework__send_media",
    )))
    assert ("Casa tools: mcp__casa-framework__search_memory, "
            "mcp__casa-framework__send_media") in text


def test_render_message_omits_casa_tools_line_when_role_grants_none() -> None:
    text = render_install_consent_message(_inspection())
    assert "Casa tools:" not in text



# --- the approval card --------------------------------------------------------
#
# The card asks about one approval record: it shows the record's effective
# settings, binds the record's consent identity, and on Approve moves the record
# to approved and has Casa carry the configurator on. A newer request replaces
# it. ``configurator_continue.start`` is replaced by a recorder here; the real
# launcher is exercised with these cards where both are built.

KEY = "casa-test/mtg:mtg"
CONTINUATION = "The operator approved installing specialist:mtg. Call specialist_install_commit."
SCOPE = "authz:111:100"
VALUES = {"config": {"API_URL": "https://example.test", "LIMIT": 5}, "secrets": ["TOKEN"]}


class _Channel:
    """Casa's endpoint: bot 111, operator chat 100."""

    def __init__(self):
        self.bot_id = 111
        self.posts = []
        self.edits = []
        self.post_result = 55

    async def post_dm_keyboard(self, *, chat_id, request_id, text, options):
        self.posts.append((chat_id, request_id, text, tuple(options)))
        return self.post_result

    async def edit_dm_message(self, chat_id, message_id, text):
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
    start_result = {"value": "started"}

    def _start(continuation, *, key, approval=None, asked="", on_outcome=None):
        starts.append(SimpleNamespace(continuation=continuation, key=key, approval=approval,
                                      asked=asked, on_outcome=on_outcome))
        return start_result["value"]

    monkeypatch.setattr(configurator_continue, "start", _start)
    yield SimpleNamespace(broker=broker, coord=ChallengeCoordinator(), channel=_Channel(),
                          acks=SpecialistInstallAckStore(path=tmp_path / "acks.json"),
                          starts=starts, start_result=start_result)
    ar.reset()


async def _request(values=VALUES, *, kind="specialist_install", inspection=None):
    inspection = inspection or _inspection()
    identity = identity_for_inspection(inspection, values=values)
    async with tools._plugin_tools_guard():
        return ar.request(kind, KEY, identity, values, asked="install mtg")


def _prompt(env, record, *, submitted=None, inspection=None):
    return prompt_specialist_install_consent(
        coordinator=env.coord, channel=env.channel, chat_id=100, operator_id=100,
        record=record, inspection_facts=inspection or _inspection(),
        continuation=CONTINUATION, acks=env.acks,
        submitted_config=submitted if submitted is not None else {"API_URL": "x"})


async def _settle(n: int = 10):
    for _ in range(n):
        await asyncio.sleep(0)


def _tap(env, idx, *, post=0):
    """What the Telegram callback does: claim, commit, then the sync step.
    Returns the claim's refusal, or ``None`` when the tap was taken."""
    request_id = env.channel.posts[post][1]
    claim = env.broker.claim(namespace="resident_ask", scope=SCOPE, request_id=request_id,
                             option_index=idx, actor_id=100)
    if isinstance(claim, str):
        return claim
    assert env.broker.commit(claim) is True
    claim.req.meta["on_commit_sync"](idx)
    return None


def test_the_card_shows_the_effective_settings_and_marks_those_kept():
    text = render_install_consent_message(
        _inspection(), values=VALUES, submitted_config={"API_URL": "x"})
    lines = text.splitlines()
    assert sum(ln.strip() == "API_URL = https://example.test" for ln in lines) == 1
    assert sum(ln.strip() == "LIMIT = 5 (kept from the current settings)" for ln in lines) == 1
    assert sum(ln.strip() == "Secrets provided (names only): TOKEN" for ln in lines) == 1


def test_a_setting_shows_bare_unless_bare_would_hide_what_it_is():
    from specialist_install_consent import render_settings_lines
    lines = render_settings_lines({"config": {
        "PLAIN": "1", "EMPTY": "", "PADDED": " x ", "TWO_LINES": "a\nb", "NUMBER": 1,
    }, "secrets": []})
    shown = {ln.strip() for ln in lines}
    assert "PLAIN = 1" in shown
    assert 'EMPTY = ""' in shown
    assert 'PADDED = " x "' in shown
    assert 'TWO_LINES = "a\\nb"' in shown
    assert "NUMBER = 1" in shown


def test_the_card_says_when_it_approves_no_settings():
    text = render_install_consent_message(
        _inspection(), values={"config": {}, "secrets": []}, submitted_config={})
    assert sum(ln.strip() == "Settings: none" for ln in text.splitlines()) == 1
    assert "Secrets provided" not in text


async def test_the_card_binds_the_records_identity_and_acks_its_values(env):
    record, outcome, _ = await _request()
    assert outcome == "new"
    handle = _prompt(env, record)
    await handle.settled_post()
    [(chat, _rid, text, options)] = env.channel.posts
    assert (chat, options) == (100, ("Approve", "Deny"))
    assert "API_URL = https://example.test" in text and "TOKEN" in text
    [key] = [k for k in env.coord._entries]
    assert key.identity == record.consent_identity
    assert ar.get(record.rid).card == TgAddress(111, 100, 55)
    assert _tap(env, 0) is None
    assert env.acks.is_acked(record.consent_identity) is True
    assert env.acks.get(record.consent_identity)["values_digest"] == record.values_digest


async def test_a_record_that_is_not_this_inspection_is_refused(env):
    record, _, _ = await _request()
    with pytest.raises(ValueError):
        _prompt(env, record, inspection=_inspection(version="0.2.0"))


async def test_approve_approves_the_record_and_carries_the_configurator_on(env):
    record, _, _ = await _request()
    await _prompt(env, record).settled_post()
    assert _tap(env, 0) is None
    await _settle()
    assert ar.get(record.rid).state == "approved"
    [s] = env.starts
    assert (s.continuation, s.key, s.approval.rid, s.approval.state, s.asked) == (
        CONTINUATION, record.rid, record.rid, "approved", "install mtg")
    assert env.channel.edits == []
    await s.on_outcome("started")
    assert env.channel.edits[-1] == (
        100, 55, "✅ Approved — the Configurator is carrying on with 'mtg'.")


@pytest.mark.parametrize("outcome", ["busy", "failed"])
async def test_a_continuation_that_could_not_start_keeps_the_approval(env, outcome):
    record, _, _ = await _request()
    await _prompt(env, record).settled_post()
    _tap(env, 0)
    await _settle()
    await env.starts[0].on_outcome(outcome)
    assert env.channel.edits[-1][2] == (
        "⚠️ Approved and saved — but the configurator could not be started. "
        "Ask Ellen again; the approval is kept.")
    assert ar.get(record.rid).state == "approved"


async def test_a_launcher_that_raises_keeps_the_approval_and_says_so(env, monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("no launcher")
    monkeypatch.setattr(configurator_continue, "start", _boom)
    record, _, _ = await _request()
    await _prompt(env, record).settled_post()
    _tap(env, 0)
    await _settle()
    assert ar.get(record.rid).state == "approved"
    assert env.channel.edits[-1][2].startswith("⚠️ Approved and saved — but the configurator")


async def test_approve_on_a_replaced_request_starts_nothing(env):
    record, _, _ = await _request()
    await _prompt(env, record).settled_post()
    # a newer request lands between the post and the tap; its poster has not
    # replaced this card yet
    await _request(values={"config": {"API_URL": "https://other.test", "LIMIT": 5},
                           "secrets": ["TOKEN"]})
    _tap(env, 0)
    await _settle()
    assert env.starts == []
    assert ar.get(record.rid).state == "superseded"
    assert env.channel.edits[-1][2] == "Replaced by a newer request; nothing was installed."


async def test_a_launcher_that_finds_the_approval_replaced_says_so(env):
    env.start_result["value"] = "superseded"
    record, _, _ = await _request()
    await _prompt(env, record).settled_post()
    _tap(env, 0)
    await _settle()
    assert env.channel.edits[-1][2] == "Replaced by a newer request; nothing was installed."


async def test_deny_denies_the_record(env):
    record, _, _ = await _request()
    await _prompt(env, record).settled_post()
    _tap(env, 1)
    await _settle()
    assert ar.get(record.rid).state == "denied"
    assert env.starts == []
    assert env.channel.edits[-1][2] == "❌ Denied — 'mtg' was not installed"


async def test_an_unanswered_card_expires_the_record(env):
    record, _, _ = await _request()
    await _prompt(env, record).settled_post()
    env.broker._on_timeout(("resident_ask", SCOPE, env.channel.posts[0][1]))
    await _settle()
    assert ar.get(record.rid).state == "expired"
    assert env.starts == []


async def test_a_card_replaced_before_it_was_posted_is_edited_replaced(env):
    """Request A is created, request B replaces it, then A's card is posted:
    A's card ends saying it was replaced, its question is settled, and a tap on
    it does nothing."""
    a, _, _ = await _request()
    b, outcome, prev = await _request(values={"config": {"API_URL": "https://b.test", "LIMIT": 5},
                                              "secrets": ["TOKEN"]})
    assert (outcome, prev.rid) == ("superseded_previous", a.rid)
    await _prompt(env, a).settled_post()
    await _settle()
    assert ar.get(a.rid).card == TgAddress(111, 100, 55)
    assert env.channel.edits == [(100, 55, "Replaced by a newer request; nothing was installed.")]
    assert env.broker.pending(namespace="resident_ask", scope=SCOPE) == []
    assert _tap(env, 0) == "stale"
    await _settle()
    assert env.starts == [] and ar.get(a.rid).state == "superseded"
    assert env.acks.is_acked(a.consent_identity) is False


async def test_supersede_card_replaces_a_card_still_open(env):
    a, _, _ = await _request()
    await _prompt(env, a).settled_post()
    _b, _, prev = await _request(values={"config": {"API_URL": "https://b.test", "LIMIT": 5},
                                         "secrets": ["TOKEN"]})
    await supersede_card(prev, channel=env.channel)
    await _settle()
    assert env.channel.edits == [(100, 55, "Replaced by a newer request; nothing was installed.")]
    assert env.broker.pending(namespace="resident_ask", scope=SCOPE) == []
    assert env.starts == []


async def test_supersede_card_edits_an_approved_card_whose_run_had_not_started(env):
    a, _, _ = await _request()
    await _prompt(env, a).settled_post()
    _tap(env, 0)
    await _settle()
    assert ar.get(a.rid).state == "approved"
    _b, _, prev = await _request(values={"config": {"API_URL": "https://b.test", "LIMIT": 5},
                                         "secrets": ["TOKEN"]})
    assert prev.state == "superseded" and prev.card == TgAddress(111, 100, 55)
    await supersede_card(prev, channel=env.channel)
    assert env.channel.edits[-1] == (100, 55, "Replaced by a newer request; nothing was installed.")


async def test_an_upgrade_card_is_worded_as_an_update(env):
    record, _, _ = await _request(kind="specialist_upgrade")
    await _prompt(env, record).settled_post()
    assert env.channel.posts[0][2].startswith("\U0001F510 Update specialist:mtg")
    _tap(env, 1)
    await _settle()
    assert env.channel.edits[-1][2] == "❌ Denied — 'mtg' was not updated"
