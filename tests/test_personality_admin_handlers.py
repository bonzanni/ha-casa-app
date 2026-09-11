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
import shutil
import stat
from pathlib import Path
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
#
# These arms drive the REGISTERED handler directly instead of going through
# ``TestClient(TestServer(app))``: the reviewer sandbox denies socket creation
# (``PermissionError: [Errno 1]`` at listener setup), so a loopback-bound arm
# is unrunnable for the acceptor. The route is still looked up through the real
# ``app.router`` after the real ``register_personality_admin_routes`` call, so
# the closure under test is the shipped one, and the response object is the
# real ``web.json_response`` the client would have received.


def _explain_handler(runtime):
    """The real ``/admin/explain`` POST handler, via the real router."""
    app = _make_app(runtime)
    matches = [r for r in app.router.routes()
               if r.method == "POST" and r.resource.canonical == "/admin/explain"]
    assert len(matches) == 1, f"expected one POST /admin/explain, got {matches!r}"
    return matches[0].handler


class _JsonRequest:
    """The only thing ``_explain`` reads off the request is ``await .json()``."""

    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


_SS_STR = "SHOW_SENSITIVE_STRING_SENTINEL"
_CF_STR = "CONFIRMED_STRING_SENTINEL"
_NUM = 314159265
_SS_ARR = ["SHOW_SENSITIVE_ARRAY_SENTINEL"]
_CF_ARR = ["CONFIRMED_ARRAY_SENTINEL"]
_SS_OBJ = {"marker": "SHOW_SENSITIVE_OBJECT_SENTINEL"}
_CF_OBJ = {"marker": "CONFIRMED_OBJECT_SENTINEL"}


@pytest.mark.parametrize("value, expected", [
    (None, "null"),
    # `isinstance(True, int)` is true, so the bool arm must precede the numeric
    # one or a JSON `true` would read as "number". `_explain` itself never
    # reaches the helper with a bool (a bool passes the gate), so this arm is
    # what keeps the mapping total for any future caller.
    (True, "boolean"),
    (False, "boolean"),
    (0, "number"),
    (314159265, "number"),
    (1.5, "number"),
    ("", "string"),
    ("true", "string"),
    ([], "array"),
    ([1], "array"),
    ({}, "object"),
    ({"a": 1}, "object"),
])
def test_json_type_name_maps_every_json_type_with_bool_before_number(value, expected):
    from personality_admin_handlers import _json_type_name

    assert _json_type_name(value) == expected


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
    handler = _explain_handler(_FakeRuntime(explanation_store=store))

    resp = await handler(_JsonRequest(
        {"correlation_id": record.correlation_id, **payload_extra}))

    assert resp.status == 400
    body = json.loads(resp.body)
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


# ---------------------------------------------------------------------------
# #673: the persona admin surface answers for an INSTALLED SPECIALIST, not only
# for residents. Driven through the REAL chain — commit_specialist_install →
# the real roles overlay → SpecialistRegistry.load with the real
# activate_binding_for_config → a real CasaRuntime → the real routes — because
# the defect is precisely that a hand-populated map cannot see: the four maps
# are derived from `role_configs` alone, and a specialist never enters it.
# ---------------------------------------------------------------------------


def _runtime_with_registry(specialist_registry, *, role_configs=None):
    """A real `CasaRuntime` carrying a real specialist registry.

    Every other collaborator is a stand-in: the derivation under test reads
    exactly two of these fields (`role_configs` and `specialist_registry`),
    and the admin routes read only the four derived maps.
    """
    from unittest.mock import MagicMock

    from runtime import CasaRuntime

    return CasaRuntime(
        agents={}, role_configs={} if role_configs is None else role_configs,
        specialist_registry=specialist_registry,
        executor_registry=MagicMock(), engagement_registry=MagicMock(),
        agent_registry=MagicMock(), trigger_registry=MagicMock(),
        mcp_registry=MagicMock(), session_registry=MagicMock(),
        channel_manager=MagicMock(), bus=MagicMock(),
        engagement_driver=MagicMock(), claude_code_driver=MagicMock(),
        policy_lib=MagicMock(),
        config_dir="/x", agents_dir="/x/agents",
        home_root="/x/home", defaults_root="/opt/casa",
    )


def _live_specialist_registry(tmp_path, monkeypatch):
    """A REAL registry holding one activated `mtg` specialist.

    Reuses `test_specialist_binding_rederive`'s real-chain helpers rather than
    re-implementing them: a real `commit_specialist_install`, a real
    `current_specialist_roles_dir` overlay, and a real `SpecialistRegistry.load`
    whose `activate_binding_for_config` is scoped at `tmp_path`.
    """
    from test_specialist_binding_rederive import (
        _install, _overlay, _registry_load, _scoped_activation,
    )

    specialists_root, agents_root, _ = _install(tmp_path, monkeypatch)
    _scoped_activation(monkeypatch, specialists_root)
    index, roles_dir = _overlay(specialists_root, agents_root)
    registry, counts = _registry_load(tmp_path, index, agents_root, roles_dir)
    # (installed, enabled, load failures) — the chain really produced one
    # activated specialist, not an empty registry that would pass vacuously.
    assert counts == (1, 1, 0), registry.load_failures()
    return registry


def _map_counts(runtime):
    return (len(runtime.role_slots), len(runtime.bindings),
            len(runtime.compiled_prompt_bundles), len(runtime.persona_packs))


async def test_activated_specialist_populates_personality_maps_and_admin_routes(
    tmp_path, monkeypatch,
) -> None:
    """#673: an installed, activated specialist is in all four maps under its
    `role_id`, and every persona route answers for it.

    RED at the base for the intended reason: `refresh_personality_maps`
    iterates `role_configs.values()` alone, which is empty here, while the
    registry holds one activated specialist — so the counts are (0, 0, 0, 0)
    and every route reports the persona absent.
    """
    registry = _live_specialist_registry(tmp_path, monkeypatch)
    cfg = registry.all_configs()["mtg"]
    role_id = cfg.role_id
    assert role_id == "specialist:mtg"
    ref = f"{cfg.persona_pack.persona_id}@{cfg.persona_pack.version}"

    runtime = _runtime_with_registry(registry)
    runtime.refresh_personality_maps()

    assert _map_counts(runtime) == (1, 1, 1, 1)
    # Identity, not equality: the maps must hand out the registry's own
    # objects, so a copy or a re-derivation would fail here.
    assert runtime.role_slots[role_id] is cfg.role_slot
    assert runtime.bindings[role_id] is cfg.binding
    assert runtime.compiled_prompt_bundles[role_id] is cfg.compiled_prompt_bundle
    assert runtime.persona_packs[ref] is cfg.persona_pack

    runtime.explanation_store = None
    app = _make_app(runtime)

    # The REGISTERED handlers, invoked in-process rather than over a loopback
    # `TestServer`: this file's other route tests open a socket, and the review
    # sandbox denies that with EPERM — an arm that cannot run where it is judged
    # delivers no evidence there. The status assertions are load-bearing in this
    # form: `web.json_response(..., status=404)` RETURNS a response object, so
    # decoding a payload is not on its own proof that the success path was taken.
    class _JSONRequest:
        def __init__(self, payload):
            self._payload = payload

        async def json(self):
            return self._payload

    def _handler(path):
        return next(
            route.handler
            for route in app.router.routes()
            if route.method == "POST" and route.resource.canonical == path
        )

    results = []

    async def _call(path, payload):
        response = await _handler(path)(_JSONRequest(payload))
        decoded = (response.status, json.loads(response.body))
        results.append(decoded)
        return decoded

    status, inspected = await _call(
        "/admin/personality/inspect", {"persona": ref})
    assert status == 200, inspected
    assert inspected["persona_id"] == cfg.persona_pack.persona_id
    assert inspected["checksum"] == cfg.persona_pack.checksum

    for projection in ("text", "voice", "restricted_webhook"):
        status, payload = await _call(
            "/admin/personality/render",
            {"persona": ref, "role": role_id, "projection": projection})
        assert status == 200, (projection, payload)
        assert payload["digest"] == getattr(
            cfg.compiled_prompt_bundle, projection).digest, projection

    # v0.188.1's ref-less form: `persona` absent means "render whatever is
    # bound", and it 404d on the bundle map before it ever read a ref.
    status, refless = await _call(
        "/admin/personality/render", {"role": role_id, "projection": "text"})
    assert status == 200, refless
    assert refless["digest"] == cfg.compiled_prompt_bundle.text.digest

    status, diffed = await _call(
        "/admin/personality/diff", {"role": role_id, "to": ref})
    assert status == 200, diffed
    assert diffed["current_persona"] == cfg.binding.persona_id
    assert diffed["target_checksum"] == cfg.persona_pack.checksum

    assert len(results) == 6


