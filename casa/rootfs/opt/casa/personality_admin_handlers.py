"""Personality Phase A, Task 14: Unix-socket-only admin routes.

Five ``POST`` routes registered ONLY on the internal Unix-socket
``AppRunner`` built by ``casa_core.start_internal_unix_runner`` (the
svc-casa-mcp socket) — NEVER on the public port-8099 app. They back
``casactl persona inspect/render/diff``, ``casactl specialist status``,
and ``casactl explain``.

Privacy: ``/admin/explain`` defaults to ``show_sensitive=False`` (strips
``system_prompt``/``memory_text`` via ``ExplanationStore.get``) and
requires ``confirmed=true`` in the request body whenever
``show_sensitive=true`` is requested (400 otherwise) — the interactive
TTY + typed ``SHOW`` gate lives in ``casactl`` itself, one layer up.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aiohttp import web

# #929: WHERE a pending slug's source receipt is read from. Its production
# value is `specialist_receipt.DEFAULT_RECEIPTS_DIR`, resolved at call time so
# a test can point this seam at a temporary tree without the status function
# growing an argument its route would have to thread. The specialists TREE is
# the ONE thing still taken from the published index (`live_specialists_dir`):
# WHERE the tree is, a per-process location fixed at boot — never WHAT is in
# it, which after `_tree_candidate_disclosure` comes from the tree itself.
SPECIALIST_RECEIPTS_DIR: "object | None" = None

# #929 (attempt 3): WHERE the bundle journal's ops directory is, for the
# recovery-debt scan that decides whether the tree is mid-transaction. Same
# seam shape and same reason as the receipts directory above — and it is a
# seam rather than a parameter because `specialist_bundle_journal.recovery_debt`
# binds `OPS_DIR` as a DEFAULT ARGUMENT at import time, so monkeypatching the
# module constant does not reach it. Production value resolved at call time.
SPECIALIST_OPS_DIR: "object | None" = None


def _json_type_name(value: object) -> str:
    """The JSON type name of a value decoded by ``await request.json()``.

    Domain: exactly what ``json.loads`` produces — ``None``, ``bool``, ``int``
    or ``float``, ``str``, ``list``, ``dict`` — which is why ``dict`` is the
    final arm rather than a tested one. ``bool`` is checked BEFORE the numeric
    arm because ``isinstance(True, int)`` is true, so a bare ``1`` must read as
    "number" and never as "boolean".
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


class _Observation:
    """One locked look at a slug's tree, with `read`/`absent`/**UNREADABLE**
    kept apart for EVERY read rather than for the ones somebody remembered.

    This is a generalisation, not a third special case, and the escalation
    rule is why. "Unreadable was collapsed into absent" has now been a finding
    three times in this one mechanism, at three different reads: the desired
    candidate (seam round), the receipt sidecar (seam round), and the ACTIVE
    tuple (diff review round 1 — an `EIO` on `active()` read as "not active",
    so a pending UPGRADE was certified and routed to the install tool, which
    refuses it `concurrent_mutation`). Each was a hand-written `try/except`
    with its own private idea of what a failure means. Sharpening a fourth one
    would be the same defect waiting at whichever read is added next, so the
    rule is applied ONCE, here, to every read the snapshot makes.

    The rule: a read either returns a value, is ABSENT for the exception
    classes the caller names as meaning absence (a missing file is a real
    answer), or is UNREADABLE — and one unreadable read makes the whole
    observation unreadable, because nothing about a torn tree can be certified
    from a partial look at it. `reason` names the first read that failed, so
    the payload can say which.
    """

    __slots__ = ("active", "present", "root", "marker", "debt", "reason")

    def __init__(self) -> None:
        self.active = False
        self.present = False
        self.root: "str | None" = None
        self.marker: "str | None" = None
        self.debt = 0
        self.reason: "str | None" = None

    @property
    def readable(self) -> bool:
        return self.reason is None

    def read(self, what: str, fn, *, default, absent: tuple = ()):
        if self.reason is not None:
            return default
        try:
            return fn()
        except absent:
            return default
        except Exception:  # noqa: BLE001
            # ValueError as well as OSError: a name the OS cannot express at
            # all (an embedded NUL) is an unreadable read, not a 500 out of a
            # status route. A candidate that will not LOAD — bad YAML, a
            # schema failure, a #372 tombstone — is likewise a candidate this
            # process could not read, not one that is absent; the index's own
            # scan isolates that slug as state="error" and puts the reason in
            # `last_activation_error`.
            self.reason = f"unreadable_{what}"
            return default

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _Observation):
            return NotImplemented
        return ((self.reason, self.active, self.present, self.root, self.marker,
                 self.debt)
                == (other.reason, other.active, other.present, other.root,
                    other.marker, other.debt))


