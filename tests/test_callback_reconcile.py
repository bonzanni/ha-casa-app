"""The authorization-callback reconciler.

``callback_reconcile`` is the ONE writer of the TriggerRegistry *callback*
overlay: it derives the COMPLETE desired overlay (every resolved + assigned +
validly-declared + acked plugin callback), swaps it atomically, maintains the
spool's ``ready.json`` / ``.index`` files with the asymmetric ordering that
keeps the readiness marker from ever being falsely positive, prunes stale acks
and fires consent prompts for callbacks whose ONLY gap is the ack.
"""
import asyncio
import json
import os
from types import SimpleNamespace

import pytest

import callback_reconcile as cr
import callback_spool
from callback_acks import CallbackAckStore
from plugin_callbacks import ack_identity, declaration_digest
import trigger_registry as _treg
from trigger_registry import TriggerRegistry

BASE = "https://casa.example.org"
# Captured before any test patches the seam (the autouse fixture below pins a
# deterministic base for every other test).
_REAL_BASE_URL = cr._base_url


# ---------------------------------------------------------------------------
# fixtures / doubles
# ---------------------------------------------------------------------------


def _manifest(names):
    return {"name": "x", "casa": {"callbacks": [{"name": n} for n in names]}}


def _plugin(name="gmail", artifact_id="art-1", callbacks=("authorize",),
            path=None, manifest=None):
    return SimpleNamespace(
        name=name, artifact_id=artifact_id,
        path=path if path is not None else f"/store/{name}/{artifact_id}",
        version="1.0.0", manifest_name=name,
        manifest=_manifest(callbacks) if manifest is None else manifest)


def _resolver(plugins, *, valid=True, issues=()):
    def resolve(target):
        return SimpleNamespace(registry_valid=valid, plugins=list(plugins),
                               issues=list(issues))
    return resolve


def _entries(*plugins, targets=("resident:assistant",)):
    rows = [{"name": p.name, "artifact_id": p.artifact_id,
             "targets": list(targets)} for p in plugins]

    def provider():
        return rows
    return provider


def _role_configs(**roles):
    return {role: SimpleNamespace(channels=list(channels))
            for role, channels in roles.items()}


def _ack(acks, plugin="gmail", declared="authorize"):
    effective = f"plg-{plugin}--{declared}"
    digest = declaration_digest({"declared": declared, "effective": effective})
    acks.record(plugin=plugin, effective=effective, declaration_digest=digest)
    return ack_identity(plugin, effective, digest)


def _identity(plugin="gmail", declared="authorize"):
    effective = f"plg-{plugin}--{declared}"
    return ack_identity(plugin, effective,
                        declaration_digest({"declared": declared,
                                            "effective": effective}))


class _SpoolStub:
    """Records the ordered file-side call sequence (the ordering contract) AND
    models the durable on-disk marker inventory the reconciler reads back — so
    a later reconcile on the SAME stub instance sees exactly what an earlier one
    published, which is the on-disk truth the paired transaction reconciles
    against (the in-memory previous overlay is no longer consulted). Reuse ONE
    stub across the reconciles of a scenario to exercise the durable path."""

    def __init__(self, calls, *, fail=()):
        self.calls = calls
        self.fail = set(fail)
        self._ready: dict[str, dict] = {}      # plugin -> ready payload
        self._index: dict[str, dict] = {}      # index key -> index payload

    def _rec(self, what, *args):
        self.calls.append((what, *args))
        if what in self.fail:
            raise OSError("synthetic spool failure")

    def ensure_plugin_dirs(self, plugin):
        self._rec("ensure", plugin)

    def write_ready(self, plugin, payload):
        self._rec("ready", plugin, payload)     # raises here if in `fail`
        self._ready[plugin] = payload

    def delete_ready(self, plugin):
        self._rec("del_ready", plugin)          # raises here if in `fail`
        self._ready.pop(plugin, None)
        return True                             # now absent (real-spool contract)

    def write_index_entry(self, artifact_realpath, payload):
        self._rec("index", artifact_realpath, payload)
        self._index[callback_spool.index_key(artifact_realpath)] = payload

    def delete_index_entry(self, artifact_realpath):
        self._rec("del_index", artifact_realpath)
        self._index.pop(callback_spool.index_key(artifact_realpath), None)
        return True

    def delete_index_key(self, key):
        self._rec("del_index_key", key)
        self._index.pop(key, None)
        return True

    def published_plugins(self):
        return sorted(self._ready)

    def index_keys(self):
        return sorted(self._index)

    @staticmethod
    def _marker(payload):
        # Model the durable reader's PRESENT shape: expose the RAW canonical
        # bytes (what a real spool writes) so the byte-strict compare works —
        # a payload the stub stored (== what casa wrote) round-trips unchanged.
        if payload is None:
            return callback_spool.Marker(callback_spool.MarkerState.ABSENT)
        return callback_spool.Marker(
            callback_spool.MarkerState.PRESENT, payload,
            raw=callback_spool.canonical_marker_bytes(payload))

    def read_marker(self, plugin):
        return self._marker(self._ready.get(plugin))

    def read_index_marker(self, artifact_realpath):
        return self._marker(
            self._index.get(callback_spool.index_key(artifact_realpath)))


class _SpyRegistry(TriggerRegistry):
    def __init__(self, calls=None):
        super().__init__(scheduler=None, app=None, bus=None)
        self._calls = calls if calls is not None else []

    def replace_callback_overlay(self, overlay):
        # #606: record the SENTINEL as itself. Coercing it with dict() here
        # would hide from every assertion below the difference between "no
        # authoritative computation" and the authoritative claim "nothing
        # should route" — the exact conflation the sentinel exists to end.
        self._calls.append(("swap", overlay if overlay is _treg.ROUTING_UNAVAILABLE
                            else dict(overlay)))
        super().replace_callback_overlay(overlay)


class _FakeTelegram:
    chat_id = "100"

    def __init__(self):
        self.posts = []

    async def post_dm_keyboard(self, *, chat_id, request_id, text, options):
        self.posts.append((chat_id, request_id, text, tuple(options)))
        return 55

    async def edit_dm_message(self, chat_id, message_id, text):
        return True


class _FakeChannelManager:
    def __init__(self, telegram=None):
        self._telegram = telegram

    def get(self, name):
        return self._telegram if name == "telegram" else None


