"""Tests for authz_grants.py — canonical argument JSON + the single-use,
TTL-bound GrantStore (A:§3.3, §3.6), plus the casa_core.py hourly sweep
wiring.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from pathlib import Path

import pytest

from authz_grants import (
    DEFAULT_GRANT_TTL_S,
    GRANTS,
    ChallengeCoordinator,
    ChallengeHandle,
    GrantKey,
    GrantStore,
    _CHALLENGE_MAX_CHARS,
    _challenge_expired_text,
    canonical_args_hash,
    canonical_args_json,
    render_challenge_message,
    short_tool_name,
)


# ---------------------------------------------------------------------------
# canonical_args_json (A:§3.6)
# ---------------------------------------------------------------------------


class TestCanonicalArgsJson:
    def test_keys_are_sorted(self):
        assert canonical_args_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'

    def test_nested_dict_keys_are_sorted(self):
        assert (
            canonical_args_json({"b": 1, "a": {"z": 1, "y": 2}})
            == '{"a":{"y":2,"z":1},"b":1}'
        )

    def test_separators_are_compact_no_spaces(self):
        out = canonical_args_json({"a": 1, "b": 2})
        assert " " not in out

    def test_unicode_is_not_escaped(self):
        out = canonical_args_json({"name": "héllo wörld", "emoji": "\U0001F389"})
        assert out == '{"emoji":"\U0001F389","name":"héllo wörld"}'
        assert "\\u" not in out

    def test_list_order_is_preserved_not_sorted(self):
        assert canonical_args_json({"a": [3, 1, 2]}) == '{"a":[3,1,2]}'

    def test_list_of_dicts_each_dict_sorted(self):
        out = canonical_args_json({"a": [{"b": 1, "a": 2}]})
        assert out == '{"a":[{"a":2,"b":1}]}'

    def test_exact_string_shared_prefix_inputs_differ(self):
        a = canonical_args_json({"invoice_id": "INV-1"})
        b = canonical_args_json({"invoice_id": "INV-10"})
        assert a != b
        assert a == '{"invoice_id":"INV-1"}'
        assert b == '{"invoice_id":"INV-10"}'

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_raises_valueerror_on_non_finite_float(self, bad):
        with pytest.raises(ValueError):
            canonical_args_json({"amount": bad})

    def test_raises_valueerror_on_unserializable_value(self):
        with pytest.raises(ValueError):
            canonical_args_json({"tags": {1, 2, 3}})

    def test_unserializable_error_message_is_clear(self):
        with pytest.raises(ValueError, match="not JSON-serializable"):
            canonical_args_json({"tags": {1, 2, 3}})


# ---------------------------------------------------------------------------
# canonical_args_hash
# ---------------------------------------------------------------------------


class TestCanonicalArgsHash:
    def test_is_sha256_hexdigest(self):
        h = canonical_args_hash({"a": 1})
        assert len(h) == 64
        assert re.fullmatch(r"[0-9a-f]{64}", h)

    def test_stable_for_same_input(self):
        assert canonical_args_hash({"a": 1, "b": 2}) == canonical_args_hash(
            {"b": 2, "a": 1}
        )

    def test_distinct_for_shared_prefix_inputs(self):
        h1 = canonical_args_hash({"invoice_id": "INV-1"})
        h2 = canonical_args_hash({"invoice_id": "INV-10"})
        assert h1 != h2

    def test_raises_valueerror_on_non_finite_float(self):
        with pytest.raises(ValueError):
            canonical_args_hash({"amount": float("nan")})


# ---------------------------------------------------------------------------
# GrantKey
# ---------------------------------------------------------------------------


def _key(**overrides) -> GrantKey:
    base = dict(
        operator_id=1,
        chat_id=100,
        enforcement_role="finance",
        artifact_id="artifact-abc",
        tool_name="invoice_reset",
        args_hash="deadbeef",
    )
    base.update(overrides)
    return GrantKey(**base)


class TestGrantKey:
    def test_frozen_and_hashable(self):
        k1 = _key()
        k2 = _key()
        assert k1 == k2
        assert hash(k1) == hash(k2)
        assert {k1: "x"}[k2] == "x"

    def test_distinct_field_differs(self):
        assert _key() != _key(tool_name="other_tool")


# ---------------------------------------------------------------------------
# GrantStore
# ---------------------------------------------------------------------------


class _FakeClock:
    """Injectable monotonic-like clock for TTL tests."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class TestGrantStoreMintConsume:
    def test_consume_without_mint_is_false(self):
        store = GrantStore(_now=_FakeClock())
        assert store.consume(_key()) is False

    def test_mint_then_consume_is_true_then_false(self):
        clock = _FakeClock()
        store = GrantStore(_now=clock)
        key = _key()
        store.mint(key)
        assert store.consume(key) is True
        assert store.consume(key) is False

    def test_default_ttl_matches_module_constant(self):
        assert DEFAULT_GRANT_TTL_S == 300.0

    def test_mint_expires_after_ttl_via_injected_clock(self):
        clock = _FakeClock()
        store = GrantStore(_now=clock)
        key = _key()
        store.mint(key, ttl_s=10.0)
        clock.advance(10.0)  # exactly at expiry -> expired
        assert store.consume(key) is False

    def test_mint_not_yet_expired_still_consumable(self):
        clock = _FakeClock()
        store = GrantStore(_now=clock)
        key = _key()
        store.mint(key, ttl_s=10.0)
        clock.advance(9.999)
        assert store.consume(key) is True

    def test_mint_uses_default_ttl_when_unspecified(self):
        clock = _FakeClock()
        store = GrantStore(_now=clock)
        key = _key()
        store.mint(key)
        clock.advance(DEFAULT_GRANT_TTL_S - 1)
        assert store.consume(key) is True

    def test_mint_replaces_existing_grant_resets_used(self):
        clock = _FakeClock()
        store = GrantStore(_now=clock)
        key = _key()
        store.mint(key)
        assert store.consume(key) is True
        assert store.consume(key) is False
        store.mint(key)  # replace -> fresh, unused grant
        assert store.consume(key) is True

    def test_mint_replaces_expired_grant_with_fresh_one(self):
        clock = _FakeClock()
        store = GrantStore(_now=clock)
        key = _key()
        store.mint(key, ttl_s=5.0)
        clock.advance(10.0)
        assert store.consume(key) is False  # expired
        store.mint(key, ttl_s=5.0)
        assert store.consume(key) is True


class TestGrantStorePurges:
    def test_purge_chat_removes_only_matching_returns_count(self):
        store = GrantStore(_now=_FakeClock())
        k1 = _key(chat_id=100)
        k2 = _key(chat_id=100, tool_name="other_tool")
        k3 = _key(chat_id=200)
        for k in (k1, k2, k3):
            store.mint(k)
        assert store.purge_chat(100) == 2
        assert store.consume(k1) is False
        assert store.consume(k2) is False
        assert store.consume(k3) is True

    def test_purge_role_removes_only_matching_returns_count(self):
        store = GrantStore(_now=_FakeClock())
        k1 = _key(enforcement_role="finance")
        k2 = _key(enforcement_role="finance", tool_name="other_tool")
        k3 = _key(enforcement_role="ops")
        for k in (k1, k2, k3):
            store.mint(k)
        assert store.purge_role("finance") == 2
        assert store.consume(k1) is False
        assert store.consume(k2) is False
        assert store.consume(k3) is True

    def test_purge_artifact_removes_only_matching_returns_count(self):
        store = GrantStore(_now=_FakeClock())
        k1 = _key(artifact_id="artifact-abc")
        k2 = _key(artifact_id="artifact-abc", tool_name="other_tool")
        k3 = _key(artifact_id="artifact-xyz")
        for k in (k1, k2, k3):
            store.mint(k)
        assert store.purge_artifact("artifact-abc") == 2
        assert store.consume(k1) is False
        assert store.consume(k2) is False
        assert store.consume(k3) is True

    def test_purge_on_empty_store_returns_zero(self):
        store = GrantStore(_now=_FakeClock())
        assert store.purge_chat(1) == 0
        assert store.purge_role("x") == 0
        assert store.purge_artifact("y") == 0


