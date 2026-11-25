"""The store cleanup (#1679): what is named is kept, what is not waits, and a
pass that cannot read its references deletes nothing."""
import json
import logging
import os
import types

import pytest

import store_gc

A, B, C = "a" * 64, "b" * 64, "c" * 64
DAY = 86400
T0 = 1_800_000_000.0
_STEP = 3600


@pytest.fixture
def paths(tmp_path, monkeypatch):
    # The tests replay weeks of hourly passes; the real writer fsyncs each one.
    # The last test runs the real writer.
    monkeypatch.setattr(store_gc, "atomic_io", types.SimpleNamespace(
        atomic_write_json=lambda path, data, **_k: path.write_text(json.dumps(data))))
    p = store_gc.Paths(
        registry=tmp_path / "plugins" / "registry.json",
        plugin_store=tmp_path / "plugins" / "store",
        specialists_dir=tmp_path / "specialists",
        engagements=tmp_path / "data" / "engagements.json",
        ledger=tmp_path / "plugins" / ".store-gc.json")
    p.plugin_store.mkdir(parents=True)
    p.specialist_store.mkdir(parents=True)
    p.engagements.parent.mkdir()
    p.registry.write_text(json.dumps({"plugins": []}))
    return p


def _plugin(paths, eid, name="probe"):
    d = paths.plugin_store / name / eid
    d.mkdir(parents=True)
    (d / "plugin.json").write_text("{}")
    os.chmod(d / "plugin.json", 0o444)      # artifacts are frozen read-only
    os.chmod(d, 0o555)
    return d


def _component(paths, eid):
    d = paths.specialist_store / eid
    (d / "role").mkdir(parents=True)
    (d / "manifest.json").write_text("{}")
    return d


def _name_in_registry(paths, *ids):
    paths.registry.write_text(json.dumps(
        {"plugins": [{"name": "probe", "artifact_id": i} for i in ids]}))


def _hourly(paths, start, end):
    """Observe once an hour, as the running service does."""
    t = start
    while t <= end:
        assert store_gc.observe(paths=paths, now=t) is not None
        t += _STEP
    return t - _STEP


def _boot(paths, now, **kw):
    kw.setdefault("registry_valid", True)
    kw.setdefault("reconcile_ok", True)
    return store_gc.run_boot_sweep(paths=paths, now=now, **kw)


def test_an_unnamed_entry_goes_after_the_wait_and_not_before(paths):
    plugin, component = _plugin(paths, A), _component(paths, B)
    assert _boot(paths, T0) == {"plugins": [], "specialists": []}
    _hourly(paths, T0, T0 + 30 * DAY - 3600)
    assert _boot(paths, T0 + 30 * DAY - 1) == {"plugins": [], "specialists": []}
    assert plugin.exists() and component.exists()
    assert _boot(paths, T0 + 30 * DAY) == {"plugins": [A], "specialists": [B]}
    assert not plugin.exists() and not component.exists()
    assert not plugin.parent.exists()            # the emptied name directory
    assert json.loads(paths.ledger.read_text())["plugins"] == {}


@pytest.mark.parametrize("where", [
    "registry", "engagements", "active", "prior", "rollback_tmp", "owned_prior",
    "specialist_named_store", "specialists_root_file", "ops_journal"])
def test_an_entry_named_by_any_reference_file_is_never_removed(paths, where):
    plugin, component = _plugin(paths, A), _component(paths, A)
    text = f"tuple:\n  root: mtg@1.0.0#sha256:{A}\n"
    target = {
        "registry": paths.registry,
        "engagements": paths.engagements,
        "active": paths.specialists_dir / "mtg" / "active.yaml",
        "prior": paths.specialists_dir / "mtg" / "active.prior.yaml",
        "rollback_tmp": paths.specialists_dir / "mtg" / "active.yaml.rollback-tmp",
        "owned_prior": paths.specialists_dir / "mtg" / "owned-plugins.prior.yaml",
        "specialist_named_store": paths.specialist_store / "desired.yaml",
        "specialists_root_file": paths.specialists_dir / "note.yaml",
        "ops_journal": paths.specialists_dir / ".ops" / "mtg.1.json.quarantined",
    }[where]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"x": text}) if target.suffix == ".json" else text)
    _boot(paths, T0)
    last = _hourly(paths, T0, T0 + 40 * DAY)
    assert _boot(paths, last + 60) == {"plugins": [], "specialists": []}
    assert plugin.exists() and component.exists()
    assert json.loads(paths.ledger.read_text())["plugins"] == {}


def test_an_entry_named_just_before_the_start_is_kept_whatever_its_ledger_says(paths):
    plugin, component, now = _waited(paths)
    _name_in_registry(paths, A)
    (paths.specialists_dir / "mtg").mkdir()
    (paths.specialists_dir / "mtg" / "active.yaml").write_text(f"root: x@1#sha256:{B}\n")
    assert _boot(paths, now) == {"plugins": [], "specialists": []}
    assert plugin.exists() and component.exists()
    ledger = json.loads(paths.ledger.read_text())
    assert ledger["plugins"] == {} and ledger["specialists"] == {}


