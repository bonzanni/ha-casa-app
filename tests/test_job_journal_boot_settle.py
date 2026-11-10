"""INV-BGJOB-021: a job that ended while Casa died before its message journal was settled:
the next boot settles that journal (each message the transcript lacks is told
as lost once) and removes a journal whose record is gone."""

from types import SimpleNamespace

import pytest

import casa_core
import job_inbox
import tools


class _Registry:
    def __init__(self, records):
        self._records = {r.id: r for r in records}

    def get(self, eid):
        return self._records.get(eid)

    async def settled_terminal_state(self, eid):
        rec = self._records.get(eid)
        return (rec.status if rec is not None else "", False)


def _rec(eid, status):
    return SimpleNamespace(id=eid, status=status)


@pytest.mark.asyncio
async def test_boot_settles_the_journal_of_a_job_that_ended_and_drops_an_orphan(
        monkeypatch):
    for eid in ("ended", "live", "orphan"):
        job_inbox.append(eid, uuid=f"u-{eid}", text="hello", notice=None)
    settled = []

    async def fake_settle(rec, driver):
        settled.append((rec.id, driver))
        job_inbox.discard(rec.id)

    monkeypatch.setattr(tools, "_settle_job_journal", fake_settle)
    registry = _Registry([_rec("ended", "cancelled"), _rec("live", "active")])
    driver = object()

    await casa_core._settle_ended_job_state(registry, driver)

    assert settled == [("ended", driver)]
    assert job_inbox.journaled_ids() == ["live"]
    assert [e["uuid"] for e in job_inbox.load("live")] == ["u-live"]
