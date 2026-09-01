"""In-process reload dispatcher and per-scope handlers.

Spec: docs/superpowers/specs/2026-05-02-granular-reload-design.md.

Public API:
- ``dispatch(scope, *, runtime, role=None, include_env=False) -> dict``
  is the single entry point used by both ``tools.casa_reload`` (MCP) and
  the ``/admin/reload`` route (casactl).
- ``ReloadError(kind, message)`` is raised by handlers on failure;
  ``dispatch`` catches and converts to result-shape.

Lock registry: per-scope-key ``asyncio.Lock`` keyed by
``f"{scope}:{role}"`` for role-bearing scopes, ``scope`` alone otherwise.
The ``full`` scope grabs ``"full"`` and is mutually exclusive with all
other scopes via the ``_GLOBAL_LOCK`` mechanism.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, Awaitable, Callable

import scheduled_asks

logger = logging.getLogger("reload")


class ReloadError(Exception):
    """Raised by per-scope handlers; converted to result envelope."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


# Per-scope-key lock registry. Keys are stable strings:
#   agent:<role>, triggers:<role>, policies, plugin_env, agents, full
_LOCKS: dict[str, asyncio.Lock] = {}

# Global lock — held in EXCLUSIVE mode by ``full``, in SHARED mode by all
# other scopes. Implemented as a Reader-Writer-style asyncio primitive
# below since asyncio.Lock alone is mutex-only.
_GLOBAL_RW = None  # initialized lazily — see _global_rw()


class _RWLock:
    """Minimal async reader-writer lock. Many readers OR one writer.

    Used so the ``full`` scope (writer) excludes every other scope
    (readers), but readers run concurrently for different scope-keys.

    Admission is FIFO (#311): pre-fix, ``acquire_read`` only blocked on an
    ACTIVE writer, so a steady stream of shared-scope reloads starved a
    pending ``full`` reload indefinitely. Any queued waiter now bars new
    entrants; consecutive readers at the head are admitted as a batch, so
    a writer stream cannot starve readers either. All lock state is mutated
    only synchronously on the event loop — waiters await their own Event,
    and admission is decided in ``_admit`` with no await between check and
    mutation.
    """

    def __init__(self) -> None:
        self._readers = 0
        # M21 (v0.49.0): the writer must hold visible lock state. Pre-fix
        # acquire_write recorded nothing, so readers arriving while a
        # 'full' reload was mid-flight ran concurrently with its
        # multi-step runtime mutation.
        self._writer = False
        self._queue: deque[tuple[str, asyncio.Event]] = deque()

    def _admit(self) -> None:
        while self._queue and self._queue[0][0] == "r" and not self._writer:
            self._readers += 1
            self._queue.popleft()[1].set()
        if (self._queue and self._queue[0][0] == "w"
                and not self._writer and self._readers == 0):
            self._writer = True
            self._queue.popleft()[1].set()

    async def _wait_admitted(self, entry: tuple[str, asyncio.Event]) -> None:
        try:
            await entry[1].wait()
        except asyncio.CancelledError:
            if entry[1].is_set():
                # Admitted synchronously (by _admit) before the cancellation
                # was delivered: this waiter already OWNS the lock — roll the
                # admission back or every later acquire hangs forever.
                if entry[0] == "r":
                    await self.release_read()
                else:
                    await self.release_write()
            else:
                # Still queued: withdraw, and re-run admission — this entry
                # may have been the head that blocked the one behind it.
                self._queue.remove(entry)
                self._admit()
            raise

    async def acquire_read(self) -> None:
        if not self._writer and not self._queue:
            self._readers += 1
            return
        entry = ("r", asyncio.Event())
        self._queue.append(entry)
        await self._wait_admitted(entry)

    async def release_read(self) -> None:
        self._readers -= 1
        self._admit()

    async def acquire_write(self) -> None:
        if not self._writer and self._readers == 0 and not self._queue:
            self._writer = True
            return
        entry = ("w", asyncio.Event())
        self._queue.append(entry)
        await self._wait_admitted(entry)

    async def release_write(self) -> None:
        self._writer = False
        self._admit()


def _global_rw() -> _RWLock:
    global _GLOBAL_RW
    if _GLOBAL_RW is None:
        _GLOBAL_RW = _RWLock()
    return _GLOBAL_RW


# #609 ------------------------------------------------------------------
# The webhook-secret seam. Both halves are deliberately incapable of changing
# routing or aborting a pass: a filesystem fault must not silently re-shape
# which routes exist, and neither 404 nor 401 authenticates, so unrouting a
# trigger whose mint failed buys nothing and costs a reload that had already
# applied other work.

_SECRET_REPORT_SCOPES = frozenset(
    {"triggers", "agent", "agents", "full", "policies", "config_sync"})


async def _mint_trigger_secrets(actions: list[str], role: str, specs: list) -> None:
    """Create the missing casa-owned per-trigger secrets for *specs*.

    Called AFTER registration at every install site. Never raises: a mint
    failure is an `actions` entry naming the trigger, which rides out on the
    ordinary envelope, plus the report row that says what the file's condition
    actually is.
    """
    try:
        import resident_trigger_secrets
        import trigger_reconcile
        failures = await asyncio.to_thread(
            resident_trigger_secrets.mint_for_specs,
            specs, secrets_dir=trigger_reconcile.SECRETS_DIR,
        )
    except Exception as exc:  # noqa: BLE001 — never abort a reload for this
        logger.warning("trigger secret mint failed for role=%s: %s", role, exc)
        actions.append(f"trigger_secret_mint_error_{role}")
        return
    for name, reason in failures:
        logger.warning(
            "trigger %r on role=%s has no usable secret and could not be "
            "minted (%s); requests to it will be refused", name, role, reason)
        actions.append(f"trigger_secret_mint_failed_{name}")


def _trigger_secret_snapshot(runtime: Any, role: str | None) -> Any:
    """The in-memory half of the report: rows decided with NO filesystem IO.

    Taken UNDER the reload lock, synchronously and with no ``await`` in it, so
    the declaration and the registry are read at one instant and cannot
    describe two different states of the world.

    Returns ``None`` when there is nothing to report, a list of rows, or a
    dict carrying an error — never raises.
    """
    if role is None:
        return None
    try:
        import resident_trigger_secrets
        import trigger_reconcile
        cfg = (getattr(runtime, "role_configs", None) or {}).get(role)
        if cfg is None:
            # A specialist is not in `role_configs`. `casa_reload_triggers`
            # already falls back here for its `registered` list, and the two
            # must describe the SAME role from the SAME places — otherwise
            # every one of a specialist's live webhooks reads
            # `routed_undeclared` in the very envelope that names it as
            # registered.
            registries = getattr(runtime, "specialist_registry", None)
            if registries is not None:
                cfg = (registries.all_configs() or {}).get(role)
        specs = list(getattr(cfg, "triggers", None) or [])
        rows = resident_trigger_secrets.snapshot_rows(
            specs=specs, registry=getattr(runtime, "trigger_registry", None),
            role=role,
            global_secret_usable=bool(
                getattr(runtime, "webhook_global_secret_usable", False)),
        )
        return rows
    except Exception as exc:  # noqa: BLE001 — never `except: pass`; say so
        return {"trigger_secrets_error": f"{type(exc).__name__}: {exc}"}


async def _trigger_secret_probe(snapshot: Any) -> dict:
    """The filesystem half, run AFTER both reload locks are released.

    Two reasons it may not run under the lock, and they are different. It must
    not run on the EVENT LOOP, because the condition this release exists for is
    a ``/data`` that is full, read-only or hung, and a hung one would stall the
    loop from inside the very report written to explain it. It must not run
    under the LOCKS either: moving a hang off the loop and into the reload
    lock only relocates it — a hung probe would hold the per-scope lock and the
    global RW lock, blocking a pending full-reload writer and, behind it, every
    later reader. Diagnostics must never be able to wedge reload admission.
    """
    if snapshot is None:
        return {}
    if isinstance(snapshot, dict):  # an error captured while snapshotting
        return snapshot
    try:
        import resident_trigger_secrets
        import trigger_reconcile
        rows = await asyncio.to_thread(
            resident_trigger_secrets.resolve_rows,
            snapshot, secrets_dir=trigger_reconcile.SECRETS_DIR)
        return {
            "trigger_secrets": rows,
            "trigger_secrets_summary": resident_trigger_secrets.summarize(rows),
        }
    except Exception as exc:  # noqa: BLE001 — never `except: pass`; say so
        return {"trigger_secrets_error": f"{type(exc).__name__}: {exc}"}


def _lock_key(scope: str, role: str | None) -> str:
    if scope in ("agent", "triggers"):
        return f"{scope}:{role or ''}"
    return scope


def _get_lock(key: str) -> asyncio.Lock:
    lock = _LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[key] = lock
    return lock


# Handlers registry — populated by per-scope tasks B.1..B.6.
HandlerFn = Callable[..., Awaitable[list[str]]]
_HANDLERS: dict[str, HandlerFn] = {}


def register_handler(scope: str, fn: HandlerFn) -> None:
    """Used by per-scope handler modules (tests + reload-impl tasks)."""
    _HANDLERS[scope] = fn


# Release B: reload scopes whose success can change what plugin triggers may
# route (resident channels/triggers, the agent set, or everything) — dispatch
# re-derives the plugin-trigger overlay after these succeed.
_TRIGGER_RECONCILE_SCOPES = frozenset({"triggers", "agent", "agents", "full"})


