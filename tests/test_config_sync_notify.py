"""Operator-notification tests: config-sync report → Casa's own Telegram
endpoint.

The overwrite heads-up is sent directly (deterministic operator delivery),
not through an LLM turn, and the report is marked notified only once the
notice was delivered (#1653).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import casa_core
from channels import ChannelManager, DeliveryOutcome

pytestmark = pytest.mark.unit


class _Endpoint:
    """Casa's endpoint: records each delivered notice, answers *outcome*."""
    def __init__(self, *, ready: bool = True,
                 outcome: DeliveryOutcome = DeliveryOutcome.DELIVERED) -> None:
        self.is_ready = ready
        self.outcome = outcome
        self.messages: list[str] = []

    async def send(self, text, context) -> DeliveryOutcome:
        if self.outcome is not DeliveryOutcome.NOT_DELIVERED:
            self.messages.append(text.text)
        return self.outcome


class _Transport:
    def __init__(self, endpoint) -> None:
        self._endpoint = endpoint

    def casa_endpoint(self):
        return self._endpoint

    def operator_user_id(self):
        return None


class _Manager:
    """A channel manager holding Telegram, or none when *endpoint* is None."""
    def __init__(self, endpoint=None) -> None:
        self._transport = _Transport(endpoint) if endpoint is not None else None

    def get(self, name):
        return self._transport if name == "telegram" else None


def _notified(path) -> bool:
    return bool(json.loads(Path(path).read_text()).get("notified"))


def _report(path: Path, **over) -> None:
    base = {
        "image_version": "v1.2.3", "pre_sync_sha": "abc",
        "updated": [], "deleted": [], "conflicts": [],
        "schema_forced": [], "casabak": [], "notified": False,
    }
    base.update(over)
    path.write_text(json.dumps(base), encoding="utf-8")


async def test_notify_posts_to_telegram_when_overwrites_present(tmp_path: Path) -> None:
    rp = tmp_path / "config-sync-report.json"
    _report(rp, conflicts=[{"path": "agents/butler/voice.yaml", "pre_sync_sha": "abc"}])
    ep = _Endpoint()
    await casa_core.notify_config_sync(_Manager(ep), report_path=str(rp))
    assert len(ep.messages) == 1
    assert "agents/butler/voice.yaml" in ep.messages[0]
    # report marked notified to prevent duplicate on svc restart
    assert _notified(rp)


async def test_no_notify_when_no_overwrites(tmp_path: Path) -> None:
    rp = tmp_path / "config-sync-report.json"
    _report(rp, updated=["agents/butler/voice.yaml"])  # routine only
    ep = _Endpoint()
    await casa_core.notify_config_sync(_Manager(ep), report_path=str(rp))
    assert ep.messages == []


async def test_no_notify_when_already_notified(tmp_path: Path) -> None:
    rp = tmp_path / "config-sync-report.json"
    _report(rp, conflicts=[{"path": "x", "pre_sync_sha": "y"}], notified=True)
    ep = _Endpoint()
    await casa_core.notify_config_sync(_Manager(ep), report_path=str(rp))
    assert ep.messages == []


async def test_no_notify_when_report_absent(tmp_path: Path) -> None:
    ep = _Endpoint()
    await casa_core.notify_config_sync(
        _Manager(ep), report_path=str(tmp_path / "missing.json"))
    assert ep.messages == []


async def test_without_telegram_the_notice_stays_owed(tmp_path: Path) -> None:
    # Telegram not configured: nothing is sent and nothing is marked, so the
    # operator is told at the first boot that has Telegram (as plugin health).
    rp = tmp_path / "config-sync-report.json"
    _report(rp, conflicts=[{"path": "x", "pre_sync_sha": "y"}])
    await casa_core.notify_config_sync(_Manager(None), report_path=str(rp))
    assert not _notified(rp)
    ep = _Endpoint()
    await casa_core.notify_config_sync(_Manager(ep), report_path=str(rp))
    assert len(ep.messages) == 1 and _notified(rp)


# --- #1653: marked only when delivered -------------------------------------


def _unstarted_casa_endpoint(tmp_path):
    """Casa's real Telegram endpoint as a failed first bring-up leaves it:
    registered, never started, with its reconnect supervisor."""
    from channels.telegram import TelegramChannel
    from bus import MessageBus
    cm = ChannelManager()
    transport, endpoint = casa_core._build_telegram_transport(
        token="123456789:" + "A" * 35, store_path=tmp_path / "bot.json",
        make_endpoint=TelegramChannel, chat_id="42",
        default_agent="assistant", bus=MessageBus())
    cm.register(transport)
    return cm, transport, endpoint


async def test_a_notice_telegram_could_not_take_is_sent_at_the_next_boot(
        tmp_path):
    rp = tmp_path / "config-sync-report.json"
    _report(rp, conflicts=[{"path": "agents/assistant/runtime.yaml"}])
    cm, _, endpoint = _unstarted_casa_endpoint(tmp_path)
    assert not endpoint.is_ready
    await casa_core.notify_config_sync(cm, report_path=str(rp))
    assert not _notified(rp)
    # the next boot, Telegram fine: the operator is told
    ep = _Endpoint()
    await casa_core.notify_config_sync(_Manager(ep), report_path=str(rp))
    assert len(ep.messages) == 1
    assert "agents/assistant/runtime.yaml" in ep.messages[0]
    assert _notified(rp)


async def test_a_send_telegram_refused_is_not_marked(tmp_path):
    rp = tmp_path / "config-sync-report.json"
    _report(rp, conflicts=[{"path": "x"}])
    ep = _Endpoint(outcome=DeliveryOutcome.NOT_DELIVERED)
    await casa_core.notify_config_sync(_Manager(ep), report_path=str(rp))
    assert not _notified(rp)


