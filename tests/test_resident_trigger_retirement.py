"""#620 — a resident webhook secret lives exactly as long as its route (INV-TRIG-016).

Before this change nothing retired a resident webhook secret: a trigger deleted and
later recreated under the same name inherited the old credential through every removal
path, and the request path read a slot by NAME with no proof of who minted it or for
whom. What is pinned here, against REAL modules (`TriggerRegistry`, `MessageBus`,
`reload.dispatch`, `reminders`, `config_sync.reconcile`, the wildcard handler behind an
aiohttp test client), is the lifecycle the change establishes:

* a Casa-owned resident route authenticates only with bytes its v2 receipt certifies for
  the role that routes the name;
* a certified slot is retired when its role's routes are next successfully re-registered
  from a declaration that no longer backs the name, when another role stages the name,
  or at a boot that finds the role's directory gone — never on a teardown, never by an
  absence sweep;
* a slot Casa cannot prove it minted is never destroyed — it is refused and reported;
* validation → retirement → installation → publication is one operation, and a refused
  retirement aborts it with nothing published.

The residual-characterisation tests at the end are named as such: they pin the stated
limits of the guarantee, not repairs.

Every fault is injected by swapping the TARGET MODULE'S OWN `os` reference (the
`_ModuleShim` idiom from `test_webhook_mint_receipt.py`), never the shared `os` module,
and never `<module>.asyncio.sleep`. Only `tmp_path` is used for files.

Base-compatibility adapters: the tests import no symbol that does not exist at the base.
Where a test names a new primitive it resolves it with `getattr` and records an EMPTY
outcome when it is absent, so the pre-fix failure is always the wrong count or file
state the base actually produces, never an `ImportError`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import reload as reload_mod
import resident_trigger_secrets as rts
import webhook_auth
from config import TriggerSpec
from trigger_registry import TriggerRegistry

try:
    from tests.broker_helpers import wait_until
    from tests.test_casa_reload_triggers_resident import (
        _seed_policies, _seed_resident_with_disclosure, _runtime_with)
except ImportError:  # pytest rootdir variance, as the sibling tests do
    from broker_helpers import wait_until
    from test_casa_reload_triggers_resident import (
        _seed_policies, _seed_resident_with_disclosure, _runtime_with)

pytestmark = pytest.mark.asyncio

ROLE = "assistant"
NAME = "door"
HEADER = "X-API-Key"
CASA_TOKEN = b"z" * 43            # casa-shaped, provider-valid, minted by nobody


# ---------------------------------------------------------------------------
# Fault injection — the module's OWN `os`, never the shared module
# ---------------------------------------------------------------------------


class _ModuleShim:
    """A stand-in for a module with some attributes overridden (the idiom from
    `test_webhook_mint_receipt.py`). `swap(monkeypatch, module, **overrides)`
    installs it on `module.os`."""

    def __init__(self, wrapped, **overrides):
        self._wrapped = wrapped
        self.__dict__.update(overrides)

    def __getattr__(self, item):
        return getattr(self._wrapped, item)


def _shim(monkeypatch, module, **overrides) -> None:
    monkeypatch.setattr(module, "os", _ModuleShim(getattr(module, "os"), **overrides))


# ---------------------------------------------------------------------------
# Base-compatibility adapters (see the module docstring)
# ---------------------------------------------------------------------------


def _mint(specs, *, secrets_dir: Path, role: str = ROLE) -> list[tuple[str, str]]:
    """`mint_for_specs` with the role when the writer takes one, without it at base."""
    try:
        return rts.mint_for_specs(specs, secrets_dir=secrets_dir, role=role)
    except TypeError:
        return rts.mint_for_specs(specs, secrets_dir=secrets_dir)


def _retire(role: str, *, declared, staged, secrets_dir: Path) -> tuple[int, list[str]]:
    """`(retired_count, failed_names)` — the base has no retirement primitive and
    records an EMPTY outcome, so a red assertion is a count, never an AttributeError."""
    fn = getattr(rts, "retire_for_role", None)
    if fn is None:
        return 0, []
    out = fn(role, declared=list(declared), staged=set(staged), secrets_dir=secrets_dir)
    return len(out.retired), list(out.failed)


def _retired_count(actions, name: str = NAME) -> int:
    """Counts the bare action and the `config_sync` cascade's prefixed form
    (`triggers:<role>:trigger_secret_retired_<name>`)."""
    tag = f"trigger_secret_retired_{name}"
    return sum(a == tag or a.endswith(":" + tag) for a in actions)


def _certified_reader_calls(monkeypatch) -> list[str]:
    """Spy on the certified reader; an absent reader (base) records nothing."""
    calls: list[str] = []
    real = getattr(webhook_auth, "read_certified_secret", None)
    if real is None:
        return calls

    def spy(name, **kw):
        calls.append(name)
        return real(name, **kw)

    monkeypatch.setattr(webhook_auth, "read_certified_secret", spy)
    return calls


def _read_secret_calls(monkeypatch) -> list[str]:
    calls: list[str] = []
    real = webhook_auth.read_secret

    def spy(name, **kw):
        calls.append(name)
        return real(name, **kw)

    monkeypatch.setattr(webhook_auth, "read_secret", spy)
    return calls


async def _boot(*, role_configs: dict, secrets_dir: Path, agents_dir: Path) -> dict:
    """Boot step 13b′ through the production seam. The base seam takes no
    `agents_dir` and returns only the mint failures; the adapter normalises both
    shapes to `{"retired": [...], "failed": [...], "mint_failures": [...]}`."""
    import casa_core
    fn = getattr(casa_core, "_boot_reconcile_resident_trigger_secrets", None)
    if fn is None:
        failures = await casa_core._boot_mint_resident_trigger_secrets(
            role_configs=role_configs, secrets_dir=secrets_dir)
        return {"retired": [], "failed": [], "mint_failures": list(failures)}
    return await fn(role_configs=role_configs, secrets_dir=secrets_dir,
                    agents_dir=str(agents_dir))


# ---------------------------------------------------------------------------
# Filesystem inventories, receipts, declarations
# ---------------------------------------------------------------------------


def _files(d: Path) -> dict[str, bytes]:
    if not d.exists():
        return {}
    return {p.name: p.read_bytes() for p in sorted(d.iterdir()) if p.is_file()}


def _write_v2(name: str, raw: bytes, role: str, secrets_dir: Path) -> None:
    secrets_dir.mkdir(parents=True, exist_ok=True)
    (secrets_dir / name).write_bytes(raw)
    (secrets_dir / f"{name}.mint").write_bytes(json.dumps({
        "v": 2, "minted_by": "casa",
        "value_sha256": hashlib.sha256(raw).hexdigest(), "role": role,
    }).encode("ascii"))


def _write_v1(name: str, raw: bytes, secrets_dir: Path) -> None:
    secrets_dir.mkdir(parents=True, exist_ok=True)
    (secrets_dir / name).write_bytes(raw)
    (secrets_dir / f"{name}.mint").write_bytes(json.dumps({
        "v": 1, "minted_by": "casa",
        "value_sha256": hashlib.sha256(raw).hexdigest(),
    }).encode("ascii"))


def _receipt_role(name: str, secrets_dir: Path) -> str | None:
    try:
        return json.loads((secrets_dir / f"{name}.mint").read_bytes()).get("role")
    except (OSError, ValueError):
        return None


def _webhook(name: str = NAME, *, mode: str = "static_header", owner: str = "casa",
             clearance: str = "public") -> TriggerSpec:
    return TriggerSpec(
        name=name, type="webhook", clearance=clearance,
        auth={"mode": mode, "header": HEADER, "tolerance_secs": 300,
              "secret_owner": owner},
    )


def _entry(name: str = NAME, *, mode: str = "static_header", owner: str = "casa") -> dict:
    return {"name": name, "type": "webhook",
            "auth": {"mode": mode, "header": HEADER, "secret_owner": owner}}


def _interval(name: str, minutes: int = 60) -> dict:
    return {"name": name, "type": "interval", "minutes": minutes,
            "channel": "telegram", "prompt": "tick"}


def _triggers_yaml(entries) -> str:
    return yaml.safe_dump({"schema_version": 2, "triggers": list(entries)},
                          sort_keys=False)


def _write_triggers(agent_dir: Path, entries) -> Path:
    p = agent_dir / "triggers.yaml"
    p.write_text(_triggers_yaml(entries), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# A real resident tree, runtime, registry and handler
# ---------------------------------------------------------------------------


_VOICE_MODEL = ("model: {source: ha_option, option: voice_agent_model, default: haiku, "
                "allowed: [opus, sonnet, haiku]}")
_PRIMARY_MODEL = ("model: {source: ha_option, option: primary_agent_model, default: opus, "
                  "allowed: [opus, sonnet, haiku]}")


def _seed_resident(agents_dir: Path, role: str = ROLE) -> Path:
    """The sibling seeder, with the runtime model block matched to the role's
    canonical `role.yaml` (the loader cross-validates exactly that block)."""
    d = _seed_resident_with_disclosure(agents_dir, role=role)
    if role != ROLE:
        rt = d / "runtime.yaml"
        rt.write_text(rt.read_text().replace(_PRIMARY_MODEL, _VOICE_MODEL), encoding="utf-8")
    return d


def _registry(scheduler=None) -> TriggerRegistry:
    return TriggerRegistry(scheduler=scheduler or MagicMock(),
                           app=web.Application(), bus=None)


def _routes(registry: TriggerRegistry, role: str) -> list[str]:
    return registry.webhook_names_for(role)


class _Tree:
    """`tmp_path/config/agents/<role>/…` + `policies/disclosure.yaml`, a runtime bound
    to it, one real registry, and the secrets directory the reload mints into."""

    def __init__(self, tmp_path: Path, monkeypatch, *, roles=(ROLE,), scheduler=None):
        self.root = tmp_path / "config"
        self.agents_dir = self.root / "agents"
        self.secrets = tmp_path / "secrets"
        self.registry = _registry(scheduler)
        _seed_policies(self.root)
        for r in roles:
            _seed_resident(self.agents_dir, role=r)
        self.runtime = _runtime_with(self.root, trigger_registry=self.registry)
        import trigger_reconcile
        monkeypatch.setattr(trigger_reconcile, "SECRETS_DIR", self.secrets)
        import agent as agent_mod
        monkeypatch.setattr(agent_mod, "active_runtime", self.runtime, raising=False)

    def dir(self, role: str = ROLE) -> Path:
        return self.agents_dir / role

    def declare(self, entries, role: str = ROLE) -> Path:
        return _write_triggers(self.dir(role), entries)

    def triggers_path(self, role: str = ROLE) -> str:
        return str(self.dir(role) / "triggers.yaml")

    async def reload(self, scope: str = "triggers", role: str | None = ROLE) -> dict:
        return await reload_mod.dispatch(scope, runtime=self.runtime, role=role)

    def load_cfg(self, role: str = ROLE):
        import agent_loader
        import policies as policies_module
        lib = policies_module.load_policies(str(self.root / "policies" / "disclosure.yaml"))
        return agent_loader.load_agent_from_dir(str(self.dir(role)), policies=lib)


def _bus():
    bus = MagicMock()
    bus.send = AsyncMock()
    return bus


async def _app(registry, secrets_dir: Path, *, bus=None, secret: str = ""):
    from casa_core import _make_webhook_handler
    from rate_limit import RateLimiter
    bus = bus or _bus()
    handler = _make_webhook_handler(
        webhook_rate_limiter=RateLimiter(capacity=0, window_s=60.0),
        webhook_secret=secret, trigger_registry=registry, default_role=ROLE,
        bus=bus, secrets_dir=str(secrets_dir))
    app = web.Application()
    app.router.add_post("/webhook/{name}", handler)
    return app, bus


async def _post_static(client, name: str, token: bytes) -> int:
    r = await client.post(f"/webhook/{name}", data=b"{}",
                          headers={HEADER: token.decode("ascii", "replace")})
    return r.status


def _dispatches(bus) -> int:
    return bus.send.await_count


def _provenance(name: str, secrets_dir: Path) -> str:
    return webhook_auth.resident_secret_provenance(name, secrets_dir=secrets_dir)


def _rows(specs, registry, role: str, secrets_dir: Path) -> dict[str, dict]:
    snap = rts.snapshot_rows(specs=specs, registry=registry, role=role,
                             global_secret_usable=True)
    return {r["name"]: r for r in rts.resolve_rows(snap, secrets_dir=secrets_dir)}


def _tolerant_registry_mocks(tree: _Tree) -> None:
    """The `agents` sweep reads two registry accessors that a bare MagicMock
    answers with MagicMocks (unpackable / not a dict)."""
    tree.runtime.specialist_registry.load_failures = MagicMock(return_value=[])
    tree.runtime.specialist_registry.all_configs = MagicMock(return_value={})
    tree.runtime.specialist_registry.load = MagicMock()


def _patch_construction(monkeypatch) -> None:
    """A resident reload constructs an Agent (SDK) — replace construction and the
    home provisioning; everything about triggers and secrets stays real."""
    monkeypatch.setattr(reload_mod, "_construct_agent",
                        lambda **kw: MagicMock(handle_message=AsyncMock()))
    monkeypatch.setattr("agent_home.provision_agent_home",
                        lambda *, role, home_root, defaults_root: None)


def _known_resident(tree: _Tree, role: str = ROLE) -> None:
    """Make the runtime know the resident (as boot would), with the personality
    map refresh a no-op — it reads nothing about secrets."""
    tree.runtime.role_configs[role] = tree.load_cfg(role)
    tree.runtime.refresh_personality_maps = lambda: None


async def _establish(tree: _Tree) -> bytes:
    """Declare `door`, register it through the real reload, return its bytes."""
    tree.declare([_entry()])
    res = await tree.reload("triggers")
    assert res["status"] == "ok", res
    files = _files(tree.secrets)
    assert NAME in files and f"{NAME}.mint" in files, files
    assert _routes(tree.registry, ROLE) == [NAME]
    return files[NAME]


def _sync_drop_door(tree: _Tree, tmp_path: Path) -> None:
    """The REAL entry-level reconcile: the image (defaults) no longer ships
    `door`, the baseline did, the live file is untouched → the merge drops it."""
    import config_sync
    try:
        from tests.test_config_sync_entry_merge import _FakeGit
    except ImportError:
        from test_config_sync_entry_merge import _FakeGit
    rel = f"agents/{ROLE}/triggers.yaml"
    defaults = tmp_path / "defaults"
    baseline = tmp_path / "baseline"
    for root, entries in ((defaults, [_interval("heartbeat")]),
                         (baseline, [_interval("heartbeat"), _entry()])):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(_triggers_yaml(entries), encoding="utf-8")
    # The live file must DIFFER from the baseline for the entry-level merge to run
    # (an untouched file is tracked byte-for-byte instead): one local addition.
    tree.declare([_interval("heartbeat"), _entry(), _interval("mine", 30)])
    report = config_sync.reconcile(
        defaults_dir=defaults, config_dir=tree.root, baseline_dir=baseline,
        image_version="test", git=_FakeGit(), validate=lambda _rel: None,
        validate_repo=None, validate_text=config_sync._make_text_validator(tree.root))
    assert len(report.merged) == 1 and report.merged[0]["deleted"] == [NAME], report
    doc = yaml.safe_load(Path(tree.triggers_path()).read_text())
    assert [e["name"] for e in doc["triggers"]] == ["heartbeat", "mine"]


# ---------------------------------------------------------------------------
# 1. The removal paths — every one retires, and a recreate is fresh
# ---------------------------------------------------------------------------

REMOVAL_CASES = [
    "path1_triggers", "path1_agent", "path2_type", "path3_hmac", "path5_rename",
    "path6_hand_edit", "path7_sync", "path8_boot_sync", "path10_boot_absent_dir",
]


@pytest.mark.parametrize("case", REMOVAL_CASES)
async def test_removal_paths_retire_and_recreate_fresh(case, tmp_path, monkeypatch):
    """Red at base for EVERY arm: the three registration sites and boot only ever
    call `mint_for_specs`, which skips an undeclared name and never deletes — the
    old live file and its receipt survive, no retirement action exists, and the
    recreated name returns the SAME bytes."""
    import reminders
    assert len(REMOVAL_CASES) == 9
    tree = _Tree(tmp_path, monkeypatch)
    _tolerant_registry_mocks(tree)
    _patch_construction(monkeypatch)
    old = await _establish(tree)
    path = tree.triggers_path()
    expected_after = {}
    reg_after = tree.registry

    if case == "path1_triggers":
        assert reminders.delete_entry(path, NAME) == "removed"
        res = await tree.reload("triggers"); actions = res["actions"]
    elif case == "path1_agent":
        _known_resident(tree)
        assert reminders.delete_entry(path, NAME) == "removed"
        res = await tree.reload("agent"); actions = res["actions"]
    elif case == "path2_type":
        assert reminders.upsert_entry(path, _interval(NAME)) == "replaced"
        res = await tree.reload("triggers"); actions = res["actions"]
    elif case == "path3_hmac":
        assert reminders.upsert_entry(path, _entry(mode="hmac_body")) == "replaced"
        res = await tree.reload("triggers"); actions = res["actions"]
    elif case == "path5_rename":
        assert reminders.delete_entry(path, NAME) == "removed"
        assert reminders.upsert_entry(path, _entry("porch")) == "added"
        res = await tree.reload("triggers"); actions = res["actions"]
        expected_after = {"porch", "porch.mint"}
    elif case == "path6_hand_edit":
        tree.declare([])
        res = await tree.reload("triggers"); actions = res["actions"]
    elif case == "path7_sync":
        import config_sync
        _known_resident(tree)
        tree.runtime.defaults_dir = str(tmp_path / "defaults")
        tree.runtime.data_dir = str(tmp_path / "data")
        monkeypatch.setitem(reload_mod._HANDLERS, "policies",
                            AsyncMock(return_value=[]))

        def fake_run(**kw):           # the real reconcile, inside `run`'s window
            _sync_drop_door(tree, tmp_path)
            return 0
        monkeypatch.setattr(config_sync, "run", fake_run)
        res = await tree.reload("config_sync", role=None); actions = res["actions"]
    elif case == "path8_boot_sync":
        _sync_drop_door(tree, tmp_path)          # the oneshot, before boot
        cfg = tree.load_cfg()
        reg_after = _registry()
        reg_after.register_agent(role=ROLE, triggers=cfg.triggers, channels=cfg.channels)
        res = await _boot(role_configs={ROLE: cfg}, secrets_dir=tree.secrets,
                          agents_dir=tree.agents_dir)
        actions = [f"trigger_secret_retired_{n}" for n in res["retired"]]
    else:  # path10_boot_absent_dir
        import shutil
        shutil.rmtree(tree.dir())
        reg_after = _registry()
        res = await _boot(role_configs={}, secrets_dir=tree.secrets,
                          agents_dir=tree.agents_dir)
        actions = [f"trigger_secret_retired_{n}" for n in res["retired"]]

    if isinstance(res, dict) and "status" in res:
        assert res["status"] == "ok", res
    assert _retired_count(actions) == 1, (case, actions)
    files = _files(tree.secrets)
    assert NAME not in files and f"{NAME}.mint" not in files, (case, sorted(files))
    assert set(files) == expected_after, (case, sorted(files))
    assert NAME not in _routes(reg_after, ROLE)

    # Recreate the same declaration; a fresh credential, certified.
    if case == "path10_boot_absent_dir":
        _seed_resident(tree.agents_dir, role=ROLE)
    tree.declare([_entry()] + ([_interval("heartbeat")] if case in ("path7_sync", "path8_boot_sync") else []))
    res2 = await tree.reload("triggers")
    assert res2["status"] == "ok", res2
    files = _files(tree.secrets)
    assert set(files) == expected_after | {NAME, f"{NAME}.mint"}, sorted(files)
    new = files[NAME]
    assert new != old
    assert _provenance(NAME, tree.secrets) == "casa_minted"

    app, bus = await _app(tree.registry, tree.secrets)
    async with TestClient(TestServer(app)) as client:
        assert await _post_static(client, NAME, old) == 401
        assert _dispatches(bus) == 0
        assert await _post_static(client, NAME, new) == 200
        assert _dispatches(bus) == 1


# ---------------------------------------------------------------------------
# 3. J1 — the credential's lifetime is the ROUTE's lifetime
# ---------------------------------------------------------------------------


def _mint_spy(monkeypatch) -> list[str]:
    calls: list[str] = []
    real = webhook_auth.mint_resident_secret

    def spy(name, **kw):
        calls.append(name)
        return real(name, **kw)
    monkeypatch.setattr(webhook_auth, "mint_resident_secret", spy)
    return calls


async def test_delete_reload_recreate_reload_rotates_once(tmp_path, monkeypatch):
    """Red at base: the first reload removes only the route and leaves both
    artifacts; the second reuses them — zero retirements, zero mints, old 200."""
    import reminders
    tree = _Tree(tmp_path, monkeypatch)
    old = await _establish(tree)
    mints = _mint_spy(monkeypatch)

    assert reminders.delete_entry(tree.triggers_path(), NAME) == "removed"
    r1 = await tree.reload("triggers")
    assert r1["status"] == "ok"
    assert _files(tree.secrets) == {}, sorted(_files(tree.secrets))
    tree.declare([_entry()])
    r2 = await tree.reload("triggers")
    assert r2["status"] == "ok"

    assert _retired_count(r1["actions"]) + _retired_count(r2["actions"]) == 1
    assert len(mints) == 1, mints
    new = (tree.secrets / NAME).read_bytes()
    assert new != old
    app, bus = await _app(tree.registry, tree.secrets)
    async with TestClient(TestServer(app)) as client:
        assert await _post_static(client, NAME, old) == 401
        assert _dispatches(bus) == 0
        assert await _post_static(client, NAME, new) == 200
        assert _dispatches(bus) == 1


async def test_delete_recreate_one_reload_keeps_credential(tmp_path, monkeypatch):
    """J1's other half, and green at base: a name deleted and recreated with NO
    registration in between never left service, so nothing is retired or minted."""
    import reminders
    tree = _Tree(tmp_path, monkeypatch)
    old = await _establish(tree)
    inode = (tree.secrets / NAME).stat().st_ino
    mints = _mint_spy(monkeypatch)

    assert reminders.delete_entry(tree.triggers_path(), NAME) == "removed"
    assert reminders.upsert_entry(tree.triggers_path(), _entry()) == "added"
    res = await tree.reload("triggers")
    assert res["status"] == "ok"

    assert _retired_count(res["actions"]) == 0
    assert mints == []
    assert (tree.secrets / NAME).read_bytes() == old
    assert (tree.secrets / NAME).stat().st_ino == inode
    app, bus = await _app(tree.registry, tree.secrets)
    async with TestClient(TestServer(app)) as client:
        assert await _post_static(client, NAME, old) == 200
        assert _dispatches(bus) == 1


# ---------------------------------------------------------------------------
# 4. J2 — an unproven slot is preserved AND refused, and the report says so
# ---------------------------------------------------------------------------


def _unlink_spy(monkeypatch, module=webhook_auth) -> list[str]:
    calls: list[str] = []
    real_os = getattr(module, "os")

    def unlink(path, *a, **kw):
        calls.append(os.path.basename(str(path)))
        return real_os.unlink(path, *a, **kw)
    _shim(monkeypatch, module, unlink=unlink)
    return calls


async def test_unproven_casa_slot_is_preserved_but_refused(tmp_path, monkeypatch):
    """Red at base: `_secret_for` reads the slot by NAME under `owner="casa"`, which
    accepts any valid 43-byte token, so the hand-placed bytes return 200 and
    dispatch, and the report calls the row `readable`."""
    tree = _Tree(tmp_path, monkeypatch)
    tree.secrets.mkdir()
    (tree.secrets / NAME).write_bytes(CASA_TOKEN)         # no receipt: unproven
    unlinks = _unlink_spy(monkeypatch)
    tree.declare([_entry()])
    res = await tree.reload("triggers")
    assert res["status"] == "ok", res

    assert unlinks == []
    assert _files(tree.secrets) == {NAME: CASA_TOKEN}, sorted(_files(tree.secrets))
    app, bus = await _app(tree.registry, tree.secrets)
    async with TestClient(TestServer(app)) as client:
        assert await _post_static(client, NAME, CASA_TOKEN) == 401
    assert _dispatches(bus) == 0

    rows = _rows([_webhook()], tree.registry, ROLE, tree.secrets)
    assert list(rows) == [NAME]
    row = rows[NAME]
    assert (row["state"], row["owner"]) == ("unproven_blocked", "casa"), row
    assert "cannot prove" in row["detail"] and "requests are refused" in row["detail"]


# ---------------------------------------------------------------------------
# 5. The certified read, and the three readers it must NOT touch
# ---------------------------------------------------------------------------


async def test_certified_read_matrix_and_bypasses(tmp_path, monkeypatch):
    """Ten arms against one real registry and one real handler; one aggregate
    assertion on the status vector. Red at base: the name-only reader ignores
    receipts and roles, so arms 2-7 return 200, and a v2 receipt reads unproven."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    registry = _registry()
    token = b"a" * 43

    # Resident casa-owned routes, one per arm 1-7.
    arms = ["v2ok", "noreceipt", "malformed", "v1", "wrongrole", "stale", "unavailable"]
    specs = [_webhook(n) for n in arms]
    registry.register_agent(role=ROLE, triggers=specs, channels=[])
    _write_v2("v2ok", token, ROLE, secrets)
    (secrets / "noreceipt").write_bytes(token)
    _write_v2("malformed", token, ROLE, secrets)
    (secrets / "malformed.mint").write_bytes(b"{not json")
    _write_v1("v1", token, secrets)
    _write_v2("wrongrole", token, "butler", secrets)
    _write_v2("stale", token, ROLE, secrets)
    (secrets / "stale").write_bytes(b"b" * 43)              # digest no longer matches
    _write_v2("unavailable", token, ROLE, secrets)
    # Arm 8: provider-owned resident route with a hand-placed value.
    registry.register_agent(role="butler", triggers=[_webhook(
        "prov", mode="timestamped_hmac", owner="provider")], channels=[])
    # Arm 9: a plugin-overlay webhook (its backing is the overlay's, not a receipt).
    registry.replace_plugin_overlay({"plg-p--hook": {
        "role": ROLE, "clearance": "public",
        "auth": {"mode": "static_header", "header": HEADER,
                 "tolerance_secs": 300, "secret_owner": "casa"}}})
    (secrets / "plg-p--hook").write_bytes(b"p" * 43)
    # Arm 10: hmac_body against the global secret.
    registry.register_agent(role="concierge", triggers=[TriggerSpec(
        name="body", type="webhook", clearance="public",
        auth={"mode": "hmac_body", "header": "X-Webhook-Signature",
              "tolerance_secs": 300, "secret_owner": "casa"})], channels=[])

    before = _files(secrets)
    certified = _certified_reader_calls(monkeypatch)
    plain = _read_secret_calls(monkeypatch)
    real_os = webhook_auth.os

    def open_faulting(path, *a, **kw):
        if os.path.basename(str(path)) == "unavailable":
            raise OSError(5, "I/O error")
        return real_os.open(path, *a, **kw)
    _shim(monkeypatch, webhook_auth, open=open_faulting)

    import hmac as _hmac
    global_secret = "s3cret"
    app, bus = await _app(registry, secrets, secret=global_secret)
    statuses = []
    async with TestClient(TestServer(app)) as client:
        for n in arms:
            statuses.append(await _post_static(client, n, token))
        # provider: timestamped_hmac over the hand-placed value
        pv = b"opaque-provider-value"
        (secrets / "prov").write_bytes(pv)
        import time as _time
        t = int(_time.time())
        sig = _hmac.new(pv, f"{t}.".encode() + b"{}", hashlib.sha256).hexdigest()
        r = await client.post("/webhook/prov", data=b"{}",
                              headers={HEADER: f"t={t},v0={sig}"})
        statuses.append(r.status)
        statuses.append(await _post_static(client, "plg-p--hook", b"p" * 43))
        body = b'{"x": 1}'
        r = await client.post("/webhook/body", data=body, headers={
            "X-Webhook-Signature": _hmac.new(global_secret.encode(), body,
                                             hashlib.sha256).hexdigest()})
        statuses.append(r.status)

    assert statuses == [200, 401, 401, 401, 401, 401, 401, 200, 200, 200], statuses
    assert _dispatches(bus) == 4
    after = _files(secrets)
    assert {k: v for k, v in after.items() if not k.startswith(("prov", "plg-"))} == \
        {k: v for k, v in before.items() if not k.startswith(("prov", "plg-"))}
    assert certified == arms, certified                      # exactly the seven casa arms
    assert sorted(plain) == ["plg-p--hook", "prov"], plain   # provider + overlay only
    route = getattr(registry, "webhook_route", None)
    assert route is not None, "the registry publishes no atomic route record"
    assert route("plg-p--hook")["resident"] is False
    assert route("prov")["resident"] is True
    rows = _rows(specs, registry, ROLE, secrets)
    assert (rows["v2ok"]["state"], rows["v2ok"].get("provenance")) == ("readable", "casa_minted")


