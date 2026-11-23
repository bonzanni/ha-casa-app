"""Operator configuration for proactive voice-job delivery."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml

from job_registry import DeliveryState, ExecutionState, JobRegistry, VoiceJob
from personality_types import SpeakerProvenance
import tools


pytestmark = pytest.mark.unit

_SYSTEM_SPEAKER = SpeakerProvenance(speaker_kind="system")

def _load_config_module():
    try:
        return importlib.import_module("voice_delivery_config")
    except ModuleNotFoundError:
        pytest.fail("voice_delivery_config module is absent")


def test_voice_delivery_config_is_constant():
    config = _load_config_module()

    assert config.ROUTE_FRESHNESS_S == 60
    assert config.DELIVERY_TTL_S == 900
    assert config.ROUTE_CAP == 5


def test_env_cannot_override_the_delivery_constants(monkeypatch):
    """Regression guard for #228: these were operator options read from env.

    A stale env var on an upgraded install must not resurrect them — the
    clamping rails that used to bound the option values are gone with it, so
    an honoured override would have nothing bounding it.
    """
    for key, value in (
        ("VOICE_ROUTE_FRESHNESS_SECONDS", "301"),
        ("VOICE_JOB_DELIVERY_TTL_SECONDS", "3601"),
        ("VOICE_JOB_ROUTE_CAP", "21"),
    ):
        monkeypatch.setenv(key, value)

    config = importlib.reload(_load_config_module())

    assert (
        config.ROUTE_FRESHNESS_S,
        config.DELIVERY_TTL_S,
        config.ROUTE_CAP,
    ) == (60, 900, 5)


def test_boot_wires_the_constants_into_all_voice_delivery_consumers():
    source = (
        Path(__file__).resolve().parent.parent
        / "casa" / "rootfs" / "opt" / "casa" / "casa_core.py"
    ).read_text(encoding="utf-8")

    assert "result_ttl_seconds=DELIVERY_TTL_S" in source
    assert "freshness_s=ROUTE_FRESHNESS_S" in source
    assert "voice_job_route_cap=ROUTE_CAP" in source


def test_addon_no_longer_declares_the_voice_delivery_options():
    """v0.125.0 (#228): removed from options, schema AND translations.

    Guards all three together because a half-removal is the failure mode: an
    orphan schema entry makes Home Assistant render a control that changes
    nothing, and an orphan translation is invisible until someone re-adds the
    key and gets stale help text.
    """
    root = Path(__file__).resolve().parent.parent / "casa"
    addon = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    translations = yaml.safe_load(
        (root / "translations" / "en.yaml").read_text(encoding="utf-8"))

    for option in (
        "voice_turn_budget_seconds",
        "voice_route_freshness_seconds",
        "voice_job_delivery_ttl_seconds",
        "voice_job_route_cap",
    ):
        assert option not in addon["options"]
        assert option not in addon["schema"]
        assert option not in translations["configuration"]


def _accepted_job(
    *,
    creator_peer: str = "voice",
    origin_route_id: str | None = "route-1",
    origin_device_id: str | None = "device-1",
    execution_state: ExecutionState = ExecutionState.ACCEPTED,
) -> VoiceJob:
    return VoiceJob(
        id="job-1",
        parent_job_id=None,
        creating_speaker=_SYSTEM_SPEAKER, executing_speaker=_SYSTEM_SPEAKER,
        creating_role="concierge",
        specialist_role="judge",
        specialist_display_name="Judge",
        creator_peer=creator_peer,
        creator_user_id=None,
        scope_id="scope-1",
        origin_route_id=origin_route_id,
        origin_device_id=origin_device_id,
        task="Question",
        context="",
        created_at=100.0,
        started_at=None,
        terminal_at=None,
        expires_at=None,
        execution_state=execution_state,
        delivery_state=DeliveryState.NONE,
        result=None,
        failure=None,
        awaiting_input=False,
        continuable_until=None,
        delivery_sequence=0,
        delivery_attempt_id=None,
        lease_until=None,
        cancel_pending=False,
    )


@pytest.mark.asyncio
async def test_configured_result_ttl_reaches_durable_job_expiry(tmp_path):
    registry = JobRegistry(
        tmp_path / "jobs.json",
        clock=lambda: 100.0,
        result_ttl_seconds=900,
    )
    await registry.load()
    await registry.create(_accepted_job())

    completed = await registry.finish("job-1", "Done")

    assert completed.expires_at == 1000.0


@pytest.mark.asyncio
async def test_configured_result_ttl_caps_specialist_requested_delivery_ttl(
    tmp_path,
):
    registry = JobRegistry(
        tmp_path / "jobs.json",
        clock=lambda: 100.0,
        result_ttl_seconds=300,
    )
    await registry.load()
    await registry.create(_accepted_job())

    completed = await registry.finish_voice_result(
        "job-1", "Done", awaiting_input=False, delivery_ttl_s=900,
    )

    assert completed.expires_at == 400.0


@pytest.mark.asyncio
async def test_configured_result_ttl_preserves_shorter_specialist_expiry(tmp_path):
    registry = JobRegistry(
        tmp_path / "jobs.json",
        clock=lambda: 100.0,
        result_ttl_seconds=300,
    )
    await registry.load()
    await registry.create(_accepted_job())

    completed = await registry.finish_voice_result(
        "job-1", "Done", awaiting_input=False, delivery_ttl_s=60,
    )

    assert completed.expires_at == 160.0


@pytest.mark.parametrize("transition", ["success", "failure", "cancel", "orphan"])
@pytest.mark.parametrize(
    "provenance",
    [
        {
            "creator_peer": "telegram",
            "origin_route_id": "telegram-route-must-not-count-as-voice",
            "origin_device_id": "telegram-device-must-not-count-as-voice",
        },
        {
            "creator_peer": "voice",
            "origin_route_id": None,
            "origin_device_id": "device-without-route",
        },
        {
            "creator_peer": "voice",
            "origin_route_id": "route-without-device",
            "origin_device_id": None,
        },
    ],
    ids=["telegram-with-route", "voice-without-route", "voice-without-device"],
)
@pytest.mark.asyncio
async def test_voice_ttl_does_not_shorten_non_voice_terminal_retention(
    tmp_path, transition, provenance,
):
    registry = JobRegistry(
        tmp_path / "jobs.json",
        clock=lambda: 100.0,
        result_ttl_seconds=300,
    )
    await registry.load()
    state = (
        ExecutionState.RUNNING
        if transition == "orphan"
        else ExecutionState.ACCEPTED
    )
    await registry.create(_accepted_job(execution_state=state, **provenance))

    if transition == "success":
        completed = await registry.finish("job-1", "Done")
    elif transition == "failure":
        completed = await registry.fail("job-1", RuntimeError("failed"))
    elif transition == "cancel":
        completed = await registry.cancel("job-1")
    else:
        await registry.recover_after_restart()
        completed = registry.get("job-1")

    assert completed is not None
    assert completed.expires_at == 100.0 + JobRegistry.RESULT_TTL_SECONDS


@pytest.mark.parametrize("transition", ["failure", "cancel", "orphan"])
@pytest.mark.asyncio
async def test_configured_voice_ttl_reaches_voice_terminal_paths(
    tmp_path, transition,
):
    registry = JobRegistry(
        tmp_path / "jobs.json",
        clock=lambda: 100.0,
        result_ttl_seconds=300,
    )
    await registry.load()
    state = (
        ExecutionState.RUNNING
        if transition == "orphan"
        else ExecutionState.ACCEPTED
    )
    await registry.create(_accepted_job(execution_state=state))

    if transition == "failure":
        completed = await registry.fail("job-1", RuntimeError("failed"))
    elif transition == "cancel":
        completed = await registry.cancel("job-1")
    else:
        await registry.recover_after_restart()
        completed = registry.get("job-1")

    assert completed is not None
    assert completed.expires_at == 400.0


def test_docs_state_websocket_hmac_security_boundary_exactly():
    docs = " ".join((
        Path(__file__).resolve().parent.parent / "casa" / "DOCS.md"
    ).read_text(encoding="utf-8").split())

    assert "empty HTTP upgrade request body" in docs
    assert "does not authenticate individual WebSocket frames" in docs
    assert "does not encrypt payloads" in docs
    assert "does not cryptographically authenticate the server" in docs


def test_configured_route_cap_reaches_every_atomic_creation_path():
    source = Path(tools.__file__).read_text(encoding="utf-8")

    assert "_MAX_ACTIVE_READY_JOBS_PER_ROUTE" not in source
    assert source.count("max_active_ready_per_route=_voice_job_route_cap") == 3


def test_tools_accept_one_validated_route_cap_snapshot(monkeypatch):
    monkeypatch.setattr(tools, "_voice_job_route_cap", tools._voice_job_route_cap)
    tools.init_tools(None, None, None, voice_job_route_cap=7)

    assert tools._voice_job_route_cap == 7
