"""#1228 (+#1229): the assistant is shown and told that a job a delegate's own
plugin declares is that delegate's to start — through a sync text delegation —
and a specialist's own start never hands it an internal engagement id.

The assistant keeps ``start_job`` (operator constraint, umbrella design
2026-10-02): nothing here refuses her call. What is pinned is what she is SHOWN
(the ``<jobs>`` listing, the tool description) and TOLD (the compiled doctrine,
per projection), and what a specialist's own start RETURNS."""
from __future__ import annotations

import re

import pytest

import plugin_grants
import tools
from agent import _render_jobs_block
from config import DelegateEntry

try:
    from tests.test_background_jobs_declaration import (
        _START_JOB, _agent_registry, _cfg, _load_jobs)
    from tests.test_assistant_prompts import _compiled_resident_carriers, _legacy_prompt_carriers
    from tests.test_specialist_start_job import (  # noqa: F401  (fixtures)
        _desk_turn_origin, _own_ledger, _real_discovery, call, finance_is_specialist,
        home, runtime, start_as)
except ImportError:
    from test_background_jobs_declaration import (
        _START_JOB, _agent_registry, _cfg, _load_jobs)
    from test_assistant_prompts import _compiled_resident_carriers, _legacy_prompt_carriers
    from test_specialist_start_job import (  # noqa: F401  (fixtures)
        _desk_turn_origin, _own_ledger, _real_discovery, call, finance_is_specialist,
        home, runtime, start_as)

OWN_LINE = ("- finance:classify — Classify transactions: Classify unreviewed "
            "transactions in batches (Alex's own job: in text, ask Alex with a sync "
            "delegation; Alex starts it itself and it runs in Alex's topic)")


