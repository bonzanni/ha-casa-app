"""v0.84.0 (round 4, spec §D1 bullets 3 & 6, Task A3) — the per-ask
render-and-measure lifecycle body-limit validator + self-explaining
``invalid_args``.

Spec: ``docs/superpowers/specs/2026-07-16-engagement-ask-labels-round4-design.md``
§D1. A1/A2 removed the invented LENGTH caps (question/label/short) from
``_validate_ask_args`` — the real Telegram 4096-char message limit is now
enforced here, by RENDERING the body and every terminal lifecycle suffix and
MEASURING the worst case, rather than by an arbitrary length heuristic:

    len(body) + max(len(s) for s in ask_lifecycle_suffixes(...)) <= 4096

Two changes ship together:
  (a) the settle copy becomes BOUNDED and POSITIONAL (``✅ Option 2`` /
      ``✅ Options 1, 3``) instead of re-appending the chosen FULL label(s) —
      an unbounded settle copy would make the worst case un-measurable at ask
      time (see ``tests/test_engagement_ask_lifecycle.py``,
      ``tests/test_multi_select_ask.py``, ``tests/test_option_labels.py``,
      ``tests/test_engagement_ask_readable_buttons.py`` and
      ``tests/test_engagement_ask.py`` for the updated positional
      assertions across every settle-text consumer);
  (b) ``drivers.claude_code_driver.ask_lifecycle_suffixes`` enumerates every
      CLOSED terminal suffix form (live answered incl. multi worst-case
      all-selected, expired, cancelled, superseded, internal-error, boot
      answered/expired, terminal cancellation) so the validator can compute
      the true worst case with no arbitrary margin — a drift test below fails
      if a new suffix constant is added without joining the enumeration.

Placement (v0.84.0 round 4, Task A5): the check runs AT ITS SPEC-LITERAL
PLACEMENT — AFTER Q-number allocation (with the REAL allocated number) and
BEFORE broker registration/posting. On refusal the validation gate owns the
cleanup (clear the ingress marker, tombstone the intent with the detailed
``invalid_args`` outcome, publish FAILED through the gate). The old
pre-allocation ``_WC`` approximation is gone; the pure
measurement machinery (``ask_lifecycle_suffixes``) is number-independent, so
these unit tests pass any placeholder number to it, while the handler-level
tests assert the detail against the REAL first-allocated number (``1``).
"""

from __future__ import annotations

import asyncio
import json

import pytest
from broker_helpers import deliver

import agent as agent_mod
import verdict_broker
from verdict_broker import VerdictBroker
from channels.channel_handlers import (
    _ASK_BODY_LIMIT,
    render_ask_body,
)
from drivers.claude_code_driver import ask_lifecycle_suffixes

# ``ask_lifecycle_suffixes`` never interpolates the number (D1 — no suffix form
# uses it), so the pure-measurement tests pass an arbitrary placeholder. The
# handler-level tests use the REAL first-allocated number (1).
_WC = 9999
_FIRST_N = 1

# ``asyncio_mode = auto`` (pytest.ini) auto-detects the async tests here; this
# file mixes sync unit tests (suffix/drift) with async handler-level tests, so
# there is no module-level asyncio mark (mirrors ``test_multi_select_ask.py``).


# ---------------------------------------------------------------------------
# 1. ask_lifecycle_suffixes — pure measurement machinery
# ---------------------------------------------------------------------------


