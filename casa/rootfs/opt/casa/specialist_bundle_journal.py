"""Bundle-op journal + boot reconciliation with quarantine semantics
(design spec §3.1).

Every specialist-bundle mutation (install/upgrade/rollback/uninstall, Task 10)
journals its FULL before-state to `<ops_dir>/<slug>.<opid>.json` BEFORE any
durable mutation — fsynced (file AND directory) so a crash mid-write never
leaves a torn journal that looks complete. `reconcile_boot` runs before the
plugin snapshot loads (the boot hook in `plugin_boot.py`) and restores
consistency from whatever it finds:

- an **in-progress** journal is rolled back from its captured before-state
  (registry entries, tuple/sidecar files, consent-ack records) then unlinked;
- a journal whose payload reached `state == "complete"` (crash between the
  complete-write and the unlink) is pruned WITHOUT rollback — the op already
  finished, undoing it would be the bug;
- a journal whose FILENAME parses but whose payload is corrupt, fails strict
  structural validation, OR whose rollback itself fails (e.g. a malformed ack
  record) is quarantined: that slug's owned registry entries are removed and
  the slug is flagged, then the journal is renamed `.quarantined` (never
  deleted — forensics);
- a journal whose filename does not even parse quarantines EVERY owned
  registry entry (deterministic worst case — there is no slug to trust).

This is degrade-and-boot, matching `plugin_boot`'s philosophy: one specialist
must not brick the house. `reconcile_boot` itself never raises; its caller
still wraps the call (belt-and-suspenders) per the boot-hook contract.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import uuid

import yaml
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import plugin_registry

logger = logging.getLogger(__name__)

OPS_DIR = Path("/config/specialists/.ops")
SPECIALISTS_DIR = Path("/config/specialists")
ACKS_PATH = Path("/data/specialist_install_acks.json")

SCHEMA_VERSION = 1

# Slug encoded in the FILENAME (outside the corruptible payload) so a corrupt
# journal still identifies its slug for selective quarantine (Sol r4).
JOURNAL_NAME_RE = re.compile(
    r"^(?P<slug>[a-z0-9][a-z0-9-]{0,31})\.(?P<opid>[0-9a-f]{32})\.json$"
)

# The fixed set of bare filenames a bundle transaction may ever record in
# `before.tuple_files` — written ONLY under specialists_dir/<slug>/, never a
# caller- or payload-supplied path (containment against traversal).
TUPLE_FILENAMES = frozenset({
    "active.yaml", "desired.yaml", "active.prior.yaml",
    "owned-plugins.yaml", "owned-plugins.desired.yaml",
    "owned-plugins.prior.yaml",
    # #339/#346 (Sol round-2): the pending prior-rotation journal
    # InstanceDir.commit_desired_to_active leaves behind when a rotation
    # fails or crashes. A bundle commit's copy step overwrites it, so
    # compensation must restore (or re-remove) it with the rest of the
    # tuple state or a compensated crash discards the pending rotation.
    "active.yaml.rollback-tmp",
    # #331 (Sol r7-2): the pending-receipt marker travels WITH the pending
    # tuple — an activating retry clears it, and if that retry's sequencer
    # fails, compensation must restore the marker alongside desired.yaml or
    # the boot sweep falls back to newest-by-mtime and can retain the wrong
    # root's receipt while sweeping the only resumable one.
    "pending-receipt.json",
})

# Task 11 reads this after every boot to surface reconciliation results in
# the plugin-health report.
last_boot_reconcile_actions: list[dict] = []


def _fsync_write(path: Path, data: str) -> None:
    # Whole-branch K: write a temp file + fsync + os.replace (atomic rename) +
    # dir fsync, NEVER O_TRUNC in place. An in-place truncate-then-write means a
    # crash mid-`complete()` tears the payload — `reconcile_boot` then fails
    # strict validation and QUARANTINES a slug whose op had already FINISHED.
    # os.replace makes the on-disk journal flip atomically between two intact
    # states, so a crash leaves either the old bytes or the new bytes, never a
    # torn hybrid.
    tmp = path.with_name(path.name + f".tmp-{uuid.uuid4().hex}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            # P2-6: a single os.write() may write fewer bytes than requested
            # (a "short write"), silently truncating the journal — the exact
            # torn-payload state os.replace was chosen to avoid. Loop until the
            # whole buffer is on the fd before fsync/replace.
            buf = memoryview(data.encode("utf-8"))
            while buf:
                buf = buf[os.write(fd, buf):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        # A crash mid-write leaves NO orphan temp (and never touches `path`).
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _dump(payload: dict) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


# #372 (D9): instance-tuple-shaped capture filenames — the only entries of
# `before.tuple_files` the sanitizer below rewrites. Sidecars and the
# pending-receipt marker never carry config snapshots.
_CAPTURED_TUPLE_FILES = frozenset({
    "active.yaml", "desired.yaml", "active.prior.yaml",
    "active.yaml.rollback-tmp",
})


def _captured_secret_union(*, op: str, target_root: str, tuple_root: object,
                           specialists_dir: Path) -> "set[str] | None":
    """#372 (D9): the secret-name union a captured tuple snapshot is
    sanitized against — the capture's own root schema, plus (for install/
    upgrade ops) the incoming target root's schema. ``None`` means fail
    closed: strip every key (unloadable/tampered schema, unusable root, or —
    r7 amendment — an install/upgrade journal without a usable target_root,
    the pre-provenance journal shape)."""
    from specialist_install import _declared_secret_names_for_root
    if not isinstance(tuple_root, str) or not tuple_root:
        return None
    union = _declared_secret_names_for_root(
        tuple_root, specialists_dir=specialists_dir)
    if union is None:
        return None
    union = set(union)
    if op in ("install", "upgrade"):
        if not isinstance(target_root, str) or not target_root:
            return None
        incoming = _declared_secret_names_for_root(
            target_root, specialists_dir=specialists_dir)
        if incoming is None:
            return None
        union |= incoming
    return union


def _sanitize_captured_tuple_files(
    tuple_files: "dict[str, str | None]", *, op: str, target_root: str,
    specialists_dir: Path,
) -> "dict[str, str | None]":
    """#372 (D9): one sanitizer for BOTH journal ends — applied when a capture
    is serialized (a new journal never holds plaintext or a secret-derived
    digest at any moment of its life) and again when any journal restores
    (covering pre-fix journals already on disk). Idempotent: an honest or
    already-sanitized payload passes through byte-identically; an unparseable
    tuple payload becomes the minimal sentinel tombstone (fail closed)."""
    from personality_binding import PRE_GUARD_SENTINEL

    sanitized: "dict[str, str | None]" = {}
    for filename, content in tuple_files.items():
        if content is None or filename not in _CAPTURED_TUPLE_FILES:
            sanitized[filename] = content
            continue
        try:
            payload = yaml.safe_load(content)
        except yaml.YAMLError:
            payload = None
        if not isinstance(payload, dict):
            sanitized[filename] = yaml.safe_dump({
                "api_version": "casa.instance-tuple/v1",
                "binding": {"effective_config_digest": PRE_GUARD_SENTINEL},
                "config_snapshot": {}, "config_digest": PRE_GUARD_SENTINEL,
            }, sort_keys=False)
            logger.warning(
                "bundle journal (op=%s): unparseable captured %s replaced by "
                "a sentinel tombstone (#372)", op, filename)
            continue
        snapshot = payload.get("config_snapshot")
        stripped = False
        if snapshot and not isinstance(snapshot, dict):
            payload["config_snapshot"] = {}
            stripped = True
        elif isinstance(snapshot, dict) and snapshot:
            union = _captured_secret_union(
                op=op, target_root=target_root,
                tuple_root=payload.get("root"), specialists_dir=specialists_dir)
            if union is None:
                union = set(snapshot)
            kept = {k: v for k, v in snapshot.items() if k not in union}
            if len(kept) != len(snapshot):
                payload["config_snapshot"] = kept
                stripped = True
        # #372 (both reviewers, diff r1): key-stripping alone misses the
        # v0.137 sanitized shape — a secret-free snapshot whose digests were
        # computed over the original secret-bearing mapping. Check the digest
        # equation on BOTH fields unconditionally; any disagreement (or an
        # undigestable snapshot) tombstones. A sentinel already in place is
        # left as-is (idempotent).
        if not stripped:
            from personality_binding import compute_effective_config_digest
            binding = payload.get("binding")
            binding_digest = (binding.get("effective_config_digest")
                              if isinstance(binding, dict) else None)
            # Diff r2 (both reviewers): the idempotence exemption applies only
            # when BOTH fields are already sentinels — a sentinel in one field
            # must not shield a stale secret-derived digest in the other.
            both_sentineled = (
                payload.get("config_digest") == PRE_GUARD_SENTINEL
                and (binding_digest == PRE_GUARD_SENTINEL
                     or not isinstance(binding, dict)))
            if not both_sentineled:
                try:
                    expected = compute_effective_config_digest(
                        payload.get("config_snapshot") or {})
                except Exception:  # noqa: BLE001 — undigestable: fail closed
                    expected = None
                if (expected is None
                        or payload.get("config_digest") != expected
                        or (isinstance(binding, dict)
                            and binding_digest != expected)):
                    stripped = True
        if stripped:
            payload["config_digest"] = PRE_GUARD_SENTINEL
            binding = payload.get("binding")
            if isinstance(binding, dict):
                binding["effective_config_digest"] = PRE_GUARD_SENTINEL
            sanitized[filename] = yaml.safe_dump(payload, sort_keys=False)
            logger.warning(
                "bundle journal (op=%s): stripped secret-union key(s) from "
                "captured %s and tombstoned its digests (#372)", op, filename)
        else:
            sanitized[filename] = content
    return sanitized


def begin(op: str, slug: str, *, before_entries: list[dict],
          before_tuple_files: dict[str, "str | None"],
          ack_records: list[dict], receipt_digest: str = "",
          consent_identity: str = "", target_root: str = "",
          ops_dir: Path = OPS_DIR) -> Path:
    """Write `<slug>.<uuid4hex>.json` with the full before-state, fsynced
    (file AND directory). Returns the journal path.

    Whole-branch I: the payload also records `consent_identity` (the computed
    install/upgrade consent identity string) and `target_root` (the target
    generation's root string, `component_id@version#root_digest`) — provenance
    a forensic reader (or a future selective boot reconcile) needs to know
    exactly which approved artifact this op was landing. Additive: both default
    to "" and `_valid_payload` tolerates their absence on pre-I journals."""
    ops_dir = Path(ops_dir)
    ops_dir.mkdir(parents=True, exist_ok=True)
    path = ops_dir / f"{slug}.{uuid.uuid4().hex}.json"
    # #372 (D9a): captures are sanitized BEFORE they are serialized — the
    # journal file itself must never hold a snapshot's secret-union keys or a
    # digest computed over them, at any moment of its life.
    before_tuple_files = _sanitize_captured_tuple_files(
        dict(before_tuple_files), op=op, target_root=target_root,
        specialists_dir=SPECIALISTS_DIR)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "op": op,
        "slug": slug,
        "state": "in-progress",
        "before": {
            "registry_entries": before_entries,
            "tuple_files": before_tuple_files,
            "ack_records": ack_records,
        },
        "receipt_digest": receipt_digest,
        "consent_identity": consent_identity,
        "target_root": target_root,
        "steps_done": [],
    }
    _fsync_write(path, _dump(payload))
    return path


def mark_step(journal_path: Path, step: str) -> None:
    journal_path = Path(journal_path)
    payload = json.loads(journal_path.read_text(encoding="utf-8"))
    payload.setdefault("steps_done", []).append(step)
    _fsync_write(journal_path, _dump(payload))


def complete(journal_path: Path) -> None:
    """Mark the journal complete (fsynced) THEN unlink. A crash between the
    two leaves a `state == "complete"` file on disk — `reconcile_boot` prunes
    that without rolling back (the op already finished)."""
    journal_path = Path(journal_path)
    payload = json.loads(journal_path.read_text(encoding="utf-8"))
    payload["state"] = "complete"
    _fsync_write(journal_path, _dump(payload))
    journal_path.unlink()


@dataclass(frozen=True)
class BundleTxn:
    """The rollback half of a bundle transaction — reusable by boot
    reconciliation (below) and by Task 10's in-process compensation path."""

    journal_path: Path
    slug: str
    before_entries: list[dict]
    before_tuple_files: dict
    ack_records: list[dict]
    removed_artifact_ids: tuple[str, ...] = ()
    new_artifact_ids: tuple[str, ...] = ()
    # #372 (D9b): provenance the restore-side sanitizer needs. Defaults are
    # the fail-closed shape — a constructor that does not thread them gets
    # all-keys stripping for any secret-bearing capture, never plaintext.
    op: str = ""
    target_root: str = ""
    registry_path: Path = plugin_registry.REGISTRY_PATH
    specialists_dir: Path = SPECIALISTS_DIR
    acks_path: Path = ACKS_PATH
    agents_specialists_dir: Path = Path("/config/agents/specialists")

    def rollback_disk(self) -> None:
        """Restore the registry entries, tuple/sidecar files, and consent-ack
        records captured in `before` to their recorded destinations. Sync —
        callers running in async contexts dispatch it to a thread."""
        from specialist_install_consent import SpecialistInstallAckStore

        # 1. Registry entries: drop anything currently owned by this slug,
        # reinsert the recorded before-entries, save.
        data = plugin_registry.load_registry(self.registry_path)
        # Whole-branch G: never reconstruct a partial registry on top of an
        # unreadable/invalid one — that would drop every non-owned entry and
        # re-save a truncated doc as authoritative. Raise so boot
        # reconciliation routes to the quarantine path (reconcile_boot's
        # rollback try/except) instead of saving partial data.
        if not data.valid:
            raise ValueError("registry_invalid: refusing to roll back over an "
                             "unreadable/invalid registry")
        raw = data.raw if isinstance(data.raw, dict) else {}
        plugins = raw.get("plugins")
        if not isinstance(plugins, list):
            plugins = []
        kept = [e for e in plugins
                if not (isinstance(e, dict) and
                        plugin_registry.entry_owner(e) == f"specialist:{self.slug}")]
        kept.extend(self.before_entries)
        raw["plugins"] = kept
        data.raw = raw
        plugin_registry.save_registry(data, self.registry_path)

        # 2. Tuple/sidecar files: write recorded content back; delete files
        # recorded as absent (content is None). Sol diff r1 (#490): steps 2/2b
        # run under MATERIALIZE_LOCK — the reconcile pass re-reads the active
        # tuple under that lock immediately before materializing, so an
        # unlocked rollback could interleave (reconcile reads a still-present
        # active → rollback removes tuple AND symlink → reconcile materializes
        # its stale read), resurrecting the orphan symlink this step removes.
        # Serialized, either order converges: reconcile-first is undone by the
        # rollback's removal; rollback-first leaves no active for reconcile to
        # materialize. No caller holds the lock here (the lifecycle's
        # sync-phase handlers call rollback_disk from except blocks after
        # their `with MATERIALIZE_LOCK:` scopes exited; the tool compensation
        # thread and boot reconciliation never take it).
        from specialist_materialize import MATERIALIZE_LOCK, resolve_material_content_dir
        # #372 (D9b): every restore — runtime compensation AND boot recovery —
        # re-runs the capture sanitizer, so a journal written by pre-fix code
        # (or with unusable provenance) restores stripped-and-tombstoned
        # state, never plaintext or a secret-derived digest. Idempotent for
        # captures D9a already sanitized.
        restored_tuple_files = _sanitize_captured_tuple_files(
            dict(self.before_tuple_files), op=self.op,
            target_root=self.target_root,
            specialists_dir=Path(self.specialists_dir))
        with MATERIALIZE_LOCK:
            slug_dir = Path(self.specialists_dir) / self.slug
            for filename, content in restored_tuple_files.items():
                target = slug_dir / filename
                if content is None:
                    target.unlink(missing_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")

            # 2b (#490). Fresh install (no prior active tuple): the op-symlink
            # created by materialization has no prior generation to fall back
            # to — remove it and GC its contained content dir, or every later
            # reload re-discovers the slug through the surviving symlink while
            # the roles overlay (rebuilt from the rolled-back index) no longer
            # carries it, and the specialist fails every reload until removed
            # by hand. An upgrade rollback keeps the symlink: the slug stays
            # installed and the self-heal reconcile re-materializes from the
            # restored active tuple. Containment-gated exactly like the
            # materializer's own GC: only THIS slug's `.{slug}.material-<hex>`
            # target inside the agents dir is ever rmtree'd; anything else is
            # unlinked only.
            if self.before_tuple_files.get("active.yaml") is None:
                agents_dir = Path(self.agents_specialists_dir)
                link = agents_dir / self.slug
                if link.is_symlink():
                    content_dir = resolve_material_content_dir(link, agents_dir)
                    link.unlink(missing_ok=True)
                    if content_dir is not None:
                        shutil.rmtree(content_dir, ignore_errors=True)

        # 3. Consent-ack records: slug-scoped delta re-insert (never a
        # whole-map rewrite — see SpecialistInstallAckStore.restore_records).
        SpecialistInstallAckStore(self.acks_path).restore_records(
            self.ack_records)


def quarantine(slug: str, *,
                registry_path: Path = plugin_registry.REGISTRY_PATH) -> bool:
    """Remove every registry entry owned by `slug` and flag the slug in the
    registry raw doc's `quarantined_bundles` list (surfaced by health,
    Task 11). Returns True when the quarantine was durably persisted; False
    when it was skipped because the registry is invalid (#372, Sol diff r3 —
    a skip is not durable quarantine, and callers must not treat it as
    success)."""
    registry_path = Path(registry_path)
    data = plugin_registry.load_registry(registry_path)
    # Whole-branch G: never save a reconstructed partial doc over an
    # unreadable/invalid registry — that would discard the original bytes (and
    # any non-owned entries a schema-invalid doc still holds). An invalid
    # registry already fails closed (resolve returns registry_valid=False, so
    # NOTHING resolves — the owned entries are already unreachable); persisting
    # a truncated `{quarantined_bundles: [...]}` would only make it worse.
    if not data.valid:
        logger.warning(
            "quarantine(%s): registry is invalid — skipping save (already "
            "fails closed)", slug)
        return False
    raw = data.raw if isinstance(data.raw, dict) else {}
    plugins = raw.get("plugins")
    if isinstance(plugins, list):
        raw["plugins"] = [
            e for e in plugins
            if not (isinstance(e, dict) and
                    plugin_registry.entry_owner(e) == f"specialist:{slug}")
        ]
    qlist = raw.setdefault("quarantined_bundles", [])
    if slug not in qlist:
        qlist.append(slug)
    data.raw = raw
    plugin_registry.save_registry(data, registry_path)
    return True


def quarantine_all(*,
                    registry_path: Path = plugin_registry.REGISTRY_PATH) -> bool:
    """Deterministic worst case: an unparseable journal filename carries no
    trustworthy slug, so every owner-bearing entry is removed and every
    owning slug flagged."""
    registry_path = Path(registry_path)
    data = plugin_registry.load_registry(registry_path)
    # Whole-branch G: same guard as quarantine() — an invalid registry already
    # fails closed; do not overwrite it with a reconstructed partial doc.
    if not data.valid:
        logger.warning(
            "quarantine_all: registry is invalid — skipping save (already "
            "fails closed)")
        return False
    raw = data.raw if isinstance(data.raw, dict) else {}
    plugins = raw.get("plugins")
    slugs: set[str] = set()
    if isinstance(plugins, list):
        kept = []
        for e in plugins:
            owner = plugin_registry.entry_owner(e) if isinstance(e, dict) else None
            if owner is not None:
                slugs.add(owner.split(":", 1)[1])
                continue
            kept.append(e)
        raw["plugins"] = kept
    qlist = raw.setdefault("quarantined_bundles", [])
    for s in sorted(slugs):
        if s not in qlist:
            qlist.append(s)
    data.raw = raw
    plugin_registry.save_registry(data, registry_path)
    return True


def replayable_tuple_files(path: Path) -> "dict[str, Any] | None":
    """#543: the captured tuple files this journal file would ACTUALLY have
    restored, or None when it would restore nothing.

    `reconcile_boot` below is the only thing that replays a journal, and it
    replays exactly one class: a file whose name parses, whose payload is
    `_valid_payload`, and whose state is "in-progress". Everything else it
    either prunes (complete) or quarantines WITHOUT rolling back (unparseable,
    wrong shape, unrecognized state) — and a quarantine that fails to persist
    retains the file for the next boot to retry, still without restoring it.

    This exists so that `persona_install.persona_references` — which must know
    whether a journal can put an override binding back on disk before it
    allows a persona to be removed — asks THIS module rather than
    re-implementing the predicate and drifting from it (Terra diff r1: the
    duplicated version disagreed on invalid states and refused removals that
    nothing could ever justify).

    An OSError is left to propagate: a file that cannot be READ right now may
    read fine at boot, so its replayability is unknown, not negative — the
    caller decides what to do with that (removal refuses)."""
    if path.name.endswith(".quarantined") or re.search(r"\.tmp-[0-9a-f]{32}$", path.name):
        return None
    match = JOURNAL_NAME_RE.match(path.name)
    if match is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    if not _valid_payload(payload, match.group("slug")):
        return None
    if payload["state"] != "in-progress":
        return None
    return dict(payload["before"]["tuple_files"])


def _valid_payload(payload: Any, slug: str) -> bool:
    """Strict, jsonschema-shaped structural validation (spec §3.1): schema
    shape, `payload["slug"] == filename slug`, and tuple-path containment —
    every `before.tuple_files` key must be one of the fixed journalled filenames (TUPLE_FILENAMES).
    Never raises — any unexpected shape is simply invalid."""
    try:
        if not isinstance(payload, dict):
            return False
        if payload.get("schema_version") != SCHEMA_VERSION:
            return False
        if not isinstance(payload.get("op"), str) or not payload["op"]:
            return False
        if payload.get("slug") != slug:
            return False
        if payload.get("state") not in ("in-progress", "complete"):
            return False
        before = payload.get("before")
        if not isinstance(before, dict):
            return False
        entries = before.get("registry_entries")
        if not isinstance(entries, list) or not all(
            isinstance(e, dict) for e in entries
        ):
            return False
        tuple_files = before.get("tuple_files")
        if not isinstance(tuple_files, dict):
            return False
        for key, value in tuple_files.items():
            if key not in TUPLE_FILENAMES:
                return False
            if value is not None and not isinstance(value, str):
                return False
        ack_records = before.get("ack_records")
        if not isinstance(ack_records, list) or not all(
            isinstance(r, dict) for r in ack_records
        ):
            return False
        # Whole-branch I (additive): when present these provenance fields must
        # be strings; absent (old journals) is tolerated.
        for key in ("consent_identity", "target_root"):
            value = payload.get(key)
            if value is not None and not isinstance(value, str):
                return False
        return True
    except Exception:  # noqa: BLE001 — strict validation must never raise
        return False


def _quarantine_remove(path: Path) -> None:
    """#372 (diff r1, both reviewers): a quarantined journal's FILE is deleted
    once the registry-level quarantine is durable — "kept until next boot" is
    no confidentiality bound on a box that never reboots, and the captured
    before-bytes can hold pre-guard plaintext and digests. The registry flag,
    the log line, and the reconcile actions list carry the diagnostics."""
    logger.warning(
        "deleting quarantined journal file %s (#372); the registry "
        "quarantine flag carries the diagnostic state", path.name)
    path.unlink(missing_ok=True)


RECEIPTS_DIR = Path("/config/specialists/.receipts")


def reconcile_boot(*, ops_dir: Path = OPS_DIR,
                    registry_path: Path = plugin_registry.REGISTRY_PATH,
                    specialists_dir: Path = SPECIALISTS_DIR,
                    acks_path: Path = ACKS_PATH,
                    receipts_dir: Path = RECEIPTS_DIR,
                    personas_dir: "Path | None" = None,
                    agents_specialists_dir: Path = Path(
                        "/config/agents/specialists")) -> list[dict]:
    """Scan EVERY regular file in `ops_dir` (skipping `*.quarantined`) and
    reconcile it per the module docstring. Runs before the plugin snapshot
    loads. Returns `[{slug, action}]` for the health report; also stashed on
    `last_boot_reconcile_actions`. Idempotent — safe to run twice."""
    global last_boot_reconcile_actions
    ops_dir = Path(ops_dir)
    registry_path = Path(registry_path)
    specialists_dir = Path(specialists_dir)
    acks_path = Path(acks_path)
    actions: list[dict] = []

    # Whole-branch N: age-sweep orphan receipt sidecars (an inspect that never
    # committed) on every boot, independent of any journal work below. Never
    # raises — a receipts-dir problem must not block boot.
    # #331 (Sol r5-2): a slug with a LIVE pending-configuration candidate
    # keeps its receipt (the configure re-commit requires it) and the staged
    # paths that receipt references, whatever their age — a pending install
    # is durable operator-visible state, not an abandoned flow.
    pending_slugs: set[str] = set()
    try:
        if specialists_dir.is_dir():
            for slug_dir in specialists_dir.iterdir():
                if not slug_dir.is_dir() or not (slug_dir / "desired.yaml").is_file():
                    continue
                # #372 (D3c liveness, Terra design r3): a tombstoned or
                # pre-guard desired is NOT a live pending candidate — its
                # configure re-commit can never succeed, and counting it here
                # would pin its receipt and staging tree through the age
                # sweep forever.
                import specialist_install as _si
                if _si._pre_guard_prior_reason(slug_dir / "desired.yaml") is not None:
                    logger.info(
                        "pending slug %r excluded from receipt retention: its "
                        "desired tuple is pre-guard/tombstoned (#372)",
                        slug_dir.name)
                    continue
                pending_slugs.add(slug_dir.name)
    except OSError:
        pass
    # Sol r6-2: prefer the durable marker naming the EXACT receipt the
    # pending candidate was committed with; newest-per-slug is only the
    # fallback for a pending slug with no readable marker.
    keep_receipt_ids: set[str] = set()
    marker_fallback_slugs: set[str] = set()
    for _slug in pending_slugs:
        marker = specialists_dir / _slug / "pending-receipt.json"
        rid = None
        try:
            import json as _mjson
            raw_marker = _mjson.loads(marker.read_text(encoding="utf-8"))
            if isinstance(raw_marker, dict):
                rid = raw_marker.get("receipt_id")
        except (OSError, ValueError):
            rid = None
        if isinstance(rid, str) and rid:
            keep_receipt_ids.add(rid)
        else:
            marker_fallback_slugs.add(_slug)
    try:
        import specialist_receipt
        swept = specialist_receipt.sweep_aged(
            receipts_dir=receipts_dir, keep_slugs=marker_fallback_slugs,
            keep_receipt_ids=keep_receipt_ids)
        if swept:
            actions.append({"slug": None, "action": "swept_receipts",
                            "count": swept})
    except Exception:  # noqa: BLE001 — degrade-and-boot
        logger.exception("receipt age-sweep failed")

    # #306: age-sweep abandoned STAGING TREES with the same 7-day cutoff —
    # denied/abandoned consent prompts and crashed flows leave full repo
    # copies under the .staging roots (and crash-leaked bundle/CAS staging
    # workspaces) that otherwise grow unbounded on the /config volume.
    try:
        import specialist_install
        if personas_dir is None:
            # #323 (Sol r3): the same env-aware seam every persona consumer
            # resolves through — a call-time default, never a frozen literal.
            from persona_install import installed_personas_root
            personas_dir = installed_personas_root()
        # Staged paths still referenced by a surviving receipt (pending
        # installs kept theirs above) are exempt from the age sweep.
        keep_paths: set[str] = set()
        try:
            import json as _json
            for rp in Path(receipts_dir).iterdir():
                if not (rp.is_file() and rp.suffix == ".json"):
                    continue
                try:
                    raw = _json.loads(rp.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(raw, dict):
                    continue
                staged = raw.get("component_staged_path")
                if isinstance(staged, str) and staged:
                    keep_paths.add(staged)
                for row in raw.get("plugins") or []:
                    p = row.get("staged_path") if isinstance(row, dict) else None
                    if isinstance(p, str) and p:
                        keep_paths.add(p)
        except OSError:
            pass
        swept_trees = specialist_install.sweep_staging_aged(roots=(
            specialists_dir / ".staging",
            specialists_dir / ".bundle-staging",
            specialists_dir / "store" / ".staging",
            Path(personas_dir) / ".staging",
        ), keep_paths=keep_paths)
        if swept_trees:
            actions.append({"slug": None, "action": "swept_staging_trees",
                            "count": swept_trees})
    except Exception:  # noqa: BLE001 — degrade-and-boot
        logger.exception("staging-tree age-sweep failed")

    if not ops_dir.is_dir():
        last_boot_reconcile_actions = actions
        return actions

    # #372 (D8): sweep terminal .ops residue BEFORE the scan.
    # - `_fsync_write`'s `<name>.tmp-<hex>` survives a hard kill between open
    #   and replace; its unrecognized filename would otherwise hit the
    #   quarantine_all arm below and drop every healthy specialist (Sol
    #   design r3). No journal writer is live during this boot oneshot.
    # - A `.quarantined` file from an EARLIER boot is skipped permanently by
    #   every recovery path, and its before.tuple_files can embed pre-guard
    #   digests and plaintext — delete it. Files quarantined DURING this run
    #   keep their one-boot diagnostic window and die at the next boot. The
    #   registry-level quarantine flag is untouched either way.
    for path in sorted(ops_dir.iterdir()):
        if not path.is_file():
            continue
        if re.search(r"\.tmp-[0-9a-f]{32}$", path.name):
            try:
                path.unlink()
                logger.warning(
                    "deleted crash-orphaned journal temporary %s (#372)", path.name)
                actions.append({"slug": None, "action": "deleted_journal_tmp"})
            except OSError:
                logger.exception("could not delete journal temporary %s", path)
        elif path.name.endswith(".quarantined"):
            try:
                path.unlink()
                logger.warning(
                    "deleted quarantined journal file %s from an earlier boot "
                    "(#372); the registry quarantine flag is unaffected",
                    path.name)
                actions.append(
                    {"slug": None, "action": "deleted_quarantined_journal"})
            except OSError:
                logger.exception("could not delete quarantined journal %s", path)

    for path in sorted(ops_dir.iterdir()):
        if not path.is_file():
            continue
        if path.name.endswith(".quarantined"):
            continue

        match = JOURNAL_NAME_RE.match(path.name)
        if match is None:
            try:
                persisted = quarantine_all(registry_path=registry_path)
            except Exception:  # noqa: BLE001 — degrade-and-boot
                logger.exception(
                    "quarantine_all failed for unparseable journal %s; the "
                    "journal file is retained so the next boot retries", path)
                continue  # #372 (Sol diff r2): delete only after durability
            if not persisted:  # #372 (Sol diff r3): a skipped save is not durable
                continue
            _quarantine_remove(path)
            actions.append({"slug": None, "action": "quarantine_all"})
            continue

        slug = match.group("slug")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None

        if not _valid_payload(payload, slug):
            try:
                persisted = quarantine(slug, registry_path=registry_path)
            except Exception:  # noqa: BLE001 — degrade-and-boot
                logger.exception(
                    "quarantine failed for slug %s; the journal file is "
                    "retained so the next boot retries", slug)
                continue  # #372 (Sol diff r2): delete only after durability
            if not persisted:  # #372 (Sol diff r3): a skipped save is not durable
                continue
            _quarantine_remove(path)
            actions.append({"slug": slug, "action": "quarantine"})
            continue

        if payload["state"] == "complete":
            # Crash between the complete-write and the unlink: the op
            # already finished — prune WITHOUT rollback.
            path.unlink()
            actions.append({"slug": slug, "action": "pruned_complete"})
            continue

        before = payload["before"]
        txn = BundleTxn(
            journal_path=path,
            slug=slug,
            before_entries=before["registry_entries"],
            before_tuple_files=before["tuple_files"],
            ack_records=before["ack_records"],
            # #372 (D9b): thread the journal's provenance to the restore-side
            # sanitizer; a pre-provenance journal (both default to "") fails
            # closed there.
            op=payload.get("op") or "",
            target_root=payload.get("target_root") or "",
            registry_path=registry_path,
            specialists_dir=specialists_dir,
            acks_path=acks_path,
            agents_specialists_dir=agents_specialists_dir,
        )
        try:
            txn.rollback_disk()
        except Exception:  # noqa: BLE001 — degrade-and-boot
            logger.exception("rollback failed for slug %s; quarantining", slug)
            try:
                persisted = quarantine(slug, registry_path=registry_path)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "quarantine failed for slug %s after rollback failure; "
                    "the journal file is retained so the next boot retries",
                    slug)
                continue  # #372 (Sol diff r2): delete only after durability
            if not persisted:  # #372 (Sol diff r3): a skipped save is not durable
                continue
            _quarantine_remove(path)
            actions.append({"slug": slug, "action": "quarantine"})
            continue

        path.unlink()
        actions.append({"slug": slug, "action": "rolled_back"})

    last_boot_reconcile_actions = actions
    return actions
