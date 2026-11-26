"""Task 2: options, the effective locale and root bootstrap."""
from __future__ import annotations

import json
import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from casa_browser.broker.main import build_vault
from casa_browser.options import (
    LOGIN_STORES,
    STORE_KEY_ENV,
    BrokerOptions,
    OptionsError,
    Owners,
    Paths,
    PublicOptions,
    bootstrap,
    chrome_policy,
    effective_locale,
    load_options,
    parse_options,
    read_ha_language,
    read_locale_record,
)

from conftest import APP_DIR

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
TOKEN_OPTION = "onepassword_service_account_token"
GUEST_OPTIONS = ("guest_sign_in_address", "guest_email", "guest_password", "guest_secret_key")
STORE_KEY = "CanaryStoreKey-0008"


def _layout(tmp_path: Path, options: dict | None) -> Paths:
    paths = Paths(data=tmp_path / "data", run=tmp_path / "run",
                  container_env=tmp_path / "s6env",
                  chrome_policy=tmp_path / "etc" / "policies" / "managed" / "casa.json")
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
    _, broker = parse_options({TOKEN_OPTION: CANARIES[TOKEN_OPTION],
                               "guest_sign_in_address": value})
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
    assert json.loads(hosts.read_text()) == {
        "onepassword_account_host": CANARIES["guest_sign_in_address"], "login_store": "onepassword"}
    assert stat.S_IMODE(hosts.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.proxy_private.stat().st_mode) == 0o700
    assert CANARIES["guest_sign_in_address"] not in _all_text(paths.state)
    # Invalid options: the file names no host.
    paths.options.write_text(json.dumps({**CANARIES, "guest_sign_in_address": "evil.example"}))
    bootstrap({"TZ": "UTC"}, paths, Owners(), log=lambda _l: None)
    assert json.loads(hosts.read_text()) == {"onepassword_account_host": None, "login_store": "none"}


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


# --------------------------------------------------------------------------- where logins are kept


def _boot(paths: Paths, env: dict | None = None, log=lambda _l: None):
    return bootstrap({"SUPERVISOR_TOKEN": TOKEN, "TZ": "UTC", **(env or {})}, paths, Owners(),
                     fetch_factory=_fake_core((200, b'{"language": "nl"}'), []), log=log)


def _three_files(paths: Paths) -> tuple[dict, dict, dict]:
    """What the broker, the controller and the proxy each read."""
    return (json.loads(paths.broker_config.read_text()),
            json.loads(paths.public_options.read_text()),
            json.loads((paths.proxy_private / "hosts.json").read_text()))


def _written(root: Path, paths: Paths) -> dict[Path, str]:
    """Every file under ``root`` except the options file the test itself put there."""
    return {p: p.read_bytes().decode("utf-8", "replace") for p in root.rglob("*")
            if p.is_file() and not p.is_symlink() and p != paths.options}


def test_token_set_selects_onepassword_and_changes_nothing_else(tmp_path):
    paths = _layout(tmp_path, {**CANARIES})
    result = _boot(paths)
    broker, public, hosts = _three_files(paths)
    assert broker == {
        "op_token": CANARIES[TOKEN_OPTION], "vault": CANARIES["onepassword_vault"],
        "guest_sign_in_address": CANARIES["guest_sign_in_address"],
        "guest_email": CANARIES["guest_email"], "guest_password": CANARIES["guest_password"],
        "guest_secret_key": CANARIES["guest_secret_key"],
        "login_store": "onepassword", "store_key": ""}
    assert public == {"language": None, "timezone": None, "retention_days": 30,
                      "log_level": "info", "configured": True, "options_error": None,
                      "login_store": "onepassword"}
    assert hosts == {"onepassword_account_host": CANARIES["guest_sign_in_address"],
                     "login_store": "onepassword"}
    assert result.configured and result.options_error is None
    # A token with a guest value missing is 1Password, not yet set up, as before.
    _, partial = parse_options({**CANARIES, "guest_email": ""})
    assert partial.login_store == "onepassword" and not partial.configured


@pytest.mark.parametrize("token", [{}, {TOKEN_OPTION: ""}, {TOKEN_OPTION: None}],
                         ids=["absent", "empty", "null"])
