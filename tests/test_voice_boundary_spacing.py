"""#257 — a streamed voice reply must survive being reassembled.

The companion integration feeds every `block` frame straight into Home
Assistant's delta stream and HA concatenates the deltas verbatim, so the
whitespace between two blocks is not decoration: it is the only thing
keeping "I'm here." and "What's on your mind?" apart. These tests pin the
whole path — splitter, tag adapter, and both transports — against the
reassembled text, not against individual frames.
"""

import asyncio
import json

import pytest
from aiohttp import web, WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from voice_auth_helpers import SigningVoiceClient, VOICE_TEST_SECRET

from bus import BusMessage, MessageBus, MessageType
from casa_core_middleware import cid_middleware
from channels.voice.channel import VoiceChannel

pytestmark = pytest.mark.unit


GARY_TEXT = "Yep, I'm here. What's on your mind?"

# The same reply chunked into SDK deltas four ways. Chunking decides WHERE
# the separator is lost, so all four have to hold: the pre-#257 splitter
# only dropped whitespace that was already buffered when the sentence mark
# closed a block, which made "boundary lands exactly on the mark" look
# healthy while the ordinary multi-character delta squashed the sentence.
GARY_CHUNKINGS = {
    "one_delta": [GARY_TEXT],
    "separator_inside_delta": ["Yep, I'm here. What's on", " your mind?"],
    "boundary_on_the_mark": ["Yep, I'm here.", " What's on", " your mind?"],
    "char_by_char": list(GARY_TEXT),
}
GARY_DELTAS = GARY_CHUNKINGS["separator_inside_delta"]


class _DeltaAgent:
    """Streams a fixed cumulative-token sequence, then returns."""

    def __init__(self, role: str, deltas: list[str], *, progress: bool = False):
        self._role = role
        self._deltas = deltas
        self._progress = progress

    async def handle_message(self, msg: BusMessage) -> BusMessage:
        if self._progress:
            sink = msg.context.get("_progress_sink")
            if sink:
                await sink("One moment — checking.")
        on_token = msg.context.get("_on_token")
        accumulated = ""
        if on_token:
            for delta in self._deltas:
                accumulated += delta
                await on_token(accumulated)
        return BusMessage(
            type=MessageType.RESPONSE, source=self._role, target=msg.source,
            content=accumulated, reply_to=msg.id, channel=msg.channel,
            context=msg.context,
        )


class _NonPrefixAgent:
    """Speaks a progress line, streams a partial thought, then corrects
    itself with a cumulative that does NOT extend the previous one — the
    AR-B mid-turn SDK retry, which rebuilds the splitter from scratch."""

    def __init__(self, role: str, *, progress: bool = True) -> None:
        self._role = role
        self._progress = progress

    async def handle_message(self, msg: BusMessage) -> BusMessage:
        if self._progress:
            sink = msg.context.get("_progress_sink")
            if sink:
                await sink("One moment — checking.")
        on_token = msg.context.get("_on_token")
        if on_token:
            await on_token("Attempt one talking")   # buffered, never flushed
            await on_token("Hi. All done.")         # non-prefix correction
        return BusMessage(
            type=MessageType.RESPONSE, source=self._role, target=msg.source,
            content="Hi. All done.", reply_to=msg.id, channel=msg.channel,
            context=msg.context,
        )


class _VanishingEpochAgent:
    """Progress line, then an epoch whose only block renders to nothing
    under dialect `none` (so it owes stream whitespace), then a non-prefix
    correction. The epoch's whitespace must die with the epoch while the
    progress line's separator survives — otherwise the two stack up."""

    def __init__(self, role: str, first: str = "  [warm]\n\n", *,
                 progress: bool = True) -> None:
        self._role = role
        self._first = first
        self._progress = progress

    async def handle_message(self, msg: BusMessage) -> BusMessage:
        sink = msg.context.get("_progress_sink") if self._progress else None
        if sink:
            await sink("One moment — checking.")
        on_token = msg.context.get("_on_token")
        if on_token:
            await on_token(self._first)
            await on_token("Hi. All done.")  # non-prefix correction
        return BusMessage(
            type=MessageType.RESPONSE, source=self._role, target=msg.source,
            content="Hi. All done.", reply_to=msg.id, channel=msg.channel,
            context=msg.context,
        )


