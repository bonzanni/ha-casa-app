"""#925 red case: the s6-overlay sources path handed to ``s6-rc-compile``.

``casa/Dockerfile`` builds the release image from the floating
``base-debian:bookworm`` tag, so the s6-overlay version inside the image is
whatever the base ships at build time. A versioned literal in
``s6_rc.S6_OVERLAY_SOURCES`` is therefore a copy of one observed base, and the
first base bump strands every ``claude_code`` launch compile on a directory
that no longer exists (``s6-rc-compile: fatal: unable to opendir``, exit 111).

The base image ships a version-less ``/package/admin/s6-overlay`` symlink on
every base Casa has shipped on, so the compile is invoked through it. These
tests stub ``subprocess.run`` exactly as ``tests/test_s6_rc.py`` does: nothing
here shells out, opens a socket, or touches ``/package/admin``.
"""

import threading

import pytest

pytestmark = pytest.mark.unit


def _compile_overlay_argument(monkeypatch):
    from drivers import s6_rc

    calls = []

    def fake_run(argv, check=True, **kwargs):
        calls.append(list(argv))

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(s6_rc.subprocess, "run", fake_run)
    monkeypatch.setattr(
        s6_rc.os.path, "realpath", lambda path: "/fake/boot-db"
    )
    monkeypatch.setattr(
        s6_rc.shutil, "rmtree", lambda *args, **kwargs: None
    )

    s6_rc._compile_swap_reap_sync("/fake/new-db", threading.Event())

    compiles = [cmd for cmd in calls if cmd[0] == "s6-rc-compile"]
    assert len(compiles) == 1
    return compiles[0][2]


def test_compile_uses_versionless_overlay_sources(monkeypatch):
    """The red case: argv[2] of the compile is the version-less sources path."""
    actual = _compile_overlay_argument(monkeypatch)
    assert actual == "/package/admin/s6-overlay/etc/s6-rc/sources", actual


def test_compile_reads_overlay_sources_at_call_time(monkeypatch):
    """Regression (green at the base): the compile reads the module attribute
    at call time, so a monkeypatched value reaches argv[2] — neither an inline
    literal nor an import-time cache would."""
    from drivers import s6_rc

    for path in ("/fake/overlay-first", "/fake/overlay-second"):
        monkeypatch.setattr(s6_rc, "S6_OVERLAY_SOURCES", path)
        assert _compile_overlay_argument(monkeypatch) == path