def _personality_cfg(role_id: str, *, persona: str, version: str = "1",
                     checksum: str = "c", activated: bool = True):
    """A narrow personality-carrying config stand-in, in the shape the
    derivation reads (`role_slot`, `persona_pack`, `binding`,
    `compiled_prompt_bundle`) — the same shape
    `test_reload.TestPersonalityMapRefresh` uses for residents."""
    from types import SimpleNamespace

    return SimpleNamespace(
        role_id=role_id,
        role_slot=SimpleNamespace(role_id=role_id),
        persona_pack=SimpleNamespace(
            persona_id=persona, version=version, checksum=checksum),
        binding=SimpleNamespace(
            persona_id=persona, persona_version=version) if activated else None,
        compiled_prompt_bundle=SimpleNamespace(role_id=role_id),
    )


class _Registry:
    """A registry stand-in whose `all_configs` returns exactly what it is given
    — including shapes a real registry never returns, which is the point: the
    derivation's two input guards must be killable independently."""

    def __init__(self, configs):
        self._configs = configs

    def all_configs(self):
        return self._configs


def test_narrow_registry_stand_ins_contribute_no_specialist_rows() -> None:
    """#673: the widened derivation must tolerate every registry stand-in the
    reload suites already seed, without a blanket `except`.

    A bare `MagicMock` alone does NOT distinguish the `callable(...)` guard
    from the `Mapping` guard — its `all_configs()` returns a `MagicMock`, which
    fails both. The LIST-returning registry is what kills the `Mapping` guard's
    mutant on its own.
    """
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    resident = _personality_cfg("resident:ellen", persona="p-ellen")
    specialist = _personality_cfg("specialist:mtg", persona="casa/judge")
    for label, registry in (
        ("none", None),
        ("no all_configs", SimpleNamespace()),
        ("all_configs not callable", SimpleNamespace(all_configs=object())),
        ("bare MagicMock", MagicMock()),
        ("all_configs returns a list", _Registry([specialist])),
        ("all_configs returns a string", _Registry("mtg")),
    ):
        runtime = _runtime_with_registry(
            registry, role_configs={"ellen": resident})
        runtime.refresh_personality_maps()
        assert _map_counts(runtime) == (1, 1, 1, 1), label
        assert "specialist:mtg" not in runtime.role_slots, label

    # ...and a registry that DOES return a mapping contributes its rows, so the
    # assertions above are about the guards and not about an inert code path.
    runtime = _runtime_with_registry(
        _Registry({"mtg": specialist}), role_configs={"ellen": resident})
    runtime.refresh_personality_maps()
    assert _map_counts(runtime) == (2, 2, 2, 2)


def test_specialist_without_an_activated_binding_yields_no_map_entry(
) -> None:
    """#673: `activate_binding_for_config` sets `persona_pack`, `binding` and
    `compiled_prompt_bundle` together; a pending-configuration or legacy
    specialist has all three `None` and must appear in NONE of the four maps —
    including `role_slots`, whose presence alone would flip `diff` from 404 to
    200 with a null current persona."""
    pending = _personality_cfg("specialist:pending", persona="x", activated=False)
    pending.persona_pack = None
    pending.compiled_prompt_bundle = None
    live = _personality_cfg("specialist:mtg", persona="casa/judge")

    runtime = _runtime_with_registry(
        _Registry({"pending": pending, "mtg": live}))
    runtime.refresh_personality_maps()

    assert _map_counts(runtime) == (1, 1, 1, 1)
    assert "specialist:pending" not in runtime.role_slots
    assert "specialist:mtg" in runtime.role_slots


def test_persona_pack_collision_resolves_to_the_resident_then_the_greatest_slug(
) -> None:
    """#673: `persona_packs` is keyed `id@version` across ALL agents, so a
    specialist's bundled default pack can collide with a resident's while
    differing in checksum — and `inspect` returns the checksum, so the winner
    is observable.

    The rule: residents are derived LAST and win, which makes the widening
    strictly additive (no `inspect` answer that was already correct changes);
    among specialists the derivation walks ascending slug order, so the
    greatest slug wins. Both halves are asserted with the mapping supplied in
    REVERSE order, so a rule that merely inherited dict order would fail.
    """
    resident = _personality_cfg("resident:ellen", persona="shared", checksum="resident")
    alpha = _personality_cfg("specialist:alpha", persona="shared", checksum="alpha")
    zulu = _personality_cfg("specialist:zulu", persona="shared", checksum="zulu")

    specialists_only = _runtime_with_registry(
        _Registry({"zulu": zulu, "alpha": alpha}))
    specialists_only.refresh_personality_maps()
    assert _map_counts(specialists_only) == (2, 2, 2, 1)
    assert specialists_only.persona_packs["shared@1"].checksum == "zulu"

    with_resident = _runtime_with_registry(
        _Registry({"zulu": zulu, "alpha": alpha}),
        role_configs={"ellen": resident})
    with_resident.refresh_personality_maps()
    assert _map_counts(with_resident) == (3, 3, 3, 1)
    assert with_resident.persona_packs["shared@1"].checksum == "resident"
    # The role-keyed views stay per-agent: the collision touches persona_packs
    # only, so each specialist keeps its own bundle and binding.
    assert with_resident.compiled_prompt_bundles["specialist:alpha"] is alpha.compiled_prompt_bundle
    assert with_resident.compiled_prompt_bundles["specialist:zulu"] is zulu.compiled_prompt_bundle


