---
last_reviewed: 2026-10-01
---

# Turn limits: the limit stop and Casa's line about it

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What bounds a resident turn: how a turn that stops at its turn limit is detected and
returned, the one line Casa adds about the stop and where it goes, and the limits the
residents run with. How the turn is assembled, called and retried — and the silence and
resume-fault rules that consult the stop (INV-TURN-008) — is
[`architecture/turn-loop.md`](turn-loop.md). How the line is spoken on voice is
[`architecture/voice.md`](voice.md); what it means for a narration's announcement is
[`architecture/delegation-announcements.md`](delegation-announcements.md).

## Mental model

A turn limit caps the model calls one resident turn may make. Reaching it is not a fault:
the turn ends early, its conversation stays resumable, and the model's text is judged
exactly as on any other turn. What the stop adds is one line of Casa's own, sent apart from
the model's reply, so that whoever is owed the turn's answer learns it was cut short.

## Contracts & invariants

**INV-TURN-014**: A resident turn whose terminal SDK result has subtype `error_max_turns` is a limit stop: it is detected from that subtype alone, returned rather than raised, its session published exactly as any returned turn's, never retried or continued, never reclassified as an error and never counted as a resume fault — a trusted turn records it healthy. Limit handling never rewrites or replaces the model's text: what admission delivers is delivered, and what it suppresses stays suppressed. Casa adds exactly one line of its own, attempted as a separate send — in the turn's Telegram chat; in the operator's Telegram chat, addressed explicitly, for a narration or a schedule that ran on no Telegram chat; at the end of a trusted `/invoke` response body; or spoken after the held tail on voice once something was spoken — except on an untrusted webhook turn, which sends nothing unless its route declares `deliver: operator` or `operator_always` (INV-TRIG-018), when the line goes to the operator's Telegram chat and names the webhook. Every limit stop logs one WARNING naming the role, the channel and the turn count. An untrusted webhook turn runs with a fixed limit of 20.

The CLI ends a turn that would need one model call more than its limit with an error result
of that subtype and no result text. The pool treats it like any non-retryable error result —
the entry is invalidated, the session id returned and published (INV-TURN-002) — so the next
message resumes the stopped conversation. Before #1121 nothing on the resident path read the
subtype: whatever progress text the turn had written went out as an ordinary reply, a
buffered turn that had written nothing was wholly silent, only an INFO line recorded it,
and a stop after a consumed SDK retry was turned into the generic error line and struck.

The fact is recorded in the result-message arm both attempt paths share and published on
the turn report from the winning attempt; a stop is never retried, so it can only be the
last. Every reader of "the turn ended silent" consults it: the INV-TURN-008 reclassification
skips it, the health note resets on it, and a narration's chosen-silence acknowledgement
skips it, because a cut narration did not choose its silence. The #1075 rule of INV-OUT-006
is untouched: the line is never merged into the admitted text, so admission decides the
model's text exactly as before and the line goes out whatever it decided.

Where the line goes is decided by the server-stamped origin route, never the message type —
an untrusted webhook turn dispatches as a scheduled one — and by whether the message that
arrived was a completion notice, captured before synthesis rebinds it. Its words must be
true where they land: "say 'continue'" only in a real chat whose next message resumes the
stopped session; a turn that ran in a session of its own (a schedule, a reminder, the
follow-up to a scheduled question, a narration whose session is not that chat's) names the
task by what the message carries and offers to redo it. A Casa-started turn in the
operator's chat is named by its kind. The WARNING is logged before any fallible delivery, and
a model delivery that raises still has the line attempted. The spoken line is
[`voice.md`](voice.md)'s.

The assistant's limit is `tools.max_turns` in its `runtime.yaml`, 80; the copy in its
`role.yaml` is kept in step, which moves the role checksum, so each open assistant
conversation restarts once, retained where its channel retains, on the release that
changes it. The restricted options every webhook-channel turn without the `invoke` route
takes — a webhook trigger, and a schedule declared on `channel: webhook` — pass the fixed
`_RESTRICTED_WEBHOOK_MAX_TURNS` instead. The butler (10) and the concierge (6) keep theirs.

What it does not cover: a delegated or job turn's limit, which stays the specialist's
`specialist_turn_limit` failure; the in-Casa engagement driver; a failure independent of the
stop — persistence, delivery, cancellation — which keeps its own handling, the line being
best-effort with its own failure logged; and a cut narration whose partial narration reached
the chat, which is acknowledged as today
([`delegation-announcements.md`](delegation-announcements.md)).

## Failure behavior

**The line's Telegram send fails.** It is best-effort: with no Telegram channel to send
through, a send that raises, or a send reported as not delivered, the failure is logged and
nothing is raised, because the turn has already ended. A cancellation is not absorbed.

## Extension points

**A new surface a resident turn can arrive on** needs a destination for the line. The
destination is chosen in one place, `Agent._limit_line_route`, from the server-stamped
origin route, and a surface it does not name falls through to the operator's Telegram chat.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/agent.py::_limit_stop_line`
- `casa/rootfs/opt/casa/agent.py::Agent._limit_line_route`
- `casa/rootfs/opt/casa/agent.py::Agent._send_limit_line`

**Tests**
- `tests/test_pin_1121_turn_limit.py`
- `tests/test_turn_limit_regressions.py`

**Related**
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
- [`architecture/voice.md`](../architecture/voice.md)
- [`architecture/delegation-announcements.md`](../architecture/delegation-announcements.md)
- [`architecture/scheduled-prompt-endings.md`](../architecture/scheduled-prompt-endings.md)
- [`architecture/plugin-triggers.md`](../architecture/plugin-triggers.md)
- [`architecture/webhook-delivery.md`](../architecture/webhook-delivery.md)
<!-- END SOURCEMAP -->
