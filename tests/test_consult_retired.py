"""consult_other_agent_memory is retired (plan 3, Task 7)."""
import pytest

import tools

pytestmark = [pytest.mark.unit]


def test_consult_tool_is_gone():
    assert not hasattr(tools, "consult_other_agent_memory")


def test_consult_not_in_casa_tools():
    names = {getattr(t, "name", getattr(t, "__name__", "")) for t in tools.CASA_TOOLS}
    assert "consult_other_agent_memory" not in names
