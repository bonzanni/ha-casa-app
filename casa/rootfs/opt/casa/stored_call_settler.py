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
and the tap must arrive in that agent's own chat: see :func:`owns_card`.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Callable

from stored_calls import OPERATOR_FILE, TELL_LINE   # the one tell line, shared with the result hook

logger = logging.getLogger(__name__)

STORED_CALL_RECEIPT_CHARS = 4000
# _post_next_card's word for an in-place edit that may have landed (#1341)
UNCONFIRMED = "not confirmed"
TELL_ECHO = " — the CLI reported arguments changed by an installed hook"


def owns_card(endpoint_agent: str, role: str) -> bool:
    """The tap's executing agent owns the card: *role*, the agent the card
    records, is loaded, and the chat the tap arrived in is that agent's own
    (the endpoint's agent, *endpoint_agent*, is *role* itself, a resident
    included). A card of another agent is never settled in this chat: an
    agent's cards are posted only in its own chat, and a card of an agent
    that moved to its own chat is answered at the callback's ingress."""
    return _ownership_refusal(endpoint_agent, role) is None


def _ownership_refusal(endpoint_agent: str, role: str) -> "str | None":
    """The reason word a tap is refused with when *role*'s card may not be
    settled in *endpoint_agent*'s chat, else ``None``."""
    import tools as tools_mod
    if not role or tools_mod._agent_role_map.get(role) is None:
        return "agent unavailable"
    return None if role == endpoint_agent else "not this chat's card"


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


# -- the tap's lane use — the pinned one-call turn ---------------------------

def _pinned_origin(*, resident_role: str, executing_role: str, bot_id: int, chat_id: int,
                   user_id: int, message_id: Any, cid: str, text: str, turn_id: str) -> dict:
    """The pinned turn's origin: the chat's own Casa-owned context, the
    chat's agent as ``role`` and the card's agent as the executing role,
    depth 1 (the executing agent cannot delegate onward), the turn id as the
    quota key, the operator's read clearance, and the route ``_tg_bot``: the
    bot that delivered the card, which every reply of the turn goes back
    through. A tap carries Casa's text, never the operator's words."""
    from channels.tg_address import AddressError
    if not isinstance(bot_id, int) or isinstance(bot_id, bool) or bot_id <= 0:
        raise AddressError(f"not a Telegram bot id: {bot_id!r}")
    return {
        "_tg_bot": bot_id,
        "role": resident_role,
        "execution_role": executing_role,
        "channel": "telegram",
        "source": "telegram",
        "message_type": "channel_in",
        "chat_id": chat_id,
        "user_id": user_id,
        "user_name": "",
        "message_id": str(message_id) if message_id is not None else "",
        "cid": cid,
        "user_text": text,
        "delegation_depth": 1,
        "_delegation_id": turn_id,
        "_origin_route": "telegram",
        # The tap is the operator's own: the turn reads at his clearance, as a
        # delegation from his private chat does.
        "_origin_clearance": "private",
        "_operator_turn": True,
    }