# ---------------------------------------------------------------------------
# 6. Receipt BEFORE link — no slot without its proof
# ---------------------------------------------------------------------------


def _ordered_os_spy(monkeypatch, module):
    """Records the order of `replace` and `link` calls on the module's `os`."""
    order: list[str] = []
    real_os = getattr(module, "os")
    ops: dict[str, object] = {}

    def replace(src, dst, **kw):
        if str(dst).endswith(".mint"):
            order.append("receipt_replace")
        r = ops.get("replace_fault")
        if r is not None and str(dst).endswith(".mint"):
            raise r
        return real_os.replace(src, dst, **kw)

    def link(src, dst, **kw):
        order.append("link")
        f = ops.get("link_fault")
        if f is not None:
            return f(src, dst)
        return real_os.link(src, dst, **kw)

    _shim(monkeypatch, module, replace=replace, link=link)
    return order, ops


async def test_receipt_first_mint_fault_matrix(tmp_path, monkeypatch):
    """Red at base: the value is linked FIRST and the receipt written best-effort
    afterwards, so a receipt fault leaves a live unproven token with zero reported
    failures, and the ordering assertion sees link before replace."""
    order, ops = _ordered_os_spy(monkeypatch, webhook_auth)

    # (i) receipt write fails → nothing published, one named failure
    d1 = tmp_path / "one"; d1.mkdir()
    ops["replace_fault"] = OSError(28, "No space left on device")
    failures = _mint([_webhook()], secrets_dir=d1)
    assert _files(d1) == {}, sorted(_files(d1))
    assert len(failures) == 1 and failures[0][0] == NAME, failures
    ops.pop("replace_fault")

    # (ii) a successful mint writes the receipt BEFORE linking the value
    d2 = tmp_path / "two"; d2.mkdir()
    order.clear()
    assert _mint([_webhook()], secrets_dir=d2) == []
    assert set(_files(d2)) == {NAME, f"{NAME}.mint"}
    assert "receipt_replace" in order and "link" in order, order
    assert order.index("receipt_replace") < order.index("link"), order

    # (iii) lost link race: a winner published first — its bytes stay, no loser receipt
    d3 = tmp_path / "three"; d3.mkdir()
    winner = b"an-operator-credential-placed-first"

    def lose(src, dst):
        Path(dst).write_bytes(winner)
        raise FileExistsError(17, "File exists")
    ops["link_fault"] = lose
    _mint([_webhook()], secrets_dir=d3)
    ops.pop("link_fault")
    assert _files(d3) == {NAME: winner}, sorted(_files(d3))

    # (iv) link raises after the receipt was written → no live file, no receipt, one failure
    d4 = tmp_path / "four"; d4.mkdir()

    def eio(src, dst):
        raise OSError(5, "I/O error")
    ops["link_fault"] = eio
    failures = _mint([_webhook()], secrets_dir=d4)
    ops.pop("link_fault")
    assert _files(d4) == {}, sorted(_files(d4))
    assert len(failures) == 1 and failures[0][0] == NAME, failures

    # (v) cleanup after a lost link never removes a receipt it did not write
    d5 = tmp_path / "five"; d5.mkdir()
    successor = json.dumps({"v": 2, "minted_by": "casa", "role": "butler",
                            "value_sha256": hashlib.sha256(winner).hexdigest()}).encode()

    def lose_and_replace_receipt(src, dst):
        Path(dst).write_bytes(winner)
        (d5 / f"{NAME}.mint").write_bytes(successor)
        raise FileExistsError(17, "File exists")
    ops["link_fault"] = lose_and_replace_receipt
    unlinks = _unlink_spy(monkeypatch)
    _mint([_webhook()], secrets_dir=d5)
    assert f"{NAME}.mint" not in unlinks, unlinks
    assert _files(d5) == {NAME: winner, f"{NAME}.mint": successor}, sorted(_files(d5))