class TestGrantStoreSweep:
    def test_sweep_drops_only_expired(self):
        clock = _FakeClock()
        store = GrantStore(_now=clock)
        expired_key = _key(tool_name="expired_tool")
        live_key = _key(tool_name="live_tool")
        store.mint(expired_key, ttl_s=5.0)
        store.mint(live_key, ttl_s=100.0)
        clock.advance(10.0)  # expired_key is now past TTL, live_key is not
        removed = store.sweep()
        assert removed == 1
        assert store.consume(expired_key) is False
        assert store.consume(live_key) is True

    def test_sweep_returns_zero_when_nothing_expired(self):
        clock = _FakeClock()
        store = GrantStore(_now=clock)
        store.mint(_key(), ttl_s=100.0)
        assert store.sweep() == 0

    def test_sweep_does_not_remove_consumed_but_unexpired_grant(self):
        clock = _FakeClock()
        store = GrantStore(_now=clock)
        key = _key()
        store.mint(key, ttl_s=100.0)
        assert store.consume(key) is True
        assert store.sweep() == 0


class TestGrantStoreRealThreadConcurrency:
    def test_exactly_one_thread_consumes(self):
        """r1-B6: N real threads behind a barrier all consume the SAME
        grant concurrently — the store's lock must serialize compare-and-
        mark so exactly one thread sees True."""
        store = GrantStore()  # real clock: default time.monotonic
        key = _key()
        store.mint(key, ttl_s=60.0)

        n = 32
        barrier = threading.Barrier(n)
        results: list[bool] = []
        results_lock = threading.Lock()

        def worker():
            barrier.wait()
            r = store.consume(key)
            with results_lock:
                results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == n
        assert results.count(True) == 1
        assert results.count(False) == n - 1


def test_grants_singleton_is_a_grant_store():
    assert isinstance(GRANTS, GrantStore)


# ---------------------------------------------------------------------------
# casa_core.py wiring: the hourly _authz_grant_sweep job
# ---------------------------------------------------------------------------


def test_authz_grant_sweep_calls_grants_sweep(monkeypatch):
    import casa_core

    calls: list[str] = []

    class _FakeStore:
        def sweep(self) -> int:
            calls.append("swept")
            return 2

    monkeypatch.setattr(casa_core, "GRANTS", _FakeStore())
    asyncio.run(casa_core._authz_grant_sweep())
    assert calls == ["swept"]


def test_authz_grant_sweep_survives_store_exception(monkeypatch):
    """A sweep failure must not kill the shared scheduler job (same
    contract as every other sweep in this file)."""
    import casa_core

    class _BrokenStore:
        def sweep(self) -> int:
            raise RuntimeError("boom")

    monkeypatch.setattr(casa_core, "GRANTS", _BrokenStore())
    asyncio.run(casa_core._authz_grant_sweep())  # must not raise


def test_authz_grant_sweep_is_a_coroutine_function():
    import casa_core

    assert asyncio.iscoroutinefunction(casa_core._authz_grant_sweep)


_CASA_CORE_SRC = (
    Path(__file__).resolve().parent.parent
    / "casa"
    / "rootfs"
    / "opt"
    / "casa"
    / "casa_core.py"
)


def test_authz_grant_sweep_registered_hourly_beside_engagement_sweep():
    """Static wiring guard, mirroring test_scheduled_sweeper_jobs.py: the
    new job must be registered as an hourly interval job, using the
    coroutine function directly (never a sync lambda wrapper — the
    v0.13.0 regression this file's sibling test guards against)."""
    text = _CASA_CORE_SRC.read_text(encoding="utf-8")

    m = re.search(
        r"scheduler\.add_job\(\s*_authz_grant_sweep,(?P<kwargs>.*?)\)",
        text,
        re.S,
    )
    assert m, (
        "casa_core.py must register _authz_grant_sweep with "
        "scheduler.add_job(_authz_grant_sweep, ...)"
    )
    kwargs = m.group("kwargs")
    assert re.search(r"trigger\s*=\s*\"interval\"", kwargs), (
        "_authz_grant_sweep must be an interval job"
    )
    assert re.search(r"hours\s*=\s*1\b", kwargs), (
        "_authz_grant_sweep must run hourly (hours=1)"
    )
    assert re.search(r'id\s*=\s*"authz_grant_sweep"', kwargs)

    # Registered "beside" _engagement_daily_sweep: both add_job calls
    # exist and _authz_grant_sweep is not defined as a sync lambda.
    assert "_engagement_daily_sweep" in text
    assert not re.search(r"lambda:\s*asyncio\.create_task\(\s*_authz_grant_sweep", text)


# ===========================================================================
# W1 — humanized approval copy (generic templates)
# ===========================================================================


class TestShortToolName:
    def test_mcp_namespaced_name_returns_segment_after_last_dunder(self):
        assert (
            short_tool_name("mcp__plugin_lesina-invoice_finance__invoice_reset")
            == "invoice_reset"
        )

    def test_plain_name_without_dunder_is_unchanged(self):
        assert short_tool_name("invoice_reset") == "invoice_reset"

    def test_name_with_exactly_one_dunder_returns_segment_after_it(self):
        assert short_tool_name("foo__bar") == "bar"

    def test_multiple_dunders_uses_the_last_one(self):
        assert short_tool_name("a__b__c__d") == "d"


class TestRenderChallengeMessage:
    def test_header_names_display_role_and_short_tool_name(self):
        text = render_challenge_message(
            tool_name="mcp__plugin_p_s__invoice_reset",
            enforcement_role="finance",
            canonical_json='{"id":"INV-1"}', display_name="Alex",
        )
        assert "Alex (finance) wants to run invoice_reset" in text
        assert text.startswith("\U0001F510 Approval needed")

    def test_no_display_name_falls_back_to_role_in_both_parens(self):
        """Legacy/absent display name ⇒ the render-time guard substitutes the
        role string, giving ``{role} ({role})`` (Part 2 fallback proof)."""
        text = render_challenge_message(
            tool_name="invoice_reset", enforcement_role="finance",
            canonical_json="{}",
        )
        assert "finance (finance) wants to run invoice_reset" in text

    def test_canonical_json_embedded_verbatim_in_fenced_block(self):
        canonical = '{"amount":10,"id":"INV-1"}'
        text = render_challenge_message(
            tool_name="invoice_reset", enforcement_role="finance",
            canonical_json=canonical,
        )
        assert f"```\n{canonical}\n```" in text

    def test_full_tool_id_present_verbatim_even_when_mcp_namespaced(self):
        full = "mcp__plugin_lesina-invoice_finance__invoice_reset"
        text = render_challenge_message(
            tool_name=full, enforcement_role="finance",
            canonical_json='{"id":"INV-1"}',
        )
        assert f"Tool id: {full}" in text

    def test_plain_tool_name_short_and_full_are_identical_but_both_present(self):
        text = render_challenge_message(
            tool_name="invoice_reset", enforcement_role="finance",
            canonical_json="{}", display_name="Alex",
        )
        assert "Alex (finance) wants to run invoice_reset" in text
        assert "Tool id: invoice_reset" in text

    def test_size_gate_measures_the_rendered_string(self):
        """B8/size-gate: overflow is still refused, and the ceiling applies
        to the FULL rendered message (short header + full args + full tool
        id), not just the args."""
        huge = '{"blob":"' + "x" * 4000 + '"}'
        text = render_challenge_message(
            tool_name="invoice_reset", enforcement_role="finance",
            canonical_json=huge,
        )
        assert len(text) > _CHALLENGE_MAX_CHARS

    def test_fallback_form_is_exact_pinned_string(self):
        """Pinned fallback challenge (no/failed summary), byte for byte."""
        text = render_challenge_message(
            tool_name="mcp__plugin_p_s__invoice_reset",
            enforcement_role="finance", canonical_json='{"id":"INV-1"}',
            display_name="Alex",
        )
        assert text == (
            "\U0001F510 Approval needed\n\n"
            "Alex (finance) wants to run invoice_reset with EXACTLY these "
            "arguments:\n\n"
            "```\n{\"id\":\"INV-1\"}\n```\n"
            "Tool id: mcp__plugin_p_s__invoice_reset"
        )

    def test_summarized_form_is_exact_pinned_string(self):
        """Pinned summarized challenge, byte for byte."""
        text = render_challenge_message(
            tool_name="mcp__plugin_p_s__invoice_reset",
            enforcement_role="finance", canonical_json='{"period":"2025-05"}',
            summary="Delete the invoice draft for {period}",
            display_name="Alex",
        )
        assert text == (
            "\U0001F510 Approval needed\n\n"
            "Alex (finance) wants to: Delete the invoice draft for 2025-05\n\n"
            "Exact action (binding):\n"
            "```\n{\"period\":\"2025-05\"}\n```\n"
            "Tool id: mcp__plugin_p_s__invoice_reset"
        )

    def test_no_parse_mode_no_escaping_markdown_metachars_pass_through(self):
        """No parse_mode is used: the renderer must NOT HTML/Markdown-escape.
        A canonical block with backticks/underscores/asterisks stays verbatim
        (the challenge is posted without parse_mode — pinned here)."""
        canonical = '{"note":"a_*b*_ `c`"}'
        text = render_challenge_message(
            tool_name="invoice_reset", enforcement_role="finance",
            canonical_json=canonical, display_name="Alex",
        )
        assert canonical in text
        assert "&" not in text  # no HTML entity escaping
        assert "\\" not in text  # no Markdown backslash escaping


