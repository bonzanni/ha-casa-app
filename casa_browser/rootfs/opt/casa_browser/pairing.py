"""Pairing: the one shared secret that makes the app answer only Casa (design §5.8, v9.1).

The operator presses "Pair with Casa" on the app's ingress page (Task 13 wires the button,
behind HA's login and a CSRF check; no ``/v1`` route arms). Arming starts a new
generation, revokes the old secret at once (and, through ``on_revoke``, every run and
live-view grant issued under it), and opens a window of exactly two minutes.
The first ``POST /v1/pair/claim`` inside the window receives a fresh random 256-bit secret
and closes the window; every later claim, and any claim at or after the deadline, is
refused. The app keeps only the secret's SHA-256 and compares in constant time.

What this is not: caller verification. The two-minute first-caller race is the approved
design; the self-reported caller label is shown escaped and marked as self-reported, next
to the address the claim actually came from, so a wrong pairing is visible. A restart
never restores an armed window.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import secrets
from datetime import datetime, timedelta, timezone
from typing import Callable

from .store import PairingState, Store

PAIR_WINDOW = timedelta(seconds=120)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class Pairing:
    def __init__(self, store: Store, *, clock: Callable[[], datetime] = _utcnow,
                 on_revoke: Callable[[], None] = lambda: None):
        self._store = store
        self._clock = clock
        self._on_revoke = on_revoke

    # -- lifecycle ---------------------------------------------------------------------

    def close_window_at_start(self) -> None:
        """App start: an armed window never survives a restart."""
        with self._store.transaction():
            state = self._store.get_pairing()
            if state.armed_until is not None:
                self._store.put_pairing(PairingState(state.generation, state.secret_sha256,
                                                     state.caller_label, state.observed_peer,
                                                     state.claimed_at, None))

    def arm(self) -> datetime:
        """Operator action (ingress + CSRF only). Revokes the current secret and every
        authority issued under it, then opens the window. Returns the deadline."""
        now = self._clock()
        deadline = now + PAIR_WINDOW
        with self._store.transaction():
            state = self._store.get_pairing()
            self._store.put_pairing(PairingState(state.generation + 1, None, None, None, None,
                                                 deadline))
        self._on_revoke()
        return deadline

    def window_open(self) -> bool:
        state = self._store.get_pairing()
        return state.armed_until is not None and self._clock() < state.armed_until

    def claim(self, caller_label: str, observed_peer: str | None) -> str | None:
        """The first claim inside the window gets the secret (shown once, never stored in
        clear); anything else gets ``None``."""
        with self._store.transaction():
            state = self._store.get_pairing()
            if state.armed_until is None or not self._clock() < state.armed_until:
                return None
            secret = secrets.token_urlsafe(32)  # 256 bits
            self._store.put_pairing(PairingState(state.generation, _hash(secret),
                                                 caller_label[:64], (observed_peer or "")[:64] or None,
                                                 self._clock(), None))
            return secret

    def verify(self, presented: str | None) -> bool:
        state = self._store.get_pairing()
        if not presented or state.secret_sha256 is None:
            # Still spend a comparison so timing does not reveal "unpaired".
            hmac.compare_digest(_hash(presented or ""), "0" * 64)
            return False
        return hmac.compare_digest(_hash(presented), state.secret_sha256)

    def generation(self) -> int:
        return self._store.get_pairing().generation

    # -- operator view (Task 13 renders it) -------------------------------------------------

    def status_view(self) -> dict:
        """What the setup page shows. Never the secret or its hash. The caller label is
        HTML-escaped and marked self-reported; the observed peer is what the app saw."""
        state = self._store.get_pairing()
        now = self._clock()
        armed = state.armed_until is not None and now < state.armed_until
        unclaimed_expired = state.armed_until is not None and not armed
        return {
            "paired": state.secret_sha256 is not None,
            "armed": armed,
            "window_closed_unclaimed": unclaimed_expired,
            "caller_label_html": None if state.caller_label is None else html.escape(state.caller_label),
            "caller_label_is_self_reported": True,
            "observed_peer_html": None if state.observed_peer is None else html.escape(state.observed_peer),
            "claimed_at": state.claimed_at,
        }
