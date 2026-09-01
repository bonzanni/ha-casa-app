"""Tests for TriggerRegistry.reregister_for (Plan 3)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class TestReregisterFor:
    def _make_registry(self):
        from trigger_registry import TriggerRegistry
        scheduler = MagicMock()
        scheduler.get_jobs = MagicMock(return_value=[])
        app = MagicMock()
        app.router = MagicMock()
        app.router.add_post = MagicMock()
        bus = MagicMock()
        return TriggerRegistry(scheduler=scheduler, app=app, bus=bus), scheduler, app

    def test_reregister_clears_existing_jobs(self):
        from config import TriggerSpec
        reg, scheduler, _app = self._make_registry()
        t1 = TriggerSpec(
            name="t1", type="interval", minutes=5,
            channel="telegram", prompt="p",
        )
        reg.register_agent("assistant", [t1], channels=["telegram"])
        assert "assistant:t1" in reg._seen_job_ids

        t2 = TriggerSpec(
            name="t2", type="cron", schedule="0 9 * * *",
            channel="telegram", prompt="q",
        )
        reg.reregister_for("assistant", [t2], channels=["telegram"])
        assert "assistant:t1" not in reg._seen_job_ids
        assert "assistant:t2" in reg._seen_job_ids
        scheduler.remove_job.assert_any_call("assistant:t1")

    def test_reregister_fails_closed_on_conflict(self):
        from config import TriggerSpec
        from trigger_registry import TriggerError
        reg, _sched, _app = self._make_registry()
        t1 = TriggerSpec(
            name="t", type="interval", minutes=5,
            channel="telegram", prompt="p",
        )
        reg.register_agent("assistant", [t1], channels=["telegram"])

        bad = TriggerSpec(
            name="bad", type="cron", schedule="not-a-cron",
            channel="telegram", prompt="q",
        )
        with pytest.raises(TriggerError):
            reg.reregister_for("assistant", [bad], channels=["telegram"])
        assert "assistant:t" not in reg._seen_job_ids
        assert "assistant:bad" not in reg._seen_job_ids

    def test_reregister_partial_failure_unwinds_new_triggers(self):
        """#307: a failure on a LATER trigger must unwind the earlier
        replacement triggers already installed — the documented fail-closed
        contract is 'the agent is left with NO triggers'."""
        from config import TriggerSpec
        from trigger_registry import TriggerError
        reg, scheduler, _app = self._make_registry()
        t0 = TriggerSpec(
            name="t0", type="interval", minutes=5,
            channel="telegram", prompt="p",
        )
        reg.register_agent("assistant", [t0], channels=["telegram"])

        good = TriggerSpec(
            name="good", type="interval", minutes=10,
            channel="telegram", prompt="p",
        )
        bad = TriggerSpec(
            name="bad", type="cron", schedule="not-a-cron",
            channel="telegram", prompt="q",
        )
        scheduler.add_job.reset_mock()
        scheduler.remove_job.reset_mock()
        with pytest.raises(TriggerError):
            reg.reregister_for("assistant", [good, bad], channels=["telegram"])
        # NO triggers survive: not the old one, not the partial new ones.
        assert reg._seen_job_ids == set()
        assert reg._specs_by_job_id == {}
        # #620 (S33): the whole list is VALIDATED before anything is installed,
        # so the good replacement is never added — nothing to remove but the
        # old job. (Pre-#620 this asserted remove_job("assistant:good"), i.e.
        # that the good job had been installed and then torn down.)
        assert scheduler.add_job.call_count == 0
        assert [c.args[0] for c in scheduler.remove_job.call_args_list] == ["assistant:t0"]

    def test_reregister_partial_failure_evicts_new_webhooks(self):
        """#307 webhook arm: an installed replacement webhook allowlist entry
        must not survive a later trigger's failure."""
        from config import TriggerSpec
        from trigger_registry import TriggerError
        reg, _sched, _app = self._make_registry()
        hook = TriggerSpec(
            name="hooked", type="webhook", path="",
            channel="telegram", prompt="p",
        )
        bad = TriggerSpec(
            name="bad", type="cron", schedule="not-a-cron",
            channel="telegram", prompt="q",
        )
        with pytest.raises(TriggerError):
            reg.reregister_for("assistant", [hook, bad], channels=["telegram"])
        assert reg.get_webhook_target("hooked") is None
        assert reg.get_auth_policy("hooked") is None

    def test_reregister_unknown_role_is_noop_then_registers(self):
        from config import TriggerSpec
        reg, _sched, _app = self._make_registry()
        t = TriggerSpec(
            name="t", type="interval", minutes=5,
            channel="telegram", prompt="p",
        )
        reg.reregister_for("assistant", [t], channels=["telegram"])
        assert "assistant:t" in reg._seen_job_ids

    def test_stuck_job_removal_refuses_reregistration(self):
        """Terra r1-2: a remove_job failure must NOT be forgotten — dropping
        the id from tracking while the job stays scheduled creates a zombie
        that keeps firing (and collides with a same-id replacement). The
        unwind keeps the stuck job tracked and reregister_for refuses."""
        from config import TriggerSpec
        from trigger_registry import TriggerError
        reg, scheduler, _app = self._make_registry()
        t0 = TriggerSpec(
            name="t0", type="interval", minutes=5,
            channel="telegram", prompt="p",
        )
        reg.register_agent("assistant", [t0], channels=["telegram"])
        scheduler.remove_job.side_effect = RuntimeError("store hiccup")

        t1 = TriggerSpec(
            name="t1", type="interval", minutes=10,
            channel="telegram", prompt="p",
        )
        with pytest.raises(TriggerError):
            reg.reregister_for("assistant", [t1], channels=["telegram"])
        # The stuck job stays TRACKED (the registry must not lie about it)…
        assert "assistant:t0" in reg._seen_job_ids
        # …and no replacement was installed.
        assert "assistant:t1" not in reg._seen_job_ids

    def test_job_already_absent_counts_as_removed(self):
        """A JobLookupError means the job is genuinely gone — tracking
        removal is correct and re-registration proceeds."""
        from apscheduler.jobstores.base import JobLookupError
        from config import TriggerSpec
        reg, scheduler, _app = self._make_registry()
        t0 = TriggerSpec(
            name="t0", type="interval", minutes=5,
            channel="telegram", prompt="p",
        )
        reg.register_agent("assistant", [t0], channels=["telegram"])
        scheduler.remove_job.side_effect = JobLookupError("assistant:t0")

        t1 = TriggerSpec(
            name="t1", type="interval", minutes=10,
            channel="telegram", prompt="p",
        )
        reg.reregister_for("assistant", [t1], channels=["telegram"])
        assert "assistant:t0" not in reg._seen_job_ids
        assert "assistant:t1" in reg._seen_job_ids

    def test_double_failure_error_discloses_leftover_live_jobs(self):
        """Terra r2-2: when registration fails AND the follow-up unwind
        cannot remove an already-installed replacement, the raised error
        must SAY which job(s) remain live — a caller relaying 'fail-closed'
        would otherwise misreport a still-firing trigger."""
        from config import TriggerSpec
        from trigger_registry import TriggerError
        reg, scheduler, _app = self._make_registry()
        t0 = TriggerSpec(
            name="t0", type="interval", minutes=5,
            channel="telegram", prompt="p",
        )
        reg.register_agent("assistant", [t0], channels=["telegram"])
        # First unwind removal (t0) succeeds; the post-failure unwind's
        # removal of the just-installed "good" job fails.
        scheduler.remove_job.side_effect = [None, RuntimeError("stuck")]

        good = TriggerSpec(
            name="good", type="interval", minutes=10,
            channel="telegram", prompt="p",
        )
        bad = TriggerSpec(
            name="bad", type="cron", schedule="not-a-cron",
            channel="telegram", prompt="q",
        )
        with pytest.raises(TriggerError) as excinfo:
            reg.reregister_for("assistant", [good, bad], channels=["telegram"])
        msg = str(excinfo.value)
        assert "assistant:good" in msg and "remain live" in msg
        # And the leftover stays tracked.
        assert "assistant:good" in reg._seen_job_ids
