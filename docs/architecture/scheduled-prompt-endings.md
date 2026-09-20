---
last_reviewed: 2026-09-20
---

# How a scheduled prompt ends

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

A scheduled turn delivers its closing text, so a prompt whose turn already sent the
operator's copy has to ask for silence itself. This is that convention: which prompts carry
the clause, what it says, how the tools a turn calls decide the question, and which surfaces
state it. Trigger types, registration, firing and who may write the file the prompts live in
are [`architecture/triggers.md`](triggers.md)'s; the sentinel's own mechanics and the gate
that reads it are [`architecture/turn-loop.md`](turn-loop.md)'s; a reminder's generated
prompt is [`architecture/reminders.md`](reminders.md)'s.

## Mental model

**A scheduled turn's closing text is delivered, so the prompt is where silence is asked
for.** A scheduled turn that sends a message with a tool and then ends with ordinary prose
delivers twice to the same chat: the tool send happens immediately and leaves no mark the
final-text path can read, and the turn's own closing text then rides the ordinary reply path.
Neither half is narrowed, and both refusals are pinned: a scheduled turn with real text still
delivers it exactly once, and prose *after* the silence sentinel is still delivered (the
recant contract — a correction after a send must reach the operator). What closes the gap is
therefore the prompt, per prompt, and it is a convention — carried by the surfaces
`tests/test_scheduled_prompt_guidance.py` enumerates (this document, `casa/DOCS.md`, and the
configurator's `trigger/add`, `trigger/update` and `prompt/edit` recipes), and extended to a
new surface by adding it there.

For interval/cron/date prompts whose turn delivers its own message, keep
the send instruction first and unconditional, and end the prompt with:
After the send, output the sentinel `<silent/>` and nothing else.

**Which prompts the clause belongs to is decided by where the operator's copy of the message
comes from, never by whether the turn calls a tool: a tool call that is not a delivery decides
nothing here.** A turn whose message reaches the operator from a delivery tool call —
`send_message`, `send_media`, or the question `ask_user` posts — has nothing left to say, and
that is the shape the clause is for: a
reminder's generated prompt ([`architecture/reminders.md`](reminders.md)) is the plain
example, and an event wake carries the same clause for the same reason, its `ack_event` call
being bookkeeping rather than the delivery
([`architecture/plugin-events.md`](plugin-events.md)). A turn whose message reaches the
operator as its own final text is the other shape, needs no clause, and is harmed by one —
the sentinel would be its whole final text and the turn would be suppressed. The shipped
heartbeat and morning-briefing defaults are that shape, telling the agent to output only the
final message text; so is any turn that calls tools to look something up and then reports
what it found. A turn that asks with `ask_user` has put its question in the chat only when the
ask reports that it is awaiting the operator's answer; when it reports anything else, the turn
outputs what the ask reported as its final text instead of the sentinel. A turn that sends
with `send_message` has put its message in the chat only when the send reports that the
message was sent; when it reports anything else, the turn outputs what the send reported as
its final text instead of the sentinel. Those two rules are the whole of it: `send_media`
does not report delivery reliably in either direction, so no surface states one for it.

A Home Assistant notification that carries this turn's message to the operator is a
delivery, including when it is reached through the Home Assistant proxy, so that prompt
takes the clause; a Home Assistant read or device action whose result the turn then reports
is not a delivery, because the operator's copy is still the turn's own final text. The three
names above are the deliveries Casa declares, not the definition of one — the property is,
and a family of tools the classification records as CONDITIONAL is what keeps the names from
closing it again.

The configurator's trigger recipes and the app's user documentation state the distinction in
the same words and name the same three tools. That enumeration is classified against the code
rather than asserted: every tool declared in the code root is either in it, recorded as outside
it with a reason, or filed in the CONDITIONAL family — tools whose one declaration reaches both
a notification that is this turn's delivery and a read that is not, so that no name can file
them either way and only the property decides. A new tool cannot join any of those without
failing
`tests/test_scheduled_prompt_guidance.py::test_no_declared_tool_is_unclassified_for_the_closing_convention`,
a member of the CONDITIONAL family whose two arms are not worked on all four naming surfaces
fails
`tests/test_scheduled_prompt_guidance.py::test_the_conditional_delivery_family_is_worked_on_every_naming_surface`,
and an existing tool that gains the scheduled-delivery eligibility fails
`tests/test_scheduled_prompt_guidance.py::test_only_the_recorded_tools_use_the_scheduled_delivery_eligibility`.
Neither reads a prompt, sees a chat write reached by another route, or sees a plugin's tools.
**The convention is not a runtime guarantee**: nothing validates a prompt, so a
hand-authored prompt of the first shape that omits the clause still delivers twice. The
mechanics of the sentinel and the gate that reads it are
[`architecture/turn-loop.md`](turn-loop.md)'s. A webhook trigger carries no prompt at all
(INV-TRIG-013), so the convention does not reach it.

## Contracts & invariants

**This document declares no invariant of its own, and that is the subject rather than a
gap.** A convention carried by prompt text has nothing a test can execute: no code reads
these surfaces at runtime, so the strongest assertion available is that each surface states
the rule in one wording, which is what
`tests/test_scheduled_prompt_guidance.py` pins. Two invariants elsewhere bound it. The
sentinel the clause names is suppressed by
[`architecture/turn-loop.md`](turn-loop.md)'s INV-TURN-009, which is what makes a prompt
carrying the clause deliver nothing extra; and a webhook trigger's refusal to carry a prompt
at all is INV-TRIG-013 in [`architecture/triggers.md`](triggers.md), which is what keeps the
convention away from webhook turns.

What the tools report is a separate matter with its own tests. `send_message` reports a
PROVEN non-delivery as an error, and `ask_user` reports whether it is awaiting an answer —
which is what makes the two rules above followable; `send_media` reports neither direction
reliably, so no surface states a rule for it.

## Failure behavior

**A prompt of the first shape that omits the clause delivers twice.** Nothing validates a
prompt, so this is the failure the convention exists to prevent and cannot itself prevent.

**A prompt of the second shape that carries the clause is suppressed entirely.** The
sentinel becomes the turn's whole final text, and the operator gets nothing.

**A delivery tool reports a failure and the turn emits the sentinel anyway.** The rules
above are what an author writes into the prompt; a model that ignores them still ends the
turn silently, and the operator receives nothing. The runtime reports honestly; it does not
enforce what the turn then does.

## Extension points

**A new authoring surface** joins by carrying the same wordings, and by being added to the
surface list `tests/test_scheduled_prompt_guidance.py` enumerates — that test is what makes
a surface a surface.

**A new delivery tool** must be classified before it ships: named on every authoring
surface, recorded as outside the class with a reason, or filed in the CONDITIONAL family.
The classification is checked against the code root rather than asserted.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Tests**
- `tests/test_scheduled_prompt_guidance.py`

**Related**
- [`architecture/triggers.md`](../architecture/triggers.md)
- [`architecture/reminders.md`](../architecture/reminders.md)
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
- [`architecture/plugin-events.md`](../architecture/plugin-events.md)
<!-- END SOURCEMAP -->