def _candidate_snapshot(slug: str, slug_dir: "Path", ops_dir=None):
    """#929: everything about this slug's tree that a resume disclosure depends
    on, read as ONE snapshot under the lock every writer of those files holds:
    whether the slug is ACTIVE, whether it holds a desired CANDIDATE, that
    candidate's own root string, its `pending-receipt.json`, and whether the
    next boot still owes RECOVERY for the slug.

    Returns `(active, candidate, root, marker, debt)` where `candidate` is
    `True` / `False` / `None` — present, absent, or **unreadable**. The third
    value is not pedantry: the earlier version of this function caught every
    exception and returned "absent", so a candidate the process could not read
    was reported as one that did not exist, and the staleness comparison then
    asserted the loaded view was wrong when in truth nothing had been
    established (astra, attempt-3 seam round: 0 disclosures and 1 staleness
    claim from a single injected `EIO`). "Cannot tell" is not "they agree", in
    this direction too.

    Why these five together and under this lock. `_record_pending_receipt`
    writes the marker immediately before `stage_desired`, and
    `_clear_pending_receipt` runs in the same scope as
    `commit_desired_to_active`, each inside `specialist_materialize.
    MATERIALIZE_LOCK` (`specialist_install.py`) — so a locked reader cannot see
    the marker of one candidate beside the root of another *within a
    completed writer*. Recovery debt joins them because a writer's journal is
    created BEFORE its tuple write and is NOT serialized by this lock: checking
    it outside the locked read let a candidate be staged, journalled and
    certified between the check and the snapshot (astra and terra, seam round,
    reproduced independently). Read here, and read again in the second
    snapshot, the last thing before anything is certified.

    PRESENCE is read from the tree rather than from the published index
    because the index is refreshed only by agent reloads and a commit that
    lands pending-configuration performs none — see
    `_tree_candidate_disclosure`. The predicate is deliberately the INDEX'S
    OWN (`InstanceDir.desired()`, exactly as `InstalledSpecialistIndex.load`
    decides `pending-configuration`), so the only difference between the two
    answers is WHEN the tree was read, which is what makes comparing them a
    sound staleness test rather than two rules disagreeing.

    Loop-safety (the lock's own contract): MATERIALIZE_LOCK is never acquired
    on the event loop — the only caller is `specialist_status_payload`, which
    the route offloads with `asyncio.to_thread`. It is the innermost of the
    three specialist locks and nothing is taken while it is held here; the
    validation that hashes the staged tree runs OUTSIDE it, between two
    snapshots.
    """
    import personality_binding
    import specialist_bundle_journal
    import specialist_materialize

    obs = _Observation()
    with specialist_materialize.MATERIALIZE_LOCK:
        instance_dir = personality_binding.InstanceDir(slug_dir)
        obs.active = obs.read(
            "active", lambda: instance_dir.active() is not None, default=False)
        desired = obs.read("candidate", instance_dir.desired, default=None)
        obs.present = desired is not None
        obs.root = getattr(desired, "root", None)
        obs.marker = obs.read(
            "marker",
            lambda: (slug_dir / "pending-receipt.json").read_text(encoding="utf-8"),
            default=None, absent=(FileNotFoundError,))
        obs.debt = obs.read("ops", lambda: sum(
            1 for row in specialist_bundle_journal.recovery_debt(
                **({} if ops_dir is None else {"ops_dir": ops_dir}))
            if row["slug"] in (None, slug)), default=0)
    return obs


