"""#555 — `plugin_status`: the assistant's read-only view of plugin state.

Before this tool the resident could not answer "what went wrong with the
fx-setup install?" at all: no tool in CASA_TOOLS read the health report or the
setup-episode store for any role, the assistant holds no plugin grants, and
`Read` is inert for residents (an empty path_scope denies everything). The only
path was `engage_executor("configurator")` — a spawned engagement thread to
answer a question — and even that could not answer a HISTORICAL failure, which
lives only in the episode row's `last_error`.

The tool returns operator-facing SENTENCES, not rows: #550 is precisely what
happens when a resident is handed raw state to paraphrase.
"""
from __future__ import annotations

import json

import pytest

import plugin_health
import plugin_setup_episodes
import tools as tools_mod

pytestmark = pytest.mark.unit      # asyncio_mode = auto (pytest.ini)


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def _issue(name="fx-setup", target="resident:assistant",
           code="env_unresolved", detail="FX_API_KEY", fp=None):
    return {"name": name, "target": target, "stage": "verify",
            "reason_code": code, "artifact_id": "a", "detail": detail,
            "fingerprint": fp or f"fp-{name}"}


def _row(plugin="fx-setup", status="failed", attempts=3, retries=1,
         last_error="the service refused the key", updated=1_700_000_000.0):
    return {"id": "e1", "plugin": plugin, "artifact_id": "a", "gen": 1,
            "status": status, "gate": "released", "attempts": attempts,
            "execution_retries": retries, "resolve_deferrals": 0,
            "approved_identities": [], "created_ts": updated - 60,
            "updated_ts": updated, "last_error": last_error}


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point BOTH stores at tmp_path and hand back setters for each."""
    health = tmp_path / "plugin-health.json"
    monkeypatch.setattr(tools_mod, "_PLUGIN_HEALTH_PATH", str(health))

    def set_health(issues=(), warnings=()):
        health.write_text(json.dumps({
            "schema_version": 1, "issues": list(issues),
            "warnings": list(warnings), "notified_fingerprints": []}),
            encoding="utf-8")

    def set_episodes(rows):
        monkeypatch.setattr(plugin_setup_episodes, "episodes",
                            lambda status=None: list(rows))

    set_health()
    set_episodes([])
    return set_health, set_episodes


async def _call():
    return _payload(await tools_mod.plugin_status.handler({}))


async def test_reports_standing_issues_as_operator_sentences(store):
    """The same translation the DM and the in-band notice use — never a reason
    code, which the operator cannot act on or look up."""
    set_health, _ = store
    set_health(issues=[_issue()])
    out = await _call()
    assert out["ok"] is True
    assert out["standing"] == [
        "fx-setup is missing a setting it needs — FX_API_KEY"]
    assert "env_unresolved" not in json.dumps(out)


async def test_reports_warnings_after_issues(store):
    set_health, _ = store
    set_health(issues=[_issue()],
               warnings=[_issue(name="other", code="legacy_provenance",
                                detail=None, fp="fp-w")])
    out = await _call()
    assert len(out["standing"]) == 2
    assert out["standing"][0].startswith("fx-setup")
    assert "usual provenance checks" in out["standing"][1]


async def test_history_carries_the_last_error_and_the_counts(store):
    """The episode row's `last_error` is the one fact that answers a HISTORICAL
    failure — the health report alone only describes standing state."""
    _, set_episodes = store
    set_episodes([_row()])
    out = await _call()
    assert len(out["history"]) == 1
    line = out["history"][0]
    assert "fx-setup" in line
    assert "the service refused the key" in line
    assert "3" in line and "1" in line          # attempts, execution retries


async def test_history_is_newest_first_and_bounded(store):
    """The store has no TTL and no size cap, so an unbounded return would grow
    the resident's context without limit."""
    _, set_episodes = store
    set_episodes([_row(plugin=f"p{i}", updated=1_700_000_000.0 + i)
                  for i in range(30)])
    out = await _call()
    assert len(out["history"]) == 20
    assert out["history"][0].startswith("p29")
    assert out["history"][-1].startswith("p10")


async def test_malformed_rows_and_timestamps_do_not_raise(store):
    """A non-numeric `updated_ts` must sort, not raise; a non-dict row is
    skipped. Degrade, never raise — the module's own boot tolerance."""
    _, set_episodes = store
    set_episodes([_row(plugin="ok", updated=5.0),
                  dict(_row(plugin="bad-ts"), updated_ts="nonsense"),
                  ["not-a-row"], None])
    out = await _call()
    assert out["ok"] is True
    assert [line.split(":")[0] for line in out["history"]] == ["ok", "bad-ts"]


