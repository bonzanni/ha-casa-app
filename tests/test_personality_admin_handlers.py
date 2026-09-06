"""Tests for Task 14's ExplanationStore + personality_admin_handlers.py.

Covers:
* ExplanationStore: privacy-safe default output (strips system_prompt/
  memory_text, no reserved casa-source- tags), reserved-tag rejection on
  record(), atomic write (mode 0600), TTL expiry, and the 1000-record cap.
* specialist_status_payload: not_installed vs. active/lifecycle-populated.
* register_personality_admin_routes: all five routes, registered ONLY on
  the app they're handed (unix-socket-only is asserted end-to-end in
  tests/test_unix_socket_runner.py), including the /admin/explain
  confirmation gate.
"""
from __future__ import annotations

import json
import os
import stat
from unittest.mock import Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from explanation_store import EXPLANATION_MAX_RECORDS, ExplanationRecord, ExplanationStore

# asyncio_mode = auto (pytest.ini) auto-detects the `async def` route tests
# below; this module also has plain sync tests (ExplanationStore,
# specialist_status_payload), so no blanket `pytestmark` here.


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    return ExplanationStore(tmp_path / "explanations")


@pytest.fixture
def record():
    return ExplanationRecord(
        correlation_id="cid-abc123",
        role_id="concierge",
        kind="resident",
        resolved_model="claude-sonnet",
        persona_ref="gary@1",
        role_checksum="rc-deadbeef",
        binding_digest="bd-cafef00d",
        dependency_digests=("dep-1", "dep-2"),
        effective_config_digest="ecd-1234",
        lifecycle_state="active",
        projection="text",
        static_prompt_digest="spd-5678",
        static_prompt_estimated_tokens=512,
        memory_tiers=("recent", "recall"),
        memory_attributions=("attr-1",),
        tool_calls=("send_message",),
        denials=(),
        system_prompt="SENSITIVE PERSONA PROSE — never leak this",
        memory_text="SENSITIVE MEMORY TEXT — never leak this",
    )


@pytest.fixture(autouse=True)
def _reset_active_specialist_index():
    import specialist_registry as specialist_registry_mod

    original = specialist_registry_mod._active_index
    yield
    specialist_registry_mod._active_index = original


# ---------------------------------------------------------------------------
# ExplanationStore: privacy-safe output (brief Step 1)
# ---------------------------------------------------------------------------


def test_explanation_default_strips_sensitive_fields(store, record) -> None:
    store.record(record)
    result = store.get(record.correlation_id, show_sensitive=False)
    assert result["role_id"] == record.role_id
    assert result["binding_digest"] == record.binding_digest
    assert "memory_text" not in result
    assert "system_prompt" not in result
    assert "casa-source-" not in json.dumps(result)


def test_explanation_show_sensitive_returns_full_record(store, record) -> None:
    store.record(record)
    result = store.get(record.correlation_id, show_sensitive=True)
    assert result["system_prompt"] == record.system_prompt
    assert result["memory_text"] == record.memory_text


def test_explanation_no_persona_prose_in_default_output(store, record) -> None:
    """Extra assertion beyond the brief's literal test: the persona_ref
    field (an id/version string, never prose) is fine to expose, but the
    stripped output must contain none of the sensitive prose text."""
    store.record(record)
    result = store.get(record.correlation_id, show_sensitive=False)
    encoded = json.dumps(result)
    assert "SENSITIVE PERSONA PROSE" not in encoded
    assert "SENSITIVE MEMORY TEXT" not in encoded


def test_explanation_default_strips_memory_tiers(store, record) -> None:
    """GH #202: the sensitivity-tier tokens are metadata gated behind the SAME
    show_sensitive confirmation as the prompt/memory prose."""
    store.record(record)
    result = store.get(record.correlation_id, show_sensitive=False)
    assert "memory_tiers" not in result
    # Attribution labels are already clearance-gated identity strings, not tier
    # tokens — they stay visible in the default output.
    assert result["memory_attributions"] == list(record.memory_attributions)


def test_explanation_show_sensitive_returns_memory_tiers(store, record) -> None:
    store.record(record)
    result = store.get(record.correlation_id, show_sensitive=True)
    assert result["memory_tiers"] == list(record.memory_tiers)


