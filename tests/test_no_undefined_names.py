"""No module may reference an undefined name.

v0.125.1: v0.125.0 removed the `telegram_delivery_mode` option and deleted the
`telegram_delivery` local along with the constructor kwarg that used it — but a
third reference survived in the "Telegram channel registered" log line. That is
inside `casa_core.main()`'s `if telegram_token:` branch, which no unit test
enters (it needs a bot token and a live channel), so 6237 green tests said
nothing and the add-on crash-looped on the production host with
`NameError: name 'telegram_delivery' is not defined`.

A NameError on a boot-only path is invisible to both the type checker we do not
run and the tests we do. Static undefined-name analysis is the cheap guard, and
it generalises: every future option removal deletes locals across long
functions, which is exactly how this class of bug is born.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pyflakes_api = pytest.importorskip("pyflakes.api")
pyflakes_messages = pytest.importorskip("pyflakes.messages")
pyflakes_reporter = pytest.importorskip("pyflakes.reporter")

CODE_ROOT = (
    Path(__file__).resolve().parent.parent
    / "casa" / "rootfs" / "opt" / "casa"
)

# Undefined-name families. `UndefinedLocal` is the read-before-assignment
# variant and is just as fatal at runtime.
_FATAL = (
    pyflakes_messages.UndefinedName,
    pyflakes_messages.UndefinedLocal,
    pyflakes_messages.UndefinedExport,
)


# Pre-existing, verified benign: every one is a name used ONLY inside a quoted
# annotation in a module with `from __future__ import annotations`, so it is
# never evaluated at runtime (e.g. `logger: "logging.Logger"` in
# casa_core_middleware.py, which never imports `logging`). They are baselined
# rather than fixed so this guard can land with the hotfix it belongs to; the
# assertion below still fails on any NEW undefined name, which is the point.
# Shrinking this list is welcome. Growing it needs the same proof of benignity.
_ANNOTATION_ONLY_BASELINE = frozenset({
    ("agent_loader.py", "ExecutorDefinition"),
    ("agent_loader.py", "PersonaPack"),
    ("casa_core_middleware.py", "logging"),
    ("config.py", "BindingRecord"),
    ("config.py", "CompiledPromptBundle"),
    ("config.py", "SpeakerProvenance"),
    ("hooks.py", "HooksConfig"),
    ("tools.py", "AgentConfig"),
    ("tools.py", "SpeakerProvenance"),
})


class _Collector(pyflakes_reporter.Reporter):
    """Keep only the fatal messages; ignore style and syntax noise."""

    def __init__(self) -> None:
        self.found: list[str] = []
        self.errors: list[str] = []

    def flake(self, message) -> None:  # noqa: ANN001 - pyflakes API
        if not isinstance(message, _FATAL):
            return
        rel = str(Path(message.filename).resolve().relative_to(CODE_ROOT))
        if (rel, message.message_args[0]) in _ANNOTATION_ONLY_BASELINE:
            return
        self.found.append(str(message))

    def unexpectedError(self, filename, msg) -> None:  # noqa: ANN001, N802
        self.errors.append(f"{filename}: {msg}")

    def syntaxError(  # noqa: N802
        self, filename, msg, lineno, offset, text,
    ) -> None:  # noqa: ANN001
        self.errors.append(f"{filename}:{lineno}: {msg}")


def test_no_module_references_an_undefined_name():
    """The whole application tree, not just the modules a test imports.

    Scoped to `casa/rootfs/opt/casa` and skips `defaults/` (agent
    doctrine and bundled plugin payloads, not Casa's own importable code).
    """
    targets = sorted(
        str(p) for p in CODE_ROOT.rglob("*.py")
        if "defaults" not in p.relative_to(CODE_ROOT).parts
    )
    assert targets, f"no application modules found under {CODE_ROOT}"

    reporter = _Collector()
    for target in targets:
        pyflakes_api.checkPath(target, reporter)

    assert not reporter.errors, (
        "pyflakes could not parse:\n  " + "\n  ".join(reporter.errors)
    )
    assert not reporter.found, (
        "undefined name(s) — these are runtime NameErrors on any path that "
        "executes:\n  " + "\n  ".join(reporter.found)
    )