async def dispatch(
    scope: str,
    *,
    runtime: Any,
    role: str | None = None,
    include_env: bool = False,
) -> dict:
    """Single entry point. Returns a result-shape dict; never raises."""
    started_ms = time.monotonic() * 1000

    envelope: dict = {}
    secret_snapshot: Any = None

    handler = _HANDLERS.get(scope)
    if handler is None:
        return {
            "status": "error",
            "kind": "unknown_scope",
            "message": f"unknown scope: {scope!r}; valid: {sorted(_HANDLERS)}",
            "scope": scope, "role": role,
            "ms": int(time.monotonic() * 1000 - started_ms),
            "actions": [],
        }

    rw = _global_rw()
    if scope == "full":
        await rw.acquire_write()
    else:
        await rw.acquire_read()
    try:
        lock_key = _lock_key(scope, role)
        lock = _get_lock(lock_key)
        async with lock:
            try:
                actions = await handler(
                    runtime, role=role, include_env=include_env,
                ) if scope == "full" else await handler(runtime, role=role)
                # Release B: a reload that can change trigger routing inputs
                # (a resident's channels/triggers, the agent set, or the whole
                # runtime) must re-derive the plugin-trigger overlay — e.g. a
                # resident LOSING its webhook channel must unroute the plugin
                # triggers that targeted it. Failure is non-fatal: the reload
                # itself succeeded; the stale overlay heals on the next
                # reconcile (any plugin mutation / reload).
                if scope in _TRIGGER_RECONCILE_SCOPES:
                    try:
                        import trigger_reconcile
                        await trigger_reconcile.reconcile_from_runtime(runtime)
                        actions = [*actions, "plugin_triggers_reconciled"]
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "plugin-trigger reconcile after reload failed",
                            exc_info=True)
                    # Pair the callback reconcile at the SAME
                    # scopes with the SAME runtime — a resident losing/gaining a
                    # role changes callback assignment (callback_no_target) just
                    # as it changes trigger routing. Independent + non-fatal.
                    try:
                        import callback_reconcile
                        await callback_reconcile.reconcile_from_runtime(runtime)
                        actions = [*actions, "plugin_callbacks_reconciled"]
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "plugin-callback reconcile after reload failed",
                            exc_info=True)
                    # Pair the EVENT reconcile at the SAME scopes with the
                    # SAME runtime — a resident losing/gaining a role changes
                    # a subscriber's own delivery target (event_no_target)
                    # just as it changes trigger/callback routing.
                    # ``reconcile_plugin_events`` takes ``runtime`` directly
                    # (no separate ``reconcile_from_runtime`` wrapper).
                    # Independent + non-fatal.
                    try:
                        import event_reconcile
                        await event_reconcile.reconcile_plugin_events(runtime)
                        actions = [*actions, "plugin_events_reconciled"]
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "plugin-event reconcile after reload failed",
                            exc_info=True)
                # #423 r3 (Sol r2-3): ANY successful reload can change
                # setup-episode readiness — plugin_env lands secrets,
                # agent/agents/policies/full reconstruct agents (stale
                # binding → lazy-ready). One kick here covers every scope,
                # present and future, instead of per-handler arms; the
                # worker re-checks its gates, so a spurious kick is a no-op.
                # Never turns a successful reload into an error.
                try:
                    import plugin_setup_episodes
                    plugin_setup_episodes.kick()
                except Exception:  # noqa: BLE001
                    logger.exception("post-reload setup-episode kick failed")
                ms = int(time.monotonic() * 1000 - started_ms)
                logger.info(
                    "casa_reload scope=%s role=%s ms=%d ok=True actions=%s",
                    scope, role, ms, actions,
                )
                envelope = {
                    "status": "ok", "scope": scope, "role": role,
                    "ms": ms, "actions": actions,
                }
            except ReloadError as exc:
                ms = int(time.monotonic() * 1000 - started_ms)
                logger.warning(
                    "casa_reload scope=%s role=%s ms=%d ok=False kind=%s msg=%s",
                    scope, role, ms, exc.kind, exc.message,
                )
                envelope = {
                    "status": "error", "kind": exc.kind,
                    "message": exc.message, "scope": scope, "role": role,
                    "ms": ms, "actions": [],
                }
            except Exception as exc:  # noqa: BLE001 — surface as error envelope
                ms = int(time.monotonic() * 1000 - started_ms)
                logger.warning(
                    "casa_reload scope=%s role=%s ms=%d ok=False kind=unexpected msg=%s",
                    scope, role, ms, exc,
                    exc_info=True,
                )
                envelope = {
                    "status": "error", "kind": "unexpected",
                    "message": str(exc), "scope": scope, "role": role,
                    "ms": ms, "actions": [],
                }
            # #609: the IN-MEMORY half of the secret report, taken under the
            # lock on every arm so it describes the same instant the reload
            # left behind. The filesystem half runs below, after BOTH locks
            # are released — see `_trigger_secret_probe`.
            if scope in _SECRET_REPORT_SCOPES:
                secret_snapshot = _trigger_secret_snapshot(runtime, role)
    finally:
        if scope == "full":
            await rw.release_write()
        else:
            await rw.release_read()
    envelope.update(await _trigger_secret_probe(secret_snapshot))
    return envelope


# ---------------------------------------------------------------------------
# Per-scope handlers
# ---------------------------------------------------------------------------

import os
from pathlib import Path


async def _specialist_roles_dir(runtime: Any) -> str:
    """Task N1b Step 25b (Round-2, finding #1 — the P0 this whole plan exists
    to close): the ONE place every specialist-tier reload call site in this
    module gets its ``roles_dir``. Reconciles the roles overlay (+
    self-heals the legacy operational-file set — see
    ``specialist_materialize.current_specialist_roles_dir``'s own docstring)
    fresh on every call, exactly matching that function's own
    'safe to redo on EVERY call' contract.

    Deviation from the brief's own snippet (disclosed in the N1b slice-C
    report): the brief calls ``specialist_materialize.
    current_specialist_roles_dir()`` bare, relying on that function's
    hardcoded ``/config/specialists`` + ``/config/agents/specialists``
    defaults. Every handler in this module already derives its own
    filesystem roots from ``runtime.config_dir``/``runtime.agents_dir``
    (never a hardcoded ``/config``) — this helper does the same, deriving
    ``specialists_dir``/``agents_specialists_dir`` from the SAME runtime
    fields, so a reload dispatched against a non-default config_dir (every
    existing test in this suite passes a tmp_path-backed one) reconciles
    under ITS OWN tree, never attempting to write the real host /config.
    In production runtime.config_dir IS CONFIG_DIR ("/config",
    casa_core.py), so this resolves identically to the brief's bare call —
    zero behavior change there.

    Stale-index fix (Plan 2 review, no GH issue): boot (casa_core.py)
    constructs its own ``InstalledSpecialistIndex`` and publishes it via
    ``specialist_registry.set_active_installed_index`` so admin/inspection
    reads (``live_installed_specialist_slugs``/``live_collision_slugs``/
    ``get_installed_instance`` — Task-14 handlers) see live state. Every
    install/upgrade/rollback/uninstall + reload path funnels through THIS
    helper on every specialist-tier reload, but until now it only asked
    ``current_specialist_roles_dir`` to load a fresh index INTERNALLY and
    discarded it — the process-wide ``_active_index`` stayed pinned at
    boot-time state forever. Building the index HERE (mirroring
    casa_core.py's own boot sequence exactly) and handing it to
    ``current_specialist_roles_dir`` (as ``installed_index=``, so it is reused
    rather than loaded twice) with ``publish=True`` makes every boot/reload path
    refresh the global — the publish now happens IN-LOCK inside that helper
    (round 6, F2), last-wins-consistent across concurrent reload workers, rather
    than pre-lock here.

    F3 (round 3): ``current_specialist_roles_dir`` now acquires
    ``specialist_materialize.MATERIALIZE_LOCK`` and does bounded file I/O
    (index load + per-slug op-file self-heal + roles-overlay rebuild). A
    concurrent install/materialize holding that lock would otherwise stall
    ALL asyncio processing, because every specialist-tier reload call site used
    to invoke this helper SYNCHRONOUSLY on the event-loop thread. This whole
    resolve now runs in a worker thread via ``asyncio.to_thread`` — one hop,
    the index build + publish + reconcile move off the loop together — so the
    lock is only ever contended between worker threads, never against the
    loop. Callers ``await`` it (``await _specialist_roles_dir(runtime) if tier
    == 'specialist' else None`` keeps the resolve lazy for non-specialist
    tiers)."""
    import specialist_materialize
    from specialist_registry import InstalledSpecialistIndex

    def _resolve() -> str:
        specialists_dir = Path(runtime.config_dir) / "specialists"
        index = InstalledSpecialistIndex(str(specialists_dir))
        index.load()
        # Round 6, F2 (index-publication coherence): publish the index INSIDE
        # `current_specialist_roles_dir` (pass `publish=True`), NOT here pre-lock.
        # Publishing under MATERIALIZE_LOCK — right after the in-lock re-load, on
        # the SAME object whose overlay is then rebuilt — is last-wins-consistent
        # across two concurrent reload workers: the process-wide global can no
        # longer be left pinned to worker A's stale object while worker B built
        # the overlay last. (Was: a pre-lock `set_active_installed_index(index)`
        # here in each worker, which raced.) The in-lock re-load of this same
        # object also keeps the published global and the overlay reflecting the
        # exact state committed under the lock.
        return specialist_materialize.current_specialist_roles_dir(
            installed_index=index,
            specialists_dir=specialists_dir,
            agents_specialists_dir=Path(runtime.agents_dir) / "specialists",
            publish=True,
        )

    return await asyncio.to_thread(_resolve)


async def _load_agent_with_overlay_retry(runtime: Any, agent_dir: str, *,
                                         policies: Any, tier: str):
    """#331(d): ``_specialist_roles_dir`` builds the overlay under
    ``MATERIALIZE_LOCK`` but the loader consumes it AFTER the lock is
    released — a concurrent upgrade can commit a new active tuple between
    the two, so the loader compiles the NEW binding against the OLD overlay
    role artifact and fails on a checksum mismatch. Holding the lock across
    the load is not an option (``load_agent_from_dir`` itself acquires the
    non-reentrant lock via ``reconcile_resident_binding`` on resident
    loads), so: rebuild the overlay once against the post-swap state and
    retry the load. Loads are bounded read-only local work, so one retry on
    a genuine (non-transient) load error only duplicates that bounded work
    before surfacing the same error.

    Returns ``(cfg, roles_dir)``. NB the overlay is a shared, destructively
    rebuilt path, not an immutable snapshot: a concurrent mutation committed
    AFTER a successful load can rebuild it again before the caller's later
    ``specialist_registry.load(roles_dir=...)`` — that reader then sees the
    newer, internally consistent overlay (pre-existing behavior, unchanged
    here). That is convergent, not corrupt: reloads serialize per scope/role
    (INV-CFG-002), the concurrent mutation's own bundle sequencer re-runs the
    registry refresh as the last lock-holder, and the overlay rebuild always
    reflects the full committed tuple state, never a torn mix."""
    import agent_loader

    # #672 (INV-CFG-003): an ALREADY-LIVE resident is loaded with the
    # validation-only reconcile. The three in-process openers that reach this
    # helper (reload_agent, reload_triggers, the policies cascade) all refuse
    # to hot-swap a resident whose identity moved — but the loader's default
    # `binding_commit=True` had already promoted a staged `desired.yaml` to
    # `active.yaml` on disk BEFORE that guard fired, so between the reload and
    # the restart the resident served a persona nothing on disk named any
    # more, and `persona_remove` deleted it. `binding_commit=False` is the
    # replay `validate_config_repo` has used since #338 for the same reason:
    # it resolves, validates and compiles the staged candidate — so the
    # identity guards below still see its digest — and writes nothing. The
    # promotion and the activation then land together, at the restart's boot
    # reconcile. A resident that is NOT live yet (its first load through
    # reload_agent) keeps committing: a first activation must promote, and
    # `_resident_identity_changed(candidate, None)` would otherwise activate a
    # staged candidate in memory that disk never committed. Specialists never
    # commit through this path (their binding follows disk on reload), so the
    # explicit `True` is inert there.
    binding_commit = not (
        tier == "resident" and os.path.basename(agent_dir) in runtime.role_configs
    )
    roles_dir = await _specialist_roles_dir(runtime) if tier == "specialist" else None
    try:
        cfg = await asyncio.to_thread(
            agent_loader.load_agent_from_dir, agent_dir,
            policies=policies, roles_dir=roles_dir, binding_commit=binding_commit)
    except Exception:  # noqa: BLE001 — retried once for specialists, then re-raised
        if tier != "specialist":
            raise
        roles_dir = await _specialist_roles_dir(runtime)
        cfg = await asyncio.to_thread(
            agent_loader.load_agent_from_dir, agent_dir,
            policies=policies, roles_dir=roles_dir, binding_commit=binding_commit)
    return cfg, roles_dir


