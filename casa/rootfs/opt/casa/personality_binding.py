# casa/rootfs/opt/casa/personality_binding.py
from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Literal, Mapping

import jsonschema
import yaml

import plugin_registry
from canonical_bytes import checksum_json
from persona_pack import PersonaPack
from role_slot import (  # noqa: F401 — re-exported for existing callers (Task 6 owns these)
    EMPTY_CONFIG_DIGEST,
    RoleSlot,
    compute_effective_config_digest,
)
from trait_renderer import RENDERER_VERSION

_SCHEMA_DIR = Path(__file__).parent / "defaults" / "schema"

logger = logging.getLogger(__name__)

# THE PERSONALITY-INSTANCE MUTATION LOCK (whole-branch review round 6, F1).
# Defined HERE — its true home — because it guards the InstanceDir state whose
# type (`InstanceDir`, below) and read/write primitives live in this module. It
# covers BOTH instance trees: the resident tree (/config/bindings/resident-<slot>/,
# written by `reconcile_resident_binding` below + `tools._stage_and_report` +
# `persona_install.apply_persona_override`'s resident branch) AND the specialist
# tree (/config/specialists/<slug>/, written by `specialist_install.py` +
# `specialist_materialize.py`). `specialist_materialize` re-exports this same object
# as `specialist_materialize.MATERIALIZE_LOCK`, so every historical reference keeps
# working; the FULL writer catalog + deadlock/loop-safety analysis lives in
# `specialist_materialize.py`'s header comment. Invariant: NO InstanceDir write
# (stage_desired / commit_desired_to_active / discard_desired) in EITHER tree ever
# happens outside this lock. LOOP-SAFETY: a non-reentrant `threading.Lock`, NEVER
# acquired synchronously on the asyncio event loop — every acquirer runs in a
# worker thread (or single-threaded boot init offloaded via `asyncio.to_thread`).
MATERIALIZE_LOCK = threading.Lock()

# NOTE: EMPTY_CONFIG_DIGEST / compute_effective_config_digest are defined in
# role_slot.py (Task 6), imported and re-exported here — NOT redefined. Task 6's
# own executor-loading wiring needs this constant BEFORE personality_binding.py
# exists under fresh-implementer, task-by-task execution; defining it twice would
# also violate this plan's "defined EXACTLY ONCE" rule (see Self-Review). Any test
# or caller that does `from personality_binding import EMPTY_CONFIG_DIGEST` keeps
# working unchanged — only the module that OWNS the value moved.


@dataclass(frozen=True, slots=True)
class BindingRecord:
    stable_agent_id: str
    role_checksum: str
    mode: Literal["image-default", "component-default", "override"]
    persona_id: str
    persona_version: str
    persona_checksum: str
    compiler_schema_version: str
    dependency_digests: tuple[str, ...]
    effective_config_digest: str
    binding_digest: str
    image_default_root: str | None = None
    component_root: str | None = None
    override_source: str | None = None


def compute_binding_digest(
    *, stable_agent_id: str, role_checksum: str, persona_id: str, persona_version: str,
    persona_checksum: str, compiler_schema_version: str, dependency_digests: tuple[str, ...],
    effective_config_digest: str,
) -> str:
    return checksum_json({
        "stable_agent_id": stable_agent_id,
        "role_checksum": role_checksum,
        "persona_id": persona_id,
        "persona_version": persona_version,
        "persona_checksum": persona_checksum,
        "compiler_schema_version": compiler_schema_version,
        "dependency_digests": sorted(dependency_digests),
        "effective_config_digest": effective_config_digest,
    })


def _build(
    *, role: RoleSlot, persona: PersonaPack, mode: str,
    dependency_digests: tuple[str, ...] = (), effective_config_digest: str = EMPTY_CONFIG_DIGEST,
    image_default_root: str | None = None, component_root: str | None = None,
    override_source: str | None = None,
) -> BindingRecord:
    digest = compute_binding_digest(
        stable_agent_id=role.role_id, role_checksum=role.checksum,
        persona_id=persona.persona_id, persona_version=persona.version,
        persona_checksum=persona.checksum,
        compiler_schema_version=RENDERER_VERSION,
        dependency_digests=dependency_digests, effective_config_digest=effective_config_digest,
    )
    return BindingRecord(
        stable_agent_id=role.role_id, role_checksum=role.checksum, mode=mode,
        persona_id=persona.persona_id, persona_version=persona.version,
        persona_checksum=persona.checksum,
        compiler_schema_version=RENDERER_VERSION,
        dependency_digests=tuple(sorted(dependency_digests)),
        effective_config_digest=effective_config_digest, binding_digest=digest,
        image_default_root=image_default_root, component_root=component_root,
        override_source=override_source,
    )


def materialize_image_default_binding(
    *, role: RoleSlot, persona: PersonaPack, image_default_root: str,
) -> BindingRecord:
    if role.kind != "resident":
        raise ValueError("image-default binding is resident-only")
    return _build(role=role, persona=persona, mode="image-default", image_default_root=image_default_root)


