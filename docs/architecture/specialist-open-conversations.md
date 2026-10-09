---
last_reviewed: 2026-10-07
---

# Specialist open conversations

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What each change to an installed specialist — a persona apply, an upgrade, a rollback or an
uninstall — does to the specialist's open conversations: the warning before it, and the
close after an uninstall. The transactions themselves — the journal, the lock and the
owned-plugin generation — are
[`specialist-bundle-transactions.md`](specialist-bundle-transactions.md); a persona change's
own mechanics are [`persona-lifecycle.md`](persona-lifecycle.md); how a conversation ends
through the terminal funnel is [`engagement-finalization.md`](engagement-finalization.md).

## Mental model

**Warn, then act on the operator's yes.** A persona apply, upgrade or rollback of a
specialist with open conversations does not commit while no acknowledgement in the call
names an engagement of that specialist, open or since closed: it returns a warning that
lists the open ones, and the configurator asks. An
uninstall that does not erase is warned the same way until its acknowledgement names every
one of them. The acknowledgement travels in the call itself, so it is matched to no earlier
warning. After an uninstall commits, it attempts to close the conversations still open.

## Contracts & invariants

**INV-SPEC-022**: While a specialist has open conversations, `persona_apply`, `specialist_upgrade` and `specialist_rollback` called with no acknowledgement naming one of that specialist's engagements (absent, empty, or only ids the registry does not know as its engagements), and `specialist_uninstall` called with erase unset or `erase_data=false` and an acknowledgement that does not name every one of them, change nothing and return a warning that lists them — with two exceptions for the uninstall: where the base call returns `consent_channel_unavailable` it returns that refusal unchanged and touches no erase state, and otherwise a refused uninstall call performs exactly its base erase-question step (it voids the subject's question where the base call would open or close one); a persona, upgrade or rollback whose acknowledgement names one of that specialist's engagements — open or since closed — commits and names every conversation open after the commit that the acknowledgement does not name; an `erase_data=true` uninstall call is never refused by it; and an uninstall, only after its commit and reload, attempts through the terminal funnel to close every conversation of that specialist still open at that point, and its result names each one it closed and each whose close failed.

A specialist's open conversations are its active or idle specialist-kind engagements —
delegated conversations, background jobs and launches still in flight; the configurator's
own executor engagement and plugin jobs are another kind and never listed. Each of the four
tools commits on the call, so the warning can only come first as a pending result:
`ok: false`, `kind: "open_conversations_unconfirmed"`, the conversations by topic and task,
and a `warning` the configurator relays verbatim before asking. The operator's yes comes back
as `acknowledged_conversations`, carried in the call itself: the check reads only the ids
the call carries and matches no call to the configurator conversation that was warned, so a
call without the ids is warned again and one carrying them commits, whoever makes it. An id counts only when the registry knows it as
one of that specialist's engagements, so a made-up list acknowledges nothing; the calls
Casa's erase-question continuations tell the model to make carry the same ids. For a persona, upgrade or rollback the warning
says, of each conversation, the one sentence `tools.py::SPECIALIST_OPEN_CONVERSATION_NOTICE_TEMPLATE`
holds — it keeps its personality and the plugin versions it started with, picks up the new
settings when it resumes, and switches only by closing it with `/complete` and asking for a
new one — and that template also feeds the three tool descriptions, so no surface can drift.
The sentence names whom to ask: `tools.py::specialist_open_conversation_notice` fills in the
assistant's configured persona name at call time, while the descriptions, built at import
before any persona is registered, say "the assistant".
A conversation opened after the operator confirmed does not refuse an ordinary change; the
committed result names it, if it is still open when the change finishes, with the same
sentence. A pending-configuration upgrade's result and a kept upgrade's result echo the ids,
and the upgrade recipe passes them to the follow-up and the re-run, so neither is warned
again.

Enforced by one listing (`tools.py::_open_specialist_engagements`) and two placements. The
ordinary changes check in `tools.py::_ordinary_change_gate` before anything commits — for
the upgrade after its read-only validation and before the bundle transaction, so a refused
call leaves the receipt, the staging tree and the consent as they were; for a persona,
inside the mutation lock before the apply. The uninstall checks inside the erase gate, in
the transaction child that owns the plugin-tools mutation lock, at the point where the base
call would open or close the erase question. So a refused call does that step and nothing
more — it voids the old question, opens none and posts no DM — and is linearized with every
other uninstall of the slug; a warning is never issued after an Erase tap was consumed,
because only `erase_data=true` calls consume one. After the sequencer reloaded and the
journal completed, `tools.py::close_specialist_engagements` reads the open conversations
again — so one opened after the confirmation, while an eraser ran, is closed too and flagged
`opened_after_confirmation` — and finalizes each through `_finalize_engagement` with outcome
`cancelled`: the topic is told and closed and the engager notified, exactly as a cancel. It
runs inline in the shielded child; the funnel takes no reload or plugin-tools lock.

What it does not cover. The model can acknowledge without asking; the carrier is a tool
result, as INV-PERS-018's notice is. A conversation launched after the close but from a
delegation resolved before the reload is not closed, and dies on its next resume as a
removed specialist's conversation does; no launch lock narrows that window further. A
handler cancelled after the commit still closes the conversations, but its result — the
closed list and any arrival — never reaches the configurator; the topics' tellings and the
engager's notice are the record. A failed close is reported, not retried. A cancel at the
warning leaves an upgrade's receipt and consent as any abandoned upgrade does.

## Failure behavior

A close that fails is named in the uninstall's result and is not retried. A handler
cancelled after the commit still closes the conversations, but its result never reaches the
configurator. Both, with the windows the rule leaves open, are stated under INV-SPEC-022
above.

## Extension points

**A new ordinary change to an installed specialist** — one that, like a persona apply, an
upgrade or a rollback, keeps the specialist — checks in `tools.py::_ordinary_change_gate`
before it commits, or it lands on conversations the operator was never warned about; its
warning says the sentence `tools.py::specialist_open_conversation_notice` returns, never a
copy of it.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/tools.py::_ordinary_change_gate`
- `casa/rootfs/opt/casa/tools.py::close_specialist_engagements`

**Tests**
- `tests/test_pin_1095_warn_then_act.py`
- `tests/test_pin_1095_removal_close.py`
- `tests/test_1095_open_conversation_regressions.py`

**Related**
- [`architecture/specialist-bundle-transactions.md`](../architecture/specialist-bundle-transactions.md)
- [`architecture/persona-lifecycle.md`](../architecture/persona-lifecycle.md)
- [`architecture/plugin-mutation-tools.md`](../architecture/plugin-mutation-tools.md)
- [`architecture/engagement-finalization.md`](../architecture/engagement-finalization.md)
- [`architecture/engagements.md`](../architecture/engagements.md)
<!-- END SOURCEMAP -->
