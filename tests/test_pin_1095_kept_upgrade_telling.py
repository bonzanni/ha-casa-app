"""#1095 — an upgrade that keeps the new version but does not activate it says so.

Ruling (ruling-1095-5): when an upgrade ends with the new version kept but not
yet active, Casa's result says so plainly: the upgrade is not active yet; new
and open conversations still use the previous version; re-running the upgrade
finishes it. Ruling-1095-6: in the variant whose cleanup of the previous version
also failed, the result says to restart Casa and then re-run the upgrade.

That is the LIBRARY-kept arm of `specialist_upgrade` (it returns before the
sequencer, so Casa has loaded nothing). The text comes from two places — the
library's `detail` (`specialist_install._kept_new_version_error`) and the tool's
`outcome` — and both must say it, with no "active and stays active" left beside
it. The REAL error constructor and the REAL handler run; only the library commit
(raising that error) and the sequencer (counted) are doubled.

RED at the base: both fields say "the new version is active and stays active".
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

_DOCTRINE = (Path(__file__).resolve().parent.parent
             / "casa/rootfs/opt/casa/defaults/agents/executors/configurator/doctrine")
NOT_ACTIVE = "not active yet"
PREVIOUS = "new and open conversations still use the previous version"
RERUN = "re-running the same upgrade finishes it"
RESTART = "restart casa, then re-run the upgrade"
OLD = "active and stays active"
# #1296: the ruled text is said when the live agent runs the version replaced.
REPLACED = "component:fin@1#sha256:" + "a" * 64


@pytest.fixture
def kept(monkeypatch):
    import agent as agent_mod
    import specialist_bundle_journal
    import specialist_install
    import specialist_receipt
    import tools as tools_mod

    class _Checked:
        ok = True

        class receipt:  # noqa: N801
            receipt_id = "r1"
            receipt_digest = "d"
            plugins = ()

        class component:  # noqa: N801
            component_id = "c"
            version = "2"
            slug = "fin"
            checksum = "x"

            class role:  # noqa: N801
                role = {}
            default_persona_ref = None
            default_persona_checksum = None
        dependencies = ()
        root_digest = "rd"

    state = SimpleNamespace(seq=0, finish_fails=False, journal_completions=0)
    monkeypatch.setattr(specialist_install, "validate_resume_inputs", lambda **k: _Checked)
    monkeypatch.setattr(specialist_receipt, "load", lambda rid, **k: object())
    monkeypatch.setattr(tools_mod, "_PLUGIN_TOOLS_LOCK", asyncio.Lock())

    def complete(path):
        state.journal_completions += 1
    monkeypatch.setattr(specialist_bundle_journal, "complete", complete)

    def finish_forward():
        if state.finish_fails:
            raise OSError("disk full")

    def lib(**kw):
        txn = SimpleNamespace(finish_forward=finish_forward)
        err = specialist_install._kept_new_version_error(
            txn, Path("/nonexistent/journal"), "fin", RuntimeError("activation failed"),
            replaced_root=REPLACED)
        err.dropped_owned_names = ()
        raise err
    monkeypatch.setattr(specialist_install, "upgrade_specialist", lib)
    live = SimpleNamespace(config=SimpleNamespace(
        binding=SimpleNamespace(component_root=REPLACED)))
    monkeypatch.setattr(agent_mod, "active_runtime",
                        SimpleNamespace(agents={"fin": live}), raising=False)

    async def seq(*a, **k):
        state.seq += 1
        return {"ok": True}
    monkeypatch.setattr(tools_mod, "_bundle_reload_and_verify", seq)
    return state


async def _upgrade() -> dict:
    import tools as tools_mod
    r = await tools_mod.specialist_upgrade.handler({
        "slug": "fin", "component_id": "c", "version": "2", "root_digest": "rd",
        "staged_dir": "/nonexistent", "receipt_id": "r1"})
    return json.loads(r["content"][0]["text"])


def _fields(out: dict) -> dict:
    return {"outcome": str(out.get("outcome", "")).lower(),
            "detail": str(out.get("detail", "")).lower()}


@pytest.mark.asyncio
async def test_rc18_v1a_kept_says_not_active_and_that_a_re_run_finishes_it(kept):
    out = await _upgrade()
    assert out.get("kind") == "upgrade_kept_new_version", out
    assert out.get("kept_new_version") is True
    assert kept.seq == 0 and kept.journal_completions == 1
    f = _fields(out)
    for name, text in f.items():
        assert OLD not in text, (name, text)
        assert NOT_ACTIVE in text, (name, text)
        assert PREVIOUS in text, (name, text)
        assert "until you re-run" not in text, (name, text)
    assert RERUN in f["outcome"], f["outcome"]
    assert RESTART not in f["outcome"], f["outcome"]


@pytest.mark.asyncio
async def test_rc19_v1b_kept_with_failed_cleanup_says_restart_then_re_run(kept):
    kept.finish_fails = True
    out = await _upgrade()
    assert out.get("kind") == "upgrade_kept_new_version", out
    assert kept.seq == 0 and kept.journal_completions == 0
    for name, text in _fields(out).items():
        assert OLD not in text, (name, text)
        assert NOT_ACTIVE in text, (name, text)
        assert PREVIOUS in text, (name, text)
        assert RESTART in text, (name, text)
        assert "until you re-run" not in text, (name, text)


def test_rc20_no_surface_promises_immediate_activation_and_the_recipe_has_the_kept_step():
    import tools as tools_mod
    apply_md = (_DOCTRINE / "recipes/persona/apply.md").read_text(encoding="utf-8")
    assert apply_md.count("activates it immediately") == 0
    assert tools_mod.persona_apply.description.count("activated by the next casa_reload") == 0
    upgrade_md = (_DOCTRINE / "recipes/specialist/upgrade.md").read_text(encoding="utf-8")
    steps = [p for p in upgrade_md.split("\n\n")
             if 'kind: "upgrade_kept_new_version"' in p]
    assert len(steps) == 1, "no single upgrade.md step keyed on the library-kept kind"
    step = steps[0].lower()
    assert NOT_ACTIVE in step and RESTART in step
    # #1296: the in-use sentence holds only when Casa found the replaced version
    # running, so the kind's own instruction relays the result's `outcome` as
    # written, and quotes the ruled sentence only inside that condition.
    own = step.split('kind: "upgrade_kept_new_version"', 1)[1].split("key this on the `kind`", 1)[0]
    assert "`outcome`" in own and "as written" in own, own
    # Over the WHOLE recipe, whitespace-normalised, so a quotation in another
    # paragraph or wrapped across lines is counted too (Astra, red-case specify).
    text = " ".join(upgrade_md.lower().split())
    sentences = re.split(r"(?<=[.!?])\s+", text)
    total = text.count(PREVIOUS)
    conditioned = sum(s.count(PREVIOUS) for s in sentences
                      if "when casa found the replaced version running" in s)
    assert total >= 1 and conditioned == total, (total, conditioned)