async def test_corrupt_health_report_degrades_to_empty(store, tmp_path):
    """A report of the wrong shape reads as absent (no standing claim), and the
    episode history still answers."""
    _, set_episodes = store
    (tmp_path / "plugin-health.json").write_text("[1,2]", encoding="utf-8")
    set_episodes([_row()])
    out = await _call()
    assert out["standing"] == []
    assert len(out["history"]) == 1


async def test_reads_nothing_when_both_stores_are_empty(store):
    out = await _call()
    assert out == {"ok": True, "standing": [], "history": []}


async def test_episode_store_failure_degrades_rather_than_raising(
        store, monkeypatch):
    def _boom(status=None):
        raise RuntimeError("store unreadable")
    monkeypatch.setattr(plugin_setup_episodes, "episodes", _boom)
    set_health, _ = store
    set_health(issues=[_issue()])
    out = await _call()
    assert out["ok"] is True
    assert out["history"] == []
    assert len(out["standing"]) == 1      # the half that still works answers


# ---------------------------------------------------------------------------
# Grant boundary: read-only, assistant-granted, never a specialist's.
# ---------------------------------------------------------------------------

def test_registered_in_casa_tools():
    assert tools_mod.plugin_status in tools_mod.CASA_TOOLS


def test_not_grantable_to_a_specialist_engagement():
    """#541's dispatch ceiling: plugin state is operator-facing, and a
    third-party specialist bundle must not be able to read it."""
    from specialist_component import SPECIALIST_CASA_TOOL_ALLOWLIST
    assert "plugin_status" not in SPECIALIST_CASA_TOOL_ALLOWLIST


def _shipped_grants(rel: str) -> list:
    import yaml
    from pathlib import Path
    doc = yaml.safe_load(
        (Path("casa/rootfs/opt/casa/defaults") / rel).read_text(
            encoding="utf-8"))
    return doc["tools"]["allowed"]


def test_granted_in_the_operational_runtime_artifact():
    """`agents/assistant/runtime.yaml` is the ONLY artifact that authorizes a
    resident's tools: `agent_loader._build_runtime_fields` builds
    `cfg.tools.allowed` exclusively from it, and config_sync copies
    defaults/agents/** to /config/agents/** where the running assistant reads
    it. A first draft of this batch granted the tool only in the canonical role
    artifact and the suite stayed green while the tool was unreachable."""
    assert ("mcp__casa-framework__plugin_status"
            in _shipped_grants("agents/assistant/runtime.yaml"))


def test_the_role_artifact_is_not_a_resident_tool_ceiling():
    """The counterpart pin, so nobody "fixes" the asymmetry above by adding an
    inert grant. `roles/resident/<role>/role.yaml` is compared for kind and
    model only; unlike an EXECUTOR's role artifact it is not intersected with
    the operational allowlist, so a grant there authorizes nothing and only
    moves the role checksum."""
    assert ("mcp__casa-framework__plugin_status"
            not in _shipped_grants("roles/resident/assistant/role.yaml"))


def test_selection_through_the_shipped_grants_exposes_it_and_no_mutation_tool():
    """Assert the OUTCOME of the real filter, not the presence of a string: a
    grant is only real if select_casa_tools() actually returns the tool."""
    allowed = frozenset(_shipped_grants("agents/assistant/runtime.yaml"))
    selected = {t.name for t in tools_mod.select_casa_tools(allowed)}
    assert "plugin_status" in selected
    # The read half only — every plugin MUTATION tool stays configurator-only.
    assert not {n for n in selected
                if n.startswith("plugin_") and n != "plugin_status"}


def test_takes_no_arguments_and_declares_none():
    assert tools_mod.plugin_status.input_schema.get("properties") == {}
    assert not tools_mod.plugin_status.input_schema.get("required")


# --- #677: an unreadable report is damage, never presented as health --------

async def test_a_healthy_empty_report_stays_exactly_three_keys(store):
    """The disclosure keys are CONDITIONAL. A box with a readable report and
    nothing wrong must keep answering in the shape it always did — otherwise
    every healthy answer starts carrying a caveat, which is how a real one stops
    being read."""
    out = await _call()
    assert out == {"ok": True, "standing": [], "history": []}


