"""The retired memory-provider module stays retired.

The name ``memory`` once held the provider abstraction and its Honcho,
cached and SQLite providers, deleted when tiered memory access replaced them.
The name now belongs to the memory store's package; what must not come back
is the provider surface.
"""
import importlib

import pytest

pytestmark = [pytest.mark.unit]

_RETIRED = ("MemoryProvider", "NoOpMemory", "HonchoMemoryProvider",
            "CachedMemoryProvider", "SqliteMemoryProvider")


def test_legacy_memory_provider_module_stays_gone():
    memory = importlib.import_module("memory")
    assert hasattr(memory, "__path__"), "memory must be the store's package"
    assert [n for n in _RETIRED if hasattr(memory, n)] == []