class TestAskLifecycleSuffixes:
    def test_anchor_includes_answered_below(self) -> None:
        suffixes = ask_lifecycle_suffixes(_WC, [], False)
        assert "\n✅ answered below" in suffixes

    def test_single_select_worst_case_is_last_option_position(self) -> None:
        suffixes = ask_lifecycle_suffixes(
            _WC, ["A", "B", "C"], False)
        assert "\n✅ Option 3" in suffixes

    def test_multi_worst_case_is_every_option_selected_exactly(self) -> None:
        suffixes = ask_lifecycle_suffixes(
            _WC, ["A", "B", "C", "D"], True)
        assert "\n✅ Options 1, 2, 3, 4" in suffixes

    def test_fixed_lifecycle_forms_always_present(self) -> None:
        # Expired / cancelled / superseded / internal-error / boot
        # answered+expired / terminal cancellation never depend on
        # options/multi.
        suffixes = ask_lifecycle_suffixes(
            _WC, ["A", "B"], False)
        assert "\n⌛ expired — engagement paused; reply here to continue" in suffixes
        assert "\n🚫 cancelled" in suffixes
        assert "\n🚫 superseded by your message below" in suffixes
        assert "\n⚠️ internal error — question withdrawn, please resend" in suffixes
        assert "\n⌛ expired — answer by text below" in suffixes       # boot
        assert "\n✅ answered below" in suffixes  # boot-reconcile answered
        assert "\n🛑 engagement ended — this question is closed" in suffixes

    def test_sol_overflow_math_beyond_the_8_option_count_cap(self) -> None:
        """The pure measurement machinery has no opinion on option COUNT
        (that cap is ``_validate_ask_args``'s separate, documented
        product-contract exception) — reproduces Sol's illustrative overflow
        case at the function level: a body that alone fits comfortably, but
        whose worst-case ALL-SELECTED multi settle suffix (over many options)
        pushes the total past Telegram's 4096-char limit."""
        # 40 one-char options — impossible through the handler's separate
        # ``_ASK_MAX_OPTIONS`` = 8 count cap, but the measurement functions
        # themselves have no opinion on count, so this exercises them
        # directly (see the handler-level tests below for the byte-exact
        # boundary construction through the real 8-option-capped endpoint).
        options = [chr(ord("A") + i % 26) for i in range(40)]
        suffixes = ask_lifecycle_suffixes(_WC, options, True)
        worst = max(len(s) for s in suffixes)
        all_selected = "\n✅ Options " + ", ".join(str(i + 1) for i in range(40))
        assert all_selected in suffixes
        assert worst >= len(all_selected)


# ---------------------------------------------------------------------------
# 2. Drift test — every _OPEN_Q_*/_SETTLE_* suffix constant is enumerated
# ---------------------------------------------------------------------------


class TestNoOrphanedSuffixConstant:
    def test_every_suffix_constant_is_enumerated_or_allowlisted(self) -> None:
        import channels.channel_handlers as ch
        import drivers.claude_code_driver as ccd

        # Allow-list is now EMPTY (Task D3): the old re-anchor persist-failure
        # ``_OPEN_Q_SEE_ABOVE`` body suffix is DELETED (r11-1 — the drained
        # unit's finite LOCAL persist retry replaces it, never a body edit), and
        # D1 already retired ``_OPEN_Q_REPOSTED_BELOW`` (its dangling allowlist
        # entry is pruned here). Every remaining ``_OPEN_Q_*``/``_SETTLE_*``
        # constant is enumerated by ``ask_lifecycle_suffixes``. The moved-marker
        # forms are named OUTSIDE the ``_OPEN_Q_*`` prefix this test scans, so
        # they never needed an entry.
        allowlist: set[str] = set()

        enumerated: set[str] = set()
        enumerated.update(
            ask_lifecycle_suffixes(_WC, [], False))
        enumerated.update(
            ask_lifecycle_suffixes(_WC, ["A", "B"], False))
        enumerated.update(ask_lifecycle_suffixes(
            _WC, ["A", "B", "C"], True))

        missing: list[str] = []
        for mod, prefix in ((ch, "_SETTLE_"), (ccd, "_OPEN_Q_")):
            for name, value in vars(mod).items():
                if not name.startswith(prefix) or not isinstance(value, str):
                    continue
                if name in allowlist:
                    continue
                if value not in enumerated:
                    missing.append(name)
        assert missing == []


# ---------------------------------------------------------------------------
# 3. Handler-level: the exact self-explaining invalid_args refusal (D1 · 6)
# ---------------------------------------------------------------------------


