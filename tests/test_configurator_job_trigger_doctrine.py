"""#1437: the configurator's doctrine tells it how a job trigger is spelled and
where it runs, and never to make one run by assigning the plugin to the resident.

In live use the configurator wrote a working delegate-hosted job trigger, then reported
that it "will not actually run" (it took the plugin's registry key for the job's
prefix, and read the resident's missing assignment as a blocker) and proposed
assigning the plugin, protected tools and all, to the resident. These pin the
text that model reads; the upsert's own ok result is pinned in
``test_file_handoff_job_schema.py``. A pin on TEXT, claiming nothing about runtime.
"""
from __future__ import annotations

import re
from pathlib import Path

DOCTRINE = (Path(__file__).resolve().parents[1]
            / "casa/rootfs/opt/casa/defaults/agents/executors/configurator/doctrine")


def _flat(path: Path) -> str:
    return re.sub(r"\s+", " ", path.read_text(encoding="utf-8"))


def test_the_add_recipe_spells_a_job_by_the_plugins_manifest_name():
    text = _flat(DOCTRINE / "recipes/trigger/add.md")
    assert "`<plugin manifest name>:<job name>`" in text
    assert "(`bank-feed`, not `finance.bank-feed`)" in text


def test_the_add_recipe_says_an_ok_upsert_means_the_trigger_works_and_names_its_host():
    text = _flat(DOCTRINE / "recipes/trigger/add.md")
    assert "The upsert checks the job, so its `ok` means the job is startable now." in text
    assert "Each fire resolves the host again and starts the job if it still can." in text
    assert "When `job_host` is a specialist, the job runs on that delegate: the resident needs no assignment of the plugin" in text


def test_the_add_recipe_never_proposes_assigning_the_plugin_to_the_resident():
    text = _flat(DOCTRINE / "recipes/trigger/add.md")
    assert "Never propose assigning the plugin to the resident to make a job trigger run." in text


def test_safety_never_widens_a_resident_as_the_way_to_make_something_else_work():
    text = _flat(DOCTRINE / "safety.md")
    assert "## Widening a resident's tools" in text
    assert ("Propose it only when the operator asked for that resident to have that plugin, "
            "never as the way to make something else work") in text


def test_the_assistant_never_offers_itself_a_plugin_the_person_did_not_ask_for():
    text = _flat(Path(__file__).resolve().parents[1]
                 / "casa/rootfs/opt/casa/defaults/roles/resident/assistant/doctrine.md")
    assert ("Never offer or propose to have a plugin assigned to you unless the person "
            "asked for that plugin's tools") in text
