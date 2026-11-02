"""Store invariant guardrail (Release A, Layer 5).

Webhook-origin content must never be captured into memory. Rather than
build origin-aware store-deny machinery, Release A relies on the pre-existing
channel write-trust gate (``_WRITABLE_CHANNELS == {"telegram"}``). These tests
PIN that invariant so a future change granting webhook/invoke write-trust
deliberately re-opens the store-deny question instead of silently leaking.
"""
from __future__ import annotations

import pytest

from channel_policy import writes_to_bank

pytestmark = [pytest.mark.unit]


def test_webhook_channel_is_not_writable():
    assert writes_to_bank("webhook") is False
    assert writes_to_bank("telegram") is True
    assert writes_to_bank("voice") is False
