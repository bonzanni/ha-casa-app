---
last_reviewed: 2026-11-24
---

# The live voice session

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How Casa follows a live voice session once it has been accepted: the provider client, the
turns built from the transcript, the two triggers of an ability and the one action state
they share, the decision step, the goodbye grace, the clock and its two-step silence, and
the delays measured for tuning. Nothing in `live_voice/` names a caller, a number or a
transfer beyond a generic target URI. The phone line that accepts the session, supplies its
profile, timing and fixed lines, and reads the result is [`phone.md`](phone.md).

## Mental model

**The client.** `live_voice/api.py` is the client of the provider's live sessions:
`accept`, `reject`, `refer`, `hangup`, `attach` (the session's control channel, a WebSocket)
and the Responses calls of the decision step and the phone line's record. The key goes only
into the Authorization header and never into an error. `LiveSession`
(`live_voice/session.py`) follows one accepted session over the control channel until it
closes, and returns a `Result`: the lines, the action log, the final phase, the reason the
session closed (`clock:<which>` when the clock ended it) and its times.

**Turns are read from the transcript.** The service has no event for the end of a turn, so
`LiveSession` builds turns from transcript deltas: consecutive deltas from one side are one
line, and when the caller starts to speak after an agent line a new round begins
(`ActionState.new_round`). Every request for an ability names the round it was decided in.

**Two triggers, one action state.** The helper model the session delegates to holds the
abilities as function tools; its calls arrive as `response.event` and belong to the round
in which their delegation was created, and a call of a delegation the session never saw
created runs nothing. The decision step (`live_voice/decision.py`) is one model call made
each time the agent has been quiet for a second after saying something; caller speech
before that moment cancels it, and one already asked is judged by its round. Both go
through one `ActionState` (`live_voice/actions.py`), which holds one lock across a runner
(INV-PHONE-002, INV-PHONE-003). A refusal a runner wrote is told to the voice model once
per round; the action state's own short words never are. A failed decision is retried once.

**A session is never ended on the other side's unanswered words.** Before a `final`
ability such as `end_call` runs, the session waits, up to an eight-second grace, for the
agent to have the last word and for that line to have been played. If the caller says
anything meanwhile, or the agent does not answer, the request does not run, and a later
request in the same turn may.

**Text is put into the session two ways.** A fixed line to be said as written (`silence`,
`too_long`) is sent with `session.instructions.append` behind an instruction to say it
exactly (`LiveSession.say`); a line of context, `check_in` or `transfer_failed`, is sent the
same way without it (`LiveSession.tell`), and the voice model answers in its own words. The
lines come from the transport, in the session's language.

**A silence is checked on before it ends anything** (`live_voice/clock.py`). Its three
deadlines — the check-in, the closing silence after it, and `undecided` — are each counted
through `Clock._silent_since`: from the moment the quiet began, but never from before the
agent's latest line has been played (its end plus a margin), not when its text arrived. When
later text of the agent moves that end, all three move with it. After `check_in_s` (7 seconds)
with nobody speaking, the agent is told, as context, to ask whether the other side is still
there. From then on only the other side's speech starts the silence again: the agent's own
question brings no second check-in, but the closing is counted from when that question has
been played. If the other side still says nothing for `silence_s` (10 seconds more), the `silence` line is said and the session ends, unless the caller speaks while it is
being sent or within the grace, or work was accepted meanwhile. One check-in per silence: when the caller answers, the
next silence brings a new check-in, not the closing line. There is no check-in before the
agent has said anything (that is `first_speech`), and none while accepted work is pending;
when that work is settled the silence starts afresh. From a check-in until the other side
speaks, a decision that cannot be made ends nothing either.

**The clock ends a session without any model** (INV-PHONE-004). The agent has not spoken
eight seconds after the attach (`first_speech`). A silence has run through both of its steps
(`silence`). The decision failed twice and nobody spoke for `undecided_silence_s` (eight seconds)
after the line had been played, with no check-in under way (`undecided`); a check-in clears
it, so with the default timing (check-in at seven seconds) the check-in always comes first
and such a call ends through the two-step silence, and `undecided` ends one only under a
timing whose `undecided_silence_s` is shorter than `check_in_s`. The session reached ten minutes: the `too_long` line, the grace,
then the end whatever is said (`max`). The control channel dropped and could not be
attached again within ten seconds (`lost`). Accepted work that stays pending for 30 seconds
is given up (`pending`), which ends nothing by itself. The clock ends the action state
before it hangs up, so no ability starts meanwhile. Every duration is a field of `Timing`,
a constructor value of the session.

**Two delays are measured, for tuning only.** `Result.first_speech_ms` is the time from
the start of following the session to the agent's first transcript delta (`None` when it
never spoke); `Result.reply_gaps_ms` holds, for each caller line the agent answered, the
time from that line's end to the start of the answer. They go into the result and nowhere
else; the phone line logs them as one line of numbers.

