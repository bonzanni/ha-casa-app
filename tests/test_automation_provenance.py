"""The ``automation`` speaker kind (#204).

A `/invoke` or `/webhook/{name}` turn is authored by a machine holding a shared
secret, not by a human. Recording it as ``speaker_kind="user"`` would be a
category error — the bearer credential proves authorization, never authorship —
and recording it as ``system`` would falsely claim Casa's own internal
authority. ``automation`` is the honest third thing: a known, non-human,
externally-originated author.
"""

import pytest


class TestAutomationValidation:
    def test_automation_requires_a_user_peer(self):
        from personality_types import SpeakerProvenance
        from speaker_provenance import validate_speaker_provenance

        with pytest.raises(ValueError, match="user_peer is required"):
            validate_speaker_provenance(
                SpeakerProvenance(speaker_kind="automation"),
            )

    def test_automation_with_a_peer_is_valid(self):
        from personality_types import SpeakerProvenance
        from speaker_provenance import validate_speaker_provenance

        validate_speaker_provenance(
            SpeakerProvenance(speaker_kind="automation", user_peer="invoke_caller"),
        )

    def test_automation_cannot_carry_a_human_user_id(self):
        # user_id is what promotes a provenance to a NAMED person. A shared
        # secret identifies no human,
        # so an automation must never be able to acquire one.
        from personality_types import SpeakerProvenance
        from speaker_provenance import validate_speaker_provenance

        with pytest.raises(ValueError, match="automation identity"):
            validate_speaker_provenance(
                SpeakerProvenance(
                    speaker_kind="automation", user_peer="invoke_caller",
                    user_id="nicola-42",
                ),
            )

    def test_automation_cannot_carry_a_display_name(self):
        from personality_types import SpeakerProvenance
        from speaker_provenance import validate_speaker_provenance

        with pytest.raises(ValueError, match="automation identity"):
            validate_speaker_provenance(
                SpeakerProvenance(
                    speaker_kind="automation", user_peer="webhook:nightly",
                    display_name="Nicola",
                ),
            )

    def test_automation_cannot_carry_agent_fields(self):
        from personality_types import SpeakerProvenance
        from speaker_provenance import validate_speaker_provenance

        with pytest.raises(ValueError, match="automation identity"):
            validate_speaker_provenance(
                SpeakerProvenance(
                    speaker_kind="automation", user_peer="invoke_caller",
                    role_id="resident:ellen",
                ),
            )

    def test_automation_peer_obeys_the_field_bound(self):
        from personality_types import SpeakerProvenance
        from speaker_provenance import validate_speaker_provenance

        with pytest.raises(ValueError, match="user_peer exceeds"):
            validate_speaker_provenance(
                SpeakerProvenance(
                    speaker_kind="automation", user_peer="webhook:" + "n" * 300,
                ),
            )

    def test_automation_survives_a_provenance_mapping_round_trip(self):
        from personality_types import SpeakerProvenance
        from speaker_provenance import provenance_from_mapping, provenance_mapping

        value = SpeakerProvenance(
            speaker_kind="automation", user_peer="webhook:plg-elevenlabs--call_ended",
        )
        assert provenance_from_mapping(provenance_mapping(value)) == value


class TestFromOriginDerivesTheKind:
    def _origin(self, route, clearance="private"):
        from personality_types import TrustedOrigin
        return TrustedOrigin(
            route=route, is_authenticated=True, clearance=clearance,
        )

    def test_invoke_surface_yields_automation(self):
        from speaker_provenance import UserProvenance

        value = UserProvenance.from_origin(
            surface="invoke", server_origin=self._origin("invoke"),
            authenticated_user=None, user_peer="invoke_caller",
        )
        assert value.speaker_kind == "automation"
        assert value.user_peer == "invoke_caller"
        assert value.user_id is None

    def test_webhook_surface_yields_automation(self):
        from speaker_provenance import UserProvenance

        value = UserProvenance.from_origin(
            surface="webhook", server_origin=self._origin("webhook", "public"),
            authenticated_user=None, user_peer="webhook:nightly",
        )
        assert value.speaker_kind == "automation"
        assert value.user_peer == "webhook:nightly"

    def test_telegram_surface_still_yields_user(self):
        from personality_types import AuthenticatedUser
        from speaker_provenance import UserProvenance

        value = UserProvenance.from_origin(
            surface="telegram", server_origin=self._origin("telegram"),
            authenticated_user=AuthenticatedUser(
                stable_id="42", configured_display_name="Nicola",
            ),
            user_peer="nicola",
        )
        assert value.speaker_kind == "user"
        assert value.user_id == "42"

    def test_voice_surface_still_yields_the_anonymous_user(self):
        from speaker_provenance import UserProvenance

        value = UserProvenance.from_origin(
            surface="voice", server_origin=self._origin("voice", "friends"),
            authenticated_user=None, user_peer="voice_speaker",
        )
        assert value.speaker_kind == "user"
        assert value.user_peer == "voice_speaker"

    def test_an_automation_surface_rejects_an_authenticated_user(self):
        # A shared bearer secret carries no per-caller identity, so a caller
        # claiming one is a wiring bug — fail loudly rather than mint a named
        # person out of a webhook.
        from personality_types import AuthenticatedUser
        from speaker_provenance import UserProvenance

        with pytest.raises(ValueError, match="automation surface"):
            UserProvenance.from_origin(
                surface="invoke", server_origin=self._origin("invoke"),
                authenticated_user=AuthenticatedUser(
                    stable_id="42", configured_display_name="Nicola",
                ),
                user_peer="invoke_caller",
            )

    def test_automation_surface_still_requires_a_peer(self):
        from speaker_provenance import UserProvenance

        with pytest.raises(ValueError, match="user_peer is required"):
            UserProvenance.from_origin(
                surface="webhook", server_origin=self._origin("webhook"),
                authenticated_user=None, user_peer="",
            )
