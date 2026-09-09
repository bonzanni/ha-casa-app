"""``tools.ack_event`` / ``tools.event_ack_revoke`` — the plugin-events tool
surface (#419).

INV-EV-005 (capability hygiene): the ack token is a bare bearer credential
for exactly one pending delivery. These tests pin that it never survives
into a tool reply or a log line, that a same-role second subscriber's
record is untouched by another subscriber's token, and that the revoke
tool's unroute-before-ack-delete ordering (owned by
``event_reconcile.revoke_and_unroute``) is observable end-to-end through
the tool.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import event_spool
from event_acks import EventAckStore
from plugin_events import ack_identity, subscribe_declaration_digest

EMITTER = "gmail"
EVENT = "mail_in"
SUBSCRIBER_A = "finance"
SUBSCRIBER_B = "billing"


def _payload(res: dict) -> dict:
    return json.loads(res["content"][0]["text"])


@pytest.fixture
def spool(tmp_path, monkeypatch):
    import event_spool as es
    sp = es.EventSpool(tmp_path / "events")
    monkeypatch.setattr(es, "_SPOOL", sp)
    yield sp
    sp.close()


def _seed(sp, *, emitter=EMITTER, event=EVENT,
         subscribers=(SUBSCRIBER_A,), now=1_000_000.0) -> dict:
    sp.ensure_emitter_dirs(emitter)
    event_spool.emit(sp.root / emitter, event)
    sp.fold_pass({(emitter, event): set(subscribers)}, now)
    return {sub: sp.read_delivery(emitter, event, sub) for sub in subscribers}


# ---------------------------------------------------------------------------
# ack_event — the three typed results
# ---------------------------------------------------------------------------


async def test_ack_event_acked_names_subscriber(spool):
    import tools
    recs = _seed(spool)
    token = recs[SUBSCRIBER_A]["ack_token"]
    res = await tools.ack_event.handler(
        {"emitter": EMITTER, "event": EVENT, "token": token})
    payload = _payload(res)
    assert payload["status"] == "ok"
    assert payload["outcome"] == "acked"
    assert payload["subscriber"] == SUBSCRIBER_A
    assert spool.read_delivery(EMITTER, EVENT, SUBSCRIBER_A)["outcome"] == "acked"


async def test_ack_event_already_done_is_quiet(spool):
    import tools
    recs = _seed(spool)
    token = recs[SUBSCRIBER_A]["ack_token"]
    first = await tools.ack_event.handler(
        {"emitter": EMITTER, "event": EVENT, "token": token})
    assert _payload(first)["outcome"] == "acked"

    res = await tools.ack_event.handler(
        {"emitter": EMITTER, "event": EVENT, "token": token})
    payload = _payload(res)
    assert payload["status"] == "ok"
    assert payload["outcome"] == "already_done"
    assert "subscriber" not in payload


async def test_ack_event_no_match_names_only_emitter_event(spool):
    import tools
    _seed(spool)
    res = await tools.ack_event.handler(
        {"emitter": EMITTER, "event": EVENT, "token": "bogus-token-value"})
    payload = _payload(res)
    assert payload["status"] == "error"
    assert payload["kind"] == "no_match"
    assert EMITTER in payload["message"]
    assert EVENT in payload["message"]
    assert "bogus-token-value" not in payload["message"]
    # no mutation on no_match
    rec = spool.read_delivery(EMITTER, EVENT, SUBSCRIBER_A)
    assert rec["status"] == "pending"


async def test_ack_event_invalid_arguments(spool):
    import tools
    res = await tools.ack_event.handler(
        {"emitter": "", "event": EVENT, "token": "x"})
    assert _payload(res)["status"] == "error"
    res2 = await tools.ack_event.handler(
        {"emitter": EMITTER, "event": EVENT, "token": ""})
    assert _payload(res2)["status"] == "error"


async def test_ack_event_no_spool_configured(monkeypatch):
    import tools
    monkeypatch.setattr(event_spool, "_SPOOL", None)
    res = await tools.ack_event.handler(
        {"emitter": EMITTER, "event": EVENT, "token": "x"})
    assert _payload(res)["status"] == "error"


# ---------------------------------------------------------------------------
# INV-EV-005 — the token never appears in a reply or a log line
# ---------------------------------------------------------------------------


async def test_ack_event_token_never_in_reply_or_logs(spool, caplog):
    import tools
    recs = _seed(spool)
    token = recs[SUBSCRIBER_A]["ack_token"]

    with caplog.at_level("DEBUG"):
        res = await tools.ack_event.handler(
            {"emitter": EMITTER, "event": EVENT, "token": token})
    assert token not in json.dumps(res)
    for record in caplog.records:
        assert token not in record.getMessage()

    caplog.clear()
    with caplog.at_level("DEBUG"):
        bad = await tools.ack_event.handler(
            {"emitter": EMITTER, "event": EVENT,
             "token": "another-secret-token-value"})
    assert "another-secret-token-value" not in json.dumps(bad)
    for record in caplog.records:
        assert "another-secret-token-value" not in record.getMessage()


# ---------------------------------------------------------------------------
# same-role two-subscriber isolation — token A acks only A
# ---------------------------------------------------------------------------


async def test_same_role_two_subscribers_token_isolation(spool):
    import tools
    recs = _seed(spool, subscribers=(SUBSCRIBER_A, SUBSCRIBER_B))
    token_a = recs[SUBSCRIBER_A]["ack_token"]
    assert token_a != recs[SUBSCRIBER_B]["ack_token"]

    res = await tools.ack_event.handler(
        {"emitter": EMITTER, "event": EVENT, "token": token_a})
    payload = _payload(res)
    assert payload["subscriber"] == SUBSCRIBER_A

    rec_a = spool.read_delivery(EMITTER, EVENT, SUBSCRIBER_A)
    rec_b = spool.read_delivery(EMITTER, EVENT, SUBSCRIBER_B)
    assert rec_a["status"] == "done" and rec_a["outcome"] == "acked"
    assert rec_b["status"] == "pending"        # B untouched by A's token


# ---------------------------------------------------------------------------
# event_ack_revoke — revoke + reconcile + health
# ---------------------------------------------------------------------------


class TestEventAckRevokeTool:
    async def test_revokes_and_reconciles(self, monkeypatch):
        import agent as agent_mod
        import event_reconcile
        import tools

        removed = [{"subscriber": SUBSCRIBER_A, "artifact_id": "art-1",
                    "emitter": EMITTER, "event": EVENT, "digest": "d",
                    "targets": ["resident:assistant"], "gen": "g", "ts": 1.0}]
        revoke_mock = AsyncMock(return_value=removed)
        reconcile_mock = AsyncMock(return_value=[])
        monkeypatch.setattr(event_reconcile, "revoke_and_unroute", revoke_mock)
        monkeypatch.setattr(event_reconcile, "reconcile_plugin_events",
                            reconcile_mock)
        monkeypatch.setattr(tools, "_regenerate_plugin_health", lambda x: None)
        cancels = []
        monkeypatch.setattr(tools.CHALLENGES, "cancel_matching",
                            lambda **k: cancels.append(k))
        monkeypatch.setattr(agent_mod, "active_runtime", SimpleNamespace(),
                            raising=False)

        res = await tools.event_ack_revoke.handler({"subscriber": SUBSCRIBER_A})
        payload = _payload(res)
        assert payload["ok"] is True
        assert payload["subscriber"] == SUBSCRIBER_A
        assert payload["revoked"] == 1
        assert payload["pairs"] == [[EMITTER, EVENT]]

        revoke_mock.assert_awaited_once_with(SUBSCRIBER_A, "", "")
        reconcile_mock.assert_awaited()
        assert reconcile_mock.await_args.kwargs.get("prompt") is False
        # pending keyboard cancelled before AND after the reconcile.
        assert cancels.count({"plugin": SUBSCRIBER_A}) == 2

    async def test_pair_scoped_revoke_passes_emitter_and_event(self, monkeypatch):
        import agent as agent_mod
        import event_reconcile
        import tools

        revoke_mock = AsyncMock(return_value=[])
        monkeypatch.setattr(event_reconcile, "revoke_and_unroute", revoke_mock)
        monkeypatch.setattr(event_reconcile, "reconcile_plugin_events",
                            AsyncMock(return_value=[]))
        monkeypatch.setattr(tools, "_regenerate_plugin_health", lambda x: None)
        monkeypatch.setattr(tools.CHALLENGES, "cancel_matching",
                            lambda **k: None)
        monkeypatch.setattr(agent_mod, "active_runtime", SimpleNamespace(),
                            raising=False)

        await tools.event_ack_revoke.handler(
            {"subscriber": SUBSCRIBER_A, "emitter": EMITTER, "event": EVENT})
        revoke_mock.assert_awaited_once_with(SUBSCRIBER_A, EMITTER, EVENT)

    async def test_unroutes_even_if_reconcile_fails(self, monkeypatch):
        import agent as agent_mod
        import event_reconcile
        import tools

        revoke_mock = AsyncMock(return_value=[])
        monkeypatch.setattr(event_reconcile, "revoke_and_unroute", revoke_mock)
        monkeypatch.setattr(
            event_reconcile, "reconcile_plugin_events",
            AsyncMock(side_effect=RuntimeError("resolver exploded")))
        monkeypatch.setattr(tools, "_regenerate_plugin_health", lambda x: None)
        monkeypatch.setattr(tools.CHALLENGES, "cancel_matching",
                            lambda **k: None)
        monkeypatch.setattr(agent_mod, "active_runtime", SimpleNamespace(),
                            raising=False)

        res = await tools.event_ack_revoke.handler({"subscriber": SUBSCRIBER_A})
        payload = _payload(res)
        assert payload["ok"] is True
        revoke_mock.assert_awaited_once()

    async def test_revoke_unroutes_before_ack_delete_end_to_end(
            self, monkeypatch, tmp_path):
        """The ordering guarantee is owned by
        ``event_reconcile.revoke_and_unroute`` — this proves the TOOL
        actually reaches it (not a bespoke, possibly-diverging unroute of
        its own), observable via a stub routed map.

        Uses the REAL ``event_episodes`` module (only ``kick_all`` is
        stubbed) rather than substituting the whole module with a
        throwaway ``SimpleNamespace`` (Important-4c, review round 1):
        swapping the module would swap ``DISPATCH_LOCK`` too, silently
        stopping this test from exercising the ACTUAL lock
        ``revoke_and_unroute`` and the worker's pre-send gate share."""
        import agent as agent_mod
        import event_episodes
        import event_reconcile
        import tools

        acks = EventAckStore(path=tmp_path / "acks.json")
        digest = subscribe_declaration_digest({"plugin": EMITTER, "event": EVENT})
        acks.record(SUBSCRIBER_A, "art-1", EMITTER, EVENT, digest,
                   ["resident:assistant"], 1.0)
        identity = ack_identity(SUBSCRIBER_A, "art-1", EMITTER, EVENT, digest,
                               ["resident:assistant"])

        monkeypatch.setattr(event_reconcile, "_default_acks", lambda: acks)
        monkeypatch.setattr(event_reconcile, "_routed", {
            (EMITTER, EVENT): {SUBSCRIBER_A: {
                "subscriber": SUBSCRIBER_A, "artifact_id": "art-1",
                "targets": ["resident:assistant"], "ack_identity": identity}}})

        monkeypatch.setattr(event_episodes, "kick_all", lambda: None)
        monkeypatch.setattr(event_reconcile, "reconcile_plugin_events",
                            AsyncMock(return_value=[]))
        monkeypatch.setattr(tools, "_regenerate_plugin_health", lambda x: None)
        monkeypatch.setattr(tools.CHALLENGES, "cancel_matching",
                            lambda **k: None)
        monkeypatch.setattr(agent_mod, "active_runtime", SimpleNamespace(),
                            raising=False)

        observed = {}
        real_revoke = acks.revoke_subscriber

        def spy(subscriber):
            routed = event_reconcile.get_routed()
            observed["still_routed"] = subscriber in (
                routed.get((EMITTER, EVENT)) or {})
            return real_revoke(subscriber)

        acks.revoke_subscriber = spy

        res = await tools.event_ack_revoke.handler({"subscriber": SUBSCRIBER_A})
        assert observed["still_routed"] is False
        payload = _payload(res)
        assert payload["ok"] is True
        assert acks.get(identity) is None

    async def test_registered_in_casa_tools(self):
        import tools
        names = {t.name for t in tools.CASA_TOOLS}
        assert "event_ack_revoke" in names
        assert "ack_event" in names

    async def test_revoke_pairs_guard_none_fields(self, monkeypatch):
        """Minor-9 (review round 1): a malformed removed-record's
        emitter/event must never crash the ``pairs`` sort."""
        import agent as agent_mod
        import event_reconcile
        import tools

        monkeypatch.setattr(
            event_reconcile, "revoke_and_unroute",
            AsyncMock(return_value=[
                {"subscriber": SUBSCRIBER_A, "emitter": None, "event": None},
                {"subscriber": SUBSCRIBER_A, "emitter": EMITTER, "event": EVENT},
            ]))
        monkeypatch.setattr(event_reconcile, "reconcile_plugin_events",
                            AsyncMock(return_value=[]))
        monkeypatch.setattr(tools, "_regenerate_plugin_health", lambda x: None)
        monkeypatch.setattr(tools.CHALLENGES, "cancel_matching",
                            lambda **k: None)
        monkeypatch.setattr(agent_mod, "active_runtime", SimpleNamespace(),
                            raising=False)

        res = await tools.event_ack_revoke.handler({"subscriber": SUBSCRIBER_A})
        payload = _payload(res)
        assert payload["ok"] is True
        assert payload["revoked"] == 2
        assert ["", ""] in payload["pairs"]
        assert [EMITTER, EVENT] in payload["pairs"]


