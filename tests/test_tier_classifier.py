# tests/test_tier_classifier.py
"""Unit tests for the production per-item tier classifier (mocked SDK)."""
from __future__ import annotations

import sys
import types

import pytest

import tier_classifier
from sensitivity import DEFAULT_TIER

pytestmark = [pytest.mark.unit]


class _FakeText:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeAssistant:
    def __init__(self, text: str) -> None:
        self.content = [_FakeText(text)]


def _install_fake_sdk(
    monkeypatch, *, reply: str | None = None, raise_exc: Exception | None = None,
    capture: dict | None = None,
):
    """Install a fake claude_agent_sdk module whose query() yields one AssistantMessage.
    If ``capture`` is given, the kwargs passed to ClaudeAgentOptions are recorded into it."""
    fake = types.ModuleType("claude_agent_sdk")

    class ClaudeAgentOptions:  # noqa: N801 — mirrors SDK name
        def __init__(self, **kw):
            self.kw = kw
            if capture is not None:
                capture.update(kw)

    class AssistantMessage:  # noqa: N801
        pass

    fake.ClaudeAgentOptions = ClaudeAgentOptions
    fake.AssistantMessage = _FakeAssistant if reply is not None else AssistantMessage

    async def query(*, prompt, options):  # noqa: ANN001
        if raise_exc is not None:
            raise raise_exc
        if reply is not None:
            yield _FakeAssistant(reply)

    fake.query = query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)


async def test_classify_returns_parsed_tier(monkeypatch):
    _install_fake_sdk(monkeypatch, reply="private")
    assert await tier_classifier.classify_tier("Nicola's salary is 5000 EUR") == "private"


async def test_classify_defaults_private_on_unparseable(monkeypatch):
    _install_fake_sdk(monkeypatch, reply="I am not sure")
    assert await tier_classifier.classify_tier("ambiguous") == DEFAULT_TIER


async def test_classify_chatty_reply_with_tier_words_defaults_private(monkeypatch):
    # #350 end-to-end: a reply that violates the single-word contract must NOT
    # have a tier word plucked out of it (the leftmost token here is "public",
    # for a fact that belongs at family) — it falls to the leak-safe default.
    _install_fake_sdk(monkeypatch, reply="This is not public; it is family")
    assert await tier_classifier.classify_tier("the home alarm code is 4712") == DEFAULT_TIER


async def test_classify_verbose_reply_with_labeled_final_line_parses(monkeypatch):
    # #497 end-to-end: the measured failure shape — a multi-hundred-char
    # verbose reply — classifies when it ends with the mandated answer line.
    _install_fake_sdk(
        monkeypatch,
        reply=(
            "Holiday plans are ordinary, socially shareable facts; anyone the "
            "user talks to in the home is friends-or-closer.\n"
            "Tier: friends"
        ),
    )
    assert await tier_classifier.classify_tier(
        "the family flies to Lisbon on the 14th") == "friends"


async def test_classify_grants_turn_headroom_for_the_reply(monkeypatch):
    """#497 reopen: 0.174.0 hit "Reached maximum number of turns (1)";
    0.176.0's single spare turn still hit "Reached maximum number of turns
    (2)" live (2 of 24 retentions, retries exhausted too, both items lost to
    the private default). Operator ruling 2026-08-11: on internal background
    calls the turn cap is a runaway backstop, never an efficiency device —
    exhaustion must be rare and terminal, not routine. With allowed_tools=[]
    the spare turns are inert (nothing agentic to do but finish emitting
    text), so the floor pinned here is generous."""
    captured: dict = {}
    _install_fake_sdk(monkeypatch, reply="family", capture=captured)
    await tier_classifier.classify_tier("the alarm code is 4712")
    assert captured.get("max_turns") >= 8


async def test_classify_turns_are_genuinely_inert(monkeypatch):
    """Sol r1 S1 (#497 reopen review): ``allowed_tools=[]`` alone is only
    auto-approval — built-in tools stay reachable and acceptEdits would
    auto-approve edits, so generous turn headroom would hand the classifier
    agentic rounds over retained-item text. Built-ins must be REMOVED
    (``tools=[]``), with Agent/Task denied (they bypass ``allowed_tools``)
    and Bash belt-and-braces — the restricted-webhook containment shape."""
    captured: dict = {}
    _install_fake_sdk(monkeypatch, reply="family", capture=captured)
    await tier_classifier.classify_tier("the alarm code is 4712")
    assert captured.get("tools") == []
    assert captured.get("allowed_tools") == []
    assert set(captured.get("disallowed_tools") or ()) >= {"Bash", "Task", "Agent"}


