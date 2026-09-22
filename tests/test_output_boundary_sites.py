"""The closed set, kept closed by recorded lists (#1038 §4.2, §4.3, INV-OUT-001).

Three enumerations over the code root, each compared with a list recorded
HERE with a classification per entry — the discipline
``test_scheduled_prompt_guidance.py`` applies to prompt surfaces. A new site
fails CI until someone records it and, in doing so, answers the one question
the boundary asks: who authored the words?

* every ``casa_text(`` call site — Casa-composed text (a template with
  controlled inputs; a template that interpolates model text is NOT one);
* every ``edit_dm_message(`` caller — a *derived* re-render of a body that was
  admitted when posted, a Casa *status* that replaces it, or a *plugin*-authored
  consent body;
* every raw Bot API text call (``bot.send_message`` / ``edit_message_text`` /
  media) by enclosing function — the function takes an ``Admitted``, is a
  derived edit, is a Casa notice, streams what ``_emit`` admitted, or is a
  sequencer-owned topic method.

These catch an accidental bypass. They are not a sandbox against Casa
deliberately fabricating admission (design §4.3).
"""
from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

pytestmark = [pytest.mark.unit]

CODE = pathlib.Path(__file__).resolve().parents[1] / "casa" / "rootfs" / "opt" / "casa"
RAW_TEXT_CALLS = {"send_message", "edit_message_text", "send_document", "send_photo",
                  "send_audio", "send_voice"}


def _enclosing(tree: ast.AST, node: ast.AST) -> str | None:
    best = None
    for p in ast.walk(tree):
        if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                n is node for n in ast.walk(p)):
            if best is None or p.lineno > best.lineno:
                best = p
    return best.name if best else None


def _scan():
    casa, edits, raw = set(), set(), set()
    for path in sorted(CODE.rglob("*.py")):
        rel = path.relative_to(CODE).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = getattr(f, "id", None) or getattr(f, "attr", None)
            if name == "casa_text":
                casa.add((rel, _enclosing(tree, node)))
            elif name == "edit_dm_message" and isinstance(f, ast.Attribute):
                edits.add((rel, _enclosing(tree, node)))
            elif name in RAW_TEXT_CALLS and isinstance(f, ast.Attribute):
                recv = f.value
                if (getattr(recv, "attr", None) or getattr(recv, "id", None)) == "bot":
                    raw.add((rel, _enclosing(tree, node)))
    return casa, edits, raw


# Recorded lists. The value says why the site is what it is.

CASA_TEXT_SITES = {
    ("agent.py", "handle_message"): "a classified-error reply (_USER_MESSAGES) is Casa's",
    ("authz_grants.py", "_post"): "a challenge raised with NO turn bound (a setup dispatch); with a turn it is admitted",
    ("casa_core.py", "_notify_plugin_health_locked"): "plugin-health notice, Casa template",
    ("casa_core.py", "notify_placeholder_rewrites"): "placeholder-rewrite notice, Casa template",
    ("casa_core.py", "_sweep_engagement_topics"): "topic-permission notice, Casa template",
    ("casa_core.py", "_telegram_outbound"): "bus target for Casa notices only; a model reply never reaches it",
    ("casa_core.py", "operator_notify"): "operator notice, Casa template",
    ("tools.py", "_post"): "wipe_memory's confirmation keyboard, Casa template",
}

EDIT_DM_MESSAGE_CALLERS = {
    ("tools.py", "_finish"): {"derived", "status"},                 # ask_user: answered/retired body; 'delivery failed' status
    ("tools.py", "_run_wipe_and_report"): {"status"},              # wipe_memory: Casa report text
    ("scheduled_asks.py", "_settle"): {"derived"},                 # the stored admitted body + terminal suffix
    ("scheduled_asks.py", "_replay_terminal_edit"): {"derived"},   # the persisted terminal_edit of that body
    ("authz_grants.py", "_finish_inner"): {"status"},              # retired / denied / delivery-failed statuses
    ("callback_consent.py", "_finish"): {"plugin"},                # consent bodies are plugin-authored
    ("event_consent.py", "_finish"): {"plugin"},
    ("persona_install_consent.py", "_finish_inner"): {"plugin"},
    ("specialist_install_consent.py", "_finish_inner"): {"plugin"},
    ("trigger_consent.py", "_finish"): {"plugin"},
}

