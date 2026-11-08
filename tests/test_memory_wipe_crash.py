"""§14 test 9d: the container-start step killed (a real SIGKILL of the
process the test spawned) after each deletion and after
the fresh store exists but before the intent goes. The next run completes:
0 canary bytes, every kept item intact, and the done line claimed once."""
from __future__ import annotations

import json

import pytest

import memory_wipe_support as w
from memory import capture, maintenance, registry
from memory.store import MemoryStore

pytestmark = [pytest.mark.unit]


def _tree(root) -> w.Tree:
    tree = w.Tree(root)
    u = lambda p: w.under(root, p)            # noqa: E731
    store = MemoryStore(str(u(registry.MEMORY_DIR)))
    assert store.open()
    capture.append(store, capture.event(
        role="assistant", channel="telegram", speaker_kind="operator",
        origin_kind="operator", text="told " + tree.canary(),
        eligibility="eligible", origin_tier="private"))
    w.write(u(f"{registry.MEMORY_DIR}/spool.jsonl"), tree.canary())
    w.write(u(f"{registry.MEMORY_DIR}/exports/m.tar"), tree.canary())
    w.write(u(f"/config/cc-home/.claude/projects/-a/{w.SID}.jsonl"),
            tree.canary())
    w.write(u(f"/config/cc-home/.claude/projects/-b/{w.SID}.jsonl"),
            tree.canary())
    w.write(u("/data/sessions.json"), json.dumps({"k": tree.canary()}))
    w.write(u("/data/explanations/abcd1234.json"), tree.canary())
    tree.keep(u("/data/reminders.json"), '[{"id": "r1"}]')
    tree.keep(u("/data/agent-inbox/letter.txt"), "kept")
    return tree


def _crash_then_complete(tmp_path, kill_at: str, k: int) -> bool:
    """One crash at the *k*-th call of *kill_at*; returns whether the step
    was actually killed (False once *k* exceeds the calls a run makes)."""
    root, proc = tmp_path / f"{kill_at}-{k}", w.fake_proc(tmp_path)
    tree = _tree(root)
    assert maintenance.write_wipe_intent(str(root)) == "written"
    first = w.run_oneshot(root, proc, kill_at=kill_at, kill_after=k)
    if not w.killed(first):
        assert first.stdout.strip() == "done", first.stderr
        return False
    intent = maintenance.read_intent(str(root))
    if intent is not None:
        assert len(intent["attempts"]) == 1
        assert intent["attempts"][0]["error"] is None
        second = w.run_oneshot(root, proc)
        assert second.stdout.strip() == "done", (kill_at, k, second.stderr)
    # else: killed after the intent went, the last step; nothing is left
    assert w.hits(root, tree.canaries) == [], (kill_at, k)
    assert w.kept_changes(tree) == [], (kill_at, k)
    assert maintenance.read_intent(str(root)) is None
    store = MemoryStore(str(w.under(root, registry.MEMORY_DIR)))
    assert store.open()
    assert [maintenance.claim_outcome_notice(store) for _ in range(2)] == [
        maintenance.FORGOTTEN_LINE, None]
    return True


@pytest.mark.parametrize("kill_at", ["_remove", "_fresh_store"])
def test_every_crash_point_reruns_to_the_same_result(tmp_path, kill_at):
    kills = 0
    for k in range(1, 80):
        if not _crash_then_complete(tmp_path, kill_at, k):
            break
        kills += 1
    else:
        pytest.fail("the step never completed without a kill")
    minimum = {"_remove": 4, "_fresh_store": 1}[kill_at]
    assert kills >= minimum, kills
