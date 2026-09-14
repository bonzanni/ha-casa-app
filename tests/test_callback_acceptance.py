"""Acceptance rehearsal for the authorization-callback facility (v0.147).

ONE end-to-end, unit-level (no container) walkthrough that drives the WHOLE
facility as an integrated whole, on a real filesystem and — wherever the
endpoint is involved — through a real aiohttp client:

* ``plugin_callbacks`` (declaration parse + digest/identity),
* ``callback_acks`` (persistent operator consent),
* ``callback_reconcile`` (the overlay + ``ready.json`` / ``.index`` writer,
  base URL via the ``callback_urls.validated_base`` seam),
* ``callback_spool`` (mint v2 / claim / attempt-first publish / the attempt
  ledger / the ``collect`` + ``ack`` consumer helpers / sweep + removal
  records),
* ``callback_attempts`` (the ledger's schema and schedule),
* ``callback_http`` (the unauthenticated ``GET /callback/{name}`` endpoint),
* ``callback_episodes`` (redelivery until receipt, operator notes).

**The consumer contract is the spine of every scenario** (spec §7): a
consumer mints with an opaque ``meta``, collects a published result by the
atomic rename to ``results/.collect-<hash>-<uuid>``, reads it *after* the
rename, persists the exchange in its own store, and then acks by renaming
``attempts/<hash>.json`` → ``attempts/.ack-<hash>``. It **never unlinks the
held file** — casa's ack-teardown removes every artifact of the hash — so a
casa pass always finds a witness (a live hold pre-ack, the ack token
post-ack) and never has to infer an outcome out of nothing.

The scenarios:

(a) **gmail shape** — register → consent → reconcile routes + publishes
    ready/index → a consumer discovers its spool by
    ``sha256(realpath(plugin_root))``, reads its ``redirect_uri``, mints a
    state, the provider redirect lands via a real aiohttp client (303 →
    ``/callback/done``), the result is published, the delivery nudge names
    the handle, and the flow settles by collect + ack + teardown.
(b) **finance renewal loop** — a second mint/redirect/collect/ack for the
    SAME routed callback after the first has settled (the 180-day shape).
(c) **consumer-dead rehearsal** — a result published with no poller; the
    ledger written by the publish is the backstop a recovery pass finds.
(d) **declaration-digest stability** — an artifact-id change with an
    unchanged declaration keeps the ack and the routing with no re-prompt.
(e) **meta + minted_ts echo** — the mint envelope's opaque ``meta`` and the
    pending's mtime reach both the attempt file and the result record (§4).
(f) **casa restart between publish and collect** — a fresh module wiring
    with no in-memory hint still re-nudges, from spool truth alone (§8).
(g) **publish failure** — a ``done/publish_failed`` attempt exists and the
    browser still gets the one neutral 303 (§5, INV-CB-005).
(h) **witness chain** — an explicit walk over every crash point of
    collect→commit→ack: a casa pass always finds a live ``.collect-<h>-*``
    or an ``.ack-<h>`` token, never zero artifacts with an open question
    (§6/§7, the most-reviewed property of the design).
(i) **removal with in-flight flows** — a durable ``.removals`` record with
    the right count, converted by a worker pass into exactly one operator
    note (notify-then-mark, §10).
(j) **v1 envelope compat** — a legacy ``{"v": 1}`` pending completes the
    whole flow with ``meta`` None (§4).
(l) **eviction record** — a cap-destroyed flow leaves an ``evicted``
    outcome: no silent destruction (INV-CB-007, §9).
(m) **claimed-unconfirmed convergence** — a consumer that collects and dies
    without acking leaves the attempt open-but-``claimed`` while its hold
    lives, and the aged-out hold converges to ``expired_unread,
    claimed: true`` (§6).

(k) The v0.146→v0.147 boot migration is proved in
``tests/test_callback_migration.py`` (legacy episode store + pre-upgrade
artifacts) and is deliberately not duplicated here.

This is a conformance test: any failure here is a bug in the facility, not
something to paper over in the test.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import callback_attempts
import callback_episodes as ce
import callback_http
import callback_reconcile as cr
import callback_spool
import callback_urls
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from callback_acks import CallbackAckStore
from plugin_callbacks import ack_identity, declaration_digest, effective_name
from trigger_registry import TriggerRegistry
from yarl import URL

BASE = "https://casa.example.org"


# ---------------------------------------------------------------------------
# a plugin the resolver/registry can serve
# ---------------------------------------------------------------------------


def _manifest(names):
    return {"name": "x", "casa": {"callbacks": [{"name": n} for n in names]}}


def _plugin(*, name, artifact_id, path, callbacks):
    return SimpleNamespace(
        name=name, artifact_id=artifact_id, path=path,
        version="1.0.0", manifest_name=name, manifest=_manifest(callbacks))


def _resolver(plugins):
    def resolve(target):
        return SimpleNamespace(registry_valid=True, plugins=list(plugins),
                               issues=[])
    return resolve


def _entries(*plugins, targets=("resident:assistant",)):
    rows = [{"name": p.name, "artifact_id": p.artifact_id,
             "targets": list(targets)} for p in plugins]

    def provider():
        return rows
    return provider


def _role_configs():
    return {"assistant": SimpleNamespace(channels=["telegram"])}


# ---------------------------------------------------------------------------
# the facility harness — a REAL spool + registry + acks + wired episodes
# ---------------------------------------------------------------------------


class _Facility:
    """Everything a consumer and the endpoint need, on a real filesystem."""

    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.spool_root = tmp_path / "callbacks"
        self.store_root = tmp_path / "store"
        self.spool = callback_spool.CallbackSpool(self.spool_root)
        self.registry = TriggerRegistry(scheduler=None, app=None, bus=None)
        self.acks = CallbackAckStore(path=tmp_path / "callback_acks.json")

        # The public base URL flows through the validated-base seam so the redirect
        # URIs a consumer reads are the validated origin, not a raw string.
        monkeypatch.setattr(
            cr, "_base_url",
            lambda: callback_urls.validated_base({"PUBLIC_URL": BASE}))

        # Wire the delivery worker against this spool — since v0.147 the
        # durable ledger IS the spool's attempts dir, so there is nothing else
        # to isolate — with recording dispatch/notify doubles (mirrors
        # tests/test_callback_episodes.py's fixture — never patch a global
        # asyncio.sleep).
        monkeypatch.setattr(ce, "_worker_task", None)
        monkeypatch.setattr(ce, "_kick", None)
        monkeypatch.setattr(ce, "_next_due", None)
        self.dispatches: list[tuple[str, str, dict]] = []
        self.notes: list[str] = []
        self._targets = ["resident:assistant"]
        self.boot()

    def boot(self) -> None:
        """(Re)wire ``callback_episodes`` from scratch — a casa restart. Every
        in-memory trace of the previous life (hints, the computed wake) is
        dropped; only the spool survives, which is the point of scenario (f).
        """
        ce._pending_hints.clear()
        ce._next_due = None
        ce._worker_task = None
        ce._kick = None

        async def _dispatch(role, text, context):
            self.dispatches.append((role, text, context))
            return True

        async def _notify(text):
            self.notes.append(text)

        async def _sleep(_s):
            return None

        ce.configure(
            dispatch=_dispatch,
            resolve_registry_entry=lambda plugin: {"targets": self._targets},
            get_spool=lambda: self.spool,
            notify_operator=_notify,
            sleep=_sleep,
        )

    def close(self) -> None:
        self.spool.close()

    # -- artifact + consent + reconcile -------------------------------------

    def make_artifact(self, name: str, artifact_id: str) -> Path:
        art = self.store_root / name / artifact_id
        art.mkdir(parents=True, exist_ok=True)
        return art

    def consent(self, plugin: str, declared: str) -> str:
        """Record the operator's ack for one declared callback; return the
        consent identity."""
        eff = effective_name(plugin, declared)
        digest = declaration_digest({"declared": declared, "effective": eff})
        self.acks.record(plugin=plugin, effective=eff,
                         declaration_digest=digest)
        return ack_identity(plugin, eff, digest)

    async def reconcile(self, plugin) -> list:
        return await cr.reconcile_plugin_callbacks(
            trigger_registry=self.registry, role_configs=_role_configs(),
            acks=self.acks, spool=self.spool, resolver=_resolver([plugin]),
            entries=_entries(plugin), prompt=False)

    # -- casa's background passes ------------------------------------------

    async def casa_pass(self) -> None:
        """One reconciling casa pass over the ledger — what the boot seam and
        the periodic recovery job run (``attempts_pass``: materialize,
        re-derive, infer receipts, consume acks, apply the bounds)."""
        await ce.recovery(self.spool)

    async def worker_pass(self, *, ahead: float = 0.0) -> None:
        """One delivery pass: reconcile, dispatch every due nudge, convert
        removal records into operator notes.

        ``ahead`` moves the worker's CLOCK SEAM forward for the pass — never
        a real sleep — so an outcome-phase slot (+30 m) can be reached
        deterministically, the same way the spool tests inject ``now=``."""
        if not ahead:
            await ce._worker_pass()
            return
        at = time.time() + ahead
        real_now = ce._now
        ce._now = lambda: at
        try:
            await ce._worker_pass()
        finally:
            ce._now = real_now

    def sweep(self, *, now: float | None = None):
        return self.spool.sweep(now=time.time() if now is None else now)

    # -- the consumer's half ---------------------------------------------

    def discover(self, plugin_root: Path) -> dict:
        """A consumer knows only ``realpath($CLAUDE_PLUGIN_ROOT)``; it reads
        ``.index/<sha256(realpath)>.json`` to find its spool dir + redirect
        URIs, exactly as a real plugin would."""
        key = callback_spool.index_key(str(plugin_root))
        entry = self.spool_root / callback_spool.INDEX_DIR / f"{key}.json"
        return json.loads(entry.read_text())

    def plugin_dir(self, plugin_dir_name: str) -> Path:
        return self.spool_root / plugin_dir_name

    def pending_dir(self, plugin_dir_name: str) -> Path:
        return self.plugin_dir(plugin_dir_name) / callback_spool.PENDING_DIR

    def results_dir(self, plugin_dir_name: str) -> Path:
        return self.plugin_dir(plugin_dir_name) / callback_spool.RESULTS_DIR

    def attempts_dir(self, plugin_dir_name: str) -> Path:
        return self.plugin_dir(plugin_dir_name) / callback_spool.ATTEMPTS_DIR

    def mint(self, plugin_dir_name: str, state: str, meta=None) -> Path:
        """The consumer's start verb (the v2 envelope helper)."""
        return callback_spool.mint(self.plugin_dir(plugin_dir_name), state,
                                   meta)

    def attempt(self, plugin_dir_name: str, state_hash_hex: str) -> dict | None:
        """The flow's durable attempt record, or ``None`` when the ledger has
        none — the v0.147 replacement for the retired episode store."""
        for h, rec in self.spool.list_attempts(plugin_dir_name):
            if h == state_hash_hex:
                return rec
        return None

    def ready_payload(self, plugin_dir_name: str) -> dict:
        return json.loads(
            (self.plugin_dir(plugin_dir_name)
             / callback_spool.READY_NAME).read_text())

    def holds(self, plugin_dir_name: str, state_hash_hex: str) -> list[str]:
        """The consumer-held ``.collect-<hash>-<uuid>`` names for one flow."""
        results = self.results_dir(plugin_dir_name)
        if not results.is_dir():
            return []
        prefix = f"{callback_spool.COLLECT_PREFIX}{state_hash_hex}-"
        return sorted(n for n in os.listdir(results) if n.startswith(prefix))

    def ack_token(self, plugin_dir_name: str, state_hash_hex: str) -> bool:
        return (self.attempts_dir(plugin_dir_name)
                / f"{callback_spool.ACK_PREFIX}{state_hash_hex}").exists()

    def artifacts(self, plugin_dir_name: str, h: str) -> set[str]:
        """Every artifact class of one flow that is still on disk — the
        witness set §6 reasons about."""
        live = set()
        if (self.pending_dir(plugin_dir_name) / f"{h}.json").exists():
            live.add("pending")
        if (self.plugin_dir(plugin_dir_name) / callback_spool.CLAIMS_DIR
                / h).exists():
            live.add("claim")
        if (self.results_dir(plugin_dir_name) / f"{h}.json").exists():
            live.add("result")
        if self.holds(plugin_dir_name, h):
            live.add("hold")
        if (self.attempts_dir(plugin_dir_name) / f"{h}.json").exists():
            live.add("attempt")
        if self.ack_token(plugin_dir_name, h):
            live.add("ack_token")
        return live

    def collect(self, plugin_dir_name: str, state_hash_hex: str, *,
                ack: bool = True) -> dict:
        """The consumer's pickup, through the REFERENCE HELPERS and in the
        contract's order (spec §7):

        ``collect`` renames ``results/<h>.json`` to a consumer-held
        ``.collect-<h>-<uuid>`` (the rename's single winner arbitrates the
        race) and reads it AFTER the rename; the consumer then persists the
        exchange in its own durable store — its commit point, simulated here
        by simply having the record in hand — and only then acks by renaming
        the attempt file to ``.ack-<h>``.

        **The held file is never unlinked here.** That is load-bearing, not
        an omission: the hold is the flow's crash journal until the ack, and
        casa's ack-teardown is what removes it along with every other
        artifact of the hash.
        """
        record, held = callback_spool.collect(self.plugin_dir(plugin_dir_name),
                                              state_hash_hex)
        assert held.exists(), "the hold survives the read — it is the witness"
        if ack:
            assert callback_spool.ack(self.plugin_dir(plugin_dir_name),
                                      state_hash_hex) is True
        return record


