# tests/test_recall_readable_slice_framing.py
"""#581: a NON-EMPTY recall result must scope itself the way the empty ones do.

#472 taught the empty arms to say "not proof of absence" and stopped there.
But emptiness was never the property that needed framing — *boundedness* is,
and it holds of a large, useful result exactly as much as of an empty one. The
recall request sends the caller's readable tiers as a server-side tag filter,
so an above-clearance hit is dropped BY THE BACKEND and leaves no client-visible
trace: no count, no flag, nothing. The only observable is what the model then
says to a person.

Observed live on v0.206.1: a `private` fact was stored; on the voice surface
(clearance `friends`) recall logged `outcome=hits hits=33` — thirty-three
unrelated readable memories, the on-topic one dropped server-side — and the
butler answered "No record of a wall safe combination hint." A well-formed,
non-empty, entirely honest result, read as an inventory of Casa's memory.

FOUR model-facing consumers render a slice; every one of them was unframed on
its non-empty path, and the suite that shipped with #472 actively pinned the
defect (`test_nonempty_digest_stays_clean`, now replaced by
`test_nonempty_digest_carries_the_note` below):

  1. `recall_memory`'s ok-arm            → tool-result `message`
  2. auto-recall's `<memory_context>`    → instruction line in the block
  3. specialist `<memory_context agent>` → instruction line in the block
  4. `_fetch_executor_archive`'s block   → instruction line under the heading

`query_engager` is deliberately absent: its synthesizer is constrained to the
supplied context and must emit the literal `UNKNOWN` when that context does not
answer, which routes to the already-framed unknown arm.

Each site is asserted in ITS OWN emitted artifact, so deleting the framing from
any one of them fails exactly one test — the mutation check that makes this a
pinning suite rather than four restatements of one import. For the same reason
the expected wording is written out LITERALLY here rather than imported from
production: a test that reads the constant it is meant to pin asserts nothing.

`test_every_slice_consumer_is_declared` is the guard against the failure mode
that produced this issue — a consumer added later that nobody remembers to
frame. It parses the tree for `render_recall` / `delegated_recall` call sites
and fails on any that this file has not classified.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import tools

pytestmark = [pytest.mark.unit]

CASA_ROOT = (
    Path(__file__).resolve().parent.parent / "casa" / "rootfs" / "opt" / "casa"
)

# The wording, written out here independently of the production constant.
EXPECTED_NOTE = (
    "Use relevant entries normally. This is the bounded view readable at this "
    "surface, not a complete inventory of Casa's memory. If it does not answer "
    "the request, do not say Casa has no record of it or does not know it — "
    "say you have nothing you can share on that here. Do not repeat or "
    "paraphrase this guidance to the user."
)
EXPECTED_PROMPT_LINE = f"[memory-use instruction: {EXPECTED_NOTE}]"


def _text(res: dict) -> str:
    return res["content"][0]["text"]


def _payload(res: dict) -> dict:
    """The tool result decoded. ``_result`` serialises with the default
    ``ensure_ascii``, so the note's em dash arrives as ``\\u2014`` in the raw
    text — assert against the decoded fields, as the model's own parse does
    (the sibling empty-arm messages have always been escaped the same way)."""
    import json
    return json.loads(_text(res))


def _hit(text: str = "Nicola keeps the thermostat at 20C."):
    from personality_types import RecallHit
    return RecallHit(
        text=text, memory_type="world", sensitivity="friends",
        application_tags=(), provenance=None, backend_id="b1",
        document_id=None, chunk_id=None, source_fact_ids=None,
        metadata=None, context=None, score=None,
    )


# ---------------------------------------------------------------------------
# The wording itself
# ---------------------------------------------------------------------------


class TestTheNoteItself:
    def test_wording_is_pinned(self):
        """Anchored outside the module under test: the literal above is the
        contract, and production is checked against it (not the reverse)."""
        import recall_renderer
        assert recall_renderer.READABLE_SLICE_NOTE == EXPECTED_NOTE
        assert recall_renderer.READABLE_SLICE_PROMPT_LINE == EXPECTED_PROMPT_LINE

    def test_the_note_never_confirms_a_hidden_record(self):
        """It must scope the slice without ever conceding that the asked-about
        record exists — the one thing the framing may not disclose."""
        low = EXPECTED_NOTE.lower()
        for leak in ("hidden", "above your clearance", "filtered out",
                     "withheld", "someone else can see", "there are entries"):
            assert leak not in low, f"the note hints at a specific record: {leak!r}"

    def test_the_note_authorises_ordinary_use(self):
        """The opposite failure to the one #581 reports: a caveat that makes
        the agent hedge over memories it can plainly read. The wording has to
        say 'use these' before it says 'but'."""
        assert "use relevant entries normally" in EXPECTED_NOTE.lower()

    def test_the_note_is_not_repeated_aloud(self):
        assert "do not repeat" in EXPECTED_NOTE.lower()

    def test_the_prompt_line_is_marked_as_an_instruction(self):
        """It sits beside rendered '- X said: ...' bullets; unmarked, it reads
        as one more recalled fact."""
        assert EXPECTED_PROMPT_LINE.startswith("[memory-use instruction:")
        assert EXPECTED_PROMPT_LINE.endswith("]")


# ---------------------------------------------------------------------------
# Consumer 1 — recall_memory's ok-arm
# ---------------------------------------------------------------------------


def _setup_recall(monkeypatch, *, channel: str, hits=(), token_budget: int = 512):
    import agent as agent_mod
    sem = AsyncMock()
    sem.recall_items.return_value = tuple(hits)
    monkeypatch.setattr(agent_mod, "active_semantic_memory", sem, raising=False)
    cfg = SimpleNamespace(memory=SimpleNamespace(token_budget=token_budget))
    monkeypatch.setattr(tools, "_agent_role_map", {"assistant": cfg}, raising=False)
    agent_mod.origin_var.set({"role": "assistant", "channel": channel})
    return sem


class TestRecallMemoryOkArm:
    async def test_nonempty_result_carries_the_note_and_the_fact(self, monkeypatch):
        _setup_recall(monkeypatch, channel="voice", hits=(_hit(),))
        res = _payload(await tools.recall_memory.handler({"query": "thermostat?"}))
        assert res["status"] == "ok"
        assert "thermostat at 20C" in res["memory"], "the digest must still be usable"
        assert res["message"] == EXPECTED_NOTE

    async def test_the_note_is_identical_at_every_clearance(self, monkeypatch):
        """A note that appeared only below `private` — or read differently
        there — would be an oracle for 'something was filtered out of THIS
        answer'. Truncation and the types filter bound the top tier too, so
        the wording is constant by design."""
        _setup_recall(monkeypatch, channel="voice", hits=(_hit(),))
        voice = _payload(await tools.recall_memory.handler({"query": "thermostat?"}))
        _setup_recall(monkeypatch, channel="telegram", hits=(_hit(),))
        telegram = _payload(await tools.recall_memory.handler({"query": "thermostat?"}))
        assert voice["message"] == telegram["message"] == EXPECTED_NOTE

    async def test_the_result_never_asserts_absence_itself(self, monkeypatch):
        """The note PROHIBITS the phrase "no record", so it necessarily
        contains it — strip the note before checking that nothing else in the
        result says it."""
        _setup_recall(monkeypatch, channel="voice", hits=(_hit(),))
        res = _payload(await tools.recall_memory.handler({"query": "thermostat?"}))
        rest = (res["memory"] + " " + res["status"]).lower()
        assert "no record" not in rest
        assert "don't have" not in rest


# ---------------------------------------------------------------------------
# Consumer 2 — auto-recall's <memory_context>
# ---------------------------------------------------------------------------


class _RecallingSem:
    """Semantic-memory stand-in whose recall always returns one readable hit."""

    def __init__(self, text: str = "Nicola keeps the thermostat at 20C."):
        self._text = text

    async def retain(self, bank, items, *, async_=True):
        return None

    async def recall_items(self, bank, query, *, tags, max_tokens, clearance,
                           types=("world", "experience", "observation"),
                           tags_match="any", budget="mid"):
        return (_hit(self._text),)

    async def profile(self, bank):
        return ""


class _CaptureClient:
    captured_options = None

    def __init__(self, options):
        type(self).captured_options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def query(self, text):
        return None

    async def receive_response(self):
        from claude_agent_sdk import (
            AssistantMessage as _AM, ResultMessage as _RM, TextBlock as _TB,
        )
        try:
            block = _TB(text="ok")
        except TypeError:
            block = _TB("ok")  # type: ignore[call-arg]
        try:
            yield _AM(content=[block])
        except TypeError:
            m = _AM.__new__(_AM)
            m.content = [block]  # type: ignore[attr-defined]
            yield m
        r = _RM.__new__(_RM)
        r.session_id = "sid-581"  # type: ignore[attr-defined]
        r.usage = {"input_tokens": 1, "output_tokens": 1}  # type: ignore[attr-defined]
        yield r


class TestAutoRecallBlock:
    async def test_injected_memory_context_carries_the_note(self, tmp_path):
        """The path where the model is MOST likely to overclaim: it never
        called a tool, so the block simply reads as 'what Casa remembers'."""
        import yaml
        from agent import Agent
        from bus import BusMessage, MessageType
        from channels import ChannelManager
        from config import AgentConfig, CharacterConfig, MemoryConfig, ToolsConfig
        from mcp_registry import McpServerRegistry
        from session_registry import SessionRegistry
        try:
            from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
        except ImportError:
            from role_artifact_stub import STUB_ROLE_ARTIFACT

        allowed = (yaml.safe_load(
            (CASA_ROOT / "defaults" / "agents" / "assistant" / "runtime.yaml")
            .read_text(encoding="utf-8"),
        ).get("tools") or {}).get("allowed") or []
        cfg = AgentConfig(
            role_artifact=STUB_ROLE_ARTIFACT, role="assistant",
            model="claude-sonnet-4-6", system_prompt="You are assistant.",
            character=CharacterConfig(name="Assistant"),
            tools=ToolsConfig(allowed=allowed, permission_mode="acceptEdits"),
            memory=MemoryConfig(token_budget=1000, read_strategy="per_turn"),
        )
        agent = Agent(
            config=cfg,
            session_registry=SessionRegistry(str(tmp_path / "assistant.json")),
            mcp_registry=McpServerRegistry(),
            channel_manager=ChannelManager(),
            semantic_memory=_RecallingSem(),
        )
        _CaptureClient.captured_options = None
        with patch("sdk_client_pool._default_make_client", _CaptureClient):
            await agent._process(BusMessage(
                type=MessageType.CHANNEL_IN, source="telegram", target="x",
                content="what do you remember?", channel="telegram",
                context={"chat_id": "c-581"},
            ))
        prompt = _CaptureClient.captured_options.system_prompt or ""
        assert "<memory_context>" in prompt
        assert "thermostat at 20C" in prompt, "the digest must still be usable"
        assert EXPECTED_PROMPT_LINE in prompt
        # Inside the block, not floating in the prompt: it must read as part of
        # the memory context it qualifies.
        block = prompt.split("<memory_context>")[1].split("</memory_context>")[0]
        assert EXPECTED_PROMPT_LINE in block


# ---------------------------------------------------------------------------
# Consumer 3 — the specialist delegation's <memory_context agent="...">
# ---------------------------------------------------------------------------


class _FakeSpecialistSem:
    def __init__(self, text: str = "Q1 spend: EUR 1200"):
        self._text = text

    async def recall_items(self, bank, query, *, tags, max_tokens, clearance,
                           types=("world", "experience", "observation"),
                           tags_match="any", budget="mid"):
        return (_hit(self._text),)

    async def retain(self, bank, items, *, async_=True):
        return None


class _FakeSpecialistClient:
    captured_prompt: str = ""

    def __init__(self, options):
        self.options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def query(self, text: str) -> None:
        type(self).captured_prompt = text

    async def receive_response(self):
        from claude_agent_sdk import AssistantMessage, TextBlock
        try:
            block = TextBlock(text="specialist reply")
        except TypeError:
            block = TextBlock("specialist reply")  # type: ignore[call-arg]
        try:
            yield AssistantMessage(content=[block])
        except TypeError:
            m = AssistantMessage.__new__(AssistantMessage)
            m.content = [block]  # type: ignore[attr-defined]
            yield m


class TestSpecialistBlock:
    async def test_specialist_memory_context_carries_the_note(self, monkeypatch):
        """A specialist reads at the DELEGATING turn's clearance, so its slice
        is bounded by someone else's floor — and it reports back to the caller
        in prose, where an absence claim travels straight to the user."""
        import agent as agent_mod
        from config import (
            AgentConfig, CharacterConfig, MemoryConfig, SessionConfig, ToolsConfig,
        )
        try:
            from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
        except ImportError:
            from role_artifact_stub import STUB_ROLE_ARTIFACT

        cfg = AgentConfig(
            role_artifact=STUB_ROLE_ARTIFACT, role="finance",
            model="claude-sonnet-4-6", system_prompt="You are finance",
            character=CharacterConfig(name="Finance"), enabled=True,
            tools=ToolsConfig(allowed=["Read"], permission_mode="acceptEdits"),
            memory=MemoryConfig(token_budget=4000),
            session=SessionConfig(strategy="ephemeral", idle_timeout=0),
        )
        monkeypatch.setattr(
            agent_mod, "active_semantic_memory", _FakeSpecialistSem(), raising=False)
        agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram", "chat_id": "abc",
            "cid": "cid581", "scope": "personal", "delegation_depth": 0,
        })
        _FakeSpecialistClient.captured_prompt = ""
        with patch.object(tools, "ClaudeSDKClient", _FakeSpecialistClient):
            await tools._run_delegated_agent(
                cfg, task_text="how is Q1 cashflow?", context_text="")
        prompt = _FakeSpecialistClient.captured_prompt
        assert '<memory_context agent="finance">' in prompt
        assert "Q1 spend" in prompt, "the digest must still be usable"
        block = prompt.split('<memory_context agent="finance">')[1].split(
            "</memory_context>")[0]
        assert EXPECTED_PROMPT_LINE in block


# ---------------------------------------------------------------------------
# Consumer 4 — the executor archive's lessons block
# ---------------------------------------------------------------------------


class TestExecutorArchiveBlock:
    async def test_lessons_block_carries_the_note(self, monkeypatch):
        """Sol, design round 1: this block reaches BOTH executor drivers
        through this one helper, and it is filtered twice over — by the
        launching turn's clearance and by the doctrine epoch."""
        import agent as agent_mod
        monkeypatch.setattr(
            agent_mod, "active_semantic_memory", object(), raising=False)

        async def _fake_recall(*a, **kw):
            return "- A prior source recorded: always run make setup first."
        monkeypatch.setattr(tools, "delegated_recall", _fake_recall)

        out = await tools._fetch_executor_archive(
            task="set up the dev loop", origin_channel="telegram",
            token_budget=2000,
        )
        assert "Prior engagements" in out
        assert "make setup" in out, "the digest must still be usable"
        assert EXPECTED_PROMPT_LINE in out

    async def test_empty_archive_stays_silent(self, monkeypatch):
        """The framing must not resurrect the empty case: "" renders as no
        block at all, and absence of a block is not a claim of absence (#201).
        A bare instruction line with no lessons under it would be exactly the
        fabricated header this helper's docstring forbids."""
        import agent as agent_mod
        monkeypatch.setattr(
            agent_mod, "active_semantic_memory", object(), raising=False)

        async def _empty(*a, **kw):
            return ""
        monkeypatch.setattr(tools, "delegated_recall", _empty)

        assert await tools._fetch_executor_archive(
            task="anything", origin_channel="telegram", token_budget=2000) == ""


