"""Unit tests for drivers.brief — the structured engagement-brief envelope."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit]

from drivers.brief import (  # noqa: E402
    COMPLETION_ACCOUNTING_LINE,
    normalize_brief,
    render_brief_task,
    validate_brief,
)


class TestValidateBrief:
    def test_objective_only_is_valid(self):
        assert validate_brief({"objective": "do a thing"}) is None

    def test_missing_objective_invalid(self):
        assert validate_brief({}) is not None
        assert validate_brief({"acceptance_criteria": ["x"]}) is not None

    def test_empty_objective_invalid(self):
        assert validate_brief({"objective": ""}) is not None

    def test_objective_wrong_type_invalid(self):
        assert validate_brief({"objective": 5}) is not None

    def test_not_a_dict_invalid(self):
        assert validate_brief("nope") is not None

    def test_acceptance_criteria_empty_entry_invalid(self):
        assert validate_brief(
            {"objective": "o", "acceptance_criteria": ["", "x"]}
        ) is not None

    def test_process_requirements_str_not_list_invalid(self):
        assert validate_brief(
            {"objective": "o", "process_requirements": "x"}
        ) is not None

    def test_context_wrong_type_invalid(self):
        assert validate_brief({"objective": "o", "context": 5}) is not None

    def test_interaction_required_truthy_non_bool_invalid(self):
        assert validate_brief(
            {"objective": "o", "interaction_required": "yes"}
        ) is not None
        # int is NOT accepted even though bool subclasses int.
        assert validate_brief(
            {"objective": "o", "interaction_required": 1}
        ) is not None

    def test_empty_lists_valid(self):
        assert validate_brief({
            "objective": "o",
            "acceptance_criteria": [],
            "process_requirements": [],
        }) is None

    def test_full_valid_brief(self):
        assert validate_brief({
            "objective": "o",
            "acceptance_criteria": ["a", "b"],
            "process_requirements": ["p"],
            "context": "c",
            "interaction_required": True,
        }) is None


class TestNormalizeBrief:
    def test_defaults_for_omitted(self):
        n = normalize_brief({"objective": "o"})
        assert n == {
            "objective": "o",
            "acceptance_criteria": [],
            "process_requirements": [],
            "context": "",
            "interaction_required": False,
        }

    def test_preserves_present_fields(self):
        n = normalize_brief({
            "objective": "o", "acceptance_criteria": ["a"],
            "process_requirements": ["p"], "context": "c",
            "interaction_required": True,
        })
        assert n["acceptance_criteria"] == ["a"]
        assert n["interaction_required"] is True


class TestRenderBriefTask:
    def _n(self, **kw):
        base = {"objective": "Do X"}
        base.update(kw)
        return normalize_brief(base)

    def test_objective_only_clean(self):
        out = render_brief_task(self._n())
        assert "## Objective\nDo X" in out
        assert "## Acceptance criteria" not in out
        assert "## Process requirements" not in out
        assert "## Context" not in out
        assert COMPLETION_ACCOUNTING_LINE in out

    def test_verbatim_process_strings_present(self):
        out = render_brief_task(
            self._n(process_requirements=["Run `make test`", "NEVER force-push"]),
        )
        assert "Run `make test`" in out
        assert "NEVER force-push" in out
        assert "VERBATIM" in out

    def test_completion_line_unconditional_both_interaction_values(self):
        # interaction_required is accepted and ignored: the render is the same
        # either way, and the completion line is always there.
        outs = [render_brief_task(self._n(interaction_required=ir))
                for ir in (True, False)]
        assert outs[0] == outs[1]
        assert COMPLETION_ACCOUNTING_LINE in outs[0]


class TestRenderRawBrief:
    def test_brief_renders_envelope(self):
        # A raw brief as engage_executor receives it: normalize, then render.
        raw = {"objective": "O", "process_requirements": ["P1"]}
        out = render_brief_task(normalize_brief(raw))
        assert "## Objective\nO" in out
        assert "P1" in out
        assert COMPLETION_ACCOUNTING_LINE in out