# ---------------------------------------------------------------------------
# #929 (INV-SPEC-015): `casactl specialist status` names a pending slug's
# resume inputs.
#
# A first install that lands pending-configuration retains its receipt, its
# staging tree and the `pending-receipt.json` marker, and the SAME
# (inspection, receipt) pair re-commits to active — but nothing a LATER
# engagement can consult carried the five values that re-commit takes, and
# every carrier pointed at a re-inspect that refuses the occupied slug. The
# payload now names them, each member null rather than raising when the
# marker, the receipt, the staged tree or the tuple root no longer yields it.
# ---------------------------------------------------------------------------

_RESUME_KEYS = {"receipt_id", "staged_dir", "component_id", "version", "root_digest"}


@pytest.fixture
def restore_installed_index():
    """The installed index is process-global. Publish into it, then put back
    whatever the rest of the session had — a leaked index makes a later
    module's status test depend on file order."""
    import specialist_registry as specialist_registry_mod

    before = specialist_registry_mod._active_index
    yield specialist_registry_mod
    specialist_registry_mod.set_active_installed_index(before)


def _install(tmp_path, monkeypatch, *, home, slug="mtg", version=None,
             required_config=("region",), config=None):
    """Drive the REAL install path to one generation of `slug`.

    Not the existing retention fixture: that one commits without a receipt, so
    the journaled `_record_pending_receipt` write never happens and the marker
    this payload reads would not exist. `required_config` with nothing supplied
    is what lands the commit in pending-configuration; supplying the values
    activates instead.
    """
    import json as _json
    from types import SimpleNamespace

    import specialist_bundle_journal
    import specialist_install
    import specialist_receipt
    from specialist_fixtures import write_minimal_component
    from specialist_install_consent import SpecialistInstallAckStore, install_consent_identity
    from specialist_registry import InstalledSpecialistIndex

    comp, mpath = write_minimal_component(home, slug=slug,
                                          required_config=list(required_config))
    if version is not None:
        manifest = _json.loads(mpath.read_text(encoding="utf-8"))
        manifest["version"] = version
        mpath.write_text(_json.dumps(manifest), encoding="utf-8")

    def _fetch(repo, ref, subdir, dest, *, expected_revision=None):
        import shutil
        # Bytes, never modes: the candidate gate runs on a read-only tree.
        shutil.copytree(comp / subdir if subdir else comp, dest,
                        copy_function=shutil.copyfile)
        return "a" * 40

    monkeypatch.setattr(specialist_install, "resolve_and_fetch", _fetch)
    receipts_dir = tmp_path / "receipts"
    specialists_dir = tmp_path / "specialists"
    idx = InstalledSpecialistIndex(specialists_dir=str(specialists_dir))
    idx.load()
    upgrade = slug in idx.installed_slugs()
    inspection = specialist_install.inspect_specialist_repo(
        "org/repo", "main", staging_root=home / "staging",
        installed_index=idx, receipts_dir=receipts_dir,
        specialists_dir=specialists_dir,
        **({"mode": "upgrade", "target_slug": slug} if upgrade else {}))
    receipt = specialist_receipt.load(inspection.receipt_id, receipts_dir=receipts_dir)
    assert receipt is not None

    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    identity = install_consent_identity(
        component_id=inspection.component_id, version=inspection.version,
        root_digest=inspection.root_digest, slug=inspection.slug,
        receipt_digest=inspection.receipt_digest)
    acks.record(identity=identity, component_id=inspection.component_id,
                version=inspection.version, component_checksum=inspection.root_digest,
                slug=inspection.slug, receipt_digest=inspection.receipt_digest)

    kw = dict(inspection=inspection, receipt=receipt, config=dict(config or {}),
              secret_names_provided=frozenset(), acks=acks,
              specialists_dir=specialists_dir,
              agents_specialists_dir=tmp_path / "agents",
              registry_path=tmp_path / "registry.json",
              plugin_store_root=tmp_path / "store",
              ops_dir=tmp_path / "ops")
    if upgrade:
        instance, txn = specialist_install.upgrade_specialist(slug=slug, **kw)
    else:
        instance, txn = specialist_install.commit_specialist_install(**kw)
    specialist_bundle_journal.complete(txn.journal_path)
    return SimpleNamespace(
        slug=slug, state=instance.state, inspection=inspection, receipt=receipt,
        acks=acks, kw=kw,
        specialists_dir=specialists_dir, receipts_dir=receipts_dir,
        marker=specialists_dir / slug / "pending-receipt.json",
        expected={"receipt_id": receipt.receipt_id,
                  "staged_dir": str(inspection.staged_dir),
                  "component_id": inspection.component_id,
                  "version": inspection.version,
                  "root_digest": inspection.root_digest})


def _pending_install(tmp_path, monkeypatch, *, slug="mtg"):
    ctx = _install(tmp_path, monkeypatch, home=tmp_path / "a", slug=slug)
    assert ctx.state == "pending-configuration"
    return ctx


def _pending_upgrade(tmp_path, monkeypatch, *, slug="mtg"):
    """An ACTIVE generation A, then an upgrade to B that lands pending: the
    active tuple stays in place, so the reloaded index reports state="active"
    while a desired candidate — B's — is what a resume would re-commit."""
    active = _install(tmp_path, monkeypatch, home=tmp_path / "a", slug=slug,
                      required_config=(), config={})
    assert active.state == "active"
    pending = _install(tmp_path, monkeypatch, home=tmp_path / "b", slug=slug,
                       version="0.2.0")
    assert pending.state == "pending-configuration"
    return active, pending


def _publish(ctx, registry_mod, monkeypatch):
    """Load a fresh real index off the pending tree, publish it, and point the
    receipts seam at this test's receipts directory."""
    import personality_admin_handlers
    from specialist_registry import InstalledSpecialistIndex

    index = InstalledSpecialistIndex(specialists_dir=str(ctx.specialists_dir))
    index.load()
    registry_mod.set_active_installed_index(index)
    monkeypatch.setattr(personality_admin_handlers, "SPECIALIST_RECEIPTS_DIR",
                        ctx.receipts_dir, raising=False)
    return index


def test_specialist_status_names_a_pending_slugs_resume_inputs(
        tmp_path, monkeypatch, restore_installed_index) -> None:
    """The five values a later engagement needs, read off a REAL pending tree:
    the marker's receipt id, the receipt's staged path, and the component id /
    version / root digest the pending tuple's own root string records."""
    from personality_admin_handlers import specialist_status_payload

    ctx = _pending_install(tmp_path, monkeypatch)
    index = _publish(ctx, restore_installed_index, monkeypatch)

    # The state the disclosure is about, asserted by count before it is read.
    instance = index.get_instance("mtg")
    assert len(index.installed_slugs()) == 1
    assert (instance.active, instance.desired is not None) == (None, True)
    assert instance.state == "pending-configuration"
    assert json.loads(ctx.marker.read_text())["receipt_id"] == ctx.receipt.receipt_id
    assert len(list(ctx.receipts_dir.glob("*.json"))) == 1
    assert Path(ctx.inspection.staged_dir).is_dir()

    payload = specialist_status_payload(object(), slug="mtg")

    assert len(payload) == 7
    assert set(payload["pending_commit"]) == _RESUME_KEYS
    assert payload["pending_commit"] == ctx.expected
    # Every pre-existing key survives.
    assert payload["slug"] == "mtg" and payload["state"] == "pending-configuration"
    assert payload["stable_agent_id"] == "specialist:mtg"
    assert payload["active"] is None and payload["desired"] is not None
    assert "last_activation_error" in payload