class TestSummaryInterpolationFailSafe:
    """The literal substitutor is deliberately NOT ``str.format``: any brace
    syntax other than a bare ``{identifier}`` — or any unresolved / non-scalar
    / unsafe / brace-bearing value — falls back to the v0.77 headline. Each
    fail class is pinned by asserting the SUMMARIZED wording is absent."""

    def _render(self, summary, canonical='{"x":"y"}'):
        return render_challenge_message(
            tool_name="invoice_reset", enforcement_role="finance",
            canonical_json=canonical, summary=summary, display_name="Alex",
        )

    def _is_summarized(self, text):
        return "wants to: " in text and "Exact action (binding):" in text

    def test_happy_path_interpolates(self):
        text = self._render("do {x}")
        assert self._is_summarized(text)
        assert "wants to: do y" in text

    def test_missing_key_falls_back(self):
        assert not self._is_summarized(self._render("do {absent}"))

    def test_non_scalar_value_falls_back(self):
        assert not self._is_summarized(
            self._render("do {x}", canonical='{"x":{"n":1}}'))

    def test_conversion_bang_r_falls_back(self):
        assert not self._is_summarized(self._render("do {x!r}"))

    def test_format_spec_falls_back(self):
        assert not self._is_summarized(self._render("do {x:>10}"))

    def test_indexing_falls_back(self):
        assert not self._is_summarized(self._render("do {x[0]}"))

    def test_attribute_access_falls_back(self):
        assert not self._is_summarized(self._render("do {x.y}"))

    def test_nested_braces_fall_back(self):
        assert not self._is_summarized(self._render("do {a{x}}"))

    def test_unmatched_open_brace_falls_back(self):
        assert not self._is_summarized(self._render("do {x"))

    def test_unmatched_close_brace_falls_back(self):
        assert not self._is_summarized(self._render("do x}"))

    def test_escaped_double_brace_falls_back(self):
        assert not self._is_summarized(self._render("do {{x}}"))

    def test_interpolated_value_with_open_brace_falls_back(self):
        assert not self._is_summarized(
            self._render("do {x}", canonical='{"x":"a{b"}'))

    def test_interpolated_value_with_close_brace_falls_back(self):
        assert not self._is_summarized(
            self._render("do {x}", canonical='{"x":"a}b"}'))

    def test_unsafe_unicode_interpolated_value_falls_back(self):
        # U+2028 line separator in the arg value (built via json.dumps so
        # no raw control/bidi glyph lives in the test source).
        canonical = json.dumps({"x": "a" + chr(0x2028) + "b"},
                               ensure_ascii=False, separators=(",", ":"))
        assert not self._is_summarized(self._render("do {x}", canonical=canonical))

    def test_bool_renders_true_false_lowercase(self):
        text = self._render("flag {x}", canonical='{"x":true}')
        assert "wants to: flag true" in text
        text2 = self._render("flag {x}", canonical='{"x":false}')
        assert "wants to: flag false" in text2

    def test_int_value_renders_matching_canonical(self):
        text = self._render("n {x}", canonical='{"x":10}')
        assert "wants to: n 10" in text

    def test_string_value_exactly_80_untouched(self):
        v = "a" * 80
        text = self._render("v {x}", canonical=f'{{"x":"{v}"}}')
        assert f"wants to: v {v}" in text  # no ellipsis

    def test_string_value_81_ellipsized_to_77_plus_ellipsis(self):
        v = "a" * 81
        text = self._render("v {x}", canonical=f'{{"x":"{v}"}}')
        assert f"wants to: v {'a' * 77}…" in text


class TestSummaryTemplateUnsafeUnicodeFallback:
    """A template carrying an UNSAFE-TEXT codepoint is refused at install time
    (W1); should one still reach the renderer, each disjoint UNSAFE-TEXT group
    makes interpolation fall back rather than emit a control/bidi glyph."""

    def _summarized(self, template):
        text = render_challenge_message(
            tool_name="invoice_reset", enforcement_role="finance",
            canonical_json='{"x":"y"}', summary=template, display_name="Alex",
        )
        return "wants to: " in text and "Exact action (binding):" in text

    def test_c0_control_template_falls_back(self):
        assert not self._summarized("do \x01 {x}")

    def test_c1_control_template_falls_back(self):
        assert not self._summarized("do \x85 {x}")

    def test_u2028_line_separator_template_falls_back(self):
        assert not self._summarized("do " + chr(0x2028) + " {x}")

    def test_u2029_paragraph_separator_template_falls_back(self):
        assert not self._summarized("do " + chr(0x2029) + " {x}")

    def test_u061c_arabic_letter_mark_template_falls_back(self):
        assert not self._summarized("do " + chr(0x61c) + " {x}")

    def test_u200e_lrm_template_falls_back(self):
        assert not self._summarized("do " + chr(0x200e) + " {x}")

    def test_u202e_bidi_override_template_falls_back(self):
        assert not self._summarized("do " + chr(0x202e) + " {x}")

    def test_u2066_bidi_isolate_template_falls_back(self):
        assert not self._summarized("do " + chr(0x2066) + " {x}")


class TestDisplayNameRenderGuard:
    def _render(self, display_name):
        return render_challenge_message(
            tool_name="invoice_reset", enforcement_role="finance",
            canonical_json="{}", display_name=display_name,
        )

    def test_name_present_used_verbatim(self):
        assert "Alex (finance)" in self._render("Alex")

    def test_empty_name_falls_back_to_role(self):
        assert "finance (finance)" in self._render("")

    def test_none_name_falls_back_to_role(self):
        assert "finance (finance)" in self._render(None)

    def test_unsafe_name_falls_back_to_role(self):
        assert "finance (finance)" in self._render("Al" + chr(0x202E) + "ex")

    def test_length_64_accepted(self):
        name = "N" * 64
        assert f"{name} (finance)" in self._render(name)

    def test_length_65_falls_back(self):
        assert "finance (finance)" in self._render("N" * 65)


class TestChallengeExpiredText:
    def test_uses_short_name_only(self):
        text = _challenge_expired_text("mcp__plugin_p_s__invoice_reset")
        assert text == "⌛ Expired — invoice_reset was not approved in time"

    def test_plain_tool_name_matches_settlement_pattern(self):
        assert _challenge_expired_text("invoice_reset") == (
            "⌛ Expired — invoice_reset was not approved in time"
        )


