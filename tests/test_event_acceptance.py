"""Acceptance rehearsal for the plugin-events facility (#419).

ONE end-to-end, unit-level (no container) walkthrough driving the WHOLE
chain as an integrated whole, on a real filesystem, with only the
DISPATCH seam stubbed (the bus/channel are out of scope here — this proves
the facility, not Telegram delivery):

* ``plugin_events`` (declaration parse + digest/identity),
* ``event_acks`` (persistent operator consent — a REAL ``EventAckStore``
  rooted at a tmp path via ``CASA_EVENT_ACKS_PATH``),
* ``event_reconcile`` (the REAL ``compute_desired``/``reconcile_plugin_events``
  — the ONE writer of the published routing map),
* ``event_spool`` (a REAL ``EventSpool`` rooted at a tmp path via
  ``CASA_EVENT_SPOOL_ROOT`` — emit, fold, sweep, delivery records, ack),
* ``event_episodes`` (the REAL delivery worker's ``_worker_pass``, wired
  with the reconciler's live ``get_routed``/the real ack store),
* ``tools.ack_event`` (the REAL tool handler a subscriber calls to close
  the loop).

Structurally the sibling of ``tests/test_callback_acceptance.py`` (read
that file's harness style before touching this one) — but simpler: events
have no HTTP endpoint, no claim/publish handshake, and no per-flow
"result" phase; a wake carries no data, so the whole protocol is
declare -> consent -> reconcile -> emit -> fold -> nudge -> ack.

Scenarios:

(a) **positive path** — emitter declares ``casa.emits``, subscriber
    declares ``casa.subscribes``, the operator's consent is recorded,
    reconcile routes the pair, an emission is folded into a delivery
    record, the worker dispatches a headless wake (the composed
    instruction + the token are captured from the stub — never inferred
    from the internal ledger), the real ``ack_event`` tool handler is
    called with that EXACT token, the record settles ``done``/``acked``,
    and a SECOND emission opens a fresh generation with its own ladder
    and its own dispatched wake.
(b) **unconsented pair** — a declared-but-unconsented subscription routes
    nothing; the FIRST authoritative sweep deletes the queued emission
    outright (the consent watermark, decision 23) — so even recording
    consent AFTER the fact can never resurrect it. No delivery record is
    ever minted, no wake is ever dispatched.
(c) **forged emission for an undeclared event** — an emission written
    under a name the emitter never declared in ``casa.emits`` (and that no
    subscriber references) is inert for the identical reason: nothing
    routes it, so the watermark sweep deletes it. Proves the facility is
    not merely "consent-timing-sensitive" but genuinely indifferent to any
    unrouted emission, however it arrived.
(d) **INV-EV-005 — no envelope bytes, no ack token, anywhere in the log
    stream** — the whole positive-path run is replayed under ``caplog`` and
    every record's rendered message + raw args are swept for the token and
    for the raw canonical envelope bytes.
"""
from __future__ import annotations

import logging
import re
import time
from types import SimpleNamespace

import pytest

import event_acks
import event_attempts
import event_episodes as ee
import event_reconcile as er
import event_spool as es
import tools
from plugin_events import ack_identity, subscribe_declaration_digest

pytestmark = pytest.mark.asyncio

EMITTER = "finance"
EVENT = "invoice-created"
SUBSCRIBER = "reporting"
SUB_ARTIFACT = "art-sub-1"
EMITTER_ARTIFACT = "art-emit-1"
TARGETS = ("resident:assistant",)


# ---------------------------------------------------------------------------
# manifest / resolver / entries plumbing (mirrors test_event_reconcile.py's
# own helpers — kept self-contained here rather than imported across files)
# ---------------------------------------------------------------------------


def _manifest(*, emits=(), subscribes=()):
    casa = {}
    if emits:
        casa["emits"] = [{"name": n} for n in emits]
    if subscribes:
        casa["subscribes"] = [{"plugin": e, "event": ev} for e, ev in subscribes]
    return {"name": "x", "casa": casa}


def _rp(name, *, artifact_id, manifest):
    return SimpleNamespace(
        name=name, artifact_id=artifact_id, path=f"/store/{name}/{artifact_id}",
        version="1.0.0", manifest_name=name, manifest=manifest)


def _resolver(plugins):
    def resolve(target):
        return SimpleNamespace(registry_valid=True, plugins=list(plugins),
                               issues=[])
    return resolve


def _entries(*plugins, targets=TARGETS):
    rows = [{"name": p.name, "artifact_id": p.artifact_id,
             "targets": list(targets)} for p in plugins]

    def provider():
        return rows
    return provider