def test_a_file_two_levels_down_does_not_name_an_entry(paths):
    _plugin(paths, A)
    deep = paths.specialists_dir / "mtg" / "sub" / "x.yaml"
    deep.parent.mkdir(parents=True)
    deep.write_text(A)
    _boot(paths, T0)
    last = _hourly(paths, T0, T0 + 30 * DAY)
    assert _boot(paths, last + 60)["plugins"] == [A]


def test_being_named_again_between_two_starts_restarts_the_wait(paths):
    """Design round 2: A superseded on day 0, selected again on day 10 and
    superseded again on day 29, with no restart in between."""
    plugin = _plugin(paths, A)
    _plugin(paths, B)
    _name_in_registry(paths, B)
    _boot(paths, T0)
    last = _hourly(paths, T0, T0 + 10 * DAY)
    _name_in_registry(paths, A)
    last = _hourly(paths, last + 3600, T0 + 29 * DAY)
    _name_in_registry(paths, B)
    last = _hourly(paths, last + 3600, T0 + 31 * DAY)
    assert _boot(paths, last + 60) == {"plugins": [], "specialists": []}
    assert plugin.exists()
    last = _hourly(paths, last + 3600, T0 + 59 * DAY + 2 * 3600)
    assert _boot(paths, last + 60)["plugins"] == [A]


def test_a_gap_in_observation_restarts_every_wait(paths):
    """Design round 3: while nobody observed (Casa stopped, or an older image
    ran) an entry may have been named, so its old time is not trusted."""
    plugin = _plugin(paths, A)
    _component(paths, B)

    def times():
        ledger = json.loads(paths.ledger.read_text())
        return ledger["plugins"], ledger["specialists"]

    _boot(paths, T0)
    last = _hourly(paths, T0, T0 + 29 * DAY)
    edge = last + 7200                 # exactly two hours: still continuous
    store_gc.observe(paths=paths, now=edge)
    assert times() == ({A: T0}, {B: T0})
    after_gap = edge + 7201            # one second more: not continuous
    store_gc.observe(paths=paths, now=after_gap)
    assert times() == ({A: after_gap}, {B: after_gap})
    _hourly(paths, after_gap + 3600, after_gap + 30 * DAY - 3600)
    assert _boot(paths, after_gap + 30 * DAY - 1)["plugins"] == []
    assert plugin.exists()
    assert _boot(paths, after_gap + 30 * DAY)["plugins"] == [A]


def test_an_existing_empty_registry_skips_the_pass(paths):
    _plugin(paths, A)
    store_gc.observe(paths=paths, now=T0)
    before = paths.ledger.read_text()
    paths.registry.write_text("")
    assert store_gc.observe(paths=paths, now=T0 + 3600) is None
    assert paths.ledger.read_text() == before


def test_a_missing_or_broken_ledger_only_delays(paths):
    plugin = _plugin(paths, A)
    _boot(paths, T0)
    last = _hourly(paths, T0, T0 + 31 * DAY)
    paths.ledger.write_text("{not json")
    restart = last + 60
    assert _boot(paths, restart)["plugins"] == []
    assert plugin.exists()
    # the broken ledger was replaced, and the wait runs from that start
    assert json.loads(paths.ledger.read_text())["plugins"] == {A: restart}
    _hourly(paths, restart + 3600, restart + 30 * DAY - 3600)
    assert _boot(paths, restart + 30 * DAY)["plugins"] == [A]
    other = _plugin(paths, B)
    paths.ledger.unlink()
    assert _boot(paths, restart + 30 * DAY + 60)["plugins"] == []
    assert other.exists()


def test_a_ledger_time_in_the_future_is_reset(paths):
    _plugin(paths, A)
    paths.ledger.write_text(json.dumps(
        {"plugins": {A: T0 + 99 * DAY}, "specialists": {}, "last_observed": T0 - 60}))
    store_gc.observe(paths=paths, now=T0)
    assert json.loads(paths.ledger.read_text())["plugins"] == {A: T0}


def _waited(paths):
    plugin, component = _plugin(paths, A), _component(paths, B)
    _boot(paths, T0)
    return plugin, component, _hourly(paths, T0, T0 + 31 * DAY) + 60


@pytest.mark.parametrize("kw", [{"registry_valid": False}, {"reconcile_ok": False}])
def test_the_sweep_is_skipped_when_boot_says_so(paths, kw):
    plugin, component, now = _waited(paths)
    before = paths.ledger.read_text()
    assert _boot(paths, now, **kw) == {"plugins": [], "specialists": []}
    assert plugin.exists() and component.exists()
    assert paths.ledger.read_text() == before


@pytest.mark.parametrize("breakage", [
    "registry_not_json", "registry_is_dir", "engagements_is_dir", "slug_file_is_dir_loop"])
