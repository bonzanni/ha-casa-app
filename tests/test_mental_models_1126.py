"""#1126 — the mental-model overlay's render, the reconcile of Casa's declared
models, and the passes' lifecycle (boot, after a wipe, shutdown).

The backend is driven through the real ``HindsightSemanticMemory`` with only
``_request`` replaced by a stateful fake of Hindsight 0.10.2's mental-model
routes; no socket is opened."""
from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest

import mental_models
from hindsight_memory import HindsightSemanticMemory
from semantic_memory import (
    MENTAL_MODEL_OVERLAY_LABEL,
    NoOpSemanticMemory,
    SemanticMemory,
    mental_model_refresh_paused,
    render_mental_models,
)
from timekeeping import resolve_tz

pytestmark = [pytest.mark.unit]

BASE = "/v1/default/banks/casa/mental-models"


@pytest.fixture
def amsterdam(monkeypatch):
    monkeypatch.setenv("CASA_TZ", "Europe/Amsterdam")
    resolve_tz.cache_clear()
    yield
    resolve_tz.cache_clear()


@pytest.fixture(autouse=True)
def _fresh_lifecycle(monkeypatch):
    monkeypatch.setattr(mental_models, "_frozen", False)
    monkeypatch.setattr(mental_models, "_tasks", set())
    monkeypatch.setattr(mental_models, "_lock", None)
    monkeypatch.setattr(mental_models, "_lock_loop", None)


def _http(status: int) -> aiohttp.ClientResponseError:
    return aiohttp.ClientResponseError(request_info=None, history=(), status=status)


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def _model(**kw):
    base = {"id": "m", "name": "Model", "content": "BODY",
            "last_refreshed_at": "2026-09-30T05:00:00Z",
            "last_refresh_failed_at": None}
    base.update(kw)
    return base


def test_render_dates_in_the_operator_timezone(amsterdam):
    # 22:30 UTC on the 30th is already the 1st in Amsterdam (CEST, +2).
    out = render_mental_models({"items": [
        _model(name="Open commitments", last_refreshed_at="2026-09-30T22:30:00Z"),
    ]})
    assert out.splitlines() == [
        MENTAL_MODEL_OVERLAY_LABEL,
        "",
        "### Open commitments (refreshed Thu 1 Oct 2026)",
        "BODY",
    ]


@pytest.mark.parametrize("failed, refreshed, paused", [
    (None, "2026-09-30T05:00:00Z", False),                    # never failed
    (None, None, False),                                      # first refresh in flight
    ("2026-09-29T05:00:00Z", "2026-09-30T05:00:00Z", False),  # failure since recovered
    ("2026-09-30T05:00:00Z", "2026-09-30T05:00:00Z", False),  # equal is not later
    ("2026-10-01T05:00:00Z", "2026-09-30T05:00:00Z", True),   # failed after success
    ("2026-10-01T05:00:00Z", None, True),                     # failed, never refreshed
    ("not a date", "2026-09-30T05:00:00Z", False),            # unreadable failure
])
def test_paused_predicate_is_the_servers(failed, refreshed, paused):
    model = {"last_refresh_failed_at": failed, "last_refreshed_at": refreshed}
    assert mental_model_refresh_paused(model) is paused


def test_render_notes_a_paused_model_only(amsterdam):
    out = render_mental_models({"items": [
        _model(id="a", name="Paused", last_refresh_failed_at="2026-10-01T05:00:00Z"),
        _model(id="b", name="Recovered", last_refresh_failed_at="2026-09-29T05:00:00Z"),
    ]})
    assert out.count("last refresh failed") == 1
    assert ("### Paused (refreshed Wed 30 Sep 2026; its last refresh failed "
            "Thu 1 Oct 2026, so it may be out of date)") in out
    assert "### Recovered (refreshed Wed 30 Sep 2026)" in out


