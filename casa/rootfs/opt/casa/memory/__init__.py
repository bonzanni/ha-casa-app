"""Casa's memory: the store, its registry, and (as they land) capture, the
writer, serving and maintenance.

The package imports nothing from the running Casa process (``agent``,
``tools``, ``casa_core`` and what they pull in) — only ``config``,
``timekeeping`` and a model port — so the container-start maintenance step
can import it while no Casa process runs. ``tests/test_memory_store.py``
pins that.
"""