def _role_configs():
    return {"assistant": SimpleNamespace(channels=["telegram"])}


def _identity(subscriber, artifact_id, emitter, event, targets=TARGETS):
    digest = subscribe_declaration_digest({"plugin": emitter, "event": event})
    return ack_identity(subscriber, artifact_id, emitter, event, digest,
                        sorted(targets))


# ---------------------------------------------------------------------------
# the facility harness — a REAL spool + acks + reconciler + wired worker
# ---------------------------------------------------------------------------


class _Facility:
    def __init__(self, tmp_path, monkeypatch):
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.spool_root = tmp_path / "events"
        self.acks_path = tmp_path / "event_acks.json"
        monkeypatch.setenv("CASA_EVENT_SPOOL_ROOT", str(self.spool_root))
        monkeypatch.setenv("CASA_EVENT_ACKS_PATH", str(self.acks_path))

        self.spool = es.EventSpool(self.spool_root)
        self.acks = event_acks.EventAckStore(path=self.acks_path)
        # ack_event (tools.py) reads event_spool.get_spool() directly — the
        # module-level singleton is the seam a real subscriber's tool call
        # goes through, so it must resolve to THIS spool.
        monkeypatch.setattr(es, "get_spool", lambda: self.spool)
        # The reconciler's published routing map is a process-global —
        # start this test from the authoritative empty map (the conftest
        # autouse fixture already does this, restated here for clarity/
        # independence from fixture ordering).
        monkeypatch.setattr(er, "_routed", {})

        self.plugins: dict[str, SimpleNamespace] = {}
        self.dispatches: list[tuple[str, str, dict]] = []
        self.notes: list[str] = []
        self._targets = list(TARGETS)
        self.boot()

    # -- lifecycle ------------------------------------------------------

    def boot(self) -> None:
        """(Re)wire ``event_episodes`` from scratch. Every in-memory trace
        of a previous life drops; only the spool + acks survive."""
        ee._next_due = None
        ee._worker_task = None
        ee._kick = None

        async def _dispatch(role, text, context):
            self.dispatches.append((role, text, context))
            return True

        async def _notify(text):
            self.notes.append(text)

        async def _sleep(_s):
            return None

        def _resolve_registry_entry(subscriber):
            rp = self.plugins.get(subscriber)
            if rp is None:
                return None
            return {"targets": list(self._targets),
                    "artifact_id": rp.artifact_id, "manifest": rp.manifest}

        def _get_emitters():
            import plugin_store
            out = set()
            for name, rp in self.plugins.items():
                try:
                    emits = plugin_store.manifest_emits(rp.manifest, name)
                except Exception:  # noqa: BLE001
                    continue
                if emits:
                    out.add(name)
            return out

        ee.configure(
            dispatch=_dispatch,
            resolve_registry_entry=_resolve_registry_entry,
            get_routed=er.get_routed,
            get_installed=lambda: set(self.plugins),
            get_registry_valid=lambda: True,
            get_emitters=_get_emitters,
            get_acks=lambda: self.acks,
            get_spool=lambda: self.spool,
            notify_operator=_notify,
            sleep=_sleep,
        )

    def close(self) -> None:
        self.spool.close()

    # -- declare + consent + reconcile -----------------------------------

    def declare_emitter(self, name: str, *, events: tuple, artifact_id: str,
                        provision: bool = True) -> None:
        """Register an emitter's declaration. ``provision=True`` (the
        default, used by scenarios not under Critical-1 test here) mints its
        spool dirs immediately via a direct harness call. The positive-path
        helper (:func:`_routed_pair`) passes ``provision=False`` and instead
        proves PRODUCTION's own worker-driven provisioning
        (``event_episodes._provision_emitters``, wired through
        ``get_emitters``) does it."""
        manifest = _manifest(emits=events)
        self.plugins[name] = _rp(name, artifact_id=artifact_id, manifest=manifest)
        if provision:
            self.spool.ensure_emitter_dirs(name)

    def declare_subscriber(self, name: str, *, subscribes: tuple,
                           artifact_id: str) -> None:
        manifest = _manifest(subscribes=subscribes)
        self.plugins[name] = _rp(name, artifact_id=artifact_id, manifest=manifest)

    def consent(self, subscriber: str, artifact_id: str, emitter: str,
               event: str) -> str:
        digest = subscribe_declaration_digest({"plugin": emitter, "event": event})
        self.acks.record(subscriber, artifact_id, emitter, event, digest,
                         sorted(self._targets), time.time())
        return _identity(subscriber, artifact_id, emitter, event, self._targets)

    async def reconcile(self) -> list:
        return await er.reconcile_plugin_events(
            None, role_configs=_role_configs(), channel_manager=None,
            acks=self.acks, resolver=_resolver(list(self.plugins.values())),
            entries=_entries(*self.plugins.values(), targets=self._targets),
            prompt=False)

    # -- emit / passes ----------------------------------------------------

    def emit(self, emitter: str, event: str):
        return es.emit(self.spool.root / emitter, event)

    async def worker_pass(self) -> None:
        await ee._worker_pass()

    def delivery(self, *, emitter=EMITTER, event=EVENT, subscriber=SUBSCRIBER):
        return self.spool.read_delivery(emitter, event, subscriber)