class _WrittenThenCorrectedAgent:
    """Writes a real block, then a block that renders to nothing (so the
    whitespace between them is owed to the wire, not to the buffer), then
    corrects itself non-prefix. The owed gap anchors "Old." to whatever the
    new epoch says — dropping it welds "Old.Hi."."""

    def __init__(self, role: str) -> None:
        self._role = role

    async def handle_message(self, msg: BusMessage) -> BusMessage:
        on_token = msg.context.get("_on_token")
        if on_token:
            await on_token("Old. [warm]\n\n")
            await on_token("Hi. All done.")  # non-prefix correction
        return BusMessage(
            type=MessageType.RESPONSE, source=self._role, target=msg.source,
            content="Hi. All done.", reply_to=msg.id, channel=msg.channel,
            context=msg.context,
        )


class _BufferedGapAgent:
    """The gap after a written block can still be INSIDE the splitter (its
    buffer or pending separator) rather than owed by the channel — and the
    reset throws that splitter away."""

    def __init__(self, role: str, first: str = "Old. ") -> None:
        self._role = role
        self._first = first

    async def handle_message(self, msg: BusMessage) -> BusMessage:
        on_token = msg.context.get("_on_token")
        if on_token:
            await on_token(self._first)
            await on_token("Hi. All done.")  # non-prefix correction
        return BusMessage(
            type=MessageType.RESPONSE, source=self._role, target=msg.source,
            content="Hi. All done.", reply_to=msg.id, channel=msg.channel,
            context=msg.context,
        )


class _Cfg:
    memory = type("M", (), {"token_budget": 800})()
    role = "butler"
    voice_errors: dict[str, str] = {}
    channels: list[str] = ["ha_voice"]

    def __init__(self, dialect: str = "square_brackets") -> None:
        self.tts = type("T", (), {"tag_dialect": dialect})()


async def _client_for(agent: _DeltaAgent, cfg: _Cfg):
    """A running VoiceChannel serving one stub agent, both transports."""
    bus = MessageBus()
    bus.register("butler", agent.handle_message)
    loop_task = asyncio.create_task(bus.run_agent_loop("butler"))
    channel = VoiceChannel(
        bus=bus, default_agent="butler", webhook_secret=VOICE_TEST_SECRET,
        sse_path="/api/converse", ws_path="/api/converse/ws",
        agent_configs={"butler": cfg},
        idle_timeout=300,
    )
    app = web.Application(middlewares=[cid_middleware])
    channel.register_routes(app)
    return app, loop_task


async def sse_blocks(deltas: list[str] | None = None, *,
                     dialect: str = "square_brackets",
                     progress: bool = False, agent=None) -> list[str]:
    """Every `block` frame's text from one SSE turn, in order."""
    app, loop_task = await _client_for(
        agent or _DeltaAgent("butler", deltas or [], progress=progress),
        _Cfg(dialect),
    )
    try:
        async with TestClient(TestServer(app)) as raw:
            client = SigningVoiceClient(raw)
            resp = await client.post("/api/converse", json={
                "prompt": "hi", "agent_role": "butler", "scope_id": "s-257",
            })
            assert resp.status == 200
            out: list[str] = []
            event = None
            async for line in resp.content:
                s = line.decode("utf-8").rstrip("\r\n")
                if s.startswith("event:"):
                    event = s.split(":", 1)[1].strip()
                elif s.startswith("data:") and event == "block":
                    out.append(json.loads(s.split(":", 1)[1].strip())["text"])
            return out
    finally:
        loop_task.cancel()


async def ws_blocks(deltas: list[str] | None = None, *,
                    dialect: str = "square_brackets",
                    progress: bool = False, agent=None) -> list[str]:
    """Every `block` frame's text from one WS turn, in order."""
    app, loop_task = await _client_for(
        agent or _DeltaAgent("butler", deltas or [], progress=progress),
        _Cfg(dialect),
    )
    try:
        async with TestClient(TestServer(app)) as raw:
            client = SigningVoiceClient(raw)
            out: list[str] = []
            async with client.ws_connect("/api/converse/ws") as ws:
                await ws.send_json({
                    "type": "utterance", "utterance_id": "u-257",
                    "text": "hi", "agent_role": "butler",
                    "scope_id": "s-257",
                })
                async for msg in ws:
                    if msg.type != WSMsgType.TEXT:
                        break
                    frame = json.loads(msg.data)
                    if frame["type"] == "block":
                        out.append(frame["text"])
                    if frame["type"] in ("done", "error"):
                        break
            return out
    finally:
        loop_task.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("chunking", sorted(GARY_CHUNKINGS))
