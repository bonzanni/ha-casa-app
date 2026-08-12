"""Bare-persona repo install/apply (spec §2.1, §9.4 decision 4) — generalizes
Task N1a's fetch/validate/consent pipeline for a MUCH smaller artifact (no
role, no dependency closure, no config schema) and applies the result as an
override binding, reusing Plan 1 Task 8's swap machinery exactly."""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from canonical_bytes import checksum_json
from specialist_install import SpecialistInstallError, resolve_and_fetch

# Whole-branch review F1: `commit_persona_install` and `persona_apply` join a
# persona_id + version into filesystem paths
# (`/config/personas/<persona_id>/<version>/...`). Validate both against the
# SAME patterns specialist-component.v1.json's `default_persona.ref` enforces
# — a `persona_id` is exactly `namespace/name` (a single embedded slash, both
# segments safe) and `version` is semver — so `..`, an absolute segment, or an
# extra path separator can never index a persona directory.
_PERSONA_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?/[a-z0-9][a-z0-9-]*$")
_PERSONA_VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def validate_persona_path_segments(persona_id: object, version: object) -> None:
    """F1: fail-closed gate for the persona_id/version path segments joined
    into `/config/personas/<persona_id>/<version>/`. Raises a typed
    ``invalid_persona_ref`` SpecialistInstallError; never lets a
    traversal/absolute/extra-separator value reach a ``Path`` join."""
    if not (isinstance(persona_id, str) and _PERSONA_ID_RE.fullmatch(persona_id)):
        raise SpecialistInstallError("invalid_persona_ref", f"invalid persona_id {persona_id!r}")
    if not (isinstance(version, str) and _PERSONA_VERSION_RE.fullmatch(version)):
        raise SpecialistInstallError(
            "invalid_persona_ref", f"invalid persona version {version!r}")

if TYPE_CHECKING:
    from persona_pack import PersonaPack
    from role_slot import RoleSlot

__all__ = [
    "PersonaInspectionResult",
    "inspect_persona_repo",
    "persona_install_consent_identity",
    "PersonaInstallAckStore",
    "commit_persona_install",
    "apply_persona_override",
    "validate_persona_path_segments",
]


def installed_personas_root() -> Path:
    """The operator-installed personas root, resolved through the SAME
    ``$CASA_CONFIG_DIR`` seam the resident loader reads (#323, Sol r3-2/3).
    Every default in the install/apply/activate flow resolves through this at
    CALL time — a def-time ``Path("/config/personas")`` default froze the
    root before the environment was consulted, so under a custom config root
    the tools published to one directory while apply and the loaders read
    another."""
    import os

    return Path(os.environ.get("CASA_CONFIG_DIR", "/config")) / "personas"


def _reclaim_inspection_staging(staged_dir: Path) -> None:
    """#306: consume the inspection staging tree once a commit has succeeded
    (including the idempotent/race-reconciled outcomes — the staged bytes are
    verified equal to what is published). Same containment guard as
    specialist_install.reclaim_staging_tree: only a direct child of a
    ``.staging`` directory is ever removed. Never raises."""
    import shutil

    path = Path(staged_dir)
    if path.parent.name != ".staging":
        return
    shutil.rmtree(path, ignore_errors=True)


@dataclass(frozen=True, slots=True)
class PersonaInspectionResult:
    persona_id: str
    version: str
    checksum: str
    display_name: str
    staged_dir: Path


def inspect_persona_repo(
    repo: str, ref: str, *, subdir: str = "", expected_revision: str | None = None,
    staging_root: "Path | None" = None,
) -> PersonaInspectionResult:
    from persona_pack import PersonaPackError, load_persona_pack

    if staging_root is None:
        staging_root = installed_personas_root() / ".staging"
    staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = staging_root / uuid.uuid4().hex
    # #306: a rejected inspection must not leave the fetched staging tree
    # behind; a successful one retains it for commit to consume.
    try:
        resolve_and_fetch(repo, ref, subdir, dest, expected_revision=expected_revision)
        manifest_path = dest / "manifest.json"
        if not manifest_path.is_file():
            raise SpecialistInstallError("manifest_missing", f"{repo}@{ref}: manifest.json not found")
        try:
            pack = load_persona_pack(dest / "pack", manifest_path)
        except PersonaPackError as exc:
            raise SpecialistInstallError("persona_invalid", str(exc)) from exc
    except BaseException:
        import shutil
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return PersonaInspectionResult(
        persona_id=pack.persona_id, version=pack.version, checksum=pack.checksum,
        display_name=pack.identity.get("display_name", pack.persona_id), staged_dir=dest,
    )