def test_record_rejects_reserved_provenance_tag(store, record) -> None:
    import dataclasses

    tainted = dataclasses.replace(record, memory_attributions=("casa-source-v1.tag",))
    with pytest.raises(ValueError):
        store.record(tainted)
    # Rejected record must not land on disk at all.
    assert not (store._root / f"{record.correlation_id}.json").exists()


def test_record_is_atomic_and_mode_0600(store, record) -> None:
    store.record(record)
    path = store._root / f"{record.correlation_id}.json"
    assert path.exists()
    assert not path.with_suffix(".json.tmp").exists()
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_get_unknown_correlation_id_raises_keyerror(store) -> None:
    with pytest.raises(KeyError):
        store.get("cid-never-recorded")


def test_get_invalid_correlation_id_raises_valueerror(tmp_path) -> None:
    bad_store = ExplanationStore(tmp_path / "explanations")
    with pytest.raises(ValueError):
        bad_store.get("../etc/passwd")


def test_get_expired_record_raises_keyerror(record, tmp_path) -> None:
    from explanation_store import EXPLANATION_TTL_SECONDS

    clock = {"now": 1_000_000.0}
    ttl_store = ExplanationStore(tmp_path / "explanations", now=lambda: clock["now"])
    ttl_store.record(record)
    clock["now"] += EXPLANATION_TTL_SECONDS + 1
    with pytest.raises(KeyError):
        ttl_store.get(record.correlation_id)


def test_prune_caps_at_max_records(tmp_path) -> None:
    clock = {"now": 0.0}
    ttl_store = ExplanationStore(tmp_path / "explanations", now=lambda: clock["now"])
    for i in range(EXPLANATION_MAX_RECORDS + 5):
        clock["now"] += 1
        rec = ExplanationRecord(
            correlation_id=f"cid-{i:05d}", role_id="r", kind="resident",
            resolved_model="m", persona_ref=None, role_checksum="rc",
            binding_digest=None, dependency_digests=(), effective_config_digest=None,
            lifecycle_state=None, projection="text", static_prompt_digest="d",
            static_prompt_estimated_tokens=1, memory_tiers=(), memory_attributions=(),
            tool_calls=(), denials=(),
        )
        ttl_store.record(rec)
    remaining = list((tmp_path / "explanations").glob("*.json"))
    assert len(remaining) <= EXPLANATION_MAX_RECORDS
    # The oldest record must be the one pruned; the newest must survive.
    assert not (tmp_path / "explanations" / "cid-00000.json").exists()
    assert (tmp_path / "explanations" / f"cid-{EXPLANATION_MAX_RECORDS + 4:05d}.json").exists()


# ---------------------------------------------------------------------------
# specialist_status_payload (brief Step 1)
# ---------------------------------------------------------------------------


def _binding_record(**overrides):
    from personality_binding import BindingRecord

    fields = dict(
        stable_agent_id="specialist:finance", role_checksum="rc-finance",
        mode="component-default", persona_id="gary", persona_version="1",
        persona_checksum="pc-1", compiler_schema_version="v1",
        dependency_digests=("dep-a",), effective_config_digest="ecd-finance",
        binding_digest="bd-finance",
    )
    fields.update(overrides)
    return BindingRecord(**fields)


def _instance_tuple(**overrides):
    from personality_binding import InstanceTuple

    binding = overrides.pop("binding", None) or _binding_record()
    fields = dict(
        root="/config/specialists/finance", binding=binding,
        config_snapshot={}, config_digest=binding.effective_config_digest,
    )
    fields.update(overrides)
    return InstanceTuple(**fields)


@pytest.fixture
def runtime_with_specialist():
    from types import SimpleNamespace

    import specialist_registry as specialist_registry_mod
    from specialist_lifecycle import SpecialistInstance

    instance = SpecialistInstance(
        slug="finance", stable_agent_id="specialist:finance", state="active",
        active=_instance_tuple(), desired=None, last_activation_error=None,
    )

    class _FakeIndex:
        def get_instance(self, slug):
            return instance if slug == "finance" else None

    specialist_registry_mod.set_active_installed_index(_FakeIndex())
    return SimpleNamespace()