# ===========================================================================
# ChallengeCoordinator (Task 5, A:§3.4) — atomic challenge registration, an
# async-settled setup driver, two-latch cleanup, the authz finish hook, and
# the pinned shutdown drain.
# ===========================================================================


class _FakeChannel:
    """Records every post/edit/dispatch the coordinator drives, and can be
    configured to block, fail, or raise on demand.

    ``post_dm_keyboard`` optionally awaits ``post_gate`` first so a test can
    hold the setup driver mid-post and race a timeout / cancel / caller
    cancellation against it. ``log`` is a shared ordered event trace used by
    the mint-before-dispatch ordering test."""

    def __init__(self, *, log: list | None = None) -> None:
        self.posts: list = []
        self.edits: list = []
        self.dispatches: list = []
        self.eng_dispatches: list = []
        self.post_result: int | None = 55
        self.post_raises = False
        self.post_gate: asyncio.Event | None = None
        self.dispatch_result = True
        self.edit_raises = False
        self.eng_dispatch_raises = False
        self.leases: list = []
        self.log = log if log is not None else []

    async def post_dm_keyboard(self, *, chat_id, request_id, text, options):
        self.posts.append((chat_id, request_id, text, tuple(options)))
        if self.post_gate is not None:
            await self.post_gate.wait()
        if self.post_raises:
            raise RuntimeError("post boom")
        return self.post_result

    async def edit_dm_message(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))
        self.log.append(("edit", text))
        if self.edit_raises:
            raise RuntimeError("edit boom")
        return True

    async def _dispatch_button_continuation(
        self, *, chat_id, user_id, target_role, request_id, text,
    ):
        self.dispatches.append(
            dict(chat_id=chat_id, user_id=user_id, target_role=target_role,
                 request_id=request_id, text=text)
        )
        self.log.append(("dispatch", text))
        return self.dispatch_result

    async def _dispatch_engagement_continuation(
        self, *, engagement_id, text, inbound_reservation=None,
    ):
        # #400: the engagement-resume seam the authz finish hook uses for an
        # engagement-origin challenge (in place of _dispatch_button_continuation).
        # C1/#663: the seam also carries the tap-commit ingress reservation so
        # deliver_system_turn can release it the instant the ticket exists;
        # defaulted so the pre-C1 call shape keeps working unchanged.
        self.eng_dispatches.append(dict(engagement_id=engagement_id, text=text))
        self.log.append(("eng_dispatch", text))
        if inbound_reservation is not None:
            self.log.append(("eng_dispatch_reservation", True))
        if self.eng_dispatch_raises:
            raise RuntimeError("resume boom")
        return self.dispatch_result

    def engagement_inbound_reservation(self, engagement_id):
        # C1/#663: the channel-owned lease factory. Present on this fake for
        # BOTH trees, so a pre-fix run records zero takes because production
        # never asks for one — not because the fixture cannot supply it.
        lease = _SpyLease(engagement_id, self.log)
        self.leases.append(lease)
        return lease


class _SpyLease:
    """C1/#663: records the production call order of the synchronous
    tap-commit reservation lease. Idempotent, like the real one."""

    def __init__(self, engagement_id, log):
        self.engagement_id = engagement_id
        self.log = log
        self.takes = 0
        self.releases = 0
        self._held = False

    def take(self) -> bool:
        self.takes += 1
        self.log.append(("reserve", self.engagement_id))
        self._held = True
        return True

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        self.releases += 1
        self.log.append(("release", self.engagement_id))


class _SpyGrants:
    """A GrantStore stand-in that records mint order into a shared log while
    still behaving like a real single-use store for consume()."""

    def __init__(self, log: list) -> None:
        self._log = log
        self._real = GrantStore()

    def mint(self, key, **kw):
        self._log.append(("mint", key))
        self._real.mint(key, **kw)

    def consume(self, key):
        return self._real.consume(key)


def _fresh_env(monkeypatch, *, ttl=None, log=None):
    """A fresh broker (monkeypatched into the singleton slot the coordinator
    resolves at call time), a fresh coordinator, and a fake channel."""
    import verdict_broker

    broker = verdict_broker.VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", broker)
    if ttl is not None:
        import authz_grants
        monkeypatch.setattr(authz_grants, "_CHALLENGE_TTL_S", ttl)
    coord = ChallengeCoordinator()
    channel = _FakeChannel(log=log)
    return broker, coord, channel


def _create(coord, channel, key=None, *, chat_id=100, operator_id=7,
            target_role="finance-full", tool_name="invoice_reset",
            canonical_json='{"amount":10,"id":"INV-1"}',
            enforcement_role="finance", summary=None, display_name=None,
            engagement_id="", grants=None):
    if key is None:
        key = _key(chat_id=chat_id, enforcement_role=enforcement_role,
                   tool_name=tool_name, args_hash=canonical_args_hash({"x": 1}),
                   engagement_id=engagement_id)
    handle = coord.get_or_create(
        key, chat_id=chat_id, operator_id=operator_id, target_role=target_role,
        tool_name=tool_name, canonical_json=canonical_json,
        enforcement_role=enforcement_role, channel=channel,
        summary=summary, display_name=display_name,
        engagement_id=engagement_id,
        grants=GrantStore() if grants is None else grants,
    )
    return key, handle


async def _settle(n: int = 6):
    for _ in range(n):
        await asyncio.sleep(0)


def _tap(broker, ch, idx, *, actor=7, run_sync_step=True):
    """Replicate the telegram callback's claim → commit → (immediate) sync
    step ordering, without any await between commit and the sync step."""
    claim = broker.claim(
        namespace="resident_ask", scope=ch.scope, request_id=ch.rid,
        option_index=idx, actor_id=actor,
    )
    assert not isinstance(claim, str), f"claim rejected: {claim}"
    assert broker.commit(claim) is True
    if run_sync_step:
        step = ch.req.meta.get("on_commit_sync")
        if step is not None:
            step(idx)


class TestChallengeAtomicRegistration:
    async def test_concurrent_hook_coroutines_one_record_one_keyboard(
        self, monkeypatch,
    ):
        """r2-B2 production shape: many hook coroutines racing the SAME key on
        ONE loop → exactly ONE broker record and ONE keyboard; exactly one
        handle reports created=True."""
        broker, coord, channel = _fresh_env(monkeypatch)
        key = _key()

        results: list[ChallengeHandle] = []

        async def hook():
            _k, handle = _create(coord, channel, key)
            results.append(handle)
            await handle.settled_post()

        await asyncio.gather(*[hook() for _ in range(16)])
        await _settle()

        assert sum(1 for h in results if h.created) == 1
        assert sum(1 for h in results if not h.created) == 15
        # exactly ONE live broker record (still pending — nobody answered)
        assert broker.pending(namespace="resident_ask", scope="authz:100") == \
            [coord._entries[key].rid]
        # exactly one keyboard was ever posted
        assert len(channel.posts) == 1

    async def test_refused_when_oversized_no_entry_no_keyboard(
        self, monkeypatch,
    ):
        broker, coord, channel = _fresh_env(monkeypatch)
        huge = '{"blob":"' + "x" * 4000 + '"}'
        key, handle = _create(coord, channel, canonical_json=huge)
        await _settle()

        assert handle.created is True
        assert handle.refused == "args_too_large"
        assert key not in coord._entries
        assert channel.posts == []
        assert broker.pending(namespace="resident_ask", scope="authz:100") == []

    async def test_registration_is_synchronous_before_first_turn(
        self, monkeypatch,
    ):
        """r4-B2/r5-B1: the broker request exists the moment get_or_create
        returns — BEFORE the driver's first event-loop turn — so /new /
        shutdown always see a cancellable record."""
        broker, coord, channel = _fresh_env(monkeypatch)
        key, handle = _create(coord, channel)
        # No await yet: the record must already be live and cancellable.
        assert broker.pending(namespace="resident_ask", scope="authz:100") == \
            [coord._entries[key].rid]
        assert channel.posts == []  # the driver has not run its first turn
        await handle.settled_post()

    async def test_posted_text_uses_humanized_render_challenge_message(
        self, monkeypatch,
    ):
        """Wiring check: the ACTUAL posted keyboard text is exactly what
        ``render_challenge_message`` renders — no separate/duplicated
        template lives inside ``get_or_create``."""
        broker, coord, channel = _fresh_env(monkeypatch)
        canonical = '{"amount":10,"id":"INV-1"}'
        key, handle = _create(
            coord, channel, tool_name="invoice_reset",
            enforcement_role="finance", canonical_json=canonical,
            summary="reset invoice {id}", display_name="Alex",
        )
        await handle.settled_post()
        # The posted keyboard text is EXACTLY what render_challenge_message
        # renders with the SAME summary + display name threaded through.
        assert channel.posts[0][2] == render_challenge_message(
            tool_name="invoice_reset", enforcement_role="finance",
            canonical_json=canonical, summary="reset invoice {id}",
            display_name="Alex",
        )

    async def test_summary_inflated_message_over_ceiling_is_refused(
        self, monkeypatch,
    ):
        """B8: the size gate measures the WHOLE rendered string, so a summary
        that interpolates a huge value can push it over the ceiling — refused,
        NO entry, NO keyboard."""
        broker, coord, channel = _fresh_env(monkeypatch)
        big = "x" * 4000
        canonical = json.dumps({"blob": big}, separators=(",", ":"))
        key, handle = _create(
            coord, channel, tool_name="invoice_reset",
            enforcement_role="finance", canonical_json=canonical,
            summary="do {blob}", display_name="Alex",
        )
        assert handle.refused == "args_too_large"
        assert channel.posts == []
        assert key not in coord._entries