def materialize_override_binding(
    *, role: RoleSlot, persona: PersonaPack, override_source: str,
    dependency_digests: tuple[str, ...] = (), effective_config_digest: str = EMPTY_CONFIG_DIGEST,
) -> BindingRecord:
    """Task N1c extension (Controller resolution #1): `dependency_digests`/
    `effective_config_digest` are additive optional kwargs, passed straight
    through to `_build` (which already accepts both — every OTHER binding
    mode already threads them). Existing resident callers
    (`reconcile_resident_binding` above, `tools.py resident_persona_swap`)
    pass neither and get byte-identical behavior: `_build`'s own defaults
    for these two params are these exact same values, so the digest and
    every other field are unchanged for a resident override. This lets
    `upgrade_specialist`/`rollback_specialist` (specialist_install.py)
    preserve an OVERRIDE-bound specialist's persona pin across an upgrade
    while still capturing the new component's dependency closure and the
    operator's re-validated config in the binding digest, exactly the way
    `materialize_component_default_binding` already does for the
    component-default mode."""
    if role.kind not in {"resident", "specialist"}:
        raise ValueError("override binding is resident- or specialist-only")
    return _build(
        role=role, persona=persona, mode="override", override_source=override_source,
        dependency_digests=dependency_digests, effective_config_digest=effective_config_digest,
    )


def materialize_component_default_binding(
    *, role: RoleSlot, persona: PersonaPack, component_root: str,
    dependency_digests: tuple[str, ...] = (), effective_config_digest: str = EMPTY_CONFIG_DIGEST,
) -> BindingRecord:
    """Spec §2.3's third binding mode — specialist-only, tracks the default
    persona pinned by the INSTALLED component (as opposed to image-default,
    which tracks the image's own default, or override, which pins an exact
    operator-chosen digest). Reuses the SAME `_build` helper Task 7's other
    two materializers use — one binding-construction path, three modes."""
    if role.kind != "specialist":
        raise ValueError("component-default binding is specialist-only")
    return _build(
        role=role, persona=persona, mode="component-default", component_root=component_root,
        dependency_digests=dependency_digests, effective_config_digest=effective_config_digest,
    )


def _raw_from_binding(record: BindingRecord) -> dict[str, object]:
    return {
        "api_version": "casa.binding/v1",
        "stable_agent_id": record.stable_agent_id, "role_checksum": record.role_checksum,
        "mode": record.mode, "persona_id": record.persona_id,
        "persona_version": record.persona_version, "persona_checksum": record.persona_checksum,
        "compiler_schema_version": record.compiler_schema_version,
        "dependency_digests": list(record.dependency_digests),
        "effective_config_digest": record.effective_config_digest,
        "binding_digest": record.binding_digest,
        "image_default_root": record.image_default_root,
        "component_root": record.component_root, "override_source": record.override_source,
    }


def verify_binding_record(raw: dict) -> BindingRecord:
    """The ONE shared verification path: recompute the digest from every OTHER
    field and reject a mismatch. Both load_binding and InstanceDir's tuple loader
    call this — a binding's on-disk integrity is checked in exactly one place.

    Also schema-validates ``raw`` against binding.v1.json before field access.
    This is what turns a tampered/malformed nested binding (e.g. inside an
    instance tuple, where the outer schema only checks ``binding`` is an
    object) into a typed ``ValueError`` instead of a bare ``KeyError`` —
    instance-tuple.v1.json does not itself enforce the nested binding's
    required fields or patterns, so this is the only place that does for the
    nested case."""
    schema = json.loads((_SCHEMA_DIR / "binding.v1.json").read_text(encoding="utf-8"))
    try:
        jsonschema.validate(raw, schema)
    except jsonschema.ValidationError as exc:
        raise ValueError(str(exc)) from exc
    record = BindingRecord(
        stable_agent_id=raw["stable_agent_id"], role_checksum=raw["role_checksum"],
        mode=raw["mode"], persona_id=raw["persona_id"], persona_version=raw["persona_version"],
        persona_checksum=raw["persona_checksum"],
        compiler_schema_version=raw["compiler_schema_version"],
        dependency_digests=tuple(raw.get("dependency_digests") or ()),
        effective_config_digest=raw["effective_config_digest"],
        binding_digest=raw["binding_digest"],
        image_default_root=raw.get("image_default_root"),
        component_root=raw.get("component_root"), override_source=raw.get("override_source"),
    )
    expected = compute_binding_digest(
        stable_agent_id=record.stable_agent_id, role_checksum=record.role_checksum,
        persona_id=record.persona_id, persona_version=record.persona_version,
        persona_checksum=record.persona_checksum,
        compiler_schema_version=record.compiler_schema_version,
        dependency_digests=record.dependency_digests,
        effective_config_digest=record.effective_config_digest,
    )
    if record.binding_digest != expected:
        raise ValueError("binding_digest does not match canonical binding inputs")
    return record