def test_render_skips_empty_undated_and_odd_items(amsterdam):
    items = [
        _model(content=""), _model(content="   "), _model(content=None),
        _model(last_refreshed_at=None), _model(last_refreshed_at="2026-09-30T05:00:00"),
        "not a dict", None, 7,
    ]
    assert render_mental_models({"items": items}) == ""
    assert render_mental_models({"items": "nope"}) == ""
    assert render_mental_models(None) == ""
    out = render_mental_models({"items": [*items, _model(name=None, id="casa-x")]})
    assert out.count("### ") == 1 and "### casa-x (refreshed" in out
    assert out.startswith(MENTAL_MODEL_OVERLAY_LABEL)


async def test_profile_404_is_no_overlay_and_other_failures_raise():
    mem = HindsightSemanticMemory("http://hs:8888")

    async def missing(method, path, payload=None, **_):
        raise _http(404)

    mem._request = missing
    assert await mem.profile("casa") == ""

    async def broken(method, path, payload=None, **_):
        raise _http(500)

    mem._request = broken
    with pytest.raises(aiohttp.ClientResponseError):
        await mem.profile("casa")


def test_reconcile_seam_is_concrete_and_document_tags_stays_abstract():
    assert "reconcile_mental_models" not in SemanticMemory.__abstractmethods__
    assert "document_tags" in SemanticMemory.__abstractmethods__
    assert NoOpSemanticMemory.reconcile_mental_models is SemanticMemory.reconcile_mental_models


# ---------------------------------------------------------------------------
# Reconcile — a stateful fake of the 0.10.2 routes
# ---------------------------------------------------------------------------


def _stored(spec, **kw):
    """A declared model as the server stores it: the full trigger dump (its own
    defaults included) merged under the declared keys, content present."""
    trigger = {"mode": "full", "refresh_after_consolidation": False,
               "refresh_cron": None, "min_refresh_interval_seconds": None,
               "exclude_mental_models": False, "fact_types": None}
    trigger.update(spec.trigger)
    row = {"id": spec.id, "bank_id": "casa", "name": spec.name, "tags": [],
           "source_query": spec.source_query, "max_tokens": spec.max_tokens,
           "trigger": trigger, "content": "c",
           "last_refreshed_at": "2026-09-30T05:00:00Z", "last_refresh_failed_at": None}
    row.update(kw)
    return row


COMMIT, PROFILE = mental_models.DECLARED


class FakeHindsight:
    def __init__(self, models=(), *, bank_exists=True, fail=None, total=None):
        self.models = {m["id"]: dict(m) for m in models}
        self.bank_exists = bank_exists
        self.fail = dict(fail or {})
        self.total = total
        self.calls: list[tuple[str, str, object]] = []
        self.active = 0
        self.peak = 0

    def writes(self):
        return [(m, p, b) for m, p, b in self.calls if m != "GET"]

    async def request(self, method, path, payload=None, *, retry_on_drop=True):
        self.calls.append((method, path, payload))
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0)
            route = urlsplit(path).path
            assert route.startswith(BASE), route
            rest = route[len(BASE):]
            if (method, rest) in self.fail:
                raise self.fail[(method, rest)]
            if method == "GET" and rest == "":
                if not self.bank_exists:
                    raise _http(404)
                assert parse_qs(urlsplit(path).query).get("detail") == ["content"]
                items = list(self.models.values())
                return {"items": items, "total": self.total or len(items),
                        "limit": 1000, "offset": 0}
            if method == "POST" and rest == "":
                if payload["id"] in self.models:
                    raise _http(409)
                self.bank_exists = True
                self.models[payload["id"]] = {**payload, "content": "",
                                              "last_refreshed_at": None,
                                              "last_refresh_failed_at": None}
                return {"mental_model_id": payload["id"], "operation_id": "op"}
            model_id = rest.strip("/").split("/")[0]
            if model_id not in self.models:
                raise _http(404)
            if method == "PATCH":
                self.models[model_id].update(payload)
                return self.models[model_id]
            if method == "DELETE":
                del self.models[model_id]
                return {"status": "deleted"}
            if method == "POST" and rest.endswith("/refresh"):
                return {"operation_id": "op", "status": "queued"}
            raise AssertionError((method, path))
        finally:
            self.active -= 1


