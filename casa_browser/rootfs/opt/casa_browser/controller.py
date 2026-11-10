"""The run controller (Plan 1 Task 12): one leased run at a time, its browser sessions, its
policy, its broker and driver calls, its live view, and every way it ends.

``RunController`` implements ``api.Controller`` (every method a coroutine) over the components
Tasks 2–11 built; ``main()`` is the ``svc-browser-controller`` service (uid 1103): it serves the
``/v1`` API on port 8098, hosts the login driver's socket and is the launcher's one client.

**Concurrency.** One command lock serialises everything that touches the browser, the proxy,
the broker or a run's state. A run-scoped command first checks the run's state in the store
and refuses at once when it cannot act (so a model call never waits behind a run's start or a
login), and checks it again once it holds the lock. The command's work runs as a child task:
``cancel``, an interruption and ``request_live`` act on it *before* they wait for the lock —
cancel and interruption cut it at once, a live view drains it for up to ``DRAIN_S`` and then
cuts it; a cut command answers ``interrupted`` (``not_ready`` for a drain). Every driver
session a command opens is closed in its ``finally``.

**The proxy.** Every install raises the run's policy revision, carries the proxy start the
controller expects (a restarted proxy refuses it and the run is interrupted, never
reinstalled), and returns only once the proxy acknowledged it. One event consumer reads the
proxy's events for the active run — every ``PUMP_S`` and once more at a live view's Done,
after its lease ended — so each event is handled once.

**Ends.** A run that ends (finish, cancel, interruption) is moved to ``finishing`` and torn
down: live view, driver session, the proxy's policy (revoked and acknowledged), the browser
(closed, then the launcher's exit proof), the broker's ``end_run``. Only a confirmed teardown
lets it take its terminal state and the next queued run start; an unconfirmed one keeps the
lease and is retried.

Nothing here logs, stores or returns a credential, a raw address (except a public-session
page's address through the trusted sources route) or page content beyond masked observations.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote_plus, urlsplit

from casa_browser import confirmations as conf
from casa_browser import gate
from casa_browser.broker import BrokerError
from casa_browser.broker_client import arm_capture_then
from casa_browser.downloads import (
    FALLBACK_USER_AGENT,
    DownloadRefused,
    Quarantined,
    html_text,
    pick_fields,
)
from casa_browser.handles import PrivateTarget, StaleHandle
from casa_browser.login import learn_provider_binding
from casa_browser.masking import mask_text, mask_whole
from casa_browser.observe import ObservationRefused, holds_words, page_view_allowed
from casa_browser.pacing import TARGET_REFUSALS, ActionError, GuardRefused
from casa_browser.policy import Lease, RequestMeta, RunPolicy, SessionRef, decide, https_upgrade
from casa_browser.protocol import (
    ACTIVE_STATES,
    MAX_READ,
    TERMINAL_STATES,
    Action,
    ActionResult,
    ActiveRun,
    Change,
    ElementView,
    LiveGrant,
    Observation,
    ProtocolError,
    Run,
    RunRecord,
    SafeEvent,
    SafeNotice,
    Site,
    SiteGrant,
    SiteSummary,
    _https_origin,
    start_url_ok,
)
from casa_browser.proxy_client import ControlError

log = logging.getLogger("casa_browser.controller")

DRAIN_S = 15.0                 # a live view waits this long for an in-flight action
PUMP_S = 0.5                   # proxy event polling interval
TEARDOWN_RETRY_S = 30.0        # an unconfirmed teardown is retried this often
RECOVER_RETRY_S = 5.0          # service start: proxy revoke + exit proof retried this often
BROKER_TRIES = 3               # bounded retries of end_run (start, run end)
NAV_TIMEOUT_MS = 30_000
LOAD_TIMEOUT_MS = 10_000
DOWNLOAD_WAIT_S = 30.0
MAX_SOURCES = 200
MAX_SOURCE_URL = 800
MAX_SOURCE_TITLE = 120
SEARCH_ORIGIN = "https://www.google.com"
SEARCH_PATH = "/search?q="
RETENTION_EVERY_S = 6 * 3600
QUESTION_SWEEP_S = 30.0         # an open question past its hour expires within this; so does
                                # a live view the model asked for and nobody opened
DEPENDENCY_CODES = frozenset({"unavailable", "timeout", "internal"})
PICTURE_WAIT_S = 12.0           # a picture for a model takes at most this; then there is none
COOKIES_WAIT_S = 5.0            # reading or putting back session cookies takes at most this
LONG_TEXT = 200                 # more characters than this into a <textarea> are one insertion


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _Cut(Exception):
    """The command's work was cut by another command (cancel, interruption, live drain)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _Dependency(Exception):
    """A component the run needs failed (proxy, browser, broker): the run is interrupted."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Fail(Exception):
    """The run cannot start (its setup is not ready): it ends ``failed``."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------------- collaborators


@dataclass
class Components:
    store: Any
    browser: Any            # browser.BrowserManager
    proxy: Any              # proxy_client.ProxyClient
    broker: Any             # broker_client.BrokerClient (with ``store``)
    driver: Any             # login.DriverServer
    observer: Any           # observe.Observer
    registry: Any           # handles.HandleRegistry
    downloader: Any         # downloads.Downloader
    pacer: Callable         # page -> pacing.Pacer
    locale: Any = None      # EffectiveLocale | None (None: needs_locale)
    configured: bool = True
    language_source: str | None = None
    clock: Callable[[], datetime] = _utcnow
    grants: Any = None      # liveview.LiveView (made here when not given)
    retention: Callable | None = None   # (store, now) -> None, run in a thread
    token: Callable[[], str] = lambda: secrets.token_urlsafe(12)
    ops: Any = None         # gate.PageOps (made here when not given)


@dataclass(eq=False)
class _Live:
    purpose: str
    lease: Lease | None = None
    token: tuple | None = None
    capture_sid: str | None = None
    capture_id: str | None = None
    events: list = field(default_factory=list, repr=False)
    site_id: str | None = None
    question_id: str | None = None       # opened from this open question (slice 3, S3-3)
    asked_at: datetime | None = None     # when the model asked for it (request_live)


@dataclass(eq=False)
class _Active:
    run_id: str
    spec: Any
    revision: int = 0
    nonce: str | None = None
    ref: SessionRef | None = None
    session: Any = None
    policy: RunPolicy | None = None
    cursor: int = 0
    live: _Live | None = None
    driver_sid: str | None = None
    pump: asyncio.Task | None = None
    ending: tuple | None = None          # (terminal state, event kind, outcome)
    record: RunRecord | None = None
    broker_ended: bool = False
    changes: list = field(default_factory=list)
    capture: asyncio.Future | None = None  # a browser download POST …/download awaits
    pending: Any = None                  # gate.PendingJudgement awaiting its verdict
    question: Any = None                 # confirmations.FrozenQuestion awaiting its answer
    judged: list = field(default_factory=list)   # run_so_far: the last three judged actions
    quarantine: Any = None               # downloads.Quarantined held by a pending or a question
    # Slice 5: judgements the app starts itself (``login_item``, ``signin``), one per kind,
    # relayed by the plugin's next call; never superseded by a run command (plan S5-2).
    app_judgements: dict = field(default_factory=dict)
    vault_asked: set = field(default_factory=set)   # sites whose vault was asked once this run
    # Slice 6 (S6-2): the sign-in that stopped for a code the site sent — (site id, the browser
    # session, the page's generation, the page itself) — until one code is given or a new
    # sign-in starts.
    code_wait: tuple | None = field(default=None, repr=False)
    # The browser session the 1Password guest flow was last started for (set before it is
    # awaited, so one that was cancelled, failed or timed out counts too). Every browser start
    # relocks the extension, so another session object means it has not run yet.
    guest_for: Any = field(default=None, repr=False)
    # The handle the last step scrolled by: the same one again wheels on instead of going back.
    scroll_anchor: str | None = None
    # Signing in off the start page. ``looked``: the (browser session, address) pairs looked at
    # since Fido's last step (every executed step clears it; every sign-in adds the page it
    # ended on). ``sign_ins``: per site, the sign-ins of the run that acted, start page or
    # look, and whether the last one worked. ``not_sign_in``: the shapes the check judged not
    # the site's sign-in. ``notice``: the sign-in step a look's sign-in recorded.
    looked: set = field(default_factory=set, repr=False)
    sign_ins: dict = field(default_factory=dict)
    not_sign_in: set = field(default_factory=set, repr=False)
    notice: str | None = None
    # The address of the account page last loaded or observed: a login view on a new session
    # (the old one is gone) starts there rather than on a blank page.
    page_url: str | None = field(default=None, repr=False)
    # What the operator approved on a plan question: {"site_key", "plan"}.
    approved: dict | None = field(default=None, repr=False)
    # Session cookies of an account session closed for another session of this run, by site id:
    # memory only, never logged or sent, until that site's session reopens or the run ends.
    kept_cookies: dict = field(default_factory=dict, repr=False)
    # The browser's User-Agent, read when its session opened: a plain fetch needs no live page.
    user_agent: str = FALLBACK_USER_AGENT
    # Files this run fetched plainly: no cookie went to them, so their addresses stay whole.
    plain_files: dict = field(default_factory=dict, repr=False)


@dataclass(eq=False)
class _Look:
    """What a ``sign_in`` verdict covers: the site, the browser session and the address looked
    at, the shape the check was asked about, and what a sign-in there carries (``guest_ask``:
    None for the run's own rule; ``password_sent``: this look sent the password already)."""

    site_id: str
    session: Any = field(repr=False)
    url: str = field(repr=False)
    shape: tuple = field(repr=False)
    guest_ask: bool | None = None
    password_sent: bool = False
    fields: tuple = field(default=(), repr=False)    # the planned fields the check was shown
    fresh: bool = False                  # looked at again after a sign-in on a page that moved


def _sign_in_mapping(fields, named) -> dict | None:
    """R9: the check's ``fields`` ({"username": id, "password": id}) checked against the planned
    fields it was shown. Mechanical facts only: every id is a planned field's, a password goes
    only into a password field and a username never into one (so no field takes both), and a
    field the page marks keeps its own role. The named fields (in the judged document a named role
    goes to its named field alone), or None: no automatic sign-in."""
    by_id = {f["id"]: f for f in fields}
    named = {role: fid for role, fid in (named or {}).items() if fid is not None}
    out = {}
    for role, fid in named.items():
        f = by_id.get(fid)
        if f is None or f["password_field"] != (role == "password"):
            return None
        if f["role"] is not None and f["role"] != role:
            return None
        out[role] = fid
    return out


@dataclass(eq=False)
class _AppJudgement:
    """A judgement the app started (slice 5): its canonical packet and what applying it needs.
    The vault's item ids stay here (controller memory), never in a packet or a reply."""

    id: str
    kind: str                            # login_item | signin | sign_in
    packet_bytes: bytes = field(repr=False)
    digest: str = ""
    packet: dict = field(default_factory=dict, repr=False)
    site_id: str = ""
    host: str | None = None              # login_item: the sign-in page's real host
    items: list = field(default_factory=list, repr=False)   # login_item: the listed items
    capture_id: str | None = field(default=None, repr=False)   # signin: the broker's capture
    proposal: Any = field(default=None, repr=False)   # slice 5n: a sign-in card's login_item
    look: Any = field(default=None, repr=False)       # sign_in: the look it judges (_Look)

    def wire(self) -> dict:
        return {"id": self.id, "kind": self.kind, "packet": self.packet_bytes.decode("utf-8")}


# --------------------------------------------------------------------------- the controller