def load_binding(path: Path) -> BindingRecord:
    """#205: the standalone binding reader. ``verify_binding_record`` already
    schema-validates against binding.v1.json, so the second validate that used
    to sit here was pure duplication — and it raised a bare
    ``jsonschema.ValidationError`` with no file context, pre-empting the
    path-qualified ``ValueError`` below. One validation, one error shape."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    try:
        return verify_binding_record(raw)
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc


def atomic_write_binding(path: Path, record: BindingRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = yaml.safe_dump(_raw_from_binding(record), sort_keys=False)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


@dataclass(frozen=True, slots=True)
class InstanceTuple:
    root: str
    binding: BindingRecord
    config_snapshot: Mapping[str, object]
    config_digest: str


# #372: the value a boot-time scrub writes over a digest that was computed
# over a secret-bearing config mapping. Deliberately fails the binding
# schema's digest pattern — a tombstoned tuple must never verify — and is
# recognized by the loaders BEFORE schema validation so the operator sees
# the real cause instead of an opaque pattern error.
PRE_GUARD_SENTINEL = "pre-guard:removed"

_PRE_GUARD_MESSAGE = (
    "instance tuple predates the secret-digest guard (#372) and was "
    "tombstoned; uninstall and reinstall this specialist"
)


def _raise_if_pre_guard_tombstone(raw: object) -> None:
    if not isinstance(raw, dict):
        return
    binding = raw.get("binding")
    if raw.get("config_digest") == PRE_GUARD_SENTINEL or (
        isinstance(binding, dict)
        and binding.get("effective_config_digest") == PRE_GUARD_SENTINEL
    ):
        raise ValueError(_PRE_GUARD_MESSAGE)


def verify_instance_tuple(raw: dict) -> InstanceTuple:
    _raise_if_pre_guard_tombstone(raw)
    binding = verify_binding_record(raw["binding"])
    config_digest = raw["config_digest"]
    if config_digest != binding.effective_config_digest:
        raise ValueError("instance tuple config_digest does not match its binding's effective_config_digest")
    snapshot = raw.get("config_snapshot") or {}
    # #372: the digest must be DERIVED from the persisted (secret-free)
    # snapshot, not merely agree with the binding — a pre-guard digest that
    # survived sanitization satisfies the binding equality but not this.
    if config_digest != compute_effective_config_digest(dict(snapshot)):
        raise ValueError(
            "instance tuple config_digest is not the digest of its persisted "
            "config_snapshot (#372: predates the secret-digest guard, or the "
            "writer is defective)"
        )
    return InstanceTuple(
        root=raw["root"], binding=binding,
        config_snapshot=snapshot, config_digest=config_digest,
    )


def make_instance_tuple(
    *, root: str, binding: BindingRecord, config_snapshot: Mapping[str, object],
) -> InstanceTuple:
    """#372: the ONE construction path for tuples that are going to be
    persisted. config_digest is always DERIVED from the snapshot — writers
    cannot choose it — and a binding whose effective_config_digest disagrees
    is refused here, before anything reaches disk."""
    config_digest = compute_effective_config_digest(dict(config_snapshot))
    if binding.effective_config_digest != config_digest:
        raise ValueError(
            "binding effective_config_digest was not computed over this "
            "config_snapshot (#372: build the binding from the same sanitized "
            "mapping that is being persisted)"
        )
    return InstanceTuple(
        root=root, binding=binding,
        config_snapshot=dict(config_snapshot), config_digest=config_digest,
    )


def _raw_from_tuple(tuple_: InstanceTuple) -> dict[str, object]:
    return {
        "api_version": "casa.instance-tuple/v1", "root": tuple_.root,
        "binding": _raw_from_binding(tuple_.binding),
        "config_snapshot": dict(tuple_.config_snapshot), "config_digest": tuple_.config_digest,
    }


def load_instance_tuple(path: Path) -> InstanceTuple | None:
    if not path.exists():
        return None
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    try:
        # #372: before schema validation — the sentinel deliberately fails the
        # digest pattern, and the typed message must win over the opaque
        # jsonschema error.
        _raise_if_pre_guard_tombstone(raw)
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc
    schema = json.loads((_SCHEMA_DIR / "instance-tuple.v1.json").read_text(encoding="utf-8"))
    jsonschema.validate(raw, schema)
    try:
        return verify_instance_tuple(raw)
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc


def atomic_write_instance_tuple(path: Path, tuple_: InstanceTuple) -> None:
    # #372 backstop at the write primitive: even a caller that bypassed
    # make_instance_tuple cannot persist a digest not derived from the
    # snapshot it is persisting — in EITHER digest field (Sol diff r1: a
    # split-digest tuple would otherwise park the oracle in
    # binding.effective_config_digest while the top-level field looks honest).
    if tuple_.config_digest != compute_effective_config_digest(dict(tuple_.config_snapshot)):
        raise ValueError(
            "refusing to persist an instance tuple whose config_digest is not "
            "the digest of its config_snapshot (#372)"
        )
    if tuple_.binding.effective_config_digest != tuple_.config_digest:
        raise ValueError(
            "refusing to persist an instance tuple whose binding "
            "effective_config_digest disagrees with its config_digest (#372)"
        )
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = yaml.safe_dump(_raw_from_tuple(tuple_), sort_keys=False)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


class InstanceDir:
    """One persona-bearing agent instance's on-disk active/desired/prior tuple
    pair (spec §4.1). Residents (Task 8) use
    ``/config/bindings/resident-<slot>/``; the specialist data model (Task 13)
    uses ``/config/specialists/<slug>/`` — SAME file format, SAME code, reused
    verbatim by Plan 2's N1 for install/upgrade/rollback."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory

    def _path(self, name: str) -> Path:
        return self._dir / name

    def active(self) -> InstanceTuple | None:
        return load_instance_tuple(self._path("active.yaml"))

    def desired(self) -> InstanceTuple | None:
        return load_instance_tuple(self._path("desired.yaml"))

    def stage_desired(self, tuple_: InstanceTuple) -> None:
        atomic_write_instance_tuple(self._path("desired.yaml"), tuple_)

    def commit_desired_to_active(self) -> InstanceTuple:
        desired_path = self._path("desired.yaml")
        candidate = load_instance_tuple(desired_path)
        if candidate is None:
            raise ValueError(f"{self._dir}: no desired tuple staged to commit")
        active_path = self._path("active.yaml")
        prior_path = self._path("active.prior.yaml")
        current_active = load_instance_tuple(active_path)
        # Task N1c fix: compare the FULL tuple (root included), not just
        # binding_digest. binding_digest deliberately excludes `root`
        # (compute_binding_digest's eight normative fields never include it
        # — see test_digest_input_set_matches_the_normative_eight_fields),
        # so two installed-component versions that bump only the manifest
        # version (role.yaml/doctrine.md/persona/dependency-digest bytes all
        # unchanged) share the SAME binding_digest while their `root`
        # (component_id@version#checksum) genuinely differs — upgrade_
        # specialist's own test_upgrade_commits_a_new_active_tuple_and_
        # retains_the_prior_as_rollback_target exercises exactly this case.
        # Under the old binding_digest-only check that scenario was
        # mis-detected as a crash-retry no-op and silently skipped writing
        # the new (different-root) active.yaml at all. Both `candidate` and
        # `current_active` are round-tripped through the SAME
        # load_instance_tuple/verify_instance_tuple path, so this equality
        # is a fair, symmetric comparison — the true crash-retry case (this
        # exact tuple, root included, already written to active.yaml before
        # a crash) is unaffected: it is still recognized and short-circuits
        # exactly as before (see test_commit_is_crash_retry_idempotent_and_
        # preserves_true_prior).
        if current_active is not None and current_active == candidate:
            # Crash-retry / no-op recommit (§4.1): a previous run already wrote
            # `candidate` to active.yaml but died before finishing. active.yaml
            # already IS the candidate, so do NOT re-copy it toward prior —
            # that would overwrite the true pre-commit rollback target with a
            # duplicate of the new active. #339: under the write-active-first
            # order below, the interrupted step may ALSO be the pending
            # tmp -> prior rotation (the copied OLD active) — finish it, but
            # only after PROVING the tmp is a genuine pre-commit generation
            # (Sol review): a bundle-journal rollback restores active/prior
            # without knowing about this tmp, so a stale tmp can survive
            # holding the SAME tuple as the restored active — rotating that
            # duplicate over prior is exactly the clobber this branch exists
            # to prevent. A tmp that fails to load (corrupt/tampered) must
            # never reach prior either. Unlink in both cases.
            pending = active_path.with_suffix(active_path.suffix + ".rollback-tmp")
            try:
                if pending.exists():
                    try:
                        pending_tuple = load_instance_tuple(pending)
                    except (ValueError, OSError, yaml.YAMLError,
                            jsonschema.ValidationError):
                        pending_tuple = None
                    if pending_tuple is not None and pending_tuple != candidate:
                        os.replace(pending, prior_path)
                    else:
                        pending.unlink(missing_ok=True)
                desired_path.unlink(missing_ok=True)
            except OSError:
                # Sol round-2: active.yaml already IS the candidate — the
                # commit is durable, so every remaining step is best-effort
                # cleanup and must not make the caller believe the commit
                # failed (it would run the pre-commit tuple while disk holds
                # the new active). The next recommit retries.
                logger.warning(
                    "%s: post-commit cleanup failed; retried on the next "
                    "recommit", self._dir, exc_info=True,
                )
            return candidate
        # #339 (rollback-generation safety): durably write the NEW active
        # before anything touches active.prior.yaml. The old order rotated
        # prior first, so an active-write failure in between left
        # prior == active — the previous rollback generation destroyed with
        # nothing gained. Now a failure at any point leaves prior intact,
        # and a crash after the active write is completed by the
        # crash-retry branch above (the .rollback-tmp copy IS the journal).
        pending_prior = (
            self._copy_to_temp(active_path) if active_path.exists() else None
        )
        try:
            atomic_write_instance_tuple(active_path, candidate)
        except BaseException:
            # A FAILED (not crashed) write must not leave the copied tmp
            # behind: a later commit of a tuple identical to active would
            # take the crash-retry branch and rotate this stale copy over
            # the true prior. (A hard crash here is safe without cleanup —
            # active != candidate still, so retry recreates the copy.)
            if pending_prior is not None:
                pending_prior.unlink(missing_ok=True)
            raise
        if pending_prior is not None:
            try:
                os.replace(pending_prior, prior_path)
            except OSError:
                # Terra review: the commit has already SUCCEEDED — the new
                # active is durably written. Failing here would make the
                # caller run the pre-commit tuple while disk says otherwise
                # (reconcile catches OSError and returns the retained
                # active). Keep the tmp as the pending-rotation journal —
                # the no-op recommit branch above completes it, and any
                # later real commit overwrites it — and log the degraded
                # prior instead of failing a committed transition.
                logger.warning(
                    "%s: new active committed but prior rotation failed; "
                    "rollback target is one generation stale until the "
                    "pending rotation completes", self._dir, exc_info=True,
                )
        try:
            desired_path.unlink(missing_ok=True)
        except OSError:
            # Sol round-2: same post-commit rule as above — the new active is
            # durable, so a failed desired unlink must not fail the commit;
            # the stale desired is cleared by the next no-op recommit.
            logger.warning(
                "%s: new active committed but desired.yaml unlink failed; "
                "cleared on the next recommit", self._dir, exc_info=True,
            )
        return candidate

    def _copy_to_temp(self, path: Path) -> Path:
        temp = path.with_suffix(path.suffix + ".rollback-tmp")
        temp.write_bytes(path.read_bytes())
        os.chmod(temp, 0o600)
        return temp

    def discard_desired(self, *, reason: str) -> None:
        desired_path = self._path("desired.yaml")
        if not desired_path.exists():
            return
        error_path = self._path("desired.error.yaml")
        payload = yaml.safe_load(desired_path.read_text(encoding="utf-8")) or {}
        payload["_error_reason"] = reason
        error_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        desired_path.unlink()

    # --- Owned-plugins sidecar triple (Task 10, spec §3.4) ------------------
    # A specialist's owned-plugin set (and its component source receipt) is
    # persisted alongside the instance tuple as a MAPPING document, one file
    # per generation, mirroring the active/desired/prior tuple triple so a
    # bundle transaction (install/upgrade/rollback) and boot reconciliation
    # can restore the exact owned set + provenance a generation carried. The
    # sidecar carries what the tuple cannot: which plugins the specialist owns
    # and where their bytes came from (closes the provenance gap for
    # plugin-less components too — `plugins: []` with a real component_source).
    def stage_desired_owned_plugins(self, doc: dict) -> None:
        write_owned_plugins(owned_plugins_desired_path(self._dir), doc)

    def commit_owned_plugins_desired_to_active(self) -> None:
        """Rotate the owned-plugins sidecar triple in lockstep with
        `commit_desired_to_active`'s tuple rotation: desired->active,
        active->prior. Called inside the SAME MATERIALIZE_LOCK step. A no-op
        when no desired sidecar was staged (defensive — every bundle commit
        stages one first)."""
        desired = owned_plugins_desired_path(self._dir)
        active = owned_plugins_path(self._dir)
        prior = owned_plugins_prior_path(self._dir)
        if not desired.exists():
            return
        if active.exists() and desired.read_bytes() == active.read_bytes():
            # #346: no-op recommit (crash-retry, or a duplicate bundle that
            # lost a race) — the staged sidecar IS the active one. Rotating
            # would clobber the true prior generation with a duplicate of
            # the new active, desyncing owned-plugins.prior from
            # active.prior.yaml for rollback. Mirrors
            # commit_desired_to_active's tuple no-op semantics.
            desired.unlink()
            return
        if active.exists():
            # Copy-then-replace (mirrors commit_desired_to_active's
            # _copy_to_temp) so a crash mid-rotation never destroys the prior
            # rollback target before the new active is in place.
            prior.write_bytes(active.read_bytes())
            os.chmod(prior, 0o600)
        os.replace(desired, active)


