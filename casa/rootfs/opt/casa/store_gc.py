"""Cleanup of the plugin store and the specialist store (#1679).

Every plugin install or update publishes an artifact directory, and every
specialist install or upgrade a component directory; nothing else ever removes
one. This module removes the entries nothing names any more.

Three rules carry the whole design:

* **Naming is textual.** A store entry is *named* when its id (64 lowercase
  hex) occurs in the bytes of a reference file: the plugin registry, any
  regular file directly inside the specialists root or inside one of its child
  directories, and the engagement records. No field is parsed, so a file or a
  field added later that names an artifact is covered without a change here.
* **Deletion waits.** The registry is tracked in the config git repository and
  can be restored to name a superseded artifact again, while git holds no
  artifact bytes. An entry is deleted only after it has been observed unnamed
  for ``WAIT_SECONDS``, counted in a ledger. ``observe`` maintains the ledger,
  at every start and hourly while Casa runs, and never deletes. Ledger times
  are trusted only while observation was continuous: after a gap longer than
  ``GAP_SECONDS`` every wait starts again.
* **Deletion happens only at boot**, in the ``init-plugin-store`` oneshot,
  before ``svc-casa`` starts: no session reads an artifact and no install
  publishes one while it runs. Naming is recomputed in the same pass.

Anything that keeps the reference set from being read skips the pass: nothing
is deleted and the ledger is left as it was.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import atomic_io

logger = logging.getLogger(__name__)

REGISTRY_PATH = Path("/config/plugins/registry.json")
PLUGIN_STORE = Path("/config/plugins/store")
SPECIALISTS_DIR = Path("/config/specialists")
ENGAGEMENTS_PATH = Path("/data/engagements.json")
LEDGER_PATH = Path("/config/plugins/.store-gc.json")

WAIT_SECONDS = 30 * 86400
GAP_SECONDS = 2 * 3600
BOOT_BUDGET_SECONDS = 30.0

_ID_RE = re.compile(r"[0-9a-f]{64}")
_STORES = ("plugins", "specialists")


class ReferencesUnavailable(Exception):
    """A reference source exists and could not be read."""


@dataclass
class Paths:
    registry: Path = REGISTRY_PATH
    plugin_store: Path = PLUGIN_STORE
    specialists_dir: Path = SPECIALISTS_DIR
    engagements: Path = ENGAGEMENTS_PATH
    ledger: Path = LEDGER_PATH

    @property
    def specialist_store(self) -> Path:
        return self.specialists_dir / "store"


@dataclass
class Observation:
    """One pass: every store entry, which of them are named, and the ledger."""
    entries: dict[str, dict[str, list[Path]]]
    named: dict[str, set[str]]
    ledger: dict[str, dict[str, float]] = field(default_factory=dict)


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""
    except OSError as exc:
        raise ReferencesUnavailable(f"{path}: {exc}") from exc


def _listdir(path: Path) -> list[Path]:
    try:
        return sorted(path.iterdir())
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ReferencesUnavailable(f"{path}: {exc}") from exc


def _reference_bytes(paths: Paths) -> bytes:
    """Everything that may name a store entry, as one byte string."""
    registry = b""
    if paths.registry.exists() or paths.registry.is_symlink():
        # A registry that exists must be the JSON object the loader accepts; an
        # empty or torn one says nothing about what is named.
        registry = _read(paths.registry)
        try:
            valid = isinstance(json.loads(registry), dict)
        except ValueError:
            valid = False
        if not valid:
            raise ReferencesUnavailable(f"{paths.registry}: not a JSON object")
    blobs = [registry, _read(paths.engagements)]
    for child in _listdir(paths.specialists_dir):
        if child.is_file():
            blobs.append(_read(child))
        elif child.is_dir() and not child.is_symlink():
            # One level, whatever the directory is called: a specialist may be
            # named `store`, and its tuple files then sit beside the artifacts.
            blobs.extend(_read(leaf) for leaf in _listdir(child) if leaf.is_file())
    return b"\n".join(blobs)


def _entry_dirs(parent: Path) -> list[Path]:
    return [p for p in _listdir(parent)
            if _ID_RE.fullmatch(p.name) and not p.is_symlink() and p.is_dir()]


def _list_stores(paths: Paths) -> dict[str, dict[str, list[Path]]]:
    plugins: dict[str, list[Path]] = {}
    for name_dir in _listdir(paths.plugin_store):
        if name_dir.is_symlink() or not name_dir.is_dir():
            continue
        for entry in _entry_dirs(name_dir):
            plugins.setdefault(entry.name, []).append(entry)
    specialists = {entry.name: [entry]
                   for entry in _entry_dirs(paths.specialist_store)}
    return {"plugins": plugins, "specialists": specialists}


def _remove_tree(entry: Path) -> None:
    """Remove one store entry. Artifacts are frozen read-only; the boot oneshot
    runs as root and does not care, but nothing here should depend on that, so
    a refused step makes the directory it failed in writable and tries once
    more. `rmtree` refuses a symlinked root and follows no symlink inside."""
    def retry(func, path, _exc):
        for target in (os.path.dirname(path), path):
            if not os.path.islink(target) and os.path.isdir(target):
                os.chmod(target, os.stat(target).st_mode | stat.S_IRWXU)
        func(path)

    if sys.version_info >= (3, 12):
        shutil.rmtree(entry, onexc=retry)
    else:
        shutil.rmtree(entry, onerror=retry)


def _load_ledger(path: Path) -> tuple[dict[str, dict[str, float]], "float | None"]:
    """The ledger, or an empty one when it is missing or not usable."""
    empty: dict[str, dict[str, float]] = {store: {} for store in _STORES}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty, None
    if not isinstance(raw, dict):
        return empty, None
    ledger = empty
    for store in _STORES:
        rows = raw.get(store)
        if isinstance(rows, dict):
            ledger[store] = {
                key: float(value) for key, value in rows.items()
                if isinstance(key, str) and _ID_RE.fullmatch(key)
                and isinstance(value, (int, float)) and not isinstance(value, bool)}
    last = raw.get("last_observed")
    if isinstance(last, bool) or not isinstance(last, (int, float)):
        last = None
    return ledger, last


def _save_ledger(path: Path, ledger: dict[str, dict[str, float]], now: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_io.atomic_write_json(
        path, {**ledger, "last_observed": now}, sort_keys=True,
        prefix=".store-gc.")


def _observe(paths: Paths, now: float) -> Observation:
    references = _reference_bytes(paths)
    entries = _list_stores(paths)
    named = {store: {eid for eid in ids if eid.encode() in references}
             for store, ids in entries.items()}
    ledger, last = _load_ledger(paths.ledger)
    continuous = last is not None and 0 <= now - last <= GAP_SECONDS
    for store in _STORES:
        rows = ledger[store]
        for eid in list(rows):
            if eid not in entries[store] or eid in named[store]:
                del rows[eid]
            elif not continuous or rows[eid] > now:
                rows[eid] = now
        for eid in entries[store]:
            if eid not in named[store]:
                rows.setdefault(eid, now)
    _save_ledger(paths.ledger, ledger, now)
    return Observation(entries=entries, named=named, ledger=ledger)


def observe(*, paths: "Paths | None" = None, now: "float | None" = None,
            ) -> "Observation | None":
    """Bring the ledger up to date. Deletes nothing and never raises; returns
    ``None`` when the pass was skipped."""
    try:
        return _observe(paths or Paths(), time.time() if now is None else now)
    except ReferencesUnavailable as exc:
        logger.warning("store cleanup: pass skipped, a reference source "
                       "could not be read (%s)", exc)
    except Exception:  # noqa: BLE001 — a housekeeping pass: log, never raise
        logger.warning("store cleanup: pass skipped", exc_info=True)
    return None


def run_boot_sweep(*, registry_valid: bool, reconcile_ok: bool,
                   paths: "Paths | None" = None, now: "float | None" = None,
                   budget_seconds: float = BOOT_BUDGET_SECONDS,
                   log: "logging.Logger | None" = None) -> dict[str, list[str]]:
    """The boot pass: observe, then delete what has waited long enough.

    Returns the ids removed per store. Never raises."""
    log = log or logger
    removed: dict[str, list[str]] = {store: [] for store in _STORES}
    if not registry_valid or not reconcile_ok:
        log.info("store cleanup: skipped (%s)",
                 "the plugin registry is not valid" if not registry_valid
                 else "a bundle operation is still to be reconciled")
        return removed
    paths = paths or Paths()
    now = time.time() if now is None else now
    started = time.monotonic()      # the budget covers the observing too
    observation = observe(paths=paths, now=now)
    if observation is None:
        return removed
    out_of_time = False
    try:
        for store in _STORES:
            rows = observation.ledger[store]
            for eid in sorted(rows):
                if now - rows[eid] < WAIT_SECONDS:
                    continue
                # Checked between entries: a removal that has started is
                # finished, so the store never holds half an entry.
                if time.monotonic() - started >= budget_seconds:
                    out_of_time = True
                    break
                gone = True
                for entry in observation.entries[store][eid]:
                    try:
                        _remove_tree(entry)
                    except OSError as exc:
                        gone = False
                        log.warning("store cleanup: %s was not removed (%s); "
                                    "the next start tries again", entry, exc)
                        continue
                    if store == "plugins":
                        try:
                            os.rmdir(entry.parent)   # only when left empty
                        except OSError:
                            pass
                if gone:
                    del rows[eid]
                    removed[store].append(eid)
        _save_ledger(paths.ledger, observation.ledger, now)
    except Exception:  # noqa: BLE001 — spec 3.6: never block svc-casa
        log.warning("store cleanup: stopped early", exc_info=True)
    for store in _STORES:
        log.info("store cleanup: %s store: %d removed, %d waiting%s",
                 store[:-1], len(removed[store]), len(observation.ledger[store]),
                 " (time budget reached; the rest goes at a later start)"
                 if out_of_time else "")
    return removed