@pytest.fixture()
def facility(tmp_path, monkeypatch):
    fac = _Facility(tmp_path, monkeypatch)
    try:
        yield fac
    finally:
        fac.close()


# ---------------------------------------------------------------------------
# aiohttp endpoint harness (reuses tests/test_callback_http.py idioms)
# ---------------------------------------------------------------------------


def _build_app(facility: _Facility) -> web.Application:
    app = web.Application()
    handler = callback_http.make_callback_handler(
        trigger_registry=facility.registry,
        spool_provider=lambda: facility.spool,
    )
    # Registration order is load-bearing: the static done route must win over
    # the wildcard.
    app.router.add_get("/callback/done", callback_http.make_done_handler())
    app.router.add_get("/callback/{name}", handler)
    return app


async def _redirect(client, effective: str, state: str, *, code="AUTHCODE"):
    """Drive the provider's browser redirect through the real endpoint,
    byte-exact (``encoded=True`` keeps the state off the client's re-encode
    path) and without following the 303."""
    target = f"/callback/{effective}?code={code}&state={state}"
    return await client.get(URL(target, encoded=True), allow_redirects=False)


async def _browser_redirect(facility: _Facility, effective: str, state: str,
                            *, code="AUTHCODE"):
    """One provider redirect through a real client against a real server."""
    app = _build_app(facility)
    async with TestClient(TestServer(app)) as client:
        return await _redirect(client, effective, state, code=code)