# ---------------------------------------------------------------------------
# 7. What is NOT retired — the disabled specialist and the negative matrix
# ---------------------------------------------------------------------------


def _bad_cron(name: str = "bad") -> dict:
    return {"name": name, "type": "cron", "schedule": "not-a-cron",
            "channel": "telegram", "prompt": "q"}


async def test_disabled_specialist_does_not_retire_secret(tmp_path, monkeypatch):
    """Green at base and after: a `scope=agent` reload of a DISABLED specialist tears
    the role down (bus consumer, routes) but its certified slot stays, and a
    re-enable re-registers the SAME credential."""
    from bus import MessageBus
    from agent_registry import AgentRegistry
    tree = _Tree(tmp_path, monkeypatch, roles=())
    (tree.agents_dir / "specialists" / "finance").mkdir(parents=True)
    _tolerant_registry_mocks(tree)
    bus = MessageBus()
    tree.runtime.bus = bus
    spec = _webhook()
    tree.registry.register_agent(role="finance", triggers=[spec], channels=[])
    assert _mint([spec], secrets_dir=tree.secrets, role="finance") == []
    before = _files(tree.secrets)
    unlinks = _unlink_spy(monkeypatch)
    retire_calls: list = []
    real_retire = getattr(rts, "retire_for_role", None)
    if real_retire is not None:
        monkeypatch.setattr(rts, "retire_for_role",
                            lambda *a, **kw: (retire_calls.append(a), real_retire(*a, **kw))[1])

    cfg = SimpleNamespace(enabled=False, role="finance", triggers=[spec], channels=[])
    monkeypatch.setattr("agent_loader.load_agent_from_dir", lambda *a, **kw: cfg)
    monkeypatch.setattr("policies.load_policies", lambda *a, **kw: MagicMock())
    stale = MagicMock(); stale.aclose = AsyncMock(); stale.active_plugin_binding = {}
    tree.runtime.agents = {"finance": stale}
    bus.register("finance", AsyncMock())

    res = await tree.reload("agent", role="finance")
    assert res["status"] == "ok", res
    assert "teardown_disabled_specialist" in res["actions"]
    assert retire_calls == [] and unlinks == []
    assert _files(tree.secrets) == before
    assert _routes(tree.registry, "finance") == []

    cfg.enabled = True
    monkeypatch.setattr(reload_mod, "_construct_agent",
                        lambda **kw: MagicMock(handle_message=AsyncMock()))
    monkeypatch.setattr(AgentRegistry, "build", classmethod(lambda c, **kw: MagicMock()))
    res2 = await tree.reload("agent", role="finance")
    assert res2["status"] == "ok", res2
    assert _files(tree.secrets) == before
    app, hbus = await _app(tree.registry, tree.secrets)
    async with TestClient(TestServer(app)) as client:
        assert await _post_static(client, NAME, before[NAME]) == 200
    assert _dispatches(hbus) == 1