# ---------------------------------------------------------------------------
# Caller inventory — the guard against the fifth consumer
# ---------------------------------------------------------------------------

# Every call site of a slice-producing function, keyed by (module, enclosing
# function) so it survives renumbering, with how that site frames a NON-EMPTY
# result. A new call site fails the test below until it is declared here —
# which is the point: #581 existed because three consumers were added over time
# and none of them inherited the framing the first one grew.
DECLARED_SLICE_CALLERS: dict[tuple[str, str], str] = {
    # render_recall — the typed renderer itself
    ("tools.py", "recall_memory"): "frames: tool-result message",
    ("agent.py", "_build_options"): "frames: instruction line in <memory_context>",
    ("delegated_memory.py", "delegated_recall"): (
        "renders only; its four callers frame (or are exempt) individually"
    ),
    # delegated_recall — the shared delegated read
    ("tools.py", "_run_delegated_agent"): (
        "frames: instruction line in <memory_context agent=...>"
    ),
    ("tools.py", "_fetch_executor_archive"): (
        "frames: instruction line under the lessons heading"
    ),
    ("tools.py", "query_engager"): (
        "exempt: the synthesizer is constrained to the context and must emit "
        "UNKNOWN when it does not answer, which routes to the framed arm"
    ),
    # A DIFFERENT render_recall — semantic_memory's legacy flat "- {text}"
    # renderer for the untyped recall() seam, which no consumer calls. Declared
    # rather than filtered out by name, so that wiring a consumer to the legacy
    # path has to pass through this table too.
    ("hindsight_memory.py", "recall"): (
        "exempt: legacy untyped recall(); no model-facing consumer calls it"
    ),
}

