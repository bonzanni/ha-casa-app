"""#783 — a test that registers a broker request or an authz challenge must not
be able to affect a later test.

`verdict_broker.BROKER` (`verdict_broker.py:525`) and `authz_grants.CHALLENGES`
(`authz_grants.py:972`) are process-global singletons, and `tools` binds the
coordinator by alias at import (`tools.py:48`). Nothing isolated them between
tests, so a `PendingRequest` registered on one pytest-asyncio loop stayed in
`BROKER._live` and `CHALLENGES._entries` after that loop closed. Two
consequences reached later tests, and both are pinned here as OUTCOMES:

* a later sweep (`cancel_all` -> `_finish` -> `set_result`) resolved a future
  bound to the closed loop and raised `RuntimeError: Event loop is closed`
  (`verdict_broker.py:199`) — the reported failure of
  `tests/test_graceful_shutdown_engagement_launch.py`;
* a stale `_entries` row deduplicated a later, unrelated registration away
  (`authz_grants.py:549`), so NO keyboard was posted at all, and a stale
  `_retired` tombstone reattached a later same-key registration to the previous
  test's outcome (`verdict_broker.py:150-160`).

Each pair is ORDERED: the first test leaks, the second one measures what the
next test actually gets. Ordering within a file is pytest's collection order,
and `--dist loadfile` keeps a file on one worker, so the pair stays together.

The victim tests deliberately assert observable outcomes rather than "the
containers are empty": a count of 0 live requests, a fresh registration that
really posts a keyboard, and an unresolved future. A symptom fix in
`verdict_broker._finish` (a closed-loop guard), a fix confined to the one
leaking test, or a fixture that REBINDS the singletons instead of clearing them
in place all leave at least one of these assertions red.
"""

from dataclasses import dataclass

import authz_grants
import tools
import verdict_broker


@dataclass(frozen=True)
class _IsolationKey:
    """Challenge identity for this file. Value-equal across the two tests of a
    pair, which is exactly what makes the coordinator's dedup observable."""

    artifact_id: str


class _RecordingChannel:
    """Minimal DM channel: records each keyboard post and returns a message id.

    Returning an int is what makes the setup driver settle as ``"posted"``
    rather than ``"delivery_failed"`` (`verdict_broker.py:_run_setup`)."""

    def __init__(self) -> None:
        self.posts: list[dict] = []

    async def post_dm_keyboard(self, **kwargs) -> int:
        self.posts.append(kwargs)
        return 4242


_PAIR_A_KEY = _IsolationKey("isolation-pair-a")
_PAIR_B_SCOPE = "isolation-pair-b"
_PAIR_B_RID = "isolation-pair-b-rid"


async def _register_pair_a(channel):
    """Register the pair-A challenge through the ``tools`` ALIAS.

    Through the alias, not through ``authz_grants.CHALLENGES``, on purpose: a
    fixture that rebound the module attribute instead of clearing the object in
    place would leave this alias — the one `tools._invalidate_lifecycle` sweeps
    (`tools.py:12529`) — polluted, and a leaker that registered through the
    dynamic name would hide exactly that."""
    handle = tools.CHALLENGES.register_challenge(
        _PAIR_A_KEY, chat_id=100, operator_id=7, channel=channel,
        challenge_text="isolation probe", kind="trigger_consent",
    )
    # `settled_post` awaits the coordinator-owned setup driver; a bare
    # `sleep(0)` is scheduling-dependent and has been measured leaving the post
    # count at 0.
    assert await handle.settled_post() == "posted"
    return handle


async def test_an_unanswered_challenge_registers_in_the_global_broker_and_coordinator():
    """The leak, expressed through public API and left unanswered — this is what
    `test_reconcile_fires_deduped_consent_prompt` does to every later test."""
    channel = _RecordingChannel()
    handle = await _register_pair_a(channel)

    assert handle.created is True
    assert len(channel.posts) == 1
    assert len(verdict_broker.BROKER._live) == 1
    assert len(tools.CHALLENGES._entries) == 1
    # Deliberately NOT answered, cancelled or drained: the point is what a test
    # that simply ends leaves behind.


async def test_a_later_test_sweeps_a_clean_broker_and_gets_a_fresh_challenge():
    """The victim. Runs on its own loop, after the leaker's loop has closed."""
    # (1) A sweep must find nothing and must not detonate. Without isolation
    # this RAISES `RuntimeError: Event loop is closed` at verdict_broker.py:199,
    # which is the reported #783 failure.
    assert verdict_broker.BROKER.cancel_all(reason="isolation_probe") == 0

    # (2) An identical registration must be a genuinely NEW challenge that
    # really posts a keyboard. A stale `_entries` row makes this `created=False`
    # with zero posts — the silent half of the defect, which the sweep count
    # alone cannot see (`cancel_matching` counts broker cancellations, so an
    # emptied broker reports 0 whether or not the coordinator is polluted).
    channel = _RecordingChannel()
    handle = await _register_pair_a(channel)

    assert handle.created is True
    assert len(channel.posts) == 1


async def test_a_cancelled_request_leaves_a_retirement_tombstone():
    """Second leak shape: a FINISHED request, whose tombstone survives in
    `_retired` for `_RETIRE_S` seconds."""
    req, created = verdict_broker.BROKER.register(
        namespace="resident_ask", scope=_PAIR_B_SCOPE,
        request_id=_PAIR_B_RID, timeout_s=600.0,
    )
    assert created is True
    assert verdict_broker.BROKER.cancel(
        namespace="resident_ask", scope=_PAIR_B_SCOPE,
        request_id=_PAIR_B_RID, reason="isolation_probe",
    ) is True
    assert req._future.result()["outcome"] == "cancelled"


async def test_a_later_test_registering_the_same_key_gets_a_fresh_unresolved_request():
    """Without isolation this reattaches to the PREVIOUS test's tombstone
    (`verdict_broker.py:150-160`): `created` is False and the future already
    carries that test's `{"outcome": "cancelled", ...}`."""
    req, created = verdict_broker.BROKER.register(
        namespace="resident_ask", scope=_PAIR_B_SCOPE,
        request_id=_PAIR_B_RID, timeout_s=600.0,
    )
    assert created is True
    assert req._future.done() is False