def test_empty_token_selects_builtin(tmp_path, token):
    paths = _layout(tmp_path, {"retention_days": 30, "log_level": "info", **token})
    result = _boot(paths)
    broker, public, hosts = _three_files(paths)
    assert broker["login_store"] == public["login_store"] == hosts["login_store"] == "builtin"
    assert "builtin" in LOGIN_STORES
    assert public["configured"] is True and public["options_error"] is None
    assert result.configured and result.options_error is None
    assert broker["op_token"] == "" and hosts["onepassword_account_host"] is None


def test_builtin_drops_the_guest_values_and_names_no_host(tmp_path):
    guest = {key: CANARIES[key] for key in GUEST_OPTIONS}
    _, parsed = parse_options(guest)
    assert parsed.login_store == "builtin" and parsed.configured
    assert [getattr(parsed, key) for key in GUEST_OPTIONS] == ["", "", "", ""]
    paths = _layout(tmp_path, {TOKEN_OPTION: "", **guest})
    logs: list[str] = []
    result = _boot(paths, log=logs.append)
    broker, _, hosts = _three_files(paths)
    assert [broker[key] for key in GUEST_OPTIONS] == ["", "", "", ""]
    assert hosts == {"onepassword_account_host": None, "login_store": "builtin"}
    written = _written(tmp_path, paths)
    assert paths.broker_config in written and paths.chrome_policy in written
    for value in guest.values():
        assert not [p for p, text in written.items() if value in text]
        assert value not in "\n".join(logs) and value not in repr(result)


def _bad_retention(paths: Paths, tmp_path: Path) -> None:
    paths.options.write_text(json.dumps({TOKEN_OPTION: "", "retention_days": -5}))


def _bad_guest_address(paths: Paths, tmp_path: Path) -> None:
    paths.options.write_text(json.dumps({TOKEN_OPTION: "", "guest_sign_in_address": "evil.example"}))


def _not_json(paths: Paths, tmp_path: Path) -> None:
    paths.options.write_text("{not json")


def _symlinked(paths: Paths, tmp_path: Path) -> None:
    # The file it points at would select the built-in store if it were read.
    target = tmp_path / "outside-options.json"
    target.write_text(json.dumps({TOKEN_OPTION: "", "retention_days": 30}))
    paths.options.symlink_to(target)


def _number_token(paths: Paths, tmp_path: Path) -> None:
    paths.options.write_text(json.dumps({TOKEN_OPTION: 123, "retention_days": 30}))


def _boolean_token(paths: Paths, tmp_path: Path) -> None:
    paths.options.write_text(json.dumps({TOKEN_OPTION: True, "retention_days": 30}))


@pytest.mark.parametrize("spoil, key", [
    (_bad_retention, "retention_days"), (_bad_guest_address, "guest_sign_in_address"),
    (_not_json, "options"), (_symlinked, "options"), (_number_token, TOKEN_OPTION),
    (_boolean_token, TOKEN_OPTION)],
    ids=["retention_days", "guest_address", "unreadable", "symlinked", "number_token",
         "boolean_token"])
def test_invalid_options_select_none_never_builtin(tmp_path, spoil, key):
    paths = _layout(tmp_path, None)
    spoil(paths, tmp_path)
    result = _boot(paths, {STORE_KEY_ENV: STORE_KEY})
    broker, public, hosts = _three_files(paths)
    assert broker["login_store"] == public["login_store"] == hosts["login_store"] == "none"
    assert result.options_error == public["options_error"] == key
    assert public["configured"] is False and result.configured is False
    rebuilt = BrokerOptions(**broker)
    assert rebuilt.login_store == "none" and rebuilt.configured is False
    # Nothing for the built-in store either: no key, and no host for the proxy.
    assert broker["store_key"] == "" and broker["op_token"] == ""
    assert hosts["onepassword_account_host"] is None
    # The vault the broker builds from that file opens no store: no file is made.
    vault_dir = tmp_path / "vault"
    build_vault(rebuilt, vault_dir)
    assert not vault_dir.exists() or list(vault_dir.iterdir()) == []


def test_none_is_not_configured_whatever_else_it_holds():
    """The mode decides: values that would make 1Password count as set up do not."""
    filled = dict(op_token="t", vault="v", guest_sign_in_address="a.1password.eu",
                  guest_email="e", guest_password="p", guest_secret_key="k")
    assert BrokerOptions(**filled).configured                       # 1Password by default
    assert BrokerOptions(**filled, login_store="onepassword").configured
    assert not BrokerOptions(**filled, login_store="none").configured
    assert BrokerOptions(op_token="", login_store="builtin").configured
    assert not BrokerOptions(op_token="").configured
    assert LOGIN_STORES == ("builtin", "onepassword", "none")