async def _reconcile(registry, *, plugins, acks, spool, entries=None,
                     role_configs=None, prompt=False, channel_manager=None,
                     resolver=None, base_url=BASE, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setattr(cr, "_base_url", lambda: base_url)
    return await cr.reconcile_plugin_callbacks(
        trigger_registry=registry,
        role_configs=role_configs or _role_configs(assistant=["telegram"]),
        channel_manager=channel_manager, acks=acks, spool=spool,
        resolver=resolver or _resolver(plugins),
        entries=entries or _entries(*plugins), prompt=prompt)


@pytest.fixture(autouse=True)
def _pinned_base(monkeypatch):
    """Every test states its own base explicitly; never read the real env."""
    monkeypatch.setattr(cr, "_base_url", lambda: BASE)


# ---------------------------------------------------------------------------
# the gate matrix
# ---------------------------------------------------------------------------


async def test_valid_assigned_acked_callback_routes(tmp_path):
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    calls: list = []
    issues = await _reconcile(registry, plugins=[p], acks=acks,
                              spool=_SpoolStub(calls))
    assert issues == []
    entry = registry.get_callback("plg-gmail--authorize")
    assert entry is not None
    assert entry["plugin"] == "gmail"
    assert entry["declared"] == "authorize"
    # The effective name is carried in the value (it is also the key) so the
    # callback handler reads the real value rather than a routed-name
    # fallback (callback_http._process).
    assert entry["effective"] == "plg-gmail--authorize"


async def test_invalid_declaration_darks_the_whole_set(tmp_path):
    """An intrinsically invalid declaration (reserved prefix) rejects the
    plugin's WHOLE callback set — the valid, acked sibling routes nothing."""
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin(callbacks=("authorize", "plg-sneaky"))
    issues = await _reconcile(registry, plugins=[p], acks=acks,
                              spool=_SpoolStub([]))
    assert [i.reason_code for i in issues] == ["callback_invalid"]
    assert issues[0].name == "gmail"
    assert issues[0].stage == "callbacks"
    assert registry.get_callback("plg-gmail--authorize") is None


async def test_unassigned_plugin_is_callback_no_target(tmp_path):
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    issues = await _reconcile(registry, plugins=[p], acks=acks,
                              spool=_SpoolStub([]),
                              entries=_entries(p, targets=[]))
    assert [i.reason_code for i in issues] == ["callback_no_target"]
    assert registry.get_callback("plg-gmail--authorize") is None


async def test_executor_only_assignment_is_no_target(tmp_path):
    """The delivery nudge targets a resident or a specialist; an
    executor-only plugin could never collect the code it accepted."""
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    issues = await _reconcile(registry, plugins=[p], acks=acks,
                              spool=_SpoolStub([]),
                              entries=_entries(p, targets=["executor:cron"]))
    assert [i.reason_code for i in issues] == ["callback_no_target"]


async def test_unknown_resident_role_is_no_target(tmp_path):
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    issues = await _reconcile(registry, plugins=[p], acks=acks,
                              spool=_SpoolStub([]),
                              entries=_entries(p, targets=["resident:ghost"]))
    assert [i.reason_code for i in issues] == ["callback_no_target"]


async def test_specialist_assignment_is_a_valid_target(tmp_path):
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    issues = await _reconcile(
        registry, plugins=[p], acks=acks, spool=_SpoolStub([]),
        entries=_entries(p, targets=["specialist:finance"]))
    assert issues == []
    assert registry.get_callback("plg-gmail--authorize") is not None


async def test_unacked_callback_is_pending_and_dark(tmp_path):
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    p = _plugin()
    issues = await _reconcile(registry, plugins=[p], acks=acks,
                              spool=_SpoolStub([]))
    assert [i.reason_code for i in issues] == ["callback_pending_ack"]
    assert registry.get_callback("plg-gmail--authorize") is None


async def test_partial_ack_darks_the_whole_plugin(tmp_path):
    """All-or-nothing per plugin (INV-TRIG-003's callback mirror): one
    un-acked callback keeps the acked sibling dark too."""
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks, declared="authorize")
    p = _plugin(callbacks=("authorize", "renew"))
    issues = await _reconcile(registry, plugins=[p], acks=acks,
                              spool=_SpoolStub([]))
    assert [i.reason_code for i in issues] == ["callback_pending_ack"]
    assert registry.get_callback("plg-gmail--authorize") is None
    assert registry.get_callback("plg-gmail--renew") is None


async def test_gate_order_no_target_outranks_pending_ack(tmp_path):
    """An unassigned plugin reports the ASSIGNMENT gap, never a consent
    prompt — approving a callback that still could not route is a broken
    promise (the non-consent gap suppresses the pending list)."""
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    p = _plugin()
    desired = cr.compute_desired(
        role_configs=_role_configs(assistant=["telegram"]), acks=acks,
        resolver=_resolver([p]), entries=_entries(p, targets=[]))
    assert [i.reason_code for i in desired.issues] == ["callback_no_target"]
    assert desired.pending == []


async def test_plugin_without_callbacks_is_silent(tmp_path):
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    p = _plugin(manifest={"name": "x"})
    issues = await _reconcile(registry, plugins=[p], acks=acks,
                              spool=_SpoolStub([]))
    assert issues == []


async def test_one_bad_plugin_never_darks_another(tmp_path):
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    good = _plugin()
    bad = _plugin(name="badone", artifact_id="art-9",
                  callbacks=("plg-nope",))
    issues = await _reconcile(
        registry, plugins=[good, bad], acks=acks, spool=_SpoolStub([]),
        entries=_entries(good, bad))
    assert [(i.name, i.reason_code) for i in issues] == [
        ("badone", "callback_invalid")]
    assert registry.get_callback("plg-gmail--authorize") is not None


# ---------------------------------------------------------------------------
# the overlay swap
# ---------------------------------------------------------------------------


async def test_stale_overlay_entries_vanish_in_the_swap(tmp_path):
    registry = _SpyRegistry()
    registry.replace_callback_overlay({
        "plg-gone--old": {"plugin": "gone", "declared": "old",
                          "path": "/store/gone/art-0"}})
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    await _reconcile(registry, plugins=[p], acks=acks, spool=_SpoolStub([]))
    assert registry.get_callback("plg-gone--old") is None
    assert registry.get_callback("plg-gmail--authorize") is not None


async def test_invalid_registry_fails_closed_to_an_empty_overlay(tmp_path):
    registry = _SpyRegistry()
    registry.replace_callback_overlay({
        "plg-gmail--authorize": {"plugin": "gmail", "declared": "authorize",
                                 "path": "/store/gmail/art-1"}})
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    issues = await _reconcile(registry, plugins=[p], acks=acks,
                              spool=_SpoolStub([]),
                              resolver=_resolver([p], valid=False))
    assert issues == []          # the registry stage owns its own issues
    assert registry.get_callback("plg-gmail--authorize") is None


async def test_compute_failure_fails_closed_and_propagates(tmp_path):
    registry = _SpyRegistry()
    registry.replace_callback_overlay({
        "plg-gmail--authorize": {"plugin": "gmail", "declared": "authorize",
                                 "path": "/store/gmail/art-1"}})
    acks = CallbackAckStore(path=tmp_path / "acks.json")

    def _boom(target):
        raise RuntimeError("resolver exploded")

    with pytest.raises(RuntimeError):
        await _reconcile(registry, plugins=[], acks=acks,
                         spool=_SpoolStub([]), resolver=_boom)
    assert registry.get_callback("plg-gmail--authorize") is None


# ---------------------------------------------------------------------------
# ready.json / .index — the asymmetric ordering
# ---------------------------------------------------------------------------


async def test_ready_and_index_written_only_after_the_swap(tmp_path):
    calls: list = []
    registry = _SpyRegistry(calls)
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    await _reconcile(registry, plugins=[p], acks=acks,
                     spool=_SpoolStub(calls))
    kinds = [c[0] for c in calls]
    assert kinds == ["swap", "ensure", "ready", "index"]
    payload = calls[2][2]
    eff = "plg-gmail--authorize"
    assert payload == {
        "v": 1, "base_url": BASE,
        "callbacks": {"authorize": {
            "effective": eff,
            "redirect_uri": f"{BASE}/callback/{eff}"}}}
    assert calls[3][1] == p.path
    assert calls[3][2] == dict(payload, plugin_dir="gmail")


async def test_ready_and_index_deleted_before_the_unrouting_swap(tmp_path):
    calls: list = []
    registry = _SpyRegistry(calls)
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    spool = _SpoolStub(calls)                      # reused: it is the on-disk truth
    await _reconcile(registry, plugins=[p], acks=acks, spool=spool)
    calls.clear()
    # the plugin disappears (uninstalled): the orphan marker + index key die
    # BEFORE the swap, retired from the DURABLE on-disk inventory. The index is
    # retired by its on-disk KEY (delete_index_key), never a re-hashed path.
    await _reconcile(registry, plugins=[], acks=acks, spool=spool,
                     entries=lambda: [])
    assert [c[0] for c in calls] == ["del_ready", "del_index_key", "swap"]
    assert calls[1][1] == callback_spool.index_key(p.path)
    assert registry.get_callback("plg-gmail--authorize") is None


async def test_revoked_ack_unroutes_marker_first(tmp_path):
    calls: list = []
    registry = _SpyRegistry(calls)
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    spool = _SpoolStub(calls)                      # reused: the on-disk truth
    await _reconcile(registry, plugins=[p], acks=acks, spool=spool)
    acks.revoke_plugin("gmail")
    calls.clear()
    await _reconcile(registry, plugins=[p], acks=acks, spool=spool)
    assert [c[0] for c in calls][:3] == ["del_ready", "del_index_key", "swap"]
    assert registry.get_callback("plg-gmail--authorize") is None


async def test_artifact_change_retires_the_old_index_key_only(tmp_path):
    """The index is keyed by the RESOLVED artifact path: an update must drop
    the old key (retired from the on-disk inventory by KEY) in the same pass
    that publishes the new one, and the readiness marker must end up serving the
    live route. A real spool pins the on-disk outcome (the paired transaction
    may rewrite the ready marker too — its atomicity is per-pair, not per-file
    — so op-order is no longer the contract; the resulting files are)."""
    root = tmp_path / "spool"
    art1 = tmp_path / "store" / "gmail" / "art-1"
    art2 = tmp_path / "store" / "gmail" / "art-2"
    art1.mkdir(parents=True)
    art2.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks)
        await _reconcile(registry, plugins=[_plugin(path=str(art1))],
                         acks=acks, spool=spool)
        old_key = callback_spool.index_key(str(art1))
        assert spool.index_keys() == [old_key]

        await _reconcile(registry, plugins=[_plugin(artifact_id="art-2",
                                                    path=str(art2))],
                         acks=acks, spool=spool)

        new_key = callback_spool.index_key(str(art2))
        assert spool.index_keys() == [new_key]       # old key retired, new one live
        assert "gmail" in spool.published_plugins()  # readiness marker still serves
        assert registry.get_callback("plg-gmail--authorize") is not None
    finally:
        spool.close()


