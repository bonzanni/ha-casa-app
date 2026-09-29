"""Tier 3 Executor type registry.

Mirrors :mod:`specialist_registry` structurally but without an in-flight
table - :mod:`engagement_registry` already owns runtime state.
"""

from __future__ import annotations

import logging
import os
from typing import NamedTuple

from config import ExecutorDefinition

logger = logging.getLogger(__name__)


class _RegistryState(NamedTuple):
    """One immutable-by-convention publication of the registry's view.

    Sol v0.142.0 r2-3: load() runs in a worker thread while readers run on
    the event loop — four independent attribute assignments let a reader
    observe e.g. the new ``defs`` beside the old ``disabled`` (an executor
    momentarily neither enabled nor disabled). All collections publish
    together via ONE reference assignment of this tuple; multi-collection
    readers snapshot it once.
    """

    defs: dict[str, ExecutorDefinition]
    disabled: set[str]
    disabled_defs: dict[str, ExecutorDefinition]
    failed_types: set[str]


class ExecutorRegistry:
    """Loads Tier 3 Executor type definitions from ``<executors_dir>/<type>/``."""

    def __init__(self, executors_dir: str) -> None:
        self._dir = executors_dir
        self._state = _RegistryState({}, set(), {}, set())
        # #1096: whether the most recent load() failed at COLLECTION level —
        # it then publishes an empty registry and records no failed type, so a
        # reload could not otherwise tell it from "no executors".
        self.collection_failed = False

    # Legacy views (tests poke collection CONTENTS through these; rebinding
    # goes through _state):
    @property
    def _defs(self) -> dict[str, ExecutorDefinition]:
        return self._state.defs

    @property
    def _disabled(self) -> set[str]:
        """Config-disabled types. NEVER exposed via get() — engagement
        launch relies on get()==None to refuse a disabled type."""
        return self._state.disabled

    @property
    def _disabled_defs(self) -> dict[str, ExecutorDefinition]:
        """Disabled executors' definitions, kept so verify/health can
        validate a plugin assigned to a currently-disabled executor (the
        executor being off by config is not a plugin-health failure)."""
        return self._state.disabled_defs

    @property
    def _failed_types(self) -> set[str]:
        """#340 (Sol r1-1): types whose LOAD failed on the most recent
        load() — kept so reload can tell "failed to load" apart from
        "genuinely removed" when refreshing the HTTP hook-policy map: a
        failed executor's live engagements must keep their old (possibly
        tighter) callbacks rather than fall back to the broader defaults."""
        return self._state.failed_types

    def load(self) -> None:
        """Load every executor type under ``<self._dir>``.

        Personality Phase A, Task 5: ``load_all_executors`` now also loads
        and cross-validates each type's canonical role artifact
        (``defaults/roles/executor/<type>/{role.yaml,doctrine.md}``) and
        attaches it as ``ExecutorDefinition.role_artifact``, reachable
        through ``get()``/``definition_any()`` like every other field. A
        missing or id/kind/slot-mismatched artifact is a per-executor
        failure isolated the same way a schema violation is — it does not
        prevent sibling executors from loading. Task 6 consumes
        ``role_artifact`` for model resolution and the role checksum;
        executors get no persona and no binding.
        """
        from agent_loader import LoadError, load_all_executors

        # #351: build into FRESH structures and swap only on success. The
        # pre-fix clear-then-scan meant any non-LoadError escaping the scan
        # (a transient PermissionError listing the dir) left the live
        # registry already emptied — every executor definition gone until a
        # later successful reload. An unexpected exception now propagates
        # with the previous state intact (reload surfaces it as load_error;
        # boot behavior is unchanged — it never caught these either).
        defs: dict[str, ExecutorDefinition] = {}
        disabled: set[str] = set()
        disabled_defs: dict[str, ExecutorDefinition] = {}
        failed_types: set[str] = set()

        base = os.path.dirname(self._dir)
        collection_failed = False
        try:
            found, failed = load_all_executors(base)
        except LoadError as exc:
            # Collection-level error (e.g. executors_root unreadable).
            logger.error("Executor load failed at collection level: %s", exc)
            found, failed = {}, []
            collection_failed = True

        # v0.37.1 B-1b: per-file failures don't poison siblings.
        for name, err in failed:
            failed_types.add(name)
            logger.error(
                "Executor %r failed to load: %s; other executors continue",
                name, err,
            )

        for type_name, defn in found.items():
            if not defn.enabled:
                logger.info("Executor %r bundled but disabled", type_name)
                disabled.add(type_name)
                disabled_defs[type_name] = defn
                continue
            defs[type_name] = defn
            logger.info(
                "Executor %r loaded (model=%s driver=%s)",
                type_name, defn.model, defn.driver,
            )

        # Sol r2-3: ONE reference assignment publishes every collection
        # together — no torn cross-collection read from the loop thread.
        self._state = _RegistryState(defs, disabled, disabled_defs,
                                     failed_types)
        self.collection_failed = collection_failed

        logger.info(
            "Executors: loaded=%s failed=%s disabled=%s",
            sorted(defs.keys()),
            sorted(n for n, _ in failed),
            sorted(disabled),
        )

    def get(self, type_name: str) -> ExecutorDefinition | None:
        return self._state.defs.get(type_name)

    def is_disabled(self, type_name: str) -> bool:
        """True iff ``type_name`` loaded but is config-disabled (enabled: false)."""
        return type_name in self._state.disabled

    def definition_any(self, type_name: str) -> ExecutorDefinition | None:
        """The definition whether the executor is ENABLED or DISABLED — for
        verify/health AND boot resume of EXISTING engagements (Task 8): a
        specialist disabled AFTER an engagement launched must still resume,
        so ``replay_undergoing_engagements`` resolves a brief-bearing record's
        executor through here, not ``get()``. A plugin assigned to a disabled
        executor can likewise still have its tools.allowed inspected.
        Engagement LAUNCH (a NEW engagement) must keep using ``get()`` (None
        for disabled) to refuse a disabled type."""
        s = self._state
        return s.defs.get(type_name) or s.disabled_defs.get(type_name)

    def list_types(self) -> list[str]:
        return sorted(self._state.defs.keys())

    @property
    def failed_types(self) -> set[str]:
        """Types whose most recent load() failed (copy — see _RegistryState)."""
        return set(self._state.failed_types)

    def list_types_any(self) -> list[str]:
        """Every loaded type, ENABLED or DISABLED — the iteration counterpart
        of :meth:`definition_any`. #315: the HTTP hook-policy map must cover
        disabled-but-resumable executors (boot replay resumes their existing
        brief-bearing engagements), so its builder iterates this, not
        ``list_types()``. Engagement LAUNCH keeps using ``list_types()``/
        ``get()`` to refuse a disabled type."""
        s = self._state
        return sorted(set(s.defs) | set(s.disabled_defs))