async def test_classify_word_plus_answer_line_parses(monkeypatch):
    """#497 reopen end-to-end: the dominant live failure shape on v0.176.0 —
    the model emits the bare tier word AND the mandated answer line
    ("private\\nTier: private\\n", 22 chars, 2 lines, label present; 8 of 24
    retentions in one session). Agreement must classify, not default."""
    _install_fake_sdk(monkeypatch, reply="private\nTier: private\n")
    assert await tier_classifier.classify_tier(
        "Nicola's salary is 5000 EUR") == "private"


async def test_classify_conflicting_answer_lines_still_default(monkeypatch):
    # The agreement carve-out must not reopen "last one wins": a conflicting
    # prior answer stays ambiguous and falls to the leak-safe default.
    _install_fake_sdk(monkeypatch, reply="private\nTier: public")
    assert await tier_classifier.classify_tier(
        "Nicola's salary is 5000 EUR") == DEFAULT_TIER


async def test_classify_defaults_private_on_error(monkeypatch):
    monkeypatch.setattr(tier_classifier, "_RETRY_BACKOFF_S", 0)  # D-5 retry path
    _install_fake_sdk(monkeypatch, raise_exc=RuntimeError("sdk boom"))
    assert await tier_classifier.classify_tier("anything") == DEFAULT_TIER


async def test_classify_blank_content_is_default(monkeypatch):
    _install_fake_sdk(monkeypatch, reply="public")
    assert await tier_classifier.classify_tier("   ") == DEFAULT_TIER


async def test_classify_uses_root_safe_permission_mode(monkeypatch):
    """The classifier must NOT use ``bypassPermissions``: the SDK turns that into
    ``--dangerously-skip-permissions``, which the bundled ``claude`` CLI refuses to
    run as root — and HA add-ons run as root, so it would fail and silently default
    every item to ``private``. Regression guard for the prod incident found on
    v0.45.0 (fixed v0.45.1)."""
    captured: dict = {}
    _install_fake_sdk(monkeypatch, reply="family", capture=captured)
    await tier_classifier.classify_tier("the family dinner is at 7")
    assert captured.get("permission_mode") != "bypassPermissions"
    assert captured.get("permission_mode") == "acceptEdits"


async def test_classify_uses_verified_cli_path(monkeypatch):
    from claude_runtime import CLAUDE_CLI_PATH

    captured: dict = {}
    _install_fake_sdk(monkeypatch, reply="family", capture=captured)

    assert await tier_classifier.classify_tier("dinner is at seven") == "family"
    assert captured.get("cli_path") == CLAUDE_CLI_PATH


def _install_flaky_sdk(monkeypatch, *, fail_times: int, reply: str,
                       exc: Exception | None = None):
    """Fake SDK whose query() raises on the first ``fail_times`` calls, then
    yields ``reply``. Records the call count on the returned dict."""
    fake = types.ModuleType("claude_agent_sdk")
    state = {"calls": 0}

    class ClaudeAgentOptions:  # noqa: N801
        def __init__(self, **kw):
            self.kw = kw

    fake.ClaudeAgentOptions = ClaudeAgentOptions
    fake.AssistantMessage = _FakeAssistant

    async def query(*, prompt, options):  # noqa: ANN001
        state["calls"] += 1
        if state["calls"] <= fail_times:
            raise (exc or RuntimeError("transient sdk boom"))
        yield _FakeAssistant(reply)

    fake.query = query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)
    return state


async def test_transient_failure_retries_once_then_classifies(monkeypatch):
    """D-5 (v0.69.2): two transient SDK failures during the 2026-07-12 probes
    permanently mis-tiered items to `private` (over-restriction). The
    classifier is off the hot path — one bounded retry is safe and cuts the
    mis-tier rate for transient spawn/API failures."""
    monkeypatch.setattr(tier_classifier, "_RETRY_BACKOFF_S", 0)
    state = _install_flaky_sdk(monkeypatch, fail_times=1, reply="family")
    assert await tier_classifier.classify_tier("dinner at seven") == "family"
    assert state["calls"] == 2


async def test_both_attempts_fail_defaults_with_typed_warning(monkeypatch, caplog):
    """The original D-5 tracebacks were truncated by log tooling — the WARNING
    line itself must carry the exception type + message (greppable one-liner)."""
    import logging as _logging

    monkeypatch.setattr(tier_classifier, "_RETRY_BACKOFF_S", 0)
    state = _install_flaky_sdk(
        monkeypatch, fail_times=99, reply="never",
        exc=ConnectionError("ProcessTransport is not ready"),
    )
    with caplog.at_level(_logging.WARNING):
        assert await tier_classifier.classify_tier("anything") == DEFAULT_TIER
    assert state["calls"] == 2  # exactly one retry — never unbounded
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "ConnectionError" in msg
    assert "ProcessTransport is not ready" in msg