def test_specialist_status_reports_lifecycle_and_last_error(runtime_with_specialist) -> None:
    from personality_admin_handlers import specialist_status_payload

    payload = specialist_status_payload(runtime_with_specialist, slug="finance")
    assert payload["state"] in {
        "not_installed", "installed", "pending-configuration", "configured",
        "active", "error",
    }
    assert "active" in payload and "desired" in payload
    assert "last_activation_error" in payload
    assert payload["active"]["persona_id"] == "gary"


def test_specialist_status_not_installed_when_no_index() -> None:
    from personality_admin_handlers import specialist_status_payload

    import specialist_registry as specialist_registry_mod

    specialist_registry_mod.set_active_installed_index(None)
    payload = specialist_status_payload(object(), slug="ghost")
    assert payload == {"slug": "ghost", "state": "not_installed"}


# ---------------------------------------------------------------------------
# register_personality_admin_routes — route-level behavior
# ---------------------------------------------------------------------------


class _FakeRuntime:
    def __init__(self, *, explanation_store):
        self.persona_packs: dict = {}
        self.compiled_prompt_bundles: dict = {}
        self.role_slots: dict = {}
        self.bindings: dict = {}
        self.explanation_store = explanation_store


def _make_app(runtime) -> web.Application:
    from personality_admin_handlers import register_personality_admin_routes

    app = web.Application()
    register_personality_admin_routes(app, runtime=runtime)
    return app


def _persona_pack(**overrides):
    from persona_pack import PersonaManifest, PersonaPack

    fields = dict(
        persona_id="gary", version="1", trait_schema_version=1,
        identity={"name": "Gary"}, relationship_posture="concierge",
        archetype="butler", traits={"warmth": 5}, quirks=(),
        markdown="# Gary\nprose that must never leak via inspect/explain",
        examples=(), manifest=PersonaManifest(files=(), checksum="mc-1"),
        checksum="pc-1",
    )
    fields.update(overrides)
    return PersonaPack(**fields)


def _role_slot(**overrides):
    from role_slot import ResolvedModel, RoleSlot

    fields = dict(
        role_id="concierge", kind="resident", slot="concierge",
        mission="", resolved_model=ResolvedModel(
            source="image-default", effective="claude-sonnet",
            sdk_model="claude-sonnet", option=None,
        ),
        normalized={}, doctrine="", checksum="rc-concierge",
    )
    fields.update(overrides)
    return RoleSlot(**fields)


async def test_inspect_returns_traits_no_markdown_prose(tmp_path) -> None:
    store = ExplanationStore(tmp_path / "explanations")
    runtime = _FakeRuntime(explanation_store=store)
    runtime.persona_packs["gary@1"] = _persona_pack()
    app = _make_app(runtime)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/admin/personality/inspect", json={"persona": "gary@1"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["persona_id"] == "gary"
        assert body["traits"] == {"warmth": 5}
        assert "markdown" not in body
        assert "prose that must never leak" not in json.dumps(body)


async def test_inspect_reports_the_packs_own_sections(tmp_path) -> None:
    """#623 — the list was a literal ["Core", "Negative space"], not derived.

    Specified by Terra during the blind-design round; accepted by Sol. The
    pack below has five headings including a NESTED one and a REPEATED name at
    a different depth, so it distinguishes the three candidate behaviours:
      - the literal            -> ["Core", "Negative space"]
      - root_sections()        -> ["Core", "House rules"]   (drops the nested
                                   sections, which is the defect #623 reports)
      - sections()             -> all five, in document order   <- intended
    """
    store = ExplanationStore(tmp_path / "explanations")
    runtime = _FakeRuntime(explanation_store=store)
    runtime.persona_packs["gary@1"] = _persona_pack(
        markdown=(
            "# Core\n" + ("core body " * 40) + "\n"
            "## Negative space\nnever do this\n"
            "### Detail\nfiner point\n"
            "# House rules\nhouse body\n"
            "## Core\nnested core, same name at another depth\n"
        ),
    )
    app = _make_app(runtime)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/admin/personality/inspect", json={"persona": "gary@1"},
        )
        assert resp.status == 200
        body = await resp.json()
        # Assert the exact list AND the count: a literal would satisfy neither,
        # and root_sections() would satisfy neither.
        assert body["sections"] == [
            "Core", "Negative space", "Detail", "House rules", "Core",
        ]
        assert len(body["sections"]) == 5
        # The response still carries names only — never bodies.
        assert "never do this" not in json.dumps(body)
        assert "core body" not in json.dumps(body)


