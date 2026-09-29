"""Source of a module-level function as written, for structural pins (#1092).

A tool registered with ``@tool`` is an SdkMcpTool whose ``.handler`` is
replaced at import by wrappers that copy only ``__name__`` — ``fenced`` for
every fenced tool, and ``accounted`` outside it for the sends (``tools.py``,
``_fence_wrapped`` and ``_account_send_attempts``). ``inspect.getsource`` on
the handler therefore reads a wrapper, and a pin written that way stays green
whatever the tool's own body contains. The wrapper depth differs by tool and
has changed before, so nothing here unwraps: the definition is located by name
in the module's own source.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import ModuleType


def module_function_source(module: ModuleType, name: str) -> str:
    """Return the source of the ONE top-level ``def``/``async def`` named
    ``name`` in ``module``'s file, from its ``def`` line to the end of its
    body (decorators excluded). A nested function of the same name is not a
    candidate; zero or several top-level definitions fail the calling test."""
    source = Path(module.__file__).read_text(encoding="utf-8")
    defs = [
        node for node in ast.parse(source).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]
    assert len(defs) == 1, (
        f"expected exactly one top-level def {name!r} in {module.__name__}, "
        f"found {len(defs)}")
    segment = ast.get_source_segment(source, defs[0])
    assert segment, f"no source segment for {module.__name__}.{name}"
    return segment