# --- Owned-plugins sidecar document (Task 10, spec §3.4) --------------------
# Path helpers for the TRIPLE, mirroring the tuple filenames. Module functions
# (not just InstanceDir methods) so boot reconciliation / rollback can address
# a slug dir directly.

def owned_plugins_desired_path(directory: Path) -> Path:
    return Path(directory) / "owned-plugins.desired.yaml"


def owned_plugins_path(directory: Path) -> Path:
    return Path(directory) / "owned-plugins.yaml"


def owned_plugins_prior_path(directory: Path) -> Path:
    return Path(directory) / "owned-plugins.prior.yaml"


# Whole-branch F: an owned-plugins sidecar is on-disk state a rollback/boot
# path later joins into a store path (`store_root / row["name"] /
# row["artifact_id"]`). A tampered/corrupt sidecar with a traversal `name`
# ("../../etc") or a bogus `artifact_id` must NEVER reach a filesystem join —
# validate the grammar on READ and fail the whole doc closed if malformed.
#
# P2-7: the owned scoped-name grammar has a canonical home — reuse
# `plugin_registry.OWNED_NAME_RE` (single source of truth) rather than copying
# the pattern. `_ARTIFACT_ID_RE` has no canonical constant in plugin_registry
# (artifact ids are raw sha256 hexdigests from `compute_artifact_id`); a test
# asserts this local pattern matches that output shape.
_OWNED_NAME_RE = plugin_registry.OWNED_NAME_RE
_ARTIFACT_ID_RE = re.compile(r"^[0-9a-f]{64}$")