def _resume_inputs(root: "str | None", marker_text: "str | None",
                   receipts_dir) -> "tuple[dict[str, object], str | None]":
    """#929 (INV-SPEC-015): the five arguments a re-commit of the candidate in
    `_candidate_snapshot`'s snapshot takes, each member `None` when it is no
    longer derivable, plus the receipt itself when it loaded.

    Every step degrades to `None` rather than raising: a pending slug that
    predates the marker has none (the boot reader tolerates exactly that,
    `specialist_bundle_journal.reconcile_boot`), an abandoned receipt is
    swept after seven days, and a staging tree can be reclaimed under a
    still-standing candidate. A status route that raised on any of those would
    fail precisely the operator trying to diagnose it. These five are
    DIAGNOSTIC; whether they may be USED is `_certify`'s answer, not theirs.
    """
    import specialist_receipt
    from specialist_install import parse_component_root

    out: dict[str, object] = {
        "receipt_id": None, "staged_dir": None,
        "component_id": None, "version": None, "root_digest": None}

    try:
        component_id, version, root_digest = parse_component_root(root)
    except (ValueError, AttributeError, TypeError):
        pass
    else:
        out.update(component_id=component_id, version=version, root_digest=root_digest)

    if marker_text is None:
        return out, None
    try:
        raw = json.loads(marker_text)
    except ValueError:
        return out, None
    receipt_id = raw.get("receipt_id") if isinstance(raw, dict) else None
    if not isinstance(receipt_id, str) or not receipt_id:
        return out, None
    out["receipt_id"] = receipt_id

    receipt, readable = specialist_receipt.load_observed(receipt_id, Path(receipts_dir))
    if receipt is None:
        return out, (None if readable else _UNREADABLE)
    # `component_staged_path` is NON-attested runtime state: the receipt's
    # digest does not cover it, so a hand-edited or truncated sidecar can load
    # with a null, non-string or EMPTY value there and still be a valid
    # receipt. Empty is checked because `Path("")` is `Path(".")`, a directory
    # that always exists — it would be disclosed as a resumable ".", and a
    # reader treats any non-null member as resumable (terra, diff review r2).
    staged_path = receipt.component_staged_path
    if not isinstance(staged_path, str) or not staged_path:
        return out, receipt
    staged = Path(staged_path)
    if staged.is_dir():
        out["staged_dir"] = str(staged)
    return out, receipt


_UNREADABLE = object()
"""Sentinel for "the receipt sidecar could not be READ", as distinct from
`None` meaning "no receipt loaded, and that is a settled answer"."""