async def test_inspect_not_found_404(tmp_path) -> None:
    runtime = _FakeRuntime(explanation_store=ExplanationStore(tmp_path / "e"))
    app = _make_app(runtime)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/admin/personality/inspect", json={"persona": "nope@1"},
        )
        assert resp.status == 404


async def test_inspect_invalid_ref_400(tmp_path) -> None:
    runtime = _FakeRuntime(explanation_store=ExplanationStore(tmp_path / "e"))
    app = _make_app(runtime)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/admin/personality/inspect", json={})
        assert resp.status == 400


async def test_render_returns_digest_and_estimated_tokens(tmp_path) -> None:
    from prompt_compiler import CompiledProjection, CompiledPromptBundle

    projection = CompiledProjection(
        system_prompt="<compiled prompt>", digest="digest-abc", estimated_tokens=42,
    )
    bundle = CompiledPromptBundle(
        role_id="concierge", resolved_model="claude-sonnet",
        text=projection, voice=projection, restricted_webhook=projection,
        binding_digest="bd-concierge",
    )
    runtime = _FakeRuntime(explanation_store=ExplanationStore(tmp_path / "e"))
    runtime.compiled_prompt_bundles["concierge"] = bundle
    # GH #356: render now validates the requested ref against the active
    # binding, so the fake runtime must carry one (gary@1).
    runtime.bindings["concierge"] = _binding_record()
    app = _make_app(runtime)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/admin/personality/render",
            json={"persona": "gary@1", "role": "concierge", "projection": "text"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["digest"] == "digest-abc"
        assert body["estimated_tokens"] == 42


async def test_render_unknown_role_404(tmp_path) -> None:
    runtime = _FakeRuntime(explanation_store=ExplanationStore(tmp_path / "e"))
    app = _make_app(runtime)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/admin/personality/render",
            json={"persona": "x", "role": "ghost", "projection": "text"},
        )
        assert resp.status == 404


async def test_diff_returns_current_and_target(tmp_path) -> None:
    runtime = _FakeRuntime(explanation_store=ExplanationStore(tmp_path / "e"))
    runtime.role_slots["concierge"] = _role_slot()
    runtime.persona_packs["gary@2"] = _persona_pack(version="2", checksum="pc-2")
    runtime.bindings["concierge"] = _binding_record()
    app = _make_app(runtime)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/admin/personality/diff",
            json={"role": "concierge", "to": "gary@2"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["current_persona"] == "gary"
        assert body["target_persona"] == "gary"
        assert body["target_checksum"] == "pc-2"


async def test_specialist_status_route_returns_payload(tmp_path, runtime_with_specialist) -> None:
    runtime = _FakeRuntime(explanation_store=ExplanationStore(tmp_path / "e"))
    app = _make_app(runtime)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/admin/specialist/status", json={"slug": "finance"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["state"] == "active"


async def test_explain_default_strips_sensitive_and_no_reserved_tags(tmp_path, record) -> None:
    store = ExplanationStore(tmp_path / "explanations")
    store.record(record)
    runtime = _FakeRuntime(explanation_store=store)
    app = _make_app(runtime)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/admin/explain", json={"correlation_id": record.correlation_id},
        )
        assert resp.status == 200
        body = await resp.json()
        encoded = json.dumps(body)
        assert "system_prompt" not in body
        assert "memory_text" not in body
        assert "casa-source-" not in encoded
        assert "SENSITIVE" not in encoded


async def test_explain_show_sensitive_without_confirmed_400(tmp_path, record) -> None:
    store = ExplanationStore(tmp_path / "explanations")
    store.record(record)
    runtime = _FakeRuntime(explanation_store=store)
    app = _make_app(runtime)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/admin/explain",
            json={"correlation_id": record.correlation_id, "show_sensitive": True},
        )
        assert resp.status == 400


async def test_explain_show_sensitive_with_confirmed_returns_full(tmp_path, record) -> None:
    store = ExplanationStore(tmp_path / "explanations")
    store.record(record)
    runtime = _FakeRuntime(explanation_store=store)
    app = _make_app(runtime)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/admin/explain",
            json={
                "correlation_id": record.correlation_id,
                "show_sensitive": True, "confirmed": True,
            },
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["system_prompt"] == record.system_prompt