async def reload_triggers(runtime: Any, *, role: str | None = None) -> list[str]:
    """Soft-reload triggers for one role. Ports tools.casa_reload_triggers body
    to the runtime/dispatcher contract; full lineage in spec §3.
    """
    if not role:
        raise ReloadError("role_required", "scope='triggers' requires role")

    if runtime.trigger_registry is None:
        raise ReloadError("not_initialized", "trigger registry not wired")

    # Find the agent dir: residents at agents/<role>/, specialists at
    # agents/specialists/<role>/. Mirrors tools.casa_reload_triggers.
    base = runtime.config_dir
    agents_dir = runtime.agents_dir
    agent_dir: str | None = None
    tier: str | None = None
    for candidate, candidate_tier in (
        (os.path.join(agents_dir, role), "resident"),
        (os.path.join(agents_dir, "specialists", role), "specialist"),
    ):
        if os.path.isdir(candidate):
            agent_dir = candidate
            tier = candidate_tier
            break
    if agent_dir is None:
        raise ReloadError(
            "unknown_role", f"no agent directory for role={role!r}",
        )

    # H-3 fix carry-forward (v0.34.0): always re-load policies from disk so
    # residents with disclosure.yaml don't trip _compose_prompt's None guard.
    import policies as policies_module
    policy_lib_path = os.path.join(base, "policies", "disclosure.yaml")
    try:
        policy_lib = await asyncio.to_thread(
            policies_module.load_policies, policy_lib_path,
        )
    except Exception as exc:  # noqa: BLE001
        raise ReloadError("load_error", f"policies: {exc}") from exc

    # Task N1b Step 25b: an installed specialist's role artifact lives ONLY
    # under the reconciled roles overlay (never agent_loader.DEFAULT_ROLES_DIR)
    # — a site the brief's own reload.py snippets MISSED (they only covered
    # the specialist_registry.load call below), which would otherwise vanish
    # a component-only specialist (no image fallback) on its very first
    # `casa_reload(scope="triggers", role=<slug>)` after install.
    try:
        cfg, roles_dir = await _load_agent_with_overlay_retry(
            runtime, agent_dir, policies=policy_lib, tier=tier)
    except Exception as exc:  # noqa: BLE001
        raise ReloadError("load_error", str(exc)) from exc

    # Personality Phase A, Task 14 (round-3 review): restart-to-swap invariant.
    # The load_agent_from_dir above VALIDATED a staged persona swap without
    # committing it (#672: an already-live resident loads with
    # binding_commit=False), yielding a NEW role_checksum/binding_digest on
    # cfg while the LIVE resident still runs the OLD identity and disk still
    # names it in active.yaml. A trigger reload
    # must NEVER activate that change — only a supervised restart may. So for a
    # RESIDENT whose personality identity moved, refuse the WHOLE operation here
    # BEFORE anything mutates: no reregister_for, no cache write, no specialist
    # reload. The trigger registry, runtime.role_configs, AND the trigger
    # reconciler's view (trigger_reconcile reads role_configs[role].channels to
    # authorize plugin webhook ingress) all stay consistently OLD, so the
    # restart the swap already requires activates everything together — no mixed
    # state. (Round 2 kept the OLD cache but still reregistered the NEW triggers,
    # a half-applied design two reviewers flagged: it left webhook ingress
    # authorized by the stale cached channels while NEW triggers were live, and
    # misreported the registered trigger list.) Raising mirrors reload_agent's
    # direct-path contract; dispatch() converts this ReloadError to a structured
    # error, and reload_full does NOT compose this handler, so nothing cascades
    # through the raise. Nothing was written to the InstanceDir on this path
    # (#672): active.yaml still names the served binding and desired.yaml the
    # staged one, until the mandatory restart's boot-time reconcile commits and
    # activates them together (same note reload_agent carries).
    if role in runtime.role_configs and _resident_identity_changed(
        cfg, runtime.role_configs.get(role),
    ):
        logger.warning(
            "reload_triggers(%s): personality identity changed on disk "
            "(role_checksum or binding_digest differs) — refusing the trigger "
            "reload to avoid mixed state; restart required to activate", role,
        )
        raise ReloadError(
            "restart_required",
            f"role={role} personality identity changed; restart via "
            f"casa_restart_supervised to activate (trigger reload refused to "
            f"avoid mixed state)",
        )

    # #573: the role's trigger set is being replaced, so every question one of
    # its schedules left pending is revoked FIRST — on the event loop, because
    # `reregister_for` itself runs in a worker thread where the broker's
    # finisher cannot reach a running loop. Each revocation settles its ask:
    # the keyboard is retired and the scheduled session is told.
    scheduled_asks.revoke_role(role, "trigger_reloaded")
    try:
        await asyncio.to_thread(
            runtime.trigger_registry.reregister_for,
            role, list(cfg.triggers), list(cfg.channels),
        )
    except Exception as exc:  # noqa: BLE001
        raise ReloadError("reregister_failed", str(exc)) from exc

    # #609: mint AFTER registration, so the cross-role webhook-name collision
    # test inside `register_agent` has already refused a name this role may not
    # have. Minting first would write a casa token into another role's slot for
    # a registration that is then rejected.
    secret_actions: list[str] = []
    await _mint_trigger_secrets(secret_actions, role, list(cfg.triggers))

    # Q-1 fix (v0.35.2): refresh the runtime cache so back-compat consumers
    # (tools.casa_reload_triggers emits `registered=[...]` by reading
    # runtime.role_configs[role].triggers) see the post-reload state, not the
    # boot-time list. Mirrors the resident vs specialist branching of
    # reload_agent. A resident whose personality identity changed never reaches
    # here — it was refused above — so this unconditional write can never
    # poison the shared restart-to-swap baseline: cfg's identity always matches
    # the live baseline on this path.
    if role in runtime.role_configs:
        runtime.role_configs[role] = cfg
        # GH #356: rebind the four personality maps in the same synchronous
        # stretch as the mutation (identity is guarded fixed here, so the
        # maps cannot actually change content — but sharing the one rule
        # "every role_configs write refreshes" keeps the invariant auditable).
        runtime.refresh_personality_maps()
    else:
        try:
            await asyncio.to_thread(runtime.specialist_registry.load, roles_dir=roles_dir)
        except Exception as exc:  # noqa: BLE001
            raise ReloadError("specialist_reload_failed", str(exc)) from exc

    # G-2 hotfix carry-forward: drain pending-reload guard if any.
    try:
        from tools import _ENGAGEMENTS_PENDING_RELOAD, engagement_var
        eng = engagement_var.get(None)
        if eng is not None:
            _ENGAGEMENTS_PENDING_RELOAD.discard(eng.id)
    except Exception:  # noqa: BLE001 — best-effort
        pass

    return ["reregister_triggers", *secret_actions]


register_handler("triggers", reload_triggers)


# Background agent-pool-close tasks (F12). Held here so they aren't
# garbage-collected mid-flight (a bare fire-and-forget create_task with no
# other reference can be swept by the GC before it completes).
_AGENT_CLOSE_TASKS: set[asyncio.Task] = set()


def _invalidate_role_grants(role: str | None) -> None:
    """Purge authorization grants + cancel pending challenges for `role`
    BEFORE its replacement/removed Agent becomes dispatchable (A:§3.3/§3.4,
    r1-B8/r2-B5). A stale grant, or an approved-but-stale challenge whose
    keyboard tap would dispatch a synthetic continuation to an Agent that no
    longer exists (or now runs different code), must never survive a role's
    Agent being swapped or torn down. ``role`` is normalized (a plain reload
    role is already plain; normalize_role is a harmless no-op for it) so
    every purge/cancel call site agrees on ONE shape."""
    if not role:
        return
    from authz_grants import CHALLENGES, GRANTS, normalize_role
    r = normalize_role(role)
    GRANTS.purge_role(r)
    CHALLENGES.cancel_matching(role=r)


def _track_draining(runtime, role, old_agent):
    """Sol #4: record a swapped-out agent's plugin binding on runtime.draining
    so verify can DISCLOSE it as a consumer still on the PREVIOUS artifact while
    its in-flight turn drains (aclose waits on the turn's lock, ≤ drain timeout).
    Returns the entry (to drop on close) or None."""
    if runtime is None or role is None:
        return None
    binding = dict(getattr(old_agent, "active_plugin_binding", {}) or {})
    if not binding:
        return None
    draining = getattr(runtime, "draining", None)
    if not isinstance(draining, list):
        draining = []
        try:
            runtime.draining = draining
        except Exception:  # noqa: BLE001 — SimpleNamespace/Mock stand-ins
            return None
    entry = {"role": role, "binding": binding}
    draining.append(entry)
    return entry


def _drop_draining(runtime, entry) -> None:
    if entry is None or runtime is None:
        return
    draining = getattr(runtime, "draining", None)
    if isinstance(draining, list):
        try:
            draining.remove(entry)
        except ValueError:
            pass


