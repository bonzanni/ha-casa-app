"""Human pacing for browser actions (Plan 1 Task 8; design §5.1 "looking like the operator").

Every action scrolls its target into view, checks it is visible and enabled, and returns
control within a fixed timeout (5 s) instead of retrying for Playwright's default 30 s. The
pointer follows a curved path to a point inside the target; typing has a bounded random delay
per character; scrolling moves in steps. Clock and randomness are injected for tests.

This is consistency with a person at the keyboard, not concealment: no fingerprint or
timing obfuscation beyond ordinary human pacing (design §5.1, the operator's "no arms race").

The action gate (action model F9, S2-4, S2-8, G1): a judged press awaits its ``guard`` after
the pointer has moved, immediately before ``mouse.down()``; only ``ok`` presses, and
``on_dispatch`` runs right before the dispatching input. ``type`` replaces what an input or a
textarea holds (a contenteditable takes the text at the caret), sends each printable
character with ``keyboard.type`` only while ``focus`` holds, and never Enter: the only
non-printable key is ``press_enter``'s, behind its own guard. A line break (slice 6b) is no key:
only when its caller allows it, ``type`` inserts it as text (``keyboard.insert_text``, no key
event), behind the same focus check, and ``insert`` puts a long text in the same way. ``scroll`` wheels only over a
point whose element is no form control or editable element, checked before every step, and says
where that left what it scrolled (``_position``).
"""
from __future__ import annotations

import asyncio
import math
import random

ACTION_TIMEOUT = 5.0
MOVE_STEP_DELAY = (0.008, 0.03)
KEY_DELAY = (0.05, 0.18)
PAUSE = (0.08, 0.3)
SCROLL_STEP = 120
SCROLL_STEPS = {"step": 1, "page": 7}
# Why a target cannot be pressed: nothing was sent, and the reply says which.
TARGET_REFUSALS = ("not_visible", "disabled", "no_size")
SHOW_CHECKS = 10    # how often, over half an action's timeout, a hidden target is looked at again