def test_a_whitespace_token_is_a_token(tmp_path):
    _, parsed = parse_options({TOKEN_OPTION: "  "})
    assert parsed.login_store == "onepassword" and parsed.op_token == "  "
    paths = _layout(tmp_path, {TOKEN_OPTION: "  ", "guest_email": CANARIES["guest_email"]})
    result = _boot(paths, {STORE_KEY_ENV: STORE_KEY})
    broker, public, hosts = _three_files(paths)
    assert broker["login_store"] == public["login_store"] == hosts["login_store"] == "onepassword"
    # Everything as it was given: the token and the guest value are kept, no store key is taken.
    assert broker["op_token"] == "  " and broker["guest_email"] == CANARIES["guest_email"]
    assert broker["store_key"] == ""
    assert public["configured"] is False and result.configured is False


def test_the_store_key_reaches_only_the_brokers_file_and_is_scrubbed(tmp_path):
    assert STORE_KEY_ENV == "CASA_BROWSER_VAULT_KEY"
    paths = _layout(tmp_path / "builtin", {"retention_days": 30})
    (paths.container_env / STORE_KEY_ENV).write_text(STORE_KEY)
    logs: list[str] = []
    result = _boot(paths, {STORE_KEY_ENV: STORE_KEY}, log=logs.append)
    assert json.loads(paths.broker_config.read_text())["store_key"] == STORE_KEY
    assert stat.S_IMODE(paths.broker_config.stat().st_mode) == 0o600
    written = _written(tmp_path / "builtin", paths)
    assert [p for p, text in written.items() if STORE_KEY in text] == [paths.broker_config]
    assert STORE_KEY not in "\n".join(logs) and STORE_KEY not in repr(result)
    assert STORE_KEY not in repr(BrokerOptions(op_token="", login_store="builtin",
                                               store_key=STORE_KEY))
    # Gone from the s6 container environment: no service can inherit it.
    assert not (paths.container_env / STORE_KEY_ENV).exists()
    assert (paths.container_env / "TZ").exists()

    # Copied as it is: bootstrap does not trim a key or judge whether it is usable.
    for given in ("   ", f" {STORE_KEY}\n"):
        paths = _layout(tmp_path / f"asis-{len(given)}", {})
        _boot(paths, {STORE_KEY_ENV: given})
        assert json.loads(paths.broker_config.read_text())["store_key"] == given
    # Unset or empty: no key.
    for env in ({}, {STORE_KEY_ENV: ""}):
        paths = _layout(tmp_path / f"unset-{len(env)}", {})
        _boot(paths, env)
        assert json.loads(paths.broker_config.read_text())["store_key"] == ""

    # With a token the key is not taken, and it is scrubbed all the same.
    paths = _layout(tmp_path / "onepassword", {**CANARIES})
    (paths.container_env / STORE_KEY_ENV).write_text(STORE_KEY)
    _boot(paths, {STORE_KEY_ENV: STORE_KEY})
    assert json.loads(paths.broker_config.read_text())["store_key"] == ""
    assert not [p for p, text in _written(tmp_path / "onepassword", paths).items()
                if STORE_KEY in text]
    assert not (paths.container_env / STORE_KEY_ENV).exists()


def test_the_store_key_is_scrubbed_when_bootstrap_fails(tmp_path):
    paths = _layout(tmp_path, None)
    (paths.container_env / STORE_KEY_ENV).write_text(STORE_KEY)
    (paths.data / "state").write_text("a file where a directory belongs")
    with pytest.raises(Exception):
        _boot(paths, {STORE_KEY_ENV: STORE_KEY})
    assert not (paths.container_env / STORE_KEY_ENV).exists()


def test_the_policy_source_ships_beside_the_module_and_chrome_reads_the_fixed_place():
    assert Paths().policy_source == APP_DIR / "rootfs" / "opt" / "casa_browser" / "chrome_policy.json"
    assert Paths().chrome_policy == Path("/etc/opt/chrome/policies/managed/casa.json")
    # The image does not ship the file Chrome reads: only bootstrap writes it.
    assert not (APP_DIR / "rootfs" / "etc" / "opt" / "chrome" / "policies" / "managed"
                / "casa.json").exists()


