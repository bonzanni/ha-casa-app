"""Document-id namespacing for ``automation`` turns (#204).

``user_peer`` is the idempotency key for user turns
(``content_document_id``), but the agent scheme folds role/persona and ignores
``user_peer`` entirely. Routing automations through the agent scheme would
therefore silently DISCARD the per-trigger peer — every trigger sending the
same text would collapse into one document. ``automation`` gets its own
domain-separated id space instead.
"""

import pytest


async def _never_saved(_document_id):
    """#1123: the stored-tier reader for a bank that holds nothing yet."""
    return None


class TestAutomationDocumentId:
    def test_the_peer_is_part_of_the_key(self):
        from hindsight_ids import automation_document_id

        assert automation_document_id("webhook:a", "same text") != (
            automation_document_id("webhook:b", "same text")
        )

    def test_same_peer_and_text_collapse_to_one_document(self):
        from hindsight_ids import automation_document_id

        assert automation_document_id("webhook:a", "hello") == (
            automation_document_id("webhook:a", "hello")
        )

    def test_ids_are_namespaced_away_from_user_and_agent_spaces(self):
        from hindsight_ids import automation_document_id, content_document_id

        automation = automation_document_id("nicola", "hello")
        user = content_document_id("nicola", "hello")
        # Same (peer, text) must NOT collide across the two spaces — that is the
        # whole point of the domain separation.
        assert automation != user
        assert automation.startswith("m-x-")
        assert not automation.startswith("m-a-")

    def test_an_empty_peer_is_rejected(self):
        from hindsight_ids import automation_document_id

        with pytest.raises(ValueError, match="user_peer"):
            automation_document_id("", "hello")


class TestAgentDocumentIdRejectsAutomation:
    def test_agent_scheme_refuses_an_automation_provenance(self):
        # Mis-routing an automation into the agent scheme is exactly the bug
        # this id space exists to prevent, so it must fail loudly.
        from hindsight_ids import agent_document_id
        from personality_types import SpeakerProvenance

        with pytest.raises(ValueError, match="automation"):
            agent_document_id(
                SpeakerProvenance(
                    speaker_kind="automation", user_peer="webhook:nightly",
                ),
                "hello",
            )


class TestRetainItemRouting:
    def _build(self, turns):
        import asyncio

        from memory_provenance import build_retain_items

        async def classify(_text):
            return "public"

        return asyncio.run(build_retain_items(
            turns, classify=classify, stored_tags=_never_saved))

    def test_an_automation_turn_lands_in_the_automation_id_space(self):
        from personality_types import RetainedTurn, SpeakerProvenance

        items = self._build([
            RetainedTurn(
                "the sensor fired",
                SpeakerProvenance(
                    speaker_kind="automation", user_peer="webhook:nightly",
                ),
            ),
        ])
        assert items[0]["document_id"].startswith("m-x-")

    def test_two_triggers_saying_the_same_thing_stay_distinct(self):
        from personality_types import RetainedTurn, SpeakerProvenance

        items = self._build([
            RetainedTurn("the sensor fired", SpeakerProvenance(
                speaker_kind="automation", user_peer="webhook:kitchen")),
            RetainedTurn("the sensor fired", SpeakerProvenance(
                speaker_kind="automation", user_peer="webhook:garage")),
        ])
        assert len({item["document_id"] for item in items}) == 2

    def test_a_user_turn_still_lands_in_the_user_id_space(self):
        from personality_types import RetainedTurn, SpeakerProvenance

        items = self._build([
            RetainedTurn("hello", SpeakerProvenance(
                speaker_kind="user", user_peer="nicola")),
        ])
        document_id = items[0]["document_id"]
        assert document_id.startswith("m-")
        assert not document_id.startswith("m-a-")
        assert not document_id.startswith("m-x-")
