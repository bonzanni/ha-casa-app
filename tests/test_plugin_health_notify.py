"""§3.10 notify_plugin_health: operator DM on NEW fingerprints via a
DIRECT channel send; mark-notified ONLY on successful send (#342 — the
telegram bus queue is always registered and its outbound handler drops
silently without a channel, so queue presence never proved delivery)."""
from __future__ import annotations

import pytest

import casa_core
import plugin_health
from plugin_registry import PluginIssue

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


class _FakeChannel:
    def __init__(self, raise_on_send=False, is_ready=True):
        self.raise_on_send = raise_on_send
        self.is_ready = is_ready
        self.sent = []

    async def send(self, message, context):
        if not self.is_ready:
            return  # TelegramChannel.send log-and-drops when not started
        if self.raise_on_send:
            raise RuntimeError("telegram down")
        self.sent.append((message, context))


class _FakeChannelManager:
    def __init__(self, channel):
        self._channel = channel

    def get(self, name):
        return self._channel if name == "telegram" else None


def _cm(raise_on_send=False):
    return _FakeChannelManager(_FakeChannel(raise_on_send=raise_on_send))


def _report(tmp_path, *issues):
    p = tmp_path / "plugin-health.json"
    plugin_health.write_report(issues=list(issues), warnings=[], path=p)
    return p


async def test_new_fingerprints_send_and_mark(tmp_path):
    p = _report(tmp_path, PluginIssue("lesina-invoice", "specialist:finance",
                                      "resolve", "corrupt_artifact", "a" * 64))
    cm = _cm()
    await casa_core.notify_plugin_health(cm, path=str(p))
    assert len(cm._channel.sent) == 1
    assert "lesina-invoice" in cm._channel.sent[0][0]
    # Marked → no new fingerprints remain.
    assert plugin_health.new_fingerprints(plugin_health.load_report(p)) == []


async def test_already_notified_no_dm(tmp_path):
    p = _report(tmp_path, PluginIssue("p", "specialist:finance", "resolve",
                                      "artifact_missing", None))
    await casa_core.notify_plugin_health(_cm(), path=str(p))
    cm2 = _cm()
    await casa_core.notify_plugin_health(cm2, path=str(p))  # report unchanged
    assert cm2._channel.sent == []


async def test_send_failure_not_marked_retries(tmp_path):
    p = _report(tmp_path, PluginIssue("p", "specialist:finance", "resolve",
                                      "artifact_missing", None))
    await casa_core.notify_plugin_health(
        _cm(raise_on_send=True), path=str(p))
    # Not marked → a later (working) call still delivers.
    assert plugin_health.new_fingerprints(plugin_health.load_report(p))
    cm = _cm()
    await casa_core.notify_plugin_health(cm, path=str(p))
    assert len(cm._channel.sent) == 1


async def test_no_telegram_channel_defers(tmp_path):
    """#342: the gate is a REAL configured channel — with none, nothing is
    marked notified (retried once Telegram is configured later)."""
    p = _report(tmp_path, PluginIssue("p", "specialist:finance", "resolve",
                                      "artifact_missing", None))
    await casa_core.notify_plugin_health(
        _FakeChannelManager(None), path=str(p))
    # Not marked (no channel) → retried next time.
    assert plugin_health.new_fingerprints(plugin_health.load_report(p))


async def test_none_channel_manager_defers(tmp_path):
    p = _report(tmp_path, PluginIssue("p", "specialist:finance", "resolve",
                                      "artifact_missing", None))
    await casa_core.notify_plugin_health(None, path=str(p))
    assert plugin_health.new_fingerprints(plugin_health.load_report(p))


async def test_empty_report_is_noop(tmp_path):
    p = _report(tmp_path)          # no issues
    cm = _cm()
    await casa_core.notify_plugin_health(cm, path=str(p))
    assert cm._channel.sent == []


async def test_warning_only_change_fires_dm(tmp_path):
    """Sol #17: a warning-only report (e.g. legacy_provenance from offline-adopt)
    must fire the operator DM — new_fingerprints now spans warnings, and the DM
    body lists them (not a vacuous '0 items')."""
    path = tmp_path / "health.json"
    w = PluginIssue(name="lesina", target=None, stage="migration",
                    reason_code="legacy_provenance")
    plugin_health.write_report(issues=[], warnings=[w], path=path)
    report = plugin_health.load_report(path)
    assert len(plugin_health.new_fingerprints(report)) == 1

    cm = _cm()
    await casa_core.notify_plugin_health(cm, path=str(path))
    assert len(cm._channel.sent) == 1
    assert "legacy_provenance" in cm._channel.sent[0][0]
    # marked notified → second call is a no-op (deduped).
    await casa_core.notify_plugin_health(cm, path=str(path))
    assert len(cm._channel.sent) == 1


async def test_unready_channel_defers(tmp_path):
    """Sol r1-2: TelegramChannel.send returns NORMALLY when the app is not
    started (log-and-drop) — an unready channel must not consume the
    fingerprints."""
    p = _report(tmp_path, PluginIssue("p", "specialist:finance", "resolve",
                                      "artifact_missing", None))
    cm = _FakeChannelManager(_FakeChannel(is_ready=False))
    await casa_core.notify_plugin_health(cm, path=str(p))
    assert plugin_health.new_fingerprints(plugin_health.load_report(p))


async def test_telegram_channel_exposes_is_ready_contract():
    """The gate above relies on TelegramChannel.is_ready mirroring the
    send() availability guard (_app is None → log-and-drop)."""
    from channels.telegram import TelegramChannel
    assert isinstance(getattr(TelegramChannel, "is_ready", None), property)


async def test_dm_includes_detail_when_present(tmp_path):
    """#533: the operator DM names the missing values (the 0.153.0
    promise), not just a reason code."""
    p = _report(tmp_path, PluginIssue(
        "probe", None, "verify", "env_unresolved",
        detail="MY_API_KEY, OTHER_KEY"))
    cm = _cm()
    await casa_core.notify_plugin_health(cm, path=str(p))
    assert len(cm._channel.sent) == 1
    assert "probe (env_unresolved: MY_API_KEY, OTHER_KEY)" in cm._channel.sent[0][0]