RAW_TRANSPORT_FUNCTIONS = {
    # admitted — the function takes an Admitted (INV-OUT-001)
    ("channels/telegram.py", "send"): "admitted",
    ("channels/telegram.py", "send_response"): "admitted",
    ("channels/telegram.py", "finalize_response_stream"): "admitted",
    ("channels/telegram.py", "finalize_stream"): "admitted",
    ("channels/telegram.py", "post_dm_keyboard"): "admitted",
    ("channels/telegram.py", "_send_one"): "admitted",             # helper of the two above
    ("channels/telegram.py", "_send_overflow_line"): "admitted",   # the admission's lines as their own message (§6.6 as amended)
    # stream — fed only from agent._emit, which admits every cumulative
    ("channels/telegram.py", "_stream_token"): "stream",
    # derived — a re-render of an admitted body
    ("channels/telegram.py", "edit_dm_message"): "derived",
    # notice — Casa-composed
    ("channels/telegram.py", "_handle_serialized"): "notice",       # routing / session notices
    ("channels/telegram.py", "_maybe_redirect_main_feed"): "notice",
    ("channels/telegram.py", "_send_rate_limit_reply"): "notice",
    ("channels/telegram.py", "reply"): "notice",                    # #1036 arrival replies
    # topic — sequencer-owned engagement-topic methods (out of this boundary)
    ("channels/telegram.py", "edit_topic_message"): "topic",
    ("channels/telegram.py", "edit_topic_message_markup"): "topic",
    ("channels/telegram.py", "edit_topic_message_rich"): "topic",
    ("channels/telegram.py", "emit"): "topic",                      # TopicStreamHandle
    ("channels/telegram.py", "finalize"): "topic",                  # TopicStreamHandle
    ("channels/telegram.py", "post_ask_body_rich"): "topic",
    ("channels/telegram.py", "send_response_to_topic"): "topic",
    ("channels/telegram.py", "send_to_topic"): "topic",
    ("channels/telegram.py", "send_to_topic_rich"): "topic",
    ("channels/telegram.py", "send_topic_message_markup"): "topic",
}


CHANNEL_METHODS = {"send", "send_response", "finalize_stream", "finalize_response_stream",
                   "post_dm_keyboard", "send_media"}
# (file, enclosing function) → what the call hands the method. "admitted" = a value
# minted by TurnScope.admit or carried from one; "casa_text" = the Casa template
# class; "forward" = the channel forwarding an already-admitted value to a sibling
# method. Receivers that are not a channel (the bus, PTB's bot) are excluded.
CHANNEL_METHOD_CALLERS = {
    ("agent.py", "handle_message"): "admitted",                 # the final reply
    ("authz_grants.py", "_post"): "admitted",                   # the challenge body (or casa_text with no turn)
    ("casa_core.py", "operator_notify"): "casa_text",
    ("casa_core.py", "_notify_plugin_health_locked"): "casa_text",
    ("casa_core.py", "notify_placeholder_rewrites"): "casa_text",
    ("casa_core.py", "_sweep_engagement_topics"): "casa_text",
    ("casa_core.py", "_telegram_outbound"): "casa_text",
    ("channels/telegram.py", "finalize_response_stream"): "forward",
    ("channels/telegram.py", "finalize_stream"): "forward",
    ("channels/telegram.py", "send_response"): "forward",
    ("tools.py", "send_message"): "admitted",
    ("tools.py", "_classify_send"): "admitted",                 # the caption, admitted by send_media
    ("tools.py", "_post"): "admitted",                          # ask_user (both arms) and wipe_memory (casa_text)
}
_NON_CHANNEL_RECEIVERS = {"bot", "_bot", "bus", "_bus", "self_bus"}


