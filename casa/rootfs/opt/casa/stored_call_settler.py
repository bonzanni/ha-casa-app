"""The stored-call settler: a tap on a stored-call button, run as one pinned
turn of the agent that executes the card.

A proposal card's buttons are stored calls: each names one tool of one plugin
and its exact arguments. A tap is settled here, as one use of the executing
agent's lane (:mod:`agent_lane`): under the lane's lock the re-checks run on
ONE captured build input (the plugin still assigned, the same artifact, a
profile that allows the tool, no erasure running, the live maps still
admitting the call) and the deadline; the agent's concurrency permit is taken
after the lock and never waited for; the pinned run is created directly as a
task (never through the bounded wrapper) under the ``PinnedRun`` ContextVar,
whose pin allows exactly the stored call, once. Its outcome is told from the
records: a validated capture is the receipt, authoritative over an aborted
turn; anything else is one labelled notice; the model's text is discarded.

The run is released through ONE function, :func:`_settle_pinned_run`, on its
normal end, at the ceiling and on a cancellation alike: the termination is
confirmed, or the agent is faulted until restart, and only then are the
pinned fds closed and the permit released.

The card's executing agent is the agent the card records (``meta["role"]``),
and it must own the card: see :func:`owns_card`.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Callable

from stored_calls import OPERATOR_FILE, TELL_LINE   # the one tell line, shared with the result hook

logger = logging.getLogger(__name__)

STORED_CALL_RECEIPT_CHARS = 4000
NO_RECEIPT = "[no receipt]"
POSTED_PROPOSAL = "[posted a proposal]"
POSTED_FILE = "[posted a file]"
# _post_next_card's word for an in-place edit that may have landed (#1341)
UNCONFIRMED = "not confirmed"
TELL_ECHO = " — the CLI reported arguments changed by an installed hook"


def owns_card(endpoint_agent: str, role: str) -> bool:
    """The tap's executing agent owns the card: *role*, the agent the card
    records, is loaded, and the chat the tap arrived in is that agent's own
    (the endpoint's agent, *endpoint_agent*, is *role* itself, a resident
    included) or a resident's, whose chat holds the cards of the agents that
    work behind it. A card of another agent in an agent's own chat is not its
    to settle."""
    import tools as tools_mod
    if not role or tools_mod._agent_role_map.get(role) is None:
        return False
    if role == endpoint_agent:
        return True
    host = tools_mod._agent_role_map.get(endpoint_agent)
    return getattr(host, "kind", "") == "resident"


def _card_bot(meta: dict, fallback: int) -> int:
    """The bot the card was delivered through, from its record."""
    bot = meta.get("bot_id")
    if isinstance(bot, int) and not isinstance(bot, bool) and bot > 0:
        return bot
    return fallback


def _outcome_text(output: Any) -> tuple[str | None, str | None]:
    """``(text, failure_kind)``: the runner's output classified exactly as
    ``delegate_to_agent`` classifies it — a CLI-aborted run yields no text."""
    import specialist_limits
    import tools as tools_mod
    if getattr(output, "run_aborted", False):
        return None, tools_mod._run_abort_kind(getattr(output, "run_subtype", None))
    text, _truncated = specialist_limits.truncate_output(str(getattr(output, "text", "") or ""))
    return text, None


# -- S5 §5: the tap's desk use — the pinned one-call turn -------------------

def _tap_prompt(label: str, runtime_name: str, canonical: str) -> str:
    """§5.2.3: the only text the model is given besides its role prompt — no
    desk block, no recalled memory, no operator frame (#1282). The model is
    the hands; the pin enforces the one call, and whether the tap still
    applies is the plugin's to decide, not the model's."""
    return (f'[casa stored call] The operator tapped "{label}" on your proposal. '
            f"Call the tool `{runtime_name}` exactly once, with exactly these arguments "
            f"and nothing else: {canonical}. Make the call even if it looks out of date or "
            f"already done: the plugin decides whether the tap still applies, and its answer "
            f"is what the operator sees. Then stop. Do not call any other tool and do "
            f"not write anything to the operator — Casa posts the tool's result.")


def keeps_card(meta: dict, idx: int) -> bool:
    """#1362: the tapped button is a ``keep_card`` file button — its tap settled
    nothing, so nothing it does may write a line over the card."""
    kinds = meta.get("kinds") or []
    return isinstance(idx, int) and 0 <= idx < len(kinds) and kinds[idx] == "keep_card"


def _assigned_plugin(resolution: Any, seg: str) -> Any:
    """The resolved plugin whose runtime segment is *seg*, or None."""
    from plugin_registry import runtime_name as _runtime_name
    from text_util import sanitize_segment
    for rp in getattr(resolution, "plugins", None) or []:
        if sanitize_segment(_runtime_name(rp)) == seg:
            return rp
    return None


def _profile_refuses(plan: Any, runtime_name: str) -> bool:
    """§4.3: when the plan has an entry whose prefixes cover the name, the
    name must be in its allowed names (S8's per-prefix guard); no entry, no
    further restriction."""
    for entry in getattr(plan, "entries", None) or ():
        if runtime_name.startswith(tuple(entry.prefixes)):
            return runtime_name not in entry.allowed_names
    return False


async def handle_tap(
    *, channel: Any, resident_role: str, chat_id: int, user_id: int, cid: str,
    desk_role: str, meta: dict, idx: int, request_id: str, reservation: Any,
    bot_id: "int | None" = None,
) -> None:
    """One desk use for a committed tap (§5.2, ``handle_reply``'s sibling):
    under the lock the faulted check, the re-checks of §4.3 on ONE captured
    build input and the deadline; the permit after the lock; the pinned run
    created DIRECTLY as a task (never the bounded wrapper) under the
    ``PinnedRun`` ContextVar, the ceiling driven here; the outcome — a
    validated capture is the receipt, authoritative over an aborted turn;
    anything else is one labelled notice; the model's text is discarded —
    the exchange ``[tapped: <label>]`` and one body-free echo line. The
    reservation, the typing lease and the permit are released on every exit.
    *channel* is the endpoint the tap arrived on; the desk, the echo and the
    turn's route are keyed by its bot (*bot_id*, which defaults to
    ``channel.bot_id``). *resident_role* is that endpoint's agent; the card's
    executing agent, *desk_role*, must own the card (:func:`owns_card`).
    """
    import agent as agent_mod
    import plugin_erasure
    import specialist_desk as sd
    import result_broker as rb
    import tools as tools_mod
    from channels import DeliveryOutcome
    from output_boundary import IntentKind, TurnScope
    from pinned_run import PinnedRun
    from stored_calls import stored_call_still_ok

    label = sd.label_for(desk_role)
    bot = channel.bot_id if bot_id is None else bot_id
    context = {"chat_id": str(chat_id), "cid": cid, "_tg_bot": bot}
    desk = sd.DESKS.get_or_create(bot, chat_id, desk_role)
    calls, options = list(meta.get("calls") or []), list(meta.get("options") or [])
    call = dict(calls[idx]) if 0 <= idx < len(calls) else {}
    button = str(options[idx]) if 0 <= idx < len(options) else "?"
    runtime = str(call.get("runtime_name") or "")
    canonical = str(call.get("canonical") or "")
    seg = str(meta.get("plugin_seg") or "")
    keep = keeps_card(meta, idx)

    async def _notice(line: str) -> None:
        try:
            await channel.deliver_desk_notice(chat_id, line)
        except Exception as exc:  # noqa: BLE001 — nothing more is attempted
            logger.warning("desk notice failed: %s", type(exc).__name__)

    async def _mark(line: str) -> bool:
        mark = getattr(channel, "mark_proposal", None)
        if mark is None or keep:          # #1362: a keep_card tap leaves its card live
            return False
        try:
            return await mark(meta, line) is not False
        except Exception as exc:  # noqa: BLE001 — the keyboard line is cosmetic
            logger.warning("proposal mark failed: %s", type(exc).__name__)
            return False

    async def _refuse(reason: str) -> None:
        """§10: the keyboard line, one notice, the refused echo line; not a use."""
        await _mark(f"✖ {reason}")
        await _notice(f"{label} could not apply your tap ({reason}).")
        sd.record_echo(bot, chat_id, f"{label} refused your tap ({button}): {reason}.")

    async def _tell_faulted() -> None:
        line = sd.faulted_line(label)
        await _notice(line)
        sd.record_echo(bot, chat_id, line)

    try:
        async with desk.use(reservation):
            if desk.faulted:
                await _mark("✖ faulted")
                await _tell_faulted()
                return
            # §4.3: the re-checks, on one captured build input, under the lock
            if not owns_card(resident_role, desk_role):
                await _refuse("not delegable")
                return
            cfg = tools_mod._agent_role_map.get(desk_role)
            build = tools_mod._capture_build_input(cfg)
            rp = _assigned_plugin(build.resolution, seg)
            if rp is None:
                await _refuse("plugin unassigned")
                return
            if str(getattr(rp, "artifact_id", "") or "") != str(meta.get("artifact_id") or ""):
                await _refuse("plugin changed")
                return
            if _profile_refuses(build.plan, runtime):
                await _refuse("profile")
                return
            if plugin_erasure.FENCE.fenced(str(getattr(rp, "name", "") or "")):
                await _refuse("plugin erasing")
                return
            why = stored_call_still_ok(runtime, contract_map=build.contract_map,
                                       protected=build.protected)
            if why is not None:
                await _refuse(why)
                return
            deadline = meta.get("deadline")
            # #1362 (d1, both reviewers): a keep_card tap admitted while its card was
            # live sends its file even if the TTL passed while it waited for the desk
            if not keep and (not isinstance(deadline, (int, float))
                             or asyncio.get_running_loop().time() >= deadline):
                await _mark("⌛ expired")           # no notice, no exchange
                return
            now = sd.DESKS.now()
            desk.begin_use(now)
            run_id = uuid.uuid4().hex
            tapped = f"[tapped: {button}]"
            # the pinned turn runs as the card's executing agent and is routed
            # through the bot that delivered the card
            origin = sd._desk_origin(
                resident_role=resident_role, desk_role=desk_role, bot_id=_card_bot(meta, bot),
                chat_id=chat_id, user_id=user_id, user_name="",
                message_id=meta.get("message_id"), cid=cid, text=tapped, turn_id=run_id)
            origin["stored_call"] = {"run_id": run_id, "runtime_name": runtime,
                                     "canonical": canonical, "label": button}
            origin["turn_scope"] = TurnScope.for_desk(
                origin, display_name=tools_mod._display_name_for_role(desk_role))
            task_text = _tap_prompt(button, runtime, canonical)
            # #1282: the pinned turn is given Casa's instruction only — not the
            # desk's exchanges, nor a frame calling the task the operator's
            # message — so nothing invites the model to judge the tap itself;
            # the desk log is still written after the tap
            context_text = ""
            # the permit AFTER the lock; it never waits
            permit = None
            limiter = tools_mod._specialist_limiter
            if limiter is not None:
                permit = limiter.try_acquire(tools_mod._delegation_scope(origin, desk_role))
                if permit is None:
                    await _mark("✖ busy")
                    await _notice(f"{label} is busy; the specialist will propose again, "
                                  "or type your verdict.")
                    sd.record_echo(bot, chat_id, f"{label} refused your tap ({button}): busy.")
                    return
            owner = PinnedRun(run_id=run_id, runtime_name=runtime, canonical=canonical,
                              label=button, build_input=build)
            plugin_name = str(getattr(rp, "name", "") or "")
            failure: str | None = None
            told_failed = False
            run = None
            mode = "normal"
            pending_cancel: BaseException | None = None

            async def _tell_failed(kind: str) -> None:
                nonlocal told_failed
                await _refuse_failed(_mark, _notice, label, button, bot, chat_id, kind)
                told_failed = True

            async def _on_alive() -> None:
                nonlocal failure
                failure = "processes alive"
                await _tell_failed(failure)

            def _disclose_cancelled(capture) -> None:
                """A cancelled tap whose call RAN (a capture exists): the
                receipt cannot be posted on a stopping channel — an ERROR
                names the run, the exchange is logged and the resident's line
                records the applied tap; nothing is silent."""
                logger.error("stored call %s: the tap was cancelled after its call ran; the "
                             "receipt was not posted", run_id)
                side = (sd.clip(str(capture.text or ""), STORED_CALL_RECEIPT_CHARS).split("\n", 1)[0]
                        if capture.kind in ("receipt", "no_post") else _posted(capture))
                stamp = sd.DESKS.now()
                desk.append("operator", tapped, stamp)
                desk.append("specialist", side, stamp)
                sd.record_echo(bot, chat_id, f"{label} applied your tap ({button})"
                                     f"{TELL_ECHO if capture.rewritten else ''}.")

            try:
                ov = agent_mod.origin_var.set(origin)
                qk = tools_mod._delegation_quota_key.set(run_id)
                pk = tools_mod._pinned_run.set(owner)
                try:
                    run = asyncio.create_task(tools_mod._run_delegated_agent(
                        cfg, task_text, context_text, resolution=build.resolution))
                finally:
                    agent_mod.origin_var.reset(ov)
                    tools_mod._delegation_quota_key.reset(qk)
                    tools_mod._pinned_run.reset(pk)
                _done, pending = await asyncio.wait({run}, timeout=tools_mod._DELEGATION_CEILING_S)
                if pending:
                    # §5.2.4: the notice at once; the desk and the permit stay
                    # held through the bounded termination path
                    mode = "ceiling"
                    failure = "timed out"
                    await _tell_failed(failure)
                else:
                    exc = run.exception()
                    if exc is not None:
                        failure = tools_mod._classify_error(exc).value
                        logger.warning("pinned run for %s failed: %s", desk_role,
                                       type(exc).__name__)
                    else:
                        _text, failure = _outcome_text(run.result())    # the text is discarded
            except asyncio.CancelledError as exc:
                mode = "cancel"
                pending_cancel = exc
            except Exception as exc:  # noqa: BLE001 — the launch itself failed: settle as a normal end
                failure = tools_mod._classify_error(exc).value
                logger.warning("pinned run for %s could not start: %s", desk_role, type(exc).__name__)
            # THE one release path — the normal end, the ceiling and a
            # cancellation alike (BRAIN, diff round 2): the confirmation, the
            # fault decision, the fd close and the
            # permit release happen together, in one function, shielded from
            # the cancellation; nothing in this turn releases by another route.
            _confirmed, interrupted = await _shielded(_settle_pinned_run(
                owner, run, mode, permit=permit, desk=desk, desk_role=desk_role, chat_id=chat_id,
                run_id=run_id, plugin_name=plugin_name, on_alive=_on_alive,
                tell_faulted=_tell_faulted))
            if pending_cancel is not None or interrupted:
                # cancelled before or DURING the settle (Terra, diff round 4): the
                # turn does not continue — a receipt captured meanwhile is
                # disclosed, never posted as if nothing had happened
                if owner.captured is not None and owner.captured.kind in ("receipt", "no_post", "delivered"):
                    _disclose_cancelled(owner.captured)
                if pending_cancel is not None:
                    raise pending_cancel
                raise asyncio.CancelledError()
            # §5.2.5/§5.4/§10: the outcome
            rb.POSTS.drain(run_id)
            capture = owner.settle(failure or "no_call")
            tell = capture.rewritten
            applied = f"{label} applied your tap ({button}){TELL_ECHO if tell else ''}."
            specialist_side = NO_RECEIPT
            replaced = in_place_unknown = False
            try:
                tapped_mid = meta.get("message_id")
                if (capture.kind == "receipt" and capture.next and capture.in_place
                        and not desk.faulted and isinstance(tapped_mid, int)
                        and not isinstance(tapped_mid, bool)):
                    # #1339: the card replaces the tapped one in place and no
                    # receipt is sent; anything short of a landed edit is
                    # today's sequence below — the receipt, then the card
                    why = await _post_next_card(capture.next, meta=meta, build=build,
                                                runtime=runtime, seg=seg, run_id=run_id,
                                                edit_message_id=tapped_mid,
                                                warning=TELL_LINE if tell else None)
                    if why is None:
                        specialist_side = sd.clip(str(capture.text or ""),
                                               STORED_CALL_RECEIPT_CHARS).split("\n", 1)[0]
                        sd.record_echo(bot, chat_id, applied)
                        replaced = True
                    else:
                        logger.info("stored call %s: the card was not replaced in place (%s)",
                                    run_id, why)
                        # an edit that may have landed leaves the message to the
                        # new card's record; the tapped card is not marked over it
                        in_place_unknown = why == UNCONFIRMED
                on_card = False
                tick = "✖" if capture.not_done else "☑"
                if (not replaced and not in_place_unknown and capture.kind == "receipt"
                        and capture.on_card and not capture.next and not tell):
                    # the plugin asked for its receipt as the card's last line: no
                    # message; a card line that did not land falls back to one
                    line = sd.clip(str(capture.text or ""), STORED_CALL_RECEIPT_CHARS).split("\n", 1)[0]
                    on_card = await _mark(f"{tick} {line}")
                    if on_card:
                        specialist_side = line
                        sd.record_echo(bot, chat_id, applied)
                if (not replaced and not in_place_unknown and not on_card
                        and capture.kind in ("receipt", "no_post", "delivered")):
                    await _mark(f"{tick} {button}")     # #1341: the tap is applied
                if replaced or on_card:
                    pass
                elif capture.kind in ("receipt", "no_post"):
                    receipt = sd.clip(str(capture.text or ""), STORED_CALL_RECEIPT_CHARS)
                    body = f"{TELL_LINE}\n{receipt}" if tell else receipt
                    admitted = origin["turn_scope"].admit(IntentKind.FINAL_REPLY, body)
                    labelled = admitted.with_text(f"{label}\n{admitted}")
                    post = rb.PostRecord(role=desk_role, operator_id=user_id, plugin=seg,
                                         slot="receipt", tool_use_id=run_id, owner=run_id,
                                         posted_at=sd.DESKS.now(), kind=rb.RECEIPT)
                    delivered = False
                    try:
                        outcome = await channel.send_response(labelled, {**context, "_post": post})
                        delivered = outcome is DeliveryOutcome.DELIVERED
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 — not proven
                        logger.warning("receipt send failed: %s", type(exc).__name__)
                    specialist_side = receipt.split("\n", 1)[0]
                    if not delivered:
                        await _notice(f"{label} applied your tap; the receipt did not go out.")
                    sd.record_echo(bot, chat_id, applied)
                    # #1302: the card follows a receipt that landed, never stands in for one
                    if capture.kind == "receipt" and capture.next and delivered and not desk.faulted:
                        why = await _post_next_card(capture.next, meta=meta, build=build,
                                                    runtime=runtime, seg=seg, run_id=run_id)
                        if why is not None:
                            logger.info("stored call %s: the next card was not shown (%s)",
                                        run_id, why)
                            await _notice(f"{label} could not show the next card ({why}).")
                elif capture.kind == "delivered":
                    specialist_side = _posted(capture)       # the landed post is the receipt
                    if tell and capture.text == OPERATOR_FILE:
                        # #1303: a file's caption is the plugin's, so the tell is
                        # one notice after it (a proposal composes its own)
                        await _notice(f"{label} {TELL_LINE}")
                    sd.record_echo(bot, chat_id, applied)
                else:
                    if tell:
                        # #1303: the call ran with changed arguments even though
                        # nothing landed — the tell precedes the refusal
                        await _notice(f"{label} {TELL_LINE}")
                    if capture.kind == "error":
                        kind = capture.text or "error"
                    elif capture.kind == "withheld":
                        kind = capture.text or "not delivered"
                    else:
                        kind = failure or "no_call"
                    if not told_failed:
                        await _tell_failed(kind)
            except asyncio.CancelledError:
                # a cancellation during the telling: an executed call is never
                # silent (Astra, diff round 2)
                if capture.kind in ("receipt", "no_post", "delivered"):
                    _disclose_cancelled(capture)
                raise
            done = sd.DESKS.now()
            desk.append("operator", tapped, done)
            desk.append("specialist", specialist_side, done)
    finally:
        if reservation is not None:
            reservation.release()             # idempotent: a cancel while waiting
        channel._release_typing(context, str(chat_id))


def _posted(capture: Any) -> str:
    """The desk exchange's side for a landed ``delivered`` capture."""
    return POSTED_FILE if capture.text == OPERATOR_FILE else POSTED_PROPOSAL


async def _post_next_card(value: str, *, meta: dict, build: Any, runtime: str, seg: str,
                          run_id: str, edit_message_id: int | None = None,
                          warning: str | None = None) -> str | None:
    """#1302: the next card a ``safe`` tap tool returned beside its receipt,
    posted right after the receipt in the same desk use. It is a proposal
    deposit like any other: judged by ``proposal_ok`` against the stored
    tool's own entry (its plugin and server) and the SAME captured maps the
    tap was re-checked against, then posted through ``_post_proposal`` (the
    live bound, the revision supersede, registered before the send). The
    identity is the tapped proposal's own record. Returns ``None`` when the
    card landed, else the reason word for the operator's notice —
    ``UNCONFIRMED`` for an in-place edit that may have landed (#1341).

    #1339: with *edit_message_id* the card replaces the tapped card in place
    (``_post_proposal`` edits that message); *warning* is the rewritten-call
    tell, composed into the card as for the ``More`` exception. A card with
    ``pages`` (#1377) is never an edit: it returns ``"pages"`` before anything is
    registered or sent, and the caller's fallback posts the receipt, the pages
    and the card."""
    import types
    import result_broker as rb
    import specialist_desk as sd
    from authz_grants import GrantIdentity
    from channels.tg_address import TgAddress
    entry = build.contract_map.tools.get(runtime)
    # the next card follows the tapped one: delivered where that card was
    identity = GrantIdentity(
        operator_id=int(meta["operator_id"]), bot_id=int(meta["bot_id"]),
        chat_id=int(meta["chat_id"]), enforcement_role=str(meta.get("role") or ""),
        artifact_id=str(meta.get("artifact_id") or ""), engagement_id="",
        delivery=TgAddress(int(meta["bot_id"]), int(meta["chat_id"])))
    call = types.SimpleNamespace(identity=identity, entry=entry, tool_use_id=run_id,
                                 contract_map=build.contract_map, protected=build.protected)
    try:
        parsed, _why = rb.proposal_ok(value, call)
    except Exception as exc:  # noqa: BLE001 — e.g. a lone surrogate the encoder refuses
        logger.info("next card refused: %s", type(exc).__name__)
        parsed = None
    if parsed is None:
        return "invalid"
    if edit_message_id is not None and parsed.get("pages"):
        return "pages"          # #1377: a card with pages is never an edit; the fallback posts it
    if edit_message_id is not None and (parsed.get("photo") or meta.get("photo")):
        return "photo"          # slice 5b: a photo card is neither edited into nor out of text
    post = rb.PostRecord(role=identity.enforcement_role, operator_id=identity.operator_id,
                         plugin=seg, slot=rb.OPERATOR_PROPOSAL, tool_use_id=run_id,
                         owner=run_id, posted_at=sd.DESKS.now(), kind=rb.OPERATOR_PROPOSAL)
    try:
        delivered, _detail, _event, withheld = await rb._post_proposal(
            identity, seg, rb.OPERATOR_PROPOSAL, call, parsed,
            rb.post_label(identity.enforcement_role), post, warning=warning,
            edit_message_id=edit_message_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — not proven
        logger.warning("next card post failed: %s", type(exc).__name__)
        return "not delivered"
    if delivered:
        return None
    if withheld == rb.EDIT_UNCONFIRMED:
        return UNCONFIRMED
    return "too many open" if withheld == rb._REASON_PROPOSAL_TOO_MANY else "not delivered"


async def _settle_pinned_run(owner: Any, run: "asyncio.Task | None", mode: str, *, permit: Any,
                             desk: Any, desk_role: str, chat_id: int, run_id: str,
                             plugin_name: str, on_alive: Callable[[], Any],
                             tell_faulted: Callable[[], Any]) -> bool:
    """THE one release path of a pinned run (BRAIN, diff round 2 — "same
    shape twice: cut the mechanism"). Called once per run, for the normal
    end (``mode == "normal"``: the controller's orderly close, then the
    termination path if a pinned process is still alive, told first through
    *on_alive*), the ceiling and a cancellation (``"ceiling"`` / ``"cancel"``:
    the termination path on the execution task). Its only input is the
    confirmation predicate — every pinned fd's own pidfd readable, every
    callback drained, every identity established — and on it, together and
    in this order: the fault decision (unconfirmed, or a teardown itself
    cancelled by a loop shutdown ⇒ the desk is faulted, told, and the health
    report refreshed), the fd close and the permit release. Nothing else in
    ``handle_tap`` performs any of these (pinned structurally in
    tests/test_desk_tap.py). The pinned run's transcript is not deleted here:
    like every delegated run's, it is kept for the retention pass
    (INV-ENG-028)."""
    import specialist_desk as sd
    import tools as tools_mod
    confirmed = False
    try:
        try:
            if mode == "normal":
                confirmed = await owner.finish()
                if not confirmed:
                    await on_alive()
                    confirmed = await owner.terminate()
            else:
                confirmed = await owner.terminate(run)
        except asyncio.CancelledError:
            confirmed = False               # the teardown itself was cancelled: unconfirmed
        if not confirmed:
            desk.fault = sd.DeskFault(run_id=run_id, plugin=plugin_name, since=sd.DESKS.now(),
                                      reason=f"run {run_id}: termination unconfirmed ({mode})")
            logger.error("desk %s/%s faulted: run %s termination unconfirmed (%s)",
                         chat_id, desk_role, run_id, mode)
            try:
                await tell_faulted()
                await tools_mod._refresh_plugin_health_live()   # the report says so at once
            except asyncio.CancelledError:
                logger.warning("faulted-desk telling interrupted by a cancellation")
            except Exception as exc:  # noqa: BLE001 — the next regeneration carries it
                logger.warning("faulted-desk telling failed: %s", type(exc).__name__)
    finally:
        owner.close_fds()
        if permit is not None:
            permit.release()                # inside the desk lock, before it is released
    return confirmed


async def _shielded(coro) -> "tuple[Any, bool]":
    """Run *coro* to completion although the caller is being cancelled: the
    work is a task of its own, awaited through a shield, and a repeated
    cancellation only re-arms the wait — the coroutine is bounded by its
    own deadlines, never by the caller's. Returns ``(result, interrupted)``:
    *interrupted* says a cancellation reached the caller meanwhile, so the
    caller can honour it once the work is done (Terra, diff round 4: a
    cancellation absorbed here must never let the turn continue as if it had
    not happened)."""
    task = asyncio.ensure_future(coro)
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # the caller is being cancelled: re-arm the wait for the work
            # (Terra, diff round 2: a cancellation landing as the work finishes
            # must not lose its result)
            interrupted = True
            continue
    # the work's own result — or its own cancellation, when the work's task
    # was itself cancelled (a loop shutdown): never a spin, never a swallow
    return task.result(), interrupted


async def _refuse_failed(mark, notice, label: str, button: str, bot_id: int, chat_id: int,
                         kind: str) -> None:
    """§10's ``✖ failed`` row: the keyboard line, the notice, the refused echo."""
    import specialist_desk as sd
    await mark("✖ failed")
    await notice(f"{label} could not apply your tap ({kind}).")
    sd.record_echo(bot_id, chat_id, f"{label} refused your tap ({button}): {kind}.")
