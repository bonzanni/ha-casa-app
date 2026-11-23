"""Minimal Home Assistant module surface for the real Casa integration.

Keep ``HA_STUB_EXPORTS`` byte-for-byte equivalent to the integration test
suite's manifest.  The cross-repository E2E test compares the two manifests
before importing production integration modules.
"""

from __future__ import annotations

import sys
import types
import uuid
from dataclasses import dataclass, field
from unittest.mock import MagicMock


HA_STUB_EXPORTS = frozenset({
    "homeassistant.core:callback", "homeassistant.core:HomeAssistant",
    "homeassistant.core:Event", "homeassistant.core:EventStateChangedData",
    "homeassistant.config_entries:ConfigFlow",
    "homeassistant.config_entries:ConfigSubentryFlow",
    "homeassistant.config_entries:OptionsFlow",
    "homeassistant.config_entries:ConfigEntry",
    "homeassistant.config_entries:ConfigSubentry",
    "homeassistant.config_entries:ConfigSubentryData",
    "homeassistant.config_entries:ConfigFlowResult",
    "homeassistant.const:Platform", "homeassistant.const:MATCH_ALL",
    "homeassistant.const:EVENT_STATE_CHANGED",
    "homeassistant.exceptions:ConfigEntryAuthFailed",
    "homeassistant.exceptions:ConfigEntryNotReady",
    "homeassistant.helpers.aiohttp_client:async_get_clientsession",
    "homeassistant.helpers.event:async_track_state_change_event",
    "homeassistant.helpers.event:TrackStates",
    "homeassistant.helpers.event:async_track_state_change_filtered",
    "homeassistant.helpers.entity_registry:EVENT_ENTITY_REGISTRY_UPDATED",
    "homeassistant.helpers.entity_registry:async_get",
    "homeassistant.helpers.entity_registry:async_entries_for_device",
    "homeassistant.helpers.service_info.hassio:HassioServiceInfo",
    "homeassistant.helpers.device_registry:DeviceInfo",
    "homeassistant.helpers.device_registry:DeviceEntryType",
    "homeassistant.helpers.intent:IntentResponse",
    "homeassistant.helpers.intent:IntentResponseErrorCode",
    "homeassistant.components.conversation:ConversationEntity",
    "homeassistant.components.conversation:ConversationInput",
    "homeassistant.components.conversation:ConversationResult",
    "homeassistant.components.conversation:ChatLog",
    "homeassistant.components.conversation:async_get_result_from_chat_log",
    "homeassistant.components.conversation.chat_log:ChatLog",
    "homeassistant.components.assist_satellite:AssistSatelliteState",
    "homeassistant:core", "homeassistant:config_entries",
    "homeassistant:const", "homeassistant:exceptions", "homeassistant:helpers",
    "homeassistant:components", "homeassistant.helpers:aiohttp_client",
    "homeassistant.helpers:event", "homeassistant.helpers:entity_registry",
    "homeassistant.helpers:service_info",
    "homeassistant.helpers:device_registry", "homeassistant.helpers:intent",
    "homeassistant.helpers.service_info:hassio",
    "homeassistant.components:conversation",
    "homeassistant.components:assist_satellite",
})


