"""Contract guard: the real python-telegram-bot must render our entities as the
fake stub (and the parser's offsets) assume.

Runs in a SUBPROCESS on purpose — tests/conftest.py installs a fake ``telegram``
stub for the unit session, which would shadow the real ``MessageEntity`` and its
UTF-16 helper. A fresh interpreter sees the real PTB (installed via requirements),
so this pins ``render()``'s astral-offset behavior to reality (the fake's
``adjust_message_entities_to_utf_16`` could otherwise drift from PTB's).
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

REPO = Path(__file__).resolve().parents[1]
CASA = REPO / "casa" / "rootfs" / "opt" / "casa"

_SCRIPT = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {casa!r})
    from telegram import MessageEntity  # REAL ptb
    from channels.tg_richtext import render

    # Astral emoji: codepoint offset 2 must become UTF-16 offset 3.
    display, ents = render("🧾 **hi**")
    assert display == "🧾 hi", display
    assert len(ents) == 1, ents
    assert ents[0].type == MessageEntity.BOLD, ents[0].type
    assert ents[0].offset == 3, ents[0].offset
    assert ents[0].length == 2, ents[0].length

    # Fenced table + inline code render as PRE/CODE with real entity types.
    display, ents = render("```\\nA  1\\nB  2\\n```")
    assert display == "A  1\\nB  2", repr(display)
    assert ents[0].type == MessageEntity.PRE, ents[0].type

    print("OK")
    """
).format(casa=str(CASA))


def test_render_matches_real_ptb():
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert proc.stdout.strip().endswith("OK"), proc.stdout
