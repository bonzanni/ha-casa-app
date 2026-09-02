from __future__ import annotations

import json
import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Literal, Mapping

import jsonschema
import yaml

from authored_markers import contains_forbidden_marker
from canonical_bytes import reject_forbidden_markers, to_plain_json
from specialist_component import SpecialistComponent, is_valid_slug, load_specialist_component
from specialist_lifecycle import check_slug_uniqueness

if TYPE_CHECKING:
    from specialist_install_consent import SpecialistInstallAckStore
    from specialist_lifecycle import SpecialistInstance
    from specialist_receipt import PluginReceiptRow, SourceReceipt
    from specialist_registry import InstalledSpecialistIndex

logger = logging.getLogger(__name__)


class SpecialistInstallError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        self.detail = message
        super().__init__(message)


# Whole-branch review F1 (slug traversal) / F4 (corpus identifier containment):
# every lifecycle function that turns a CALLER-SUPPLIED slug or a
# schema-unconstrained dependency identifier into a `Path` join must first
# validate it against a canonical, single-segment shape — a value like
# `../../..`, `/data`, or `a/b` must NEVER index shutil.rmtree/copytree or a
# CAS/corpus lookup. These validators are the ONE authority every entry point
# routes through (fail-closed, typed refusal).
_CORPUS_IDENT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def validate_specialist_slug(slug: object) -> str:
    """F1: fail-closed slug gate at the lifecycle-function boundary — the layer
    every caller (MCP tool, test, direct) must pass through. Reuses the
    component loader's canonical slug regex (``specialist_component.
    is_valid_slug`` / role.v1.json ``slot``). Raises a typed
    ``invalid_slug`` error; never lets a traversal/absolute/separator slug
    reach a ``Path`` join."""
    if not is_valid_slug(slug):
        raise SpecialistInstallError("invalid_slug", f"invalid specialist slug {slug!r}")
    return slug  # type: ignore[return-value]


def is_safe_corpus_identifier(identifier: object) -> bool:
    """F4: a corpus dependency's ``identifier`` is schema-unconstrained
    (specialist-component.v1.json only requires ``minLength: 1``) yet it is
    joined as ``component_dir / "corpus" / identifier``. Require a
    conservative single-segment name (no separators, no ``..``, not absolute)
    so a hostile manifest can never make the join stat/hash bytes outside the
    component directory."""
    return (
        isinstance(identifier, str)
        and _CORPUS_IDENT_RE.fullmatch(identifier) is not None
        and ".." not in identifier
    )


# Whole-branch review round 4, F1/F2 — in-lock concurrent-mutation guards.
# EVERY InstanceDir write (stage_desired/commit_desired_to_active/
# discard_desired) in this module now happens under
# specialist_materialize.MATERIALIZE_LOCK (round 3 covered only the activating
# stage+commit; round 4 extends it to the pending-configuration placeholder and
# the upgrade error path). But holding the lock is not enough on its own: a
# pre-lock read (upgrade/rollback's `active_before`, or a fresh install's "the
# slug is not yet active" premise) can go STALE while the caller blocks on the
# lock, because a concurrent uninstall/install/upgrade for the SAME slug may
# have run to completion first. These two helpers RE-VALIDATE that premise
# INSIDE the lock, immediately before the first InstanceDir write, and fail
# closed with a typed `concurrent_mutation` refusal — leaving every on-disk
# tuple untouched — rather than resurrect a removed slug's InstanceDir or
# double-activate over a concurrent winner. Both MUST be called with
# MATERIALIZE_LOCK held.