class _FakeChannel:
    def __init__(self) -> None:
        self.options_keyboards: list[dict] = []
        self.edits: list[dict] = []
        self._next = 5000

    async def post_options_keyboard(
        self, *, engagement_id, request_id, question, options,
        shorts=None, multi=False,
    ) -> int:
        self.options_keyboards.append({"question": question, "options": list(options)})
        mid = self._next
        self._next += 1
        return mid

    async def send_response_to_topic(self, topic_id, text) -> int:
        mid = self._next
        self._next += 1
        return mid

    async def edit_topic_message(
        self, topic_id, message_id, text, *, clear_keyboard=False,
    ) -> bool:
        self.edits.append({"text": text, "clear_keyboard": clear_keyboard})
        return True


class _FakeRequest:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def json(self) -> dict:
        return self._payload


def _make_ask_handler(tmp_path, monkeypatch):
    from engagement_registry import EngagementRegistry
    from channels.channel_handlers import _make_channel_handlers

    fresh = VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", fresh)
    monkeypatch.setattr(agent_mod, "active_claude_code_driver", None)
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    ch = _FakeChannel()
    handlers = _make_channel_handlers(telegram_channel=ch, engagement_registry=reg)
    return handlers["/internal/channel/ask"], reg, ch, fresh


def _oversized_question(
    options: list, *, multi: bool, over_by: int, number: int = _FIRST_N,
) -> str:
    """Build a question whose rendered body ALONE is just under budget, but
    whose worst-case lifecycle suffix pushes the total ``over_by`` chars past
    Telegram's 4096-char limit — isolating that it is the SUFFIX (not the raw
    body) causing the refusal, per spec §D1 bullet 3. Rendered with the REAL
    allocated ``number`` (the validator now runs post-allocation, Task A5)."""
    worst_suffix_len = max(
        len(s) for s in
        ask_lifecycle_suffixes(number, options, multi))
    target_body_len = (_ASK_BODY_LIMIT - worst_suffix_len) + over_by
    skeleton = render_ask_body(number, "", options)
    question_len = target_body_len - len(skeleton)
    assert question_len > 0
    return "Q" * question_len


async def test_oversized_ask_refused_with_self_explaining_detail(
    tmp_path, monkeypatch,
) -> None:
    options = ["Alpha", "Beta"]
    # The refusal now fires POST-allocation with the REAL first number (1).
    question = _oversized_question(options, multi=False, over_by=5)
    expected_body = render_ask_body(_FIRST_N, question, options)
    expected_suffix = max(
        len(s) for s in
        ask_lifecycle_suffixes(_FIRST_N, options, False))
    expected_n = len(expected_body) + expected_suffix
    assert expected_n == _ASK_BODY_LIMIT + 5

    ask, reg, ch, _broker = _make_ask_handler(tmp_path, monkeypatch)
    rec = await reg.create(
        "executor", "configurator", "claude_code", "t",
        {"user_id": 555}, topic_id=42)
    payload = {
        "engagement_id": rec.id, "request_id": "r1",
        "engagement_token": rec.auth_token,
        "question": question, "options": options, "timeout_s": 60,
    }
    resp = await ask(_FakeRequest(payload))
    body = json.loads(resp.text)

    assert body["ok"] is False
    assert body["error"] == "invalid_args"
    assert "4096" in body["detail"]
    assert "incl. lifecycle suffix" in body["detail"]
    assert str(expected_n) in body["detail"]
    assert body["detail"] == (
        "rendered question+options would exceed Telegram's 4096-char message "
        f"limit (was {expected_n} incl. lifecycle suffix); shorten the "
        "question or reduce options"
    )
    # Post-allocation refusal: the gate owns cleanup — NO keyboard ever posted.
    assert ch.options_keyboards == []