async def test_non_retirement_negative_matrix(tmp_path, monkeypatch):
    """Seven arms that must retire nothing; the last (two changed residents →
    exactly two scoped trigger cascades) is red at base, which cascades none."""
    import scheduled_asks
    # (a) provider-owned declared slot: zero writes, zero unlinks
    d = tmp_path / "a"; d.mkdir()
    (d / NAME).write_bytes(b"operator-provider-value")
    prov = _webhook(mode="timestamped_hmac", owner="provider")
    unlinks = _unlink_spy(monkeypatch)
    assert _mint([prov], secrets_dir=d) == []
    assert _retire(ROLE, declared=[prov], staged={NAME}, secrets_dir=d) == (0, [])
    assert _files(d) == {NAME: b"operator-provider-value"} and unlinks == []
    # (b) owner flip casa → provider keeps the token (documented as unsupported)
    d = tmp_path / "b"; d.mkdir()
    _write_v2(NAME, b"c" * 43, ROLE, d)
    assert _retire(ROLE, declared=[prov], staged={NAME}, secrets_dir=d) == (0, [])
    assert _files(d)[NAME] == b"c" * 43
    # (c) beta registering an unrelated name leaves alpha's certified slot alone
    d = tmp_path / "c"; d.mkdir()
    _write_v2(NAME, b"c" * 43, ROLE, d)
    assert _retire("butler", declared=[_webhook("window")], staged={"window"},
                   secrets_dir=d) == (0, [])
    assert set(_files(d)) == {NAME, f"{NAME}.mint"}
    # (d) unreadable agents_dir at boot: zero directory-loss retirements
    d = tmp_path / "d"; d.mkdir()
    _write_v2(NAME, b"c" * 43, "ghost", d)
    not_a_dir = tmp_path / "agents-file"; not_a_dir.write_bytes(b"")
    res = await _boot(role_configs={}, secrets_dir=d, agents_dir=not_a_dir)
    assert res["retired"] == [] and set(_files(d)) == {NAME, f"{NAME}.mint"}
    # (e) invalid later trigger → reregister fails → zero retirements
    tree = _Tree(tmp_path, monkeypatch)
    await _establish(tree)
    before = _files(tree.secrets)
    tree.declare([_entry("porch"), _bad_cron()])
    res = await tree.reload("triggers")
    assert res["status"] == "error" and res["kind"] == "reregister_failed", res
    assert _files(tree.secrets) == before
    # (f) unchanged config sync → zero trigger cascades, zero revocations
    import config_sync
    _tolerant_registry_mocks(tree)
    _known_resident(tree)
    tree.declare([_entry()])
    tree.runtime.defaults_dir = str(tmp_path / "defaults")
    tree.runtime.data_dir = str(tmp_path / "data")
    monkeypatch.setitem(reload_mod._HANDLERS, "policies", AsyncMock(return_value=[]))
    monkeypatch.setattr(config_sync, "run", lambda **kw: 0)
    revoked: list = []
    real_revoke = scheduled_asks.revoke_role
    monkeypatch.setattr(scheduled_asks, "revoke_role",
                        lambda role, reason="trigger_changed": (revoked.append(role), real_revoke(role, reason))[1])
    cascaded: list[str] = []
    real_triggers = reload_mod._HANDLERS["triggers"]

    async def spy_triggers(runtime, *, role=None):
        cascaded.append(role)
        return await real_triggers(runtime, role=role)
    monkeypatch.setitem(reload_mod._HANDLERS, "triggers", spy_triggers)
    res = await tree.reload("config_sync", role=None)
    assert res["status"] == "ok" and cascaded == [] and revoked == [], (res, cascaded, revoked)
    # (g) two changed residents → exactly two role-scoped cascades
    for r in ("butler", "concierge"):
        _seed_resident(tree.agents_dir, role=r)
        tree.declare([_interval("hb")], role=r)
        _known_resident(tree, r)

    def run_changing_two(**kw):
        tree.declare([_interval("hb"), _interval("extra")], role="butler")
        tree.declare([_entry(), _interval("extra")], role=ROLE)
        return 0
    monkeypatch.setattr(config_sync, "run", run_changing_two)
    res = await tree.reload("config_sync", role=None)
    assert res["status"] == "ok", res
    assert sorted(cascaded) == [ROLE, "butler"], cascaded
    assert sorted(revoked) == [ROLE, "butler"], revoked