async def test_dropping_one_callback_retires_the_marker_before_the_swap(
    tmp_path,
):
    """'Never falsely positive' holds per FILE. A plugin that drops
    one of its callbacks would otherwise keep a ready.json advertising the
    dropped one across the swap window (and forever, if the rewrite fails)."""
    calls: list = []
    registry = _SpyRegistry(calls)
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks, declared="authorize")
    _ack(acks, declared="renew")
    spool = _SpoolStub(calls)                      # reused: the on-disk truth
    p2 = _plugin(callbacks=("authorize", "renew"))
    await _reconcile(registry, plugins=[p2], acks=acks, spool=spool)
    calls.clear()
    p1 = _plugin(callbacks=("authorize",))
    await _reconcile(registry, plugins=[p1], acks=acks, spool=spool)
    # BOTH published files carry the callbacks map, so both retire first
    assert [c[0] for c in calls] == ["del_ready", "del_index", "swap",
                                     "ensure", "ready", "index"]
    assert set(calls[4][2]["callbacks"]) == {"authorize"}
    assert registry.get_callback("plg-gmail--renew") is None


async def test_dropping_while_adding_retires_both_markers(tmp_path):
    """A strict-subset test misses the MIXED transition — drop one
    callback and add another in the same pass and the old marker still named
    the dropped one. Additions are irrelevant to the property."""
    calls: list = []
    registry = _SpyRegistry(calls)
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks, declared="authorize")
    _ack(acks, declared="renew")
    spool = _SpoolStub(calls)                      # reused: the on-disk truth
    await _reconcile(registry,
                     plugins=[_plugin(callbacks=("authorize", "renew"))],
                     acks=acks, spool=spool)
    calls.clear()
    _ack(acks, declared="refresh")
    await _reconcile(registry,
                     plugins=[_plugin(callbacks=("authorize", "refresh"))],
                     acks=acks, spool=spool)
    assert [c[0] for c in calls] == ["del_ready", "del_index", "swap",
                                     "ensure", "ready", "index"]
    assert set(calls[4][2]["callbacks"]) == {"authorize", "refresh"}
    assert registry.get_callback("plg-gmail--renew") is None
    assert registry.get_callback("plg-gmail--refresh") is not None


async def test_failed_rewrite_after_a_drop_and_add_leaves_neither_marker(
    tmp_path,
):
    """The point of retiring both: when the post-swap rewrite fails, the
    consumer reads 'facility unavailable' from BOTH files rather than a
    redirect URI for a callback the endpoint now 404s."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks, declared="authorize")
        _ack(acks, declared="renew")
        await _reconcile(
            registry, acks=acks, spool=spool,
            plugins=[_plugin(callbacks=("authorize", "renew"), path=str(art))])
        # consented only once the plugin declares it (an ack for an
        # undeclared name is exactly what the stale prune removes)
        _ack(acks, declared="refresh")
        ready = root / "gmail" / "ready.json"
        index = root / callback_spool.INDEX_DIR / \
            f"{callback_spool.index_key(str(art))}.json"
        assert ready.is_file() and index.is_file()

        class _WriteFails:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def write_ready(self, plugin, payload):
                raise OSError("disk full")

        issues = await _reconcile(
            registry, acks=acks, spool=_WriteFails(spool),
            plugins=[_plugin(callbacks=("authorize", "refresh"),
                             path=str(art))])
        assert [i.reason_code for i in issues] == ["callback_spool_error"]
        assert not ready.exists()
        assert not index.exists()
        assert registry.get_callback("plg-gmail--renew") is None
    finally:
        spool.close()


async def test_a_failed_rewrite_of_a_shrunk_set_leaves_no_marker(tmp_path):
    """The same fix's real point: when the post-swap rewrite fails, the
    operator is left with NO marker (fail-closed, the consumer sees the
    facility as unavailable) rather than one still advertising a callback the
    endpoint now 404s."""
    root = tmp_path / "spool"
    spool = callback_spool.CallbackSpool(root)
    try:
        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks, declared="authorize")
        _ack(acks, declared="renew")
        p2 = _plugin(callbacks=("authorize", "renew"))
        await _reconcile(registry, plugins=[p2], acks=acks, spool=spool)
        ready = root / "gmail" / "ready.json"
        import json
        assert set(json.loads(ready.read_text())["callbacks"]) == {
            "authorize", "renew"}

        class _WriteFails:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def write_ready(self, plugin, payload):
                raise OSError("disk full")

        p1 = _plugin(callbacks=("authorize",))
        issues = await _reconcile(registry, plugins=[p1], acks=acks,
                                  spool=_WriteFails(spool))
        assert [i.reason_code for i in issues] == ["callback_spool_error"]
        assert not ready.exists()
        assert not (root / callback_spool.INDEX_DIR /
                    f"{callback_spool.index_key(p1.path)}.json").exists()
        assert registry.get_callback("plg-gmail--renew") is None
    finally:
        spool.close()


async def test_growing_the_set_keeps_the_marker_through_the_swap(tmp_path):
    """The opposite direction is fail-closed (the marker under-advertises), so
    it must NOT churn the file — no delete, just the post-swap rewrite."""
    calls: list = []
    registry = _SpyRegistry(calls)
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks, declared="authorize")
    await _reconcile(registry, plugins=[_plugin(callbacks=("authorize",))],
                     acks=acks, spool=_SpoolStub(calls))
    calls.clear()
    _ack(acks, declared="renew")
    await _reconcile(registry,
                     plugins=[_plugin(callbacks=("authorize", "renew"))],
                     acks=acks, spool=_SpoolStub(calls))
    assert [c[0] for c in calls] == ["swap", "ensure", "ready", "index"]


async def test_index_key_is_the_resolved_artifact_path(tmp_path):
    """A real spool: the entry lands under sha256(realpath(artifact root)) —
    the one value a consumer provably knows."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    link = tmp_path / "linked"
    os.symlink(art, link)
    spool = callback_spool.CallbackSpool(root)
    try:
        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks)
        p = _plugin(path=str(link))
        await _reconcile(registry, plugins=[p], acks=acks, spool=spool)
        key = callback_spool.index_key(str(art))
        entry = root / callback_spool.INDEX_DIR / f"{key}.json"
        assert entry.is_file()
        import json
        assert json.loads(entry.read_text())["plugin_dir"] == "gmail"
        assert (root / "gmail" / "ready.json").is_file()
        assert (root / "gmail" / "pending").is_dir()
    finally:
        spool.close()


async def test_scoped_bundle_name_gets_its_own_spool_dir(tmp_path):
    """Bundled plugins register as ``slug.manifest-name`` — a dotted (but not
    dot-LEADING) name the spool accepts."""
    root = tmp_path / "spool"
    spool = callback_spool.CallbackSpool(root)
    try:
        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks, plugin="finance.gmail")
        p = _plugin(name="finance.gmail")
        await _reconcile(registry, plugins=[p], acks=acks, spool=spool)
        assert registry.get_callback("plg-finance.gmail--authorize") is not None
        assert (root / "finance.gmail" / "ready.json").is_file()
    finally:
        spool.close()


async def test_spool_write_failure_surfaces_but_keeps_the_route(tmp_path):
    calls: list = []
    registry = _SpyRegistry(calls)
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    issues = await _reconcile(registry, plugins=[p], acks=acks,
                              spool=_SpoolStub(calls, fail={"ready"}))
    assert [i.reason_code for i in issues] == ["callback_spool_error"]
    # the overlay is the authority — an advisory-marker failure never unroutes
    assert registry.get_callback("plg-gmail--authorize") is not None


async def test_a_pathless_routed_plugin_never_touches_the_index(tmp_path):
    """The index key is sha256(realpath(path)) and realpath("") is the process
    CWD — a routed plugin with an empty resolved path must never make the
    reconcile read, write or delete an index entry keyed off the CWD. It
    publishes only its readiness marker; no index op is ever issued."""
    calls: list = []
    registry = _SpyRegistry(calls)
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin(path="")
    await _reconcile(registry, plugins=[p], acks=acks, spool=_SpoolStub(calls))
    kinds = [c[0] for c in calls]
    assert kinds == ["swap", "ensure", "ready"]      # ready only, never an index op
    assert not any(k in ("index", "del_index", "del_index_key") for k in kinds)


