"""The decision step of a live session, against the local fake of the service.

No network: the one model call goes to ``FakeOpenAI`` on localhost."""
from __future__ import annotations

import aiohttp
import pytest

from live_voice import decision
from live_voice.actions import Outcome
from live_voice.api import LiveApi
from live_voice.session import Ability, Line
from phone_fakes import FakeOpenAI, function_call, message

KEY = "sk-test-not-a-real-key"
PARAMETERS = {"properties": {"why": {"type": "string"}}, "required": ["why"]}


async def _never(args: dict, round: int) -> Outcome:
    raise AssertionError("the decision step never runs an ability")


ABILITIES = (
    Ability("transfer", "Hand the session over.", PARAMETERS, _never, "plain"),
    Ability("end_call", "End the session.", PARAMETERS, _never, "final"),
)
LINES = (
    Line("agent", "Hello, who is this?", 0, 900),
    Line("caller", "It is Sam. Can you help me?", 1500, 3000),
    Line("agent", "Of course.", 3200, 3800),
)


@pytest.fixture
async def fake():
    async with FakeOpenAI() as server:
        yield server


@pytest.fixture
async def api(fake):
    async with aiohttp.ClientSession() as http:
        yield LiveApi(key=KEY, http=http, timeout_s=2.0, base_url=fake.base_url)


async def _decide(api, *, lines=LINES, timeout_s=2.0):
    return await decision.decide(
        api, model="decision-m", instructions="decision rules", lines=lines,
        abilities=ABILITIES, timeout_s=timeout_s)


async def test_the_input_is_the_conversation_so_far(fake, api) -> None:
    fake.responses_queue = [[function_call("end_call", {"why": "goodbye said"})]]
    assert await _decide(api) == ("end_call", {"why": "goodbye said"})
    verb, _, body = fake.requests[0]
    assert verb == "responses"
    assert body["input"] == ("The call so far:\n"
                             "Assistant: Hello, who is this?\n"
                             "Caller: It is Sam. Can you help me?\n"
                             "Assistant: Of course.")
    assert (body["model"], body["instructions"]) == ("decision-m", "decision rules")
    assert [tool["name"] for tool in body["tools"]] == ["transfer", "end_call"]
    assert body["tools"][0] == {
        "type": "function", "name": "transfer",
        "description": "Hand the session over.",
        "parameters": {"type": "object",
                       "properties": {"why": {"type": "string"}},
                       "required": ["why"], "additionalProperties": False}}


async def test_no_function_call_is_a_clean_none(fake, api) -> None:
    fake.responses_queue = [[message("none")]]
    assert await _decide(api) is None


async def test_an_ability_that_was_not_granted_is_ignored(fake, api) -> None:
    fake.responses_queue = [
        [function_call("open_the_door", {})],
        [function_call("open_the_door", {}), function_call("transfer", {"why": "x"})],
    ]
    assert await _decide(api) is None
    assert await _decide(api) == ("transfer", {"why": "x"})


async def test_an_api_error_is_a_failed_decision(fake, api) -> None:
    fake.responses_queue = [RuntimeError("boom")]
    assert await _decide(api) is decision.FAILED


async def test_a_timeout_is_a_failed_decision(fake, api) -> None:
    fake.hang_for.add("responses")
    assert await _decide(api, timeout_s=0.1) is decision.FAILED