# ---------------------------------------------------------------------------
# Minor-10 — event issues surface into the health report as dicts
# ---------------------------------------------------------------------------


def test_event_current_issues_shape_is_health_report_compatible(tmp_path):
    """Minor-10 (review round 1): ``event_reconcile.current_issues()``
    rows are DICTS, never ``PluginIssue`` objects — proves
    ``plugin_health``'s ``fingerprint``/``write_report`` already handle
    that shape correctly (dict/attribute dual accessor), so whenever
    Task 10 wires this merge in (concatenated DIRECTLY, per
    ``current_issues()``'s own docstring — never through
    ``tools._regenerate_plugin_health``'s ``PluginIssue``-attribute-only
    ``_add``/``_rediscoverable`` helpers, which would silently degrade a
    dict row's fields to ``None`` instead of raising), the report already
    renders it correctly end-to-end. Deliberately does NOT call
    ``tools._regenerate_plugin_health`` — wiring the merge into that
    function is Task 10's scope and touches every OTHER test that calls it
    without mocking this module."""
    import event_reconcile
    import plugin_health

    event_row = event_reconcile._issue("finance", "event_pending_ack",
                                       "art-1")
    assert isinstance(event_row, dict)

    # fingerprint/serialization never crash and never silently degrade to
    # a (None, None, None) row.
    fp = plugin_health.fingerprint(event_row)
    assert fp and isinstance(fp, str)

    report = plugin_health.write_report(
        issues=[event_row], warnings=[], path=tmp_path / "health.json",
        registry_path=tmp_path / "registry.json")
    assert [i["reason_code"] for i in report["issues"]] == ["event_pending_ack"]
    assert report["issues"][0]["name"] == "finance"
    assert report["issues"][0]["fingerprint"] == fp