def _tap_prompt(label: str, runtime_name: str, canonical: str) -> str:
    """The only text the model is given besides its role prompt — no earlier
    exchanges, no recalled memory, no operator frame (#1282). The model is
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
    """When the plan has an entry whose prefixes cover the name, the
    name must be in its allowed names (S8's per-prefix guard); no entry, no
    further restriction."""
    for entry in getattr(plan, "entries", None) or ():
        if runtime_name.startswith(tuple(entry.prefixes)):
            return runtime_name not in entry.allowed_names
    return False


async def handle_tap(
    *, channel: Any, resident_role: str, chat_id: int, user_id: int, cid: str,
    executing_role: str, meta: dict, idx: int, request_id: str, reservation: Any,
    bot_id: "int | None" = None,
) -> None:
    """One use of the executing agent's lane for a committed tap: under the
    lock the faulted check, the re-checks on ONE captured build input and the
    deadline; the permit after the lock; the pinned run created DIRECTLY as a
    task (never the bounded wrapper) under the ``PinnedRun`` ContextVar, the
    ceiling driven here; the outcome — a validated capture is the receipt,
    authoritative over an aborted turn; anything else is one labelled notice;
    the model's text is discarded — and one body-free note for the chat's
    agent. The reservation, the typing lease and the permit are released on
    every exit. *channel* is the endpoint the tap arrived on; the note and
    the turn's route are keyed by its bot (*bot_id*, which defaults to
    ``channel.bot_id``). *resident_role* is that endpoint's agent; the card's
    executing agent, *executing_role*, must own the card (:func:`owns_card`).
    """
    import agent as agent_mod
    import agent_lane
    import chat_notes
    import plugin_erasure
    import result_broker as rb
    import tools as tools_mod
    from channels import DeliveryOutcome
    from output_boundary import IntentKind, TurnScope
    from pinned_run import PinnedRun
    from stored_calls import stored_call_still_ok

    label = rb.post_label(executing_role)
    bot = channel.bot_id if bot_id is None else bot_id
    context = {"chat_id": str(chat_id), "cid": cid, "_tg_bot": bot}
    lane = agent_lane.LANES.get(executing_role)
    calls, options = list(meta.get("calls") or []), list(meta.get("options") or [])
    call = dict(calls[idx]) if 0 <= idx < len(calls) else {}
    button = str(options[idx]) if 0 <= idx < len(options) else "?"
    runtime = str(call.get("runtime_name") or "")
    canonical = str(call.get("canonical") or "")
    seg = str(meta.get("plugin_seg") or "")
    keep = keeps_card(meta, idx)

    async def _notice(line: str) -> None:
        try:
            await channel.deliver_casa_notice(chat_id, line)
        except Exception as exc:  # noqa: BLE001 — nothing more is attempted
            logger.warning("tap notice failed: %s", type(exc).__name__)

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
        """The keyboard line, one notice, the refused note; not a use."""
        await _mark(f"✖ {reason}")
        await _notice(f"{label} could not apply your tap ({reason}).")
        chat_notes.record(bot, chat_id, f"{label} refused your tap ({button}): {reason}.")

    async def _tell_faulted() -> None:
        line = agent_lane.faulted_line(label)
        await _notice(line)
        chat_notes.record(bot, chat_id, line)

    try:
        async with lane.use(reservation):
            if lane.faulted:
                await _mark("✖ faulted")
                await _tell_faulted()
                return
            # the re-checks, on one captured build input, under the lock
            why = _ownership_refusal(resident_role, executing_role)
            if why is not None:
                await _refuse(why)
                return
            cfg = tools_mod._agent_role_map.get(executing_role)
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
            # #1362: a keep_card tap admitted while its card was live sends its
            # file even if the TTL passed while it waited for the lane
            if not keep and (not isinstance(deadline, (int, float))
                             or asyncio.get_running_loop().time() >= deadline):
                await _mark("⌛ expired")           # no notice, no exchange
                return
            run_id = uuid.uuid4().hex
            tapped = f"[tapped: {button}]"
            # the pinned turn runs as the card's executing agent and is routed
            # through the bot that delivered the card
            origin = _pinned_origin(
                resident_role=resident_role, executing_role=executing_role,
                bot_id=_card_bot(meta, bot), chat_id=chat_id, user_id=user_id,
                message_id=meta.get("message_id"), cid=cid, text=tapped, turn_id=run_id)
            origin["stored_call"] = {"run_id": run_id, "runtime_name": runtime,
                                     "canonical": canonical, "label": button}
            origin["turn_scope"] = TurnScope.for_pinned(
                origin, display_name=tools_mod._display_name_for_role(executing_role))
            task_text = _tap_prompt(button, runtime, canonical)
            # #1282: the pinned turn is given Casa's instruction only — no
            # earlier exchanges, nor a frame calling the task the operator's
            # message — so nothing invites the model to judge the tap itself
            context_text = ""
            # the permit AFTER the lock; it never waits
            permit = None
            limiter = tools_mod._specialist_limiter
            if limiter is not None:
                permit = limiter.try_acquire(tools_mod._delegation_scope(origin, executing_role))
                if permit is None:
                    await _mark("✖ busy")
                    await _notice(f"{label} is busy; the specialist will propose again, "
                                  "or type your verdict.")
                    chat_notes.record(bot, chat_id, f"{label} refused your tap ({button}): busy.")
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
                names the run and the chat's note records the applied tap;
                nothing is silent."""
                logger.error("stored call %s: the tap was cancelled after its call ran; the "
                             "receipt was not posted", run_id)
                chat_notes.record(bot, chat_id, f"{label} applied your tap ({button})"
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
                    # the notice at once; the lane and the permit stay held
                    # through the bounded termination path
                    mode = "ceiling"
                    failure = "timed out"
                    await _tell_failed(failure)
                else:
                    exc = run.exception()
                    if exc is not None:
                        failure = tools_mod._classify_error(exc).value
                        logger.warning("pinned run for %s failed: %s", executing_role,
                                       type(exc).__name__)
                    else:
                        _text, failure = _outcome_text(run.result())    # the text is discarded
            except asyncio.CancelledError as exc:
                mode = "cancel"
                pending_cancel = exc
            except Exception as exc:  # noqa: BLE001 — the launch itself failed: settle as a normal end
                failure = tools_mod._classify_error(exc).value
                logger.warning("pinned run for %s could not start: %s", executing_role,
                               type(exc).__name__)
            # THE one release path — the normal end, the ceiling and a
            # cancellation alike: the confirmation, the fault decision, the fd
            # close and the permit release happen together, in one function,
            # shielded from the cancellation; nothing in this turn releases by
            # another route.
            _confirmed, interrupted = await _shielded(_settle_pinned_run(
                owner, run, mode, permit=permit, executing_role=executing_role, chat_id=chat_id,
                run_id=run_id, plugin_name=plugin_name, on_alive=_on_alive,
                tell_faulted=_tell_faulted))
            if pending_cancel is not None or interrupted:
                # cancelled before or DURING the settle: the turn does not
                # continue — a receipt captured meanwhile is disclosed, never
                # posted as if nothing had happened
                if owner.captured is not None and owner.captured.kind in ("receipt", "no_post", "delivered"):
                    _disclose_cancelled(owner.captured)
                if pending_cancel is not None:
                    raise pending_cancel
                raise asyncio.CancelledError()
            # the outcome
            rb.POSTS.drain(run_id)
            capture = owner.settle(failure or "no_call")
            tell = capture.rewritten
            applied = f"{label} applied your tap ({button}){TELL_ECHO if tell else ''}."
            replaced = in_place_unknown = False
            try:
                tapped_mid = meta.get("message_id")
                if (capture.kind == "receipt" and capture.next and capture.in_place
                        and not lane.faulted and isinstance(tapped_mid, int)
                        and not isinstance(tapped_mid, bool)):
                    # #1339: the card replaces the tapped one in place and no
                    # receipt is sent; anything short of a landed edit is
                    # today's sequence below — the receipt, then the card
                    why = await _post_next_card(capture.next, meta=meta, build=build,
                                                runtime=runtime, seg=seg, run_id=run_id,
                                                edit_message_id=tapped_mid,
                                                warning=TELL_LINE if tell else None)
                    if why is None:
                        chat_notes.record(bot, chat_id, applied)
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
                    line = chat_notes.clip(str(capture.text or ""),
                                           STORED_CALL_RECEIPT_CHARS).split("\n", 1)[0]
                    on_card = await _mark(f"{tick} {line}")
                    if on_card:
                        chat_notes.record(bot, chat_id, applied)
                if (not replaced and not in_place_unknown and not on_card
                        and capture.kind in ("receipt", "no_post", "delivered")):
                    await _mark(f"{tick} {button}")     # #1341: the tap is applied
                if replaced or on_card:
                    pass
                elif capture.kind in ("receipt", "no_post"):
                    receipt = chat_notes.clip(str(capture.text or ""), STORED_CALL_RECEIPT_CHARS)
                    body = f"{TELL_LINE}\n{receipt}" if tell else receipt
                    admitted = origin["turn_scope"].admit(IntentKind.FINAL_REPLY, body)
                    labelled = admitted.with_text(f"{label}\n{admitted}")
                    post = rb.PostRecord(role=executing_role, operator_id=user_id, plugin=seg,
                                         slot="receipt", tool_use_id=run_id, owner=run_id,
                                         posted_at=time.time(), kind=rb.RECEIPT)
                    delivered = False
                    try:
                        outcome = await channel.send_response(labelled, {**context, "_post": post})
                        delivered = outcome is DeliveryOutcome.DELIVERED
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 — not proven
                        logger.warning("receipt send failed: %s", type(exc).__name__)
                    if not delivered:
                        await _notice(f"{label} applied your tap; the receipt did not go out.")
                    chat_notes.record(bot, chat_id, applied)
                    # #1302: the card follows a receipt that landed, never stands in for one
                    if capture.kind == "receipt" and capture.next and delivered and not lane.faulted:
                        why = await _post_next_card(capture.next, meta=meta, build=build,
                                                    runtime=runtime, seg=seg, run_id=run_id)
                        if why is not None:
                            logger.info("stored call %s: the next card was not shown (%s)",
                                        run_id, why)
                            await _notice(f"{label} could not show the next card ({why}).")
                elif capture.kind == "delivered":
                    # the landed post is the receipt
                    if tell and capture.text == OPERATOR_FILE:
                        # #1303: a file's caption is the plugin's, so the tell is
                        # one notice after it (a proposal composes its own)
                        await _notice(f"{label} {TELL_LINE}")
                    chat_notes.record(bot, chat_id, applied)
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
                # a cancellation during the telling: an executed call is never silent
                if capture.kind in ("receipt", "no_post", "delivered"):
                    _disclose_cancelled(capture)
                raise
    finally:
        if reservation is not None:
            reservation.release()             # idempotent: a cancel while waiting
        channel._release_typing(context, str(chat_id))


async def _post_next_card(value: str, *, meta: dict, build: Any, runtime: str, seg: str,
                          run_id: str, edit_message_id: int | None = None,
                          warning: str | None = None) -> str | None:
    """#1302: the next card a ``safe`` tap tool returned beside its receipt,
    posted right after the receipt in the same lane use. It is a proposal
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
                         owner=run_id, posted_at=time.time(), kind=rb.OPERATOR_PROPOSAL)
    try:
        delivered, _detail, _event, withheld = await rb._post_proposal(
            identity, seg, rb.OPERATOR_PROPOSAL, call, parsed,
            rb.label_at(identity.enforcement_role, identity.delivery), post, warning=warning,
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
                             executing_role: str, chat_id: int, run_id: str,
                             plugin_name: str, on_alive: Callable[[], Any],
                             tell_faulted: Callable[[], Any]) -> bool:
    """THE one release path of a pinned run. Called once per run, for the normal
    end (``mode == "normal"``: the controller's orderly close, then the
    termination path if a pinned process is still alive, told first through
    *on_alive*), the ceiling and a cancellation (``"ceiling"`` / ``"cancel"``:
    the termination path on the execution task). Its only input is the
    confirmation predicate — every pinned fd's own pidfd readable, every
    callback drained, every identity established — and on it, together and
    in this order: the fault decision (unconfirmed, or a teardown itself
    cancelled by a loop shutdown ⇒ the agent is faulted, told, and the health
    report refreshed), the fd close and the permit release. Nothing else in
    ``handle_tap`` performs any of these (pinned structurally in
    tests/test_stored_call_tap.py). The pinned run's transcript is not deleted here:
    like every delegated run's, it is kept for the retention pass
    (INV-ENG-028)."""
    import agent_lane
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
            agent_lane.FAULTS.mark(executing_role, run_id, plugin_name,
                                   reason=f"run {run_id}: termination unconfirmed ({mode})")
            logger.error("agent %s faulted in chat %s: run %s termination unconfirmed (%s)",
                         executing_role, chat_id, run_id, mode)
            try:
                await tell_faulted()
                await tools_mod._refresh_plugin_health_live()   # the report says so at once
            except asyncio.CancelledError:
                logger.warning("faulted-agent telling interrupted by a cancellation")
            except Exception as exc:  # noqa: BLE001 — the next regeneration carries it
                logger.warning("faulted-agent telling failed: %s", type(exc).__name__)
    finally:
        owner.close_fds()
        if permit is not None:
            permit.release()                # inside the lane's lock, before it is released
    return confirmed


async def _shielded(coro) -> "tuple[Any, bool]":
    """Run *coro* to completion although the caller is being cancelled: the
    work is a task of its own, awaited through a shield, and a repeated
    cancellation only re-arms the wait — the coroutine is bounded by its
    own deadlines, never by the caller's. Returns ``(result, interrupted)``:
    *interrupted* says a cancellation reached the caller meanwhile, so the
    caller can honour it once the work is done (a cancellation absorbed here
    must never let the turn continue as if it had not happened)."""
    task = asyncio.ensure_future(coro)
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # the caller is being cancelled: re-arm the wait for the work
            # (a cancellation landing as the work finishes must not lose its result)
            interrupted = True
            continue
    # the work's own result — or its own cancellation, when the work's task
    # was itself cancelled (a loop shutdown): never a spin, never a swallow
    return task.result(), interrupted


async def _refuse_failed(mark, notice, label: str, button: str, bot_id: int, chat_id: int,
                         kind: str) -> None:
    """The ``✖ failed`` row: the keyboard line, the notice, the refused note."""
    import chat_notes
    await mark("✖ failed")
    await notice(f"{label} could not apply your tap ({kind}).")
    chat_notes.record(bot_id, chat_id, f"{label} refused your tap ({button}): {kind}.")