class TestReassembledReply:
    async def test_sse_reply_reassembles_with_its_spacing(self, chunking):
        blocks = await sse_blocks(GARY_CHUNKINGS[chunking])
        assert "".join(blocks) == GARY_TEXT
        # …and it really was split — this is not a single-frame turn.
        assert len(blocks) > 1

    async def test_ws_reply_reassembles_with_its_spacing(self, chunking):
        blocks = await ws_blocks(GARY_CHUNKINGS[chunking])
        assert "".join(blocks) == GARY_TEXT
        assert len(blocks) > 1

    async def test_no_sentence_is_welded_to_the_next(self, chunking):
        """The exact symptom: 'here.What's'."""
        deltas = GARY_CHUNKINGS[chunking]
        for blocks in (await sse_blocks(deltas), await ws_blocks(deltas)):
            assert "here.What's" not in "".join(blocks)


@pytest.mark.asyncio
class TestReplyShapes:
    @pytest.mark.parametrize("deltas,expected", [
        # Whitespace around an em dash inside one long clause (the cap
        # breaks on the dash — both spaces must survive).
        ([("a" * 190) + " — no speaker to announce it."],
         ("a" * 190) + " — no speaker to announce it."),
        # A paragraph break between two thoughts.
        (["First line.", "\n\nSecond line."], "First line.\n\nSecond line."),
        # Multiple sentences arriving in one delta.
        (["One. Two. Three."], "One. Two. Three."),
        # No separator in the source: none invented.
        (["One.Two."], "One.Two."),
        # A hard cut mid-word at the 200-char cap: no space injected.
        (["b" * 250], "b" * 250),
        # Unterminated tail after a complete sentence.
        (["Done. and then some trailing thought"],
         "Done. and then some trailing thought"),
    ])
    async def test_shapes_round_trip_on_both_transports(self, deltas, expected):
        assert "".join(await sse_blocks(deltas)) == expected
        assert "".join(await ws_blocks(deltas)) == expected


@pytest.mark.asyncio
class TestTagDialects:
    async def test_dialect_none_keeps_the_separator_it_used_to_lstrip(self):
        """`render()` for dialect `none` lstrips, so the separator has to be
        applied after rendering, not through it."""
        deltas = ["[warm] Hi.", " [flat] There."]
        assert "".join(await sse_blocks(deltas, dialect="none")) == "Hi. There."
        assert "".join(await ws_blocks(deltas, dialect="none")) == "Hi. There."

    async def test_dialect_none_tag_only_block_hands_its_separator_on(self):
        """A block that renders to nothing emits no frame — the integration
        drops empty frames, which would take the separator with it."""
        deltas = ["[warm]", "\n\nHi there."]
        for blocks in (await sse_blocks(deltas, dialect="none"),
                       await ws_blocks(deltas, dialect="none")):
            assert "" not in blocks, blocks
            assert "".join(blocks) == "\n\nHi there."

    async def test_parens_dialect_rewrites_and_keeps_spacing(self):
        deltas = ["[warm] Hi.", " [flat] There."]
        assert "".join(await sse_blocks(deltas, dialect="parens")) == (
            "(warm) Hi. (flat) There."
        )


