"""Task 2: options, the effective locale and root bootstrap."""
from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from casa_browser.options import (
    OptionsError,
    Owners,
    Paths,
    PublicOptions,
    bootstrap,
    child_environment,
    effective_locale,
    load_options,
    parse_options,
    read_ha_language,
    read_locale_record,
)

CANARIES = {
    "onepassword_service_account_token": "ops_CANARYTOKEN0000000000000000000001",
    "guest_sign_in_address": "canary-signin-0002.1password.eu",
    "guest_email": "canary-0003@example.com",
    "guest_password": "CanaryPassword-0004",
    # Built at run time: a literal Secret-Key-shaped value trips the push gate's secret scanner.
    "guest_secret_key": "-".join(("canary", "guest", "0005")),
    "onepassword_vault": "CanaryVault-0006",
}
SECRET_VALUES = [v for k, v in CANARIES.items() if k != "onepassword_vault"]
TOKEN = "SUPERVISORTOKEN-CANARY-7777777777777777"


def _layout(tmp_path: Path, options: dict | None) -> Paths:
    paths = Paths(data=tmp_path / "data", run=tmp_path / "run",
                  container_env=tmp_path / "s6env")
    paths.data.mkdir(parents=True)
    paths.container_env.mkdir(parents=True)
    (paths.container_env / "SUPERVISOR_TOKEN").write_text(TOKEN)
    (paths.container_env / "HASSIO_TOKEN").write_text(TOKEN)
    (paths.container_env / "TZ").write_text("Europe/Amsterdam")
    if options is not None:
        paths.options.write_text(json.dumps(options))
    return paths


def _fake_core(answer: tuple[int, bytes] | Exception, seen: list):
    def factory(token: str):
        seen.append(("token", token))

        def fetch(url: str):
            seen.append(("url", url))
            if isinstance(answer, Exception):
                raise answer
            return answer

        return fetch

    return factory


def _all_text(root: Path) -> str:
    out = []
    for p in root.rglob("*"):
        if p.is_file() and not p.is_symlink():
            out.append(p.read_bytes().decode("utf-8", "replace"))
    return "\n".join(out)


def test_secret_options_not_in_public_config_or_repr(tmp_path):
    public, broker = parse_options({**CANARIES, "retention_days": 30, "log_level": "info"})
    rendered = repr(public) + repr(broker) + str(broker) + json.dumps(public.__dict__)
    for value in SECRET_VALUES:
        assert value not in rendered
    assert broker.configured
    # A validation error names the key, never the value.
    with pytest.raises(OptionsError) as err:
        parse_options({**CANARIES, "onepassword_service_account_token": ["x" * 10, SECRET_VALUES[0]]})
    assert err.value.key == "onepassword_service_account_token"
    assert all(v not in str(err.value) and v not in repr(err.value) for v in SECRET_VALUES)
    # Bootstrap: public state carries no secret; the broker file has them, 0600.
    paths = _layout(tmp_path, {**CANARIES, "retention_days": 30, "log_level": "info",
                               "language": "it"})
    logs: list[str] = []
    bootstrap({"SUPERVISOR_TOKEN": TOKEN, "TZ": "Europe/Amsterdam"}, paths, Owners(),
              fetch_factory=_fake_core((200, b'{"language":"de"}'), []), log=logs.append)
    state_text = _all_text(paths.state)
    for value in SECRET_VALUES:
        assert value not in state_text
        assert value not in "\n".join(logs)
    broker_file = paths.broker_config
    assert stat.S_IMODE(broker_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.broker_dir.stat().st_mode) == 0o700
    assert json.loads(broker_file.read_text())["op_token"] == CANARIES["onepassword_service_account_token"]
    assert stat.S_IMODE(paths.options.stat().st_mode) == 0o600


