"""Tests for the session_registry module source."""

import pytest

pytestmark = [pytest.mark.unit]


def test_session_registry_no_honcho_ids_dependency():
    import inspect
    import session_registry
    src = inspect.getsource(session_registry)
    assert "honcho_ids" not in src and "honcho_session_id" not in src