async def test_explain_default_omits_memory_tiers(tmp_path, record) -> None:
    """GH #202 at the route: the default (unconfirmed) explain response must not
    carry the memory sensitivity-tier tokens, while attribution labels remain."""
    store = ExplanationStore(tmp_path / "explanations")
    store.record(record)
    runtime = _FakeRuntime(explanation_store=store)
    app = _make_app(runtime)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/admin/explain", json={"correlation_id": record.correlation_id},
        )
        assert resp.status == 200
        body = await resp.json()
        assert "memory_tiers" not in body
        assert body["memory_attributions"] == list(record.memory_attributions)


async def test_explain_show_sensitive_returns_memory_tiers(tmp_path, record) -> None:
    store = ExplanationStore(tmp_path / "explanations")
    store.record(record)
    runtime = _FakeRuntime(explanation_store=store)
    app = _make_app(runtime)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/admin/explain",
            json={
                "correlation_id": record.correlation_id,
                "show_sensitive": True, "confirmed": True,
            },
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["memory_tiers"] == list(record.memory_tiers)


async def test_explain_unknown_correlation_id_404(tmp_path) -> None:
    runtime = _FakeRuntime(explanation_store=ExplanationStore(tmp_path / "e"))
    app = _make_app(runtime)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/admin/explain", json={"correlation_id": "cid-never-recorded"},
        )
        assert resp.status == 404


# ---------------------------------------------------------------------------
# GH #356: explain confirmation requires JSON booleans (not truthy coercion)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload_extra", [
    {"show_sensitive": True, "confirmed": "false"},
    {"show_sensitive": True, "confirmed": "no"},
    {"show_sensitive": True, "confirmed": [1]},
    {"show_sensitive": "true", "confirmed": True},
    {"show_sensitive": 1, "confirmed": True},
])
async def test_explain_nonboolean_gate_values_rejected_400(
    tmp_path, record, payload_extra,
) -> None:
    """GH #356: `bool()` coercion let any truthy value ("false", "no", a
    non-empty list) pass the documented `confirmed=true` gate and disclose
    the full system_prompt/memory_text. The gate now requires JSON booleans
    and refuses everything else with 400 — before touching the store."""
    store = ExplanationStore(tmp_path / "explanations")
    store.record(record)
    runtime = _FakeRuntime(explanation_store=store)
    app = _make_app(runtime)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/admin/explain",
            json={"correlation_id": record.correlation_id, **payload_extra},
        )
        assert resp.status == 400
        body = await resp.json()
        assert "SENSITIVE" not in json.dumps(body)


# ---------------------------------------------------------------------------
# GH #634: the explain gate's refusal names the offending field and the type
# ---------------------------------------------------------------------------


_SS_STR = "SHOW_SENSITIVE_STRING_SENTINEL"
_CF_STR = "CONFIRMED_STRING_SENTINEL"
_NUM = 314159265
_SS_ARR = ["SHOW_SENSITIVE_ARRAY_SENTINEL"]
_CF_ARR = ["CONFIRMED_ARRAY_SENTINEL"]
_SS_OBJ = {"marker": "SHOW_SENSITIVE_OBJECT_SENTINEL"}
_CF_OBJ = {"marker": "CONFIRMED_OBJECT_SENTINEL"}