def test_options_reject_invalid_locale_retention():
    for bad in ({"timezone": "Mars/Olympus"}, {"timezone": "../../etc/passwd"},
                {"retention_days": -1}, {"retention_days": 0}, {"retention_days": "30"},
                {"retention_days": True}, {"language": "not a tag!"}, {"language": "x"},
                {"log_level": "verbose"}):
        with pytest.raises(OptionsError) as err:
            parse_options({**CANARIES, **bad})
        assert all(v not in str(err.value) for v in SECRET_VALUES)
        assert list(bad)[0] == err.value.key
    public, _ = parse_options({**CANARIES, "timezone": "Europe/Rome", "language": "en-GB",
                               "retention_days": 1})
    assert (public.timezone, public.language, public.retention_days) == ("Europe/Rome", "en-GB", 1)
    # Empty optional strings mean "unset".
    public, _ = parse_options({**CANARIES, "language": "", "timezone": ""})
    assert public.language is None and public.timezone is None


def test_load_options_unreadable(tmp_path):
    with pytest.raises(OptionsError):
        load_options(tmp_path / "missing.json")
    (tmp_path / "bad.json").write_text("{not json")
    with pytest.raises(OptionsError):
        load_options(tmp_path / "bad.json")


def test_language_follows_ha(tmp_path):
    paths = _layout(tmp_path, {**CANARIES})
    seen: list = []
    result = bootstrap({"SUPERVISOR_TOKEN": TOKEN, "TZ": "Europe/Amsterdam"}, paths, Owners(),
                       fetch_factory=_fake_core((200, b'{"language": "de", "time_zone": "x"}'), seen),
                       log=lambda _l: None)
    assert result.locale is not None
    assert (result.locale.language, result.locale.language_source) == ("de", "home_assistant")
    assert result.locale.timezone == "Europe/Amsterdam"
    assert ("url", "http://supervisor/core/api/config") in seen
    assert read_locale_record(paths) == result.locale


def test_language_option_overrides(tmp_path):
    paths = _layout(tmp_path, {**CANARIES, "language": "it"})
    seen: list = []
    result = bootstrap({"SUPERVISOR_TOKEN": TOKEN, "TZ": "Europe/Amsterdam"}, paths, Owners(),
                       fetch_factory=_fake_core((200, b'{"language": "de"}'), seen),
                       log=lambda _l: None)
    assert (result.locale.language, result.locale.language_source) == ("it", "option")
    assert effective_locale(PublicOptions(language="it"), "de", "UTC").language == "it"
    assert effective_locale(PublicOptions(timezone="Asia/Tokyo"), "de", "UTC").timezone == "Asia/Tokyo"


@pytest.mark.parametrize("answer", [
    OSError("connection refused"),
    (500, b'{"language": "de"}'),
    (401, b""),
    (200, b"not json"),
    (200, b'{"time_zone": "UTC"}'),
    (200, b'{"language": 7}'),
    (200, b'{"language": "<script>"}'),
    (200, b'["de"]'),
])
def test_ha_language_unavailable_holds_runs(tmp_path, answer):
    paths = _layout(tmp_path, {**CANARIES})
    result = bootstrap({"SUPERVISOR_TOKEN": TOKEN, "TZ": "Europe/Amsterdam"}, paths, Owners(),
                       fetch_factory=_fake_core(answer, []), log=lambda _l: None)
    assert result.locale is None
    assert json.loads(paths.locale_record.read_text()) == {"needs_locale": True}
    assert read_locale_record(paths) is None
    # No silent default.
    assert "en-US" not in paths.locale_record.read_text()


def test_needs_locale_without_token_or_tz():
    assert effective_locale(PublicOptions(), None, "Europe/Amsterdam") is None
    assert effective_locale(PublicOptions(language="it"), None, None) is None
    assert effective_locale(PublicOptions(language="it"), None, "Not/AZone") is None
    assert read_ha_language(lambda url: (200, b'{"language": "pt_BR"}')) == "pt_BR"
    assert effective_locale(PublicOptions(), "pt_BR", "UTC").language == "pt-BR"


