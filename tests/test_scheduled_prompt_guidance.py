"""The surfaces this module enumerates state the closing-silence convention for
a scheduled trigger prompt (#932), and no configurator doctrine file that
names a per-trigger prompt path is missing from that enumeration.

A SCHEDULED turn that delivers its message with ``tools.send_message`` and then
ends with ordinary prose delivers TWO messages to the same operator chat: the
send happens immediately and marks nothing the final-text path can read
(``tools.py`` ``send_message``), and the turn's closing text is then delivered
through ``send_response`` unless it strips to the silence sentinel exactly
(``agent.py``). Neither half may be narrowed — the imperative send stays first
and unconditional (fail-noisy, never a lost reminder: the v0.132.0
morning-briefing lesson), and prose after a sentinel is still delivered (the G-3
recant contract). So the gap is closed PER PROMPT, in the prompt text, which is
what ``set_reminder`` and the event wakes already do for the prompts Casa
authors itself (#511, #534).

This pins the same convention on the surfaces that instruct somebody ELSE to
author a scheduled prompt: the configurator's two trigger recipes, the
operator-facing ``triggers.yaml`` guidance, and the corpus document that records
the convention once, plus the recipe for EDITING an existing per-trigger prompt,
which points at ``trigger/add.md`` for the shape rather than restating it. That
enumeration is checked rather than claimed as a total:
``test_no_doctrine_file_naming_a_per_trigger_prompt_is_unclassified`` fails when a
configurator doctrine file naming a per-trigger prompt path is neither covered
nor exempted with a reason. The TOOLS those surfaces name are checked on a
second axis the same way: every tool declared in the code root is in one of
four recorded buckets, or
``test_no_declared_tool_is_unclassified_for_the_closing_convention`` fails. The
fourth of those buckets exists because a name is not always enough to answer the
question: one declaration can reach both a notification that IS this turn's
delivery and a read that is not, so it is filed as CONDITIONAL and its
membership costs a worked conditional on every surface instead of a name.

It is a pin on TEXT and claims nothing about runtime — a hand-authored prompt
that omits the clause still delivers twice, by design, because the alternative
shapes are the two the pinning tests above refuse.

Read-only: these files are read out of the repository in place, never copied, so
nothing here depends on the tree being writable.
"""
from __future__ import annotations

import ast
import functools
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_RECIPES = (
    REPO_ROOT
    / "casa/rootfs/opt/casa/defaults/agents/executors/configurator"
    / "doctrine/recipes/trigger"
)

# The one wording, carried verbatim by every surface below. Whitespace is
# normalized on both sides so a document may wrap it however its prose wraps.
CONVENTION = (
    "For interval/cron/date prompts whose turn delivers its own message, keep "
    "the send instruction first and unconditional, and end the prompt with: "
    "After the send, output the sentinel `<silent/>` and nothing else."
)

SURFACES = [
    _RECIPES / "add.md",
    _RECIPES / "update.md",
    REPO_ROOT / "casa/DOCS.md",
    REPO_ROOT / "docs/architecture/triggers.md",
]


def _normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


@pytest.mark.parametrize("path", SURFACES, ids=lambda p: p.name)
def test_closing_silence_convention(path):
    assert _normalized(path).count(CONVENTION) == 1


def test_the_webhook_route_is_not_told_to_carry_a_prompt():
    """INV-TRIG-013 guard on this change: a webhook trigger carries no prompt
    at all, so the convention must stay scoped to interval/cron/date and the
    recipe must keep saying the schema refuses a webhook prompt."""
    add = _normalized(_RECIPES / "add.md")
    assert "A webhook trigger has no prompt and the schema refuses one" in add
    assert "`prompt`/`prompt_file` are **refused by the schema** for a webhook" in add