async def test_unparseable_reply_logs_the_reply_shape(monkeypatch, caplog):
    """A garbled/unparseable reply used to default to private with ZERO log
    trace — indistinguishable from a correct `private` classification."""
    import logging as _logging

    _install_fake_sdk(monkeypatch, reply="I am not sure")
    with caplog.at_level(_logging.WARNING):
        assert await tier_classifier.classify_tier("ambiguous") == DEFAULT_TIER
    msgs = [r.getMessage() for r in caplog.records]
    assert any("unparseable" in m.lower() for m in msgs)


async def test_unparseable_warn_is_content_free_but_structural(monkeypatch, caplog):
    """#497 + review r1 (Sol+Terra): the diagnostic must carry enough
    structure to distinguish failure shapes (length, line count, whether a
    Tier: label appeared) while NEVER logging the reply text — the
    classifier's words paraphrase the retained item, and this module's
    doctrine is leak-safety."""
    import logging as _logging

    reply = "The user's salary details suggest money.\nTier: public would be wrong"
    _install_fake_sdk(monkeypatch, reply=reply)
    with caplog.at_level(_logging.WARNING):
        assert await tier_classifier.classify_tier("salary is 5000") == DEFAULT_TIER
    warn = next(r.getMessage() for r in caplog.records
                if "unparseable" in r.getMessage().lower())
    # Structural metadata present…
    assert f"{len(reply)} chars" in warn
    assert "2 lines" in warn
    assert "tier label present" in warn
    # …reply content absent, and the line stays bounded however long the reply.
    assert "salary" not in warn
    assert len(warn) < 200


def _install_sequenced_sdk(monkeypatch, *, replies: list[str],
                           raise_from: int | None = None):
    """Fake SDK whose query() yields replies[i] on the i-th call (the last
    reply repeats past the end) and records every prompt. Calls numbered from
    1; ``raise_from`` makes that call and all later ones raise instead."""
    fake = types.ModuleType("claude_agent_sdk")
    state = {"calls": 0, "prompts": []}

    class ClaudeAgentOptions:  # noqa: N801
        def __init__(self, **kw):
            self.kw = kw

    fake.ClaudeAgentOptions = ClaudeAgentOptions
    fake.AssistantMessage = _FakeAssistant

    async def query(*, prompt, options):  # noqa: ANN001
        state["calls"] += 1
        state["prompts"].append(prompt)
        if raise_from is not None and state["calls"] >= raise_from:
            raise RuntimeError("sdk boom")
        yield _FakeAssistant(replies[min(state["calls"] - 1, len(replies) - 1)])

    fake.query = query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)
    return state


async def test_unparseable_reply_is_reasked_once_then_classifies(monkeypatch):
    """#508: the exception ladder never covered a reply the parser refuses —
    it defaulted to private FIRST STRIKE, silently, at a measured ~12% of
    calls on a 48-item save (v0.177.0). One re-ask with the format restated
    must recover the compliant answer instead of defaulting."""
    state = _install_sequenced_sdk(monkeypatch, replies=[
        "This concerns household finances.\nIt should not be shared.\nprivate",
        "Tier: private",
    ])
    assert await tier_classifier.classify_tier("salary is 5000 EUR") == "private"
    assert state["calls"] == 2


async def test_reask_prompt_restates_the_answer_line_mandate(monkeypatch):
    """The re-ask must carry the fact again plus the format reminder — the
    stricter restatement of the answer-line mandate, not a bare repeat."""
    state = _install_sequenced_sdk(monkeypatch, replies=[
        "I would not want to commit to a tier here.",
        "Tier: friends",
    ])
    assert await tier_classifier.classify_tier("dinner is at seven") == "friends"
    first, second = state["prompts"]
    assert first == "dinner is at seven"
    assert second.startswith("dinner is at seven")
    assert "Tier: <word>" in second
    assert "private, family, friends, or public" in second


async def test_reask_still_unparseable_defaults_after_exactly_two_asks(
        monkeypatch, caplog):
    """Exactly ONE re-ask — then the leak-safe default, never a third ask.
    Both structural warnings land: the re-ask announcement and the final
    default."""
    import logging as _logging

    state = _install_sequenced_sdk(monkeypatch, replies=[
        "chatty non-answer", "still a chatty non-answer",
    ])
    with caplog.at_level(_logging.WARNING):
        assert await tier_classifier.classify_tier("anything") == DEFAULT_TIER
    assert state["calls"] == 2
    warns = [r.getMessage() for r in caplog.records
             if "unparseable" in r.getMessage().lower()]
    assert len(warns) == 2
    assert "re-asking once" in warns[0]
    assert f"defaulting to {DEFAULT_TIER}" in warns[1]