class TestSettledPostClassification:
    async def test_pending_is_posted(self, monkeypatch):
        broker, coord, channel = _fresh_env(monkeypatch)
        _key_, handle = _create(coord, channel)
        assert await handle.settled_post() == "posted"
        assert len(channel.posts) == 1

    async def test_post_none_is_delivery_failed(self, monkeypatch):
        broker, coord, channel = _fresh_env(monkeypatch)
        channel.post_result = None
        _key_, handle = _create(coord, channel)
        assert await handle.settled_post() == "delivery_failed"

    async def test_post_raises_is_delivery_failed(self, monkeypatch):
        broker, coord, channel = _fresh_env(monkeypatch)
        channel.post_raises = True
        _key_, handle = _create(coord, channel)
        assert await handle.settled_post() == "delivery_failed"

    async def test_timeout_while_posting_is_inactive_even_if_post_succeeds(
        self, monkeypatch,
    ):
        broker, coord, channel = _fresh_env(monkeypatch, ttl=0.02)
        channel.post_gate = asyncio.Event()
        key, handle = _create(coord, channel)
        task = asyncio.ensure_future(handle.settled_post())
        await asyncio.sleep(0)          # driver starts, post blocks on the gate
        await asyncio.sleep(0.05)       # the 0.02s TTL timer fires while blocked
        channel.post_gate.set()         # NOW the post completes (returns a mid)
        assert await task == "inactive"
        await _settle()
        assert len(channel.posts) == 1  # exactly one keyboard
        # the finish hook edited it to an expired state
        assert any("expired" in e[2].lower() for e in channel.edits)
        assert key not in coord._entries

    async def test_new_while_posting_is_inactive_even_if_post_succeeds(
        self, monkeypatch,
    ):
        broker, coord, channel = _fresh_env(monkeypatch)
        channel.post_gate = asyncio.Event()
        key, handle = _create(coord, channel)
        task = asyncio.ensure_future(handle.settled_post())
        await asyncio.sleep(0)          # driver starts, post blocks
        broker.cancel_scope(namespace="resident_ask", scope="authz:100",
                            reason="new_session")
        channel.post_gate.set()
        assert await task == "inactive"
        await _settle()
        assert len(channel.posts) == 1
        assert key not in coord._entries

    async def test_survives_caller_cancellation_one_keyboard(self, monkeypatch):
        broker, coord, channel = _fresh_env(monkeypatch)
        channel.post_gate = asyncio.Event()
        key, handle = _create(coord, channel)
        task = asyncio.ensure_future(handle.settled_post())
        await asyncio.sleep(0)          # driver posting, blocked on the gate
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        channel.post_gate.set()         # driver completes on its own
        await coord.drain()
        await _settle()
        assert len(channel.posts) == 1  # exactly ONE keyboard ever


class TestPrePostAndShutdownTerminal:
    async def test_immediate_new_before_first_turn(self, monkeypatch):
        """/new fired synchronously after get_or_create, BEFORE the driver's
        first turn → record cancelled, NO keyboard, NO entry, NO lingering
        task (r4-B2)."""
        broker, coord, channel = _fresh_env(monkeypatch)
        key, handle = _create(coord, channel)
        # synchronous /new, before any await
        broker.cancel_scope(namespace="resident_ask", scope="authz:100",
                            reason="new_session")
        assert await handle.settled_post() == "inactive"
        await _settle()
        assert channel.posts == []          # ensure_posted no-op'd on done future
        assert key not in coord._entries
        assert all(d.done() for d in coord._drivers)

    async def test_shutdown_before_first_turn(self, monkeypatch):
        broker, coord, channel = _fresh_env(monkeypatch)
        key, handle = _create(coord, channel)
        broker.cancel_all(reason="casa_shutdown")
        await coord.drain()
        await broker.drain_hooks()
        await _settle()
        assert channel.posts == []
        assert key not in coord._entries
        assert all(d.done() for d in coord._drivers)

    async def test_pre_post_terminal_marks_setup_directly(self, monkeypatch):
        """Request cancelled before ensure_posted ran → req._setup_task stays
        None and the driver settles setup DIRECTLY (both latches land → entry
        removed)."""
        broker, coord, channel = _fresh_env(monkeypatch)
        key, handle = _create(coord, channel)
        req = coord._entries[key].req
        broker.cancel(namespace="resident_ask", scope="authz:100",
                      request_id=coord._entries[key].rid, reason="x")
        await handle.settled_post()
        await _settle()
        assert req._setup_task is None
        assert key not in coord._entries


