"""#1207 under ruling #1252 (option 2): the sentence the protected-tools
prompt used to offer for a turn waiting on an approval is gone from both
fallback prompts — every word written after the protected call is withheld
now, so offering one is offering nothing.

Red case specified by **astra** (drive redcase round, MODE: SPECIFY, against
``350a9c74c89e9ecb573a67cd53d3be47193083ea``). The sentence crosses a source
line break in both files, so the count runs on whitespace-normalised text: a
literal substring check passes at base and pins nothing.
"""
from __future__ import annotations

from pathlib import Path

import pytest

AGENTS = Path(__file__).resolve().parent.parent / "casa" / "rootfs" / "opt" / "casa" / "defaults" / "agents"
NEEDLE = "I won't run this action without your approval."


@pytest.mark.parametrize("role", ["assistant", "butler"])
def test_protected_prompt_has_no_allowed_sentence(role):
    text = (AGENTS / role / "prompts" / "system.md").read_text(encoding="utf-8")
    assert " ".join(text.split()).count(NEEDLE) == 0