async def test_missing_spool_reports_one_issue_per_plugin(tmp_path):
    """An unwired spool fails EVERY file operation — the health report gets one
    actionable row per ROUTED plugin, not one per syscall (ensure + ready +
    index would otherwise contribute three rows for one plugin)."""
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    p1 = _plugin()
    p2 = _plugin(name="other")
    _ack(acks)
    _ack(acks, plugin="other")
    issues = await _reconcile(registry, plugins=[p1, p2], acks=acks, spool=None,
                              entries=_entries(p1, p2))
    assert sorted((i.name, i.reason_code) for i in issues) == [
        ("gmail", "callback_spool_error"), ("other", "callback_spool_error")]


async def test_missing_spool_still_swaps_the_overlay(tmp_path):
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    issues = await _reconcile(registry, plugins=[p], acks=acks, spool=None)
    assert [i.reason_code for i in issues] == ["callback_spool_error"]
    assert registry.get_callback("plg-gmail--authorize") is not None


# ---------------------------------------------------------------------------
# base URL
# ---------------------------------------------------------------------------


async def test_no_base_url_writes_no_files_and_reports_it(monkeypatch, tmp_path):
    calls: list = []
    registry = _SpyRegistry(calls)
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    issues = await _reconcile(registry, plugins=[p], acks=acks,
                              spool=_SpoolStub(calls), base_url=None,
                              monkeypatch=monkeypatch)
    assert [i.reason_code for i in issues] == ["callback_base_url_invalid"]
    assert [c[0] for c in calls] == ["swap"]
    # consent still routes the overlay — the facility is merely unavailable
    assert registry.get_callback("plg-gmail--authorize") is not None


async def test_base_url_loss_retires_a_previously_published_marker(
    monkeypatch, tmp_path,
):
    calls: list = []
    registry = _SpyRegistry(calls)
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    spool = _SpoolStub(calls)                      # reused: the on-disk truth
    await _reconcile(registry, plugins=[p], acks=acks, spool=spool)
    calls.clear()
    await _reconcile(registry, plugins=[p], acks=acks,
                     spool=spool, base_url=None, monkeypatch=monkeypatch)
    assert [c[0] for c in calls] == ["del_ready", "del_index_key", "swap"]


# ---------------------------------------------------------------------------
# durable marker reconcile — survives a restart (on-disk inventory, not the
# in-memory previous overlay)
# ---------------------------------------------------------------------------


def _seed_prior_boot_marker(spool, plugin, art_path):
    """Write ready.json + the index entry directly, simulating a marker
    published by a PRIOR process (this process's in-memory overlay is empty,
    as it is right after a restart)."""
    spool.ensure_plugin_dirs(plugin)
    payload = {"v": 1, "base_url": BASE, "callbacks": {}}
    spool.write_ready(plugin, payload)
    spool.write_index_entry(str(art_path), dict(payload, plugin_dir=plugin))


def _marker_paths(root, plugin, art_path):
    return (root / plugin / "ready.json",
            root / callback_spool.INDEX_DIR /
            f"{callback_spool.index_key(str(art_path))}.json")


async def test_prior_boot_marker_retired_when_ack_now_absent(tmp_path):
    """A plugin routed in a PRIOR process whose ack is now gone: the durable
    reconcile retires ready.json AND the index entry (the in-memory previous
    overlay is empty, so only on-disk truth catches it), and the route is not
    served."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        _seed_prior_boot_marker(spool, "gmail", art)
        ready, index = _marker_paths(root, "gmail", art)
        assert ready.is_file() and index.is_file()

        registry = _SpyRegistry()               # empty overlay, like a boot
        acks = CallbackAckStore(path=tmp_path / "acks.json")   # NO ack
        p = _plugin(path=str(art))
        await _reconcile(registry, plugins=[p], acks=acks, spool=spool)

        assert not ready.exists()
        assert not index.exists()
        assert registry.get_callback("plg-gmail--authorize") is None
        assert "gmail" not in spool.published_plugins()
    finally:
        spool.close()


async def test_prior_boot_marker_retired_when_base_url_now_invalid(
    monkeypatch, tmp_path,
):
    """Routed + acked, but the base URL is now invalid: nothing is publishable,
    so a marker from a prior boot (when it was valid) is retired."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        _seed_prior_boot_marker(spool, "gmail", art)
        ready, index = _marker_paths(root, "gmail", art)

        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks)
        p = _plugin(path=str(art))
        await _reconcile(registry, plugins=[p], acks=acks, spool=spool,
                         base_url=None, monkeypatch=monkeypatch)

        assert not ready.exists()
        assert not index.exists()
    finally:
        spool.close()


async def test_prior_boot_marker_retired_when_declaration_removed(tmp_path):
    """The plugin is no longer installed (declaration gone): its orphaned
    prior-boot marker is retired even though the in-memory overlay never
    carried it this process."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        _seed_prior_boot_marker(spool, "gmail", art)
        ready, index = _marker_paths(root, "gmail", art)

        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        await _reconcile(registry, plugins=[], acks=acks, spool=spool,
                         entries=lambda: [])

        assert not ready.exists()
        assert not index.exists()
    finally:
        spool.close()


async def test_durable_reconcile_preserves_a_still_routed_marker(tmp_path):
    """A plugin that IS in the desired routed set keeps its marker across a
    later reconcile with a fresh (empty) overlay."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks)
        p = _plugin(path=str(art))
        await _reconcile(registry, plugins=[p], acks=acks, spool=spool)
        ready, index = _marker_paths(root, "gmail", art)
        assert ready.is_file() and index.is_file()

        # A later "boot": fresh registry (empty overlay), same routed plugin.
        registry2 = _SpyRegistry()
        await _reconcile(registry2, plugins=[p], acks=acks, spool=spool)
        assert ready.is_file() and index.is_file()
        assert registry2.get_callback("plg-gmail--authorize") is not None
    finally:
        spool.close()


async def test_registry_invalid_does_not_retire_durable_markers(tmp_path):
    """Fail-closed availability: a wholesale compute failure (invalid registry
    ⇒ prunable False) must NOT nuke a valid plugin's on-disk marker."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        _seed_prior_boot_marker(spool, "gmail", art)
        ready, index = _marker_paths(root, "gmail", art)

        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        p = _plugin(path=str(art))
        await _reconcile(registry, plugins=[p], acks=acks, spool=spool,
                         resolver=_resolver([p], valid=False))

        assert ready.is_file()
        assert index.is_file()
    finally:
        spool.close()


# ---------------------------------------------------------------------------
# durable marker reconcile — STILL-routed but payload changed while down. The
# in-memory swap diff is empty across a restart, so only the on-disk payload
# compare catches a dropped callback or a changed redirect base.
# ---------------------------------------------------------------------------


class _WriteFails:
    """Wrap a real spool, failing every ready.json write (a disk-full rewrite).
    Deletes and reads pass through, so a retire-before-swap still happens and a
    failed rewrite leaves the marker ABSENT rather than stale."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def write_ready(self, plugin, payload):
        raise OSError("disk full")


def _seed_marker(spool, plugin, art_path, callbacks, base=BASE):
    """Write a ready.json + index entry EXACTLY as a prior boot with this
    (plugin, callbacks, base) would have — via the same payload builder the
    reconcile writes with — so only the intended field (a dropped callback, a
    changed base) differs from a later desired pass."""
    routed = cr.RoutedCallbacks(
        plugin=plugin, artifact_id="art-seed", path=str(art_path),
        callbacks=[{"declared": d, "effective": f"plg-{plugin}--{d}"}
                   for d in callbacks])
    ready, index = cr._desired_marker_payloads(base, routed)
    spool.ensure_plugin_dirs(plugin)
    spool.write_ready(plugin, ready)
    spool.write_index_entry(str(art_path), index)


