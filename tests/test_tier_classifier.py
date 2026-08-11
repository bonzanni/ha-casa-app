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


async def test_classify_allows_a_spare_turn_for_the_reply(monkeypatch):
    """#497: 1 of 8 measured failures was the SDK erroring with "Reached
    maximum number of turns (1)" and returning no text at all. With
    allowed_tools=[] a second turn can only finish emitting text, so the
    classifier grants exactly one spare turn."""
    captured: dict = {}
    _install_fake_sdk(monkeypatch, reply="family", capture=captured)
    await tier_classifier.classify_tier("the alarm code is 4712")
    assert captured.get("max_turns") == 2


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
