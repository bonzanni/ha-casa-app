"""A cancelled plugin mutation keeps the plugin-tools lock until its worker
thread has finished.

`asyncio.to_thread` cancels the await, never the thread. A plugin tool that
holds `_PLUGIN_TOOLS_LOCK` across a raw thread hop releases the lock the moment
its caller is cancelled (an operator cancel, a run's time limit, shutdown) while
the worker keeps running. The next mutation then takes the lock, writes the
registry and reports success, and the old worker's save of the registry it read
earlier lands last and drops that entry.

The behavioural test drives the real `plugin_add` handler (its network and
filesystem seams doubled by `test_plugin_tools._wire`) with the publish step
held on a `threading.Event`. The structural test pins the rule for every plugin
tool: each thread hop that runs while the lock is held settles through
cancellation.
"""
from __future__ import annotations

import ast
import asyncio
import json
import threading
from pathlib import Path

import pytest

from test_plugin_tools import _State, _pr, _wire

pytestmark = pytest.mark.unit

_TOOLS_PY = (Path(__file__).resolve().parents[1]
             / "casa" / "rootfs" / "opt" / "casa" / "tools.py")


def _add_args(name: str) -> dict:
    return {"name": name, "repo": "o/r", "ref": "v1",
            "targets": ["resident:assistant"]}


async def test_cancelled_plugin_add_holds_the_lock_until_its_worker_finishes(
        monkeypatch, tmp_path):
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st)
    monkeypatch.setattr(tools_mod, "_PLUGIN_TOOLS_LOCK", asyncio.Lock())
    monkeypatch.setattr(tools_mod, "_PLUGIN_TOOLS_LOCK_OWNER", None)

    first_in_publish = threading.Event()
    release_first = threading.Event()
    first_worker_done = threading.Event()

    def publish(*, name, repo, ref, subdir="", commit=None):
        st.log.append(f"publish:{name}")
        if name == "first":
            first_in_publish.set()
            try:
                # Bounded so a broken run fails instead of hanging the suite.
                assert release_first.wait(10), "test never released the worker"
            finally:
                first_worker_done.set()
        return _pr(name=name)

    monkeypatch.setattr(tools_mod.plugin_store, "publish", publish)

    first = asyncio.create_task(tools_mod.plugin_add.handler(_add_args("first")))
    assert await asyncio.to_thread(first_in_publish.wait, 10)
    first.cancel()
    # Let the cancellation reach the handler before the second call queues.
    for _ in range(5):
        await asyncio.sleep(0)

    second = asyncio.create_task(tools_mod.plugin_add.handler(_add_args("second")))
    done, _ = await asyncio.wait({second}, timeout=0.5)
    entered_while_worker_ran = (
        "publish:second" in st.log and not first_worker_done.is_set())

    release_first.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    payload = json.loads((await second)["content"][0]["text"])

    assert not entered_while_worker_ran, (
        "a second plugin mutation ran while the cancelled call's worker was "
        f"still writing: {st.log}")
    assert not done
    assert payload["ok"] is True
    # The cancelled call's worker committed; the second call's entry survives.
    assert sorted(e["name"] for e in st.raw["plugins"]) == ["first", "second"]


# --- every thread hop under the lock settles ---------------------------------

def _is_lock_hold(node: ast.AsyncWith) -> bool:
    for item in node.items:
        text = ast.unparse(item.context_expr)
        if text in ("_PLUGIN_TOOLS_LOCK", "_plugin_tools_guard()", "lock") \
                or text.startswith("_plugin_tools_reload_guard("):
            return True
    return False


def _unsettled_hops_under_the_plugin_lock() -> list[str]:
    """Each `asyncio.to_thread` call reachable from inside a plugin-lock hold in
    tools.py — lexically, or through a tools.py coroutine awaited there — that
    is not the direct argument of `_settle_through_cancellation`."""
    tree = ast.parse(_TOOLS_PY.read_text())
    defs: dict[str, list[ast.AST]] = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)):
            defs.setdefault(n.name, []).append(n)
    parents = {child: parent for parent in ast.walk(tree)
               for child in ast.iter_child_nodes(parent)}

    scopes: list[ast.AST] = [n for n in ast.walk(tree)
                             if isinstance(n, ast.AsyncWith) and _is_lock_hold(n)]
    seen_defs: set[int] = set()
    i = 0
    while i < len(scopes):
        for n in ast.walk(scopes[i]):
            if isinstance(n, ast.Await) and isinstance(n.value, ast.Call) \
                    and isinstance(n.value.func, ast.Name):
                for d in defs.get(n.value.func.id, []):
                    if isinstance(d, ast.AsyncFunctionDef) and id(d) not in seen_defs:
                        seen_defs.add(id(d))
                        scopes.append(d)
        i += 1

    bad = set()
    for scope in scopes:
        for n in ast.walk(scope):
            if isinstance(n, ast.Call) and ast.unparse(n.func) == "asyncio.to_thread":
                parent = parents.get(n)
                if isinstance(parent, ast.Call) \
                        and ast.unparse(parent.func) == "_settle_through_cancellation":
                    continue
                bad.add(f"tools.py:{n.lineno}: {ast.unparse(n)[:80]}")
    return sorted(bad)


def test_every_thread_hop_under_the_plugin_lock_settles_through_cancellation():
    assert _unsettled_hops_under_the_plugin_lock() == []