async def test_the_boot_notice_waits_for_telegram_to_come_up(tmp_path):
    """Boot hands the notice to the ready wait and goes on; once Casa's
    endpoint comes up the notice is delivered and only then marked."""
    rp = tmp_path / "config-sync-report.json"
    _report(rp, conflicts=[{"path": "agents/assistant/runtime.yaml"}])
    ep = _Endpoint(ready=False)
    cm = _Manager(ep)
    ready = asyncio.Event()
    casa_core._after_telegram_ready(
        ready, lambda: casa_core.notify_config_sync(cm, report_path=str(rp)),
        "config sync notice")
    for _ in range(5):
        await asyncio.sleep(0)
    assert ep.messages == [] and not _notified(rp)
    ep.is_ready = True
    ready.set()
    for _ in range(20):
        if _notified(rp):
            break
        await asyncio.sleep(0)
    assert len(ep.messages) == 1 and _notified(rp)


async def test_the_memory_outcome_line_stays_owed_until_delivered(
        tmp_path, monkeypatch):
    from memory import maintenance
    marked: list = []
    monkeypatch.setattr(maintenance, "mark_outcome_notice_sent", marked.append)
    store = object()
    _, _, endpoint = _unstarted_casa_endpoint(tmp_path)
    await casa_core._send_maintenance_line(endpoint, store, "Memory wiped.")
    assert marked == []

    class _Up:
        def __init__(self):
            self.lines = []

        async def notify_operator(self, text):
            self.lines.append(text)
            return True

    up = _Up()
    await casa_core._send_maintenance_line(up, store, "Memory wiped.")
    assert up.lines == ["Memory wiped."] and marked == [store]


# --- #398: entry-level reconcile outcomes reach the operator ---------------


def _report_398(tmp_path, **fields):
    p = tmp_path / "report.json"
    p.write_text(json.dumps({"image_version": "v0.149.0", **fields}),
                 encoding="utf-8")
    return str(p)


async def test_notifies_when_a_merge_displaced_a_local_entry(tmp_path):
    ep = _Endpoint()
    path = _report_398(tmp_path, merged=[{
        "path": "agents/assistant/triggers.yaml",
        "displaced_local": ["garbage-reminder"], "conflicted": [],
        "reinserted": [], "kept_local": [], "tracked_image": [],
        "deleted": [], "pre_sync_sha": "SHA1"}])
    await casa_core.notify_config_sync(_Manager(ep), report_path=path)
    assert ep.messages, "a displaced local entry must reach the operator"
    assert "agents/assistant/triggers.yaml" in ep.messages[0]


async def test_notifies_when_entries_were_dropped(tmp_path):
    ep = _Endpoint()
    path = _report_398(tmp_path, entries_dropped=[{
        "path": "agents/assistant/triggers.yaml", "names": ["bad"],
        "reason": "invalid against the current schema"}])
    await casa_core.notify_config_sync(_Manager(ep), report_path=path)
    assert ep.messages
    assert "agents/assistant/triggers.yaml" in ep.messages[0]


async def test_a_purely_additive_merge_does_not_notify(tmp_path):
    """The ordinary case after #398: the image's entries arrive, the
    operator's survive, nothing was lost. Alerting here would train the
    operator to ignore the alert."""
    ep = _Endpoint()
    path = _report_398(tmp_path, merged=[{
        "path": "agents/assistant/triggers.yaml",
        "displaced_local": [], "conflicted": [], "reinserted": [],
        "kept_local": ["garbage-reminder"], "tracked_image": ["heartbeat"],
        "deleted": [], "pre_sync_sha": None}])
    await casa_core.notify_config_sync(_Manager(ep), report_path=path)
    assert not ep.messages


async def test_a_path_is_listed_once_even_when_several_outcomes_name_it(tmp_path):
    ep = _Endpoint()
    rel = "agents/assistant/triggers.yaml"
    path = _report_398(
        tmp_path,
        casabak=[rel],
        entries_dropped=[{"path": rel, "names": ["bad"], "reason": "r"}],
        merged=[{"path": rel, "displaced_local": ["mine"], "conflicted": [],
                 "reinserted": [], "kept_local": [], "tracked_image": [],
                 "deleted": [], "pre_sync_sha": "SHA1"}])
    await casa_core.notify_config_sync(_Manager(ep), report_path=path)
    assert ep.messages
    assert ep.messages[0].count(rel) == 1


async def test_two_concurrent_callers_send_the_heads_up_once(
        tmp_path, monkeypatch):
    """#1688: notify_config_sync holds the operator notice lock and reads the
    report after acquiring it, so a caller queued behind a send in flight
    finds the report marked and does not send it again."""
    monkeypatch.setattr(casa_core, "_OPERATOR_NOTICE_LOCK", asyncio.Lock())
    rp = tmp_path / "config-sync-report.json"
    _report(rp, conflicts=[{"path": "agents/assistant/runtime.yaml"}])
    gate = asyncio.Event()

    class _HeldEndpoint(_Endpoint):
        async def send(self, text, context):
            self.messages.append(text.text)
            await gate.wait()
            return DeliveryOutcome.DELIVERED

    ep = _HeldEndpoint()
    first = asyncio.create_task(
        casa_core.notify_config_sync(_Manager(ep), report_path=str(rp)))
    while not ep.messages:
        await asyncio.sleep(0)
    second = asyncio.create_task(
        casa_core.notify_config_sync(_Manager(ep), report_path=str(rp)))
    for _ in range(5):
        await asyncio.sleep(0)       # let the second caller run to its await
    gate.set()
    await asyncio.gather(first, second)

    assert len(ep.messages) == 1
    assert _notified(rp)
