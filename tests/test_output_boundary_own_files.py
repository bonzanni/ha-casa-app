"""#1218 — the "answered without opening" line counts only the files the turn
received (an S6 file desk turn's own file), a hand-off of a file to a plugin
(``share_inbound_file``) acts on it, and every count names how its files were
armed: a turn with no file of its own never says "you sent in this turn"
(coordinator ruling (A), 2026-10-03)."""
from __future__ import annotations

import os

import pytest

import agent as agent_mod
import agent_inbox as ai
import casa_handoff
import output_boundary as ob
import plugin_handoff as ph
import specialist_desk as sd
import tools
from output_boundary import IntentKind as K
from test_desk_turn import LABEL, OPERATOR, _reply, env  # noqa: F401 — the real desk harness

pytestmark = [pytest.mark.unit]

READY = "/data/agent-inbox/finance/ready/"
OWN = (READY + "1759500000000-b0b0.pdf", "B.pdf")
OLD = tuple((READY + f"17595000000{i:02d}-a{i:03d}.pdf", f"old-{i}.pdf") for i in range(16))
SENT = "you sent in this turn"


def _scope(name="Alex"):
    return ob.TurnScope(id="t1", cid="c", role="assistant", display_name=name,
                        channel="telegram", message_type="channel_in")


def _listed(s, files):
    s.arm(ob.ReadBeforeDescribe(files=tuple(files)))


# --- a turn with a file of its own (T1, T2, T5) -------------------------------------------------

def test_an_own_file_turn_that_listed_the_inbox_and_handed_its_file_off_carries_no_line():
    s = _scope()
    s.receive_files((OWN,))
    _listed(s, (OWN, *OLD))                                  # the inbox holds 17
    s.note_handed_off(OWN[0])
    assert s.admit(K.FINAL_REPLY, "Filed B.").annotations == ()


def test_an_own_file_turn_that_neither_read_nor_handed_off_names_only_its_own_file():
    s = _scope()
    s.receive_files((OWN,))
    _listed(s, (OWN, *OLD))
    assert s.admit(K.FINAL_REPLY, "Filed B.").annotations == (
        "Casa: Alex answered without opening “B.pdf” in this turn.",)
    a = s.admit(K.STORED, "later")
    assert a.note == "Casa: Alex wrote this without opening “B.pdf”."


def test_an_own_file_turn_is_neither_widened_nor_discharged_by_another_inbox_file():
    s = _scope()
    s.receive_files((OWN,))
    other = OLD[0][0]
    s.note_read_attempt(other, display_name="old-0.pdf")
    s.note_read_ok(other)
    s.note_handed_off(OLD[1][0])
    assert s.admit(K.FINAL_REPLY, "x").annotations == (
        "Casa: Alex answered without opening “B.pdf” in this turn.",)
    s.note_read_ok(OWN[0])
    assert s.admit(K.FINAL_REPLY, "x").annotations == ()


def test_own_files_received_after_a_listing_replace_the_listing():
    s = _scope()
    _listed(s, OLD)
    s.receive_files((OWN,))
    assert s.admit(K.FINAL_REPLY, "x").annotations == (
        "Casa: Alex answered without opening “B.pdf” in this turn.",)


def test_several_own_files_keep_the_sent_in_this_turn_wording():
    s = _scope()
    s.receive_files((OWN, OLD[0]))
    assert s.admit(K.FINAL_REPLY, "x").annotations == (
        "Casa: Alex answered without opening any of the 2 files you sent in this turn.",)
    assert s.admit(K.STORED, "x").note == (
        "Casa: Alex wrote this without opening any of the 2 files you sent.")


# --- a turn with no file of its own (T3, T6, Astra d1) -------------------------------------------

def test_a_listed_turn_that_handed_off_any_listed_file_carries_no_line():
    s = _scope()
    _listed(s, OLD[:3])
    s.note_handed_off(OLD[1][0])
    assert s.admit(K.FINAL_REPLY, "x").annotations == ()


def test_a_listed_turn_that_acted_on_nothing_counts_the_files_it_listed():
    s = _scope()
    _listed(s, OLD[:3])
    a = s.admit(K.FINAL_REPLY, "x")
    assert a.annotations == ("Casa: Alex answered without opening any of the 3 files Alex listed.",)
    assert s.admit(K.STORED, "x").note == (
        "Casa: Alex wrote this without opening any of the 3 files Alex listed.")