_SLICE_FUNCS = frozenset({"render_recall", "delegated_recall"})


def _call_sites(path: Path) -> set[tuple[str, str]]:
    """(file, enclosing function) for every call to a slice-producing function.

    Line numbers are deliberately not part of the key — this guard must survive
    ordinary edits and fail only on a genuinely NEW consumer.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[tuple[str, str]] = set()

    class _V(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def _enter(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_FunctionDef = _enter
        visit_AsyncFunctionDef = _enter

        def visit_Call(self, node):
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name in _SLICE_FUNCS and self.stack:
                found.add((path.name, self.stack[-1]))
            self.generic_visit(node)

    _V().visit(tree)
    return found


def test_every_slice_consumer_is_declared():
    sites: set[tuple[str, str]] = set()
    for path in sorted(CASA_ROOT.rglob("*.py")):
        sites |= _call_sites(path)
    undeclared = sites - set(DECLARED_SLICE_CALLERS)
    assert not undeclared, (
        "a new caller renders a clearance-filtered memory slice and has not "
        "declared how it frames a NON-EMPTY result (#581). Add it to "
        f"DECLARED_SLICE_CALLERS with its disposition: {sorted(undeclared)}"
    )


def test_the_inventory_is_not_vacuous():
    """The guard above passes trivially if the parse finds nothing. Pin that
    it actually resolves the known sites."""
    sites: set[tuple[str, str]] = set()
    for path in sorted(CASA_ROOT.rglob("*.py")):
        sites |= _call_sites(path)
    for expected in (
        ("tools.py", "recall_memory"),
        ("agent.py", "_build_options"),
        ("tools.py", "_fetch_executor_archive"),
    ):
        assert expected in sites, f"caller scan missed {expected}"
