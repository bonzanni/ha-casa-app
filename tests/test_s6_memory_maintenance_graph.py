"""§14 test 9b (INV-MEM-035): over the real ``s6-rc.d`` and ``user-bundles.d``
trees, the container-start memory step runs before every longrun and every
engagement service, and no other oneshot touches the content of a registry
path while an intent exists.

The script scan finds every content-carrying operation (read, copy, move,
link, remove, write) on a registry path in the oneshots and the programs they
run, and each must sit inside the ``maintenance.json`` guard. Creating an
empty directory (``mkdir -p``) and the content-free ``{}`` of a missing
``sessions.json`` are exempt by name: they carry no memory."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from memory import registry

pytestmark = [pytest.mark.unit]

REPO = Path(__file__).resolve().parents[1]
OVERLAY = REPO / "casa" / "rootfs" / "etc" / "s6-overlay"
CODE = REPO / "casa" / "rootfs" / "opt" / "casa"
STEP = "init-memory-maintenance"


def graph(overlay: Path) -> dict[str, set[str]]:
    services = overlay / "s6-rc.d"
    return {d.name: {p.name for p in (d / "dependencies.d").iterdir()}
            if (d / "dependencies.d").is_dir() else set()
            for d in services.iterdir() if d.is_dir()}


def reaches(g: dict[str, set[str]], start: str, target: str) -> bool:
    seen, todo = set(), [start]
    while todo:
        node = todo.pop()
        if node == target:
            return True
        if node not in seen:
            seen.add(node)
            todo.extend(g.get(node, ()))
    return False


def graph_violations(overlay: Path, *, engagement_deps=()) -> list[str]:
    g = graph(overlay)
    out = []
    step = overlay / "s6-rc.d" / STEP
    if not step.is_dir():
        return [f"{STEP} missing"]
    if (step / "type").read_text().strip() != "oneshot":
        out.append(f"{STEP} is not a oneshot")
    if g[STEP] != {"base"}:
        out.append(f"{STEP} depends on {sorted(g[STEP])}")
    if not (overlay / "user-bundles.d/user/contents.d" / STEP).exists():
        out.append(f"{STEP} is not in the user bundle")
    for name in sorted(g):
        if (overlay / "s6-rc.d" / name / "type").read_text().strip() == "longrun":
            if not reaches(g, name, STEP):
                out.append(f"{name} does not reach {STEP}")
    for dep in engagement_deps:
        if not reaches(g, dep, STEP):
            out.append(f"engagement dependency {dep} does not reach {STEP}")
    return out


def _engagement_deps(tmp_path: Path) -> list[str]:
    """What ``s6_rc.write_service_dir`` writes for an engagement, with the
    dependency list the claude_code driver passes."""
    from drivers import s6_rc
    src = (CODE / "drivers" / "claude_code_driver.py").read_text(
        encoding="utf-8")
    found = re.findall(r"depends_on=\[([^\]]*)\]", src)
    assert len(found) == 1, found
    deps = [d.strip().strip("\"'") for d in found[0].split(",") if d.strip()]
    svc = s6_rc.write_service_dir(
        svc_root=str(tmp_path), engagement_id="e" * 32,
        run_script="#!/bin/sh\n", depends_on=deps)
    return sorted(p.name for p in (Path(svc) / "dependencies.d").iterdir())


def test_the_step_runs_before_every_longrun_and_engagement(tmp_path):
    deps = _engagement_deps(tmp_path)
    assert deps == ["init-setup-configs"]
    assert graph_violations(OVERLAY, engagement_deps=deps) == []
    longruns = [n for n in graph(OVERLAY) if (
        OVERLAY / "s6-rc.d" / n / "type").read_text().strip() == "longrun"]
    assert sorted(longruns) == ["svc-casa", "svc-casa-mcp", "svc-nginx",
                                "svc-ttyd"]


def test_dropping_the_setup_configs_edge_is_reported(tmp_path):
    copy = tmp_path / "s6-overlay"
    shutil.copytree(OVERLAY, copy)
    (copy / "s6-rc.d/init-setup-configs/dependencies.d" / STEP).unlink()
    out = graph_violations(copy, engagement_deps=["init-setup-configs"])
    assert len(out) == 5, out          # four longruns and the engagements


# --- the script scan --------------------------------------------------------------

#: Registry paths outside the store, plus the store's directory itself.
def registry_paths() -> list[str]:
    paths = [registry.MEMORY_DIR]
    for entry in registry.REGISTRY:
        paths += [p for p in entry.paths if p.startswith("/")
                  and not p.startswith(registry.MEMORY_DIR)]
    return sorted(set(paths))


_CONTENT_OPS = re.compile(
    r"(^\s*|[;&|(]\s*|\bthen\s+)"
    r"(cp|mv|ln|rm|cat|tee|rsync|tar|install|dd|sed|chown|chmod)\b|>")
_VAR = re.compile(r'^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)="?([^"\s]*)"?\s*$')
_GUARD = re.compile(r'if \[ ! -e "\$DATA_DIR/memory/maintenance\.json" \]; then')


def _expand(line: str, env: dict[str, str]) -> str:
    for k, v in sorted(env.items(), key=lambda kv: -len(kv[0])):
        line = line.replace("${" + k + "}", v).replace("$" + k, v)
    return line


def _touches(line: str, paths: list[str]) -> bool:
    for p in paths:
        pattern = re.escape(p).replace(r"\*", r"[^/\s\"']+")
        if re.search(pattern, line):
            return True
    return False


def script_violations(text: str, paths: list[str]) -> tuple[int, list[str]]:
    """``(guarded content operations, unguarded ones)`` in a shell script."""
    env = {"DATA_DIR": "/data", "CONFIG_DIR": "/config"}
    depth_in_guard, depth, guarded, bad = None, 0, 0, []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0] if not raw.lstrip().startswith("#!") else ""
        if not line.strip():
            continue
        m = _VAR.match(line)
        if m:
            env[m.group(1)] = _expand(m.group(2), env)
            continue
        expanded = _expand(line, env)
        if _GUARD.search(line):
            depth_in_guard = depth
        if re.match(r"\s*if\b", line):
            depth += 1
        if re.match(r"\s*fi\b", line):
            depth -= 1
            if depth_in_guard is not None and depth == depth_in_guard:
                depth_in_guard = None
        if not _touches(expanded, paths) or not _CONTENT_OPS.search(expanded):
            continue
        if re.match(r"\s*mkdir -p\b", line):
            continue
        if re.search(r"echo '\{\}' > \S*/sessions\.json", expanded):
            continue
        if depth_in_guard is not None:
            guarded += 1
        else:
            bad.append(raw.strip())
    return guarded, bad


ONESHOT_SCRIPTS = ("validate-config.sh", "setup-configs.sh",
                   "setup-plugin-store.sh", "setup-nginx.sh")


def test_every_oneshot_is_scanned():
    ups = {(OVERLAY / "s6-rc.d" / d / "up").read_text().strip()
           for d in graph(OVERLAY)
           if (OVERLAY / "s6-rc.d" / d / "up").exists()}
    assert ups == {f"/etc/s6-overlay/scripts/{s}" for s in
                   (*ONESHOT_SCRIPTS, "memory-maintenance.sh")}


def test_no_oneshot_touches_registry_content_outside_the_guard():
    paths = registry_paths()
    counts = {}
    for name in ONESHOT_SCRIPTS:
        guarded, bad = script_violations(
            (OVERLAY / "scripts" / name).read_text(encoding="utf-8"), paths)
        assert bad == [], (name, bad)
        counts[name] = guarded
    # today the only one is the projects migration: its copy, its removal
    # and its symlink
    assert counts == {"validate-config.sh": 0, "setup-configs.sh": 3,
                      "setup-plugin-store.sh": 0, "setup-nginx.sh": 0}


def test_removing_the_guard_is_reported():
    text = (OVERLAY / "scripts" / "setup-configs.sh").read_text(
        encoding="utf-8").replace(
        'if [ ! -e "$DATA_DIR/memory/maintenance.json" ]; then', "if true; then")
    guarded, bad = script_violations(text, registry_paths())
    assert (guarded, len(bad)) == (0, 3)


def test_the_python_programs_the_oneshots_run_name_no_registry_path():
    programs = ["plugin_boot.py", "config_sync.py", "supervisor_discovery.py",
                "scripts/reconcile_system_requirements.py"]
    for name in ONESHOT_SCRIPTS:
        text = (OVERLAY / "scripts" / name).read_text(encoding="utf-8")
        for prog in re.findall(r"/opt/casa/([\w/]+\.py)", text):
            assert prog in programs, (name, prog)
    paths = registry_paths()
    for prog in programs:
        src = (CODE / prog).read_text(encoding="utf-8")
        assert not _touches(src, paths), prog
