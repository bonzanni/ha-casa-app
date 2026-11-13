"""Task 1 Step 6: the app manifest's shape, pinned exactly."""
from __future__ import annotations

import re

import yaml

from casa_browser import __version__

from conftest import APP_DIR, REPO_ROOT

CONFIG = yaml.safe_load((APP_DIR / "config.yaml").read_text())
TRANSLATIONS = yaml.safe_load((APP_DIR / "translations" / "en.yaml").read_text())

SECRET_OPTIONS = {"onepassword_service_account_token", "guest_password", "guest_secret_key"}


def test_config_shape():
    assert CONFIG["slug"] == "casa_browser"
    assert CONFIG["name"] == "Casa Browser"
    assert CONFIG["image"] == "ghcr.io/bonzanni/casa_browser"
    assert CONFIG["arch"] == ["amd64"]
    assert CONFIG["version"] == __version__
    assert "stage" not in CONFIG          # stable: the default, which the linter refuses to repeat
    assert CONFIG["init"] is False
    # Permissions: exactly the ones the design needs, and nothing that widens them.
    # Supervisor defaults stand for ingress_port 8099, no hassio_api and no host network
    # (the app linter refuses a key that repeats its default).
    assert CONFIG["ingress"] is True and "ingress_port" not in CONFIG
    assert CONFIG["homeassistant_api"] is True
    assert CONFIG["video"] is True
    assert CONFIG["privileged"] == ["NET_ADMIN"]
    for absent in ("ports", "map", "host_pid", "host_ipc", "host_dbus", "full_access",
                   "docker_api", "devices", "uart", "usb", "gpio", "kernel_modules",
                   "apparmor", "hassio_role", "auth_api", "webui", "watchdog",
                   "hassio_api", "host_network"):
        assert absent not in CONFIG, absent
    # Saved logins never leave the machine in a backup (design §5.1, v9.1).
    assert CONFIG["backup_exclude"] == ["profiles/*", "downloads/*", "proxy/*"]


def test_options_schema_translations_agree():
    options, schema = CONFIG["options"], CONFIG["schema"]
    assert set(options) <= set(schema)
    assert set(TRANSLATIONS["configuration"]) == set(schema)
    for key in SECRET_OPTIONS:
        assert schema[key] == "password", key
    # The language and time zone are optional overrides of Home Assistant's.
    assert schema["language"] == "str?" and "language" not in options
    assert schema["timezone"] == "str?" and "timezone" not in options
    assert options["retention_days"] == 30 and options["log_level"] == "info"
    assert options["onepassword_vault"] == "Casa Browser"
    for key, entry in TRANSLATIONS["configuration"].items():
        assert entry["name"] and entry["description"], key


def test_dependencies_pinned():
    for name in ("requirements.txt", "requirements-test.txt"):
        for line in (APP_DIR / name).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                assert re.fullmatch(r"[A-Za-z0-9_.\-\[\]]+==[A-Za-z0-9_.+\-]+", line), line


def test_package_and_boot_layout():
    opt = APP_DIR / "rootfs" / "opt" / "casa_browser"
    assert (opt / "__init__.py").is_file()
    assert not (APP_DIR / "__init__.py").exists()  # the outer folder is not a package
    s6 = APP_DIR / "rootfs" / "etc" / "s6-overlay"
    unit = s6 / "s6-rc.d" / "init-browser-config"
    assert (unit / "type").read_text().strip() == "oneshot"
    assert (unit / "up").read_text().strip() == "/etc/s6-overlay/scripts/browser-config.sh"
    assert (s6 / "user-bundles.d" / "user" / "contents.d" / "init-browser-config").exists()
    assert not (s6 / "s6-rc.d" / "user").exists()  # the base image ships the bundle
    script = (s6 / "scripts" / "browser-config.sh").read_bytes()
    assert script.startswith(b"#!/command/with-contenv bash\n") and b"\r" not in script
    assert b"casa_browser.options" in script
    dockerfile = (APP_DIR / "Dockerfile").read_text()
    for user, uid in (("casa-browser", 1100), ("casa-proxy", 1101), ("casa-broker", 1102),
                      ("casa-controller", 1103)):
        assert f"--uid {uid} --gid {uid}" in dockerfile and user in dockerfile


def test_icons_allowlisted():
    allow = (REPO_ROOT / ".githooks" / "binary-allowlist.txt").read_text().splitlines()
    for name in ("icon.png", "logo.png"):
        assert f"casa_browser/{name}" in allow
        assert (APP_DIR / name).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_apparmor_profile():
    """Task 14: the app ships its own AppArmor profile (Supervisor loads it instead of
    docker-default). It is named for the slug; only Chrome's child profile holds the
    namespace sandbox's capabilities; nobody gets raw or packet sockets or mounts; the main
    profile denies sys_ptrace explicitly (the launcher's /proc scan asks for it, harmlessly —
    measured on the test host, AA-1). When an AppArmor parser is installed the profile must
    compile."""
    import shutil
    import subprocess
    text = (APP_DIR / "apparmor.txt").read_text()
    rules = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    assert any(r.startswith("profile casa_browser ") for r in rules)
    child = text.index("profile chrome ")
    main, chrome = text[:child], text[child:]
    for cap in ("sys_admin", "sys_chroot", "sys_module", "sys_rawio", "mknod", "sys_boot"):
        assert f"capability {cap}," not in "\n".join(l for l in main.splitlines() if not l.strip().startswith("#")), cap
    # positive controls: the same scan finds what each profile does grant
    assert "capability net_admin," in main and "capability setuid," in main
    assert "capability sys_admin," in chrome and "capability net_admin," not in chrome
    assert "/opt/google/chrome/chrome cx -> chrome," in main
    main_rules = [l.strip() for l in main.splitlines() if l.strip() and not l.strip().startswith("#")]
    assert "deny capability sys_ptrace," in main_rules
    assert "capability sys_ptrace," not in main_rules
    assert "capability sys_ptrace," in [l.strip() for l in chrome.splitlines()]   # control
    assert "DRAFT" not in text
    for body in (main, chrome):
        assert "deny network raw," in body and "deny network packet," in body and "deny mount," in body
        assert "\n  capability,\n" not in body and "\n    capability,\n" not in body
    parser = shutil.which("apparmor_parser")
    if parser:
        r = subprocess.run([parser, "-S", "-I", "/etc/apparmor.d", str(APP_DIR / "apparmor.txt")],
                           capture_output=True)
        assert r.returncode == 0, r.stderr.decode()[-600:]


def test_image_lane_fails_on_a_skipped_test(monkeypatch):
    """run-integration.sh sets CASA_IT_NO_SKIP=1: a skipped image test fails the lane; outside
    the lane a skip is a skip."""
    from types import SimpleNamespace

    import conftest

    def session(skipped):
        reporter = SimpleNamespace(stats={"skipped": [object()] * skipped})
        manager = SimpleNamespace(get_plugin=lambda name: reporter)
        return SimpleNamespace(config=SimpleNamespace(pluginmanager=manager), exitstatus=0)

    monkeypatch.setenv("CASA_IT_NO_SKIP", "1")
    s = session(1)
    conftest.pytest_sessionfinish(s, 0)
    assert s.exitstatus == 1
    s = session(0)
    conftest.pytest_sessionfinish(s, 0)
    assert s.exitstatus == 0
    monkeypatch.delenv("CASA_IT_NO_SKIP")
    s = session(2)
    conftest.pytest_sessionfinish(s, 0)
    assert s.exitstatus == 0
    assert "CASA_IT_NO_SKIP=1" in (APP_DIR / "test" / "run-integration.sh").read_text()