@pytest.fixture()
def facility(tmp_path, monkeypatch):
    fac = _Facility(tmp_path, monkeypatch)
    try:
        yield fac
    finally:
        fac.close()


def _extract_token(instruction: str, *, emitter: str, event: str) -> str:
    """Parse the token out of the wake instruction EXACTLY the way a
    conforming subscriber would — never peek at the internal ledger."""
    m = re.search(
        r"ack_event\(emitter='" + re.escape(emitter) + r"', event='"
        + re.escape(event) + r"', token='([^']+)'\)", instruction)
    assert m, f"no ack_event(...) call found in instruction: {instruction!r}"
    return m.group(1)


async def _routed_pair(facility: _Facility) -> None:
    """Declare + consent + reconcile the canonical (finance/invoice-created
    -> reporting) pair; assert it actually routed.

    Critical-1: the emitter is declared with ``provision=False`` — no
    harness-side ``ensure_emitter_dirs()`` call anywhere in this helper.
    Instead, one production worker pass (the real
    ``event_episodes._worker_pass``, wired with ``get_emitters`` exactly as
    casa_core wires it) is what provisions the emitter's spool dirs and
    publishes its ready marker, proving production does this rather than
    the test harness."""
    facility.declare_emitter(EMITTER, events=(EVENT,), artifact_id=EMITTER_ARTIFACT,
                             provision=False)
    facility.declare_subscriber(SUBSCRIBER, subscribes=((EMITTER, EVENT),),
                                artifact_id=SUB_ARTIFACT)
    facility.consent(SUBSCRIBER, SUB_ARTIFACT, EMITTER, EVENT)
    issues = await facility.reconcile()
    assert issues == []
    routed = er.get_routed()
    assert routed is not es.ROUTING_UNAVAILABLE
    assert SUBSCRIBER in routed.get((EMITTER, EVENT), {})

    assert facility.spool.read_marker(EMITTER).state == es.MarkerState.ABSENT
    await facility.worker_pass()
    assert facility.spool.read_marker(EMITTER).state == es.MarkerState.PRESENT


# ---------------------------------------------------------------------------
# (a) positive path
# ---------------------------------------------------------------------------


async def test_positive_path_emit_wake_ack_then_second_generation(facility):
    await _routed_pair(facility)

    # emit -> fold -> a due, dispatchable delivery record
    facility.emit(EMITTER, EVENT)
    await facility.worker_pass()

    rec1 = facility.delivery()
    assert rec1 is not None and rec1["status"] == "pending" and rec1["gen"] == 1

    assert len(facility.dispatches) == 1
    role, instruction, context = facility.dispatches[0]
    assert role == "assistant"
    assert context["synthetic"] == "event_wake"
    assert context["emitter"] == EMITTER and context["event"] == EVENT
    assert f"Plugin '{EMITTER}' emitted the event '{EVENT}'" in instruction
    assert "do not ask" in instruction

    token = _extract_token(instruction, emitter=EMITTER, event=EVENT)
    assert token == rec1["ack_token"]     # the composed instruction and the
    # ledger agree on the SAME token — parsed independently, cross-checked
    # here only for test confidence (the handler below uses the PARSED one).

    # the real ack_event tool handler closes the loop
    res = await tools.ack_event.handler(
        {"emitter": EMITTER, "event": EVENT, "token": token})
    import json
    payload = json.loads(res["content"][0]["text"])
    assert payload["status"] == "ok"
    assert payload["outcome"] == "acked"
    assert payload["subscriber"] == SUBSCRIBER
    assert "token" not in payload            # INV-EV-005: never echoed back

    settled = facility.delivery()
    assert settled["status"] == "done" and settled["outcome"] == "acked"

    # a re-ack with the SAME (now-stale) token is a no-op, never an error —
    # exactly the idempotent-retry shape the tool's own docstring promises
    res2 = await tools.ack_event.handler(
        {"emitter": EMITTER, "event": EVENT, "token": token})
    payload2 = json.loads(res2["content"][0]["text"])
    assert payload2["outcome"] == "already_done"

    # second emission -> next pass is idle, opens gen 2 with a FRESH ladder,
    # and dispatches a second wake
    facility.emit(EMITTER, EVENT)
    await facility.worker_pass()

    rec2 = facility.delivery()
    assert rec2 is not None
    assert rec2["gen"] == 2
    assert rec2["status"] == "pending"
    # a FRESH ladder, not a continuation of generation 1's spent budget: the
    # SAME worker_pass call both opens (nudges=0) and immediately dispatches
    # the first-due slot (PHASE_OFFSETS[0] == 0.0), so nudges==1 here is the
    # record of exactly ONE dispatch on this NEW generation — never carried
    # forward from generation 1 (which itself ended at nudges==1 too, before
    # the ack settled it).
    assert rec2["nudges"] == 1
    assert rec2["ack_token"] != token                # a fresh credential too

    assert len(facility.dispatches) == 2
    role2, instruction2, context2 = facility.dispatches[1]
    assert role2 == "assistant"
    token2 = _extract_token(instruction2, emitter=EMITTER, event=EVENT)
    assert token2 == rec2["ack_token"]
    assert token2 != token


