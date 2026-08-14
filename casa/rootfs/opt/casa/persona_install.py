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
    "PersonaLedgerInvalid",
    "PersonaReference",
    "commit_persona_install",
    "apply_persona_override",
    "validate_persona_path_segments",
    "persona_references",
    "require_persona_present",
    "list_installed_personas",
    "remove_installed_persona",
    "prune_installed_personas",
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


class PersonaLedgerInvalid(Exception):
    """#543: the ack ledger's ``revocations`` map is present but malformed.

    Distinct from the ledger's ordinary fail-closed read (a content-invalid
    ledger reads as "no acks", which manufactures no consent). A revocation
    generation that cannot be read is the opposite polarity: treating it as 0
    would let a stale consent tap re-record past a completed revoke, and
    incrementing from an unknown base could move a generation BACKWARDS. Both
    mutation paths (``record``/``revoke``) therefore refuse outright."""


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

    def _require_valid_document(self) -> None:
        """#543 (Sol diff r1): a MUTATION that carries a revocation generation
        must refuse a ledger document it cannot interpret, instead of reading
        it as the empty baseline.

        The polarity matters and it is the opposite of ``_load``'s. For the
        ACKS, "unreadable ⇒ empty" manufactures no consent, so it is safe and
        it is what lets the next successful record rewrite a valid store. For
        the GENERATIONS it is the other way round: a document that reads as
        empty reports generation 0, so a consent tap that captured 0 before a
        completed revoke would match and re-record the approval that revoke
        removed — and the same empty read would silently drop every unrelated
        ack the file still held. Refuse both mutations instead. Nothing is
        authorized in the meantime: ``is_acked`` already reads a damaged
        document as no acks at all."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return                      # no ledger yet — a clean baseline
        except ValueError as exc:
            raise PersonaLedgerInvalid(
                f"{self.path}: the consent ledger is not readable JSON ({exc}); "
                "refusing to record or revoke consent against it") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA_VERSION:
            raise PersonaLedgerInvalid(
                f"{self.path}: the consent ledger has an unrecognized shape or "
                "schema version; refusing to record or revoke consent against it")
        if self._load() != (raw.get("acks") or {}):
            raise PersonaLedgerInvalid(
                f"{self.path}: the consent ledger's ack records do not validate; "
                "refusing to record or revoke consent against it")

    def _load_revocations(self, *, strict_read: bool = False) -> dict[str, int]:
        """#543: the revocation-generation map, read from the SAME ledger file
        under the SAME lock as the acks.

        Absent map ⇒ ``{}`` (the upgrade baseline: every generation is 0 —
        no revoke has ever happened). Present-but-malformed ⇒
        ``PersonaLedgerInvalid``, never a silent 0 (see that class). The map
        deliberately lives under the UNCHANGED ``schema_version: 1``: bumping
        the version would make ``_load``'s version check discard every
        recorded ack on the upgrading boot."""
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
        revocations = raw.get("revocations")
        if revocations is None:
            return {}
        if not isinstance(revocations, dict) or not all(
                isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and v >= 0
                for k, v in revocations.items()):
            raise PersonaLedgerInvalid(
                f"{self.path}: the revocations map is malformed; refusing every "
                "consent record and revoke until it is repaired")
        return dict(revocations)

    def _persist_locked(self, candidate: dict[str, dict[str, Any]],
                        revocations: dict[str, int]) -> None:
        # Caller must hold _LEDGER_LOCK. #543: acks and revocation generations
        # are persisted by ONE atomic replacement — there is no crash point at
        # which a revoke has dropped an ack but not yet advanced its
        # generation (or the reverse).
        from atomic_io import PRIVATE, atomic_write_text
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {"schema_version": _SCHEMA_VERSION, "acks": candidate}
        if revocations:
            payload["revocations"] = revocations
        atomic_write_text(
            self.path,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            mode=PRIVATE,
        )

    def is_acked(self, identity: str) -> bool:
        with _LEDGER_LOCK:
            return identity in self._load()

    def revocation_generations(self, *, persona_id: str, version: str) -> tuple[int, int]:
        """#543: the (WILDCARD, PER-VERSION) generation pair a consent prompt
        captures and ``record`` re-checks.

        Two generations, not one, because a wildcard revoke
        (``revoke(persona_id, version=None)``) has nothing to increment for a
        version that is only PENDING — a prompt that has not recorded an ack
        yet appears in neither map, so a per-version generation alone leaves
        exactly that prompt able to re-create the removed approval (Sol+Terra
        design round 3, the single finding they converged on)."""
        with _LEDGER_LOCK:
            revocations = self._load_revocations()
        return (revocations.get(persona_id, 0),
                revocations.get(f"{persona_id}@{version}", 0))

    def record(self, *, identity: str, persona_id: str, version: str, checksum: str,
               expect_generations: "tuple[int, int] | None" = None) -> bool:
        """Record an approval. Returns True when it was written.

        #543: with *expect_generations* (the pair captured when the keyboard
        was posted), the write happens ONLY if neither generation has moved —
        i.e. no revoke for this persona, or for this exact version, landed
        while the operator's tap was in flight. Returns False otherwise,
        writing nothing; the consent finish hook turns that into "re-run the
        install". Without the argument the write is unconditional (the
        pre-#543 contract every direct caller still relies on)."""
        rec = {"persona_id": persona_id, "version": version, "checksum": checksum,
               "ts": int(time.time())}
        with _LEDGER_LOCK:
            if expect_generations is not None:
                # Only the generation-checked path refuses a damaged document:
                # a direct record (no generations) keeps the siblings'
                # documented corruption recovery, where the next successful
                # write rewrites a valid store.
                self._require_valid_document()
            revocations = self._load_revocations(strict_read=True)
            if expect_generations is not None:
                current = (revocations.get(persona_id, 0),
                           revocations.get(f"{persona_id}@{version}", 0))
                if current != tuple(expect_generations):
                    return False
            candidate = dict(self._load(strict_read=True))
            candidate[identity] = rec
            self._persist_locked(candidate, revocations)
            return True

    def revoke(self, *, persona_id: str, version: "str | None" = None) -> list[dict[str, Any]]:
        """#543: drop every recorded approval for *persona_id* (all versions
        when *version* is None, that one version otherwise) and ADVANCE the
        matching revocation generation(s), so a consent tap already in flight
        can no longer re-record the approval this call just removed.

        Returns exactly the records removed — the same shape
        ``uninstall_specialist`` journals for its own ack retirement."""
        with _LEDGER_LOCK:
            # A revoke must never rewrite a document it could not interpret:
            # doing so would drop every ack the file still validly held and
            # persist a generation derived from an unknown base.
            self._require_valid_document()
            revocations = self._load_revocations(strict_read=True)
            acks = dict(self._load(strict_read=True))
            removed: list[dict[str, Any]] = []
            keep: dict[str, dict[str, Any]] = {}
            for ident, rec in acks.items():
                matches = rec.get("persona_id") == persona_id and (
                    version is None or rec.get("version") == version)
                if matches:
                    removed.append(rec)
                else:
                    keep[ident] = rec
            if version is None:
                # The wildcard generation covers pending prompts for versions
                # that appear nowhere in the ledger yet; the per-version bumps
                # cover the ones that do (a prompt captured both, and either
                # moving is enough to refuse it).
                bump = {persona_id}
                bump |= {f"{persona_id}@{rec['version']}" for rec in removed
                         if isinstance(rec.get("version"), str)}
                bump |= {key for key in revocations
                         if key == persona_id or key.startswith(f"{persona_id}@")}
            else:
                bump = {f"{persona_id}@{version}"}
            for key in bump:
                revocations[key] = revocations.get(key, 0) + 1
            self._persist_locked(keep, revocations)
            return removed