def _mem(fake):
    mem = HindsightSemanticMemory("http://hs:8888")
    mem._request = fake.request
    return mem


def _create_body(spec):
    return {"id": spec.id, "name": spec.name, "source_query": spec.source_query,
            "tags": [], "max_tokens": spec.max_tokens, "trigger": dict(spec.trigger)}


def test_declarations_are_the_ruled_ones():
    assert [s.id for s in mental_models.DECLARED] == [
        "casa-open-commitments", "casa-operator-profile"]
    assert COMMIT.max_tokens == PROFILE.max_tokens == 1024
    assert dict(COMMIT.trigger) == {"refresh_cron": "0 5 * * *"}
    assert dict(PROFILE.trigger) == {"refresh_after_consolidation": True,
                                     "min_refresh_interval_seconds": 86400}
    for spec in mental_models.DECLARED:
        assert "exclude_mental_models" not in spec.trigger
        assert spec.id.startswith(mental_models.RESERVED_PREFIX)
    assert "health, money and other people's private matters" in PROFILE.source_query


@pytest.mark.parametrize("bank_exists", [False, True])
async def test_missing_models_are_created_with_their_declared_bodies(bank_exists):
    # A missing bank (after a wipe, fresh install) answers the list with 404.
    fake = FakeHindsight(bank_exists=bank_exists)
    await _mem(fake).reconcile_mental_models("casa", mental_models.DECLARED)
    assert fake.writes() == [("POST", BASE, _create_body(COMMIT)),
                             ("POST", BASE, _create_body(PROFILE))]


@pytest.mark.parametrize("failure", [_http(500), _http(503), aiohttp.ClientConnectionError(),
                                     asyncio.TimeoutError()])
async def test_an_unreadable_list_writes_nothing(failure):
    fake = FakeHindsight(fail={("GET", ""): failure})
    with pytest.raises(type(failure)):
        await _mem(fake).reconcile_mental_models("casa", mental_models.DECLARED)
    assert fake.writes() == []


async def test_a_malformed_list_writes_nothing():
    mem = HindsightSemanticMemory("http://hs:8888")
    calls = []

    async def odd(method, path, payload=None, **_):
        calls.append(method)
        return {"items": "nope"}

    mem._request = odd
    with pytest.raises(Exception):
        await mem.reconcile_mental_models("casa", mental_models.DECLARED)
    assert calls == ["GET"]


async def test_stored_defaults_are_not_drift():
    fake = FakeHindsight([_stored(COMMIT), _stored(PROFILE)])
    await _mem(fake).reconcile_mental_models("casa", mental_models.DECLARED)
    assert fake.writes() == []


async def test_a_drifted_key_is_patched_alone_then_refreshed_once():
    fake = FakeHindsight([_stored(COMMIT, source_query="edited"),
                          _stored(PROFILE, trigger={"refresh_cron": "0 3 * * *",
                                                    "refresh_after_consolidation": False})])
    await _mem(fake).reconcile_mental_models("casa", mental_models.DECLARED)
    assert fake.writes() == [
        ("PATCH", f"{BASE}/{COMMIT.id}", {"source_query": COMMIT.source_query}),
        ("POST", f"{BASE}/{COMMIT.id}/refresh", None),
        ("PATCH", f"{BASE}/{PROFILE.id}", {"trigger": dict(PROFILE.trigger)}),
        ("POST", f"{BASE}/{PROFILE.id}/refresh", None),
    ]


async def test_tags_name_and_max_tokens_drift():
    fake = FakeHindsight([_stored(COMMIT, tags=["x"], name="N", max_tokens=2048),
                          _stored(PROFILE)])
    await _mem(fake).reconcile_mental_models("casa", mental_models.DECLARED)
    assert fake.writes() == [
        ("PATCH", f"{BASE}/{COMMIT.id}",
         {"name": COMMIT.name, "max_tokens": 1024, "tags": []}),
        ("POST", f"{BASE}/{COMMIT.id}/refresh", None),
    ]


