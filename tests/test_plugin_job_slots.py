"""Durable and in-process plugin-job slot identity."""
from types import SimpleNamespace

import background_jobs as jobs


class Registry:
    def __init__(self, records):
        self.records = records

    def active_and_idle(self):
        return self.records


def test_live_job_identity_uses_new_metadata_and_phase_one_artifact():
    new = SimpleNamespace(origin={"job": {"name": "ledger:scan"},
                                  "plugin_job": {"registry_name": "installed-ledger"}})
    old = SimpleNamespace(origin={"job": {"name": "ledger:classify"}},
                          plugin_artifacts=({"name": "installed-ledger",
                                             "manifest_name": "ledger"},))
    assert jobs.running_job_for_plugin(Registry([new]), "installed-ledger") is new
    assert jobs.running_job_for_plugin(Registry([old]), "installed-ledger") is old


def test_pending_plugin_start_claim_is_exclusive_and_released():
    plugin = "test-pending-plugin"
    jobs.release_plugin_job_start(plugin)
    assert jobs.claim_plugin_job_start(plugin, "ledger:scan", "Scan")
    assert not jobs.claim_plugin_job_start(plugin, "ledger:other", "Other")
    assert jobs.pending_plugin_job_start(plugin) == ("ledger:scan", "Scan")
    jobs.release_plugin_job_start(plugin)
    assert jobs.pending_plugin_job_start(plugin) is None


def test_a_record_naming_another_installed_plugin_does_not_block_this_one():
    """Diff review r1: a specialist's bundled `finance.ledger` and a resident's
    own `ledger` are two installed plugins of the same manifest. The qualified
    job name stands in for identity ONLY when the record names no installed
    plugin at all — otherwise one plugin's live job refused the other's start,
    naming the wrong topic."""
    bundled = SimpleNamespace(
        origin={"job": {"name": "ledger:classify", "title": "Classify"}},
        plugin_artifacts=({"name": "finance.ledger", "manifest_name": "ledger"},),
        id="eng-1", topic_id=42)
    assert jobs.running_job_for_plugin(Registry([bundled]), "ledger") is None
    assert jobs.running_job_for_plugin(Registry([bundled]), "finance.ledger") is bundled


def test_a_record_with_no_recorded_identity_still_blocks_its_manifest():
    """The phase-1 fallback it replaces: a record pinning no artifact at all is
    identified by its qualified job name, so a second start is still refused."""
    legacy = SimpleNamespace(
        origin={"job": {"name": "ledger:classify", "title": "Classify"}},
        plugin_artifacts=(), id="eng-2", topic_id=7)
    assert jobs.running_job_for_plugin(Registry([legacy]), "ledger") is legacy