# ---------------------------------------------------------------------------
# (b) unconsented pair — the pre-consent watermark
# ---------------------------------------------------------------------------


async def test_unconsented_pair_is_swept_and_never_resurrected(facility):
    facility.declare_emitter(EMITTER, events=(EVENT,), artifact_id=EMITTER_ARTIFACT)
    facility.declare_subscriber(SUBSCRIBER, subscribes=((EMITTER, EVENT),),
                                artifact_id=SUB_ARTIFACT)
    # NO consent recorded — reconcile surfaces event_pending_ack and routes
    # nothing for this pair.
    issues = await facility.reconcile()
    codes = {i["reason_code"] for i in issues}
    assert "event_pending_ack" in codes
    routed = er.get_routed()
    assert routed.get((EMITTER, EVENT), {}) == {}

    emission = facility.emit(EMITTER, EVENT)
    assert emission.exists()

    # the FIRST authoritative worker pass sweeps the unrouted emission
    # outright (decision 23 — the consent watermark, not a TTL)
    await facility.worker_pass()
    assert not emission.exists()
    assert facility.delivery() is None
    assert facility.dispatches == []

    # consenting AFTER the fact cannot resurrect what the watermark already
    # deleted — the emission is simply gone
    facility.consent(SUBSCRIBER, SUB_ARTIFACT, EMITTER, EVENT)
    issues2 = await facility.reconcile()
    assert issues2 == []
    routed2 = er.get_routed()
    assert SUBSCRIBER in routed2.get((EMITTER, EVENT), {})   # NOW routed...

    await facility.worker_pass()
    assert facility.delivery() is None       # ...but nothing to fold — no
    assert facility.dispatches == []          # delivery record, no wake, ever


# ---------------------------------------------------------------------------
# (c) forged emission for an undeclared event name
# ---------------------------------------------------------------------------


async def test_forged_undeclared_event_emission_is_inert(facility):
    await _routed_pair(facility)     # the (finance, invoice-created) pair
    # IS routed and would dispatch — proves the negative below is about the
    # forged event specifically, not a broken harness.
    facility.emit(EMITTER, EVENT)    # the LEGITIMATE emission, for contrast

    forged_event = "not-a-real-event"
    assert forged_event != EVENT
    # No plugin declares this event and no subscriber references it — the
    # spool itself performs no declaration check on emit() (that policy
    # lives entirely in the reconciler), so this simulates a forged or
    # buggy emission landing on disk under a name nothing routes.
    forged = facility.emit(EMITTER, forged_event)
    assert forged.exists()

    await facility.worker_pass()

    # the LEGITIMATE pair still dispatched (the harness is alive)...
    assert len(facility.dispatches) == 1
    assert facility.dispatches[0][2]["event"] == EVENT
    # ...but the forged one is simply gone: swept by the identical watermark
    # mechanism (nothing routes (finance, not-a-real-event)), no delivery
    # record ever minted for it, and it never rode ANY dispatched wake.
    assert not forged.exists()
    assert facility.spool.read_delivery(EMITTER, forged_event, SUBSCRIBER) is None
    assert all(d[2]["event"] != forged_event for d in facility.dispatches)