@pytest.mark.parametrize("arming", ["listed", "tried", "mixed"])
def test_a_turn_with_no_file_of_its_own_never_says_you_sent_in_this_turn(arming):
    s = _scope()
    if arming in ("listed", "mixed"):
        _listed(s, OLD[:2])
    if arming in ("tried", "mixed"):
        for path, name in OLD[2:4]:
            s.note_read_attempt(path, display_name=name)
            s.note_read_failed(path)
    want = {"listed": "any of the 2 files Alex listed",
            "tried": "any of the 2 files Alex tried to open",
            "mixed": "any of the 4 files Alex listed or tried to open"}[arming]
    # every admission kind (Terra, diff round 1): the line is THERE, with the
    # truthful wording, and never the own-files phrase
    for kind in (K.FINAL_REPLY, K.STREAM_UPDATE, K.DISCRETE, K.CAPTION, K.KEYBOARD,
                 K.STORED):
        a = s.admit(kind, "x")
        assert all(SENT not in line for line in a.annotations), (kind, a.annotations)
        assert SENT not in str(a) and SENT not in (a.note or "")
        if kind is K.STORED:
            assert want in (a.note or ""), (kind, a.note)
        else:
            assert any(want in line for line in a.annotations), (kind, a.annotations)
            assert want in str(a), kind


def _fail_reads(s, files):
    for path, name in files:
        s.note_read_attempt(path, display_name=name)
        s.note_read_failed(path)


@pytest.mark.parametrize("kind", [K.FINAL_REPLY, K.STREAM_UPDATE, K.DISCRETE, K.CAPTION,
                                  K.KEYBOARD, K.STORED], ids=lambda k: k.name)
@pytest.mark.parametrize("arming,qualifier", [("listed", "listed"),
                                              ("tried", "tried to open"),
                                              ("mixed", "listed or tried to open")])
def test_non_own_count_line_names_the_persona(arming, qualifier, kind):
    """#1247: Casa knows no persona's pronouns, so a count of several files that
    were not the turn's own names the agent by the line's own persona label —
    never "it" — live and STORED alike. Specified by astra in the red-case
    round; at 9636f81d every arm said "files it …"."""
    s = _scope("Ellen")
    if arming == "listed":
        _listed(s, OLD[:2])
    elif arming == "tried":
        _fail_reads(s, OLD[:2])
    else:
        _listed(s, OLD[:1])
        _fail_reads(s, OLD[1:2])
    verb = "wrote this" if kind is K.STORED else "answered"
    line = f"Casa: Ellen {verb} without opening any of the 2 files Ellen {qualifier}."
    a = s.admit(kind, "x")
    assert a.annotations == (line,)
    if kind is K.STORED:
        assert a.note == line
        assert str(a) == "x"
    else:
        assert str(a) == line + "\n\nx"


def test_a_failed_read_of_an_unlisted_path_is_counted_as_tried_not_listed():
    s = _scope()
    _listed(s, OLD[:1])
    s.note_read_attempt(OLD[5][0], display_name="old-5.pdf")
    s.note_read_failed(OLD[5][0])
    assert s.admit(K.FINAL_REPLY, "x").annotations == (
        "Casa: Alex answered without opening any of the 2 files Alex listed or tried to open.",)
    assert s.admit(K.STORED, "x").note == (
        "Casa: Alex wrote this without opening any of the 2 files Alex listed or tried to open.")


def test_two_failed_reads_with_no_listing_are_files_it_tried_to_open():
    s = _scope()
    for path, name in OLD[:2]:
        s.note_read_attempt(path, display_name=name)
        s.note_read_failed(path)
    assert s.admit(K.FINAL_REPLY, "x").annotations == (
        "Casa: Alex answered without opening any of the 2 files Alex tried to open.",)


def test_a_read_attempt_on_a_listed_path_keeps_it_listed():
    s = _scope()
    _listed(s, OLD[:2])
    s.note_read_attempt(OLD[0][0], display_name="old-0.pdf")
    s.note_read_failed(OLD[0][0])
    assert s.admit(K.FINAL_REPLY, "x").annotations == (
        "Casa: Alex answered without opening any of the 2 files Alex listed.",)


# --- the hand-off tool records evidence only on success (T4) -------------------------------------

