"""Test helpers for the output boundary (#1038).

The Telegram channel's model-text methods accept only an ``Admitted`` value
(INV-OUT-001). Mechanics tests that drive those methods directly — pagination,
entity fallback, delivery outcomes — are about the transport, not about
admission, so they mint a model-sourced ``Admitted`` through a real scope with
nothing owed. ``casa_text`` is deliberately NOT used here: it is a recorded
production class, and a test that reached for it would be testing the wrong
provenance.
"""
from __future__ import annotations

from output_boundary import Admitted, IntentKind, TurnScope


def scope(role: str = "assistant", name: str = "Test", **over) -> TurnScope:
    fields = dict(id="turn-test", cid="cid-test", role=role, display_name=name,
                  channel="telegram", message_type="channel_in", markers={})
    fields.update(over)
    return TurnScope(**fields)


def admitted(text: str, kind: IntentKind = IntentKind.DISCRETE) -> Admitted:
    """*text* admitted by a scope that owes nothing — the transport sees the
    text unchanged, with model provenance. ``DISCRETE`` by default: a mechanics
    test that hands the channel whitespace or a bare sentinel is testing the
    transport, not closing silence, which only a ``FINAL_REPLY`` admission judges."""
    return scope().admit(kind, text)


def with_scope(origin: dict) -> dict:
    """An origin dict as ``Agent._process`` would build it: the same fields
    plus the scope ``TurnScope.mint`` would mint for a message with that
    channel, type and reserved markers — so the per-turn decisions registered
    at mint (no streaming, the webhook destination binding) are the real ones,
    not a fixture's guess. The emitting tools refuse an origin without a scope
    (design §5), so a test that binds an origin by hand binds this."""
    from types import SimpleNamespace

    if origin is None:
        return None     # a test binding NO origin means exactly that
    out = dict(origin)
    if "turn_scope" not in out:
        role = str(origin.get("role") or "assistant")
        msg = SimpleNamespace(
            id="turn-test", channel=str(origin.get("channel") or "telegram"),
            type=SimpleNamespace(value=str(origin.get("message_type") or "channel_in")),
            context={k: v for k, v in origin.items() if k != "turn_scope"})
        config = SimpleNamespace(role=role, character=SimpleNamespace(name="Test"))
        out["turn_scope"] = TurnScope.mint(msg, config)
    return out
