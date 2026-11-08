"""v0.85.0 (round 4, D3/D4) — the A6 enumerator strip and A7 embedded-options
regex gate are DELETED: an option's label/short and an anchor's question are
now stored/rendered VERBATIM, exactly as the agent supplied them. Doctrine
(not code) tells the agent enumerable answers belong in ``options``, never
pre-labelled prose.

Pure-function coverage of ``_validate_ask_args`` (verbatim pass-through +
structural checks) and ``render_ask_body`` (verbatim rendering; the
``_canonical_question`` cases live in ``test_option_labels.py``).
Handler-level anchor-acceptance tests (inline "(a) … (b) …" no longer
refused) live in ``test_ask_gates.py`` where a real driver/broker is needed.
"""

from __future__ import annotations

from channels.channel_handlers import _validate_ask_args
from channels.telegram import render_ask_body


def _body(options, *, question="Pick:", **over):
    base = {"question": question, "options": options, "timeout_s": 60}
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# D4 · _validate_ask_args — labels and shorts flow through VERBATIM
# ---------------------------------------------------------------------------


class TestValidateAskArgsVerbatim:
    def test_str_options_kept_verbatim(self):
        out = _validate_ask_args(
            _body(["A — Python MCP + MCPB", "B — Rust bridge"]))
        assert out is not None
        _q, labels, _t, shorts = out
        assert labels == ["A — Python MCP + MCPB", "B — Rust bridge"]
        assert shorts == [None, None]

    def test_no_double_labelling_regression_is_fine(self):
        # D4: the redundant "A —" prefix now SURVIVES into the render — it is
        # the agent's verbatim text, not a bug (downstream indexes by
        # position, never parses the label).
        out = _validate_ask_args(
            _body(["A — Python MCP + MCPB", "B — Rust bridge"]))
        labels = out[1]
        rendered = render_ask_body(1, "Which stack?", labels)
        assert "1. A — Python MCP + MCPB" in rendered
        assert "2. B — Rust bridge" in rendered

    def test_option_a_dash_label_stored_and_rendered_verbatim(self):
        # Task B1 brief: "Option A — Python MCP server" is stored/rendered
        # VERBATIM (no strip), including its short if any.
        out = _validate_ask_args(_body([
            {"label": "Option A — Python MCP server", "short": "A. py"},
            "Option B — Rust bridge",
        ]))
        assert out is not None
        _q, labels, _t, shorts = out
        assert labels == ["Option A — Python MCP server", "Option B — Rust bridge"]
        assert shorts == ["A. py", None]
        rendered = render_ask_body(1, "Which stack?", labels)
        assert "1. Option A — Python MCP server" in rendered
        assert "2. Option B — Rust bridge" in rendered

    def test_mixed_enumerator_forms_kept_verbatim(self):
        out = _validate_ask_args(
            _body(["1. one", "b) beta", "12) twelve", "2 · dot"]))
        assert out[1] == ["1. one", "b) beta", "12) twelve", "2 · dot"]

    def test_a_la_carte_survives_validation(self):
        out = _validate_ask_args(_body(["A la carte", "Be right back"]))
        assert out is not None
        assert out[1] == ["A la carte", "Be right back"]

    def test_raw_unique_labels_that_would_have_collided_after_strip_now_accepted(self):
        # v0.83.0 refused this (post-strip collision); D4 removes the strip
        # entirely, so raw-unique labels are simply accepted verbatim.
        out = _validate_ask_args(_body(["A — Same", "1. Same"]))
        assert out is not None
        assert out[1] == ["A — Same", "1. Same"]

    def test_marker_only_option_now_accepted_verbatim(self):
        # v0.83.0 refused "A — " as normalizing to empty; D4 removes the
        # strip, so it is a non-blank verbatim label like any other.
        out = _validate_ask_args(_body(["A — ", "keep"]))
        assert out is not None
        assert out[1] == ["A — ", "keep"]

    def test_whitespace_only_option_still_refused(self):
        # Structural non-blank check is unaffected by D4 (no stripping
        # involved — the raw label is blank after ``.strip()``).
        assert _validate_ask_args(_body(["   ", "keep"])) is None

    def test_dict_labels_and_shorts_kept_verbatim(self):
        out = _validate_ask_args(_body([
            {"label": "A — Python MCP", "short": "1. py"},
            {"label": "B — Rust", "short": "2. rs"},
        ]))
        assert out is not None
        _q, labels, _t, shorts = out
        assert labels == ["A — Python MCP", "B — Rust"]
        assert shorts == ["1. py", "2. rs"]

    def test_dict_label_raw_unique_after_would_be_strip_now_accepted(self):
        out = _validate_ask_args(_body([
            {"label": "A — Same", "short": "x"},
            {"label": "1. Same", "short": "y"},
        ]))
        assert out is not None
        assert out[1] == ["A — Same", "1. Same"]

    def test_dict_short_collision_after_would_be_strip_kept_distinct(self):
        # D4: shorts are never normalized, so these stay distinct (advisory
        # data either way — never a rejection cause).
        out = _validate_ask_args(_body([
            {"label": "One", "short": "A — s"},
            {"label": "Two", "short": "1. s"},
        ]))
        assert out is not None
        _q, labels, _t, shorts = out
        assert labels == ["One", "Two"]
        assert shorts == ["A — s", "1. s"]

    def test_dict_marker_only_short_kept_verbatim(self):
        out = _validate_ask_args(_body([
            {"label": "One", "short": "A — "},
            {"label": "Two", "short": "ok"},
        ]))
        assert out is not None
        _q, labels, _t, shorts = out
        assert labels == ["One", "Two"]
        assert shorts == ["A — ", "ok"]

    def test_multi_dict_composition_end_to_end_verbatim(self):
        out = _validate_ask_args(_body(
            [
                {"label": "A — Python MCP", "short": "1. py"},
                {"label": "B — Rust bridge", "short": "2. rs"},
            ],
            question="Which apply?",
            multi=True,
        ))
        assert out is not None
        _q, labels, _t, shorts = out
        assert labels == ["A — Python MCP", "B — Rust bridge"]
        assert shorts == ["1. py", "2. rs"]
        rendered = render_ask_body(3, "Which apply?", labels)
        assert "1. A — Python MCP" in rendered and "2. B — Rust bridge" in rendered