def _certify(slug: str, inputs: dict, receipt, receipts_dir) -> "tuple[str, str]":
    """#929: apply the acceptance predicate of the tool that will CONSUME the
    disclosed set, to the set itself, before it is disclosed — and name that
    tool.

    This is where the guarantee is established, and the choice of WHERE is the
    whole design. Not at the write: `MATERIALIZE_LOCK` is non-reentrant and
    `specialist_bundle_journal.rollback_disk` re-acquires it itself, requiring
    that no caller hold it (the sync-phase handlers call it from `except`
    blocks after their `with` scope exits), so widening a writer's lock across
    its own rollback deadlocks — and establishing coherence at the write means
    proving every writer's every FAILURE path leaves a coherent tree, an
    argument that has to be remade for paths that do not exist yet. Not at the
    read either: a locked read makes two files contemporaneous, not coherent —
    after a failed write they are contemporaneous and still name different
    candidates. At the point of use the question "could this have been
    produced by a torn write?" is never asked; only "is this set acceptable?",
    which is decidable from the bytes in hand, for every sequence including
    the ones nobody enumerated.

    The predicate is the tools' own (`specialist_install.validate_resume_inputs`
    — literally the function both handlers now run), plus two checks that
    belong to the CLAIM rather than to the handlers: the receipt's own slug,
    and the identity triple against the tree candidate's root. Those two are
    deliberately NOT pushed into the shared predicate: both handlers today
    activate with a caller-supplied `component_id`/`version` that disagrees
    with the staged manifest, so adding them there would turn accepted calls
    into refusals (astra, seam round).

    Returns `(state, reason)`. There are exactly TWO states, and the missing
    third one is the point. An earlier version separated a settled refusal
    (`blocked`, carrying the tool's own kind) from "I could not establish it"
    (`unknown`), so that a reader could act on the first and retry the
    second — and the only action the first licensed was destroying the
    candidate. Every read on the way to a verdict can fail transiently, and
    five review findings in a row were one shape: a read this process could
    not perform, reported as a fact about the tree. The fifth arrived AFTER
    the observation rule had been generalised over every read the snapshot
    makes and after the receipt load had been made to report its own
    readability, because the shared validator loads again internally and
    cannot know.

    So the distinction is CUT rather than repaired a sixth time. This payload
    never asserts that a candidate is permanently unresumable; `reason` is
    advice about what to look at, never authority to destroy anything, and any
    reason may be transient. The verdict a reader acts on is the one that says
    the inputs were checked.
    """
    from specialist_install import validate_resume_inputs

    if inputs["receipt_id"] is None or any(
            inputs[k] is None for k in ("component_id", "version", "root_digest")):
        # A member that is not derivable at all: no marker, a marker that is
        # not JSON or carries no usable id, a root string the checked parser
        # refuses. The caller has already separated "the receipt sidecar is
        # there and unreadable" from this, so what is left is settled.
        return "not_verified", "incomplete_inputs"
    if receipt is None:
        # The id is there and no sidecar loads for it: swept, or tampered and
        # failing its own digest re-derivation. The consuming tool answers
        # this exact refusal.
        return "not_verified", "receipt_required"
    if inputs["staged_dir"] is None:
        # The receipt loaded, but the staging tree it names is gone, is not a
        # directory, or was never a usable string.
        return "not_verified", "staged_dir_invalid"

    if receipt.slug != slug:
        # BEFORE the staged path is followed, not after: the receipt is reached
        # through the marker, and one naming another slug's receipt would
        # otherwise have this route read, parse and hash another slug's staging
        # tree before refusing. The receipt carries no attested component root,
        # so id agreement alone would establish nothing either — which is why
        # the staged bytes still have to participate below.
        return "not_verified", "receipt_mismatch"

    checked = validate_resume_inputs(
        staged_dir=inputs["staged_dir"], receipt_id=inputs["receipt_id"],
        root_digest=inputs["root_digest"], receipts_dir=Path(receipts_dir))
    if not checked.ok:
        return "not_verified", checked.kind
    if (checked.component.component_id, checked.component.version) != (
            inputs["component_id"], inputs["version"]):
        # The handlers read identity from the staged manifest and never compare
        # it to their arguments, so these two are exactly the members whose
        # wrongness the consuming tool cannot catch. The claim is made here, so
        # it is checked here.
        return "not_verified", "checksum_changed"
    return "verified", ""