def _schedule_agent_close(old_agent, *, runtime=None, role=None) -> None:
    """Background-drain a replaced/evicted Agent's SDK client pool (F12).

    Background is load-bearing: casa_reload runs as a casa-framework tool
    INSIDE a warm client's turn — a synchronous drain would deadlock on
    that turn's own entry lock. The drain task waits for in-flight turns
    (bounded by the pool's drain timeout) then disconnects.

    Sol #4: when ``runtime``+``role`` are supplied, the draining agent's plugin
    binding is tracked on ``runtime.draining`` for the duration of the drain so
    verify can disclose the still-running old turn (cleared on close).

    Tolerates non-Agent stand-ins used throughout the reload test suite:
    objects with no ``aclose`` at all (``getattr`` default). A real
    ``Agent.aclose`` is always awaitable.
    """
    aclose = getattr(old_agent, "aclose", None)
    if aclose is None:
        return
    entry = _track_draining(runtime, role, old_agent)
    try:
        coro = aclose()
    except Exception:  # noqa: BLE001 — best-effort teardown, never block reload
        logger.warning("agent aclose() raised while scheduling close", exc_info=True)
        _drop_draining(runtime, entry)
        return
    task = asyncio.create_task(coro, name="agent-pool-close")
    _AGENT_CLOSE_TASKS.add(task)

    def _done(t):
        _AGENT_CLOSE_TASKS.discard(t)
        _drop_draining(runtime, entry)

    task.add_done_callback(_done)


_PROMPT_REFRESH_TASKS: set[asyncio.Task] = set()


def _delegate_directory() -> dict[str, str] | None:
    """The live role → display name view, or ``None`` if it cannot be READ.

    An empty map is ``{}`` and a genuine diff input; an exception is ``None``
    and is not. Collapsing the two would let one failed read diff as "every
    role changed" and stampede a cold reconnect across every live agent —
    strictly worse than the stale block the refresh exists to avoid.
    """
    try:
        from tools import agent_display_names
        return agent_display_names() or {}
    except Exception:  # noqa: BLE001 — a diff input, never worth failing on
        logger.warning("delegate directory read failed", exc_info=True)
        return None


def _schedule_prompt_refresh(agent: Any, role: str) -> bool:
    """Background-drop ``agent``'s warm SDK clients so its next turn rebuilds
    its system prompt (#436). True when a refresh was actually scheduled.

    BACKGROUND is load-bearing, for the same reason as
    :func:`_schedule_agent_close`: ``casa_reload`` runs as a casa-framework
    tool INSIDE a warm client's turn, and ``SdkClientPool.invalidate_all``
    awaits each entry's turn lock. Awaiting it here would deadlock the
    reloading agent against its own in-flight turn. Scheduled, the
    invalidation completes the moment that turn ends, and the next turn cold-
    connects — resuming the same session, rendering a current ``<delegates>``
    block.

    Tolerates the non-Agent stand-ins used throughout the reload test suite:
    an object with no ``invalidate_tool_surface`` is skipped.
    """
    invalidate = getattr(agent, "invalidate_tool_surface", None)
    if invalidate is None:
        return False
    try:
        coro = invalidate()
    except Exception:  # noqa: BLE001 — best-effort refresh, never block reload
        logger.warning(
            "prompt refresh raised while scheduling for role=%s", role,
            exc_info=True,
        )
        return False
    task = asyncio.create_task(coro, name=f"delegates-prompt-refresh:{role}")
    _PROMPT_REFRESH_TASKS.add(task)
    task.add_done_callback(_PROMPT_REFRESH_TASKS.discard)
    return True


def _refresh_role_map(runtime: Any, *, context: str) -> list[str]:
    """P-6: refresh tools' delegation role map, then (#436) drop the warm SDK
    clients of every live agent whose ``<delegates>`` block would now render
    differently.

    The map alone is not enough. ``Agent._build_options`` runs only on a COLD
    pool connect; a warm client is reused without rebuilding its options, and
    a per-role reload closes only the RELOADED role's pool. Without this, an
    agent already mid-conversation would keep sending the pre-reload prompt —
    advertising a display name the refreshed ACL no longer accepts.

    Scoped by an actual diff of the directory, not by "a reload happened":
    per-role reloads are routine (a prompt tweak, a tools grant) and none of
    those change what anyone else advertises. A cold reconnect costs seconds
    and a fresh prompt-cache prefix, so only the agents that declare a role
    whose name or presence CHANGED pay it.
    """
    before = _delegate_directory()
    actions: list[str] = []
    # GH #356: reconciliation pass over the four personality maps. Every
    # role_configs mutation site already refreshes synchronously in place;
    # this catch-all keeps the maps honest for any future mutation path that
    # forgets, and runs BEFORE (independent of) the role-map try below so a
    # sync_agent_role_map failure can never skip it. Pure in-memory
    # derivation, but guarded log-don't-fail like the rest of this helper.
    try:
        runtime.refresh_personality_maps()
    except Exception as exc:  # noqa: BLE001 — log but don't fail the caller
        logger.warning("personality-map refresh failed (%s): %s", context, exc)
    try:
        from tools import sync_agent_role_map
        sync_agent_role_map(runtime)
        actions.append("refresh_role_map")
    except Exception as exc:  # noqa: BLE001 — log but don't fail the caller
        logger.warning("role-map refresh failed (%s): %s", context, exc)
        return actions

    after = _delegate_directory()
    if before is None or after is None:
        return actions
    changed = {
        r for r in set(before) | set(after) if before.get(r) != after.get(r)
    }
    if not changed:
        return actions

    refreshed = 0
    for role, agent in list(getattr(runtime, "agents", {}).items()):
        declared = {
            d.agent
            for d in (getattr(getattr(agent, "config", None), "delegates", None)
                      or [])
        }
        if declared & changed and _schedule_prompt_refresh(agent, role):
            refreshed += 1
    if refreshed:
        actions.append(f"refresh_delegates_prompt_{refreshed}_roles")
    return actions


def _construct_agent(*, cfg, runtime, agent_registry=None):
    """Factory wrapper so tests can monkeypatch construction.

    Mirrors the per-role Agent construction in casa_core.main.

    #327(c): ``agent_registry`` lets reload paths hand the Agent the
    POST-reload registry. ``Agent.__init__`` retains the registry object it
    is given (``AgentRegistry`` is immutable — a later
    ``runtime.agent_registry`` rebind can never reach a live Agent), and the
    Agent's lazy plugin resolution reads its own tier from it — so
    constructing against the pre-reload registry left a freshly installed
    specialist tier-missing its own ``specialist:<role>`` plugin assignment
    for its whole lifetime. ``None`` falls back to the runtime's current
    registry (correct wherever that registry already contains ``cfg.role``).

    G-2 v0.37.7: idempotently provision the agent-home for ``cfg.role``
    BEFORE constructing the Agent. The Agent's cwd resolves to
    ``/config/agent-home/<role>`` (agent.py:518-521);
    when the configurator creates a new specialist and calls
    ``casa_reload(scope=agent role=<new>)`` (granular per-role scope),
    the agent-home dir wasn't being created — only the scope=agents
    path provisioned it. Idempotent on existing dirs; cheap mkdir.
    """
    import agent_home
    try:
        agent_home.provision_agent_home(
            role=cfg.role,
            home_root=runtime.home_root,
            defaults_root=runtime.defaults_root,
        )
    except Exception as exc:  # noqa: BLE001 — provisioning is best-effort
        # If provisioning fails the Agent will still try to run with a
        # missing home; surface in logs but don't block construction —
        # we preserve the prior failure mode (SDK error) for visibility
        # rather than swallowing the call here.
        logger.warning(
            "provision_agent_home failed for role=%s: %s", cfg.role, exc,
        )

    from agent import Agent
    return Agent(
        config=cfg,
        session_registry=runtime.session_registry,
        mcp_registry=runtime.mcp_registry,
        channel_manager=runtime.channel_manager,
        agent_registry=(agent_registry if agent_registry is not None
                        else runtime.agent_registry),
        # H9 (v0.45.0 regression, fixed v0.49.0): reuse the boot-built
        # long-term memory. Omitting this silently downgraded every
        # reload-constructed resident to NoOpSemanticMemory. getattr with
        # None default keeps runtime stand-ins without the field working
        # (Agent maps None → NoOp).
        semantic_memory=getattr(runtime, "semantic_memory", None),
    )


def _start_bus_loop(runtime: Any, role: str) -> None:
    """Ensure ``role`` has a live bus consumer after a ``bus.register``.

    H10 (v0.49.0): boot only spawns ``run_agent_loop`` consumers for
    boot-time roles (casa_core step 13). A role added by reload used to
    get a queue + handler but no consumer, so its messages sat forever.
    ``MessageBus.start_agent_loop`` is idempotent, so calling this after
    every register is safe for existing roles (their running consumer
    is reused).
    """
    try:
        runtime.bus.start_agent_loop(role)
    except Exception as exc:  # noqa: BLE001 — never fail the swap on this
        logger.warning("start_agent_loop(%s) failed: %s", role, exc)


async def _teardown_role(runtime: Any, role: str) -> None:
    """Best-effort full deregistration of an evicted role.

    H11 (v0.49.0): the remove half of the add/remove lifecycle —
    ``bus.unregister`` cancels the role's consumer task and drops its
    queue + handler (the cancellation is awaited so no consumer
    outlives the evict), then ``reregister_for(role, [], [])`` unwinds
    the role's APScheduler jobs, webhook paths, and webhook-allowlist
    names. Pre-fix, eviction called a bus method that did not exist
    (the AttributeError was swallowed) and never touched triggers, so
    'deleted' residents kept consuming and firing as ghost agents until
    the next add-on restart.
    """
    try:
        # #343(b): the awaiting variant also cancels + drains the role's
        # in-flight dispatch tasks, so no handler work survives the evict
        # (a deleted role must not keep sending/acting after teardown
        # reports complete).
        await runtime.bus.unregister_and_wait(role)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "reload_agents: bus.unregister(%s) failed: %s", role, exc,
        )
    scheduled_asks.revoke_role(role, "role_evicted")
    try:
        await asyncio.to_thread(
            runtime.trigger_registry.reregister_for, role, [], [],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "reload_agents: trigger deregister(%s) failed: %s", role, exc,
        )


def _resident_identity_changed(new_cfg: Any, live_cfg: Any) -> bool:
    """True iff a resident's personality identity moved between the live config
    and a freshly-loaded one — i.e. its ``role_checksum`` OR ``binding_digest``
    differs (a role.yaml/doctrine edit, or a staged persona swap/reset that
    ``load_agent_from_dir``'s reconcile validated — without committing it, for
    an already-live resident (#672) — as the candidate the restart will activate).

    Personality Phase A, Task 8/Task 14: this is the ONE canonical restart-to-swap
    predicate, shared by every reload path that could otherwise hot-swap a live
    resident (reload_agent, the policies cascade, the bulk agents sweep). A
    ``None`` ``live_cfg`` (a fresh add, or a non-resident with no live entry) is
    NOT a change -> False. ``getattr`` defaults keep it inert for non-resident
    tiers and narrow test stand-ins whose cfgs carry neither field.

    ``runtime.role_configs`` mutation audit (the comparison BASELINE this
    predicate reads — every production writer must either be identity-guarded
    or provably not an activation path, else a staged identity change can be
    laundered through a poisoned baseline):
      * reload_agent's post-guard commit — guarded (raises restart_required
        first).
      * the policies-cascade swap (_reload_role_after_policies) — guarded
        (skip+warn).
      * the bulk agents-sweep ADD loop — iterates on_disk - known only (a live
        resident never enters it) + guarded as defense-in-depth.
      * reload_triggers' Q-1 cache refresh — refuses outright (raises
        restart_required BEFORE any mutation) on an identity change.
      * the bulk sweep's EVICT ``role_configs.pop(...)`` — deletion-only
        (removes the baseline entry with its agent; never installs a new
        digest), not an activation path, no guard needed.
      * boot-time construction in casa_core — no live baseline exists yet.
    Any NEW writer must be classified against this list."""
    if live_cfg is None:
        return False
    return (
        getattr(new_cfg, "role_checksum", None)
        != getattr(live_cfg, "role_checksum", None)
        or getattr(new_cfg, "binding_digest", None)
        != getattr(live_cfg, "binding_digest", None)
    )