# --- Round 2 of diff review, terra, two S2s, generalised rather than patched ---
#
# Stating the convention is not enough: an EXAMPLE prompt is what the
# configurator copies, and an example that carries the closing clause
# unconditionally silences a turn whose own final text is the delivery
# (`_strips_to_silence` clears it and nothing is sent), while an example that
# carries no ending at all reproduces the original double message. So every
# surface that shows an example prompt shows BOTH shapes, labelled, and no
# surface shows one ending as the default.

SHAPE_A = "After the send, output the sentinel `<silent/>` and nothing else."
SHAPE_B = ("If there is nothing worth sending, output the sentinel `<silent/>` "
           "and nothing else.")

EXAMPLE_SURFACES = [
    _RECIPES / "add.md",
    _RECIPES / "update.md",
    REPO_ROOT / "casa/DOCS.md",
]


@pytest.mark.parametrize("path", EXAMPLE_SURFACES, ids=lambda p: p.name)
def test_both_prompt_shapes_are_shown_wherever_an_example_prompt_is(path):
    """Neither ending may be presented alone: a surface that shows one shape's
    example shows the other's beside it."""
    text = _normalized(path)
    assert SHAPE_A in text
    assert SHAPE_B in text
    assert "shape A" in text and "shape B" in text


@pytest.mark.parametrize("path", [_RECIPES / "add.md", _RECIPES / "update.md"],
                         ids=lambda p: p.name)
def test_the_upsert_template_does_not_hardcode_one_ending(path):
    """The `config_trigger_upsert` template — the line the configurator fills in
    and calls — must defer to the shape, never carry an ending of its own."""
    template = [ln for ln in path.read_text(encoding="utf-8").splitlines()
                if "prompt=\"<" in ln]
    assert len(template) == 1, template
    assert "shape requires" in template[0]
    assert "<silent/>" not in template[0]


@pytest.mark.parametrize("path", EXAMPLE_SURFACES, ids=lambda p: p.name)
def test_shape_a_is_defined_by_the_delivery_tool_not_by_tool_use(path):
    """Gate-owned review round, terra, S2. "The turn uses a tool" is the wrong
    test: a turn may call `ack_event`, read the calendar or query Home Assistant
    and still deliver through its OWN final text, and giving that prompt the
    closing clause suppresses its only delivery. So every surface names the
    DELIVERY tools where it defines the shape, and says in terms that a
    non-delivery tool call does not decide the question."""
    text = _normalized(path)
    assert "send_message" in text and "send_media" in text
    assert "DELIVERY tool call" in text or "delivery tool call" in text


# --- Candidate review, round 1, S2: a FIFTH authoring surface ---------------
#
# `recipes/prompt/edit.md` is where a request to change an EXISTING trigger
# prompt routes: its table names a resident's `prompts/<trigger>.md` as
# editable, and it carried none of this. A prompt edited from it is saved
# without the closing instruction and its trigger goes on delivering twice —
# the defect this change exists to remove, reachable through a surface the
# change did not touch. The recipe's form is a routing table, so it carries the
# convention verbatim and DEFERS the shape distinction to `trigger/add.md`
# rather than growing a second copy of a definition that took three rounds to
# settle.
#
# The second test is the durable half. The prose no longer claims a total, so
# nothing here has to be re-asserted when a surface is added: a doctrine file
# that names a per-trigger prompt path is either covered or exempted with a
# reason, and a new one fails this test until someone says which.

_DOCTRINE = (
    REPO_ROOT
    / "casa/rootfs/opt/casa/defaults/agents/executors/configurator/doctrine"
)
PROMPT_EDIT = _DOCTRINE / "recipes/prompt/edit.md"

# COVERED: the file instructs authoring or editing that prompt's TEXT, so it
# states the convention or points at the recipe that does.
COVERED_DOCTRINE = {
    "recipes/trigger/add.md",
    "recipes/trigger/update.md",
    "recipes/prompt/edit.md",
}
# EXEMPT: the file names the path for some other reason, recorded here.
EXEMPT_DOCTRINE = {
    "recipes/trigger/remove.md": "offers to delete the file; authors no text",
    "reload.md": "picks the reload scope after an edit",
    "architecture.md": "a layout table row saying where the file lives",
    "safety.md": "an authorization row: allowed, but inert without a reload",
}