_ACKS_PATH = Path("/data/persona_install_acks.json")
_SCHEMA_VERSION = 1

# #310: ONE process-wide ledger lock (the same shape Task 7 gave
# SpecialistInstallAckStore). The tool layer constructs a fresh
# PersonaInstallAckStore per call, and a consent keyboard's approve callback
# records through its own prompt-time instance — a per-instance lock plus an
# instance cache let sibling prompts clobber each other's persisted acks.
# Every store method acquires this module-level lock, reloads the ledger file
# fresh, applies its delta, and persists; the instance holds no authoritative
# in-memory cache.
_LEDGER_LOCK = threading.Lock()


def persona_install_consent_identity(*, persona_id: str, version: str, checksum: str) -> str:
    return checksum_json({"persona_id": persona_id, "version": version, "checksum": checksum})


class PersonaInstallAckStore:
    """Same fail-closed/atomic-write shape as SpecialistInstallAckStore and
    trigger_acks.TriggerAckStore — a third sibling on the SAME structural
    pattern, not a fourth divergent design.

    #310: like SpecialistInstallAckStore (Task 7), the instance holds NO
    authoritative in-memory cache — every method takes the module-level
    ``_LEDGER_LOCK``, re-reads the ledger fresh, applies its delta, and (for
    mutations) persists. Multiple instances over the same file interleave
    safely. (TriggerAckStore keeps its instance cache: it is only ever used
    through the process-wide ``ACKS`` singleton, and its in-memory view
    surviving a later unreadable file is a documented contract there.)"""

    def __init__(self, path: Path = _ACKS_PATH) -> None:
        self.path = Path(path)

    def _load(self, *, strict_read: bool = False) -> dict[str, dict[str, Any]]:
        # Caller must hold _LEDGER_LOCK. Sol r1 (#310): a MUTATION passes
        # strict_read=True — a transient read failure (an OSError that is not
        # file-missing) must abort the read-modify-write rather than persist
        # the fail-closed empty view over previously recorded acks. Reads keep
        # the fail-closed {} (no consent manufactured), and a content-invalid
        # store still starts empty: the next successful record rewrites a
        # valid store (the siblings' documented corruption recovery).
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except OSError:
            if strict_read:
                raise
            return {}
        except ValueError:
            return {}
        if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA_VERSION:
            return {}
        acks = raw.get("acks")
        if not isinstance(acks, dict):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for ident, rec in acks.items():
            if not (isinstance(ident, str) and isinstance(rec, dict)):
                return {}
            fields = {k: rec.get(k) for k in ("persona_id", "version", "checksum")}
            if not all(isinstance(v, str) and v for v in fields.values()):
                return {}
            if persona_install_consent_identity(**fields) != ident:
                return {}
            out[ident] = rec
        return out

    def _persist_locked(self, candidate: dict[str, dict[str, Any]]) -> None:
        # Caller must hold _LEDGER_LOCK.
        from atomic_io import PRIVATE, atomic_write_text
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self.path,
            json.dumps({"schema_version": _SCHEMA_VERSION, "acks": candidate},
                       indent=2, sort_keys=True) + "\n",
            mode=PRIVATE,
        )

    def is_acked(self, identity: str) -> bool:
        with _LEDGER_LOCK:
            return identity in self._load()

    def record(self, *, identity: str, persona_id: str, version: str, checksum: str) -> None:
        rec = {"persona_id": persona_id, "version": version, "checksum": checksum,
               "ts": int(time.time())}
        with _LEDGER_LOCK:
            candidate = dict(self._load(strict_read=True))
            candidate[identity] = rec
            self._persist_locked(candidate)