def commit_persona_install(
    *, inspection: PersonaInspectionResult, acks: "PersonaInstallAckStore",
    personas_root: "Path | None" = None,
) -> "PersonaPack":
    """#543 (Sol design r1): the publication decision runs under
    ``MATERIALIZE_LOCK``, and the operator's approval is re-read INSIDE it.

    Before this, the ack was checked once and the bytes were published later
    with nothing serializing the two: a ``persona_remove`` that revoked the
    approval and deleted the directory in between was silently undone by the
    in-flight install, which republished the removed persona on an
    authorization the operator had already withdrawn. Removal revokes and
    deletes under the SAME lock, so holding it here makes the two orders both
    correct — remove-first ⇒ this re-check finds no ack and refuses;
    commit-first ⇒ removal's reference scan and revoke run against a
    published persona, exactly as if the install had finished earlier."""
    import specialist_materialize

    if personas_root is None:
        personas_root = installed_personas_root()

    identity = persona_install_consent_identity(
        persona_id=inspection.persona_id, version=inspection.version, checksum=inspection.checksum)
    if not acks.is_acked(identity):
        raise SpecialistInstallError(
            "consent_missing", "no recorded operator approval for this persona install")

    # LOOP-SAFETY: the sole production caller (tools.persona_install_commit)
    # offloads this via asyncio.to_thread, so acquiring the threading lock
    # here never blocks the event loop.
    with specialist_materialize.MATERIALIZE_LOCK:
        return _commit_persona_install_locked(
            inspection=inspection, acks=acks, personas_root=personas_root, identity=identity)


