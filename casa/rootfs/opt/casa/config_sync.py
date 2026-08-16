"""Three-way /config default-sync reconciler.

Makes image-default-owned config under /config/{agents,policies} track the
shipped defaults at /opt/casa/defaults, preserving genuine runtime edits.
Image-wins on true conflict, made safe by a commit-first snapshot to
/config/.git; a schema backstop forces image-wins on any kept-live file
invalid against the new schema so casa always boots.

Spec: docs/superpowers/specs/2026-06-08-config-sync-reconciler-design.md.
Pure-Python and dependency-injected (git + validator) for unit testing;
__main__ supplies real implementations.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import yaml

import trigger_write_lock
from atomic_io import atomic_write_text
from config import _ENV_RE, text_has_lone_placeholder

logger = logging.getLogger("config_sync")

# Whether a merge-eligible file may be rewritten at all: see
# ``config.text_has_lone_placeholder``, which is shared with the reminder writer
# so the two cannot disagree about what a rewrite can damage.

# In-scope trees, relative to each of the three roots. schema/ keeps its
# own always-overwrite handling in setup-configs.sh and is out of scope here.
SYNC_TREES = ("agents", "policies", "bindings", "specialists")

# --- Entry-level reconcile (#398) -----------------------------------------
#
# Files that are really LISTS OF NAMED ENTRIES, for which the image's copy is a
# SEED rather than the whole truth. Byte-level reconcile destroys anything an
# operator or an agent added to one of these the moment the shipped default also
# changes; resolving per entry keeps image-owned names tracking the image while
# locally-added names survive.
#
# Keyed on (first path component, basename) and deliberately NOT derived from
# the schemas. Deriving it — "any schema with an array of objects" — would
# silently enrol future files whose entries are not order-independent. These
# three are: a trigger's meaning does not depend on its neighbours, and
# delegates/executors are independent prompt cards. That is a fact about these
# files, not about their shape, so the table is written out.
#
# The tree component is load-bearing for the same reason ``_make_validator`` is
# path-aware: ``policies/disclosure.yaml`` reuses the basename of
# ``agents/<role>/disclosure.yaml`` while binding to a different schema, so
# basename-only matching is a known trap in this module.
#
#   (tree, basename) -> (list key, identity key)
MERGE_ELIGIBLE: dict[tuple[str, str], tuple[str, str]] = {
    ("agents", "triggers.yaml"):  ("triggers", "name"),
    ("agents", "delegates.yaml"): ("delegates", "agent"),
    ("agents", "executors.yaml"): ("executors", "executor_type"),
}


def _merge_spec(rel: str) -> tuple[str, str] | None:
    """``(list_key, identity_key)`` for *rel*, or None if it is not
    merge-eligible. Everything not in the table keeps byte-level reconcile."""
    parts = Path(rel).parts
    if len(parts) < 2:
        return None
    return MERGE_ELIGIBLE.get((parts[0], parts[-1]))


class _Unparseable:
    """Sentinel for "this text is not a YAML document at all".

    A distinct value rather than ``None`` because the comparison below asks
    whether two texts MEAN the same thing, and two failures to parse are not
    evidence that they do — see :func:`_same_document`.
    """
    __slots__ = ()

    def __repr__(self) -> str:            # pragma: no cover — diagnostics only
        return "<unparseable>"


_UNPARSEABLE = _Unparseable()


def _parse(text: str):
    """Parse *text* the way the components that CONSUME these files do
    (``agent_loader._read_yaml``, ``reminders._read_doc``): plain
    ``yaml.safe_load``, aliases and all. Returns ``_UNPARSEABLE`` instead of
    raising, since every caller here runs inside boot-critical, non-fatal
    reconciliation.

    Not ``yaml_safety.load_yaml_no_aliases``: that loader exists for content
    whose whole trust model is that it never aliases (persona packs, role
    artifacts, which are parsed nowhere else). Using it on files the loader
    then parses permissively made this module disagree with the loader about
    what is on disk, which is #408 in one sentence.

    Admitting aliases means admitting the two documents an alias can build that
    a tree cannot, so both are refused here rather than met further in: a
    document that aliases one of its own ancestors is CYCLIC, and every step
    after this one walks it (``==`` between entries, ``safe_dump``, jsonschema)
    and would recurse until it raised; one that aliases a shared subtree
    repeatedly EXPANDS, and can be tiny on disk yet astronomically large walked
    (``&a1 [*a0, *a0]`` chained thirty deep is ~2^30 leaves). Refusing is the
    fallback this gate already has for every other irregularity — the file
    takes byte-level resolution, exactly as it did before #408 — and it is why
    ordinary shared anchors, the case #408 is about, stay cheap to accept.
    """
    try:
        doc = yaml.safe_load(text)
    except (Exception, RecursionError):  # noqa: BLE001 — never raise here
        return _UNPARSEABLE
    return doc if _walkable(doc) else _UNPARSEABLE


# Generous enough that no authored config approaches it, small enough that the
# walk below is bounded work: an expansion attack overshoots it by orders of
# magnitude, not by a factor.
_MAX_EXPANDED_NODES = 100_000


def _walkable(doc) -> bool:
    """Whether *doc* is acyclic and does not expand past ``_MAX_EXPANDED_NODES``.

    Counts REVISITS deliberately — a shared subtree is counted once per
    reference, because that is what every downstream walk pays for it. It counts
    SCALARS for the same reason: an alias can share a wide list of scalars as
    readily as a nested one, and charging only for containers let four hundred
    aliases to a four-hundred-scalar list through at a hundred and sixty
    thousand visits. The container set is every one ``SafeLoader`` can build,
    not the two that are obvious: ``!!omap`` and ``!!pairs`` build their entries
    as TUPLES and ``!!set`` builds a set, and a cycle hidden inside a tuple is
    still a cycle every later walk has to follow. (A set cannot hold a cycle —
    its members must be hashable — but it is walked for the budget, since
    charging one node for ten thousand members is the same blind spot.)
    """
    budget = _MAX_EXPANDED_NODES
    on_path: set[int] = set()
    stack: list[tuple[object, bool]] = [(doc, False)]
    while stack:
        node, leaving = stack.pop()
        if leaving:
            on_path.discard(id(node))
            continue
        budget -= 1
        if budget < 0:
            return False
        if not isinstance(node, (dict, list, tuple, set, frozenset)):
            continue
        if id(node) in on_path:
            return False                     # aliases an ancestor → cyclic
        on_path.add(id(node))
        stack.append((node, True))
        for value in (node.values() if isinstance(node, dict) else node):
            stack.append((value, False))
    return True


def _same_document(a: str, b: str) -> bool:
    """Whether *a* and *b* are the same document, ignoring formatting.

    A text that does not parse is the same as NOTHING, not even another text
    that does not parse. Folding both sides to a shared "could not parse" value
    made them compare equal, which the caller reads as "nothing to apply": it
    then writes nothing while the baseline still advances to the new image, so
    the next boot sees baseline and image already agreeing and never reconciles
    the file again. That turns a visible loss into a silently skipped, never
    retried shipped change (#408).
    """
    doc_a = _parse(a)
    doc_b = _parse(b)
    if doc_a is _UNPARSEABLE or doc_b is _UNPARSEABLE:
        return False
    return doc_a == doc_b


@dataclass
class EntryDoc:
    """A merge-eligible document split into the parts reconcile treats
    differently: the top-level fields (carried through untouched) and the
    entries keyed by identity (reconciled one by one)."""
    top_level: dict
    entries: dict[str, dict]


def _entry_doc(text: str, list_key: str, identity_key: str) -> EntryDoc | None:
    """Parse *text* into an :class:`EntryDoc`, or None on ANY irregularity.

    Returning None means "this file is not mergeable", and the caller falls
    back to byte-level reconcile — behaviour that already exists and is already
    tested. That is the whole design of this gate: the alternative is deciding
    what an irregularity *means* (what a duplicate name should resolve to,
    whether a malformed entry can be salvaged), and every such judgment grows a
    new defect each time it is patched. There is no judgment here.

    Never raises. ``reconcile()`` runs at boot and is contractually non-fatal
    (INV-CFG-005), so a malformed config file must not escape as an exception —
    parsing can raise ``yaml.YAMLError`` OR ``RecursionError``, and both fold
    into None here (the same contract ``reminders._load`` documents).

    Parsed with ``_parse``, which admits YAML aliases, because the question
    this gate answers is "can this file be reconciled per entry?" and the file
    is one ``agent_loader`` will parse with plain ``yaml.safe_load`` moments
    later. Refusing an anchored document here bought no safety — the alias is
    expanded downstream regardless — and cost the operator the entry-level
    protection, silently, on the one file where losing an entry means a
    reminder that is never delivered (#408).
    """
    if text_has_lone_placeholder(text):
        # A scalar that is nothing but `${VAR}` makes the file unmergeable,
        # decided on the TEXT and so independent of today's environment. This
        # module parses without resolving — it REWRITES these files, and
        # resolving first would bake the environment into one permanently — so
        # it re-emits through `safe_dump`, which does not preserve quote style.
        # Quoting is what tells the loader such a scalar is text (#409), so a
        # rewrite retypes a locally-added entry authored as
        # `prompt: "${DETAIL}"` the moment the value looks like a number or a
        # flag; per-entry validation then judges that entry invalid and DROPS
        # it — the exact loss entry-level reconcile exists to prevent. Refusing
        # hands the file to byte-level resolution, which is loud: a snapshot, a
        # conflict record and an operator notification. Same door, same reason,
        # same predicate as `reminders.add_entry`.
        return None
    doc = _parse(text)
    if doc is _UNPARSEABLE:
        return None
    if not isinstance(doc, dict):
        return None

    raw = doc.get(list_key)
    if not isinstance(raw, list):
        return None

    entries: dict[str, dict] = {}
    for item in raw:
        if not isinstance(item, dict):
            return None
        identity = item.get(identity_key)
        # `bool` is a subclass of `int` but not of `str`, so an exact str check
        # is enough here; empty is refused because it cannot name anything.
        if not isinstance(identity, str) or not identity:
            return None
        if identity in entries:
            return None                      # duplicate identity → not mergeable
        entries[identity] = item

    top_level = {k: v for k, v in doc.items() if k != list_key}
    return EntryDoc(top_level=top_level, entries=entries)


@dataclass
class MergeOutcome:
    """What the three-way did to each entry, by identity. Sorted for a stable
    report; the merged document's ORDER is a separate concern (image entries
    first, then local additions)."""
    tracked_image: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    kept_local: list[str] = field(default_factory=list)
    displaced_local: list[str] = field(default_factory=list)
    conflicted: list[str] = field(default_factory=list)
    reinserted: list[str] = field(default_factory=list)

    def destroys_local(self) -> bool:
        """True when resolution overrode local content — the trigger for a
        pre-sync snapshot, an unconditional .casabak and a notification."""
        return bool(self.displaced_local or self.conflicted or self.reinserted)

    def sort(self) -> "MergeOutcome":
        for name in ("tracked_image", "deleted", "kept_local",
                     "displaced_local", "conflicted", "reinserted"):
            getattr(self, name).sort()
        return self


def _merge_entries(new: EntryDoc, base: EntryDoc, live: EntryDoc,
                   identity_key: str) -> tuple[dict[str, dict], MergeOutcome]:
    """The three-way, per entry (spec §6.2).

    ``base`` mirrors the PREVIOUS image's defaults, so "in base" means exactly
    "the image owned this name at the last reconcile" — the only ownership
    evidence available. Entries are compared structurally, as they are, with no
    version normalization: a normalizing merge destroys that meaning wherever a
    document cannot be normalized (see #402).

    One principle throughout: **the image only overrides a local change when
    the image itself changed.** A deletion is an extreme edit and takes the
    same rule, rather than earning a second, competing principle.
    """
    out = MergeOutcome()
    merged: dict[str, dict] = {}

    # Image entries first, in the NEW DEFAULT's order.
    for name, new_entry in new.entries.items():
        base_entry = base.entries.get(name)
        live_entry = live.entries.get(name)

        if live_entry is None:
            if base_entry is None:
                merged[name] = new_entry            # brand-new image entry
                out.tracked_image.append(name)
            elif base_entry == new_entry:
                out.kept_local.append(name)         # deleted locally, image idle
            else:
                merged[name] = new_entry            # deleted locally, image moved
                out.reinserted.append(name)
            continue

        if base_entry is None:
            # Both sides created this name since the last reconcile: no
            # ownership evidence either way. The image reserves the names it
            # ships (INV-CFG-006 states the cost of that).
            merged[name] = new_entry
            out.displaced_local.append(name)
        elif live_entry == base_entry:
            merged[name] = new_entry                # untouched → track the image
            out.tracked_image.append(name)
        elif new_entry == base_entry:
            merged[name] = live_entry               # image idle → keep the edit
            out.kept_local.append(name)
        else:
            merged[name] = new_entry                # both moved → image wins
            out.conflicted.append(name)

    # Then everything left in live, in ITS order: local additions, and entries
    # the image dropped.
    for name, live_entry in live.entries.items():
        if name in new.entries:
            continue
        base_entry = base.entries.get(name)
        if base_entry is None:
            merged[name] = live_entry               # locally added → KEEP (#398)
            out.kept_local.append(name)
        elif live_entry == base_entry:
            out.deleted.append(name)                # image dropped it, untouched
        else:
            merged[name] = live_entry               # image dropped it, but edited
            out.kept_local.append(name)

    return merged, out.sort()


@dataclass
class MergeResult:
    """What ``_merge_file`` did. ``refused`` means the caller must fall back to
    the byte-level branch — nothing was written."""
    outcome: MergeOutcome
    dropped: list[str] = field(default_factory=list)
    prior_text: str | None = None      # pre-merge live bytes, for .casabak
    text: str | None = None            # what to write; None = nothing to apply
    copy_default: bool = False         # write the default's bytes instead
    top_level_changed: bool = False    # composed under the default's top level

    @property
    def wrote(self) -> bool:
        """Whether this merge has something to apply. Named for the caller's
        report; the write itself happens in ``reconcile``."""
        return self.text is not None

    def overrode_local(self) -> bool:
        """Whether resolution actually took something away from the operator.

        This drives what they are TOLD, not what is preserved. Preservation is
        unconditional (see ``_apply_merge``); alerting is not, because a merge
        that only adds the image's entries alongside local ones is the ordinary
        case this change exists to make ordinary, and alerting on it would
        train the operator to ignore the alert.
        """
        return (self.outcome.destroys_local() or bool(self.dropped)
                or self.top_level_changed)


def _dump(top_level: dict, list_key: str, entries: dict[str, dict]) -> str:
    doc = dict(top_level)
    doc[list_key] = list(entries.values())
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def _merge_file(*, rel: str, defaults_dir: Path, baseline_dir: Path,
                config_dir: Path, list_key: str, identity_key: str,
                validate_text: Callable[[str, str], str | None],
                ) -> "MergeResult | None":
    """Reconcile one merge-eligible file entry by entry, and COMPUTE what
    should be written — without writing it.

    Writing is the caller's job, and deliberately so: this module's whole
    safety property is that nothing is destroyed before a recovery artifact
    exists. Writing here and recording afterwards would invert that, leaving a
    crash between the two with the local content gone and neither the git
    snapshot nor the sidecar taken.

    Returns None when the file is not mergeable and the caller must use the
    byte-level branch — either the shape gate refused (§6.1) or nothing the
    merge could compose validates (INV-CFG-007: never write a document we
    cannot show is valid).
    """
    try:
        live_text = (config_dir / rel).read_text(encoding="utf-8")
        new_text = (defaults_dir / rel).read_text(encoding="utf-8")
        base_text = (baseline_dir / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    live = _entry_doc(live_text, list_key, identity_key)
    new = _entry_doc(new_text, list_key, identity_key)
    base = _entry_doc(base_text, list_key, identity_key)
    if live is None or new is None or base is None:
        return None

    merged, outcome = _merge_entries(new, base, live, identity_key)

    # Compose under the LIVE top level first, then the default's. The two may
    # legitimately differ — the configurator writes a newer schema_version than
    # the shipped default — and an entry authored under one version can be
    # invalid under the other. Trying both beats dropping the entry, and beats
    # inventing a third version (INV-CFG-008).
    candidates = [live.top_level]
    if new.top_level != live.top_level:
        candidates.append(new.top_level)

    text: str | None = None
    chosen_top: dict | None = None
    for top_level in candidates:
        candidate = _dump(top_level, list_key, merged)
        if validate_text(rel, candidate) is None:
            text, chosen_top = candidate, top_level
            break

    dropped: list[str] = []
    if text is None:
        # Nothing composes cleanly. Drop the entries that fail on their own,
        # under the top level that got furthest (the live one), and try again.
        top_level = live.top_level
        kept = {}
        for name, entry in merged.items():
            if validate_text(rel, _dump(top_level, list_key,
                                        {name: entry})) is None:
                kept[name] = entry
            else:
                if name in new.entries:
                    # The failing entry is one the IMAGE ships. Dropping it
                    # would silently withhold what this update was delivering —
                    # and byte-level resolution would have delivered it, by
                    # copying the default wholesale. Local content may be
                    # sacrificed here (preserved and reported); the image's own
                    # entries may not be. Refuse and let the byte-level branch
                    # take the file.
                    return None
                dropped.append(name)
        if not dropped:
            return None                      # the fault is document-level
        candidate = _dump(top_level, list_key, kept)
        if validate_text(rel, candidate) is not None:
            return None                      # still invalid → byte-level branch
        merged, text, chosen_top = kept, candidate, top_level

    result = MergeResult(outcome=outcome, dropped=dropped,
                         prior_text=live_text,
                         top_level_changed=chosen_top != live.top_level)
    if _same_document(text, live_text):
        return result                        # nothing to apply → leave bytes
    result.text = text
    # Converged on the default → copy its BYTES, so an install with no local
    # entries stays byte-identical to what byte-level reconcile produced.
    result.copy_default = _same_document(text, new_text)
    return result


@dataclass
class SyncReport:
    image_version: str
    pre_sync_sha: str | None = None
    updated: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    schema_forced: list[dict] = field(default_factory=list)
    casabak: list[str] = field(default_factory=list)
    # #398: entry-level reconcile. One record per merge-eligible file that was
    # merged, naming what happened to each entry — the report is read by
    # someone asking where their trigger went, so the answer is per entry.
    merged: list[dict] = field(default_factory=list)
    # Entries dropped because they do not validate; the file survived.
    entries_dropped: list[dict] = field(default_factory=list)
    # Files the reconciler ADOPTS (the image has never shipped them, so there
    # is no default to fall back to) that do not validate. Nothing here can
    # repair them — agent_loader will raise on the next boot — so the only
    # useful thing is to say so loudly instead of passing them through in
    # silence. This is the class that makes an in-place schema tightening
    # boot-fatal; see #402.
    adopted_invalid: list[dict] = field(default_factory=list)
    # Merges abandoned because no recovery artifact could be taken.
    merge_refused: list[dict] = field(default_factory=list)
    # Sidecars written defensively before a merge that took nothing away. Kept
    # apart from `casabak` so the operator notification stays a signal that
    # something was LOST, not that a file was rewritten.
    merge_backup: list[str] = field(default_factory=list)
    # Finding 2 (theme 8/9): the POST-SYNC boot-parity validation of the
    # reconciled /config tree. ``post_sync_errors`` are boot-fatal
    # inconsistencies the reconciler could NOT self-heal (surfaced loudly);
    # ``post_sync_healed`` are files the reconciler removed to keep boot alive.
    post_sync_errors: list[str] = field(default_factory=list)
    post_sync_healed: list[str] = field(default_factory=list)
    notified: bool = False

    def has_overwrites(self) -> bool:
        """True when reconciliation destroyed something the operator or an
        agent had. Drives the operator notification.

        An entry-level merge that only ADDS the image's entries alongside
        local ones is not an overwrite — saying so would fire a "we overwrote
        your config" alert on the ordinary case this change exists to make
        ordinary. Only a merge that displaced, conflicted or re-inserted
        counts (#398).
        """
        return bool(
            self.conflicts or self.schema_forced or self.casabak
            or self.entries_dropped
            or any(m.get("displaced_local") or m.get("conflicted")
                   or m.get("reinserted") or m.get("top_level_changed")
                   for m in self.merged)
        )

    def destroyed_paths(self) -> list[str]:
        """Every path whose local content reconciliation overrode — what the
        operator notification lists."""
        paths = (
            [c["path"] for c in self.conflicts]
            + [c["path"] for c in self.schema_forced]
            + [d["path"] for d in self.entries_dropped]
            + [m["path"] for m in self.merged
               if m.get("displaced_local") or m.get("conflicted")
               or m.get("reinserted") or m.get("top_level_changed")]
            + list(self.casabak)
        )
        seen: dict[str, None] = {}
        for p in paths:
            seen.setdefault(p, None)
        return list(seen)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"


def _list_tree_files(root: Path) -> set[str]:
    """Relative posix paths of regular files under SYNC_TREES of *root*.

    Skips any `.git/` path and `.casabak` sidecars.
    """
    out: set[str] = set()
    root = Path(root)
    for tree in SYNC_TREES:
        base = root / tree
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            if rel.startswith(".git/") or "/.git/" in f"/{rel}":
                continue
            if rel.endswith(".casabak"):
                continue
            out.add(rel)
    return out


def _bytes_equal(a: Path, b: Path) -> bool:
    try:
        return Path(a).read_bytes() == Path(b).read_bytes()
    except OSError:
        return False


def _copy(src_root: Path, rel: str, dst_root: Path) -> None:
    dst = Path(dst_root) / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(src_root) / rel, dst)


def _delete(root: Path, rel: str) -> None:
    p = Path(root) / rel
    try:
        p.unlink()
    except FileNotFoundError:
        return
    # Prune now-empty parent dirs up to (not including) the tree root.
    parent = p.parent
    root = Path(root)
    while parent != root and parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent


def _archive_casabak(config_dir: Path, rel: str, report: SyncReport) -> None:
    src = Path(config_dir) / rel
    bak = src.with_name(src.name + ".casabak")
    shutil.copy2(src, bak)
    report.casabak.append(rel)


# Matches agent_loader's delegates-without-delegate-tool boot fatal, e.g.
#   agent 'assistant': delegates.yaml is non-empty but runtime.yaml ...
_DELEGATE_MISMATCH_RE = re.compile(
    r"agent '([^']+)': delegates\.yaml is non-empty but")

# #311: the seed arm honors an operator's deletion ONLY for paths whose
# absence is boot-valid — today exactly the per-agent delegates.yaml
# (optional in agent_loader's file contract, and the one file the post-sync
# heal deletes). Everything else missing from /config is unconditionally
# reseeded: the reseed is what repairs a deleted REQUIRED file before the
# boot loader fatals on it.
_ABSENCE_VALID_RE = re.compile(r"agents/[^/]+/delegates\.yaml")


def _post_sync_validate_and_heal(
    *, config_dir: Path, defaults_dir: Path, report: SyncReport,
    validate_repo: Callable[[], list[str]] | None,
    ensure_pre_sync: Callable[[], str | None] | None = None,
) -> None:
    """Finding 2 backstop: validate the POST-SYNC /config tree with the boot
    loader and repair-or-surface any boot-fatal inconsistency the reconciler
    itself introduced by re-injecting an image-owned default.

    The known case: the committed tree validly dropped an image-owned
    ``agents/<role>/delegates.yaml`` (and the delegate tool from
    runtime.yaml), which passes the pre-commit gate — but config_sync
    re-seeds the image-owned delegates.yaml here, producing a
    delegates-without-delegate-tool mismatch that FATALs the next boot.

    Best-effort and boot-safe: any error inside this backstop is swallowed so
    the backstop can never itself block boot.
    """
    if validate_repo is None:
        return
    try:
        errors = validate_repo()
    except Exception as exc:  # noqa: BLE001 — backstop must never crash boot
        logger.warning("config_sync post-sync validation raised (ignored): %s", exc)
        return
    if not errors:
        return

    # Self-heal the delegates/tool case: drop the re-injected image-owned
    # delegates.yaml so boot survives. Only when the on-disk file is byte-equal
    # to the image default (proof it is the image-owned copy, not an operator's
    # genuine delegates content) — never delete real user delegation config.
    healed = False
    for err in errors:
        m = _DELEGATE_MISMATCH_RE.search(err)
        if not m:
            continue
        role = m.group(1)
        rel = f"agents/{role}/delegates.yaml"
        live = config_dir / rel
        default = defaults_dir / rel
        if live.exists() and default.exists() and _bytes_equal(live, default):
            _delete(config_dir, rel)
            report.post_sync_healed.append(rel)
            healed = True

    def _revalidate() -> "list[str] | None":
        """Errors, or None when validation could not be run.

        None is NOT "clean". Folding an exception into an empty list would let
        a validator crash read as a healthy tree and authorise a revert on the
        strength of it.
        """
        try:
            return validate_repo()
        except Exception as exc:  # noqa: BLE001 — backstop must never crash boot
            logger.warning("config_sync post-heal validation raised: %s", exc)
            return None

    if healed:
        # Re-validate BEFORE deciding what else is implicated. Acting on a
        # stale error list is how an unrelated merge gets reverted for a fault
        # that has already been repaired.
        again = _revalidate()
        errors = errors if again is None else again

    # #398: a merged file can leave the tree boot-invalid in ways per-file
    # schema validation cannot see, because the inconsistency is BETWEEN files —
    # preserved delegates against a runtime that no longer allows the delegate
    # tool, preserved triggers against a runtime that no longer declares their
    # channel. Review found two such siblings, so this enumerates none of them.
    #
    # EVERY merge is reverted, not searched through for the guilty one. An
    # earlier draft reverted candidates one at a time and restored the ones
    # that did not help; that was more precise and strictly worse in a boot
    # path, because the restore is itself a write that can fail — leaving the
    # shipped default live while the report says the trial was abandoned.
    #
    # This is best-effort per file, NOT atomic across them: a revert can fail
    # partway and leave some merges reverted and some not. Every file the heal
    # wanted to revert but could not — unreadable, unpreservable, or a failed
    # copy — holds its baseline, so the next boot re-derives the decision
    # instead of concluding there is nothing to do. Holding only on a failed
    # COPY left the other two branches turning a transient error into a
    # permanent boot failure. That is acceptable
    # precisely because each file preserves before it is touched, so a partial
    # sweep loses nothing — it just may not have fixed the tree, and the
    # residual errors say so. What it costs is the local entries of merges that
    # were not the cause, and those are in the sidecar and the pre-sync commit.
    if errors and report.merged:
        rels = [r["path"] for r in report.merged
                if (defaults_dir / r["path"]).is_file()]
        reverted: list[str] = []
        for rel in rels:
            prior = _read_or_none(config_dir / rel)
            if prior is None:
                logger.error(
                    "config_sync: %s may be why the tree cannot load, but it "
                    "could not be read to back it up, so it is left as it is",
                    rel,
                )
                report.merge_refused.append(
                    {"path": rel, "reason": "revert unprepared: unreadable"})
                continue
            sha = ensure_pre_sync() if ensure_pre_sync is not None else None
            # A no-op merge wrote nothing and so has no sidecar yet; its live
            # text IS the operator's original content.
            ok = _write_casabak(rel, prior, config_dir, report)
            if sha is None and not ok:
                logger.error(
                    "config_sync: %s may be why the tree cannot load, but "
                    "nothing could preserve it, so it is left as it is", rel,
                )
                report.merge_refused.append(
                    {"path": rel, "reason": "revert unprepared: not preserved"})
                continue
            # Recorded BEFORE the copy: `shutil.copy2` writes the content
            # and then the metadata, so a failure can leave the file already
            # overwritten. Accounting it only on success meant a destructive
            # overwrite could go unreported and unnotified.
            _record_revert(rel, report)
            reverted.append(rel)
            try:
                _copy(defaults_dir, rel, config_dir)
            except OSError as exc:
                # The file may or may not have been replaced. Hold its baseline
                # so the next boot re-derives the whole decision instead of
                # concluding there is nothing to do — otherwise a transient
                # filesystem error becomes a permanent boot failure.
                logger.error("config_sync: could not revert %s: %s", rel, exc)
                report.merge_refused.append(
                    {"path": rel, "reason": f"revert failed: {exc}"})
        if reverted:
            healed = True
            after = _revalidate()
            errors = errors if after is None else after

    # Re-validate after any heal so the report reflects the true residual state.
    if healed:
        final = _revalidate()
        errors = errors if final is None else final

    report.post_sync_errors = list(errors)
    for err in errors:
        logger.error(
            "config_sync POST-SYNC boot-parity error (next boot will FATAL "
            "unless fixed): %s", err,
        )


def _apply_merge(rel: str, result: "MergeResult", report: SyncReport,
                 defaults_dir: Path, config_dir: Path,
                 ensure_pre_sync: Callable[[], str | None]) -> None:
    """Preserve what the merge overrides, THEN write it, then record it.

    Ordering is the whole point and it matches the byte-level branch: the
    module's safety property is commit-first, so nothing is destroyed before a
    recovery artifact exists. An earlier draft wrote first and preserved
    afterwards, which left a crash between the two with the local content gone
    and neither the snapshot nor the sidecar taken.

    A merge that only ADDS the image's entries alongside the operator's takes
    no snapshot and writes no sidecar — there is nothing to recover, and a
    sidecar per update is noise that trains the operator to ignore them. When
    resolution did override local content, BOTH are taken: the git snapshot
    AND a ``.casabak``, unconditionally rather than only when git is
    unavailable, because a commit inside /config/.git is invisible to the
    person who lost the entry.
    """
    out = result.outcome
    sha = None
    if result.text is not None:
        # ALWAYS preserve before writing — never "preserve if we think this
        # write is destructive". Five successive review rounds each found a
        # different path that changed the operator's file while the
        # destructive-or-not test said otherwise: an entry displaced, an entry
        # dropped, a re-inserted deletion, and finally the top-level fields
        # being replaced by the default's while every entry outcome looked
        # purely additive. The test was the defect, not any one of its answers,
        # so it is gone. If the merge writes, the prior state is captured.
        sha = ensure_pre_sync()
        saved = (result.prior_text is not None
                 and _write_casabak(rel, result.prior_text, config_dir, report,
                                    overwrote=result.overrode_local()))
        if sha is not None and not saved:
            logger.warning(
                "config_sync: %s was merged but its .casabak could not be "
                "written; the prior content is recoverable only from the "
                "config repository commit %s", rel, sha,
            )
        if sha is None and not saved:
            # Both recovery routes failed. Proceeding would change the
            # operator's file with nothing to recover it from, which is the one
            # outcome this whole module exists to prevent. Leave it alone and
            # say so; the next boot retries.
            logger.error(
                "config_sync: refusing to merge %s — neither a snapshot nor a "
                "backup of the current file could be taken, so what it would "
                "change could not be recovered", rel,
            )
            report.merge_refused.append({"path": rel,
                                         "reason": "no recovery artifact"})
            return

        try:
            if result.copy_default:
                _copy(defaults_dir, rel, config_dir)
            else:
                atomic_write_text(str(config_dir / rel), result.text)
        except OSError as exc:
            # Contain it to this file. Propagating would abort the pass and
            # skip every later file AND the baseline mirror; the per-file hold
            # now gives the same "the next boot retries" guarantee without that
            # cost, which it could not before the hold existed.
            logger.error("config_sync: could not write merged %s: %s", rel, exc)
            report.merge_refused.append({"path": rel,
                                         "reason": f"write failed: {exc}"})
            return

    report.merged.append({
        "path": rel,
        "tracked_image": out.tracked_image,
        "deleted": out.deleted,
        "kept_local": out.kept_local,
        "displaced_local": out.displaced_local,
        "conflicted": out.conflicted,
        "reinserted": out.reinserted,
        "top_level_changed": result.top_level_changed,
        "pre_sync_sha": sha,
    })
    if result.dropped:
        report.entries_dropped.append({
            "path": rel, "names": result.dropped,
            "reason": "invalid against the current schema",
        })


def _write_casabak(rel: str, text: str, config_dir: Path,
                   report: SyncReport, *, overwrote: bool = True) -> bool:
    """Write the pre-change text beside the file. Never overwrites an existing
    sidecar from earlier in the same run: the FIRST one holds the state the
    operator actually authored, and a later stage's copy would only hold this
    run's own intermediate output.

    ``overwrote`` says whether the change this backs up actually took something
    away from the operator. Only those reach ``casabak`` and therefore the
    notification; a defensive backup taken before a purely additive merge is
    recorded separately, because it is not evidence that anything was lost.
    """
    if rel in report.casabak or rel in report.merge_backup:
        return True
    dst = (config_dir / rel).with_name(Path(rel).name + ".casabak")
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(text, encoding="utf-8")
    except OSError:
        logger.warning("config_sync: could not write %s.casabak", rel)
        return False
    (report.casabak if overwrote else report.merge_backup).append(rel)
    return True


def _try_drop_invalid_entries(**kwargs) -> bool:
    """``_drop_invalid_entries`` with the same containment as the merge: any
    escape falls through to the existing whole-file force-default."""
    try:
        return _drop_invalid_entries(**kwargs)
    except (Exception, RecursionError) as exc:  # noqa: BLE001
        logger.warning(
            "config_sync: entry-level salvage of %s failed (%s); falling back "
            "to whole-file resolution", kwargs.get("rel"), exc,
        )
        return False


def _drop_invalid_entries(*, rel: str, config_dir: Path, defaults_dir: Path,
                          list_key: str, identity_key: str,
                          validate_text: Callable[[str, str], str | None],
                          report: SyncReport,
                          ensure_pre_sync: Callable[[], str | None]) -> bool:
    """Salvage a schema-invalid merge-eligible file by dropping only the
    entries that fail. True when the file was rewritten and the caller should
    skip the whole-file force-default.

    Each entry is validated ALONE, under its own document's top level — never
    rewrapped in another version, which would silently reinterpret it under
    semantics it was not authored for. That distinction is why this is a
    salvage and not a migration; a real migration is #402.

    Preserve-then-write, as in ``_apply_merge`` and for the same reason.
    """
    prior = _read_or_none(config_dir / rel)
    if prior is None:
        return False                       # unreadable → whole-file path
    live = _entry_doc(prior, list_key, identity_key)
    if live is None:
        return False                       # not mergeable → whole-file path
    shipped = _entry_doc(_read_or_none(defaults_dir / rel), list_key,
                         identity_key)
    shipped_names = set(shipped.entries) if shipped else set()

    kept: dict[str, dict] = {}
    dropped: list[str] = []
    for name, entry in live.entries.items():
        if validate_text(rel, _dump(live.top_level, list_key,
                                    {name: entry})) is None:
            kept[name] = entry
        elif name in shipped_names:
            # See _merge_file: never drop an entry the image ships. Forcing the
            # default delivers it; salvaging around it would not.
            return False
        else:
            dropped.append(name)
    if not dropped:
        return False                       # the fault is document-level

    text = _dump(live.top_level, list_key, kept)
    if validate_text(rel, text) is not None:
        return False                       # salvage does not validate either

    sha = ensure_pre_sync()
    saved = _write_casabak(rel, prior, config_dir, report)
    if sha is None and not saved:
        # Same rule as _apply_merge: never destroy local content with nothing
        # to recover it from.
        logger.error(
            "config_sync: refusing to salvage %s — neither a snapshot nor a "
            "backup could be taken", rel,
        )
        report.merge_refused.append({"path": rel,
                                     "reason": "no recovery artifact"})
        return True                        # handled: skip the force-default
    try:
        atomic_write_text(str(config_dir / rel), text)
    except OSError as exc:
        # Same containment as _apply_merge: one file, held for retry, rather
        # than aborting the pass.
        logger.error("config_sync: could not write salvaged %s: %s", rel, exc)
        report.merge_refused.append({"path": rel,
                                     "reason": f"write failed: {exc}"})
        return True
    report.entries_dropped.append({
        "path": rel, "names": sorted(dropped),
        "reason": "invalid against the current schema",
    })
    logger.warning(
        "config_sync backstop: %s — dropped %d invalid entr(y|ies) (%s); "
        "the rest of the file was kept", rel, len(dropped),
        ", ".join(sorted(dropped)),
    )
    return True


def _record_revert(rel: str, report: SyncReport) -> None:
    """Account a COMMITTED revert as destructive.

    A purely additive merge files its sidecar under ``merge_backup``, which
    deliberately does not notify. Once that merge is reverted the operator HAS
    lost the entries, so the path is promoted to ``casabak`` and the merge
    record marked — otherwise the one case where a kept entry really did
    disappear is the one case nobody is told about.
    """
    if rel in report.merge_backup:
        report.merge_backup.remove(rel)
    if rel not in report.casabak:
        report.casabak.append(rel)
    if rel not in report.post_sync_healed:
        report.post_sync_healed.append(rel)
    for rec in report.merged:
        if rec["path"] == rel:
            rec["reverted"] = True
    logger.warning(
        "config_sync: reverted the entry-level merge of %s — the preserved "
        "entries left the tree unable to load. They are in %s.casabak and the "
        "pre-sync commit.", rel, rel,
    )


def _read_or_none(path: Path) -> "str | None":
    """File text, or None when it could not be read.

    None is NOT the empty string. An earlier version returned "" on failure,
    which the heal then accepted as a valid backup — writing an EMPTY sidecar,
    concluding the content was preserved, and overwriting the real file with
    the shipped default. Preservation must be able to fail.
    """
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def reconcile(*, defaults_dir, config_dir, baseline_dir,
              image_version: str, git, validate: Callable[[str], str | None],
              validate_repo: Callable[[], list[str]] | None = None,
              validate_text: Callable[[str, str], str | None] | None = None,
              ) -> SyncReport:
    """Three-way reconcile of /config against the shipped defaults.

    ``validate_text`` validates candidate file text (rather than a file on
    disk) and enables entry-level reconcile for the files in
    ``MERGE_ELIGIBLE`` (#398). Omitting it disables the merge entirely and
    every file keeps byte-level resolution: the merge must never write a
    document it cannot first show to be valid (INV-CFG-007), so no validator
    means no merge rather than an unchecked one.

    The whole pass is held under ``trigger_write_lock.PASS_LOCK`` (#458): a
    reminder written on the event loop between this pass's read and write of a
    role ``triggers.yaml`` would otherwise be discarded, and is unrecoverable
    because the pre-sync git snapshot / ``.casabak`` predate it. The lock is one
    per process and covers every write site in every phase of the pass — the
    two reconcile loops and ``_post_sync_validate_and_heal`` — so a reminder
    mutator (which takes the same lock) can only run before or after the pass,
    never inside it. Reminder mutators are invoked off the loop via
    ``asyncio.to_thread``, so a held pass waits a worker thread, not the loop.
    """
    with trigger_write_lock.PASS_LOCK:
        return _reconcile_impl(
            defaults_dir=defaults_dir, config_dir=config_dir,
            baseline_dir=baseline_dir, image_version=image_version, git=git,
            validate=validate, validate_repo=validate_repo,
            validate_text=validate_text,
        )


def _reconcile_impl(*, defaults_dir, config_dir, baseline_dir,
                    image_version: str, git,
                    validate: Callable[[str], str | None],
                    validate_repo: Callable[[], list[str]] | None = None,
                    validate_text: Callable[[str, str], str | None] | None = None,
                    ) -> SyncReport:
    defaults_dir = Path(defaults_dir)
    config_dir = Path(config_dir)
    baseline_dir = Path(baseline_dir)
    report = SyncReport(image_version=image_version)

    new_files = _list_tree_files(defaults_dir)
    base_files = _list_tree_files(baseline_dir)
    live_files = _list_tree_files(config_dir)

    # Lazy pre-sync snapshot — taken once, before the first image-wins overwrite.
    pre_sync: list[str | None] = []  # box: empty = not captured yet

    def _ensure_pre_sync() -> str | None:
        if not pre_sync:
            if git.available:
                # No `or git.head()` fallback: git.snapshot() now returns None
                # ONLY when the snapshot actually failed (e.g. dubious-ownership
                # or a stale index.lock). Falling back to a stale pre-edit HEAD
                # would record a misleading recovery pointer for an edit the
                # commit never captured — treat a failed snapshot as degraded
                # (sha None) so the caller writes a .casabak instead (M12).
                pre_sync.append(git.snapshot(
                    "casa-sync: pre-sync snapshot before default reconcile"))
            else:
                pre_sync.append(None)
            report.pre_sync_sha = pre_sync[0]
        return pre_sync[0]

    for rel in sorted(new_files | base_files | live_files):
        new_ex = rel in new_files
        base_ex = rel in base_files
        live_ex = rel in live_files

        if not live_ex:
            if new_ex and not (
                base_ex
                and _ABSENCE_VALID_RE.fullmatch(rel)
                and _bytes_equal(defaults_dir / rel, baseline_dir / rel)
            ):
                # create / seed. #311: a tracked absence-valid file the
                # operator deleted (baseline has it, image copy unchanged)
                # is a DELIBERATE deletion and stays deleted — pre-fix the
                # unconditional reseed resurrected the image-owned
                # delegates.yaml every boot, the post-sync heal deleted it
                # again, and every reconcile reported `changed` for a
                # byte-identical tree. An image CHANGE still wins (reseeds
                # once; the mirrored baseline makes the next boot stable),
                # mirroring the edited-file doctrine below. Scoped to
                # absence-valid paths (design r2): a deleted REQUIRED file
                # (e.g. runtime.yaml, boot-fatal when missing) must keep
                # being repaired by the reseed.
                _copy(defaults_dir, rel, config_dir)
                report.updated.append(rel)
            continue                                       # baseline-only & gone: baseline rewrite drops it

        if not base_ex:
            continue                                       # adopt: no ownership proof → keep live

        live_eq_base = _bytes_equal(config_dir / rel, baseline_dir / rel)
        if live_eq_base:                                   # untouched
            if not new_ex:
                _delete(config_dir, rel)
                report.deleted.append(rel)
            elif not _bytes_equal(defaults_dir / rel, baseline_dir / rel):
                _copy(defaults_dir, rel, config_dir)       # image changed → track
                report.updated.append(rel)
            continue

        # live edited
        if not new_ex:
            continue                                       # edited + removed-from-defaults → keep live
        if _bytes_equal(defaults_dir / rel, baseline_dir / rel):
            continue                                       # image unchanged → keep live
        if _bytes_equal(config_dir / rel, defaults_dir / rel):
            continue                                       # converged

        # #398: for a file that is a LIST OF NAMED ENTRIES, resolve per entry
        # instead of replacing the file. The image's copy is a seed, so an
        # entry the operator or an agent added has no reason to die alongside
        # the parts the image owns. Anything that is not cleanly mergeable
        # falls through to the byte-level conflict arm below, unchanged.
        merge_spec = _merge_spec(rel) if validate_text is not None else None
        if merge_spec is not None:
            # One containment point for the whole merge attempt, matching the
            # shape gate's rule: ANY irregularity means byte-level reconcile,
            # which already exists and is already tested. `yaml.safe_dump` and
            # jsonschema can both raise on pathologically nested content that
            # the gate itself accepts — the gate checks an entry is a mapping
            # with a string identity, not how deep its values go — and an
            # escape here would abort the pass mid-loop. Guarding each of them
            # separately is how the next one gets missed.
            try:
                result = _merge_file(
                    rel=rel, defaults_dir=defaults_dir,
                    baseline_dir=baseline_dir, config_dir=config_dir,
                    list_key=merge_spec[0], identity_key=merge_spec[1],
                    validate_text=validate_text,
                )
            except (Exception, RecursionError) as exc:  # noqa: BLE001
                logger.warning(
                    "config_sync: entry-level merge of %s failed (%s); "
                    "falling back to whole-file resolution", rel, exc,
                )
                result = None
            if result is not None:
                _apply_merge(rel, result, report, defaults_dir,
                             config_dir, _ensure_pre_sync)
                continue

        # conflict → image wins
        sha = _ensure_pre_sync()
        if sha is None:
            # No commit captured the user's edit (git unavailable OR present
            # but failing) — snapshot it to a .casabak before clobbering so the
            # edit is always recoverable; never silently destroy it (M12).
            _archive_casabak(config_dir, rel, report)
        _copy(defaults_dir, rel, config_dir)
        report.conflicts.append({"path": rel, "pre_sync_sha": sha})

    # --- Schema backstop (spec §3.4): any kept-live file invalid against the
    # new schema is force-overwritten with the default so boot can't FATAL.
    for rel in sorted(_list_tree_files(config_dir)):
        if rel not in new_files:
            # Adopted: no shipped default, so there is nothing to force. The
            # file is left exactly as it is — but if it does not validate, the
            # next boot will FATAL on it and this is the only place that can
            # see it coming (#402).
            err = validate(rel)
            if err:
                report.adopted_invalid.append({"path": rel, "detail": err})
                logger.error(
                    "config_sync: %s is invalid against the current schema and "
                    "has no shipped default to fall back to, so nothing here "
                    "can repair it. A resident or policy file in this state "
                    "stops the next boot; a specialist or executor one loads "
                    "on an isolated non-fatal path and is skipped: %s",
                    rel, err,
                )
            continue                                   # no default to fall back to
        if _bytes_equal(config_dir / rel, defaults_dir / rel):
            continue                                   # already the default → valid by construction
        err = validate(rel)
        if not err:
            continue

        # A merge refused for want of a recovery artifact must STAY refused:
        # otherwise the salvage below destroys the very entries the refusal
        # protected, and does it with a smaller write that may well succeed
        # where the backup did not.
        if any(m["path"] == rel for m in report.merge_refused):
            continue

        # #398: for a merge-eligible file, drop only the entries that actually
        # fail rather than resetting the file. This is the partial answer to
        # the format-change path: a tightening that a locally-added entry still
        # satisfies no longer costs that entry. An entry the new schema
        # genuinely rejects is still lost — carrying it forward needs a
        # migration (#402).
        merge_spec = _merge_spec(rel) if validate_text is not None else None
        if merge_spec is not None and _try_drop_invalid_entries(
                rel=rel, config_dir=config_dir, defaults_dir=defaults_dir,
                list_key=merge_spec[0],
                identity_key=merge_spec[1], validate_text=validate_text,
                report=report, ensure_pre_sync=_ensure_pre_sync):
            continue

        logger.warning("config_sync backstop: %s invalid vs new schema (%s) — forcing default", rel, err)
        sha = _ensure_pre_sync()
        if sha is None:
            # See conflict-site note: snapshot to .casabak whenever no commit
            # captured the edit (git unavailable or failing) before clobbering.
            _archive_casabak(config_dir, rel, report)
        _copy(defaults_dir, rel, config_dir)
        report.schema_forced.append({"path": rel, "pre_sync_sha": sha})

    # Finding 2 backstop — validate the reconciled tree with the real boot
    # loader and repair-or-surface any inconsistency the re-seed introduced.
    _post_sync_validate_and_heal(
        config_dir=config_dir, defaults_dir=defaults_dir, report=report,
        validate_repo=validate_repo, ensure_pre_sync=_ensure_pre_sync,
    )

    # Mirrored LAST, so anything the post-sync pass could not repair holds its
    # baseline back. Mirroring first meant a failed revert still advanced the
    # baseline: the next boot then saw image == baseline, recorded no merge,
    # never re-ran the heal, and a transient filesystem error had become a
    # permanent boot failure.
    _mirror_baseline(defaults_dir, baseline_dir,
                     hold=[m["path"] for m in report.merge_refused])

    # Every arm that WROTE ANYTHING under /config belongs here, and the
    # sidecars count: `.casabak` files live under agents/**, which the tracking
    # whitelist admits. `merged` and `entries_dropped` were missing, so a pass
    # whose only change was an entry-level merge committed the PRE-sync snapshot
    # and then left the merged file uncommitted in /config/.git indefinitely;
    # so were the recovery-copy arms, so a pass that took a sidecar and then
    # FAILED its write left that sidecar uncommitted the same way. Either way
    # the next pre-sync snapshot eventually sweeps the leftovers up under a
    # message about a different reconcile.
    changed = bool(
        report.updated or report.deleted or report.conflicts
        or report.schema_forced or report.post_sync_healed
        or report.merged or report.entries_dropped
        or report.casabak or report.merge_backup or report.merge_refused
    )
    if changed and git.available:
        git.snapshot(f"casa-sync: default reconcile {image_version}")

    return report


def _mirror_baseline(defaults_dir: Path, baseline_dir: Path,
                     hold: "list[str] | None" = None) -> None:
    """Replace baseline SYNC_TREES with an exact copy of the new defaults.

    ``hold`` names files that were NOT reconciled — because nothing could
    preserve what reconciling them would destroy — and whose baseline entry
    must therefore stay where it is, so the next boot still sees the
    divergence and retries.

    Only those files are held. Holding the whole tree instead would anchor
    every SUCCESSFULLY reconciled file to a stale baseline, and that is not
    merely wasteful: a later operator edit to one of those files would then be
    read as "both sides changed since the baseline" and overwritten as a
    conflict, even though the image had not moved at all.
    """
    hold = list(hold or [])
    held: dict[str, bytes | None] = {}
    for rel in hold:
        p = Path(baseline_dir) / rel
        try:
            held[rel] = p.read_bytes() if p.is_file() else None
        except OSError:
            held[rel] = None

    for tree in SYNC_TREES:
        dst = Path(baseline_dir) / tree
        src = Path(defaults_dir) / tree
        if dst.exists():
            shutil.rmtree(dst)
        if src.is_dir():
            shutil.copytree(src, dst)

    for rel, data in held.items():
        dst = Path(baseline_dir) / rel
        try:
            if data is None:
                dst.unlink(missing_ok=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(data)
        except OSError:
            logger.warning(
                "config_sync: could not hold the baseline for %s; the next "
                "boot may not retry it", rel,
            )


class RealGit:
    """Git shim over the /config repo. Commits all pending changes and
    returns the resulting HEAD sha. ``available`` is False when git is
    missing or *repo* is not a git work-tree (degraded → .casabak path)."""

    def __init__(self, repo) -> None:
        self.repo = str(repo)
        self.available = bool(shutil.which("git")) and Path(self.repo, ".git").is_dir()

    def _run(self, *args: str):
        return __import__("subprocess").run(
            ["git", "-C", self.repo, *args],
            capture_output=True, text=True,
        )

    def head(self) -> str | None:
        if not self.available:
            return None
        res = self._run("rev-parse", "HEAD")
        return res.stdout.strip() if res.returncode == 0 else None

    def snapshot(self, message: str) -> str | None:
        """Commit all pending /config changes; return the resulting HEAD.

        Fails CLOSED: returns None on ANY git error (dubious-ownership, a
        stale index.lock left by a crash mid-commit, a corrupt repo) so the
        reconciler treats the snapshot as not-taken and falls back to a
        .casabak instead of clobbering a user edit uncaptured (M12).
        """
        if not self.available:
            return None
        add = self._run("add", "-A")
        if add.returncode != 0:
            logger.warning("config_sync: git add failed: %s", add.stderr.strip())
            return None
        # `git diff --cached --quiet` exit codes: 0=clean, 1=staged changes,
        # >=2 (e.g. 128)=git error. The old code conflated error with "clean"
        # and skipped the commit, then returned a stale pre-edit HEAD.
        staged = self._run("diff", "--cached", "--quiet")
        if staged.returncode == 1:
            commit = self._run(
                "-c", "user.email=casa@local",
                "-c", "user.name=Casa",
                "commit", "-q", "-m", message,
            )
            if commit.returncode != 0:
                logger.warning(
                    "config_sync: git commit failed: %s", commit.stderr.strip())
                return None
        elif staged.returncode != 0:
            logger.warning(
                "config_sync: git diff --cached failed: %s", staged.stderr.strip())
            return None
        return self.head()


def _schema_for(rel: str):
    """``(schema_name, version)`` for *rel*, or None when no schema binds to
    it. Shared by the file validator and the document validator so the two can
    never disagree about which schema a path uses."""
    import agent_loader as al

    name = Path(rel).name
    parts = Path(rel).parts
    if parts and parts[0] == "agents":
        schema_name = al._SCHEMA_BY_FILENAME.get(name)
        return None if schema_name is None else (schema_name, "v1")
    if parts and parts[0] == "policies":
        return al._SCHEMA_BY_POLICY_FILE.get(name)
    return None


def _make_text_validator(config_dir) -> Callable[[str, str], str | None]:
    """Validate candidate FILE TEXT against the schema bound to *rel*.

    The entry-level merge needs this rather than ``_make_validator``: it must
    decide whether what it is about to write is valid *before* writing it
    (INV-CFG-007), and a file validator can only answer for bytes already on
    disk.

    It validates TEXT rather than an already-parsed document, and runs it
    through ``agent_loader.parse_yaml_text`` — the loader's own pipeline,
    parse-then-substitute — rather than parsing it here. Validating the
    unsubstituted document instead would let the two validators disagree about
    a file using ``${VAR}``, and a merge that passed one check only to fail the
    other would be written and then immediately salvaged again by the backstop.

    Returns an error string when invalid, else None; a path with no schema
    validates trivially, exactly as the file validator does.
    """
    import agent_loader as al

    config_dir = Path(config_dir)

    def validate_text(rel: str, text: str) -> str | None:
        binding = _schema_for(rel)
        if binding is None:
            return None
        schema_name, version = binding
        try:
            doc = al.parse_yaml_text(text, str(config_dir / rel))
        except Exception:  # noqa: BLE001 — unparseable is simply invalid here
            return "document did not parse"
        try:
            # #608: READ-path tolerance. This validator decides whether an
            # entry survives the merge, and the salvage path DROPS an entry it
            # rejects (unless the image ships it) — so validating a stored
            # webhook prompt strictly here would silently delete the
            # operator's trigger on upgrade, not merely refuse it.
            al.validate_persisted(doc, schema_name, str(config_dir / rel),
                                  version=version)
        except al.LoadError as exc:
            return str(exc)
        return None

    return validate_text


def _make_validator(config_dir) -> Callable[[str], str | None]:
    """Validator backed by agent_loader's schema maps + _validate, checking
    a live file at *config_dir/rel* against the NEW image schema (agent_loader
    reads defaults/schema, which is this image's schema). Returns an error
    string when invalid (incl. YAML parse errors), else None. Files with no
    associated schema return None."""
    import agent_loader as al

    config_dir = Path(config_dir)

    def validate(rel: str) -> str | None:
        name = Path(rel).name
        parts = Path(rel).parts
        abs_path = str(config_dir / rel)
        try:
            if parts and parts[0] == "agents":
                schema_name = al._SCHEMA_BY_FILENAME.get(name)
                if schema_name is None:
                    return None
                # #608: read-path tolerance, same helper as the loader and the
                # text validator next door — a live file judged more strictly
                # than boot judges it is a file config_sync would replace or
                # salvage for no reason.
                al.validate_persisted(al._read_yaml(abs_path), schema_name, abs_path)
            elif parts and parts[0] == "policies":
                mapping = al._SCHEMA_BY_POLICY_FILE.get(name)
                if mapping is None:
                    return None
                schema_name, version = mapping
                al.validate_persisted(al._read_yaml(abs_path), schema_name, abs_path,
                                      version=version)
            else:
                return None
        except al.LoadError as exc:
            return str(exc)
        except Exception as exc:  # noqa: BLE001 — see below
            # `_read_yaml` opens the file itself and guards only YAMLError, so
            # an unreadable file, a non-UTF-8 one, a pathological nesting or a
            # ValueError out of env substitution all raise straight through.
            # This validator runs in boot-critical reconcile(), and #398
            # widened the set of paths it sees to include ADOPTED files — an
            # escape here aborts the whole pass, skipping every later file and
            # the baseline mirror. Enumerating the types is how the ValueError
            # was missed the first time, so it takes all of them: a file this
            # cannot read or parse IS invalid, which is what gets reported.
            return f"{rel}: cannot read: {exc}"
        return None

    return validate


def _make_repo_validator(config_dir) -> Callable[[], list[str]]:
    """Post-sync whole-tree validator backed by agent_loader's boot-parity
    ``validate_config_repo`` (hardened in v0.55.0 to faithfully catch boot
    fatals). Returns the list of error strings for the reconciled /config
    tree; used by the Finding 2 backstop to repair-or-surface inconsistencies
    the re-seed introduces."""
    import agent_loader as al

    def validate_repo() -> list[str]:
        return al.validate_config_repo(str(config_dir))

    return validate_repo


def run(*, defaults_dir, config_dir, baseline_dir, report_path,
        image_version: str) -> int:
    """Boot/reload entry point. Non-fatal by contract: logs and returns 0
    on any unexpected error so a reconciler bug never blocks boot."""
    try:
        git = RealGit(config_dir)
        validate = _make_validator(config_dir)
        validate_repo = _make_repo_validator(config_dir)
        validate_text = _make_text_validator(config_dir)
        report = reconcile(
            defaults_dir=defaults_dir, config_dir=config_dir,
            baseline_dir=baseline_dir, image_version=image_version,
            git=git, validate=validate, validate_repo=validate_repo,
            validate_text=validate_text,
        )
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text(report.to_json(), encoding="utf-8")
        logger.info(
            "config_sync: updated=%d deleted=%d conflicts=%d schema_forced=%d "
            "casabak=%d merged=%d entries_dropped=%d post_sync_healed=%d "
            "post_sync_errors=%d",
            len(report.updated), len(report.deleted), len(report.conflicts),
            len(report.schema_forced), len(report.casabak),
            len(report.merged), len(report.entries_dropped),
            len(report.post_sync_healed), len(report.post_sync_errors),
        )
    except Exception as exc:  # noqa: BLE001 — boot-critical: never fatal
        logger.warning("config_sync: reconcile failed (non-fatal): %s", exc)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] config_sync: %(message)s")
    config_dir = os.environ.get("CASA_CONFIG_DIR", "/config")
    defaults_dir = os.environ.get("CASA_DEFAULTS_DIR", "/opt/casa/defaults")
    data_dir = os.environ.get("CASA_DATA_DIR", "/data")
    image_version = os.environ.get("CASA_IMAGE_VERSION", "unknown")
    return run(
        defaults_dir=defaults_dir,
        config_dir=config_dir,
        baseline_dir=os.path.join(data_dir, "config-baseline"),
        report_path=os.path.join(data_dir, "config-sync-report.json"),
        image_version=image_version,
    )


if __name__ == "__main__":
    raise SystemExit(main())