def _fresh_state() -> str:
    """A consumer-minted state in the endpoint's grammar ([A-Za-z0-9._~-],
    22-256 chars)."""
    return secrets.token_urlsafe(24)


async def _routed(facility: _Facility, plugin: str, declared: str,
                  artifact_id: str = "art-1"):
    """Register + consent + reconcile one plugin; return its artifact dir."""
    art = facility.make_artifact(plugin, artifact_id)
    p = _plugin(name=plugin, artifact_id=artifact_id, path=str(art),
                callbacks=(declared,))
    facility.consent(plugin, declared)
    assert await facility.reconcile(p) == []
    assert facility.registry.get_callback(
        effective_name(plugin, declared)) is not None
    return art


# ---------------------------------------------------------------------------
# (a) gmail shape — register → redirect → nudge → collect → ack → teardown
# ---------------------------------------------------------------------------


async def test_gmail_shape_end_to_end(facility):
    plugin, declared = "gmail", "authorize"
    eff = effective_name(plugin, declared)
    art = facility.make_artifact(plugin, "art-1")
    p = _plugin(name=plugin, artifact_id="art-1", path=str(art),
                callbacks=(declared,))

    # register + operator consent + reconcile
    identity = facility.consent(plugin, declared)
    issues = await facility.reconcile(p)
    assert issues == []
    assert facility.registry.get_callback(eff) is not None
    assert facility.acks.get(identity) is not None

    # the reconcile published the readiness marker + the discovery index entry
    assert (facility.plugin_dir(plugin) / callback_spool.READY_NAME).is_file()

    # consumer side: discover the spool by sha256(realpath(plugin_root)),
    # read the redirect_uri from the ready/index payload
    index = facility.discover(art)
    assert index["plugin_dir"] == plugin
    assert index["base_url"] == BASE
    redirect_uri = index["callbacks"][declared]["redirect_uri"]
    assert redirect_uri == f"{BASE}/callback/{eff}"
    # the ready.json marker carries the same map
    assert facility.ready_payload(plugin)["callbacks"][declared][
        "redirect_uri"] == redirect_uri

    # consumer mints a state (v2 envelope, opaque non-secret context) into its
    # own spool dir, registers redirect_uri with its provider, and hands
    # control to the browser
    state = _fresh_state()
    h = callback_spool.state_hash(state)
    facility.mint(plugin, state, meta={"kind": "initial", "session_ref": "s1"})

    # the provider's browser redirect lands at the endpoint
    r = await _browser_redirect(facility, eff, state)
    assert r.status == 303
    assert r.headers["Location"] == "/callback/done"

    # the result was published, publish-once, into results/<hash>.json — and
    # the attempt-first ordering means the ledger already knows the flow
    assert (facility.results_dir(plugin) / f"{h}.json").is_file()
    rec = facility.attempt(plugin, h)
    assert rec["status"] == "result_ready" and rec["outcome"] is None

    # the delivery nudge was kicked with the handle: the HTTP handler recorded
    # an in-memory hint; the worker turns it into a dispatched turn naming the
    # exact handle (result still present)
    assert (plugin, h) in ce._pending_hints
    await facility.worker_pass()
    assert len(facility.dispatches) == 1
    role, instruction, context = facility.dispatches[0]
    assert role == "assistant"
    assert f"(handle {h})" in instruction
    assert context["synthetic"] == "callback_nudge"

    # the agent collects the code through the contract: rename → read → commit
    # → ack. The hold is NEVER unlinked by the consumer.
    record = facility.collect(plugin, h)
    assert record["v"] == 1
    assert record["plugin"] == plugin
    assert record["effective"] == eff
    assert record["raw_query"] == f"code=AUTHCODE&state={state}"
    assert ["code", "AUTHCODE"] in record["query"]
    assert ["state", state] in record["query"]
    assert facility.holds(plugin, h), "the hold survives until ack-teardown"
    assert facility.ack_token(plugin, h), "ack = a durable rename, not an unlink"

    # casa consumes the receipt: full-flow teardown. The ack IS the record
    # (INV-CB-007 arm (a)), so nothing of the flow survives and nothing is
    # owed — no orphan to re-materialize, no re-nudge loop.
    await facility.casa_pass()
    assert facility.artifacts(plugin, h) == set()
    assert facility.attempt(plugin, h) is None
    await facility.worker_pass()
    assert len(facility.dispatches) == 1, "a settled flow nudges no further"


