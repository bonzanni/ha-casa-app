"""The pacer's gate (action model slice 2: F9, S2-4, G1 c): a judged press awaits its guard
after the pointer moved and immediately before ``mouse.down()``; typing checks the focus
before every character and never sends Enter; Enter goes only through ``press_enter``'s
guard; scrolling never wheels over a form control. The real ``Pacer`` over a recording page."""
from __future__ import annotations

import asyncio
import random

import pytest

from casa_browser import pacing
from casa_browser.pacing import ActionError, GuardRefused

pytestmark = pytest.mark.asyncio


class Page:
    def __init__(self, editable=lambda x, y: False):
        self.log = []
        self.editable = editable
        self.scrollers = [[0, 0]]
        page = self

        class Mouse:
            async def move(self, x, y):
                page.log.append(("move", round(x), round(y)))

            async def down(self):
                page.log.append(("down",))

            async def up(self):
                page.log.append(("up",))

            async def wheel(self, dx, dy):
                page.log.append(("wheel", page.pointer))

        class Keyboard:
            async def type(self, ch):
                page.log.append(("key", ch))

            async def press(self, key):
                page.log.append(("press", key))

            async def insert_text(self, text):
                page.log.append(("insert", text))
        self.mouse, self.keyboard = Mouse(), Keyboard()
        self.pointer = None

    async def evaluate(self, expr, arg=None):
        if "scrollTop" in expr:
            return self.scrollers
        if "elementFromPoint" in expr:
            return self.editable(*arg)
        if "innerWidth" in expr:
            return [1000, 800]
        return None


class Locator:
    def __init__(self, page):
        self.page = page

    async def scroll_into_view_if_needed(self, timeout=None):
        return None

    async def is_visible(self):
        return True

    async def is_enabled(self):
        return True

    async def bounding_box(self):
        return {"x": 100, "y": 100, "width": 80, "height": 30}

    async def evaluate(self, js, arg=None):          # as a contenteditable answers the select
        return False


async def _no_sleep(_s):
    return None


def pacer(page):
    p = pacing.Pacer(page, rng=random.Random(3), sleep=_no_sleep)
    real_move = page.mouse.move

    async def move(x, y):
        page.pointer = (round(x), round(y))
        await real_move(x, y)
    page.mouse.move = move
    return p


async def test_guard_runs_after_move_before_mousedown():
    page = Page()
    seen = []

    async def guard(point):
        page.log.append(("guard", round(point[0]), round(point[1])))
        seen.append(point)
        return "ok"
    await pacer(page).click(Locator(page), guard=guard,
                            on_dispatch=lambda: page.log.append(("dispatch",)))
    kinds = [e[0] for e in page.log]
    assert kinds[-4:] == ["guard", "dispatch", "down", "up"]
    assert kinds.index("guard") > max(i for i, k in enumerate(kinds) if k == "move")
    last_move = [e for e in page.log if e[0] == "move"][-1]
    assert last_move[1:] == page.log[kinds.index("guard")][1:]   # the point it moved to


@pytest.mark.parametrize("answer", ["moved", "changed", "anything"])
async def test_guard_failure_presses_nothing(answer):
    page = Page()

    async def guard(point):
        return answer
    with pytest.raises(GuardRefused) as err:
        await pacer(page).click(Locator(page), guard=guard,
                                on_dispatch=lambda: page.log.append(("dispatch",)))
    assert err.value.code == ("moved" if answer == "moved" else "changed")
    assert not [e for e in page.log if e[0] in ("down", "up", "dispatch")]


async def test_type_clicks_through_guard():
    page = Page()

    async def guard(point):
        page.log.append(("guard",))
        return "changed"
    with pytest.raises(GuardRefused):
        await pacer(page).type(Locator(page), "hello", guard=guard)
    assert not [e for e in page.log if e[0] in ("down", "key")]


async def test_type_checks_focus_before_every_character():
    page = Page()
    checks = []

    async def focus():
        checks.append(len([e for e in page.log if e[0] == "key"]))
        return len(checks) < 3          # the focus moves after two characters

    async def guard(point):
        return "ok"
    with pytest.raises(GuardRefused, match="changed"):
        await pacer(page).type(Locator(page), "a b", guard=guard, focus=focus)
    assert [e for e in page.log if e[0] == "key"] == [("key", "a"), ("key", " ")]
    assert checks == [0, 1, 2]


async def test_enter_only_through_press_enter_guard():
    page = Page()

    async def guard(point=None):
        page.log.append(("guard",))
        return "ok"
    p = pacer(page)
    await p.type(Locator(page), "zoek", guard=guard)
    assert not [e for e in page.log if e[0] == "press"]       # type never sends Enter
    assert [e[1] for e in page.log if e[0] == "key"] == list("zoek")
    page.log.clear()
    await p.press_enter(guard, on_dispatch=lambda: page.log.append(("dispatch",)))
    assert page.log == [("guard",), ("dispatch",), ("press", "Enter")]
    page.log.clear()

    async def refuse():
        return "changed"
    with pytest.raises(GuardRefused):
        await p.press_enter(refuse)
    assert page.log == []