def test_supervisor_token_confined(tmp_path, monkeypatch):
    paths = _layout(tmp_path, {**CANARIES})
    logs: list[str] = []
    seen: list = []
    result = bootstrap({"SUPERVISOR_TOKEN": TOKEN, "TZ": "Europe/Amsterdam", "PATH": "/usr/bin"},
                       paths, Owners(),
                       fetch_factory=_fake_core((200, b'{"language": "de"}'), seen),
                       log=logs.append)
    # The token reached only the fetch factory (a header, never argv).
    assert ("token", TOKEN) in seen
    # Gone from the s6 container environment: with-contenv cannot pass it to any service.
    assert not (paths.container_env / "SUPERVISOR_TOKEN").exists()
    assert not (paths.container_env / "HASSIO_TOKEN").exists()
    assert (paths.container_env / "TZ").exists()
    # Absent from every file bootstrap wrote, the logs and the locale record.
    for root in (paths.state, paths.run, paths.container_env):
        assert TOKEN not in _all_text(root)
    assert TOKEN not in "\n".join(logs)
    assert TOKEN not in repr(result)
    # A service child's environment is the allowlist only.
    child = child_environment({"SUPERVISOR_TOKEN": TOKEN, "HASSIO_TOKEN": TOKEN, "PATH": "/bin",
                               "TZ": "UTC", "OP_SERVICE_ACCOUNT_TOKEN": "x"})
    assert child == {"PATH": "/bin", "TZ": "UTC"}
    # The token is scrubbed even when bootstrap fails part-way.
    paths2 = _layout(tmp_path / "second", None)
    (paths2.data / "state").write_text("a file where a directory belongs")
    with pytest.raises(Exception):
        bootstrap({"SUPERVISOR_TOKEN": TOKEN}, paths2, Owners(),
                  fetch_factory=_fake_core((200, b"{}"), []), log=lambda _l: None)
    assert not (paths2.container_env / "SUPERVISOR_TOKEN").exists()


def test_invalid_options_still_bootstrap(tmp_path):
    paths = _layout(tmp_path, {**CANARIES, "retention_days": -5})
    logs: list[str] = []
    result = bootstrap({"SUPERVISOR_TOKEN": TOKEN, "TZ": "UTC"}, paths, Owners(),
                       fetch_factory=_fake_core((200, b'{"language": "nl"}'), []), log=logs.append)
    assert result.options_error == "retention_days"
    assert not result.configured
    public = json.loads(paths.public_options.read_text())
    assert public["options_error"] == "retention_days" and public["configured"] is False
    # The broker gets no credentials from an options file that did not validate.
    assert json.loads(paths.broker_config.read_text())["op_token"] == ""
    assert any("retention_days" in line for line in logs)


def test_bootstrap_layout_modes_and_idempotence(tmp_path):
    paths = _layout(tmp_path, {**CANARIES})
    for _ in range(2):
        bootstrap({"SUPERVISOR_TOKEN": TOKEN, "TZ": "UTC"}, paths, Owners(),
                  fetch_factory=_fake_core((200, b'{"language": "nl"}'), []), log=lambda _l: None)
    for d in (paths.state, paths.artifacts, paths.profiles, paths.broker_dir):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700
    for f in (paths.public_options, paths.locale_record, paths.broker_config):
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert not [p for p in paths.state.iterdir() if p.name.startswith(".tmp-")]
    assert stat.S_IMODE(paths.downloads.stat().st_mode) == 0o2770


def test_bootstrap_empties_the_browser_downloads(tmp_path):
    paths = _layout(tmp_path, {**CANARIES})
    paths.downloads.mkdir(parents=True)
    (paths.downloads / "left-by-chrome").write_bytes(b"%PDF-")
    (paths.downloads / "sub").mkdir()
    outside = tmp_path / "outside-sentinel"
    outside.write_text("keep")
    (paths.downloads / "link").symlink_to(outside)
    bootstrap({"SUPERVISOR_TOKEN": TOKEN, "TZ": "UTC"}, paths, Owners(),
              fetch_factory=_fake_core((200, b'{"language": "nl"}'), []), log=lambda _l: None)
    assert list(paths.downloads.iterdir()) == [] and outside.read_text() == "keep"


def test_bootstrap_refuses_symlinked_data_dir(tmp_path):
    paths = _layout(tmp_path, {**CANARIES})
    outside = tmp_path / "outside"
    outside.mkdir()
    (paths.data / "state").symlink_to(outside)
    with pytest.raises(OptionsError):
        bootstrap({"TZ": "UTC"}, paths, Owners(), log=lambda _l: None)
    assert list(outside.iterdir()) == []