# ---------------------------------------------------------------------------
# (b) finance renewal loop — a second full flow on the SAME callback
# ---------------------------------------------------------------------------


async def test_finance_renewal_loop(facility):
    plugin, declared = "finance", "renew"
    eff = effective_name(plugin, declared)
    art = await _routed(facility, plugin, declared)

    index = facility.discover(art)
    assert index["callbacks"][declared]["redirect_uri"] == \
        f"{BASE}/callback/{eff}"

    async def _one_flow(kind: str) -> str:
        state = _fresh_state()
        h = callback_spool.state_hash(state)
        facility.mint(plugin, state, meta={"kind": kind})
        r = await _browser_redirect(facility, eff, state, code=f"code-{h[:6]}")
        assert r.status == 303
        await facility.worker_pass()
        record = facility.collect(plugin, h)
        assert record["plugin"] == plugin
        assert record["meta"] == {"kind": kind}, "the flow identifies itself"
        await facility.casa_pass()
        return h

    # first authorization settles fully — the ack tore the whole flow down
    h1 = await _one_flow("initial")
    assert facility.artifacts(plugin, h1) == set()

    # 180 days later the same callback is re-exercised — a fresh state, a fresh
    # hash, the SAME routed effective name. It must work identically.
    h2 = await _one_flow("renewal")
    assert h2 != h1
    assert facility.artifacts(plugin, h2) == set()
    assert len(facility.dispatches) == 2
    assert f"(handle {h2})" in facility.dispatches[-1][1]


# ---------------------------------------------------------------------------
# (c) consumer-dead rehearsal — the ledger is the backstop for a lost kick
# ---------------------------------------------------------------------------


async def test_consumer_dead_recovery_reenqueues_the_nudge(facility):
    plugin, declared = "gmail", "authorize"
    eff = effective_name(plugin, declared)
    await _routed(facility, plugin, declared)

    # A result lands with NO poller: publish it directly through the spool
    # (a redirect whose in-memory kick was lost to a crash), and run NO worker
    # pass — so nothing in memory remembers the flow.
    state = _fresh_state()
    h = callback_spool.state_hash(state)
    facility.mint(plugin, state)
    claim = facility.spool.claim(plugin, h, now=time.time())
    assert claim is not None
    assert facility.spool.publish_result(
        claim, {"v": 1, "plugin": plugin, "effective": eff},
    ) is callback_spool.PublishOutcome.PUBLISHED
    assert facility.dispatches == []     # no kick was delivered
    assert ce._pending_hints == set()

    # the ledger is the backstop: the publish itself wrote a due `result_ready`
    # attempt, so a recovery pass finds work no in-memory hint remembers
    await facility.casa_pass()
    rec = facility.attempt(plugin, h)
    assert rec["status"] == "result_ready"
    assert rec["next_nudge_ts"] is not None

    # and the worker then dispatches the nudge with the handle
    await facility.worker_pass()
    assert len(facility.dispatches) == 1
    assert f"(handle {h})" in facility.dispatches[0][1]