def _tree_candidate_disclosure(slug: str, loaded_candidate: object) -> dict[str, object]:
    """#929: every status key that describes a pending candidate — its
    PRESENCE, its five values, whether they may be USED, and the one key that
    tells a reader when the payload's loaded view does not describe that
    candidate.

    What was cut from the published index, and why it is a cut. This
    disclosure used to ask the index WHETHER there was a candidate
    (`instance.desired is not None`) and the tree only for the values. The
    index is a snapshot refreshed by agent RELOADS, and a commit that lands
    `pending-configuration` performs none — a pending candidate is
    deliberately not loadable, so nothing reloads and nothing republishes. The
    index is therefore blind to exactly the case this route exists for: after
    a first install that lands pending, it has no instance for the slug at all
    and status answered `not_installed` with NO resume inputs, while the tree
    held all five — the route prevented the recovery it was added to enable.
    A pending UPGRADE is the same defect one step on. Presence and values now
    come from one locked snapshot of the tree, and the index is not consulted
    about this slug's candidate at all.

    What is disclosed, and what it means:

    * `pending_commit` — the five diagnostic values plus `tool`, the name of
      the handler that takes them (`specialist_upgrade` when the slug is
      active, because `commit_specialist_install` refuses an active tuple
      outright; `specialist_install_commit` otherwise). Present whenever a
      candidate is observed, members `None` where not derivable.
    * `pending_commit_check` — `verified`, or `not_verified` with a reason.
      ONLY `verified` licenses a call, and `not_verified` licenses nothing
      at all: it is never a claim that the candidate is permanently
      unresumable, because every read on the way to a verdict can fail
      transiently and five findings in a row were exactly that failure being
      reported as a fact about the tree. The verdict is separate from the
      values on purpose: recovery debt can withhold certification from five
      perfectly derivable, mutually consistent values, and nulling an
      arbitrary member to signal that would misdescribe the evidence.
    * `state_is_stale` — index-vs-tree only, true-only, computed from the
      OBSERVATION and never from the validation, so a torn tree still reports
      staleness truthfully. It is not a resume-validity flag.

    Coherence, since the loaded view and the tree answer different questions.
    `state`, `active` and `desired` remain the RUNNING PROCESS's loaded view —
    what an operator asking "is this slug serving?" is asking, and no tree
    read can answer it. They move at a reload; the tree moves at a commit.
    What a reader is meant to believe: `pending_commit` about what can be
    re-committed, gated by `pending_commit_check`; `state` about what is
    loaded; and that the loaded view catches up at the next reload or restart.

    The slug is fenced HERE, where the caller's string becomes a path, with
    the lifecycle's own canonical rule (`specialist_component.is_valid_slug`,
    the regex `specialist_install.validate_specialist_slug` enforces). Reading
    the tree for ANY requested slug is new with the index cut — before it,
    only a slug the index already held reached disk — so an unfenced value
    would read and YAML-parse a tuple from outside the tree and disclose
    another slug's values under a name that is not that slug. The earlier
    fence was `slug == Path(slug).name`, which ADMITS `".."` (measured:
    `Path("..").name == ".."`) and an embedded NUL; both reviewers reproduced
    it in the seam round. Verification does not subsume the fence either: a
    traversal slug can name a tree elsewhere whose set is internally coherent
    and would then certify. A symlinked slug directory is refused before
    anything under it is read.

    Nothing is disclosed and NO staleness is claimed when there is no
    published tree: "cannot tell" is not "they agree".
    """
    from specialist_component import is_valid_slug
    from specialist_registry import live_specialists_dir

    specialists_dir = live_specialists_dir()
    if specialists_dir is None or not is_valid_slug(slug):
        return {}
    slug_dir = Path(specialists_dir) / slug
    if slug_dir.is_symlink():
        return {}

    receipts_dir = (SPECIALIST_RECEIPTS_DIR if SPECIALIST_RECEIPTS_DIR is not None
                    else _default_receipts_dir())
    ops_dir = SPECIALIST_OPS_DIR
    obs = _candidate_snapshot(slug, slug_dir, ops_dir)

    if not obs.readable:
        # ANY read in the snapshot failed. No candidate assertion, no
        # staleness claim, and a reason naming the read: nothing about a tree
        # that could not be looked at whole can be certified from part of it.
        return {"pending_commit_check": {"state": "not_verified",
                                         "reason": obs.reason}}

    out: dict[str, object] = {}
    if obs.present:
        inputs, receipt = _resume_inputs(obs.root, obs.marker, receipts_dir)
        # The tool is decided by the ACTIVE tuple, which is why an unreadable
        # one had to stop the observation above rather than read as inactive.
        tool = "specialist_upgrade" if obs.active else "specialist_install_commit"
        out["pending_commit"] = {**inputs, "tool": tool}
        if obs.debt:
            state, reason = "not_verified", "recovery_pending"
        elif receipt is _UNREADABLE:
            state, reason = "not_verified", "unreadable_receipt"
        else:
            state, reason = _certify(slug, inputs, receipt, receipts_dir)
            if state == "verified":
                # Re-observe: the validation above hashed a staged tree and
                # resolved a dependency closure OUTSIDE the lock, because that
                # work must not be done while the innermost lifecycle lock is
                # held. One re-check — not a retry loop, which would be a new
                # hazard on a status route — and the recovery-debt scan rides
                # with it, so the last thing read before certifying is whether
                # a writer opened a journal in the meantime. An unreadable
                # second look is a changed one: it is not the observation the
                # validation was done against.
                again = _candidate_snapshot(slug, slug_dir, ops_dir)
                if obs != again or again.debt:
                    state, reason = "not_verified", "observation_changed"
        out["pending_commit_check"] = ({"state": state} if state == "verified"
                                       else {"state": state, "reason": reason})
    # No candidate at all: the payload for a `not_installed`, `active` or
    # `error` slug is exactly what it was before this change. A verdict key on
    # every status call would be noise about a question nobody asked.
    if (loaded_candidate is not None,
            getattr(loaded_candidate, "root", None)) != (obs.present, obs.root):
        out["state_is_stale"] = True
    return out


def _default_receipts_dir():
    import specialist_receipt

    return specialist_receipt.DEFAULT_RECEIPTS_DIR