def test_an_unreadable_reference_source_deletes_nothing(paths, breakage):
    plugin, component, now = _waited(paths)
    before = paths.ledger.read_text()
    if breakage == "registry_not_json":
        paths.registry.write_text("{torn")
    elif breakage == "registry_is_dir":
        paths.registry.unlink()
        paths.registry.mkdir()
    elif breakage == "engagements_is_dir":
        paths.engagements.mkdir()
    else:
        # a specialists root that cannot be listed
        paths.specialist_store.rename(paths.specialists_dir.parent / "kept-store")
        paths.specialists_dir.rmdir()
        paths.specialists_dir.write_text("not a directory")
    assert _boot(paths, now) == {"plugins": [], "specialists": []}
    assert plugin.exists()
    assert paths.ledger.read_text() == before


def test_one_unreadable_specialist_file_deletes_nothing(paths, monkeypatch):
    plugin, component, now = _waited(paths)
    before = paths.ledger.read_text()
    tuple_file = paths.specialists_dir / "mtg" / "active.yaml"
    tuple_file.parent.mkdir()
    tuple_file.write_text("root: unknown\n")
    real = type(tuple_file).read_bytes

    def read_bytes(self):
        if self == tuple_file:
            raise PermissionError(13, "denied")
        return real(self)

    monkeypatch.setattr(type(tuple_file), "read_bytes", read_bytes)
    assert _boot(paths, now) == {"plugins": [], "specialists": []}
    assert plugin.exists() and component.exists()
    assert paths.ledger.read_text() == before


def test_only_real_hex_named_directories_are_candidates(paths, tmp_path):
    outside = tmp_path / "outside" / A
    outside.mkdir(parents=True)
    (outside / "keep").write_text("x")
    (paths.plugin_store / "probe").mkdir()
    os.symlink(outside, paths.plugin_store / "probe" / A)        # symlinked entry
    os.symlink(outside.parent, paths.plugin_store / "linked")    # symlinked name dir
    staging = paths.plugin_store / "probe" / f".import-{B}"
    staging.mkdir()
    odd = paths.specialist_store / ".staging" / "abc"
    odd.mkdir(parents=True)
    upper = paths.specialist_store / ("A" * 64)
    upper.mkdir()
    _boot(paths, T0)
    last = _hourly(paths, T0, T0 + 31 * DAY)
    assert _boot(paths, last + 60) == {"plugins": [], "specialists": []}
    assert (outside / "keep").exists() and staging.exists() and odd.exists() and upper.exists()


def test_the_time_budget_leaves_the_rest_for_a_later_start(paths, monkeypatch):
    first, second = _plugin(paths, A), _plugin(paths, B)
    _boot(paths, T0)
    last = _hourly(paths, T0, T0 + 31 * DAY)

    clock = {"now": 0.0}
    real_observe, real_remove = store_gc.observe, store_gc._remove_tree

    def slow_observe(**kw):          # observing takes the whole budget
        clock["now"] += 30.0
        return real_observe(**kw)

    def slow_remove(entry):          # so does one removal
        clock["now"] += 30.0
        real_remove(entry)

    fake_time = types.SimpleNamespace(time=lambda: T0, monotonic=lambda: clock["now"])
    # The budget runs out while the pass is still observing: nothing is started.
    with monkeypatch.context() as patched:
        patched.setattr(store_gc, "time", fake_time)
        patched.setattr(store_gc, "observe", slow_observe)
        assert _boot(paths, last + 60, budget_seconds=30.0)["plugins"] == []
    assert first.exists() and second.exists()
    # It runs out during the first removal: the second waits for a later start.
    with monkeypatch.context() as patched:
        patched.setattr(store_gc, "time", fake_time)
        patched.setattr(store_gc, "_remove_tree", slow_remove)
        assert _boot(paths, last + 120, budget_seconds=30.0)["plugins"] == [A]
    assert not first.exists() and second.exists()
    assert _boot(paths, last + 180)["plugins"] == [B]


def test_a_removal_that_fails_is_tried_again(paths, monkeypatch, caplog):
    plugin = _plugin(paths, A)
    _boot(paths, T0)
    last = _hourly(paths, T0, T0 + 31 * DAY)
    def refuse(entry):
        raise OSError("busy")

    with monkeypatch.context() as patched:
        patched.setattr(store_gc, "_remove_tree", refuse)
        with caplog.at_level(logging.WARNING):
            assert _boot(paths, last + 60)["plugins"] == []
    assert plugin.exists() and "was not removed" in caplog.text
    assert _boot(paths, last + 120)["plugins"] == [A]


def test_observe_never_deletes_and_never_raises(paths, monkeypatch):
    plugin, component, now = _waited(paths)
    assert store_gc.observe(paths=paths, now=now) is not None
    assert plugin.exists() and component.exists()
    monkeypatch.setattr(store_gc, "_observe", lambda *a: 1 / 0)
    assert store_gc.observe(paths=paths, now=now) is None


def test_nothing_on_a_fresh_install(tmp_path):
    p = store_gc.Paths(
        registry=tmp_path / "plugins" / "registry.json",
        plugin_store=tmp_path / "plugins" / "store",
        specialists_dir=tmp_path / "specialists",
        engagements=tmp_path / "data" / "engagements.json",
        ledger=tmp_path / "plugins" / ".store-gc.json")
    assert _boot(p, T0) == {"plugins": [], "specialists": []}