async def test_oversized_multi_ask_all_selected_worst_case_refused(
    tmp_path, monkeypatch,
) -> None:
    options = ["A", "B", "C", "D", "E", "F", "G", "H"]  # the 8-option cap
    question = _oversized_question(options, multi=True, over_by=1)

    ask, reg, ch, _broker = _make_ask_handler(tmp_path, monkeypatch)
    rec = await reg.create(
        "executor", "configurator", "claude_code", "t",
        {"user_id": 555}, topic_id=42)
    payload = {
        "engagement_id": rec.id, "request_id": "r2",
        "engagement_token": rec.auth_token,
        "question": question, "options": options, "multi": True,
        "timeout_s": 60,
    }
    resp = await ask(_FakeRequest(payload))
    body = json.loads(resp.text)

    assert body["ok"] is False
    assert body["error"] == "invalid_args"
    assert "4096" in body["detail"]
    assert ch.options_keyboards == []


async def test_near_boundary_single_select_body_passes(
    tmp_path, monkeypatch,
) -> None:
    """A body that stays within budget even against the worst-case lifecycle
    suffix is accepted — the render-and-measure validator must not
    false-positive-refuse a legitimate near-boundary ask."""
    options = ["Alpha", "Beta"]
    question = _oversized_question(options, multi=False, over_by=-5)

    ask, reg, ch, broker = _make_ask_handler(tmp_path, monkeypatch)
    rec = await reg.create(
        "executor", "configurator", "claude_code", "t",
        {"user_id": 555}, topic_id=42)
    payload = {
        "engagement_id": rec.id, "request_id": "r3",
        "engagement_token": rec.auth_token,
        "question": question, "options": options, "timeout_s": 60,
    }
    task = asyncio.ensure_future(ask(_FakeRequest(payload)))
    # v0.124.1 deflake class (#256): never a fixed sleep before asserting a
    # transition that happens inside the ask task's first scheduler turn —
    # poll bounded by wall-clock instead (this was the one fixed sleep this
    # file kept after test_ask_gates.py was converted; it went red on a
    # loaded CI worker on 2026-08-12).
    deadline = asyncio.get_running_loop().time() + 5.0
    turns = 0
    while not ch.options_keyboards:
        if asyncio.get_running_loop().time() > deadline:
            break
        # A few free yields first, then back off — never a hot spin
        # (test_ask_gates._wait_until's shape).
        await asyncio.sleep(0 if turns < 10 else 0.01)
        turns += 1
    assert len(ch.options_keyboards) == 1
    assert deliver(broker, 
        namespace="engagement_ask", scope=rec.id, request_id="r3",
        option_index=0, actor_id=555) == "delivered"
    resp = await asyncio.wait_for(task, timeout=1.0)
    await broker.drain_hooks()
    body = json.loads(resp.text)
    assert body["ok"] is True
    assert body["outcome"] == "answered"


async def test_astral_body_over_utf16_limit_refused_invalid_args(
    tmp_path, monkeypatch,
) -> None:
    """#328 family (Terra r1): the body gate measures UTF-16 units. An
    astral-heavy question whose ``len()`` fits but whose UTF-16 length
    exceeds the limit is refused ``invalid_args`` here — not passed through
    to a keyboard post Telegram will reject."""
    from text_util import utf16_len

    options = ["Alpha", "Beta"]
    # ~2200 astral chars: code-point length well under 4096, UTF-16 ~4400.
    question = "\U0001F389" * 2200
    body = render_ask_body(_FIRST_N, question, options)
    suffix = max(
        len(s) for s in ask_lifecycle_suffixes(_FIRST_N, options, False))
    assert len(body) + suffix <= _ASK_BODY_LIMIT      # old gate would pass
    assert utf16_len(body) + suffix > _ASK_BODY_LIMIT

    ask, reg, ch, _broker = _make_ask_handler(tmp_path, monkeypatch)
    rec = await reg.create(
        "executor", "configurator", "claude_code", "t",
        {"user_id": 555}, topic_id=42)
    payload = {
        "engagement_id": rec.id, "request_id": "r-astral",
        "engagement_token": rec.auth_token,
        "question": question, "options": options, "timeout_s": 60,
    }
    resp = await ask(_FakeRequest(payload))
    body_out = json.loads(resp.text)
    assert body_out["ok"] is False
    assert body_out["error"] == "invalid_args"
    assert ch.options_keyboards == []
