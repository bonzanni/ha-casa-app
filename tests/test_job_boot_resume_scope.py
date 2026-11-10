"""INV-BGJOB-018: boot resumes only the jobs that were open when Casa
stopped. A job launched after the channels and agents started — the
registry already holds it when the boot resume runs — belongs to its
launch and is never resumed (or ended) by boot."""

import inspect
from types import SimpleNamespace

import pytest

import background_jobs
import casa_core


class _Registry:
    def __init__(self, records):
        self.records = records

    def active_and_idle(self):
        return list(self.records)


def _job(eid):
    return SimpleNamespace(id=eid, driver="in_casa", origin={"job": {"title": eid}})


@pytest.mark.asyncio
async def test_boot_resumes_only_the_jobs_open_before_the_channels_started(monkeypatch):
    resumed = []

    class _Runner:
        driver = None

        async def resume_at_boot(self, rec):
            resumed.append(rec.id)

    monkeypatch.setattr(background_jobs, "_runner", _Runner())
    monkeypatch.setattr(casa_core, "_settle_ended_job_state",
                        lambda reg, drv: _none())
    reg = _Registry([_job("open-at-stop"), _job("launched-since")])
    await casa_core._resume_background_jobs(reg, object(),
                                            only=frozenset({"open-at-stop"}))
    assert resumed == ["open-at-stop"]


async def _none():
    return None


def test_the_boot_job_snapshot_is_taken_before_the_channels_start():
    """Structural pin (labelled as such): the snapshot must precede every
    path that can launch a job — the channels and the agent loops."""
    src = inspect.getsource(casa_core.main)
    snap = src.find("boot_job_ids = frozenset(")
    start = src.find("await channel_manager.start_all()")
    resume = src.find("only=boot_job_ids)")
    assert 0 < snap < start < resume
