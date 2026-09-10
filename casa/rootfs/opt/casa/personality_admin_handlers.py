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
# growing an argument its route would have to thread. The specialists tree
# comes from the published index instead (`live_specialists_dir`), because the
# marker must be read out of the SAME tree the instance came from.
SPECIALIST_RECEIPTS_DIR: "object | None" = None


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


def _candidate_snapshot(slug_dir: "Path") -> "tuple[str | None, str | None]":
    """#929 (terra, candidate review): the candidate's own root string and its
    `pending-receipt.json`, read as ONE snapshot of the tree — under the lock
    every writer of those two files holds.

    They are written together and only together: `_record_pending_receipt`
    immediately before `stage_desired`, and `_clear_pending_receipt` in the
    same scope as `commit_desired_to_active`, each inside
    `specialist_materialize.MATERIALIZE_LOCK` (`specialist_install.py`). An
    unlocked reader can therefore see the marker of one candidate beside the
    root of another — and so can a reader that takes the ROOT from the
    published index, which is a snapshot no pending write republishes (a
    pending candidate is never loaded, so no reload follows it). Either
    pairing advertises five inputs the re-commit refuses `checksum_changed`,
    which is the one route this disclosure exists to keep an engagement out
    of. Taking the writers' lock for the two reads makes the pair untearable
    in both directions; the receipt sidecar the marker names is write-once
    and is loaded outside it.

    Loop-safety (the lock's own contract): MATERIALIZE_LOCK is never acquired
    on the event loop — the only caller is `specialist_status_payload`, which
    the route offloads with `asyncio.to_thread`. It is the innermost of the
    three specialist locks and nothing is taken while it is held here.
    """
    import personality_binding
    import specialist_materialize

    root: str | None = None
    marker: str | None = None
    with specialist_materialize.MATERIALIZE_LOCK:
        try:
            desired = personality_binding.InstanceDir(slug_dir).desired()
        except Exception:  # noqa: BLE001
            # A candidate that will not load — bad YAML, a schema failure, a
            # #372 tombstone — has no root to disclose, and the route must
            # still answer for exactly the slug an operator is diagnosing.
            desired = None
        root = getattr(desired, "root", None)
        try:
            marker = (slug_dir / "pending-receipt.json").read_text(encoding="utf-8")
        except OSError:
            marker = None
    return root, marker


def _pending_commit_inputs(slug: str) -> dict[str, object]:
    """#929 (INV-SPEC-015): the five arguments a re-commit of this slug's
    desired candidate takes, each member `None` when it is no longer
    derivable.

    A first install that lands `pending-configuration` retains its receipt,
    its staging tree and a `pending-receipt.json` marker, and the same
    `(inspection, receipt)` pair re-commits to `active` — but a re-inspect
    refuses the now-occupied slug, so a later engagement that was not told
    these values had no route at all. Three of them are already on disk in
    the candidate's own root string; the other two come from the marker and
    the receipt it names — read as one locked snapshot of the tree, never
    the index's root beside the tree's marker (`_candidate_snapshot`).

    Every step degrades to `None` rather than raising: a pending slug that
    predates the marker has none (the boot reader tolerates exactly that,
    `specialist_bundle_journal.reconcile_boot`), an abandoned receipt is
    swept after seven days, and a staging tree can be reclaimed under a
    still-standing candidate. A status route that raised on any of those
    would fail precisely the operator trying to diagnose it. `staged_dir` is
    disclosed only while the recorded path is still a directory: naming a
    reclaimed path would send the engagement to a route that refuses.
    """
    import specialist_receipt
    from specialist_install import parse_component_root
    from specialist_registry import live_specialists_dir

    out: dict[str, object] = {
        "receipt_id": None, "staged_dir": None,
        "component_id": None, "version": None, "root_digest": None}

    specialists_dir = live_specialists_dir()
    if specialists_dir is None:
        return out
    root, marker_text = _candidate_snapshot(Path(specialists_dir) / slug)
    try:
        component_id, version, root_digest = parse_component_root(root)
    except (ValueError, AttributeError, TypeError):
        pass
    else:
        out.update(component_id=component_id, version=version, root_digest=root_digest)

    if marker_text is None:
        return out
    try:
        raw = json.loads(marker_text)
    except ValueError:
        return out
    receipt_id = raw.get("receipt_id") if isinstance(raw, dict) else None
    if not isinstance(receipt_id, str) or not receipt_id:
        return out
    out["receipt_id"] = receipt_id

    receipts_dir = (SPECIALIST_RECEIPTS_DIR if SPECIALIST_RECEIPTS_DIR is not None
                    else specialist_receipt.DEFAULT_RECEIPTS_DIR)
    receipt = specialist_receipt.load(receipt_id, Path(receipts_dir))
    if receipt is None:
        return out
    # `component_staged_path` is NON-attested runtime state: the receipt's
    # digest does not cover it, so a hand-edited or truncated sidecar can load
    # with a null, non-string or EMPTY value there and still be a valid
    # receipt. Empty is checked because `Path("")` is `Path(".")`, a directory
    # that always exists — it would be disclosed as a resumable ".", and the
    # recipe reads any non-null member as resumable (terra, diff review r2).
    staged_path = receipt.component_staged_path
    if not isinstance(staged_path, str) or not staged_path:
        return out
    staged = Path(staged_path)
    if staged.is_dir():
        out["staged_dir"] = str(staged)
    return out


def specialist_status_payload(runtime, *, slug: str) -> dict[str, object]:
    """Blocking: reads the pending candidate, its marker and its receipt
    sidecar off disk, the first two under MATERIALIZE_LOCK. Callers on the
    event loop offload it (see `_specialist_status`)."""
    from specialist_registry import get_installed_instance

    instance = get_installed_instance(slug)
    if instance is None:
        return {"slug": slug, "state": "not_installed"}

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

    payload = {
        "slug": slug,
        "stable_agent_id": instance.stable_agent_id,
        "state": instance.state,
        "active": _tuple_view(instance.active),
        "desired": _tuple_view(instance.desired),
        "last_activation_error": instance.last_activation_error,
    }
    # #929: on the DESIRED CANDIDATE, not on the state string — a pending
    # UPGRADE keeps its active tuple, so the reloaded index calls that slug
    # `active` while the candidate is exactly what a resume re-commits. The
    # index decides only WHETHER there is a candidate to disclose; the five
    # values are read from the tree, so a snapshot older than the tree
    # cannot contribute one of them (terra, candidate review).
    if instance.desired is not None:
        payload["pending_commit"] = _pending_commit_inputs(slug)
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