async def test_still_routed_dropped_callback_retired_across_restart(tmp_path):
    """A plugin STILL routed whose prior-boot marker advertises an EXTRA,
    now-dropped callback: the durable payload compare retires the stale marker
    before the swap and the rewrite advertises only the live callback."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        _seed_marker(spool, "gmail", art, ["authorize", "renew"])
        ready, index = _marker_paths(root, "gmail", art)
        assert set(json.loads(ready.read_text())["callbacks"]) == {
            "authorize", "renew"}

        registry = _SpyRegistry()                 # empty overlay, like a boot
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks, declared="authorize")          # only authorize is live now
        p = _plugin(callbacks=("authorize",), path=str(art))
        await _reconcile(registry, plugins=[p], acks=acks, spool=spool)

        assert set(json.loads(ready.read_text())["callbacks"]) == {"authorize"}
        assert set(json.loads(index.read_text())["callbacks"]) == {"authorize"}
        assert registry.get_callback("plg-gmail--authorize") is not None
    finally:
        spool.close()


async def test_still_routed_stale_marker_absent_on_rewrite_failure(tmp_path):
    """The point of retiring before the swap: when the post-swap rewrite fails,
    the still-routed plugin's marker is left ABSENT (consumer reads 'not
    approved yet, wait'), never STALE advertising the dropped callback."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        _seed_marker(spool, "gmail", art, ["authorize", "renew"])
        ready, index = _marker_paths(root, "gmail", art)

        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks, declared="authorize")
        p = _plugin(callbacks=("authorize",), path=str(art))
        issues = await _reconcile(registry, plugins=[p], acks=acks,
                                  spool=_WriteFails(spool))

        assert [i.reason_code for i in issues] == ["callback_spool_error"]
        assert not ready.exists()
        assert not index.exists()
    finally:
        spool.close()


async def test_still_routed_base_url_change_retires_old_redirect(tmp_path):
    """A plugin still routed whose redirect BASE changed while down: the old
    redirect URI is gone after the reconcile (durable payload compare, not the
    empty in-memory diff)."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    old_base = "https://old.casa.example.org"
    spool = callback_spool.CallbackSpool(root)
    try:
        _seed_marker(spool, "gmail", art, ["authorize"], base=old_base)
        ready, index = _marker_paths(root, "gmail", art)
        assert old_base in ready.read_text()

        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks)
        p = _plugin(path=str(art))
        await _reconcile(registry, plugins=[p], acks=acks, spool=spool)

        data = json.loads(ready.read_text())
        assert data["base_url"] == BASE
        assert old_base not in json.dumps(data)
        assert all(cb["redirect_uri"].startswith(BASE)
                   for cb in data["callbacks"].values())
        assert old_base not in index.read_text()
    finally:
        spool.close()


async def test_still_routed_base_url_change_absent_on_rewrite_failure(tmp_path):
    """The base-URL-change subcase is fail-closed on a rewrite failure too: the
    obsolete-redirect marker is retired before the swap, so a failed rewrite
    leaves it absent rather than advertising the old redirect URI forever."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        _seed_marker(spool, "gmail", art, ["authorize"],
                     base="https://old.casa.example.org")
        ready, index = _marker_paths(root, "gmail", art)

        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks)
        p = _plugin(path=str(art))
        issues = await _reconcile(registry, plugins=[p], acks=acks,
                                  spool=_WriteFails(spool))

        assert [i.reason_code for i in issues] == ["callback_spool_error"]
        assert not ready.exists()
        assert not index.exists()
    finally:
        spool.close()


async def test_unchanged_still_routed_marker_is_not_rewritten(tmp_path):
    """No churn: when the on-disk payload already matches the desired one, a
    later reconcile (fresh empty overlay, as after a restart) neither deletes
    nor rewrites the marker — the file's inode and mtime are untouched."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks)
        p = _plugin(path=str(art))
        await _reconcile(registry, plugins=[p], acks=acks, spool=spool)
        ready, index = _marker_paths(root, "gmail", art)
        st_ready, st_index = ready.stat(), index.stat()

        class _Counting:
            def __init__(self, inner):
                self._inner = inner
                self.writes = 0
                self.deletes = 0

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def write_ready(self, *a):
                self.writes += 1
                return self._inner.write_ready(*a)

            def write_index_entry(self, *a):
                self.writes += 1
                return self._inner.write_index_entry(*a)

            def delete_ready(self, *a):
                self.deletes += 1
                return self._inner.delete_ready(*a)

            def delete_index_entry(self, *a):
                self.deletes += 1
                return self._inner.delete_index_entry(*a)

            def delete_index_key(self, *a):
                self.deletes += 1
                return self._inner.delete_index_key(*a)

        counting = _Counting(spool)
        registry2 = _SpyRegistry()                # fresh overlay = a restart
        await _reconcile(registry2, plugins=[p], acks=acks, spool=counting)

        assert counting.writes == 0               # no rewrite of a matching marker
        assert counting.deletes == 0              # and no retire
        assert ready.stat().st_ino == st_ready.st_ino
        assert ready.stat().st_mtime == st_ready.st_mtime
        assert index.stat().st_ino == st_index.st_ino
        assert registry2.get_callback("plg-gmail--authorize") is not None
    finally:
        spool.close()


async def test_registry_invalid_does_not_retire_a_differing_marker(tmp_path):
    """The double-gate holds for the payload compare too: an invalid-registry
    pass (``prunable`` False) must NOT retire even a marker whose payload
    differs from what a valid pass would desire — a transient bad compute may
    never delete a valid plugin's marker."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        _seed_marker(spool, "gmail", art, ["authorize", "renew"],
                     base="https://old.casa.example.org")
        ready, index = _marker_paths(root, "gmail", art)
        before_ready = ready.read_text()

        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks)
        p = _plugin(path=str(art))
        await _reconcile(registry, plugins=[p], acks=acks, spool=spool,
                         resolver=_resolver([p], valid=False))

        assert ready.is_file() and index.is_file()
        assert ready.read_text() == before_ready   # untouched, not rewritten
    finally:
        spool.close()


def _resolution_hiccup():
    from plugin_registry import PluginIssue
    return PluginIssue(name="other", target=None, stage="resolve",
                       reason_code="artifact_invalid", artifact_id="art-x")


async def test_resolution_issue_still_refreshes_a_routed_plugins_own_marker(
    tmp_path,
):
    """CLASS 2, INVERTED in v0.162.0 (#453). This used to assert that an
    untrustworthy pass (a resolution HICCUP on some OTHER plugin ⇒ ``prunable``
    False) left a routed plugin's differing marker UNCHANGED.

    That was safe only while the marker was purely advisory. Once the
    setup-dispatch gate holds on the pair matching the desired one
    (INV-PLUG-011), "leave it stale" became a state with no exit: `prunable` is
    registry-GLOBAL, so one unresolvable artifact anywhere froze every other
    plugin's markers for as long as it stayed broken, and the gate then demanded
    a pair the writer had decided never to write. No operator action on the held
    plugin could clear it. Two independent reviewers reached it from different
    starting points; the ordinary trigger is a plugin UPDATE, whose moved
    artifact path leaves the index entry absent beside a byte-identical
    `ready.json`.

    The availability double-gate keeps its real job — refusing to DELETE a
    marker on a pass that may simply have failed to see its plugin, pinned by
    the orphan and invalid-registry cases around this one. It does not apply
    here: a plugin in the routed set resolved cleanly in THIS pass and holds a
    persisted ack, so the pair being written is derived from its own good
    resolution. The stale value it replaces was not a safe one to preserve — an
    old base URL is a redirect URI Casa no longer serves."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        _seed_marker(spool, "gmail", art, ["authorize", "renew"],
                     base="https://old.casa.example.org")   # differs from desired
        ready, index = _marker_paths(root, "gmail", art)
        before_ready, before_index = ready.read_text(), index.read_text()

        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks)
        p = _plugin(path=str(art))
        await _reconcile(registry, plugins=[p], acks=acks, spool=spool,
                         resolver=_resolver([p], issues=[_resolution_hiccup()]))

        assert ready.read_text() != before_ready   # refreshed to desired
        assert index.read_text() != before_index
        assert "old.casa.example.org" not in ready.read_text()
        # consent + resolution of THIS plugin stand, so the overlay still routes
        assert registry.get_callback("plg-gmail--authorize") is not None
    finally:
        spool.close()


async def test_untrustworthy_pass_still_publishes_a_fresh_absent_pair(tmp_path):
    """The double-gate blocks retire/overwrite of an EXISTING marker on an
    untrustworthy pass, but a genuinely-ABSENT pair for a routed plugin is a
    pure fresh publish (it destroys nothing) and is still written."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks)
        p = _plugin(path=str(art))
        await _reconcile(registry, plugins=[p], acks=acks, spool=spool,
                         resolver=_resolver([p], issues=[_resolution_hiccup()]))
        ready, index = _marker_paths(root, "gmail", art)
        assert ready.is_file() and index.is_file()   # fresh publish allowed
        assert set(json.loads(ready.read_text())["callbacks"]) == {"authorize"}
    finally:
        spool.close()


