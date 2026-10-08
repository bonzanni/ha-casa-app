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


def test_release_build_compile_smoke_reads_the_driver_sources():
    """#957/#1357 regression: `casa/Dockerfile`'s compile smoke hands
    `s6-rc-compile` the same three image source directories the driver compiles
    from (overlay, Casa's services, Casa's user bundles), and runs after
    `COPY rootfs /`, because the merged `/etc/s6-overlay` only exists from
    there. A path moved in the driver and not in the Dockerfile would leave the
    smoke compiling something the launch never reads. This pins the TEXT; the
    smoke's execution is covered by `tests/test_baseline_runtime_assert.py` on
    the runner's architecture."""
    import re
    from pathlib import Path

    from drivers import s6_rc

    dockerfile = (
        Path(__file__).resolve().parents[1] / "casa" / "Dockerfile"
    ).read_text(encoding="utf-8")
    smokes = list(re.finditer(
        r's6-rc-compile "\$\{scratch\}/db" (\S+) (\S+) (\S+) \\', dockerfile))
    assert len(smokes) == 1, [m.group(0) for m in smokes]
    assert smokes[0].groups() == (
        s6_rc.S6_OVERLAY_SOURCES, s6_rc.CASA_SOURCES, s6_rc.CASA_BUNDLES)
    assert dockerfile.count("\nCOPY rootfs /\n") == 1
    assert dockerfile.index("\nCOPY rootfs /\n") < smokes[0].start()


# --------------------------------------------------------------------------
# #1357: s6-overlay 3.2.3.2 reads the `user` bundle from
# /etc/s6-overlay/user-bundles.d. A `user` directory left in s6-rc.d makes
# stage2 take its deprecated branch, which ignores user-bundles.d, and makes
# the driver's compile fail (a duplicate `user`, or a `user` with no type).
# --------------------------------------------------------------------------

def _overlay_root():
    from pathlib import Path

    return (Path(__file__).resolve().parents[1]
            / "casa" / "rootfs" / "etc" / "s6-overlay")


def test_rootfs_ships_no_user_bundle_in_the_service_sources():
    services = _overlay_root() / "s6-rc.d"
    assert not (services / "user").exists()
    assert not (services / "user2").exists()


def test_every_service_is_a_member_of_the_user_bundle():
    """Every Casa service definition is enrolled in the `user` bundle, and the
    bundle names nothing that is not a Casa service — counted, not truthy."""
    root = _overlay_root()
    services = sorted(p.name for p in (root / "s6-rc.d").iterdir() if p.is_dir())
    members = sorted(
        p.name for p in (root / "user-bundles.d" / "user" / "contents.d").iterdir())
    assert len(services) == 8, services
    assert members == services


def test_rootfs_ships_no_bundle_type_of_its_own():
    """The base ships `user-bundles.d/user/type`; Casa only adds members, as the
    s6-overlay README documents. A Casa copy would mask a base that moved it."""
    bundle = _overlay_root() / "user-bundles.d" / "user"
    assert sorted(p.name for p in bundle.iterdir()) == ["contents.d"]


def test_driver_bundle_sources_constant():
    from drivers import s6_rc

    assert s6_rc.CASA_SOURCES == "/etc/s6-overlay/s6-rc.d"
    assert s6_rc.CASA_BUNDLES == "/etc/s6-overlay/user-bundles.d"
