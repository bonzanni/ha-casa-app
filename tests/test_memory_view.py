"""The served view (``memory/view.py``).

A ledger is served whole while it fits the budget; past it, as an index:
entry lines, then group lines, then topic lines, each level added only when
the one below no longer fits, the coarsest served even when it overflows,
then the cheapest refinement that still fits, again and again. Every
current entry stays visible. The view is one ledger's, rendered as of a
commit from the revision history. Real store, ledgers written through the
writer's own patches.
"""
from __future__ import annotations

import re

import pytest

from memory import view
from memory_serving_support import add, fact, ledger_store
from prompt_compiler import CompiledProjection, CompiledPromptBundle, projection_for

pytestmark = [pytest.mark.unit]


def _render(store, budget, ledger="private", commit_no=None):
    with store.transaction() as conn:
        if commit_no is None:
            return view.render(conn, ledger, budget)
        return view.render_as_of(conn, ledger, commit_no, budget)


def _ids(text: str) -> set[str]:
    return set(re.findall(r"^- (o[0-9a-f]{16})\b", text, re.M))


def test_whole_while_it_fits(tmp_path):
    store = ledger_store(tmp_path, [
        fact("thermostat", "set to 20C [2026-10-01]", topic="Home"),
        fact("dentist", "Dr Rossi, Tuesdays", topic="Health"),
    ])
    v = _render(store, 1000)
    assert (v.mode, v.overflow) == (view.WHOLE, False)
    assert v.text.startswith("<memory>\n" + view.HEAD_WHOLE + "\n\n")
    assert v.text.endswith("</memory>")
    assert "## Home\n- thermostat: set to 20C [2026-10-01]" in v.text
    assert "## Health\n- dentist: Dr Rossi, Tuesdays" in v.text
    assert not _ids(v.text)


def test_index_past_the_budget_keeps_every_entry_visible(tmp_path):
    ops = [fact(f"item{i}", f"VALUE-{i} " + "x" * 300, line=f"need {i}")
           for i in range(12)]
    store = ledger_store(tmp_path, ops)
    v = _render(store, 300)
    assert (v.mode, v.overflow) == (view.INDEX, False)
    assert view.HEAD_INDEX in v.text
    assert v.tokens <= 300 + view.tokens(view.HEAD_INDEX) + 10
    with store.transaction() as conn:
        objs = view.objects_as_of(conn, "private", 1)
    full = {o.object_id for o in objs if f"- {o.subject}: " in v.text}
    lines = _ids(v.text)
    # Every entry is shown once: in full or as its line, never both.
    assert full | lines == {o.object_id for o in objs}
    assert not full & lines
    # An index line names the need, never the value.
    for o in objs:
        if o.object_id in lines:
            assert f"VALUE-{o.subject[4:]} " not in v.text


def _entry_line_chars(store) -> int:
    """The size of the entry-line level: every entry as its line."""
    with store.transaction() as conn:
        objs = view.objects_as_of(conn, "private", 10**9)
    packing = view._Packing(view._Tree(objs))
    for o in objs:
        if o.kind == "entry":
            packing.add(o, "line")
    return packing.chars


def test_the_cheapest_refinement_is_applied_first(tmp_path):
    """From the index, the smallest promotion that still fits goes
    first. Red case: promoting in creation order spends the budget on the
    first (larger) entry and leaves the smaller ones as lines."""
    store = ledger_store(tmp_path, [fact("medium", "M" * 200,
                                         line="the medium need")])
    add(store, [fact(f"small{i}", "s" * 60, line=f"small need {i}")
                for i in range(3)])
    # Room for the medium entry alone, or for the three small ones.
    budget = (_entry_line_chars(store) + 190) // 4
    v = _render(store, budget)
    assert v.mode == view.INDEX
    assert all(f"- small{i}: " + "s" * 60 in v.text for i in range(3))
    assert "M" * 200 not in v.text and "the medium need" in v.text