def specialist_status_payload(runtime, *, slug: str) -> dict[str, object]:
    """Blocking: reads the pending candidate, its marker and its receipt
    sidecar off disk, the first two under MATERIALIZE_LOCK. Callers on the
    event loop offload it (see `_specialist_status`)."""
    from specialist_registry import get_installed_instance

    def _tuple_view(value):
        if value is None:
            return None
        return {
            "root": value.root,
            "persona_id": value.binding.persona_id,
            "persona_version": value.binding.persona_version,
            "binding_digest": value.binding.binding_digest,
            "dependency_digests": list(value.binding.dependency_digests),
            "effective_config_digest": value.binding.effective_config_digest,
            "config_digest": value.config_digest,
        }

    instance = get_installed_instance(slug)
    if instance is None:
        # #929: a slug the loaded index does not know is NOT evidence that the
        # tree holds nothing — a first install that lands pending-configuration
        # reloads nothing, so it never enters the index. This stays the payload
        # for a slug that is genuinely absent everywhere; the disclosure below
        # is what decides which of the two this is.
        payload: dict[str, object] = {"slug": slug, "state": "not_installed"}
    else:
        payload = {
            "slug": slug,
            "stable_agent_id": instance.stable_agent_id,
            "state": instance.state,
            "active": _tuple_view(instance.active),
            "desired": _tuple_view(instance.desired),
            "last_activation_error": instance.last_activation_error,
        }
    # #929: the candidate's presence and its values both come from the tree,
    # and the loaded view above is labelled stale when it does not describe
    # that candidate (`_tree_candidate_disclosure`).
    payload.update(_tree_candidate_disclosure(
        slug, instance.desired if instance is not None else None))
    return payload


