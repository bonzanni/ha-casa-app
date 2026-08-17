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

from aiohttp import web


def specialist_status_payload(runtime, *, slug: str) -> dict[str, object]:
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

    return {
        "slug": slug,
        "stable_agent_id": instance.stable_agent_id,
        "state": instance.state,
        "active": _tuple_view(instance.active),
        "desired": _tuple_view(instance.desired),
        "last_activation_error": instance.last_activation_error,
    }


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
        return web.json_response(specialist_status_payload(runtime, slug=slug))

    async def _explain(request: "web.Request") -> "web.Response":
        body = await request.json()
        cid = body.get("correlation_id")
        # GH #356: the confirmation gate requires JSON booleans — `bool()`
        # coercion let any truthy value ("false", "no", a non-empty list)
        # pass the documented `confirmed=true` gate and disclose the full
        # system_prompt/memory_text. casactl sends real booleans.
        show_sensitive = body.get("show_sensitive", False)
        confirmed = body.get("confirmed", False)
        if not isinstance(show_sensitive, bool) or not isinstance(confirmed, bool):
            return web.json_response({"error": "invalid_args"}, status=400)
        if show_sensitive and not confirmed:
            return web.json_response({"error": "confirmation_required"}, status=400)
        if not isinstance(cid, str) or not cid:
            return web.json_response({"error": "not_found"}, status=404)
        try:
            # F3 (round 3): ExplanationStore.get acquires the store's
            # threading.Lock and does file I/O — offload to a worker thread so a
            # concurrent per-turn store write (also to_thread'd) can never stall
            # the event loop. The other admin routes read only in-memory runtime
            # dicts / the lock-free installed-index snapshot, so none of them
            # need this offload — only the store acquires a lock.
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