# ---------------------------------------------------------------------------
# the paired marker transaction — fail-closed to absent, INVALID != absent,
# never a half-published pair (r3 findings)
# ---------------------------------------------------------------------------


class _IndexWriteFails:
    """Wrap a real spool, failing every ``.index`` write while ready.json
    writes and all deletes/reads pass through — models the
    write-ready-succeeds-then-write-index-FAILS split."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def write_index_entry(self, artifact_realpath, payload):
        raise OSError("disk full")


async def test_write_index_failure_after_ready_leaves_neither_marker(tmp_path):
    """Retire-index, then write-ready SUCCEEDS, then write-index FAILS — the
    pass must leave BOTH markers absent (fail-closed), never the old .index +
    new ready.json split. The prior stale pair is retired pre-swap; the
    post-swap index failure deletes the just-written ready.json too."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        _seed_marker(spool, "gmail", art, ["authorize", "renew"])   # stale prior
        ready, index = _marker_paths(root, "gmail", art)
        assert ready.is_file() and index.is_file()

        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks, declared="authorize")
        p = _plugin(callbacks=("authorize",), path=str(art))
        issues = await _reconcile(registry, plugins=[p], acks=acks,
                                  spool=_IndexWriteFails(spool))

        assert [i.reason_code for i in issues] == ["callback_spool_error"]
        assert not ready.exists()
        assert not index.exists()
        assert registry.get_callback("plg-gmail--authorize") is not None
    finally:
        spool.close()


async def test_fresh_publish_index_failure_leaves_neither_marker(tmp_path):
    """The same fail-closed rule on a FIRST publish (nothing on disk): a
    write-index failure after a successful write-ready must roll the ready.json
    back to absent — never a lone ready marker advertising a route with no
    discoverable redirect URI."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks)
        p = _plugin(path=str(art))
        issues = await _reconcile(registry, plugins=[p], acks=acks,
                                  spool=_IndexWriteFails(spool))
        ready, index = _marker_paths(root, "gmail", art)
        assert [i.reason_code for i in issues] == ["callback_spool_error"]
        assert not ready.exists()
        assert not index.exists()
    finally:
        spool.close()


async def test_invalid_ready_marker_is_republished_without_hanging(tmp_path):
    """An INVALID on-disk ready.json (a swapped-in FIFO) is NOT conflated with
    absent: it is treated as needing republish, retired and rewritten as a
    regular file — and the reconcile must not hang on the FIFO (the read is
    non-blocking)."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks)
        p = _plugin(path=str(art))
        await _reconcile(registry, plugins=[p], acks=acks, spool=spool)
        ready, _index = _marker_paths(root, "gmail", art)
        ready.unlink()
        os.mkfifo(ready)                       # corrupt ready.json into a FIFO

        registry2 = _SpyRegistry()             # a later boot
        await _reconcile(registry2, plugins=[p], acks=acks, spool=spool)

        assert ready.is_file()                 # republished as a regular file
        assert set(json.loads(ready.read_text())["callbacks"]) == {"authorize"}
        assert registry2.get_callback("plg-gmail--authorize") is not None
    finally:
        spool.close()


async def test_oversized_index_marker_is_republished(tmp_path):
    """An INVALID (oversized) on-disk index entry is republished, not left in
    place — INVALID is never mistaken for a matching PRESENT marker."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks)
        p = _plugin(path=str(art))
        await _reconcile(registry, plugins=[p], acks=acks, spool=spool)
        _ready, index = _marker_paths(root, "gmail", art)
        index.write_bytes(
            b'{"pad":"' + b"p" * (callback_spool.MARKER_STATE_MAX_BYTES + 16)
            + b'"}')

        registry2 = _SpyRegistry()
        await _reconcile(registry2, plugins=[p], acks=acks, spool=spool)

        data = json.loads(index.read_text())   # small, valid again
        assert data["plugin_dir"] == "gmail"
    finally:
        spool.close()


async def test_half_published_pair_is_made_whole(tmp_path):
    """A half-published pair (ready.json present, its index entry missing — a
    crash between the two writes) is reconciled to BOTH-present: the invariant
    is both-absent or both-present-equal, never one of each."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks)
        p = _plugin(path=str(art))
        await _reconcile(registry, plugins=[p], acks=acks, spool=spool)
        ready, index = _marker_paths(root, "gmail", art)
        index.unlink()                         # lose one half of the pair
        assert ready.is_file() and not index.exists()

        registry2 = _SpyRegistry()
        await _reconcile(registry2, plugins=[p], acks=acks, spool=spool)

        assert ready.is_file() and index.is_file()
        assert json.loads(index.read_text())["plugin_dir"] == "gmail"
    finally:
        spool.close()


async def test_directory_shaped_orphan_marker_is_retired(tmp_path):
    """CLASS 1: a NON-REGULAR (directory) ready.json for a plugin no longer
    routed is an orphan a trustworthy pass must RETIRE — a raw unlink fails on a
    directory and would leave it to block republication forever."""
    root = tmp_path / "spool"
    spool = callback_spool.CallbackSpool(root)
    try:
        spool.ensure_plugin_dirs("ghost")
        os.mkdir(root / "ghost" / "ready.json")      # dir-shaped orphan marker
        assert "ghost" in spool.published_plugins()

        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        await _reconcile(registry, plugins=[], acks=acks, spool=spool,
                         entries=lambda: [])          # nothing routed ⇒ pure orphan

        assert "ghost" not in spool.published_plugins()
        assert not (root / "ghost" / "ready.json").exists()
    finally:
        spool.close()


async def test_directory_shaped_routed_marker_is_made_whole(tmp_path):
    """CLASS 1: a routed plugin whose on-disk ready.json is a DIRECTORY (invalid)
    is retired type-aware and rewritten to a regular file — never left to block
    republication behind a raw unlink that fails on a directory."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        spool.ensure_plugin_dirs("gmail")
        os.mkdir(root / "gmail" / "ready.json")      # invalid, dir-shaped
        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks)
        p = _plugin(path=str(art))
        await _reconcile(registry, plugins=[p], acks=acks, spool=spool)

        ready = root / "gmail" / "ready.json"
        assert ready.is_file()                        # rewritten as a regular file
        assert set(json.loads(ready.read_text())["callbacks"]) == {"authorize"}
        assert registry.get_callback("plg-gmail--authorize") is not None
    finally:
        spool.close()


async def test_directory_shaped_orphan_index_entry_is_retired(tmp_path):
    """CLASS 1: a NON-REGULAR (directory) ``.index`` entry for a key no longer
    desired is enumerated and retired type-aware by a trustworthy pass."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        spool.write_index_entry(str(art), {"v": 1})  # creates .index + the entry
        key = callback_spool.index_key(str(art))
        entry = root / callback_spool.INDEX_DIR / f"{key}.json"
        entry.unlink()
        os.mkdir(entry)                               # dir-shaped orphan index entry
        assert key in spool.index_keys()

        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        await _reconcile(registry, plugins=[], acks=acks, spool=spool,
                         entries=lambda: [])
        assert spool.index_keys() == []
        assert not entry.exists()
    finally:
        spool.close()