@pytest.mark.asyncio
class TestProgressBlockSpacing:
    """The synthetic "still working" line never goes through the splitter,
    so nothing downstream carries a separator against it."""

    async def test_sse_progress_line_is_separated_from_real_speech(self):
        blocks = await sse_blocks(GARY_DELTAS, progress=True)
        joined = "".join(blocks)
        assert joined == f"One moment — checking. {GARY_TEXT}", joined

    async def test_ws_progress_line_is_separated_from_real_speech(self):
        blocks = await ws_blocks(GARY_DELTAS, progress=True)
        joined = "".join(blocks)
        assert joined == f"One moment — checking. {GARY_TEXT}", joined

    async def test_sse_progress_separator_survives_a_midturn_correction(self):
        """AR-B resets the splitter and drops the old epoch's unsent buffer —
        but the separator the progress line owes was never part of that
        buffer, and dropping it welds "checking.Hi."."""
        blocks = await sse_blocks(agent=_NonPrefixAgent("butler"))
        joined = "".join(blocks)
        assert joined == "One moment — checking. Hi. All done.", joined

    async def test_ws_progress_separator_survives_a_midturn_correction(self):
        blocks = await ws_blocks(agent=_NonPrefixAgent("butler"))
        joined = "".join(blocks)
        assert joined == "One moment — checking. Hi. All done.", joined

    async def test_progress_then_a_tail_only_reply_is_separated(self):
        """A reply with no sentence mark reaches the wire only through
        `flush_tail()` — that path owes the progress separator too."""
        deltas = ["Hi"]
        expected = "One moment — checking. Hi"
        assert "".join(await sse_blocks(deltas, progress=True)) == expected
        assert "".join(await ws_blocks(deltas, progress=True)) == expected

    async def test_progress_gap_yields_to_the_replys_own_separator(self):
        """The progress line's separator is a fallback, not a summand: when
        the reply opens with its own whitespace, that whitespace stands
        alone rather than gaining a space in front of it."""
        deltas = ["\n\nHi. All done."]
        expected = "One moment — checking.\n\nHi. All done."
        assert "".join(await sse_blocks(deltas, progress=True)) == expected
        assert "".join(await ws_blocks(deltas, progress=True)) == expected

    async def test_progress_gap_is_spent_on_the_first_real_block(self):
        """Once a real frame carries the gap, it is spent — a later block
        with no separator of its own must not get a second one."""
        deltas = ["One.Two."]
        expected = "One moment — checking. One.Two."
        assert "".join(await sse_blocks(deltas, progress=True)) == expected
        assert "".join(await ws_blocks(deltas, progress=True)) == expected

    @pytest.mark.parametrize("first,expected", [
        # gap sitting in the splitter's buffer
        ("Old. ", "Old. Hi. All done."),
        # gap consumed into the splitter's pending separator
        ("Old.\n\n", "Old.\n\nHi. All done."),
        # gap spanning both, ahead of text the reset discards
        ("Old. \n\nmore", "Old. \n\nHi. All done."),
    ])
    async def test_gap_held_inside_the_discarded_splitter_survives(
        self, first, expected,
    ):
        """The separator can still be inside the splitter when the reset
        discards it — the gap is not part of the abandoned text."""
        for blocks in (
            await sse_blocks(agent=_BufferedGapAgent("butler", first)),
            await ws_blocks(agent=_BufferedGapAgent("butler", first)),
        ):
            assert "".join(blocks) == expected, repr("".join(blocks))

    async def test_gap_with_nothing_on_the_wire_is_dropped(self):
        """A discarded epoch that never wrote anything leaves a gap with no
        predecessor to anchor it: the reply must not open on whitespace."""
        def agent():
            return _VanishingEpochAgent("butler", progress=False)

        for blocks in (
            await sse_blocks(dialect="none", agent=agent()),
            await ws_blocks(dialect="none", agent=agent()),
        ):
            joined = "".join(blocks)
            assert joined == "Hi. All done.", repr(joined)

    async def test_written_block_keeps_its_gap_across_a_correction(self):
        """A gap owed to text ALREADY on the wire survives the reset that
        discards the rest of its epoch — and when the gap is split across
        the channel and the splitter (here `" "` before a suppressed
        `[warm]` and `"\\n\\n"` after it), both runs are owed: they were
        adjacent in the source once the tag was deleted."""
        for blocks in (
            await sse_blocks(dialect="none",
                             agent=_WrittenThenCorrectedAgent("butler")),
            await ws_blocks(dialect="none",
                            agent=_WrittenThenCorrectedAgent("butler")),
        ):
            joined = "".join(blocks)
            assert joined == "Old. \n\nHi. All done.", repr(joined)

    @pytest.mark.parametrize("first", ["  [warm]\n\n", "\n\n[warm]\n\n"])
    async def test_discarded_epoch_does_not_stack_onto_the_progress_gap(
        self, first,
    ):
        """One separator, not two: the progress line owes a space, and a
        discarded epoch's leftover whitespace is not added to it."""
        for blocks in (
            await sse_blocks(dialect="none",
                             agent=_VanishingEpochAgent("butler", first)),
            await ws_blocks(dialect="none",
                            agent=_VanishingEpochAgent("butler", first)),
        ):
            joined = "".join(blocks)
            assert joined == "One moment — checking. Hi. All done.", repr(
                joined,
            )