@pytest.mark.parametrize("failed, refreshed, refreshes", [
    (None, None, 0),                                       # first refresh in flight
    (None, "2026-09-30T05:00:00Z", 0),
    ("2026-09-29T05:00:00Z", "2026-09-30T05:00:00Z", 0),
    ("2026-10-01T05:00:00Z", "2026-09-30T05:00:00Z", 1),
    ("2026-10-01T05:00:00Z", None, 1),
])
async def test_a_paused_model_gets_one_refresh(failed, refreshed, refreshes):
    fake = FakeHindsight([
        _stored(COMMIT, last_refresh_failed_at=failed, last_refreshed_at=refreshed),
        _stored(PROFILE),
    ])
    await _mem(fake).reconcile_mental_models("casa", mental_models.DECLARED)
    assert fake.writes() == [("POST", f"{BASE}/{COMMIT.id}/refresh", None)] * refreshes


async def test_patched_and_paused_is_still_one_refresh():
    fake = FakeHindsight([
        _stored(COMMIT, source_query="edited",
                last_refresh_failed_at="2026-10-01T05:00:00Z"),
        _stored(PROFILE),
    ])
    await _mem(fake).reconcile_mental_models("casa", mental_models.DECLARED)
    assert [m for m, p, b in fake.writes()].count("POST") == 1


async def test_a_failed_patch_never_refreshes_and_the_other_model_proceeds():
    fake = FakeHindsight(
        [_stored(COMMIT, source_query="edited",
                 last_refresh_failed_at="2026-10-01T05:00:00Z")],
        fail={("PATCH", f"/{COMMIT.id}"): _http(500)},
    )
    await _mem(fake).reconcile_mental_models("casa", mental_models.DECLARED)
    assert fake.writes() == [
        ("PATCH", f"{BASE}/{COMMIT.id}", {"source_query": COMMIT.source_query}),
        ("POST", BASE, _create_body(PROFILE)),
    ]


async def test_undeclared_casa_ids_are_deleted_and_no_other_id_is_written():
    ops = {"id": "ops-model", "name": "Ops", "tags": ["t"], "source_query": "q",
           "max_tokens": 9, "trigger": {}, "content": "c",
           "last_refreshed_at": None, "last_refresh_failed_at": "2026-10-01T05:00:00Z"}
    fake = FakeHindsight([_stored(COMMIT), _stored(PROFILE), dict(ops, id="casa-x"), ops])
    await _mem(fake).reconcile_mental_models("casa", mental_models.DECLARED)
    assert fake.writes() == [("DELETE", f"{BASE}/casa-x", None)]
    assert [p for m, p, b in fake.calls if "ops-model" in p] == []
    assert set(fake.models) == {COMMIT.id, PROFILE.id, "ops-model"}


async def test_an_incomplete_list_deletes_nothing():
    fake = FakeHindsight([_stored(COMMIT), _stored(PROFILE),
                          _stored(COMMIT, id="casa-x")], total=1500)
    await _mem(fake).reconcile_mental_models("casa", mental_models.DECLARED)
    assert fake.writes() == []
    fake = FakeHindsight([_stored(COMMIT, id="casa-x")], total=1500)
    await _mem(fake).reconcile_mental_models("casa", mental_models.DECLARED)
    assert [m for m, p, b in fake.writes()] == ["POST", "POST"]