class OwnedPluginsSidecarError(ValueError):
    """P1-4: a PRESENT owned-plugins sidecar that fails strict v1 validation.
    Distinct from an ABSENT sidecar (legacy/pre-feature generation) so a
    rollback/boot caller can tell "no owned set to restore" apart from "the
    recorded owned set is unreadable" — the latter must refuse loudly rather
    than silently discard the prior owned set as if it were empty."""


def _valid_owned_source(source: object) -> bool:
    """P2: an owned row's `source` sub-mapping, per the sidecar contract
    written by `_owned_sidecar_doc`/`_owned_entry_for` (specialist_install.py)
    — always `{"type": "github", "repo", "ref", "revision", "subdir"}`. A
    non-mapping (or field-incomplete) `source` must fail validation HERE so
    `read_owned_plugins` raises the typed `OwnedPluginsSidecarError` — leaving
    it unchecked lets a bogus `source` (e.g. a bare string) pass `_valid_owned_row`
    and then blow up later as an untyped exception where a caller (rollback's
    `_prior_owned_entry`) indexes `src["repo"]`/`src["ref"]`/`src["revision"]`."""
    if not isinstance(source, dict):
        return False
    if source.get("type") != "github":
        return False
    for key in ("repo", "ref", "revision"):
        if not isinstance(source.get(key), str) or not source[key]:
            return False
    if not isinstance(source.get("subdir", ""), str):
        return False
    if not plugin_registry.REVISION_RE.match(source["revision"]):
        return False
    try:
        plugin_registry.normalize_subdir(source.get("subdir", ""))
    except ValueError:
        return False
    return True