class TestAuthzFinishHook:
    async def test_approval_mints_into_injected_store_not_module_global(
        self, monkeypatch,
    ):
        """#311: the approval used to mint into the module-global GRANTS even
        when the coordinator's caller injected its own store (the documented
        AuthzDeps seam) — the injected store never saw the approval and the
        continuation re-challenged. The mint must land in the store passed to
        get_or_create, and ONLY there."""
        import authz_grants

        class _Boom:
            def mint(self, key, **kw):  # pragma: no cover — the red case
                raise AssertionError(
                    "approval minted into the module-global GRANTS, "
                    "not the injected store"
                )

        monkeypatch.setattr(authz_grants, "GRANTS", _Boom())
        log: list = []
        injected = _SpyGrants(log)
        broker, coord, channel = _fresh_env(monkeypatch, log=log)
        key, handle = _create(coord, channel, grants=injected)
        assert await handle.settled_post() == "posted"
        ch = coord._entries[key]
        _tap(broker, ch, 0)
        await _settle()

        assert log[0] == ("mint", key)
        # The grant is really IN the injected store (single-use consume).
        assert injected.consume(key) is True
        assert [e[0] for e in log if e[0] == "mint"] == ["mint"]

    async def test_approve_event_order_mint_edit_dispatch_verbatim(
        self, monkeypatch,
    ):
        log: list = []
        spy = _SpyGrants(log)
        broker, coord, channel = _fresh_env(monkeypatch, log=log)
        canonical = '{"amount":10,"id":"INV-1"}'
        key, handle = _create(coord, channel, canonical_json=canonical,
                              grants=spy)
        assert await handle.settled_post() == "posted"
        ch = coord._entries[key]
        _tap(broker, ch, 0)
        await _settle()

        kinds = [e[0] for e in log]
        assert kinds == ["mint", "edit", "dispatch"]
        assert log[0][1] == key                         # minted the exact key
        # the approved edit precedes the dispatch and names the tool
        assert "approved" in channel.edits[-1][2].lower()
        assert channel.dispatches[0]["target_role"] == "finance-full"
        # canonical JSON embedded verbatim in the continuation text
        assert canonical in channel.dispatches[0]["text"]
        assert "[authorization approved]" in channel.dispatches[0]["text"]
        assert "finance" in channel.dispatches[0]["text"]  # enforcement_role

    async def test_engagement_approve_resumes_engagement_not_bus_role(
        self, monkeypatch,
    ):
        """#400: a challenge created with engagement_id resumes the ENGAGEMENT
        (via _dispatch_engagement_continuation) on approval — NOT the resident
        bus-role button continuation — carrying the same verbatim approval text,
        and mints the engagement-bound key."""
        log: list = []
        spy = _SpyGrants(log)
        broker, coord, channel = _fresh_env(monkeypatch, log=log)
        canonical = '{"amount":10,"id":"INV-1"}'
        eng_key = _key(
            chat_id=100, enforcement_role="finance", tool_name="invoice_reset",
            args_hash=canonical_args_hash({"x": 1}), engagement_id="eng-123",
        )
        key, handle = _create(
            coord, channel, eng_key, canonical_json=canonical,
            engagement_id="eng-123", grants=spy,
        )
        assert await handle.settled_post() == "posted"
        ch = coord._entries[key]
        _tap(broker, ch, 0)
        await _settle()

        # resumed the engagement, NEVER the bus-role continuation
        assert channel.dispatches == []
        assert len(channel.eng_dispatches) == 1
        assert channel.eng_dispatches[0]["engagement_id"] == "eng-123"
        assert canonical in channel.eng_dispatches[0]["text"]
        assert "[authorization approved]" in channel.eng_dispatches[0]["text"]
        # minted the exact engagement-bound key
        assert log[0][0] == "mint" and log[0][1] == eng_key
        assert log[0][1].engagement_id == "eng-123"

    async def test_engagement_deny_resumes_engagement_no_mint(self, monkeypatch):
        """#400: denying an engagement-origin challenge resumes the engagement
        with the denial text and mints NOTHING."""
        log: list = []
        spy = _SpyGrants(log)
        broker, coord, channel = _fresh_env(monkeypatch, log=log)
        eng_key = _key(
            chat_id=100, enforcement_role="finance", tool_name="invoice_reset",
            args_hash=canonical_args_hash({"x": 1}), engagement_id="eng-9",
        )
        key, handle = _create(
            coord, channel, eng_key, engagement_id="eng-9", grants=spy,
        )
        await handle.settled_post()
        ch = coord._entries[key]
        _tap(broker, ch, 1)
        await _settle()

        assert "mint" not in [e[0] for e in log]
        assert channel.dispatches == []
        assert len(channel.eng_dispatches) == 1
        assert channel.eng_dispatches[0]["engagement_id"] == "eng-9"
        assert "[authorization denied]" in channel.eng_dispatches[0]["text"]

    async def test_deny_no_mint_edit_then_dispatch(self, monkeypatch):
        log: list = []
        spy = _SpyGrants(log)
        broker, coord, channel = _fresh_env(monkeypatch, log=log)
        key, handle = _create(coord, channel, grants=spy)
        await handle.settled_post()
        ch = coord._entries[key]
        _tap(broker, ch, 1)
        await _settle()

        assert [e[0] for e in log] == ["edit", "dispatch"]   # NO mint
        assert "denied" in channel.edits[-1][2].lower() or \
            "denied" in channel.edits[0][2].lower()
        assert "[authorization denied]" in channel.dispatches[0]["text"]

    async def test_minted_absent_internal_error_no_dispatch(self, monkeypatch):
        """Approve committed but the sync step never recorded the mint (raised
        and was swallowed) → edit the internal-error text, NEVER dispatch."""
        broker, coord, channel = _fresh_env(monkeypatch)
        key, handle = _create(coord, channel)
        await handle.settled_post()
        ch = coord._entries[key]
        _tap(broker, ch, 0, run_sync_step=False)   # commit, but no mint recorded
        await _settle()

        assert channel.dispatches == []
        assert "internal error" in channel.edits[-1][2].lower()
        assert "call the tool again" in channel.edits[-1][2].lower()

    async def test_approve_dispatch_exhaustion_overwrites_failure(
        self, monkeypatch,
    ):
        broker, coord, channel = _fresh_env(monkeypatch)
        channel.dispatch_result = False
        key, handle = _create(coord, channel)
        await handle.settled_post()
        ch = coord._entries[key]
        _tap(broker, ch, 0)
        await _settle()

        assert channel.dispatches, "dispatch was attempted"
        # last edit is the visible-failure overwrite
        last = channel.edits[-1][2].lower()
        assert "delivery to finance-full failed" in last
        assert "retry" in last

    async def test_deny_dispatch_exhaustion_overwrites_failure(
        self, monkeypatch,
    ):
        broker, coord, channel = _fresh_env(monkeypatch)
        channel.dispatch_result = False
        key, handle = _create(coord, channel)
        await handle.settled_post()
        ch = coord._entries[key]
        _tap(broker, ch, 1)
        await _settle()

        last = channel.edits[-1][2].lower()
        assert "failed" in last

    async def test_no_answer_edits_expired(self, monkeypatch):
        broker, coord, channel = _fresh_env(monkeypatch)
        key, handle = _create(coord, channel)
        await handle.settled_post()
        ch = coord._entries[key]
        broker.cancel(namespace="resident_ask", scope=ch.scope,
                      request_id=ch.rid, reason="timeout")
        await _settle()
        assert any("expired" in e[2].lower() for e in channel.edits)
        assert channel.dispatches == []

    # -- W1: exact humanized settlement copy --------------------------------

    async def test_approved_edit_uses_humanized_settlement_copy(
        self, monkeypatch,
    ):
        broker, coord, channel = _fresh_env(monkeypatch)
        key, handle = _create(coord, channel, tool_name="invoice_reset",
                               enforcement_role="finance")
        await handle.settled_post()
        ch = coord._entries[key]
        _tap(broker, ch, 0)
        await _settle()
        # No display name threaded ⇒ render-time guard substitutes the role.
        assert channel.edits[-1][2] == (
            "✅ Approved — finance (finance) may run invoice_reset once with "
            "exactly these arguments"
        )

    async def test_approved_edit_names_display_name_when_threaded(
        self, monkeypatch,
    ):
        broker, coord, channel = _fresh_env(monkeypatch)
        key, handle = _create(coord, channel, tool_name="invoice_reset",
                               enforcement_role="finance", display_name="Alex")
        await handle.settled_post()
        ch = coord._entries[key]
        _tap(broker, ch, 0)
        await _settle()
        assert channel.edits[-1][2] == (
            "✅ Approved — Alex (finance) may run invoice_reset once with "
            "exactly these arguments"
        )

    async def test_denied_edit_uses_humanized_settlement_copy(
        self, monkeypatch,
    ):
        broker, coord, channel = _fresh_env(monkeypatch)
        key, handle = _create(coord, channel, tool_name="invoice_reset")
        await handle.settled_post()
        ch = coord._entries[key]
        _tap(broker, ch, 1)
        await _settle()
        assert channel.edits[-1][2] == "❌ Denied — invoice_reset will not run"

    async def test_no_answer_edit_uses_humanized_expired_copy(
        self, monkeypatch,
    ):
        broker, coord, channel = _fresh_env(monkeypatch)
        key, handle = _create(coord, channel, tool_name="invoice_reset")
        await handle.settled_post()
        ch = coord._entries[key]
        broker.cancel(namespace="resident_ask", scope=ch.scope,
                      request_id=ch.rid, reason="timeout")
        await _settle()
        assert channel.edits[-1][2] == (
            "⌛ Expired — invoice_reset was not approved in time"
        )

    async def test_approved_dispatch_failure_overwrite_uses_humanized_copy(
        self, monkeypatch,
    ):
        broker, coord, channel = _fresh_env(monkeypatch)
        channel.dispatch_result = False
        key, handle = _create(coord, channel, tool_name="invoice_reset")
        await handle.settled_post()
        ch = coord._entries[key]
        _tap(broker, ch, 0)
        await _settle()
        assert channel.edits[-1][2] == (
            "⚠️ Approved, but delivery to finance-full failed — "
            "say 'retry' in chat"
        )

    async def test_denied_dispatch_failure_overwrite_uses_humanized_copy(
        self, monkeypatch,
    ):
        broker, coord, channel = _fresh_env(monkeypatch)
        channel.dispatch_result = False
        key, handle = _create(coord, channel, tool_name="invoice_reset")
        await handle.settled_post()
        ch = coord._entries[key]
        _tap(broker, ch, 1)
        await _settle()
        assert channel.edits[-1][2] == (
            "⚠️ Denied, but delivery to finance-full failed — "
            "say 'retry' in chat"
        )

    async def test_delegated_delivery_failure_names_originating_resident(
        self, monkeypatch,
    ):
        """r3-2: the delivery-failure overwrite reports failure to
        ``target_role`` — the ORIGINATING resident (e.g. Ellen) — verbatim
        v0.77 bytes, and the EXECUTING specialist's display name ('Alex') does
        NOT appear in it."""
        broker, coord, channel = _fresh_env(monkeypatch)
        channel.dispatch_result = False
        key, handle = _create(
            coord, channel, tool_name="invoice_reset",
            enforcement_role="finance", target_role="assistant",
            display_name="Alex",
        )
        await handle.settled_post()
        ch = coord._entries[key]
        _tap(broker, ch, 0)
        await _settle()
        assert channel.edits[-1][2] == (
            "⚠️ Approved, but delivery to assistant failed — say 'retry' in chat"
        )
        assert "Alex" not in channel.edits[-1][2]