def test_the_prompt_edit_recipe_carries_the_convention_and_defers_the_shape():
    text = _normalized(PROMPT_EDIT)
    assert text.count(CONVENTION) == 1
    assert "recipes/trigger/add.md" in text
    assert "Every scheduled prompt says how the turn ends" in text
    # One wording, one place: it must not grow its own shape definition, since
    # a second copy is what drifts.
    assert "shape A" not in text and "shape B" not in text


def test_no_doctrine_file_naming_a_per_trigger_prompt_is_unclassified():
    """The enumeration above is checked, not claimed. A doctrine file that
    names `prompts/<...>` fails here until it is covered or exempted."""
    named = {
        str(path.relative_to(_DOCTRINE))
        for path in sorted(_DOCTRINE.rglob("*.md"))
        if "prompts/<" in path.read_text(encoding="utf-8")
    }
    assert named == COVERED_DOCTRINE | set(EXEMPT_DOCTRINE)


# --- #962 / #963: the class the surfaces name, and the rules they state -----
#
# #962: `ask_user` gained a scheduled arm (#573) and so joined the tools that put
# a scheduled turn's message in the operator's chat, and no surface said so. A
# scheduled turn that asks and then narrates delivers twice. #963: the rule that
# a tool call which is not a delivery decides nothing was stated four ways and
# pinned nowhere — the frozen case above stays green with add.md's rule block
# replaced by its opposite. The red cases below each bind their own literals, so
# a later edit to a shared constant cannot quietly change what they pin.

_TOOLS_PY = REPO_ROOT / "casa/rootfs/opt/casa/tools.py"


def test_every_delivering_tool_is_named_on_every_surface():
    """#962. Every tool whose successful call puts a scheduled turn's message
    in the operator's chat is named, by its declared name, on each surface that
    defines how such a prompt ends. The class is stipulated here; whether it is
    still the whole class is what the tool-axis classification checks."""
    tree = ast.parse(_TOOLS_PY.read_text(encoding="utf-8"))
    registry = [
        node for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "CASA_TOOLS"
    ]
    assert len(registry) == 1, ("CASA_TOOLS", len(registry))
    assert isinstance(registry[0].value, ast.Tuple)
    registered = [e.id for e in registry[0].value.elts if isinstance(e, ast.Name)]

    missing = []
    for key in ("send_message", "send_media", "ask_user"):
        assert registered.count(key) == 1, (
            f"tools.py::{key}", "CASA_TOOLS", registered.count(key))
        functions = [
            node for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == key
        ]
        assert len(functions) == 1, (f"tools.py::{key}", "async def", len(functions))
        decorators = [
            d for d in functions[0].decorator_list
            if isinstance(d, ast.Call) and isinstance(d.func, ast.Name)
            and d.func.id == "tool"
        ]
        assert len(decorators) == 1, (f"tools.py::{key}", "@tool", len(decorators))
        first = decorators[0].args[0]
        assert isinstance(first, ast.Constant) and isinstance(first.value, str)
        declared = first.value
        for path in SURFACES:
            count = len(re.findall(
                r"(?<!\w)" + re.escape(declared) + r"(?!\w)", _normalized(path)))
            if count < 1:
                missing.append((f"tools.py::{key}", str(path.relative_to(REPO_ROOT)), count))
    assert missing == []