async def test_an_absent_report_is_ordinary_absence(store, tmp_path,
                                                    monkeypatch):
    """No report yet is the normal state of a box that has not regenerated
    health. It is the one None case that is not damage, so it discloses
    nothing."""
    monkeypatch.setattr(tools_mod, "_PLUGIN_HEALTH_PATH",
                        str(tmp_path / "absent.json"))
    out = await _call()
    assert out == {"ok": True, "standing": [], "history": []}


@pytest.mark.parametrize("body", ["{not json", '"a string"', "[1, 2]"])
async def test_a_present_unreadable_report_is_disclosed(store, tmp_path, body):
    """#677: `load_report` returns None for unreadable, unparseable, and valid
    JSON that is not an object alike, and `or {}` made all three indistinguishable
    from a healthy empty report — so the agent asserted absence of problems on
    the strength of a file it could not read."""
    (tmp_path / "plugin-health.json").write_text(body, encoding="utf-8")
    out = await _call()
    assert out["standing"] == []
    assert "standing_unavailable" in out
    assert "could not be read" in out["standing_unavailable"]


async def test_a_failing_existence_probe_is_disclosed_as_damage(store,
                                                                monkeypatch):
    """"Cannot tell" must not read as "healthy": when the probe that would
    distinguish absent from unreadable fails itself, the answer discloses."""
    import pathlib
    real_exists = pathlib.Path.exists

    def boom(self):
        if str(self).endswith("plugin-health.json"):
            raise OSError("probe failed")
        return real_exists(self)
    monkeypatch.setattr(tools_mod, "_PLUGIN_HEALTH_PATH", "/nope/plugin-health.json")
    monkeypatch.setattr(pathlib.Path, "exists", boom)
    out = await _call()
    assert "standing_unavailable" in out


async def test_a_raising_health_read_is_disclosed(store, monkeypatch):
    monkeypatch.setattr(plugin_health, "load_report",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    out = await _call()
    assert out["standing"] == []
    assert "standing_unavailable" in out


async def test_a_raising_history_read_is_disclosed(store, monkeypatch):
    """The two halves still degrade independently: a broken history must not
    cost the standing set, and it must say so rather than showing an empty
    record."""
    set_health, _ = store
    set_health(issues=[_issue()])
    monkeypatch.setattr(plugin_setup_episodes, "episodes",
                        lambda status=None: (_ for _ in ()).throw(OSError("x")))
    out = await _call()
    assert len(out["standing"]) == 1
    assert "history_unavailable" in out
    assert "standing_unavailable" not in out


async def test_the_tool_never_raises_when_both_halves_are_broken(store,
                                                                 monkeypatch):
    monkeypatch.setattr(plugin_health, "load_report",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(plugin_setup_episodes, "episodes",
                        lambda status=None: (_ for _ in ()).throw(OSError("x")))
    out = await _call()
    assert out["ok"] is True
    assert len([k for k in out if k.endswith("_unavailable")]) == 2


@pytest.mark.parametrize("which", ["plugin", "callback"])
async def test_unavailable_routing_is_disclosed_over_a_readable_report(
        store, monkeypatch, which):
    """The joint with #606. A readable report can still be stale in a specific,
    invisible way: while a routing overlay is the unavailable sentinel, no
    reconcile has published an authoritative set, so the trigger, callback and
    pending-approval rows in it describe a routing state nobody has confirmed.
    The report reads clean, so nothing else discloses this.
    """
    import agent as agent_mod
    from types import SimpleNamespace

    registry = SimpleNamespace(
        plugin_overlay_unavailable=lambda: which == "plugin",
        callback_overlay_unavailable=lambda: which == "callback")
    monkeypatch.setattr(agent_mod, "active_runtime",
                        SimpleNamespace(trigger_registry=registry),
                        raising=False)
    out = await _call()
    assert out["standing"] == []
    assert "routing_unavailable" in out
    assert "standing_unavailable" not in out


async def test_a_registry_whose_probe_raises_never_breaks_the_tool(
        store, monkeypatch):
    import agent as agent_mod
    from types import SimpleNamespace

    def boom():
        raise RuntimeError("registry probe exploded")
    monkeypatch.setattr(
        agent_mod, "active_runtime",
        SimpleNamespace(trigger_registry=SimpleNamespace(
            plugin_overlay_unavailable=boom,
            callback_overlay_unavailable=boom)),
        raising=False)
    out = await _call()
    assert out == {"ok": True, "standing": [], "history": []}
