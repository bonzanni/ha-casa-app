"""Recall rendering for ``automation`` hits (#204).

Without its own branch an automation hit falls into the renderer's final
``else``, which announces it as "A prior Casa model output" — attributing
third-party webhook content to Casa itself, the exact misattribution the
provenance work exists to prevent.
"""


def _hit(provenance, text="the sensor fired"):
    from personality_types import RecallHit

    return RecallHit(
        text=text, memory_type="observation", sensitivity="public",
        application_tags=(), provenance=provenance, backend_id=None,
        document_id=None, chunk_id=None, source_fact_ids=None,
        metadata=None, context=None, score=None,
    )


def _render(provenance, *, clearance="private", surface="text"):
    from personality_types import SpeakerProvenance
    from recall_renderer import render_recall

    return render_recall(
        [_hit(provenance)],
        current_speaker=SpeakerProvenance(speaker_kind="system"),
        surface=surface, clearance=clearance, token_budget=1000,
    )


class TestAutomationAttribution:
    def test_an_automation_is_not_announced_as_a_person(self):
        from personality_types import SpeakerProvenance

        rendered = _render(SpeakerProvenance(
            speaker_kind="automation", user_peer="webhook:nightly"))
        assert "user" not in rendered.lower()

    def test_an_automation_is_not_announced_as_casa_itself(self):
        from personality_types import SpeakerProvenance

        rendered = _render(SpeakerProvenance(
            speaker_kind="automation", user_peer="webhook:nightly"))
        assert "Casa model output" not in rendered

    def test_an_automation_is_named_as_an_automation(self):
        from personality_types import SpeakerProvenance

        rendered = _render(SpeakerProvenance(
            speaker_kind="automation", user_peer="webhook:nightly"))
        assert "automation" in rendered.lower()
        assert "the sensor fired" in rendered

    def test_the_trigger_name_is_never_disclosed_even_at_private(self):
        # user_peer is deliberately outside ProvenanceView: a trigger name is
        # Casa's own routing detail, and webhook content is untrusted, so the
        # model is told THAT an automation spoke, never which one.
        from personality_types import SpeakerProvenance

        rendered = _render(SpeakerProvenance(
            speaker_kind="automation", user_peer="webhook:plg-acme--secret_flow"))
        assert "plg-acme" not in rendered
        assert "secret_flow" not in rendered

    def test_a_restricted_webhook_surface_still_renders_it(self):
        from personality_types import SpeakerProvenance

        rendered = _render(
            SpeakerProvenance(speaker_kind="automation", user_peer="webhook:x"),
            clearance="public", surface="restricted_webhook",
        )
        assert "automation" in rendered.lower()
