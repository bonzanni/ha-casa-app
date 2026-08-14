"""Tests for provenance.py — the transport x execution-role classifier and
reserved-key sanitizer every later ask_user/authz task gates on (A:§1, §3.5).
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

import agent as agent_mod
import tools as tools_mod
from provenance import (
    RESERVED_CONTEXT_KEYS,
    Provenance,
    sanitize_external_context,
    strict_positive_id,
    turn_provenance,
)


# ---------------------------------------------------------------------------
# RESERVED_CONTEXT_KEYS
# ---------------------------------------------------------------------------


def test_reserved_context_keys_are_exactly_the_spec_set():
    assert RESERVED_CONTEXT_KEYS == frozenset({
        "synthetic", "button_answer", "execution_role",
        "message_type", "source",
        "_voice_route_id", "_voice_route_capabilities",
        "_voice_job_control_id",
        "_origin_device_id", "_voice_transport",
        "_voice_handoff_reservation",
        # #233/#224: the per-utterance delivery offer decides whether Casa
        # promises a deferred answer and by which modality. It is stamped from
        # the authenticated route AFTER sanitization; if an external caller
        # could set it, a client could manufacture a capability it lacks.
        "_voice_delivery_offer",
        # Release A: unspoofable server-set webhook-origin markers.
        "_origin_route", "_origin_clearance",
        # #283: live-operator marker — an external caller who could set it
        # would exempt itself from the agent-spawn cap.
        "_operator_turn",
        # #485: scheduled-delivery marker — an external caller who could set it
        # would aim media at the operator's DM from a webhook payload. The
        # authenticated webhook route dispatches MessageType.SCHEDULED too,
        # which is why eligibility is this marker and never the message type.
        "_scheduled_delivery",
    })


# ---------------------------------------------------------------------------
# sanitize_external_context
# ---------------------------------------------------------------------------


class TestSanitizeExternalContext:
    def test_none_normalizes_to_empty_dict(self):
        assert sanitize_external_context(None) == {}

    def test_empty_dict_stays_empty(self):
        assert sanitize_external_context({}) == {}

    def test_non_dict_normalizes_to_empty_dict(self):
        # #287 r3 (Terra): "context" is caller-controlled on the SSE body,
        # the WS utterance frame AND the /invoke payload; a truthy non-dict
        # ([1], "x", 42) reached .items() and crashed the handler after the
        # stream was already prepared.
        for bad in ([1], "x", 42, [["nested"]]):
            assert sanitize_external_context(bad) == {}

    def test_strips_exactly_the_reserved_keys(self):
        ctx = {
            "chat_id": "1", "cid": "abc",
            "synthetic": "button", "button_answer": "yes",
            "execution_role": "butler", "message_type": "channel_in",
            "source": "telegram",
            "_voice_route_id": "spoofed-entry",
            "_voice_route_capabilities": ["background_jobs"],
            "_voice_job_control_id": "spoofed-control",
            "_origin_device_id": "spoofed-device",
            "_voice_transport": "ws",
            "_voice_handoff_reservation": Mock(),
        }
        out = sanitize_external_context(ctx)
        assert out == {"chat_id": "1", "cid": "abc"}

    def test_non_reserved_keys_pass_through_untouched(self):
        ctx = {"chat_id": "1", "device_id": "d1", "conversation_id": "c1"}
        assert sanitize_external_context(ctx) == ctx

    def test_returns_a_copy_not_the_same_object(self):
        ctx = {"chat_id": "1"}
        out = sanitize_external_context(ctx)
        assert out == ctx
        assert out is not ctx
        out["chat_id"] = "mutated"
        assert ctx["chat_id"] == "1"


# ---------------------------------------------------------------------------
# strict_positive_id
# ---------------------------------------------------------------------------


class TestStrictPositiveId:
    @pytest.mark.parametrize("value,expected", [
        (True, None),
        (False, None),
        ("12x", None),
        (-5, None),
        ("-5", None),
        (0, None),
        ("0", None),
        (7, 7),
        ("7", 7),
        (7.0, None),
        (None, None),
        ("", None),
        ("  7", None),
    ])
    def test_truth_table(self, value, expected):
        assert strict_positive_id(value) == expected

    def test_mock_is_malformed(self):
        assert strict_positive_id(Mock()) is None


# ---------------------------------------------------------------------------
# turn_provenance — Provenance dataclass shape
# ---------------------------------------------------------------------------


def test_provenance_is_frozen_dataclass():
    p = Provenance(transport="dm", execution="direct")
    assert p.transport == "dm"
    assert p.execution == "direct"
    with pytest.raises(Exception):
        p.transport = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# turn_provenance — origin construction helper
# ---------------------------------------------------------------------------


def _origin(**overrides) -> dict:
    base = {
        "role": "assistant",
        "channel": "telegram",
        "chat_id": "42",
        "user_id": 100,
        "cid": "abc",
        "user_text": "hi",
        "message_type": "channel_in",
        "source": "telegram",
        "execution_role": "assistant",
    }
    base.update(overrides)
    return base


class _OriginCtx:
    """Sets agent.origin_var (and optionally tools.engagement_var) for the
    duration of the `with` block, restoring both on exit."""

    def __init__(self, origin: dict | None, *, engaged: bool = False):
        self._origin = origin
        self._engaged = engaged

    def __enter__(self):
        self._origin_token = agent_mod.origin_var.set(self._origin)
        eng = Mock(name="EngagementRecord") if self._engaged else None
        self._eng_token = tools_mod.engagement_var.set(eng)
        return self

    def __exit__(self, *exc):
        agent_mod.origin_var.reset(self._origin_token)
        tools_mod.engagement_var.reset(self._eng_token)


# ---------------------------------------------------------------------------
# turn_provenance — missing origin
# ---------------------------------------------------------------------------


async def test_missing_origin_classifies_other_direct():
    with _OriginCtx(None):
        p = turn_provenance()
    assert p == Provenance(transport="other", execution="direct")


# ---------------------------------------------------------------------------
# turn_provenance — full transport x execution cross-product
# ---------------------------------------------------------------------------


class TestTransportClassification:
    async def test_dm_direct(self):
        with _OriginCtx(_origin()):
            p = turn_provenance()
        assert p == Provenance(transport="dm", execution="direct")

    async def test_button_direct(self):
        with _OriginCtx(_origin(synthetic="button")):
            p = turn_provenance()
        assert p == Provenance(transport="button", execution="direct")

    async def test_dm_delegated(self):
        origin = _origin(execution_role="butler")
        with _OriginCtx(origin):
            p = turn_provenance()
        assert p == Provenance(transport="dm", execution="delegated")

    async def test_button_delegated(self):
        origin = _origin(synthetic="button", execution_role="butler")
        with _OriginCtx(origin):
            p = turn_provenance()
        assert p == Provenance(transport="button", execution="delegated")

    async def test_dm_engagement(self):
        with _OriginCtx(_origin(), engaged=True):
            p = turn_provenance()
        assert p == Provenance(transport="dm", execution="engagement")

    async def test_button_engagement(self):
        with _OriginCtx(_origin(synthetic="button"), engaged=True):
            p = turn_provenance()
        assert p == Provenance(transport="button", execution="engagement")

    async def test_other_direct_voice_channel(self):
        origin = _origin(channel="voice", source="voice", message_type="request")
        with _OriginCtx(origin):
            p = turn_provenance()
        assert p == Provenance(transport="other", execution="direct")

    async def test_other_delegated_voice_channel(self):
        origin = _origin(
            channel="voice", source="voice", message_type="request",
            execution_role="butler",
        )
        with _OriginCtx(origin):
            p = turn_provenance()
        assert p == Provenance(transport="other", execution="delegated")

    async def test_other_engagement(self):
        origin = _origin(channel="voice", source="voice", message_type="request")
        with _OriginCtx(origin, engaged=True):
            p = turn_provenance()
        assert p == Provenance(transport="other", execution="engagement")

    async def test_wrong_source_is_other(self):
        """channel=='telegram' but source spoofed/mismatched -> other."""
        origin = _origin(source="webhook")
        with _OriginCtx(origin):
            p = turn_provenance()
        assert p.transport == "other"

    async def test_missing_source_is_other(self):
        origin = _origin()
        del origin["source"]
        with _OriginCtx(origin):
            p = turn_provenance()
        assert p.transport == "other"

    async def test_wrong_message_type_is_other(self):
        origin = _origin(message_type="scheduled")
        with _OriginCtx(origin):
            p = turn_provenance()
        assert p.transport == "other"

    async def test_malformed_chat_id_is_other(self):
        origin = _origin(chat_id="not-a-number")
        with _OriginCtx(origin):
            p = turn_provenance()
        assert p.transport == "other"

    async def test_malformed_user_id_is_other(self):
        origin = _origin(user_id=Mock())
        with _OriginCtx(origin):
            p = turn_provenance()
        assert p.transport == "other"

    async def test_zero_chat_id_is_other(self):
        origin = _origin(chat_id="0")
        with _OriginCtx(origin):
            p = turn_provenance()
        assert p.transport == "other"

    async def test_negative_user_id_is_other(self):
        origin = _origin(user_id=-5)
        with _OriginCtx(origin):
            p = turn_provenance()
        assert p.transport == "other"

    async def test_unrecognized_marker_is_other(self):
        """A marker present but not 'button' fails both the dm ('no
        marker') and button ('== "button"') conditions."""
        origin = _origin(synthetic="something-else")
        with _OriginCtx(origin):
            p = turn_provenance()
        assert p.transport == "other"

    async def test_engagement_takes_priority_over_delegated(self):
        """Both engagement_var set AND execution_role != role — engagement
        wins per spec ordering."""
        origin = _origin(execution_role="butler")
        with _OriginCtx(origin, engaged=True):
            p = turn_provenance()
        assert p.execution == "engagement"
