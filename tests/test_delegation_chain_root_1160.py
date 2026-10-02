"""#1160 red cases: a chained delegation quotes the chain's ROOT question.

A turn synthesized from a delegation completion must record, as its origin's
``user_text``, the question that completion's origin carried — never the
synthesized notice. Otherwise a delegation launched from that narration turn
records the whole notice as "the original user question", and the next notice
quotes it again, nesting every earlier notice and result text (and persisting
the nested text as the job row's ``task``).

Every case drives the real ``Agent.handle_message``; the origin is read from
``origin_var`` INSIDE the fake SDK client's ``query`` — exactly what
``delegate_to_agent`` snapshots on that turn.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import agent as agent_mod
from bus import BusMessage, MessageBus, MessageType
from channels import ChannelManager
from specialist_registry import DelegationComplete, SpecialistRegistry

try:
    from tests.test_notification_handling import _FakeClient, _make_agent
    from tests.test_delegate_to_agent import (
        _FakeSpecialistClient, _caller_cfg, _seed_specialist_dir,
    )
    from tests.test_specialist_registry import _use_synthetic_roles_dir
except ImportError:
    from test_notification_handling import _FakeClient, _make_agent
    from test_delegate_to_agent import (
        _FakeSpecialistClient, _caller_cfg, _seed_specialist_dir,
    )
    from test_specialist_registry import _use_synthetic_roles_dir

pytestmark = pytest.mark.asyncio

R = "ROOT_1160: reconcile the household invoice"
LABEL = "The original user question was:"


class _CapturingClient(_FakeClient):
    """The resident's SDK client: records each prompt and the origin the turn
    published, read while the turn is live (what a tool handler would see)."""

    prompts: list[str] = []
    origins: list[dict] = []
    on_query = None

    @classmethod
    def reset(cls):
        super().reset()
        cls.prompts = []
        cls.origins = []
        cls.on_query = None

    async def query(self, text):
        await super().query(text)
        type(self).prompts.append(text)
        type(self).origins.append(dict(agent_mod.origin_var.get() or {}))
        if type(self).on_query is not None:
            await type(self).on_query()


def _origin(user_text=...):
    origin = {"role": "assistant", "channel": "telegram",
              "chat_id": "777", "cid": "c1"}
    if user_text is not ...:
        origin["user_text"] = user_text
    return origin


def _notice(origin: dict, n: int, result: str) -> BusMessage:
    complete = DelegationComplete(
        delegation_id=f"d-{n}", agent="finance", status="ok",
        text=result, origin=origin, elapsed_s=1.0,
    )
    return BusMessage(
        type=MessageType.NOTIFICATION, source="finance", target="assistant",
        content=complete, channel="telegram",
        context={"cid": "c1", "chat_id": "777", "delegation_id": f"d-{n}"},
    )


async def test_three_hop_root_survives(tmp_path):
    """RC1: three hops, each completion carrying the origin the previous
    narration turn published. Base: label counts [1, 2, 3]."""
    agent = _make_agent(tmp_path)
    _CapturingClient.reset()
    origin = _origin(R)
    with patch("sdk_client_pool._default_make_client", _CapturingClient):
        for n in range(1, 4):
            await agent.handle_message(
                _notice(origin, n, f"RESULT_CANARY_{n}"))
            origin = dict(_CapturingClient.origins[-1])

    prompts, origins = _CapturingClient.prompts, _CapturingClient.origins
    assert len(prompts) == len(origins) == 3
    assert [p.count(LABEL) for p in prompts] == [1, 1, 1]
    assert [o["user_text"] for o in origins] == [R, R, R]
    assert all(f"{LABEL} {R}\n" in p for p in prompts)
    assert sum(
        f"RESULT_CANARY_{j}" in prompts[k - 1]
        for k in range(1, 4) for j in range(1, k)
    ) == 0


async def test_narration_launch_persists_root(tmp_path, monkeypatch):
    """RC2: a delegation launched through the real ``delegate_to_agent`` from
    inside a narration turn records R — on the registered record's origin and
    on the persisted job row — and no second origin key carries the question."""
    import tools
    from job_registry import JobRegistry

    specialists = tmp_path / "ex"
    specialists.mkdir()
    _seed_specialist_dir(specialists, "finance", enabled=True)
    _use_synthetic_roles_dir(monkeypatch, tmp_path, "finance")
    reg = SpecialistRegistry(
        str(specialists), tombstone_path=str(tmp_path / "del.json"))
    reg.load()
    tools.init_tools(
        ChannelManager(), MessageBus(), reg,
        agent_role_map={"assistant": _caller_cfg(delegates=("finance",)),
                        "finance": reg.get("finance")},
    )

    records = []
    real_register = reg.register_delegation

    async def _spy(record, *args, **kwargs):
        records.append(record)
        return await real_register(record, *args, **kwargs)

    monkeypatch.setattr(reg, "register_delegation", _spy)
    _FakeSpecialistClient.reset(response="FOLLOWUP_RESULT")
    monkeypatch.setattr(tools, "ClaudeSDKClient", _FakeSpecialistClient)

    envelopes = []

    async def _launch():
        envelopes.append(await tools.delegate_to_agent.handler({
            "agent": "finance", "task": "FOLLOWUP_TASK",
            "context": "", "mode": "sync",
        }))

    agent = _make_agent(tmp_path)
    _CapturingClient.reset()
    _CapturingClient.on_query = _launch
    with patch("sdk_client_pool._default_make_client", _CapturingClient):
        await agent.handle_message(_notice(_origin(R), 1, "RESULT_CANARY_1"))

    assert len(_CapturingClient.prompts) == 1
    notice_body = _CapturingClient.prompts[0]
    assert len(envelopes) == 1
    payload = json.loads(envelopes[0]["content"][0]["text"])
    assert len(records) == 1
    record = records[0]
    assert record.origin["user_text"] == R
    assert sum(
        R in str(value) or "RESULT_CANARY_1" in str(value)
        for key, value in record.origin.items() if key != "user_text"
    ) == 0

    reloaded = JobRegistry(tmp_path / "jobs.json")
    await reloaded.load()
    rows = [reloaded.get(payload["delegation_id"])]
    assert rows[0] is not None
    assert [row.task for row in rows] == [R]
    assert "RESULT_CANARY_1" in notice_body  # the turn itself saw the notice


@pytest.mark.parametrize("root", ["", ...], ids=["empty", "missing"])
async def test_empty_or_missing_root_stays_empty(tmp_path, root):
    """RC3: a completion whose origin carries an empty or no ``user_text``
    (legacy rows, boot conversions) records ``''`` — never the notice."""
    agent = _make_agent(tmp_path)
    _CapturingClient.reset()
    with patch("sdk_client_pool._default_make_client", _CapturingClient):
        await agent.handle_message(_notice(_origin(root), 1, "EMPTY_RESULT"))

    assert len(_CapturingClient.origins) == 1
    assert _CapturingClient.origins[0]["user_text"] == ""