def test_specialist_status_names_the_desired_upgrades_resume_inputs(
        tmp_path, monkeypatch, restore_installed_index) -> None:
    """A pending UPGRADE leaves the active tuple in place, so the reloaded
    index calls the slug `active` — the disclosure follows the desired
    candidate, not the state string, and names B's inputs, not A's."""
    from personality_admin_handlers import specialist_status_payload

    active, pending = _pending_upgrade(tmp_path, monkeypatch)
    index = _publish(pending, restore_installed_index, monkeypatch)

    instance = index.get_instance("mtg")
    assert (instance.active is not None, instance.desired is not None) == (True, True)
    assert instance.active.root != instance.desired.root
    assert instance.state == "active"

    payload = specialist_status_payload(object(), slug="mtg")

    assert payload["pending_commit"] == pending.expected
    assert payload["pending_commit"]["version"] == "0.2.0"
    assert payload["pending_commit"]["root_digest"] != active.inspection.root_digest


def _write_json(path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _break_root(ctx) -> None:
    """Leave the desired tuple LOADABLE but give it a root string that the
    checked parser refuses — an unloadable tuple would remove `instance.desired`
    and exercise a different predicate entirely."""
    import yaml as _yaml

    path = ctx.specialists_dir / ctx.slug / "desired.yaml"
    raw = _yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["root"] = "not-a-component-root"
    path.write_text(_yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")


def _rewrite_receipt(ctx, **fields) -> None:
    path = ctx.receipts_dir / f"{ctx.receipt.receipt_id}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.update(fields)
    path.write_text(json.dumps(raw), encoding="utf-8")


_MARKER_AND_RECEIPT = {"receipt_id", "staged_dir"}
_FROM_ROOT = {"component_id", "version", "root_digest"}

_ARMS = [
    ("marker deleted", lambda c: c.marker.unlink(), _FROM_ROOT),
    ("marker is not JSON", lambda c: _write_json(c.marker, "{not json"), _FROM_ROOT),
    ("marker is a JSON list", lambda c: _write_json(c.marker, "[]"), _FROM_ROOT),
    ("marker has no receipt_id", lambda c: _write_json(c.marker, "{}"), _FROM_ROOT),
    ("marker receipt_id is not a string",
     lambda c: _write_json(c.marker, '{"receipt_id": 7}'), _FROM_ROOT),
    ("receipt deleted",
     lambda c: (c.receipts_dir / f"{c.receipt.receipt_id}.json").unlink(),
     _FROM_ROOT | {"receipt_id"}),
    ("receipt tampered",
     lambda c: _rewrite_receipt(c, component_repo="somewhere/else"),
     _FROM_ROOT | {"receipt_id"}),
    ("receipt staged path is null",
     lambda c: _rewrite_receipt(c, component_staged_path=None),
     _FROM_ROOT | {"receipt_id"}),
    ("staged tree removed",
     lambda c: shutil.rmtree(c.inspection.staged_dir), _FROM_ROOT | {"receipt_id"}),
    ("staged path is a regular file",
     lambda c: (shutil.rmtree(c.inspection.staged_dir),
                Path(c.inspection.staged_dir).write_text("x", encoding="utf-8")),
     _FROM_ROOT | {"receipt_id"}),
    ("desired root does not parse", _break_root, _MARKER_AND_RECEIPT),
    ("marker gone and root does not parse",
     lambda c: (c.marker.unlink(), _break_root(c)), set()),
]


@pytest.mark.parametrize("label,mutate,populated", _ARMS, ids=[a[0] for a in _ARMS])
def test_specialist_status_reports_a_pending_slug_it_cannot_fully_resume(
        tmp_path, monkeypatch, restore_installed_index, label, mutate, populated) -> None:
    """Every arm still answers, with five members and nulls where the value is
    gone — a legacy pending slug predating the marker included. A raise here
    would take the whole status route down for an operator trying to diagnose
    exactly this."""
    from personality_admin_handlers import specialist_status_payload

    ctx = _pending_install(tmp_path, monkeypatch)
    mutate(ctx)
    _publish(ctx, restore_installed_index, monkeypatch)

    payload = specialist_status_payload(object(), slug="mtg")

    pending_commit = payload["pending_commit"]
    assert set(pending_commit) == _RESUME_KEYS
    assert {k for k, v in pending_commit.items() if v is not None} == populated
    assert {k: pending_commit[k] for k in populated} == {
        k: ctx.expected[k] for k in populated}


@pytest.mark.asyncio
async def test_the_disclosed_inputs_are_what_completes_the_pending_install(
        tmp_path, monkeypatch, restore_installed_index) -> None:
    """Usability, not plausibility: the five strings the payload disclosed —
    and nothing held over from the fixture — are handed straight back to the
    PUBLIC re-commit tool, which reaches `active`. Five convincing-looking
    strings that the tool refuses would satisfy every other assertion here.

    Only the tool's process-global LOCATIONS are redirected at this test's
    tree (the receipts directory, the ack ledger, the lifecycle core's
    `/config` roots, the bundle sequencer); the real `commit_specialist_install`
    runs, and the arguments are exactly `slug` plus the disclosed mapping.
    """
    from test_tools_specialist_install import _stub_bundle_sequencer

    import specialist_install
    import sys

    import specialist_install_consent
    import specialist_receipt
    import tools as tools_mod
    from personality_admin_handlers import specialist_status_payload
    from specialist_registry import InstalledSpecialistIndex
    from tools import specialist_install_commit

    ctx = _pending_install(tmp_path, monkeypatch)
    _publish(ctx, restore_installed_index, monkeypatch)
    disclosed = specialist_status_payload(object(), slug="mtg")["pending_commit"]

    real_commit = specialist_install.commit_specialist_install
    real_load = specialist_receipt.load
    core_calls: list[dict] = []
    pruned: list[str] = []

    def _commit(**kw):
        core_calls.append(kw)
        return real_commit(**dict(kw, specialists_dir=ctx.kw["specialists_dir"],
                                  agents_specialists_dir=ctx.kw["agents_specialists_dir"],
                                  registry_path=ctx.kw["registry_path"],
                                  plugin_store_root=ctx.kw["plugin_store_root"],
                                  ops_dir=ctx.kw["ops_dir"]))

    monkeypatch.setattr(specialist_install, "commit_specialist_install", _commit)
    monkeypatch.setattr(specialist_receipt, "load",
                        lambda rid, *a, **k: real_load(rid, receipts_dir=ctx.receipts_dir))
    monkeypatch.setattr(specialist_install_consent, "SpecialistInstallAckStore",
                        lambda *a, **k: ctx.acks)
    monkeypatch.setattr(tools_mod, "_prune_bundle_receipt", pruned.append)
    monkeypatch.setattr(specialist_install, "reclaim_staging_tree", lambda d: None)
    _stub_bundle_sequencer(monkeypatch)

    result = await specialist_install_commit.handler(
        {"slug": "mtg", **disclosed, "config": {"region": "EU"}})
    payload = json.loads(result["content"][0]["text"])

    assert len(core_calls) == 1
    assert payload["ok"] is True and payload["state"] == "active"
    assert len(_RESUME_KEYS & payload.keys()) == 0
    assert pruned == [disclosed["receipt_id"]]
    assert not ctx.marker.exists()

    reloaded = InstalledSpecialistIndex(specialists_dir=str(ctx.specialists_dir))
    reloaded.load()
    after = reloaded.get_instance("mtg")
    assert (after.active is not None, after.desired) == (True, None)


def test_specialist_status_discloses_no_resume_for_an_instance_with_no_candidate(
        tmp_path, monkeypatch, restore_installed_index) -> None:
    """An active-only instance has nothing to resume, and a slug that is not
    installed keeps its two-key payload."""
    from personality_admin_handlers import specialist_status_payload

    ctx = _install(tmp_path, monkeypatch, home=tmp_path / "a", slug="mtg",
                   required_config=(), config={})
    assert ctx.state == "active"
    index = _publish(ctx, restore_installed_index, monkeypatch)
    assert index.get_instance("mtg").desired is None

    payload = specialist_status_payload(object(), slug="mtg")
    assert len(_RESUME_KEYS & payload.keys()) == 0
    assert "pending_commit" not in payload

    assert specialist_status_payload(object(), slug="ghost") == {
        "slug": "ghost", "state": "not_installed"}


@pytest.mark.asyncio
async def test_the_status_route_builds_its_payload_off_the_event_loop(
        tmp_path, monkeypatch, restore_installed_index) -> None:
    """The payload now reads the marker, the receipt sidecar and the staged
    directory — filesystem work that must not run on the event loop, exactly
    as `/admin/explain` already offloads its locked store read. Asserted by
    thread identity, and with no listening socket: the route handler is
    invoked directly with a stand-in request."""
    import threading

    import personality_admin_handlers

    ctx = _pending_install(tmp_path, monkeypatch)
    _publish(ctx, restore_installed_index, monkeypatch)

    loop_thread = threading.get_ident()
    threads: list[int] = []
    real = personality_admin_handlers.specialist_status_payload

    def _record(runtime, *, slug):
        threads.append(threading.get_ident())
        return real(runtime, slug=slug)

    monkeypatch.setattr(personality_admin_handlers, "specialist_status_payload", _record)

    app = web.Application()
    personality_admin_handlers.register_personality_admin_routes(
        app, runtime=_FakeRuntime(explanation_store=None))
    handler = next(r.handler for r in app.router.routes()
                   if r.resource.canonical == "/admin/specialist/status")

    class _Request:
        async def json(self):
            return {"slug": "mtg"}

    response = await handler(_Request())

    assert len(threads) == 1
    assert threads.count(loop_thread) == 0
    assert json.loads(response.text)["pending_commit"] == ctx.expected


@pytest.mark.parametrize("staged_path", [None, 7, "", "   "], ids=["null", "number", "empty", "blank"])
def test_a_receipt_whose_staged_path_is_not_a_usable_string_discloses_none(
        tmp_path, monkeypatch, restore_installed_index, staged_path) -> None:
    """`component_staged_path` is not covered by the receipt digest, so a
    truncated or hand-edited sidecar loads as valid with anything there.
    `Path("")` is `Path(".")` — a directory that always exists — so an empty
    value would be disclosed as a resumable `"."` and send the next engagement
    to commit against the process working directory (terra, diff review r2).
    A blank string is kept alongside it: it is a real path that will not be a
    directory, so it must null for the ordinary reason.
    """
    from personality_admin_handlers import specialist_status_payload

    ctx = _pending_install(tmp_path, monkeypatch)
    _rewrite_receipt(ctx, component_staged_path=staged_path)
    _publish(ctx, restore_installed_index, monkeypatch)

    pending_commit = specialist_status_payload(object(), slug="mtg")["pending_commit"]

    assert pending_commit["staged_dir"] is None
    assert pending_commit["receipt_id"] == ctx.receipt.receipt_id
    assert {k for k, v in pending_commit.items() if v is not None} == {
        "receipt_id", "component_id", "version", "root_digest"}


@pytest.mark.asyncio
async def test_the_disclosed_inputs_never_pair_a_stale_root_with_a_newer_receipt(
        tmp_path, monkeypatch, restore_installed_index) -> None:
    """Two inspected roots, one slug: candidate A is what the PUBLISHED INDEX
    holds, candidate B is what the tree holds — B's commit wrote B's marker,
    and the index is a snapshot nothing republished (a pending candidate is
    never loaded, so no reload follows it).

    Reading the root from that snapshot and the marker off disk pairs them
    across a write: the payload named A's component id, version and root digest
    beside B's receipt id and staged directory. That mapping is not merely
    inconsistent — handed straight to the PUBLIC re-commit tool it is refused
    `checksum_changed`, which is exactly the route this disclosure exists to
    keep an engagement out of. Asserted as the OUTCOME: the five values the
    payload discloses complete the install.
    """
    from test_tools_specialist_install import _stub_bundle_sequencer

    import specialist_install
    import sys

    import specialist_install_consent
    import specialist_receipt
    import tools as tools_mod
    from personality_admin_handlers import specialist_status_payload
    from tools import specialist_install_commit

    first = _pending_install(tmp_path, monkeypatch)
    index = _publish(first, restore_installed_index, monkeypatch)

    # A leaves the tree; a second inspected root takes the freed slug and lands
    # pending too. Both steps are the real library calls; neither republishes.
    specialist_install.uninstall_specialist(
        slug="mtg", specialists_dir=first.kw["specialists_dir"],
        agents_specialists_dir=first.kw["agents_specialists_dir"],
        registry_path=first.kw["registry_path"], ops_dir=first.kw["ops_dir"])
    second = _install(tmp_path, monkeypatch, home=tmp_path / "b", slug="mtg",
                      version="0.2.0")
    assert second.state == "pending-configuration"
    assert second.specialists_dir == first.specialists_dir
    assert second.expected["root_digest"] != first.expected["root_digest"]
    assert second.expected["receipt_id"] != first.expected["receipt_id"]

    # The premise, asserted before it is read: the index still holds A.
    stale = index.get_instance("mtg")
    assert stale.desired is not None
    assert stale.desired.root.endswith(first.expected["root_digest"])
    assert not stale.desired.root.endswith(second.expected["root_digest"])
    assert json.loads(second.marker.read_text())["receipt_id"] == second.receipt.receipt_id

    disclosed = specialist_status_payload(object(), slug="mtg")["pending_commit"]

    real_commit = specialist_install.commit_specialist_install
    real_load = specialist_receipt.load
    core_calls: list[dict] = []

    def _commit(**kw):
        core_calls.append(kw)
        return real_commit(**dict(kw, specialists_dir=second.kw["specialists_dir"],
                                  agents_specialists_dir=second.kw["agents_specialists_dir"],
                                  registry_path=second.kw["registry_path"],
                                  plugin_store_root=second.kw["plugin_store_root"],
                                  ops_dir=second.kw["ops_dir"]))

    monkeypatch.setattr(specialist_install, "commit_specialist_install", _commit)
    monkeypatch.setattr(specialist_receipt, "load",
                        lambda rid, *a, **k: real_load(rid, receipts_dir=second.receipts_dir))
    monkeypatch.setattr(specialist_install_consent, "SpecialistInstallAckStore",
                        lambda *a, **k: second.acks)
    monkeypatch.setattr(tools_mod, "_prune_bundle_receipt", lambda rid: None)
    monkeypatch.setattr(specialist_install, "reclaim_staging_tree", lambda d: None)
    _stub_bundle_sequencer(monkeypatch)

    result = await specialist_install_commit.handler(
        {"slug": "mtg", **disclosed, "config": {"region": "EU"}})
    payload = json.loads(result["content"][0]["text"])

    assert payload.get("kind") is None       # pre-fix: "checksum_changed"
    assert payload["ok"] is True and payload["state"] == "active"
    assert len(core_calls) == 1
    # And the five are the tree's candidate throughout, not a mixture of two.
    assert disclosed == second.expected


def test_the_marker_and_the_candidate_root_are_read_as_one_locked_snapshot(
        tmp_path, monkeypatch, restore_installed_index) -> None:
    """The narrow form of the same pairing, against the real writer: a pending
    stage writes `pending-receipt.json` and THEN `desired.yaml`, both inside
    MATERIALIZE_LOCK (`specialist_install.py`). A reader that does not take
    that lock can land between the two writes and pair the incoming
    candidate's receipt with the outgoing candidate's root.

    Driven by a second pending upgrade paused inside its own `stage_desired`,
    with the marker already written: the unlocked read in the middle of this
    test SHOWS the torn tree, and the payload built while it is torn must
    still be one candidate's five values.
    """
    import threading

    import personality_binding
    import yaml as _yaml
    from personality_admin_handlers import specialist_status_payload

    active = _install(tmp_path, monkeypatch, home=tmp_path / "a", slug="mtg",
                      required_config=(), config={})
    assert active.state == "active"
    first = _install(tmp_path, monkeypatch, home=tmp_path / "b", slug="mtg",
                     version="0.2.0")
    assert first.state == "pending-configuration"
    _publish(first, restore_installed_index, monkeypatch)

    reached, released = threading.Event(), threading.Event()
    real_stage = personality_binding.InstanceDir.stage_desired

    def _pausing_stage(self, tuple_):
        reached.set()
        released.wait(30)
        return real_stage(self, tuple_)

    monkeypatch.setattr(personality_binding.InstanceDir, "stage_desired", _pausing_stage)

    done: dict[str, object] = {}

    def _second_upgrade():
        try:
            done["ctx"] = _install(tmp_path, monkeypatch, home=tmp_path / "c",
                                   slug="mtg", version="0.3.0")
        except BaseException as exc:  # noqa: BLE001 — re-raised on the main thread
            done["error"] = exc
        finally:
            reached.set()

    writer = threading.Thread(target=_second_upgrade, name="pending-stage")
    writer.start()
    assert reached.wait(60)
    assert "error" not in done

    # The tree AS THE ROUTE WOULD FIND IT, read without the lock on purpose:
    # the marker has moved to the incoming candidate, the tuple has not.
    torn_receipt = json.loads(first.marker.read_text(encoding="utf-8"))["receipt_id"]
    torn_root = _yaml.safe_load(
        (first.specialists_dir / "mtg" / "desired.yaml").read_text(encoding="utf-8"))["root"]
    assert torn_receipt != first.receipt.receipt_id
    assert torn_root.endswith(first.expected["root_digest"])

    timer = threading.Timer(0.5, released.set)
    timer.start()
    try:
        disclosed = specialist_status_payload(object(), slug="mtg")["pending_commit"]
    finally:
        timer.cancel()
        released.set()
        writer.join(60)

    assert done.get("error") is None
    second = done["ctx"]
    assert not writer.is_alive()
    assert second.state == "pending-configuration"
    assert torn_receipt == second.receipt.receipt_id
    assert disclosed == second.expected


# ---------------------------------------------------------------------------
# #929, candidate review r2 (astra): the PUBLISHED INDEX is not a source of
# truth for pending-candidate state — presence included.
#
# Round 1 took the five VALUES off the index because it is a snapshot nothing
# republishes for a pending candidate. The same snapshot was still left
# deciding WHETHER there is a candidate at all, and that is the same defect:
# a commit that lands pending-configuration reloads no agent (a pending
# candidate is deliberately not loadable) and only a reload republishes the
# index, so the index is blind to exactly the case this route exists for. The
# mechanism is cut rather than patched a third time: presence and values both
# come from one locked snapshot of the tree, and where the loaded view then
# disagrees the payload says so instead of contradicting itself.
# ---------------------------------------------------------------------------


async def _resume_with_disclosed(ctx, disclosed, monkeypatch, *, upgrade=False):
    """Hand the five disclosed strings straight to the PUBLIC re-commit tool
    and return `(core_calls, payload)`.

    Only the tool's process-global LOCATIONS are redirected at this test's tree
    (the receipts directory, the ack ledger, the lifecycle core's `/config`
    roots, the bundle sequencer); the real `commit_specialist_install` /
    `upgrade_specialist` runs, and the arguments are exactly `slug`, the
    disclosed mapping and the configuration the operator supplies.
    """
    from test_tools_specialist_install import _stub_bundle_sequencer

    import specialist_install
    import sys

    import specialist_install_consent
    import specialist_receipt
    import tools as tools_mod
    from tools import specialist_install_commit, specialist_upgrade

    name = "upgrade_specialist" if upgrade else "commit_specialist_install"
    real_core = getattr(specialist_install, name)
    real_load = specialist_receipt.load
    core_calls: list[dict] = []
    roots = {k: ctx.kw[k] for k in ("specialists_dir", "agents_specialists_dir",
                                    "registry_path", "plugin_store_root", "ops_dir")}

    def _core(**kw):
        core_calls.append(kw)
        return real_core(**dict(kw, **roots))

    monkeypatch.setattr(specialist_install, name, _core)
    monkeypatch.setattr(specialist_receipt, "load",
                        lambda rid, *a, **k: real_load(rid, receipts_dir=ctx.receipts_dir))
    monkeypatch.setattr(specialist_install_consent, "SpecialistInstallAckStore",
                        lambda *a, **k: ctx.acks)
    monkeypatch.setattr(tools_mod, "_prune_bundle_receipt", lambda rid: None)
    monkeypatch.setattr(specialist_install, "reclaim_staging_tree", lambda d: None)
    _stub_bundle_sequencer(monkeypatch)

    tool = specialist_upgrade if upgrade else specialist_install_commit
    result = await tool.handler(
        {"slug": ctx.slug, **disclosed, "config": {"region": "EU"}})
    return core_calls, json.loads(result["content"][0]["text"])


@pytest.mark.asyncio
async def test_a_pending_install_the_index_never_saw_still_names_its_resume_inputs(
        tmp_path, monkeypatch, restore_installed_index) -> None:
    """The ordinary first install that lands pending-configuration, in the
    order a running process actually sees it: the index was published BEFORE
    the install and no reload followed it, because a pending candidate is not
    loadable and only a reload republishes.

    The index therefore holds no instance for the slug, and the route answered
    `{"slug": ..., "state": "not_installed"}` with zero resume inputs while the
    tree held all five — the recipe sends a LATER engagement here for exactly
    those values, so the route prevented the recovery it exists to enable
    (astra, candidate review r2). Asserted as the OUTCOME: the five it now
    discloses are accepted by the public re-commit tool and reach `active`.
    """
    import personality_admin_handlers
    from personality_admin_handlers import specialist_status_payload
    from specialist_registry import InstalledSpecialistIndex

    specialists_dir = tmp_path / "specialists"
    index = InstalledSpecialistIndex(specialists_dir=str(specialists_dir))
    index.load()
    restore_installed_index.set_active_installed_index(index)
    assert len(index.installed_slugs()) == 0

    ctx = _pending_install(tmp_path, monkeypatch)
    monkeypatch.setattr(personality_admin_handlers, "SPECIALIST_RECEIPTS_DIR",
                        ctx.receipts_dir, raising=False)

    # The premise, asserted before it is read: one tree, the install in it,
    # and an index that still knows nothing about the slug.
    assert ctx.specialists_dir == specialists_dir
    assert len(index.installed_slugs()) == 0
    assert index.get_instance("mtg") is None
    assert (specialists_dir / "mtg" / "desired.yaml").is_file()
    assert json.loads(ctx.marker.read_text())["receipt_id"] == ctx.receipt.receipt_id
    assert len(list(ctx.receipts_dir.glob("*.json"))) == 1

    payload = specialist_status_payload(object(), slug="mtg")

    assert set(payload["pending_commit"]) == _RESUME_KEYS   # pre-fix: KeyError
    assert payload["pending_commit"] == ctx.expected
    # The loaded view is untouched AND labelled, so the payload cannot be read
    # as one contradictory claim about the tree.
    assert payload["state"] == "not_installed"
    assert payload["state_is_stale"] is True

    core_calls, result = await _resume_with_disclosed(
        ctx, payload["pending_commit"], monkeypatch)

    assert result.get("kind") is None
    assert result["ok"] is True and result["state"] == "active"
    assert len(core_calls) == 1
    assert not ctx.marker.exists()


@pytest.mark.asyncio
async def test_a_pending_upgrade_the_index_predates_still_names_its_resume_inputs(
        tmp_path, monkeypatch, restore_installed_index) -> None:
    """The same mechanism one step on: the index holds the slug's ACTIVE
    generation, published before the upgrade staged a candidate beside it, so
    its instance has `desired = None`. Presence taken from that instance
    withheld the disclosure exactly as the empty index did — the tree is what
    is asked, and the disclosed five re-commit through the public upgrade tool.
    """
    from personality_admin_handlers import specialist_status_payload

    active = _install(tmp_path, monkeypatch, home=tmp_path / "a", slug="mtg",
                      required_config=(), config={})
    assert active.state == "active"
    index = _publish(active, restore_installed_index, monkeypatch)
    pending = _install(tmp_path, monkeypatch, home=tmp_path / "b", slug="mtg",
                       version="0.2.0")
    assert pending.state == "pending-configuration"

    # The premise: the index's instance predates the staged candidate.
    instance = index.get_instance("mtg")
    assert instance.desired is None and instance.state == "active"
    assert instance.active.root.endswith(active.expected["root_digest"])
    assert json.loads(pending.marker.read_text())["receipt_id"] == pending.receipt.receipt_id

    payload = specialist_status_payload(object(), slug="mtg")

    assert set(payload["pending_commit"]) == _RESUME_KEYS   # pre-fix: KeyError
    assert payload["pending_commit"] == pending.expected
    assert payload["pending_commit"]["version"] == "0.2.0"
    assert payload["state"] == "active" and payload["desired"] is None
    assert payload["state_is_stale"] is True

    core_calls, result = await _resume_with_disclosed(
        pending, payload["pending_commit"], monkeypatch, upgrade=True)

    assert result.get("kind") is None
    assert result["ok"] is True and result["state"] == "active"
    assert len(core_calls) == 1
    assert not pending.marker.exists()


def test_a_candidate_gone_from_the_tree_is_not_disclosed_from_a_stale_index(
        tmp_path, monkeypatch, restore_installed_index) -> None:
    """The absence rule is the tree's too, in the other direction: the index
    still holds the pending instance, the tree no longer holds the candidate.

    Pre-fix the index's `desired` alone put a five-member `pending_commit` in
    the payload with every value null, describing a candidate that is not
    there; the tree decides, so the key is absent and the loaded view carrying
    a `desired` it no longer has is marked stale.
    """
    import specialist_install
    from personality_admin_handlers import specialist_status_payload

    ctx = _pending_install(tmp_path, monkeypatch)
    index = _publish(ctx, restore_installed_index, monkeypatch)
    specialist_install.uninstall_specialist(
        slug="mtg", specialists_dir=ctx.kw["specialists_dir"],
        agents_specialists_dir=ctx.kw["agents_specialists_dir"],
        registry_path=ctx.kw["registry_path"], ops_dir=ctx.kw["ops_dir"])

    assert index.get_instance("mtg").desired is not None
    assert not (ctx.specialists_dir / "mtg" / "desired.yaml").exists()

    payload = specialist_status_payload(object(), slug="mtg")

    assert "pending_commit" not in payload          # pre-fix: five null members
    assert payload["desired"] is not None
    assert payload["state_is_stale"] is True
    assert len(payload) == 7


def test_an_index_that_names_no_tree_discloses_nothing_about_a_candidate(
        tmp_path, monkeypatch, restore_installed_index) -> None:
    """WHERE the tree is remains the one thing taken from the index. A
    publisher that names none leaves the route unable to read the tree at all,
    and it then discloses no candidate AND no staleness — "cannot tell" is not
    "they agree" — even with a real pending candidate sitting in this tree.
    """
    import personality_admin_handlers
    from personality_admin_handlers import specialist_status_payload
    from specialist_lifecycle import SpecialistInstance

    ctx = _pending_install(tmp_path, monkeypatch)
    monkeypatch.setattr(personality_admin_handlers, "SPECIALIST_RECEIPTS_DIR",
                        ctx.receipts_dir, raising=False)

    class _NoTreeIndex:
        def get_instance(self, slug):
            return SpecialistInstance(
                slug=slug, stable_agent_id=f"specialist:{slug}", state="active",
                active=_instance_tuple(), desired=None, last_activation_error=None)

    restore_installed_index.set_active_installed_index(_NoTreeIndex())
    assert restore_installed_index.live_specialists_dir() is None
    assert (ctx.specialists_dir / "mtg" / "desired.yaml").is_file()

    payload = specialist_status_payload(object(), slug="mtg")

    assert "pending_commit" not in payload
    assert "state_is_stale" not in payload
    assert len(payload) == 6


def test_a_loaded_candidate_that_is_not_the_trees_candidate_is_marked_stale(
        tmp_path, monkeypatch, restore_installed_index) -> None:
    """The residual round 1 left behind, now named in the payload: BOTH views
    hold a candidate and they are different ones, so `desired` describes A
    while `pending_commit` — correctly, since round 1 — names B's five.

    A reader handed those two together had no way to tell which is the tree.
    `state_is_stale` says it, on the same rule as the other direction: the
    loaded candidate is compared with the tree's by presence AND root.
    """
    import specialist_install
    from personality_admin_handlers import specialist_status_payload

    first = _pending_install(tmp_path, monkeypatch)
    index = _publish(first, restore_installed_index, monkeypatch)
    specialist_install.uninstall_specialist(
        slug="mtg", specialists_dir=first.kw["specialists_dir"],
        agents_specialists_dir=first.kw["agents_specialists_dir"],
        registry_path=first.kw["registry_path"], ops_dir=first.kw["ops_dir"])
    second = _install(tmp_path, monkeypatch, home=tmp_path / "b", slug="mtg",
                      version="0.2.0")
    assert second.state == "pending-configuration"

    # The premise: two candidates, one slug, neither view empty.
    stale = index.get_instance("mtg")
    assert stale.desired is not None
    assert stale.desired.root.endswith(first.expected["root_digest"])
    assert second.expected["root_digest"] != first.expected["root_digest"]

    payload = specialist_status_payload(object(), slug="mtg")

    assert payload["desired"]["root"].endswith(first.expected["root_digest"])
    assert payload["pending_commit"] == second.expected
    assert payload["state_is_stale"] is True        # pre-fix: absent entirely


def test_a_slug_that_is_not_a_plain_tree_name_reads_no_tree(
        tmp_path, monkeypatch, restore_installed_index) -> None:
    """Taking presence off the index made the tree read reachable for ANY slug
    string — before, a slug the index did not hold never became a path. So the
    string is fenced where it becomes one: only a single plain directory name
    names a candidate.

    Asserted against a real candidate reached by traversal: unfenced, this
    discloses `mtg`'s five values under a slug that is not `mtg`.
    """
    from personality_admin_handlers import specialist_status_payload

    ctx = _pending_install(tmp_path, monkeypatch)
    _publish(ctx, restore_installed_index, monkeypatch)

    traversed = "../specialists/mtg"
    assert (ctx.specialists_dir / traversed).resolve() == (ctx.specialists_dir / "mtg")
    assert (ctx.specialists_dir / traversed / "desired.yaml").is_file()

    payload = specialist_status_payload(object(), slug=traversed)

    assert payload == {"slug": traversed, "state": "not_installed"}


# --- #929 red case (attempt 3, specified by astra) -------------------------
#
# The disclosure's guarantee is that the values it names are a set the tool
# that consumes them ADMITS. For a pending UPGRADE the active tuple is still
# in place, so `commit_specialist_install` refuses `concurrent_mutation`
# (`specialist_install.py:241-246` at the attempt's base) while
# `upgrade_specialist` activates the replacement and retains the prior. A
# payload that names five values and not the tool that takes them therefore
# sends a later engagement to a route that refuses — which is the one outcome
# the disclosure exists to prevent.

_ADMITTING_TOOLS = ("specialist_install_commit", "specialist_upgrade")


def _real_lifecycle_counter(monkeypatch, name, ctx):
    """Count calls to a REAL lifecycle function, injecting this test's
    directories — the tool layer resolves them from `/config` defaults it does
    not thread. Everything the function itself does is unstubbed."""
    import specialist_install

    real = getattr(specialist_install, name)
    calls: list[dict] = []

    def _wrapper(**kw):
        calls.append(kw)
        merged = dict(kw)
        merged.update({k: v for k, v in ctx.kw.items()
                       if k not in ("inspection", "receipt", "config",
                                    "secret_names_provided", "acks")})
        return real(**merged)

    monkeypatch.setattr(specialist_install, name, _wrapper)
    return calls


@pytest.mark.asyncio
async def test_verified_pending_upgrade_names_and_uses_its_admitting_tool(
        tmp_path, monkeypatch, restore_installed_index) -> None:
    """A pending upgrade's disclosed resume set names the tool that admits it,
    and that tool — selected from the payload, never from the fixture —
    activates the candidate."""
    import sys

    import specialist_install_consent
    import specialist_receipt
    import tools as tools_mod
    from personality_admin_handlers import specialist_status_payload
    from specialist_bundle_journal import recovery_debt
    from specialist_registry import InstalledSpecialistIndex

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_tools_specialist_install import _payload, _stub_bundle_sequencer

    active, pending = _pending_upgrade(tmp_path, monkeypatch)
    index = _publish(pending, restore_installed_index, monkeypatch)

    # 2. The state from disk, by count, before anything is read.
    instance = index.get_instance("mtg")
    assert (instance.active is not None, instance.desired is not None) == (True, True)
    assert instance.desired.root != instance.active.root
    # Two receipts: the fixture drives the LIBRARY, so generation A's receipt
    # was never pruned (only the tool layer prunes). B's is the one the marker
    # names and the one a resume needs.
    assert len(list(pending.receipts_dir.glob("*.json"))) == 2
    assert json.loads(pending.marker.read_text())["receipt_id"] == pending.receipt.receipt_id
    assert Path(pending.inspection.staged_dir).is_dir()
    assert len(list(recovery_debt(ops_dir=tmp_path / "ops"))) == 0

    # 3. Five resume members AND exactly one admitting-tool designation.
    payload = specialist_status_payload(object(), slug="mtg")
    disclosed = payload.get("pending_commit", {})
    assert sum(disclosed.get(k) is not None for k in _RESUME_KEYS) == 5
    assert sum(disclosed.get("tool") == name for name in _ADMITTING_TOOLS) == 1
    assert payload.get("pending_commit_check", {}).get("state") == "verified"

    # 4. The REAL public handler, selected by the disclosed name alone.
    real_loader = specialist_receipt.load
    monkeypatch.setattr(specialist_receipt, "load",
                        lambda rid, receipts_dir=None: real_loader(
                            rid, receipts_dir=pending.receipts_dir))
    monkeypatch.setattr(specialist_install_consent, "SpecialistInstallAckStore",
                        lambda *a, **k: pending.acks)
    monkeypatch.setattr(tools_mod, "_prune_bundle_receipt", lambda *a, **k: None)
    _stub_bundle_sequencer(monkeypatch)
    commits = _real_lifecycle_counter(monkeypatch, "commit_specialist_install", pending)
    upgrades = _real_lifecycle_counter(monkeypatch, "upgrade_specialist", pending)

    handler = getattr(tools_mod, disclosed["tool"]).handler
    result = _payload(await handler({
        "slug": "mtg",
        "component_id": disclosed["component_id"],
        "version": disclosed["version"],
        "root_digest": disclosed["root_digest"],
        "staged_dir": disclosed["staged_dir"],
        "receipt_id": disclosed["receipt_id"],
        "config": {"region": "EU"},
    }))

    # 5. Counts, then the disk.
    assert (len(upgrades), len(commits)) == (1, 0)
    assert (result["ok"], result["state"]) == (True, "active")
    after = InstalledSpecialistIndex(specialists_dir=str(pending.specialists_dir))
    after.load()
    landed = after.get_instance("mtg")
    assert landed.active.root == instance.desired.root
    assert landed.desired is None
    assert not pending.marker.exists()