# ---------------------------------------------------------------------------
# 8. The cross-role handoff — alpha-first converges; beta-first refuses, then converges
# ---------------------------------------------------------------------------


def _hook_barrier(monkeypatch, *, role: str):
    """Block inside `role`'s retirement hook (the worker thread) until released.
    At base nothing calls the hook, so `entered` never fires."""
    entered = threading.Event()
    release = threading.Event()
    real = getattr(rts, "retire_for_role", None)

    def blocking(r, **kw):
        out = real(r, **kw) if real is not None else None
        if r == role:
            entered.set()
            release.wait(timeout=10)
        return out
    monkeypatch.setattr(rts, "retire_for_role", blocking, raising=False)
    return entered, release


async def test_alpha_first_cross_role_handoff_converges(tmp_path, monkeypatch):
    """Red at base: no lifecycle lock and no retirement — beta either collides with
    alpha's still-live route or inherits alpha's role-unbound bytes."""
    tree = _Tree(tmp_path, monkeypatch, roles=(ROLE, "butler"))
    old = await _establish(tree)
    tree.declare([])                      # alpha drops door
    tree.declare([_entry()], role="butler")   # beta adds it
    entered, release = _hook_barrier(monkeypatch, role=ROLE)

    alpha = asyncio.ensure_future(tree.reload("triggers", role=ROLE))
    await wait_until(lambda: entered.is_set() or alpha.done())
    beta = asyncio.ensure_future(tree.reload("triggers", role="butler"))
    release.set()
    ra, rb = await asyncio.gather(alpha, beta)

    assert (ra["status"], rb["status"]) == ("ok", "ok"), (ra, rb)
    files = _files(tree.secrets)
    assert set(files) == {NAME, f"{NAME}.mint"}, sorted(files)
    assert _receipt_role(NAME, tree.secrets) == "butler"
    assert _routes(tree.registry, "butler") == [NAME] and _routes(tree.registry, ROLE) == []
    app, bus = await _app(tree.registry, tree.secrets)
    async with TestClient(TestServer(app)) as client:
        assert await _post_static(client, NAME, files[NAME]) == 200
        assert await _post_static(client, NAME, old) == 401
    assert _dispatches(bus) == 1


async def test_beta_first_handoff_fails_loudly_then_retry_converges(tmp_path, monkeypatch):
    """Red at base: the collision is loud, but alpha's later pass leaves the slot and
    beta's retry inherits the old bytes."""
    tree = _Tree(tmp_path, monkeypatch, roles=(ROLE, "butler"))
    old = await _establish(tree)
    before = _files(tree.secrets)
    tree.declare([_entry()], role="butler")

    rb = await tree.reload("triggers", role="butler")
    assert rb["status"] == "error" and rb["kind"] == "reregister_failed", rb
    assert NAME in rb["message"] and ROLE in rb["message"], rb["message"]
    assert _files(tree.secrets) == before

    tree.declare([])
    ra = await tree.reload("triggers", role=ROLE)
    assert ra["status"] == "ok", ra
    assert _files(tree.secrets) == {}, sorted(_files(tree.secrets))

    rb2 = await tree.reload("triggers", role="butler")
    assert rb2["status"] == "ok", rb2
    files = _files(tree.secrets)
    assert set(files) == {NAME, f"{NAME}.mint"} and _receipt_role(NAME, tree.secrets) == "butler"
    app, bus = await _app(tree.registry, tree.secrets)
    async with TestClient(TestServer(app)) as client:
        assert await _post_static(client, NAME, old) == 401
        assert await _post_static(client, NAME, files[NAME]) == 200
    assert _dispatches(bus) == 1


# ---------------------------------------------------------------------------
# 9. A refused or unreadable retirement aborts the registration; validation first
# ---------------------------------------------------------------------------


def _install_fault(monkeypatch, kind: str):
    """Install one filesystem fault by kind; returns a callable that clears it."""
    active = {"on": True}
    if kind == "every_unlink":
        real = webhook_auth.os

        def unlink(path, *a, **kw):
            if active["on"]:
                raise OSError(13, "Permission denied")
            return real.unlink(path, *a, **kw)
        _shim(monkeypatch, webhook_auth, unlink=unlink)
    elif kind == "live_unlink":
        real = webhook_auth.os

        def unlink(path, *a, **kw):
            if active["on"] and os.path.basename(str(path)) == NAME:
                raise OSError(13, "Permission denied")
            return real.unlink(path, *a, **kw)
        _shim(monkeypatch, webhook_auth, unlink=unlink)
    elif kind == "enumeration":
        if not hasattr(rts, "os"):        # the base never enumerates the inventory
            return lambda: None
        real = rts.os

        def listdir(path, *a, **kw):
            if active["on"]:
                raise OSError(5, "I/O error")
            return real.listdir(path, *a, **kw)
        _shim(monkeypatch, rts, listdir=listdir)
    elif kind == "live_read":
        real = webhook_auth.os

        def open_(path, *a, **kw):
            if active["on"] and os.path.basename(str(path)) == NAME:
                raise OSError(5, "I/O error")
            return real.open(path, *a, **kw)
        _shim(monkeypatch, webhook_auth, open=open_)
    else:
        raise ValueError(kind)
    return lambda: active.update(on=False)


@pytest.mark.parametrize("kind", ["every_unlink", "live_unlink", "enumeration", "live_read"])
async def test_retirement_fault_matrix(kind, tmp_path, monkeypatch):
    """Red at base: no retirement consults the filesystem, so the reload succeeds
    and republishes routes while the faulted artifacts survive."""
    tree = _Tree(tmp_path, monkeypatch)
    await _establish(tree)
    before = _files(tree.secrets)
    tree.declare([_interval("hb")])
    clear = _install_fault(monkeypatch, kind)

    res = await tree.reload("triggers")
    assert res["status"] == "error" and res["kind"] == "reregister_failed", res
    assert (NAME in res["message"]) if kind != "enumeration" else ("inventory" in res["message"]), res["message"]
    assert _routes(tree.registry, ROLE) == []
    assert tree.registry._seen_job_ids == set()
    assert _files(tree.secrets) == before

    clear()
    res2 = await tree.reload("triggers")
    assert res2["status"] == "ok", res2
    assert _retired_count(res2["actions"]) == 1
    assert _files(tree.secrets) == {}


async def test_malformed_receipt_under_dropped_name_is_left_alone(tmp_path, monkeypatch):
    """Uncertified evidence is skipped, never a fault: registration succeeds, zero unlinks."""
    tree = _Tree(tmp_path, monkeypatch)
    await _establish(tree)
    (tree.secrets / f"{NAME}.mint").write_bytes(b"{not json")
    before = _files(tree.secrets)
    unlinks = _unlink_spy(monkeypatch)
    tree.declare([])
    res = await tree.reload("triggers")
    assert res["status"] == "ok", res
    assert unlinks == [] and _files(tree.secrets) == before


async def test_live_before_receipt_retirement_order(tmp_path, monkeypatch):
    """Red at base: no unlink is attempted and the registration succeeds."""
    tree = _Tree(tmp_path, monkeypatch)
    await _establish(tree)
    before = _files(tree.secrets)
    clear = _install_fault(monkeypatch, "live_unlink")
    unlinks = _unlink_spy(monkeypatch)
    tree.declare([])

    res = await tree.reload("triggers")
    assert res["status"] == "error", res
    assert f"{NAME}.mint" not in unlinks, unlinks
    assert _files(tree.secrets) == before
    assert _provenance(NAME, tree.secrets) == "casa_minted"
    assert _routes(tree.registry, ROLE) == []

    clear()
    res2 = await tree.reload("triggers")
    assert res2["status"] == "ok" and _retired_count(res2["actions"]) == 1
    assert _files(tree.secrets) == {}