class TestTwoLatchCleanup:
    async def test_answered_removes_entry(self, monkeypatch):
        broker, coord, channel = _fresh_env(monkeypatch)
        key, handle = _create(coord, channel)
        await handle.settled_post()
        ch = coord._entries[key]
        _tap(broker, ch, 0)
        await _settle()
        assert key not in coord._entries

    async def test_cancel_then_post_failure_removes_entry(self, monkeypatch):
        """Future latch (cancel) + setup latch (post failure) both land →
        entry removed even though the post failed."""
        broker, coord, channel = _fresh_env(monkeypatch)
        channel.post_gate = asyncio.Event()
        channel.post_result = None      # post will report delivery failure
        key, handle = _create(coord, channel)
        task = asyncio.ensure_future(handle.settled_post())
        await asyncio.sleep(0)
        broker.cancel_scope(namespace="resident_ask", scope="authz:100",
                            reason="new")
        channel.post_gate.set()
        await task
        await _settle()
        assert key not in coord._entries

    async def test_edit_raise_does_not_block_removal(self, monkeypatch):
        """The keyboard edit is owned by a SEPARATE broker hook task; a raise
        there must not stop the coordinator's two-latch removal."""
        broker, coord, channel = _fresh_env(monkeypatch)
        channel.edit_raises = True
        key, handle = _create(coord, channel)
        await handle.settled_post()
        ch = coord._entries[key]
        broker.cancel(namespace="resident_ask", scope=ch.scope,
                      request_id=ch.rid, reason="x")
        await _settle()
        assert key not in coord._entries

    async def test_stale_late_cleanup_cannot_remove_newer(self, monkeypatch):
        """Identity-guarded removal: an old challenge object's late latch
        landing must NOT evict a newer challenge registered under the same
        key."""
        broker, coord, channel = _fresh_env(monkeypatch)
        key, handle = _create(coord, channel)
        await handle.settled_post()
        old = coord._entries[key]
        # A newer challenge takes the slot (simulating a completed retry).
        newer = coord._entries[key].__class__(
            key=key, scope=old.scope, rid="newer-rid", req=old.req,
            broker=broker,
        )
        coord._entries[key] = newer
        # The OLD challenge's latches land late.
        coord._settle_request(old)
        coord._settle_setup(old)
        assert coord._entries.get(key) is newer   # newer survived

    async def test_retry_after_deny_is_fresh_challenge(self, monkeypatch):
        broker, coord, channel = _fresh_env(monkeypatch)
        key, handle = _create(coord, channel)
        await handle.settled_post()
        first_rid = coord._entries[key].rid
        _tap(broker, coord._entries[key], 1)  # deny
        await _settle()
        assert key not in coord._entries
        # retry -> brand new challenge with a distinct rid
        key2, handle2 = _create(coord, channel, key)
        assert handle2.created is True
        assert coord._entries[key].rid != first_rid
        await handle2.settled_post()

    async def test_retry_after_post_failure_is_fresh_challenge(
        self, monkeypatch,
    ):
        broker, coord, channel = _fresh_env(monkeypatch)
        channel.post_result = None
        key, handle = _create(coord, channel)
        assert await handle.settled_post() == "delivery_failed"
        await _settle()
        assert key not in coord._entries
        channel.post_result = 99
        key2, handle2 = _create(coord, channel, key)
        assert handle2.created is True
        assert await handle2.settled_post() == "posted"


class TestCancelMatching:
    async def test_cancel_by_role(self, monkeypatch):
        broker, coord, channel = _fresh_env(monkeypatch)
        k1, h1 = _create(coord, channel, enforcement_role="finance",
                         tool_name="a")
        k2, h2 = _create(coord, channel, enforcement_role="ops", tool_name="b",
                         chat_id=100)
        await h1.settled_post()
        await h2.settled_post()
        n = coord.cancel_matching(role="finance")
        await _settle()
        assert n == 1
        assert any("expired" in e[2].lower() for e in channel.edits)
        assert k1 not in coord._entries
        assert k2 in coord._entries

    async def test_cancel_by_artifact(self, monkeypatch):
        broker, coord, channel = _fresh_env(monkeypatch)
        k1 = _key(artifact_id="art-1", tool_name="a")
        k2 = _key(artifact_id="art-2", tool_name="b")
        _c, h1 = _create(coord, channel, k1)
        _c, h2 = _create(coord, channel, k2)
        await h1.settled_post()
        await h2.settled_post()
        assert coord.cancel_matching(artifact="art-1") == 1
        await _settle()
        assert k1 not in coord._entries
        assert k2 in coord._entries

    async def test_cancel_by_chat(self, monkeypatch):
        broker, coord, channel = _fresh_env(monkeypatch)
        k1, h1 = _create(coord, channel, chat_id=100, tool_name="a")
        k2, h2 = _create(coord, channel, chat_id=200, tool_name="b")
        await h1.settled_post()
        await h2.settled_post()
        assert coord.cancel_matching(chat=100) == 1
        await _settle()
        assert k1 not in coord._entries
        assert k2 in coord._entries


async def test_drain_awaits_outstanding_drivers(monkeypatch):
    broker, coord, channel = _fresh_env(monkeypatch)
    channel.post_gate = asyncio.Event()
    key, handle = _create(coord, channel)
    task = asyncio.ensure_future(handle.settled_post())
    await asyncio.sleep(0)                 # driver posting, blocked
    channel.post_gate.set()
    await coord.drain()
    assert all(d.done() for d in coord._drivers)
    await task


def test_challenges_singleton_is_a_coordinator():
    from authz_grants import CHALLENGES
    assert isinstance(CHALLENGES, ChallengeCoordinator)


# ---------------------------------------------------------------------------
# #328 — challenge rendering: bidi-control spoofing + UTF-16 size gate
# ---------------------------------------------------------------------------