async def test_conflict_and_gone_answers_are_not_failures(caplog):
    fake = FakeHindsight(
        [_stored(COMMIT, source_query="edited"),
         _stored(PROFILE, last_refresh_failed_at="2026-10-01T05:00:00Z"),
         _stored(COMMIT, id="casa-gone")],
        fail={("PATCH", f"/{COMMIT.id}"): _http(404),
              ("POST", f"/{PROFILE.id}/refresh"): _http(404),
              ("DELETE", "/casa-gone"): _http(404)},
    )
    with caplog.at_level("WARNING", logger="hindsight_memory"):
        await _mem(fake).reconcile_mental_models("casa", mental_models.DECLARED)
    assert [m for m, p, b in fake.writes()] == ["PATCH", "POST", "DELETE"]
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    # 409 on a create = present (another creator, or a retried create that landed).
    fake = FakeHindsight([_stored(PROFILE)], fail={("POST", ""): _http(409)})
    with caplog.at_level("WARNING", logger="hindsight_memory"):
        await _mem(fake).reconcile_mental_models("casa", mental_models.DECLARED)
    assert [m for m, p, b in fake.writes()] == ["POST"]
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


async def test_a_refresh_is_sent_once_when_its_connection_drops():
    mem = HindsightSemanticMemory("http://hs:8888")
    wire: list[tuple[str, str]] = []
    rows = [_stored(COMMIT, last_refresh_failed_at="2026-10-01T05:00:00Z"),
            _stored(PROFILE)]

    async def roundtrip(method, url, payload):
        wire.append((method, url))
        if url.endswith("/refresh"):
            raise aiohttp.ServerDisconnectedError()
        return {"items": rows, "total": 2}

    mem._roundtrip = roundtrip
    await mem.reconcile_mental_models("casa", mental_models.DECLARED)
    assert [u for m, u in wire if u.endswith("/refresh")] == [
        f"http://hs:8888{BASE}/{COMMIT.id}/refresh"]
    await mem.close()


# ---------------------------------------------------------------------------
# Lifecycle — lock, tasks, drain
# ---------------------------------------------------------------------------


class _Gate:
    """A backend whose pass blocks until released, recording entry order."""

    def __init__(self, name, log, release=None):
        self.name, self.log = name, log
        self.release = release or asyncio.Event()

    async def reconcile_mental_models(self, bank, declared):
        self.log.append(f"{self.name}:start")
        await self.release.wait()
        self.log.append(f"{self.name}:end")


async def test_a_pass_never_raises_and_tolerates_a_backend_without_the_method(caplog):
    class Raising:
        async def reconcile_mental_models(self, bank, declared):
            raise RuntimeError("boom")

    with caplog.at_level("WARNING", logger="mental_models"):
        await mental_models.reconcile(Raising(), "casa", reason="boot")
        await mental_models.reconcile(object(), "casa", reason="wipe")
    errors = [r.getMessage() for r in caplog.records if "outcome=aborted" in r.getMessage()]
    assert len(errors) == 2
    assert "error=RuntimeError" in errors[0] and "error=AttributeError" in errors[1]


async def test_passes_are_serialised_in_arrival_order():
    log: list[str] = []
    boot = _Gate("boot", log)
    wipe = _Gate("wipe", log, release=boot.release)
    t1 = mental_models.schedule_reconcile(boot, "casa", reason="boot")
    await asyncio.sleep(0)
    t2 = mental_models.schedule_reconcile(wipe, "casa", reason="wipe")
    for _ in range(5):
        await asyncio.sleep(0)
    assert log == ["boot:start"]
    boot.release.set()
    await asyncio.gather(t1, t2)
    assert log == ["boot:start", "boot:end", "wipe:start", "wipe:end"]


async def test_two_concurrent_passes_create_each_model_once():
    fake = FakeHindsight(bank_exists=False)
    mem = _mem(fake)
    await asyncio.gather(
        mental_models.schedule_reconcile(mem, "casa", reason="boot"),
        mental_models.schedule_reconcile(mem, "casa", reason="wipe"),
    )
    assert [m for m, p, b in fake.writes()] == ["POST", "POST"]
    assert fake.peak == 1