# ---------------------------------------------------------------------------
# (d) declaration-digest stability — an artifact bump keeps ack + routing
# ---------------------------------------------------------------------------


async def test_declaration_digest_survives_artifact_change(facility):
    plugin, declared = "gmail", "authorize"
    eff = effective_name(plugin, declared)
    identity = facility.consent(plugin, declared)

    art1 = facility.make_artifact(plugin, "art-1")
    p1 = _plugin(name=plugin, artifact_id="art-1", path=str(art1),
                 callbacks=(declared,))
    assert await facility.reconcile(p1) == []
    assert facility.registry.get_callback(eff) is not None
    # discoverable under the art-1 path
    assert facility.discover(art1)["plugin_dir"] == plugin

    # a routine upgrade: SAME declaration, new artifact id + path. The consent
    # identity binds the declaration digest, not the artifact, so the ack (and
    # the routing) survive with no re-prompt and no dark window.
    art2 = facility.make_artifact(plugin, "art-2")
    p2 = _plugin(name=plugin, artifact_id="art-2", path=str(art2),
                 callbacks=(declared,))
    issues = await facility.reconcile(p2)
    assert issues == []                                  # no callback_pending_ack
    assert facility.registry.get_callback(eff) is not None
    assert facility.acks.get(identity) is not None       # ack untouched

    # the discovery index followed the artifact: new path present, old retired
    index2 = facility.discover(art2)
    assert index2["callbacks"][declared]["redirect_uri"] == \
        f"{BASE}/callback/{eff}"
    old_key = callback_spool.index_key(str(art1))
    assert not (facility.spool_root / callback_spool.INDEX_DIR
                / f"{old_key}.json").exists()

    # a full flow still works on the upgraded artifact, ledger included
    state = _fresh_state()
    h = callback_spool.state_hash(state)
    facility.mint(plugin, state, meta={"kind": "post-upgrade"})
    r = await _browser_redirect(facility, eff, state)
    assert r.status == 303
    record = facility.collect(plugin, h)
    assert record["plugin"] == plugin
    assert record["effective"] == eff
    assert record["meta"] == {"kind": "post-upgrade"}
    await facility.casa_pass()
    assert facility.artifacts(plugin, h) == set()


# ---------------------------------------------------------------------------
# (e) meta + minted_ts echo end-to-end (spec §4)
# ---------------------------------------------------------------------------


async def test_meta_and_minted_ts_echo_end_to_end(facility):
    """Spec §4 (the binding fix): the mint envelope's opaque ``meta`` and the
    MINT clock reach both surfaces the consumer's next life can read — the
    attempt file and the published result record — value-preserving and
    unread by casa. ``minted_ts`` is the PENDING's mtime, carried through the
    claim's hard link, never the result's publish time."""
    plugin, declared = "gmail", "authorize"
    eff = effective_name(plugin, declared)
    await _routed(facility, plugin, declared)

    meta = {"kind": "renewal", "session_ref": "abc-123", "n": 7,
            "nested": {"provider": "example", "flags": [True, None]}}
    state = _fresh_state()
    h = callback_spool.state_hash(state)
    pending = facility.mint(plugin, state, meta=meta)
    minted_mtime = pending.stat().st_mtime

    r = await _browser_redirect(facility, eff, state)
    assert r.status == 303

    # the attempt file — casa's ledger and the consumer's read surface
    rec = facility.attempt(plugin, h)
    assert rec["meta"] == meta, "value-preserving, never interpreted"
    assert rec["minted_ts"] == minted_mtime, "the MINT clock, not publish time"

    # the result record — the same values, additively (record v stays 1)
    published = json.loads(
        (facility.results_dir(plugin) / f"{h}.json").read_text())
    assert published["v"] == 1
    assert published["meta"] == meta
    assert published["minted_ts"] == minted_mtime

    # and the consumer reads them back through the collect helper
    collected = facility.collect(plugin, h)
    assert collected["meta"] == meta
    assert collected["minted_ts"] == minted_mtime


# ---------------------------------------------------------------------------
# (f) casa restart between publish and collect (spec §8)
# ---------------------------------------------------------------------------


async def test_restart_between_publish_and_collect_renudges(facility):
    """Spec §8: the schedule is DURABLE. A publish kicks an in-memory hint;
    a restart before any collection destroys that hint and every other
    in-process trace — and the flow must still be re-nudged, from spool truth
    alone, because ``next_nudge_ts`` lives in the attempt file."""
    plugin, declared = "gmail", "authorize"
    eff = effective_name(plugin, declared)
    await _routed(facility, plugin, declared)

    state = _fresh_state()
    h = callback_spool.state_hash(state)
    facility.mint(plugin, state, meta={"kind": "initial"})
    r = await _browser_redirect(facility, eff, state)
    assert r.status == 303
    assert (plugin, h) in ce._pending_hints     # the pre-restart hint

    # casa restarts: fresh module wiring, no worker task, no hints, no
    # computed wake. Only the spool survives.
    facility.boot()
    assert ce._pending_hints == set()
    assert ce._next_due is None
    assert facility.dispatches == []

    # boot recovery reconciles the ledger; the worker then finds the due
    # attempt with nothing but the spool to go on
    await facility.casa_pass()
    assert facility.attempt(plugin, h)["status"] == "result_ready"
    await facility.worker_pass()

    assert len(facility.dispatches) == 1
    assert f"(handle {h})" in facility.dispatches[0][1]
    assert facility.attempt(plugin, h)["nudges"] == 1
    # the schedule advanced durably rather than re-firing on the next pass
    assert facility.attempt(plugin, h)["next_nudge_ts"] > time.time()

    # and the consumer, in its new life, completes the flow off the ledger
    assert facility.collect(plugin, h)["plugin"] == plugin
    await facility.casa_pass()
    assert facility.artifacts(plugin, h) == set()


