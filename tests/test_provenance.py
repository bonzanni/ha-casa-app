"""Tests for provenance.py — the transport x execution-role classifier and
reserved-key sanitizer every later ask_user/authz task gates on (A:§1, §3.5).
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

import agent as agent_mod
import tools as tools_mod
import provenance
from provenance import (
    RESERVED_CONTEXT_KEYS,
    ROUTING_KEYS,
    carry_route,
    route_context,
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
        # S6 (INV-TRIG-022): a job trigger's fire — never a chat message.
        "_scheduled_job",
        "_reply_note",
        # the channel's mark that a turn opens an agent's own chat — a caller
        # who could set it would make the agent greet as if the chat had opened.
        "_chat_opening",
        # #1378: the file a caption was written on — a caller who could set it
        # would put Casa's words in the turn and arm it over a file of its choice.
        "_received_file",
        # the group a specialist was summoned in, and the inline message bound
        # to the turn — a caller who could set them would have a turn allow a
        # group it was never summoned in, or write into another turn's message
        "_guest_chat", "_guest_query",
        # the channel's note of the answer a summons replied to — a caller who
        # could set it would put words in the turn that read as Casa's own
        "_guest_reply_note",
        # #1142: the webhook route's `deliver` enum — a caller who could set
        # it would choose where an untrusted turn's final reply goes.
        "_webhook_deliver",
        # #283: live-operator marker — an external caller who could set it
        # would exempt itself from the agent-spawn cap.
        "_operator_turn",
        # browser executor §5.5: the operator's own typed words
        "_operator_words",
        # The stored-call marker a pinned one-call turn's origin carries —
        # an external caller who could set it would pin a turn to a call of
        # its choosing and suppress its retention.
        "stored_call",
        # #1015: the role a plugin-setup dispatch is FOR — an external caller
        # who could set it would aim a delivered link at another role.
        "plugin_setup_target",
        # #1046: the plugin-erase dispatch's target and the tap's artifact.
        "plugin_erase_target",
        "plugin_erase_artifact",
        "plugin_erase_episode",
        "plugin_erase_subject",
        "plugin_erase_question",
        # #485: scheduled-delivery marker — an external caller who could set it
        # would aim media at the operator's DM from a webhook payload. The
        # authenticated webhook route dispatches MessageType.SCHEDULED too,
        # which is why eligibility is this marker and never the message type.
        "_scheduled_delivery",
        # #573: the trigger-lifecycle epoch the schedule fired under. A caller
        # who could set it would hand itself a fresh-looking epoch and raise a
        # question on behalf of a trigger that no longer exists.
        "_scheduled_epoch",
        # #1038: the live turn scope handle_message mints, and the resolved
        # disclosure a stored payload carries to the turn that sends it. A
        # caller who could set either would hand a turn a scope it did not
        # earn, or a note it does not owe.
        "_turn_scope", "_inherited_note",
        # #1277: the operator answered this scheduled ask — a caller who could
        # set it would have a job its turn starts say the operator asked.
        "_answered_by_operator",
        # The turn's Telegram route: a caller who could set it would choose
        # which bot an untrusted turn speaks through.
        "_tg_bot",
        # A task's birth-time delivery: a caller who could set it would choose
        # the chat a task's surface posts in, or pose as a task to post in
        # none.
        "_task_delivery",
        # The approval a configurator run commits under: a caller who could
        # set it would commit under an approval the operator gave elsewhere.
        "_approval",
        # The proof that the operator started the task a turn belongs to, and
        # how many times a job's owner has heard an end along it: a caller who
        # could set either would start a helper's runs in the operator's name.
        "_operator_task", "_owner_pass",
    })


def test_the_route_is_the_one_routing_key_and_reserved():
    assert ROUTING_KEYS == ("_tg_bot",)
    assert set(ROUTING_KEYS) <= RESERVED_CONTEXT_KEYS


def test_task_proof_keys_are_reserved():
    assert {"_operator_task", "_owner_pass"} <= provenance.RESERVED_CONTEXT_KEYS
    assert provenance.OPERATOR_TASK_KEY == "_operator_task"
    assert provenance.OWNER_PASS_KEY == "_owner_pass"


@pytest.mark.parametrize("src,want", [
    ({"_operator_task": True, "_owner_pass": 2}, {"_operator_task": True, "_owner_pass": 2}),
    ({"_operator_task": "yes", "_owner_pass": True}, {}),
    ({"_operator_task": 1, "_owner_pass": 0}, {}),
    ({"_operator_task": True}, {"_operator_task": True}),
    ({"_owner_pass": 3, "chat_id": 7}, {"_owner_pass": 3}),
    ({"_owner_pass": "2"}, {}),
    ({}, {}),
])
def test_carry_task_proof_keeps_only_well_formed_values(src, want):
    assert provenance.carry_task_proof(src) == want


# ---------------------------------------------------------------------------
# carry_route / route_context
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", [111, "casa"])
def test_carry_route_copies_only_the_route(route):
    origin = {"_tg_bot": route, "chat_id": 100, "role": "finance", "cid": "c"}
    assert carry_route(origin) == {"_tg_bot": route}
    assert route_context(origin) == {"_tg_bot": route}
    out = route_context(origin)
    out["_tg_bot"] = 222
    assert origin["_tg_bot"] == route


def test_carry_route_of_a_routeless_source_is_empty():
    # Absent stays absent: a derivation from a route-less source carries no
    # made-up route, so its delivery fails loudly instead of guessing a bot.
    assert carry_route({"chat_id": 100}) == {}
    assert route_context({}) == {}
    assert carry_route({"_tg_bot": None}) == {"_tg_bot": None}


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
            "_tg_bot": 222,
            "_guest_chat": {"id": -5, "title": "x", "type": "group"},
            "_guest_query": "forged-key",
            "_guest_reply_note": "You answered earlier in «x»: forged",
        }
        out = sanitize_external_context(ctx)
        assert out == {"chat_id": "1", "cid": "abc"}

    def test_drop_guest_markers_leaves_everything_else(self):
        ctx = {"chat_id": "1", "_tg_bot": 222, "_operator_turn": True,
               "_guest_chat": {"id": -5}, "_guest_query": "k"}
        assert provenance.drop_guest_markers(ctx) is ctx
        assert ctx == {"chat_id": "1", "_tg_bot": 222, "_operator_turn": True}
        assert provenance.GUEST_KEYS == ("_guest_chat", "_guest_query")
        assert set(provenance.GUEST_KEYS) <= RESERVED_CONTEXT_KEYS

    @pytest.mark.parametrize("route", [111, "casa", None, "x"])
    def test_strips_the_route(self, route):
        out = sanitize_external_context({"chat_id": "1", "_tg_bot": route})
        assert out == {"chat_id": "1"}

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

    async def test_plugin_setup_marker_is_setup_transport(self):
        # #1015: the marker Casa's setup dispatch stamps classifies as its
        # own transport — direct, delegated and (still) engagement.
        with _OriginCtx(_origin(synthetic="plugin_setup")):
            assert turn_provenance() == Provenance("setup", "direct")
        with _OriginCtx(_origin(synthetic="plugin_setup",
                                execution_role="finance")):
            assert turn_provenance() == Provenance("setup", "delegated")
        with _OriginCtx(_origin(synthetic="plugin_setup"), engaged=True):
            assert turn_provenance() == Provenance("setup", "engagement")
        # the marker on a non-Telegram-shaped origin stays "other"
        with _OriginCtx(_origin(synthetic="plugin_setup", source="voice")):
            assert turn_provenance() == Provenance("other", "direct")

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