async def test_drain_cancels_running_passes_then_refuses_new_ones():
    log: list[str] = []
    task = mental_models.schedule_reconcile(_Gate("boot", log), "casa", reason="boot")
    await asyncio.sleep(0)
    assert log == ["boot:start"] and not task.done()
    await mental_models.drain_reconcile_tasks()
    assert task.cancelled()
    assert mental_models._tasks == set()
    assert mental_models.schedule_reconcile(_Gate("late", log), "casa", reason="wipe") is None
    assert log == ["boot:start"]


# ---------------------------------------------------------------------------
# After a wipe
# ---------------------------------------------------------------------------


def _registry(tmp_path):
    from session_registry import SessionRegistry
    return SessionRegistry(str(tmp_path / "sessions.json"))


class _WipeSem:
    def __init__(self, order, *, explode=False, hang=None):
        self.order, self.explode, self.hang = order, explode, hang

    async def delete_bank(self, bank):
        self.order.append("delete_bank")
        return True

    async def retain(self, bank, items, *, async_=True):
        return None

    async def reconcile_mental_models(self, bank, declared):
        self.order.append(f"reconcile:{bank}")
        if self.hang is not None:
            await self.hang.wait()
        if self.explode:
            raise RuntimeError("memory server down")


async def _wipe(tmp_path, sem):
    from memory_wipe import RetainFence, wipe_long_term_memory
    return await wipe_long_term_memory(
        registry=_registry(tmp_path), semantic_memory=sem, fence=RetainFence(),
        bank="casa", retry_dir=tmp_path / "nospool",
    )


async def test_a_wipe_returns_its_report_before_the_pass_runs(tmp_path):
    order: list[str] = []
    hang = asyncio.Event()
    report = await _wipe(tmp_path, _WipeSem(order, hang=hang))
    assert report.bank_deleted is True
    assert order == ["delete_bank"]            # the pass has not started yet
    (task,) = mental_models._tasks
    await asyncio.sleep(0)
    assert order == ["delete_bank", "reconcile:casa"] and not task.done()
    hang.set()
    await task


@pytest.mark.parametrize("sem_factory", [
    lambda order: _WipeSem(order, explode=True),
    lambda order: type("Bare", (), {
        "delete_bank": _WipeSem.delete_bank, "retain": _WipeSem.retain,
        "__init__": lambda self, o: setattr(self, "order", o)})(order),
])
async def test_a_failing_or_absent_pass_never_changes_the_wipe(tmp_path, sem_factory):
    order: list[str] = []
    report = await _wipe(tmp_path, sem_factory(order))
    summary = report.summary()
    (task,) = mental_models._tasks
    await task                                  # the pass swallowed its failure
    assert report.bank_deleted is True and report.summary() == summary


async def test_an_aborted_wipe_schedules_no_pass(tmp_path):
    order: list[str] = []

    class Exploding(_WipeSem):
        async def delete_bank(self, bank):
            raise RuntimeError("backend down")

    with pytest.raises(RuntimeError):
        await _wipe(tmp_path, Exploding(order))
    assert mental_models._tasks == set()
    assert order == []


# ---------------------------------------------------------------------------
# Boot and shutdown wiring (structural: casa_core.main is not runnable here)
# ---------------------------------------------------------------------------


def test_boot_schedules_a_pass_for_a_real_backend_and_shutdown_drains_before_close():
    import casa_core
    from module_source import module_function_source

    src = module_function_source(casa_core, "main")
    built = src.index("semantic_memory = build_semantic_memory(")
    guard = src.index("if not isinstance(semantic_memory, _NoOpSemanticMemory):", built)
    boot = src.index('reason="boot"', guard)
    assert built < guard < boot < built + 1200
    src = module_function_source(casa_core, "_shutdown_cleanup")
    broker = src.index("await _drain_broker_before_channel_shutdown(channel_manager)")
    drain = src.index("await _mental_models_shutdown.drain_reconcile_tasks()")
    close = src.index("await semantic_memory.close()")
    assert broker < drain < close
    assert src.count("drain_reconcile_tasks()") == 1