# ---------------------------------------------------------------------------
# (g) publish failure — recorded outcome, still the one neutral 303
# ---------------------------------------------------------------------------


async def test_publish_failure_records_the_outcome_and_still_redirects(
        facility, monkeypatch):
    """Spec §5 + INV-CB-005: a publish that fails after the claim leaves a
    durable ``done/publish_failed`` attempt (the consumer can tell this apart
    from expiry, replay and eviction) — and the browser still gets the ONE
    neutral 303, because the response never varies with the outcome."""
    plugin, declared = "gmail", "authorize"
    eff = effective_name(plugin, declared)
    await _routed(facility, plugin, declared)

    state = _fresh_state()
    h = callback_spool.state_hash(state)
    facility.mint(plugin, state, meta={"kind": "initial"})

    # Fail the RESULT staging only (the `.claims/.tmp-<hash>` temp): the
    # attempt writes use the `_replace_json` staging grammar and stay intact,
    # which is the FAILED_RECORDED arm.
    real_write = callback_spool._write_new_file
    failing = {"on": True}

    def boom(name, dir_fd, data):
        if failing["on"] and name.startswith(callback_spool.TEMP_PREFIX):
            raise OSError(28, "No space left on device")
        return real_write(name, dir_fd, data)

    monkeypatch.setattr(callback_spool, "_write_new_file", boom)
    r = await _browser_redirect(facility, eff, state)
    failing["on"] = False

    assert r.status == 303, "the response is the same on every outcome"
    assert r.headers["Location"] == "/callback/done"

    rec = facility.attempt(plugin, h)
    assert (rec["status"], rec["outcome"]) == ("done", "publish_failed")
    assert rec["meta"] == {"kind": "initial"}, "meta survives the failure"
    assert not (facility.results_dir(plugin) / f"{h}.json").exists()
    # the state is consumed (fail-closed single-use): the handler discarded
    # the claim on the RECORDED failure, and nothing rewinds to pending
    assert facility.artifacts(plugin, h) == {"attempt"}

    # the consumer learns about it: a terminal, unacked attempt is nudged on
    # the outcome phase (+30 m from `ended_ts`) and the turn names the
    # outcome — #935: it no longer points at the attempt list, which only the
    # plugin can read
    await facility.casa_pass()
    assert facility.attempt(plugin, h)["outcome"] == "publish_failed"
    await facility.worker_pass()
    assert facility.dispatches == [], "the outcome phase is not due yet"
    await facility.worker_pass(
        ahead=callback_attempts.OUTCOME_PHASE_OFFSETS[0] + 1)
    assert facility.dispatches, "an unacked terminal outcome is delivered"
    assert "ended without collection" in facility.dispatches[0][1]

    # and the consumer's ack retires it
    assert callback_spool.ack(facility.plugin_dir(plugin), h) is True
    await facility.casa_pass()
    assert facility.artifacts(plugin, h) == set()


# ---------------------------------------------------------------------------
# (h) the witness chain (spec §6/§7) — an explicit walk over the crash points
# ---------------------------------------------------------------------------


async def test_witness_chain_at_every_consumer_crash_point(facility):
    """Spec §7's load-bearing ordering, walked explicitly.

    A conforming consumer NEVER unlinks its hold: it collects, commits to its
    own store, then acks — and ack-teardown removes the hold. So at every
    crash point of that sequence a casa pass finds a witness — a live
    ``.collect-<h>-*`` before the ack, the durable ``.ack-<h>`` token after
    it — and never has to answer "was this collected?" out of a zero-artifact
    state. This test crashes one flow at each point and checks exactly that.
    """
    plugin, declared = "gmail", "authorize"
    eff = effective_name(plugin, declared)
    await _routed(facility, plugin, declared)

    async def _publish() -> str:
        state = _fresh_state()
        h = callback_spool.state_hash(state)
        facility.mint(plugin, state, meta={"kind": "witness"})
        r = await _browser_redirect(facility, eff, state)
        assert r.status == 303
        return h

    # -- crash point 1: after publish, before the consumer ever ran ---------
    h1 = await _publish()
    assert "result" in facility.artifacts(plugin, h1)
    await facility.casa_pass()
    assert "result" in facility.artifacts(plugin, h1), "the result is the witness"
    assert facility.attempt(plugin, h1)["status"] == "result_ready"

    # -- crash point 2: after the collect rename, before the exchange -------
    h2 = await _publish()
    _record, held = callback_spool.collect(facility.plugin_dir(plugin), h2)
    assert facility.holds(plugin, h2) == [held.name]
    await facility.casa_pass()
    assert facility.holds(plugin, h2) == [held.name], "the hold is the witness"
    rec2 = facility.attempt(plugin, h2)
    assert rec2["status"] == "result_ready" and rec2["outcome"] is None, \
        "a live hold keeps the flow OPEN — never inferred collected"

    # -- crash point 3: after the consumer's durable commit, before the ack -
    h3 = await _publish()
    callback_spool.collect(facility.plugin_dir(plugin), h3)
    # (the consumer persisted the exchange in its own store here and died)
    await facility.casa_pass()
    assert facility.holds(plugin, h3), "the hold still witnesses the flow"
    assert facility.attempt(plugin, h3)["outcome"] is None

    # -- crash point 4: after the ack rename, before casa consumed it -------
    h4 = await _publish()
    callback_spool.collect(facility.plugin_dir(plugin), h4)
    assert callback_spool.ack(facility.plugin_dir(plugin), h4) is True
    assert facility.ack_token(plugin, h4), "the token is the post-ack witness"
    await facility.casa_pass()
    assert facility.artifacts(plugin, h4) == set(), \
        "the receipt supersedes the record: full teardown, nothing owed"

    # The property, stated over the whole walk: at NO point was the flow's
    # witness set empty while an open question remained.
    for h in (h1, h2, h3):
        witnesses = facility.artifacts(plugin, h) & {"result", "hold",
                                                     "ack_token"}
        assert witnesses, "an open flow always presents a witness"
        assert facility.attempt(plugin, h)["outcome"] is None


