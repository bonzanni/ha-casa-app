"""#233/#224 (v0.119.0): `delegate_to_agent(mode=...)` is a closed enum.

ROOT CAUSE, confirmed by production telemetry on 2026-07-25: the model emitted
a `mode` value that was not exactly ``"sync"``. `validate_voice_handoff_static`
strict-compares against ``"sync"``, so that single token silently passed
through the ENTIRE concierge voice-handoff policy:

  * no background hand-off was created,
  * the channel's fixed spoken acknowledgement ("I will ask <specialist>.")
    therefore never fired  -> #233's dead air, and
  * the delegation fell into the ordinary sync path, where the ~16s remaining
    voice budget killed it  -> #224's "no answer".

The live line showed `mode_is_sync=False` while EVERY other predicate was
correct (`role_matches=True`, `channel_is_voice=True`, `transport=ws`,
`route_id=True`, all three capabilities, `route_available=True`) — i.e. the
route was fully capable and the designed hand-off would have run. One bad
token defeated the whole feature.

These tests pin the two-layer fix: the schema makes an unrecognised value
impossible, and the handler refuses to carry one through if it ever arrives.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


class TestModeSchema:
    """Layer 1: the MCP input validator rejects a bad mode before the handler."""

    def test_mode_is_a_closed_enum(self):
        import tools

        schema = tools.delegate_to_agent.input_schema
        assert schema.get("type") == "object"
        mode = schema["properties"]["mode"]
        assert mode.get("enum") == ["sync", "async", "interactive"], (
            "mode must be a CLOSED enum — a free-form string is what let the "
            "model bypass the voice hand-off policy in production"
        )

    def test_required_keys_are_unchanged(self):
        """The old {key: type} shorthand marked EVERY key required; switching
        to an explicit schema must not quietly relax that."""
        import tools

        schema = tools.delegate_to_agent.input_schema
        assert set(schema["required"]) == {"agent", "task", "context", "mode"}

    def test_every_mode_the_code_branches_on_is_accepted(self):
        """The enum must not be narrower than the modes the handler + prelaunch
        actually implement, or a legitimate call becomes unreachable."""
        import tools

        allowed = set(tools.delegate_to_agent.input_schema["properties"]["mode"]["enum"])
        assert allowed == tools._KNOWN_MODES


class TestHandlerCoercion:
    """Layer 2: defence in depth if an unrecognised value ever reaches us."""

    async def _call(self, mode_value, caplog):
        """Drive the handler far enough to observe the normalized mode.

        `_prelaunch` is stubbed to abort right after mode handling, so the test
        stays on the pure argument-normalization path.
        """
        import tools as tools_mod

        seen = {}

        async def _fake_prelaunch(agent_name, origin, mode, task_text, context_text):
            seen["mode"] = mode
            return agent_name, None, None, None, tools_mod._result({"status": "error"})

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tools_mod, "_prelaunch", _fake_prelaunch)
            with caplog.at_level(logging.WARNING, logger="tools"):
                await tools_mod.delegate_to_agent.handler({
                    "agent": "mtg", "task": "t", "context": "",
                    "mode": mode_value,
                })
        return seen.get("mode")

    async def test_unrecognised_mode_is_coerced_to_sync(self, caplog):
        """THE production bug: an unrecognised value must NOT be carried
        through — carrying it through is what bypassed the policy."""
        assert await self._call("synchronous", caplog) == "sync"
        assert any("unrecognised mode" in r.getMessage()
                   for r in caplog.records), "the coercion must be loud"

    async def test_coercion_never_echoes_the_model_supplied_value(self, caplog):
        leak = "sync; and the user's private question was ..."
        await self._call(leak, caplog)
        blob = " ".join(r.getMessage() for r in caplog.records)
        assert "private question" not in blob

    @pytest.mark.parametrize("mode", ["sync", "async", "interactive"])
    async def test_known_modes_pass_through_untouched(self, mode, caplog):
        assert await self._call(mode, caplog) == mode
        assert not [r for r in caplog.records
                    if "unrecognised mode" in r.getMessage()]

    async def test_missing_mode_still_defaults_to_sync(self, caplog):
        """Absent/empty keeps the long-standing default."""
        assert await self._call("", caplog) == "sync"

    @pytest.mark.parametrize("hostile", [["sync"], {"mode": "sync"}, 7, True])
    async def test_non_string_modes_do_not_raise(self, hostile, caplog):
        """Sol/Terra: the fallback must be TOTAL over arbitrary JSON. The
        paths that don't run the schema validator first can carry any type, and
        a truthy UNHASHABLE value (['sync']) raises TypeError on a bare
        membership test — turning a diagnostic into an outage."""
        assert await self._call(hostile, caplog) == "sync"


class TestBypassIsClosed:
    """The point of the fix: with a valid "sync" on a CAPABLE route, the
    concierge delegation is normalized to the background hand-off — the path
    that speaks "I will ask <specialist>." immediately and delivers the answer
    out-of-band instead of racing the voice budget."""

    def _capable_origin(self):
        return {
            "role": "concierge",
            "execution_role": "concierge",
            "channel": "voice",
            "voice_transport": "ws",
            "voice_route_id": "route-1",
            "origin_device_id": "device-1",
            "voice_route_capabilities": frozenset({
                "background_jobs", "endpoint_delivery", "voice_handoff"}),
            # Route protocol 3 (#233/#224): capability is a property of the
            # ENDPOINT that asked. A capable route alone no longer authorizes
            # a deferred answer — the per-utterance offer does.
            "voice_delivery_offer": {
                "modality": "audio", "receipt": "playback_complete"},
            "_voice_handoff_reservation": MagicMock(
                reserve=MagicMock(), release=MagicMock(), commit=MagicMock()),
        }

    def test_sync_on_a_capable_route_becomes_the_handoff(self, monkeypatch):
        import tools as tools_mod

        cfg = MagicMock()
        cfg.delegates = [MagicMock(agent="mtg")]
        monkeypatch.setattr(tools_mod, "_agent_role_map", {"concierge": cfg})
        monkeypatch.setattr(tools_mod, "_runtime", None, raising=False)

        mode, reservation, err = tools_mod.validate_voice_handoff_static(
            "mtg", self._capable_origin(), "sync")

        assert err is None
        assert mode == "async", (
            "a capable route must normalize to the background hand-off — this "
            "is the path production never reached because `mode` wasn't 'sync'"
        )
        assert reservation is not None

    def test_the_production_value_would_have_bypassed_it(self, monkeypatch):
        """Same origin, only the mode differs: this is exactly what happened
        live, and is what the schema + coercion now prevent."""
        import tools as tools_mod

        cfg = MagicMock()
        cfg.delegates = [MagicMock(agent="mtg")]
        monkeypatch.setattr(tools_mod, "_agent_role_map", {"concierge": cfg})

        mode, reservation, err = tools_mod.validate_voice_handoff_static(
            "mtg", self._capable_origin(), "synchronous")

        assert (mode, reservation, err) == ("synchronous", None, None)
