"""A stand-in for the ``task_posts`` index in the channel's tests.

The channel files every task's landed post through ``task_posts.record`` and
asks ``task_posts.owner`` / ``ended_owner`` who owns a quoted post. The double
answers the way the index does: a job's posts through the engagement registry
that holds the job (every registry the test creates is tracked), a
delegation's from the entries the test opens here.
"""
from __future__ import annotations

from task_model import Requester
from task_posts import TaskRef


class TaskPostsDouble:
    def __init__(self) -> None:
        self.regs: list = []
        self.delegations: dict[str, TaskRef] = {}
        self.live: dict = {}            # TgAddress -> TaskRef
        self.ended: dict = {}           # TgAddress -> TaskRef
        self.filed: list = []           # (owner, address), every call

    @staticmethod
    def _job(rec) -> TaskRef:
        origin = getattr(rec, "origin", None) or {}
        return TaskRef("job", rec.id, rec.role_or_type,
                       Requester(str(origin.get("role") or "assistant"), {}))

    def delegation(self, task_id: str, executor: str, *,
                   requester: str = "assistant") -> TaskRef:
        ref = TaskRef("delegation", task_id, executor, Requester(requester, {}))
        self.delegations[task_id] = ref
        return ref

    def post(self, task_id: str, addr) -> None:
        self.live[addr] = self.delegations[task_id]

    def end(self, task_id: str) -> None:
        for addr in [a for a, r in self.live.items() if r.task_id == task_id]:
            self.ended[addr] = self.live.pop(addr)

    # -- the index's interface ------------------------------------------
    def owner(self, addr):
        if addr in self.live:
            return self.live[addr]
        for reg in self.regs:
            rec = reg.job_post_owner(addr)
            if rec is not None:
                return self._job(rec)
        return None

    def ended_owner(self, addr):
        if addr in self.ended:
            return self.ended[addr]
        for reg in self.regs:
            rec = reg.ended_job_post_owner(addr)
            if rec is not None:
                return self._job(rec)
        return None

    def record(self, owner: str, addr) -> None:
        self.filed.append((owner, addr))
        if owner in self.delegations:
            self.live[addr] = self.delegations[owner]
            return
        for reg in self.regs:
            if reg.get(owner) is not None:
                reg.record_job_post(owner, addr)
                return


def install(monkeypatch) -> TaskPostsDouble:
    """Replace the index's three functions with a double that also tracks
    every ``EngagementRegistry`` created from now on."""
    import engagement_registry
    import task_posts
    double = TaskPostsDouble()
    for name in ("owner", "ended_owner", "record"):
        monkeypatch.setattr(task_posts, name, getattr(double, name))
    real_init = engagement_registry.EngagementRegistry.__init__

    def _init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        double.regs.append(self)
    monkeypatch.setattr(engagement_registry.EngagementRegistry, "__init__", _init)
    return double