def test_every_surface_states_the_non_delivery_rule_in_one_wording():
    """Pins a WORDING on five carriers, not a closed class of tools.

    What this pins is a WORDING, not a closed class. Nothing in the tree reads
    these files at runtime — they are prompt surfaces consumed by a model — so
    there is no property here a test could execute, and the strongest assertion
    available is a string match on prose. Measured, it catches the three
    mutations this module was green on before it existed: deleting the rule,
    re-wording it on one surface, and replacing it with its opposite. It catches
    none of these: a contradiction added beside the sentence, a surface that
    carries the sentence and then works its example the other way, or the prompt
    an author actually writes after reading it. A reader must not take this test
    as evidence that the class of tools is closed — what closes that class
    against the code is
    `test_no_declared_tool_is_unclassified_for_the_closing_convention`, and what
    closes nothing at all is the runtime, which is #960's.
    """
    NON_DELIVERY_RULE = (
        "Which prompts the clause belongs to is decided by where the operator's "
        "copy of the message comes from, never by whether the turn calls a tool: "
        "a tool call that is not a delivery decides nothing here."
    )
    counts = {
        str(path.relative_to(REPO_ROOT)): _normalized(path).count(NON_DELIVERY_RULE)
        for path in [*SURFACES, PROMPT_EDIT]
    }
    assert counts == dict.fromkeys(counts, 1)


def test_every_carrier_says_what_a_turn_outputs_when_the_ask_was_not_awaiting():
    """Pins a WORDING on five carriers, not a behaviour.

    A scheduled `ask_user` call can return without posting anything — the
    operator already has a question or an authorization challenge waiting, or
    the post landed no message id — and a turn that then outputs only the
    sentinel delivers NOTHING. Naming `ask_user` as a delivery (the case above)
    is only safe beside this rule. Its reach, stated: it pins that every carrier
    says what the turn outputs when the ask was not awaiting the operator's
    answer; it does not run that turn, and it does not make an agent follow the
    sentence. It deliberately says nothing about `send_message` or `send_media`,
    whose reports are not reliable in either direction (#990), so no rule an
    author could follow from them is stated.
    """
    ASK_RULE = (
        "A turn that asks with `ask_user` has put its question in the chat only "
        "when the ask reports that it is awaiting the operator's answer; when it "
        "reports anything else, the turn outputs what the ask reported as its "
        "final text instead of the sentinel."
    )
    counts = {
        str(path.relative_to(REPO_ROOT)): _normalized(path).count(ASK_RULE)
        for path in [*SURFACES, PROMPT_EDIT]
    }
    assert counts == dict.fromkeys(counts, 1)


# --- The tool axis: the class the surfaces name is checked, not claimed -----
#
# The same checked-not-claimed shape as the FILE axis above, applied to TOOLS.
# Every function declared as a tool anywhere in the code root is in exactly one
# of four recorded buckets, keyed on the FUNCTION (a facade declares its tool
# with a name only the Home Assistant server knows at runtime), so a tool nobody
# has written yet fails the suite until somebody answers the question below.
#
# The buckets are exhaustive and disjoint BY DECLARATION, and both halves are
# asserted: the union equals the declared set, and no key is filed twice. The
# fourth bucket is the one that keeps the classification from re-closing the
# property — see CONDITIONAL_DELIVERY_FAMILY.

_CODE_ROOT = REPO_ROOT / "casa/rootfs/opt/casa"

# The convention counts a call to one of these as the turn's delivery: when it
# reports success, the turn has nothing left to say. That is what the tool is
# FOR, not a guarantee that something landed — `send_message` can report success
# for a send that delivered nothing (#990), and a refused `ask_user` posts
# nothing, which is why the surfaces state `ask_user`'s rule. Named on every
# authoring surface; adding a member here is a prose change too.
DELIVERS_THE_OPERATORS_COPY = {
    "tools.py::send_message",
    "tools.py::send_media",
    "tools.py::ask_user",
}