async def reload_agent(runtime: Any, *, role: str | None = None) -> list[str]:
    """Atomic-swap reload of a single role's Agent + AgentConfig.

    Tier detection: residents at agents/<role>/, specialists at
    agents/specialists/<role>/. ``unknown_role`` if neither exists.
    """
    if not role:
        raise ReloadError("role_required", "scope='agent' requires role")

    base = runtime.config_dir
    agents_dir = runtime.agents_dir

    resident_dir = os.path.join(agents_dir, role)
    specialist_dir = os.path.join(agents_dir, "specialists", role)
    if os.path.isdir(resident_dir):
        agent_dir = resident_dir
        tier = "resident"
    elif os.path.isdir(specialist_dir):
        agent_dir = specialist_dir
        tier = "specialist"
    else:
        raise ReloadError(
            "unknown_role", f"no agent directory for role={role!r}",
        )

    import policies as policies_module
    policy_lib_path = os.path.join(base, "policies", "disclosure.yaml")
    try:
        policy_lib = await asyncio.to_thread(
            policies_module.load_policies, policy_lib_path,
        )
    except Exception as exc:  # noqa: BLE001
        raise ReloadError("load_error", f"policies: {exc}") from exc

    # Task N1b Step 25b: an installed specialist's role artifact lives ONLY
    # under the reconciled roles overlay, never agent_loader.DEFAULT_ROLES_DIR.
    try:
        new_cfg, roles_dir = await _load_agent_with_overlay_retry(
            runtime, agent_dir, policies=policy_lib, tier=tier)
    except Exception as exc:  # noqa: BLE001
        raise ReloadError("load_error", str(exc)) from exc

    actions = ["load_config"]

    # Personality Phase A, Task 8, Step 9: refuse a hot-swap across a
    # personality-identity change. A resident whose role_checksum OR
    # binding_digest moved (a role.yaml/doctrine edit, or a staged persona
    # swap/reset) is restart-to-swap, never hot-reloaded — the compiled prompt
    # bundle and session epoch are bound to that identity. Runs BEFORE
    # _construct_agent, so a rejected reload wastes no work and leaves every
    # live registry/agent untouched (read-only on this path). getattr defaults
    # keep this inert for non-resident tiers and narrow test stand-ins whose
    # cfgs carry neither field.
    if tier == "resident" and _resident_identity_changed(
        new_cfg, runtime.role_configs.get(role),
    ):
        logger.warning(
            "reload_agent(%s): personality identity changed (role_checksum or "
            "binding_digest differs) — refusing hot-swap, restart required", role,
        )
        # Note: the load_agent_from_dir() call above did NOT commit the staged
        # desired->active binding — an already-live resident is loaded with
        # binding_commit=False (#672, INV-CFG-003), so disk keeps naming the
        # served binding until the mandatory restart's own boot-time reconcile
        # promotes and activates it together; this guard leaves the live
        # in-memory agent/registries untouched, so runtime and disk agree.
        raise ReloadError(
            "restart_required",
            f"role={role} personality identity changed; restart via "
            f"casa_restart_supervised to activate",
        )

    # v0.74.1 (Sol B1, live proxy-drive finding): a DISABLED specialist must
    # not be constructed or (re)registered. Reload used to install it into
    # runtime.agents + register its bus handler, leaving it reachable via
    # /invoke — and because the AgentRegistry excludes disabled specialists,
    # its resolve tier-missed to resident:<role> and it would execute with an
    # EMPTY plugin binding. Tear down any existing instance and deregister
    # the role instead; verify reports its plugin targets state="disabled".
    if tier == "specialist" and getattr(new_cfg, "enabled", True) is False:
        # A:§3.3/§3.4 (r2-B5 enumerated seam): purge+cancel BEFORE teardown
        # proceeds — the role is about to become undispatchable entirely.
        _invalidate_role_grants(role)
        old_agent = runtime.agents.pop(role, None)
        _schedule_agent_close(old_agent, runtime=runtime, role=role)
        await _teardown_role(runtime, role)
        try:
            await asyncio.to_thread(runtime.specialist_registry.load, roles_dir=roles_dir)
        except Exception as exc:  # noqa: BLE001
            raise ReloadError("specialist_reload_failed", str(exc)) from exc
        from agent_registry import AgentRegistry
        runtime.agent_registry = AgentRegistry.build(
            residents=runtime.role_configs,
            specialists=runtime.specialist_registry.all_configs(),
        )
        actions += ["teardown_disabled_specialist", "rebuild_agent_registry"]
        actions += _refresh_role_map(runtime, context=f"role={role}")
        return actions

    # #327(c): build the construction registry as an OVERLAY — the live
    # registries stay untouched until the swap window. The new Agent must
    # retain a registry that already contains its own tier entry (see
    # _construct_agent's docstring), and the overlay provides that from
    # ``new_cfg`` directly, WITHOUT reloading the SpecialistRegistry first:
    # Terra r4-2 — a pre-construct load meant a construction failure left
    # runtime consumers observing the new specialist configuration while
    # the surviving old agent executed under the old one; on this ordering
    # a construction failure mutates nothing, exactly like main.
    from agent_registry import AgentRegistry
    residents_view = dict(runtime.role_configs)
    specialists_view = dict(runtime.specialist_registry.all_configs())
    if tier == "resident":
        residents_view[role] = new_cfg
    else:
        specialists_view[role] = new_cfg
    fresh_registry = AgentRegistry.build(
        residents=residents_view,
        specialists=specialists_view,
    )

    # Construct new Agent instance OUTSIDE the swap window. Failure leaves
    # every live registry and agent untouched.
    try:
        new_agent = await asyncio.to_thread(
            _construct_agent, cfg=new_cfg, runtime=runtime,
            agent_registry=fresh_registry,
        )
    except Exception as exc:  # noqa: BLE001
        raise ReloadError("construct_failed", str(exc)) from exc
    actions.append("construct_agent")

    # --- ATOMIC SWAP WINDOW ---
    # SpecialistRegistry refresh first (mirrors main's ordering): a load
    # failure here raises with the old agent still live and nothing else
    # mutated.
    if tier == "specialist":
        try:
            await asyncio.to_thread(
                runtime.specialist_registry.load, roles_dir=roles_dir)
        except Exception as exc:  # noqa: BLE001
            raise ReloadError("specialist_reload_failed", str(exc)) from exc
    old_agent = runtime.agents.get(role)  # AR-7: capture before overwrite
    # A:§3.3/§3.4 (r2-B5 enumerated seam): purge+cancel BEFORE the
    # replacement agent becomes dispatchable.
    _invalidate_role_grants(role)
    if tier == "resident":
        runtime.role_configs[role] = new_cfg
        # GH #356: rebind the personality maps synchronously with the swap —
        # `casactl persona inspect/render/diff` must describe the config the
        # now-live agent runs, not the pre-reload one, with no await between.
        runtime.refresh_personality_maps()
    runtime.agents[role] = new_agent
    # Publish the SAME overlay registry the agent was constructed with
    # (Sol r5-1): rebuilding from post-load state here could publish a
    # different version of this very role — the file can change on disk
    # between load_agent_from_dir and the swap-window rescan — leaving the
    # published registry describing config the live agent does not run.
    # The overlay reflects exactly what this reload activated; disk changes
    # that landed mid-reload (this role's or any other's) activate on their
    # own reload, with specialist_registry already holding disk truth.
    runtime.agent_registry = fresh_registry
    runtime.bus.register(role, new_agent.handle_message)
    # H10: a role whose dir was created after boot has no consumer yet;
    # idempotent no-op for roles that already have one.
    _start_bus_loop(runtime, role)
    actions.append("reregister_bus")

    # F12: drain/close the replaced Agent's SDK client pool in the
    # background so no warm subprocess outlives this swap. Sol #4: track its
    # binding on runtime.draining so verify discloses the still-draining turn.
    _schedule_agent_close(old_agent, runtime=runtime, role=role)
    actions.append("rebuild_agent_registry")

    # P-6: refresh tools' delegation role map. It is a boot-time snapshot;
    # without this, delegate_to_agent keeps resolving the PRE-reload
    # AgentConfig (stale tools.allowed etc.) for every fresh delegation.
    # #436: and refresh the prompts of whoever advertises this role.
    actions += _refresh_role_map(runtime, context=f"role={role}")

    # Re-register triggers for that role only.
    scheduled_asks.revoke_role(role, "trigger_reloaded")
    try:
        await asyncio.to_thread(
            runtime.trigger_registry.reregister_for,
            role, list(new_cfg.triggers), list(new_cfg.channels),
        )
        actions.append("reregister_triggers")
    except Exception as exc:  # noqa: BLE001
        # #327(b): surface the failure — pre-fix this logged and returned
        # ok, silently leaving the role with no triggers (reregister_for
        # unwinds first; #307 makes the no-trigger state actually hold).
        # The swap above already landed and stays — the error is about the
        # trigger state, and the message says exactly that.
        logger.warning("trigger reregister failed for role=%s: %s", role, exc)
        raise ReloadError(
            "reregister_failed",
            f"agent swap for role={role} applied, but trigger "
            f"re-registration failed; the role is left with NO active "
            f"triggers unless the error below names job(s) that remain "
            f"live: {exc}",
        ) from exc

    await _mint_trigger_secrets(actions, role, list(new_cfg.triggers))

    # Drain pending-reload guard if any.
    try:
        from tools import _ENGAGEMENTS_PENDING_RELOAD, engagement_var
        eng = engagement_var.get(None)
        if eng is not None:
            _ENGAGEMENTS_PENDING_RELOAD.discard(eng.id)
    except Exception:  # noqa: BLE001
        pass

    # #423 r2: a setup episode held on "waiting for target agent reload"
    # becomes dispatchable now — the wake arrives via the trigger
    # re-registration above (trigger_reconcile kicks the setup worker on
    # every reconcile), pinned by
    # tests/test_reload.py::test_agent_reload_kicks_setup_episode_worker.

    return actions