def _valid_owned_row(row: object) -> bool:
    if not isinstance(row, dict):
        return False
    name = row.get("name")
    if (not isinstance(name, str) or not _OWNED_NAME_RE.match(name)
            or len(name.encode()) > 72):
        return False
    if not _ARTIFACT_ID_RE.match(str(row.get("artifact_id", ""))):
        return False
    mname = row.get("manifest_name")
    if not isinstance(mname, str) or name.partition(".")[2] != mname:
        return False
    if not isinstance(row.get("version", ""), str):
        return False
    if not _valid_owned_source(row.get("source")):
        return False
    return True


def _valid_owned_doc(raw: object) -> bool:
    """P1-4: the complete v1 document shape — `schema_version == 1`, a
    `component_source` mapping, and a `plugins` list of grammar-valid rows."""
    if not isinstance(raw, dict):
        return False
    if raw.get("schema_version") != 1:
        return False
    if not isinstance(raw.get("component_source"), dict):
        return False
    plugins = raw.get("plugins")
    if not isinstance(plugins, list) or not all(_valid_owned_row(r) for r in plugins):
        return False
    return True


def read_owned_plugins(path: Path) -> "dict | None":
    """Load an owned-plugins sidecar document.

    Returns None ONLY when the file is ABSENT (a legacy/pre-feature generation
    ⇒ empty owned set). A PRESENT file is validated against the full v1 shape
    `{"schema_version": 1, "component_source": {...}, "plugins": [...]}` — row
    grammar included, so a downstream store-path join can trust every row — and
    a malformed present file raises `OwnedPluginsSidecarError` (P1-4) rather
    than returning None. Collapsing malformed-present into None let a rollback
    silently discard the prior owned set (treating an unreadable sidecar as
    "nothing was owned"); the typed error forces the caller to refuse."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise OwnedPluginsSidecarError(
            f"owned-plugins sidecar {p} is present but unreadable: {exc}")
    if not _valid_owned_doc(raw):
        raise OwnedPluginsSidecarError(
            f"owned-plugins sidecar {p} failed v1 validation")
    return raw


def write_owned_plugins(path: Path, doc: dict) -> None:
    """Atomic write of an owned-plugins sidecar document (same os.replace-backed
    primitive the instance tuples use)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = yaml.safe_dump(doc, sort_keys=False)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


# --- Resident persona defaults + boot-time reconciliation (Task 8) ----------

# The ONLY code path that chooses a fixed resident slot's default persona ref.
# Keyed by role_slot.FIXED_RESIDENT_SLOTS; each value is an exact
# "<namespace>/<slug>@<version>" ref resolvable under defaults/personas/.
IMAGE_DEFAULT_PERSONA_BY_SLOT: Mapping[str, str] = MappingProxyType({
    "assistant": "casa/ellen@0.1.0",
    "butler": "casa/tina@0.1.0",
    "concierge": "casa/gary@0.1.0",
})

# A persona_requirements entry is either an exact "ns/slug@X.Y.Z" pin (matched
# by string equality) or a "ns/slug@>=X.Y.Z <A.B.C" range; a "ns/*@..." pattern
# matches any slug in that namespace.
_RANGE_RE = re.compile(
    r"^(?P<ns_slug>[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?/[a-z0-9*][a-z0-9-]*)@"
    r">=(?P<low>\d+\.\d+\.\d+)\s*<(?P<high>\d+\.\d+\.\d+)$"
)


def _semver_tuple(value: str) -> tuple[int, int, int]:
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


def check_persona_requirements(role: Mapping[str, object], persona: PersonaPack) -> None:
    """Validate persona.compatibility (the role's optional persona_requirements
    constraint, spec §2.3): each entry is either an exact 'ns/slug@X.Y.Z' pin or a
    'ns/slug@>=X.Y.Z <A.B.C' range; 'ns/*@...' matches any slug in that namespace.
    A role with persona.policy == 'forbidden' has no compatibility list to check."""
    persona_block = role.get("persona", {}) or {}
    if persona_block.get("policy") == "forbidden":
        return
    entries = persona_block.get("compatibility") or ()
    ref = f"{persona.persona_id}@{persona.version}"
    for entry in entries:
        if entry == ref:
            return
        match = _RANGE_RE.match(entry)
        if not match:
            continue
        namespace, _, slug_pattern = match.group("ns_slug").partition("/")
        persona_namespace, _, persona_slug = persona.persona_id.partition("/")
        if namespace != persona_namespace or (slug_pattern != "*" and slug_pattern != persona_slug):
            continue
        low, high = _semver_tuple(match.group("low")), _semver_tuple(match.group("high"))
        if low <= _semver_tuple(persona.version) < high:
            return
    raise ValueError(
        f"persona {ref} does not satisfy role {role.get('id')}'s persona_requirements {list(entries)}"
    )