@pytest.mark.parametrize("corrupt", [
    pytest.param(lambda d: {**d, "v": True}, id="true-vs-1"),
    pytest.param(lambda d: {**d, "v": 1.0}, id="float-vs-int"),
    pytest.param(lambda d: {**d, "extra": "x"}, id="extra-key"),
])
async def test_type_corrupted_marker_is_not_read_as_unchanged(tmp_path, corrupt):
    """CLASS 3: a payload that is ``==`` the desired one but not byte-identical
    to its canonical JSON (``True``/``1.0`` vs ``1``, or an extra key) must be
    treated as DIFFERING — retired and rewritten — not read as 'unchanged' under
    a plain ``dict ==``."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks)
        p = _plugin(path=str(art))
        await _reconcile(registry, plugins=[p], acks=acks, spool=spool)
        ready, _index = _marker_paths(root, "gmail", art)
        good = json.loads(ready.read_text())
        # Corrupt the on-disk payload so ONLY type/extra differs from desired.
        ready.write_text(json.dumps(corrupt(good), sort_keys=True))

        registry2 = _SpyRegistry()                    # a later boot
        await _reconcile(registry2, plugins=[p], acks=acks, spool=spool)

        # The rewrite is proven by CONTENT, never by st_ino: the retire frees
        # the old inode before the staging write, and ext4 hands a freed
        # inode number straight back, so "new file ⇒ new inode" does not
        # hold (it was exactly this proxy that broke on the ext4 CI runners).
        after = json.loads(ready.read_text())
        assert type(after["v"]) is int and after["v"] == 1
        assert "extra" not in after
        assert registry2.get_callback("plg-gmail--authorize") is not None
    finally:
        spool.close()


@pytest.mark.parametrize("rewrite,label", [
    pytest.param(
        lambda good: json.dumps(
            {k: good[k] for k in reversed(list(good))},
            separators=(",", ":"), ensure_ascii=False),
        "key-reordered", id="reorder"),
    pytest.param(
        lambda good: json.dumps(good, sort_keys=True, indent=2,
                                ensure_ascii=False),
        "whitespace-padded", id="whitespace"),
])
async def test_byte_different_but_equal_marker_is_rewritten(
    tmp_path, rewrite, label,
):
    """A marker that parses to the SAME payload but is not byte-identical to the
    canonical form — a key reorder or a whitespace diff — must be treated as
    DIFFERING (retired + rewritten), so after a trustworthy pass the on-disk
    marker is byte-identical to canonical(desired)."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks)
        p = _plugin(path=str(art))
        await _reconcile(registry, plugins=[p], acks=acks, spool=spool)
        ready, _index = _marker_paths(root, "gmail", art)
        good = json.loads(ready.read_text())
        canonical = callback_spool.canonical_marker_bytes(good)
        ready.write_text(rewrite(good), encoding="utf-8")
        assert ready.read_bytes() != canonical               # genuinely differs

        registry2 = _SpyRegistry()                           # a later boot
        await _reconcile(registry2, plugins=[p], acks=acks, spool=spool)

        # Byte-identity to canonical(desired) IS the rewrite proof — the
        # pre-state genuinely differed. st_ino is not usable as a proxy: the
        # retire frees the old inode before the staging write, and ext4
        # recycles freed inode numbers immediately.
        assert ready.read_bytes() == canonical               # now byte-identical
        assert registry2.get_callback("plg-gmail--authorize") is not None
    finally:
        spool.close()


async def test_second_pass_does_not_churn_a_fresh_write(tmp_path):
    """A marker casa itself just wrote is byte-identical to canonical(desired),
    so a second immediate pass leaves inode and mtime untouched (no churn)."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks)
        p = _plugin(path=str(art))
        await _reconcile(_SpyRegistry(), plugins=[p], acks=acks, spool=spool)
        ready, index = _marker_paths(root, "gmail", art)
        ready_bytes = ready.read_bytes()
        # The fresh write is exactly the canonical desired form.
        assert ready_bytes == callback_spool.canonical_marker_bytes(
            json.loads(ready_bytes))
        st_ready, st_index = ready.stat(), index.stat()

        await _reconcile(_SpyRegistry(), plugins=[p], acks=acks, spool=spool)
        assert ready.stat().st_ino == st_ready.st_ino        # untouched
        assert ready.stat().st_mtime == st_ready.st_mtime
        assert index.stat().st_ino == st_index.st_ino
    finally:
        spool.close()


async def test_byte_identical_marker_stays_unchanged_under_strict_compare(
    tmp_path,
):
    """The strict compare has no false churn: a genuinely byte-identical payload
    is still 'unchanged' — inode and mtime untouched across a later reconcile."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks)
        p = _plugin(path=str(art))
        await _reconcile(registry, plugins=[p], acks=acks, spool=spool)
        ready, index = _marker_paths(root, "gmail", art)
        st_ready, st_index = ready.stat(), index.stat()

        registry2 = _SpyRegistry()
        await _reconcile(registry2, plugins=[p], acks=acks, spool=spool)
        assert ready.stat().st_ino == st_ready.st_ino
        assert ready.stat().st_mtime == st_ready.st_mtime
        assert index.stat().st_ino == st_index.st_ino
    finally:
        spool.close()


@pytest.mark.parametrize("raw,expected", [
    ("https://casa.example.org", "https://casa.example.org"),
    ("https://casa.example.org/", "https://casa.example.org"),
    ("  https://casa.example.org  ", "https://casa.example.org"),
    ("null", None),
    ("None", None),
    ("", None),
])
def test_base_url_seam_reads_public_url(monkeypatch, raw, expected):
    monkeypatch.setenv("PUBLIC_URL", raw)
    assert _REAL_BASE_URL() == expected


def test_base_url_seam_without_public_url(monkeypatch):
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    assert _REAL_BASE_URL() is None


# ---------------------------------------------------------------------------
# the declaration digest — consent survives a routine upgrade
# ---------------------------------------------------------------------------


async def test_same_declaration_across_artifacts_keeps_the_ack(
    monkeypatch, tmp_path,
):
    """The consent identity excludes the artifact: an upgrade that leaves the
    declaration untouched stays routed with NO new prompt and no dark pass."""
    import authz_grants
    import verdict_broker
    monkeypatch.setattr(verdict_broker, "BROKER", verdict_broker.VerdictBroker())
    monkeypatch.setattr(authz_grants, "CHALLENGES",
                        authz_grants.ChallengeCoordinator())
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    telegram = _FakeTelegram()
    p1 = _plugin()
    await _reconcile(registry, plugins=[p1], acks=acks, spool=_SpoolStub([]),
                     prompt=True, channel_manager=_FakeChannelManager(telegram))
    p2 = _plugin(artifact_id="art-2")
    issues = await _reconcile(registry, plugins=[p2], acks=acks,
                              spool=_SpoolStub([]), prompt=True,
                              channel_manager=_FakeChannelManager(telegram))
    for _ in range(8):
        await asyncio.sleep(0)
    assert issues == []
    assert registry.get_callback("plg-gmail--authorize") is not None
    assert telegram.posts == []          # never re-prompted
    assert acks.get(_identity()) is not None


async def test_renamed_declaration_needs_fresh_consent(monkeypatch, tmp_path):
    import authz_grants
    import verdict_broker
    monkeypatch.setattr(verdict_broker, "BROKER", verdict_broker.VerdictBroker())
    monkeypatch.setattr(authz_grants, "CHALLENGES",
                        authz_grants.ChallengeCoordinator())
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    telegram = _FakeTelegram()
    p2 = _plugin(artifact_id="art-2", callbacks=("authorise",))
    issues = await _reconcile(registry, plugins=[p2], acks=acks,
                              spool=_SpoolStub([]), prompt=True,
                              channel_manager=_FakeChannelManager(telegram))
    for _ in range(8):
        await asyncio.sleep(0)
    assert [i.reason_code for i in issues] == ["callback_pending_ack"]
    assert registry.get_callback("plg-gmail--authorise") is None
    assert len(telegram.posts) == 1


# ---------------------------------------------------------------------------
# stale-ack prune
# ---------------------------------------------------------------------------


async def test_stale_ack_is_pruned_at_reconcile(tmp_path):
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)                                 # gmail/authorize — installed
    _ack(acks, plugin="ghost", declared="old")  # nothing declares this
    p = _plugin()
    await _reconcile(registry, plugins=[p], acks=acks, spool=_SpoolStub([]))
    assert acks.get(_identity()) is not None
    assert acks.get(_identity("ghost", "old")) is None


async def test_prune_keeps_acks_of_unassigned_and_unacked_declarations(tmp_path):
    """Prunability is about the DECLARATION existing, not about routing: an
    unassigned plugin's consent must survive so re-assignment needs no re-tap."""
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    await _reconcile(registry, plugins=[p], acks=acks, spool=_SpoolStub([]),
                     entries=_entries(p, targets=[]))
    assert acks.get(_identity()) is not None


async def test_prune_is_skipped_when_the_registry_is_invalid(tmp_path):
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    await _reconcile(registry, plugins=[], acks=acks, spool=_SpoolStub([]),
                     resolver=_resolver([], valid=False), entries=lambda: [])
    assert acks.get(_identity()) is not None


async def test_prune_is_skipped_when_resolution_reported_issues(tmp_path):
    """An artifact hiccup (checksum, unreadable manifest) must never vaporize
    consent — the prune is opportunistic and waits for a clean pass."""
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    from plugin_registry import PluginIssue
    hiccup = PluginIssue(name="gmail", target=None, stage="resolve",
                         reason_code="artifact_invalid", artifact_id="art-1")
    await _reconcile(registry, plugins=[], acks=acks, spool=_SpoolStub([]),
                     resolver=_resolver([], issues=[hiccup]),
                     entries=lambda: [])
    assert acks.get(_identity()) is not None