# ---------------------------------------------------------------------------
# (i) removal with in-flight flows — a durable record and exactly one note
# ---------------------------------------------------------------------------


async def test_removal_with_in_flight_flows_records_and_notes_once(facility):
    """Spec §10: plugin removal is abort-WITH-NOTICE, the documented
    INV-CB-007 exception. The per-flow ledger dies with the plugin, so the
    purge first writes a strict-durable ``.removals`` record counting the
    unsettled union — INCLUDING a flow the ledger never materialized — and
    the worker converts it into exactly one operator note (notify-then-mark:
    at-least-once, never a silent abort)."""
    plugin, declared = "gmail", "authorize"
    eff = effective_name(plugin, declared)
    await _routed(facility, plugin, declared)

    # flow 1: published and uncollected — it HAS an attempt file
    state_a = _fresh_state()
    ha = callback_spool.state_hash(state_a)
    facility.mint(plugin, state_a, meta={"kind": "published"})
    assert (await _browser_redirect(facility, eff, state_a)).status == 303
    assert facility.attempt(plugin, ha) is not None

    # flow 2: minted, never redirected, and never seen by a pass — the ledger
    # has NOTHING for it, so only the artifact union can count it
    state_b = _fresh_state()
    hb = callback_spool.state_hash(state_b)
    facility.mint(plugin, state_b, meta={"kind": "pending"})
    assert facility.attempt(plugin, hb) is None

    assert facility.spool.remove_plugin(plugin) is True
    assert not facility.plugin_dir(plugin).exists()

    records = facility.spool.list_removal_records()
    assert len(records) == 1
    _filename, rec = records[0]
    assert rec["plugin"] == plugin
    assert rec["reason"] == "remove"
    assert rec["count"] == 2, "the unmaterialized flow is counted too"
    assert rec["noted"] is False

    await facility.worker_pass()
    assert len(facility.notes) == 1
    assert plugin in facility.notes[0]
    assert "2 authorization" in facility.notes[0]
    assert facility.spool.list_removal_records()[0][1]["noted"] is True

    # a second pass must not re-notify (the mark is what stops it)
    await facility.worker_pass()
    assert len(facility.notes) == 1


# ---------------------------------------------------------------------------
# (j) v1 envelope compatibility (spec §4)
# ---------------------------------------------------------------------------


async def test_v1_envelope_still_completes_with_meta_none(facility):
    """Spec §4: ``{"v": 1}`` minters keep working. The legacy pending is
    written by hand (bypassing the v2 helper, exactly as a v0.146 consumer
    would have), and the whole flow — redirect, ledger, collect, ack,
    teardown — completes with ``meta`` None rather than refusing."""
    plugin, declared = "finance", "renew"
    eff = effective_name(plugin, declared)
    await _routed(facility, plugin, declared)

    state = _fresh_state()
    h = callback_spool.state_hash(state)
    (facility.pending_dir(plugin) / f"{h}.json").write_bytes(
        callback_spool.canonical_marker_bytes({"v": 1}))

    r = await _browser_redirect(facility, eff, state)
    assert r.status == 303

    rec = facility.attempt(plugin, h)
    assert rec["status"] == "result_ready"
    assert rec["meta"] is None
    assert rec["minted_ts"] is not None, "the mint clock still comes from mtime"

    record = facility.collect(plugin, h)
    assert record["plugin"] == plugin
    assert record["meta"] is None
    await facility.casa_pass()
    assert facility.artifacts(plugin, h) == set()


# ---------------------------------------------------------------------------
# (l) eviction record — INV-CB-007's "no silent destruction"
# ---------------------------------------------------------------------------


async def test_cap_eviction_leaves_an_evicted_outcome(facility, monkeypatch):
    """INV-CB-007 at the cap (spec §9): cap pressure used to destroy an
    in-flight authorization with no trace. Now every hash-named victim is
    terminalized ``evicted`` write-ahead FIRST, so the consumer can tell
    "destroyed by cap pressure" from expiry, and the operator-visible
    delivery keeps running on the outcome phase."""
    plugin, declared = "gmail", "authorize"
    eff = effective_name(plugin, declared)
    await _routed(facility, plugin, declared)

    hashes = []
    for kind in ("older", "newer"):
        state = _fresh_state()
        h = callback_spool.state_hash(state)
        facility.mint(plugin, state, meta={"kind": kind})
        assert (await _browser_redirect(facility, eff, state)).status == 303
        hashes.append(h)
    older, newer = hashes

    # Age the first result so the cap's within-rank mtime ordering is
    # deterministic (both flows carry an open attempt, so they share a rank).
    now = time.time()
    victim = facility.results_dir(plugin) / f"{older}.json"
    os.utime(victim, (now - 120, now - 120))

    monkeypatch.setattr(callback_spool, "MAX_RESULTS", 1)
    report = facility.sweep(now=now)

    assert report.deleted_capped == 1
    assert not victim.exists()
    assert (facility.results_dir(plugin) / f"{newer}.json").exists()

    rec = facility.attempt(plugin, older)
    assert (rec["status"], rec["outcome"]) == ("done", "evicted")
    assert rec["meta"] == {"kind": "older"}, "the record keeps the binding"
    assert rec["next_nudge_ts"] is not None, "the outcome is still delivered"
    assert facility.attempt(plugin, newer)["status"] == "result_ready"


