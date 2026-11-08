# tests/test_internal_handlers.py
"""The INV-TOOL-001 pin on ``tools._result``.

The internal tool-call and hook-resolve handlers this file used to exercise
are gone; ``internal_handlers`` now holds only the peer-credential middleware
and the ``/admin/*`` route builders.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_pin_inv_tool_001_result_marks_errors_only_for_error_status():
    """Pins INV-TOOL-001: _result infers the outer MCP error only from
    status=="error" or ok is False; other statuses ride as successes.

    Red case demonstrated: widening the inference to treat "unavailable" as
    an error fails this test.
    """
    from tools import _result

    assert _result({"status": "error"})["is_error"] is True
    assert _result({"ok": False})["is_error"] is True
    for payload in ({"status": "unavailable"}, {"status": "pending"},
                    {"status": "acknowledged"}, {"ok": True}):
        assert _result(payload).get("is_error") is not True