register_handler("agent", reload_agent)


async def _reload_role_after_policies(runtime: Any, role: str) -> None:
    """Re-load one role's AgentConfig + Agent with the new policy_lib.

    Used by reload_policies — does the agent-scope work without holding
    the agent-scope lock (caller already holds the policies lock; agent
    re-loads here are sequential).
    """
    # Determine tier
    base = runtime.config_dir
    agents_dir = runtime.agents_dir
    resident_dir = os.path.join(agents_dir, role)
    specialist_dir = os.path.join(agents_dir, "specialists", role)
    if os.path.isdir(resident_dir):
        agent_dir = resident_dir
        tier = "resident"
    elif os.path.isdir(specialist_dir):
        agent_dir = specialist_dir
        tier = "specialist"
    else:
        return  # role disappeared between scan and re-load — silently skip

    # Task N1b Step 25b: same roles_dir threading as reload_agent above.
    new_cfg, _roles_dir = await _load_agent_with_overlay_retry(
        runtime, agent_dir, policies=runtime.policy_lib, tier=tier)

    # Personality Phase A, Task 14 (whole-branch review): restart-to-swap must
    # hold on the POLICY cascade too. A resident whose role_checksum OR
    # binding_digest moved (a doctrine edit, or a staged persona swap/reset that
    # the load above validated WITHOUT committing — #672) is restart-to-swap —
    # never hot-reloaded. Unlike reload_agent's single-role path we do NOT raise
    # (that would abort the whole cascade for every OTHER role); we SKIP just
    # this role, leaving its LIVE agent + cfg + registries untouched, so its
    # deferred policy change lands only on the mandatory supervised restart,
    # whose boot-time reconcile is what commits desired->active on disk. Every
    # identity-UNCHANGED role still reloads below to pick up the new policy_lib.
    if tier == "resident" and _resident_identity_changed(
        new_cfg, runtime.role_configs.get(role),
    ):
        logger.warning(
            "policies cascade: role=%s personality identity changed "
            "(role_checksum or binding_digest differs) — skipping hot-swap; the "
            "policy change activates on a supervised restart", role,
        )
        return

    new_agent = await asyncio.to_thread(
        _construct_agent, cfg=new_cfg, runtime=runtime,
    )
    old_agent = runtime.agents.get(role)  # AR-7: capture before overwrite
    # A:§3.3/§3.4 (r2-B5 enumerated seam): purge+cancel BEFORE the
    # replacement agent becomes dispatchable.
    _invalidate_role_grants(role)
    if tier == "resident":
        runtime.role_configs[role] = new_cfg
        # GH #356: rebind the personality maps before any await, so an admin
        # inspect/render racing this cascade never sees the maps describe a
        # config the just-swapped agent does not run.
        runtime.refresh_personality_maps()
    runtime.agents[role] = new_agent
    runtime.bus.register(role, new_agent.handle_message)
    _start_bus_loop(runtime, role)
    # F12: drain/close the replaced Agent's SDK client pool in the
    # background so no warm subprocess outlives this swap.
    _schedule_agent_close(old_agent)


async def reload_policies(runtime: Any, *, role: str | None = None) -> list[str]:
    """Reload policies/disclosure.yaml; cascade to per-role AgentConfig
    rebuild so agents pick up the new policy_lib.
    """
    base = runtime.config_dir
    actions: list[str] = []

    import policies as policies_module
    policy_lib_path = os.path.join(base, "policies", "disclosure.yaml")
    try:
        new_policy_lib = await asyncio.to_thread(
            policies_module.load_policies, policy_lib_path,
        )
    except Exception as exc:  # noqa: BLE001
        raise ReloadError("load_error", f"policies: {exc}") from exc

    # Stage swaps in locals; commit to runtime atomically.
    runtime.policy_lib = new_policy_lib
    actions += ["reload_policy_lib"]

    # Cascade: re-load each role's Agent so new policy_lib propagates.
    role_list = list(runtime.role_configs.keys()) + list(
        runtime.specialist_registry.all_configs().keys()
    )
    for r in role_list:
        try:
            # #327(d): serialize each role's swap with that role's
            # agent-scope lock. `agent:<role>` and `policies` are
            # independent lock keys, so an in-flight scope=agent reload
            # could otherwise suspend mid-construction and install its
            # stale-policy agent AFTER this cascade rebuilt the same role
            # — both reporting success. Lock order is one-directional
            # (policies -> agent:<r>; the agent handler takes no other
            # scope lock), so this cannot deadlock.
            async with _get_lock(_lock_key("agent", r)):
                await _reload_role_after_policies(runtime, r)
        except Exception as exc:  # noqa: BLE001 — one role's failure shouldn't kill the rest
            logger.warning("policies cascade: role=%s failed: %s", r, exc)
    actions.append(f"cascaded_to_{len(role_list)}_roles")

    # #436: the cascade commits a fresh AgentConfig for every role it swaps
    # (`runtime.role_configs[role] = new_cfg`), so a display name can change
    # here with no `agent`/`agents` reload in sight. This scope never
    # refreshed the delegation role map, which left the ACL and every
    # rendered block on the pre-cascade names indefinitely. `config_sync`
    # inherits the same hole: it cascades `agents` FIRST and `policies`
    # second, so the sweep's own refresh runs before these swaps land.
    actions += _refresh_role_map(runtime, context="policies cascade")

    return actions


register_handler("policies", reload_policies)


# Snapshot of last-applied plugin-env keys, used to detect deletions.
_PLUGIN_ENV_LAST_KEYS: set[str] = set()


def note_boot_plugin_env(keys: set[str]) -> None:
    """Seed the last-applied plugin-env key snapshot from the boot path.

    M22 (v0.49.0): casa_core.main step 1b sources plugin-env.conf into
    os.environ directly. Without this seed the snapshot starts empty, so
    the FIRST ``casa_reload(scope='plugin_env')`` computes
    ``dropped = {} - new_keys`` and can never remove a key that was
    applied at boot but has since been deleted from plugin-env.conf —
    a revoked plugin secret survived in the process env (and kept being
    inherited by plugin MCP subprocesses) for the container's lifetime.
    Only the boot path may call this: it alone knows which env vars came
    from plugin-env.conf rather than the ambient environment.
    """
    global _PLUGIN_ENV_LAST_KEYS
    _PLUGIN_ENV_LAST_KEYS = set(keys)


async def reload_plugin_env(runtime: Any, *, role: str | None = None) -> list[str]:
    """Re-source plugin-env.conf into os.environ.

    Resolves op:// references via secrets_resolver. Computes the diff
    against the last-applied key set and pops any that are now absent.
    """
    global _PLUGIN_ENV_LAST_KEYS
    import plugin_env_conf
    import secrets_resolver
    from secrets_resolver import resolve as resolve_secret

    # #345: a reload is the rotation path — drop the resolver's plaintext cache
    # so op:// references are re-read from 1Password instead of returning the
    # possibly-revoked cached value (which made rotation a silent no-op).
    secrets_resolver.invalidate_cache()

    try:
        entries = await asyncio.to_thread(plugin_env_conf.read_entries)
    except Exception as exc:  # noqa: BLE001
        raise ReloadError("read_error", f"plugin-env.conf: {exc}") from exc

    new_keys: set[str] = set(entries.keys())
    actions: list[str] = []

    unresolved = 0
    for var, raw in entries.items():
        try:
            resolved = await asyncio.to_thread(resolve_secret, raw)
        except RuntimeError as exc:
            # #580: a reference casa could not resolve is not a value, and
            # installing the literal is worse than absence in BOTH directions.
            # A bare `${VAR}` reference is withheld either way (an op:// value
            # already counts as unresolved), but a `${VAR:-default}` reference
            # is invisible to the withhold gate BY DESIGN — its default is
            # supposed to cover it. Measured on the pinned CLI: only an UNSET
            # variable takes that default, so a set-but-meaningless value made
            # the plugin's MCP server launch holding `op://vault/item/field`
            # where a credential belongs — the placeholder-credential failure
            # INV-PLUG-008 exists to prevent, arriving through the one form
            # that gate deliberately does not police.
            #
            # This is also what the boot path has always done (casa_core step
            # 1b assigns INSIDE its try, so a failure leaves the variable
            # unset); the line this replaces claimed that parity in a comment
            # while doing the opposite.
            logger.warning(
                "plugin-env: %s op:// resolution failed: %s — leaving it "
                "unset (a plugin requiring it is withheld; a defaulted "
                "reference takes its default)", var, exc)
            os.environ.pop(var, None)
            unresolved += 1
            continue
        os.environ[var] = resolved
    actions.append(f"set_{len(entries) - unresolved}_vars")
    if unresolved:
        actions.append(f"unresolved_{unresolved}_vars")

    # Drop keys present last time but absent now.
    dropped = _PLUGIN_ENV_LAST_KEYS - new_keys
    for var in dropped:
        os.environ.pop(var, None)
    if dropped:
        actions.append(f"dropped_{len(dropped)}_vars")

    _PLUGIN_ENV_LAST_KEYS = new_keys

    # #423: the env just changed — a setup episode held pending on "waiting
    # for plugin secrets" can now dispatch. The wake is the dispatch-level
    # post-reload kick (r3, Sol r2-3: EVERY successful scope kicks, since
    # agent-reconstructing scopes change readiness too), pinned by
    # tests/test_reload.py::test_kicks_setup_episode_worker.

    # P4b (2026-07-18 self-containment plan): regenerate plugin health from
    # the NEW effective environment. Without this, a secrets-only repair
    # (set_plugin_env_reference + this reload) could never clear a stale-red
    # plugin-health.json — health regeneration only ran on §3.9 registry
    # mutations. Env refresh stays the primary contract: a health failure
    # logs and is dropped, never turning a successful reload into an error.
    try:
        import tools as tools_mod
        # Sol r4-2: serialize with §3.9 registry mutations — both paths do
        # regenerate → notify → mark_notified against shared state; unlocked
        # interleaving allows stale-red last-writer-wins and a mark_notified
        # race that suppresses a later genuine notification. #489: this is
        # the GUARD, not the raw lock — a full-scope reload with include_env
        # reaches here while its entry point already holds the lock (the
        # arm the r4-2 comment's "no mutation dispatches scope=plugin_env"
        # argument missed), and the raw re-acquire self-deadlocked the
        # reload and everything behind the reload writer lock.
        # #706: on every DISPATCHED path this acquisition is now a same-task
        # re-entrant no-op — the entry point owns the guard before dispatch
        # takes the RW lock (tools._plugin_tools_reload_guard). It is kept for
        # the DIRECT callers, where it is the only thing serializing this
        # regen+notify against a §3.9 registry mutation; taking it here while
        # the RW lock is held is what INV-CFG-011 forbids, and no dispatched
        # path does that any more.
        async with tools_mod._plugin_tools_guard():
            await asyncio.to_thread(tools_mod._regenerate_plugin_health, [])
            await tools_mod._notify_plugin_health_if_possible()
        actions.append("plugin_health_regenerated")
    except Exception:  # noqa: BLE001
        logger.warning("plugin_env reload: health regeneration failed",
                       exc_info=True)
    return actions