async def test_a_line_break_is_inserted_as_text_only_when_allowed():
    """Slice 6b: a line break is never a key. With ``line_breaks`` it is inserted as text (no
    key event, so a page's Enter handler never sees it), behind the same focus check as every
    character; without it the text is refused before anything is clicked or sent."""
    page = Page()
    checks = []

    async def focus():
        checks.append(len([e for e in page.log if e[0] in ("key", "insert")]))
        return True

    async def guard(point):
        return "ok"
    await pacer(page).type(Locator(page), "a\nb", guard=guard, focus=focus, line_breaks=True)
    assert [e for e in page.log if e[0] in ("key", "insert", "press")] == [
        ("key", "a"), ("insert", "\n"), ("key", "b")]
    assert checks == [0, 1, 2]
    page.log.clear()
    with pytest.raises(ActionError, match="unsupported"):
        await pacer(page).type(Locator(page), "a\nb", guard=guard, focus=focus)
    assert not [e for e in page.log if e[0] in ("move", "down", "key", "insert", "press")]


async def test_a_line_break_waits_for_the_focus_check():
    page = Page()

    async def focus():
        return not [e for e in page.log if e[0] == "key"]   # lost after the first character

    async def guard(point):
        return "ok"
    with pytest.raises(GuardRefused, match="changed"):
        await pacer(page).type(Locator(page), "a\nb", guard=guard, focus=focus,
                               line_breaks=True)
    assert [e for e in page.log if e[0] in ("key", "insert")] == [("key", "a")]


async def test_scroll_checks_every_wheel_step():
    """G1 c: before every step, the point under the pointer is no form control; over one the
    pointer moves to a point that is none, and with no such point the scroll is refused."""
    page = Page(editable=lambda x, y: 450 <= x <= 550 and 350 <= y <= 450)   # a number input
    p = pacer(page)
    p.pointer = (500, 400)                       # resting over the input
    await p.scroll("down", "page")
    wheels = [e[1] for e in page.log if e[0] == "wheel"]
    assert len(wheels) >= 3
    assert all(not (450 <= x <= 550 and 350 <= y <= 450) for x, y in wheels)
    # The control scrolls under the pointer after each step: every later step moves first.
    last = {"at": None}

    def under_last_wheel(x, y):
        return (round(x), round(y)) == last["at"]
    page2 = Page(editable=under_last_wheel)
    p2 = pacer(page2)
    real = page2.mouse.wheel

    async def wheel(dx, dy):
        await real(dx, dy)
        last["at"] = page2.pointer
    page2.mouse.wheel = wheel
    await p2.scroll("down", "page")
    w2 = [e[1] for e in page2.log if e[0] == "wheel"]
    assert len(w2) >= 3 and all(a != b for a, b in zip(w2, w2[1:]))
    page3 = Page(editable=lambda x, y: True)
    with pytest.raises(ActionError, match="unsupported"):
        await pacer(page3).scroll("down", "step")
    assert not [e for e in page3.log if e[0] == "wheel"]


async def test_scroll_says_where_it_left_what_it_scrolled():
    """The outermost scroller that moved, or with none the innermost the wheel was at."""
    assert pacing._position([[0, 900]], [[120, 900]]) == {"top": 120, "end": 900, "moved": True}
    assert pacing._position([[900, 900]], [[900, 900]]) == {"top": 900, "end": 900, "moved": False}
    assert pacing._position([[0, 0], [40, 500]], [[0, 0], [160, 500]]) == {"top": 160, "end": 500, "moved": True}
    # the page moved, and another box came under the pointer
    assert pacing._position([[0, 900], [40, 500]], [[120, 900], [0, 77]])["top"] == 120
    assert pacing._position([[0, 0], [500, 500]], [[0, 0], [500, 500]]) == {"top": 500, "end": 500, "moved": False}
    page = Page(editable=lambda x, y: False)
    page.scrollers = [[300, 900]]
    assert await pacer(page).scroll("down", "step") == {"top": 300, "end": 900, "moved": False}


class Area(Locator):
    async def evaluate(self, js, *args):
        self.page.log.append(("select",) if "select()" in js else ("eval", js))
        return True                                  # an input or a textarea


