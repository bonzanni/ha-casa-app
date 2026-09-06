"""The shipped assistant trigger document accepts the recipe-shaped webhook (#654).

Red case for cluster F, clause A. It drives the REAL shipped default, the REAL
writer (``reminders.upsert_entry``) and the REAL loader
(``agent_loader.validate_persisted`` + ``_build_triggers``) — a fixture copy of
the YAML body would pin nothing about the bytes that ship in the image.

The copy is an explicit walk rather than ``shutil.copytree``: the candidate gate
executes on a read-only materialization of the tree, and ``copytree`` calls
``copystat`` on every DIRECTORY it creates regardless of ``copy_function``, so
the destination inherits mode ``0555`` and the writer's sibling temp file
cannot be created inside it. Measured on that tree: ``PermissionError`` out of
``atomic_write_text``'s ``tempfile.mkstemp``, green in every writable worktree.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

import agent_loader
import reminders

_SHIPPED_ASSISTANT = (
    Path(__file__).resolve().parents[1]
    / "casa" / "rootfs" / "opt" / "casa" / "defaults" / "agents" / "assistant"
)


def _copy_shipped_agent(src: Path, dst: Path) -> None:
    """Copy the shipped agent directory as BYTES, with fresh directory modes."""
    dst.mkdir(parents=True, exist_ok=True)
    for path in sorted(src.rglob("*")):
        target = dst / path.relative_to(src)
        if path.is_dir():
            target.mkdir(exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)


def test_shipped_assistant_accepts_configurator_webhook_recipe(tmp_path, caplog):
    agent_dir = tmp_path / "assistant"
    _copy_shipped_agent(_SHIPPED_ASSISTANT, agent_dir)
    triggers_path = agent_dir / "triggers.yaml"
    assert triggers_path.read_bytes() == (
        _SHIPPED_ASSISTANT / "triggers.yaml").read_bytes()

    # Exactly what recipes/trigger/add.md tells the configurator to write: no
    # `path` field, and no prompt/prompt_file (INV-TRIG-013 refuses those on a
    # webhook at the writer).
    entry = {
        "name": "parcel-delivered",
        "type": "webhook",
        "clearance": "public",
        "auth": {"mode": "static_header", "header": "X-API-Key",
                 "tolerance_secs": 300},
    }
    assert "path" not in entry
    assert "prompt" not in entry and "prompt_file" not in entry

    assert reminders.upsert_entry(str(triggers_path), entry) == "added"

    document = agent_loader._read_yaml(str(triggers_path))
    assert document["schema_version"] == 2
    saved = [e for e in document["triggers"] if e["name"] == "parcel-delivered"]
    assert len(saved) == 1
    assert saved[0] == entry          # the writer synthesized no `path`

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        agent_loader.validate_persisted(document, "triggers", str(triggers_path))
        specs = agent_loader._build_triggers(document, agent_dir=str(agent_dir))

    assert [s.name for s in specs].count("parcel-delivered") == 1
    deprecations = [r for r in caplog.records
                    if "'path' is deprecated" in r.getMessage()]
    assert len(deprecations) == 0


def test_a_v1_document_with_a_pathed_webhook_still_loads_and_still_warns(caplog):
    """The union schema keeps accepting v1 — carrying an existing document
    forward is #402's, and this change does not do it."""
    document = {
        "schema_version": 1,
        "triggers": [{"name": "legacy", "type": "webhook",
                      "path": "/hooks/legacy"}],
    }
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        agent_loader.validate_persisted(document, "triggers", "<v1 doc>")
        specs = agent_loader._build_triggers(document, agent_dir="/nonexistent")

    assert [s.name for s in specs] == ["legacy"]
    deprecations = [r for r in caplog.records
                    if "'path' is deprecated" in r.getMessage()]
    assert len(deprecations) == 1
