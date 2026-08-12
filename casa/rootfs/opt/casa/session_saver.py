# casa/rootfs/opt/casa/session_saver.py
"""Long-term save orchestration (design §4.2): freshness windows, transcript →
retain items, and the idempotent save_session entry point. Retains whole ended
conversations to Hindsight at session granularity (short-term covers live turns)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from atomic_io import PRIVATE, atomic_write_json
from channel_policy import writes_to_bank
from claude_agent_sdk import get_session_messages
from hindsight_ids import bank_id
from memory_provenance import build_retain_items
from personality_types import RetainedTurn, SpeakerProvenance
# #526: the "guard unconditionally" sentinel — save_session's
# ``expected_generation`` default must forward "omitted" (not None, which
# means "snapshotted a pre-restart entry with no live generation").
from session_registry import _UNCONDITIONAL as _GEN_UNCONDITIONAL
from speaker_provenance import provenance_from_mapping, provenance_mapping
from sensitivity import DEFAULT_TIER
from tier_classifier import classify_stats, classify_tier
from timekeeping import split_time_envelope

if TYPE_CHECKING:
    from agent import SessionEntrySnapshot

logger = logging.getLogger(__name__)

# Per-channel freshness windows (spec §3.3). Independent of SESSION_TTL.
_DEFAULT_VOICE_MIN = 30
_DEFAULT_TELEGRAM_H = 12

# #345: durable retry spool for failed gap retains. retain_cold_session runs
# DECOUPLED from the registry, so once the new turn overwrites the entry a
# failure has no in-registry retry — the spool (one JSON record per sid under
# /data) is the durable path; the FreshnessReaper drives retries each sweep.
_COLD_RETAIN_RETRY_DIR = "/data/cold-retain-retry"
# Hourly reaper cadence → ~2 days of retries before a poison record (e.g. a
# transcript the TTL sweeper already reaped) is dropped, loudly.
_COLD_RETAIN_MAX_ATTEMPTS = 48

# M29: bound the concurrency of per-item tier classification (each is a full
# claude-CLI subprocess + LLM turn). Classifying a long transcript serially
# blocked /new for minutes; a bounded gather cuts wall time while capping the
# number of concurrent CLI subprocesses.
_CLASSIFY_CONCURRENCY = 4


def freshness_window(channel: str) -> timedelta:
    """How long a session stays 'live' (resumable) before it goes cold and is
    eligible for save. Env-overridable: FRESHNESS_VOICE_MINUTES,
    FRESHNESS_TELEGRAM_HOURS."""
    if channel == "voice":
        return timedelta(minutes=int(os.environ.get("FRESHNESS_VOICE_MINUTES", _DEFAULT_VOICE_MIN)))
    # telegram + default for any conversational channel
    return timedelta(hours=int(os.environ.get("FRESHNESS_TELEGRAM_HOURS", _DEFAULT_TELEGRAM_H)))


def _message_text(message: Any) -> str:
    """Extract plain text from a SessionMessage.message payload. The payload is
    Any: either a string, or an Anthropic-style {role, content} where content is
    a string or a list of blocks ({type:'text', text:...}). Tool-use/result
    blocks contribute no text."""
    if isinstance(message, str):
        return message.strip()
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            return "".join(parts).strip()
    return ""


async def transcript_to_items(
    messages: list, *, speaker_provenance: SpeakerProvenance,
    user_provenance: SpeakerProvenance,
) -> list[dict[str, Any]]:
    """Turn an SDK transcript into provenance-bearing Hindsight retain items
    (design §4.2; tier model §2.4; personality Task 10). Each user turn is
    attributed to ``user_provenance`` (the trusted per-turn ingress identity) and
    each assistant turn to ``speaker_provenance`` (the resident's real persona
    identity), then funneled through :func:`build_retain_items` so every item
    carries its speaker provenance tag + canonical metadata alongside its tier.

    F1 (2026-07-09 bug review): dedup is now owned by ``build_retain_items`` — a
    line repeated N times collapses within-batch, and the content-addressed
    ``document_id`` (user_peer- or persona-identity-keyed) makes the same turn
    retained from any later session upsert to the SAME document instead of
    duplicating. ``classify_tier`` is passed by name so tests that monkeypatch
    ``session_saver.classify_tier`` still take effect."""
    turns: list[RetainedTurn] = []
    for m in messages:
        text = _message_text(getattr(m, "message", None))
        if not text:
            continue
        if getattr(m, "type", "") == "user":
            # #471: agent.py prepends a <current_time> envelope to the SENT
            # query text and the SDK transcript echoes it back — with it inside
            # the hash, an identical utterance minted a new document in every
            # session and the cross-session dedup content addressing exists for
            # (F1) never engaged. Split it off USER turns only (an assistant
            # reply starting with an envelope-shaped block is its own content),
            # so id and stored text are envelope-free while the wall-clock time
            # survives out-of-band on the item.
            ts, text = split_time_envelope(text)
            if not text.strip():
                continue
            turns.append(RetainedTurn(text, user_provenance, timestamp=ts))
        else:
            turns.append(RetainedTurn(text, speaker_provenance))
    if not turns:
        return []
    # #508: count failure-defaults across the whole batch so the operator sees
    # N-defaulted-of-M in ONE line instead of grepping per-item WARNs (at ~12%
    # per-call, small saves look clean by chance and only an aggregate over a
    # large save shows the rate). The scope is a contextvar inside
    # tier_classifier, so the monkeypatch-by-name contract above is untouched
    # and a test's fake classifier simply never counts. Counts only — never
    # item or reply content (leak-safety), and nothing is persisted on the
    # item (the structural-metadata-only rule).
    with classify_stats() as stats:
        items = await build_retain_items(
            turns, classify=classify_tier, classify_concurrency=_CLASSIFY_CONCURRENCY,
        )
    if stats.defaulted:
        logger.warning(
            "tier classification defaulted to %s for %d of %d items in this "
            "save (per-item warnings above carry each failure's shape)",
            DEFAULT_TIER, stats.defaulted, stats.total,
        )
    return items


async def save_session(
    channel_key: str, registry, semantic_memory, *, directory: str, channel: str,
    expected_sid: str | None = None,
    expected_generation: object = _GEN_UNCONDITIONAL,
) -> bool:
    """Idempotently retain an ended session to long-term memory (design §4.2; tier
    model §2.4; personality Task 10). Channels that fail write-trust (voice —
    recall-only) persist nothing. Atomically claims the entry; the persisted
    speaker/user identities come from the entry's own provenance snapshot (never
    a caller-passed role/user_peer), so a legacy or corrupt entry with no usable
    provenance releases the claim rather than retaining with invented authorship.
    On success retains the per-item tier+provenance-tagged transcript to the
    shared ``casa`` bank and removes the entry.

    ``expected_sid`` (#353): callers that decided WHICH session to save at a
    suspension point before this call (the freshness reaper's cold check, a
    reset's snapshot) pass the sid they judged. If a new turn re-registered the
    key in that window, the claim just placed on the NEW session is released
    and nothing is retained — otherwise the save would snapshot the active
    session and ``finish_save`` would remove its live resume pointer.

    ``expected_generation`` (#526): the registration generation the caller
    captured with its entry snapshot. Guards the claim itself (Sol design-r2:
    an unguarded claim can land on a NEWER registration and then be
    unreleasable once the downstream guards decline) and every conditional
    mutation below against a re-registration of the SAME sid, which
    ``expected_sid`` cannot see."""
    from agent import snapshot_session_entry

    if not writes_to_bank(channel):
        return False  # recall-only channel (e.g. voice): never persists facts
    if not await registry.try_begin_save(
        channel_key, expected_generation=expected_generation,
    ):
        return False  # missing/claimed/re-registered (reaper/next-turn race)
    snapshot = snapshot_session_entry(registry.get(channel_key))
    if snapshot is None or snapshot.speaker_provenance is None or snapshot.user_provenance is None:
        logger.debug(
            "save_session: %s has no usable provenance snapshot — releasing claim",
            channel_key,
        )
        await registry.clear_save_claim(
            channel_key, expected_generation=expected_generation,
        )
        return False
    if expected_sid is not None and snapshot.sdk_session_id != expected_sid:
        logger.debug(
            "save_session: %s re-registered under a newer session — releasing claim",
            channel_key,
        )
        await registry.clear_save_claim(
            channel_key, expected_generation=expected_generation,
        )
        return False
    sid = snapshot.sdk_session_id
    try:
        messages = await asyncio.to_thread(get_session_messages, sid, directory)
        items = await transcript_to_items(
            messages, speaker_provenance=snapshot.speaker_provenance,
            user_provenance=snapshot.user_provenance,
        )
        if items:
            await semantic_memory.retain(bank_id("casa"), items, async_=True)
        # Pass the saved sid so a user turn that re-registered this channel
        # mid-save (slow multi-minute reaper retain) is not clobbered (M24);
        # the generation guard (#526) extends that to a same-sid re-register.
        await registry.finish_save(
            channel_key, sid, expected_generation=expected_generation,
        )
        return True
    except asyncio.CancelledError:
        # #345: a cancel (shutdown, task teardown) bypassed the Exception arm
        # below and stranded the claim — the reaper then skipped this entry for
        # ~2 sweep intervals before C3 stale-claim recovery reopened it. Release
        # the claim on the way out. Loop-shield (cf. engagement_registry's
        # _settle_despite_cancel): the clear is a to_thread file commit that can
        # only be abandoned, never cancelled — re-shield until it settles even
        # if further cancels land, then re-raise. Best-effort: if the clear
        # itself fails, C3 recovery remains the backstop.
        settle = asyncio.ensure_future(registry.clear_save_claim(
            channel_key, sid, expected_generation=expected_generation,
        ))
        while not settle.done():
            try:
                await asyncio.shield(settle)
            except asyncio.CancelledError:
                continue
            except Exception:  # noqa: BLE001 — best-effort; C3 is the backstop
                break
        raise
    except Exception as exc:  # noqa: BLE001 — never crash a save; reaper retries
        logger.warning("save_session failed for %s: %s — will retry", channel_key, exc)
        await registry.clear_save_claim(
            channel_key, sid, expected_generation=expected_generation,
        )
        return False


def _cold_retry_path(retry_dir: str | Path, sdk_session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", sdk_session_id) or "unnamed"
    return Path(retry_dir) / f"{safe}.json"


def _spool_cold_retain(
    old: "SessionEntrySnapshot", *, directory: str, channel: str,
    retry_dir: str | Path,
) -> None:
    """#345: persist a durable retry record for a failed cold retain. Preserves
    an existing record's attempt count; itself best-effort (a spool failure
    logs — it must never crash the background retain's caller)."""
    try:
        path = _cold_retry_path(retry_dir, old.sdk_session_id)
        attempts = 0
        if path.exists():
            try:
                attempts = int(json.loads(path.read_text(encoding="utf-8")).get("attempts", 0))
            except (OSError, ValueError, TypeError, AttributeError):
                attempts = 0
        # 0o700: the retry records carry SDK session ids, transcript dirs and
        # speaker provenance (GHSA-569r-7crq-xr43). Tightening the directory is
        # what makes them unreachable; private_state repairs an existing one.
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write_json(str(path), {
            "sdk_session_id": old.sdk_session_id,
            "directory": directory,
            "channel": channel,
            "speaker_provenance": provenance_mapping(old.speaker_provenance),
            "user_provenance": provenance_mapping(old.user_provenance),
            "attempts": attempts,
        }, mode=PRIVATE)
    except Exception:  # noqa: BLE001 — spooling is best-effort
        logger.warning(
            "cold-retain retry: failed to spool sid=%s", old.sdk_session_id,
            exc_info=True,
        )


async def retry_spooled_cold_retains(
    semantic_memory, *, retry_dir: str | Path | None = None,
) -> None:
    """#345: drive the durable retry spool (called by the FreshnessReaper each
    sweep). Success removes the record; failure increments its attempt count
    and gives up loudly at ``_COLD_RETAIN_MAX_ATTEMPTS``; a structurally
    unreadable record is dropped rather than retried forever."""
    root = Path(retry_dir if retry_dir is not None else _COLD_RETAIN_RETRY_DIR)
    try:
        records = sorted(root.glob("*.json"))
    except OSError:
        return
    for path in records:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            sid = str(record["sdk_session_id"])
            directory = str(record["directory"])
            speaker = provenance_from_mapping(record["speaker_provenance"])
            user = provenance_from_mapping(record["user_provenance"])
        except (OSError, ValueError, KeyError, TypeError):
            logger.error(
                "cold-retain retry: unreadable record %s — dropping", path.name)
            path.unlink(missing_ok=True)
            continue
        try:
            messages = await asyncio.to_thread(get_session_messages, sid, directory)
            items = await transcript_to_items(
                messages, speaker_provenance=speaker, user_provenance=user)
            if items:
                await semantic_memory.retain(bank_id("casa"), items, async_=True)
            path.unlink(missing_ok=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — per-record; next sweep retries
            attempts = 0
            try:
                attempts = int(record.get("attempts", 0))
            except (ValueError, TypeError):
                pass
            attempts += 1
            if attempts >= _COLD_RETAIN_MAX_ATTEMPTS:
                logger.error(
                    "cold-retain retry: giving up on sid=%s after %d attempts (%s)",
                    sid, attempts, exc,
                )
                path.unlink(missing_ok=True)
                continue
            record["attempts"] = attempts
            try:
                atomic_write_json(str(path), record, mode=PRIVATE)
            except OSError:
                logger.warning(
                    "cold-retain retry: failed to update attempts for sid=%s", sid)


async def retain_cold_session(
    old: "SessionEntrySnapshot", *, directory: str, channel: str, semantic_memory,
    retry_dir: str | Path = _COLD_RETAIN_RETRY_DIR,
) -> None:
    """Retain a specific cold SDK session's transcript to the shared ``casa`` bank,
    OFF the turn's critical path and DECOUPLED from the session registry (no
    try_begin_save/finish_save). Used by the next-turn-after-gap path, where the
    registry entry for this channel is about to be overwritten by the new session —
    so a registry-claiming save (save_session) would race register(); this does not
    touch the registry at all. Channels failing write-trust (voice) retain nothing.

    Personality Task 10: consumes the immutable ``SessionEntrySnapshot`` directly.
    A legacy/corrupt snapshot with no usable speaker/user provenance retains
    NOTHING — memory is never written with invented authorship. The
    content-addressed ``document_id`` keeps re-retain idempotent."""
    if not writes_to_bank(channel):
        return
    if old.speaker_provenance is None or old.user_provenance is None:
        return  # legacy/corrupt snapshot: never retain with invented authorship
    try:
        messages = await asyncio.to_thread(get_session_messages, old.sdk_session_id, directory)
        items = await transcript_to_items(
            messages, speaker_provenance=old.speaker_provenance,
            user_provenance=old.user_provenance,
        )
        if items:
            await semantic_memory.retain(bank_id("casa"), items, async_=True)
    except asyncio.CancelledError:
        # Terra r1 (#345): shutdown cancels these unawaited background tasks —
        # without this arm the cancel bypassed the spool and lost exactly the
        # registry-decoupled transcript the spool protects. The spool write is
        # synchronous (no await), so it completes before the cancel propagates.
        _spool_cold_retain(old, directory=directory, channel=channel, retry_dir=retry_dir)
        raise
    except Exception:  # noqa: BLE001 — background; never surface to the turn
        logger.warning(
            "background cold-session retain failed for sid=%s — spooling for "
            "durable retry", old.sdk_session_id, exc_info=True,
        )
        # #345: the registry entry is (about to be) overwritten by the new
        # turn — without a durable record this transcript would be lost for
        # good on a transient outage. The reaper retries the spool each sweep.
        _spool_cold_retain(old, directory=directory, channel=channel, retry_dir=retry_dir)


async def reset_channel(
    channel_key: str, registry, semantic_memory, *, channel: str,
) -> None:
    """Explicit reset (design §4.2 #2, correction C2): retain the current session,
    then drop the pointer so the next turn starts fresh. Role + transcript
    directory are derived from the registry entry (the caller — e.g. the Telegram
    channel — does not need to know them). If there is no entry there is nothing
    to save; just return (the caller still acks)."""
    # Imported lazily: agent imports session_saver at module load, so a
    # top-level import here would cycle.
    from agent import agent_home_for_role_id, snapshot_session_entry

    # AR-4 (pooling spec): close any warm SDK client for this key FIRST —
    # disconnect sends stdin EOF, which is what makes the CLI flush the
    # transcript .jsonl this save is about to read (SDK #625).
    await registry.notify_reset(channel_key)
    entry = registry.get(channel_key)
    snapshot = snapshot_session_entry(entry)
    if snapshot is None:
        return
    role = snapshot.agent
    # Task 9: entries now store the canonical role_id; derive the transcript
    # cwd from it. A legacy short-role entry (pre-Task-9) falls back to the
    # bare-slug formula so its transcript is still found.
    try:
        directory = agent_home_for_role_id(role)
    except ValueError:
        directory = f"/config/agent-home/{role}"
    # Task 10: the reduced save_session reads speaker/user provenance from the
    # entry snapshot itself — no role=/user_peer= to pass here.
    # save_session is idempotent and removes the entry on a successful retain;
    # remove() afterwards guarantees the pointer is cleared even when the save
    # was a no-op (nothing to retain). Both carry the snapshot's sid (#317):
    # a follow-up turn that re-registered this key mid-save keeps its fresh
    # session instead of having it retained or its pointer deleted.
    # #526 deliberately NOT generation-guarded here: the reset's contract
    # (INV-MEM-006) is to retain-and-drop exactly the conversation the sid
    # names — a same-sid re-registration IS that conversation, whoever
    # refreshed the pointer, so removing it is the reset executing its
    # contract, not the ABA the generation guard exists to stop.
    await save_session(
        channel_key, registry, semantic_memory,
        directory=directory, channel=channel,
        expected_sid=snapshot.sdk_session_id,
    )
    await registry.remove(channel_key, expected_sid=snapshot.sdk_session_id)