def _ws(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _assistant():
    return _cfg("assistant", "Ellen",
                delegates=[DelegateEntry(agent="finance", purpose="Money.",
                                         when="Asked about money.")])


# --- what she is shown ------------------------------------------------------------------

def test_a_delegates_own_loadable_job_is_listed_as_that_delegates_to_start(tmp_path, monkeypatch):
    _load_jobs(tmp_path, monkeypatch)
    assistant = _assistant()
    block = _render_jobs_block(assistant.role, assistant.delegates,
                               _agent_registry(assistant), allowed_tools=[_START_JOB])
    assert block == "<jobs>\n" + OWN_LINE + "\n</jobs>"


def test_a_delegates_job_withheld_for_an_unresolved_secret_is_not_listed_as_its_own(
        tmp_path, monkeypatch):
    """Both reviewers, design round 1: the delegate cannot start a job whose
    plugin its session withholds, so the listing must not say it can."""
    _load_jobs(tmp_path, monkeypatch)
    monkeypatch.setattr(plugin_grants, "blocking_unresolved_env_vars_for_resolved",
                        lambda rp, environ=None: ["FINANCE_TOKEN"])
    assistant = _assistant()
    block = _render_jobs_block(assistant.role, assistant.delegates,
                               _agent_registry(assistant), allowed_tools=[_START_JOB])
    assert block == (
        "<jobs>\n"
        "- finance:classify — Classify transactions: Classify unreviewed "
        "transactions in batches (runs in Alex's topic)\n"
        "</jobs>")
    assert "own job" not in block


def test_the_start_job_description_names_the_delegation_route():
    description = _ws(tools.start_job.description)
    assert ("A job <jobs> lists as a delegate's own is started by delegating the "
            "request to that delegate.") in description


# --- what she is told -------------------------------------------------------------------

_OWN_JOB_SENTENCES = (
    "A job listed as a delegate's own job belongs to that delegate: ask that delegate "
    "for it with `delegate_to_agent` in `sync` mode, never in `interactive` mode, and "
    "it starts the job itself.",
    "A delegation does run that job, its batches included, so never tell the person "
    "it cannot.",
    "Use `start_job` for such a job only when the delegation reports that it could "
    "not start it.",
    "Never do the work of a job that is not a delegate's own through "
    "`delegate_to_agent` instead.",
)
_VOICE_LINE = ("On a voice call no background job can start: do not call `start_job`, "
               "and do not delegate a request to start one. Ask the person to make the "
               "request in text.")
_OLD_UNCONDITIONAL = "Never do a listed job's work through `delegate_to_agent` instead."


def _assistant_carriers() -> dict[str, str]:
    return {name.split(":", 1)[1]: _ws(text)
            for name, text in _compiled_resident_carriers()
            if name.startswith("assistant:")}


def test_the_text_projection_tells_her_to_delegate_a_delegates_own_job_and_drops_the_old_rule():
    carriers = _assistant_carriers()
    assert set(carriers) == {"text", "voice", "restricted_webhook"}
    for sentence in _OWN_JOB_SENTENCES:
        assert carriers["text"].count(_ws(sentence)) == 1, sentence
        assert carriers["voice"].count(_ws(sentence)) == 0, sentence
        assert carriers["restricted_webhook"].count(_ws(sentence)) == 0, sentence
    assert sum(c.count(_OLD_UNCONDITIONAL) for c in carriers.values()) == 0
    # the composed-prompt configuration's carrier says the same
    legacy = _ws(dict(_legacy_prompt_carriers())["assistant"])
    for sentence in _OWN_JOB_SENTENCES:
        assert legacy.count(_ws(sentence.replace("the person", "the user"))) == 1, sentence
    assert legacy.count(_ws(_VOICE_LINE)) == 1
    assert legacy.count(_OLD_UNCONDITIONAL) == 0


def test_the_voice_projection_says_no_job_starts_on_a_call():
    carriers = _assistant_carriers()
    assert carriers["voice"].count(_ws(_VOICE_LINE)) == 1
    assert carriers["text"].count(_ws(_VOICE_LINE)) == 0
    assert carriers["restricted_webhook"].count(_ws(_VOICE_LINE)) == 0


def test_the_topic_rule_admits_a_delegates_reported_job_start():
    """Astra, design round 1: 'a sync delegation opens no topic' would deny the
    topic of a job the delegate reports it started."""
    text = _assistant_carriers()["text"]
    assert _ws("or when a delegate's result says it started one of its own jobs, "
               "whose topic then exists; a sync delegation otherwise opens no topic."
               ) in text


def test_the_legacy_carriers_completion_rule_keeps_a_started_jobs_topic_open():
    """Astra, diff round 1: the composed-prompt carrier said EVERY completion
    notification closes its topic, which a delegation that started a still-running
    job contradicts."""
    legacy = _ws(dict(_legacy_prompt_carriers())["assistant"])
    assert legacy.count(_ws("A completion NOTIFICATION for an engagement means that "
                            "engagement's topic is **closed**.")) == 1
    assert legacy.count(_ws("A delegation result or completion saying the delegate started "
                            "one of its own jobs is not one: that job's topic stays open "
                            "until the job's own end notice arrives.")) == 1
    assert legacy.count(_ws("A completion NOTIFICATION means that engagement's topic is "
                            "**closed**.")) == 0


# --- #1229: what a specialist's own start returns --------------------------------------

async def test_a_specialists_own_start_returns_no_engagement_id_pending_or_busy(
        finance_is_specialist, tmp_path, monkeypatch):
    _real_discovery(monkeypatch, tmp_path)
    _own_ledger(tmp_path)
    started = await start_as(_desk_turn_origin())
    assert started["status"] == "pending", started
    assert "engagement_id" not in started and "topic_id" not in started
    assert started["message"].startswith("Started ") and "Progress appears in" in started["message"]
    again = await start_as(_desk_turn_origin())
    assert again["kind"] == "job_busy", again
    assert "engagement_id" not in again and "topic_id" not in again
    assert "already has a running job" in again["message"]


async def test_the_assistants_own_results_still_carry_the_engagement_id(
        finance_is_specialist, tmp_path, monkeypatch):
    _real_discovery(monkeypatch, tmp_path)
    _own_ledger(tmp_path)
    started = await call()
    assert started["status"] == "pending", started
    assert started.get("engagement_id")
    again = await call()
    assert again["kind"] == "job_busy", again
    assert again.get("engagement_id") == started["engagement_id"]
