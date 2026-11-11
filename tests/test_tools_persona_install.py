"""Task N1d Step 6 (task-n1d-brief requirement 6): tool-level tests for the
configurator MCP tools persona_install_commit/persona_apply (tools.py),
mirroring tests/test_tools_specialist_install.py's patterns: monkeypatch
network/disk-touching pieces only, never touch the real /data or /config
paths."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


@pytest.mark.asyncio
async def test_persona_install_commit_rejects_a_changed_checksum(monkeypatch, tmp_path) -> None:
    """(a) staged bytes not matching the caller-supplied args -> ok:False
    checksum_changed, WITHOUT ever calling commit_persona_install."""
    from test_persona_install import _write_persona_repo
    import persona_install
    from tools import persona_install_commit

    staged = tmp_path / "staged"
    _write_persona_repo(staged)

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("commit_persona_install must never be called on a checksum mismatch")

    monkeypatch.setattr(persona_install, "commit_persona_install", _must_not_be_called)

    result = await persona_install_commit.handler({
        "persona_id": "casa/newton", "version": "0.1.0",
        "checksum": "sha256:" + "f" * 64,  # deliberately wrong
        "staged_dir": str(staged),
    })

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["kind"] == "checksum_changed"


@pytest.mark.asyncio
async def test_persona_install_commit_rejects_without_a_recorded_consent_ack(monkeypatch, tmp_path) -> None:
    """(b) an unrecorded consent -> ok:False consent_missing, never touching
    the real /data acks store."""
    from test_persona_install import _write_persona_repo
    from persona_pack import load_persona_pack
    import persona_install
    from persona_install import PersonaInstallAckStore
    from tools import persona_install_commit

    staged = tmp_path / "staged"
    _write_persona_repo(staged)
    pack = load_persona_pack(staged / "pack", staged / "manifest.json")

    # The tool constructs its ack store via a bare `PersonaInstallAckStore()`
    # call (production default path /data/persona_install_acks.json) — never
    # write to that real path from a test. The tool's local `from
    # persona_install import ... PersonaInstallAckStore` re-reads the module
    # attribute at call time, so patching it here is sufficient — redirect
    # the no-arg construction to a tmp_path file (mirrors
    # test_tools_specialist_install.py's _TmpAckStore pattern).
    tmp_acks_path = tmp_path / "acks.json"

    class _TmpAckStore(PersonaInstallAckStore):
        def __init__(self, path=None):  # noqa: ARG002 — tool always calls with no args
            super().__init__(path=tmp_acks_path)

    monkeypatch.setattr(persona_install, "PersonaInstallAckStore", _TmpAckStore)

    result = await persona_install_commit.handler({
        "persona_id": pack.persona_id, "version": pack.version, "checksum": pack.checksum,
        "staged_dir": str(staged),
    })

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["kind"] == "consent_missing"
    assert not tmp_acks_path.exists()


@pytest.mark.asyncio
async def test_persona_install_inspect_surfaces_a_structured_failure(monkeypatch) -> None:
    import persona_install
    from specialist_install import SpecialistInstallError
    from tools import persona_install_inspect

    def _boom(*args, **kwargs):
        raise SpecialistInstallError("fetch_failed", "simulated fetch failure")

    # Mirrors test_specialist_install_inspect_surfaces_a_structured_failure:
    # patch the WHOLE inspect function (not the inner resolve_and_fetch
    # primitive) — inspect_persona_repo's own staging_root.mkdir(...) runs
    # BEFORE resolve_and_fetch and defaults to /config/personas/.staging,
    # which this sandbox can't create (no /config here at all).
    monkeypatch.setattr(persona_install, "inspect_persona_repo", _boom)

    result = await persona_install_inspect.handler({"repo": "owner/repo", "ref": "main"})

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["kind"] == "fetch_failed"


@pytest.mark.asyncio
async def test_persona_apply_on_a_non_installed_specialist_target_reports_not_installed(
    monkeypatch,
) -> None:
    """(c) persona_apply on a non-installed specialist target -> ok:False
    not_installed. The tool loads the persona from the (hard-coded, matching
    every other install tool's production-default style) /config/personas
    tree BEFORE branching on target kind — bypass that with a fake pack
    (persona_pack.load_persona_pack is the module attribute the tool's local
    import re-reads at call time) so the specialist branch's
    InstalledSpecialistIndex (which naturally finds nothing under this
    sandbox's nonexistent /config/specialists) is what actually answers."""
    import persona_pack
    from tools import persona_apply

    class _FakePack:
        persona_id = "casa/newton"
        version = "0.1.0"

    monkeypatch.setattr(persona_pack, "load_persona_pack", lambda *a, **k: _FakePack())

    result = await persona_apply.handler({
        "target_role_id": "specialist:definitely-not-installed",
        "persona_id": "casa/newton", "persona_version": "0.1.0",
    })

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["kind"] == "not_installed"
    assert payload["slug"] == "definitely-not-installed"


def _plant_pack_dirs(root, persona_id: str, version: str) -> None:
    """Create the pack/ + manifest.json layout _resolve_local_persona probes
    (content is irrelevant — load_persona_pack is monkeypatched)."""
    base = root / persona_id / version
    (base / "pack").mkdir(parents=True)
    (base / "manifest.json").write_text("{}", encoding="utf-8")


def test_resolve_local_persona_refuses_traversal_ref(monkeypatch, tmp_path) -> None:
    """#323: a traversal-bearing persona_ref (../../..) escaped the approved
    roots and loaded an arbitrary on-disk pack. The segments are now validated
    with the same F1 patterns every other persona path join uses."""
    import persona_pack
    import tools

    monkeypatch.setenv("CASA_CONFIG_DIR", str(tmp_path / "config"))

    class _FakePack:
        persona_id = "evil/x"
        version = "1.0.0"

    # Plant a loadable-looking pack OUTSIDE the approved roots, reachable
    # only via traversal from <config>/personas.
    _plant_pack_dirs(tmp_path / "evil-root", "x", "1.0.0")
    monkeypatch.setattr(persona_pack, "load_persona_pack", lambda *a, **k: _FakePack())

    with pytest.raises(ValueError, match="invalid persona ref"):
        tools._resolve_local_persona("../../evil-root/x@1.0.0")


def test_resolve_local_persona_refuses_identity_mismatch(monkeypatch, tmp_path) -> None:
    """#323: the loaded pack must declare the identity the ref names — a pack
    parked at the wrong <id>/<version> directory never resolves."""
    import persona_pack
    import tools

    monkeypatch.setenv("CASA_CONFIG_DIR", str(tmp_path))

    class _WrongPack:
        persona_id = "casa/other"
        version = "9.9.9"

    _plant_pack_dirs(tmp_path / "personas", "casa/newton", "0.1.0")
    monkeypatch.setattr(persona_pack, "load_persona_pack", lambda *a, **k: _WrongPack())

    with pytest.raises(ValueError, match="declares"):
        tools._resolve_local_persona("casa/newton@0.1.0")


def test_resolve_local_persona_honors_casa_config_dir(monkeypatch, tmp_path) -> None:
    """#323: an installed pack under a custom $CASA_CONFIG_DIR/personas root
    must resolve — pre-fix the roots were hard-coded to /config/personas and
    the tool reported the pack unavailable."""
    import persona_pack
    import tools

    monkeypatch.setenv("CASA_CONFIG_DIR", str(tmp_path))

    class _Pack:
        persona_id = "casa/newton"
        version = "0.1.0"

    _plant_pack_dirs(tmp_path / "personas", "casa/newton", "0.1.0")
    monkeypatch.setattr(persona_pack, "load_persona_pack", lambda *a, **k: _Pack())

    pack = tools._resolve_local_persona("casa/newton@0.1.0")
    assert pack.persona_id == "casa/newton"


@pytest.mark.asyncio
async def test_persona_apply_resident_branch_honors_casa_bindings_dir(
        monkeypatch, tmp_path) -> None:
    """#323: persona_apply's resident branch hard-coded /config/bindings while
    boot reads CASA_BINDINGS_DIR — with the override set, the tool reported
    success + restart_required but the restarted resident read a different
    directory and kept the prior persona. Both sides must resolve the same
    seam (agent_loader._resident_bindings_root)."""
    import persona_install
    import persona_pack
    from tools import persona_apply

    monkeypatch.setenv("CASA_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CASA_BINDINGS_DIR", str(tmp_path / "bindings-root"))

    class _FakePack:
        persona_id = "casa/newton"
        version = "0.1.0"

    _plant_pack_dirs(tmp_path / "cfg" / "personas", "casa/newton", "0.1.0")
    monkeypatch.setattr(persona_pack, "load_persona_pack", lambda *a, **k: _FakePack())

    seen = {}

    def _capture_override(*, target_role_id, persona, role, instance_dir_root,
                          candidate_validator):
        seen["instance_dir_root"] = instance_dir_root

        class _Committed:
            class binding:
                binding_digest = "sha256:" + "0" * 64
        return _Committed()

    monkeypatch.setattr(persona_install, "apply_persona_override", _capture_override)

    result = await persona_apply.handler({
        "target_role_id": "resident:assistant",
        "persona_id": "casa/newton", "persona_version": "0.1.0",
    })
    payload = _payload(result)
    assert payload["ok"] is True
    assert seen["instance_dir_root"] == tmp_path / "bindings-root" / "resident-assistant"


@pytest.mark.asyncio
async def test_persona_apply_invalid_target_kind_reports_invalid_target(monkeypatch) -> None:
    """(d) persona_apply invalid target kind -> invalid_target."""
    import persona_pack
    from tools import persona_apply

    class _FakePack:
        persona_id = "casa/newton"
        version = "0.1.0"

    monkeypatch.setattr(persona_pack, "load_persona_pack", lambda *a, **k: _FakePack())

    result = await persona_apply.handler({
        "target_role_id": "bogus:foo",
        "persona_id": "casa/newton", "persona_version": "0.1.0",
    })

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["kind"] == "invalid_target"


@pytest.mark.asyncio
async def test_persona_install_commit_returns_version_content_conflict_kind(
    monkeypatch, tmp_path,
) -> None:
    """(c) Fix-round-1 CRITICAL regression, tool level: committing the SAME
    persona_id@version a second time with DIFFERENT, freshly-approved
    content must surface through persona_install_commit as ok:False
    kind:"version_content_conflict" — never an unstructured exception, and
    never a silent ok:True carrying the FIRST install's stale bytes."""
    from test_persona_install import _write_persona_repo
    from persona_pack import load_persona_pack
    import persona_install
    from persona_install import PersonaInstallAckStore
    from tools import persona_install_commit

    tmp_acks_path = tmp_path / "acks.json"

    class _TmpAckStore(PersonaInstallAckStore):
        def __init__(self, path=None):  # noqa: ARG002 — tool always calls with no args
            super().__init__(path=tmp_acks_path)

    monkeypatch.setattr(persona_install, "PersonaInstallAckStore", _TmpAckStore)

    # Same seam as the ack-store redirect above: the tool's `commit_persona_
    # install(...)` call never passes personas_root, so it defaults to the
    # real /config/personas — redirect it to tmp_path while still running
    # the REAL commit_persona_install logic (the thing under test), not a
    # stand-in that would hide a regression.
    real_commit = persona_install.commit_persona_install
    personas_root = tmp_path / "personas"

    def _commit_with_tmp_root(*, inspection, acks):
        return real_commit(inspection=inspection, acks=acks, personas_root=personas_root)

    monkeypatch.setattr(persona_install, "commit_persona_install", _commit_with_tmp_root)

    async def _ack_and_commit_via_tool(staged: Path, pack) -> dict:
        acks = _TmpAckStore()
        identity = persona_install.persona_install_consent_identity(
            persona_id=pack.persona_id, version=pack.version, checksum=pack.checksum)
        acks.record(identity=identity, persona_id=pack.persona_id, version=pack.version,
                     checksum=pack.checksum)
        result = await persona_install_commit.handler({
            "persona_id": pack.persona_id, "version": pack.version, "checksum": pack.checksum,
            "staged_dir": str(staged),
        })
        return _payload(result)

    staged1 = tmp_path / "staged1"
    _write_persona_repo(staged1)
    pack1 = load_persona_pack(staged1 / "pack", staged1 / "manifest.json")
    first = await _ack_and_commit_via_tool(staged1, pack1)
    assert first["ok"] is True

    staged2 = tmp_path / "staged2"
    _write_persona_repo(staged2, negative_space="Always double-checks the units.")
    pack2 = load_persona_pack(staged2 / "pack", staged2 / "manifest.json")
    assert pack2.checksum != pack1.checksum  # same persona_id@version, genuinely different content

    second = await _ack_and_commit_via_tool(staged2, pack2)
    assert second["ok"] is False
    assert second["kind"] == "version_content_conflict"

    # dest bytes are still the FIRST, approved install's — never clobbered.
    dest = personas_root / pack1.persona_id / pack1.version
    reloaded = load_persona_pack(dest / "pack", dest / "manifest.json")
    assert reloaded.checksum == pack1.checksum


@pytest.mark.asyncio
async def test_persona_apply_on_a_pending_configuration_specialist_reports_no_active_tuple(
    monkeypatch, tmp_path,
) -> None:
    """Fix-round-1 IMPORTANT regression: installed_component_role_dirs()
    legitimately resolves a pending-configuration specialist (desired-only,
    active=None — a real state per specialist_registry.py's own docstring),
    so persona_apply proceeds into apply_persona_override, whose specialist
    branch raises SpecialistInstallError("no_active_tuple", ...). Before
    this fix, persona_apply only caught ValueError, so that exception
    escaped unstructured instead of the tool's {ok, kind} contract."""
    from test_persona_install import _write_specialist_component
    import persona_pack
    import specialist_registry
    from tools import persona_apply

    slug = "pending-n1d"
    component_root = _write_specialist_component(tmp_path / "component", slug=slug)

    class _FakePack:
        persona_id = "casa/newton"
        version = "0.1.0"

    class _FakeIndex:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def load(self) -> None:
            pass

        def installed_component_role_dirs(self) -> dict:
            # Mirrors the real method's "active-or-desired" fallback for a
            # pending-configuration slug: the role artifact resolves even
            # though no active tuple exists yet at instance_dir_root
            # (Path("/config/specialists")/slug, which this sandbox has no
            # real directory for — so InstanceDir(...).active() naturally
            # returns None, exactly like a genuine pending-configuration
            # specialist whose desired.yaml was staged but never committed).
            return {slug: component_root}

    monkeypatch.setattr(specialist_registry, "InstalledSpecialistIndex", _FakeIndex)
    monkeypatch.setattr(persona_pack, "load_persona_pack", lambda *a, **k: _FakePack())

    result = await persona_apply.handler({
        "target_role_id": f"specialist:{slug}",
        "persona_id": "casa/newton", "persona_version": "0.1.0",
    })

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["kind"] == "no_active_tuple"


# ---------------------------------------------------------------------------
# Round-5b (Sol P1): persona_install_inspect — same ledger-precedence +
# verified-keyboard-post contract as specialist_install_inspect (see
# tests/test_tools_specialist_install.py for the specialist variant).
# ---------------------------------------------------------------------------

import asyncio
from types import SimpleNamespace


def _fake_persona_inspection(tmp_path):
    return SimpleNamespace(
        persona_id="warm-helper", version="1.0.0",
        checksum="sha256:" + "d" * 64, display_name="Warm Helper",
        staged_dir=tmp_path / "staged", resolved_ref="main",
    )


def _telegram_manager(channel):
    """A channel manager whose ``"telegram"`` is the transport with
    *channel* as Casa's endpoint (bot 111), the way ``casa_core`` registers
    it; one with no Telegram for ``None``."""
    if channel is None:
        return SimpleNamespace(get=lambda name: None)
    from telegram_channel_double import make_transport
    channel.default_agent = "assistant"
    channel.has_bot_id = True
    channel.bot_id = 111
    channel.on_identity = None
    return make_transport(channel)


def _wire_persona_inspect(monkeypatch, tmp_path, *, channel=None):
    import persona_install
    from persona_install import PersonaInstallAckStore
    import tools as tools_mod

    fake = _fake_persona_inspection(tmp_path)
    monkeypatch.setattr(
        persona_install, "inspect_persona_repo", lambda *a, **k: fake)
    tmp_acks = tmp_path / "persona_acks.json"

    class _TmpAckStore(PersonaInstallAckStore):
        def __init__(self, path=None):  # noqa: ARG002 — tool calls with no args
            super().__init__(path=tmp_acks)

    monkeypatch.setattr(persona_install, "PersonaInstallAckStore", _TmpAckStore)
    monkeypatch.setattr(
        tools_mod, "_channel_manager", _telegram_manager(channel))
    return fake, _TmpAckStore


class _Handle:
    def __init__(self, refused=None, settled="posted", hang=False):
        self.refused = refused
        self._settled = settled
        self._hang = hang

    async def settled_post(self):
        if self._hang:
            await asyncio.Event().wait()  # cancelled by the tool's wait_for bound
        return self._settled


@pytest.mark.asyncio
async def test_inspect_ok_payload_reports_the_resolved_ref(monkeypatch, tmp_path) -> None:
    """INV-PERS-020: the configurator names the installed version from the ref
    the tool resolved. For ``latest`` that is the release tag, never the literal."""
    from persona_install import persona_install_consent_identity
    from tools import persona_install_inspect

    fake, tmp_store_cls = _wire_persona_inspect(monkeypatch, tmp_path, channel=None)
    fake.resolved_ref = "v1.2.0"
    tmp_store_cls().record(
        identity=persona_install_consent_identity(
            persona_id=fake.persona_id, version=fake.version, checksum=fake.checksum),
        persona_id=fake.persona_id, version=fake.version, checksum=fake.checksum)

    payload = _payload(await persona_install_inspect.handler(
        {"repo": "owner/repo", "ref": "latest"}))

    assert payload["ok"] is True
    assert payload["resolved_ref"] == "v1.2.0"
    assert "latest" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_inspect_with_no_release_reports_no_release_found(monkeypatch, tmp_path) -> None:
    """INV-PERS-020 at the tool: the real inspection, with only the release
    lookup stubbed, refuses in the structured envelope and stages nothing."""
    import plugin_store
    from tools import persona_install_inspect

    config = tmp_path / "config"
    monkeypatch.setenv("CASA_CONFIG_DIR", str(config))

    def _none(repo, **_k):
        raise plugin_store.NoReleaseFound(f"{repo}: no published release tag (v<semver>) found")

    monkeypatch.setattr(plugin_store, "resolve_latest_release", _none)

    payload = _payload(await persona_install_inspect.handler(
        {"repo": "owner/repo", "ref": "latest"}))

    assert payload == {"ok": False, "kind": "no_release_found",
                       "detail": "owner/repo: no published release tag (v<semver>) found"}
    assert not config.exists()


def test_inspect_tool_description_names_latest() -> None:
    from tools import persona_install_inspect

    assert "latest" in persona_install_inspect.description


@pytest.mark.asyncio
async def test_persona_inspect_without_channel_returns_consent_channel_unavailable(
    monkeypatch, tmp_path,
) -> None:
    from tools import persona_install_inspect
    _wire_persona_inspect(monkeypatch, tmp_path, channel=None)

    payload = _payload(await persona_install_inspect.handler(
        {"repo": "owner/repo", "ref": "main"}))
    assert payload["ok"] is False
    assert payload["kind"] == "consent_channel_unavailable"
    assert "no telegram channel" in payload["detail"]


@pytest.mark.asyncio
async def test_persona_inspect_with_preacked_ledger_skips_keyboard(
    monkeypatch, tmp_path,
) -> None:
    import persona_install_consent
    from persona_install import persona_install_consent_identity
    from tools import persona_install_inspect

    fake, tmp_store_cls = _wire_persona_inspect(monkeypatch, tmp_path, channel=None)
    identity = persona_install_consent_identity(
        persona_id=fake.persona_id, version=fake.version, checksum=fake.checksum)
    tmp_store_cls().record(
        identity=identity, persona_id=fake.persona_id, version=fake.version,
        checksum=fake.checksum)

    def _must_not_post(**kwargs):
        raise AssertionError("keyboard must not be attempted on a pre-acked install")

    monkeypatch.setattr(
        persona_install_consent, "prompt_persona_install_consent", _must_not_post)

    payload = _payload(await persona_install_inspect.handler(
        {"repo": "owner/repo", "ref": "main"}))
    assert payload["ok"] is True
    assert payload["consent"] == "pre_authorized"
    assert payload["checksum"] == fake.checksum


@pytest.mark.asyncio
async def test_persona_inspect_happy_path_posts_keyboard(monkeypatch, tmp_path) -> None:
    import persona_install_consent
    from tools import persona_install_inspect

    calls: list[dict] = []

    def _prompt(**kwargs):
        calls.append(kwargs)
        return _Handle(settled="posted")

    _wire_persona_inspect(monkeypatch, tmp_path, channel=SimpleNamespace(chat_id="123"))
    monkeypatch.setattr(
        persona_install_consent, "prompt_persona_install_consent", _prompt)

    payload = _payload(await persona_install_inspect.handler(
        {"repo": "owner/repo", "ref": "main"}))
    assert payload["ok"] is True
    assert payload["consent"] == "keyboard_posted"
    assert len(calls) == 1
    assert calls[0]["chat_id"] == 123 and calls[0]["operator_id"] == 123


@pytest.mark.asyncio
@pytest.mark.parametrize("handle,expected_kind", [
    (_Handle(refused="args_too_large"), "consent_prompt_refused"),
    (_Handle(settled="delivery_failed"), "consent_delivery_failed"),
    (_Handle(settled="inactive"), "consent_prompt_inactive"),
    (_Handle(hang=True), "consent_post_unsettled"),
])
async def test_persona_inspect_post_failures_are_structured(
    monkeypatch, tmp_path, handle, expected_kind,
) -> None:
    import persona_install_consent
    import tools as tools_mod
    from tools import persona_install_inspect

    _wire_persona_inspect(monkeypatch, tmp_path, channel=SimpleNamespace(chat_id="123"))
    monkeypatch.setattr(
        persona_install_consent, "prompt_persona_install_consent",
        lambda **kwargs: handle)
    # Bounded: shrink the settle bound instead of waiting 30s (never patch
    # <module>.asyncio.sleep — memory-cage rule).
    monkeypatch.setattr(tools_mod, "_INSTALL_CONSENT_POST_TIMEOUT_S", 0.05)

    payload = _payload(await persona_install_inspect.handler(
        {"repo": "owner/repo", "ref": "main"}))
    assert payload["ok"] is False
    assert payload["kind"] == expected_kind


@pytest.mark.asyncio
async def test_persona_inspect_prompt_exception_is_structured(
    monkeypatch, tmp_path,
) -> None:
    import persona_install_consent
    from tools import persona_install_inspect

    def _boom(**kwargs):
        raise RuntimeError("registration blew up")

    _wire_persona_inspect(monkeypatch, tmp_path, channel=SimpleNamespace(chat_id="123"))
    monkeypatch.setattr(
        persona_install_consent, "prompt_persona_install_consent", _boom)

    payload = _payload(await persona_install_inspect.handler(
        {"repo": "owner/repo", "ref": "main"}))
    assert payload["ok"] is False
    assert payload["kind"] == "consent_prompt_failed"
    assert "registration blew up" in payload["detail"]


# ---------------------------------------------------------------------------
# v0.102.0 (#217), reworked in v0.351.0:
# persona_install_inspect mirrors specialist_install_inspect — on
# Approve+ack, reconcile_cb continues the configurator as one turn on its desk
# in the operator's DM (``_dispatch_desk_continuation``) so the persona recipe
# finishes without a manual nudge. reconcile_cb runs from the tap-callback
# finish hook and must never raise.
# ---------------------------------------------------------------------------


def _capture_persona_reconcile(monkeypatch):
    import persona_install_consent

    cap: dict = {}

    def _prompt(**kwargs):
        cap["reconcile_cb"] = kwargs["reconcile_cb"]
        return _Handle(settled="posted")

    monkeypatch.setattr(
        persona_install_consent, "prompt_persona_install_consent", _prompt)
    return cap


@pytest.mark.asyncio
async def test_persona_reconcile_cb_resumes_the_captured_engagement(
    monkeypatch, tmp_path,
) -> None:
    from tools import persona_install_inspect

    delivered: list = []

    async def _dispatch(*, chat_id, user_id, desk_role, request_id, text):
        delivered.append((chat_id, user_id, desk_role, text))
        return True

    channel = SimpleNamespace(chat_id="123", _dispatch_desk_continuation=_dispatch)
    fake, _ = _wire_persona_inspect(monkeypatch, tmp_path, channel=channel)
    cap = _capture_persona_reconcile(monkeypatch)

    payload = _payload(await persona_install_inspect.handler(
        {"repo": "owner/repo", "ref": "main"}))
    assert payload["consent"] == "keyboard_posted"

    # #662: the callback REPORTS its outcome (see the specialist sibling).
    assert await cap["reconcile_cb"]() is True
    assert len(delivered) == 1
    chat_id, user_id, desk_role, text = delivered[0]
    assert (chat_id, user_id, desk_role) == (123, 123, "configurator")
    assert "persona_install_commit" in text
    # The desk turn starts a fresh session: the text carries every staged value.
    for value in (fake.persona_id, fake.version, fake.checksum, str(fake.staged_dir)):
        assert value in text
    assert "emit_completion" not in text


@pytest.mark.asyncio
async def test_persona_reconcile_cb_swallows_a_delivery_failure(
    monkeypatch, tmp_path,
) -> None:
    from tools import persona_install_inspect

    async def _dispatch(**kwargs):
        raise RuntimeError("delivery blew up")

    channel = SimpleNamespace(chat_id="123", _dispatch_desk_continuation=_dispatch)
    _wire_persona_inspect(monkeypatch, tmp_path, channel=channel)
    cap = _capture_persona_reconcile(monkeypatch)

    await persona_install_inspect.handler({"repo": "owner/repo", "ref": "main"})

    # fail-safe: must not raise — and #662: it reports the swallowed failure.
    assert await cap["reconcile_cb"]() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("tap_state", ["refused", "no_desk_route"])
async def test_662_persona_reconcile_cb_reports_false_for_a_dead_engagement(
    monkeypatch, tmp_path, tap_state,
) -> None:
    """#662, persona surface: a tap whose configurator continuation the desk
    route does not take (it refuses, or the channel has none) must report
    False, so the finish hook writes the corrective DM instead of claiming an
    install. The specialist sibling is pinned end-to-end in
    test_tools_specialist_install.py; this pins that the persona callback — a
    separately written copy — agrees."""
    from tools import persona_install_inspect

    delivered: list = []
    channel = SimpleNamespace(chat_id="123")
    if tap_state == "refused":
        async def _dispatch(**kwargs):
            delivered.append(kwargs)
            return False

        channel._dispatch_desk_continuation = _dispatch
    _wire_persona_inspect(monkeypatch, tmp_path, channel=channel)
    cap = _capture_persona_reconcile(monkeypatch)

    await persona_install_inspect.handler({"repo": "owner/repo", "ref": "main"})

    result = await cap["reconcile_cb"]()
    actual = (result, len(delivered))
    assert actual == (False, 1 if tap_state == "refused" else 0), (
        f"persona reconciliation facts: {actual!r}")


@pytest.mark.asyncio
async def test_persona_apply_refuses_a_pack_that_does_not_declare_the_requested_identity(
        monkeypatch, tmp_path) -> None:
    """INV-PERS-013 red case (#737): a pack found under the approved roots must
    DECLARE the identity the ref names. `_resolve_local_persona` (#323) and the
    resident loader (#670) already refuse a mis-parked pack; `persona_apply`
    loaded the pack at the requested directory and applied whatever identity
    it declared. RED at the base: the tool answers `ok: true` for a request of
    `casa/ellen@0.9.0` and stages a binding for `casa/ellen@0.8.0`."""
    import shutil

    from persona_pack import load_persona_pack
    from personality_binding import InstanceDir
    from test_persona_install import install_persona_for_apply
    from tools import persona_apply

    bindings_root = tmp_path / "bindings"
    monkeypatch.setenv("CASA_BINDINGS_DIR", str(bindings_root))
    # A valid pack declaring casa/ellen@0.8.0 at its canonical directory, and
    # the SAME bytes parked at the 0.9.0 directory.
    install_persona_for_apply(tmp_path, monkeypatch, persona_id="casa/ellen", version="0.8.0")
    personas = tmp_path / "config-root" / "personas" / "casa" / "ellen"
    shutil.copytree(personas / "0.8.0", personas / "0.9.0")
    canonical = load_persona_pack(personas / "0.8.0" / "pack", personas / "0.8.0" / "manifest.json")
    parked = load_persona_pack(personas / "0.9.0" / "pack", personas / "0.9.0" / "manifest.json")
    assert canonical.persona_id == parked.persona_id == "casa/ellen"
    assert canonical.version == parked.version == "0.8.0"
    assert canonical.checksum == parked.checksum

    result = await persona_apply.handler({
        "target_role_id": "resident:assistant",
        "persona_id": "casa/ellen", "persona_version": "0.9.0",
    })

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["kind"] == "persona_unavailable"
    assert "casa/ellen@0.9.0" in payload["detail"]
    assert "casa/ellen@0.8.0" in payload["detail"]
    instance_dir = InstanceDir(bindings_root / "resident-assistant")
    assert instance_dir.active() is None
    assert instance_dir.desired() is None
    assert len(list((bindings_root / "resident-assistant").glob("*.yaml"))) == 0


async def _persona_expiry(monkeypatch, tmp_path, *, outcome, run="deleg-p",
                          commit_index=None):
    from unittest.mock import AsyncMock

    import agent as agent_mod
    import persona_install_consent
    from tools import persona_install_inspect

    # The REAL prompt, even when an earlier call in this test wrapped it.
    real = getattr(persona_install_consent.prompt_persona_install_consent,
                   "_real", persona_install_consent.prompt_persona_install_consent)
    cap: dict = {}

    class _Coordinator:
        def register_challenge(self, key, **kwargs):
            cap["key"] = key
            cap["on_commit_sync"] = kwargs["on_commit_sync"]
            cap["finish_factory"] = kwargs["finish_factory"]
            return _Handle(settled="posted")

    def _prompt(**kwargs):
        kwargs["coordinator"] = _Coordinator()
        return real(**kwargs)

    _prompt._real = real
    edits: list = []

    async def _edit(chat_id, message_id, text):
        edits.append(text)

    channel = SimpleNamespace(
        chat_id="123",
        _dispatch_desk_continuation=AsyncMock(return_value=True),
        edit_dm_message=_edit)
    _wire_persona_inspect(monkeypatch, tmp_path, channel=channel)
    monkeypatch.setattr(
        persona_install_consent, "prompt_persona_install_consent", _prompt)

    token = agent_mod.origin_var.set(
        {"execution_role": "configurator", "_delegation_id": run})
    try:
        payload = _payload(await persona_install_inspect.handler(
            {"repo": "owner/repo", "ref": "main"}))
    finally:
        agent_mod.origin_var.reset(token)
    assert payload["consent"] == "keyboard_posted"
    if outcome is not None:
        req = SimpleNamespace(meta={})
        if commit_index is not None:
            cap["on_commit_sync"](commit_index, req.meta)
        await cap["finish_factory"](88, req)(outcome)
    return channel, edits, cap


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome, edit", [
    ({"outcome": "no_answer"},
     "⌛ Expired — persona install consent for 'warm-helper' was not answered; "
     "nothing was installed"),
    ({"outcome": "cancelled", "reason": "challenge_cancelled"}, None),
    ({"outcome": "answered", "option_index": 1},
     "❌ Denied — 'warm-helper' was not installed"),
])
async def test_inspect_withdrawal_tells_the_engagement_nothing(
    monkeypatch, tmp_path, outcome, edit,
) -> None:
    """An expiry, a Deny or a withdrawal edits
    the card and continues nobody — the configurator's desk is not dispatched
    and no approval is recorded."""
    from persona_install import PersonaInstallAckStore

    channel, edits, _ = await _persona_expiry(
        monkeypatch, tmp_path, outcome=outcome,
        commit_index=outcome.get("option_index"))
    ack_path = tmp_path / "persona_acks.json"
    acks = len(PersonaInstallAckStore(path=ack_path)._load()) if ack_path.exists() else 0
    assert (channel._dispatch_desk_continuation.await_count, len(edits), acks) == (0, 1, 0)
    if edit is not None:
        assert edits == [edit]


@pytest.mark.asyncio
async def test_the_persona_card_key_names_its_requester(monkeypatch, tmp_path) -> None:
    """#1445 (design round d1): one card per requesting configurator run (its
    delegation id, since v0.351.0); the approval identity is
    unchanged."""
    _c, _e, cap_a = await _persona_expiry(
        monkeypatch, tmp_path, outcome=None, run="deleg-a")
    _c, _e, cap_b = await _persona_expiry(
        monkeypatch, tmp_path, outcome=None, run="deleg-b")
    ka, kb = cap_a["key"], cap_b["key"]
    facts = (ka == kb, ka.identity == kb.identity, ka.requester, kb.requester)
    assert facts == (False, True, "deleg-a", "deleg-b"), f"key facts: {facts!r}"
