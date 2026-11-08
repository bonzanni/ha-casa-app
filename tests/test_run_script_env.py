"""Regression test for v0.18.1: bashio->env wiring of telegram_engagement_supergroup_id.

Reads the s6-overlay/s6-rc.d/svc-casa/run script and asserts every
schema option that casa_core.py::os.environ.get reads is exported.
Catches the v0.11.0 -> v0.18.0 regression where
telegram_engagement_supergroup_id was added to schema + casa_core but
not to the run script."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _read_run_script() -> str:
    p = (
        Path(__file__).resolve().parent.parent
        / "casa" / "rootfs" / "etc" / "s6-overlay"
        / "s6-rc.d" / "svc-casa" / "run"
    )
    return p.read_text(encoding="utf-8")


@pytest.mark.parametrize("var", [
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_TRANSPORT",
    "TELEGRAM_ENGAGEMENT_SUPERGROUP_ID",  # v0.18.1 - was missing pre-fix
    "TELEGRAM_BOT_API_BASE",  # was missing pre-fix; option was dead in prod
])
def test_run_script_exports_telegram_env_var(var):
    """svc-casa/run must export every TELEGRAM_* env var that
    casa_core.py reads at startup. Regression guard for v0.11.0 ->
    v0.18.0 regression where TELEGRAM_ENGAGEMENT_SUPERGROUP_ID was
    silently dropped."""
    script = _read_run_script()
    # Match `export VAR=` or `export VAR="..."` patterns
    assert (
        f"export {var}=" in script or f'export {var}="' in script
    ), f"Missing `export {var}=...` in svc-casa/run"


def test_run_script_pins_subagent_spawn_depth():
    """v0.131.0: CLI 2.1.219 changed the default nested-subagent spawn depth
    from 1 to 3. The pin is load-bearing for the assistant resident (its role
    ships `disallowed: []`, and Agent/Task spawns bypass allowed_tools), and
    the CHANGELOG claims it for all agents — keep the claim honest."""
    script = _read_run_script()
    assert "export CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=1" in script, (
        "Missing subagent spawn-depth pin in svc-casa/run"
    )


def test_run_script_exports_log_level():
    """v0.18.1: operator-facing log_level addon option must export to env.

    Uses null-normalize pattern (matches CASA_TZ / CASA_SCOPE_THRESHOLD
    handling) so install_logging() defaults to INFO when unset."""
    script = _read_run_script()
    assert "LOG_LEVEL" in script, "Missing LOG_LEVEL handling in svc-casa/run"


def test_run_script_exports_telegram_bot_api_base_null_normalized():
    """v0.12.0 regression: telegram_bot_api_base was declared in config.yaml
    schema and consumed by channels/telegram.py via env, but svc-casa/run
    never exported it, so the add-on option was silently dead in production
    (only the test-local harness exported it). Guard: the run script must
    read the option via bashio and export TELEGRAM_BOT_API_BASE, normalizing
    the "null" sentinel bashio returns for unset str? options."""
    script = _read_run_script()
    assert "bashio::config 'telegram_bot_api_base'" in script, (
        "svc-casa/run must read the telegram_bot_api_base add-on option"
    )
    start = script.index("_tg_api_base=")
    block = script[start:script.index("\nfi", start)]
    assert "export TELEGRAM_BOT_API_BASE=" in block, (
        "TELEGRAM_BOT_API_BASE must be exported (telegram.py reads it from env)"
    )
    assert '"null"' in block, (
        'must skip export when bashio returns the "null" sentinel for unset option'
    )


def test_run_script_never_exports_the_null_sentinel_as_webhook_secret():
    """v0.125.0 (#228): auth is mandatory, so there is no toggle to gate the
    export on — but the export must still not fire for an UNSET option.

    `bashio::config` returns the literal string "null" for an unset optional,
    and casa_core.py treats a non-empty WEBHOOK_SECRET as authoritative over
    /data/webhook_secret. Exporting "null" would therefore make that literal
    word the HMAC key while every signer used the generated secret file, and
    nothing would verify. Guard: the export is conditional on a real value.
    """
    script = _read_run_script()
    assert "bashio::config.true 'webhook_auth_enabled'" not in script, (
        "the webhook_auth_enabled toggle was removed in v0.125.0"
    )
    assert '_webhook_secret="$(bashio::config \'webhook_secret\')"' in script
    start = script.index('_webhook_secret="$(bashio::config')
    block = script[start:script.index("\nfi", start)]
    assert '[ "$_webhook_secret" != "null" ]' in block, (
        'the "null" sentinel must never be exported as the secret'
    )
    assert 'export WEBHOOK_SECRET="$_webhook_secret"' in block
    # Sol review: `with-contenv` imports the container environment, so an
    # inherited WEBHOOK_SECRET would outrank /data/webhook_secret in casa_core
    # while every signer still used the file. The option is the only override.
    assert script.index("unset WEBHOOK_SECRET") < start, (
        "inherited WEBHOOK_SECRET must be cleared before reading the option"
    )


def test_run_script_exports_no_memory_backend():
    """Memory is built in: the run script passes no memory-server address and
    selects no memory backend."""
    script = _read_run_script()
    assert "MEMORY_BACKEND" not in script
    assert "_API_URL" not in script


def test_run_script_execs_venv_python():
    """svc-casa/run must exec the venv interpreter by ABSOLUTE path.
    setup-configs.sh (P-9) prepends the user-writable /config/tools/bin
    ahead of /opt/casa/venv/bin in the s6 container PATH, so a bare
    `python3` exec is hijackable by any plugin whose systemRequirements
    declare verify_bin=python3 (install_venv symlinks it into tools/bin).
    Regression guard for the Dockerfile venv invariant."""
    script = _read_run_script()
    assert "exec /opt/casa/venv/bin/python3 /opt/casa/casa_core.py" in script
    assert "exec python3 " not in script


@pytest.mark.parametrize("option,var", [
    ("telegram_bot_token", "TELEGRAM_BOT_TOKEN"),
    ("telegram_chat_id", "TELEGRAM_CHAT_ID"),
    ("telegram_engagement_supergroup_id", "TELEGRAM_ENGAGEMENT_SUPERGROUP_ID"),
])
def test_run_script_null_normalizes_telegram_optionals(option, var):
    """#325: the three Telegram options are OPTIONAL (password?/str?/int?) in
    config.yaml, so a cleared field makes `bashio::config` yield the literal
    string "null". Exported unguarded:

    - TELEGRAM_BOT_TOKEN="null" is truthy in casa_core's `if telegram_token:`
      gate, so the Telegram channel constructs with a garbage token;
    - TELEGRAM_ENGAGEMENT_SUPERGROUP_ID="null" defeats the `or "0"` fallback
      and `int("null")` raises ValueError → svc-casa exits → the finish
      script stops the add-on → crash-loop from an ordinary config edit.

    Guard: none of the three may be exported straight from bashio; each must
    normalize the "null" sentinel to empty, like the neighboring optionals."""
    script = _read_run_script()
    assert f"bashio::config '{option}'" in script, (
        f"svc-casa/run must read the {option} add-on option"
    )
    assert f'export {var}="$(bashio::config' not in script, (
        f"{var} must not be exported straight from bashio — a cleared "
        f'optional yields the literal "null" (boot-fatal for the supergroup '
        f"id, garbage-truthy for the token/chat id)"
    )
    # The normalization must be bound to this var's read: between the
    # bashio read and the export there must be a "null" comparison.
    start = script.index(f"bashio::config '{option}'")
    block = script[start:script.index(f"export {var}=", start)]
    assert '"null"' in block, (
        f'{var} must normalize the "null" sentinel before exporting'
    )


def test_supergroup_id_parse_tolerates_garbage_and_keeps_negatives():
    """#325 defence-in-depth: even if a "null" (or other garbage) reaches the
    env, casa_core must not die parsing TELEGRAM_ENGAGEMENT_SUPERGROUP_ID —
    and real Telegram supergroup IDs are NEGATIVE (-100xxxxxxxxxx), so the
    parse must not clamp them to 0 (which would silently disable engagement
    routing — the reason `_env_int_or(min_value=0)` is NOT usable here)."""
    import casa_core

    assert casa_core._telegram_supergroup_id_from_env(
        {"TELEGRAM_ENGAGEMENT_SUPERGROUP_ID": "null"}) == 0
    assert casa_core._telegram_supergroup_id_from_env(
        {"TELEGRAM_ENGAGEMENT_SUPERGROUP_ID": ""}) == 0
    assert casa_core._telegram_supergroup_id_from_env({}) == 0
    assert casa_core._telegram_supergroup_id_from_env(
        {"TELEGRAM_ENGAGEMENT_SUPERGROUP_ID": "-1001234567890"}
    ) == -1001234567890


def test_run_script_null_normalizes_the_model_options():
    """#205: the boot path must not export bashio's "null" sentinel as a model.

    `primary_agent_model`/`voice_agent_model` are `list(...)?` — OPTIONAL — in
    config.yaml's schema, so their `options:` entries are INSTALL-TIME defaults,
    not a guarantee the key stays present in the stored options. An operator who
    clears the field leaves it absent and `bashio::config` yields the literal
    string "null".

    Exported unguarded, that "null" survives `role_slot._ha_model_options`
    (which defaults only on a BLANK value), reaches `resolve_role_model`, and is
    rejected as outside the role's allowed list — a RoleValidationError that
    FATALs every resident at boot.

    setup-configs.sh already applies this exact fallback for its config_sync
    boot-parity replay, so before the fix the validator could pass while the
    real boot crash-looped. Guard the parity in BOTH scripts.
    """
    script = _read_run_script()
    for option, var, fallback in (
        ("primary_agent_model", "PRIMARY_AGENT_MODEL", "opus"),
        ("voice_agent_model", "VOICE_AGENT_MODEL", "haiku"),
    ):
        assert f"bashio::config '{option}'" in script, (
            f"svc-casa/run must read the {option} add-on option"
        )
        assert f'export {var}="$(bashio::config' not in script, (
            f"{var} must not be exported straight from bashio — an unset "
            f'optional yields the literal "null" and FATALs role resolution'
        )
        assert '!= "null" ]' in script and f"={fallback}" in script, (
            f'{var} must fall back to {fallback} when bashio returns "null"'
        )


def test_setup_configs_and_run_script_agree_on_model_fallbacks():
    """The two scripts export the same model env for the same stored options.

    config_sync's boot-parity validation runs in a SEPARATE s6 oneshot
    (setup-configs.sh) from the actual boot (svc-casa/run). If only one of them
    normalizes the "null" sentinel, the validator's verdict stops predicting
    what boot will do — which is precisely the failure #205 closed.
    """
    setup = (
        Path(__file__).resolve().parent.parent
        / "casa" / "rootfs" / "etc" / "s6-overlay"
        / "scripts" / "setup-configs.sh"
    ).read_text(encoding="utf-8")
    run = _read_run_script()

    for fallback in ("opus", "haiku"):
        assert f"={fallback}" in setup and f"={fallback}" in run, (
            f"both scripts must fall back to {fallback} for the same option"
        )
    for var in ("PRIMARY_AGENT_MODEL", "VOICE_AGENT_MODEL"):
        assert f"export {var}=" in setup and f"export {var}=" in run


def test_run_script_has_no_direct_export_from_bashio_config():
    """#291: no env var may be exported straight from a bashio::config
    read. A key deleted from the stored options (or a future edit dropping
    its default) makes bashio return the literal string "null", which
    downstream truthy checks accept — e.g. `op` invoked with token "null"
    or a vault named "null". Every read goes through a temp var + null
    normalization."""
    script = _read_run_script()
    offenders = [
        line.strip() for line in script.splitlines()
        if line.strip().startswith("export ") and "bashio::config" in line
    ]
    assert offenders == [], (
        f"unguarded direct exports from bashio::config: {offenders}"
    )


@pytest.mark.parametrize("option", [
    "claude_oauth_token",
    "onepassword_service_account_token",
    "onepassword_default_vault",
    "enable_terminal",
    "specialist_max_concurrency",
    "specialist_cost_alert_threshold",
    "telegram_transport",
])
def test_run_script_null_guards_every_option_read(option):
    """#291: each bashio::config read is followed by a `"null"` guard on
    the temp var within its handling block (3 lines)."""
    script = _read_run_script()
    lines = script.splitlines()
    read_idxs = [
        i for i, line in enumerate(lines)
        if f"bashio::config '{option}'" in line
    ]
    assert read_idxs, f"svc-casa/run never reads option {option!r}"
    for i in read_idxs:
        read_line = lines[i].strip()
        var = read_line.split("=", 1)[0].strip()
        window = "\n".join(lines[i:i + 4])
        assert (
            f'"${var}" = "null"' in window
            or f'"${var}" != "null"' in window
        ), (
            f"read of {option!r} into {var} at line {i + 1} has no null "
            f"guard on THAT var in its handling block:\n{window}"
        )