# ---------------------------------------------------------------------------
# D1 (round 4, spec §D1 bullets 1-2): the invented LENGTH caps are removed
# ---------------------------------------------------------------------------


class TestCapsRemovedD1:
    """Structural checks (type, non-blank, anchor/button shape, uniqueness,
    option COUNT) stay; ``short`` becomes purely advisory (never a rejection
    cause)."""

    def test_139_char_option_label_accepted(self):
        # The live Q2 failure form: a long, readable label used to be
        # refused at the old 48-char cap.
        long_label = "y" * 139
        out = _validate_ask_args(_body([long_label, "B"]))
        assert out is not None
        assert out[1] == [long_label, "B"]

    def test_2000_char_question_accepted(self):
        out = _validate_ask_args(_body(["A", "B"], question="q" * 2000))
        assert out is not None
        assert out[0] == "q" * 2000

    def test_blank_short_does_not_reject(self):
        out = _validate_ask_args(_body(
            [{"label": "One", "short": "   "}, {"label": "Two", "short": "ok"}]))
        assert out is not None
        _q, labels, _t, shorts = out
        assert labels == ["One", "Two"]
        assert shorts == ["   ", "ok"]

    def test_duplicate_shorts_do_not_reject(self):
        out = _validate_ask_args(_body([
            {"label": "One", "short": "dup"},
            {"label": "Two", "short": "dup"},
        ]))
        assert out is not None
        assert out[3] == ["dup", "dup"]

    def test_over_budget_short_does_not_reject(self):
        out = _validate_ask_args(_body(
            [{"label": "One", "short": "z" * 100}, "B"]))
        assert out is not None
        assert out[3] == ["z" * 100, None]

    def test_non_string_short_accepted_as_absent(self):
        out = _validate_ask_args(_body(
            [{"label": "One", "short": 7}, "B"]))
        assert out is not None
        assert out[3] == [None, None]

    def test_missing_short_accepted_as_absent(self):
        out = _validate_ask_args(_body([{"label": "One"}, "B"]))
        assert out is not None
        assert out[3] == [None, None]

    def test_duplicate_full_labels_still_rejected(self):
        # Full-label uniqueness is unaffected by D1/D4 — only ``short`` floors.
        assert _validate_ask_args(_body(["Same", "Same"])) is None

    def test_9_options_still_rejected_count_cap(self):
        # Option COUNT (documented product-contract exception) is unaffected.
        assert _validate_ask_args(
            _body([f"o{i}" for i in range(9)])) is None

    def test_blank_full_label_still_rejected(self):
        assert _validate_ask_args(_body(["", "B"])) is None

    def test_blank_full_label_dict_still_rejected(self):
        assert _validate_ask_args(
            _body([{"label": "   ", "short": "ok"}, "B"])) is None