def _under_specialist_lifecycle_lock(fn):
    """#810 (INV-SPEC-011): run *fn* — one of the four lifecycle entry points —
    WHOLE under ``specialist_materialize.SPECIALIST_LIFECYCLE_LOCK``, read at
    call time so a test that wraps the lock object sees every acquisition.
    The library function takes the lock, not its tool handler: the handler's
    asyncio task can be cancelled while the worker thread it offloaded to runs
    on, and a lock the handler held would be released mid-section. Every
    caller is a worker thread (tools.py's ``asyncio.to_thread``, the
    configure re-commit, direct library callers in tests); the lock is never
    acquired on the event loop. Non-reentrant, and none of the four calls
    another (measured), so there is no nesting."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        import specialist_materialize
        with specialist_materialize.SPECIALIST_LIFECYCLE_LOCK:
            return fn(*args, **kwargs)
    return wrapper


def _require_active_unchanged(instance_dir, active_before, *, slug: str) -> None:
    """F2: upgrade/rollback captured `active_before` BEFORE taking the lock. A
    concurrent uninstall (which removes specialists/<slug>, active.yaml
    included) or a concurrent upgrade/rollback/persona-override (which commits a
    different active) may have won while we blocked on the lock. Require the
    active tuple to still EXIST and be BYTE-FOR-BYTE the tuple captured at
    `active_before`; a vanished or in-ANY-way-changed active means a concurrent
    mutation won — refuse, so we never stage/commit over it (or recreate a
    just-removed InstanceDir).

    Round-5 fix (F1): compare the FULL tuple (`re_read != active_before`), not
    just `root`. `root` alone (component_id@version#checksum) misses a
    concurrent SAME-ROOT mutation — a config-only upgrade (different
    config_digest/binding on an unchanged component version) or a persona
    override (mode=override, root unchanged) commits a genuinely different
    active tuple that a root-only check waves through, letting this caller
    silently overwrite it with the stale `active_before`. Full-tuple equality
    is the SAME convention InstanceDir.commit_desired_to_active already uses for
    its crash-retry short-circuit (`current_active == candidate`, root
    included); both sides here are round-tripped through the same
    load_instance_tuple/verify_instance_tuple path, so the comparison is fair
    and symmetric (frozen-dataclass value equality over root+binding+
    config_snapshot+config_digest)."""
    current = instance_dir.active()
    if current is None or current != active_before:
        raise SpecialistInstallError(
            "concurrent_mutation",
            f"{slug!r}: the active install changed under a concurrent mutation "
            f"(uninstall/upgrade/rollback/persona-override) while acquiring the lock; "
            f"refusing to overwrite it — retry the operation")


def _refuse_if_active_present(instance_dir, *, slug: str, root: str) -> None:
    """F2: a fresh `commit_specialist_install` (both the activating and the
    pending-configuration placeholder paths) presumes the slug is NOT yet
    active — inspect's `check_slug_uniqueness` rejected an already-installed
    slug, and a reinstall-after-uninstall legitimately sees no active tuple
    (uninstall removed it). But two concurrent fresh installs of the SAME slug
    can both clear inspect and then race here; the first commits active, and
    without this guard the second would stage its desired and
    `commit_desired_to_active` would rotate the winner's active into prior and
    write ours over it — a silent double-activate demoting the winner. Re-read
    under the lock and refuse if an active tuple already exists (fail closed),
    rather than clobber a concurrent winner.

    #346 extends the same guard to a PENDING candidate: uniqueness at inspect
    time sees neither tuple for a free slug, so two fresh installs of the same
    slug could both pass and the second would silently replace the first's
    desired.yaml (and its config/owned-plugin sidecar). A pending candidate
    with a DIFFERENT component root is refused; the SAME root may restage —
    that is the pending-configuration slug's own configure re-commit (the
    documented pending -> active step), which legitimately replaces its own
    placeholder. ``root`` is the caller's component_root string
    (component_id@version#checksum). A pending tuple that fails to LOAD is
    treated as a conflict too (cannot prove it is ours — fail closed; the
    boot-time index isolates it as state="error" for inspection).

    Deliberately NOT keyed on receipt/operation identity (Terra round-3):
    a same-root restage with different config is last-writer-wins pending
    activation. An operator resuming configuration later re-inspects and
    holds a NEW receipt for the same root, so demanding receipt equality
    would refuse the legitimate resume flow; and both writers necessarily
    hold consent for this exact install identity with byte-identical
    content, so no consent or integrity boundary is crossed — only the
    not-yet-activated config of one consented component."""
    if instance_dir.active() is not None:
        raise SpecialistInstallError(
            "concurrent_mutation",
            f"{slug!r}: an active install appeared under a concurrent install "
            f"while acquiring the lock; refusing to double-activate — re-inspect "
            f"and retry")
    try:
        pending = instance_dir.desired()
    except (ValueError, OSError, yaml.YAMLError,
            jsonschema.ValidationError) as exc:
        # load_instance_tuple wraps only its own ValueError path; a bad-YAML
        # or schema-invalid desired.yaml raises the raw parser/validator
        # error. All of them mean the same thing here: an occupant we cannot
        # prove is ours — fail closed.
        raise SpecialistInstallError(
            "concurrent_mutation",
            f"{slug!r}: an unreadable pending candidate already exists "
            f"({exc}); refusing to replace it — uninstall or repair first")
    if pending is not None and pending.root != root:
        raise SpecialistInstallError(
            "concurrent_mutation",
            f"{slug!r}: a different pending install ({pending.root}) already "
            f"occupies this slug; refusing to replace it — complete or "
            f"uninstall it first")


@dataclass(frozen=True, slots=True)
class DependencyResolution:
    kind: str
    identifier: str
    digest: str
    available: bool
    detail: str
    # Task 8 fix-round-1 (consent-review CRITICAL): the sourced-plugin
    # surfaces `_validate_sourced_plugin_tree` already parses while
    # validating — captured here (rather than re-parsed at the
    # PluginReceiptRow-building site) so the consent DM can enumerate them
    # (spec §3.2). Empty for every non-sourced-plugin row (persona/corpus/
    # legacy sourceless plugin) and for a sourced row that failed validation
    # before reaching the point these are extracted.
    mcp_servers: tuple[str, ...] = ()
    protected_tools: tuple[str, ...] = ()
    env_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class InspectionResult:
    component_id: str
    version: str
    slug: str
    component_checksum: str
    root_digest: str          # Round-2 addition (finding #2) — see compute_install_root_digest
    mission: str
    default_persona_ref: str
    default_persona_checksum: str
    required_config_names: tuple[str, ...]
    required_secret_names: tuple[str, ...]
    dependencies: tuple[DependencyResolution, ...]
    staged_dir: Path
    # Task 8 additions (spec §3.2.1) — defaulted so every pre-Task-8
    # constructor call (production and test) keeps working unchanged.
    # ``receipt_id``/``receipt_digest`` are "" and ``plugin_resolutions`` is
    # () for a hand-built InspectionResult that predates the trusted-source
    # receipt; a real inspect_specialist_repo call always populates all three
    # (every inspect issues a receipt, plugin-less components included).
    receipt_id: str = ""
    receipt_digest: str = ""
    plugin_resolutions: tuple["PluginReceiptRow", ...] = ()
    # #541: the role.yaml's own casa-framework tool grants, for the consent
    # DM's ``Casa tools:`` line — the powers the specialist arrives with.
    # Post-ceiling (load_specialist_component rejected anything outside the
    # allowlist), so always consumer-safe, but the operator approving the
    # install still sees them. Defaulted for hand-built InspectionResults.
    role_tool_grants: tuple[str, ...] = ()


def _record_pending_receipt(slug_dir: Path, receipt_id: str) -> None:
    """#331 (Sol r6-2/r7-2): durably record WHICH receipt the pending
    candidate was committed with, so the boot sweep exempts exactly that
    receipt — a same-slug receipt for a different root cannot resume this
    pending tuple (cross-root restage refuses), so newest-by-mtime alone
    could keep the wrong one and sweep the only usable one.

    STRICT, and called BEFORE the desired stage in the same locked step: a
    write failure fails the commit (the bundle rollback handles it) rather
    than silently leaving a pending tuple whose marker never existed; the
    marker-first ordering means desired.yaml never exists without it. The
    filename is journalled in TUPLE_FILENAMES, so compensation restores it
    with the rest of the tuple state."""
    from atomic_io import atomic_write_json

    slug_dir.mkdir(parents=True, exist_ok=True)   # fresh install: dir not yet staged
    atomic_write_json(slug_dir / "pending-receipt.json",
                      {"receipt_id": receipt_id})


def _clear_pending_receipt(slug_dir: Path) -> None:
    try:
        (slug_dir / "pending-receipt.json").unlink()
    except OSError:
        pass


def reclaim_staging_tree(staged_dir: "Path | str") -> None:
    """#306: best-effort removal of an inspection staging tree once the flow
    that produced it is terminally done with it (rejected inspection, or a
    commit that reached state="active").

    Containment guard: only a direct child of a directory named ``.staging``
    is ever removed — a hand-built InspectionResult (or a test's staged_dir
    living elsewhere) can never aim this at an arbitrary path. Never raises."""
    path = Path(staged_dir)
    if path.parent.name != ".staging":
        return
    shutil.rmtree(path, ignore_errors=True)


def sweep_staging_aged(*, roots: "Iterable[Path]",
                       max_age_s: float = 7 * 24 * 3600,
                       now: "float | None" = None,
                       keep_paths: "frozenset[str] | set[str]" = frozenset(),
                       ) -> int:
    """#306: boot-time age sweep for abandoned staging trees (operator denied
    or abandoned the consent prompt, or the process died mid-flow), mirroring
    ``specialist_receipt.sweep_aged``'s 7-day cutoff so a staged tree never
    outlives the receipt that could still consume it. Sweeps every direct
    subdirectory of each root older than ``max_age_s``. Never raises; returns
    the count removed.

    ``keep_paths`` (#331, Sol r5-2): staged paths a RETAINED receipt still
    references (a live pending-configuration install's attested bytes) —
    age alone must never delete them, or the supported configure re-commit
    dead-ends at staged_dir_invalid after a week plus one restart."""
    import time as _time

    cutoff = (now if now is not None else _time.time()) - max_age_s
    kept = {str(Path(p)) for p in keep_paths}
    removed = 0
    for root in roots:
        root = Path(root)
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if str(entry) in kept:
                    continue
                if (entry.is_dir() and not entry.is_symlink()
                        and entry.stat().st_mtime < cutoff):
                    shutil.rmtree(entry, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
    return removed


def compute_install_root_digest(
    component: "SpecialistComponent", dependencies: tuple[DependencyResolution, ...],
    *, manifest_bytes: bytes,
) -> str:
    """Round-2 fix (finding #2): `component.checksum` (Plan 1 Task 13) only
    covers role.yaml/doctrine.md/config-schema.json — NOT manifest.json
    itself, NOT the bundled persona pack, NOT corpus bytes, NOT the pinned
    plugin digest. Operator consent and CAS addressing must attest to the
    FULL closure, not a 3-file subset. This is the identity `commit_
    specialist_install`/`upgrade_specialist` bind consent AND the CAS
    directory name to; it is ALWAYS recomputed fresh from re-loaded bytes,
    never trusted from a caller-supplied field."""
    from canonical_bytes import checksum_bytes, checksum_json

    return checksum_json({
        "component_checksum": component.checksum,
        "manifest_checksum": checksum_bytes(manifest_bytes),
        "dependency_digests": sorted(d.digest for d in dependencies),
    })


def resolve_and_fetch(
    repo: str, ref: str, subdir: str, dest: Path, *, expected_revision: str | None = None,
) -> str:
    """Resolve *ref* to a commit sha (guarding against a moved tag exactly
    like `plugin_add`'s `_resolve_and_guard`, tools.py) then fetch that
    EXACT commit's subtree — never a mutable branch fetch. Raises
    SpecialistInstallError on any resolve/fetch failure; never partially
    populates *dest* on failure (fetch_commit_tree extracts to a temp dir
    first)."""
    import plugin_store

    try:
        commit = plugin_store.resolve_ref(repo, ref)
    except plugin_store.RefNotFound as exc:
        raise SpecialistInstallError("ref_not_found", str(exc)) from exc
    except plugin_store.ResolveAuthFailed as exc:
        raise SpecialistInstallError("resolve_auth_failed", str(exc)) from exc
    except plugin_store.SourceEmpty as exc:
        raise SpecialistInstallError("source_empty", str(exc)) from exc
    except plugin_store.ResolveUnavailable as exc:
        raise SpecialistInstallError("resolve_unavailable", str(exc)) from exc
    if expected_revision is not None:
        want = plugin_store.normalize_revision(expected_revision)
        if want is None or want != commit:
            raise SpecialistInstallError(
                "revision_mismatch",
                f"expected_revision {expected_revision!r} does not match resolved "
                f"commit {commit!r} for {repo}@{ref}",
            )
    try:
        plugin_store.fetch_commit_tree(repo, commit, subdir, dest, timeout=300.0)
    except plugin_store.StoreError as exc:
        raise SpecialistInstallError(
            getattr(exc, "reason_code", "fetch_failed"), str(exc)) from exc
    return commit


def resolve_dependency_closure(
    component: SpecialistComponent, component_dir: Path,
) -> tuple[DependencyResolution, ...]:
    """Resolve every typed dependency's AVAILABILITY (spec §2.4). Convention
    (this plan, "Component repository layout"): `persona` and `corpus/data`
    dependencies are bundled INSIDE the component repo (`persona/`,
    `corpus/<identifier>/`) so a fresh install never depends on the TARGET
    image already having a matching blob; `plugin/implementation`
    dependencies reference an ALREADY plugin_add-installed plugin (a
    component never bundles plugin code, only pins which published artifact
    digest it was validated against)."""
    import plugin_registry
    from persona_pack import PersonaPackError, load_persona_pack
    from plugin_store import content_checksum

    out: list[DependencyResolution] = []
    for dep in component.dependencies:
        if dep.kind == "persona":
            pack_dir = component_dir / "persona" / "pack"
            manifest_path = component_dir / "persona" / "manifest.json"
            if not pack_dir.is_dir() or not manifest_path.is_file():
                out.append(DependencyResolution(
                    kind=dep.kind, identifier=dep.identifier, digest=dep.digest,
                    available=False, detail="bundled persona/ directory is missing"))
                continue
            try:
                pack = load_persona_pack(pack_dir, manifest_path)
            except PersonaPackError as exc:
                out.append(DependencyResolution(
                    kind=dep.kind, identifier=dep.identifier, digest=dep.digest,
                    available=False, detail=f"bundled persona invalid: {exc}"))
                continue
            # Whole-branch review F3 (persona identity binding): a matching
            # checksum alone proves the bundled bytes are INTERNALLY
            # consistent, NOT that they are the persona the operator
            # approved. Require, in addition to the checksum, that the
            # bundled pack's own identity (`persona_id@version`) IS the
            # declared dependency identifier AND that it matches the
            # manifest's `default_persona` ref+checksum — otherwise a
            # component could ship persona Y under a dependency line naming
            # persona X and slip the substitution past consent. Any mismatch
            # flows into `dependency_unavailable` at both inspect and the
            # commit-time re-verification.
            pack_ref = f"{pack.persona_id}@{pack.version}"
            checksum_ok = pack.checksum == dep.digest
            identity_ok = pack_ref == dep.identifier
            default_ok = (
                component.default_persona_ref == dep.identifier
                and component.default_persona_checksum == dep.digest
            )
            available = checksum_ok and identity_ok and default_ok
            if available:
                detail = ""
            elif not identity_ok:
                detail = (f"bundled persona is {pack_ref!r}, not the declared "
                          f"dependency {dep.identifier!r}")
            elif not checksum_ok:
                detail = "bundled persona checksum does not match manifest"
            else:
                detail = (
                    "bundled persona does not match the component's declared default_persona "
                    f"({component.default_persona_ref}#{component.default_persona_checksum})")
            out.append(DependencyResolution(
                kind=dep.kind, identifier=dep.identifier, digest=dep.digest,
                available=available, detail=detail))
        elif dep.kind == "corpus/data":
            # F4: reject a hostile/unsafe corpus identifier BEFORE it ever
            # indexes a filesystem join (`component_dir / "corpus" /
            # identifier`) — nothing outside component_dir is stat'd/hashed.
            if not is_safe_corpus_identifier(dep.identifier):
                out.append(DependencyResolution(
                    kind=dep.kind, identifier=dep.identifier, digest=dep.digest,
                    available=False, detail="unsafe corpus identifier"))
                continue
            corpus_dir = component_dir / "corpus" / dep.identifier
            if not corpus_dir.is_dir():
                out.append(DependencyResolution(
                    kind=dep.kind, identifier=dep.identifier, digest=dep.digest,
                    available=False, detail=f"bundled corpus/{dep.identifier}/ is missing"))
                continue
            # Round-2 fix (finding #6): plugin_store.content_checksum returns a
            # BARE hex digest; ComponentDependency.digest is schema-constrained
            # to `sha256:<hex>` (specialist-component.v1.json). Comparing the
            # bare form directly against the prefixed field can never match —
            # normalize here, the ONE place a bare content_checksum() result
            # crosses into a sha256:-prefixed digest field.
            digest = "sha256:" + content_checksum(corpus_dir)
            available = digest == dep.digest
            out.append(DependencyResolution(
                kind=dep.kind, identifier=dep.identifier, digest=dep.digest,
                available=available,
                detail="" if available else "bundled corpus checksum does not match manifest"))
        elif dep.kind == "plugin/implementation":
            if dep.source is not None:
                # Task 8 (spec §1/§3.2.1): a SOURCED dep resolves its tree from
                # component_dir itself — bundled -> the manifest-declared
                # subtree, github -> the ".dep-plugins" convention every
                # closure call site (inspect/CAS-staging/final-CAS/rollback)
                # shares unconditionally (see inspect_specialist_repo's
                # github-fetch loop) — instead of an already-installed
                # registry plugin (the sourceless/legacy branch below).
                if dep.source.type == "bundled":
                    tree = component_dir / dep.source.path
                else:
                    tree = component_dir / ".dep-plugins" / dep.identifier
                if not tree.is_dir():
                    out.append(DependencyResolution(
                        kind=dep.kind, identifier=dep.identifier, digest=dep.digest,
                        available=False, detail="sourced plugin tree missing"))
                    continue
                if dep.source.type == "bundled":
                    # Containment: specialist_component's loader already
                    # rejects a non-canonical (absolute/traversal) path
                    # string, but a symlinked component_dir (or a path
                    # component that is itself a symlink) could still let the
                    # RESOLVED tree escape — assert the resolved location
                    # stays inside the resolved component_dir before this
                    # tree is ever read/hashed.
                    try:
                        tree.resolve().relative_to(component_dir.resolve())
                    except ValueError:
                        out.append(DependencyResolution(
                            kind=dep.kind, identifier=dep.identifier, digest=dep.digest,
                            available=False,
                            detail="bundled plugin path escapes the component"))
                        continue
                detail, surfaces = _validate_sourced_plugin_tree(
                    tree, slug=component.slug, identifier=dep.identifier)
                if detail:
                    out.append(DependencyResolution(
                        kind=dep.kind, identifier=dep.identifier, digest=dep.digest,
                        available=False, detail=detail))
                    continue
                digest = "sha256:" + content_checksum(tree)
                out.append(DependencyResolution(
                    kind=dep.kind, identifier=dep.identifier, digest=dep.digest,
                    available=(digest == dep.digest),
                    detail="" if digest == dep.digest else
                           "sourced plugin content does not match the pinned digest",
                    mcp_servers=surfaces.mcp_servers,
                    protected_tools=surfaces.protected_tools,
                    env_names=surfaces.env_names))
                continue
            # Legacy/sourceless (spec §1 "no source -> legacy behavior"):
            # UNCHANGED — the dependency must already be plugin_add-installed.
            #
            # Round-2 fix (finding #6): a registry entry's `artifact_id` is
            # `plugin_registry.compute_artifact_id` — sha256(repo + "\n" +
            # revision + "\n" + subdir + "\n" + name), an IDENTITY hash of the
            # plugin's SOURCE COORDINATES. It is never equal to, and shares no
            # meaningful relationship with, a content checksum — the previous
            # `artifact_id.endswith(digest_suffix)` comparison could never
            # match two independently-computed 64-hex strings by construction.
            # The REAL way to verify an installed plugin's CONTENT: resolve it
            # through plugin_registry (which already deep-validates identity +
            # stored content_checksum via plugin_store.artifact_verdict at
            # snapshot-build time) and hash its on-disk artifact directory the
            # same way plugin_store always does.
            resolved = next(
                (p for p in plugin_registry.resolve_all().plugins
                 if p.name == dep.identifier), None,
            )
            if resolved is None:
                out.append(DependencyResolution(
                    kind=dep.kind, identifier=dep.identifier, digest=dep.digest,
                    available=False, detail=(
                        f"plugin {dep.identifier!r} is not registered/valid — "
                        f"plugin_add it first")))
                continue
            current_digest = "sha256:" + content_checksum(Path(resolved.path))
            available = current_digest == dep.digest
            out.append(DependencyResolution(
                kind=dep.kind, identifier=dep.identifier, digest=dep.digest,
                available=available,
                detail="" if available else (
                    f"plugin {dep.identifier!r} is installed but its current content checksum "
                    f"does not match the pinned digest — re-publish or re-plugin_add it")))
        else:
            out.append(DependencyResolution(
                kind=dep.kind, identifier=dep.identifier, digest=dep.digest,
                available=False, detail=f"unknown dependency kind {dep.kind!r}"))

    # Whole-branch review F2 (round 2, persona dependency row required
    # STRUCTURALLY): the per-dependency persona LEG above only fires when a
    # persona row is PRESENT. A manifest with `dependencies: []` (or with no
    # kind=="persona" row) would otherwise activate its bundled default persona
    # with NO identity/checksum binding at all — the exact substitution the
    # persona-identity check (F3, Plan-1) exists to prevent, bypassed by simple
    # absence. The component-layout contract requires EXACTLY ONE kind=="persona"
    # dependency whose identifier/digest match the manifest's `default_persona`
    # 1:1. The COUNT invariant (exactly one) is the part the per-row loop cannot
    # see — for a single persona row, the loop already fails-closed on any
    # identity/checksum/default_persona mismatch (its `available` requires
    # identity_ok and default_ok), so appending here ONLY for count != 1 avoids
    # a duplicate resolution while still refusing absence (0 rows) and ambiguity
    # (2+ rows). Enforced in the single closure `inspect_specialist_repo` and
    # `commit_specialist_install` both route through, so it flows into
    # `dependency_unavailable` at BOTH inspect and commit.
    persona_deps = [d for d in component.dependencies if d.kind == "persona"]
    if len(persona_deps) != 1:
        out.append(DependencyResolution(
            kind="persona", identifier=component.default_persona_ref,
            digest=component.default_persona_checksum, available=False,
            detail="persona dependency row missing/mismatched"))
    return tuple(out)


# Task 8 (spec §1, §3.2.1) — prohibition/error-code prefixes a sourced
# plugin's validation detail can start with. `inspect_specialist_repo` scans
# the closure's unavailable rows for these prefixes and raises the matching
# SpecialistInstallError kind INSTEAD OF the generic `dependency_unavailable`
# (Sol plan-r1: a bundle transaction must surface WHY a sourced dep was
# refused, not fold every refusal into one undifferentiated code).
BUNDLED_SYSREQS_UNSUPPORTED = "bundled_sysreqs_unsupported"
BUNDLED_TRIGGERS_UNSUPPORTED = "bundled_triggers_unsupported"
ENV_NAME_COLLISION = "env_name_collision"
# Unlike casa.triggers, a sourced/bundled dependency MAY
# declare casa.callbacks — a callback grants no turn/memory access, so there
# is no reason to extend the triggers prohibition to it. The one thing that
# DOES need an inspect-time gate: a bundled dep's OWNED registry entry is
# scoped (`slug.manifest_name`, up to 73 chars — plugin_callbacks.py's own
# comment), longer than the identifier alone. `manifest_callbacks` (via
# `_validate_sourced_plugin_tree`'s later `validate_manifest` call) only
# ever checks the effective name against the unscoped `identifier` (mirrors
# triggers' runtime-name doctrine), so a callback declaration that
# fits under the identifier could still overflow once scoped — catch that
# HERE, against the scoped name, before it can ever reach the registry.
CALLBACK_NAME_TOO_LONG = "callback_name_too_long"
# casa.emits/casa.subscribes follow the SAME carve-out as casa.callbacks — a
# sourced/bundled dependency MAY declare either: casa.emits is inert
# without a consented subscriber (declaring a name grants no turn or memory
# access by itself, exactly like a callback), and casa.subscribes only ever
# wakes the plugin on a real occurrence elsewhere, operator-consented at
# reconcile time (mirrors plugin_triggers' consent posture, never automatic
# on install). The one thing that DOES need an inspect-time gate, mirroring
# CALLBACK_NAME_TOO_LONG exactly: an emitted event's effective name routes
# under the bundled dep's SCOPED registry name (`slug.identifier`), longer
# than the bare identifier `manifest_emits` (via `validate_manifest`) checks
# internally — catch the overflow HERE, before it can ever reach the
# registry.
EVENT_NAME_TOO_LONG = "event_name_too_long"
_PROHIBITION_KIND_PREFIXES = (
    BUNDLED_SYSREQS_UNSUPPORTED, BUNDLED_TRIGGERS_UNSUPPORTED, ENV_NAME_COLLISION,
    CALLBACK_NAME_TOO_LONG, EVENT_NAME_TOO_LONG,
)


def _env_name_conflicts(tree_env_names: "set[str]", *, exclude_owner: str) -> "list[str]":
    """Global env-name collision check (spec §1 "Global-namespace collision
    preflight"; brief Step 7). `tree_env_names` are the `${VAR}` references a
    sourced plugin's OWN `.mcp.json` requires (plugin_env_extractor —
    ownership-blind, NOT plugin_env_conf, which stores VALUES not per-plugin
    ownership). #431: BOTH reference forms count on BOTH sides — a collision
    is about which names are claimed, not which must resolve, and reading one
    side with the requirement set would let a `${VAR:-}` on EITHER side hide
    the clash. The installed-side inventory is the same extraction run over
    every OTHER validated installed artifact's `.mcp.json`
    (`plugin_registry.resolve_all().plugins`), excluding entries owned by
    `exclude_owner` (the slug's own set, being replaced on upgrade — its own
    prior env names are not a collision with its own new ones).

    A monkeypatchable module-level seam by design (Step 3 of the Task 8
    brief) — tests stub this directly rather than building a real registry +
    store fixture to exercise the collision-kind wiring."""
    import plugin_env_extractor
    import plugin_registry

    owned_names = {
        e["name"] for e in plugin_registry.owned_entries_for(
            exclude_owner, plugin_registry.snapshot_registry())
    }
    installed_names: "set[str]" = set()
    for rp in plugin_registry.resolve_all().plugins:
        if rp.name in owned_names:
            continue
        installed_names |= plugin_env_extractor.extract_referenced_env_vars(
            Path(rp.path) / ".mcp.json")
    return sorted(tree_env_names & installed_names)


def _sibling_env_name_collisions(
    dependencies: "tuple[DependencyResolution, ...]",
) -> "list[str]":
    """Whole-branch E: `_env_name_conflicts` (per-plugin) only checks a sourced
    plugin's env names against ALREADY-INSTALLED artifacts — never against its
    SIBLINGS in the same incoming bundle. Two bundled plugins each requiring the
    same `${VAR}` would both pass (the name is not yet installed) and both
    publish, then collide in the global env namespace. Aggregate every sourced
    plugin's env surface across the closure and flag any name required by more
    than one sibling. Each DependencyResolution.env_names is already a set, so a
    duplicate across the aggregate means DISTINCT siblings share the name."""
    from collections import Counter

    counts: "Counter[str]" = Counter()
    for dep in dependencies:
        if dep.kind == "plugin/implementation" and dep.env_names:
            counts.update(set(dep.env_names))
    return sorted(name for name, count in counts.items() if count > 1)


def _manifest_name_collisions(component: SpecialistComponent) -> "list[str]":
    """Per-target manifest-name precheck (spec §2.1, brief): a sourced dep's
    `identifier` becomes its OWNED entry's effective runtime name
    (`manifest_name`). If some OTHER already-registered entry targeting this
    same `specialist:<slug>` already resolves to that same effective name,
    committing this install would collide at load time (the registry's own
    per-target uniqueness invariant would then quarantine one of them). Fail
    BEFORE any staging/consent, not after. Entries owned by THIS slug are
    excluded — an upgrade legitimately replaces its own prior owned set with
    identical names."""
    import plugin_registry

    sourced_idents = {
        d.identifier for d in component.dependencies
        if d.kind == "plugin/implementation" and d.source is not None
    }
    if not sourced_idents:
        return []
    target = f"specialist:{component.slug}"
    collisions: list[str] = []
    for entry in plugin_registry.snapshot_registry().entries:
        if target not in entry.get("targets", []):
            continue
        if plugin_registry.entry_owner(entry) == target:
            continue  # this slug's own (prior) owned set — replaced, not a collision
        effective = entry.get("manifest_name") or entry.get("name")
        if effective in sourced_idents:
            collisions.append(effective)
    return collisions


@dataclass(frozen=True, slots=True)
class _PluginSurfaces:
    """Task 8 fix-round-1 (consent-review CRITICAL, spec §3.2): the three
    consent-enumeration surfaces `_validate_sourced_plugin_tree` extracts
    from the manifest/`.mcp.json` it already parses while validating —
    captured once here (into the row `resolve_dependency_closure` builds)
    rather than re-parsed at the PluginReceiptRow-building site in
    `inspect_specialist_repo`. Empty (the default) for a tree that failed
    validation before reaching the extraction point."""
    mcp_servers: tuple[str, ...] = ()
    protected_tools: tuple[str, ...] = ()
    env_names: tuple[str, ...] = ()


_EMPTY_SURFACES = _PluginSurfaces()


# #431: the ``.mcp.json`` expansion carve-out, covering BOTH documented
# forms — ``${VAR}`` and ``${VAR:-default}``. It used to match only the bare
# form, so a defaulted reference left ``${`` in the leaf and tripped the
# forbidden-marker gate: a BUNDLED plugin was refused at install for syntax a
# standalone plugin may use freely. That asymmetry was the bug.
_MCP_JSON_VAR_RE = re.compile(
    r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::-(?P<default>[^{}]*))?\}")


def _realize_mcp_expansions(text: str) -> str:
    """The string as the CLI will actually produce it, for marker scanning.

    A bare ``${VAR}`` is DELETED: its value comes from the environment at
    spawn time, so the plugin author does not control it and cannot smuggle
    anything through it. A ``${VAR:-default}`` is REPLACED BY ITS DEFAULT,
    because that text IS author-controlled and is what the CLI substitutes
    when the variable is unset.

    Substituting rather than deleting is the whole point (r1, Sol): deleting
    the expansion lets a marker be assembled from the text AROUND it —
    ``"<${NEVER_SET:-script}>"`` scans as ``<>`` and passes, then expands to
    ``<script>`` at runtime. Restricting which characters may appear inside
    the default cannot fix that, because neither half is a marker on its own.
    Scanning the realized form is closed under this by construction."""
    return _MCP_JSON_VAR_RE.sub(lambda m: m.group("default") or "", text)


def _walk_reject_markers_in_json(value: object) -> None:
    """Parsed-leaf marker scan (mirrors `authored_markers.
    reject_markers_in_parsed`'s dict/list/str walk) with a `${VAR}` carve-out
    applied PER STRING LEAF before the marker check. See
    `_reject_forbidden_markers_in_json` for why both pieces (parsed-leaf, not
    raw-text; `${VAR}`-stripped, not blanket) are required together."""
    if isinstance(value, str):
        if contains_forbidden_marker(_realize_mcp_expansions(value)):
            raise ValueError("template, include, HTML, or delimiter detected")
    elif isinstance(value, dict):
        for key, item in value.items():
            _walk_reject_markers_in_json(key)
            _walk_reject_markers_in_json(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _walk_reject_markers_in_json(item)


def _reject_forbidden_markers_in_json(text: str) -> None:
    """Task 8 fix-round-1 (found while wiring the mcp_servers/protected_tools/
    env_names consent surfaces): a plain `reject_forbidden_markers(text)`
    raw-text scan has TWO false-positive sources on real `plugin.json`/
    `.mcp.json` content, both unconditional:

    1. `${VAR}` env-var interpolation is the UNIVERSAL, legitimate
       `.mcp.json` syntax every real plugin uses — `${CLAUDE_PLUGIN_ROOT}`
       in a `command`/`args` entry, `${MY_SECRET}` in an `env` entry (see
       `tests/test_env_var_extraction.py`, `plugin_env_extractor`'s own
       module docstring) — never a template-injection attempt.
    2. Nested JSON objects routinely end two (or more) closing braces in a
       row — e.g. `{"casa": {"protectedTools": ["x"]}}` or
       `{"env": {"K": "V"}}` — an exact byte-for-byte match for the
       forbidden Jinja-close marker `}}`, on pure JSON structural
       punctuation. `casa.protectedTools`/`casa.systemRequirements`/
       `casa.triggers` are ALL nested one level under `casa`, so this hits
       `plugin.json` itself for precisely the shapes this fix-round exists
       to enumerate. The identical class of false positive already forced a
       raw-scan carve-out for role.yaml's flow-style YAML
       (`_extract_full_line_yaml_comments`); `authored_markers.
       reject_markers_in_parsed` is this codebase's existing canonical fix
       for exactly this — scan the PARSED tree's string leaves, never the
       raw structural bytes.

    Both, together, made this scan reject EVERY real `plugin.json` declaring
    `casa.protectedTools` (etc.) and EVERY real `.mcp.json` a sourced/bundled
    plugin dependency could ever declare, unconditionally, before step 4
    (`validate_manifest`)/6/7 below could ever run — silently making Task
    8's own protectedTools/env-name-collision handling dead code for any
    plugin with a realistic manifest or MCP server config.

    Fix: parse first, walk parsed string leaves only (immune to JSON's own
    `}}`), stripping `${VAR}` per leaf before the marker check (still catches
    a genuine `{{`/`{%`/`!include`/HTML-tag/structural-tag smuggled into any
    string value). Malformed JSON falls back to the original blanket raw-text
    scan (`${VAR}`-stripped) — `plugin_store`'s own parse/verdict gates are
    what actually gate malformed JSON; this is defense in depth over
    whatever text is there, not the primary check."""
    try:
        parsed = json.loads(text)
    except ValueError:
        reject_forbidden_markers(_realize_mcp_expansions(text))
        return
    _walk_reject_markers_in_json(parsed)


def _mcp_server_summary(name: str, cfg: "Mapping") -> str:
    """One-line "name: command arg1 arg2…" consent-surface summary (spec
    §3.2) for a single validated `.mcp.json` server entry. A `url`-form
    server (no `command`) summarizes as "name: url <url>" instead — still a
    single line naming exactly what the operator's tap would approve."""
    command = cfg.get("command")
    if isinstance(command, str) and command:
        args = cfg.get("args")
        arg_list = [a for a in args if isinstance(a, str)] if isinstance(args, list) else []
        return f"{name}: " + " ".join([command] + arg_list)
    url = cfg.get("url")
    if isinstance(url, str) and url:
        return f"{name}: url {url}"
    return f"{name}: (unrecognized server config)"


def _validate_sourced_plugin_tree(
    tree: Path, *, slug: str, identifier: str,
) -> "tuple[str, _PluginSurfaces]":
    """Full validation of a sourced (bundled/github) plugin dependency's
    staged tree (spec §1/§3.2.1, brief Step 2). Returns `("", surfaces)` when
    the tree is clean (`surfaces` populated for the consent DM — spec §3.2);
    otherwise `(detail, _EMPTY_SURFACES)` with a non-empty detail string —
    for the prefixes in `_PROHIBITION_KIND_PREFIXES`,
    `inspect_specialist_repo` raises that exact kind; every other non-empty
    detail flows into the generic `dependency_unavailable`.

    Order (Sol plan-r1 — prohibition codes must never be preempted by
    `validate_manifest`'s own `apt_requirements_rejected`/`triggers_invalid`,
    which are legacy per-plugin refusals, not this bundle's distinct codes):

    1. Normalize FIRST (`plugin_store.strip_bytecode_derivatives`) — the SAME
       normalization `_stage_and_swap` applies at publish, run before any
       digest is computed so receipt attestation covers exactly the bytes
       publish will checksum.
    2. Reject an escaping symlink (`plugin_store._reject_escaping_symlinks`).
    3. Prohibitions on the RAW manifest, read directly (before
       `validate_manifest` gets a chance to raise its OWN, differently-coded,
       refusal for the same underlying key): any `manifest_sysreqs` row ⇒
       `bundled_sysreqs_unsupported`; any `casa.triggers` KEY present (even
       malformed) ⇒ `bundled_triggers_unsupported`. `casa.callbacks` and
       `casa.emits`/`casa.subscribes` are NOT prohibited — instead, any
       declared callback/emit entry whose effective name computed against
       the SCOPED registry name (`slug.identifier`, longer than `identifier`
       alone) exceeds `plugin_callbacks.MAX_EFFECTIVE_LEN` /
       `plugin_events.MAX_EFFECTIVE_LEN` ⇒ `callback_name_too_long` /
       `event_name_too_long`.
    4. `plugin_store.validate_manifest` (identity: `plugin.json::name` must
       equal `identifier`; this is also where a non-prohibited
       `apt_requirements_rejected`/`triggers_invalid`/`name_mismatch`/etc.
       surfaces for anything the raw-manifest prohibitions above did not
       already catch — impossible for sysreqs/triggers by construction, but
       real for name mismatches and malformed protectedTools).
    5. Untrusted-bytes marker scan over `plugin.json`, every `*.md`, and every
       `.mcp.json` under the tree (symlinks already vetted in step 2); the
       `plugin.json`/`.mcp.json` scans (fix-round-1) walk PARSED string
       leaves (immune to JSON's own structural `}}`) and strip legitimate
       `${VAR}` env-var interpolation per leaf — see
       `_reject_forbidden_markers_in_json`. `*.md` prose stays a raw scan.
    6. Reserved-env + command-verdict gates over the tree's `.mcp.json`
       (`plugin_store.reserved_env_violations` / `mcp_command_verdicts`).
    7. Env-name collision (`_env_name_conflicts`) against every OTHER
       installed plugin's required env names, excluding this slug's own
       (prior) owned set.
    """
    import plugin_env_extractor
    import plugin_registry
    import plugin_store

    plugin_store.strip_bytecode_derivatives(tree)
    try:
        plugin_store._reject_escaping_symlinks(tree)
    except plugin_store.StoreError as exc:
        return f"{exc.reason_code}: {exc}", _EMPTY_SURFACES

    manifest_path = tree / ".claude-plugin" / "plugin.json"
    try:
        raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"manifest_invalid: plugin.json missing/unparseable: {exc}", _EMPTY_SURFACES
    if not isinstance(raw_manifest, dict):
        return "manifest_invalid: plugin.json is not an object", _EMPTY_SURFACES

    try:
        has_sysreqs = bool(plugin_store.manifest_sysreqs(raw_manifest))
    except plugin_store.StoreError:
        # #354: manifest_sysreqs is now strict — a malformed declaration is
        # still a PRESENT declaration, and this path prohibits any.
        has_sysreqs = True
    if has_sysreqs:
        return (f"{BUNDLED_SYSREQS_UNSUPPORTED}: a sourced/bundled plugin dependency "
                "must not declare casa.systemRequirements", _EMPTY_SURFACES)
    casa = raw_manifest.get("casa")
    if isinstance(casa, dict) and "triggers" in casa:
        return (f"{BUNDLED_TRIGGERS_UNSUPPORTED}: a sourced/bundled plugin dependency "
                "must not declare casa.triggers", _EMPTY_SURFACES)

    scoped = plugin_registry.scoped_name(slug, identifier)

    # casa.callbacks IS permitted for a bundled dep (unlike
    # triggers), but its OWNED registry entry routes under the SCOPED name —
    # check the scoped-name effective length here, before validate_manifest
    # below (which only ever sees the unscoped identifier).
    if isinstance(casa, dict) and isinstance(casa.get("callbacks"), list):
        import plugin_callbacks
        for entry in casa["callbacks"]:
            if not isinstance(entry, dict):
                continue
            cb_name = entry.get("name")
            if not isinstance(cb_name, str):
                continue
            eff = plugin_callbacks.effective_name(scoped, cb_name)
            if len(eff) > plugin_callbacks.MAX_EFFECTIVE_LEN:
                return (
                    f"{CALLBACK_NAME_TOO_LONG}: bundled dependency {identifier!r} "
                    f"callback {cb_name!r} scoped effective name {eff!r} exceeds "
                    f"{plugin_callbacks.MAX_EFFECTIVE_LEN} chars", _EMPTY_SURFACES)

    # casa.emits IS permitted for a bundled dep (same carve-out as
    # casa.callbacks), but its OWNED registry entry routes under the SCOPED
    # name — check the scoped-name effective length here, mirroring the
    # callbacks gate above, before validate_manifest below (which only ever
    # sees the unscoped identifier).
    if isinstance(casa, dict) and isinstance(casa.get("emits"), list):
        import plugin_events
        for entry in casa["emits"]:
            if not isinstance(entry, dict):
                continue
            ev_name = entry.get("name")
            if not isinstance(ev_name, str):
                continue
            eff = plugin_events.effective_name(scoped, ev_name)
            if len(eff) > plugin_events.MAX_EFFECTIVE_LEN:
                return (
                    f"{EVENT_NAME_TOO_LONG}: bundled dependency {identifier!r} "
                    f"emit {ev_name!r} scoped effective name {eff!r} exceeds "
                    f"{plugin_events.MAX_EFFECTIVE_LEN} chars", _EMPTY_SURFACES)

    try:
        plugin_store.validate_manifest(tree, scoped, manifest_name=identifier)
    except plugin_store.StoreError as exc:
        return f"{exc.reason_code}: {exc}", _EMPTY_SURFACES

    try:
        _reject_forbidden_markers_in_json(manifest_path.read_text(encoding="utf-8"))
        for md_path in sorted(tree.rglob("*.md")):
            reject_forbidden_markers(md_path.read_text(encoding="utf-8"))
        for mcp_path in sorted(tree.rglob(".mcp.json")):
            _reject_forbidden_markers_in_json(mcp_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return f"forbidden_markers: {exc}", _EMPTY_SURFACES
    except OSError as exc:
        return f"forbidden_markers: unreadable file during marker scan: {exc}", _EMPTY_SURFACES

    mcp_json_path = tree / ".mcp.json"
    reserved = plugin_store.reserved_env_violations(mcp_json_path)
    if reserved:
        return "mcp_reserved_env: " + "; ".join(reserved), _EMPTY_SURFACES
    missing = [v for v in plugin_store.mcp_command_verdicts(mcp_json_path, tree)
               if v.get("status") == "missing"]
    if missing:
        return ("mcp_command_missing: " + "; ".join(
            f"{v['server']}:{v['ref']} ({v.get('reason', '')})" for v in missing), _EMPTY_SURFACES)

    # #431: BOTH reference forms here — the consent enumeration and the
    # collision preflight are about which names the tree touches, not which
    # must resolve. Using the requirement set would let a bundled plugin
    # reuse a name another plugin owns by writing ``${VAR:-}``.
    tree_env_names = plugin_env_extractor.extract_referenced_env_vars(
        mcp_json_path)
    conflicts = _env_name_conflicts(tree_env_names, exclude_owner=slug)
    if conflicts:
        return f"{ENV_NAME_COLLISION}: colliding env name(s): " + ", ".join(conflicts), _EMPTY_SURFACES

    # Task 8 fix-round-1: the consent-enumeration surfaces (spec §3.2),
    # extracted from state already parsed above — `raw_manifest` (step 3/4)
    # and `mcp_json_path`/`tree_env_names` (step 6/7) — never re-read from
    # disk. Sorted for determinism (dict/set iteration order is not a
    # contract either the manifest author or `extract_env_vars` promises).
    surfaces = _PluginSurfaces(
        mcp_servers=tuple(
            _mcp_server_summary(name, cfg)
            for name, cfg in sorted(plugin_store.mcp_servers_map(mcp_json_path).items())),
        protected_tools=tuple(
            e["name"] for e in plugin_store.manifest_protected_tools(raw_manifest)),
        env_names=tuple(sorted(tree_env_names)),
    )
    return "", surfaces


def _sourced_plugin_manifest_version(tree: Path) -> str:
    """The receipt row's `version` — read AFTER `_validate_sourced_plugin_tree`
    has already fully validated the manifest; mirrors `validate_manifest`'s
    own missing-version default (`"0.0.0"`) without re-running the whole
    validation gate a second time just to extract one field."""
    try:
        manifest = json.loads((tree / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "0.0.0"
    version = manifest.get("version") if isinstance(manifest, dict) else None
    return version if isinstance(version, str) and version else "0.0.0"


def _extract_full_line_yaml_comments(text: str) -> str:
    """Task N2 fix: role.yaml's own legitimate flow-style syntax (e.g.
    `disclosure: {policy: delegated, overrides: {}}`, used by every
    hand-authored role.yaml this repo ships, finance's and mtg's included)
    contains a literal `}}` byte-for-byte identical to the forbidden
    template-close marker — role_artifact.py's own loader deliberately
    never raw-text-scans role.yaml for exactly this reason (see its module
    docstring), relying on the parsed-leaf scan instead. This function
    narrows _validate_untrusted_bytes's raw scan to just the full-line
    comments (a line whose stripped form starts with '#') — the ONE thing
    the parsed-leaf scan structurally cannot see (YAML comments never
    survive parsing) and the ONLY threat model this belt-and-suspenders
    check exists to close (see
    tests/test_specialist_install.py's `_write_component_with_role_yaml_
    comment_marker`) — without re-raw-scanning the structural YAML bytes
    that collide with a forbidden marker by pure syntactic coincidence."""
    return "\n".join(line for line in text.splitlines() if line.strip().startswith("#"))


def _validate_untrusted_bytes(component: SpecialistComponent) -> None:
    """Extra check role_artifact.load_role_artifact does not perform: reject
    templating/HTML/delimiter markers hidden in a YAML COMMENT of the
    FETCHED role.yaml (invisible to the parsed-leaf scan, since comments
    never survive YAML parsing), plus the doctrine.md prose — both
    adversarial input, unlike image-owned role artifacts."""
    import yaml

    role_text = component.role.role_path.read_text(encoding="utf-8")
    try:
        reject_forbidden_markers(_extract_full_line_yaml_comments(role_text))
        reject_forbidden_markers(component.role.doctrine)
    except ValueError as exc:
        raise SpecialistInstallError("forbidden_markers", str(exc)) from exc
    # Belt-and-suspenders: re-serializing role.yaml must not silently absorb
    # a marker that only appears in a value jsonschema doesn't visit.
    # component.role.role is role_artifact.load_role_artifact's
    # canonical_bytes.deep_freeze()-produced tree (nested dict -> MappingProxyType,
    # list -> tuple) — comparing it directly against yaml.safe_load's plain
    # dict/list tree would spuriously mismatch on every list-valued field
    # (list != tuple always, even when every element is equal). Normalize
    # through to_plain_json first so this is a genuine structural-drift
    # check, not a frozen-container-type false positive.
    reparsed = yaml.safe_load(role_text)
    if reparsed != to_plain_json(component.role.role):
        raise SpecialistInstallError(
            "role_artifact_drift", "role.yaml on disk does not match the loaded artifact")


def inspect_specialist_repo(
    repo: str, ref: str, *, subdir: str = "", expected_revision: str | None = None,
    staging_root: Path = Path("/config/specialists/.staging"),
    installed_index: "InstalledSpecialistIndex | None" = None,
    mode: "Literal['install', 'upgrade']" = "install",
    target_slug: str | None = None,
    specialists_dir: Path = Path("/config/specialists"),
    receipts_dir: Path = Path("/config/specialists/.receipts"),
) -> InspectionResult:
    """Fetch for inspection into a NON-PERSISTENT staging directory (spec §6
    N1) — no CAS write, no binding, no activation. Every check that can
    reject an install runs here, BEFORE any operator is ever prompted.

    Round-2 fix (finding #5): `mode="upgrade"` (with a required `target_slug`)
    is the ONLY sanctioned way to inspect a repo for a slug that is ALREADY
    installed — plain `mode="install"` (the default, used for a fresh
    install) always applies the full collision check, so re-inspecting an
    already-installed slug in install mode correctly still fails
    (`check_slug_uniqueness` sees it in `installed_specialist_slugs`) —
    upgrade mode does not weaken that for any OTHER slug, it narrowly
    excludes only `target_slug` after independently confirming an active
    instance of that exact slug already exists (never usable to backdoor a
    fresh install past collision checks under a false 'upgrade' claim).

    Task 8 (spec §1/§3.2.1): fetches every github-sourced plugin dependency
    into `.dep-plugins/<identifier>` under the SAME staging dir, runs a
    per-target manifest-name collision precheck, resolves the full
    dependency closure (now including sourced-plugin validation), raises a
    prohibition's OWN kind (bundled_sysreqs_unsupported/
    bundled_triggers_unsupported/env_name_collision) ahead of the generic
    dependency_unavailable, and — for EVERY inspection, plugin-less
    components included — mints and persists a trusted source receipt
    (`specialist_receipt`) so a bundled/declared plugin closure's provenance
    is bound into consent (`receipt_digest`) and available to commit."""
    import plugin_registry
    import specialist_receipt
    from specialist_registry import InstalledSpecialistIndex, _discover_image_role_slots

    if mode == "upgrade" and not target_slug:
        raise SpecialistInstallError("target_slug_required", "mode='upgrade' requires target_slug")
    if target_slug is not None:
        # F1: a caller-supplied target_slug is joined as `specialists_dir /
        # target_slug` below (InstanceDir.active()) — validate before any join.
        validate_specialist_slug(target_slug)

    staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    component_dir = staging_root / uuid.uuid4().hex
    # #306: any rejection below (manifest_missing/invalid, dependency
    # failures, slug collisions, ...) must not leave the fetched staging
    # tree behind; a SUCCESSFUL inspection retains it for commit reuse.
    try:
        component_revision = resolve_and_fetch(
            repo, ref, subdir, component_dir, expected_revision=expected_revision)

        manifest_path = component_dir / "manifest.json"
        if not manifest_path.is_file():
            raise SpecialistInstallError("manifest_missing", f"{repo}@{ref}: manifest.json not found")
        try:
            component = load_specialist_component(component_dir, manifest_path)
        except (ValueError, jsonschema.ValidationError) as exc:
            # #346: load_specialist_component pins jsonschema.ValidationError —
            # NOT a ValueError subclass — for a schema-violating manifest; both
            # must land in the structured manifest_invalid envelope.
            raise SpecialistInstallError("manifest_invalid", str(exc)) from exc

        _validate_untrusted_bytes(component)

        # Task 8: fetch every github-sourced plugin dependency INTO this same
        # staging tree, at the `.dep-plugins/<identifier>` convention every
        # closure call site (inspect/CAS-staging/final-CAS/rollback) shares
        # unconditionally — `dep.identifier` is already PLUGIN_IDENT_RE-validated
        # (specialist_component's loader), so this join is safe without a
        # separate containment check. Revision-pinned: `resolve_and_fetch` itself
        # refuses a moved `ref` against `dep.source.revision`.
        for dep in component.dependencies:
            if dep.kind == "plugin/implementation" and dep.source is not None \
                    and dep.source.type == "github":
                dest = component_dir / ".dep-plugins" / dep.identifier
                resolve_and_fetch(dep.source.repo, dep.source.ref, "", dest,
                                  expected_revision=dep.source.revision)

        index = installed_index or InstalledSpecialistIndex()
        if installed_index is None:
            index.load()

        if mode == "upgrade":
            if component.slug != target_slug:
                raise SpecialistInstallError(
                    "slug_mismatch",
                    f"upgrade target_slug={target_slug!r} but the fetched component declares "
                    f"slug={component.slug!r} — a slug rename is a fresh install, not an upgrade")
            from personality_binding import InstanceDir
            if InstanceDir(specialists_dir / target_slug).active() is None:
                raise SpecialistInstallError(
                    "no_active_tuple", f"{target_slug!r} has no active install to upgrade")
            fixed_role_slots = _discover_image_role_slots() - {target_slug}
            installed_specialist_slugs = index.installed_slugs() - {target_slug}
        else:
            fixed_role_slots = _discover_image_role_slots()
            installed_specialist_slugs = index.installed_slugs()

        try:
            check_slug_uniqueness(
                candidate_slug=component.slug,
                fixed_role_slots=fixed_role_slots,
                installed_specialist_slugs=installed_specialist_slugs,
            )
        except ValueError as exc:
            raise SpecialistInstallError("slug_collision", str(exc)) from exc

        manifest_name_collisions = _manifest_name_collisions(component)
        if manifest_name_collisions:
            raise SpecialistInstallError(
                "manifest_name_collision",
                f"specialist:{component.slug} already resolves an entry with effective "
                f"manifest name(s) {sorted(set(manifest_name_collisions))!r} — a sourced "
                "plugin dependency must not collide with it")

        dependencies = resolve_dependency_closure(component, component_dir)
        unavailable = [d for d in dependencies if not d.available]
        if unavailable:
            # Task 8: a prohibition (sysreqs/triggers/env-collision) aborts with
            # its OWN kind, not the generic dependency_unavailable — scan every
            # unavailable row first (Sol plan-r1).
            for d in unavailable:
                for prefix in _PROHIBITION_KIND_PREFIXES:
                    if d.detail.startswith(prefix):
                        raise SpecialistInstallError(prefix, f"{d.kind}:{d.identifier}: {d.detail}")
            detail = "; ".join(f"{d.kind}:{d.identifier}: {d.detail}" for d in unavailable)
            raise SpecialistInstallError("dependency_unavailable", detail)

        # Whole-branch E: cross-sibling env-name collision (the per-plugin check
        # only saw installed artifacts). Every remaining dep resolved available.
        sibling_env = _sibling_env_name_collisions(dependencies)
        if sibling_env:
            raise SpecialistInstallError(
                ENV_NAME_COLLISION,
                "sourced plugins in this bundle require the same env name(s): "
                + ", ".join(sibling_env))

        root_digest = compute_install_root_digest(
            component, dependencies, manifest_bytes=manifest_path.read_bytes())

        # Task 8: build one PluginReceiptRow per sourced plugin dependency
        # (dependencies is 1:1 positional with component.dependencies — every
        # row appended by resolve_dependency_closure's per-dependency loop, and
        # any additional synthetic persona-count row would have been `available=
        # False` and already raised above, so this dict is exhaustive here).
        resolved_by_identity = {(d.kind, d.identifier): d for d in dependencies}
        plugin_rows: list[specialist_receipt.PluginReceiptRow] = []
        for dep in component.dependencies:
            if dep.kind != "plugin/implementation" or dep.source is None:
                continue
            resolution = resolved_by_identity[(dep.kind, dep.identifier)]
            if dep.source.type == "bundled":
                tree = component_dir / dep.source.path
                row_repo, row_ref, row_revision = repo, ref, f"git:{component_revision}"
                row_subdir = f"{subdir}/{dep.source.path}" if subdir else dep.source.path
            else:
                tree = component_dir / ".dep-plugins" / dep.identifier
                row_repo, row_ref, row_revision = dep.source.repo, dep.source.ref, dep.source.revision
                row_subdir = ""
            plugin_rows.append(specialist_receipt.PluginReceiptRow(
                identifier=dep.identifier,
                scoped_name=plugin_registry.scoped_name(component.slug, dep.identifier),
                manifest_name=dep.identifier,
                version=_sourced_plugin_manifest_version(tree),
                source_type=dep.source.type,
                repo=row_repo, ref=row_ref, revision=row_revision, subdir=row_subdir,
                content_digest=resolution.digest, staged_path=str(tree),
                # Task 8 fix-round-1 (consent-review CRITICAL): captured by
                # `_validate_sourced_plugin_tree` during `resolve_dependency_
                # closure` above — never re-parsed here.
                mcp_servers=resolution.mcp_servers,
                protected_tools=resolution.protected_tools,
                env_names=resolution.env_names,
            ))

        receipt = specialist_receipt.build_receipt(
            slug=component.slug, component_repo=repo, component_ref=ref,
            component_revision=f"git:{component_revision}", component_subdir=subdir,
            component_staged_path=str(component_dir), plugins=tuple(plugin_rows),
        )
        specialist_receipt.persist(receipt, receipts_dir=receipts_dir)

        required = component.config_schema.get("required", [])
        secret_names = set(component.config_schema.get("secret_names", []))
        logger.info(
            "inspect_specialist_repo passed all gates: mode=%s slug=%s component_id=%s "
            "version=%s root_digest=%s receipt_id=%s (staged at %s, not yet activated)",
            mode, component.slug, component.component_id, component.version,
            root_digest, receipt.receipt_id, component_dir,
        )
        return InspectionResult(
            component_id=component.component_id, version=component.version, slug=component.slug,
            component_checksum=component.checksum, root_digest=root_digest,
            mission=str(component.role.role.get("mission", "")),
            default_persona_ref=component.default_persona_ref,
            default_persona_checksum=component.default_persona_checksum,
            required_config_names=tuple(n for n in required if n not in secret_names),
            required_secret_names=tuple(n for n in required if n in secret_names),
            dependencies=dependencies, staged_dir=component_dir,
            receipt_id=receipt.receipt_id, receipt_digest=receipt.receipt_digest,
            plugin_resolutions=tuple(plugin_rows),
            # #541: casa-framework grants only — CC built-ins are bounded by
            # the role schema, plugin tools by the bundled-plugin blocks.
            role_tool_grants=tuple(
                t for t in ((component.role.role.get("tools") or {})
                            .get("allowed") or ())
                if isinstance(t, str) and t.startswith("mcp__casa-framework")
            ),
        )
    except BaseException:
        shutil.rmtree(component_dir, ignore_errors=True)
        raise


# ---------------------------------------------------------------------------
# CAS addressing (Step 11)
# ---------------------------------------------------------------------------
#
# The CAS store root is /config/specialists/store/<component_checksum-without-
# "sha256:"-prefix>/ (content-addressed, spec §2.5), holding the fetched
# component verbatim (role/, persona/, corpus/, config-schema.json,
# manifest.json) after validation. BindingRecord.component_root (Task 7's
# free-form str | None field) is set to
# f"{component_id}@{version}#{component_checksum}" — human-readable AND
# parseable, so InstalledSpecialistIndex can recover the CAS directory from a
# loaded active.yaml/desired.yaml without a second sidecar file.


def component_root_string(*, component_id: str, version: str, component_checksum: str) -> str:
    return f"{component_id}@{version}#{component_checksum}"


_COMPONENT_CHECKSUM_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def parse_component_root(component_root: str) -> tuple[str, str, str]:
    """Inverse of component_root_string. Raises ValueError on a malformed root
    (never silently returns a partial tuple — a corrupt InstanceTuple's root
    must fail closed, not resolve to a guessed CAS path).

    #346: the checksum segment is pinned to exactly ``sha256:`` + 64 lower-hex
    — ``cas_store_dir`` joins it (prefix-stripped) straight into the store
    root, so a tampered tuple root like ``...#sha256:../../outside`` must
    never parse."""
    head, sep, checksum = component_root.rpartition("#")
    if not sep or not _COMPONENT_CHECKSUM_RE.match(checksum):
        raise ValueError(f"malformed component_root: {component_root!r}")
    component_id, sep2, version = head.rpartition("@")
    if not sep2:
        raise ValueError(f"malformed component_root: {component_root!r}")
    return component_id, version, checksum


def cas_store_dir(
    component_checksum: str, *, store_root: Path = Path("/config/specialists/store"),
) -> Path:
    return store_root / component_checksum.removeprefix("sha256:")


def _publish_cas_staging(cas_staging_dir: Path, cas_dir: Path) -> None:
    """Atomically publish a verified staging directory into its final
    content-addressed `cas_dir` via `os.replace`.

    Md (concurrent-install CAS race): commit/upgrade check `cas_dir.exists()`
    and, if absent, stage + verify + publish. Two installs of the SAME digest
    racing can both pass the exists() check; the loser's `os.replace` then
    lands on a now-populated `cas_dir` and raises `OSError` (ENOTEMPTY —
    POSIX rename refuses a non-empty directory target). CAS content is
    immutable/content-addressed, so the winner's bytes are byte-identical to
    ours: discard our staging copy and return, letting the shared
    re-load-and-verify path downstream run against the winner's `cas_dir`.
    Any OTHER failure (or an OSError where `cas_dir` did NOT appear) cleans up
    staging and re-raises — never silently swallowed."""
    try:
        os.replace(cas_staging_dir, cas_dir)
    except OSError:
        if cas_dir.exists():
            shutil.rmtree(cas_staging_dir, ignore_errors=True)
            return
        shutil.rmtree(cas_staging_dir, ignore_errors=True)
        raise


# ---------------------------------------------------------------------------
# Bundle transaction shared helpers (Task 10, spec §3.2-§3.5)
# ---------------------------------------------------------------------------


def _tree_content_digest(tree: Path) -> str:
    """The `sha256:`-prefixed content checksum a receipt row records — bytecode
    stripped first, exactly as `plugin_store` does at publish time."""
    from plugin_store import content_checksum, strip_bytecode_derivatives
    strip_bytecode_derivatives(tree)
    return "sha256:" + content_checksum(tree)


def _resolve_owned_tree_paths(receipt: "SourceReceipt", *, workspace: Path,
                              ) -> "dict[str, Path]":
    """Preflight step (spec §3.2 commit sequence): map every receipt plugin
    row to the tree whose bytes will be published. Prefer the retained staging
    tree when it still matches the attested digest; otherwise recover it by
    re-fetching the row's own pinned coordinates
    (`resolve_and_fetch(row.repo, row.ref, row.subdir, tmp,
    expected_revision=row.revision)`), stripping bytecode, and re-verifying the
    digest. Any mismatch (staged OR recovered) is a `receipt_drift` refusal —
    the publish path must only ever write the attested bytes."""
    tree_paths: dict[str, Path] = {}
    for row in receipt.plugins:
        staged = Path(row.staged_path) if row.staged_path else None
        if staged is not None and staged.is_dir() and \
                _tree_content_digest(staged) == row.content_digest:
            tree_paths[row.identifier] = staged
            continue
        # Vanished/tampered staging — recover from the attested coordinates.
        # `resolve_and_fetch` populates `recovered` itself (must not pre-exist).
        recovered = workspace / row.identifier
        if recovered.exists():
            shutil.rmtree(recovered, ignore_errors=True)
        workspace.mkdir(parents=True, exist_ok=True)
        resolve_and_fetch(row.repo, row.ref, row.subdir, recovered,
                          expected_revision=row.revision)
        if _tree_content_digest(recovered) != row.content_digest:
            raise SpecialistInstallError(
                "receipt_drift",
                f"recovered plugin tree {row.scoped_name!r} no longer matches "
                f"the attested content digest {row.content_digest}")
        tree_paths[row.identifier] = recovered
    return tree_paths


def _recover_component_staging(receipt: "SourceReceipt", *, workspace: Path) -> Path:
    """Vanished-component-staging recovery (spec §3.2.1, Terra plan-r2): refetch
    the component subtree (and its github dep-plugins, at the `.dep-plugins`
    convention) from the receipt's attested coordinates. The CAS closure/root-
    digest re-verify in the caller then validates the reproduced bytes are
    exactly what the operator approved."""
    comp = workspace / "component"
    workspace.mkdir(parents=True, exist_ok=True)
    if comp.exists():
        shutil.rmtree(comp, ignore_errors=True)
    resolve_and_fetch(
        receipt.component_repo, receipt.component_ref, receipt.component_subdir,
        comp, expected_revision=receipt.component_revision)
    component = load_specialist_component(comp, comp / "manifest.json")
    for dep in component.dependencies:
        if (dep.kind == "plugin/implementation" and dep.source is not None
                and dep.source.type == "github"):
            dest = comp / ".dep-plugins" / dep.identifier
            resolve_and_fetch(dep.source.repo, dep.source.ref, "", dest,
                              expected_revision=dep.source.revision)
    return comp


def _publish_owned_plugins(
    slug: str, receipt: "SourceReceipt", tree_paths: "dict[str, Path]",
    *, store_root: Path,
) -> "list[tuple[PluginReceiptRow, object]]":
    """Publish each owned plugin tree to the content-addressed store under its
    SCOPED name (spec §3.2b). Asserts the published metadata's content checksum
    IS the attested digest (`receipt_drift` otherwise — published bytes must BE
    the attested bytes). Returns the (row, PublishResult) pairs in receipt
    order."""
    import plugin_store

    # Stage under the store root's sibling (writable wherever store_root is),
    # not the module-default /config path — keeps unit tests off /config.
    staging_root = Path(store_root).parent / ".staging"
    published: list[tuple["PluginReceiptRow", object]] = []
    for row in receipt.plugins:
        res = plugin_store.publish_from_tree(
            name=row.scoped_name, repo=row.repo, ref=row.ref,
            revision=row.revision, subdir=row.subdir,
            src_root=tree_paths[row.identifier], store_root=store_root,
            staging_root=staging_root, manifest_name=row.manifest_name)
        meta = plugin_store.read_metadata(Path(res.path)) or {}
        # row.content_digest is `sha256:<hex>`; metadata carries the bare hex.
        if "sha256:" + str(meta.get("content_checksum")) != row.content_digest:
            raise SpecialistInstallError(
                "receipt_drift",
                f"published artifact {row.scoped_name!r} content checksum "
                f"does not match the attested digest {row.content_digest}")
        published.append((row, res))
    return published


def _owned_entry_for(slug: str, row: "PluginReceiptRow", res) -> dict:
    """Build the registry entry for one owned plugin (spec §3.3): the
    manifest-level `bundled` term never enters the registry — owned entries
    always register `source.type = github` with the receipt's coordinates."""
    import plugin_registry

    return {
        "name": row.scoped_name,
        "owner": f"specialist:{slug}",
        "manifest_name": row.manifest_name,
        "targets": [f"specialist:{slug}"],
        "version": res.version,
        "source": {"type": "github", "repo": row.repo, "ref": row.ref,
                   "revision": row.revision,
                   "subdir": plugin_registry.normalize_subdir(row.subdir)},
        "artifact_id": res.artifact_id,
    }


def _owned_sidecar_doc(
    slug: str, receipt: "SourceReceipt",
    published: "list[tuple[PluginReceiptRow, object]]",
) -> dict:
    """The owned-plugins sidecar document (spec §3.4): the exact owned set +
    component source receipt for this generation — written for EVERY
    specialist, plugin-less ones included (`plugins: []`)."""
    plugins = []
    for row, res in published:
        plugins.append({
            "name": row.scoped_name, "manifest_name": row.manifest_name,
            "version": res.version, "artifact_id": res.artifact_id,
            "digest": row.content_digest,
            "source": {"type": "github", "repo": row.repo, "ref": row.ref,
                       "revision": row.revision, "subdir": row.subdir},
        })
    return {
        "schema_version": 1,
        "component_source": {
            "repo": receipt.component_repo, "ref": receipt.component_ref,
            "revision": receipt.component_revision,
            "subdir": receipt.component_subdir,
        },
        "plugins": plugins,
    }


def _tuple_files_snapshot(slug_dir: Path) -> "dict[str, str | None]":
    """Read the journalled tuple/sidecar files' bytes (or None when absent)
    for the bundle-op journal's before-state."""
    from specialist_bundle_journal import TUPLE_FILENAMES

    out: dict[str, str | None] = {}
    for name in sorted(TUPLE_FILENAMES):
        path = slug_dir / name
        out[name] = path.read_text(encoding="utf-8") if path.is_file() else None
    return out


def _removed_artifact_ids(before_entries: "list[dict]",
                          new_artifact_ids: "set[str]") -> "tuple[str, ...]":
    """Pre-swap owned artifact ids that the new owned set no longer carries —
    drives the sequencer's grant/challenge invalidation (spec §3.2d)."""
    removed = []
    for e in before_entries:
        aid = e.get("artifact_id")
        if isinstance(aid, str) and aid not in new_artifact_ids:
            removed.append(aid)
    return tuple(dict.fromkeys(removed))


def _removed_owned_names(before_entries: "list[dict]",
                         new_entries: "list[dict]") -> "tuple[str, ...]":
    """#676 (INV-TOOL-007): the owned-plugin names this swap DROPPED — present
    in the pre-swap owned set, absent from the set that replaced it. An
    uninstall passes `new_entries=[]`, so every name is dropped; an upgrade or
    a rollback drops only what the new generation no longer carries; an install
    over an empty owned set drops nothing. Names, not artifact ids, because the
    name is the plugin identity the CLI's persistent-data directory is keyed by
    and the only one an operator can act on."""
    kept = {e.get("name") for e in new_entries if isinstance(e, dict)}
    dropped = [e["name"] for e in before_entries
               if isinstance(e, dict) and isinstance(e.get("name"), str)
               and e["name"] not in kept]
    return tuple(dict.fromkeys(dropped))


def _reject_receiptless_sourced_deps(
    component: "SpecialistComponent", *, receipt: "SourceReceipt | None",
) -> None:
    """Task 10 review, defense-in-depth (F1): the journaled bundle publish/
    registry-swap path (spec §3.2/§3.4) only engages when the tool layer
    supplies a trusted-source `receipt` (`bundle_mode` in
    commit_specialist_install / the receipt-aware branch of upgrade_specialist).
    A direct in-process caller can otherwise walk the ORIGINAL legacy
    no-receipt path with a component that DECLARES a source-bearing
    `plugin/implementation` dependency — `resolve_dependency_closure` resolves
    it available straight off the component tree (no fetch needed, the tree
    is already staged), so every other gate passes, but no plugin is ever
    published to the store or registered as owned. The result is an installed/
    upgraded specialist pinned to a dependency that no `casa.triggers`/tool
    surface can ever see — an inert dangling pin, silently violating the
    F1 doctrine that validators live at the lifecycle-function boundary every
    caller (sanctioned or not) routes through.

    Fires for BOTH the fresh-staging and vanished/reused-staging CAS paths —
    callers place this right after the CAS-resident `component` is loaded and
    slug-checked, before either the persona/role load or the dependency-
    closure resolution, so it is BEFORE any InstanceDir mutation. Sourceless,
    persona-only, and corpus-only components (every dependency has
    `source is None`, which includes non-`plugin/implementation` kinds and
    bundled/no-op-source plugin deps that don't need registry publication) are
    untouched — the legacy call-site fleet keeps working unchanged."""
    if receipt is not None:
        return
    sourced = sorted(
        d.identifier for d in component.dependencies
        if d.kind == "plugin/implementation" and d.source is not None
    )
    if sourced:
        raise SpecialistInstallError(
            "receipt_required",
            f"component {component.component_id!r} bundles source-declared "
            f"plugin dependencies ({', '.join(sourced)}); a source receipt is "
            "mandatory to install or upgrade it")


def _assert_receipt_matches_inspection(
    receipt: "SourceReceipt | None", inspection: "InspectionResult",
) -> None:
    """Whole-branch D: the consent gate is keyed on `inspection.receipt_digest`
    (via the consent identity), but commit/upgrade then PUBLISH from the
    separately-supplied `receipt` object. Nothing previously asserted the two
    describe the same closure — a caller with a valid ack for inspection X could
    hand a receipt for a different closure Y and commit Y's bytes past X's
    approval. Fail closed (`receipt_mismatch`) unless the receipt's id, digest,
    and slug all equal the inspection's AND its per-plugin row set (identifier +
    attested content digest) matches `inspection.plugin_resolutions`, BEFORE the
    consent check. Legacy sourceless installs pass `receipt=None` and skip this
    (their `receipt_digest` is "" — identity unchanged)."""
    if receipt is None:
        return
    if (receipt.receipt_id != inspection.receipt_id
            or receipt.receipt_digest != inspection.receipt_digest
            or receipt.slug != inspection.slug):
        raise SpecialistInstallError(
            "receipt_mismatch",
            "supplied receipt does not match the approved inspection "
            "(id/digest/slug)")
    insp_rows = {(r.identifier, r.content_digest)
                 for r in inspection.plugin_resolutions or ()}
    receipt_rows = {(r.identifier, r.content_digest) for r in receipt.plugins}
    if insp_rows != receipt_rows:
        raise SpecialistInstallError(
            "receipt_mismatch",
            "supplied receipt's plugin row set does not match the approved "
            "inspection")


@_under_specialist_lifecycle_lock
def commit_specialist_install(
    *, inspection: "InspectionResult", receipt: "SourceReceipt | None" = None,
    config: "Mapping[str, str]",
    secret_names_provided: frozenset[str], acks: "SpecialistInstallAckStore",
    specialists_dir: Path = Path("/config/specialists"),
    agents_specialists_dir: Path = Path("/config/agents/specialists"),
    registry_path: "Path | None" = None,
    plugin_store_root: "Path | None" = None,
    ops_dir: "Path | None" = None,
) -> "SpecialistInstance | tuple[SpecialistInstance, object]":
    """The ONLY function that writes into the CAS/specialists tree (spec §6
    N1: "consent precedes any persistent CAS install/activation"). Order:
    verify consent -> persist to CAS -> compile (persona↔role compatibility)
    -> stage the InstanceDir tuple as desired -> commit the tuple to active
    -> materialize the runtime files as a best-effort follow-up. Commit is
    skipped entirely for a pending-configuration candidate — an
    uninstantiable specialist must not appear loadable.

    Round-4 fix (this review pass, finding #2 — supersedes round 2's
    "materialize BEFORE commit" ordering below). `InstanceDir.
    commit_desired_to_active()` (Plan 1) is the single authoritative,
    atomically-written record — writing `active.yaml` via
    `atomic_write_instance_tuple` is itself a single `os.replace`-backed
    write, and re-running the whole method on a later boot is a documented
    safe no-op. The operational files this function materializes afterward
    are a DERIVED CACHE of that tuple, not a second source of truth, so
    committing first and materializing second (rather than the reverse) is
    safe: if materialize fails here (disk full, permission error, a racing
    uninstall), the failure is caught, logged, and surfaced as a non-fatal
    `last_activation_error` on the returned `SpecialistInstance` — the
    already-committed tuple is NOT rolled back, because
    `specialist_materialize.current_specialist_roles_dir` (threaded through
    every boot/`casa_reload` call site per Correction #1) unconditionally
    re-materializes every ACTIVE slug's operational files from its tuple on
    every subsequent call, so this slug self-heals on the very next
    reconcile with no operator action required."""
    from personality_binding import (
        InstanceDir, check_persona_requirements,
        compute_effective_config_digest, make_instance_tuple,
        materialize_component_default_binding,
    )
    from persona_pack import load_persona_pack
    from prompt_compiler import compile_prompt_bundle
    from role_slot import _ha_model_options, materialize_role
    from role_artifact import load_role_artifact
    from specialist_lifecycle import SpecialistInstance, satisfy_config, secret_config_violations
    from specialist_component import load_specialist_component
    from specialist_install_consent import install_consent_identity
    import plugin_registry
    import plugin_store
    import specialist_bundle_journal
    from specialist_bundle_journal import BundleTxn
    import specialist_materialize

    if registry_path is None:
        registry_path = plugin_registry.REGISTRY_PATH
    if plugin_store_root is None:
        plugin_store_root = plugin_store.STORE_ROOT

    # F1 (round 2, inspection.slug traversal): `inspection.slug` is joined as
    # `specialists_dir / inspection.slug` (InstanceDir) and threaded as the
    # materialize slug below. A hand-built InspectionResult (or a compromised
    # tool layer) with a matching ack could otherwise drive a Path join with a
    # traversal slug. Validate at the lifecycle-function boundary every caller
    # routes through — before consent, before any filesystem write — and
    # re-assert after the post-publish CAS reload that the component's OWN slug
    # agrees (mirroring upgrade_specialist's slug_mismatch treatment).
    validate_specialist_slug(inspection.slug)

    # Whole-branch D: bind the supplied receipt to the approved inspection
    # BEFORE consent — fail closed on any id/digest/slug/row-set drift.
    _assert_receipt_matches_inspection(receipt, inspection)

    # Task 8 seam (Task 7 P0): thread the trusted-source receipt digest into
    # the consent identity.
    identity = install_consent_identity(
        component_id=inspection.component_id, version=inspection.version,
        root_digest=inspection.root_digest, slug=inspection.slug,
        receipt_digest=inspection.receipt_digest,
    )
    if not acks.is_acked(identity):
        raise SpecialistInstallError(
            "consent_missing",
            f"no recorded operator approval for {inspection.component_id}@"
            f"{inspection.version} (root digest {inspection.root_digest})",
        )

    # Task 10 (spec §3.2): the JOURNALED bundle transaction engages only when
    # the tool layer supplies a trusted source `receipt` (it always does; the
    # tool loads it by opaque id). Called without one — legacy/direct callers,
    # and the pre-Task-10 tests — this keeps the original non-journaled
    # behavior AND the original single-`SpecialistInstance` return, so no
    # owned-plugin publish/registry-swap/sidecar ever runs and the default
    # /config registry is never touched. `bundle_mode` returns
    # `(instance, BundleTxn)`; legacy returns just `instance`.
    bundle_mode = receipt is not None
    workspace = None
    if bundle_mode:
        workspace = specialists_dir / ".bundle-staging" / uuid.uuid4().hex
        workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    _instance_out: "SpecialistInstance | None" = None
    _txn_out: "object | None" = None
    try:
        if bundle_mode:
            # Preflight: map every attested plugin tree to the bytes to publish
            # — retained staging when it still matches the receipt digest, else
            # recovered from the attested coordinates (receipt_drift on any
            # mismatch). Vanished component staging is refetched too.
            tree_paths = _resolve_owned_tree_paths(receipt, workspace=workspace)
            effective_staged = inspection.staged_dir
            if not Path(effective_staged).is_dir():
                effective_staged = _recover_component_staging(receipt, workspace=workspace)
        else:
            tree_paths = {}
            effective_staged = inspection.staged_dir

        # Round-3 fix (finding #1 — CAS-before-verify): CAS addressing is keyed
        # by the FULL-CLOSURE root_digest, not the narrow component_checksum —
        # the operator's approval attests to the whole closure, so the CAS
        # directory identity must too. CRITICALLY, the copy from the staged dir
        # lands in a TEMPORARY staging directory first — never directly at the
        # final, content-addressed `cas_dir` — so a digest mismatch below can
        # never leave a wrong-digest-named CAS directory behind.
        cas_dir = cas_store_dir(inspection.root_digest, store_root=specialists_dir / "store")
        if not cas_dir.exists():
            staging_root = specialists_dir / "store" / ".staging"
            staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            cas_staging_dir = staging_root / uuid.uuid4().hex
            shutil.copytree(effective_staged, cas_staging_dir, dirs_exist_ok=False, symlinks=True)
            for path in cas_staging_dir.rglob("*"):
                if path.is_file():
                    path.chmod(0o400)
            try:
                # Reload + recompute the FULL closure/root digest from the
                # STAGED (not-yet-CAS) bytes and compare to the acked digest
                # BEFORE this content is ever visible under its content-
                # addressed name. A mismatch discards the staging dir + raises.
                staged_component = load_specialist_component(
                    cas_staging_dir, cas_staging_dir / "manifest.json")
                staged_deps = resolve_dependency_closure(staged_component, cas_staging_dir)
                staged_unavailable = [d for d in staged_deps if not d.available]
                if staged_unavailable:
                    detail = "; ".join(
                        f"{d.kind}:{d.identifier}: {d.detail}" for d in staged_unavailable)
                    raise SpecialistInstallError("dependency_unavailable", detail)
                staged_root_digest = compute_install_root_digest(
                    staged_component, staged_deps,
                    manifest_bytes=(cas_staging_dir / "manifest.json").read_bytes())
                if staged_root_digest != inspection.root_digest:
                    raise SpecialistInstallError(
                        "checksum_changed",
                        "staged component no longer matches the approved inspection")
            except Exception:
                shutil.rmtree(cas_staging_dir, ignore_errors=True)
                raise
            _publish_cas_staging(cas_staging_dir, cas_dir)

        # Re-load from the now-final (or pre-existing) CAS directory.
        component = load_specialist_component(cas_dir, cas_dir / "manifest.json")
        if component.slug != inspection.slug:
            raise SpecialistInstallError(
                "slug_mismatch",
                f"CAS component slug {component.slug!r} does not match the approved "
                f"inspection slug {inspection.slug!r}")
        _reject_receiptless_sourced_deps(component, receipt=receipt)
        role = materialize_role(
        source=load_role_artifact(cas_dir / "role"),
        # #355: resolve ha_option models exactly as the agent loader
        # does — options={} froze the DEFAULT into the checksum, and
        # the loader then rejected the persisted binding.
        options=_ha_model_options())
        persona = load_persona_pack(cas_dir / "persona" / "pack", cas_dir / "persona" / "manifest.json")

        fresh_deps = resolve_dependency_closure(component, cas_dir)
        unavailable = [d for d in fresh_deps if not d.available]
        if unavailable:
            detail = "; ".join(f"{d.kind}:{d.identifier}: {d.detail}" for d in unavailable)
            raise SpecialistInstallError("dependency_unavailable", detail)
        fresh_root_digest = compute_install_root_digest(
            component, fresh_deps, manifest_bytes=(cas_dir / "manifest.json").read_bytes())
        if fresh_root_digest != inspection.root_digest:
            raise SpecialistInstallError(
                "checksum_changed", "CAS-persisted component no longer matches the approved inspection")

        # #337: refuse BEFORE satisfy_config and before any instance-dir
        # mutation — a secret-named key with a plaintext value must never be
        # persisted into a config_snapshot, and secret_names_provided may only
        # name schema-declared secrets (else a required non-secret key could be
        # "satisfied" with no value ever provided).
        secret_valued, unknown_secret_names = secret_config_violations(
            schema=component.config_schema, provided_non_secret=config,
            provided_secret_names=secret_names_provided,
        )
        if secret_valued:
            raise SpecialistInstallError(
                "secret_value_in_config",
                f"config keys {secret_valued} are declared secret_names — secret values "
                "are never accepted as plaintext config; provide the secret through the "
                "secret channel and list its name in secret_names_provided")
        if unknown_secret_names:
            raise SpecialistInstallError(
                "unknown_secret_name",
                f"secret_names_provided entries {unknown_secret_names} are not declared "
                "in the component's config_schema secret_names")
        root = component_root_string(
            component_id=component.component_id, version=component.version,
            component_checksum=fresh_root_digest,
        )
        instance_dir = InstanceDir(specialists_dir / inspection.slug)
        dependency_digests = tuple(sorted(d.digest for d in fresh_deps))

        # #331: a pending-configuration retry supplies only the STILL-MISSING
        # settings — merge the SAME-ROOT pending candidate's persisted
        # config_snapshot under the caller's config (caller wins per key) so
        # already-supplied settings survive the retry instead of being
        # recomputed from the current argument alone and overwritten. Only
        # schema-known, non-secret keys carry (same filter as the upgrade
        # merge, #337). A different-root or unreadable candidate contributes
        # nothing — _refuse_if_active_present, re-run in-lock below, stays
        # the authority on whether staging may proceed at all.
        try:
            pending_before = instance_dir.desired()
        except Exception:  # noqa: BLE001 — unreadable candidate: in-lock guard refuses
            pending_before = None
        merged_config = dict(config)
        if pending_before is not None and pending_before.root == root:
            _secret_declared = set(component.config_schema.get("secret_names", []) or [])
            _known = set(component.config_schema.get("required", []) or []) | _secret_declared
            carried = {
                k: v for k, v in dict(pending_before.config_snapshot).items()
                if k in _known and k not in _secret_declared
            }
            merged_config = {**carried, **dict(config)}

        satisfied, missing = satisfy_config(
            schema=component.config_schema, provided_non_secret=merged_config,
            provided_secret_names=secret_names_provided,
        )

        # Persona/compile GATE before any durable mutation (satisfiable path
        # only; the pending path stages a placeholder with no compile).
        binding = None
        effective_config_digest = None
        if satisfied:
            try:
                check_persona_requirements(role.normalized, persona)
            except ValueError as exc:
                raise SpecialistInstallError("persona_incompatible", str(exc)) from exc
            effective_config_digest = compute_effective_config_digest(dict(merged_config))
            binding = materialize_component_default_binding(
                role=role, persona=persona, component_root=root,
                dependency_digests=dependency_digests,
                effective_config_digest=effective_config_digest,
            )
            compile_prompt_bundle(
                role=role, persona=persona, binding=binding,
                platform_frame=(Path(__file__).parent / "defaults" / "personality"
                                 / "platform-frame.md").read_text(encoding="utf-8"),
                safety_kernel=(Path(__file__).parent / "defaults" / "personality"
                               / "safety-kernel.md").read_text(encoding="utf-8"),
            )

        # Shared stage/commit: pending-configuration stages a placeholder
        # desired (no activation); the satisfiable path commits the tuple to
        # active and materializes op-files best-effort. `sidecar_doc` (bundle
        # mode only) stages+rotates the owned-plugins sidecar in the SAME
        # locked step as the tuple rotation; None (legacy) touches no sidecar.
        def _stage_and_commit(sidecar_doc: "dict | None") -> "SpecialistInstance":
            if not satisfied:
                # #372 (D0): the placeholder binding is built over the SAME
                # partial merged_config that is persisted as the snapshot, so
                # the tuple's digest equation holds for pending state too.
                placeholder_binding = materialize_component_default_binding(
                    role=role, persona=persona, component_root=root,
                    dependency_digests=dependency_digests,
                    effective_config_digest=compute_effective_config_digest(
                        dict(merged_config)),
                )
                with specialist_materialize.MATERIALIZE_LOCK:
                    _refuse_if_active_present(instance_dir, slug=inspection.slug, root=root)
                    if receipt is not None:
                        # Marker BEFORE the stage (Sol r7-2): desired.yaml
                        # must never exist without its receipt marker.
                        _record_pending_receipt(
                            specialists_dir / inspection.slug, receipt.receipt_id)
                    instance_dir.stage_desired(make_instance_tuple(
                        root=root, binding=placeholder_binding,
                        config_snapshot=dict(merged_config),
                    ))
                    if sidecar_doc is not None:
                        instance_dir.stage_desired_owned_plugins(sidecar_doc)
                return SpecialistInstance(
                    slug=inspection.slug, stable_agent_id=f"specialist:{inspection.slug}",
                    state="pending-configuration", active=None, desired=instance_dir.desired(),
                    last_activation_error=f"missing required config/secret: {missing}",
                )
            last_activation_error: str | None = None
            with specialist_materialize.MATERIALIZE_LOCK:
                _refuse_if_active_present(instance_dir, slug=inspection.slug, root=root)
                instance_dir.stage_desired(make_instance_tuple(
                    root=root, binding=binding, config_snapshot=dict(merged_config),
                ))
                if sidecar_doc is not None:
                    instance_dir.stage_desired_owned_plugins(sidecar_doc)
                committed = instance_dir.commit_desired_to_active()
                if sidecar_doc is not None:
                    instance_dir.commit_owned_plugins_desired_to_active()
                _clear_pending_receipt(specialists_dir / inspection.slug)
                try:
                    specialist_materialize.materialize_specialist_operational_files(
                        agents_specialists_dir=agents_specialists_dir, slug=inspection.slug,
                        role=role, persona=persona,
                        binding_digest=committed.binding.binding_digest,
                        component_root=committed.root,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "specialist install %r: operational-file materialize failed "
                        "post-commit (%s); will self-heal on next reconcile",
                        inspection.slug, exc, exc_info=True)
                    last_activation_error = f"operational files pending reconcile: {exc}"
            return SpecialistInstance(
                slug=inspection.slug, stable_agent_id=f"specialist:{inspection.slug}",
                state="active", active=committed, desired=None,
                last_activation_error=last_activation_error,
            )

        if not bundle_mode:
            _instance_out = _stage_and_commit(None)
        else:
            # --- Journaled bundle mutation (spec §3.2 steps 1-5) -----------
            # begin captures the FULL before-state (owned entries, tuple/
            # sidecar bytes, consent-ack records) BEFORE any durable mutation;
            # a sync-phase failure rolls it back synchronously in the except
            # below. The tool layer (holding _PLUGIN_TOOLS_LOCK) owns the
            # post-mutation sequencer + journal-complete via the returned txn.
            slug_dir = specialists_dir / inspection.slug
            _reg = plugin_registry.load_registry(registry_path)
            before_owned = plugin_registry.owned_entries_for(inspection.slug, _reg)
            before_tuple_files = _tuple_files_snapshot(slug_dir)
            ack_records = acks.snapshot_slug(inspection.slug)
            _begin_kwargs = {} if ops_dir is None else {"ops_dir": ops_dir}
            journal = specialist_bundle_journal.begin(
                "install", inspection.slug, before_entries=before_owned,
                before_tuple_files=before_tuple_files, ack_records=ack_records,
                receipt_digest=receipt.receipt_digest, consent_identity=identity,
                target_root=component_root_string(
                    component_id=inspection.component_id, version=inspection.version,
                    component_checksum=inspection.root_digest),
                **_begin_kwargs)
            rollback_txn = BundleTxn(
                journal_path=journal, slug=inspection.slug,
                before_entries=before_owned, before_tuple_files=before_tuple_files,
                ack_records=ack_records, op="install",
                target_root=component_root_string(
                    component_id=inspection.component_id, version=inspection.version,
                    component_checksum=inspection.root_digest),
                registry_path=registry_path,
                specialists_dir=specialists_dir, acks_path=acks.path,
                agents_specialists_dir=agents_specialists_dir)
            try:
                published = _publish_owned_plugins(
                    inspection.slug, receipt, tree_paths, store_root=plugin_store_root)
                # Whole-branch O: mark the owned-plugin publication boundary in
                # the journal. NB the COMPONENT CAS publish (_publish_cas_staging
                # above) still runs BEFORE begin() — plan step 1b's journal-first
                # ideal — because that CAS directory is CONTENT-ADDRESSED by
                # root_digest: a crash after it but before begin leaves an inert,
                # deduplicated, GC-later residue that no rollback needs to undo
                # (spec §3.2: "CAS residue is harmless, content-addressed").
                # The registry-VISIBLE mutations (owned-plugin store publish,
                # owned_swap, tuple/sidecar) are all inside this journaled block.
                specialist_bundle_journal.mark_step(journal, "owned_plugins_published")
                new_entries = [_owned_entry_for(inspection.slug, row, res)
                               for row, res in published]
                before_entries, _ = plugin_registry.apply_owned_swap(
                    slug=inspection.slug, new_entries=new_entries, registry_path=registry_path)
                new_artifact_ids = {res.artifact_id for _, res in published}
                removed = _removed_artifact_ids(before_entries, new_artifact_ids)
                sidecar_doc = _owned_sidecar_doc(inspection.slug, receipt, published)
                _instance_out = _stage_and_commit(sidecar_doc)
                specialist_bundle_journal.mark_step(journal, "committed")
                _txn_out = BundleTxn(
                    journal_path=journal, slug=inspection.slug,
                    before_entries=before_entries, before_tuple_files=before_tuple_files,
                    ack_records=ack_records, removed_artifact_ids=removed,
                    new_artifact_ids=tuple(sorted(new_artifact_ids)),
                    op="install", owned_swap_committed=True,
                    removed_owned_names=_removed_owned_names(
                        before_entries, new_entries),
                    target_root=component_root_string(
                        component_id=inspection.component_id, version=inspection.version,
                        component_checksum=inspection.root_digest),
                    registry_path=registry_path, specialists_dir=specialists_dir,
                    acks_path=acks.path,
                    agents_specialists_dir=agents_specialists_dir)
            except BaseException:
                # Sync-phase failure: nothing was reloaded, so restore disk
                # state from the before-state (spec §3.2 failure handling — no
                # sequencer needed). P1-1: complete the journal ONLY after a
                # SUCCESSFUL rollback — if rollback_disk() raises (e.g. an
                # unreadable registry, which fails closed rather than persist a
                # partial doc), leave the in-progress journal on disk so boot
                # reconciliation re-runs the rollback or quarantines the slug.
                # Completing it here would strand a half-rolled-back mutation
                # with no recovery.
                rollback_txn.rollback_disk()
                specialist_bundle_journal.complete(journal)
                raise
    finally:
        if workspace is not None:
            shutil.rmtree(workspace, ignore_errors=True)

    # #306: the inspection staging tree is consumed once the install is
    # terminally successful. On the BUNDLE path the tool layer owns that
    # boundary — its reload-and-verify sequencer can still fail and
    # compensate back to the pending state, and the retry needs the staged
    # bytes (Sol r2-1) — so it reclaims beside the receipt prune. Only the
    # legacy no-receipt path, which has no sequencer, consumes it here. A
    # pending-configuration outcome always retains the tree (the follow-up
    # commit reuses the attested bytes; abandonment falls to the boot sweep).
    if (not bundle_mode and _instance_out is not None
            and _instance_out.state == "active"):
        reclaim_staging_tree(inspection.staged_dir)

    if bundle_mode:
        return _instance_out, _txn_out
    return _instance_out


def _rederive_stale_binding(role, active_tuple, cas_dir: Path):
    """#597: the binding for *active_tuple* re-derived for *role*, after
    proving that the resolved model is the ONLY input that can have moved.

    The stored binding carries a checksum and no normalized role, so the old
    inputs cannot be diffed; the argument is structural and every leg of it
    is checked here:

    L1 — same closure bytes. The tuple root is content-addressed by the
    install root digest (component checksum + manifest bytes + dependency
    digests) and the component store is keyed by it; recompute that digest
    from the store's bytes and require it, and the component id/version, to
    match the root. A store whose bytes drifted is refused.

    L2 — the role being loaded IS that component's role under the LIVE
    option resolution. Re-materialize the store's role artifact with
    ``_ha_model_options()`` and require its checksum, id and slot to equal
    *role*'s. Load-bearing for the overlay race ``reload.py``'s
    ``_load_agent_with_overlay_retry`` documents: a concurrent upgrade can
    commit a NEW tuple between the overlay build and the load, so the role
    the loader compiled is the OLD version's — without L2 the re-derivation
    would bind the new tuple to the old role and WRITE it. With L2 it
    refuses; the loader rebuilds the overlay once and retries.

    Given L1 and L2 the role's declared bytes and doctrine are fixed, and
    ``compute_role_checksum`` has exactly one other input: the resolved
    model. The re-derivation itself (``rederive_binding_for_role``) carries
    every stored field forward and requires the stored agent id to be the
    role's (L3); the persona identity it carries is compared against the
    loaded pack by the compile that follows, before anything is written.

    Raises ``ValueError`` on every refusal — the specialist branch folds it
    into ``LoadError`` and the specialist is isolated, exactly as a
    mismatch is today."""
    from personality_binding import rederive_binding_for_role
    from role_artifact import load_role_artifact
    from role_slot import _ha_model_options, materialize_role

    component_id, version, suffix = parse_component_root(active_tuple.root)
    component = load_specialist_component(cas_dir, cas_dir / "manifest.json")
    deps = resolve_dependency_closure(component, cas_dir)
    fresh_root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=(cas_dir / "manifest.json").read_bytes())
    if (fresh_root_digest != suffix or component.component_id != component_id
            or component.version != version):
        raise ValueError(
            f"binding for {role.role_id} not re-derived: the installed component in the "
            f"store ({component.component_id}@{component.version}#{fresh_root_digest}) is "
            f"not the active tuple's root ({active_tuple.root})")
    live_role = materialize_role(
        source=load_role_artifact(cas_dir / "role"), options=_ha_model_options())
    if (live_role.checksum != role.checksum or live_role.role_id != role.role_id
            or live_role.slot != role.slot):
        raise ValueError(
            f"binding for {role.role_id} not re-derived: the role being loaded "
            f"({role.checksum}) is not the installed component's role artifact under "
            f"the current option resolution ({live_role.checksum}) — a stale roles "
            f"overlay; the load is retried against a rebuilt overlay")
    return rederive_binding_for_role(binding=active_tuple.binding, role=role)


def activate_binding_for_config(
    cfg, *, specialists_root: Path = Path("/config/specialists"), commit: bool = True,
) -> None:
    """Mutates *cfg* in place with the compiled binding for its installed
    component, if one has an ACTIVE tuple (spec §4.1: a pending-configuration
    or legacy-bundled specialist has none, and this is a no-op — cfg keeps
    compiled_prompt_bundle=None, and tools.py's system_prompt seam falls back
    to the legacy cfg.system_prompt path). This is the seam Plan 1 Task 9's
    speaker_provenance_for_role docstring names — 'once Plan 2's N1 populates
    cfg.speaker_provenance for specialists — no further code change needed
    there.'

    Testability note (Task N1b Step 19): rather than inlining this the way
    agent_loader.py's resident block does (which hard-codes
    Path("/config/bindings")), this is a standalone function parameterized
    by ``specialists_root`` — agent_loader.py's specialist branch calls it
    with the real, hard-coded production root; unit tests call it directly
    with a tmp_path root, no monkeypatching needed.

    #597 (INV-PERS-016): a binding whose ``role_checksum`` no longer matches
    the role being loaded is RE-DERIVED rather than compiled as-is — the
    role checksum covers the resolved model by design, so an HA option flip
    or an alias move moves every installed specialist's checksum with nothing
    else changing, and compiling the stored binding dropped every one of
    them until re-installed. ``_rederive_stale_binding`` proves the resolved
    model is the only moved input and carries the stored identity forward;
    the compile below is the identity gate (persona triple, renderer
    version, digest) and runs BEFORE any write. Then, only when ``commit``
    is true AND the specialist is enabled, the re-derived binding replaces
    ``active.yaml`` in place through the same-generation primitive — never
    ``desired.yaml``, never the retained prior. ``commit=False`` is the
    ``validate_config_repo`` replay (#338): re-derive in memory, write
    nothing, record nothing. The common path — checksums agree — is
    byte-for-byte today's and takes no lock."""
    from personality_binding import InstanceDir, make_instance_tuple
    from persona_pack import load_persona_pack
    from prompt_compiler import compile_prompt_bundle
    from personality_types import SpeakerProvenance

    # F1 (confirm): cfg.role_slot.slot is already loader-validated
    # (role_artifact enforces role.v1.json's slot pattern), but this function
    # joins it as `specialists_root / slot` — re-assert the canonical shape so
    # the containment guarantee holds regardless of how cfg was constructed.
    validate_specialist_slug(cfg.role_slot.slot)
    instance_dir = InstanceDir(specialists_root / cfg.role_slot.slot)
    active_tuple = instance_dir.active()
    if active_tuple is None:
        return
    # `root` is ALWAYS the component root regardless of binding.mode (Round-2
    # fix, finding #4) — apply_persona_override never rewrites it for a
    # specialist target, so this parse is unconditionally safe.
    _, _, checksum = parse_component_root(active_tuple.root)
    cas_dir = cas_store_dir(checksum, store_root=specialists_root / "store")
    if active_tuple.binding.mode == "override":
        # The persona to COMPILE with is the override's, not the component's
        # bundled default — role/doctrine still come from cas_dir above.
        # #323 (Sol r3-3): resolved through the same env-aware seam the
        # persona tools and resident loader use, or an override applied
        # under a custom config root fails activation on the next reload.
        from persona_install import installed_personas_root
        personas_root = installed_personas_root()
        bound_persona = load_persona_pack(
            personas_root / active_tuple.binding.persona_id / active_tuple.binding.persona_version / "pack",
            personas_root / active_tuple.binding.persona_id / active_tuple.binding.persona_version / "manifest.json",
        )
    else:
        bound_persona = load_persona_pack(
            cas_dir / "persona" / "pack", cas_dir / "persona" / "manifest.json")
    binding = active_tuple.binding
    if binding.role_checksum != cfg.role_slot.checksum:
        binding = _rederive_stale_binding(cfg.role_slot, active_tuple, cas_dir)
    defaults_root = Path(__file__).parent / "defaults"
    bundle = compile_prompt_bundle(
        role=cfg.role_slot, persona=bound_persona, binding=binding,
        platform_frame=(defaults_root / "personality" / "platform-frame.md").read_text(
            encoding="utf-8"),
        safety_kernel=(defaults_root / "personality" / "safety-kernel.md").read_text(
            encoding="utf-8"),
    )
    # `enabled` is read fail-closed: a cfg that does not carry it (a bare
    # test double) writes nothing. The loader always sets it
    # (`_build_runtime_fields`), so production never hits the default.
    if binding is not active_tuple.binding and commit and getattr(cfg, "enabled", False):
        written = instance_dir.replace_active_same_generation(
            active_tuple,
            make_instance_tuple(root=active_tuple.root, binding=binding,
                                config_snapshot=active_tuple.config_snapshot))
        logger.info("specialist_binding_rederived %s", json.dumps({
            "specialist": cfg.role_slot.role_id,
            "root": written.root,
            "role_checksum": {"from": active_tuple.binding.role_checksum,
                              "to": written.binding.role_checksum},
            "binding_digest": {"from": active_tuple.binding.binding_digest,
                               "to": written.binding.binding_digest},
            "resolved_model": cfg.role_slot.resolved_model.effective,
        }, sort_keys=True))
    cfg.persona_pack = bound_persona
    cfg.binding = binding
    cfg.compiled_prompt_bundle = bundle
    cfg.binding_digest = binding.binding_digest
    cfg.speaker_provenance = SpeakerProvenance(
        speaker_kind="specialist", role_id=cfg.role_slot.role_id,
        persona_id=bound_persona.persona_id, persona_version=bound_persona.version,
        display_name=bound_persona.identity["display_name"],
        binding_digest=binding.binding_digest,
    )


@_under_specialist_lifecycle_lock
def upgrade_specialist(
    *, slug: str, inspection: "InspectionResult", receipt: "SourceReceipt | None" = None,
    config: "Mapping[str, str]",
    secret_names_provided: frozenset[str], acks: "SpecialistInstallAckStore",
    specialists_dir: Path = Path("/config/specialists"),
    agents_specialists_dir: Path = Path("/config/agents/specialists"),
    registry_path: "Path | None" = None,
    plugin_store_root: "Path | None" = None,
    ops_dir: "Path | None" = None,
) -> "SpecialistInstance | tuple[SpecialistInstance, object]":
    """Bundle-aware upgrade (Task 10). Without a `receipt` (legacy/direct
    callers) this is the original transactional upgrade returning a single
    `SpecialistInstance`. WITH a receipt (the tool layer) it wraps the core
    upgrade in the journaled bundle transaction: journal the before-state,
    run the core tuple commit, then — ONLY when the upgrade actually activates
    — publish the new owned plugin set, atomically swap it into the registry,
    and rotate the owned-plugins sidecar; a pending/error outcome leaves the
    old owned generation fully untouched (spec §3.4). Returns
    `(instance, BundleTxn)`.

    Delta (code): the owned-plugins sidecar is PUBLISHED (desired->active) in a
    second MATERIALIZE_LOCK step immediately after the core tuple commit (the
    lock is non-reentrant and the core takes it internally); the prior sidecar
    was already rotated BY the tuple commit, paired with active.prior.yaml
    (#810, INV-SPEC-011). The journal's before-state covers the crash window,
    and the whole transaction — sampling, journal, publish, swap, both commits
    — runs under SPECIALIST_LIFECYCLE_LOCK, so nothing can observe the
    tuple-vs-sidecar window between the two materialize scopes."""
    import dataclasses

    import plugin_registry
    import specialist_bundle_journal
    from specialist_bundle_journal import BundleTxn
    from specialist_lifecycle import SpecialistInstance  # noqa: F401 (typing)
    from specialist_install_consent import install_consent_identity
    import plugin_store
    import specialist_materialize
    from personality_binding import InstanceDir

    if receipt is None:
        instance = _upgrade_core(
            slug=slug, inspection=inspection, config=config,
            secret_names_provided=secret_names_provided, acks=acks,
            specialists_dir=specialists_dir, agents_specialists_dir=agents_specialists_dir,
            receipt=None)
        if instance.state == "active":
            reclaim_staging_tree(inspection.staged_dir)   # #306
        return instance

    if registry_path is None:
        registry_path = plugin_registry.REGISTRY_PATH
    if plugin_store_root is None:
        plugin_store_root = plugin_store.STORE_ROOT

    validate_specialist_slug(slug)
    workspace = specialists_dir / ".bundle-staging" / uuid.uuid4().hex
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    instance = None
    txn = None
    try:
        tree_paths = _resolve_owned_tree_paths(receipt, workspace=workspace)
        eff_inspection = inspection
        if not Path(inspection.staged_dir).is_dir():
            recovered = _recover_component_staging(receipt, workspace=workspace)
            eff_inspection = dataclasses.replace(inspection, staged_dir=recovered)

        slug_dir = specialists_dir / slug
        _reg = plugin_registry.load_registry(registry_path)
        before_owned = plugin_registry.owned_entries_for(slug, _reg)
        before_tuple_files = _tuple_files_snapshot(slug_dir)
        ack_records = acks.snapshot_slug(slug)
        _begin_kwargs = {} if ops_dir is None else {"ops_dir": ops_dir}
        journal = specialist_bundle_journal.begin(
            "upgrade", slug, before_entries=before_owned,
            before_tuple_files=before_tuple_files, ack_records=ack_records,
            receipt_digest=receipt.receipt_digest,
            consent_identity=install_consent_identity(
                component_id=inspection.component_id, version=inspection.version,
                root_digest=inspection.root_digest, slug=inspection.slug,
                receipt_digest=inspection.receipt_digest),
            target_root=component_root_string(
                component_id=inspection.component_id, version=inspection.version,
                component_checksum=inspection.root_digest),
            **_begin_kwargs)
        rollback_txn = BundleTxn(
            journal_path=journal, slug=slug, before_entries=before_owned,
            before_tuple_files=before_tuple_files, ack_records=ack_records,
            op="upgrade",
            target_root=component_root_string(
                component_id=inspection.component_id, version=inspection.version,
                component_checksum=inspection.root_digest),
            registry_path=registry_path, specialists_dir=specialists_dir,
            acks_path=acks.path,
            agents_specialists_dir=agents_specialists_dir)
        try:
            instance = _upgrade_core(
                slug=slug, inspection=eff_inspection, config=config,
                secret_names_provided=secret_names_provided, acks=acks,
                specialists_dir=specialists_dir, agents_specialists_dir=agents_specialists_dir,
                receipt=receipt)
            if instance.state == "active":
                published = _publish_owned_plugins(
                    slug, receipt, tree_paths, store_root=plugin_store_root)
                # Whole-branch O: publication boundary (see
                # commit_specialist_install's note — the component CAS publish is
                # content-addressed/inert; the registry-visible mutations are all
                # journaled from here).
                specialist_bundle_journal.mark_step(journal, "owned_plugins_published")
                new_entries = [_owned_entry_for(slug, row, res) for row, res in published]
                before_entries, _ = plugin_registry.apply_owned_swap(
                    slug=slug, new_entries=new_entries, registry_path=registry_path)
                new_artifact_ids = {res.artifact_id for _, res in published}
                removed = _removed_artifact_ids(before_entries, new_artifact_ids)
                sidecar_doc = _owned_sidecar_doc(slug, receipt, published)
                with specialist_materialize.MATERIALIZE_LOCK:
                    InstanceDir(slug_dir).stage_desired_owned_plugins(sidecar_doc)
                    InstanceDir(slug_dir).commit_owned_plugins_desired_to_active()
                swapped = True
            else:
                # pending/error: old owned generation untouched (spec §3.4).
                before_entries = before_owned
                removed = ()
                new_artifact_ids = set()
                # #676: nothing was swapped, so before_entries is the UNCHANGED
                # owned set and holds no removal candidate.
                swapped = False
            specialist_bundle_journal.mark_step(journal, "committed")
            txn = BundleTxn(
                journal_path=journal, slug=slug, before_entries=before_entries,
                before_tuple_files=before_tuple_files, ack_records=ack_records,
                removed_artifact_ids=removed,
                new_artifact_ids=tuple(sorted(new_artifact_ids)),
                op="upgrade", owned_swap_committed=swapped,
                removed_owned_names=_removed_owned_names(
                    before_entries, new_entries) if swapped else (),
                target_root=component_root_string(
                    component_id=inspection.component_id, version=inspection.version,
                    component_checksum=inspection.root_digest),
                registry_path=registry_path, specialists_dir=specialists_dir,
                acks_path=acks.path,
                agents_specialists_dir=agents_specialists_dir)
        except BaseException:
            # P1-1: complete the journal ONLY after a SUCCESSFUL rollback. A
            # rollback that raises leaves the in-progress journal on disk so boot
            # reconciliation re-runs it (or quarantines the slug) — completing here
            # would strand a half-rolled-back mutation with no recovery.
            rollback_txn.rollback_disk()
            specialist_bundle_journal.complete(journal)
            raise
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    # #306/Sol r2-1: NO staging reclaim here — this is the bundle path, and
    # the tool layer's sequencer decides terminal success (see
    # commit_specialist_install's identical note).
    return instance, txn


def _upgrade_core(
    *, slug: str, inspection: "InspectionResult", config: "Mapping[str, str]",
    secret_names_provided: frozenset[str], acks: "SpecialistInstallAckStore",
    specialists_dir: Path = Path("/config/specialists"),
    agents_specialists_dir: Path = Path("/config/agents/specialists"),
    receipt: "SourceReceipt | None" = None,
) -> "SpecialistInstance":
    """Spec §2.4/§4.1's transactional reinstall/upgrade: stage the new
    version as desired, validate+compile it fully BEFORE touching active,
    commit atomically on success. On ANY failure BEFORE that commit the
    active tuple is left completely untouched — this function never calls
    anything that mutates active.yaml except InstanceDir.commit_desired_to_active
    itself, and that is reached only after every validation gate below has
    already passed.

    Commit-first ordering (matches commit_specialist_install): once every
    validation gate passes and commit_desired_to_active() runs, that IS the
    success boundary the docstring's "never touches active until success"
    refers to — materializing the operational files afterward is a
    best-effort follow-up, not a second gate (see commit_specialist_install's
    docstring for the full rationale: the tuple is the single authoritative
    record; the operational files are a self-healing derived cache
    current_specialist_roles_dir rebuilds on every boot/reload).

    `receipt` (threaded through by both of upgrade_specialist's call sites —
    None on the legacy no-receipt path, the real receipt on the bundle path)
    is used ONLY for `_reject_receiptless_sourced_deps`'s defense-in-depth
    check below; this function never publishes/registers plugins itself
    (upgrade_specialist's bundle branch does that after this returns)."""
    from personality_binding import (
        InstanceDir, check_persona_requirements, compute_effective_config_digest,
        make_instance_tuple, materialize_component_default_binding,
        materialize_override_binding,
    )
    from persona_pack import load_persona_pack
    from prompt_compiler import compile_prompt_bundle
    from role_slot import _ha_model_options, materialize_role
    from role_artifact import load_role_artifact
    from specialist_lifecycle import SpecialistInstance, satisfy_config, secret_config_violations
    from specialist_install_consent import install_consent_identity
    import specialist_materialize

    # F1: `slug` is a caller-supplied argument independent of `inspection`;
    # it indexes `specialists_dir / slug` (and downstream Path joins) below.
    # Validate at the top before any filesystem operation.
    validate_specialist_slug(slug)

    # Whole-branch D: bind the supplied receipt to the approved inspection
    # BEFORE consent (see commit_specialist_install's identical note).
    _assert_receipt_matches_inspection(receipt, inspection)

    # Task 8 seam (Task 7 P0): see commit_specialist_install's identical note.
    identity = install_consent_identity(
        component_id=inspection.component_id, version=inspection.version,
        root_digest=inspection.root_digest, slug=inspection.slug,
        receipt_digest=inspection.receipt_digest)
    if not acks.is_acked(identity):
        raise SpecialistInstallError("consent_missing", "no recorded operator approval for the upgrade")

    instance_dir = InstanceDir(specialists_dir / slug)
    try:
        active_before = instance_dir.active()
    except ValueError as exc:
        # #372: a pre-guard (or otherwise unverifiable) active is a typed
        # refusal, not an escaped ValueError — recovery is uninstall+reinstall.
        raise SpecialistInstallError("active_unreadable", str(exc)) from exc
    if active_before is None:
        raise SpecialistInstallError("no_active_tuple", f"{slug!r} has no active install to upgrade")

    # Same CAS-before-verify TEMP-staging + reload + recompute + compare +
    # os.replace pattern as commit_specialist_install (see that function's
    # comments for the full rationale) — a digest mismatch here must never
    # leave a wrong-digest-named CAS directory behind.
    cas_dir = cas_store_dir(inspection.root_digest, store_root=specialists_dir / "store")
    if not cas_dir.exists():
        staging_root = specialists_dir / "store" / ".staging"
        staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        cas_staging_dir = staging_root / uuid.uuid4().hex
        shutil.copytree(inspection.staged_dir, cas_staging_dir, dirs_exist_ok=False, symlinks=True)
        for path in cas_staging_dir.rglob("*"):
            if path.is_file():
                path.chmod(0o400)
        try:
            staged_component = load_specialist_component(
                cas_staging_dir, cas_staging_dir / "manifest.json")
            staged_deps = resolve_dependency_closure(staged_component, cas_staging_dir)
            staged_unavailable = [d for d in staged_deps if not d.available]
            if staged_unavailable:
                detail = "; ".join(
                    f"{d.kind}:{d.identifier}: {d.detail}" for d in staged_unavailable)
                raise SpecialistInstallError("dependency_unavailable", detail)
            staged_root_digest = compute_install_root_digest(
                staged_component, staged_deps,
                manifest_bytes=(cas_staging_dir / "manifest.json").read_bytes())
            if staged_root_digest != inspection.root_digest:
                raise SpecialistInstallError(
                    "checksum_changed",
                    "staged component no longer matches the approved inspection")
        except Exception:
            shutil.rmtree(cas_staging_dir, ignore_errors=True)
            raise
        _publish_cas_staging(cas_staging_dir, cas_dir)
    component = load_specialist_component(cas_dir, cas_dir / "manifest.json")
    # The MCP tool boundary passes `slug` and `inspection` as INDEPENDENT
    # arguments — specialist_upgrade builds `inspection` from the freshly-
    # loaded staged component but takes `args["slug"]` separately, so
    # nothing previously stopped a caller (compromised or mistaken tool-call
    # sequence, or a test/direct caller that hand-builds InspectionResult)
    # from upgrading slug X using component Y's bytes. Assert agreement at
    # the lifecycle-function level — the layer every caller, sanctioned or
    # not, must pass through.
    if component.slug != slug:
        raise SpecialistInstallError(
            "slug_mismatch",
            f"component slug {component.slug!r} does not match the requested upgrade slug {slug!r}")
    _reject_receiptless_sourced_deps(component, receipt=receipt)
    fresh_deps = resolve_dependency_closure(component, cas_dir)
    fresh_unavailable = [d for d in fresh_deps if not d.available]
    if fresh_unavailable:
        detail = "; ".join(f"{d.kind}:{d.identifier}: {d.detail}" for d in fresh_unavailable)
        raise SpecialistInstallError("dependency_unavailable", detail)
    fresh_root_digest = compute_install_root_digest(
        component, fresh_deps, manifest_bytes=(cas_dir / "manifest.json").read_bytes())
    if fresh_root_digest != inspection.root_digest:
        raise SpecialistInstallError(
            "checksum_changed", "CAS-persisted component no longer matches the approved inspection")
    role = materialize_role(
        source=load_role_artifact(cas_dir / "role"),
        # #355: resolve ha_option models exactly as the agent loader
        # does — options={} froze the DEFAULT into the checksum, and
        # the loader then rejected the persisted binding.
        options=_ha_model_options())
    # An existing OVERRIDE binding must survive an upgrade — the component's
    # own bundled default persona is only used when the active binding was
    # already component-default (or this is a first activation). Reverting
    # silently on every upgrade would discard an operator's explicit
    # persona choice.
    if active_before.binding.mode == "override":
        from persona_install import installed_personas_root
        personas_root = installed_personas_root()   # #323 (Sol r3-3)
        persona = load_persona_pack(
            personas_root / active_before.binding.persona_id / active_before.binding.persona_version / "pack",
            personas_root / active_before.binding.persona_id / active_before.binding.persona_version / "manifest.json",
        )
    else:
        persona = load_persona_pack(cas_dir / "persona" / "pack", cas_dir / "persona" / "manifest.json")

    # #337: same channel discipline as commit_specialist_install — refuse a
    # secret-named key arriving with a plaintext value, and an undeclared
    # secret_names_provided entry, BEFORE any staging.
    secret_valued, unknown_secret_names = secret_config_violations(
        schema=component.config_schema, provided_non_secret=config,
        provided_secret_names=secret_names_provided,
    )
    if secret_valued:
        raise SpecialistInstallError(
            "secret_value_in_config",
            f"config keys {secret_valued} are declared secret_names — secret values "
            "are never accepted as plaintext config; provide the secret through the "
            "secret channel and list its name in secret_names_provided")
    if unknown_secret_names:
        raise SpecialistInstallError(
            "unknown_secret_name",
            f"secret_names_provided entries {unknown_secret_names} are not declared "
            "in the component's config_schema secret_names")

    # Re-validate the OPERATOR'S EXISTING non-secret config against the NEW
    # schema, fail-closed, into the DESIRED snapshot only (spec §4.1) — the
    # active config_snapshot is never read or touched here. Keys the NEW
    # schema no longer declares are DROPPED, not carried forward forever —
    # "re-validate ... fail-closed" means the schema is authoritative on
    # every upgrade, not just at fresh install. #337: secret-named keys are
    # ALSO stripped from the carried snapshot — a pre-#337 install may have
    # persisted a secret's plaintext there, and an upgrade must not carry it
    # forward (the secret channel, secret_names_provided, satisfies instead).
    secret_names = set(component.config_schema.get("secret_names", []) or [])
    # Terra r2 (#337): the CARRIED snapshot is stripped against the PRIOR
    # component's declaration too — a key the incoming schema reclassifies
    # from secret to plain-required must not smuggle its legacy plaintext
    # through the carry-over (the operator supplies a fresh plain value, or
    # the upgrade lands pending-configuration). Caller-supplied `config` is
    # untouched here; it was already guarded against the NEW secret_names.
    prior_secret_names = _declared_secret_names_for_root(
        active_before.root, specialists_dir=specialists_dir)
    if prior_secret_names is None:
        # Sol r3: unloadable prior schema — fail CLOSED. Nothing carries; the
        # operator re-supplies config (or the upgrade lands
        # pending-configuration) rather than legacy plaintext riding through.
        prior_secret_names = set(active_before.config_snapshot)
    carried_config = {
        k: v for k, v in dict(active_before.config_snapshot).items()
        if k not in prior_secret_names
    }
    known_keys = set(component.config_schema.get("required", [])) | secret_names
    # #331: a pending-configuration UPGRADE retry must also keep the settings
    # its earlier attempt already supplied — carry the SAME-TARGET-ROOT
    # desired candidate's snapshot too, between the active carry and the
    # caller's config (precedence: active < prior desired < caller). A
    # different-root or unreadable candidate contributes nothing.
    root = component_root_string(component_id=component.component_id, version=component.version,
                                  component_checksum=fresh_root_digest)
    try:
        _desired_before = instance_dir.desired()
    except Exception:  # noqa: BLE001 — unreadable candidate contributes nothing
        _desired_before = None
    desired_carried = {}
    if _desired_before is not None and _desired_before.root == root:
        desired_carried = {
            k: v for k, v in dict(_desired_before.config_snapshot).items()
            if k in known_keys and k not in secret_names
        }
    stale_config = {**carried_config, **desired_carried, **dict(config)}
    dropped_keys = sorted(k for k in stale_config if k not in known_keys)
    stripped_secret_keys = sorted(
        {k for k in active_before.config_snapshot if k in prior_secret_names}
        | {k for k in stale_config if k in secret_names})
    if stripped_secret_keys:
        logger.info(
            "specialist upgrade %r: stripped legacy plaintext secret key(s) %s from "
            "the carried config_snapshot (#337)", slug, stripped_secret_keys)
    merged_config = {
        k: v for k, v in stale_config.items()
        if k in known_keys and k not in secret_names
    }
    satisfied, missing = satisfy_config(
        schema=component.config_schema, provided_non_secret=merged_config,
        provided_secret_names=secret_names_provided,
    )
    dependency_digests = tuple(sorted(d.digest for d in fresh_deps))

    def _build_upgrade_binding(*, effective_config_digest: str):
        # Reuse the SAME override-vs-default branch the "satisfied" path
        # below needs, so a pending-configuration placeholder ALSO
        # preserves an active override rather than silently dropping it if
        # the operator has to supply missing config later.
        if active_before.binding.mode == "override":
            return materialize_override_binding(
                role=role, persona=persona, override_source=active_before.binding.override_source,
                dependency_digests=dependency_digests, effective_config_digest=effective_config_digest)
        return materialize_component_default_binding(
            role=role, persona=persona, component_root=root,
            dependency_digests=dependency_digests, effective_config_digest=effective_config_digest)

    if not satisfied:
        # #372 (D0): the placeholder carries the digest of the NEW merged
        # snapshot it persists — never the prior active's digest, which may
        # predate the secret-digest guard.
        placeholder = _build_upgrade_binding(
            effective_config_digest=compute_effective_config_digest(
                dict(merged_config)))
        # F1 (round 4): the placeholder stage_desired is an InstanceDir write —
        # hold MATERIALIZE_LOCK. F2: `active_before` was read before the lock;
        # re-read inside it and refuse if a concurrent uninstall removed the
        # slug or a concurrent upgrade changed its root (staging here would else
        # recreate a just-removed InstanceDir).
        with specialist_materialize.MATERIALIZE_LOCK:
            _require_active_unchanged(instance_dir, active_before, slug=slug)
            if receipt is not None:
                # Marker BEFORE the stage (Sol r7-2): desired.yaml must
                # never exist without its receipt marker.
                _record_pending_receipt(specialists_dir / slug, receipt.receipt_id)
            instance_dir.stage_desired(make_instance_tuple(
                root=root, binding=placeholder, config_snapshot=merged_config))
        note = f"missing required config/secret: {missing}"
        if dropped_keys:
            note += f"; dropped_config_keys={dropped_keys}"
        return SpecialistInstance(
            slug=slug, stable_agent_id=f"specialist:{slug}", state="pending-configuration",
            active=active_before, desired=instance_dir.desired(),
            last_activation_error=note)

    # Mc: a persona↔role incompatibility is a hard, typed refusal — raise
    # SpecialistInstallError("persona_incompatible") BEFORE any desired tuple
    # is staged (active stays untouched, matching upgrade's transactional
    # contract) so the tool surfaces {ok:false, kind:persona_incompatible}
    # rather than the generic error-state below (which stays reserved for a
    # compile/ceiling ValueError, a genuinely different failure class).
    try:
        check_persona_requirements(role.normalized, persona)
    except ValueError as exc:
        raise SpecialistInstallError("persona_incompatible", str(exc)) from exc
    # #372: binding construction is equation-safe (pure digest arithmetic over
    # schema-known string config) — build it OUTSIDE the try so the compile
    # error arm below can stage an honest error record without re-running a
    # step that could itself raise inside the except.
    effective_config_digest = compute_effective_config_digest(merged_config)
    binding = _build_upgrade_binding(effective_config_digest=effective_config_digest)
    try:
        compile_prompt_bundle(
            role=role, persona=persona, binding=binding,
            platform_frame=(Path(__file__).parent / "defaults" / "personality"
                             / "platform-frame.md").read_text(encoding="utf-8"),
            safety_kernel=(Path(__file__).parent / "defaults" / "personality"
                           / "safety-kernel.md").read_text(encoding="utf-8"))
    except ValueError as exc:
        # F1 (round 4): this error-path stage_desired + discard_desired are
        # InstanceDir writes — hold MATERIALIZE_LOCK. F2: `active_before` was
        # read before the lock; re-read inside it and refuse if a concurrent
        # uninstall/upgrade won, so recording this compile error never
        # resurrects a just-removed slug's InstanceDir (stage_desired would
        # recreate specialists/<slug>/desired.yaml). The concurrent-mutation
        # refusal supersedes the (now-moot) compile error for a vanished slug.
        with specialist_materialize.MATERIALIZE_LOCK:
            _require_active_unchanged(instance_dir, active_before, slug=slug)
            # #372 (Sol design r2): stage the error record over the NEW merged
            # snapshot with the binding computed over that same snapshot — never
            # the prior active's binding, whose digest belongs to a different
            # (possibly pre-guard) mapping. A crash before discard_desired()
            # must not leave a mismatched desired.yaml behind.
            instance_dir.stage_desired(make_instance_tuple(
                root=root, binding=binding, config_snapshot=merged_config))
            instance_dir.discard_desired(reason=str(exc))
        return SpecialistInstance(
            slug=slug, stable_agent_id=f"specialist:{slug}", state="error",
            active=active_before, desired=None, last_activation_error=str(exc))

    # Commit FIRST (every gate above — persona/role compatibility,
    # compile_prompt_bundle — already passed, so this is the authoritative
    # record), THEN materialize as a best-effort follow-up that self-heals
    # via current_specialist_roles_dir if it fails. See
    # commit_specialist_install's docstring for the full rationale.
    # F3 (round 2): commit+materialize under MATERIALIZE_LOCK — see
    # commit_specialist_install's F3 note and the lock's deadlock analysis.
    # F1 (round 3): stage_desired is inside the lock so stage+commit+materialize
    # is one atomic unit against a concurrent same-slug mutation — see
    # commit_specialist_install's F1 note.
    note = f"dropped_config_keys={dropped_keys}" if dropped_keys else None
    with specialist_materialize.MATERIALIZE_LOCK:
        # F2 (round 4): `active_before` was read before the lock; re-read inside
        # it and refuse if a concurrent uninstall removed the slug or a
        # concurrent upgrade/rollback committed a different active — never
        # commit this upgrade over a concurrent winner or recreate a removed dir.
        _require_active_unchanged(instance_dir, active_before, slug=slug)
        instance_dir.stage_desired(make_instance_tuple(
            root=root, binding=binding, config_snapshot=merged_config))
        committed = instance_dir.commit_desired_to_active()  # new binding digest -> new session epoch
        _clear_pending_receipt(specialists_dir / slug)
        # #337 (Sol r1): the commit just rotated the OLD active — possibly
        # carrying legacy plaintext — into active.prior.yaml; strip
        # secret-named keys (the union of both components' declarations).
        _sanitize_prior_snapshot(
            specialists_dir / slug / "active.prior.yaml",
            secret_names=secret_names | prior_secret_names,
            slug=slug)
        try:
            specialist_materialize.materialize_specialist_operational_files(
                agents_specialists_dir=agents_specialists_dir, slug=slug, role=role, persona=persona,
                binding_digest=committed.binding.binding_digest, component_root=committed.root)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "specialist upgrade %r: operational-file materialize failed post-commit "
                "(%s); will self-heal on next reconcile", slug, exc, exc_info=True)
            heal_note = f"operational files pending reconcile: {exc}"
            note = f"{note}; {heal_note}" if note else heal_note
    return SpecialistInstance(
        slug=slug, stable_agent_id=f"specialist:{slug}", state="active", active=committed,
        desired=None, last_activation_error=note)


def _sanitize_prior_snapshot(prior_path: Path, *, secret_names: "set[str]", slug: str) -> None:
    """#337 (Sol r1): ``commit_desired_to_active`` copies the OLD active tuple
    — possibly carrying pre-#337 plaintext secrets in its ``config_snapshot``
    — into ``active.prior.yaml``, which the config git repository tracks and
    rollback restores verbatim. Strip secret-named keys from the retained
    prior. Best-effort by design: this runs post-commit, when the new active
    is already durable, so a failure logs and degrades (the rollback path
    strips again as defense-in-depth)."""
    if not secret_names:
        return
    from atomic_io import atomic_write_text
    from personality_binding import PRE_GUARD_SENTINEL
    try:
        # #372 (D5b): raw rewrite, not a tuple round-trip — when the strip
        # changes the snapshot, the retained digests were computed over a
        # mapping that contained a secret-classified value (the plain→secret
        # reclassification makes this reachable for post-guard priors too).
        # Strip the plaintext AND sentinel both digest fields in the same
        # atomic write: the prior becomes a tombstone rollback refuses
        # (legacy_prior) instead of an oracle the config git repo commits.
        if not prior_path.is_file():
            return
        payload = yaml.safe_load(prior_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return
        snapshot = payload.get("config_snapshot")
        if not isinstance(snapshot, dict) or not snapshot:
            return
        cleaned = {k: v for k, v in snapshot.items() if k not in secret_names}
        if len(cleaned) == len(snapshot):
            return
        payload["config_snapshot"] = cleaned
        payload["config_digest"] = PRE_GUARD_SENTINEL
        binding = payload.get("binding")
        if isinstance(binding, dict):
            binding["effective_config_digest"] = PRE_GUARD_SENTINEL
        atomic_write_text(
            prior_path, yaml.safe_dump(payload, sort_keys=False), mode=0o600)
        logger.info(
            "specialist upgrade %r: stripped secret-classified key(s) from "
            "active.prior.yaml and tombstoned its digests (#337/#372)", slug)
    except Exception:  # noqa: BLE001 — post-commit, best-effort
        logger.warning(
            "specialist %r: prior-snapshot sanitization failed (best-effort; "
            "the committed active is unaffected)", slug, exc_info=True)


def _pre_guard_prior_reason(prior_path: Path) -> "str | None":
    """#372 (D5): classify a retained prior WITHOUT the strict loader. Returns
    a human-readable reason when the file bears the pre-guard tombstone
    sentinel or its config_digest is not the digest of its persisted snapshot
    — the two shapes the boot scrub and the upgrade-time sanitizer produce, or
    that v0.137's digest-preserving sanitization left behind. None means the
    file is absent, unparseable (the strict loader owns that error), or passes
    the digest equation."""
    from personality_binding import PRE_GUARD_SENTINEL, compute_effective_config_digest
    if not prior_path.is_file():
        return None
    try:
        payload = yaml.safe_load(prior_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — unparseable: strict loader owns it
        return None
    if not isinstance(payload, dict):
        return None
    binding = payload.get("binding")
    if payload.get("config_digest") == PRE_GUARD_SENTINEL or (
        isinstance(binding, dict)
        and binding.get("effective_config_digest") == PRE_GUARD_SENTINEL
    ):
        return "its digests were tombstoned by sanitization"
    snapshot = payload.get("config_snapshot") or {}
    if not isinstance(snapshot, dict):
        return "its config_snapshot is not a mapping"
    try:
        expected = compute_effective_config_digest(snapshot)
    except Exception:  # noqa: BLE001 — undigestable snapshot: fail closed
        return "its config_snapshot cannot be canonically digested"
    if payload.get("config_digest") != expected:
        return "its config_digest was not computed over its persisted snapshot"
    return None


def _declared_secret_names_for_root(
    component_root: str, *, specialists_dir: Path,
) -> "set[str] | None":
    """Read a CAS-stored component's ``secret_names`` — used to sanitize
    snapshots that belong to the PRIOR component, whose schema may declare
    different secret names than the incoming one.

    Sol r3 (#337): returns ``None`` — not an empty set — when the schema
    cannot be loaded, so callers FAIL CLOSED (treat every snapshot key as
    potentially secret) instead of silently skipping the strip. Sol r4: the
    schema is read through :func:`load_specialist_component`, whose checksum
    verification distinguishes a GENUINE no-secret declaration from parseable
    corruption (a truncated ``{}`` no longer matches the manifest checksum)."""
    try:
        _, _, checksum = parse_component_root(component_root)
        cas_dir = cas_store_dir(checksum, store_root=specialists_dir / "store")
        component = load_specialist_component(cas_dir, cas_dir / "manifest.json")
        # Sol r5: the loader's internal check only proves manifest↔files
        # consistency — a tamper that rewrites BOTH passes it. The CAS dir is
        # named by the FULL root digest; recompute it and require a match.
        deps = resolve_dependency_closure(component, cas_dir)
        recomputed = compute_install_root_digest(
            component, deps, manifest_bytes=(cas_dir / "manifest.json").read_bytes())
        if recomputed != checksum:
            logger.warning(
                "component %s: CAS content no longer matches its root digest — "
                "treating every carried snapshot key as potentially secret "
                "(fail closed)", component_root)
            return None
        return set(component.config_schema.get("secret_names", []) or [])
    except Exception:  # noqa: BLE001 — callers treat None as "strip everything"
        logger.warning(
            "component schema for %s is unloadable — treating every carried "
            "snapshot key as potentially secret (fail closed)", component_root,
            exc_info=True)
        return None


_TUPLE_SNAPSHOT_FILES = (
    "active.yaml", "desired.yaml", "active.prior.yaml",
    "active.yaml.rollback-tmp", "desired.error.yaml",
)


def sanitize_specialist_snapshots(
    specialists_dir: Path = Path("/config/specialists"),
) -> int:
    """#337 (Sol r2): boot-time scrub of legacy plaintext secret keys from
    every persisted specialist tuple snapshot. Two gaps this closes: a
    pre-guard install keeps its plaintext until that slug's next
    upgrade/rollback, and the upgrade's post-commit prior sanitization has a
    crash window — both are healed at the next boot, BEFORE the boot
    config-git snapshot can commit the plaintext.

    Works on the raw YAML payload (so ``desired.error.yaml``'s extra
    ``_error_reason`` and a pending ``.rollback-tmp`` are handled uniformly)
    and strips each snapshot against ITS OWN component root's declared
    ``secret_names``.

    #372 (D3): after the strip, every file is checked against the digest
    equation — ``config_digest == digest(config_snapshot)``. A mismatch means
    the digest was computed over a mapping that no longer equals the persisted
    snapshot (the pre-guard oracle #372 closes), whether or not anything was
    just stripped: residue files (``desired.error.yaml``,
    ``active.yaml.rollback-tmp``) are DELETED; tuple files get both digest
    fields tombstoned with ``PRE_GUARD_SENTINEL`` so the strict loader raises
    the typed #372 error and the index isolates the slug. An unparseable
    tuple file is replaced by a minimal sentinel tombstone (fail closed);
    unparseable residue is deleted. Tombstoning ``desired.yaml`` also removes
    the slug's pending-receipt marker — the configure re-commit can never
    consume it, and a present marker would pin the receipt and its staging
    trees through the boot age sweep forever. Best-effort per file; returns
    the number of files cleaned."""
    from atomic_io import atomic_write_text
    from personality_binding import PRE_GUARD_SENTINEL, compute_effective_config_digest

    _RESIDUE_FILES = ("desired.error.yaml", "active.yaml.rollback-tmp")

    def _digest_mismatch(payload: dict) -> bool:
        if payload.get("config_digest") == PRE_GUARD_SENTINEL:
            return False  # already tombstoned — idempotent
        snapshot = payload.get("config_snapshot") or {}
        if not isinstance(snapshot, dict):
            return True
        try:
            return payload.get("config_digest") != compute_effective_config_digest(snapshot)
        except Exception:  # noqa: BLE001 — undigestable snapshot: fail closed
            return True

    def _tombstone(path: Path, payload: dict, slug: str, filename: str) -> None:
        payload["config_digest"] = PRE_GUARD_SENTINEL
        binding = payload.get("binding")
        if isinstance(binding, dict):
            binding["effective_config_digest"] = PRE_GUARD_SENTINEL
        atomic_write_text(path, yaml.safe_dump(payload, sort_keys=False), mode=0o600)
        logger.warning(
            "specialist %r: %s carries a digest not derived from its "
            "persisted snapshot — tombstoned (#372); uninstall and reinstall",
            slug, filename)
        if filename == "desired.yaml":
            marker = path.parent / "pending-receipt.json"
            if marker.is_file():
                marker.unlink()
                logger.info(
                    "specialist %r: released the pending-receipt marker of "
                    "the tombstoned desired tuple (#372)", slug)

    cleaned = 0
    if not specialists_dir.is_dir():
        return 0
    # One schema lookup per distinct root, not per file — a slug's five
    # snapshot files usually share at most two roots.
    schema_cache: dict[str, "set[str] | None"] = {}
    for slug_dir in sorted(specialists_dir.iterdir()):
        if not slug_dir.is_dir() or slug_dir.name in {"store", ".bundle-staging"}:
            continue
        # #372 (D8): a crash-orphaned atomic-write temporary
        # (`<tuple>.yaml.tmp`) is read by nothing and can hold a complete
        # pre-guard tuple — delete it before the config-git snapshot.
        try:
            for orphan in sorted(slug_dir.glob("*.yaml.tmp")):
                orphan.unlink()
                cleaned += 1
                logger.warning(
                    "specialist %r: deleted orphan tuple write-temporary %s "
                    "(#372)", slug_dir.name, orphan.name)
        except OSError:
            logger.exception(
                "specialist %r: orphan-temporary sweep failed", slug_dir.name)
        for filename in _TUPLE_SNAPSHOT_FILES:
            path = slug_dir / filename
            if not path.is_file():
                continue
            try:
                try:
                    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
                except yaml.YAMLError:
                    payload = None
                if not isinstance(payload, dict):
                    # #372 (D3a): unclassifiable payload — fail closed.
                    if filename in _RESIDUE_FILES:
                        path.unlink()
                        logger.warning(
                            "specialist %r: deleted unparseable residue %s (#372)",
                            slug_dir.name, filename)
                    else:
                        _tombstone(path, {
                            "api_version": "casa.instance-tuple/v1",
                            "config_snapshot": {},
                        }, slug_dir.name, filename)
                    cleaned += 1
                    continue
                mutated = False
                snapshot = payload.get("config_snapshot")
                root = payload.get("root")
                if snapshot and not isinstance(snapshot, dict):
                    # Sol r4 family: a truthy non-mapping snapshot cannot be
                    # classified key-by-key — replace it outright.
                    payload["config_snapshot"] = {}
                    mutated = True
                    logger.info(
                        "specialist %r: replaced a non-mapping config_snapshot "
                        "in %s (#337)", slug_dir.name, filename)
                elif isinstance(snapshot, dict) and snapshot:
                    if not isinstance(root, str) or not root:
                        # Sol r4: an unusable root cannot be classified — scrub
                        # everything rather than skipping into the git snapshot.
                        secret_names: "set[str] | None" = set(snapshot)
                    else:
                        if root not in schema_cache:
                            schema_cache[root] = _declared_secret_names_for_root(
                                root, specialists_dir=specialists_dir)
                        secret_names = schema_cache[root]
                        if secret_names is None:
                            # Sol r3: unloadable schema — scrub EVERY key rather
                            # than silently skipping; the config-git snapshot follows.
                            secret_names = set(snapshot)
                    # Sol r5: str() before sorting — mixed-type mapping keys must
                    # not TypeError out of a fail-closed scrub into the skip arm.
                    stripped = sorted(str(k) for k in snapshot if k in secret_names)
                    if stripped:
                        payload["config_snapshot"] = {
                            k: v for k, v in snapshot.items() if k not in secret_names}
                        mutated = True
                        logger.info(
                            "specialist %r: scrubbed legacy plaintext secret key(s) %s "
                            "from %s (#337)", slug_dir.name, stripped, filename)
                # #372 (D3b): the digest equation is the detector — a strip
                # above ALWAYS breaks it (the digest covered the pre-strip
                # mapping), and an already-sanitized pre-guard file breaks it
                # with nothing left to strip.
                if _digest_mismatch(payload):
                    if filename in _RESIDUE_FILES:
                        path.unlink()
                        logger.warning(
                            "specialist %r: deleted residue %s carrying a "
                            "digest not derived from its snapshot (#372)",
                            slug_dir.name, filename)
                    else:
                        _tombstone(path, payload, slug_dir.name, filename)
                    cleaned += 1
                    continue
                if mutated:
                    atomic_write_text(
                        path, yaml.safe_dump(payload, sort_keys=False), mode=0o600)
                    cleaned += 1
            except Exception:  # noqa: BLE001 — best-effort per file
                logger.warning(
                    "specialist %r: snapshot scrub of %s failed",
                    slug_dir.name, filename, exc_info=True)
    return cleaned


def _prior_owned_entry(slug: str, row: dict) -> dict:
    """Build a registry entry from a prior-generation owned-plugins sidecar row
    (rollback restore, spec §3.4)."""
    import plugin_registry

    src = row.get("source") or {}
    return {
        "name": row["name"],
        "owner": f"specialist:{slug}",
        "manifest_name": row["manifest_name"],
        "targets": [f"specialist:{slug}"],
        "version": row["version"],
        "source": {"type": "github", "repo": src["repo"], "ref": src["ref"],
                   "revision": src["revision"],
                   "subdir": plugin_registry.normalize_subdir(src.get("subdir", ""))},
        "artifact_id": row["artifact_id"],
    }


@_under_specialist_lifecycle_lock
def rollback_specialist(
    *, slug: str, bundle: bool = False,
    acks: "SpecialistInstallAckStore | None" = None,
    specialists_dir: Path = Path("/config/specialists"),
    agents_specialists_dir: Path = Path("/config/agents/specialists"),
    registry_path: "Path | None" = None,
    plugin_store_root: "Path | None" = None,
    ops_dir: "Path | None" = None,
) -> "SpecialistInstance | tuple[SpecialistInstance, object]":
    """Bundle-aware rollback (Task 10). Without `bundle=True` (legacy/direct
    callers) this restores the prior instance tuple and returns a single
    `SpecialistInstance`. WITH `bundle=True` (the tool layer) it wraps the core
    rollback in the journaled bundle transaction: read the prior generation's
    owned-plugins sidecar, PREFLIGHT that every retained artifact is still
    present+valid (`artifact_verdict`; a missing/corrupt one is a typed
    `rollback_artifact_missing` with the active tuple untouched), journal the
    before-state, atomically swap the prior owned set back into the registry,
    roll the tuple + sidecar back, and return `(instance, BundleTxn)` with
    `removed_artifact_ids = current-minus-prior`."""
    import plugin_registry
    import plugin_store
    import specialist_bundle_journal
    from specialist_bundle_journal import BundleTxn
    from personality_binding import (
        InstanceDir, OwnedPluginsSidecarError, owned_plugins_path,
        owned_plugins_prior_path, read_owned_plugins,
    )
    import specialist_materialize

    validate_specialist_slug(slug)
    slug_dir = specialists_dir / slug

    # #810 (INV-SPEC-011, F1): a prior promotion that failed left the retained
    # generation in a PAIR of temporaries, and the visible active.prior.yaml /
    # owned-plugins.prior.yaml are one generation stale. Complete that pair
    # BEFORE reading either retained file, so the generation this rollback
    # restores is the immediately preceding one; if completion itself fails,
    # refuse rather than consume a prior that is not it. One materialize-lock
    # acquisition; the core takes its own later.
    with specialist_materialize.MATERIALIZE_LOCK:
        try:
            InstanceDir(slug_dir).complete_pending_rotation()
        except OSError as exc:
            raise SpecialistInstallError(
                "pending_rotation_failed",
                f"{slug!r}: a pending prior rotation could not be completed ({exc}); "
                "the retained generation is not readable as the immediately "
                "preceding one — retry the rollback") from exc

    if not bundle:
        # #810 (INV-SPEC-011, F2): this arm restores the tuple only — it has
        # no registry, no store and no journal. It may therefore restore a
        # generation only when that generation's owned set is the ACTIVE
        # one; otherwise the released state would be a tuple of one
        # generation beside a registry and sidecar of another, which is the
        # thing the invariant exists to exclude. Absent reads as the empty
        # set on both sides (a pre-sidecar generation, or a specialist that
        # never owned a plugin); a PRESENT but malformed sidecar refuses as
        # today. Nothing has been written when this refuses.
        try:
            prior_doc = read_owned_plugins(owned_plugins_prior_path(slug_dir))
            active_doc = read_owned_plugins(owned_plugins_path(slug_dir))
        except OwnedPluginsSidecarError as exc:
            raise SpecialistInstallError("rollback_sidecar_invalid", str(exc))
        _rows = lambda doc: sorted(  # noqa: E731
            (list(doc.get("plugins") or []) if doc else []),
            key=lambda r: str(r.get("name", "")))
        if _rows(prior_doc) != _rows(active_doc):
            raise SpecialistInstallError(
                "bundle_required",
                f"{slug!r}: the retained prior generation owns a different plugin "
                "set from the active one; a direct rollback cannot swap the "
                "registry — use the bundle rollback (specialist_rollback) so the "
                "owned set is exchanged with the tuple")
        return _rollback_core(
            slug=slug, specialists_dir=specialists_dir,
            agents_specialists_dir=agents_specialists_dir)

    if registry_path is None:
        registry_path = plugin_registry.REGISTRY_PATH
    if plugin_store_root is None:
        plugin_store_root = plugin_store.STORE_ROOT
    if acks is None:
        from specialist_install_consent import SpecialistInstallAckStore
        acks = SpecialistInstallAckStore()

    # Prior owned set from its sidecar (missing sidecar but present prior tuple
    # ⇒ pre-feature generation ⇒ empty owned set, spec §3.4). P1-4: a PRESENT
    # but malformed prior sidecar raises OwnedPluginsSidecarError — refuse the
    # rollback with a typed error rather than silently proceeding with an empty
    # owned set (which would drop the prior owned plugins the rollback exists to
    # restore). This preflight runs BEFORE any durable mutation, so nothing is
    # touched on refusal.
    try:
        prior_sidecar = read_owned_plugins(owned_plugins_prior_path(slug_dir))
    except OwnedPluginsSidecarError as exc:
        raise SpecialistInstallError("rollback_sidecar_invalid", str(exc))
    prior_rows = list(prior_sidecar.get("plugins") or []) if prior_sidecar else []

    # Preflight retained artifacts BEFORE any durable mutation.
    store_root_resolved = Path(plugin_store_root).resolve()
    for row in prior_rows:
        src = row.get("source") or {}
        # Whole-branch F: `read_owned_plugins` already grammar-validated
        # row["name"]/["artifact_id"], but containment-check the DERIVED store
        # path too (defense in depth) — a join must never escape the store root
        # before it is stat'd/hashed by artifact_verdict.
        store_path = Path(plugin_store_root) / row["name"] / row["artifact_id"]
        try:
            store_path.resolve().relative_to(store_root_resolved)
        except ValueError:
            raise SpecialistInstallError(
                "rollback_artifact_missing",
                f"prior owned row {row.get('name')!r} resolves outside the plugin store")
        verdict = plugin_store.artifact_verdict(
            store_path, name=row["name"], repo=src.get("repo", ""),
            revision=src.get("revision", ""), subdir=src.get("subdir", ""),
            artifact_id=row["artifact_id"], manifest_name=row["manifest_name"])
        if verdict is not None:
            raise SpecialistInstallError(
                "rollback_artifact_missing",
                f"retained artifact for {row['name']!r} is unavailable ({verdict})")

    _reg = plugin_registry.load_registry(registry_path)
    before_owned = plugin_registry.owned_entries_for(slug, _reg)
    before_tuple_files = _tuple_files_snapshot(slug_dir)
    ack_records = acks.snapshot_slug(slug)
    _begin_kwargs = {} if ops_dir is None else {"ops_dir": ops_dir}
    journal = specialist_bundle_journal.begin(
        "rollback", slug, before_entries=before_owned,
        before_tuple_files=before_tuple_files, ack_records=ack_records,
        **_begin_kwargs)
    rollback_txn = BundleTxn(
        journal_path=journal, slug=slug, before_entries=before_owned,
        before_tuple_files=before_tuple_files, ack_records=ack_records,
        op="rollback", registry_path=registry_path, specialists_dir=specialists_dir,
        acks_path=acks.path,
        agents_specialists_dir=agents_specialists_dir)
    try:
        prior_entries = [_prior_owned_entry(slug, r) for r in prior_rows]
        before_entries, _ = plugin_registry.apply_owned_swap(
            slug=slug, new_entries=prior_entries, registry_path=registry_path)
        prior_ids = {r["artifact_id"] for r in prior_rows}
        removed = _removed_artifact_ids(before_entries, prior_ids)
        # #810 (INV-SPEC-011): the retained prior's owned document becomes
        # the ACTIVE sidecar inside the core's own commit scope — the tuple
        # commit rotates the outgoing active pair (tuple AND sidecar) into
        # the prior pair, then the restored generation's document is
        # published; one lock scope, so a second call exchanges back.
        prior_doc = prior_sidecar or {"schema_version": 1,
                                      "component_source": {}, "plugins": []}
        instance = _rollback_core(
            slug=slug, specialists_dir=specialists_dir,
            agents_specialists_dir=agents_specialists_dir, owned_doc=prior_doc)
        specialist_bundle_journal.mark_step(journal, "committed")
        txn = BundleTxn(
            journal_path=journal, slug=slug, before_entries=before_entries,
            before_tuple_files=before_tuple_files, ack_records=ack_records,
            removed_artifact_ids=removed,
            new_artifact_ids=tuple(sorted(prior_ids)),
            op="rollback", owned_swap_committed=True,
            removed_owned_names=_removed_owned_names(
                before_entries, prior_entries),
            registry_path=registry_path, specialists_dir=specialists_dir,
            acks_path=acks.path,
            agents_specialists_dir=agents_specialists_dir)
    except BaseException:
        # P1-1: complete the journal ONLY after a SUCCESSFUL rollback. A
        # rollback that raises leaves the in-progress journal on disk so boot
        # reconciliation re-runs it (or quarantines the slug) — completing here
        # would strand a half-rolled-back mutation with no recovery.
        rollback_txn.rollback_disk()
        specialist_bundle_journal.complete(journal)
        raise
    return instance, txn


def _rollback_core(
    *, slug: str, specialists_dir: Path = Path("/config/specialists"),
    agents_specialists_dir: Path = Path("/config/agents/specialists"),
    owned_doc: "dict | None" = None,
) -> "SpecialistInstance":
    """Restore the RETAINED active.prior.yaml as the new active tuple (spec
    §2.4's rollback target — the prior binding's blobs stay pinned exactly
    because a retained tuple still references them, see Task N1d's
    cas_pin_roots). Rollback IS an upgrade to the prior tuple — reuse
    InstanceDir's own stage/commit, never a bespoke restore path.

    #815 (INV-SPEC-012): the retained prior's binding is compiled as stored
    only while its role checksum still equals the prior component's role
    materialized under the CURRENT option resolution. When the resolved model
    moved between retention and rollback (an HA option flip, a MODEL_MAP
    alias move — both fold into the checksum), the binding is RE-DERIVED for
    that role behind the same three gates the loader's re-derivation uses
    (`_rederive_stale_binding`: the store's bytes still hash to the prior's
    root, the role is that component's under live options, the agent id is
    the role's), compiled, and the RE-DERIVED tuple is what gets committed —
    never the stale prior verbatim, or the next load would rewrite the active
    again and the operational-file marker would carry a digest nobody
    persisted. A drifted store or a moved identity is refused by name, the
    active untouched.

    #810 (INV-SPEC-011): `owned_doc`, when given (the bundle arm), is the
    restored generation's owned-plugins document, published as the active
    sidecar inside the same lock scope as the tuple commit, after it — the
    commit itself rotates the outgoing pair into the prior pair."""
    from personality_binding import (
        InstanceDir, InstanceTuple, load_instance_tuple, make_instance_tuple,
    )
    from prompt_compiler import compile_prompt_bundle
    from role_slot import _ha_model_options, materialize_role
    from role_artifact import load_role_artifact
    from persona_pack import load_persona_pack
    from specialist_lifecycle import SpecialistInstance
    import specialist_materialize

    # F1: `slug` is caller-supplied and indexes `specialists_dir / slug`.
    validate_specialist_slug(slug)

    instance_dir = InstanceDir(specialists_dir / slug)
    prior_path = specialists_dir / slug / "active.prior.yaml"
    # #372 (D5): classify the RAW prior before the strict loader touches it —
    # a sentineled or equation-violating prior must surface as a typed
    # legacy_prior refusal (active untouched; the rollback target requires a
    # reinstall), never as an escaped ValueError.
    legacy_reason = _pre_guard_prior_reason(prior_path)
    if legacy_reason is not None:
        raise SpecialistInstallError(
            "legacy_prior",
            f"{slug!r}: retained prior tuple predates the secret-digest guard "
            f"(#372): {legacy_reason}; the current active is untouched — "
            "reinstall to obtain a rollback target")
    prior = load_instance_tuple(prior_path)
    if prior is None:
        raise SpecialistInstallError("no_prior_tuple", f"{slug!r} has no retained prior tuple")

    # F2 (round 4): capture the CURRENT active BEFORE the lock (like upgrade's
    # `active_before`) so the in-lock re-read below can detect a concurrent
    # uninstall/upgrade/rollback that ran while we validated + blocked on the
    # lock. Rollback replaces the running active with `prior`; a rollback with
    # no current active would be a resurrection (staging `prior` recreates a
    # just-removed InstanceDir), so require an active to roll back FROM.
    active_before = instance_dir.active()
    if active_before is None:
        raise SpecialistInstallError(
            "no_active_tuple", f"{slug!r} has no active install to roll back")

    # `prior.root` is ALWAYS the component root (the override fix never
    # touches `root`), independent of prior.binding.mode.
    _, _, checksum = parse_component_root(prior.root)
    cas_dir = cas_store_dir(checksum, store_root=specialists_dir / "store")
    # #815 (INV-SPEC-012, diff review r7): EVERY load of the retained
    # generation's store — the role artifact, the persona pack, the component
    # and its closure — is one guarded step. A store whose bytes drifted
    # surfaces from these loaders as a ValueError (a checksum that no longer
    # matches its manifest, a pack that no longer validates) or an OSError,
    # and every one of them is the SAME fact the digest gate below refuses:
    # the retained root no longer names its bytes. Refused by name, typed,
    # the active untouched — never an unstructured error out of the tool.
    try:
        # ONE pass over the store (diff review r9): the manifest bytes are read
        # here, once, and reused by the digest gate below — nothing after this
        # block touches the store again.
        _manifest_bytes = (cas_dir / "manifest.json").read_bytes()
        role = materialize_role(
            source=load_role_artifact(cas_dir / "role"),
            # #355: resolve ha_option models exactly as the agent loader
            # does — options={} froze the DEFAULT into the checksum, and
            # the loader then rejected the persisted binding.
            options=_ha_model_options())
        if prior.binding.mode == "override":
            from persona_install import installed_personas_root
            personas_root = installed_personas_root()   # #323 (Sol r3-3)
            persona = load_persona_pack(
                personas_root / prior.binding.persona_id / prior.binding.persona_version / "pack",
                personas_root / prior.binding.persona_id / prior.binding.persona_version / "manifest.json",
            )
        else:
            persona = load_persona_pack(cas_dir / "persona" / "pack", cas_dir / "persona" / "manifest.json")
        # Whole-branch review F6 (rollback verification gate): the prior tuple
        # was valid when it was active, but the world may have changed since —
        # a pinned plugin dependency can have been uninstalled/re-published out
        # from under it. Re-run the SAME pre-activation gates upgrade uses (full
        # dependency-closure availability against the prior's CAS bytes + a
        # compile of role/persona/prior-binding) BEFORE staging/committing, so a
        # rollback into a now-broken tuple is refused with a typed error and the
        # current active tuple keeps running untouched.
        prior_component = load_specialist_component(cas_dir, cas_dir / "manifest.json")
        prior_deps = resolve_dependency_closure(prior_component, cas_dir)
        _fresh_root_digest = compute_install_root_digest(
            prior_component, prior_deps, manifest_bytes=_manifest_bytes)
    except Exception as exc:  # noqa: BLE001 — ONE boundary, one rule (diff review r8)
        # Whatever a loader raises — a ValueError, an OSError, a jsonschema
        # ValidationError the component loader deliberately propagates
        # unwrapped, a YAML error — is the same fact at THIS boundary: the
        # retained generation's store cannot be loaded as the generation the
        # prior names. Three review rounds each found one more class escaping
        # an enumerated catch; the enumeration is cut. The class and message
        # travel in the detail, so nothing is hidden, and the active tuple has
        # not been touched.
        raise SpecialistInstallError(
            "compile_failed",
            f"{slug!r}: the retained prior is not restored — its component store "
            f"({prior.root}) could not be loaded as the generation the prior names "
            f"({type(exc).__name__}: {exc}); the current active is untouched") from exc
    unavailable = [d for d in prior_deps if not d.available]
    if unavailable:
        detail = "; ".join(f"{d.kind}:{d.identifier}: {d.detail}" for d in unavailable)
        raise SpecialistInstallError("dependency_unavailable", detail)
    # #815 (INV-SPEC-012, diff review r4): the store-integrity gate runs
    # UNCONDITIONALLY, not only when the role checksum moved — the role
    # checksum does not cover the manifest or the dependency closure, so a
    # store whose bytes drifted under an unchanged role would otherwise be
    # restored to a root that no longer names its bytes. Same predicate as
    # the loader's L1; same typed refusal, naming both roots.
    _prior_id, _prior_version, _prior_suffix = parse_component_root(prior.root)
    if (_fresh_root_digest != _prior_suffix
            or prior_component.component_id != _prior_id
            or prior_component.version != _prior_version):
        raise SpecialistInstallError(
            "compile_failed",
            f"{slug!r}: the retained prior is not restored — the installed component in "
            f"the store ({prior_component.component_id}@{prior_component.version}"
            f"#{_fresh_root_digest}) is not the prior tuple's root ({prior.root}); the "
            "current active is untouched")
    binding = prior.binding
    if binding.role_checksum != role.checksum:
        # #815: the only input that may have moved is the resolved model, and
        # the three legs the loader's `_rederive_stale_binding` proves are all
        # proved HERE without a second pass over the store (diff review r9):
        # L1 — the store's bytes hash to the prior's root — is the unconditional
        # gate above; L2 — the role is that component's under the live option
        # resolution — holds by construction, because `role` IS the store's
        # role artifact materialized under `_ha_model_options()`; L3 — the
        # stored agent id is the role's — is inside the pure helper, which
        # returns the binding UNCHANGED on an identity move so the compile
        # below refuses it naming `stable_agent_id`.
        from personality_binding import rederive_binding_for_role
        binding = rederive_binding_for_role(binding=binding, role=role)
    try:
        compile_prompt_bundle(
            role=role, persona=persona, binding=binding,
            platform_frame=(Path(__file__).parent / "defaults" / "personality"
                             / "platform-frame.md").read_text(encoding="utf-8"),
            safety_kernel=(Path(__file__).parent / "defaults" / "personality"
                           / "safety-kernel.md").read_text(encoding="utf-8"),
        )
    except ValueError as exc:
        raise SpecialistInstallError("compile_failed", str(exc)) from exc
    # The tuple that gets committed: the prior verbatim when nothing moved
    # (byte-identical restore), else the prior with its re-derived binding —
    # same root, same snapshot, same digest equation (#372: the factory
    # derives config_digest from the snapshot it persists).
    restore = (prior if binding is prior.binding
               else make_instance_tuple(root=prior.root, binding=binding,
                                        config_snapshot=prior.config_snapshot))

    # #337/#372 (D5): a pre-v0.137 prior can carry a secret's plaintext with a
    # digest VALIDLY computed over that secret-bearing mapping (the equation
    # holds, so the raw classification above does not catch it). Restoring it
    # would republish the plaintext; stripping it would persist a digest not
    # derived from the stripped snapshot (the write backstop refuses). The
    # only correct disposition is a typed refusal.
    _prior_secret_names = set(prior_component.config_schema.get("secret_names", []) or [])
    _present = sorted(k for k in prior.config_snapshot if k in _prior_secret_names)
    if _present:
        raise SpecialistInstallError(
            "legacy_prior",
            f"{slug!r}: retained prior tuple carries secret-classified key(s) "
            f"{_present} from before the secret-digest guard (#372); the "
            "current active is untouched — reinstall to obtain a rollback target")

    # Commit FIRST, same reordering as commit_specialist_install/
    # upgrade_specialist — `prior` is a previously-active, already-validated
    # tuple (it was active.yaml once before), so committing it back is
    # itself the authoritative act; materialize is a best-effort follow-up
    # that self-heals via current_specialist_roles_dir if it fails.
    # F3 (round 2): commit+materialize under MATERIALIZE_LOCK — see
    # commit_specialist_install's F3 note and the lock's deadlock analysis.
    # F1 (round 3): stage_desired (of the already-loaded `prior` tuple) is
    # inside the lock so stage+commit+materialize is one atomic unit against a
    # concurrent same-slug mutation — see commit_specialist_install's F1 note.
    last_activation_error: str | None = None
    with specialist_materialize.MATERIALIZE_LOCK:
        # F2 (round 4): re-read active under the lock and refuse if it vanished
        # (concurrent uninstall) or its root changed (concurrent upgrade/rollback)
        # since `active_before` — never roll back over a concurrent winner or
        # resurrect a removed InstanceDir.
        _require_active_unchanged(instance_dir, active_before, slug=slug)
        instance_dir.stage_desired(restore)
        committed = instance_dir.commit_desired_to_active()
        if owned_doc is not None:
            # #810: the restored generation's owned set becomes the active
            # sidecar in the SAME scope as its tuple — the commit above
            # already retained the outgoing pair as the new prior pair.
            instance_dir.stage_desired_owned_plugins(owned_doc)
            instance_dir.commit_owned_plugins_desired_to_active()
        try:
            specialist_materialize.materialize_specialist_operational_files(
                agents_specialists_dir=agents_specialists_dir, slug=slug, role=role, persona=persona,
                binding_digest=committed.binding.binding_digest, component_root=committed.root)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "specialist rollback %r: operational-file materialize failed post-commit "
                "(%s); will self-heal on next reconcile", slug, exc, exc_info=True)
            last_activation_error = f"operational files pending reconcile: {exc}"

    return SpecialistInstance(
        slug=slug, stable_agent_id=f"specialist:{slug}", state="active", active=committed,
        desired=None, last_activation_error=last_activation_error)


@_under_specialist_lifecycle_lock
def uninstall_specialist(
    *, slug: str, bundle: bool = False,
    acks: "SpecialistInstallAckStore | None" = None,
    specialists_dir: Path = Path("/config/specialists"),
    agents_specialists_dir: Path = Path("/config/agents/specialists"),
    registry_path: "Path | None" = None,
    ops_dir: "Path | None" = None,
) -> "None | object":
    """Bundle-aware uninstall (Task 10). Without `bundle=True` (legacy/direct
    callers) this removes the instance + op files and returns None. WITH
    `bundle=True` (the tool layer) it wraps the core removal in the journaled
    cascade (spec §3.5): journal the before-state (owned entries, tuple bytes,
    AND the slug's consent-ack records — all captured BEFORE the slug tree is
    deleted), atomically swap the owned entries OUT (new_entries=[]), delete the
    slug tree, retire ALL of the slug's consent acks, and return a BundleTxn
    whose removed_artifact_ids are every pre-swap owned artifact id (drives the
    sequencer's grant/challenge invalidation). Operator-owned entries targeting
    the slug are never touched (they carry no owner).

    Task 10 review, defense-in-depth (F1, due-diligence companion to
    `_reject_receiptless_sourced_deps`): unlike a fresh install/upgrade, a
    non-bundle uninstall doesn't merely skip publishing — it DELETES the
    InstanceDir tree outright, so any owned registry entries a prior bundle
    install/upgrade published for this slug would become PERMANENTLY
    orphaned (owner points at a `specialist:<slug>` that no longer exists,
    with no future reconcile able to notice). That is a strictly worse,
    unrecoverable version of the dangling-pin gap, so refuse the legacy path
    outright when owned entries exist rather than silently stranding them."""
    import plugin_registry

    if not bundle:
        _existing_owned = plugin_registry.owned_entries_for(
            slug, plugin_registry.load_registry(
                registry_path if registry_path is not None else plugin_registry.REGISTRY_PATH))
        if _existing_owned:
            raise SpecialistInstallError(
                "bundle_required",
                f"{slug!r} has {len(_existing_owned)} owned plugin registry "
                "entries from a prior bundle install/upgrade; uninstall must "
                "use bundle=True so they are cascaded out, not stranded")
        return _uninstall_core(
            slug=slug, specialists_dir=specialists_dir,
            agents_specialists_dir=agents_specialists_dir)

    import specialist_bundle_journal
    from specialist_bundle_journal import BundleTxn

    if registry_path is None:
        registry_path = plugin_registry.REGISTRY_PATH
    if acks is None:
        from specialist_install_consent import SpecialistInstallAckStore
        acks = SpecialistInstallAckStore()

    validate_specialist_slug(slug)
    slug_dir = specialists_dir / slug
    _reg = plugin_registry.load_registry(registry_path)
    before_owned = plugin_registry.owned_entries_for(slug, _reg)
    before_tuple_files = _tuple_files_snapshot(slug_dir)
    # Whole-branch J: retire the slug's consent acks ATOMICALLY (one
    # _LEDGER_LOCK critical section) and journal EXACTLY the removed records.
    # The old flow snapshot_slug'd here then retire_slug'd later in a SEPARATE
    # critical section — a concurrent same-slug approval (a consent tap runs
    # outside _PLUGIN_TOOLS_LOCK) landing between the two would be deleted by
    # retire yet absent from the journaled before-state, so a rollback could not
    # restore it. retire_slug returns exactly what it removed under one lock; we
    # journal those and rollback restores them via restore_records. Retiring
    # before begin means a crash in the (tiny) gap loses only the acks — a
    # fail-closed outcome (the operator simply re-approves), never registry/
    # tuple corruption (those mutations all follow begin).
    #
    # Accepted residual (COMMENT-ONLY, Fable #4): a consent tap racing this
    # uninstall could re-record a slug ack in the window between this retire and
    # the tool-layer journal-complete — leaving a stale identity-bound ack. It
    # is fail-closed: the ack is bound to a specific install identity tuple, so
    # any FUTURE install of this slug computes a different identity and never
    # matches it (the stale record can authorize nothing).
    ack_records = acks.retire_slug(slug)
    _begin_kwargs = {} if ops_dir is None else {"ops_dir": ops_dir}
    # P2-5: retire_slug already removed the records from the live ledger; if
    # begin() now raises (an unwritable ops dir, say) those retired records
    # would be lost with no journal to restore them from. Restore exactly the
    # retire_slug() return before propagating, so a begin failure leaves the
    # ack ledger untouched.
    try:
        journal = specialist_bundle_journal.begin(
            "uninstall", slug, before_entries=before_owned,
            before_tuple_files=before_tuple_files, ack_records=ack_records,
            **_begin_kwargs)
    except BaseException:
        acks.restore_records(ack_records)
        raise
    rollback_txn = BundleTxn(
        journal_path=journal, slug=slug, before_entries=before_owned,
        before_tuple_files=before_tuple_files, ack_records=ack_records,
        op="uninstall", registry_path=registry_path, specialists_dir=specialists_dir,
        acks_path=acks.path,
        agents_specialists_dir=agents_specialists_dir)
    try:
        before_entries, _ = plugin_registry.apply_owned_swap(
            slug=slug, new_entries=[], registry_path=registry_path)
        all_ids = tuple(dict.fromkeys(
            e["artifact_id"] for e in before_entries
            if isinstance(e.get("artifact_id"), str)))
        _uninstall_core(slug=slug, specialists_dir=specialists_dir,
                        agents_specialists_dir=agents_specialists_dir)
        # acks already retired atomically above (whole-branch J).
        specialist_bundle_journal.mark_step(journal, "committed")
        txn = BundleTxn(
            journal_path=journal, slug=slug, before_entries=before_entries,
            before_tuple_files=before_tuple_files, ack_records=ack_records,
            removed_artifact_ids=all_ids, new_artifact_ids=(),
            op="uninstall", owned_swap_committed=True,
            removed_owned_names=_removed_owned_names(before_entries, []),
            registry_path=registry_path, specialists_dir=specialists_dir,
            acks_path=acks.path,
            agents_specialists_dir=agents_specialists_dir)
    except BaseException:
        # P1-1: complete the journal ONLY after a SUCCESSFUL rollback. A
        # rollback that raises leaves the in-progress journal on disk so boot
        # reconciliation re-runs it (or quarantines the slug) — completing here
        # would strand a half-rolled-back mutation with no recovery.
        rollback_txn.rollback_disk()
        specialist_bundle_journal.complete(journal)
        raise
    return txn


def _uninstall_core(
    *, slug: str, specialists_dir: Path = Path("/config/specialists"),
    agents_specialists_dir: Path = Path("/config/agents/specialists"),
) -> None:
    """Removes the instance and its legacy operational directory. Does NOT
    delete CAS blobs (Task N1d's GC-root policy: a blob stays pinned while
    ANY tuple of ANY installed specialist references it — deletion here
    would need a full cross-specialist reference scan, which is exactly what
    Task N1d's `cas_pin_roots` builds; the GC SWEEP itself stays deferred
    per this plan's Global Constraints).

    `agents_specialists_dir / slug` is a SYMLINK to a versioned content
    directory (materialize_specialist_operational_files) once the specialist
    has ever materialized, not a real directory. `shutil.rmtree` deliberately
    REFUSES to operate on a symlink (raises OSError; with ignore_errors=True
    it silently does nothing at all) — calling it directly on a symlinked
    slug_dir would leave BOTH the symlink and its target behind, a silent
    uninstall no-op. Unlink the symlink itself, then remove the versioned
    content directory it pointed at.

    F1: `slug` is caller-supplied and indexes both `agents_specialists_dir /
    slug` and `specialists_dir / slug` — validate before either join.
    F2: `os.readlink` on the materialize symlink used to be turned into an
    `shutil.rmtree` target directly. A PRE-EXISTING malicious or accidental
    symlink whose target is absolute (`finance -> /data`) or escapes the
    directory would make that rmtree delete out-of-tree content.
    `resolve_material_content_dir` fails closed on any non-contained target —
    then we unlink ONLY the symlink and never rmtree its target."""
    import specialist_materialize
    from specialist_materialize import resolve_material_content_dir

    validate_specialist_slug(slug)
    # F2 (round 3): hold MATERIALIZE_LOCK across the WHOLE removal — op symlink/
    # content AND the InstanceDir tree. Without it a self-heal reconcile that
    # already passed its in-lock active-tuple re-read could rematerialize this
    # slug's op-dir from retained CAS bytes AFTER uninstall removed it,
    # resurrecting the removed specialist until the next reconcile. Serializing
    # under the same lock makes the two orderings both safe: reconcile-then-
    # uninstall completes the rematerialize, then uninstall removes it; uninstall-
    # then-reconcile removes active.yaml FIRST, so the reconcile's in-lock
    # `InstanceDir(...).active()` re-read (specialist_materialize
    # _reconcile_specialist_operational_files) yields None and the slug is
    # skipped — no resurrection either way. Removing specialists/<slug> (which
    # holds active.yaml) inside the lock is what makes that re-read authoritative.
    with specialist_materialize.MATERIALIZE_LOCK:
        op_dir = agents_specialists_dir / slug
        if op_dir.is_symlink():
            content_dir = resolve_material_content_dir(op_dir, agents_specialists_dir)
            op_dir.unlink(missing_ok=True)
            if content_dir is not None:
                shutil.rmtree(content_dir, ignore_errors=True)
            else:
                logger.warning(
                    "uninstall %r: operational symlink target failed containment; removed the "
                    "symlink only, left its (out-of-tree) target untouched", slug)
        else:
            shutil.rmtree(op_dir, ignore_errors=True)  # legacy real-dir layout, never migrated
        shutil.rmtree(specialists_dir / slug, ignore_errors=True)


# ---------------------------------------------------------------------------
# CAS/persona pin-reference roots (Task N1d, spec §4.4)
# ---------------------------------------------------------------------------


def cas_pin_roots(specialists_dir: Path = Path("/config/specialists")) -> frozenset[str]:
    """Spec §4.4's CAS retention roots — every root_digest referenced by ANY
    installed specialist's active, desired, OR retained-prior tuple. This is
    the pin/reference AUTHORITY; a GC sweep that deletes anything NOT in
    this set is deferred (Global Constraints) but the roots this function
    returns are exactly what such a sweep would need.

    Round-2 fix (finding #8, and a bug the finding #4 override fix exposed):
    parses `tup.root` (the InstanceTuple-level field), NOT
    `tup.binding.component_root` — for an OVERRIDE-mode specialist binding,
    `BindingRecord.component_root` is always `None` (only component-default
    mode populates it; override mode populates `override_source` instead),
    but `InstanceTuple.root` still holds the real component CAS root in
    EVERY mode (finding #4's `apply_persona_override` fix never touches
    `root` for a specialist target) — the original `tup.binding.
    component_root is None: continue` guard would silently un-pin an
    override-applied specialist's component root on every scan, an
    immediate GC-root regression for the exact case this task exists to
    close.

    This function only ever pins SPECIALIST COMPONENT blobs (CAS-addressed
    under `specialists_dir/store/`) — there is nothing image-bundled to pin
    here, since a specialist has no "image-default" binding mode (only
    residents do; see `personality_binding.py`'s mode enum). Spec §4.4's
    "current image defaults are pinned" requirement is closed on the
    PERSONA side instead, by `persona_pin_roots` below, which every caller
    of this function should call alongside it to get the complete pin-root
    set."""
    from personality_binding import load_instance_tuple

    pinned: set[str] = set()
    if not specialists_dir.is_dir():
        return frozenset(pinned)
    for entry in sorted(specialists_dir.iterdir()):
        if not entry.is_dir() or entry.name in {"store", ".staging", ".roles-overlay"}:
            continue
        for filename in ("active.yaml", "desired.yaml", "active.prior.yaml"):
            path = entry / filename
            if not path.is_file():
                continue
            tup = load_instance_tuple(path)
            if tup is None:
                continue
            try:
                _, _, checksum = parse_component_root(tup.root)
            except ValueError:
                continue
            pinned.add(checksum)
    return frozenset(pinned)


def persona_pin_roots(
    *, bindings_dir: Path = Path("/config/bindings"),
    specialists_dir: Path = Path("/config/specialists"),
) -> frozenset[str]:
    """Round-2 addition (finding #8): `cas_pin_roots` only ever pins
    SPECIALIST COMPONENT blobs under `specialists_dir/store/`. Installed
    persona overrides live in a COMPLETELY SEPARATE tree
    (`/config/personas/<persona_id>/<persona_version>/`,
    `persona_install.commit_persona_install`'s write target) with no
    reference-root function of its own — a resident OR specialist actively
    bound to an installed persona via `mode="override"` had no recorded pin
    at all. Scans EVERY InstanceDir under both the resident bindings root
    (`bindings_dir/resident-*`) and the specialist root
    (`specialists_dir/<slug>`), for `active.yaml`/`desired.yaml`/
    `active.prior.yaml`, and records `f"{persona_id}@{persona_version}"` for
    every tuple whose `binding.mode == "override"` — the exact
    `/config/personas/<persona_id>/<persona_version>/` directory that must
    stay referenced.

    Round-3 addition (finding #5): spec §4.4 also pins "the current image
    defaults" — this is NOT limited to whatever a resident's tuple happens
    to reference right now (an override-bound resident's tuple never has
    `mode == "image-default"`, so the scan above alone would silently drop
    the image default the moment EVERY resident is override-bound), and the
    spec's own "offer to reset to the current default" language (§4.4's
    "pinned digest unavailable" clause) requires that reset target to always
    resolve. So every value of `personality_binding.
    IMAGE_DEFAULT_PERSONA_BY_SLOT` (today: `"casa/ellen@0.1.0"`,
    `"casa/tina@0.1.0"`, `"casa/gary@0.1.0"`) is added UNCONDITIONALLY,
    independent of the tuple scan."""
    from personality_binding import IMAGE_DEFAULT_PERSONA_BY_SLOT, load_instance_tuple

    pinned: set[str] = set(IMAGE_DEFAULT_PERSONA_BY_SLOT.values())

    def _scan(root: Path, *, skip_names: frozenset[str] = frozenset()) -> None:
        if not root.is_dir():
            return
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or entry.name in skip_names:
                continue
            for filename in ("active.yaml", "desired.yaml", "active.prior.yaml"):
                path = entry / filename
                if not path.is_file():
                    continue
                tup = load_instance_tuple(path)
                if tup is None or tup.binding.mode != "override":
                    continue
                pinned.add(f"{tup.binding.persona_id}@{tup.binding.persona_version}")

    _scan(bindings_dir)
    _scan(specialists_dir, skip_names=frozenset({"store", ".staging", ".roles-overlay"}))
    return frozenset(pinned)