async def test_validation_pass_installs_nothing(tmp_path, monkeypatch):
    """Red at base: `register_agent` installs `assistant:good` before it meets the bad
    cron, then removes it — one add_job, one remove_job of the new job."""
    tree = _Tree(tmp_path, monkeypatch)
    await _establish(tree)
    before = _files(tree.secrets)
    sched = tree.registry._scheduler
    sched.add_job.reset_mock(); sched.remove_job.reset_mock()
    tree.declare([_interval("good"), _bad_cron()])      # and door dropped

    res = await tree.reload("triggers")
    assert res["status"] == "error" and res["kind"] == "reregister_failed", res
    assert _retired_count(res["actions"]) == 0 and _files(tree.secrets) == before
    assert sched.add_job.call_count == 0, sched.add_job.call_args_list
    assert not any(c.args[:1] == ("assistant:good",) for c in sched.remove_job.call_args_list)
    assert _routes(tree.registry, ROLE) == [] and tree.registry._seen_job_ids == set()


async def test_inventory_fault_aborts_but_redeclared_name_keeps_j1_bytes(tmp_path, monkeypatch):
    """Red at base: the inventory is never read, so the first reload succeeds."""
    tree = _Tree(tmp_path, monkeypatch)
    old = await _establish(tree)
    before = _files(tree.secrets)
    clear = _install_fault(monkeypatch, "enumeration")
    res = await tree.reload("triggers")                  # door still declared
    assert res["status"] == "error" and res["kind"] == "reregister_failed", res
    assert _routes(tree.registry, ROLE) == [] and _files(tree.secrets) == before

    clear()
    res2 = await tree.reload("triggers")
    assert res2["status"] == "ok" and _retired_count(res2["actions"]) == 0
    assert _files(tree.secrets) == before
    app, bus = await _app(tree.registry, tree.secrets)
    async with TestClient(TestServer(app)) as client:
        assert await _post_static(client, NAME, old) == 200
    assert _dispatches(bus) == 1


async def test_error_envelope_names_refused_slot(tmp_path, monkeypatch):
    tree = _Tree(tmp_path, monkeypatch)
    await _establish(tree)
    _install_fault(monkeypatch, "every_unlink")
    tree.declare([])
    res = await tree.reload("triggers")
    assert res["status"] == "error", res
    assert res["actions"] == [] and res["kind"] == "reregister_failed"
    assert res["message"].count(NAME) == 1, res["message"]


# ---------------------------------------------------------------------------
# 10-11. Nothing is published, and no job exists, before the hook has succeeded
# ---------------------------------------------------------------------------


async def test_hook_barrier_keeps_route_unpublished(tmp_path, monkeypatch):
    """Red at base: there is no hook phase — the reload completes, publishes the
    route, and the surviving old token is accepted."""
    tree = _Tree(tmp_path, monkeypatch, roles=(ROLE, "butler"))
    old = await _establish(tree)
    tree.registry.reregister_for(ROLE, [], [])          # alpha unrouted; slot stays
    tree.declare([_entry()], role="butler")
    entered, release = _hook_barrier(monkeypatch, role="butler")
    app, bus = await _app(tree.registry, tree.secrets)

    task = asyncio.ensure_future(tree.reload("triggers", role="butler"))
    await wait_until(lambda: entered.is_set() or task.done())
    try:
        assert (int(entered.is_set()), int(task.done())) == (1, 0)
        files = _files(tree.secrets)
        assert NAME not in files and f"{NAME}.mint" not in files, sorted(files)
        async with TestClient(TestServer(app)) as client:
            assert await _post_static(client, NAME, old) == 404
        assert _dispatches(bus) == 0
    finally:
        release.set()
    res = await task
    assert res["status"] == "ok", res


async def test_no_scheduled_job_exists_before_the_hook_succeeds(tmp_path, monkeypatch):
    """S33. Red at base: no hook phase; the date job is installed and the route
    published by a reload that then reports `ok`."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    sched = AsyncIOScheduler(timezone=timezone.utc)
    sched.start(paused=True)
    try:
        tree = _Tree(tmp_path, monkeypatch, scheduler=sched)
        await _establish(tree)
        at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        tree.declare([{"name": "soon", "type": "date", "at": at, "one_shot": True,
                       "channel": "telegram", "prompt": "now"}])   # door dropped
        entered, release = _hook_barrier(monkeypatch, role=ROLE)
        _install_fault(monkeypatch, "every_unlink")                # the hook will refuse

        task = asyncio.ensure_future(tree.reload("triggers"))
        await wait_until(lambda: entered.is_set() or task.done())
        try:
            assert (int(entered.is_set()), int(task.done())) == (1, 0)
            assert sched.get_jobs() == []
        finally:
            release.set()
        res = await task
        assert res["status"] == "error" and res["kind"] == "reregister_failed", res
        assert sched.get_jobs() == [] and _routes(tree.registry, ROLE) == []
    finally:
        sched.shutdown(wait=False)


# ---------------------------------------------------------------------------
# 12. config_sync: a changed trigger file re-registers that resident, even when
#     the before-hash could not be read
# ---------------------------------------------------------------------------


async def test_config_sync_unreadable_before_hash_cascades_changed_role(tmp_path, monkeypatch):
    """Red at base: `reload_config_sync` cascades only `agents` and `policies`; the
    dropped route and its credential survive."""
    import config_sync
    import scheduled_asks
    tree = _Tree(tmp_path, monkeypatch)
    _tolerant_registry_mocks(tree)
    _known_resident(tree)
    await _establish(tree)
    tree.runtime.defaults_dir = str(tmp_path / "defaults")
    tree.runtime.data_dir = str(tmp_path / "data")
    monkeypatch.setitem(reload_mod._HANDLERS, "policies", AsyncMock(return_value=[]))
    runs: list[int] = []

    def fake_run(**kw):
        runs.append(1)
        _sync_drop_door(tree, tmp_path)
        return 0
    monkeypatch.setattr(config_sync, "run", fake_run)
    target = str(Path(tree.triggers_path()))
    real_os = reload_mod.os
    faults = {"left": 1}

    def open_once_faulting(path, *a, **kw):
        if str(path) == target and faults["left"]:
            faults["left"] -= 1
            raise OSError(5, "I/O error")
        return real_os.open(path, *a, **kw)
    _shim(monkeypatch, reload_mod, open=open_once_faulting)
    revoked: list[str] = []
    real_revoke = scheduled_asks.revoke_role
    monkeypatch.setattr(scheduled_asks, "revoke_role",
                        lambda role, reason="trigger_changed": (revoked.append(role), real_revoke(role, reason))[1])
    cascaded: list[str] = []
    real_triggers = reload_mod._HANDLERS["triggers"]

    async def spy_triggers(runtime, *, role=None):
        cascaded.append(role)
        return await real_triggers(runtime, role=role)
    monkeypatch.setitem(reload_mod._HANDLERS, "triggers", spy_triggers)

    res = await tree.reload("config_sync", role=None)
    assert res["status"] == "ok", res
    assert runs == [1] and cascaded == [ROLE] and revoked == [ROLE], (cascaded, revoked)
    assert tree.registry.get_webhook_target(NAME) is None
    assert _files(tree.secrets) == {}, sorted(_files(tree.secrets))
    assert _retired_count(res["actions"]) == 1, res["actions"]


# ---------------------------------------------------------------------------
# 13-14. One route record per request and per report row
# ---------------------------------------------------------------------------


class _TearingRegistry(TriggerRegistry):
    """A real registry that TRANSFERS `door` from `assistant` to `butler` right
    after the first route read — the interleaving a concurrent re-registration
    produces. The transfer runs from whichever read the consumer performs first:
    the legacy getters (base) or `webhook_route` (the atomic record)."""

    def __init__(self, *a, transfer_on: str, **kw):
        super().__init__(*a, **kw)
        self._transfer_on = transfer_on
        self.transfers = 0
        self.route_reads = 0

    def _transfer(self):
        if self.transfers:
            return
        self.transfers += 1
        self.reregister_for(ROLE, [], [])
        self.register_agent(role="butler", triggers=[TriggerSpec(
            name=NAME, type="webhook", clearance="family",
            auth={"mode": "hmac_body", "header": "X-Webhook-Signature",
                  "tolerance_secs": 300, "secret_owner": "casa"})], channels=[])

    def get_webhook_target(self, name):
        r = super().get_webhook_target(name)
        if self._transfer_on == "target":
            self._transfer()
        return r

    def get_auth_policy(self, name):
        r = super().get_auth_policy(name)
        if self._transfer_on == "policy":
            self._transfer()
        return r

    def webhook_route(self, name):
        rec = super().webhook_route(name)
        self.route_reads += 1
        self._transfer()
        return rec


async def test_webhook_handler_uses_one_atomic_route_snapshot(tmp_path, monkeypatch):
    """Red at base (reproduced by review): target read as `assistant`, clearance
    read later as `family` — one message stamped from two routes."""
    import hmac as _hmac
    reg = _TearingRegistry(scheduler=MagicMock(), app=web.Application(), bus=None,
                           transfer_on="target")
    reg.register_agent(role=ROLE, triggers=[TriggerSpec(
        name=NAME, type="webhook", clearance="public",
        auth={"mode": "hmac_body", "header": "X-Webhook-Signature",
              "tolerance_secs": 300, "secret_owner": "casa"})], channels=[])
    secret = "s3cret"
    app, bus = await _app(reg, tmp_path / "secrets", secret=secret)
    body = b'{"x": 1}'
    async with TestClient(TestServer(app)) as client:
        r = await client.post(f"/webhook/{NAME}", data=body, headers={
            "X-Webhook-Signature": _hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()})
    assert r.status == 200
    assert _dispatches(bus) == 1
    msg = bus.send.await_args.args[0]
    assert (msg.target, msg.trusted_user_origin.server_origin.clearance, msg.context["_origin_clearance"]) == \
        (ROLE, "public", "public")
    assert reg.get_webhook_target(NAME) == "butler"


async def test_misrouted_nested_probe_uses_effective_route_role(tmp_path, monkeypatch):
    """Red at base: the nested probe reports `readable` for bytes the request refuses."""
    secrets = tmp_path / "secrets"
    reg = _registry()
    reg.register_agent(role="butler", triggers=[_webhook()], channels=[])
    _write_v2(NAME, b"a" * 43, ROLE, secrets)          # certified for the DECLARING role
    rows = _rows([_webhook()], reg, ROLE, secrets)
    assert list(rows) == [NAME]
    row = rows[NAME]
    assert row["state"] == "misrouted" and row["effective"]["role"] == "butler", row
    assert row["probe"]["state"] == "unproven_blocked", row["probe"]
    app, bus = await _app(reg, secrets)
    async with TestClient(TestServer(app)) as client:
        assert await _post_static(client, NAME, b"a" * 43) == 401
    assert _dispatches(bus) == 0


async def test_snapshot_rows_reads_one_route_record(tmp_path, monkeypatch):
    """Red at base: target, policy and clearance are three getter reads, and a
    re-registration between them yields a record no route ever had."""
    reg = _TearingRegistry(scheduler=MagicMock(), app=web.Application(), bus=None,
                           transfer_on="policy")
    reg.register_agent(role=ROLE, triggers=[_webhook()], channels=[])
    snap = rts.snapshot_rows(specs=[_webhook()], registry=reg, role=ROLE,
                             global_secret_usable=True)
    assert len(snap) == 1
    eff = snap[0]["effective"] if "effective" in snap[0] else None
    alpha_record = {"mode": "static_header", "header": HEADER, "tolerance_secs": 300,
                    "secret_owner": "casa", "clearance": "public", "role": ROLE}
    beta_record = {"mode": "hmac_body", "header": "X-Webhook-Signature", "tolerance_secs": 300,
                   "secret_owner": "casa", "clearance": "family", "role": "butler"}
    assert eff in (alpha_record, beta_record) or eff is None and snap[0].get("state") is None, snap
    assert reg.route_reads == 1, reg.route_reads


# ---------------------------------------------------------------------------
# Path 9 — a directory eviction retires nothing; the next boot does
# ---------------------------------------------------------------------------


async def test_directory_eviction_does_not_retire_until_later_event(tmp_path, monkeypatch):
    """The in-process half is green at base; the boot half is red (no directory-gone
    sweep exists there)."""
    import shutil
    from bus import MessageBus
    tree = _Tree(tmp_path, monkeypatch)
    _tolerant_registry_mocks(tree)
    _patch_construction(monkeypatch)
    old = await _establish(tree)
    _known_resident(tree)
    bus = MessageBus()
    tree.runtime.bus = bus
    bus.register(ROLE, AsyncMock())
    agent = MagicMock(); agent.aclose = AsyncMock()
    tree.runtime.agents = {ROLE: agent}
    before = _files(tree.secrets)

    shutil.rmtree(tree.dir())
    res = await tree.reload("agents", role=None)
    assert res["status"] == "ok" and f"evicted_{ROLE}" in res["actions"], res
    assert _retired_count(res["actions"]) == 0
    assert _files(tree.secrets) == before
    assert _routes(tree.registry, ROLE) == []
    app, hbus = await _app(tree.registry, tree.secrets)
    async with TestClient(TestServer(app)) as client:
        assert await _post_static(client, NAME, old) == 404

    _seed_resident(tree.agents_dir, role=ROLE)
    tree.declare([_entry()])
    res2 = await tree.reload("agents", role=None)
    assert res2["status"] == "ok" and f"added_{ROLE}" in res2["actions"], res2
    assert _files(tree.secrets) == before
    async with TestClient(TestServer(app)) as client:
        assert await _post_static(client, NAME, old) == 200

    shutil.rmtree(tree.dir())
    boot = await _boot(role_configs={}, secrets_dir=tree.secrets, agents_dir=tree.agents_dir)
    assert boot["retired"] == [NAME], boot
    assert _files(tree.secrets) == {}


# ---------------------------------------------------------------------------
# S8 / S10 / S11 — successors, provider and hmac successors, one winner per name
# ---------------------------------------------------------------------------


async def test_recovered_alpha_refusal_cannot_delete_beta_successor(tmp_path, monkeypatch):
    """Red at base: alpha's refusal is never consulted, and beta reuses the bytes."""
    tree = _Tree(tmp_path, monkeypatch, roles=(ROLE, "butler"))
    await _establish(tree)
    tree.declare([])
    clear = _install_fault(monkeypatch, "every_unlink")
    ra = await tree.reload("triggers", role=ROLE)
    assert ra["status"] == "error" and _routes(tree.registry, ROLE) == [], ra
    clear()

    tree.declare([_entry()], role="butler")
    rb = await tree.reload("triggers", role="butler")
    assert rb["status"] == "ok" and _retired_count(rb["actions"]) == 1, rb
    files = _files(tree.secrets)
    assert set(files) == {NAME, f"{NAME}.mint"} and _receipt_role(NAME, tree.secrets) == "butler"

    ra2 = await tree.reload("triggers", role=ROLE)
    assert ra2["status"] == "ok" and _retired_count(ra2["actions"]) == 0, ra2
    assert _files(tree.secrets) == files
    app, bus = await _app(tree.registry, tree.secrets)
    async with TestClient(TestServer(app)) as client:
        assert await _post_static(client, NAME, files[NAME]) == 200
    assert _dispatches(bus) == 1