def _module(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


def install() -> frozenset[str]:
    """Install the exact integration-test HA surface and return its manifest."""
    if "homeassistant" in sys.modules:
        return HA_STUB_EXPORTS

    ha = _module("homeassistant")
    core = _module("homeassistant.core")
    core.callback = lambda function: function
    core.HomeAssistant = MagicMock
    core.Event = MagicMock
    core.EventStateChangedData = dict

    config_entries = _module("homeassistant.config_entries")

    class ConfigFlowResult(dict):
        pass

    class ConfigFlow:
        def __init_subclass__(cls, domain=None, **kwargs):
            super().__init_subclass__(**kwargs)
            if domain is not None:
                cls._domain = domain

        def __init__(self):
            self.hass = None
            self.context = {}

        def async_show_form(self, *, step_id, data_schema=None, errors=None, **kwargs):
            return ConfigFlowResult(
                type="form", step_id=step_id, data_schema=data_schema,
                errors=errors or {},
            )

        def async_create_entry(self, *, title, data, **kwargs):
            return ConfigFlowResult(type="create_entry", title=title, data=data)

        def async_abort(self, *, reason):
            return ConfigFlowResult(type="abort", reason=reason)

        async def async_set_unique_id(self, unique_id):
            return None

        def _abort_if_unique_id_configured(self):
            return None

        def _get_reauth_entry(self):
            return MagicMock()

        def async_update_reload_and_abort(self, entry, *, data_updates=None, **kwargs):
            return ConfigFlowResult(type="abort", reason="reauth_successful")

    class OptionsFlow:
        def __init__(self):
            self.hass = None
            self.config_entry = MagicMock(options={})

        def async_show_form(self, *, step_id, data_schema=None, errors=None, **kwargs):
            return ConfigFlowResult(
                type="form", step_id=step_id, data_schema=data_schema,
                errors=errors or {},
            )

        def async_create_entry(self, *, data, **kwargs):
            return ConfigFlowResult(type="create_entry", data=data)

        def add_suggested_values_to_schema(self, schema, suggested_values):
            return schema

    config_entries.ConfigFlow = ConfigFlow
    config_entries.OptionsFlow = OptionsFlow
    config_entries.ConfigEntry = MagicMock

    @dataclass(frozen=True)
    class ConfigSubentry:
        data: dict
        subentry_type: str
        title: str
        unique_id: str | None = None
        subentry_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    class ConfigSubentryFlow:
        """Present so the integration imports; no flow runs in this test."""

    config_entries.ConfigSubentry = ConfigSubentry
    config_entries.ConfigSubentryData = dict
    config_entries.ConfigSubentryFlow = ConfigSubentryFlow
    config_entries.ConfigFlowResult = ConfigFlowResult

    const = _module("homeassistant.const")
    const.Platform = MagicMock(CONVERSATION="conversation")
    const.MATCH_ALL = "*"
    const.EVENT_STATE_CHANGED = "state_changed"

    exceptions = _module("homeassistant.exceptions")
    exceptions.ConfigEntryAuthFailed = type("ConfigEntryAuthFailed", (Exception,), {})
    exceptions.ConfigEntryNotReady = type("ConfigEntryNotReady", (Exception,), {})

    helpers = _module("homeassistant.helpers")
    aiohttp_client = _module("homeassistant.helpers.aiohttp_client")
    aiohttp_client.async_get_clientsession = MagicMock(return_value=MagicMock())
    event = _module("homeassistant.helpers.event")
    event.async_track_state_change_event = MagicMock(return_value=lambda: None)

    class TrackStates:
        def __init__(self, all_states, entities, domains):
            self.all_states = all_states
            self.entities = entities
            self.domains = domains

    event.TrackStates = TrackStates
    event.async_track_state_change_filtered = MagicMock(return_value=MagicMock())
    entity_registry = _module("homeassistant.helpers.entity_registry")
    entity_registry.EVENT_ENTITY_REGISTRY_UPDATED = "entity_registry_updated"
    entity_registry.async_get = MagicMock(return_value=MagicMock())
    entity_registry.async_entries_for_device = lambda *a, **kw: []
    service_info = _module("homeassistant.helpers.service_info")
    service_info_hassio = _module("homeassistant.helpers.service_info.hassio")

    class HassioServiceInfo:
        def __init__(self, config, name, slug, uuid):
            self.config = config
            self.name = name
            self.slug = slug
            self.uuid = uuid

    service_info_hassio.HassioServiceInfo = HassioServiceInfo
    device_registry = _module("homeassistant.helpers.device_registry")
    device_registry.DeviceInfo = dict
    device_registry.DeviceEntryType = MagicMock(SERVICE="service")
    intent = _module("homeassistant.helpers.intent")

    class IntentResponse:
        def __init__(self, language=None):
            self.language = language
            self._speech = {}
            self._error = None

        def async_set_speech(self, speech, speech_type="plain", extra_data=None):
            self._speech = {"speech": speech, "type": speech_type, "extra": extra_data}

        def async_set_error(self, code, message):
            self._error = (code, message)

    class IntentResponseErrorCode:
        FAILED_TO_HANDLE = "failed_to_handle"
        NO_INTENT_MATCH = "no_intent_match"

    intent.IntentResponse = IntentResponse
    intent.IntentResponseErrorCode = IntentResponseErrorCode

    components = _module("homeassistant.components")
    conversation = _module("homeassistant.components.conversation")
    conversation.ConversationEntity = type(
        "ConversationEntity",
        (),
        {
            "_attr_has_entity_name": False,
            "_attr_name": None,
            "_attr_unique_id": None,
            "unique_id": property(lambda self: getattr(self, "_attr_unique_id", None)),
        },
    )
    conversation.ConversationInput = MagicMock
    conversation.ConversationResult = MagicMock
    conversation.ChatLog = MagicMock
    conversation.async_get_result_from_chat_log = lambda user_input, chat_log: {
        "type": "result",
        "conversation_id": getattr(user_input, "conversation_id", None),
    }
    conversation_chat_log = _module(
        "homeassistant.components.conversation.chat_log",
    )
    conversation_chat_log.ChatLog = conversation.ChatLog
    assist_satellite = _module("homeassistant.components.assist_satellite")

    class AssistSatelliteState:
        LISTENING = type("_State", (), {"value": "listening"})()
        IDLE = type("_State", (), {"value": "idle"})()
        PROCESSING = type("_State", (), {"value": "processing"})()
        RESPONDING = type("_State", (), {"value": "responding"})()

    assist_satellite.AssistSatelliteState = AssistSatelliteState

    ha.core = core
    ha.config_entries = config_entries
    ha.const = const
    ha.exceptions = exceptions
    ha.helpers = helpers
    ha.components = components
    helpers.aiohttp_client = aiohttp_client
    helpers.event = event
    helpers.entity_registry = entity_registry
    helpers.service_info = service_info
    helpers.device_registry = device_registry
    helpers.intent = intent
    service_info.hassio = service_info_hassio
    components.conversation = conversation
    components.assist_satellite = assist_satellite
    return HA_STUB_EXPORTS


def validate_exports(exports: frozenset[str] = HA_STUB_EXPORTS) -> None:
    """Prove the manifest names are present, not merely textually equal."""
    missing = []
    for declaration in sorted(exports):
        module_name, separator, attribute = declaration.partition(":")
        module = sys.modules.get(module_name)
        if module is None:
            missing.append(declaration)
        elif separator and not hasattr(module, attribute):
            missing.append(declaration)
    if missing:
        raise AssertionError(f"HA stub manifest exports are missing: {missing}")


__all__ = ["HA_STUB_EXPORTS", "install", "validate_exports"]