class ActionError(Exception):
    """A typed action failure; carries no selector, address or page text."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class GuardRefused(ActionError):
    """The guard did not pass: ``moved`` (the point is not on the target) or ``changed`` (a
    fact differs, or the keyboard focus left the target). Nothing was pressed or sent."""

    reread = None     # the controller attaches a final read that differed (a tap)


# The element under a point, descending through open shadow roots, is a form control, an
# editable element or a frame (or inside one): a wheel there could change a value.
_EDITABLE_AT = r"""([x, y]) => {
  let e = document.elementFromPoint(x, y);
  while (e && e.shadowRoot) { const d = e.shadowRoot.elementFromPoint(x, y); if (!d || d === e) break; e = d; }
  if (!e) return false;
  for (let c = e; c; c = c.parentElement || ((c.getRootNode && c.getRootNode() instanceof ShadowRoot) ? c.getRootNode().host : null)) {
    // A frame's content is not inspected from here: its area is never a wheel target.
    if (/^(INPUT|TEXTAREA|SELECT|OPTION|IFRAME|FRAME|EMBED|OBJECT)$/.test(c.tagName) || c.isContentEditable) return true;
  }
  return false;
}"""
# What can scroll around an element, the page first and the innermost last, each as
# [how far down it is, how far it can go]: a wheel moves the innermost that can still move.
_SCROLLERS_JS = r"""(from) => {
  const page = document.scrollingElement || document.documentElement, out = [];
  for (let c = from; c; c = c.parentElement || ((c.getRootNode && c.getRootNode() instanceof ShadowRoot) ? c.getRootNode().host : null)) {
    const y = getComputedStyle(c).overflowY;
    if (c !== page && (y === 'auto' || y === 'scroll') && c.scrollHeight > c.clientHeight)
      out.unshift([Math.round(c.scrollTop), c.scrollHeight - c.clientHeight]);
  }
  out.unshift([Math.round(page.scrollTop), page.scrollHeight - page.clientHeight]);
  return out;
}"""
_SCROLLERS_AT = r"""([x, y]) => {
  let e = document.elementFromPoint(x, y);
  while (e && e.shadowRoot) { const d = e.shadowRoot.elementFromPoint(x, y); if (!d || d === e) break; e = d; }
  return (%s)(e);
}""" % _SCROLLERS_JS
_VIEWPORT = "() => [window.innerWidth, window.innerHeight]"
# An input or a textarea with all it holds selected, so what comes next replaces it; anything else
# (a contenteditable, its caret kept: a signature, a quoted reply) answers false.
_SELECT_FIELD = ("(e) => { if (e.tagName !== 'INPUT' && e.tagName !== 'TEXTAREA') return false; "
                 "e.select(); return true; }")


def _position(before: list, after: list) -> dict:
    """Where a scroll left things, from two ``_SCROLLERS_JS`` answers: the outermost scroller that
    moved (what holds it stays under the pointer only while it does not), or with none the
    innermost, the one the wheel was at. ``top`` of ``end`` CSS pixels, and whether it moved."""
    for was, now in zip(before, after):
        if was[0] != now[0]:
            return {"top": now[0], "end": now[1], "moved": True}
    return {"top": after[-1][0], "end": after[-1][1], "moved": False}


def _bezier(p0, p1, p2, p3, t):
    u = 1 - t
    return (u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
            u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1])


def pointer_path(start, end, rng: random.Random) -> list[tuple[float, float]]:
    """A cubic curve from ``start`` to ``end`` with control points off the straight line."""
    dx, dy = end[0] - start[0], end[1] - start[1]
    dist = math.hypot(dx, dy) or 1.0
    nx, ny = -dy / dist, dx / dist  # unit normal
    bend = rng.uniform(0.15, 0.35) * dist * rng.choice((-1, 1))
    c1 = (start[0] + dx * 0.3 + nx * bend, start[1] + dy * 0.3 + ny * bend)
    c2 = (start[0] + dx * 0.7 + nx * bend * 0.5, start[1] + dy * 0.7 + ny * bend * 0.5)
    steps = max(8, min(40, int(dist / 25)))
    return [_bezier(start, c1, c2, end, i / steps) for i in range(1, steps + 1)]


class Pacer:
    def __init__(self, page, *, rng: random.Random | None = None, sleep=asyncio.sleep,
                 action_timeout: float = ACTION_TIMEOUT):
        self.page = page
        self.rng = rng or random.SystemRandom()
        self.sleep = sleep
        self.action_timeout = action_timeout
        self.pointer = (self.rng.uniform(200, 900), self.rng.uniform(150, 600))

    async def _bounded(self, coro):
        try:
            return await asyncio.wait_for(coro, self.action_timeout)
        except asyncio.TimeoutError:
            raise ActionError("timeout") from None

    @staticmethod
    async def _unseen(locator) -> str:
        """Why a target is not visible: the visibility check says no to a zero-area box as well,
        and only its box tells that from a hidden element."""
        box = await locator.bounding_box()
        return "no_size" if box and (box["width"] <= 0 or box["height"] <= 0) else "not_visible"

    async def shown(self, locator) -> None:
        """A target hidden right now may be in a page's transition, so it gets a moment. Scrolling
        one that stays hidden would wait out the whole timeout, so this says why before that."""
        for _ in range(SHOW_CHECKS):
            if await self._bounded(locator.is_visible()):
                return
            await self.sleep(self.action_timeout / 2 / SHOW_CHECKS)
        if await self._bounded(locator.is_visible()):
            return
        raise ActionError(await self._bounded(self._unseen(locator)))

    async def _target_point(self, locator):
        await self.shown(locator)
        await locator.scroll_into_view_if_needed(timeout=self.action_timeout * 1000)
        if not await locator.is_visible():
            raise ActionError(await self._unseen(locator))
        if not await locator.is_enabled():
            raise ActionError("disabled")
        box = await locator.bounding_box()
        if not box or box["width"] <= 0 or box["height"] <= 0:
            raise ActionError("no_size")
        # Integer CSS pixels: the guard's hit test and the press use this very point.
        return (round(box["x"] + box["width"] * self.rng.uniform(0.3, 0.7)),
                round(box["y"] + box["height"] * self.rng.uniform(0.3, 0.7)))

    async def _move_to(self, point):
        for x, y in pointer_path(self.pointer, point, self.rng):
            await self.page.mouse.move(x, y)
            await self.sleep(self.rng.uniform(*MOVE_STEP_DELAY))
        self.pointer = point

    async def click(self, locator, *, guard=None, on_dispatch=None) -> None:
        """Scroll the target into view, move to a point inside it, pause; then, immediately
        before ``mouse.down()``, await ``guard(point)`` (``ok``, ``moved`` or ``changed``) and
        call ``on_dispatch()``. A guard that does not answer ``ok`` presses nothing."""
        async def act():
            point = await self._target_point(locator)
            await self._move_to(point)
            await self.sleep(self.rng.uniform(*PAUSE))
            if guard is not None:
                verdict = await guard(point)
                if verdict != "ok":
                    raise GuardRefused(verdict if verdict in ("moved", "changed") else "changed")
            if on_dispatch is not None:
                on_dispatch()
            await self.page.mouse.down()
            await self.sleep(self.rng.uniform(0.04, 0.12))
            await self.page.mouse.up()
        await self._bounded(act())

    async def type(self, locator, text: str, *, guard=None, on_dispatch=None,
                   focus=None, line_breaks: bool = False) -> None:
        """Click into the field (through ``guard``), select what an input or a textarea holds, then
        type with a per-character delay, so the text replaces it, and an empty text clears it (one
        empty insertion, while ``focus`` holds); a contenteditable takes it at the caret. The
        text is never logged or kept. Typing at human pace may take longer than one action
        timeout in all (a long text is never cut short); every operation on the page — the
        click, each key — is bounded by it, so control always returns. Before every character
        ``focus()`` must hold (the target is the focused element), or typing stops with
        ``changed``. Each character is one ``keyboard.type`` of a printable character; Enter is
        never sent from here (``press_enter``). A line break is inserted as text, never a key
        (a page's Enter handler never sees it), and only with ``line_breaks``: without it a text
        holding one is refused (``unsupported``) before anything is clicked or sent."""
        if "\n" in text and not line_breaks:
            raise ActionError("unsupported")
        await self.click(locator, guard=guard, on_dispatch=on_dispatch)
        selected = await self._bounded(locator.evaluate(_SELECT_FIELD))
        if selected and not text:
            if focus is not None and not await self._bounded(focus()):
                raise GuardRefused("changed")
            await self._bounded(self.page.keyboard.insert_text(""))
        for ch in text:
            if focus is not None and not await self._bounded(focus()):
                raise GuardRefused("changed")
            if ch == "\n":
                await self._bounded(self.page.keyboard.insert_text(ch))
            else:
                await self._bounded(self.page.keyboard.type(ch))
            await self.sleep(self.rng.uniform(*KEY_DELAY))

    async def insert(self, locator, text: str, *, guard=None, on_dispatch=None, focus=None) -> None:
        """Click into a <textarea> through ``guard``, then replace what it holds with one
        insertion (no key event) while ``focus`` holds."""
        await self.click(locator, guard=guard, on_dispatch=on_dispatch)
        await self._bounded(locator.evaluate("(e) => e.select()"))
        if focus is not None and not await self._bounded(focus()):
            raise GuardRefused("changed")
        await self._bounded(self.page.keyboard.insert_text(text))

    async def press_enter(self, guard, on_dispatch=None) -> None:
        """The one non-printable key an agent action sends: Enter, only after ``guard()``
        answers ``ok`` immediately before it."""
        verdict = await self._bounded(guard())
        if verdict != "ok":
            raise GuardRefused(verdict if verdict in ("moved", "changed") else "changed")
        if on_dispatch is not None:
            on_dispatch()
        await self._bounded(self.page.keyboard.press("Enter"))

    async def _editable_at(self, point) -> bool:
        return bool(await self._bounded(self.page.evaluate(_EDITABLE_AT, [point[0], point[1]])))

    async def _safe_point(self):
        """A viewport point whose element is no form control or editable element, or None."""
        size = await self._bounded(self.page.evaluate(_VIEWPORT))
        w, h = (size or (0, 0))[:2]
        if not w or not h:
            return None
        for fy in (0.5, 0.3, 0.7, 0.15, 0.85, 0.05, 0.95):
            for fx in (0.5, 0.3, 0.7, 0.15, 0.85, 0.05, 0.95):
                point = (w * fx, h * fy)
                if not await self._editable_at(point):
                    return point
        return None

    async def scroll_to(self, locator) -> dict | None:
        """Bring a target into view. Its position (``_position``) when that moved anything; None
        when it already was in view. A hidden target says why before it would wait out the
        timeout."""
        await self.shown(locator)
        before = await self._bounded(locator.evaluate(_SCROLLERS_JS))
        await locator.scroll_into_view_if_needed(timeout=self.action_timeout * 1000)
        position = _position(before, await self._bounded(locator.evaluate(_SCROLLERS_JS)))
        return position if position["moved"] else None

    async def scroll(self, direction: str, amount: str) -> dict:
        """Wheel steps at the pointer, and where they left what they scrolled (``_position``).
        Before every step the element under the pointer is checked: over a form control or an
        editable element the pointer first moves to a point that is none of these (never a blur,
        which would fire the field's ``change``), and with no such point the scroll is refused as
        ``unsupported``."""
        if direction not in ("up", "down") or amount not in SCROLL_STEPS:
            raise ActionError("unsupported")
        sign = 1 if direction == "down" else -1
        steps = SCROLL_STEPS[amount] + (self.rng.randint(0, 2) if amount == "page" else 0)
        before = None
        for n in range(max(steps, 3 if amount == "page" else 1)):
            # The browser's real pointer is wherever an earlier action left it: the first step
            # first moves it to a checked point, so every check is of the point it wheels at.
            point = self.pointer
            if await self._editable_at(point):
                point = await self._safe_point()
                if point is None:
                    raise ActionError("unsupported")
            if n == 0 or point != self.pointer:
                await self._bounded(self._move_to(point))
                if await self._editable_at(self.pointer):
                    raise ActionError("unsupported")
            if before is None:
                before = await self._scrollers()
            await self._bounded(self.page.mouse.wheel(0, sign * SCROLL_STEP * self.rng.uniform(0.8, 1.2)))
            await self.sleep(self.rng.uniform(*PAUSE))
        return _position(before, await self._scrollers())

    async def _scrollers(self) -> list:
        return await self._bounded(self.page.evaluate(_SCROLLERS_AT, list(self.pointer)))