async def test_prune_is_skipped_when_a_declaration_is_unparseable(tmp_path):
    """An invalid declaration contributes NO identities, so pruning
    that pass would destroy the operator's consent for the plugin's OTHER,
    perfectly valid callback — all-or-nothing rejects a set, it must never
    delete the acks behind it."""
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)                                   # gmail/authorize, consented
    p = _plugin(callbacks=("authorize", "plg-sneaky"))
    issues = await _reconcile(registry, plugins=[p], acks=acks,
                              spool=_SpoolStub([]))
    assert [i.reason_code for i in issues] == ["callback_invalid"]
    assert acks.get(_identity()) is not None
    # and the consent is still there once the author fixes the declaration
    fixed = _plugin(callbacks=("authorize",))
    issues = await _reconcile(registry, plugins=[fixed], acks=acks,
                              spool=_SpoolStub([]), entries=_entries(fixed))
    assert issues == []
    assert registry.get_callback("plg-gmail--authorize") is not None


async def test_one_invalid_plugin_suppresses_the_whole_prune(tmp_path):
    """The prune is global and opportunistic: another plugin's unreadable
    declaration is enough reason to wait for a pass that can read everything."""
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks, plugin="ghost", declared="old")   # genuinely stale
    good = _plugin()
    _ack(acks)
    bad = _plugin(name="badone", artifact_id="art-9", callbacks=("plg-nope",))
    await _reconcile(registry, plugins=[good, bad], acks=acks,
                     spool=_SpoolStub([]), entries=_entries(good, bad))
    assert acks.get(_identity("ghost", "old")) is not None
    # the next clean pass prunes it
    await _reconcile(registry, plugins=[good], acks=acks,
                     spool=_SpoolStub([]), entries=_entries(good))
    assert acks.get(_identity("ghost", "old")) is None


async def test_prune_failure_never_breaks_the_reconcile(monkeypatch, tmp_path):
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)

    def _boom(valid_identities):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(acks, "prune_stale", _boom)
    p = _plugin()
    issues = await _reconcile(registry, plugins=[p], acks=acks,
                              spool=_SpoolStub([]))
    assert issues == []
    assert registry.get_callback("plg-gmail--authorize") is not None


# ---------------------------------------------------------------------------
# consent prompting
# ---------------------------------------------------------------------------


def _fresh_challenges(monkeypatch):
    import authz_grants
    import verdict_broker
    broker = verdict_broker.VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", broker)
    coord = authz_grants.ChallengeCoordinator()
    monkeypatch.setattr(authz_grants, "CHALLENGES", coord)
    return broker, coord


async def test_pending_consent_fires_one_prompt(monkeypatch, tmp_path):
    _fresh_challenges(monkeypatch)
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    telegram = _FakeTelegram()
    p = _plugin()
    await _reconcile(registry, plugins=[p], acks=acks, spool=_SpoolStub([]),
                     prompt=True, channel_manager=_FakeChannelManager(telegram))
    for _ in range(8):
        await asyncio.sleep(0)
    assert len(telegram.posts) == 1
    assert "/callback/plg-gmail--authorize" in telegram.posts[0][2]
    # a second reconcile dedupes onto the live challenge
    await _reconcile(registry, plugins=[p], acks=acks, spool=_SpoolStub([]),
                     prompt=True, channel_manager=_FakeChannelManager(telegram))
    for _ in range(8):
        await asyncio.sleep(0)
    assert len(telegram.posts) == 1


async def test_prompt_false_posts_nothing(monkeypatch, tmp_path):
    _fresh_challenges(monkeypatch)
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    telegram = _FakeTelegram()
    p = _plugin()
    await _reconcile(registry, plugins=[p], acks=acks, spool=_SpoolStub([]),
                     prompt=False,
                     channel_manager=_FakeChannelManager(telegram))
    for _ in range(8):
        await asyncio.sleep(0)
    assert telegram.posts == []


async def test_no_operator_channel_leaves_pending(monkeypatch, tmp_path):
    _fresh_challenges(monkeypatch)
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    p = _plugin()
    issues = await _reconcile(registry, plugins=[p], acks=acks,
                              spool=_SpoolStub([]), prompt=True,
                              channel_manager=_FakeChannelManager(None))
    assert [i.reason_code for i in issues] == ["callback_pending_ack"]


# ---------------------------------------------------------------------------
# health recomputability
# ---------------------------------------------------------------------------


async def test_current_issues_recomputes_from_active_runtime(
    monkeypatch, tmp_path,
):
    import agent as agent_mod
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    p = _plugin()
    runtime = SimpleNamespace(
        trigger_registry=_SpyRegistry(),
        role_configs=_role_configs(assistant=["telegram"]),
        channel_manager=None)
    monkeypatch.setattr(agent_mod, "active_runtime", runtime)
    spool = _SpoolStub([])
    monkeypatch.setattr(cr, "_default_resolver", lambda: _resolver([p]))
    monkeypatch.setattr(cr, "_default_entries", lambda: _entries(p))
    monkeypatch.setattr(cr, "_default_acks", lambda: acks)
    monkeypatch.setattr(cr, "_default_spool", lambda: spool)
    # #606: the runtime's registry here has never had a reconcile publish to
    # it, so its callback overlay is the ROUTING_UNAVAILABLE sentinel and the
    # honesty row leads. That is the point: "no authoritative routing
    # computation stands behind this" is now stated rather than silent.
    assert [i.reason_code for i in cr.current_issues()] == [
        "callback_routing_unavailable", "callback_pending_ack"]
    _ack(acks)
    # #453: the ack alone no longer makes the recomputation clean. The redirect
    # URI a setup tool registers with its provider is read out of the marker
    # pair, and only the reconcile's post-swap half writes it — so until it
    # does, the plugin still carries a gap and the setup gate holds.
    assert [i.reason_code for i in cr.current_issues()] == [
        "callback_routing_unavailable", "callback_spool_error"]
    desired = cr.compute_desired(role_configs=runtime.role_configs, acks=acks,
                                 resolver=_resolver([p]), entries=_entries(p))
    cr._publish_markers_post_swap(spool, desired, desired.routed)
    # Every per-plugin gap is now closed. The routing row remains, correctly:
    # nothing in this test ever ran a reconcile, so the registry still carries
    # no authoritative callback overlay.
    assert [i.reason_code for i in cr.current_issues()] == [
        "callback_routing_unavailable"]


async def test_current_issues_without_runtime_is_empty(monkeypatch):
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "active_runtime", None)
    assert cr.current_issues() == []


async def test_current_issues_never_raises(monkeypatch):
    import agent as agent_mod
    runtime = SimpleNamespace(role_configs=_role_configs(assistant=["x"]))
    monkeypatch.setattr(agent_mod, "active_runtime", runtime)

    def _boom():
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(cr, "_default_resolver", _boom)
    # #606: it still never RAISES — that is what this test is for — but it no
    # longer degrades to []. A live runtime with role configs whose fresh
    # compute exploded is exactly "could not compute", and reporting that as
    # "nothing is wrong" is the defect. This runtime carries no registry, so
    # only the recomputation row applies.
    assert [i.reason_code for i in cr.current_issues()] == [
        "callback_state_unavailable"]


# ---------------------------------------------------------------------------
# the runtime seam
# ---------------------------------------------------------------------------


async def test_reconcile_from_runtime_without_registry_is_a_noop():
    assert await cr.reconcile_from_runtime(None) == []
    assert await cr.reconcile_from_runtime(
        SimpleNamespace(trigger_registry=None)) == []


async def test_reconcile_from_runtime_uses_the_runtime_registry(
    monkeypatch, tmp_path,
):
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    monkeypatch.setattr(cr, "_default_resolver", lambda: _resolver([p]))
    monkeypatch.setattr(cr, "_default_entries", lambda: _entries(p))
    monkeypatch.setattr(cr, "_default_acks", lambda: acks)
    monkeypatch.setattr(cr, "_default_spool", lambda: _SpoolStub([]))
    runtime = SimpleNamespace(
        trigger_registry=registry,
        role_configs=_role_configs(assistant=["telegram"]),
        channel_manager=None)
    issues = await cr.reconcile_from_runtime(runtime, prompt=False)
    assert issues == []
    assert registry.get_callback("plg-gmail--authorize") is not None