class TestChallengeBidiEscape:
    """#328: a bidi control character inside a tool-input VALUE renders
    verbatim in the challenge, so the human-visible "exact action" can differ
    materially from the string bound into the grant (RLO makes
    ``safe\\u202egpj.exe`` display as a benign ``...jpg`` name). The renderer
    must show unsafe codepoints as visible ``\\uXXXX`` escapes; the BINDING
    (canonical json + args_hash) stays byte-identical."""

    def test_bidi_override_in_value_is_escaped_in_display(self):
        canonical = canonical_args_json({"target": "safe" + chr(0x202E) + "gpj.exe"})
        assert chr(0x202E) in canonical  # the binding keeps the real value
        text = render_challenge_message(
            tool_name="invoice_reset", enforcement_role="finance",
            canonical_json=canonical,
        )
        assert chr(0x202E) not in text       # never rendered raw
        assert "\\u202e" in text             # visible escape instead

    @pytest.mark.parametrize("codepoint", [0x202A, 0x200E, 0x2066, 0x061C, 0x007F])
    def test_all_unsafe_ranges_escaped(self, codepoint):
        canonical = canonical_args_json({"v": f"a{chr(codepoint)}b"})
        text = render_challenge_message(
            tool_name="t", enforcement_role="r", canonical_json=canonical,
        )
        assert chr(codepoint) not in text
        assert "\\u%04x" % codepoint in text

    def test_escaped_display_block_parses_to_the_same_args(self):
        """The escape is a JSON string escape — the displayed block is still
        valid JSON for the SAME value, so what the operator reads IS what the
        grant binds (just spelled visibly)."""
        args = {"target": "safe" + chr(0x202E) + "gpj.exe"}
        canonical = canonical_args_json(args)
        text = render_challenge_message(
            tool_name="invoice_reset", enforcement_role="finance",
            canonical_json=canonical,
        )
        block = text.split("```\n", 1)[1].split("\n```", 1)[0]
        assert json.loads(block) == args

    def test_summarized_form_also_escapes_the_binding_block(self):
        canonical = canonical_args_json({"period": "x" + chr(0x202E) + "y"})
        text = render_challenge_message(
            tool_name="invoice_reset", enforcement_role="finance",
            canonical_json=canonical, summary="Delete the draft for {other}",
        )
        assert chr(0x202E) not in text

    def test_safe_canonical_json_is_untouched(self):
        canonical = '{"note":"a_*b*_ `c`"}'
        text = render_challenge_message(
            tool_name="invoice_reset", enforcement_role="finance",
            canonical_json=canonical,
        )
        assert canonical in text
        assert "\\" not in text


class TestChallengeSizeGateUtf16:
    async def test_astral_challenge_over_utf16_limit_is_refused(
        self, monkeypatch,
    ):
        """#328 (low): the gate must measure UTF-16 units as Telegram does —
        an astral-heavy challenge whose ``len()`` is under the ceiling but
        whose UTF-16 length is over it would pass the old gate and then fail
        ``post_dm_keyboard``, permanently denying that exact-argument action."""
        from text_util import utf16_len
        broker, coord, channel = _fresh_env(monkeypatch)
        # ~2000 astral chars: len() ~2100 (< 3900) but ~4200 UTF-16 units.
        blob = "\U0001F389" * 2000
        canonical = canonical_args_json({"blob": blob})
        rendered = render_challenge_message(
            tool_name="invoice_reset", enforcement_role="finance",
            canonical_json=canonical,
        )
        assert len(rendered) <= _CHALLENGE_MAX_CHARS  # old gate would pass
        assert utf16_len(rendered) > _CHALLENGE_MAX_CHARS
        key, handle = _create(coord, channel, canonical_json=canonical)
        await _settle()
        assert handle.refused == "args_too_large"
        assert key not in coord._entries
        assert channel.posts == []


class TestChallengeTTL:
    def test_challenge_ttl_matches_the_consent_ask_class(self):
        """#498: the tool-authorization ask lived 120 s while consent asks
        lived 600 s — two minutes assumed the operator was watching the chat
        the moment the keyboard landed, and a missed window forced a fresh
        chat turn to re-mint the ask. Both ask classes are the same human
        decision on a phone and neither holds a turn open (the challenge is
        detached), so they share one TTL. The grant minted on approval keeps
        its own, shorter single-use TTL.

        Red case demonstrated: restoring ``_CHALLENGE_TTL_S = 120.0`` fails
        this test."""
        import authz_grants
        import trigger_consent

        assert authz_grants._CHALLENGE_TTL_S == 600.0
        assert (authz_grants._CHALLENGE_TTL_S
                == trigger_consent.TRIGGER_CONSENT_TTL_S)
        # The approval grant stays short-lived and single-use — the raise
        # widens the DECISION window, not the blast radius of an approval.
        assert authz_grants.DEFAULT_GRANT_TTL_S == 300.0


class TestC1EngagementContinuationReservation:
    """C1 red cases for the #400 arm — #663's WIDEST window, and the one no
    prior survey saw.

    ``_make_finish_hook._finish`` AWAITS the approval ``edit_dm_message``
    before it dispatches the continuation, and the continuation's admission
    ticket is only born inside ``deliver_system_turn``. So the whole Telegram
    edit round-trip is a window in which a successful completion commits
    un-vetoed and the resume turn is dropped. The remedy is ingress, not
    reordering: the reservation is taken at the SYNCHRONOUS tap-commit, which
    leaves the observable edit-before-dispatch order (pinned above) untouched.
    """

    @pytest.mark.parametrize(
        ("idx", "expected_events"),
        [(0, ["mint", "reserve"]), (1, ["reserve"])],
        ids=["approve", "deny"],
    )
    async def test_the_tap_reserves_before_the_finish_hook_can_run(
        self, monkeypatch, idx, expected_events,
    ):
        """Both taps dispatch a continuation, so both must reserve — testing
        approval alone would let the reservation stay wrongly conditional on
        ``grants.mint``.

        Pre-fix: approve logs only ``["mint"]`` and deny logs ``[]``; nothing
        counts as inbound for the racing completion to be refused over.
        """
        log: list = []
        spy = _SpyGrants(log)
        broker, coord, channel = _fresh_env(monkeypatch, log=log)
        eng_key = _key(
            chat_id=100, enforcement_role="finance", tool_name="invoice_reset",
            args_hash=canonical_args_hash({"x": 1}), engagement_id="eng-c1",
        )
        key, handle = _create(
            coord, channel, eng_key, engagement_id="eng-c1", grants=spy)
        assert await handle.settled_post() == "posted"
        ch = coord._entries[key]

        _tap(broker, ch, idx)
        at_commit = [e[0] for e in log]
        await _settle()

        facts = (at_commit, sum(l.takes for l in channel.leases),
                 sum(l.releases for l in channel.leases))
        assert facts == (expected_events, 1, 1), f"authz tap facts: {facts!r}"

    async def test_a_pre_hand_off_raise_still_warns_on_the_dm(
        self, monkeypatch,
    ):
        """Seam-review finding (Sol, r3). ``deliver_system_turn`` re-raises a
        pre-hand-off exception (its visibility law settles the ticket, then
        re-raises so the caller learns there is no task owner). The two
        install arms contain that inside ``_reconcile_cb``; this arm does not,
        so the exception leaves ``_finish`` entirely and the operator is left
        with the ✅ approval edit for a continuation that was never spawned.

        Pre-fix: exactly one edit — the approval — and no warning.
        """
        log: list = []
        spy = _SpyGrants(log)
        broker, coord, channel = _fresh_env(monkeypatch, log=log)
        channel.eng_dispatch_raises = True
        eng_key = _key(
            chat_id=100, enforcement_role="finance", tool_name="invoice_reset",
            args_hash=canonical_args_hash({"x": 1}), engagement_id="eng-c1r",
        )
        key, handle = _create(
            coord, channel, eng_key, engagement_id="eng-c1r", grants=spy)
        assert await handle.settled_post() == "posted"
        ch = coord._entries[key]

        _tap(broker, ch, 0)
        await _settle()

        texts = [e[2] for e in channel.edits]
        facts = (len(texts),
                 "approved" in texts[0].lower() if texts else None,
                 "failed" in texts[-1].lower() if len(texts) > 1 else False,
                 sum(l.releases for l in channel.leases))
        assert facts == (2, True, True, 1), f"authz raise facts: {facts!r}"
