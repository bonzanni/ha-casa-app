"""``plugin_dispatch.compose`` — the shared execution-target selection over a
plugin registry entry's ``targets`` list.

Extracted from ``callback_episodes._compose`` and
``plugin_setup_episodes._compose`` (byte-identical target-order behavior in
both): ``resident:assistant`` when targeted, else the lexicographically
first resident, else the first specialist via assistant delegation, else no
target at all.
"""
import plugin_dispatch as pd


def test_resident_assistant_is_preferred_when_present():
    entry = {"targets": ["resident:zeta", "resident:assistant"]}
    role, instruction = pd.compose(entry, "do the thing")
    assert role == "assistant"
    assert instruction == "do the thing"


def test_lexicographically_first_resident_when_no_assistant():
    entry = {"targets": ["resident:mars", "resident:aqua"]}
    role, instruction = pd.compose(entry, "do the thing")
    assert role == "aqua"
    assert instruction == "do the thing"


def test_first_specialist_via_assistant_delegation():
    entry = {"targets": ["specialist:zulu", "specialist:finance"]}
    role, instruction = pd.compose(entry, "do the thing")
    assert role == "assistant"
    # Lexicographically first specialist chosen, delegation instruction
    # names it exactly and forbids substitution.
    assert instruction == (
        "[casa system notice — not a message from the operator] "
        "Delegate to the specialist 'finance' with mode='sync' and the "
        "instruction: do the thing Do not substitute another agent.")


def test_no_resident_or_specialist_target():
    entry = {"targets": []}
    role, reason = pd.compose(entry, "do the thing")
    assert role is None
    assert reason == "no resident or specialist target"


def test_missing_targets_key_is_no_target():
    entry = {}
    role, reason = pd.compose(entry, "do the thing")
    assert role is None
    assert reason == "no resident or specialist target"


def test_resident_beats_specialist():
    entry = {"targets": ["specialist:finance", "resident:aqua"]}
    role, instruction = pd.compose(entry, "do the thing")
    assert role == "aqua"
    assert instruction == "do the thing"


def test_executor_only_target_is_no_target():
    """A target that is neither ``resident:`` nor ``specialist:`` prefixed
    contributes to neither list."""
    entry = {"targets": ["executor:something"]}
    role, reason = pd.compose(entry, "do the thing")
    assert role is None
    assert reason == "no resident or specialist target"


def test_execution_role_matches_compose_for_residents():
    # #423 r2 (Terra 1): the role whose SESSION runs the tool — identical to
    # the dispatch role on the resident branches.
    assert pd.execution_role(
        {"targets": ["resident:zeta", "resident:assistant"]}) == "assistant"
    assert pd.execution_role(
        {"targets": ["resident:zeta", "resident:aqua"]}) == "aqua"


def test_execution_role_is_the_specialist_on_the_delegation_branch():
    # compose dispatches to the assistant, but the SPECIALIST executes —
    # readiness gates must check the executing session, not the courier.
    assert pd.execution_role(
        {"targets": ["specialist:finance"]}) == "finance"
    assert pd.execution_role(
        {"targets": ["specialist:zz", "specialist:finance"]}) == "finance"


def test_execution_role_none_without_resident_or_specialist():
    assert pd.execution_role({"targets": ["executor:something"]}) is None
    assert pd.execution_role({}) is None


def test_execution_ready_lazy_agent_is_ready():
    # #423 r2 (Sol 1/Terra 1): an agent with NO published snapshot resolves
    # the current registry+environment at its next turn (FR3), so the setup
    # turn itself triggers a fresh, post-secrets build.
    from types import SimpleNamespace
    a = SimpleNamespace(plugin_binding_snapshot=None)
    assert pd.execution_ready(a, "gmail", "art-1") is True


def test_execution_ready_requires_matching_published_binding():
    from types import SimpleNamespace
    snap = SimpleNamespace(binding={"gmail": "art-1"})
    a = SimpleNamespace(plugin_binding_snapshot=snap)
    assert pd.execution_ready(a, "gmail", "art-1") is True
    assert pd.execution_ready(a, "gmail", "art-2") is False
    withheld = SimpleNamespace(plugin_binding_snapshot=SimpleNamespace(binding={}))
    assert pd.execution_ready(withheld, "gmail", "art-1") is False


def test_execution_ready_missing_agent_is_not_ready():
    assert pd.execution_ready(None, "gmail", "art-1") is False
