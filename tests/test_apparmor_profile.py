"""The app's AppArmor profile must let Casa signal an engagement's processes.

#1382: Casa and s6 run as root, an engagement's CLI runs as its own uid, and
signalling another uid needs CAP_KILL. The profile did not grant it, so on
HAOS every kill of a running engagement was refused and swallowed; the quiesce
ladder could only report the uid NOT verified extinct. No unit or e2e run loads
the profile, so this pin is the only thing that notices the grant going away.
"""
from __future__ import annotations

import re
from pathlib import Path

PROFILE = Path(__file__).resolve().parents[1] / "casa" / "apparmor.txt"


def _rules() -> list[str]:
    return [line.split("#", 1)[0].strip()
            for line in PROFILE.read_text(encoding="utf-8").splitlines()]


def test_the_profile_lets_casa_signal_an_engagement_uid() -> None:
    rules = _rules()
    assert "capability kill," in rules
    # The signal rule must still allow the signals the ladder and s6 send.
    sig = [r for r in rules if r.startswith("signal")]
    assert sig, "the profile has no signal rule"
    allowed = set(re.findall(r"[a-z]+", sig[0].split("set=", 1)[1]))
    assert {"kill", "term", "cont"} <= allowed