# ---------------------------------------------------------------------------
# (e) Critical-3 — a trailing-dash event name never reaches the spool via
# a manifest declaration.
# ---------------------------------------------------------------------------


async def test_trailing_dash_declared_event_never_routes_to_the_spool(facility):
    """Spool-level Critical-3 pin: a plugin declaring
    ``casa.emits=[{"name": "invoice-"}]`` (trailing dash — the exact shape
    that used to misparse `<event>--<u32hex>.json`'s filename split and get
    swept as unfoldable) is refused at the manifest-parse layer
    (``plugin_events.parse_and_validate_emits``/``parse_and_validate_
    subscribes``), so ``event_reconcile``'s ``declared_events`` set never
    includes it and no ``(emitter, event)`` pair for it is ever published
    to the routed map this facility's real spool reads — it can never
    reach the spool through the manifest-driven production path at all.
    (The SUBSCRIBER's own ``casa.subscribes`` entry naming the same event
    is rejected too — ``event_invalid``, caught even earlier than the
    emitter-side ``event_emitter_missing`` gate.)"""
    bad_event = "invoice-"
    facility.declare_emitter(EMITTER, events=(bad_event,),
                             artifact_id=EMITTER_ARTIFACT, provision=False)
    facility.declare_subscriber(SUBSCRIBER, subscribes=((EMITTER, bad_event),),
                                artifact_id=SUB_ARTIFACT)

    # The EMITTER side, independently: its own casa.emits declaration is
    # refused at the manifest-extraction layer (the same call
    # event_reconcile.compute_desired makes to build `declared_events`).
    import plugin_store
    with pytest.raises(plugin_store.StoreError):
        plugin_store.manifest_emits(
            facility.plugins[EMITTER].manifest, EMITTER)

    issues = await facility.reconcile()
    codes = {i["reason_code"] for i in issues}
    assert "event_invalid" in codes

    routed = er.get_routed()
    assert routed is not es.ROUTING_UNAVAILABLE
    assert routed.get((EMITTER, bad_event), {}) == {}


# ---------------------------------------------------------------------------
# (d) INV-EV-005 — no envelope bytes, no ack token, anywhere in the logs
# ---------------------------------------------------------------------------


async def test_no_envelope_bytes_or_ack_token_reach_any_log_surface(
        facility, caplog):
    """The facility is deliberately QUIET on a clean run (every logger.info/
    warning/exception call in event_spool/event_episodes/event_reconcile
    sits on an error path or the boot-time init_spool() line this harness
    never calls) — so a vacuous ``caplog.records == []`` would prove
    nothing. To make the sweep genuine, the run also exercises the ONE
    real, non-error INFO log line the reconciler emits in normal operation:
    an opportunistic stale-ack prune (event_reconcile.py:573) triggered by
    a routine subscribe-declaration change — proving caplog really is
    wired to the SUT's loggers before trusting its silence on the
    token/envelope."""
    caplog.set_level(logging.DEBUG)

    await _routed_pair(facility)
    facility.emit(EMITTER, EVENT)
    await facility.worker_pass()

    rec = facility.delivery()
    token = rec["ack_token"]
    envelope = es.canonical_marker_bytes({"v": event_attempts.SCHEMA_VERSION})

    res = await tools.ack_event.handler(
        {"emitter": EMITTER, "event": EVENT, "token": token})
    import json
    assert json.loads(res["content"][0]["text"])["outcome"] == "acked"

    # a second generation for good measure — more log surface exercised
    facility.emit(EMITTER, EVENT)
    await facility.worker_pass()
    rec2 = facility.delivery()
    token2 = rec2["ack_token"]
    await tools.ack_event.handler(
        {"emitter": EMITTER, "event": EVENT, "token": token2})

    # the non-vacuity canary: a routine declaration change makes the
    # settled ack's identity stale, and the NEXT reconcile's opportunistic
    # prune drops it — a REAL log line through the SUT's own reconciler.
    facility.plugins[SUBSCRIBER].manifest = _manifest(subscribes=())
    await facility.reconcile()

    assert any("pruned" in r.getMessage() and "stale event ack" in r.getMessage()
               for r in caplog.records), \
        "the non-vacuity canary itself never logged — caplog is not wired"

    envelope_text = envelope.decode("utf-8", errors="replace")
    for record in caplog.records:
        rendered = f"{record.getMessage()} {record.args!r}"
        assert token not in rendered, (record.name, record.getMessage())
        assert token2 not in rendered, (record.name, record.getMessage())
        assert envelope_text not in rendered, (record.name, record.getMessage())