async def test_reask_exception_ladder_then_default(monkeypatch):
    """An unparseable first reply followed by a broken backend on the re-ask
    still lands on the default: the re-ask gets its own D-5 exception retry
    (calls 2 and 3), then private."""
    monkeypatch.setattr(tier_classifier, "_RETRY_BACKOFF_S", 0)
    state = _install_sequenced_sdk(
        monkeypatch, replies=["chatty non-answer"], raise_from=2)
    assert await tier_classifier.classify_tier("anything") == DEFAULT_TIER
    assert state["calls"] == 3


async def test_reask_cannot_downgrade_below_discarded_evidence(monkeypatch):
    """Review r1 (Sol S1): the re-ask is a fresh stateless sample — a second
    opinion of ``public`` must not overrule a discarded first reply whose only
    answer-shaped line said ``private``. Evidence is a floor; a re-ask below
    it is a cross-ask conflict and takes the leak-safe default."""
    state = _install_sequenced_sdk(monkeypatch, replies=[
        "This must be kept confidential.\nprivate",
        "public",
    ])
    with tier_classifier.classify_stats() as stats:
        assert await tier_classifier.classify_tier(
            "salary is 5000 EUR") == DEFAULT_TIER
    assert state["calls"] == 2
    assert stats.defaulted == 1


async def test_reask_at_or_above_evidence_floor_is_accepted(monkeypatch):
    state = _install_sequenced_sdk(monkeypatch, replies=[
        "Some ungrammatical multi-line reply.\nfriends",
        "Tier: family",
    ])
    assert await tier_classifier.classify_tier(
        "the alarm code is 4712") == "family"
    assert state["calls"] == 2
    state = _install_sequenced_sdk(monkeypatch, replies=[
        "Some ungrammatical multi-line reply.\nfriends",
        "friends",
    ])
    assert await tier_classifier.classify_tier("dinner at seven") == "friends"


async def test_prose_tier_words_create_no_evidence_floor(monkeypatch):
    """#350 boundary holds for the floor too: a tier word INSIDE prose is not
    evidence — only a whole answer-shaped line is. The re-ask answer stands."""
    state = _install_sequenced_sdk(monkeypatch, replies=[
        "This is not about the public; nor family matters, to be honest",
        "Tier: public",
    ])
    assert await tier_classifier.classify_tier("bin day is Tuesday") == "public"
    assert state["calls"] == 2


async def test_classify_stats_counts_failure_defaults_only(monkeypatch):
    """#508: a counting scope sees N-defaulted-of-M across a batch. A genuine
    ``private`` classification is NOT a default — only failure paths count."""
    with tier_classifier.classify_stats() as stats:
        _install_fake_sdk(monkeypatch, reply="private")
        assert await tier_classifier.classify_tier("salary is 5000") == "private"
        _install_fake_sdk(monkeypatch, reply="chatty non-answer")
        assert await tier_classifier.classify_tier("ambiguous") == DEFAULT_TIER
    assert stats.total == 2
    assert stats.defaulted == 1


async def test_classify_stats_counts_exception_defaults(monkeypatch):
    monkeypatch.setattr(tier_classifier, "_RETRY_BACKOFF_S", 0)
    _install_fake_sdk(monkeypatch, raise_exc=RuntimeError("sdk boom"))
    with tier_classifier.classify_stats() as stats:
        assert await tier_classifier.classify_tier("anything") == DEFAULT_TIER
    assert stats.total == 1
    assert stats.defaulted == 1


async def test_classify_without_a_stats_scope_still_works(monkeypatch):
    """No scope open (e.g. a direct caller outside the save path): counting
    is simply inert."""
    _install_fake_sdk(monkeypatch, reply="family")
    assert await tier_classifier.classify_tier("dinner at seven") == "family"


async def test_unparseable_warn_reports_absent_label(monkeypatch, caplog):
    import logging as _logging

    _install_fake_sdk(monkeypatch, reply="x" * 5000)
    with caplog.at_level(_logging.WARNING):
        assert await tier_classifier.classify_tier("anything") == DEFAULT_TIER
    warn = next(r.getMessage() for r in caplog.records
                if "unparseable" in r.getMessage().lower())
    assert "5000 chars" in warn
    assert "tier label absent" in warn
    assert "x" * 20 not in warn
    assert len(warn) < 200