# Tools a reader could take for a delivery because they put something in a
# chat, each with the reason it is not a scheduled turn's delivery of its
# message. Membership here is a judgement, recorded so it can be contested.
WRITES_TO_A_CHAT_ELSEWHERE = {
    "tools.py::wipe_memory":
        "posts a confirmation keyboard to the operator's DM and is refused "
        "unless the turn is a direct DM or button turn; it has no scheduled arm",
    "channels/casa_engagement_channel.py::reply":
        "the engagement channel's tool: a specialist inside an engagement posts "
        "to that engagement's topic, never a resident's trigger turn",
    "channels/casa_engagement_channel.py::ask":
        "same server and reason as reply: a question posted to an engagement's topic",
    "tools.py::emit_completion":
        "posts an engagement's completion summary to that engagement's topic; "
        "its caller is the engaged executor or specialist, not a scheduled turn",
    "tools.py::cancel_engagement":
        "closes an engagement's topic and notifies the engager; that notice is "
        "Casa's, not the calling turn's message",
    "tools.py::consent_reprompt":
        "re-posts Casa's own consent keyboards to the operator's DM; none of "
        "them is the calling turn's message",
    "tools.py::react":
        "sets a reaction on an active engagement's current inbound message and "
        "does nothing without one; a reaction is not a message",
}

# CONDITIONAL: one declaration, and whether a call to it is the turn's delivery
# depends on which call it is. A member is declared with a name only a remote
# server knows at runtime, and the same declaration reaches both a notification
# that puts THIS turn's message in the operator's chat and a read whose result
# the turn still has to report itself. No name can file it either way, so the
# only thing that decides is the property the surfaces state — and a property an
# author cannot apply is a property that fails. Membership therefore costs a
# WORKED CONDITIONAL: both arms, in one wording, on every naming surface, which
# is what `test_the_conditional_delivery_family_is_worked_on_every_naming_surface`
# asserts.
#
# Why this is a fourth file and not a member of DELIVERS_THE_OPERATORS_COPY:
# filing it as a delivery would name it on the surfaces as one, telling an author
# that a scheduled prompt touching Home Assistant at all takes the closing clause.
# That suppresses the final text of every scheduled turn whose only delivery IS
# its final text — a turn that reads a sensor and reports what it read would
# deliver NOTHING. Requiring both arms to be documented has no such effect.
CONDITIONAL_DELIVERY_FAMILY = {
    "ha_mcp_facade.py::proxy":
        "one proxy per Home Assistant tool, named by the HA server at runtime. A "
        "notify service reached through it puts this turn's message in the "
        "operator's chat and IS the delivery; a read or a device action leaves the "
        "operator's copy to be the turn's own final text",
}

# The worked conditional each member owes, verbatim, on each naming surface. A
# member added to the family above without an entry here fails, and an entry
# whose sentence is not on a surface fails — which is what keeps a member from
# being filed with the question left unanswered for the author.
CONDITIONAL_DELIVERY_EXAMPLES = {
    "ha_mcp_facade.py::proxy": (
        "A Home Assistant notification that carries this turn's message to the "
        "operator is a delivery, including when it is reached through the Home "
        "Assistant proxy, so that prompt takes the clause; a Home Assistant read "
        "or device action whose result the turn then reports is not a delivery, "
        "because the operator's copy is still the turn's own final text."
    ),
}