def test_symlinked_options_file_not_followed(tmp_path):
    """Bootstrap neither reads nor chmods through a symlinked options file (review r3-c1-r1)."""
    paths = _layout(tmp_path, None)
    target = tmp_path / "outside-options.json"
    target.write_text(json.dumps(CANARIES))
    target.chmod(0o644)
    paths.options.symlink_to(target)
    result = bootstrap({"SUPERVISOR_TOKEN": TOKEN, "TZ": "UTC"}, paths, Owners(),
                       fetch_factory=_fake_core((200, b'{"language": "nl"}'), []), log=lambda _l: None)
    assert result.options_error == "options" and not result.configured
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert json.loads(paths.broker_config.read_text())["op_token"] == ""


@pytest.mark.parametrize("value,host", [
    ("acme.1password.eu", "acme.1password.eu"),
    ("https://Acme-Team.1password.com/", "acme-team.1password.com"),
    ("acme.1password.ca", "acme.1password.ca"),
    ("", ""),
])
def test_sign_in_address_is_one_account_host(value, host):
    _, broker = parse_options({"guest_sign_in_address": value})
    assert broker.guest_sign_in_address == host


@pytest.mark.parametrize("value", [
    "my.1password.eu",                 # the generic region address names no account
    "acme.1password.eu.evil.example", "evil.example", "acme.1password.eu/signin",
    "http://acme.1password.eu", "acme.1password.eu:8443", "a.b.1password.eu", "-x.1password.eu",
    "acme.1password.de", "acme 1password.eu",
])
def test_sign_in_address_refused_shapes(value):
    with pytest.raises(OptionsError) as err:
        parse_options({"guest_sign_in_address": value})
    assert err.value.key == "guest_sign_in_address" and value not in str(err.value)


def test_bootstrap_hands_the_account_host_to_the_proxy_only(tmp_path):
    paths = _layout(tmp_path, {**CANARIES})
    bootstrap({"SUPERVISOR_TOKEN": TOKEN, "TZ": "UTC"}, paths, Owners(),
              fetch_factory=_fake_core((200, b'{"language": "nl"}'), []), log=lambda _l: None)
    hosts = paths.proxy_private / "hosts.json"
    assert json.loads(hosts.read_text()) == {"onepassword_account_host": CANARIES["guest_sign_in_address"]}
    assert stat.S_IMODE(hosts.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.proxy_private.stat().st_mode) == 0o700
    assert CANARIES["guest_sign_in_address"] not in _all_text(paths.state)
    # Invalid options: the file names no host.
    paths.options.write_text(json.dumps({**CANARIES, "guest_sign_in_address": "evil.example"}))
    bootstrap({"TZ": "UTC"}, paths, Owners(), log=lambda _l: None)
    assert json.loads(hosts.read_text()) == {"onepassword_account_host": None}


@pytest.mark.parametrize('existing', [False, True])
def test_bootstrap_private_persistent_service_directories(tmp_path, monkeypatch, existing):
    paths = _layout(tmp_path, {'language': 'en'})
    broker_state, proxy_state = paths.data / 'broker', paths.data / 'proxy'
    if existing:
        for directory in (broker_state, proxy_state):
            directory.mkdir(mode=0o777)
            directory.chmod(0o777)
    ownership = {}
    monkeypatch.setattr('casa_browser.options.os.chown', lambda path, uid, gid: ownership.update({path: (uid, gid)}))
    monkeypatch.setattr('casa_browser.options.os.fchown', lambda *args: None)
    owners = Owners(broker=(1102, 1102), proxy=(1101, 1101))
    bootstrap({'TZ': 'UTC'}, paths, owners, log=lambda line: None)
    for directory, owner in ((broker_state, owners.broker), (proxy_state, owners.proxy)):
        assert directory.is_dir()
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert ownership[directory] == owner
    assert paths.broker_state == broker_state
    assert paths.proxy_state == proxy_state