def reconcile_resident_binding(
    *, role: RoleSlot, image_default_persona_loader: Callable[[str], PersonaPack],
    override_persona_loader: Callable[[str], PersonaPack], instance_dir: InstanceDir,
    candidate_validator: "Callable[[PersonaPack, BindingRecord], None] | None" = None,
    commit: bool = True,
) -> InstanceTuple:
    """Boot-time reconciliation of a resident's binding (spec §4.1, §4.2, §4.4).

    Reads an already-staged ``desired.yaml`` FIRST — the artifact
    ``resident_persona_swap``/``resident_persona_reset`` write BEFORE a restart —
    and, when present, THAT staged candidate's persona selection is what gets
    validated/compiled/committed (spec §4.2 step 4: "restart the affected agent;
    on success active := desired"). Without this, a swap/reset staged before the
    restart would be silently discarded and the resident would boot back onto its
    old binding.

    Either way — staged swap/reset, or the passive image-default-tracking path
    when nothing is staged — the candidate binding is ALWAYS recomputed against
    the role CURRENTLY loading (never a stale stored role_checksum), so an image
    upgrade to role.yaml landing in the same restart as a pending swap still gets
    the current role_checksum (spec §4.4).

    1. Determine the candidate's persona SELECTION (mode + persona ref):
       - a staged ``desired.yaml``, if present, wins;
       - otherwise an override-bound ACTIVE tuple keeps its exact pinned persona;
       - otherwise (image-default binding, or no active tuple at all — fresh
         install) resolve the CURRENT ``IMAGE_DEFAULT_PERSONA_BY_SLOT[role.slot]``.
    2. Materialize the candidate binding against the CURRENT role.
    3. If the candidate's binding_digest equals the active tuple's, this is a
       no-op — return the active tuple unchanged, discarding any now-redundant
       staged file.
    4. Otherwise (re-)stage the candidate as desired, validate persona↔role
       compatibility, and on success commit ``active := desired`` via
       ``InstanceDir.commit_desired_to_active()``. On failure (persona blob
       missing/incompatible, disk error), log the refusal's own facts, discard
       the desired candidate, and return the RETAINED PRIOR active tuple.
    5. **Retaining is not surviving** (#670). Step 4 bounds THIS function only:
       it returns the active tuple as read. Whether that tuple can be SERVED is
       settled by the caller's mandatory re-load of the same persona bytes, and
       a resident load failure is boot-fatal (INV-PERS-003). This docstring used
       to say step 4 meant "boot proceeds on the last-known-good binding, never
       crash-looping", which is false for exactly the case #670 reports — an
       ACTIVE override whose pinned bytes changed is retained here and then
       fails to load — and that sentence was read as a documented promise of a
       degraded mode that has never existed. A rejected staged candidate is not
       an exception either: ``resident_persona_swap`` may stage a ref that is
       already active, so the same changed bytes can back both tuples.
    6. Only when there is NO active tuple at all (fresh install) AND step 4 fails
       does this hard-fail loudly — raise ValueError so the caller turns it into
       an actionable LoadError.

    The persona resolve/materialize calls run INSIDE the same guarded block as
    validate/stage/commit, so every failure mode is caught by the SAME handler
    and ``active`` is preserved whenever an active tuple exists.

    Whole-branch review round 6, F1: the ENTIRE read-decide-write body runs under
    ``MATERIALIZE_LOCK`` — this call stages/commits/discards the resident
    InstanceDir on the boot and reload load paths, concurrently with the now-locked
    persona-swap tools (``tools._stage_and_report`` /
    ``persona_install.apply_persona_override``). Without the lock a reconcile could
    overwrite or discard a freshly staged swap (or vice versa). The active/desired
    reads are pulled INSIDE the lock so the read-modify-write is atomic against any
    other lock holder — no TOCTOU. The persona-pack loads here are bounded LOCAL
    disk reads on the rare boot/reload path, so holding the lock across them (a
    correctness-first choice over the usual "read-only work outside the lock"
    optimization) costs nothing measurable. Loop-safety: every caller reaches this
    off the event loop — reload via ``asyncio.to_thread(load_agent_from_dir)``, boot
    via ``asyncio.to_thread(load_all_agents)`` (casa_core), config_sync as a
    standalone process, and ``tools.validate_config_repo`` via ``asyncio.to_thread``.
    """
    with MATERIALIZE_LOCK:
        active = instance_dir.active()
        staged = instance_dir.desired()

        source_binding = staged.binding if staged is not None else (
            active.binding if active is not None and active.binding.mode == "override" else None
        )
        # #670: these three are bound BEFORE the loader call and read by the
        # handler below, because `str(exc)` is not the diagnosis. The exception
        # that arrives is usually raised by `persona_pack`, which knows nothing
        # about the binding: on the issue's commonest variant — a prose edit
        # under a stale manifest — the reason is "Core body must contain 300-500
        # characters", carrying neither the ref nor the pin. `found_checksum`
        # stays None unless a pack actually resolved; it is never invented.
        persona_ref = None
        pinned_checksum = None
        found_checksum = None
        try:
            if source_binding is not None and source_binding.mode == "override":
                persona_ref = f"{source_binding.persona_id}@{source_binding.persona_version}"
                pinned_checksum = source_binding.persona_checksum
                persona = override_persona_loader(persona_ref)
                found_checksum = persona.checksum
                # #339: a published persona version is immutable and its
                # activation is checksum-bound consent. The binding pins the
                # approved bytes; if the (mutable) path now holds DIFFERENT
                # bytes for the same id@version, refuse to rematerialize —
                # silently committing whatever is present would bypass the
                # consent contract.
                if persona.checksum != source_binding.persona_checksum:
                    # #670: the tail of this sentence used to be "re-run
                    # resident_persona_swap to re-approve, or
                    # resident_persona_reset to recover". Both begin at
                    # `tools._resolve_resident_role`, which reads
                    # `agent.active_runtime` and answers `runtime_unavailable`
                    # when it is absent — and on a resident this refusal is
                    # boot-fatal (INV-PERS-003), so by the time an operator
                    # reads it there is no runtime to resolve against. What is
                    # left is the fact set `persona_install.require_persona_present`
                    # already uses in-tree for this same condition: the ref, the
                    # checksum found, and the checksum pinned. The recovery
                    # itself is stated in docs/architecture/personality.md,
                    # where it can carry the conditions under which each step
                    # works; five rounds of evidence in this cluster say an
                    # emitted string cannot.
                    raise ValueError(
                        f"persona {persona_ref} on disk has checksum "
                        f"{persona.checksum} but the binding pins "
                        f"{source_binding.persona_checksum} — bytes changed "
                        f"under a pinned version"
                    )
                candidate_binding = materialize_override_binding(
                    role=role, persona=persona, override_source=source_binding.override_source,
                )
                root = candidate_binding.override_source
            else:
                default_ref = IMAGE_DEFAULT_PERSONA_BY_SLOT[role.slot]
                persona = image_default_persona_loader(default_ref)
                candidate_binding = materialize_image_default_binding(
                    role=role, persona=persona, image_default_root=default_ref,
                )
                root = candidate_binding.image_default_root

            if active is not None and active.binding.binding_digest == candidate_binding.binding_digest:
                if staged is not None and commit:
                    instance_dir.discard_desired(reason="no-op: candidate matches the already-active binding")
                return active

            candidate_tuple = make_instance_tuple(
                root=root, binding=candidate_binding, config_snapshot={},
            )
            check_persona_requirements(role.normalized, persona)
            # #339: the candidate must PROVE it loads before promotion. The
            # caller supplies the same compile/admission check the loader
            # runs after reconcile (agent_loader wires compile_prompt_bundle
            # — not imported here, which would cycle). Without this, a
            # schema-valid persona that fails a compile ceiling was committed
            # active and every subsequent boot failed on it with no
            # last-known-good left to retain.
            if candidate_validator is not None:
                candidate_validator(persona, candidate_binding)
            if not commit:
                # Validation-only replay (#338): report what boot WOULD
                # activate without writing any InstanceDir state.
                return candidate_tuple
            instance_dir.stage_desired(candidate_tuple)
            return instance_dir.commit_desired_to_active()
        except (ValueError, OSError) as exc:
            # #670: this handler used to be the end of the diagnosis. It passed
            # the reason to discard_desired() and nowhere else, and
            # discard_desired() early-returns when nothing is staged
            # (:515-518) — so on a boot with no staged swap, which is exactly
            # the "changed bytes back the ACTIVE binding" case the issue
            # reports, the refusal computed above reached no file and no
            # caller. Measured at the parent of this change, on all three
            # damage variants: ZERO WARNING-or-higher records across an entire
            # failing resident load.
            #
            # Fields, not a sentence. Every prose form of a record here has
            # been false in some reachable state — retaining a tuple is not
            # surviving it, since a swap may stage a ref that is ALREADY
            # active, so the same changed bytes can back both tuples and the
            # load fails anyway. So this asserts nothing about what is live,
            # what was written, or whether startup is prevented: it carries
            # what was READ and the reason that was caught.
            #
            # ONE level for every arm, deliberately. The level is not a
            # survivability judgement — this function provably cannot make one
            # — it is the observation that an approved selection did not take
            # effect, which is always true here.
            #
            # json.dumps, and over the WHOLE object rather than the reason
            # alone: these values include untrusted text (a persona ref is read
            # from an operator-writable tuple file, and binding.v1.json's
            # `^...$` pattern admits a trailing newline because Python's `re`
            # matches `$` before a final newline), so an unescaped value could
            # split the record into two physical lines whose second parsed as
            # fields of its own.
            #
            # Gated on `commit` because the validation replay
            # (agent_loader.validate_config_repo, binding_commit=False) is a
            # REPORT path whose output is its return value, and
            # `config_git_commit`'s pre-commit gate replays it on every
            # operator config commit. A report that manufactures ERROR records
            # into the live process log is noise, not diagnosis.
            if commit:
                logger.error("persona_binding_reconcile_failed %s", json.dumps({
                    "resident": role.role_id,
                    "persona_ref": persona_ref,
                    "pinned_checksum": pinned_checksum,
                    "found_checksum": found_checksum,
                    "active_tuple": "present" if active is not None else "absent",
                    "staged_tuple": "present" if staged is not None else "absent",
                    "reason": str(exc),
                }, sort_keys=True))
            # discard_desired() is a no-op when nothing was ever staged (e.g. the
            # persona loader itself raised before stage_desired ran).
            if commit:
                instance_dir.discard_desired(reason=str(exc))
            if active is None:
                raise ValueError(
                    f"resident {role.role_id}: no prior active binding exists and the "
                    f"fresh reconciliation failed: {exc}"
                ) from exc
            return active