class _Sched:
    def add_job(self, *a, **k):
        pass


PDF = b"%PDF-1.4\n%%EOF\n"


@pytest.fixture
async def inbox(tmp_path):
    await ai.wire(_Sched(), str(tmp_path / "agent-inbox"), role="assistant")
    yield ai.get_inbox("assistant")
    ai._reset_for_tests()


@pytest.fixture
async def handoff(tmp_path):
    await ph.wire(_Sched(), str(tmp_path / "handoff"))
    return ph.root()


async def _call(tool, args, scope):
    token = agent_mod.origin_var.set({"role": "assistant", "turn_scope": scope})
    try:
        out = await tool.handler(args)
    finally:
        agent_mod.origin_var.reset(token)
    return out["content"][0]["text"]


async def test_listing_then_sharing_a_file_discharges_the_line(inbox, handoff):
    await inbox.publish(PDF, ".pdf", "a.pdf")
    await inbox.publish(PDF, ".pdf", "b.pdf")
    s = _scope()
    await _call(tools.list_inbound_files, {}, s)
    assert s.admit(K.FINAL_REPLY, "x").annotations == (
        "Casa: Alex answered without opening any of the 2 files Alex listed.",)
    f = inbox.list_files()[0]
    text = await _call(tools.share_inbound_file, {"path": f.path}, s)
    assert text.startswith("Shared ")
    assert s.admit(K.FINAL_REPLY, "x").annotations == ()


async def test_a_refused_or_failed_share_records_no_hand_off(inbox, handoff, monkeypatch):
    await inbox.publish(PDF, ".pdf", "a.pdf")
    [f] = inbox.list_files()
    s = _scope()
    await _call(tools.list_inbound_files, {}, s)
    await _call(tools.share_inbound_file, {"path": f.path + ".nope"}, s)   # not an inbound file
    assert s.evidence.handed_off == set()

    def boom(*a, **k):
        raise casa_handoff.HandoffError("storage_failed", "disk full")
    monkeypatch.setattr(casa_handoff, "publish", boom)
    text = await _call(tools.share_inbound_file, {"path": f.path}, s)
    assert "could not be shared" in text
    assert s.evidence.handed_off == set()
    assert s.admit(K.FINAL_REPLY, "x").annotations == (
        "Casa: Alex answered without opening “a.pdf” in this turn.",)


async def test_a_share_with_no_handoff_folder_records_no_hand_off(inbox, monkeypatch):
    await inbox.publish(PDF, ".pdf", "a.pdf")
    [f] = inbox.list_files()
    monkeypatch.setattr(ph, "root", lambda: None)
    s = _scope()
    await _call(tools.list_inbound_files, {}, s)
    await _call(tools.share_inbound_file, {"path": f.path}, s)
    assert s.evidence.handed_off == set()


# --- the desk turn carries its own file (T7) ------------------------------------------------------

async def test_a_file_desk_turn_owes_only_its_own_file_and_its_hand_off_clears_it(env):
    async def respond(call):
        scope = call.origin["turn_scope"]
        _listed(scope, (OWN, *OLD))                          # list_inbound_files: 17 files
        return tools.DelegatedOutput(text="Filed it.")
    env.respond = respond
    await _reply(env, text="[casa file] B", file_name="B.pdf", file_path=OWN[0])
    [(sent, _ctx)] = env.channel.replies
    assert "Casa: Finance answered without opening “B.pdf” in this turn." in str(sent)
    assert "17" not in str(sent) and SENT not in str(sent)

    async def respond_shared(call):
        scope = call.origin["turn_scope"]
        _listed(scope, (OWN, *OLD))
        scope.note_handed_off(OWN[0])
        return tools.DelegatedOutput(text="Filed it.")
    env.respond = respond_shared
    env.channel.replies.clear()
    await _reply(env, text="[casa file] B", file_name="B.pdf", file_path=OWN[0])
    [(sent, _ctx)] = env.channel.replies
    assert "without opening" not in str(sent)


async def test_a_reply_turn_without_a_file_has_no_own_obligation(env):
    seen = []

    async def respond(call):
        seen.append(call.origin["turn_scope"]._read_before_describe())
        return tools.DelegatedOutput(text="ok")
    env.respond = respond
    await _reply(env)
    assert seen == [None]
