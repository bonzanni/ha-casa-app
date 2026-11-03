"""The capture eligibility table, as one function (``memory/eligibility.py``).

One parametrised matrix over every row of the table. Channel write-trust is
read through the real ``channel_policy.writes_to_bank``, so a channel the
policy does not trust captures nothing here either.
"""
from __future__ import annotations

import pytest

from channel_policy import writes_to_bank
from memory.eligibility import Source, admit

T, R = Source.TURN, Source.RESIDENT_MESSAGE
DQ, DA = Source.DELEGATED_REQUEST, Source.DELEGATED_ANSWER
ET, EA, ES = (Source.ENGAGEMENT_TURN, Source.ENGAGEMENT_ANSWER,
              Source.ENGAGEMENT_SUMMARY)
SP = Source.SPECIALIST_POST

MATRIX = [
    # source, channel, facts, admitting rule
    ("operator DM", T, "telegram", {}, "write_trusted_turn"),
    ("voice", T, "voice", {}, None),
    ("webhook", T, "webhook", {}, None),
    ("unknown channel", T, "carrier-pigeon", {}, None),
    ("scheduled on the DM", T, "telegram", {"scheduled": True},
     "scheduled_turn"),
    ("notice of an excluded specialist", T, "telegram",
     {"excluded_notice": True}, None),
    ("blank turn", T, "telegram", {"text": "  \n"}, None),
    ("resident message", R, "telegram", {}, "resident_message"),
    ("resident relay of an excluded notice", R, "telegram",
     {"excluded_notice": True}, "resident_message"),
    ("resident message on voice", R, "voice", {}, None),
    ("delegated request", DQ, "telegram", {}, "delegated_request"),
    ("request, answer incomplete", DQ, "telegram",
     {"answer_incomplete": True}, "delegated_request"),
    ("blank request", DQ, "telegram", {"text": " "}, None),
    ("request in a continuation or hand-off", DQ, "telegram",
     {"resident_request": False}, None),
    ("request to a role that writes no memory", DQ, "telegram",
     {"role_writes_memory": False}, None),
    ("request of a stored call", DQ, "telegram", {"stored_call": True}, None),
    ("request from voice", DQ, "voice", {}, None),
    ("complete answer", DA, "telegram", {}, "delegated_answer"),
    ("answer of a hand-off", DA, "telegram", {"resident_request": False},
     "delegated_answer"),
    ("incomplete answer", DA, "telegram", {"answer_incomplete": True}, None),
    ("answer of a stored call", DA, "telegram", {"stored_call": True}, None),
    ("answer of a role that writes no memory", DA, "telegram",
     {"role_writes_memory": False}, None),
    ("answer to voice", DA, "voice", {}, None),
    ("engagement topic turn", ET, "telegram", {}, "engagement_turn"),
    ("engagement turn, no-memory role", ET, "telegram",
     {"role_writes_memory": False}, None),
    ("engagement answer", EA, "telegram", {}, "engagement_answer"),
    ("engagement answer, no-memory role", EA, "telegram",
     {"role_writes_memory": False}, None),
    ("engagement summary", ES, "telegram", {}, "engagement_summary"),
    ("summary of a no-memory specialist", ES, "telegram",
     {"role_writes_memory": False}, None),
    ("summary of a voice-started engagement", ES, "voice", {}, None),
    ("post line of a no-memory specialist", SP, "telegram",
     {"role_writes_memory": False}, "specialist_post"),
    ("post line of a specialist that keeps memory", SP, "telegram", {},
     None),
    ("post line on voice", SP, "voice", {"role_writes_memory": False}, None),
]


@pytest.mark.parametrize("name,source,channel,facts,expected", MATRIX,
                         ids=[row[0] for row in MATRIX])
def test_eligibility_matrix(name, source, channel, facts, expected):
    facts = dict(facts)
    text = facts.pop("text", "something said")
    assert admit(source, text=text, channel_writes=writes_to_bank(channel),
                 **facts) == expected


def test_every_source_has_a_row():
    assert {row[1] for row in MATRIX} == set(Source)
