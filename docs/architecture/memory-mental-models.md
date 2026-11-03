---
last_reviewed: 2026-10-01
---

# Mental models: the overlay and the models Casa declares

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The mental-model overlay together with the mental models Casa declares for it: how a
fresh session at the highest clearance is shown the backend's mental models, and how
Casa keeps its own declared models reconciled with the backend. How a recalled fact is
rendered, what a caller is told when the store cannot answer, and what a caller may claim
from what comes back is [`architecture/memory.md`](memory.md); what a wipe does to the
models is [`architecture/memory-wipe.md`](memory-wipe.md).

## Mental model

**Mental-model overlays cannot be tier-filtered at all**, because they are bank-wide
summaries rather than individually tagged facts. That is why they are exposed only at the
highest clearance — there is no way to redact part of one.

**The overlay is the backend's mental models, each dated, framed as leads.** A *mental
model* is a standing question the memory backend re-answers from the bank on its own
schedule. On a fresh session at `private` origin clearance, the turn reads the bank's models
(with their content — the backend's list returns metadata only unless asked) and pushes the
ones that have content into the prompt. Each renders under its name with `refreshed <weekday>
<day> <month> <year>` — the date of its last refresh in the operator's timezone, the same form
as a recalled fact's recorded date — and, when its automatic refreshes are paused because the
last one failed (the backend's own rule: a failure later than the last success, or a failure
with no success at all), says that it may be out of date. A model with no content yet (its
first refresh still running) or no readable refresh date is left out: the overlay never shows a
summary whose age it cannot state. A non-empty overlay opens with one line framing it as
memory-derived leads: not live state, and not a complete list, so nothing missing from it is
evidence of absence. A missing bank (after a wipe, before the first save) is simply no overlay;
any other failure to read it is logged and the turn runs without it.

**Casa declares two of those models and keeps them reconciled.** `casa-open-commitments` asks
for the operator's open commitments and follow-ups with their stated dates and deadlines,
leaving out the assistant's own reminders and briefings; it refreshes on the backend's daily
schedule at 05:00 UTC. `casa-operator-profile` asks for a profile of the operator and their
household — who they are, the people around them, how they like to be addressed, standing
preferences and routines with their cadence, each item with the date it was last stated —
leaving out health, money and other people's private matters; it refreshes after the backend
consolidates new memories, at most once a day. Both are untagged, so each reads the whole
bank, including the other model and any model the operator created. The exclusions are
written into the question, not enforced: they shape what the backend writes and filter
nothing. A reconcile pass makes the backend match the declarations: it creates a missing
model, rewrites a definition whose declared fields drifted (fields Casa does not declare are
left as they are), and deletes any model under the **reserved `casa-` prefix** that Casa no
longer declares — so a model the operator creates under that prefix is deleted at the next
pass, while every other id is never touched. The pass reads one page of up to 1,000 models,
and when the backend reports more than it returned, it deletes nothing. A model whose
definition was rewritten, or whose automatic refreshes are paused, gets one explicit refresh in
that pass, because rewriting does not refresh and only an explicit refresh resumes a paused
model. A pass runs in the background
at boot and after every completed wipe, one at a time in arrival order; boot and the wipe never
wait for it, and shutdown cancels it before the memory client closes. It is best-effort: when
the backend is unreachable it logs and changes nothing, and the next boot or wipe tries again.

## Contracts & invariants

**INV-MEM-020**: The mental-model overlay is pushed only on a fresh session at `private` origin clearance, and every model it renders carries its name and the date of its last refresh in the operator's timezone.

Enforced by the turn's load plan, which pushes the overlay on a fresh session only, behind the
`private`-only overlay gate; by the backend's overlay read, which asks the list for content;
and by the overlay render, which emits a model only together with its name and its local
refresh date and leaves out a model whose refresh date it cannot read.

What it does not cover: what the summaries *say*. They are written by the backend's own model
from the whole bank; their dates are the last refresh, not when any fact in them was stated,
and the declared exclusions are part of the question rather than a filter. Nor does it cover
the declared models' reconcile, which is best-effort by design and pinned by ordinary tests:
a pass that cannot reach the backend leaves the models as they were until the next boot or
wipe.

## Failure behavior

**A reconcile pass fails.** If the backend's model list cannot be read — unreachable,
timing out, a server error, an unrecognisable answer — the pass writes nothing and logs it.
A missing bank is not a failure: its list answers 404 and the pass creates both models, which
recreates the bank. One model's create, rewrite, refresh or delete failing is logged and the
pass moves on; a rewrite that failed is not followed by a refresh. A refresh whose connection
drops is not resent — the pass cannot tell whether the backend accepted it. Nothing retries
inside the process: if the backend comes up after Casa, the declared models wait for the next
boot or wipe.

## Extension points

**A new declared mental model** is one more entry in the declarations, under the reserved
`casa-` prefix. Its first reconcile creates it; removing the entry later deletes it from the
backend at the next pass.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/semantic_memory.py::render_mental_models`
- `casa/rootfs/opt/casa/semantic_memory.py::mental_model_refresh_paused`
- `casa/rootfs/opt/casa/hindsight_memory.py::HindsightSemanticMemory.profile`
- `casa/rootfs/opt/casa/hindsight_memory.py::HindsightSemanticMemory.reconcile_mental_models`
- `casa/rootfs/opt/casa/mental_models.py::reconcile`
- `casa/rootfs/opt/casa/mental_models.py::schedule_reconcile`
- `casa/rootfs/opt/casa/mental_models.py::drain_reconcile_tasks`

**Tests**
- `tests/test_mental_models_1126.py`

**Related**
- [`architecture/memory.md`](../architecture/memory.md)
- [`architecture/memory-wipe.md`](../architecture/memory-wipe.md)
<!-- END SOURCEMAP -->