def test_entry_lines_come_before_group_lines(tmp_path):
    """While every entry's line fits, no entry is folded into a group
    line. Red case: starting from the coarsest level and refining down
    spends the budget inside the cheaper group first, and the other group
    stays folded."""
    store = ledger_store(tmp_path)
    _, [a, b] = add(store, [{"op": "add", "kind": "group", "line": "group a"},
                            {"op": "add", "kind": "group", "line": "group b"}])
    add(store, [fact(f"a{i}", "a" * 100, parent=a) for i in range(5)]
        + [fact(f"b{i}", "b" * 400, parent=b) for i in range(20)])
    v = _render(store, (_entry_line_chars(store) + 100) // 4)
    assert a not in v.text and b not in v.text
    assert len(_ids(v.text)) + v.text.count("a" * 100) == 25


def test_groups_then_topics_only_when_the_level_below_no_longer_fits(
        tmp_path):
    store = ledger_store(tmp_path)
    _, [topic] = add(store, [{"op": "add", "kind": "topic",
                               "line": "household matters"}])
    _, [group] = add(store, [{"op": "add", "kind": "group",
                               "line": "the house", "parent": topic}])
    add(store, [fact(f"room{i}", "y" * 400, line=f"what room {i} needs " * 3,
                     parent=group) for i in range(30)])
    # Entry lines fit: no group line.
    roomy = _render(store, 1200)
    assert roomy.mode == view.INDEX
    assert group not in roomy.text and topic not in roomy.text
    assert len(_ids(roomy.text)) + roomy.text.count("y" * 400) == 30
    # Entry lines do not fit, the group line does.
    tight = _render(store, 60)
    assert _ids(tight.text) == {group} and "(30 entries)" in tight.text
    assert tight.overflow is False
    # Nothing finer than the topic fits: the coarsest is served, flagged.
    tiny = _render(store, 2)
    assert _ids(tiny.text) == {topic} and tiny.overflow is True
    assert view.HEAD_OVERFLOW in tiny.text


def test_the_view_is_one_ledgers(tmp_path):
    """Every line comes from the session's ledger; another ledger's
    objects never appear."""
    store = ledger_store(tmp_path, [fact("mine", "PRIVATE-CANARY")])
    add(store, [fact("theirs", "FRIENDS-CANARY")], ledger="friends")
    assert "FRIENDS-CANARY" not in _render(store, 1000).text
    assert "PRIVATE-CANARY" not in _render(store, 1000, ledger="friends").text


def test_render_as_of_an_earlier_commit(tmp_path):
    store = ledger_store(tmp_path)
    first, [oid] = add(store, [fact("city", "Rome")])
    add(store, [{"op": "replace", "object_id": oid,
                 "body": "Porto; earlier: Rome (until 2026-10-01)"}])
    add(store, [fact("pet", "a cat")])
    then = _render(store, 1000, commit_no=first)
    assert "- city: Rome" in then.text and "Porto" not in then.text
    assert "a cat" not in then.text
    now = _render(store, 1000)
    assert "Porto" in now.text and "a cat" in now.text
    # Rendering the same commit twice is byte-identical.
    assert _render(store, 1000, commit_no=first).block_hash == then.block_hash


def test_an_empty_ledger_serves_no_block(tmp_path):
    v = _render(ledger_store(tmp_path), 1000)
    assert v.text == "" and v.mode == view.WHOLE


def test_a_session_reads_the_highest_maintained_ledger_at_or_below_it():
    assert view.ledger_for("private") == "private"
    for clearance in ("family", "friends", "public", None, "bogus"):
        assert view.ledger_for(clearance) is None


def test_unreachable_names_what_no_view_could_reach(tmp_path):
    store = ledger_store(tmp_path)
    _, [group] = add(store, [{"op": "add", "kind": "group", "line": "g"}])
    _, [member] = add(store, [fact("m", "v", parent=group)])
    with store.transaction() as conn:
        assert view.unreachable(conn, "private") == []
        assert view.reachable(conn, "private")
    add(store, [{"op": "retire", "object_id": group}])
    with store.transaction() as conn:
        assert view.unreachable(conn, "private") == [member]
        assert not view.reachable(conn, "private")


def _bundle(text: str) -> CompiledPromptBundle:
    proj = CompiledProjection(system_prompt=text, digest="sha256:" + "0" * 64,
                              estimated_tokens=7)
    return CompiledPromptBundle(role_id="resident:assistant",
                                resolved_model="m", text=proj, voice=proj,
                                restricted_webhook=proj, binding_digest="d")


def test_the_block_sits_between_persona_and_doctrine_outside_the_digest():
    """One opaque slot after <persona>, before <role_doctrine>; the
    projection keeps its compiled digest and estimate."""
    compiled = ("<platform_frame>\nF\n</platform_frame>\n<persona>\nP\n"
                "</persona>\n<role_doctrine>\nD\n</role_doctrine>\n")
    bundle = _bundle(compiled)
    served = projection_for(bundle, channel="telegram", origin_route=None,
                            memory="<memory>\nM\n</memory>")
    assert served.system_prompt == (
        "<platform_frame>\nF\n</platform_frame>\n<persona>\nP\n</persona>\n"
        "<memory>\nM\n</memory>\n<role_doctrine>\nD\n</role_doctrine>\n")
    assert (served.digest, served.estimated_tokens) == (
        bundle.text.digest, bundle.text.estimated_tokens)
    assert projection_for(bundle, channel="telegram", origin_route=None) \
        is bundle.text