async def test_insert_goes_through_the_guard_and_the_focus():
    """A long text is one insertion after the guarded click and the select, with no key event;
    a guard or a focus that does not hold inserts nothing."""
    page = Page()

    async def ok(point=None):
        return "ok"

    async def focused():
        page.log.append(("focus",))
        return True
    await pacer(page).insert(Area(page), "a\nb", guard=ok, focus=focused,
                             on_dispatch=lambda: page.log.append(("dispatch",)))
    kinds = [e[0] for e in page.log if e[0] != "move"]
    assert kinds == ["dispatch", "down", "up", "select", "focus", "insert"]
    assert ("insert", "a\nb") in page.log and not [e for e in page.log if e[0] in ("key", "press")]

    for answer in ("moved", "changed"):
        page = Page()

        async def refuses(point):
            return answer
        with pytest.raises(GuardRefused) as err:
            await pacer(page).insert(Area(page), "text", guard=refuses, focus=focused)
        assert err.value.code == answer and not [e for e in page.log if e[0] == "insert"]

    page = Page()

    async def lost():
        return False
    with pytest.raises(GuardRefused):
        await pacer(page).insert(Area(page), "text", guard=ok, focus=lost)
    assert not [e for e in page.log if e[0] == "insert"]


def slow_inserting(page, done: asyncio.Event | None):
    """``page``'s one insertion returns when ``done`` is set; with None it never returns."""
    async def insert_text(text):
        page.log.append(("inserting", text))
        await (done.wait() if done is not None else asyncio.Event().wait())
        page.log.append(("insert", text))
    page.keyboard.insert_text = insert_text


async def ok_guard(point=None):
    return "ok"


async def test_a_long_insertion_has_its_own_bound_past_the_action_timeout():
    """Chrome's one insertion of a long list grows with its line count (seconds for a couple of
    thousand lines): it is bounded by ``insert_timeout``, not by the per-step action timeout,
    so an insertion that outlasts the action timeout still ends done."""
    page = Page()
    done = asyncio.Event()
    slow_inserting(page, done)
    p = pacer(page)
    p.action_timeout, p.insert_timeout = 0.05, 2.0

    async def focused():
        return True
    asyncio.get_running_loop().call_later(0.3, done.set)    # six action timeouts
    await p.insert(Area(page), "a\nb", guard=ok_guard, focus=focused)
    assert ("insert", "a\nb") in page.log
    assert pacing.Pacer(Page()).insert_timeout == pacing.INSERT_TIMEOUT > pacing.ACTION_TIMEOUT


async def test_an_insertion_past_its_own_bound_and_every_other_step_still_time_out():
    """The insertion's bound is a bound: past it the step raises ``timeout``. The click, the
    select and the focus check keep the action timeout."""
    page = Page()
    slow_inserting(page, None)
    p = pacer(page)
    p.action_timeout, p.insert_timeout = 0.05, 0.3

    async def focused():
        return True
    with pytest.raises(ActionError) as err:
        await p.insert(Area(page), "a\nb", guard=ok_guard, focus=focused)
    assert err.value.code == "timeout" and ("inserting", "a\nb") in page.log
    assert ("insert", "a\nb") not in page.log

    page = Page()
    slow_inserting(page, asyncio.Event())
    p = pacer(page)
    p.action_timeout, p.insert_timeout = 0.05, 5.0

    async def slow_focus():
        await asyncio.sleep(0.3)
        return True
    with pytest.raises(ActionError) as err:
        await p.insert(Area(page), "a\nb", guard=ok_guard, focus=slow_focus)
    assert err.value.code == "timeout" and not [e for e in page.log if e[0] == "inserting"]


async def test_type_selects_an_input_or_textarea_so_the_text_replaces_it():
    """The select comes after the guarded click and before the first key; an empty text is one
    empty insertion behind the focus check, which clears the selection."""
    page = Page()

    async def ok(point=None):
        return "ok"

    async def focused():
        page.log.append(("focus",))
        return True
    await pacer(page).type(Area(page), "ab", guard=ok, focus=focused)
    assert [e for e in page.log if e[0] != "move"] == [
        ("down",), ("up",), ("select",), ("focus",), ("key", "a"), ("focus",), ("key", "b")]

    page = Page()
    await pacer(page).type(Area(page), "", guard=ok, focus=focused)
    assert [e for e in page.log if e[0] != "move"] == [
        ("down",), ("up",), ("select",), ("focus",), ("insert", "")]

    page = Page()

    async def lost():
        return False
    with pytest.raises(GuardRefused):
        await pacer(page).type(Area(page), "", guard=ok, focus=lost)
    assert not [e for e in page.log if e[0] == "insert"]


async def test_type_leaves_a_contenteditable_at_its_caret():
    """Nothing selected, nothing cleared: a signature or a quoted reply stays."""
    page = Page()

    async def ok(point=None):
        return "ok"
    await pacer(page).type(Locator(page), "ab", guard=ok)
    assert [e for e in page.log if e[0] in ("key", "insert")] == [("key", "a"), ("key", "b")]
    page = Page()
    await pacer(page).type(Locator(page), "", guard=ok)
    assert not [e for e in page.log if e[0] in ("key", "insert")]