def commit_persona_install(
    *, inspection: PersonaInspectionResult, acks: "PersonaInstallAckStore",
    personas_root: "Path | None" = None,
) -> "PersonaPack":
    import os
    import shutil

    if personas_root is None:
        personas_root = installed_personas_root()

    from persona_pack import PersonaPackError, load_persona_pack

    identity = persona_install_consent_identity(
        persona_id=inspection.persona_id, version=inspection.version, checksum=inspection.checksum)
    if not acks.is_acked(identity):
        raise SpecialistInstallError(
            "consent_missing", "no recorded operator approval for this persona install")

    # F1: validate the path segments BEFORE joining them into `dest`.
    validate_persona_path_segments(inspection.persona_id, inspection.version)
    dest = personas_root / inspection.persona_id / inspection.version
    if (dest / "manifest.json").is_file():
        # Fix-round-1 (finding CRITICAL): `dest` is keyed by persona_id +
        # a MUTABLE version string, NOT a content digest (unlike the
        # specialist CAS, where path == digest makes "exists" imply
        # "correct"). So "dest already exists" is NOT proof that what's
        # there matches this inspection's approved bytes — a persona_id@
        # version can be re-committed with genuinely different content
        # (e.g. an operator edited the repo but forgot to bump version).
        # Fail CLOSED: reload what's actually on disk and compare its
        # checksum to the approved inspection.checksum before ever
        # returning it. Never silently substitute the stale on-disk pack
        # for the just-approved one, and never silently overwrite an
        # existing version's bytes either — versions are immutable, so a
        # genuine content change must bump the version, not clobber `dest`.
        try:
            existing_pack = load_persona_pack(dest / "pack", dest / "manifest.json")
        except (PersonaPackError, OSError) as exc:
            # dest exists but is unreadable/corrupt — do not attempt to
            # repair or replace it automatically; that would risk masking
            # a tampered or half-written directory. Fail closed with the
            # same typed error, disclosing that manual removal is required.
            raise SpecialistInstallError(
                "version_content_conflict",
                f"{inspection.persona_id}@{inspection.version} already exists at {dest} "
                f"but is corrupt or unreadable ({exc}); manual removal of the local copy "
                "is required before retrying this install") from exc
        if existing_pack.checksum != inspection.checksum:
            raise SpecialistInstallError(
                "version_content_conflict",
                f"{inspection.persona_id}@{inspection.version} already exists locally with "
                f"different content (on-disk checksum {existing_pack.checksum!r} != approved "
                f"{inspection.checksum!r}); re-publish under a new version — an existing "
                "persona version's bytes are never silently replaced")
        # Genuine idempotent re-commit: on-disk content matches what was
        # just approved, so returning it is correct (round-3's original
        # short-circuit, now justified by an actual checksum comparison
        # rather than mere path existence).
        _reclaim_inspection_staging(inspection.staged_dir)   # #306
        return existing_pack

    # Round-3 fix (finding #1): this was the ONE commit path in the
    # whole plan that copied inspection-time bytes straight into their
    # FINAL location with NO verification step at all — no reload, no
    # re-derived checksum, nothing between "operator approved this
    # checksum" and "these bytes are now live at `dest`". Mirror the
    # specialist pipeline: stage into a TEMP directory under
    # `personas_root`, reload + recompute the checksum from THOSE
    # staged bytes, compare to `inspection.checksum`, and only then
    # atomically `os.replace` the verified temp directory into `dest`.
    # A mismatch/failure leaves no partial or wrong-checksum content at
    # `dest`.
    staging_parent = personas_root / ".staging"
    staging_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging_dest = staging_parent / uuid.uuid4().hex
    staging_dest.mkdir(parents=True, mode=0o700)
    try:
        shutil.copytree(inspection.staged_dir / "pack", staging_dest / "pack")
        shutil.copy2(inspection.staged_dir / "manifest.json", staging_dest / "manifest.json")
        staged_pack = load_persona_pack(staging_dest / "pack", staging_dest / "manifest.json")
        if (staged_pack.persona_id != inspection.persona_id
                or staged_pack.version != inspection.version
                or staged_pack.checksum != inspection.checksum):
            raise SpecialistInstallError(
                "checksum_changed",
                "staged persona no longer matches the approved inspection")
    except Exception:
        shutil.rmtree(staging_dest, ignore_errors=True)
        raise
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(staging_dest, dest)
    except OSError as exc:
        # Publication race (Sol P2, #217): two concurrent commits of the SAME
        # persona_id@version can both miss the is_file() precheck above, both
        # stage into their own temp dir, then race here — the loser's
        # `os.replace` onto a now-populated `dest` directory fails (POSIX
        # rename requires an empty destination directory: ENOTEMPTY). Never
        # leak the losing staging dir or surface a raw OSError. Clean up, then
        # reconcile against what actually got published: identical content
        # (same persona_id@version+checksum — which is all that could have
        # won this exact race) is an IDEMPOTENT success; different content
        # surfaces the SAME typed version_content_conflict the pre-race
        # is_file() branch raises. Mirrors the specialist path's fail-closed
        # `_refuse_if_active_present` guard against a concurrent winner.
        shutil.rmtree(staging_dest, ignore_errors=True)
        try:
            published = load_persona_pack(dest / "pack", dest / "manifest.json")
        except (PersonaPackError, OSError) as read_exc:
            raise SpecialistInstallError(
                "version_content_conflict",
                f"{inspection.persona_id}@{inspection.version}: a concurrent "
                f"publish raced this commit and the winning copy is unreadable "
                f"({read_exc}); manual removal of the local copy is required "
                "before retrying this install") from exc
        if published.checksum != inspection.checksum:
            raise SpecialistInstallError(
                "version_content_conflict",
                f"{inspection.persona_id}@{inspection.version} was published "
                f"concurrently with different content (on-disk checksum "
                f"{published.checksum!r} != approved {inspection.checksum!r}); "
                "re-publish under a new version — an existing persona version's "
                "bytes are never silently replaced") from exc
        _reclaim_inspection_staging(inspection.staged_dir)   # #306
        return published
    _reclaim_inspection_staging(inspection.staged_dir)   # #306
    return load_persona_pack(dest / "pack", dest / "manifest.json")