def _commit_persona_install_locked(
    *, inspection: PersonaInspectionResult, acks: "PersonaInstallAckStore",
    personas_root: Path, identity: str,
) -> "PersonaPack":
    import os
    import shutil

    from persona_pack import PersonaPackError, load_persona_pack

    # #543: the authoritative consent read. The pre-lock check above is a fast
    # refusal; THIS one is the one that counts, because no revoke can land
    # between it and the publication below while this lock is held.
    if not acks.is_acked(identity):
        raise SpecialistInstallError(
            "consent_missing",
            "the operator's approval for this persona install was revoked while "
            "the install was in flight; nothing was published")

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
                f"but is corrupt or unreadable ({exc}); remove the local copy with "
                "persona_remove before retrying this install") from exc
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
                f"({read_exc}); remove the local copy with persona_remove "
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
        InstanceDir, check_persona_requirements, make_instance_tuple,
        materialize_override_binding,
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
            require_persona_present(binding)   # #543
            instance_dir.stage_desired(make_instance_tuple(
                root=override_source, binding=binding, config_snapshot={},
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
        require_persona_present(binding)   # #543
        instance_dir.stage_desired(make_instance_tuple(
            root=active_before.root,                       # UNCHANGED — still the component root
            binding=binding,
            config_snapshot=active_before.config_snapshot,  # UNCHANGED — override never touches config
        ))
        return instance_dir.commit_desired_to_active()


# ---------------------------------------------------------------------------
# #543 — the disposal side: references, list, remove, prune
#
# Design rounds (Sol+Terra, 3): the whole subsystem turns on ONE question —
# is this persona's directory still needed by something that a later load, or
# a later RECOVERY, will read? Deleting bytes an active resident binding pins
# is boot-fatal (agent_loader._activate_resident_binding reloads the override
# pack on every boot and INV-PERS-003 makes a resident load failure fatal), and
# the failure is silent until the next restart. So the reference set is
# computed fail-closed, and removal refuses rather than forcing.
# ---------------------------------------------------------------------------


def persona_pack_roots() -> tuple[Path, ...]:
    """The approved persona roots, installed-first — the SAME order (and the
    same seams) every persona consumer resolves through. ``tools._persona_roots``
    delegates here so there is one authority for "where a persona may live"."""
    import agent_loader

    return (installed_personas_root(),
            Path(agent_loader.SCHEMA_DIR).parent / "personas")


@dataclass(frozen=True, slots=True)
class PersonaReference:
    """One reason a persona's bytes must stay on disk."""
    ref: str          # "<persona_id>@<version>"
    referrer: str     # "resident:<slot>" | "specialist:<slug>" | "journal:<file>"
    source: str       # the file the reference was read from


# Resident InstanceDirs contribute active + desired ONLY. `active.prior.yaml`
# is deliberately excluded there: nothing in this tree reads a RESIDENT prior
# (the sole prior consumer is specialist_install.rollback_specialist, which is
# specialist-only), and counting it would pin the outgoing persona forever
# after a reset — commit_desired_to_active rotates the old active into prior,
# so reset+restart would never free the bytes it exists to free (Sol design
# round 2, verified against personality_binding.commit_desired_to_active).
_RESIDENT_TUPLE_FILES = ("active.yaml", "desired.yaml")
# Specialists add prior (rollback_specialist's input) AND the pending-rotation
# temp: a failed `tmp -> active.prior.yaml` rotation leaves the OLD tuple at
# `active.yaml.rollback-tmp`, and a later no-op recommit promotes it to prior,
# where rollback then reads it (Terra design round 2).
_SPECIALIST_TUPLE_FILES = (
    "active.yaml", "desired.yaml", "active.prior.yaml", "active.yaml.rollback-tmp")
_SPECIALIST_NON_SLUG_DIRS = frozenset({"store", ".staging", ".bundle-staging",
                                       ".roles-overlay", ".ops"})


def _override_ref(tup: Any) -> "str | None":
    if tup is None or tup.binding.mode != "override":
        return None
    return f"{tup.binding.persona_id}@{tup.binding.persona_version}"


def _journal_references(ops_dir: Path) -> "list[PersonaReference]":
    """Persona references held by bundle journals that would still be REPLAYED
    (Sol design round 1).

    A journal captures the slug's tuple files before the mutation and
    ``BundleTxn.rollback_disk`` writes those exact bytes back — from the tool
    layer's compensation path AND from ``specialist_bundle_journal.reconcile_boot``
    on the next boot. So a tuple that is not on disk right now can still be
    restored later: the visible files are not the whole reference universe.

    Which journals those are is NOT decided here — ``replayable_tuple_files``
    in the journal module is the authority, so this scan and boot cannot drift
    apart (Terra diff r1: the duplicated predicate treated any non-complete
    journal as replayable, while boot quarantines an invalid one without ever
    restoring it, so removals were refused for a state nothing could replay).

    An OSError is the one thing that refuses: a file that cannot be READ now
    may read fine at boot, so its replayability is unknown rather than
    negative."""
    import yaml
    import specialist_bundle_journal
    from personality_binding import verify_instance_tuple

    out: list[PersonaReference] = []
    try:
        if not ops_dir.is_dir():
            return out
        entries = sorted(ops_dir.iterdir())
    except OSError as exc:
        raise SpecialistInstallError(
            "references_unavailable",
            f"the bundle journal directory {ops_dir} could not be read ({exc}); a "
            "journal there may still restore a persona binding, so no persona can "
            "be removed until it is readable") from exc
    for path in entries:
        try:
            if not path.is_file():
                continue
            tuple_files = specialist_bundle_journal.replayable_tuple_files(path)
        except OSError as exc:
            raise SpecialistInstallError(
                "references_unavailable",
                f"bundle journal {path.name} could not be read ({exc}); it may still "
                "restore a persona binding, so no persona can be removed until it is "
                "readable") from exc
        if not tuple_files:
            continue
        for filename, content in sorted(tuple_files.items()):
            if not isinstance(content, str) or not content.strip():
                continue
            try:
                tup = verify_instance_tuple(yaml.safe_load(content))
            except Exception:  # noqa: BLE001
                # A captured file that does not parse cannot be restored into a
                # tuple any loader will read either — the restore writes the
                # same unparseable bytes back. It pins nothing. (Unlike a file
                # ON DISK, whose read failure may be transient — see _scan.)
                continue
            ref = _override_ref(tup)
            if ref is not None:
                out.append(PersonaReference(
                    ref=ref, referrer=f"journal:{path.name}", source=filename))
    return out


def persona_references(
    *, bindings_dir: "Path | None" = None, specialists_dir: "Path | None" = None,
    ops_dir: "Path | None" = None,
) -> dict[str, list[PersonaReference]]:
    """Every reason an installed persona's bytes must stay on disk, keyed by
    ``"<persona_id>@<version>"``.

    Roots resolve through the SAME seams the loaders read — the resident
    bindings root through ``agent_loader._resident_bindings_root`` (#323's
    scar: a hard-coded ``/config/bindings`` here would compute a retention set
    for a directory nothing boots from), the specialists root through the
    literal the specialist loader itself uses
    (``specialist_install.activate_binding_for_config``), and the journal root
    through ``specialist_bundle_journal.OPS_DIR``.

    Raises ``SpecialistInstallError("references_unavailable")`` when the set
    cannot be computed completely."""
    import agent_loader
    import specialist_bundle_journal
    from personality_binding import load_instance_tuple

    if bindings_dir is None:
        bindings_dir = agent_loader._resident_bindings_root(None)
    if specialists_dir is None:
        specialists_dir = Path("/config/specialists")
    if ops_dir is None:
        ops_dir = specialist_bundle_journal.OPS_DIR

    refs: dict[str, list[PersonaReference]] = {}

    def _add(ref: str, referrer: str, source: str) -> None:
        refs.setdefault(ref, []).append(
            PersonaReference(ref=ref, referrer=referrer, source=source))

    def _scan(root: Path, *, filenames: tuple[str, ...], kind: str,
              skip: frozenset[str] = frozenset()) -> None:
        try:
            if not root.is_dir():
                return
            entries = sorted(root.iterdir())
        except OSError as exc:
            raise SpecialistInstallError(
                "references_unavailable",
                f"{root} could not be read ({exc}); the bindings it holds may still "
                "name a persona, so no persona can be removed until it is readable"
            ) from exc
        for entry in entries:
            if not entry.is_dir() or entry.name in skip:
                continue
            name = entry.name[len("resident-"):] if kind == "resident" else entry.name
            for filename in filenames:
                path = entry / filename
                if not path.is_file():
                    continue
                # Sol diff r1 (S1): a tuple file that EXISTS but cannot be
                # interpreted is NOT evidence of "no reference" — and
                # load_instance_tuple RAISES (it never returns None for a file
                # that exists) on a read error, a YAML error, a schema
                # violation or the pre-guard tombstone. Skipping it silently
                # un-pinned a persona a resident's unchanged active binding
                # still named: once a transient read failure cleared, the
                # binding was readable again and pointed at bytes this removal
                # had deleted — a resident that cannot boot. Refuse instead;
                # the refusal names the file, which is also the only way the
                # operator learns the tuple is damaged.
                try:
                    tup = load_instance_tuple(path)
                except Exception as exc:  # noqa: BLE001
                    raise SpecialistInstallError(
                        "references_unavailable",
                        f"{path} exists but could not be read ({exc}); it may name a "
                        "persona, so no persona can be removed until it is resolved"
                    ) from exc
                ref = _override_ref(tup)
                if ref is not None:
                    _add(ref, f"{kind}:{name}", filename)

    _scan(bindings_dir, filenames=_RESIDENT_TUPLE_FILES, kind="resident")
    _scan(specialists_dir, filenames=_SPECIALIST_TUPLE_FILES, kind="specialist",
          skip=_SPECIALIST_NON_SLUG_DIRS)
    for journal_ref in _journal_references(Path(ops_dir)):
        refs.setdefault(journal_ref.ref, []).append(journal_ref)
    return refs


def require_persona_present(binding: Any, *, roots: "tuple[Path, ...] | None" = None) -> None:
    """#543: re-prove an override binding's persona is on disk with the bytes
    the binding pins — called INSIDE ``MATERIALIZE_LOCK``, immediately before
    the InstanceDir write.

    Both application paths resolve the pack BEFORE taking the lock, so without
    this a removal could delete the directory in that window and the commit
    would pin bytes that no longer exist — boot-fatal for a resident. Removal
    computes its reference set and deletes under the same lock, so the two
    orders are now both safe: apply-first ⇒ removal sees the tuple and
    refuses; remove-first ⇒ this refuses and nothing is staged."""
    from persona_pack import PersonaPackError, load_persona_pack

    if getattr(binding, "mode", None) != "override":
        return
    ref = f"{binding.persona_id}@{binding.persona_version}"
    for root in (roots if roots is not None else persona_pack_roots()):
        base = root / binding.persona_id / binding.persona_version
        if not (base / "manifest.json").is_file():
            continue
        try:
            pack = load_persona_pack(base / "pack", base / "manifest.json")
        except (PersonaPackError, OSError) as exc:
            raise SpecialistInstallError(
                "persona_unavailable",
                f"persona {ref} became unreadable while this change was being "
                f"applied ({exc}); nothing was staged") from exc
        if pack.checksum != binding.persona_checksum:
            raise SpecialistInstallError(
                "persona_unavailable",
                f"persona {ref} on disk now has checksum {pack.checksum!r}, not the "
                f"{binding.persona_checksum!r} this binding pins; nothing was staged")
        return
    raise SpecialistInstallError(
        "persona_unavailable",
        f"persona {ref} was removed while this change was being applied; nothing "
        "was staged")


def _installed_versions(personas_root: Path) -> "list[tuple[str, str, Path]]":
    """Every ``(persona_id, version, directory)`` under the installed root.
    ``.staging`` and anything whose id/version segments are not the canonical
    shape are skipped — they are not installed personas and are not this
    subsystem's to delete."""
    out: list[tuple[str, str, Path]] = []
    if not personas_root.is_dir():
        return out
    for namespace in sorted(personas_root.iterdir()):
        if not namespace.is_dir() or namespace.name.startswith("."):
            continue
        for name in sorted(namespace.iterdir()):
            if not name.is_dir():
                continue
            persona_id = f"{namespace.name}/{name.name}"
            for version in sorted(name.iterdir()):
                if not version.is_dir():
                    continue
                try:
                    validate_persona_path_segments(persona_id, version.name)
                except SpecialistInstallError:
                    continue
                out.append((persona_id, version.name, version))
    return out


def list_installed_personas(
    *, personas_root: "Path | None" = None,
    references: "dict[str, list[PersonaReference]] | None" = None,
    acks: "PersonaInstallAckStore | None" = None,
) -> list[dict[str, Any]]:
    """Enumerate the installed personas with what refers to each one.

    A directory whose pack does not load is REPORTED (``invalid``), never
    omitted: a corrupt install is precisely what an operator needs to see, and
    it is what ``commit_persona_install`` tells them to remove."""
    from persona_pack import PersonaPackError, load_persona_pack

    if personas_root is None:
        personas_root = installed_personas_root()
    if references is None:
        references = persona_references()
    if acks is None:
        acks = PersonaInstallAckStore()

    out: list[dict[str, Any]] = []
    for persona_id, version, directory in _installed_versions(personas_root):
        ref = f"{persona_id}@{version}"
        entry: dict[str, Any] = {
            "persona_id": persona_id, "version": version, "ref": ref,
            "referenced_by": [{"referrer": r.referrer, "source": r.source}
                              for r in references.get(ref, ())],
        }
        try:
            pack = load_persona_pack(directory / "pack", directory / "manifest.json")
        except (PersonaPackError, OSError) as exc:
            entry.update(checksum=None, display_name=None, acked=False,
                         invalid=str(exc))
        else:
            entry.update(
                checksum=pack.checksum,
                display_name=pack.identity.get("display_name", persona_id),
                acked=acks.is_acked(persona_install_consent_identity(
                    persona_id=persona_id, version=version, checksum=pack.checksum)),
                invalid=None)
        entry["removable"] = not entry["referenced_by"]
        out.append(entry)
    return out


def _remove_installed_persona_locked(
    *, persona_id: str, version: str, personas_root: Path,
    references: dict[str, list[PersonaReference]], acks: "PersonaInstallAckStore",
) -> dict[str, Any]:
    """Caller must hold ``MATERIALIZE_LOCK`` (it is a plain, NON-REENTRANT
    ``threading.Lock``, so prune acquires it once and calls this per
    candidate — Terra design round 2). Returns a result dict rather than
    raising, so a sweep can report per-entry refusals."""
    import os
    import shutil

    ref = f"{persona_id}@{version}"
    validate_persona_path_segments(persona_id, version)

    root = Path(os.path.realpath(personas_root))
    dest = personas_root / persona_id / version
    resolved = Path(os.path.realpath(dest))
    # Defense in depth behind the segment patterns: only a direct
    # <root>/<namespace>/<name>/<version> directory is ever removable, so no
    # symlink under the tree can redirect the delete out of it.
    if resolved.parent.parent.parent != root:
        return {"ok": False, "ref": ref, "kind": "outside_installed_root",
                "detail": f"{dest} does not resolve to a directory directly under {root}"}
    if not (dest / "manifest.json").is_file() and not dest.is_dir():
        return {"ok": False, "ref": ref, "kind": "not_installed",
                "detail": f"{ref} is not installed under {root} (image-shipped "
                          "personas are part of the image and are never removed)"}

    referrers = references.get(ref, [])
    if referrers:
        return {"ok": False, "ref": ref, "kind": "persona_pinned",
                "referenced_by": [{"referrer": r.referrer, "source": r.source}
                                  for r in referrers],
                "detail": (f"{ref} is still bound by "
                           + ", ".join(sorted({r.referrer for r in referrers}))
                           + " — reset or re-apply those agents (and restart, for a "
                             "resident) before removing it")}

    # Revoke BEFORE deleting (both reviewers, design round 1): if the delete
    # then fails, the bytes are still there but can no longer be installed on
    # the old approval — one re-approval. The inverse order can leave a live
    # approval for bytes that are gone.
    try:
        revoked = acks.revoke(persona_id=persona_id, version=version)
    except (OSError, PersonaLedgerInvalid) as exc:
        return {"ok": False, "ref": ref, "kind": "ack_revoke_failed",
                "detail": f"could not revoke the install approval for {ref} ({exc}); "
                          "nothing was removed"}

    # Rename-then-delete: the rename is atomic, so no reader ever observes a
    # half-deleted pack. A crash between the two leaves a tree under
    # `.staging`, which the boot age sweep already reclaims.
    staging_parent = personas_root / ".staging"
    try:
        staging_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        trash = staging_parent / uuid.uuid4().hex
        os.replace(dest, trash)
    except OSError as exc:
        return {"ok": False, "ref": ref, "kind": "remove_failed",
                "detail": f"could not remove {dest} ({exc}); the install approval "
                          "for this persona was revoked and must be re-granted"}
    shutil.rmtree(trash, ignore_errors=True)
    # Prune the now-empty <persona_id> directories, newest level first. Never
    # recursive: rmdir refuses a non-empty directory, which is the guard.
    for parent in (dest.parent, dest.parent.parent):
        try:
            parent.rmdir()
        except OSError:
            break
    return {"ok": True, "ref": ref, "persona_id": persona_id, "version": version,
            "revoked_acks": len(revoked)}


def remove_installed_persona(
    *, persona_id: str, version: str, personas_root: "Path | None" = None,
    acks: "PersonaInstallAckStore | None" = None,
    bindings_dir: "Path | None" = None, specialists_dir: "Path | None" = None,
    ops_dir: "Path | None" = None,
) -> dict[str, Any]:
    """Remove ONE installed persona version, refusing whenever anything still
    refers to it. Raises ``SpecialistInstallError`` for every typed refusal."""
    import specialist_materialize

    if personas_root is None:
        personas_root = installed_personas_root()
    if acks is None:
        acks = PersonaInstallAckStore()
    validate_persona_path_segments(persona_id, version)
    with specialist_materialize.MATERIALIZE_LOCK:
        references = persona_references(
            bindings_dir=bindings_dir, specialists_dir=specialists_dir, ops_dir=ops_dir)
        result = _remove_installed_persona_locked(
            persona_id=persona_id, version=version, personas_root=personas_root,
            references=references, acks=acks)
    if not result["ok"]:
        raise SpecialistInstallError(result["kind"], result["detail"])
    return result


def prune_installed_personas(
    *, personas_root: "Path | None" = None, acks: "PersonaInstallAckStore | None" = None,
    bindings_dir: "Path | None" = None, specialists_dir: "Path | None" = None,
    ops_dir: "Path | None" = None,
) -> dict[str, Any]:
    """The GC sweep (spec §4.4's deferred half): remove every installed
    persona version nothing refers to.

    Deliberately operator-invoked rather than wired into boot — deleting
    consent-approved operator content with nobody in the loop is the silent
    class this codebase keeps paying for, and `persona_list` plus this tool is
    a complete remedy for the accumulation. One reference computation and one
    lock acquisition for the whole sweep, so the set cannot move mid-sweep."""
    import specialist_materialize

    if personas_root is None:
        personas_root = installed_personas_root()
    if acks is None:
        acks = PersonaInstallAckStore()
    removed: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    with specialist_materialize.MATERIALIZE_LOCK:
        references = persona_references(
            bindings_dir=bindings_dir, specialists_dir=specialists_dir, ops_dir=ops_dir)
        for persona_id, version, _directory in _installed_versions(personas_root):
            result = _remove_installed_persona_locked(
                persona_id=persona_id, version=version, personas_root=personas_root,
                references=references, acks=acks)
            (removed if result["ok"] else kept).append(result)
    return {"removed": removed, "kept": kept}
