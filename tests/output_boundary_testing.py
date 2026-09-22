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


def admitted(text: str, kind: IntentKind = IntentKind.FINAL_REPLY) -> Admitted:
    """*text* admitted by a scope that owes nothing — the transport sees the
    text unchanged, with model provenance."""
    return scope().admit(kind, text)


def with_scope(origin: dict) -> dict:
    """An origin dict as ``Agent._process`` would build it: the same fields
    plus a turn scope owing nothing. The emitting tools refuse an origin
    without one (design §5), so a test that binds an origin by hand binds
    this."""
    if origin is None:
        return None     # a test binding NO origin means exactly that
    out = dict(origin)
    out.setdefault("turn_scope", scope(role=str(origin.get("role") or "assistant")))
    return out
