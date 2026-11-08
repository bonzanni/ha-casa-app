"""#1353: every model call Casa makes names a full model id and passes the
effort decided for it.

The decision, per call path under the shipped option defaults: the assistant
on Opus 5.5 at medium; the butler, the concierge, the tier classifier, the
observer and query_engager's synthesis on Haiku 5.5 at low; the finance
specialist on Sonnet 5.5 at medium; the configurator on
Sonnet 5 at high; the Casa Browser action check on Opus 5.5 at high, fixed; the memory writer on Opus 5.5 at medium. The CLI's own defaults (model and effort) move with every
CLI release, so a call that leaves either out silently changes behaviour on
the next bump — which is what these tests pin.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

try:
    from tests.test_cross_session_tools import (
        APP_ROOT, _admission_launches, _call_name, _classifier_launches,
        _judgement_launches, _memory_writer_launches, _sites,
        _synthesis_launches,
    )
except ImportError:
    from test_cross_session_tools import (
        APP_ROOT, _admission_launches, _call_name, _classifier_launches,
        _judgement_launches, _memory_writer_launches, _sites,
        _synthesis_launches,
    )

pytestmark = [pytest.mark.unit]

REPO = Path(__file__).resolve().parents[1]
DEFAULTS = APP_ROOT / "defaults"


def test_the_decided_model_and_effort_per_shortname():
    """Literal on purpose: the mapping's own test, not a read-back of it."""
    from config import MODEL_MAP, effort_for, resolve_model

    assert MODEL_MAP == {
        "opus": "claude-opus-5-5",
        "sonnet": "claude-sonnet-5-5",
        "haiku": "claude-haiku-5-5",
    }
    assert effort_for("claude-opus-5-5") == "medium"
    assert effort_for("claude-haiku-5-5") == "low"
    assert effort_for("claude-sonnet-5-5") == "medium"
    assert effort_for("claude-sonnet-5") == "high"
    assert resolve_model("claude-sonnet-5") == "claude-sonnet-5"
    # An id the table does not name gets no effort: the CLI default applies.
    assert effort_for("claude-opus-5") is None
    assert effort_for(None) is None


# The sites whose model and effort are fixed by a ruling, not by
# ``effort_for``: (file, enclosing function) -> (model name, effort name). The
# action check's judge is Opus 5.5 at high whatever MODEL_MAP later says
# (the action-model design's ruling 5).
FIXED_EFFORT = {("action_judgement.py", "_ask_check"): ("JUDGE_MODEL", "JUDGE_EFFORT")}


def test_every_options_site_passes_effort_for_its_own_model():
    """Every ``ClaudeAgentOptions`` construction passes ``model=`` and
    ``effort=effort_for(<that same expression>)`` — a site that drops either,
    or derives the effort from another value, fails here. A site in
    ``FIXED_EFFORT`` passes exactly its two named constants instead, and every
    listed site must exist."""
    problems: list[str] = []
    count = 0
    fixed_seen: set = set()
    for path in sorted(APP_ROOT.rglob("*.py")):
        rel = str(path.relative_to(APP_ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for scope, _fn, call in _sites(tree):
            count += 1
            kws = {k.arg: k.value for k in call.keywords}
            where = f"{rel}::{scope} (line {call.lineno})"
            model, effort = kws.get("model"), kws.get("effort")
            if model is None:
                problems.append(f"{where}: no model")
                continue
            if (rel, scope) in FIXED_EFFORT:
                fixed_seen.add((rel, scope))
                names = FIXED_EFFORT[(rel, scope)]
                if (effort is None or not isinstance(model, ast.Name)
                        or not isinstance(effort, ast.Name)
                        or (model.id, effort.id) != names):
                    problems.append(f"{where}: model/effort are not {names}")
                continue
            if (effort is None or _call_name(effort) != "effort_for"
                    or len(effort.args) != 1
                    or ast.unparse(effort.args[0]) != ast.unparse(model)):
                problems.append(f"{where}: effort is not effort_for(<model>)")
    assert count == 11, count
    assert fixed_seen == set(FIXED_EFFORT), fixed_seen
    assert not problems, problems


async def test_tier_classifier_runs_haiku_at_low(monkeypatch):
    launches = await _classifier_launches(monkeypatch)
    assert len(launches) == 2
    assert [(o.model, o.effort) for o in launches] == [
        ("claude-haiku-5-5", "low")] * 2


async def test_memory_writer_runs_opus_at_medium(monkeypatch):
    launches = await _memory_writer_launches(monkeypatch)
    assert [(o.model, o.effort) for o in launches] == [
        ("claude-opus-5-5", "medium")]


async def test_run_admission_runs_haiku_at_low(monkeypatch):
    launches = await _admission_launches(monkeypatch)
    assert [(o.model, o.effort) for o in launches] == [("claude-haiku-5-5", "low")]


async def test_action_judgement_runs_opus_at_high(monkeypatch):
    launches = await _judgement_launches(monkeypatch)
    assert [(o.model, o.effort) for o in launches] == [("claude-opus-5-5", "high")]


async def test_query_engager_synthesis_runs_haiku_at_low(monkeypatch):
    monkeypatch.setenv("SECONDARY_AGENT_MODEL", "opus")  # no longer read
    launches = await _synthesis_launches(monkeypatch)
    assert [(o.model, o.effort) for o in launches] == [("claude-haiku-5-5", "low")]


def test_observer_is_wired_to_the_full_haiku_id():
    """casa_core builds the one Observer with ``resolve_model("haiku")`` —
    never a bare alias the CLI resolves by its own release."""
    tree = ast.parse((APP_ROOT / "casa_core.py").read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and _call_name(n) == "Observer"]
    assert len(calls) == 1
    kw = {k.arg: k.value for k in calls[0].keywords}
    assert ast.unparse(kw["model_name"]) == "resolve_model('haiku')"


@pytest.mark.parametrize("executor", ["configurator"])
def test_shipped_executors_stay_on_sonnet_5(executor):
    from role_artifact import load_role_artifact
    from role_slot import _ha_model_options, materialize_role

    role = materialize_role(
        source=load_role_artifact(DEFAULTS / "roles" / "executor" / executor),
        options=_ha_model_options({}))
    assert role.resolved_model.sdk_model == "claude-sonnet-5"
    text = (DEFAULTS / "agents" / "executors" / executor
            / "definition.yaml").read_text(encoding="utf-8")
    assert re.search(r"(?m)^model: claude-sonnet-5$", text)


@pytest.mark.parametrize("resident,option,expected", [
    ("assistant", "primary_agent_model", "claude-opus-5-5"),
    ("butler", "voice_agent_model", "claude-haiku-5-5"),
    ("concierge", "voice_agent_model", "claude-haiku-5-5"),
])
def test_residents_resolve_their_default_option(resident, option, expected):
    from role_artifact import load_role_artifact
    from role_slot import _ha_model_options, materialize_role

    role = materialize_role(
        source=load_role_artifact(DEFAULTS / "roles" / "resident" / resident),
        options=_ha_model_options({}))
    assert role.resolved_model.option == option
    assert role.resolved_model.sdk_model == expected


@pytest.mark.parametrize("recorded,expected", [
    ("claude-opus-5", "claude-opus-5-5"),
    ("claude-sonnet-5", "claude-sonnet-5-5"),
    ("claude-haiku-4-5", "claude-haiku-5-5"),
    ("claude-opus-5-5", "claude-opus-5-5"),
])
def test_resident_hosted_job_recorded_before_the_move_resumes_on_the_successor(
        recorded, expected):
    from config import current_resident_model

    assert current_resident_model(recorded) == expected