@pytest.mark.parametrize("successor", ["provider", "hmac_body"])
async def test_cross_role_handoff_to_provider_and_hmac_retires_predecessor(
        successor, tmp_path, monkeypatch):
    """Red at base: the predecessor's slot stays; a provider successor authenticates
    with it (the provider rule accepts a 43-byte casa token)."""
    tree = _Tree(tmp_path, monkeypatch, roles=(ROLE, "butler"))
    old = await _establish(tree)
    tree.registry.reregister_for(ROLE, [], [])           # unrouted, slot left behind
    entry = (_entry(mode="timestamped_hmac", owner="provider") if successor == "provider"
             else _entry(mode="hmac_body"))
    tree.declare([entry], role="butler")
    mints = _mint_spy(monkeypatch)

    res = await tree.reload("triggers", role="butler")
    assert res["status"] == "ok", res
    assert _retired_count(res["actions"]) == 1 and mints == [], (res["actions"], mints)
    assert _files(tree.secrets) == {}, sorted(_files(tree.secrets))
    app, bus = await _app(tree.registry, tree.secrets, secret="g")
    async with TestClient(TestServer(app)) as client:
        assert await _post_static(client, NAME, old) == 401
    assert _dispatches(bus) == 0
    if successor == "provider":
        spec = _webhook(mode="timestamped_hmac", owner="provider")
        rows = _rows([spec], tree.registry, "butler", tree.secrets)
        assert rows[NAME]["state"] == "awaiting_import", rows[NAME]