# ---------------------------------------------------------------------------
# (m) claimed-unconfirmed convergence (spec §6)
# ---------------------------------------------------------------------------


async def test_claimed_but_unconfirmed_collect_converges(facility):
    """Spec §6's documented coarse label: a consumer that renames the result
    into a hold and then dies before its commit point leaves casa unable to
    say whether the payload was read. While the hold lives the attempt stays
    OPEN (never inferred ``collected`` — the rename is not a receipt); when
    the hold ages out on ``RESULT_TTL_S`` the flow is retired
    ``expired_unread`` with ``claimed`` set, which is exactly "the consumer
    may or may not have seen it — redo is safe".

    The flag is raised while the hold is still LIVE, not only at the terminal
    write-ahead: it is what a successor consumer reads to learn its
    predecessor already renamed the result, instead of retrying a ``collect``
    that can only ever ENOENT until the hold ages out.
    """
    plugin, declared = "gmail", "authorize"
    eff = effective_name(plugin, declared)
    await _routed(facility, plugin, declared)

    state = _fresh_state()
    h = callback_spool.state_hash(state)
    facility.mint(plugin, state, meta={"kind": "initial"})
    assert (await _browser_redirect(facility, eff, state)).status == 303

    # the consumer wins the rename — and dies right there
    _record, held = callback_spool.collect(facility.plugin_dir(plugin), h)

    await facility.casa_pass()
    rec = facility.attempt(plugin, h)
    assert rec["status"] == "result_ready" and rec["outcome"] is None, \
        "a rename is not a receipt — only the ack (or absence) settles it"
    assert rec["claimed"] is True, \
        "the open record tells a successor the payload may already be taken"
    assert facility.holds(plugin, h) == [held.name]

    # the credential's own TTL bounds the hold; the sweep retires it with the
    # write-ahead outcome BEFORE the inode goes (INV-CB-007)
    aged = time.time() - callback_spool.RESULT_TTL_S - 10
    os.utime(held, (aged, aged))
    report = facility.sweep()

    assert report.deleted_collect == 1
    assert facility.holds(plugin, h) == []
    rec = facility.attempt(plugin, h)
    assert (rec["status"], rec["outcome"]) == ("done", "expired_unread")
    assert rec["claimed"] is True, "the rename provably happened"
    assert rec["meta"] == {"kind": "initial"}

    # the outcome is still owed to the consumer: it is unacked, so delivery
    # continues on the outcome phase until the ack retires the flow
    await facility.worker_pass(
        ahead=callback_attempts.OUTCOME_PHASE_OFFSETS[0] + 1)
    assert facility.dispatches
    assert "ended without collection" in facility.dispatches[-1][1]
    assert callback_spool.ack(facility.plugin_dir(plugin), h) is True
    await facility.casa_pass()
    assert facility.artifacts(plugin, h) == set()


# ---------------------------------------------------------------------------
# (n) meta opacity across every log surface — INV-CB-009's "never logged" arm
# ---------------------------------------------------------------------------


async def test_meta_never_reaches_any_log_surface(facility, caplog):
    """INV-CB-009 red case: a consumer's opaque ``meta`` is stored and echoed
    but must never reach ANY log record — not the request path (INV-CB-006's
    ground), and not the surfaces v0.147 added: the publish path, the ledger
    passes, the sweep's write-ahead outcomes, and the delivery worker.

    The sentinel rides a full lifecycle (mint → redirect → publish → ledger
    pass → nudge → hold age-out → sweep → outcome nudge), with every record
    from every logger swept. It is checked against the rendered message AND
    the raw args, because a ``%``-style record hides its payload in ``args``
    until something formats it.
    """
    import logging

    plugin, declared = "finance", "renew"
    eff = effective_name(plugin, declared)
    art = facility.make_artifact(plugin, "art-1")
    p = _plugin(name=plugin, artifact_id="art-1", path=str(art),
                callbacks=(declared,))
    facility.consent(plugin, declared)
    assert await facility.reconcile(p) == []

    sentinel = "M3TA-S3NT1NEL-do-not-log"
    state = _fresh_state()
    h = callback_spool.state_hash(state)
    facility.mint(plugin, state, {"kind": "renewal", "ref": sentinel})

    caplog.set_level(logging.DEBUG)
    app = _build_app(facility)
    async with TestClient(TestServer(app)) as client:
        r = await _redirect(client, eff, state)
    assert r.status == 303

    # the ledger + delivery passes, then the hold's age-out and its
    # write-ahead outcome, then the outcome-phase nudge
    await facility.casa_pass()
    await facility.worker_pass()
    _got, held = callback_spool.collect(facility.plugin_dir(plugin), h)
    aged = time.time() - callback_spool.RESULT_TTL_S - 10
    os.utime(held, (aged, aged))
    facility.sweep()
    await facility.worker_pass(
        ahead=callback_attempts.OUTCOME_PHASE_OFFSETS[0] + 1)

    # the meta really did survive where it BELONGS — otherwise this test
    # would pass vacuously on a facility that simply dropped it
    assert facility.attempt(plugin, h)["meta"]["ref"] == sentinel

    assert caplog.records, "the lifecycle must have logged something"
    for record in caplog.records:
        rendered = f"{record.getMessage()} {record.args!r}"
        assert sentinel not in rendered, record.name
