"""The specialist desk is gone, and what it carried still holds.

- Casa's notes for a chat's agent are kept per bot and chat: each producer of
  a note — the Stop line, a refused tap, a scheduled job that did not start,
  and a task run for someone else — leaves exactly one note for the agent of
  the chat on the bot it was made on, read once by that agent's next turn,
  and none for the agent of the same chat id on another bot.
- The code root names none of the desk's pieces.
"""
from __future__ import annotations

import ast
import dataclasses
import pathlib
from types import SimpleNamespace as NS

import pytest

import chat_notes
from notes_harness import _agent, _turn

CODE = pathlib.Path(__file__).resolve().parents[1] / "casa" / "rootfs" / "opt" / "casa"

A, B = 111, 222              # two bots: the operator's private chat id is the same on both
CHAT = 42


# --- the producers, each fired once at bot A --------------------------------------------

async def _stop_line(monkeypatch):
    import plugin_stop

    async def send_reply_line(chat_id, text, reply_to, *, post=None, silent=False):
        return 7
    endpoint = NS(bot_id=A, default_agent="assistant", send_reply_line=send_reply_line)
    monkeypatch.setattr(plugin_stop, "_transport",
                        lambda: NS(endpoint=lambda bot: endpoint))
    await plugin_stop.tell_line(A, CHAT, "finance", "stopped", reply_to=None)


async def _tap_refusal(monkeypatch):
    import agent_lane
    import stored_call_settler
    import tools as tools_mod

    async def deliver_casa_notice(chat_id, text):
        return True
    monkeypatch.setattr(tools_mod, "_agent_role_map", {}, raising=False)  # finance unloaded
    channel = NS(bot_id=A, deliver_casa_notice=deliver_casa_notice,
                 _release_typing=lambda context, chat_id: None)
    lane = agent_lane.LANES.get("finance")
    await stored_call_settler.handle_tap(
        channel=channel, resident_role="finance", chat_id=CHAT, user_id=CHAT, cid="c-1",
        executing_role="finance", idx=0, request_id="r" * 32, reservation=lane.reserve(),
        meta={"options": ["Yes"], "calls": [{"runtime_name": "mcp__p__apply"}],
              "bot_id": A, "chat_id": CHAT})


async def _scheduled_job_not_started(monkeypatch):
    import authz_grants
    import background_jobs
    import tools as tools_mod

    async def deliver_casa_notice(chat_id, text):
        return True
    monkeypatch.setattr(tools_mod, "_agent_role_map", {}, raising=False)
    monkeypatch.setattr(background_jobs, "find_job_host", lambda *a, **k: None)
    monkeypatch.setattr(authz_grants, "_live_operator_identity", lambda: (CHAT, CHAT))
    monkeypatch.setattr(tools_mod, "_casa_endpoint", lambda: NS(
        bot_id=A, has_bot_id=True, deliver_casa_notice=deliver_casa_notice))
    await tools_mod.start_scheduled_job("assistant", NS(job="reconcile", name="nightly"))


async def _task_note(monkeypatch):
    import tools as tools_mod
    from channels.tg_address import TgAddress
    monkeypatch.setattr(tools_mod, "_agent_role_map", {
        "assistant": NS(kind="resident"), "finance": NS(kind="specialist")}, raising=False)
    birth = tools_mod.TaskBirth(task_id="d-1", brief="Check the Q3 invoices",
                                executor="finance", requester=NS(role="assistant"),
                                delivery=TgAddress(A, CHAT))
    tools_mod._note_task(birth, "asked", "done")


PRODUCERS = {
    "Stop line": _stop_line,
    "refused tap": _tap_refusal,
    "scheduled job did not start": _scheduled_job_not_started,
    "task note": _task_note,
}


@pytest.fixture
def fresh():
    import agent_lane
    chat_notes.reset()
    agent_lane.reset()
    yield
    chat_notes.reset()
    agent_lane.reset()


def _notes(query: str) -> int:
    """The number of Casa's notes a turn's query carries."""
    import agent as agent_mod
    return sum(1 for line in query.splitlines()
               if line.startswith(agent_mod.CHAT_NOTE_PREFIX))


@pytest.mark.parametrize("producer", sorted(PRODUCERS))
async def test_a_note_reaches_once_only_the_agent_of_the_chat_on_its_own_bot(
        fresh, monkeypatch, tmp_path, producer):
    await PRODUCERS[producer](monkeypatch)
    agent = _agent(tmp_path)
    on_b = await _turn(agent, chat_id=str(CHAT), _tg_bot=B)       # same chat id, other bot
    assert _notes(on_b) == 0, on_b
    on_a = await _turn(agent, chat_id=str(CHAT), _tg_bot=A)
    assert _notes(on_a) == 1, on_a
    again = await _turn(agent, chat_id=str(CHAT), _tg_bot=A)      # drained once
    assert _notes(again) == 0, again


# --- the code root names none of the desk's pieces ---------------------------------------

#: Every name the desk's removal took out of the code.
REMOVED = (
    "specialist_desk", "DESKS", "DeskRegistry", "Desk(", "DeskBusy", "DeskFaulted",
    "DeskFault", "handle_reply", "delegation_use", "desk_for_delegation", "_desk_skip_permit",
    "_desk_pre", "_desk_run_sink", "desk_log", "has_desk", "desk_target_ok", "_desk_origin",
    "record_echo", "prompt_lines", "DESK_ECHO", "POST_MAP", "PostMap", "DESK_REPLY",
    "DESK_REPLY_BEFORE_APPROVAL", "capture_desk_reply", "desk_role",
    "_dispatch_desk_continuation", "_maybe_route_desk_reply", "_reserve_desk_or_refuse",
    "_spawn_desk_turn", "_route_addressed_file", "file_outcome", "_continue_configurator",
    "deliver_desk_notice", "for_desk", "approval_kept", "(front desk)",
)


def _code_files() -> list[pathlib.Path]:
    return sorted(p for p in CODE.rglob("*") if p.is_file() and "__pycache__" not in p.parts)


def test_no_module_imports_the_desk_and_the_module_is_gone():
    assert not (CODE / "specialist_desk.py").exists()
    importers = []
    for path in sorted(CODE.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            if any(n.split(".")[0] == "specialist_desk" for n in names):
                importers.append(path.relative_to(CODE).as_posix())
    assert importers == []


def test_the_code_root_names_none_of_the_removed_pieces():
    found = []
    for path in _code_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        found += [(path.relative_to(CODE).as_posix(), name) for name in REMOVED if name in text]
    assert found == []


def test_a_delegated_output_carries_no_reply_messages():
    """The delegated run's output lost its ``reply_messages``; the agent's own
    turn report keeps a key of that name, read only where the report is."""
    import tools as tools_mod
    assert "reply_messages" not in {f.name for f in dataclasses.fields(tools_mod.DelegatedOutput)}
    users = sorted({path.relative_to(CODE).as_posix() for path in CODE.rglob("*.py")
                    if "reply_messages" in path.read_text(encoding="utf-8")})
    assert users == ["agent.py", "output_boundary.py"]