class _BarrierDict(dict):
    """A live route map whose `get(NAME)` parks the first two callers on a barrier:
    at base both registrations pass the check-then-assign window together."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.barrier = threading.Barrier(2, timeout=2.5)
        self.armed = True

    def get(self, key, default=None):
        if self.armed and key == NAME and threading.current_thread() is not threading.main_thread():
            try:
                self.barrier.wait()
            except threading.BrokenBarrierError:
                pass
            self.armed = False
        return super().get(key, default)


async def test_concurrent_roles_one_name_has_one_winner(tmp_path, monkeypatch):
    """Red at base: both roles pass the collision read before either assigns, so
    both registrations succeed and the route map holds one owner over two
    role bookkeeping entries."""
    tree = _Tree(tmp_path, monkeypatch, roles=(ROLE, "butler"))
    attr = "_webhook_routes" if hasattr(tree.registry, "_webhook_routes") else "_webhook_targets"
    setattr(tree.registry, attr, _BarrierDict(getattr(tree.registry, attr)))
    tree.declare([_entry()], role=ROLE)
    tree.declare([_entry()], role="butler")

    ra, rb = await asyncio.gather(tree.reload("triggers", role=ROLE),
                                  tree.reload("triggers", role="butler"))
    statuses = sorted((ra["status"], rb["status"]))
    assert statuses == ["error", "ok"], (ra, rb)
    winner = ROLE if ra["status"] == "ok" else "butler"
    assert tree.registry.get_webhook_target(NAME) == winner
    assert _routes(tree.registry, ROLE) + _routes(tree.registry, "butler") == [NAME]
    files = _files(tree.secrets)
    assert set(files) == {NAME, f"{NAME}.mint"} and _receipt_role(NAME, tree.secrets) == winner


async def test_same_role_agent_and_trigger_reload_agree(tmp_path, monkeypatch):
    """Red at base: the two reloads of one role are not one route lifecycle, so
    routing and slot state can come from different declarations."""
    tree = _Tree(tmp_path, monkeypatch)
    _tolerant_registry_mocks(tree)
    _patch_construction(monkeypatch)
    await _establish(tree)
    _known_resident(tree)
    for order in ("agent_first", "triggers_first"):
        tree.declare([_entry()])
        if order == "agent_first":
            await tree.reload("agent")
        a = asyncio.ensure_future(tree.reload("agent"))
        tree.declare([])            # the agent pass may read either declaration
        t = asyncio.ensure_future(tree.reload("triggers"))
        await asyncio.gather(a, t)
        routes = _routes(tree.registry, ROLE)
        files = _files(tree.secrets)
        if routes == [NAME]:
            assert set(files) == {NAME, f"{NAME}.mint"} and _receipt_role(NAME, tree.secrets) == ROLE, (order, files)
        else:
            assert routes == [] and files == {}, (order, routes, sorted(files))


# ---------------------------------------------------------------------------
# Residual characterisations — the STATED limits of INV-TRIG-016, pinned as such
# ---------------------------------------------------------------------------


async def test_residual_s15_unproven_provider_successor_inherits(tmp_path, monkeypatch):
    """Green at base and after: an UNPROVEN value under a name another role once
    routed is inherited by a provider-owned successor — Casa cannot attribute an
    uncertified value; the cure is a provider import binding (#621)."""
    import hmac as _hmac, time as _time
    secrets = tmp_path / "secrets"; secrets.mkdir()
    (secrets / NAME).write_bytes(CASA_TOKEN)
    reg = _registry()
    reg.register_agent(role=ROLE, triggers=[_webhook()], channels=[])
    reg.reregister_for(ROLE, [], [])
    prov = _webhook(mode="timestamped_hmac", owner="provider")
    unlinks = _unlink_spy(monkeypatch)
    assert _retire("butler", declared=[prov], staged={NAME}, secrets_dir=secrets) == (0, [])
    assert _mint([prov], secrets_dir=secrets, role="butler") == []
    reg.register_agent(role="butler", triggers=[prov], channels=[])
    assert unlinks == [] and _files(secrets) == {NAME: CASA_TOKEN}
    app, bus = await _app(reg, secrets)
    t = int(_time.time())
    sig = _hmac.new(CASA_TOKEN, f"{t}.".encode() + b"{}", hashlib.sha256).hexdigest()
    async with TestClient(TestServer(app)) as client:
        r = await client.post(f"/webhook/{NAME}", data=b"{}", headers={HEADER: f"t={t},v0={sig}"})
    assert r.status == 200 and _dispatches(bus) == 1
    row = _rows([prov], reg, "butler", secrets)[NAME]
    assert (row["state"], row["owner"], row.get("provenance")) == ("readable", "provider", "unproven")


async def _two_certified(tree: _Tree, names=(NAME, "window")) -> dict[str, bytes]:
    tree.declare([_entry(n) for n in names])
    res = await tree.reload("triggers")
    assert res["status"] == "ok", res
    files = _files(tree.secrets)
    assert set(files) == {x for n in names for x in (n, f"{n}.mint")}
    return files


def _refuse_unlink_of(monkeypatch, name: str):
    real = webhook_auth.os

    def unlink(path, *a, **kw):
        if os.path.basename(str(path)) == name:
            raise OSError(13, "Permission denied")
        return real.unlink(path, *a, **kw)
    _shim(monkeypatch, webhook_auth, unlink=unlink)


async def test_residual_s21_dropped_name_may_retire_before_later_refusal(tmp_path, monkeypatch):
    """Red at base (nothing is retired); pins the stated arm-(a) residual."""
    tree = _Tree(tmp_path, monkeypatch)
    before = await _two_certified(tree)
    _refuse_unlink_of(monkeypatch, "window")
    tree.declare([])
    res = await tree.reload("triggers")
    assert res["status"] == "error", res
    assert _routes(tree.registry, ROLE) == []
    files = _files(tree.secrets)
    assert NAME not in files and f"{NAME}.mint" not in files, sorted(files)
    assert files == {k: v for k, v in before.items() if k.startswith("window")}


async def test_residual_s31_hmac_flip_may_retire_before_later_refusal(tmp_path, monkeypatch):
    tree = _Tree(tmp_path, monkeypatch)
    before = await _two_certified(tree)
    _refuse_unlink_of(monkeypatch, "window")
    tree.declare([_entry(mode="hmac_body")])          # door un-backed, window dropped
    res = await tree.reload("triggers")
    assert res["status"] == "error", res
    files = _files(tree.secrets)
    assert NAME not in files and f"{NAME}.mint" not in files, sorted(files)
    assert files == {k: v for k, v in before.items() if k.startswith("window")}
    assert _routes(tree.registry, ROLE) == []


async def test_residual_s24_arm_a_refusal_prevents_arm_b(tmp_path, monkeypatch):
    """Arm (a)'s refusal aborts before arm (b) starts: alpha's slot is untouched."""
    tree = _Tree(tmp_path, monkeypatch, roles=(ROLE, "butler"))
    await _establish(tree)                                   # alpha's door
    tree.registry.reregister_for(ROLE, [], [])
    tree.declare([_entry("window")], role="butler")
    rb = await tree.reload("triggers", role="butler")        # butler's window, certified
    assert rb["status"] == "ok", rb
    before = _files(tree.secrets)
    _refuse_unlink_of(monkeypatch, "window")
    tree.declare([_entry()], role="butler")                  # drops window, stages door
    res = await tree.reload("triggers", role="butler")
    assert res["status"] == "error", res
    assert _routes(tree.registry, "butler") == []
    assert _files(tree.secrets) == before


async def test_residual_s24_arm_b_partial_retirement(tmp_path, monkeypatch):
    """Red at base: no predecessor is retired. Pins the complete arm-(b) residual."""
    tree = _Tree(tmp_path, monkeypatch, roles=(ROLE, "butler", "concierge"))
    await _establish(tree)
    tree.registry.reregister_for(ROLE, [], [])
    tree.declare([_entry("window")], role="concierge")
    assert (await tree.reload("triggers", role="concierge"))["status"] == "ok"
    tree.registry.reregister_for("concierge", [], [])
    before = _files(tree.secrets)
    _refuse_unlink_of(monkeypatch, "window")
    tree.declare([_entry(), _entry("window")], role="butler")
    res = await tree.reload("triggers", role="butler")
    assert res["status"] == "error", res
    assert _routes(tree.registry, "butler") == []
    files = _files(tree.secrets)
    assert NAME not in files and f"{NAME}.mint" not in files, sorted(files)
    assert files == {k: v for k, v in before.items() if k.startswith("window")}


async def test_residual_s27_abort_after_hook_keeps_completed_cross_role_retirements(tmp_path, monkeypatch):
    """Red at base: the install raises before any retirement exists."""
    tree = _Tree(tmp_path, monkeypatch, roles=(ROLE, "butler", "concierge"))
    await _establish(tree)
    tree.registry.reregister_for(ROLE, [], [])
    tree.declare([_entry("window")], role="concierge")
    assert (await tree.reload("triggers", role="concierge"))["status"] == "ok"
    tree.registry.reregister_for("concierge", [], [])
    assert len(_files(tree.secrets)) == 4
    tree.declare([_entry(), _entry("window")], role="butler")
    monkeypatch.setattr(TriggerRegistry, "register_agent",
                        lambda self, *a, **kw: (_ for _ in ()).throw(RuntimeError("install fault")))
    res = await tree.reload("triggers", role="butler")
    assert res["status"] == "error" and res["kind"] == "reregister_failed", res
    assert _routes(tree.registry, "butler") == []
    assert _files(tree.secrets) == {}, sorted(_files(tree.secrets))


async def test_residual_s28_boot_read_fault_leaves_directory_gone_slot(tmp_path, monkeypatch):
    secrets = tmp_path / "secrets"
    _write_v2(NAME, b"c" * 43, "ghost", secrets)
    before = _files(secrets)
    _install_fault(monkeypatch, "live_read")
    res = await _boot(role_configs={}, secrets_dir=secrets, agents_dir=tmp_path / "agents")
    assert res["retired"] == [] and res["failed"] == [NAME], res
    assert _files(secrets) == before


async def test_residual_s32_boot_inventory_fault_skips_whole_sweep(tmp_path, monkeypatch):
    secrets = tmp_path / "secrets"
    _write_v2(NAME, b"c" * 43, "ghost", secrets)
    before = _files(secrets)
    _install_fault(monkeypatch, "enumeration")
    res = await _boot(role_configs={}, secrets_dir=secrets, agents_dir=tmp_path / "agents")
    assert res["retired"] == [] and res.get("inventory_unavailable") is True, res
    assert _files(secrets) == before


async def test_residual_s35_absent_secrets_directory_is_empty_inventory(tmp_path, monkeypatch):
    """No secrets directory at all: nothing is unwound; the first mint creates it."""
    tree = _Tree(tmp_path, monkeypatch)
    assert not tree.secrets.exists()
    old = await _establish(tree)
    assert set(_files(tree.secrets)) == {NAME, f"{NAME}.mint"}
    import shutil
    shutil.rmtree(tree.secrets)
    cfg = tree.load_cfg()
    reg = _registry()
    reg.register_agent(role=ROLE, triggers=cfg.triggers, channels=cfg.channels)
    res = await _boot(role_configs={ROLE: cfg}, secrets_dir=tree.secrets,
                      agents_dir=tree.agents_dir)
    assert res["retired"] == [] and res["failed"] == [] and res["mint_failures"] == [], res
    assert _routes(reg, ROLE) == [NAME]
    assert set(_files(tree.secrets)) == {NAME, f"{NAME}.mint"}
    assert (tree.secrets / NAME).read_bytes() != old


async def test_residual_s36_receipt_only_refusal_leaves_orphan_until_next_mint(tmp_path, monkeypatch):
    """Red at base: no live-first retirement happens; the registration succeeds."""
    tree = _Tree(tmp_path, monkeypatch)
    await _establish(tree)
    orphan = (tree.secrets / f"{NAME}.mint").read_bytes()
    _refuse_unlink_of(monkeypatch, f"{NAME}.mint")
    tree.declare([])
    res = await tree.reload("triggers")
    assert res["status"] == "error", res
    assert _files(tree.secrets) == {f"{NAME}.mint": orphan}, sorted(_files(tree.secrets))
    assert _routes(tree.registry, ROLE) == []

    mints = _mint_spy(monkeypatch)
    tree.declare([_entry()])
    res2 = await tree.reload("triggers")
    assert res2["status"] == "ok" and _retired_count(res2["actions"]) == 0, res2
    assert len(mints) == 1
    files = _files(tree.secrets)
    assert set(files) == {NAME, f"{NAME}.mint"} and files[f"{NAME}.mint"] != orphan
    assert _receipt_role(NAME, tree.secrets) == ROLE
    assert _provenance(NAME, tree.secrets) == "casa_minted"