def _channel_method_callers():
    found = set()
    for path in sorted(CODE.rglob("*.py")):
        rel = path.relative_to(CODE).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in CHANNEL_METHODS):
                continue
            recv = node.func.value
            rname = getattr(recv, "id", None) or getattr(recv, "attr", None)
            if rname in _NON_CHANNEL_RECEIVERS or rel == "bus.py":
                continue
            found.add((rel, _enclosing(tree, node)))
    return found


def test_every_caller_of_a_channel_text_method_is_recorded():
    """Plan round 1 (Terra): the placeholder-rewrite notice handed `send` a
    bare string and nothing here saw it — the other lists watch what is minted
    and what reaches the bot, not who calls the methods in between."""
    found = _channel_method_callers()
    assert found == set(CHANNEL_METHOD_CALLERS), {
        "unrecorded": sorted(found - set(CHANNEL_METHOD_CALLERS)),
        "gone": sorted(set(CHANNEL_METHOD_CALLERS) - found)}
    assert set(CHANNEL_METHOD_CALLERS.values()) <= {"admitted", "casa_text", "forward"}


def test_every_casa_text_call_site_is_recorded():
    casa, _, _ = _scan()
    assert casa == set(CASA_TEXT_SITES), {
        "unrecorded": sorted(casa - set(CASA_TEXT_SITES)),
        "gone": sorted(set(CASA_TEXT_SITES) - casa)}


def test_every_edit_dm_message_caller_is_recorded_and_classified():
    _, edits, _ = _scan()
    assert edits == set(EDIT_DM_MESSAGE_CALLERS), {
        "unrecorded": sorted(edits - set(EDIT_DM_MESSAGE_CALLERS)),
        "gone": sorted(set(EDIT_DM_MESSAGE_CALLERS) - edits)}
    assert all(v <= {"derived", "status", "plugin"} for v in EDIT_DM_MESSAGE_CALLERS.values())


def test_every_raw_transport_call_is_inside_a_recorded_function():
    _, _, raw = _scan()
    assert raw == set(RAW_TRANSPORT_FUNCTIONS), {
        "unrecorded": sorted(raw - set(RAW_TRANSPORT_FUNCTIONS)),
        "gone": sorted(set(RAW_TRANSPORT_FUNCTIONS) - raw)}
    assert set(RAW_TRANSPORT_FUNCTIONS.values()) <= {"admitted", "stream", "derived", "notice", "topic"}


ADMITTED_METHODS = {"send", "send_response", "finalize_stream", "finalize_response_stream",
                    "post_dm_keyboard", "send_media"}
DERIVED_METHODS = {"edit_dm_message"}
TOPIC_METHODS = {"send_to_topic", "send_to_topic_rich", "send_to_topic_paged",
                 "send_response_to_topic", "edit_topic_message", "edit_topic_message_rich",
                 "edit_topic_message_markup", "send_topic_message_markup",
                 "post_ask_body_rich", "post_options_keyboard", "post_perm_keyboard",
                 "post_topic_message"}
NOTICE_METHODS = {"deliver_operator_link", "reply"}
# dispatch — the text becomes a TURN's input, not an emission (Astra r1 item 1)
DISPATCH_METHODS = {"deliver_system_turn"}


def test_channel_text_methods_are_classified():
    """Every public coroutine of TelegramChannel that takes a text-like
    parameter is in exactly one class. A new one fails here until classified."""
    from channels.telegram import TelegramChannel
    text_params = {"text", "message", "full_text", "caption", "body", "label"}
    found = set()
    for name, member in inspect.getmembers(TelegramChannel):
        if name.startswith("_") or not inspect.iscoroutinefunction(member):
            continue
        params = set(inspect.signature(member).parameters)
        if params & text_params:
            found.add(name)
    classified = (ADMITTED_METHODS | DERIVED_METHODS | TOPIC_METHODS | NOTICE_METHODS
                  | DISPATCH_METHODS)
    assert found <= classified, sorted(found - classified)
    assert not (ADMITTED_METHODS & (DERIVED_METHODS | TOPIC_METHODS | NOTICE_METHODS
                                    | DISPATCH_METHODS))