@pytest.mark.parametrize(
    "payload_extra, field, json_type, sentinels",
    [
        # --- show_sensitive wrong, confirmed a real boolean -----------------
        ({"show_sensitive": _SS_STR, "confirmed": True},
         "show_sensitive", "string", [_SS_STR]),
        ({"show_sensitive": _NUM, "confirmed": True},
         "show_sensitive", "number", [str(_NUM)]),
        ({"show_sensitive": _SS_ARR, "confirmed": True},
         "show_sensitive", "array", _SS_ARR),
        ({"show_sensitive": _SS_OBJ, "confirmed": True},
         "show_sensitive", "object", [_SS_OBJ["marker"]]),
        ({"show_sensitive": None, "confirmed": True},
         "show_sensitive", "null", []),
        # --- confirmed wrong, show_sensitive a real boolean -----------------
        ({"show_sensitive": True, "confirmed": _CF_STR},
         "confirmed", "string", [_CF_STR]),
        ({"show_sensitive": True, "confirmed": _NUM},
         "confirmed", "number", [str(_NUM)]),
        ({"show_sensitive": True, "confirmed": _CF_ARR},
         "confirmed", "array", _CF_ARR),
        ({"show_sensitive": True, "confirmed": _CF_OBJ},
         "confirmed", "object", [_CF_OBJ["marker"]]),
        ({"show_sensitive": True, "confirmed": None},
         "confirmed", "null", []),
        # --- BOTH wrong: show_sensitive is the field named ------------------
        ({"show_sensitive": _SS_STR, "confirmed": _CF_OBJ},
         "show_sensitive", "string", [_SS_STR, _CF_OBJ["marker"]]),
    ],
)
async def test_explain_nonboolean_gate_values_report_safe_first_field_before_store_read(
    tmp_path, record, monkeypatch, payload_extra, field, json_type, sentinels,
) -> None:
    """GH #634: the #356 gate refused every non-boolean with one constant
    ``{"error": "invalid_args"}`` payload, so a caller on the internal socket
    could not tell which of the two fields was rejected or that a JSON boolean
    was expected. The refusal now carries a ``detail`` naming exactly one field
    and the JSON TYPE received — never the value — and still fires before the
    explanation store is read. When both fields are wrong, ``show_sensitive``
    is named (the gate's own source order)."""
    store = ExplanationStore(tmp_path / "explanations")
    store.record(record)
    store_get = Mock(side_effect=AssertionError(
        "ExplanationStore.get must not be reached by a refused request"))
    monkeypatch.setattr(store, "get", store_get)
    runtime = _FakeRuntime(explanation_store=store)
    app = _make_app(runtime)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/admin/explain",
            json={"correlation_id": record.correlation_id, **payload_extra},
        )
        assert resp.status == 400
        body = await resp.json()
        assert body == {
            "error": "invalid_args",
            "detail": f"{field} must be a JSON boolean; received {json_type}",
        }
    rendered = json.dumps(body)
    assert "SENSITIVE PERSONA PROSE" not in rendered
    for sentinel in sentinels:
        assert sentinel not in rendered
    store_get.assert_not_called()


async def test_explain_boolean_gate_still_serves_sensitive(tmp_path, record) -> None:
    store = ExplanationStore(tmp_path / "explanations")
    store.record(record)
    runtime = _FakeRuntime(explanation_store=store)
    app = _make_app(runtime)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/admin/explain",
            json={"correlation_id": record.correlation_id,
                  "show_sensitive": True, "confirmed": True},
        )
        assert resp.status == 200
        assert (await resp.json())["system_prompt"] == record.system_prompt


# ---------------------------------------------------------------------------
# GH #356: render validates the requested persona ref against the binding
# ---------------------------------------------------------------------------


def _bundle_for(role_id: str = "concierge"):
    from prompt_compiler import CompiledProjection, CompiledPromptBundle

    projection = CompiledProjection(
        system_prompt="<compiled prompt>", digest="digest-abc", estimated_tokens=42,
    )
    return CompiledPromptBundle(
        role_id=role_id, resolved_model="claude-sonnet",
        text=projection, voice=projection, restricted_webhook=projection,
        binding_digest="bd-concierge",
    )


@pytest.mark.parametrize("ref", ["gary", "gary@1"])
async def test_render_accepts_bare_id_and_full_ref_of_bound_persona(
    tmp_path, ref,
) -> None:
    runtime = _FakeRuntime(explanation_store=ExplanationStore(tmp_path / "e"))
    runtime.compiled_prompt_bundles["concierge"] = _bundle_for()
    runtime.bindings["concierge"] = _binding_record()  # gary@1
    app = _make_app(runtime)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/admin/personality/render",
            json={"persona": ref, "role": "concierge", "projection": "text"},
        )
        assert resp.status == 200
        assert (await resp.json())["digest"] == "digest-abc"


async def test_render_mismatched_ref_409_names_bound_persona(tmp_path) -> None:
    """GH #356: `casactl persona render <ref>` used to ignore <ref> entirely
    and could return a different persona's compiled prompt than requested."""
    runtime = _FakeRuntime(explanation_store=ExplanationStore(tmp_path / "e"))
    runtime.compiled_prompt_bundles["concierge"] = _bundle_for()
    runtime.bindings["concierge"] = _binding_record()  # gary@1
    app = _make_app(runtime)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/admin/personality/render",
            json={"persona": "olga@3", "role": "concierge", "projection": "text"},
        )
        assert resp.status == 409
        body = await resp.json()
        assert body["error"] == "persona_mismatch"
        assert body["bound_persona"] == "gary@1"
        assert "system_prompt" not in body


