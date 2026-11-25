"""#1678: the tools that refuse a task over the delegation limit state that
limit in the task field the model reads before it writes the request."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize("name", ["delegate_to_agent", "start_job", "hand_off"])
def test_task_field_states_the_limit(name):
    import specialist_limits
    import tools

    props = getattr(tools, name).input_schema.get("properties", {})
    task_desc = props.get("task", {}).get("description", "")
    assert f"{specialist_limits._MAX_TASK_CHARS:,}" in task_desc
    context_desc = props.get("context", {}).get("description", "")
    assert f"{specialist_limits._MAX_CONTEXT_CHARS:,}" in context_desc


def test_start_job_keeps_every_field_required():
    import tools

    schema = tools.start_job.input_schema
    assert schema.get("type") == "object"
    assert schema.get("required") == ["job", "task", "context"]
    assert set(schema.get("properties", {})) == {"job", "task", "context"}