def apply_persona_override(
    *, target_role_id: str, persona: "PersonaPack", role: "RoleSlot", instance_dir_root: Path,
) -> Any:
    """Generalizes Task 8's resident_persona_swap for ANY persona-bearing
    agent — reuses check_persona_requirements + materialize_override_binding
    + InstanceDir exactly as Task 8 does; residents pass
    instance_dir_root=BINDINGS_ROOT/f"resident-{role.slot}",
    installed specialists pass SPECIALISTS_ROOT/role.slot (the
    SAME InstanceDir tree Task N1b-ii already writes to).

    Round-2 fix (finding #4): `InstanceTuple.root` means DIFFERENT things for
    the two tiers. For a resident it is a free-form descriptive label — role
    artifacts always load from the fixed image tree, never from `root` —
    so Plan 1's own resident_persona_swap already sets root=override_source
    and that is fine, unchanged here. For a specialist, `root` is
    STRUCTURALLY PARSED by activate_binding_for_config
    (parse_component_root) to locate the component's role artifact AND its
    bundled default persona in the CAS store — overwriting it with a bare
    persona ref (no "#sha256:..." suffix) makes parse_component_root raise
    ValueError on the very next load, and silently drops the existing
    config_snapshot/dependency_digests. A specialist override must keep
    `root` pointed at the component and carry the override ELSEWHERE on the
    binding (mode="override" + override_source — BindingRecord already has
    both fields; this function only needed to stop clobbering `root`)."""
    from personality_binding import (
        InstanceDir, InstanceTuple, check_persona_requirements, materialize_override_binding,
    )
    import specialist_materialize

    check_persona_requirements(role.normalized, persona)
    override_source = f"{persona.persona_id}@{persona.version}"
    instance_dir = InstanceDir(instance_dir_root)

    # Round-5 fix (F2, the MISSED InstanceDir writer): both branches below write
    # an InstanceDir (stage_desired + commit_desired_to_active) — the SAME
    # personality-instance mutations every specialist_install.py writer performs
    # under specialist_materialize.MATERIALIZE_LOCK. They must take that lock too
    # (its invariant is: NO InstanceDir write, resident OR specialist tree, ever
    # happens outside it). LOOP-SAFETY: this function's sole production caller,
    # tools.persona_apply, offloads it via asyncio.to_thread, so it always runs
    # in a WORKER THREAD — acquiring the threading.Lock synchronously here never
    # blocks the event loop.

    if role.kind != "specialist":
        # Resident path — root is descriptive only. F2b: extend the SAME lock to
        # the resident InstanceDir write for uniformity, so a concurrent
        # tools._stage_and_report resident swap (also offloaded to a worker
        # thread) can never interleave desired.yaml writes on the same resident.
        binding = materialize_override_binding(
            role=role, persona=persona, override_source=override_source)
        with specialist_materialize.MATERIALIZE_LOCK:
            instance_dir.stage_desired(InstanceTuple(
                root=override_source, binding=binding, config_snapshot={},
                config_digest=binding.effective_config_digest,
            ))
            return instance_dir.commit_desired_to_active()

    # Specialist path — root MUST stay the component root; config/dependency
    # state carries forward from whatever is currently active.
    # F1 (confirm): role.slot is loader-validated (role_artifact enforces
    # role.v1.json's slot pattern), but the caller-built `instance_dir_root`
    # is derived from that slot upstream — re-assert the canonical slug shape
    # so a hostile target can never index the specialist InstanceDir tree.
    from specialist_install import validate_specialist_slug, _require_active_unchanged
    validate_specialist_slug(role.slot)
    active_before = instance_dir.active()
    if active_before is None:
        raise SpecialistInstallError(
            "no_active_tuple",
            f"{target_role_id!r} has no active installed component to apply an override to")
    # materialize_override_binding (Plan 1 Task 7) hard-defaults
    # dependency_digests=()/EMPTY_CONFIG_DIGEST — correct for a resident (no
    # dependency closure exists there) but wrong for a specialist, whose
    # existing dependency/config state must survive an override swap
    # unchanged. Round-2 (finding #4) extends the Plan 1 signature with two
    # optional keyword-only params (already landed in N1c — see
    # personality_binding.materialize_override_binding), additive and
    # defaulted so every existing resident call site is unaffected. The binding
    # is built from `active_before` OUTSIDE the lock (read-only work); the
    # in-lock revalidation below guarantees that snapshot is still current.
    binding = materialize_override_binding(
        role=role, persona=persona, override_source=override_source,
        dependency_digests=active_before.binding.dependency_digests,
        effective_config_digest=active_before.binding.effective_config_digest,
    )
    # F2a: re-read + stage + commit under MATERIALIZE_LOCK. `active_before` was
    # read BEFORE the lock; a concurrent uninstall (resurrection — staging here
    # would recreate a just-removed InstanceDir), a config-only upgrade, or
    # another override may have committed a different active while we built the
    # binding and blocked on the lock. `_require_active_unchanged` re-reads the
    # active tuple in-lock and refuses (typed concurrent_mutation) unless it is
    # byte-for-byte `active_before` — which also subsumes the vanished-active
    # (no_active_tuple) case, since a removed active re-reads as None. Never
    # commit this override over a concurrent winner, and never overwrite it with
    # a binding derived from the now-stale `active_before`.
    with specialist_materialize.MATERIALIZE_LOCK:
        _require_active_unchanged(instance_dir, active_before, slug=role.slot)
        instance_dir.stage_desired(InstanceTuple(
            root=active_before.root,                       # UNCHANGED — still the component root
            binding=binding,
            config_snapshot=active_before.config_snapshot,  # UNCHANGED — override never touches config
            config_digest=active_before.config_digest,
        ))
        return instance_dir.commit_desired_to_active()