## Contracts & invariants

**INV-PHONE-002**: An ability runs at most once per caller turn, whichever trigger asks for it — the helper's function call or the decision step — and a request that arrives while it runs waits and gets its outcome instead of running it again.

The lock is held across the runner, so the two triggers asking at nearly the same moment
are one run. A refusal stays the turn's outcome; the next turn may try again. A transfer the
provider may have accepted is spent for the whole call.

**INV-PHONE-003**: A request decided before the caller's latest turn began never runs: it is answered `superseded`, also when it waited for the lock while that turn began, and a transfer checks again just before it is placed.

A decision that was being made when the caller spoke, and a helper call whose delegation
was created before the caller spoke, are both stale. A request that waited behind a slow
runner is judged when it gets the lock, not when it was sent.

**INV-PHONE-004**: An accepted call is ended by Casa's clock, with no model involved, when the agent has not spoken in time after the attach, when a silence outlasts both its steps — the agent told to check whether the other side is still there, then no word from the other side for a second stretch — when the decision step failed twice and nobody spoke while no check-in was under way, when the call reaches its maximum length, and when the control channel is lost and cannot be attached again in time.

Each ending hangs up and is recorded by the phone line as `ended_by_casa`. A hang-up the
provider refuses still ends the session. Speech of the other side cancels a silence at
either step, also while the closing line is sent and during the grace after it; the agent's own words after a
check-in do not. What it does not cover: a maximum duration at the carrier, which the person
installing the line sets as a backstop to this one.

## Failure behavior

**The decision step fails.** It is retried once; a second failure arms `undecided`, counted
from the end of the line being played. Under the default timing the check-in comes first and
clears it, after which only the silence decides.

**The control channel drops.** The session attaches again for up to ten seconds; a first
attach that never completes counts the same. When that fails the session ends as `lost`.

**A refer the provider may have accepted.** Success, no answer and a 5xx all mark the work
pending; a session that closes while work is pending was handed over, and work still
pending after 30 seconds is given up and `transfer_failed` is told to the voice model.

## Extension points

**Another transport** can reuse `live_voice/` unchanged: it accepts its own session,
supplies a `Profile` (instructions, voice, models, abilities), a `Timing` and its fixed
lines, and reads the `Result`.

**A new clock ending** is a deadline in `Clock._due` and a branch in
`LiveSession._on_clock`; a call that does not end the session must move the deadline it was
called for.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/live_voice/__init__.py`
- `casa/rootfs/opt/casa/live_voice/api.py`
- `casa/rootfs/opt/casa/live_voice/actions.py`
- `casa/rootfs/opt/casa/live_voice/session.py`
- `casa/rootfs/opt/casa/live_voice/clock.py`
- `casa/rootfs/opt/casa/live_voice/decision.py`

**Tests**
- `tests/test_live_voice_api.py`
- `tests/test_live_voice_actions.py`
- `tests/test_live_voice_session.py`
- `tests/test_live_voice_decision.py`

**Related**
- [`architecture/phone.md`](../architecture/phone.md)
<!-- END SOURCEMAP -->