# NOT a delivery: calling it is not how a scheduled turn delivers its message.
# This bucket claims only that. It is NOT audited for incidental posts — several
# members make Casa post something of its own (a consent prompt, a rewrite
# notice) — so membership here is not a claim that a tool writes nothing.
NOT_A_DELIVERY = {
    "tools.py::ack_event",
    "tools.py::callback_ack_revoke",
    "tools.py::cancel_reminder",
    "tools.py::cancel_voice_job",
    "tools.py::casa_reload",
    "tools.py::casa_reload_triggers",
    "tools.py::casa_restart_supervised",
    "tools.py::cleanup_engagement_topics",
    "tools.py::config_git_commit",
    "tools.py::config_trigger_delete",
    "tools.py::config_trigger_upsert",
    "tools.py::continue_voice_job",
    "tools.py::delegate_to_agent",
    "tools.py::delete_engagement_workspace",
    "tools.py::engage_executor",
    "tools.py::event_ack_revoke",
    "tools.py::get_item_fields",
    "tools.py::get_schedule",
    "tools.py::list_engagement_workspaces",
    "tools.py::list_vault_items",
    "tools.py::peek_engagement_workspace",
    "tools.py::persona_ack_revoke",
    "tools.py::persona_apply",
    "tools.py::persona_install_commit",
    "tools.py::persona_install_inspect",
    "tools.py::persona_list",
    "tools.py::persona_prune",
    "tools.py::persona_remove",
    "tools.py::plugin_add",
    "tools.py::plugin_assign",
    "tools.py::plugin_list",
    "tools.py::plugin_remove",
    "tools.py::plugin_status",
    "tools.py::plugin_unassign",
    "tools.py::plugin_update",
    "tools.py::query_engager",
    "tools.py::recall_memory",
    "tools.py::remove_plugin_env_reference",
    "tools.py::resident_persona_reset",
    "tools.py::resident_persona_swap",
    "tools.py::set_plugin_env_reference",
    "tools.py::set_reminder",
    "tools.py::specialist_install_commit",
    "tools.py::specialist_install_inspect",
    "tools.py::specialist_rollback",
    "tools.py::specialist_uninstall",
    "tools.py::specialist_upgrade",
    "tools.py::trigger_ack_revoke",
    "tools.py::verify_plugin_secrets",
    "tools.py::verify_plugin_state",
    "tools.py::voice_job_status",
}

# The declared tools whose OWN source consults the one shared predicate for "a
# scheduled turn may deliver to the operator" (`tools.py`
# `_scheduled_operator_target`).
SCHEDULED_ELIGIBILITY_CONSUMERS = {
    "tools.py::send_media",
    "tools.py::ask_user",
}

_UNCLASSIFIED_QUESTION = (
    "is a tool nobody has classified. Does a scheduled turn that calls it put "
    "the operator's copy of the turn's message in the chat? If yes, add it to "
    "DELIVERS_THE_OPERATORS_COPY and name it on the four surfaces; if that "
    "depends on WHICH call it is, add it to CONDITIONAL_DELIVERY_FAMILY and work "
    "both arms on those surfaces; if it writes to a chat for another reason, add "
    "it to WRITES_TO_A_CHAT_ELSEWHERE with that reason; otherwise add it to "
    "NOT_A_DELIVERY."
)