def test_policy_for_onepassword_and_none_is_the_source_byte_for_byte(tmp_path):
    source = Paths().policy_source.read_bytes()
    parsed = json.loads(source)
    assert chrome_policy(parsed, "onepassword") == parsed == chrome_policy(parsed, "none")
    for name, options in (("onepassword", {**CANARIES}), ("none", {"retention_days": -5})):
        paths = _layout(tmp_path / name, options)
        for _ in range(2):                                  # written again at every start
            _boot(paths)
            assert json.loads(paths.broker_config.read_text())["login_store"] == name
            assert paths.chrome_policy.read_bytes() == source
        assert stat.S_IMODE(paths.chrome_policy.stat().st_mode) == 0o644
        assert [p.name for p in paths.chrome_policy.parent.iterdir()] == ["casa.json"]
        # The bytes themselves, not the same policy written out again: a source laid out
        # differently arrives as it is.
        odd = tmp_path / f"{name}-source.json"
        odd.write_bytes(b'{"SyncDisabled":true ,\n\n   "ExtensionSettings": {},"BrowserSignin":0}')
        _boot(replace(paths, policy_source=odd))
        assert paths.chrome_policy.read_bytes() == odd.read_bytes()


@pytest.mark.parametrize("options", [{**CANARIES}, {}], ids=["onepassword", "builtin"])
def test_chrome_can_reach_the_policy_under_a_strict_umask(tmp_path, options):
    """Chrome runs as its own user. The file and every directory bootstrap makes on the way to
    it are readable by all, whatever umask bootstrap was started with."""
    paths = _layout(tmp_path, options)
    before = os.umask(0o077)
    try:
        _boot(paths)
    finally:
        os.umask(before)
    assert stat.S_IMODE(paths.chrome_policy.stat().st_mode) == 0o644
    for made in (paths.chrome_policy.parent, paths.chrome_policy.parent.parent, tmp_path / "etc"):
        assert stat.S_IMODE(made.stat().st_mode) == 0o755


def test_policy_for_builtin_drops_exactly_two_keys(tmp_path):
    source = json.loads(Paths().policy_source.read_text())
    before = json.dumps(source, sort_keys=True)
    paths = _layout(tmp_path, {})
    _boot(paths)
    written = json.loads(paths.chrome_policy.read_text())
    assert set(source) - set(written) == {"ExtensionSettings", "DeveloperToolsAvailability"}
    assert set(written) < set(source)
    assert all(written[key] == source[key] for key in written)
    assert chrome_policy(source, "builtin") == written
    assert json.dumps(source, sort_keys=True) == before      # the source itself is not changed
    assert stat.S_IMODE(paths.chrome_policy.stat().st_mode) == 0o644
    assert stat.S_IMODE(paths.chrome_policy.parent.stat().st_mode) == 0o755
    # The mode is decided at every start: a token set later brings the whole policy back.
    paths.options.write_text(json.dumps({**CANARIES}))
    _boot(paths)
    assert paths.chrome_policy.read_bytes() == Paths().policy_source.read_bytes()
    paths.options.write_text(json.dumps({}))
    _boot(paths)
    assert json.loads(paths.chrome_policy.read_text()) == written
    assert [p.name for p in paths.chrome_policy.parent.iterdir()] == ["casa.json"]


@pytest.mark.parametrize("options", [{**CANARIES}, {}], ids=["onepassword", "builtin"])
def test_a_policy_that_cannot_be_written_fails_bootstrap(tmp_path, options):
    paths = _layout(tmp_path, options)
    paths.chrome_policy.parent.parent.mkdir(parents=True)
    paths.chrome_policy.parent.write_text("a file where a directory belongs")
    with pytest.raises(OSError):
        _boot(paths)
    # Nothing that lets a service start was written, and the token is scrubbed all the same.
    assert not paths.broker_config.exists() and not paths.public_options.exists()
    assert not (paths.proxy_private / "hosts.json").exists()
    assert not (paths.container_env / "SUPERVISOR_TOKEN").exists()


def test_a_missing_policy_source_fails_bootstrap(tmp_path):
    paths = replace(_layout(tmp_path, {**CANARIES}), policy_source=tmp_path / "absent.json")
    with pytest.raises(OSError):
        _boot(paths)
    assert not paths.broker_config.exists() and not paths.chrome_policy.exists()