def register_personality_admin_routes(
    app: "web.Application",
    *,
    runtime,
) -> None:
    async def _inspect(request: "web.Request") -> "web.Response":
        from markdown_sections import sections

        body = await request.json()
        ref = body.get("persona")
        if not isinstance(ref, str) or not ref:
            return web.json_response({"error": "invalid_persona_ref"}, status=400)
        pack = runtime.persona_packs.get(ref)
        if pack is None:
            return web.json_response({"error": "not_found"}, status=404)
        return web.json_response({
            "persona_id": pack.persona_id,
            "version": pack.version,
            "checksum": pack.checksum,
            "traits": dict(pack.traits),
            # #623: derive from the pack's OWN markdown. The old literal was
            # the loader's MINIMUM (persona_pack.py requires one level-1 Core
            # and some level-2 Negative space), not a description of a pack --
            # the heading namespace is open, so an authored pack may carry any
            # number of further sections and this is the only surface that
            # answers "what is in this persona". Flat sections(), NOT
            # root_sections(): the latter omits nested headings, which would
            # recreate the very defect this fixes.
            "sections": [name for _level, name, _body in sections(pack.markdown)],
        })

    async def _render(request: "web.Request") -> "web.Response":
        body = await request.json()
        role_id, projection = body.get("role"), body.get("projection")
        # v0.188.1 (main-red fix): ``persona`` is OPTIONAL. Absent means
        # "render whatever is bound" (the pre-v0.187.0 contract the tier2
        # e2e exercises); the #356 ref-vs-binding check applies only when a
        # ref is actually supplied — a gate may only demand what the caller
        # writes. Present-but-empty/non-string is still refused.
        # Key PRESENCE decides which contract applies (Sol+Terra, v0.188.1
        # review): an explicit ``"persona": null`` is a present non-string
        # value and is refused — ``body.get()`` alone would conflate it with
        # the absent-key ref-less form.
        persona_supplied = "persona" in body
        ref = body.get("persona")
        if persona_supplied and (not isinstance(ref, str) or not ref):
            return web.json_response({"error": "invalid_persona_ref"}, status=400)
        bundle = runtime.compiled_prompt_bundles.get(role_id)
        if bundle is None or projection not in {"text", "voice", "restricted_webhook"}:
            return web.json_response({"error": "not_found"}, status=404)
        # GH #356: a SUPPLIED ref must name the persona actually bound to
        # this role — previously the field was ignored and `casactl persona
        # render <ref>` could return a different persona's compiled prompt
        # than the one named. Accept the bare persona id or the full
        # "<id>@<version>" ref (ids cannot contain "@", so bare-id is
        # unambiguous against the single active binding).
        if persona_supplied:
            binding = runtime.bindings.get(role_id)
            bound_id = getattr(binding, "persona_id", None)
            bound_ref = (
                f"{bound_id}@{binding.persona_version}" if bound_id else None
            )
            if ref not in {bound_id, bound_ref} or bound_id is None:
                return web.json_response(
                    {"error": "persona_mismatch", "bound_persona": bound_ref},
                    status=409,
                )
        selected = getattr(bundle, projection)
        return web.json_response({
            "digest": selected.digest,
            "estimated_tokens": selected.estimated_tokens,
            "system_prompt": selected.system_prompt,
        })

    async def _diff(request: "web.Request") -> "web.Response":
        body = await request.json()
        role_id, to_ref = body.get("role"), body.get("to")
        role = runtime.role_slots.get(role_id)
        target_persona = runtime.persona_packs.get(to_ref)
        if role is None or target_persona is None:
            return web.json_response({"error": "not_found"}, status=404)
        current_binding = runtime.bindings.get(role_id)
        return web.json_response({
            "role": role_id,
            "current_persona": current_binding.persona_id if current_binding else None,
            "target_persona": target_persona.persona_id,
            "target_checksum": target_persona.checksum,
        })

    async def _specialist_status(request: "web.Request") -> "web.Response":
        body = await request.json()
        slug = body.get("slug")
        if not isinstance(slug, str) or not slug:
            return web.json_response({"error": "invalid_slug"}, status=400)
        # #929: the payload now reads a pending slug's marker, its receipt
        # sidecar and the staged directory — filesystem work, off the loop,
        # for the same reason /admin/explain offloads its store read.
        payload = await asyncio.to_thread(specialist_status_payload, runtime, slug=slug)
        return web.json_response(payload)

    async def _explain(request: "web.Request") -> "web.Response":
        body = await request.json()
        cid = body.get("correlation_id")
        # GH #356: the confirmation gate requires JSON booleans — `bool()`
        # coercion let any truthy value ("false", "no", a non-empty list)
        # pass the documented `confirmed=true` gate and disclose the full
        # system_prompt/memory_text. casactl sends real booleans.
        show_sensitive = body.get("show_sensitive", False)
        confirmed = body.get("confirmed", False)
        # GH #634: one or-joined predicate and one constant payload threw away
        # WHICH field was rejected, so a caller hand-building a POST on the
        # socket got the same bare `invalid_args` for either field and for both
        # at once. Refuse per field, in this source order, naming the field and
        # the JSON TYPE received — never the value, which is caller-supplied
        # and would be a new leak class. `casactl` relays the body verbatim to
        # stderr, so the detail reaches whoever is reading.
        for field, value in (("show_sensitive", show_sensitive),
                             ("confirmed", confirmed)):
            if not isinstance(value, bool):
                return web.json_response({
                    "error": "invalid_args",
                    "detail": (f"{field} must be a JSON boolean; received "
                               f"{_json_type_name(value)}"),
                }, status=400)
        if show_sensitive and not confirmed:
            return web.json_response({"error": "confirmation_required"}, status=400)
        if not isinstance(cid, str) or not cid:
            return web.json_response({"error": "not_found"}, status=404)
        try:
            # F3 (round 3): ExplanationStore.get acquires the store's
            # threading.Lock and does file I/O — offload to a worker thread so a
            # concurrent per-turn store write (also to_thread'd) can never stall
            # the event loop. The three persona routes read only in-memory
            # runtime dicts, so they need no offload; the specialist status
            # route acquired one when it began reading a pending slug's marker
            # and receipt off disk (#929).
            payload = await asyncio.to_thread(
                runtime.explanation_store.get, cid, show_sensitive=show_sensitive)
        except (KeyError, ValueError):
            return web.json_response({"error": "not_found"}, status=404)
        return web.json_response(payload)

    app.router.add_post("/admin/personality/inspect", _inspect)
    app.router.add_post("/admin/personality/render", _render)
    app.router.add_post("/admin/personality/diff", _diff)
    app.router.add_post("/admin/specialist/status", _specialist_status)
    app.router.add_post("/admin/explain", _explain)