@functools.lru_cache(maxsize=1)
def _declared_tools() -> dict[str, str]:
    """`<module path under the code root>::<function>` -> that function's own
    source, for every function whose decorator's identifier is `tool` — the SDK
    form `@tool(...)` and the FastMCP form `@server.tool()` alike. AST over
    source text: nothing under the code root is imported."""
    found: dict[str, str] = {}
    for path in sorted(_CODE_ROOT.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                target = decorator.func if isinstance(decorator, ast.Call) else decorator
                ident = (target.id if isinstance(target, ast.Name)
                         else target.attr if isinstance(target, ast.Attribute)
                         else None)
                if ident == "tool":
                    key = f"{path.relative_to(_CODE_ROOT).as_posix()}::{node.name}"
                    assert key not in found, ("declared twice", key)
                    found[key] = ast.get_source_segment(source, node) or ""
    return found


def test_no_declared_tool_is_unclassified_for_the_closing_convention():
    """Classification BY DECLARATION: the input is the decorator, not what the
    body does, so a tool nobody has written yet fails here until it is filed.

    What it does not close, stated so a green run is not over-read: an EXISTING
    tool that starts writing to a chat by a route other than the shared
    eligibility predicate (the next test sees only that route); a tool a plugin
    supplies, which lives outside the code root and is never scanned; a decorator
    whose identifier is not `tool`; and a wrong answer — a chat-writing tool filed
    in NOT_A_DELIVERY passes. The check makes the classification a recorded,
    reviewable act in the diff; it cannot make it a correct one."""
    declared = set(_declared_tools())
    buckets = [DELIVERS_THE_OPERATORS_COPY, set(CONDITIONAL_DELIVERY_FAMILY),
               set(WRITES_TO_A_CHAT_ELSEWHERE), NOT_A_DELIVERY]
    filed_twice = sorted(
        key for i, first in enumerate(buckets) for second in buckets[i + 1:]
        for key in first & second
    )
    assert filed_twice == []
    classified = set().union(*buckets)
    unclassified = sorted(declared - classified)
    assert unclassified == [], f"{unclassified} {_UNCLASSIFIED_QUESTION}"
    assert sorted(classified - declared) == []

    # The one registration path the decorator scan cannot see: a tool handed to
    # the framework server without a decorator. `CASA_TOOLS` must list exactly
    # the functions `tools.py` declares.
    tree = ast.parse((_CODE_ROOT / "tools.py").read_text(encoding="utf-8"))
    registry = [
        node for node in tree.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
        and node.target.id == "CASA_TOOLS"
    ]
    assert len(registry) == 1, ("CASA_TOOLS", len(registry))
    registered = [e.id if isinstance(e, ast.Name) else ast.dump(e)
                  for e in registry[0].value.elts]
    in_tools_py = {key.split("::", 1)[1] for key in declared if key.startswith("tools.py::")}
    assert sorted(set(registered) ^ in_tools_py) == []
    assert len(registered) == len(set(registered))


def test_only_the_recorded_tools_use_the_scheduled_delivery_eligibility():
    """The totality check above would NOT have caught #962: `ask_user` already
    existed, and was already classified, when #573 gave it a scheduled arm. A
    check on declarations fires on a new declaration, not on a new capability of
    an old one. This fires on that event: a declared tool whose own source starts
    consulting the shared scheduled-delivery predicate. It is lexical containment
    in the decorated function's own source, not a call-graph walk — a helper that
    consults the predicate on a tool's behalf is not seen."""
    consumers = {
        key for key, source in _declared_tools().items()
        if "_scheduled_operator_target" in source
    }
    assert sorted(consumers ^ SCHEDULED_ELIGIBILITY_CONSUMERS) == [], (
        "a tool gained or lost the scheduled-delivery eligibility; decide whether "
        "it now puts a scheduled turn's message in the operator's chat, and update "
        "DELIVERS_THE_OPERATORS_COPY and the four surfaces with it")
    assert SCHEDULED_ELIGIBILITY_CONSUMERS <= DELIVERS_THE_OPERATORS_COPY


def test_the_conditional_delivery_family_is_worked_on_every_naming_surface():
    """A tool filed as CONDITIONAL is filed with a question still open — WHICH
    call is the delivery — and the only place that question can be answered is
    the prose an author reads. So membership costs a worked conditional, both
    arms, in one wording, on each of the four surfaces that name the class.

    It is not filed as a delivery instead: naming it on the surfaces as one
    would tell an author that any scheduled prompt touching that tool takes the
    closing clause, and a turn whose only delivery IS its final text would then
    be suppressed and deliver nothing. Requiring the two arms to be documented
    does not carry that instruction.

    `prompt/edit.md` is deliberately NOT in this set. It carries the rule and
    defers which tool calls put the copy there to `recipes/trigger/add.md`,
    which a committed test pins; a worked conditional there would be the second
    copy of a classification that recipe owns.

    Reach, stated so a green run is not over-read: this pins a WORDING on four
    prose surfaces. It does not run a Home Assistant notification, does not know
    whether a given proxy call is one, and does not make an agent apply the
    sentence to the prompt it writes.
    """
    assert sorted(CONDITIONAL_DELIVERY_EXAMPLES) == sorted(CONDITIONAL_DELIVERY_FAMILY), (
        "every member of CONDITIONAL_DELIVERY_FAMILY owes a worked conditional "
        "naming both arms; add it here and to the four surfaces")
    counts = {
        (key, str(path.relative_to(REPO_ROOT))): _normalized(path).count(example)
        for key, example in CONDITIONAL_DELIVERY_EXAMPLES.items()
        for path in SURFACES
    }
    assert counts == dict.fromkeys(counts, 1)
