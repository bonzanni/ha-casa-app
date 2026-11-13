# tests/test_channel_policy.py
"""The write-trust rule: which channels may persist facts to the trusted bank."""
import pytest

from channel_policy import writes_to_bank

pytestmark = [pytest.mark.unit]


def test_voice_is_recall_only():
    assert writes_to_bank("voice") is False


def test_authenticated_channels_write():
    assert writes_to_bank("telegram") is True


def test_unknown_channel_does_not_write():
    # Leak-safe default: an unrecognised channel is treated as untrusted-to-write.
    assert writes_to_bank("some-future-surface") is False


def test_the_guest_channel_does_not_write():
    # A friend's summons in a group: nothing said there is captured.
    from guest_run import GUEST_CHANNEL
    assert GUEST_CHANNEL == "telegram_guest"
    assert writes_to_bank(GUEST_CHANNEL) is False