register_handler("plugin_env", reload_plugin_env)


async def reload_agents(runtime: Any, *, role: str | None = None) -> list[str]:
    """Scan agents/ for new/deleted residents + agents/specialists/ for
    new/deleted specialists. Add or evict accordingly.
    """
    actions: list[str] = []
    base = runtime.config_dir
    agents_dir = runtime.agents_dir

    import policies as policies_module
    policy_lib_path = os.path.join(base, "policies", "disclosure.yaml")
    try:
        policy_lib = await asyncio.to_thread(
            policies_module.load_policies, policy_lib_path,
        )
    except Exception as exc:  # noqa: BLE001
        raise ReloadError("load_error", f"policies: {exc}") from exc

    import agent_loader
    import agent_home

    # ---- Residents ----
    on_disk_residents = set()
    if os.path.isdir(agents_dir):
        for ent in os.scandir(agents_dir):
            if ent.is_dir() and ent.name not in (
                "specialists", "executors",
            ):
                on_disk_residents.add(ent.name)

    known_residents = set(runtime.role_configs.keys())

    # Add new residents
    for r in on_disk_residents - known_residents:
        try:
            new_cfg = await asyncio.to_thread(
                agent_loader.load_agent_from_dir,
                os.path.join(agents_dir, r),
                policies=policy_lib,
            )
            # Personality Phase A, Task 14 (whole-branch review): enforce
            # restart-to-swap on the bulk sweep with the ONE canonical
            # predicate. This loop only adds genuinely-new residents (r is not
            # in role_configs), so ``live_cfg`` is None and the predicate is
            # False — a fresh add's first activation is legitimate. An identity
            # change on an ALREADY-LIVE resident is never reconstructed here (a
            # known resident is not in this add set) and stays restart-to-swap;
            # sharing the predicate guarantees a live resident can never be
            # hot-swapped onto a new binding via scope=agents even if this path
            # is later broadened to refresh existing residents.
            if _resident_identity_changed(new_cfg, runtime.role_configs.get(r)):
                logger.warning(
                    "reload_agents: role=%s personality identity changed "
                    "(role_checksum or binding_digest differs) — leaving the "
                    "live agent in place; restart required to activate", r,
                )
                continue
            await asyncio.to_thread(
                agent_home.provision_agent_home,
                role=r,
                home_root=runtime.home_root,
                defaults_root=runtime.defaults_root,
            )
            new_agent = await asyncio.to_thread(
                _construct_agent, cfg=new_cfg, runtime=runtime,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("reload_agents: failed to add %s: %s", r, exc)
            continue
        # A:§3.3/§3.4 (r2-B5 enumerated seam): purge+cancel BEFORE the
        # (re)constructed agent becomes dispatchable.
        _invalidate_role_grants(r)
        runtime.role_configs[r] = new_cfg
        # GH #356: refresh in the same synchronous stretch as the add — the
        # end-of-sweep reconciliation below (line ~1560) is too late: trigger
        # registration awaits in between, and an admin render in that window
        # would 404 for a resident that is already dispatchable.
        runtime.refresh_personality_maps()
        runtime.agents[r] = new_agent
        runtime.bus.register(r, new_agent.handle_message)
        # H10: without a consumer the new resident's queue is write-only
        # until the next add-on restart.
        _start_bus_loop(runtime, r)
        actions.append(f"added_{r}")
        # #327(a): wire the added resident's declared triggers — boot does
        # this for every resident (casa_core step 13b) and the eviction
        # path unwinds them, but this add path used to skip registration
        # entirely, so a resident added via scope=agents accepted messages
        # while its cron/interval/webhook triggers never fired until a
        # later per-role reload or restart. A trigger failure is per-role
        # contained (the sweep survives; the add stands) but must be
        # visible in the action trail.
        if getattr(new_cfg, "triggers", None):
            scheduled_asks.revoke_role(r, "trigger_reloaded")
            try:
                await asyncio.to_thread(
                    runtime.trigger_registry.reregister_for,
                    r, list(new_cfg.triggers), list(new_cfg.channels),
                )
                actions.append(f"registered_triggers_{r}")
                await _mint_trigger_secrets(actions, r, list(new_cfg.triggers))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "reload_agents: trigger register failed for added "
                    "role=%s: %s", r, exc,
                )
                actions.append(f"trigger_register_failed_{r}")

    # Evict deleted residents — H11: full lifecycle teardown (cancel
    # consumer, drop queue/handler, unwind triggers), mirroring the add
    # path's register + start.
    for r in known_residents - on_disk_residents:
        # A:§3.3/§3.4 (r2-B5 enumerated seam): purge+cancel BEFORE teardown —
        # the role is about to become undispatchable entirely.
        _invalidate_role_grants(r)
        # Deletion-only baseline write: eviction, not activation — see the
        # role_configs mutation audit on _resident_identity_changed.
        runtime.role_configs.pop(r, None)
        # GH #356: drop the evicted role from the personality maps before the
        # teardown awaits — an evicted resident must not stay inspectable.
        runtime.refresh_personality_maps()
        old_agent = runtime.agents.pop(r, None)  # AR-7: capture before drop
        _schedule_agent_close(old_agent)  # F12
        await _teardown_role(runtime, r)
        actions.append(f"evicted_{r}")

    # ---- Specialists ----
    specialists_dir = os.path.join(agents_dir, "specialists")
    on_disk_specialists = set()
    if os.path.isdir(specialists_dir):
        for ent in os.scandir(specialists_dir):
            if ent.is_dir():
                on_disk_specialists.add(ent.name)

    # Defer to specialist_registry's own re-scan, then diff. S-3 (block-S
    # live finding 2026-07-15, N150 07:49:56Z): the add/evict REPORT is a
    # before/after diff of the REGISTRY, not of runtime.agents — boot never
    # puts specialists into runtime.agents (they are direct-loaded), so the
    # first agents reload after boot used to mis-report every boot-loaded
    # specialist as `added_specialist_<role>`. The runtime.agents backfill
    # below still runs for registry-known specialists missing an Agent
    # object (plugin-verify grades specialists through runtime.agents) —
    # it just no longer drives the report.
    known_specialists_before = set(
        runtime.specialist_registry.all_configs().keys())
    try:
        roles_dir = await _specialist_roles_dir(runtime)
        await asyncio.to_thread(
            runtime.specialist_registry.load,
            roles_dir=roles_dir,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("specialist_registry.load failed: %s", exc)

    # O-2b (v0.37.9): surface per-specialist load failures so casactl
    # callers see them. The registry's load() catches per-dir LoadError
    # internally to keep siblings loading; without surfacing here a
    # malformed new specialist would return ok=True with no trace in
    # the action trail.
    try:
        for name, err in runtime.specialist_registry.load_failures():
            actions.append(f"failed:{name}:{err}")
    except AttributeError:
        # Pre-v0.37.9 registry mock without load_failures(); legacy path.
        pass

    known_specialists_after = set(
        runtime.specialist_registry.all_configs().keys())

    # S-3: the report comes from the registry before/after diff exclusively.
    # An added specialist is delegatable the moment the registry re-scan
    # picked it up (direct-load), independent of whether the Agent-object
    # backfill below succeeds — so the diff, not the backfill, is the truth.
    for s in sorted(known_specialists_after - known_specialists_before):
        actions.append(f"added_specialist_{s}")
    evicted_from_registry = known_specialists_before - known_specialists_after
    for s in sorted(evicted_from_registry):
        actions.append(f"evicted_specialist_{s}")

    # #327(c): rebuild the AgentRegistry from the freshly-rescanned state
    # BEFORE the backfill constructions below — the Agent retains the
    # registry object it is constructed with (see _construct_agent), so
    # building it only at the end of the sweep left every backfilled
    # specialist tier-missing its own specialist:<role> plugin assignment.
    # Inputs (role_configs + specialist configs) are final at this point:
    # resident adds/evicts and the registry re-scan all happened above; the
    # remaining loops mutate runtime.agents only.
    from agent_registry import AgentRegistry
    runtime.agent_registry = AgentRegistry.build(
        residents=runtime.role_configs,
        specialists=runtime.specialist_registry.all_configs(),
    )
    actions.append("rebuild_agent_registry")

    # Registry-known specialists missing an Agent object need agent-home +
    # Agent construction (boot direct-loads them without one); eviction is
    # handled by the registry's own load() (tombstone-tracked). Reporting
    # already happened above — this loop is state backfill only.
    for s in on_disk_specialists - set(runtime.agents.keys()):
        cfg = runtime.specialist_registry.all_configs().get(s)
        if cfg is None:
            continue
        try:
            await asyncio.to_thread(
                agent_home.provision_agent_home,
                role=s,
                home_root=runtime.home_root,
                defaults_root=runtime.defaults_root,
            )
            new_agent = await asyncio.to_thread(
                _construct_agent, cfg=cfg, runtime=runtime,
                agent_registry=runtime.agent_registry,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("reload_agents: failed to add specialist %s: %s", s, exc)
            continue
        # A:§3.3/§3.4 (r2-B5 enumerated seam): purge+cancel BEFORE the
        # (re)constructed specialist becomes dispatchable.
        _invalidate_role_grants(s)
        runtime.agents[s] = new_agent
        runtime.bus.register(s, new_agent.handle_message)
        _start_bus_loop(runtime, s)

    # Evict missing specialists from runtime.agents (registry already
    # forgot them via its own load()).
    for s in (set(runtime.agents.keys()) & known_residents) - on_disk_residents:
        # No-op — handled in resident block above.
        pass
    for s in set(runtime.agents.keys()) - on_disk_residents - on_disk_specialists:
        # A:§3.3/§3.4 (r2-B5 enumerated seam): purge+cancel BEFORE teardown —
        # the role is about to become undispatchable entirely.
        _invalidate_role_grants(s)
        old_agent = runtime.agents.pop(s, None)  # AR-7: capture before drop
        _schedule_agent_close(old_agent)  # F12
        await _teardown_role(runtime, s)
        # S-3: the registry diff above already reported registry-known
        # evictions; only a runtime.agents entry the diff did NOT cover
        # still needs surfacing here (a leaked entry, or the second step of
        # a disabled-then-deleted specialist: the registry entry went in an
        # earlier reload, the backfilled runtime Agent only now — each run
        # reports the layer it actually tore down).
        if s not in evicted_from_registry:
            actions.append(f"evicted_specialist_{s}")

    # (agent_registry rebuild happens ABOVE, before the backfill loop —
    # #327(c); the eviction loops here mutate runtime.agents only, which is
    # not a registry input.)

    # P-6: refresh tools' delegation role map (adds + evictions included) —
    # same rationale as the reload_agent hook.
    actions += _refresh_role_map(runtime, context="agents sweep")

    return actions


register_handler("agents", reload_agents)


async def reload_executors(
    runtime: Any, *, role: str | None = None,
) -> list[str]:
    """v0.37.1 A-1: re-scan executors/ and rebuild ExecutorRegistry.

    Picks up adds, deletes, enabled-flag flips, permission_mode
    changes, allowed_tools edits, prompt path changes — anything
    that lives in the executor's `definition.yaml` or its sibling
    files. ~30ms in steady state.

    Closes the v0.35.0+ contract gap where executor lifecycle
    changes required a Supervisor restart.

    O-2a (v0.37.9): residents cache their `<executors>` system-prompt
    block (rendered from ``self.config.executors`` at construct_agent
    time). Re-running the registry load alone leaves residents holding
    stale prompts until the next agent-scope reload. Fan out to
    ``reload_agent`` per resident so the cached state regenerates.
    Specialists are NOT in the fan-out — they don't see executors.
    Per-resident sub-actions are surfaced with prefix ``agent:<role>:``
    so casactl output makes the cascade visible.
    """
    try:
        await asyncio.to_thread(runtime.executor_registry.load)
    except Exception as exc:  # noqa: BLE001
        raise ReloadError("load_error", f"executors: {exc}") from exc
    actions: list[str] = ["rebuild_executor_registry"]

    # #340: the /hooks/resolve handlers captured the executor hook-policy map
    # instance at boot (runtime.executor_cc_policies). Rebuild it from the
    # freshly loaded registry and swap the CONTENTS in place, so a new/edited
    # executor's parameters take effect and a tightened policy stops enforcing
    # its stale broader callbacks. Build-then-swap: a builder failure keeps
    # the old map intact (stale but consistent) rather than half-clearing it.
    shared_map = getattr(runtime, "executor_cc_policies", None)
    if shared_map is not None:
        try:
            from casa_core import _build_executor_cc_hook_policies
            registry = runtime.executor_registry
            fresh = await asyncio.to_thread(
                _build_executor_cc_hook_policies, registry)
            # Sol r1-1: an executor ABSENT from the fresh map is either
            # genuinely removed (drop its entry) or a load/build FAILURE —
            # and a failed executor's live engagements must keep their old
            # (possibly tighter) callbacks, not fall back to the broader
            # defaults (commit_size_guard default 20 vs a configured 5).
            # Failure = the registry reported the type failed, or its loaded
            # definition says the builder should have produced an entry
            # (claude_code + hooks_path) but none arrived (builder skip).
            failed = set(getattr(registry, "failed_types", set()) or set())
            # #442 r2 (Sol P1): the builder now installs a marked deny-all map
            # for an executor that failed to load or build, so such a type IS
            # present in ``fresh`` — which would skip the preservation below
            # and replace a KNOWN-GOOD pre-reload set with deny-all, taking
            # live engagements down. Deny-all is the right answer only when
            # there is nothing better; a pre-reload set is something better.
            from hooks import DenyAllPolicyMap
            for t, entry in list(shared_map.items()):
                if t in fresh and not isinstance(fresh[t], DenyAllPolicyMap):
                    continue
                defn = registry.definition_any(t)
                build_expected = (
                    defn is not None
                    and getattr(defn, "driver", "") == "claude_code"
                    and getattr(defn, "hooks_path", None)
                )
                if t in failed or build_expected:
                    fresh[t] = entry
                    logger.warning(
                        "executors reload: executor %r failed to reload its "
                        "hook policies — keeping the pre-reload callbacks "
                        "for its live engagements", t,
                    )
                    actions.append(f"executor_hook_policies_kept_stale:{t}")
            shared_map.clear()
            shared_map.update(fresh)
            actions.append("rebuild_executor_hook_policies")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "executors reload: hook-policy map rebuild failed — HTTP "
                "hook enforcement keeps the pre-reload policies: %s", exc,
            )
            actions.append(f"rebuild_executor_hook_policies_failed:{exc}")

    for r in list(runtime.role_configs.keys()):
        try:
            # #327(d): same serialization as the policies cascade — this
            # fan-out swaps each role's agent outside the dispatcher's
            # agent:<role> lock, racing a concurrent scope=agent reload.
            async with _get_lock(_lock_key("agent", r)):
                sub = await _HANDLERS["agent"](runtime, role=r)
            actions += [f"agent:{r}:{a}" for a in sub]
        except ReloadError as exc:
            actions.append(f"agent:{r}:failed:{exc.kind}:{exc.message}")
        except Exception as exc:  # noqa: BLE001
            actions.append(f"agent:{r}:failed:{exc}")

    # v0.71.1 (Sol Task-5): an executor enable/disable flip changes plugin
    # authorization (a disabled executor is dormant; enabling it makes its
    # grant checks real). Refresh plugin-health from the rebuilt registry so
    # enabling an executor whose assigned plugin lacks a grant surfaces
    # authorization_missing + a DM now, instead of leaving the report
    # stale-green until an unrelated regeneration trigger. Never fail the
    # reload on the refresh.
    try:
        import tools as tools_mod
        # #582 batch (Sol design r1): serialized with §3.9 registry mutations
        # exactly as the plugin_env scope already is — the report lock orders
        # the WRITE, not the computation before it, so an unguarded pass can
        # write a pre-mutation result last and delete the row that mutation
        # just added. The GUARD, not the raw lock: this handler is reachable
        # from a `full` cascade whose entry point already holds it.
        # #706: and now from EVERY dispatched path, because the executors scope
        # is fenced at the entry too (tools._plugin_tools_reload_guard) — this
        # acquisition is a same-task re-entrant no-op there. Kept for the direct
        # callers, which it still serializes; INV-CFG-011.
        async with tools_mod._plugin_tools_guard():
            await asyncio.to_thread(tools_mod._regenerate_plugin_health, [])
            await tools_mod._notify_plugin_health_if_possible()
        actions.append("plugin_health_regenerated")
    except Exception as exc:  # noqa: BLE001
        logger.debug("executors reload: plugin-health regen skipped: %s", exc)

    return actions


register_handler("executors", reload_executors)


async def reload_config_sync(runtime: Any, *, role: str | None = None) -> list[str]:
    """Re-run the default-sync reconciler live (same entry as boot), then
    cascade agents + policies reloads so synced files take effect without a
    container restart. Spec: 2026-06-08-config-sync-reconciler-design.md §3.1.
    """
    import config_sync

    config_dir = runtime.config_dir
    defaults_dir = getattr(runtime, "defaults_dir", "/opt/casa/defaults")
    data_dir = getattr(runtime, "data_dir", "/data")
    image_version = getattr(runtime, "image_version", "unknown")

    actions: list[str] = []
    rc = await asyncio.to_thread(
        config_sync.run,
        defaults_dir=defaults_dir,
        config_dir=config_dir,
        baseline_dir=os.path.join(data_dir, "config-baseline"),
        report_path=os.path.join(data_dir, "config-sync-report.json"),
        image_version=image_version,
    )
    actions.append(f"reconcile_rc={rc}")

    # Cascade so live runtime picks up any synced changes.
    for scope in ("agents", "policies"):
        handler = _HANDLERS.get(scope)
        if handler is None:
            continue
        try:
            # #327(d): take the cascaded scope's own lock — this handler
            # call bypasses the dispatcher's lock machinery, so a
            # concurrent dispatch("agents"/"policies") would otherwise
            # interleave with the same sweep. Lock order stays
            # one-directional: config_sync -> {agents|policies} ->
            # agent:<role> (inside the policies cascade).
            async with _get_lock(_lock_key(scope, None)):
                sub = await handler(runtime, role=None)
            actions.append(f"{scope}:{sub}")
        except Exception as exc:  # noqa: BLE001 — one cascade failure shouldn't abort the rest
            logger.warning("config_sync cascade: scope=%s failed: %s", scope, exc)

    return actions


register_handler("config_sync", reload_config_sync)


async def reload_full(
    runtime: Any, *, role: str | None = None, include_env: bool = False,
) -> list[str]:
    """Compose policies + agents + executors + per-role agent
    (+ optional plugin_env).

    Each sub-handler is invoked DIRECTLY (not via dispatch) so a
    single ``full``-scope lock guards the whole sequence —
    sub-handlers don't re-enter the dispatcher's lock machinery.

    Order rationale: executors before per-role agent reload because
    ``engage_executor`` lookups go through the ExecutorRegistry; if
    an operator edits an executor definition and a resident
    delegate-list at the same time, we want the executor refresh to
    land first so any subsequent delegate is dispatching against
    fresh state.
    """
    actions: list[str] = []

    # §3.9 mutation sequencing / manual-edit seam: refresh the plugin resolver
    # snapshot from disk FIRST — BEFORE any agent is reconstructed below — so
    # reconstructed agents pick up the new registry and desired==active
    # verification compares fresh state (a stale snapshot would false-pass).
    import plugin_registry
    await asyncio.to_thread(plugin_registry.reload_snapshot)
    actions.append("plugins:snapshot_reloaded")

    # Policies — full cascade includes per-role re-load.
    sub = await _HANDLERS["policies"](runtime, role=None)
    actions += [f"policies:{a}" for a in sub]

    # Agents — adds/evicts residents + specialists.
    sub = await _HANDLERS["agents"](runtime, role=None)
    actions += [f"agents:{a}" for a in sub]

    # v0.37.1 A-1: executors — picks up definition.yaml edits + adds/deletes.
    sub = await _HANDLERS["executors"](runtime, role=None)
    actions += [f"executors:{a}" for a in sub]

    # Per-role agent reload.
    for r in list(runtime.role_configs.keys()) + list(
        runtime.specialist_registry.all_configs().keys(),
    ):
        sub = await _HANDLERS["agent"](runtime, role=r)
        actions += [f"agent:{r}:{a}" for a in sub]

    if include_env:
        sub = await _HANDLERS["plugin_env"](runtime, role=None)
        actions += [f"plugin_env:{a}" for a in sub]

    return actions


register_handler("full", reload_full)