async def test_render_no_binding_409(tmp_path) -> None:
    runtime = _FakeRuntime(explanation_store=ExplanationStore(tmp_path / "e"))
    runtime.compiled_prompt_bundles["concierge"] = _bundle_for()
    app = _make_app(runtime)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/admin/personality/render",
            json={"persona": "gary@1", "role": "concierge", "projection": "text"},
        )
        assert resp.status == 409
        assert (await resp.json())["bound_persona"] is None


async def test_render_without_persona_renders_the_bound_persona(tmp_path) -> None:
    """v0.188.1 (main-red fix): `persona` is OPTIONAL — the ref check only
    applies when a ref is supplied. The tier2 e2e (and any caller asking
    "render whatever is bound") sends only {role, projection}; v0.187.0's
    required-field reading of the #356 fix broke that contract and the unit
    pin agreed with the code instead of the caller."""
    runtime = _FakeRuntime(explanation_store=ExplanationStore(tmp_path / "e"))
    runtime.compiled_prompt_bundles["concierge"] = _bundle_for()
    runtime.bindings["concierge"] = _binding_record()
    app = _make_app(runtime)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/admin/personality/render",
            json={"role": "concierge", "projection": "text"},
        )
        assert resp.status == 200
        assert (await resp.json())["digest"] == "digest-abc"


async def test_render_without_persona_needs_no_binding_record(tmp_path) -> None:
    """The ref-less form must keep working where it worked before v0.187.0 —
    including a role with a compiled bundle but no binding record visible to
    the admin runtime view."""
    runtime = _FakeRuntime(explanation_store=ExplanationStore(tmp_path / "e"))
    runtime.compiled_prompt_bundles["concierge"] = _bundle_for()
    app = _make_app(runtime)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/admin/personality/render",
            json={"role": "concierge", "projection": "text"},
        )
        assert resp.status == 200


async def test_render_empty_or_non_string_persona_400(tmp_path) -> None:
    runtime = _FakeRuntime(explanation_store=ExplanationStore(tmp_path / "e"))
    runtime.compiled_prompt_bundles["concierge"] = _bundle_for()
    runtime.bindings["concierge"] = _binding_record()
    app = _make_app(runtime)
    async with TestClient(TestServer(app)) as client:
        for payload in (
            {"persona": "", "role": "concierge", "projection": "text"},
            {"persona": 7, "role": "concierge", "projection": "text"},
            # Sol+Terra (v0.188.1 review): an EXPLICIT null is a present,
            # non-string value — body.get() must not conflate it with the
            # absent-key ref-less form.
            {"persona": None, "role": "concierge", "projection": "text"},
        ):
            resp = await client.post("/admin/personality/render", json=payload)
            assert resp.status == 400
            assert (await resp.json())["error"] == "invalid_persona_ref"


# ---------------------------------------------------------------------------
# GH #356: failed record() cleans its temp file; prune sweeps orphan *.tmp
# ---------------------------------------------------------------------------


def test_record_failure_leaves_no_temp_file(store, record, monkeypatch) -> None:
    """GH #356: a failure between staging and publish (chmod/replace) used to
    strand `<cid>.<uuid>.tmp` — full sensitive content — which the `*.json`
    prune glob never swept."""
    import explanation_store as es_mod

    def _boom(*a, **kw):
        raise OSError("disk says no")

    monkeypatch.setattr(es_mod.os, "replace", _boom)
    with pytest.raises(OSError):
        store.record(record)
    assert list(store._root.glob("*.tmp")) == []
    assert list(store._root.glob("*.json")) == []


def test_prune_sweeps_orphaned_tmp_files(store, record) -> None:
    store.record(record)
    orphan = store._root / "cid-dead.0123abcd.tmp"
    orphan.write_text("{\"system_prompt\": \"leftover sensitive\"}", encoding="utf-8")
    store.prune()
    assert not orphan.exists()
    # Published records are untouched by the tmp sweep.
    assert (store._root / f"{record.correlation_id}.json").exists()