class RunController:
    def __init__(self, c: Components, *, pairing=None):
        from casa_browser.pairing import Pairing

        self.c = c
        self.store = c.store
        self.browser = c.browser
        self.proxy = c.proxy
        self.broker = c.broker
        self.driver = c.driver
        self.observer = c.observer
        self.registry = c.registry
        self.downloader = c.downloader
        self.clock = c.clock
        self.ops = c.ops if c.ops is not None else gate.PageOps()
        if c.grants is None:
            from casa_browser.liveview import LiveView
            c.grants = LiveView(store=c.store, browser=c.browser, clock=c.clock)
        self.grants = c.grants
        if hasattr(self.grants, "attach"):
            self.grants.attach(self)
        self.pairing = pairing or Pairing(self.store, clock=c.clock,
                                          on_revoke=self.revoke_authority)
        self.active: _Active | None = None
        self._lock = asyncio.Lock()
        self._events_lock = asyncio.Lock()
        self._work: asyncio.Task | None = None
        self._work_run: str | None = None
        self._cut_code: dict[asyncio.Task, str] = {}
        self._ready = False
        self._binding_key_id: str | None = None   # the proxy's binding key, read at start
        self._revoking = False
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ helpers

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(_consume)
        return task

    def _event(self, run_id: str, kind: str, outcome: str, text: str = "", *, handle=None,
               origin=None, screenshot_id=None) -> int:
        return self.store.append_event(run_id, SafeEvent(
            0, run_id, kind, origin, handle, mask_text(text).text[:500], outcome,
            screenshot_id, self.clock()))

    def _run(self, run_id: str) -> Run:
        run = self.store.get_run(run_id)
        if run is None:
            raise ProtocolError("not_found")
        return run

    @staticmethod
    def _refuse(run: Run, allowed: tuple[str, ...]) -> None:
        if run.state in allowed:
            return
        if run.state == "interrupted":
            raise ProtocolError("interrupted")
        if run.state in TERMINAL_STATES:
            raise ProtocolError("conflict")
        raise ProtocolError("not_ready")

    def _sites(self, spec) -> dict[str, Site]:
        out = {}
        for grant in spec.sites:
            site = self.store.get_site(grant.site_id)
            if site is not None:
                out[grant.site_id] = site
        return out

    @staticmethod
    def _access(spec, site_id: str | None) -> str | None:
        for grant in spec.sites:
            if grant.site_id == site_id:
                return grant.access
        return None

    @staticmethod
    def _start_url(site: Site) -> str:
        return site.start_page_private or (site.origins[0] + "/")

    @staticmethod
    def _origin(url: str) -> str | None:
        try:
            parts = urlsplit(url)
        except ValueError:
            return None
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        port = parts.port
        default = {"http": 80, "https": 443}[parts.scheme]
        host = parts.hostname.lower()
        return f"{parts.scheme}://{host}" + (f":{port}" if port and port != default else "")

    # ------------------------------------------------------------------ work and cuts

    def _authority(self) -> None:
        """Inside the lock: a command queued before a re-pair never acts after it (the
        revocation interrupts every run before it releases the lock)."""
        if self._revoking:
            raise ProtocolError("interrupted")

    async def _as_work(self, coro, run_id: str | None):
        """Run ``coro`` as the current in-flight work (the caller holds the lock)."""
        task = asyncio.ensure_future(coro)
        self._work, self._work_run = task, run_id
        try:
            return await task
        except asyncio.CancelledError:
            code = self._cut_code.pop(task, None)
            if code is not None and (asyncio.current_task().cancelling() == 0):
                raise _Cut(code) from None
            raise
        except BaseException:
            code = self._cut_code.pop(task, None)
            if code is not None:
                raise _Cut(code) from None
            raise
        finally:
            self._cut_code.pop(task, None)
            if self._work is task:
                self._work, self._work_run = None, None

    def _cut(self, code: str, run_id: str | None = None) -> asyncio.Task | None:
        work = self._work
        if work is None or work.done() or work is asyncio.current_task():
            return None
        if run_id is not None and self._work_run != run_id:
            return None
        if work in self._cut_code:
            return work      # already being cut: a second cancel must not reach its teardown
        active = self.active
        if active is not None and active.run_id == self._work_run and active.ending is not None:
            return None      # the run is already ending: its teardown is not cut
        self._cut_code[work] = code
        work.cancel()
        return work

    async def _command(self, run_id: str, allowed: tuple[str, ...], body, *,
                       supersede: bool = True):
        """A run-scoped command: state check, lock, state check, work; a dependency failure
        interrupts the run before the reply. Any command of the run supersedes its pending
        judgement (design §2.1), except the trusted callbacks that apply one."""
        self._refuse(self._run(run_id), allowed)
        async with self._lock:
            self._authority()
            run = self._run(run_id)
            active = self.active
            if active is None or active.run_id != run_id or active.ending is not None:
                self._refuse(run, ())
            self._refuse(run, allowed)
            if supersede:
                active.pending = None
            try:
                return await self._as_work(body(active), run_id)
            except _Cut as cut:
                raise ProtocolError(cut.code) from None
            except _Dependency as dep:
                await self._end(active, "interrupted", "run_interrupted", dep.reason)
                raise ProtocolError("interrupted") from None
            finally:
                self._sweep_quarantine(active)

    # ------------------------------------------------------------------ service start

    async def start(self) -> None:
        """Service start, before the API answers: every run a previous process left is
        interrupted (durable notices), ended at the broker and dropped from memory."""
        self.pairing.close_window_at_start()
        # A controller restart may leave the proxy holding a run's policy and a Chrome alive:
        # withdraw both, with acknowledgement and exit proof, before any run is ended or begun.
        while not await self._recover():
            await asyncio.sleep(RECOVER_RETRY_S)
        tried = set()
        # A question's frozen operation lived in memory: an open one expires, a confirming one
        # may have pressed (F8) and is recorded outcome_unknown.
        for run_id, _qid, state in self.store.end_questions(None, now=self.clock()):
            if state == "outcome_unknown":
                self._event(run_id, "action", "outcome_unknown",
                            "A confirmed action may have run before the app restarted.")
        for run_id in self.store.interrupt_nonterminal(now=self.clock()):
            self._event(run_id, "run_interrupted", "app_restart",
                        "The app restarted; the run was not resumed.")
            tried.add(run_id)
            await self._broker_end_run(run_id)   # owed since its interruption (account runs)
            self.registry.drop_run(run_id)
        # The B1 rule across a restart: a run whose end_run a previous process never got
        # acknowledged keeps the queue waiting until the broker answers.
        for run_id in self.store.owed_releases():
            if run_id not in tried:
                await self._broker_end_run(run_id)
        try:
            await self.broker.import_notices()
        except Exception:  # noqa: BLE001 — the next import (GET /v1/notices) retries
            log.warning("broker notices not imported at start")
        try:
            self.downloader.cleanup_partials()
        except OSError:
            log.warning("partial downloads not cleaned")
        await self.driver.start()
        self._ready = True
        if self.c.retention is not None:
            self._spawn(self._retention_loop())
        self._spawn(self._question_loop())
        if self.store.owed_releases():
            self._spawn(self._retry_owed())
        self._spawn(self._advance())

    async def _recover(self) -> bool:
        try:
            status = await self.proxy.status()
            key_id = status.get("binding_key_id")
            self._binding_key_id = key_id if isinstance(key_id, str) else None
            if status.get("run_id") is not None:
                await self.proxy.revoke(status["run_id"])
        except ControlError:
            log.warning("proxy not reachable at start; retrying")
            return False
        try:
            await self.browser.close(force=True)
        except Exception:  # noqa: BLE001
            log.warning("browser exit not confirmed at start; retrying")
            return False
        return True

    async def _retention_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.c.retention, self.store, self.clock())
            except Exception:  # noqa: BLE001 — retried at the next period
                log.warning("retention sweep failed")
            await asyncio.sleep(RETENTION_EVERY_S)

    async def _question_loop(self) -> None:
        while True:
            await asyncio.sleep(QUESTION_SWEEP_S)
            try:
                await self.expire_questions()
            except Exception:  # noqa: BLE001 — retried at the next period
                log.warning("question expiry failed")
            try:
                await self.expire_live()
            except Exception:  # noqa: BLE001 — retried at the next period
                log.warning("live view expiry failed")

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        active = self.active
        if active is not None:
            self.grants.revoke(active.run_id)
            self._cut("interrupted", active.run_id)
            async with self._lock:
                if self.active is active and active.ending is None:
                    await self._end(active, "interrupted", "run_interrupted", "app_stop",
                                    advance=False)
        try:
            await self.driver.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await self.browser.shutdown()
        except Exception:  # noqa: BLE001
            pass

    async def _retry_owed(self) -> None:
        """Owed end_runs (a previous process's, or start's own failures) are retried until
        every one is acknowledged; then the queue advances."""
        while self.store.owed_releases():
            await asyncio.sleep(TEARDOWN_RETRY_S)
            for run_id in self.store.owed_releases():
                await self._broker_end_run(run_id)
        self._spawn(self._advance())

    async def _broker_end_run(self, run_id: str) -> bool:
        for attempt in range(BROKER_TRIES):
            try:
                reply = await self.broker.end_run(run_id)
            except Exception:  # noqa: BLE001
                reply = {"error": "unavailable"}
            if "error" not in reply:
                self.store.clear_owed_release(run_id)
                return True
            if attempt + 1 < BROKER_TRIES:
                await asyncio.sleep(1.0)
        log.warning("broker end_run not acknowledged")
        return False

    # ------------------------------------------------------------------ hello, sites

    async def hello(self) -> dict:
        locale = self.c.locale
        return {"configured": bool(self.c.configured), "needs_locale": locale is None,
                "language_source": None if locale is None else locale.language_source}

    async def list_sites(self) -> list[SiteSummary]:
        out = []
        for s in self.store.list_sites():
            status = "ready" if (s.vault_item_id or not s.login_origins) else "needs_login"
            out.append(SiteSummary(s.site_id, s.display_name, status))
        return out

    async def register_site(self, payload: dict) -> str:
        site_id, _reply = await self.register_site_detailed(payload)
        return site_id

    async def register_site_detailed(self, payload: dict) -> tuple[str, dict]:
        """Registration with the broker's reply (the setup page shows an item's extra field
        names, never their contents)."""
        async with self._lock:
            self._authority()
            return await self._register_locked(payload)

    async def _register_locked(self, payload: dict) -> tuple[str, dict]:
        """The registration itself, under the lock its caller holds (the Sites page's, or a
        sign-in card's Yes inside its run command, slice 5n)."""
        reply: dict = {}
        site_id = "site_" + secrets.token_hex(8)
        if payload["login_origins"]:
            if payload["vault_item_id"]:
                reply = await self.broker.register_item(
                    site_id, payload["vault_item_id"], list(payload["login_origins"]),
                    payload["operator_ref"])
            else:
                reply = await self.broker.register_capture(
                    site_id, list(payload["login_origins"]), payload["operator_ref"],
                    payload["display_name"])
            if "error" in reply:
                raise ProtocolError({"not_found": "not_found", "unavailable": "not_ready",
                                     "timeout": "not_ready"}.get(reply["error"], "unsupported"))
        self.store.put_site(Site(site_id, payload["display_name"], payload["origins"],
                                 payload["login_origins"], payload["vault_item_id"], (),
                                 payload["start_page"], 1))
        return site_id, reply

    # ------------------------------------------------------------------ runs: queue and lease

    async def start_run(self, spec) -> tuple[Run, bool]:
        if self._revoking:
            raise ProtocolError("not_ready")   # a re-pair is ending every run: nothing new yet
        if not self._ready or self.c.locale is None:
            raise ProtocolError("not_ready")
        if spec.mode == "account" and not self.c.configured:
            raise ProtocolError("not_ready")
        for grant in spec.sites:
            if self.store.get_site(grant.site_id) is None:
                raise ProtocolError("not_found")
        run, created = self.store.create_run(spec, "run_" + secrets.token_hex(8), now=self.clock())
        if created and run.state == "cancelled":
            # its request was stopped before it existed (``cancel_request``): never leased
            self._event(run.run_id, "run_cancelled", "cancelled")
        elif created:
            self._event(run.run_id, "run_queued", "queued")
            self._spawn(self._advance())
        return run, created

    async def _advance(self) -> None:
        """Lease the next queued run when none is active and start it (holds the lock)."""
        async with self._lock:
            if self.active is not None or self._revoking or not self._ready:
                return
            if self.store.owed_releases():
                return   # a run's broker release is owed (B1): the queue waits for the broker
            run = self.store.lease_next_run()
            if run is None:
                return
            active = _Active(run.run_id, run.spec, revision=run.policy_revision)
            self.active = active
            try:
                await self._as_work(self._start_run(active), run.run_id)
            except _Cut:
                return  # the cutter (cancel, interruption) ends it
            except _Fail as fail:
                await self._end(active, "failed", "run_failed", fail.reason)
            except _Dependency as dep:
                await self._end(active, "interrupted", "run_interrupted", dep.reason)
            except Exception as err:  # noqa: BLE001 — never leave a leased run behind
                log.warning("run start failed (%s)", type(err).__name__)  # the type, never text
                if active.ending is None:
                    await self._end(active, "interrupted", "run_interrupted", "internal")

    async def _start_run(self, active: _Active) -> None:
        spec = active.spec
        sites = self._sites(spec)
        if len(sites) != len(spec.sites):
            raise _Fail("site_missing")
        if spec.mode == "account" and not self.c.configured:
            raise _Fail("not_configured")
        if self.c.locale is None:
            raise _Fail("needs_locale")
        accounts = [g.site_id for g in spec.sites if g.access == "account"]
        if spec.mode == "account":
            reply = await self.broker.begin_run(active.run_id, accounts)
            if "error" in reply:
                raise _Dependency("broker_" + reply["error"])
        first = sites[accounts[0]] if (spec.mode == "account" and accounts) else None
        await self._open_session(active, first)
        if first is not None and active.session is not None:
            await self._open_start_page(active, first)
        elif spec.mode == "open_web" and spec.start_url and active.session is not None:
            await self._open_public_start(active, spec.start_url)
        if self.store.transition(active.run_id, "starting", "running"):
            self._event(active.run_id, "run_started", "running")

    # ------------------------------------------------------------------ policy and sessions

    def _policy(self, active: _Active) -> RunPolicy:
        spec = active.spec
        sites = tuple(self._sites(spec).values())
        lease = active.live.lease if active.live is not None else None
        return RunPolicy(active.run_id, spec.mode, spec.sites, sites, None, lease,
                         active.revision, active.ref)

    async def _install(self, active: _Active) -> None:
        if active.ending is not None:
            # A run being torn down never regains authority.
            raise _Dependency("ending")
        if self._revoking:
            # Nor while a re-pair is ending every run: no install between the re-pair and the
            # revocation's teardown, whatever command was already past its own check.
            raise _Dependency("pairing_revoked")
        active.revision += 1
        policy = self._policy(active)
        try:
            revision = await self.proxy.install(policy, start=active.nonce)
        except ControlError as err:
            raise _Dependency("proxy_" + err.code) from None
        self.store.set_policy_revision(active.run_id, revision)
        active.policy = policy

    async def _close_browser(self, active: _Active) -> None:
        active.session = None
        self.registry.drop_run(active.run_id)
        try:
            await self.browser.close()
        except Exception:  # noqa: BLE001 — the launcher refuses a second Chrome anyway
            pass

    async def _keep_session_cookies(self, active: _Active) -> None:
        """Chrome closes on a switch and a site's session cookies die with it: read them first.
        A failure leaves the run going, signed out when it comes back."""
        session = active.session
        if session is None or session.kind != "account" or self.browser.session is not session:
            return
        try:
            cookies = await asyncio.wait_for(session.context.cookies(), COOKIES_WAIT_S)
        except Exception:  # noqa: BLE001 — its text is not logged: it could carry a value
            log.warning("session cookies not read before a switch")
            return
        kept = [c for c in cookies if c.get("expires") == -1]
        if kept and active.ending is None:
            active.kept_cookies[session.site_id] = kept

    async def _restore_session_cookies(self, active: _Active, session) -> None:
        """What ``_keep_session_cookies`` read, into the same site's new session, before it
        loads anything. Only a session of that very site id ever gets them."""
        kept = active.kept_cookies.pop(session.site_id, None)
        if not kept:
            return
        try:
            await asyncio.wait_for(session.context.add_cookies(kept), COOKIES_WAIT_S)
        except Exception:  # noqa: BLE001 — its text is not logged: it could carry a value
            log.warning("session cookies not put back after a switch")

    async def _open_session(self, active: _Active, site: Site | None) -> None:
        """Close whatever runs, install the session's policy, start it. The guest unlock is
        not run here: a sign-in that meets a passkey hint, or a sign-in live view, runs it."""
        spec = active.spec
        account = (spec.mode == "account" and site is not None
                   and self._access(spec, site.site_id) == "account")
        if active.session is not None or self.browser.session is not None:
            await self._keep_session_cookies(active)
            await self._close_browser(active)
        # No session's policy is installed while any Chrome of the browser user may still run:
        # the launcher's exit proof first (a profile switch whose close failed is interrupted).
        try:
            await self.browser.close(force=True)
        except Exception:  # noqa: BLE001
            raise _Dependency("browser_unconfirmed") from None
        try:
            status = await self.proxy.status()
        except ControlError as err:
            raise _Dependency("proxy_" + err.code) from None
        nonce = status["token"][0]
        if active.nonce is None:
            active.nonce = nonce
            active.pump = self._spawn(self._pump(active))
        elif nonce != active.nonce:
            raise _Dependency("proxy_restarted")
        # Before a browser starts while no Chrome runs (after an app or proxy start): close
        # the credential window a started proxy opened. Never while Chrome runs.
        if status.get("window_open") and self.browser.session is None:
            try:
                await self.proxy.close_window(status["token"])
            except ControlError as err:
                raise _Dependency("proxy_" + err.code) from None
        active.ref = SessionRef("account" if account else "public",
                                site.site_id if account else None, self.browser.next_generation)
        await self._install(active)
        try:
            session = await self.browser.open(site.site_id if account else None, spec.mode,
                                              self.c.locale)
        except Exception:  # noqa: BLE001 — a browser that cannot start is a dependency death
            raise _Dependency("browser_unavailable") from None
        active.session = session
        self.registry.drop_run(active.run_id)
        if session.generation != active.ref.generation:
            active.ref = replace(active.ref, generation=session.generation)
            await self._install(active)
        self._watch_downloads(active, session)
        await self._restore_session_cookies(active, session)
        try:
            active.user_agent = await self.downloader.user_agent(session)
        except Exception:  # noqa: BLE001 — only a plain fetch needs it, and has a fallback
            log.warning("the browser's user agent was not read")

    async def _ensure_session(self, active: _Active):
        """The run's browser session; after a forced close, a new one on the same site."""
        session = active.session
        if session is not None and self.browser.session is session:
            return session
        site = None
        if active.ref is not None and active.ref.kind == "account":
            site = self.store.get_site(active.ref.site_id)
        active.session = None
        await self._open_session(active, site)
        if active.session is None:
            raise ProtocolError("not_ready")  # no session came of the open
        return active.session

    def _watch_downloads(self, active: _Active, session) -> None:
        """A download the browser starts outside ``POST …/download`` is cancelled and
        deleted; inside it, it is handed to the waiting capture."""

        def on_download(download):
            fut = active.capture
            if fut is not None and not fut.done():
                fut.set_result(download)
                return

            async def drop():
                for op in ("cancel", "delete"):
                    try:
                        await getattr(download, op)()
                    except Exception:  # noqa: BLE001
                        pass
            self._spawn(drop())

        def on_page(page):
            try:
                page.on("download", on_download)
            except Exception:  # noqa: BLE001
                pass

        try:
            for page in list(session.context.pages):
                on_page(page)
            session.context.on("page", on_page)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ broker and driver

    async def _close_driver(self, active: _Active, sid: str) -> bool:
        """``close_session``, the controller's only cleanup call. True when the driver had to
        close the browser (K1): the session is gone. An unconfirmed close interrupts."""
        session = active.session
        try:
            await self.driver.close_session(sid)
        except BrokerError:
            raise _Dependency("browser_unconfirmed") from None
        finally:
            if active.driver_sid == sid:
                active.driver_sid = None
        if session is not None and self.browser.session is not session:
            active.session = None
            self.registry.drop_run(active.run_id)
            return True
        return False

    async def _driver_session(self, active: _Active, kind: str, site: Site) -> str:
        try:
            sid = await self.driver.open_session(kind, active.run_id, site.site_id,
                                                 active.session, self.proxy, site.login_origins)
        except BrokerError:
            raise _Dependency("driver_" + kind) from None
        active.driver_sid = sid
        return sid

    def _login_needed(self, active: _Active, reason: str, text: str | None = None) -> str:
        reason = reason if isinstance(reason, str) and reason.isascii() and len(reason) <= 40 \
            else "unknown"
        text = text or "The site needs the operator in a login live view."
        self._event(active.run_id, "login_needed", reason, text)
        active.notice = mask_text(text).text[:500]
        return reason

    async def _guest(self, active: _Active, site: Site) -> bool:
        """The 1Password guest unlock, best effort: only a passkey needs the extension (slice 5v:
        a fresh profile's extension is signed in, a restarted browser's unlocked, and the
        login answers 1Password's passkey prompt for the site's linked item). A state other
        than unlocked or signed in stops nothing and asks for nothing (slice 5s). True when the
        driver closed the browser under it (K1); the caller says what that means for it. The
        caller sets ``guest_for`` before awaiting it."""
        sid = await self._driver_session(active, "guest", site)
        reply: dict = {}
        try:
            reply = await self.broker.unlock_guest(active.run_id, site.site_id, sid)
        finally:
            forced = await self._close_driver(active, sid)
        if "error" in reply:
            raise _Dependency("broker_" + str(reply["error"]))
        if forced:
            return True
        if reply.get("state") not in ("unlocked", "signed_in"):
            reason = reply.get("reason")
            log.info("guest unlock not completed (%s)", reason if isinstance(reason, str)
                     and reason.isascii() and reason.isidentifier() and len(reason) <= 40
                     else "unknown")
        return False

    async def _login(self, active: _Active, site: Site, url: str, *,
                     guest_ask: bool | None = None, password_sent: bool = False) -> None:
        """A login needs, in order: the browser on the site's profile, the policy installed,
        a ``login`` driver session (its first lease), the start page; then the broker. While
        the guest flow has not run in this browser session, the broker stops on a passkey
        hint (``needs_guest``): the guest flow runs once, then the sign-in once more from the
        start page. A form with a box the page marks with no role (``needs_check``) is left
        as it is for the look: Fido's next observe brings it to Casa's check, which names the
        fields."""
        active.code_wait = None              # a new sign-in replaces a code report (S6-2)
        session = active.session
        if guest_ask is None:
            guest_ask = active.guest_for is not session
        sid = await self._driver_session(active, "login", site)
        reply: dict = {}
        try:
            await self._goto(active, url)
            reply = await self.broker.login(active.run_id, site.site_id, sid,
                                            guest_ask=guest_ask, password_sent=password_sent)
        finally:
            forced = await self._close_driver(active, sid)
        self._looked_now(active)
        if "error" in reply:
            raise _Dependency("broker_" + str(reply["error"]))
        if forced:
            self._ledger(active, site, "failed", "timeout")
            self._login_needed(active, "timeout")
            return
        if reply.get("status") == "needs_guest" and guest_ask:
            active.guest_for = session
            if await self._guest(active, site) or active.session is not session:
                self._ledger(active, site, "failed", "timeout")
                self._login_needed(active, "timeout")   # K1: the session is gone, no retry
                return
            await self._login(active, site, url, guest_ask=False,
                              password_sent=password_sent or reply.get("password_sent") is True)
            return
        self._ledger(active, site, reply.get("status"), reply.get("reason"))
        if reply.get("status") == "needs_code":
            self._code_asked(active, site, reply.get("page_generation"))
            return
        if reply.get("status") == "no_form":
            # Slice 5u: the sign-in filled nothing and found no form — it claims nothing; the
            # model reading the page decides whether he is signed in or needs the live view.
            # A form that rendered late is the look's too.
            if reply.get("reason") in ("needs_check", "no_form") and active.session is session:
                page = session.page             # this page is the look's, whatever came before
                if page is not None and not page.is_closed():
                    active.looked.discard((session, page.url))
            if reply.get("reason") == "no_form":
                self._event(active.run_id, "sign_in_nothing", "no_form",
                            f"No sign-in form showed on {site.display_name}; nothing was filled.")
            return
        if reply.get("status") != "logged_in":
            if reply.get("reason") == "not_found" and await self._vault_judgement(active, site):
                return
            self._login_needed(active, reply.get("reason", "unknown"))

    # -- signing in off the start page: the look, its check and its sign-in ------------------

    def _looked_now(self, active: _Active) -> None:
        """The page a sign-in ended on is not looked at again before Fido's next step."""
        session = active.session
        page = session.page if session is not None else None
        if page is not None and not page.is_closed():
            active.looked.add((session, page.url))

    @staticmethod
    def _ledger(active: _Active, site: Site, status, reason) -> None:
        """A sign-in of the run (start page or look) that acted counts: every outcome but one
        that acted on nothing (``no_form`` that found no form, could not read it or found it
        moved, and any ``not_found``: no item to sign in with). The last one's success is
        kept."""
        if (status == "no_form" and reason in ("no_form", "probe_failed", "page_moved",
                                               "needs_check")) \
                or reason == "not_found":
            return
        count, _worked = active.sign_ins.get(site.site_id, (0, False))
        active.sign_ins[site.site_id] = (count + 1, status == "logged_in")

    @staticmethod
    def _ledger_allows(active: _Active, site: Site) -> bool:
        """A look may sign in once, and once more only after a sign-in of the run that worked
        (R4): never after one that failed."""
        count, worked = active.sign_ins.get(site.site_id, (0, False))
        return count == 0 or (count == 1 and worked)

    async def _look(self, active: _Active) -> _AppJudgement | None:
        """``observe`` with ``sign_in`` (Fido 0.1.21): mechanical facts only, then one read of
        the page (``read_sign_in``); a page with sign-in fields becomes a ``sign_in`` judgement
        for Casa's check, returned in the observe reply. None: observe as today."""
        spec, ref = active.spec, active.ref
        if spec.mode != "account" or ref is None or ref.kind != "account":
            return None
        site = self.store.get_site(ref.site_id)
        if (site is None or self._access(spec, site.site_id) != "account"
                or not site.login_origins or site.vault_item_id is None):
            return None
        if active.code_wait is not None or not self._ledger_allows(active, site):
            return None
        session = active.session
        if session is None or self.browser.session is not session:
            return None
        page = session.page
        if page is None or page.is_closed():
            return None
        url = page.url
        if self._origin(url) is None or (session, url) in active.looked:
            return None
        active.looked.add((session, url))
        return await self._read_look(active, site, session, url, guest_ask=None,
                                     password_sent=False)

    async def _read_look(self, active: _Active, site: Site, session, url: str, *,
                         guest_ask: bool | None, password_sent: bool,
                         fresh: bool = False) -> _AppJudgement | None:
        """The look's read, then its judgement: the page reading as an action packet has it
        and what a sign-in would do (masked labels). A read error, no sign-in fields, a shape
        the check already judged not a sign-in in this run, or a page that moved: None."""
        try:
            sid = await self.driver.open_session("login", active.run_id, site.site_id, session,
                                                 self.proxy, site.login_origins)
        except BrokerError:
            return None
        active.driver_sid = sid
        reply: Any = {}
        try:
            reply = await self.broker.read_sign_in(active.run_id, site.site_id, sid)
        finally:
            forced = await self._close_driver(active, sid)
        if (forced or not isinstance(reply, dict) or "error" in reply or not reply.get("found")
                or active.session is not session or session.page is None
                or session.page.is_closed() or session.page.url != url):
            return None
        label = lambda text: mask_text(text if isinstance(text, str) else "").text.strip()[:200]  # noqa: E731
        fields = [{"id": f["id"], "role": f["role"] if isinstance(f.get("role"), str) else None,
                   "label": label(f.get("label")), "password_field": f.get("password") is True,
                   "button": label(f["button"]) if f.get("button") else None}
                  for f in reply.get("fields") or () if isinstance(f, dict)
                  and isinstance(f.get("id"), str)]
        passkey = reply.get("passkey") is True
        shape = (site.site_id, tuple((f["role"], f["label"], f["password_field"], f["button"])
                                     for f in fields), passkey)
        if shape in active.not_sign_in:
            return None
        frames = tuple(self.driver.sign_in_frames(active.run_id, site.site_id))
        page = session.page
        try:
            reading, bound = await self.ops.read_page_and_bound(page, frames, policy=active.policy)
        except Exception:  # noqa: BLE001 — a page that cannot be read is not judged
            return None
        if not bound.complete or page.url != url:
            # A plain text box (a search, a quantity) on a page that could not be read is no
            # sign-in that went unchecked: only a page with sign-in fields gets this word.
            if passkey or any(f["password_field"] or f["role"] for f in fields):
                self._event(active.run_id, "sign_in_nothing", "not_checked",
                            f"Casa could not check the sign-in page of {site.display_name}; "
                            "nothing was filled.")
            return None
        packet = gate.sign_in_packet(
            reading.data, site=site.display_name,
            origins=sorted({*site.origins, *site.login_origins}),
            planned={"fields": fields, "passkey": passkey})
        look = _Look(site.site_id, session, url, shape, guest_ask=guest_ask,
                     password_sent=password_sent, fields=tuple(fields), fresh=fresh)
        self._freeze_app(active, "sign_in", packet, site_id=site.site_id, look=look)
        self._event(active.run_id, "sign_in_judgement", "pending",
                    f"Casa checks whether this page is the sign-in of {site.display_name}.")
        return active.app_judgements["sign_in"]

    def _look_current(self, active: _Active, look: _Look) -> bool:
        """The session and the address looked at, with no step since."""
        session = active.session
        if session is not look.session or self.browser.session is not session:
            return False
        page = session.page
        return not (page is None or page.is_closed() or page.url != look.url
                    or (session, look.url) not in active.looked)

    async def _apply_sign_in(self, active: _Active, app: _AppJudgement, body: dict) -> dict:
        """A ``sign_in`` verdict. ``sign_in`` on the page as it was judged: Casa signs in by
        itself on that page (no navigation). ``not_sign_in`` (remembered for the run by its
        shape), no verdict (F1: never a sign-in without one) or a page that moved: nothing is
        filled. Either way the reply is the page as it is now, with the sign-in step the look
        recorded, if any (``notice``); the guest unlock's reload brings a fresh judgement."""
        verdict = body.get("verdict")
        if verdict is not None and not gate.sign_in_within_bounds(verdict):
            verdict = None
        look = app.look
        site = self.store.get_site(app.site_id)
        name = site.display_name if site is not None else app.site_id
        if verdict is None:
            self._event(active.run_id, "judgement", "no_verdict",
                        f"Check of the sign-in page on {name}: no verdict.")
        else:
            latency = body.get("latency_ms")
            seconds = "" if latency is None else f" ({latency / 1000:.1f} s)"
            self._event(active.run_id, "judgement", verdict["verdict"],
                        f"Check of the sign-in page on {name}: {verdict['verdict']}{seconds}.")
        active.notice = None
        if verdict is not None and verdict["verdict"] == "not_sign_in":
            active.not_sign_in.add(look.shape)
        mapping = (_sign_in_mapping(look.fields, verdict.get("fields"))
                   if verdict is not None and verdict["verdict"] == "sign_in" else None)
        if mapping is None and verdict is not None and verdict["verdict"] == "sign_in":
            self._event(active.run_id, "sign_in_mapping", "refused",
                        f"The check's fields for the sign-in page on {name} do not fit it; "
                        "nothing is filled.")
        if mapping is not None and site is not None and self._look_current(active, look):
            # What the verdict covers from here on is the broker's binding (the judged fields
            # and button in their marked documents): the page's text around them may move.
            successor = await self._auto_login(active, site, look, mapping)
            if successor is not None:
                return {"outcome": "judgement_required", "generation": self._generation(active),
                        "judgement": successor.wire()}
        notice, active.notice = active.notice, None
        obs = replace(await self._observe(active), notice=notice)
        return {"outcome": "done", "generation": obs.generation, "observation": obs.to_wire()}

    async def _auto_login(self, active: _Active, site: Site, look: _Look,
                          mapping: dict) -> _AppJudgement | None:
        """The sign-in on the judged page as it is (``broker.login(auto=True)``, no ``_goto``).
        On ``needs_guest`` the guest flow runs, the looked address is loaded again and looked
        at afresh: its judgement (no second unlock, the password already sent carried) is the
        successor returned; nothing reloaded is ever filled on the old verdict."""
        active.code_wait = None              # a new sign-in replaces a code report (S6-2)
        session = active.session
        guest_ask = look.guest_ask if look.guest_ask is not None else active.guest_for is not session
        sid = await self._driver_session(active, "login", site)
        reply: dict = {}
        try:
            reply = await self.broker.login(active.run_id, site.site_id, sid, guest_ask=guest_ask,
                                            auto=True, password_sent=look.password_sent,
                                            fields=mapping or None)
        finally:
            forced = await self._close_driver(active, sid)
        self._looked_now(active)
        if "error" in reply:
            raise _Dependency("broker_" + str(reply["error"]))
        if forced:
            self._ledger(active, site, "failed", "timeout")
            self._login_needed(active, "timeout")
            return None
        status, reason = reply.get("status"), reply.get("reason")
        if status == "needs_guest" and guest_ask:
            active.guest_for = session
            if await self._guest(active, site) or active.session is not session:
                self._ledger(active, site, "failed", "timeout")
                self._login_needed(active, "timeout")   # K1: the session is gone, no retry
                return None
            sent = look.password_sent or reply.get("password_sent") is True
            try:
                await self._goto(active, look.url)
            except ProtocolError:
                return None
            page = session.page
            if active.session is not session or page is None or page.is_closed():
                return None
            active.looked.add((session, page.url))
            return await self._read_look(active, site, session, page.url, guest_ask=False,
                                         password_sent=sent)
        self._ledger(active, site, status, reason)
        if status == "logged_in":
            # The form went away after the submit: no proof it worked. It claims nothing; the
            # model reading the page judges whether he is signed in.
            text = (f"Casa Browser filled in the sign-in of {site.display_name} and sent it; "
                    "read the page to see whether it worked.")
            self._event(active.run_id, "sign_in_sent", "submitted", text)
            active.notice = mask_text(text).text[:500]
        elif status == "needs_code":
            self._code_asked(active, site, reply.get("page_generation"))
        elif status == "no_form" and reason == "page_moved":
            # The judged fields or button are not the ones the look marked any more (a re-render,
            # a reload): nothing went out. The check judges the page as it is now, once.
            self._event(active.run_id, "sign_in_nothing", "page_moved",
                        f"The sign-in page of {site.display_name} changed while it was being "
                        "filled; nothing was sent. The next observe after a step looks at it "
                        "again.")
            if not look.fresh and self._look_current(active, look):
                return await self._read_look(active, site, session, look.url,
                                             guest_ask=look.guest_ask,
                                             password_sent=look.password_sent, fresh=True)
        elif status != "no_form":
            self._login_needed(active, reason if isinstance(reason, str) else "unknown")
        return None

    # -- slice 6: a code the site sent (design §5.4, plan S6-2, S6-3) ------------------------

    def _code_asked(self, active: _Active, site: Site, generation) -> None:
        """The broker stopped on a page asking for a code (no authenticator in the item): the
        page stays, and the report is bound to this site, browser session and that very page:
        its generation (which any navigation of it changes) and the tab itself (a closed tab
        leaves another as the session's page, whose navigations the generation does not count)."""
        session = active.session
        active.code_wait = (site.site_id, session,
                            generation if type(generation) is int else None,
                            session.page if session is not None else None)
        self._login_needed(active, "needs_code",
                           f"{site.display_name} asks for a sign-in code it sent.")

    async def login_code(self, run_id: str, login) -> ActionResult:
        """``POST /v1/runs/{run}/login-code``: the code for the site whose sign-in asked for it
        in this run, filled through a fresh ``login`` driver session (closed on every path)."""
        async def body(active):
            return await self._login_code(active, login)
        return await self._command(run_id, ("running",), body)

    async def _login_code(self, active: _Active, login) -> ActionResult:
        wait = active.code_wait
        site = self._sites(active.spec).get(login.site_id) if wait is not None else None
        if wait is None or wait[0] != login.site_id or site is None:
            raise ProtocolError("not_ready")
        active.code_wait = None              # one report, one code
        session = active.session
        page = session.page if session is not None else None
        if session is None or session is not wait[1] or page is None or page is not wait[3] \
                or page.is_closed() or wait[2] is None or session.view_generation != wait[2]:
            # Not the page that asked (it navigated or reloaded, its tab closed, or another
            # browser session):
            # nothing is filled. The broker checks the generation again on the page itself.
            reason = self._login_needed(active, "code_page_gone",
                                        "The page that asked for the sign-in code is gone.")
            return ActionResult("login_needed", self._generation(active), {"reason": reason})
        sid = await self._driver_session(active, "login", site)
        reply: dict = {}
        try:
            reply = await self.broker.fill_code(active.run_id, site.site_id, sid, wait[2],
                                                login.value)
        finally:
            forced = await self._close_driver(active, sid)
        if "error" in reply:
            raise _Dependency("broker_" + str(reply["error"]))
        gen = self._generation(active)
        status = "timeout" if forced else reply.get("status")
        self._event(active.run_id, "login_code", {"logged_in": "submitted", "needs_code": "needs_code"
                                                  }.get(status, "not_signed_in"),
                    f"A sign-in code was given for {site.display_name}.")
        if status == "logged_in":
            return ActionResult("done", gen)
        if status == "needs_code":
            self._code_asked(active, site, reply.get("page_generation"))
            return ActionResult("login_needed", gen, {"reason": "needs_code"})
        reason = self._login_needed(active, "timeout" if forced else reply.get("reason", "unknown"))
        return ActionResult("login_needed", gen, {"reason": reason})

    # -- slice 5: finding the item (design §5.1, plan S5-2) ----------------------------------

    def _freeze_app(self, active: _Active, kind: str, packet: dict, **facts) -> _AppJudgement | None:
        """Freeze an app-started judgement of ``kind`` (replacing an older one, returned)."""
        body = gate.canonical(packet)
        old = active.app_judgements.get(kind)
        active.app_judgements[kind] = _AppJudgement(
            "j" + self.c.token().replace("=", ""), kind, body, gate.digest(body), packet, **facts)
        return old

    async def _vault_judgement(self, active: _Active, site: Site) -> bool:
        """No item is linked: the browser vault's login items are listed (metadata only) and
        frozen as a ``login_item`` judgement for the check; the plugin's next call relays it.
        False when there is nothing to judge (the live view follows), and once per site per
        run: a site whose question was answered without a link is not asked again."""
        if site.site_id in active.vault_asked:
            return False
        reply = await self.broker.list_login_items(active.run_id, site.site_id)
        items = reply.get("items") if isinstance(reply, dict) and "error" not in reply else None
        if not items:
            active.vault_asked.add(site.site_id)
            return False
        page = active.session.page if active.session is not None else None
        host = None
        try:
            parts = urlsplit(page.url) if page is not None else None
            if parts is not None and parts.scheme in ("http", "https"):
                host = parts.hostname
        except ValueError:
            host = None
        host = host or urlsplit(site.login_origins[0]).hostname
        packet = {"kind": "login_item",
                  "site": {"name": site.display_name, "sign_in_host": host,
                           "registered_origins": sorted({*site.origins, *site.login_origins})},
                  "items": [{"index": i, "title": mask_text(it["title"]).text,
                             "website_hosts": list(it["website_hosts"])}
                            for i, it in enumerate(items)],
                  "items_truncated": bool(reply.get("truncated"))}
        self._freeze_app(active, "login_item", packet, site_id=site.site_id, host=host,
                         items=list(items))
        self._event(active.run_id, "login_item_judgement", "pending",
                    f"Casa checks which 1Password item signs in to {site.display_name}.")
        return True

    async def pending_judgements(self, run_id: str) -> dict:
        """``GET /v1/control/runs/{run}/judgements/pending``: the judgements the app started,
        for the plugin to relay (``login_item`` only while the run is running: its question
        needs the run)."""
        run = self._run(run_id)
        active = self.active
        if active is None or active.run_id != run.run_id or active.ending is not None:
            return {"judgements": []}
        # A look's ``sign_in`` judgement is relayed only from the observe reply that made it.
        return {"judgements": [j.wire() for j in active.app_judgements.values()
                               if j.kind != "sign_in"
                               and (j.kind != "login_item" or run.state == "running")]}

    # ------------------------------------------------------------------ navigation

    def _target_site(self, active: _Active, url: str) -> Site | None:
        origin = self._origin(url)
        # A plain-http address of a host a site registered over https is that site's (the
        # proxy answers it with the https redirect, ``policy.https_upgrade``; casa-test F16).
        secure = ("https://" + origin[len("http://"):]
                  if origin is not None and origin.startswith("http://") and ":" not in
                  origin[len("http://"):] else None)
        for site in self._sites(active.spec).values():
            if origin in (*site.origins, *site.login_origins) or (
                    secure is not None and secure in (*site.origins, *site.login_origins)):
                return site
        return None

    def _session_for(self, active: _Active, url: str) -> tuple[str, Site | None]:
        """The session a page belongs in: ``("account", site)`` for a site of the run with
        account access, else ``("public", None)`` — an address off the site set moves the run
        to the public profile (design §4)."""
        if active.spec.mode == "open_web":
            return ("public", None)
        site = self._target_site(active, url)
        if site is None or self._access(active.spec, site.site_id) != "account":
            return ("public", None)
        return ("account", site)

    async def _switch_for(self, active: _Active, url: str, *, public: bool = False) -> None:
        kind, site = ("public", None) if public else self._session_for(active, url)
        ref = active.ref
        same = ref is not None and ref.kind == kind and (
            kind == "public" or ref.site_id == site.site_id)
        if same and active.session is not None and self.browser.session is active.session:
            return
        # A profile switch: the model is paused (this command holds the lock); the former
        # browser closes before the new session's policy and browser start.
        await self._open_session(active, site if kind == "account" else None)

    def _precheck(self, active: _Active, url: str) -> None:
        """A top-level navigation the run's policy (leases removed) refuses is never sent: it
        is ``blocked`` (the floor; an address off the site set moved to the public session
        before this). A plain-http address of a site goes: the proxy redirects it to https."""
        policy = replace(active.policy, login_lease=None, manual_lease=None)
        meta = RequestMeta("GET", url, resource_type="document", is_navigation=True)
        if not decide(meta, policy).allow and https_upgrade(meta, policy) is None:
            raise ProtocolError("blocked")

    async def _goto(self, active: _Active, url: str) -> None:
        page = active.session.page if active.session is not None else None
        if page is None:
            raise _Dependency("browser_gone")
        try:
            await page.goto(url, wait_until="commit", timeout=NAV_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 — Patchright's text can carry the address
            raise ProtocolError("blocked") from None
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=LOAD_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 — a slow page is still the page
            pass
        self._keep_page_url(active, page.url)

    @staticmethod
    def _keep_page_url(active: _Active, url: str) -> None:
        if active.ref is not None and active.ref.kind == "account":
            active.page_url = url

    async def _load_for_login(self, active: _Active, site: Site) -> None:
        """A login view does not open on an empty fresh session: the page first, the one the
        run was last on if it is this site's, else the site's
        start page. Through a login driver session, whose lease covers a login-only origin, as
        a sign-in's load is. No sign-in here and no credential touched. A page that does not
        load leaves the view as it is."""
        session = active.session
        page = session.page if session is not None else None
        if page is None or page.url != "about:blank":
            return
        url = active.page_url
        kept = self._target_site(active, url) if url is not None else None
        if kept is None or kept.site_id != site.site_id:
            url = self._start_url(site)
        try:
            sid = await self._driver_session(active, "login", site)
        except _Dependency:
            log.warning("login view: no driver session to load its page")
            return                           # the view goes on; opening it says if it cannot
        try:
            self._precheck(active, url)
            await self._goto(active, url)
        except ProtocolError:
            self._event(active.run_id, "page_unreachable", "blocked",
                        "The page for the login live view did not load.")
        finally:
            await self._close_driver(active, sid)

    async def _navigate(self, active: _Active, url: str, *, before_goto=None,
                        public: bool = False) -> None:
        """Switch to the page's session (``public``: the public session whatever the address),
        check the run's policy, then load it. ``before_goto`` runs immediately before the load
        (the gate's G1 check of a free navigation, or a judged navigation's session check and
        dispatch journal) and may refuse it."""
        await self._switch_for(active, url, public=public)
        if active.session is None:
            # No session came of the switch: this step ends here; the run's next step opens
            # a new one.
            raise ProtocolError("not_ready")
        self._precheck(active, url)
        if before_goto is not None:
            before_goto()
        site = self._target_site(active, url) if active.spec.mode == "account" else None
        if (site is not None and active.ref.kind == "account" and active.ref.site_id == site.site_id
                and site.login_origins and url == self._start_url(site)):
            await self._login(active, site, url)
        else:
            await self._goto(active, url)

    async def _open_start_page(self, active: _Active, site: Site) -> None:
        url = self._start_url(site)
        try:
            await self._navigate(active, url)
        except ProtocolError:
            self._event(active.run_id, "page_unreachable", "blocked",
                        "The site's start page did not load.")

    async def _open_public_start(self, active: _Active, url: str) -> None:
        """Slice 5b (F-a): an open-web run whose task gives an exact public address opens it
        first, the way a free public navigation goes: the public session, the floor, the G1
        check immediately before the load. A refusal leaves the run on a blank page."""
        try:
            await self._navigate(active, url, before_goto=self._free_nav_guard(active),
                                 public=True)
        except (ProtocolError, GuardRefused):
            self._event(active.run_id, "page_unreachable", "blocked",
                        "The start page did not load.")

    # ------------------------------------------------------------------ observations

    def _start_entries(self, active: _Active, generation: int,
                       find: str | None = None) -> list[ElementView]:
        if active.spec.mode != "account":
            return []
        out = []
        for site in self._sites(active.spec).values():
            target = PrivateTarget("link", "link", None, self._start_url(site), None)
            handle = self.registry.issue(active.run_id, None, generation, target)
            label = mask_text(f"Open site: {site.display_name}").text[:300]
            out.append(ElementView(handle, "link", label,
                                   found=find is not None and holds_words(find, label)))
        return out

    def _source(self, active: _Active, session, url: str, title: str) -> str | None:
        if session.kind != "public":
            return None
        return self._add_source(active, url, title)

    def _add_source(self, active: _Active, url: str, title: str) -> str | None:
        if not isinstance(url, str) or not url.startswith("https://") or (
                len(url.encode("utf-8")) > MAX_SOURCE_URL):
            return None
        title_masked = mask_text(title or "").text.strip()[:MAX_SOURCE_TITLE]
        return self.store.add_source(active.run_id, url, title_masked,
                                     "src_" + secrets.token_hex(8), MAX_SOURCES)

    @staticmethod
    async def _page_read(read):
        """A reading of the page, once more if it moved under it; a refusal as the API's code."""
        for attempt in range(2):
            try:
                return await read()
            except ObservationRefused as refused:
                if refused.code == "page_off_session":
                    # The page is not this session's (an account page that left the site).
                    raise ProtocolError("blocked") from None
                if refused.code == "no_page":
                    raise _Dependency("browser_gone") from None
                if refused.code in ("stale", "unavailable") and attempt == 0:
                    continue
                raise ProtocolError("not_ready") from None

    async def _observe(self, active: _Active, find: str | None = None) -> Observation:
        session = await self._ensure_session(active)
        page = session.page
        if page is None or page.is_closed():
            raise _Dependency("browser_gone")
        self.registry.drop_run(active.run_id)
        generation = session.view_generation
        if page.url == "about:blank":
            page_handle = self.registry.issue(active.run_id, session.site_id, generation,
                                              PrivateTarget("page", "page", None, None, None))
            text = ("No page is open yet." if active.spec.mode == "account"
                    else "No page is open yet; search to begin, or search for an exact https "
                         "address to open that page.")
            return Observation(page_handle, generation, text,
                               tuple(self._start_entries(active, generation, find)), None,
                               find_whole=None if find is None else True)
        run = self._run(active.run_id)
        obs, url, title = await self._page_read(
            lambda: self.observer.observe_page(run, session, active.policy, find))
        self._keep_page_url(active, url)
        source_id = self._source(active, session, url, title)
        extra = tuple(self._start_entries(active, obs.generation, find))
        return replace(obs, source_id=source_id, elements=obs.elements + extra)

    async def observe(self, run_id: str, sign_in: bool = False, find: str | None = None):
        """``POST …/observe``: the page. With ``sign_in`` (Fido 0.1.21), a page of the site
        that shows sign-in fields is first looked at: the reply is then the ``sign_in``
        judgement for Casa's check (an ``ActionResult``), whose verdict reply carries the
        observation, one with no search. With ``find``, what holds those words comes with handles
        wherever it is on the page, and the reply says whether all of it was searched."""
        if not sign_in:
            return await self._command(run_id, ("running",),
                                       lambda active: self._observe(active, find))

        async def body(active):
            app = await self._look(active)
            if app is not None:
                return ActionResult("judgement_required", self._generation(active),
                                    {"judgement": app.wire()})
            return await self._observe(active, find)
        return await self._command(run_id, ("running",), body)

    async def _model_picture(self, active: _Active, session, element) -> bytes | None:
        """The page as a picture for a model, ``element`` outlined; any failure is no picture."""
        try:
            png = await asyncio.wait_for(self.observer.capture_for_model(
                self._run(active.run_id), session, active.policy, element), PICTURE_WAIT_S)
        except Exception:  # noqa: BLE001 — the caller goes on without one
            return None
        return png if png is not None and len(png) <= gate.MAX_JUDGE_PICTURE_BYTES else None

    async def look(self, run_id: str, handle: str | None) -> str | None:
        """``POST …/look``: what the page looks like, as ``run_look`` returns it."""
        async def body(active):
            session = await self._ensure_session(active)
            element = (None if handle is None else self._resolve(
                active, session, handle, session.view_generation).element)
            png = await self._model_picture(active, session, element)
            return None if png is None else base64.b64encode(png).decode("ascii")
        return await self._command(run_id, ("running",), body)

    async def _read_field(self, active: _Active, session, handle: str) -> tuple[str, bool, bool]:
        """A text field's whole value (``mask_whole``'s answer); a secret field shows nothing."""
        element = self._resolve(active, session, handle, session.view_generation).element
        if element is None:
            raise ProtocolError("unsupported")
        if not page_view_allowed(session.page.url, active.policy):
            raise ProtocolError("blocked")
        try:
            facts, value = await self.ops.text_facts(element), await self.ops.value(element)
        except Exception:  # noqa: BLE001 — the element left the page
            raise ProtocolError("stale_handle") from None
        if facts.get("secret") or value == gate.NOT_SHOWN:
            return "", True, True
        if not (facts.get("editable_text") or facts.get("value_kind")) or not isinstance(value, str):
            raise ProtocolError("unsupported")
        return mask_whole(value)

    async def _read_file(self, active: _Active, file_id: str) -> tuple[str, bool, bool]:
        """A downloaded text file of this run (``mask_whole``'s answer)."""
        record = self.store.get_file(file_id)
        if record is None or record.run_id != active.run_id:
            raise ProtocolError("not_found")
        if not record.mime.startswith("text/") and record.mime not in ("application/json",
                                                                       "application/csv"):
            raise ProtocolError("unsupported")
        path = Path(record.private_path)
        address = active.plain_files.get(file_id, "")
        as_text = ((lambda raw: html_text(raw, address)) if record.mime == "text/html"
                   else (lambda raw: raw.decode("utf-8")))
        try:
            keep = file_id in active.plain_files
            return await asyncio.to_thread(
                lambda: mask_whole(as_text(path.read_bytes()), keep_addresses=keep))
        except OSError:
            raise ProtocolError("not_found") from None
        except UnicodeDecodeError:
            raise ProtocolError("unsupported") from None

    async def read(self, run_id: str, handle: str | None, file_id: str | None, offset: int,
                   rows: bool = False) -> dict:
        """``POST …/read``: at most ``MAX_READ`` characters from ``offset`` of the page's whole
        text, its rows (``observe.EXTRACT``'s rows mode, one JSON line each), a field's value or a file; the
        windows join to the whole when ``complete``."""
        async def body(active):
            if file_id is not None:
                text, whole, masked = await self._read_file(active, file_id)
            else:
                session = await self._ensure_session(active)
                reader = self.observer.read_rows if rows else self.observer.read_text
                text, whole, masked = (
                    await self._read_field(active, session, handle) if handle is not None
                    else await self._page_read(lambda: reader(
                        self._run(active.run_id), session, active.policy)))
            return self._window(text, whole, masked, offset)
        return await self._command(run_id, ("running",), body)

    @staticmethod
    def _window(text: str, whole: bool, masked: bool, offset: int = 0) -> dict:
        end = offset + MAX_READ
        next_offset = end if end < len(text) else None
        return {"text": text[offset:end], "next_offset": next_offset,
                "complete": whole and next_offset is None, "masked": masked}

    @staticmethod
    def _masked_fields(record, fields) -> tuple[str, bool, bool] | None:
        picked = pick_fields(Path(record.private_path).read_bytes(), fields)
        return None if picked is None else mask_whole(picked, keep_addresses=True)

    async def fetch(self, run_id: str, url: str, fields: tuple[str, ...] = ()) -> ActionResult:
        """``POST …/fetch``: a public read with no browser (``Downloader.fetch_plain``). It holds
        the command lock like a download and touches neither Chrome, nor the session, nor the
        policy, so it leaves a frozen judgement standing: the page did not move."""
        async def body(active):
            generation = self._generation(active)
            if not start_url_ok(url):
                return ActionResult("not_run", generation, {"reason": "bad_address"})
            try:
                answer = await self.downloader.fetch_plain(
                    self._run(active.run_id), url, active.policy, active.user_agent)
            except DownloadRefused as err:
                raise ProtocolError(err.code) from None
            if answer.reason is not None:
                return ActionResult("not_run", generation, {"reason": answer.reason})
            record = answer.record
            active.plain_files[record.file_id] = answer.url
            self._event(active.run_id, "file_saved", "done",
                        f"Saved {record.safe_name} ({record.size} bytes).")
            extra = {"file": record.to_wire(), "url": mask_whole(answer.url, keep_addresses=True)[0],
                     "text": None, "next_offset": None,
                     "complete": False, "masked": False}
            title = record.safe_name
            try:
                text, whole, masked = await self._read_file(active, record.file_id)
            except ProtocolError as err:
                if err.code != "unsupported":
                    raise
            else:
                extra.update(self._window(text, whole, masked))
                if record.mime == "text/html":
                    title = text.split("\n", 1)[0]
            if fields:
                picked = await asyncio.to_thread(self._masked_fields, record, fields)
                if picked is None:
                    extra["not_json"] = True
                else:   # one window: the rest of the answer is the file's, not these fields'
                    extra.update(self._window(*picked), next_offset=None)
            source_id = self._add_source(active, answer.url, title)
            if source_id is not None:
                extra["source_id"] = source_id
            return ActionResult("done", generation, extra)
        return await self._command(run_id, ("running",), body, supersede=False)

    async def search(self, run_id: str, query: str):
        async def body(active):
            # Always public and free (design §3, §4): the search page opens in the public
            # profile, whatever the run's sites — a site with account access on the search
            # host included — and the load is checked to be public (G1). A query that is
            # exactly one https address opens that page instead, as an address bar does, and
            # the destination decides: a public one is the same free public load as an open-web
            # run's start address; a site of the run with account access is a signed-in link's
            # navigation, checked unless it stays on the host open now (G1 d).
            address = start_url_ok(query)
            if address and self._dest_kind(active, query) == "account":
                session = await self._ensure_session(active)
                generation = session.view_generation
                handle = self.registry.issue(active.run_id, None, generation,
                                             PrivateTarget("link", "link", None, query, None))
                return await self._act(active, Action("navigate", handle, generation))
            url = query if address else SEARCH_ORIGIN + SEARCH_PATH + quote_plus(query)
            await self._ensure_session(active)
            await self._navigate(active, url, before_goto=self._free_nav_guard(active),
                                 public=True)
            return await self._observe(active)
        try:
            return await self._command(run_id, ("running",), body)
        except GuardRefused:
            return ActionResult("not_run", 0, {"reason": "changed"})

    # ------------------------------------------------------------------ actions

    def _resolve(self, active: _Active, session, handle: str, generation: int) -> PrivateTarget:
        if generation != session.view_generation:
            raise ProtocolError("stale_handle")
        try:
            return self.registry.resolve(handle, active.run_id, session.view_generation)
        except StaleHandle:
            raise ProtocolError("stale_handle") from None

    async def _screenshot(self, active: _Active, session) -> str | None:
        try:
            return await self.observer.capture_operator(self._run(active.run_id), session,
                                                        active.policy)
        except Exception:  # noqa: BLE001 — a screenshot is illustration, never required
            return None

    async def _act(self, active: _Active, action) -> ActionResult:
        """An agent action (design §2.1, §3). ``scroll``, what sends nothing by itself and the
        free inputs of G1 run at once, each checked at delivery against what makes it free; every
        other action is frozen as a pending judgement whose packet the plugin's code relays to
        the check."""
        session = await self._ensure_session(active)
        target = self._resolve(active, session, action.handle, action.generation)
        page = session.page
        if page is None or page.is_closed():
            raise _Dependency("browser_gone")
        kind = action.kind
        link = target.kind == "link" and isinstance(target.href, str)
        http_link = link and target.href.lower().startswith(("https://", "http://"))
        if kind == "scroll":
            return await self._scroll(active, session, target, action)
        active.scroll_anchor = None
        if kind == "navigate" and not http_link:
            raise ProtocolError("unsupported")
        nav = kind == "navigate" or (kind == "click" and http_link and (
            target.element is None or self._switch_needed(active, target.href)))
        if not nav and kind != "back" and target.element is None:
            raise ProtocolError("unsupported")
        choice = None
        if kind == "select":
            choice = self._resolve(active, session, action.choice_handle, action.generation)
            if choice.element is None:
                raise ProtocolError("unsupported")
        search_field, option = await self._admissible(kind, target, choice, action)
        # A step carrying a plan is the one the operator's Yes is asked on, and one Fido marks
        # ask_first may commit money: both go to the check.
        if (kind in ("click", "type", "select") and not nav and not link and action.plan is None
                and not action.ask_first):
            done = await self._prepare(active, session, target, action, choice, option)
            if done is not None:
                return done
        dest = (self._dest_kind(active, target.href) if (kind == "navigate" or (
            kind == "click" and http_link)) else active.ref.kind)
        on_host = await self._free_on_host(active, session, target, action)
        if not gate.in_scope(dest, kind, is_link=http_link, search_field=search_field,
                             same_host=on_host):
            return await self._free(active, session, target, action, nav=nav, on_host=on_host)
        packet_action: dict = {"kind": kind}
        if kind == "type":
            packet_action.update(text=action.text, then_enter=action.enter)
        elif kind == "select":
            packet_action["option"] = option.get("label")
        pending = await self._freeze(active, session, target if kind != "back" else None,
                                     packet_action, kind="act", dest_kind=dest, action=action,
                                     enter=action.enter, name=self._entry_name(active, target),
                                     nav=nav)
        pending.choice, pending.option = choice, option
        return ActionResult("judgement_required", session.view_generation,
                            {"judgement": pending.wire()})

    async def _prepare(self, active: _Active, session, target, action, choice, option):
        """An action that sends nothing by itself (``gate.sends_nothing``) runs with no check, as
        a go does (F4, F9, the journal); None when the facts say it is not one."""
        try:
            reading = await self.ops.read_page(session.page, target.element, policy=active.policy)
        except Exception:  # noqa: BLE001 — Patchright's text can carry the address
            raise ProtocolError("stale_handle") from None
        facts = reading.data.get("target")
        if not facts:
            raise ProtocolError("stale_handle")
        if not gate.sends_nothing(action.kind, facts, enter=action.enter):
            return None
        op = gate.PendingJudgement(
            id="", kind="act", packet_bytes=b"", digest="", generation=session.view_generation,
            session_ref=active.ref, url=session.page.url, created_at=self.clock(), action=action,
            target=target, frozen=gate.target_facts(facts),
            label=self._step_label(action.kind, facts.get("name"), target),
            choice=choice, option=option)
        result = await self._guarded(active, op, op, target, final=None)
        outcome, generation = result.pop("outcome"), result.pop("generation")
        if outcome == "done":
            generation = (await self._action_done(active, op.label)).generation
        return ActionResult(outcome, generation, result)

    async def _admissible(self, kind: str, target, choice, action=None) -> tuple[bool, Any]:
        """The checks an action passes before any packet, at arrival and again whenever it is
        frozen afresh: S2-4 rule 0 (a type targets an editable text control or, slice 5g, a
        value field, never a field whose value a packet does not show — password, one-time
        code, payment card), and a select's option belongs to it and the select is no card
        select. Returns (search field, option). Slice 5g: a read-only field (``not_typable``),
        a value field's text its format refuses (``value_format``) and Enter after a value
        field (``no_enter``) do not run, nothing judged. Slice 6b: neither does a line break
        into anything but a real <textarea> (``one_line``), nor Enter after one (``no_enter``:
        sending stays its own checked step)."""
        if kind == "type":
            text = getattr(action, "text", None)
            lines = isinstance(text, str) and "\n" in text
            try:
                facts = await self.ops.text_facts(target.element, text)
            except Exception:  # noqa: BLE001 — the element left the page
                raise ProtocolError("stale_handle") from None
            if not isinstance(facts, dict) or facts.get("secret"):
                raise ProtocolError("unsupported")
            if facts.get("readonly"):
                raise GuardRefused("not_typable")
            enter = getattr(action, "enter", False)
            if facts.get("value_kind"):
                if lines:
                    raise GuardRefused("one_line")
                if enter:
                    raise GuardRefused("no_enter")
                if facts.get("accepts") is not True:
                    raise GuardRefused("value_format")
                return False, None
            if not facts.get("editable_text"):
                raise ProtocolError("unsupported")
            if lines and not facts.get("textarea"):
                raise GuardRefused("one_line")
            if lines and enter:
                raise GuardRefused("no_enter")
            return bool(facts.get("search_field")), None
        if kind == "select":
            try:
                option = await self.ops.option_facts(target.element, choice.element)
                select_facts = await self.ops.text_facts(target.element)
            except Exception:  # noqa: BLE001
                raise ProtocolError("unsupported") from None
            if not isinstance(option, dict) or not option.get("belongs"):
                raise ProtocolError("unsupported")
            if not isinstance(select_facts, dict) or select_facts.get("secret"):
                # A card select's choice is a value no packet shows: never the agent's.
                raise ProtocolError("unsupported")
            return False, option
        return False, None

    async def _scroll(self, active: _Active, session, target, action) -> ActionResult:
        """``scroll`` is free everywhere (G1 c); the pacer checks every wheel step. The first
        scroll on a handle brings its element into view, and when that moved anything it is the
        whole step; the same handle again wheels on from where the page is, never back to the
        element, so repeated scrolls advance. The reply says where that left the page."""
        pacer = self.c.pacer(session.page)
        position = None
        if target.kind != "page" and active.scroll_anchor != action.handle:
            if target.element is None:
                raise ProtocolError("unsupported")
            position = await pacer.scroll_to(target.element)
        active.scroll_anchor = action.handle
        if position is None:
            position = await pacer.scroll(action.direction, action.amount)
        return replace(await self._action_done(active, "scroll"), extra={"position": position})

    async def _action_done(self, active: _Active, kind: str, note: str = "", *,
                           sent: bool = False) -> ActionResult:
        """An action's event. Only one that went through the check and sent something (``sent``)
        keeps a picture of the page after it, as the operator's record of what was sent."""
        session = active.session
        if session is None:
            raise ProtocolError("stale_handle")
        shot = await self._screenshot(active, session) if sent else None
        self._event(active.run_id, "action", "done", f"Action: {kind}.{note}", screenshot_id=shot)
        active.looked.clear()                # a step: every page may be looked at again
        return ActionResult("done", session.view_generation)

    def _on_host(self, active: _Active, session, url: str) -> bool:
        """The address loads another page of the host open in this account session: the run's own
        site for this session (no switch) and the host of the page open now."""
        page = session.page
        return (active.ref is not None and active.ref.kind == "account"
                and active.session is session and self.browser.session is session
                and page is not None and not page.is_closed()
                and not self._switch_needed(active, url) and gate.same_host_load(url, page.url))

    async def _free_on_host(self, active: _Active, session, target, action) -> bool:
        """G1 (d): a navigation that opens an address on the host open runs with no check. A click
        never does: it is a real press, and a script behind a link is an action. A step carrying a
        plan is the one the operator's Yes is asked on, and a link whose address moved since it was
        observed is judged, which says so."""
        if (action.kind != "navigate" or action.plan is not None
                or not self._on_host(active, session, target.href)):
            return False
        if target.element is None:
            return True
        try:
            return await self.ops.link_href(target.element) == target.href
        except Exception:  # noqa: BLE001 — the element left the page: the check's read says so
            return False

    def _free_nav_guard(self, active: _Active):
        """G1 a: a free navigation runs only in the public session it was judged free for,
        checked immediately before ``_goto``."""
        def check():
            if active.ref is None or active.ref.kind != "public":
                raise GuardRefused("changed")
        return check

    async def _free(self, active: _Active, session, target, action, *, nav: bool,
                    on_host: bool = False) -> ActionResult:
        """The free inputs of G1 (a), (b) and (d), each checked at delivery."""
        kind = action.kind
        page = session.page

        async def public(point=None) -> str:
            ok = (active.ref is not None and active.ref.kind == "public"
                  and active.session is session and self.browser.session is session)
            return "ok" if ok else "changed"

        def still_on_host() -> None:
            if not self._on_host(active, session, target.href):
                raise GuardRefused("changed")

        if nav:
            await self._navigate(active, target.href, before_goto=(
                still_on_host if on_host else self._free_nav_guard(active)))
        elif kind == "back":
            if await public() != "ok":
                raise GuardRefused("changed")
            try:
                await page.go_back(wait_until="commit", timeout=NAV_TIMEOUT_MS)
            except Exception:  # noqa: BLE001
                raise ProtocolError("blocked") from None
        elif kind == "click":
            element = target.element

            async def free_link(point) -> str:
                # The property that made it free, at delivery: still an http(s) link whose
                # destination is the public session, in the public session, and the point
                # lands on it.
                if await public() != "ok":
                    return "changed"
                href = await self.ops.link_href(element)
                if (not isinstance(href, str) or not href.lower().startswith(("https://", "http://"))
                        or self._dest_kind(active, href) != "public"):
                    return "changed"
                return "ok" if await self.ops.hit(element, point) else "moved"
            await self.c.pacer(page).click(element, guard=free_link)
        elif kind == "type":
            element = target.element
            pacer = self.c.pacer(page)

            async def search_field() -> bool:
                facts = await self.ops.text_facts(element)
                return (isinstance(facts, dict) and bool(facts.get("editable_text"))
                        and bool(facts.get("search_field")) and not facts.get("secret"))

            async def free_field(point) -> str:
                if await public() != "ok" or not await search_field():
                    return "changed"
                return "ok" if await self.ops.hit(element, point) else "moved"

            async def focus() -> bool:
                return (await public() == "ok" and await self.ops.focused(element)
                        and await search_field())
            await pacer.type(element, action.text, guard=free_field, focus=focus)
            if action.enter:
                async def at_enter() -> str:
                    return "ok" if await focus() else "changed"
                await pacer.press_enter(at_enter)
        else:
            raise ProtocolError("unsupported")
        return await self._action_done(active, self._step_label(kind, None, target))

    def _switch_needed(self, active: _Active, url: str) -> bool:
        if active.ref is None:
            return False
        kind, site = self._session_for(active, url)
        return not (active.ref.kind == kind and (kind == "public" or active.ref.site_id == site.site_id))

    async def act(self, run_id: str, action) -> ActionResult:
        async def body(active):
            try:
                return await self._act(active, action)
            except GuardRefused as refused:
                return ActionResult("not_run", active.session.view_generation
                                    if active.session is not None else 0,
                                    {"reason": refused.code})
            except ActionError as err:
                if err.code in TARGET_REFUSALS:
                    return ActionResult("not_run", self._generation(active), {"reason": err.code})
                raise ProtocolError("unsupported" if err.code == "unsupported" else "blocked") \
                    from None
        return await self._command(run_id, ("running",), body)

    # ------------------------------------------------------------------ the action gate

    def _dest_kind(self, active: _Active, url: str | None) -> str:
        """The session an input to ``url`` is delivered in (G1: decided on the destination)."""
        if not url:
            return active.ref.kind if active.ref is not None else "public"
        return self._session_for(active, url)[0]

    @staticmethod
    def _step_label(kind: str, name: str | None, target) -> str:
        """How an event names a step: its name, else what the observation called its target, else,
        for an address the model gave itself (the only target with neither), that address's host
        and masked path. A page's own link never shows more than the model was shown."""
        shown = mask_text(name or "").text.strip()[:80]
        if not shown and target is not None:
            shown = target.label[:80] or (gate.address_name(target.href)[:80]
                                          if target.element is None and target.href else "")
        return f"{kind} “{shown}”" if shown else kind

    def _entry_name(self, active: _Active, target) -> str:
        """The label the app gave a link it issued itself (a site's start entry)."""
        if target is None or target.element is not None or not target.href:
            return ""
        for site in self._sites(active.spec).values():
            if self._start_url(site) == target.href:
                return f"Open site: {site.display_name}"
        return ""

    def _site_name(self, active: _Active, url: str | None) -> str:
        site = self._target_site(active, url) if url else None
        if site is None and active.ref is not None and active.ref.kind == "account":
            site = self.store.get_site(active.ref.site_id)
        if site is not None:
            return site.display_name
        try:
            return urlsplit(url or "").hostname or ""
        except ValueError:
            return ""

    def _site_key(self, active: _Active, url: str) -> str | None:
        """What an approval is bound to: the run's site by id (display names are not unique),
        else the page's origin."""
        site = self._target_site(active, url)
        return site.site_id if site is not None else self._origin(url)

    async def _freeze(self, active: _Active, session, target, packet_action: dict, *, kind: str,
                      dest_kind: str, action=None, handle: str | None = None,
                      search_url: str | None = None, enter: bool = False, name: str = "",
                      via: str | None = None, download: dict | None = None,
                      nav: bool = False) -> gate.PendingJudgement:
        """Read the page, build the packet and freeze the action as the run's pending
        judgement (S2-2, S2-3, S2-5). Called inside the command lock; the check runs after."""
        page = session.page
        if page is None or page.is_closed():
            raise _Dependency("browser_gone")
        generation, url = session.view_generation, page.url
        element = target.element if target is not None else None
        # The picture first: its scroll can load more, which the reading and stale check then see.
        picture = await self._model_picture(active, session, element)
        try:
            reading = await self.ops.read_page(page, element, policy=active.policy)
        except Exception:  # noqa: BLE001 — Patchright's text can carry the address
            raise ProtocolError("stale_handle") from None
        if session.view_generation != generation or page.url != url:
            raise ProtocolError("stale_handle")
        data = dict(reading.data)
        facts = data.get("target")
        if element is not None and not facts:
            raise ProtocolError("stale_handle")
        if facts and ((facts.get("is_submitter") and (facts.get("submission") or {}).get("unreadable"))
                      or (enter and (facts.get("enter_submission") or {}).get("unreadable"))):
            # What it would submit cannot be read whole (a chosen file): not judged, the
            # operator does it in the live view.
            raise ProtocolError("unsupported")
        if element is not None and target.href is not None and facts.get("href") != target.href:
            # The link's address changed since it was observed: what would be judged is not
            # what the handle names. Observe again.
            raise ProtocolError("stale_handle")
        if element is None and target is not None and target.href:
            # A link the app issued itself (a site's start entry) or the app's search page.
            facts = {"role": "link", "name": name, "tag": "a", "input_type": None,
                     "href": target.href, "block": None, "record": None, "state": {}}
            data["target"] = facts
        packet = gate.build_packet(data, facts, packet_action, task=active.spec.task_text,
                                   session="signed_in" if dest_kind == "account" else "public",
                                   site=self._site_name(active, url),
                                   run_so_far=self._run_so_far(active),
                                   download=download, plan=action.plan if action else None,
                                   approved=self._approved_here(active, url, data))
        body = gate.canonical(packet)
        pending = gate.PendingJudgement(
            id="j" + self.c.token().replace("=", ""), kind=kind, packet_bytes=body,
            digest=gate.digest(body), generation=generation, session_ref=active.ref, url=url,
            created_at=self.clock(), action=action, target=target,
            frozen=gate.target_facts(facts, enter=enter), reading=data, packet=packet,
            label=self._step_label(packet_action["kind"], (facts or {}).get("name"), target),
            verb=packet_action["kind"] if kind != "download" else "download",
            dest_kind=dest_kind, handle=handle, search_url=search_url, enter=enter, via=via,
            nav=nav or kind == "search", entry_name=name, frame_url=data.get("href"),
            picture=picture)
        active.pending = pending
        return pending

    def _approved_here(self, active: _Active, url: str, reading: dict) -> str | None:
        """The approved plan, on the site it was approved for and on no page with a card field."""
        approved, key = active.approved, self._site_key(active, url)
        if approved is None or key is None or approved["site_key"] != key or reading.get("has_cc"):
            return None
        return approved["plan"]

    async def judgement_picture(self, run_id: str, judgement_id: str) -> bytes:
        """``GET …/judgements/{judgement}/picture``: no lock, it only reads memory; a verdict
        that started applying has cleared it."""
        run, active = self._run(run_id), self.active
        pending = active.pending if active is not None and active.run_id == run.run_id else None
        if pending is None or pending.id != judgement_id or pending.picture is None:
            raise ProtocolError("not_found")
        return pending.picture

    # -- verdicts (S2-5, S2-7) ---------------------------------------------------------

    async def apply_judgement(self, run_id: str, judgement_id: str, body: dict) -> dict:
        """``POST /v1/control/runs/{run}/judgements/{judgement}`` (trusted callback). A stored
        id answers its stored response with ``repeat: true`` and dispatches nothing; else the
        run's current pending judgement is applied once, under the lock; else
        ``judgement_stale``."""
        key = "j:" + judgement_id
        stored = self.store.get_response(self._run(run_id).run_id, key)
        if stored is not None and not (stored.get("provisional") and self._in_flight(run_id)):
            # A provisional outcome_unknown (the journal's) is final only once no work of the
            # run is in flight; while the dispatch runs, a repeat waits for its real response.
            return _replayed(stored)

        async def work(active):
            stored = self.store.get_response(run_id, key)
            if stored is not None:
                return _replayed(stored)
            app = next((j for j in active.app_judgements.values() if j.id == judgement_id), None)
            if app is not None:
                if "no_verdict" not in body and body["packet_sha256"] != app.digest:
                    raise ProtocolError("digest_mismatch")
                response = await self._apply_app(active, app, body)
                self.store.put_response(run_id, key, response)
                if active.app_judgements.get(app.kind) is app:
                    del active.app_judgements[app.kind]
                return response
            pending = active.pending
            if pending is None or pending.id != judgement_id or pending.state != "open":
                raise ProtocolError("judgement_stale")
            if (self.clock() - pending.created_at).total_seconds() > gate.JUDGEMENT_TTL_S:
                active.pending = None
                raise ProtocolError("judgement_stale")
            if "no_verdict" not in body and body["packet_sha256"] != pending.digest:
                raise ProtocolError("digest_mismatch")
            pending.state = "applying"
            pending.picture = None
            response, successor = await self._apply(active, pending, body)
            # The complete response is stored before any successor replaces the pending one.
            self.store.put_response(run_id, key, response)
            active.pending = successor
            return response
        return await self._command(run_id, ("running", "waiting_approval"), work,
                                   supersede=False)

    async def _apply_app(self, active: _Active, app: _AppJudgement, body: dict) -> dict:
        """Slice 5: a ``login_item`` or ``signin`` verdict. Outside its bounds (an index the
        packet does not hold, evidence naming no block of it) it is no verdict, which links
        and saves nothing (F1)."""
        if app.kind == "sign_in":
            return await self._apply_sign_in(active, app, body)
        verdict = body.get("verdict")
        if app.kind == "login_item":
            if self._run(active.run_id).state != "running":
                raise ProtocolError("not_ready")      # its question needs the run: kept, relayed later
            ok = verdict is not None and gate.login_item_within_bounds(verdict, len(app.items))
        else:
            ok = verdict is not None and gate.signin_within_bounds(
                verdict, gate.packet_blocks(app.packet))
        verdict = verdict if ok else None
        site = self.store.get_site(app.site_id) if app.site_id else None
        name = (app.proposal.name if app.proposal is not None
                else site.display_name if site is not None else app.site_id)
        what = (f"the 1Password items for {name}" if app.kind == "login_item"
                else f"the live-view sign-in on {name}")
        if verdict is None:
            self._event(active.run_id, "judgement", "no_verdict", f"Check of {what}: no verdict.")
        else:
            latency = body.get("latency_ms")
            seconds = "" if latency is None else f" ({latency / 1000:.1f} s)"
            said = (f"{len(verdict['items'])} candidate(s)" if app.kind == "login_item"
                    else verdict["verdict"])
            # No reason, evidence or packet text is recorded: what the sign-in check read may
            # hold page text the operator typed (operator ruling, slice 5), so only the verdict
            # and its timing are stored.
            self._event(active.run_id, "judgement",
                        "items" if app.kind == "login_item" else verdict["verdict"],
                        f"Check of {what}: {said}{seconds}.")
        if app.kind == "login_item" and app.proposal is not None:
            return await self._apply_site_items(active, app, verdict)
        if app.kind == "login_item":
            return await self._apply_login_item(active, app, verdict, site)
        return await self._apply_signin(active, app, verdict, site)

    async def _apply_login_item(self, active: _Active, app: _AppJudgement, verdict, site) -> dict:
        """S5-3: no candidate (or no verdict) is the live view; one or more is a question, with
        the browser's real host and the vault's whole titles (design §5.1 step 3)."""
        gen = self._generation(active)
        cands = conf.login_candidates(app.items, verdict["items"]) if (
            verdict is not None and site is not None) else []
        if not cands:
            active.vault_asked.add(app.site_id)
            self._login_needed(active, "not_found")
            return {"outcome": "login_needed", "generation": gen}
        labels = [label for _i, label in cands]
        view = conf.login_view(app.host or "", labels)
        qid = "q" + self.c.token().replace("=", "")
        run_id = active.run_id
        with self.store.transaction():
            self.store.create_question(qid, run_id, 1, app.digest, view, self.clock())
            if not self.store.transition(run_id, "running", "waiting_approval"):
                raise ProtocolError("not_ready")
        active.question = conf.LoginQuestion(qid, 1, app.digest, app.site_id,
                                             [app.items[i]["item_id"] for i, _ in cands], labels)
        self._event(run_id, "question", "asked",
                    f"The operator is asked which 1Password item signs in to {site.display_name}.")
        return {"outcome": "asked", "generation": gen, "question_id": qid}

    async def _answer_login(self, active: _Active, q, frozen, body: dict) -> dict:
        """S5-3, the operator's answer: Yes or a title links that item (the item is read,
        never written) and the site's start page is opened again, which signs in with it; No or
        None of these links nothing, and the run reports ``login_needed``."""
        gen = self._generation(active)
        choice, ref = body["choice"], body["operator_ref"]
        site = self.store.get_site(frozen.site_id)
        if choice == "not_now" or site is None:
            response = {"outcome": "declined", "generation": gen}
            self._settle_question(active, q, "declined", response, ref,
                                  text="The operator chose no 1Password item; nothing was linked.")
            active.vault_asked.add(frozen.site_id)
            self._login_needed(active, "not_found")
            return response
        if not (isinstance(choice, dict) and isinstance(choice.get("item"), int)
                and choice["item"] < len(frozen.item_ids)):
            raise ProtocolError("unsupported")
        i = choice["item"]
        reply = await self.broker.register_item(site.site_id, frozen.item_ids[i],
                                                list(site.login_origins), ref)
        if "error" in reply:
            response = {"outcome": "not_linked", "generation": gen}
            self._settle_question(active, q, "answered", response, ref, outcome="not_linked",
                                  text="The chosen 1Password item could not be linked.")
            active.vault_asked.add(frozen.site_id)
            self._login_needed(active, "not_found")
            return response
        self.store.put_site(replace(site, vault_item_id=frozen.item_ids[i]))
        response = {"outcome": "linked", "generation": gen, "label": frozen.titles[i]}
        self._settle_question(active, q, "answered", response, ref, outcome="linked",
                              text=f"The operator chose the 1Password item for "
                                   f"{site.display_name}; signing in with it.")
        try:
            await self._navigate(active, self._start_url(site))
        except ProtocolError:
            self._event(active.run_id, "page_unreachable", "blocked",
                        "The site's start page did not load.")
        return response

    # -- slice 5n: the sign-in card for a site that is not set up ----------------------------

    async def ask_sign_in(self, run_id: str, site_name: str) -> ActionResult:
        """``POST /v1/runs/{run}/sign-in``: the model judged that the public page it is on needs
        a sign-in. The app reads only mechanical facts: the page's https origin from the
        browser, the app's own screenshot of that very page, a registered site that is exactly
        that origin (a broader one is named instead: it has its own account run), and the
        vault's logins for the check. The card is a question; only its Yes registers."""
        async def body(active):
            def no(outcome: str, **extra) -> ActionResult:
                return ActionResult(outcome, self._generation(active), extra)
            if not self.c.configured:
                return no("not_configured")
            if active.question is not None or self.store.open_question(run_id) is not None:
                return no("question_open")
            session = await self._ensure_session(active)
            if active.ref is None or active.ref.kind != "public":
                return no("account_site")
            page = session.page
            if page is None or page.is_closed():
                raise _Dependency("browser_gone")
            origin, host, start = _sign_in_address(page.url)
            if origin is None:
                return no("not_https")
            seen = session.view_generation
            try:
                shot = await self.observer.capture_question(self._run(run_id), session,
                                                            active.policy, None)
            except Exception:  # noqa: BLE001 — no faithful screenshot: the card goes as text
                shot = None
            if (session.view_generation != seen or session.page is not page
                    or _sign_in_address(page.url)[0] != origin):
                return no("page_changed")
            covering = [s for s in self.store.list_sites()
                        if origin in (*s.origins, *s.login_origins)]
            exact = [s for s in covering if self._exactly(s, origin)]
            if covering and not exact:
                return no("registered_site", site=covering[0].display_name)
            site = exact[0] if exact else None
            name = site.display_name if site is not None else (_label(site_name) or host)
            proposal = conf.SignInProposal(origin, host, start, name, shot,
                                           site.site_id if site is not None else None)
            if site is None or site.vault_item_id is None:
                reply = await self.broker.list_vault_logins(run_id)
                items = (reply.get("items") if isinstance(reply, dict) and "error" not in reply
                         else None)
                if items:
                    packet = {"kind": "login_item",
                              "site": {"name": name, "sign_in_host": host,
                                       "registered_origins": [origin]},
                              "items": [{"index": i, "title": mask_text(it["title"]).text,
                                         "website_hosts": list(it["website_hosts"])}
                                        for i, it in enumerate(items)],
                              "items_truncated": bool(reply.get("truncated"))}
                    self._freeze_app(active, "login_item", packet, site_id="", host=host,
                                     items=list(items), proposal=proposal)
                    self._event(run_id, "login_item_judgement", "pending",
                                f"Casa checks which 1Password item signs in to {host}.")
                    return no("judging")
            asked = self._site_question(active, proposal, None, None, 0)
            return ActionResult(asked["outcome"], asked["generation"],
                                {"question_id": asked["question_id"]})
        return await self._command(run_id, ("running",), body)

    def _exactly(self, site: Site, origin: str) -> bool:
        """A registered site whose every address is ``origin``: the card shows all it reaches."""
        return (set(site.origins) == {origin} and set(site.login_origins) == {origin}
                and self._origin(self._start_url(site)) == origin)

    async def _apply_site_items(self, active: _Active, app: _AppJudgement, verdict) -> dict:
        """The check's items for a sign-in card: exactly one fitting item is named on the card
        (one Yes links it too); none, several or no verdict name none (several: the existing
        pick follows the Yes)."""
        cands = conf.login_candidates(app.items, verdict["items"]) if verdict is not None else []
        item_id = title = None
        if len(cands) == 1:
            i, title = cands[0]
            item_id = app.items[i]["item_id"]
        return self._site_question(active, app.proposal, item_id, title, len(cands))

    def _site_question(self, active: _Active, proposal, item_id, title, candidates: int) -> dict:
        gen = self._generation(active)
        view = conf.site_view(proposal, title)
        digest = gate.digest(gate.canonical({
            "kind": "site_signin", "origin": proposal.origin, "host": proposal.host,
            "start_page": proposal.start_page, "name": proposal.name, "item": title,
            "screenshot_id": proposal.screenshot_id, "site_id": proposal.site_id}))
        qid = "q" + self.c.token().replace("=", "")
        run_id = active.run_id
        with self.store.transaction():
            self.store.create_question(qid, run_id, 1, digest, view, self.clock())
            if not self.store.transition(run_id, "running", "waiting_approval"):
                raise ProtocolError("not_ready")
        active.question = conf.SiteQuestion(qid, 1, digest, proposal, item_id, title, candidates)
        self._event(run_id, "question", "asked",
                    f"The operator is asked whether to sign in to {proposal.host}.",
                    screenshot_id=proposal.screenshot_id)
        return {"outcome": "asked", "generation": gen, "question_id": qid}

    async def _answer_site(self, active: _Active, q, frozen, body: dict) -> dict:
        """The sign-in card's answer. No: nothing is registered or signed in. Yes: the card's
        origin becomes a site (or the exact registered site the card named), the run gains it
        as an account site, and its start page opens on its own profile, which signs in by the
        existing login flow. A site registered for that origin meanwhile is never taken over:
        the operator did not see it."""
        gen = self._generation(active)
        choice, ref = body["choice"], body["operator_ref"]
        p = frozen.proposal
        run_id = active.run_id
        if choice == "not_now":
            response = {"outcome": "declined", "generation": gen}
            self._settle_question(active, q, "declined", response, ref,
                                  text="The operator said no; no site was saved and nothing "
                                       "was signed in.")
            return response
        if choice != "confirm":
            raise ProtocolError("unsupported")
        covering = [s for s in self.store.list_sites()
                    if p.origin in (*s.origins, *s.login_origins)]
        if p.site_id is not None:
            site = next((s for s in covering if s.site_id == p.site_id), None)
            ok = site is not None and self._exactly(site, p.origin) and len(covering) == 1
        else:
            site, ok = None, not covering
        if not ok:
            response = {"outcome": "already_saved", "generation": gen, "label": p.host}
            self._settle_question(active, q, "answered", response, ref, outcome="already_saved",
                                  text=f"A site for {p.host} changed since the card; nothing "
                                       "was signed in.")
            return response
        if site is None:
            try:
                site_id, _reply = await self._register_locked({
                    "display_name": p.name, "origins": (p.origin,), "login_origins": (p.origin,),
                    "vault_item_id": frozen.item_id, "start_page": p.start_page,
                    "operator_ref": ref})
            except ProtocolError:
                response = {"outcome": "not_saved", "generation": gen, "label": p.host}
                self._settle_question(active, q, "answered", response, ref, outcome="not_saved",
                                      text=f"Casa Browser could not save {p.host}.")
                return response
            site = self.store.get_site(site_id)
        elif frozen.item_id is not None and site.vault_item_id is None:
            reply = await self.broker.register_item(site.site_id, frozen.item_id,
                                                    list(site.login_origins), ref)
            if "error" not in reply:
                site = replace(site, vault_item_id=frozen.item_id)
                self.store.put_site(site)
        grants = tuple(g for g in active.spec.sites if g.site_id != site.site_id)
        active.spec = replace(active.spec, mode="account",
                              sites=grants + (SiteGrant(site.site_id, "account"),))
        self.store.set_run_spec(run_id, active.spec)
        reply = await self.broker.begin_run(
            run_id, [g.site_id for g in active.spec.sites if g.access == "account"])
        if "error" in reply:
            raise _Dependency("broker_" + str(reply["error"]))
        if frozen.candidates == 0 and site.vault_item_id is None:
            active.vault_asked.add(site.site_id)    # no item fitted: the live view, no 2nd check
        # The start page opens before the answer is settled, so the receipt says what happened
        # (r1 Terra): signing in, or saved but its sign-in page did not open.
        outcome = "signing_in"
        try:
            await self._navigate(active, self._start_url(site))
        except ProtocolError:
            outcome = "not_opened"
            self._event(run_id, "page_unreachable", "blocked",
                        "The site's start page did not load.")
        response = {"outcome": outcome, "generation": self._generation(active), "label": p.host}
        self._settle_question(active, q, "answered", response, ref, outcome=outcome,
                              text=f"The operator agreed to sign in to {p.host}; it is one of "
                                   "their sites now.")
        return response

    # -- slice 5: the live-view sign-in (design §5.3, plan S5-5) -----------------------------

    async def _signin_reading(self, active: _Active, page) -> dict | None:
        """A page as the sign-in check sees it (``gate.read_signin_page``), read with the run's
        current policy."""
        return await gate.read_signin_page(self.ops, page, active.policy)

    async def _take_signin(self, active: _Active, live: _Live) -> None:
        """At Done (before the view's release): the capture's last attempt as flags, and the
        pages the driver read after it and at the take. With an attempt that did not fail,
        nothing typed into a password field after it, ended on one of the site's origins and
        not already saved by the operator, the ``signin`` judgement is frozen for the check;
        otherwise the broker drops it unsaved. No attempt: the release ends the capture as
        before (no judgement)."""
        site = self.store.get_site(live.site_id)
        if site is None:
            return
        run_id = active.run_id
        reply = await self.broker.take_attempt(run_id, site.site_id, live.capture_id)
        if (not isinstance(reply, dict) or "error" in reply or reply.get("attempt") is None
                or reply.get("incomplete")):
            return
        got = self.driver.take_readings(live.capture_id)
        got = got if isinstance(got, dict) else {}
        strip = lambda r: {k: v for k, v in r.items() if k != "_origin"}  # noqa: E731
        after = [strip(r) for r in got.get("after") or () if isinstance(r, dict)]
        at_done = got.get("at_done")
        attempt = reply["attempt"]
        if (attempt.get("failed") is not False or attempt.get("typed_after") is not False
                or not isinstance(at_done, dict)
                or at_done.get("_origin") not in site.origins):
            await self.broker.drop_attempt(run_id, site.site_id, live.capture_id)
            self._event(run_id, "signin", "not_saved", "The live-view sign-in is not saved.")
            return
        packet = gate.signin_packet(site.display_name, sorted(site.origins), after, strip(at_done))
        old = self._freeze_app(active, "signin", packet, site_id=site.site_id,
                               capture_id=live.capture_id)
        if old is not None and old.capture_id is not None:
            await self.broker.drop_attempt(run_id, old.site_id, old.capture_id)
        self._event(run_id, "signin_judgement", "pending",
                    "Casa checks whether the live-view sign-in worked.")

    async def _apply_signin(self, active: _Active, app: _AppJudgement, verdict, site) -> dict:
        """S5-6: ``signed_in`` saves the attempt's credential by the broker's rules (a new item,
        or a linked one only when rejected in this run or unverified); anything else saves
        nothing."""
        gen = self._generation(active)
        run_id = active.run_id
        if verdict is None or verdict["verdict"] != "signed_in" or site is None:
            await self.broker.drop_attempt(run_id, app.site_id, app.capture_id)
            return {"outcome": "not_saved", "generation": gen}
        name = site.display_name
        title = " ".join("".join(c if c.isprintable() else " " for c in name).split())[:80]
        reply = await self.broker.save_attempt(run_id, site.site_id, app.capture_id,
                                               title or site.site_id)
        if reply.get("saved") and reply.get("item_id"):
            site = self.store.get_site(site.site_id) or site
            if site.vault_item_id != reply["item_id"]:
                self.store.put_site(replace(site, vault_item_id=reply["item_id"]))
            active.changes.append(Change("credential_saved", None, "",
                                         "A password was saved to the vault."))
            self._event(run_id, "credential_saved", "saved",
                        f"The {name} login from the live view was saved to the vault.")
            return {"outcome": "credential_saved", "generation": gen, "site": name}
        if reply.get("reason") == "item_kept":
            self._event(run_id, "credential_not_saved", "item_kept",
                        "The operator signed in with a different password than the 1Password "
                        "item holds; the item was left as it is.")
            return {"outcome": "credential_not_saved", "generation": gen, "site": name}
        return {"outcome": "not_saved", "generation": gen}

    def _run_so_far(self, active: _Active) -> list[dict]:
        """The packet's ``run_so_far``: the last three judged actions with the check's verdict,
        and, for one the operator was asked about, how that question ended, read from its
        stored state. Without it an asked step stays a bare ``ask`` and reads as still held
        after the operator's Yes ran it."""
        out = []
        for entry in active.judged:
            item = {"action": entry["action"], "verdict": entry["verdict"]}
            qid = entry.get("question_id")
            q = self.store.get_question(qid) if qid else None
            said = _question_outcome(q) if q is not None else None
            if said:
                item["outcome"] = said
            out.append(item)
        return out

    def _record_judgement(self, active: _Active, pending, verdict, latency_ms) -> dict | None:
        """Design §2.5 Record: every judgement is a run event (verdict, category, latency, the
        check's one-line reason, masked); ``run_so_far`` keeps the last three. Returns the
        ``run_so_far`` entry (None for a re-judgement), which a question it raises names."""
        if verdict is None:
            text = f"Check of {pending.label}: no verdict."
            outcome = "no_verdict"
        else:
            seconds = "" if latency_ms is None else f", {latency_ms / 1000:.1f} s"
            category = f"{verdict.get('category')}, " if verdict.get("category") else ""
            text = (f"Check of {pending.label}: {verdict['verdict']} ({category}"
                    f"{seconds.lstrip(', ')}): {gate.one_line(verdict.get('reason'))}")
            outcome = verdict["verdict"]
        self._event(active.run_id, "judgement", outcome, text)
        if pending.kind == "rejudge":
            return None
        entry = {"action": pending.label,
                 "verdict": verdict["verdict"] if verdict else "no_verdict"}
        active.judged.append(entry)
        del active.judged[:-3]
        return entry

    async def _apply(self, active: _Active, pending, body: dict) -> tuple[dict, Any]:
        """S2-7, in order: F1, F2, look_first, ask, go. Returns (response, successor)."""
        kind = "rejudge" if pending.kind == "rejudge" else "action"
        verdict = body.get("verdict")
        if verdict is not None and not gate.verdict_within_bounds(kind, verdict):
            verdict = None     # F1: outside the reply bounds is no verdict
        entry = self._record_judgement(active, pending, verdict, body.get("latency_ms"))
        if kind == "rejudge":
            return await self._apply_rejudge(active, pending, verdict)
        gen = self._generation(active)
        if verdict is None:
            return {"outcome": "check_unavailable", "generation": gen}, None
        if verdict["verdict"] == "go" and not gate.go_allowed(verdict["category"], pending.packet):
            return await self._ask(active, pending, verdict, f2=True, entry=entry)   # F2
        if verdict["verdict"] == "look_first":
            return {"outcome": "look_first", "generation": gen,
                    "note": gate.one_line(verdict.get("look_first") or verdict.get("reason"),
                                          gate.MAX_NOTE)}, None
        if verdict["verdict"] == "ask":
            return await self._ask(active, pending, verdict, f2=False, entry=entry)
        return await self._go(active, pending, verdict)

    def _generation(self, active: _Active) -> int:
        session = active.session
        return session.view_generation if session is not None else 0

    # -- executing (S2-8, S2-10, S2-11, G2) ---------------------------------------------

    async def _current_target(self, active: _Active, op) -> tuple[bool, Any]:
        """The frozen target on the current page: ``(True, target)`` for the same generation or
        design §2.6's re-resolution (same address, exactly one control with the frozen role,
        name, form identity and exact destination, its element handle issued fresh); a
        page-level action (``back``) needs the same generation or address, an app-issued link
        only the same session. ``(False, None)`` when the session or the target is gone."""
        session = active.session
        if (session is None or self.browser.session is not session or active.ref != op.session_ref
                or session.page is None or session.page.is_closed()):
            return False, None
        target = op.target
        if target is None:
            same = session.view_generation == op.generation or session.page.url == op.url
            return same, None
        if target.element is None:
            return True, target
        if session.view_generation == op.generation:
            return True, target
        page = session.page
        if page.url != op.url:
            return False, None
        try:
            reading = await self.ops.read_page(page, None, policy=active.policy, with_handles=True)
        except Exception:  # noqa: BLE001
            return False, None
        # Exactly one match on the whole page, in a frame at the target's own frame address.
        matches = gate.resolve_matches(reading.data, op.frozen or {}, op.frame_url)
        if len(matches) != 1 or not reading.handles or matches[0] >= len(reading.handles):
            return False, None
        control = reading.data["controls"][matches[0]]
        return True, PrivateTarget("link" if control.get("href") else "element", target.role,
                                   reading.handles[matches[0]], control.get("href"),
                                   target.frame_url, target.label)

    def _in_flight(self, run_id: str) -> bool:
        work = self._work
        return work is not None and not work.done() and self._work_run == run_id

    def _journal(self, active: _Active, applied, op) -> Callable[[], None]:
        """G2: immediately before a dispatching input, the operation is marked started and its
        response is stored, provisionally, as ``outcome_unknown`` (store writes are
        synchronous): any end of the work without a recorded result keeps that."""
        def started():
            op.dispatch_started = True
            applied.dispatch_started = True
            if applied.id:                 # an unjudged step has no verdict to replay
                self.store.put_response(active.run_id, "j:" + applied.id,
                                        {"outcome": "outcome_unknown",
                                         "generation": self._generation(active),
                                         "provisional": True})
        return started

    def _record_unknown(self, active: _Active, op, question=None) -> None:
        """F8: the press may or may not have happened. Recorded before any teardown, never
        retried."""
        name = op.label
        self._event(active.run_id, "action", "outcome_unknown",
                    f"Action: {name}. It may have run; it is not retried.")
        active.changes.append(Change("outcome_unknown", None, "", mask_text(name).text[:200]))
        if question is not None:
            with self.store.transaction():
                self.store.move_question(question.question_id, ("confirming",), "outcome_unknown",
                                         outcome="outcome_unknown", now=self.clock())
                self.store.transition(active.run_id, "waiting_approval", "running")
            active.question = None

    async def _guarded(self, active: _Active, applied, op, target, *, final: dict | None,
                       question=None) -> dict:
        """Dispatch ``op`` on ``target`` under F4 and F9 (and, for a tap, the final read). The
        response for the verdict being applied, or a raise after recording an uncertain
        outcome."""
        try:
            return await self._dispatch(active, applied, op, target, final=final,
                                        question=question)
        except GuardRefused:
            raise
        except BaseException as err:
            if not op.dispatch_started:
                if isinstance(err, (ActionError, DownloadRefused)):
                    return {"outcome": "not_run", "generation": self._generation(active),
                            "reason": getattr(err, "code", "blocked")}
                if isinstance(err, ProtocolError):
                    return {"outcome": "not_run", "generation": self._generation(active),
                            "reason": err.code}
                raise
            self._record_unknown(active, op, question)
            if isinstance(err, (ActionError, DownloadRefused, ProtocolError, asyncio.TimeoutError)):
                return {"outcome": "outcome_unknown", "generation": self._generation(active)}
            raise

    async def _dispatch(self, active: _Active, applied, op, target, *, final, question) -> dict:
        session = active.session
        page = session.page
        element = target.element if target is not None else None
        action = op.action
        expected = {"facts": op.frozen}
        seen: dict = {}
        same = gate.same_reading if question is not None else gate.same_as_judged

        async def reread(point=None, *, typed: bool = False) -> str:
            """F4 (and for a tap, the final read), immediately before the input."""
            if (active.session is not session or self.browser.session is not session
                    or active.ref != op.session_ref or page.is_closed()):
                return "changed"
            if element is None:
                if final is not None:
                    data = await self._read_whole(active, page)
                    if data is None or not same(final, data):
                        seen["reread"] = data
                        return "changed"
                return "ok"
            try:
                cur = await self.ops.read_page(page, element, policy=active.policy)
            except Exception:  # noqa: BLE001
                return "changed"
            data = cur.data
            if final is not None:
                if not same(final, data):
                    seen["reread"] = data
                    return "changed"
            facts = gate.target_facts(data.get("target"), enter=op.enter)
            if typed:
                if facts != expected["typed"]:
                    return "changed"
            elif facts != expected["facts"]:
                return "changed"
            if point is not None and not await self.ops.hit(element, point):
                return "moved"
            return "ok"

        journal = self._journal(active, applied, op)
        pacer = self.c.pacer(page) if page is not None else None
        result: dict = {"outcome": "done", "generation": 0}
        try:
            if op.nav:
                # The judged exact destination (the re-read above holds it equal to the page's).
                url = op.search_url or (op.frozen or {}).get("destination") or target.href
                if await reread() != "ok":
                    raise GuardRefused("changed")

                def before_goto():
                    if active.ref is None or active.ref.kind != op.dest_kind:
                        raise GuardRefused("changed")
                    journal()
                await self._navigate(active, url, before_goto=before_goto)
                if op.kind == "search":
                    obs = await self._observe(active)
                    result["observation"] = obs.to_wire()
            elif op.kind == "download" and op.quarantine is not None:
                # S4-6: the judged file, already fetched, is kept; nothing is sent.
                if await reread() != "ok":
                    raise GuardRefused("changed")
                if active.quarantine is not op.quarantine:
                    raise ProtocolError("blocked")
                journal()
                active.quarantine = None
                record = self.downloader.keep(op.quarantine)
                self._event(active.run_id, "file_saved", "done",
                            f"Saved {record.safe_name} ({record.size} bytes).")
                result["file"] = record.to_wire()
            elif op.kind == "download" and op.via == "link":
                if await reread() != "ok":
                    raise GuardRefused("changed")
                handle = op.handle
                if session.view_generation != op.generation:
                    handle = self.registry.issue(active.run_id, session.site_id,
                                                 session.view_generation, target)
                run = self._run(active.run_id)
                try:
                    record = await self.downloader.fetch(run, handle, session, active.policy,
                                                         on_send=journal)
                except DownloadRefused as err:
                    if not op.dispatch_started:
                        raise ProtocolError(err.code) from None
                    raise
                self._event(active.run_id, "file_saved", "done",
                            f"Saved {record.safe_name} ({record.size} bytes).")
                result["file"] = record.to_wire()
            elif op.kind == "download":
                run = self._run(active.run_id)
                record = await self._browser_download(active, session, run, target, guard=reread,
                                                      on_dispatch=journal)
                self._event(active.run_id, "file_saved", "done",
                            f"Saved {record.safe_name} ({record.size} bytes).")
                result["file"] = record.to_wire()
            elif action.kind == "click":
                await pacer.click(element, guard=reread, on_dispatch=journal)
            elif action.kind == "back":
                if await reread() != "ok":
                    raise GuardRefused("changed")
                journal()
                await page.go_back(wait_until="commit", timeout=NAV_TIMEOUT_MS)
            elif action.kind == "select":
                if await reread() != "ok":
                    raise GuardRefused("changed")
                journal()
                outcome = await self.ops.select_one(element, op.choice.element,
                                                    op.option["label"], op.option["value"])
                if outcome != "ok":
                    # The one-step check found a difference and set nothing: not dispatched.
                    op.dispatch_started = applied.dispatch_started = False
                    raise GuardRefused("changed")
            elif action.kind == "type":
                return await self._dispatch_type(active, applied, op, target, pacer, reread,
                                                 expected, journal, final=final,
                                                 question=question)
            else:
                raise ProtocolError("unsupported")
        except GuardRefused as refused:
            if refused.code == "changed" and "reread" in seen:
                refused.reread = seen["reread"]
            raise
        result["generation"] = self._generation(active)
        return result

    async def _dispatch_type(self, active: _Active, applied, op, target, pacer, reread, expected,
                             journal, *, final, question) -> dict:
        """S2-4: a type clicks into the field through the guard and types each printable
        character (slice 6b: and line break, as text, into a <textarea>) only while the field
        holds the focus. Without Enter the click is the
        dispatch. With Enter the dispatch is the Enter, behind ``press_enter``'s own guard:
        after a go, the field's facts re-read (everything as frozen but its own state, and its
        value as read back right after the last keystroke); after a tap, the typed page is
        re-judged first (``post_typing``), and the Enter follows only its ``same`` and a final
        read identical to the typed reading it judged."""
        element = target.element
        action = op.action
        frozen = op.frozen or {}
        if frozen.get("tag") == "input" and frozen.get("input_type") in gate.VALUE_TYPES:
            # Slice 5g: a value field takes the judged text as one in-page step, as a select
            # takes its option: F4 (and a tap's final read) first, then the step, which fires
            # nothing when the browser's value format refuses the text.
            if await reread() != "ok":
                raise GuardRefused("changed")
            journal()
            outcome = await self.ops.set_value(element, frozen["input_type"], action.text)
            if outcome not in ("ok", "changed_by_page"):
                op.dispatch_started = applied.dispatch_started = False
                raise GuardRefused("changed")
            return {"outcome": "done", "generation": self._generation(active),
                    "typed": "shown" if outcome == "ok" else "not_shown"}

        lines = "\n" in action.text
        # Typed by key a long list would outlast the check, and append.
        bulk = frozen.get("tag") == "textarea" and len(action.text) > LONG_TEXT and not action.enter

        async def text_field() -> bool:
            # Rule 0: still an editable text control (a field turned into a checkbox would take
            # a space as a toggle) and, for a text with line breaks or an insertion, still a real
            # <textarea> (slice 6b: an input would take a line break as implicit submission).
            facts = await self.ops.text_facts(element)
            return (isinstance(facts, dict) and bool(facts.get("editable_text"))
                    and not facts.get("secret")
                    and (not (lines or bulk) or bool(facts.get("textarea"))))

        async def focus() -> bool:
            # Rule 2 with rule 0: the focused element is the frozen target, still a text field.
            return await self.ops.focused(element) and await text_field()

        async def into_text_field(point) -> str:
            # Rule 0 at the click itself.
            if not await text_field():
                return "changed"
            return await reread(point)

        if bulk:
            await pacer.insert(element, action.text, guard=into_text_field, on_dispatch=journal,
                               focus=focus)
            return {"outcome": "done", "generation": self._generation(active),
                    **await self._typed_shown(element, action.text)}
        if not (question is not None and op.typed_judged):
            await pacer.type(element, action.text, guard=into_text_field,
                             on_dispatch=None if action.enter else journal, focus=focus,
                             line_breaks=lines)
            if not action.enter:
                return {"outcome": "done", "generation": self._generation(active),
                        **await self._typed_shown(element, action.text)}
            if question is not None:
                return {"post_typing": True}
            value = await self.ops.value(element)
            try:
                cur = await self.ops.read_page(active.session.page, element,
                                               policy=active.policy)
            except Exception:  # noqa: BLE001
                raise GuardRefused("changed") from None
            typed = gate.target_facts(cur.data.get("target"), enter=True)
            if (typed is None or gate.facts_but_state(typed) != gate.facts_but_state(op.frozen)
                    or (typed.get("state") or {}).get("value") != value):
                raise GuardRefused("changed")
        else:
            typed = gate.target_facts((final or {}).get("target"), enter=True)
        expected["typed"] = typed

        async def at_enter() -> str:
            if not await focus():
                return "changed"
            return await reread(typed=True)
        await pacer.press_enter(at_enter, on_dispatch=journal)
        return {"outcome": "done", "generation": self._generation(active)}

    async def _typed_shown(self, element, text: str) -> dict:
        """Slice 5g: whether the field shows what was typed, read back after the last key — one
        bit for the agent (a field that reformats or refuses keys is often a widget), never the
        value itself (INV-BRW-043)."""
        try:
            value = await self.ops.value(element)
        except Exception:  # noqa: BLE001 — the field left the page: nothing to say
            return {}
        if not isinstance(value, str) or value == gate.NOT_SHOWN:
            return {}
        # A contenteditable keeps a typed space as a no-break space (r1 Astra). An empty text is
        # shown by an empty field, not by being part of whatever the field holds.
        same = (text.replace("\u00a0", " ") in value.replace("\u00a0", " ") if text
                else not value.strip())
        return {"typed": "shown" if same else "not_shown"}

    async def _read_whole(self, active: _Active, page) -> dict | None:
        try:
            return dict((await self.ops.read_page(page, None, policy=active.policy)).data)
        except Exception:  # noqa: BLE001
            return None

    async def _go(self, active: _Active, pending, verdict) -> tuple[dict, Any]:
        """S2-8: a go executes under F4 and F9; ``changed`` judges afresh, ``moved`` runs
        nothing."""
        gen = self._generation(active)
        found, target = await self._current_target(active, pending)
        if not found:
            return {"outcome": "not_run", "generation": gen, "reason": "changed"}, None
        try:
            # Under an approval the whole page must still be the one judged: a fee shown since
            # is judged afresh, never pressed on the earlier Yes.
            final = pending.reading if verdict.get("category") == "account_change" else None
            result = await self._guarded(active, pending, pending, target, final=final)
        except GuardRefused as refused:
            if refused.code == "moved":
                return {"outcome": "not_run", "generation": self._generation(active),
                        "reason": "moved"}, None
            fresh = await self._refreeze(active, pending)
            if fresh is None:
                return {"outcome": "not_run", "generation": self._generation(active),
                        "reason": "changed"}, None
            return {"outcome": "judgement_required", "generation": self._generation(active),
                    "judgement": fresh.wire()}, fresh
        if result.get("outcome") == "done":
            if verdict.get("category") == "account_change":
                active.changes.append(Change("action_confirmed", None, "",
                                             mask_text(pending.label).text[:200]))
            note = (f" Check: go ({verdict.get('category')}): "
                    f"{gate.one_line(verdict.get('reason'), 200)}")
            sent = pending.enter or (pending.verb == "click" and not pending.nav) or (
                pending.verb == "download" and pending.via == "button")
            done = await self._action_done(active, pending.label, note, sent=sent)
            result = {**result, "generation": done.generation}
        return result, None

    async def _refreeze(self, active: _Active, op):
        """F4 "judged afresh": the same action, frozen again on the current page (None when its
        target cannot be found there)."""
        found, target = await self._current_target(active, op)
        session = active.session
        if not found or session is None:
            return None
        try:
            if op.action is not None and target is not None:
                _search, option = await self._admissible(op.action.kind, target, op.choice,
                                                         op.action)
                if option is not None and option != op.option:
                    return None
            fresh = await self._freeze(
                active, session, target, dict(op.packet["action"]), kind=op.kind,
                dest_kind=op.dest_kind, action=op.action, handle=op.handle,
                search_url=op.search_url, enter=op.enter, via=op.via, name=op.entry_name,
                nav=op.nav, download=op.packet.get("downloaded_file"))
        except (ProtocolError, GuardRefused):
            return None
        fresh.choice, fresh.option = op.choice, op.option
        fresh.quarantine = op.quarantine
        fresh.after_lapse = op.after_lapse     # still the question after his lapsed Yes
        return fresh

    # -- questions (S2-9) ----------------------------------------------------------------

    async def _ask(self, active: _Active, pending, verdict, *, f2: bool,
                   entry: dict | None = None) -> tuple[dict, Any]:
        """An ask freezes a question: a confirmations row, the run waiting on it, and the
        frozen operation in memory. F2, F6, F6a and a blind reading decide whether a confirm
        button is offered. ``entry``, the step's ``run_so_far`` entry, is tied to the question
        so a later step's packet says how it ended."""
        session = active.session
        gen = self._generation(active)
        question = None if f2 else verdict.get("question")
        has_cc = bool((pending.reading or {}).get("has_cc"))
        category = verdict.get("category", "")
        blind = gate.blind(pending.reading or {})
        # A plan card shows the plan verbatim; money is never one, nor a question with no confirm.
        plan = None
        if (question is not None and category == "account_change" and not blind
                and not question.get("text_incomplete")
                and not conf.money(category, question, has_cc)):
            plan = pending.packet.get("plan")
        shot = None
        if plan is None and conf.wants_screenshot(category, question, has_cc) and not f2:
            _found, target = await self._current_target(active, pending)
            element = target.element if target is not None else None
            try:
                shot = await self.observer.capture_question(self._run(active.run_id), session,
                                                            active.policy, element)
            except Exception:  # noqa: BLE001 — no faithful screenshot
                shot = None
        name = (pending.reading or {}).get("target", {}) or {}
        view = conf.build_view(verdict, has_cc=has_cc, screenshot_id=shot, f2=f2,
                               verb=conf.VERBS.get(pending.verb, "use"),
                               name=name.get("name") or "", site=self._site_name(active, pending.url),
                               plan=plan, blind=blind)
        if pending.after_lapse:
            view["after_lapse"] = True
        qid = "q" + self.c.token().replace("=", "")
        run_id = active.run_id
        with self.store.transaction():
            self.store.create_question(qid, run_id, 1, pending.digest, view, self.clock())
            if not self.store.transition(run_id, "running", "waiting_approval"):
                raise ProtocolError("not_ready")
        if entry is not None:
            entry["question_id"] = qid
        active.question = conf.FrozenQuestion(qid, 1, pending.digest, pending, verdict,
                                              pending.packet, pending.reading, shot,
                                              view["confirm_label"])
        self._event(run_id, "question", "asked",
                    f"The operator is asked about {pending.label}"
                    + ("" if view["confirm_offered"] else " (live view only)") + ".",
                    screenshot_id=shot)
        return {"outcome": "asked", "generation": gen, "question_id": qid}, None

    async def get_question(self, run_id: str, question_id: str) -> dict:
        """``GET /v1/control/runs/{run}/questions/{question}``: the view ``post_question``
        renders (no exact destination)."""
        q = self.store.get_question(question_id)
        if q is None or q.run_id != self._run(run_id).run_id:
            raise ProtocolError("not_found")
        return {"question_id": q.question_id, "run_id": q.run_id, "revision": q.revision,
                "digest": q.digest, "state": q.state, "view": q.view}

    # -- the tap (S2-10) -----------------------------------------------------------------

    async def answer_question(self, run_id: str, question_id: str, body: dict) -> dict:
        """``POST /v1/control/runs/{run}/questions/{question}/answer`` (trusted callback). F7:
        a compare-and-set on (question, revision, digest, state); a repeat of an answered
        (question, revision) returns its stored response with ``repeat: true``."""
        q = self.store.get_question(question_id)
        if q is None or q.run_id != self._run(run_id).run_id:
            raise ProtocolError("not_found")
        if q.response is not None and q.revision == body["revision"] and q.digest == body["digest"]:
            return {**q.response, "repeat": True}
        if q.state != "open" or q.revision != body["revision"] or q.digest != body["digest"]:
            # A lapsed, superseded, expired or other-revision tap executes nothing.
            return {"outcome": "not_current", "generation": 0}

        async def work(active):
            q = self.store.get_question(question_id)
            if q.response is not None and q.revision == body["revision"] \
                    and q.digest == body["digest"]:
                return {**q.response, "repeat": True}
            if self._run(run_id).state == "running":
                # settled meanwhile by another answer (whose response is stored above) or by
                # an expiry: this one executes nothing
                return {"outcome": "not_current", "generation": self._generation(active)}
            if self._run(run_id).state == "waiting_live":
                # A live view asked from this question but never opened: the answer ends it
                # (the question's buttons answer it either way). Any other view is the
                # operator's until he presses Done.
                live = active.live
                if live is None or live.question_id != question_id or live.lease is not None:
                    raise ProtocolError("not_ready")
                await self.grants.end(run_id)
                await self._release_live(active)
                active.live = None
                if not self.store.transition(run_id, "waiting_live", "waiting_approval"):
                    raise ProtocolError("not_ready")
                self._event(run_id, "live_done", "waiting_approval",
                            "The operator answered the question instead of opening the live view.")
            frozen = active.question
            gen = self._generation(active)
            current = (q.state == "open" and q.revision == body["revision"]
                       and q.digest == body["digest"] and frozen is not None
                       and frozen.question_id == question_id
                       and (self.clock() - q.created_at).total_seconds() <= conf.QUESTION_TTL_S)
            if not current:
                return {"outcome": "not_current", "generation": gen}
            if isinstance(frozen, conf.SiteQuestion):
                return await self._answer_site(active, q, frozen, body)
            if isinstance(frozen, conf.LoginQuestion):
                return await self._answer_login(active, q, frozen, body)
            choice = body["choice"]
            ref = body["operator_ref"]
            if isinstance(choice, dict) and "item" in choice:
                raise ProtocolError("unsupported")      # an item answers a login question only
            if choice == "not_now":
                response = {"outcome": "declined", "generation": gen}
                self._settle_question(active, q, "declined", response, ref,
                                      text=_declined_text(ref))
                return response
            if isinstance(choice, dict):
                options = q.view.get("extra_options") or []
                if choice["extra"] >= len(options):
                    raise ProtocolError("unsupported")
                response = {"outcome": "answered", "generation": gen,
                            "label": options[choice["extra"]]}
                self._settle_question(active, q, "answered", response, ref,
                                      outcome=options[choice["extra"]],
                                      text=f"The operator chose “{options[choice['extra']]}”.")
                return response
            if not q.view.get("confirm_offered"):
                raise ProtocolError("unsupported")     # a live-only question has no confirm
            response, successor = await self._rejudge(active, frozen, rejudges=0)
            if response["outcome"] == "rejudgement_required":
                with self.store.transaction():
                    moved = self.store.move_question(question_id, ("open",), "confirming",
                                                     response=response, operator_ref=ref,
                                                     now=self.clock())
                    if not moved:
                        raise ProtocolError("conflict")
                active.pending = successor
            else:
                gone = response.get("reason") == "target_gone"
                self._settle_question(
                    active, q, "lapsed", response, ref,
                    text=("The question's target is gone or can no longer be told apart from another "
                          "(perhaps done in the live view); observe the page." if gone else ""))
            return response
        # "running" too: an identical answer racing the first one finds the run already
        # running and must still get the first answer's stored response (r1 Astra 2a)
        return await self._command(run_id, ("running", "waiting_approval", "waiting_live"), work,
                                   supersede=False)

    # -- the live view from a question (slice 3, S3-3) -----------------------------------

    async def live_from_question(self, run_id: str, question_id: str, body: dict) -> dict:
        """``POST /v1/control/runs/{run}/questions/{question}/live`` (trusted callback): an
        ``assist`` live view from the open question. The run goes ``waiting_approval →
        waiting_live``; at Done it returns to ``waiting_approval`` while the question is still
        open, with its row and its buttons. The response is stored under ``(question,
        revision, n)`` (G2): a repeat returns it with ``repeat: true`` and asks nothing again.
        A view already asked from this question answers ``live_open``."""
        q = self.store.get_question(question_id)
        if q is None or q.run_id != self._run(run_id).run_id:
            raise ProtocolError("not_found")
        key = f"live:{question_id}:{body['revision']}:{body['n']}"

        async def work(active):
            stored = self.store.get_response(run_id, key)
            if stored is not None:
                return {**stored, "repeat": True}
            q = self.store.get_question(question_id)
            state = self._run(run_id).state
            gen = self._generation(active)
            frozen = active.question
            if isinstance(frozen, (conf.LoginQuestion, conf.SiteQuestion)) and \
                    frozen.question_id == question_id:
                # a login question, or a sign-in card (slice 5n), offers no live view
                raise ProtocolError("unsupported")
            current = (q.state == "open" and q.revision == body["revision"]
                       and q.digest == body["digest"] and frozen is not None
                       and frozen.question_id == question_id
                       and (self.clock() - q.created_at).total_seconds() <= conf.QUESTION_TTL_S)
            if not current:
                response = {"outcome": "not_current", "generation": gen}
            elif state in ("waiting_live", "live"):
                live = active.live
                if live is None or live.question_id != question_id:
                    raise ProtocolError("not_ready")
                response = {"outcome": "live_open", "generation": gen}
            elif state != "waiting_approval":
                raise ProtocolError("not_ready")
            else:
                with self.store.transaction():
                    if not self.store.transition(run_id, "waiting_approval", "waiting_live"):
                        raise ProtocolError("not_ready")
                    grant = self.grants.issue(run_id, "assist", self.clock())
                    response = {"outcome": "live", "generation": gen, "grant": grant.to_wire()}
                    self.store.put_response(run_id, key, response)
                active.live = _Live("assist", site_id=active.ref.site_id if active.ref else None,
                                    question_id=question_id)
                self._event(run_id, "live_requested", "assist",
                            "The operator opened the live view from a question.")
                return response
            self.store.put_response(run_id, key, response)
            return response
        return await self._command(run_id, ("running", "waiting_approval", "waiting_live",
                                            "live"), work, supersede=False)

    def _after_live(self, live: _Live | None) -> str:
        """Where a live view returns the run: to its question while that is still open
        (design §2.5: at Done the same question stands), else to ``running``."""
        if live is not None and live.question_id is not None:
            q = self.store.get_question(live.question_id)
            if q is not None and q.state == "open":
                return "waiting_approval"
        return "running"

    async def expire_questions(self) -> None:
        """S3-4: an open question past its hour expires with its tap tokens: ``open →
        expired``, the run back to ``running`` (a live view from it returns there at Done),
        and a ``question`` event, so Fido's sweep continues the run."""
        active = self.active
        if active is None or active.ending is not None:
            return
        q = self.store.open_question(active.run_id)
        if q is None or q.state != "open" or \
                (self.clock() - q.created_at).total_seconds() <= conf.QUESTION_TTL_S:
            return
        async with self._lock:
            if self.active is not active or active.ending is not None:
                return
            with self.store.transaction():
                if not self.store.move_question(q.question_id, ("open",), "expired",
                                                outcome="expired", now=self.clock()):
                    return
                self.store.transition(active.run_id, "waiting_approval", "running")
            live = active.live
            if (self._run(active.run_id).state == "waiting_live" and live is not None
                    and live.question_id == q.question_id):
                # a view asked from the question but never opened ends with it (r3 Terra); one
                # the operator opened stays his until Done, which then returns to running
                await self.grants.end(active.run_id)
                await self._release_live(active)
                active.live = None
                if self.store.transition(active.run_id, "waiting_live", "running"):
                    self._event(active.run_id, "live_done", "running",
                                "The question expired before its live view was opened.")
            login = None
            if active.question is not None and active.question.question_id == q.question_id:
                if isinstance(active.question, conf.LoginQuestion):
                    login = active.question
                active.question = None
                self._sweep_quarantine(active)
            self._event(active.run_id, "question", "expired",
                        "The question expired unanswered; nothing was done.")
            if login is not None:
                active.vault_asked.add(login.site_id)
                self._login_needed(active, "not_found")

    async def expire_live(self) -> None:
        """A live view the model asked for (``request_live``) that nobody opened within a
        question's hour (``QUESTION_TTL_S``) cancels its run, with a ``live_done`` event
        (outcome ``expired``): nothing else ends a view nobody comes for, and the run cannot
        go on without it. A view from a question ends with that question
        (``expire_questions``); one the operator opened stays his until Done."""
        active = self.active
        if active is None or active.ending is not None:
            return
        live = active.live
        if (live is None or live.asked_at is None
                or (self.clock() - live.asked_at).total_seconds() <= conf.QUESTION_TTL_S
                or self._run(active.run_id).state != "waiting_live"):
            return
        async with self._lock:
            if (self.active is not active or active.ending is not None
                    or active.live is not live
                    or self._run(active.run_id).state != "waiting_live"):
                return
            self._event(active.run_id, "live_done", "expired",
                        "Nobody opened the live view within an hour, so the run was cancelled.")
            await self._end(active, "cancelled", "run_cancelled", "cancelled")

    def _settle_question(self, active: _Active, q, state: str, response: dict | None,
                         operator_ref: str | None, *, outcome: str | None = None,
                         text: str = "") -> None:
        """The question's last move: the run is running again. ``response`` is the answer's
        stored response, given only when the move is the answer itself."""
        with self.store.transaction():
            self.store.move_question(q.question_id, ("open", "confirming"), state,
                                     response=response, operator_ref=operator_ref,
                                     outcome=outcome, now=self.clock())
            self.store.transition(active.run_id, "waiting_approval", "running")
        active.question = None
        self._event(active.run_id, "question", state, text or f"The question was {state}.")

    async def _rejudge(self, active: _Active, frozen, *, rejudges: int,
                       post_typing: bool = False, typed_reading: dict | None = None,
                       typed_target=None) -> tuple[dict, Any]:
        """S2-10 steps 1–4: a fresh whole reading, the difference over the whole readings, the
        private ``destination_changed``, and the re-judgement input as a pending ``rejudge``.
        Too large a difference, or a blind reading, has no ``same``: the question lapses with
        design §2.3's copy. A reading only capped goes on: ``all_controls`` compares what it does
        not list."""
        op = frozen.pending
        gen = self._generation(active)
        if typed_reading is not None:
            current, target = typed_reading, typed_target
        else:
            found, target = await self._current_target(active, op)
            if not found:
                return {"outcome": "lapsed", "generation": gen, "reason": "target_gone"}, None
            session = active.session
            element = target.element if target is not None else None
            try:
                current = dict((await self.ops.read_page(session.page, element,
                                                         policy=active.policy)).data)
            except Exception:  # noqa: BLE001
                return {"outcome": "lapsed", "generation": gen, "reason": "unreadable"}, None
            if element is None and target is not None and target.href:
                current["target"] = (op.reading or {}).get("target")
        if gate.blind(current):
            return {"outcome": "lapsed", "generation": gen, "copy": conf.COPY_INCOMPLETE,
                    "reason": "incomplete"}, None
        diff = gate.difference(frozen.approved_reading, current)
        if not gate.difference_fits(diff):
            return {"outcome": "lapsed", "generation": gen, "copy": conf.COPY_TOO_MUCH,
                    "reason": "too_much"}, None
        dest_changed = (gate.exact_destination(current.get("target"), enter=op.enter)
                        != gate.exact_destination(frozen.approved_reading.get("target"),
                                                  enter=op.enter)
                        or gate.exact_destination(current.get("target"))
                        != gate.exact_destination(frozen.approved_reading.get("target")))
        q = frozen.verdict.get("question") or {}
        packet_action = dict(frozen.approved_packet["action"])
        current_packet = gate.build_packet(
            current, current.get("target"), packet_action, task=active.spec.task_text,
            session="signed_in" if op.dest_kind == "account" else "public",
            site=self._site_name(active, active.session.page.url if active.session else op.url),
            run_so_far=frozen.approved_packet.get("run_so_far", []),
            download=frozen.approved_packet.get("downloaded_file"),   # the same fetched file
            plan=frozen.approved_packet.get("plan"),
            approved=frozen.approved_packet.get("approved"))
        rj = {"approved_packet": frozen.approved_packet,
              "approved_question": {"text": q.get("text", ""),
                                    "confirm_label": q.get("confirm_label", ""),
                                    "highlight": list(q.get("highlight") or [])},
              "current_packet": current_packet, "difference": diff,
              "destination_changed": dest_changed}
        body = gate.canonical(rj)
        session = active.session
        pending = gate.PendingJudgement(
            id="j" + self.c.token().replace("=", ""), kind="rejudge", packet_bytes=body,
            digest=gate.digest(body), generation=session.view_generation,
            session_ref=active.ref, url=session.page.url, created_at=self.clock(),
            target=target, frozen=op.frozen, reading=current, packet=rj,
            label=f"re-check of {op.label}", dest_kind=op.dest_kind,
            question_id=frozen.question_id, rejudges=rejudges + 1, post_typing=post_typing,
            destination_changed=dest_changed)
        return {"outcome": "rejudgement_required", "generation": session.view_generation,
                "question_id": frozen.question_id, "judgement": pending.wire()}, pending

    async def _apply_rejudge(self, active: _Active, pending, verdict) -> tuple[dict, Any]:
        """A re-judgement's verdict (S2-10): ``changed``, no verdict, ``same`` with
        ``text_incomplete`` or with ``destination_changed`` lapse the question into a fresh
        judgement; ``same`` leads to the final read, then F4/F9 and the press."""
        frozen = active.question
        gen = self._generation(active)
        if frozen is None or frozen.question_id != pending.question_id:
            raise ProtocolError("judgement_stale")
        q = self.store.get_question(frozen.question_id)
        if (verdict is None or verdict["verdict"] != "same" or verdict.get("text_incomplete")
                or pending.destination_changed):
            return await self._lapse(active, frozen, q)
        op = frozen.pending
        if pending.post_typing:
            op.typed_judged = True
        target = pending.target
        try:
            result = await self._guarded(active, pending, op, target, final=pending.reading,
                                         question=frozen)
        except GuardRefused as refused:
            reread = refused.reread
            if refused.code == "changed" and reread is not None and pending.rejudges < conf.MAX_REJUDGES:
                # The final read differs: the re-judgement runs again on the new reading.
                if pending.post_typing:
                    return await self._rejudge(active, frozen, rejudges=pending.rejudges,
                                               post_typing=True, typed_reading=reread,
                                               typed_target=target)
                return await self._rejudge(active, frozen, rejudges=pending.rejudges)
            if refused.code == "moved":
                # The answer's stored response stays the one it was given.
                self._settle_question(active, q, "lapsed", None, None)
                return {"outcome": "not_run", "generation": self._generation(active),
                        "reason": "moved"}, None
            return await self._lapse(active, frozen, q)
        if result.get("post_typing"):
            # A tapped type+Enter: typed, no Enter yet; the typed page is re-judged first.
            if pending.rejudges >= conf.MAX_REJUDGES:
                return await self._lapse(active, frozen, q)
            session = active.session
            try:
                typed = dict((await self.ops.read_page(session.page, target.element,
                                                       policy=active.policy)).data)
            except Exception:  # noqa: BLE001
                return await self._lapse(active, frozen, q)
            return await self._rejudge(active, frozen, rejudges=pending.rejudges,
                                       post_typing=True, typed_reading=typed,
                                       typed_target=target)
        if result.get("outcome") == "done":
            self._executed(active, frozen, q)
        elif result.get("outcome") == "not_run":
            self._settle_question(active, q, "lapsed", None, None)
        return result, None

    def _executed(self, active: _Active, frozen, q) -> None:
        """An executed change: the question executed, the run running again, and a Change with
        the question screenshot's id (design case 6)."""
        with self.store.transaction():
            self.store.move_question(frozen.question_id, ("confirming",), "executed",
                                     outcome="executed", now=self.clock())
            self.store.transition(active.run_id, "waiting_approval", "running")
        active.changes.append(Change("action_confirmed", None, "",
                                     mask_text(frozen.confirm_label).text[:200]))
        if q.view.get("plan"):
            active.approved = {"site_key": self._site_key(active, frozen.pending.url),
                               "plan": frozen.approved_packet["plan"]}
        active.looked.clear()                # a step: every page may be looked at again
        self._event(active.run_id, "action", "confirmed",
                    f"Action confirmed by the operator: {frozen.pending.label}.",
                    screenshot_id=frozen.screenshot_id)
        active.question = None

    async def _lapse(self, active: _Active, frozen, q) -> tuple[dict, Any]:
        """The question lapses (nothing executes) and the action is judged afresh on the current
        page; if that asks, a new question with a new screenshot follows."""
        with self.store.transaction():
            self.store.move_question(frozen.question_id, ("open", "confirming"), "lapsed",
                                     outcome="lapsed", now=self.clock())
            self.store.transition(active.run_id, "waiting_approval", "running")
        active.question = None
        self._event(active.run_id, "question", "lapsed",
                    "The page changed before the confirmed action ran; it is checked again.")
        fresh = await self._refreeze(active, frozen.pending)
        gen = self._generation(active)
        if fresh is None:
            return {"outcome": "lapsed", "generation": gen, "reason": "changed"}, None
        fresh.after_lapse = True          # a question it raises says his last Yes did not run
        return {"outcome": "judgement_required", "generation": gen,
                "judgement": fresh.wire()}, fresh

    # ------------------------------------------------------------------ downloads and files

    async def _download(self, active: _Active, handle: str):
        session = await self._ensure_session(active)
        try:
            target = self.registry.resolve(handle, active.run_id, session.view_generation)
        except StaleHandle:
            raise ProtocolError("stale_handle") from None
        via = "link" if (target.kind == "link" and isinstance(target.href, str)) else (
            "button" if target.element is not None else None)
        if via is None:
            raise ProtocolError("unsupported")
        dest = active.ref.kind if active.ref is not None else "public"
        if gate.in_scope(dest, "download", download_via=via):
            pending = await self._freeze(active, session, target, {"kind": "download"},
                                         kind="download", dest_kind=dest, handle=handle, via=via)
            return ActionResult("judgement_required", session.view_generation,
                                {"judgement": pending.wire()})
        # A link download in the public session (G1 a): fetched in that session; a type
        # outside the document set is judged before it is kept (S4-6).
        run = self._run(active.run_id)
        if active.ref is None or active.ref.kind != "public":
            raise ProtocolError("blocked")
        try:
            got = await self.downloader.fetch(run, handle, session, active.policy,
                                              quarantine=True)
        except DownloadRefused as err:
            raise ProtocolError(err.code) from None
        if isinstance(got, Quarantined):
            active.quarantine = got
            try:
                pending = await self._freeze(active, session, target, {"kind": "download"},
                                             kind="download", dest_kind="public", handle=handle,
                                             via="link", download=got.packet())
            except BaseException:
                self._sweep_quarantine(active)
                raise
            pending.quarantine = got
            return ActionResult("judgement_required", session.view_generation,
                                {"judgement": pending.wire()})
        self._event(active.run_id, "file_saved", "done",
                    f"Saved {got.safe_name} ({got.size} bytes).")
        return got

    def _sweep_quarantine(self, active: _Active) -> None:
        """S4-6: a quarantined file lives exactly as long as a pending judgement or a question
        that holds it; at any other end it is deleted (kept only by its execution)."""
        q = active.quarantine
        if q is None:
            return
        pending, question = active.pending, active.question
        if (pending is not None and pending.quarantine is q) or (
                question is not None and question.pending is not None
                and question.pending.quarantine is q):
            return
        active.quarantine = None
        self.downloader.discard(q)

    async def _browser_download(self, active: _Active, session, run, target, *, guard=None,
                                on_dispatch=None):
        fut = asyncio.get_running_loop().create_future()
        active.capture = fut
        try:
            await self.c.pacer(session.page).click(target.element, guard=guard,
                                                   on_dispatch=on_dispatch)
            try:
                download = await asyncio.wait_for(fut, DOWNLOAD_WAIT_S)
            except asyncio.TimeoutError:
                raise ProtocolError("blocked") from None
            return await self.downloader.capture_browser_download(run, download, session,
                                                                  active.policy)
        finally:
            active.capture = None

    async def download(self, run_id: str, handle: str):
        async def body(active):
            try:
                return await self._download(active, handle)
            except ActionError:
                raise ProtocolError("blocked") from None
        return await self._command(run_id, ("running",), body)

    async def archive(self, run_id: str, file_ids: tuple):
        async def body(active):
            run = self._run(active.run_id)
            return await asyncio.to_thread(self.downloader.make_archive, run, file_ids)
        return await self._command(run_id, ("running",), body)

    async def file_bytes(self, file_id: str) -> tuple:
        record = self.store.get_file(file_id)
        if record is None:
            raise ProtocolError("not_found")
        try:
            data = await asyncio.to_thread(Path(record.private_path).read_bytes)
        except OSError:
            raise ProtocolError("not_found") from None
        return record, data

    async def ack_delivery(self, file_id: str, body: dict) -> dict:
        return self.downloader.ack_delivery(file_id, body["requester_id"], body["delivery_id"],
                                            body["outcome"])

    # ------------------------------------------------------------------ live view

    async def request_live(self, run_id: str, purpose: str) -> LiveGrant:
        self._refuse(self._run(run_id), ("running",))
        work = self._work
        if work is not None and not work.done() and self._work_run == run_id:
            try:
                await asyncio.wait_for(asyncio.shield(work), DRAIN_S)
            except asyncio.TimeoutError:
                self._cut("not_ready", run_id)
            except BaseException:  # noqa: BLE001 — its own command reports it
                pass

        async def body(active):
            # A login needs an account session; an assist view pauses the model in any session.
            if purpose == "login" and (active.ref is None or active.ref.kind != "account"):
                raise ProtocolError("unsupported")
            site = self.store.get_site(active.ref.site_id) if purpose == "login" else None
            if site is not None and site.login_origins:
                # A sign-in view gets the guest unlocked before its link goes out (the operator
                # may use 1Password's own fill there): on the run's live session, reopened when
                # gone or stale as begin_live would, once per browser session. Not completed,
                # or K1 under it: the request goes on.
                session = await self._ensure_session(active)
                if active.guest_for is not session:
                    active.guest_for = session
                    await self._guest(active, site)
                if purpose == "login":
                    # The link goes out with the page there (a K1 under the guest flow included).
                    await self._ensure_session(active)
                    await self._load_for_login(active, site)
            if not self.store.transition(run_id, "running", "waiting_live"):
                raise ProtocolError("not_ready")
            active.live = _Live(purpose, site_id=active.ref.site_id if active.ref else None,
                                asked_at=self.clock())
            self._event(run_id, "live_requested", purpose,
                        f"The model asked for {'an' if purpose[0] in 'aeiou' else 'a'} {purpose} live view.")
            return self.grants.issue(run_id, purpose, self.clock())
        return await self._command(run_id, ("running",), body)

    async def begin_live(self, run_id: str, expose: Callable) -> None:
        """Task 13's claim, before the operator's input is exposed: waiting_live → live; the
        manual lease; on an account session the capture is armed and acknowledged before
        ``expose`` runs."""
        async def body(active):
            live = active.live
            if live is None:
                raise ProtocolError("not_ready")
            began = time.monotonic()

            def stage(name: str) -> None:   # mechanical: a stage and its time, nothing of a page
                log.info("live view open: %s at %.2f s", name, time.monotonic() - began)

            session = await self._ensure_session(active)
            login_site = (self.store.get_site(active.ref.site_id)
                          if live.purpose == "login" and active.ref.kind == "account" else None)
            if login_site is not None and login_site.login_origins:
                # The session can have been replaced since the request.
                await self._load_for_login(active, login_site)
                session = await self._ensure_session(active)
            stage("session")
            if not self.store.transition(run_id, "waiting_live", "live"):
                raise ProtocolError("not_ready")
            # Operator ruling E-T12-1 = A1: from here every failure, in any session kind,
            # leaves through the one cleanup path (_live_unavailable → _release_live).
            try:
                if active.ref.kind != "account":
                    await expose()   # a public session: the model is paused; no lease, no window
                    return None
                site_id = active.ref.site_id
                lease = Lease("m" + self.c.token().replace("=", ""), run_id, site_id,
                              session.generation, "manual")
                await self._consume_events(active)   # earlier events are not the view's
                live.lease = lease
                live.token = await self.proxy.begin_manual(lease, purpose=live.purpose)
                stage("lease")
                if active.policy is not None:
                    active.policy = replace(active.policy, manual_lease=lease)
                site = self.store.get_site(site_id)
                sid = await self.driver.open_session(
                    "capture", run_id, site_id, session, self.proxy, site.login_origins,
                    reader=lambda page: self._signin_reading(active, page))
                live.capture_sid = sid
                stage("capture session")
                # The capture is the view's from the moment the broker armed it (before the
                # input is exposed), so every end of the view — a failed exposure included —
                # ends it.
                reply = await arm_capture_then(_OwnCapture(self.broker, live), run_id=run_id,
                                               site_id=site_id, driver_session=sid,
                                               purpose=live.purpose, expose=expose)
                if "error" in reply:
                    raise _LiveUnavailable(str(reply["error"]))
                stage("capture armed, view exposed")
                return None
            except _LiveUnavailable as unavailable:
                await self._live_unavailable(active, unavailable.code)
                raise ProtocolError("not_ready") from None
            except (ControlError, BrokerError) as err:
                await self._live_unavailable(active, getattr(err, "code", "unavailable"))
                raise ProtocolError("not_ready") from None
            except Exception:  # noqa: BLE001 — the view's own exposure failed: none is offered
                await self._live_unavailable(active, "internal")
                raise ProtocolError("not_ready") from None
        return await self._command(run_id, ("waiting_live",), body)

    async def _release_live(self, active: _Active) -> None:
        """End a live view's resources: end the broker's capture —
        reviewed first if it never was, so its values reach the broker, which warns about an
        unsaved changed value — and close the capture session; the manual lease; then the
        window its opener opened."""
        live = active.live
        if live is None:
            return
        failure = None   # raised once the lease and the window are released too
        if live.capture_id is not None:
            # The view's capture ends with it: reviewed if it never was (its values reach the
            # broker), then dropped — warning about an unsaved changed value — so the site's
            # next live view can capture again. A capture that cannot be ended is a broker
            # failure: the run is interrupted and end_run retires it.
            capture_id, live.capture_id = live.capture_id, None
            try:
                reply = await self.broker.end_capture(active.run_id, live.site_id, capture_id)
            except Exception:  # noqa: BLE001
                reply = {"error": "unavailable"}
            if "error" in reply:
                failure = "broker_" + str(reply["error"])
        if live.capture_sid is not None:
            sid, live.capture_sid = live.capture_sid, None
            try:
                await self.driver.close_session(sid)
            except BrokerError:
                failure = failure or "browser_unconfirmed"
        if live.lease is not None:
            try:
                await self.proxy.end_manual(live.lease.lease_id)
            except ControlError as err:
                failure = failure or "proxy_" + err.code
        await self._consume_events(active)   # every event of the view, before it is judged
        if live.token is not None:
            token, live.token = live.token, None
            try:
                await self.proxy.close_window(token)
            except ControlError as err:
                failure = failure or "proxy_" + err.code
        if failure is not None:
            raise _Dependency(failure)

    async def _live_unavailable(self, active: _Active, code: str) -> None:
        live = active.live
        await self.grants.end(active.run_id)   # no operator input after this, before the release
        try:
            await self._release_live(active)
        finally:
            active.live = None
        back = self._after_live(live)
        if self.store.transition(active.run_id, "live", back) or \
                self.store.transition(active.run_id, "waiting_live", back):
            self._event(active.run_id, "live_unavailable",
                        code if code.isascii() and len(code) <= 40 else "unavailable",
                        "The live view could not be offered.")

    async def live_done(self, run_id: str) -> Run:
        """The operator's Done (Task 13 closes the screen and restores the window first). A
        login view learns the sign-in provider it saw (slice 5g, operator ruling 2026-10-31:
        no "Did you sign in?")."""
        return await self._command(run_id, ("live", "waiting_live"),
                                   lambda active: self._done(active, True))

    async def live_complete(self, run_id: str, operator_ref: str) -> Run:
        """``POST /v1/control/runs/{run}/live-complete``: Done without the operator's page (no
        provider binding is learned through it)."""
        return await self._command(run_id, ("live", "waiting_live"),
                                   lambda active: self._done(active, False))

    async def _done(self, active: _Active, learn: bool) -> Run:
        run_id = active.run_id
        live = active.live
        state = self._run(run_id).state
        # Task 13: the viewer's sockets are closed and Chrome's window is back at 1920×1080,
        # verified, before anything of the view is released or automation could resume (also
        # for /v1 …/live-complete, which never passes through the ingress Done).
        restored = await self.grants.close_view(run_id)
        await self.grants.end(run_id)
        if not restored:
            raise _Dependency("window_unrestored")
        back = self._after_live(live)
        if live is None or state == "waiting_live":
            await self._release_live(active)
            active.live = None
            self.store.transition(run_id, state, back)
            self._event(run_id, "live_done", back)
            return self._run(run_id)
        if (live.purpose in ("login", "assist") and live.capture_id is not None
                and active.ref is not None and active.ref.kind == "account"):
            await self._take_signin(active, live)
        await self._release_live(active)
        active.live = None
        if active.policy is not None:
            active.policy = replace(active.policy, manual_lease=None)  # as the proxy holds it now
        if live.purpose == "login" and learn:
            # Exactly one provider attempt and the provider's redirect back to the site's own
            # callback (``learn_provider_binding``). Accepted residual (ruling 2026-10-31): a
            # sign-in that failed but was still redirected back teaches the binding too.
            site = self.store.get_site(live.site_id)
            binding = learn_provider_binding(live.events, site, "signed_in") if site else None
            if binding is not None:
                self.store.put_site(replace(site, provider_bindings=(binding,),
                                            revision=site.revision + 1))
                self.store.put_binding_key(site.site_id, self._binding_key_id)
                await self._install(active)   # the binding reaches the proxy
        self.store.transition(run_id, "live", back)
        self._event(run_id, "live_done", back)
        return self._run(run_id)

    # ------------------------------------------------------------------ events (proxy)

    async def _consume_events(self, active: _Active) -> None:
        async with self._events_lock:
            try:
                events, _dropped = await self.proxy.events(active.cursor)
            except ControlError:
                return
            for event in events:
                if event.seq <= active.cursor:
                    continue
                active.cursor = event.seq
                if event.run_id != active.run_id:
                    continue
                live = active.live
                if live is not None and live.lease is not None and live.purpose == "login":
                    live.events.append(event)

    async def _pump(self, active: _Active) -> None:
        while self.active is active and active.ending is None:
            await asyncio.sleep(PUMP_S)
            if not await self._pump_once(active):
                return

    async def _pump_once(self, active: _Active) -> bool:
        """One poll: a proxy that restarted (another start nonce) interrupts the run (False);
        otherwise its new events are consumed. A proxy that does not answer is polled again."""
        try:
            status = await self.proxy.status()
        except Exception:  # noqa: BLE001 — down or restarting: it refuses everything meanwhile
            return True
        if status.get("token", [None])[0] != active.nonce:
            self._spawn(self._interrupt(active.run_id, "proxy_restarted"))
            return False
        try:
            await self._consume_events(active)
        except Exception:  # noqa: BLE001 — never kills the pump
            log.warning("proxy events not processed")
        return True

    async def _interrupt(self, run_id: str, reason: str) -> None:
        self.grants.revoke(run_id)     # before waiting for the lock: no operator input meanwhile
        self._cut("interrupted", run_id)
        async with self._lock:
            active = self.active
            if active is not None and active.run_id == run_id and active.ending is None:
                await self._end(active, "interrupted", "run_interrupted", reason)

    # ------------------------------------------------------------------ ends

    async def finish(self, run_id: str, manifest) -> Run:
        async def body(active):
            issued = {s.source_id for s in self.store.sources_for(run_id)}
            if any(s not in issued for s in manifest.source_ids):
                raise ProtocolError("unsupported")
            run = self._run(run_id)
            for file_id in manifest.file_ids:
                rec = self.store.get_file(file_id)
                if rec is None or rec.run_id != run_id:
                    raise ProtocolError("unsupported")
            summary = mask_text(manifest.summary_masked).text
            active.record = RunRecord(run_id, run.spec.requester_id,
                                      tuple(g.site_id for g in run.spec.sites), summary, (),
                                      tuple(manifest.file_ids), (), self.clock())
            await self._end(active, "completed", "run_finished", manifest.expectation_status)
            return self._run(run_id)
        return await self._command(run_id, ("running",), body)

    async def cancel(self, run_id: str) -> Run:
        run = self._run(run_id)
        if run.state in TERMINAL_STATES:
            return run
        self.grants.revoke(run_id)     # before waiting for the lock: no operator input meanwhile
        self._cut("interrupted", run_id)
        async with self._lock:
            run = self._run(run_id)
            if run.state in TERMINAL_STATES:
                return run
            active = self.active
            if active is not None and active.run_id == run_id:
                if active.ending is None:
                    await self._end(active, "cancelled", "run_cancelled", "cancelled")
            elif run.state == "queued" and self.store.transition(run_id, "queued", "cancelled",
                                                                  now=self.clock()):
                self._event(run_id, "run_cancelled", "cancelled")
            return self._run(run_id)

    async def request_stopped(self, request_id: str) -> bool:
        """Whether the operator's Stop stopped this request id (``cancelled_requests``)."""
        return self.store.request_cancelled(request_id)

    async def cancel_requests(self, request_ids: tuple[str, ...]) -> dict:
        """``POST /v1/requests/cancel`` (the operator's Stop of one request, all its request
        ids at once). In one synchronous step — no await between — every id is decided: an id
        with no run yet is recorded in ``cancelled_requests`` (its create comes back
        cancelled), a queued run is recorded and moved to ``cancelled``, and the driving run,
        when its ending has not started, is recorded, its in-flight command cut and its ending
        begun (``cancelled``); a terminal run, or one whose ending another command already
        began, is not recorded. Only then is the driving run torn down, under the command
        lock. ``lease_next_run`` skips a recorded id, so no sibling is started meanwhile. Each
        id's ``stopped`` is whether it is recorded after this call (the same on a repeat)."""
        now = self.clock()
        ending: _Active | None = None
        for request_id in request_ids:
            run = self.store.run_by_request(request_id)
            if run is None:
                self.store.cancel_request_id(request_id, now=now)
            elif run.state == "queued":
                self.store.cancel_request_id(request_id, now=now)
                if self.store.transition(run.run_id, "queued", "cancelled", now=now):
                    self._event(run.run_id, "run_cancelled", "cancelled")
            elif run.state not in TERMINAL_STATES:
                active = self.active
                if active is not None and active.run_id == run.run_id and active.ending is None:
                    self.store.cancel_request_id(request_id, now=now)
                    self.grants.revoke(run.run_id)
                    self._cut("interrupted", run.run_id)       # before the ending begins
                    self._begin_end(active, "cancelled", "run_cancelled", "cancelled")
                    ending = active
        if ending is not None:
            async with self._lock:
                if self.active is ending and self._run(ending.run_id).state not in TERMINAL_STATES:
                    await self._end(ending, "cancelled", "run_cancelled", "cancelled")
        out = []
        for request_id in request_ids:
            run = self.store.run_by_request(request_id)
            out.append({"request_id": request_id,
                        "run_id": None if run is None else run.run_id,
                        "stopped": self.store.request_cancelled(request_id),
                        "state": "cancelled" if run is None else run.state})
        return {"requests": out}

    async def _end(self, active: _Active, state: str, kind: str, outcome: str, *,
                   advance: bool = True) -> None:
        """Move the run to ``finishing`` and tear it down; terminal only when confirmed. The
        teardown runs in its own task: a command that ends its own run is not cut by it."""
        self.grants.revoke(active.run_id)   # the live view ends now (no await before it)
        if active.ending is None:
            self._begin_end(active, state, kind, outcome)
        work = self._work
        if work is not None and work is not asyncio.current_task() and \
                self._work_run == active.run_id and not work.done():
            # Another task's work (never the caller's own): cut it before tearing down.
            self._cut("interrupted", active.run_id)
        pump = active.pump
        if pump is not None and pump is not asyncio.current_task():
            pump.cancel()
        ending = asyncio.ensure_future(self._teardown_and_settle(active, advance))
        cancelled = False
        while not ending.done():
            try:
                await asyncio.shield(ending)
            except asyncio.CancelledError:
                cancelled = True     # the teardown runs to its end whatever cancels the caller
        ending.result()
        if cancelled:
            raise asyncio.CancelledError

    def _begin_end(self, active: _Active, state: str, kind: str, outcome: str) -> None:
        """The synchronous start of a run's ending: from here the run is ``finishing`` and its
        ending is decided (``active.ending``), so every later command of it is refused."""
        active.ending = (state, kind, outcome)
        active.kept_cookies.clear()
        # The run's question ends with it: nothing it froze can run any more. (A press
        # that may have happened was already recorded outcome_unknown by its own work.)
        active.pending = None
        active.question = None
        self._sweep_quarantine(active)
        self.store.end_questions(active.run_id, now=self.clock(), confirming="expired")
        current = self._run(active.run_id).state
        if current not in TERMINAL_STATES and current != "finishing":
            self.store.transition(active.run_id, current, "finishing")

    async def _teardown_and_settle(self, active: _Active, advance: bool) -> None:
        await self._settle(active, await self._teardown(active), advance)

    async def _settle(self, active: _Active, confirmed: bool, advance: bool) -> None:
        if not confirmed:
            # Operator ruling E-T12-2 = B1: a run whose release is not confirmed ends
            # interrupted (never completed or cancelled), says so to its requester, and keeps
            # the browser lease: no queued run starts until the release is confirmed.
            log.warning("run release unconfirmed; retrying")
            await self._finalize(active, advance, confirmed=False)
            self._spawn(self._retry_teardown(active, advance))
            return
        await self._finalize(active, advance)

    async def _teardown(self, active: _Active) -> bool:
        """Every step bounded; a failure skips none of the later ones. True when the run's four
        external holds are released: the proxy acknowledged the revoke, the launcher proved no
        Chrome runs, no driver session remains and (account runs) the broker acknowledged
        ``end_run``."""
        # The live view first: no operator input after this point (the one operation in flight
        # has finished), before its capture, lease and window are released.
        await self.grants.end(active.run_id)
        if active.live is not None:
            try:
                await self._release_live(active)
            except Exception:  # noqa: BLE001
                pass
            active.live = None
        if active.driver_sid is not None:
            sid, active.driver_sid = active.driver_sid, None
            try:
                await self.driver.close_session(sid)
            except Exception:  # noqa: BLE001 — retired after the exit proof below
                pass
        revoked = False
        try:
            await self.proxy.revoke(active.run_id)
            revoked = True
        except Exception:  # noqa: BLE001
            pass
        active.session = None
        try:
            await self.browser.close()
        except Exception:  # noqa: BLE001
            pass
        stopped = False
        try:
            await self.browser.close(force=True)  # the launcher's exit proof
            stopped = True
        except Exception:  # noqa: BLE001
            pass
        if stopped:
            # Every driver session still open — one whose close was unconfirmed, or one a failed
            # opening left behind — is retired now that no Chrome runs (the driver accepts the
            # launcher's proof once its browser is gone).
            for sid in self.driver.open_sessions():
                try:
                    await self.driver.close_session(sid)
                except Exception:  # noqa: BLE001
                    pass
        if not active.broker_ended and active.spec.mode == "account":
            active.broker_ended = await self._broker_end_run(active.run_id)
        self.registry.drop_run(active.run_id)
        broker = active.broker_ended or active.spec.mode != "account"
        return revoked and stopped and not self.driver.open_sessions() and broker

    async def _retry_teardown(self, active: _Active, advance: bool) -> None:
        while True:
            await asyncio.sleep(TEARDOWN_RETRY_S)
            async with self._lock:
                if self.active is not active:
                    return
                if await self._teardown(active):
                    await self._finalize(active, advance)   # terminal already: frees the lease
                    return

    async def _finalize(self, active: _Active, advance: bool, *, confirmed: bool = True) -> None:
        """The terminal state, its event and (interrupted) its notice; with a confirmed
        release also the lease. An unconfirmed release ends the run interrupted and keeps the
        lease until a retry confirms it."""
        state, kind, outcome = active.ending
        run_id = active.run_id
        if not confirmed:
            state, kind, outcome = "interrupted", "run_interrupted", "release_unconfirmed"
        if active.record is not None:
            record = replace(active.record, changes=tuple(active.changes),
                             delivery_outcomes=tuple(self.store.deliveries_for_run(run_id)),
                             ended_at=self.clock())
            self.store.put_record(record)
            active.record = None
        if not confirmed and active.spec.mode == "account" and not active.broker_ended:
            # Durable before the terminal state: a restart before the retry confirms the
            # release still owes this run its end_run.
            self.store.owe_release(run_id, self.clock())
        if self.store.transition(run_id, "finishing", state, now=self.clock()):
            text = (_UNCONFIRMED_TEXT if not confirmed else _END_TEXT.get(state, ""))
            self._event(run_id, kind, state if state != "interrupted" else outcome, text)
            if state == "interrupted":
                self.store.add_notice(SafeNotice(
                    0, "run_interrupted", run_id, None,
                    _UNCONFIRMED_TEXT if not confirmed else
                    "A browser run was interrupted and was not resumed.", self.clock(),
                    dedupe_key=f"interrupted:{run_id}"))
        if not confirmed:
            return
        if self.active is active:
            self.active = None
        if advance:
            self._spawn(self._advance())

    # ------------------------------------------------------------------ forget, pairing

    async def forget(self, site_id: str, operator_ref: str) -> dict:
        site = self.store.get_site(site_id)
        if site is None:
            raise ProtocolError("not_found")
        active = self.active
        if active is not None and any(g.site_id == site_id for g in active.spec.sites):
            self.grants.revoke(active.run_id)
            self._cut("interrupted", active.run_id)
        async with self._lock:
            self._authority()
            active = self.active
            if active is not None and any(g.site_id == site_id for g in active.spec.sites):
                if active.ending is None:
                    await self._end(active, "interrupted", "run_interrupted", "site_forgotten")
                if self.active is active:
                    raise ProtocolError("not_ready")  # its browser is not confirmed closed yet
            try:
                await self.browser.forget(site_id)
            except Exception:  # noqa: BLE001
                raise ProtocolError("not_ready") from None
            site = self.store.get_site(site_id)
            self.store.put_site(replace(site, provider_bindings=(), revision=site.revision + 1))
            self.store.put_binding_key(site_id, None)
            reply = await self.broker.forget_site(site_id, operator_ref)
            if "error" in reply:
                raise ProtocolError("not_ready")
            # The site stays; only its login link goes: its next login asks again (slice 5).
            self.store.put_site(replace(self.store.get_site(site_id), vault_item_id=None))
            return {"site_id": site_id, "status": "forgotten"}

    def bindings_lost(self) -> set[str]:
        """Sites whose provider binding was learned under another proxy key than the current
        one (after an HA restore: the key is not backed up), or under no recorded key: their
        provider sign-in needs a live login (the setup pages say so)."""
        keys = self.store.binding_keys()
        return {s.site_id for s in self.store.list_sites()
                if s.provider_bindings and (self._binding_key_id is None
                                            or keys.get(s.site_id) != self._binding_key_id)}

    def revoke_authority(self) -> None:
        """Pairing rotation (``Pairing.arm``): every run and live grant issued under the old
        secret stops. No queued run is leased meanwhile."""
        self._revoking = True
        self.grants.revoke_all()     # every live view ends now, before the revocation runs
        self._cut("interrupted")     # work in flight stops now, before the revocation runs
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._revoking = False
            return
        loop.create_task(self._revoke_all())

    async def _revoke_all(self) -> None:
        try:
            active = self.active
            if active is not None:
                self._cut("interrupted", active.run_id)
            async with self._lock:   # commands queued meanwhile refuse (_authority)
                for run in self.store.list_runs(("queued",)):
                    if self.store.transition(run.run_id, "queued", "interrupted", now=self.clock()):
                        self._event(run.run_id, "run_interrupted", "pairing_revoked")
                await self.grants.end_all()
                active = self.active
                if active is not None and active.ending is None:
                    await self._end(active, "interrupted", "run_interrupted", "pairing_revoked",
                                    advance=False)
        finally:
            self._revoking = False
            self._spawn(self._advance())

    # ------------------------------------------------------------------ reads

    async def get_run(self, run_id: str):
        return self.store.get_run(run_id)

    async def list_active(self) -> list[ActiveRun]:
        with self.store.transaction():
            runs = self.store.list_runs(ACTIVE_STATES)
            out = []
            for r in runs:
                question = self.store.open_question(r.run_id)
                active = self.active
                own = active if active is not None and active.run_id == r.run_id else None
                wait = own.code_wait if own is not None else None
                out.append(ActiveRun(r.run_id, r.spec.request_id, r.state, r.created_at,
                                     question.question_id if question is not None else None,
                                     wait[0] if wait is not None else None,
                                     own.approved["plan"] if own and own.approved else None))
            return out

    async def events(self, run_id: str, after: int) -> list:
        return self.store.events_after(run_id, after)

    async def records(self, requester_id: str, since: datetime) -> list:
        return self.store.records_for(requester_id, since)

    async def notices(self, after: int) -> list:
        try:
            await self.broker.import_notices()
        except Exception:  # noqa: BLE001 — the store's copy still answers
            pass
        return self.store.notices_after(after)

    async def take_screenshot(self, run_id: str) -> str:
        """Slice 5b (F-j): the page as it stands now, captured like every operator screenshot
        (``Observer.capture_operator``: the page-view gate before and after, secret fields
        masked), for the plugin to show the operator what a confirmed step led to. A running
        run only; it supersedes nothing and records no step."""
        async def body(active):
            session = await self._ensure_session(active)
            shot = await self._screenshot(active, session)
            if shot is None:
                raise ProtocolError("not_ready")
            return shot
        return await self._command(run_id, ("running",), body, supersede=False)

    async def screenshot(self, run_id: str, screenshot_id: str) -> bytes:
        path = self.store.screenshot_path(run_id, screenshot_id)
        if path is None:
            raise ProtocolError("not_found")
        try:
            return await asyncio.to_thread(Path(path).read_bytes)
        except OSError:
            raise ProtocolError("not_found") from None

    async def sources(self, run_id: str) -> list:
        self._run(run_id)
        return self.store.sources_for(run_id)


def _sign_in_address(url) -> tuple[str | None, str, str]:
    """Slice 5n: a page address as a sign-in card reads it — its https origin in the one
    spelling registration stores (lowercase host, the default port dropped), the host as the
    card shows it, and the sign-in page without query or fragment. ``(None, "", "")`` for any
    other address."""
    try:
        parts = urlsplit(url) if isinstance(url, str) else None
        port = parts.port if parts is not None else None
    except ValueError:
        return None, "", ""
    if parts is None or parts.scheme != "https" or not parts.hostname:
        return None, "", ""
    try:
        origin = _https_origin("https://" + parts.hostname.lower()
                               + (f":{port}" if port is not None else ""))
    except ProtocolError:
        return None, "", ""
    start = origin + (parts.path or "/")
    if len(start) > 2048:
        start = origin + "/"
    return origin, origin[len("https://"):], start


def _question_outcome(q) -> str | None:
    """How an asked step's question ended, in words for the check's ``run_so_far``; None while
    it is still open."""
    if q.state == "executed":
        return "the operator said Yes and it ran"
    if q.state == "outcome_unknown":
        return "the operator said Yes; it may or may not have run"
    if q.state == "lapsed":
        # a lapse has several causes (a changed page, a target gone, no re-check): none is named
        return "the operator said Yes, but it did not run"
    if q.state == "declined":
        if (q.operator_ref or "").startswith("fido:"):
            return "Fido took the question back before the operator answered; it did not run"
        return "the operator said Not now; it did not run"
    if q.state == "answered":
        return f"the operator chose “{gate.one_line(q.outcome)}” instead; it did not run"
    if q.state == "expired":
        return "nobody answered within the hour; it did not run"
    return None


def _declined_text(ref: str) -> str:
    """What a Not now means: the operator's answer, or Fido taking back a question the operator
    never saw (an ``operator_ref`` of ``fido:<why>``)."""
    if ref.startswith("fido:"):
        return (f"Fido took this question back before the operator answered it "
                f"({ref[len('fido:'):]}); nothing was done.")
    return "The operator said not now; nothing was done."


def _label(text) -> str:
    """The model's everyday name for a site: a label only, masked, one line."""
    one = " ".join("".join(c if c.isprintable() else " " for c in str(text or "")).split())
    return mask_text(one).text.strip()[:60]


def _replayed(stored: dict) -> dict:
    """A stored response, as a repeat answers it."""
    return {**{k: v for k, v in stored.items() if k != "provisional"}, "repeat": True}


class _OwnCapture:
    """``arm_capture_then``'s broker, recording the capture id on the view the moment the
    broker acknowledges it (before the input is exposed)."""

    def __init__(self, broker, live):
        self._broker, self._live = broker, live

    async def begin_capture(self, run_id, site_id, driver_session, purpose):
        reply = await self._broker.begin_capture(run_id, site_id, driver_session, purpose)
        if isinstance(reply, dict) and "capture_id" in reply:
            self._live.capture_id = reply["capture_id"]
        return reply


class _LiveUnavailable(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


_UNCONFIRMED_TEXT = ("The browser run was interrupted: Casa Browser could not confirm that it "
                     "released the browser and its permissions. The next run waits until it can.")

_END_TEXT = {"completed": "The run finished.", "cancelled": "The run was cancelled.",
             "failed": "The run could not start.",
             "interrupted": "The run was interrupted and was not resumed."}


def _consume(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.warning("controller task failed")


# --------------------------------------------------------------------------- the service


RUN_DIR = Path("/run/casa-browser/controller")
DRIVER_SOCKET = Path("/run/casa-browser/driver/driver.sock")


async def serve_forever() -> None:  # pragma: no cover — exercised by the image tests
    import signal

    from casa_browser import api, retention
    from casa_browser.broker_client import BrokerClient
    from casa_browser.browser import BrowserManager
    from casa_browser.downloads import Downloader
    from casa_browser.handles import HandleRegistry
    from casa_browser.login import DriverServer
    from casa_browser.netfloor import API_PORT
    from casa_browser.observe import Observer
    from casa_browser.options import Paths, read_locale_record
    from casa_browser.pacing import Pacer
    from casa_browser.proxy_client import ProxyClient
    from casa_browser.store import Store

    paths = Paths()
    try:
        public = json.loads(paths.public_options.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        public = {}
    locale = read_locale_record(paths)
    store = Store(paths.database)
    registry = HandleRegistry()
    browser = BrowserManager(placeholder_dir=RUN_DIR / "placeholder")
    days = public.get("retention_days", 30)
    controller = RunController(Components(
        store=store, browser=browser, proxy=ProxyClient(), broker=BrokerClient(store=store),
        driver=DriverServer(browser, DRIVER_SOCKET), observer=Observer(registry, paths.artifacts, store),
        registry=registry, downloader=Downloader(store, registry, paths.artifacts),
        pacer=Pacer, locale=locale,
        configured=bool(public.get("configured")),
        retention=lambda s, now: retention.sweep(s, now, days, paths.artifacts)))
    await controller.start()
    runner = await api.serve(api.make_app(controller), "0.0.0.0", API_PORT)
    from casa_browser import ui
    ui_runner = await ui.serve(ui.make_ui(controller))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        await runner.cleanup()
        await ui_runner.cleanup()
        await controller.shutdown()
        store.close()


LOG_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING,
              "error": logging.ERROR}


def configure_logging(public: dict) -> int:
    """The ``log_level`` option sets the level of the app's own loggers (``casa_browser.*``);
    libraries stay at warnings, and those that can carry request bytes are filtered
    (``api.quiet_library_logs``). An absent or unknown value is ``info``, the option's default."""
    level = LOG_LEVELS.get(str(public.get("log_level", "info")), logging.INFO)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("casa_browser").setLevel(level)
    from casa_browser import api
    api.quiet_library_logs()
    return level


def main() -> None:  # pragma: no cover
    from casa_browser.options import Paths
    try:
        public = json.loads(Paths().public_options.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        public = {}
    configure_logging(public if isinstance(public, dict) else {})
    asyncio.run(serve_forever())


if __name__ == "__main__":  # pragma: no cover
    main()
