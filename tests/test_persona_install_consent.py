"""#662 red case, persona sibling — specified by the red-case reviewer at
1c8033bb and frozen once accepted.

The specialist and persona install-consent finish hooks are separately
implemented copies of the same shape, and a specialist-only fix has been a
convicted omission here before. This module pins the persona half of the same
contract: the approval edit is SELECTED from the reconciliation outcome rather
than written before it, and only a literal ``True`` may select the success
text.
"""
from types import SimpleNamespace

import pytest

from persona_install_consent import prompt_persona_install_consent

_SUCCESS = (
    "✅ Approved — requested an automatic configurator continuation for 'judge'"
)
_CORRECTIVE = (
    "⚠️ Approved and saved — but the install of 'judge' was not "
    "started automatically. Start a new configurator engagement and re-run "
    "the install; the approval recorded for this exact version is reused "
    "if it still applies."
)


def _inspection() -> SimpleNamespace:
    return SimpleNamespace(
        persona_id="judge", version="1.0.0",
        checksum="sha256:" + "a" * 64, display_name="MTG Judge",
    )


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (True, _SUCCESS),
        (False, _CORRECTIVE),
        (None, _CORRECTIVE),
        (1, _CORRECTIVE),
    ],
    ids=["true", "false", "none", "truthy-one"],
)
async def test_662_only_a_literal_true_outcome_selects_the_success_edit(
    outcome, expected,
) -> None:
    class _Coordinator:
        def register_challenge(self, key, **kwargs):
            self.on_commit_sync = kwargs["on_commit_sync"]
            self.finish_factory = kwargs["finish_factory"]
            return SimpleNamespace(created=True)

    class _Acks:
        def __init__(self):
            self.records = []

        def revocation_generations(self, *, persona_id, version):
            return (0, 0)

        def record(self, **kwargs):
            self.records.append(kwargs)
            return True

    class _Channel:
        def __init__(self):
            self.edits = []

        async def edit_dm_message(self, chat_id, message_id, text):
            self.edits.append((chat_id, message_id, text))

    coordinator = _Coordinator()
    acks = _Acks()
    channel = _Channel()
    reconcile_calls = []

    async def _reconcile_cb():
        reconcile_calls.append(True)
        return outcome

    prompt_persona_install_consent(
        coordinator=coordinator, channel=channel, chat_id=701, operator_id=701,
        inspection=_inspection(), acks=acks, reconcile_cb=_reconcile_cb,
    )

    req = SimpleNamespace(meta={})
    coordinator.on_commit_sync(0, req.meta)
    finish = coordinator.finish_factory(88, req)
    await finish({"outcome": "answered", "option_index": 0})

    actual = (
        len(acks.records), len(reconcile_calls), len(channel.edits),
        channel.edits[0][2],
    )
    assert actual == (1, 1, 1, expected), f"persona consent facts: {actual!r}"


async def test_662_a_raising_callback_is_contained_and_selects_the_corrective_edit(
) -> None:
    """The callback contract is never-raise, so the hook's own try/except is
    defence in depth — and defence in depth nobody tests is defence nobody
    has. A raise must not propagate out of the tap-callback finish hook, and
    must not be laundered into a success edit."""
    class _Coordinator:
        def register_challenge(self, key, **kwargs):
            self.on_commit_sync = kwargs["on_commit_sync"]
            self.finish_factory = kwargs["finish_factory"]
            return SimpleNamespace(created=True)

    class _Acks:
        def __init__(self):
            self.records = []

        def revocation_generations(self, *, persona_id, version):
            return (0, 0)

        def record(self, **kwargs):
            self.records.append(kwargs)
            return True

    class _Channel:
        def __init__(self):
            self.edits = []

        async def edit_dm_message(self, chat_id, message_id, text):
            self.edits.append((chat_id, message_id, text))

    coordinator = _Coordinator()
    acks = _Acks()
    channel = _Channel()

    async def _reconcile_cb():
        raise RuntimeError("contract violation")

    prompt_persona_install_consent(
        coordinator=coordinator, channel=channel, chat_id=701, operator_id=701,
        inspection=_inspection(), acks=acks, reconcile_cb=_reconcile_cb,
    )
    req = SimpleNamespace(meta={})
    coordinator.on_commit_sync(0, req.meta)
    finish = coordinator.finish_factory(88, req)
    await finish({"outcome": "answered", "option_index": 0})  # must not raise

    actual = (len(acks.records), len(channel.edits), channel.edits[0][2])
    assert actual == (1, 1, _CORRECTIVE), f"raise facts: {actual!r}"


async def test_662_an_absent_callback_selects_the_corrective_edit() -> None:
    """No callback means no continuation was ever requested — the DM must not
    claim one was."""
    class _Coordinator:
        def register_challenge(self, key, **kwargs):
            self.on_commit_sync = kwargs["on_commit_sync"]
            self.finish_factory = kwargs["finish_factory"]
            return SimpleNamespace(created=True)

    class _Acks:
        def __init__(self):
            self.records = []

        def revocation_generations(self, *, persona_id, version):
            return (0, 0)

        def record(self, **kwargs):
            self.records.append(kwargs)
            return True

    class _Channel:
        def __init__(self):
            self.edits = []

        async def edit_dm_message(self, chat_id, message_id, text):
            self.edits.append((chat_id, message_id, text))

    coordinator = _Coordinator()
    acks = _Acks()
    channel = _Channel()
    prompt_persona_install_consent(
        coordinator=coordinator, channel=channel, chat_id=701, operator_id=701,
        inspection=_inspection(), acks=acks,
    )
    req = SimpleNamespace(meta={})
    coordinator.on_commit_sync(0, req.meta)
    finish = coordinator.finish_factory(88, req)
    await finish({"outcome": "answered", "option_index": 0})

    actual = (len(acks.records), len(channel.edits), channel.edits[0][2])
    assert actual == (1, 1, _CORRECTIVE), f"absent-callback facts: {actual!r}"
