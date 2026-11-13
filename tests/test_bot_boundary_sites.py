"""The bot boundary's closed site lists.

Once Casa runs more than one Telegram bot, two things must hold everywhere:
a turn's route (the reserved ``_tg_bot`` key: the bot it delivers through) is
decided where the turn is born and carried by every derivation, and every
identity that two bots could otherwise produce equal names the bot. Neither
can be checked by a test of one site; both are kept by recorded lists.

Each list below is produced by a fixed source search over the code root, and
each hit is recorded by its enclosing function with the number of hits there
and a classification. A new hit (or a changed count) fails until someone
records it and, in doing so, answers the question the list asks:

* where an origin or delivery context is built: is it born at an inbound
  update (INBOUND), derived from another origin (DERIVED, naming its case in
  the derivation sweep of ``test_bot_boundary_invariants.py``), born with no
  parent (FRESH, naming the test of its birth), Casa's own notice (RULE_C),
  an identity formed before its post (PRE_POST), a stored record (PERSISTED),
  a raw call on one endpoint's own bot (OUTBOUND), not Telegram at all
  (NON_TELEGRAM), or not an origin (NOISE);
* where a key could hold a value two bots share: does it carry the bot
  (RULE_K, naming the test that keeps two bots apart), is it safe without it
  (SAFE, with the reason), does it live on one endpoint (ENDPOINT), or is it
  not a key (NOISE);
* where a route is read: to resolve, carry, store or stamp it (ROUTE), or as
  the bot an inbound message arrived on (RECEIPT);
* where a bot id is read: at receipt (RECEIPT), after the post that names it
  returned (POST_TIME), before a post that must already name it (PRE_POST,
  naming the test of the window before Casa's bot has any id), off a value
  that already carries it (FIELD), or to resolve a route (RESOLUTION).

These catch an accidental omission; they are not a proof.
"""
from __future__ import annotations

import ast
import collections
import functools
import pathlib
import re

import pytest

pytestmark = [pytest.mark.unit]

CODE = pathlib.Path(__file__).resolve().parents[1] / "casa" / "rootfs" / "opt" / "casa"
TESTS = pathlib.Path(__file__).resolve().parent

# Classifications.
INBOUND, DERIVED, FRESH, RULE_C, PRE_POST = "inbound", "derived", "fresh", "rule-c", "pre-post"
PERSISTED, OUTBOUND, NON_TELEGRAM, NOISE = "persisted", "outbound", "non-telegram", "noise"
RULE_K, SAFE, ENDPOINT = "rule-k", "safe", "endpoint"
ROUTE, RECEIPT, POST_TIME, FIELD, RESOLUTION = "route", "receipt", "post-time", "field", "resolution"

#: The searches, as regular expressions over each source line.
SEARCHES = {
    # where turns and origins are built, and literal lists of origin keys
    "origin-builders": r'BusMessage\(|"_origin_route"|"chat_id":|_ORIGIN_KEYS|creator_peer'
               r'|_origin_from_job|scheduled_delivery_markers\(',
    # empty delivery contexts
    "empty-contexts": r'(ctx|context|delivery_context)(: dict\[str, Any\])? = \{\}',
    # contexts that carry only a correlation id
    "cid-only-contexts": r'\{"cid": new_cid\(\)\}',
    # whole-dict copies of an origin or context (they carry the route)
    "whole-copies": r'\*\*(origin|parent)\b|dict\((origin|ctx|context)\)',
    # broker registrations: the metadata a later tap is checked against
    "broker-registrations": r'BROKER\.register\(',
    # use of a bot outside the transport
    "raw-bot-use": r'api\.telegram\.org|TELEGRAM_BOT_TOKEN',
    # keys that may hold a value two bots share
    "id-strings": r'f"[^"]*\{[^}]*\b(chat|chat_id|operator|operator_id|op|op_id|plugin)\b[^}]*\}',
    "id-fields": r'^\s+(operator_id|chat_id|operator|plugin|seg|subject|role)\s*:\s*',
    "id-tuples": r'\(\s*(self\.)?(plugin|plugin_name|operator|operator_id|chat|chat_id)\s*,',
    "id-collections": r'(_KEY_LOCKS|_FLIGHTS|by_key|_pending|_cache|_seen|_live|_entries|_index'
              r'|_items|_calls|_rows|_refs|_inflight|_futures)\b',
    # where a route is read (the key in either quote style)
    "ROUTE": r'["\']_tg_bot["\']|ROUTING_KEYS|route_for\(|casa_route\(',
    # where a bot id is read
    "BOT_ID": r'\.bot_id\b',
}
#: id-collections keeps only the lines that index, look up or change a collection.
_COLLECTION_USE = re.compile(r'\[|\.get\(|\.setdefault\(|\.pop\(|\.add\(|\.discard\(| in ')


@functools.lru_cache(maxsize=None)
def _parsed(path: pathlib.Path) -> tuple[str, ast.Module]:
    src = path.read_text(encoding="utf-8")
    return src, ast.parse(src)


def _owners(tree: ast.AST) -> list[tuple[int, int, str]]:
    """Every function and class as ``(first line, last line, dotted name)``."""
    out: list[tuple[int, int, str]] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}.{child.name}" if prefix else child.name
                out.append((child.lineno, child.end_lineno, name))
                walk(child, name)
            else:
                walk(child, prefix)

    walk(tree, "")
    return out


def _owner(owners, line: int) -> str:
    best = None
    for first, last, name in owners:
        if first <= line <= last and (best is None or first >= best[0]):
            best = (first, name)
    return best[1] if best else "<module>"


def _search() -> dict[str, collections.Counter]:
    """``{search: Counter((file, function) -> hits)}`` over the code root."""
    found = {name: collections.Counter() for name in SEARCHES}
    patterns = {name: re.compile(rx) for name, rx in SEARCHES.items()}
    for path in sorted(CODE.rglob("*.py")):
        rel = path.relative_to(CODE).as_posix()
        src, tree = _parsed(path)
        owners = _owners(tree)
        for number, line in enumerate(src.splitlines(), 1):
            for name, rx in patterns.items():
                if not rx.search(line):
                    continue
                if name == "id-collections" and not _COLLECTION_USE.search(line):
                    continue
                found[name][(rel, _owner(owners, number))] += 1
    return found


@pytest.fixture(scope="module")
def found() -> dict[str, collections.Counter]:
    return _search()


def _tests_in(path: pathlib.Path) -> set[str]:
    """``test`` and ``Class::test`` names defined in a test module."""
    names: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            names.update(f"{node.name}::{f.name}" for f in node.body
                         if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)))
    return names


def _test_exists(ref: str) -> bool:
    module, _, name = ref.partition("::")
    path = TESTS / module
    return path.is_file() and name in _tests_in(path)


def _diff(recorded: dict, now: collections.Counter, what: str) -> list[str]:
    problems = []
    for key in sorted(set(recorded) | set(now)):
        if key not in recorded:
            problems.append(f"new {what}, not classified: {key} ({now[key]} hit(s))")
        elif key not in now:
            problems.append(f"recorded {what} is gone, drop it from the list: {key}")
        elif recorded[key] != now[key]:
            problems.append(f"{what} {key}: {now[key]} hit(s) now, {recorded[key]} recorded "
                            "— classify the change")
    return problems


# ===========================================================================
# Where origins and delivery contexts are built
# ===========================================================================

_C_SITES = "test_bot_boundary_invariants.py::test_casas_own_cards_and_notices_post_only_on_casas_bot"

#: ``(search, file, function): (hits, classification, case or test or reason)``.
#: A DERIVED entry names its case in the derivation sweep; a FRESH entry the
#: test of its birth.
ORIGIN_SITES = {
    # origin-builders
    ("origin-builders", "agent.py", "<module>"): (1, DERIVED, "turn_reply"),
    ("origin-builders", "agent.py", "Agent._build_options"): (1, NOISE, "reads the ingress kind (_origin_route), not a Telegram route"),
    ("origin-builders", "agent.py", "Agent._limit_line_route"): (1, NOISE, "reads the ingress kind (_origin_route), not a Telegram route"),
    ("origin-builders", "agent.py", "Agent._process"): (2, DERIVED, "turn_reply"),
    ("origin-builders", "agent.py", "Agent._record_turn_explanation"): (1, NOISE, "reads the ingress kind (_origin_route), not a Telegram route"),
    ("origin-builders", "agent.py", "Agent._send_limit_line"): (1, RULE_C, "test_bot_boundary_invariants.py::test_casas_own_cards_and_notices_post_only_on_casas_bot"),
    ("origin-builders", "agent.py", "Agent._synthesize_delegation_turn"): (2, DERIVED, "completion_synthesis"),
    ("origin-builders", "agent.py", "Agent.handle_message"): (1, DERIVED, "turn_reply"),
    ("origin-builders", "agent.py", "_TurnCapture.__init__"): (1, NOISE, "reads the ingress kind (_origin_route), not a Telegram route"),
    ("origin-builders", "agent.py", "_current_origin_clearance"): (1, NOISE, "reads the ingress kind (_origin_route), not a Telegram route"),
    ("origin-builders", "agent.py", "_limit_stop_line"): (1, NOISE, "reads the ingress kind (_origin_route), not a Telegram route"),
    ("origin-builders", "agent_logs.py", "AgentLogs._snapshot"): (1, NON_TELEGRAM, "the agent log group's own state, written through Casa's bot only"),
    ("origin-builders", "authz_grants.py", "ChallengeCoordinator.register_challenge"): (1, PRE_POST, "test_ask_scopes.py::test_a_turn_on_222_is_approved_on_222"),
    ("origin-builders", "bus.py", "MessageBus.run_agent_loop._dispatch"): (3, NOISE, "a bus response echoes its request's channel and context"),
    ("origin-builders", "bus.py", "MessageBus.unregister"): (1, NOISE, "a bus error response; no context"),
    ("origin-builders", "casa_core.py", "_make_webhook_handler.webhook_handler"): (3, FRESH, "test_bot_boundary_invariants.py::test_after_a_normal_reboot_a_turn_born_before_the_bot_connects_carries_its_id"),
    ("origin-builders", "casa_core.py", "_notify_recovered_delegations"): (4, DERIVED, "boot_replay"),
    ("origin-builders", "casa_core.py", "_setup_turn_context"): (1, FRESH, "test_bot_webhook_routes.py::test_a_setup_turn_stays_on_casas_bot_after_its_role_is_bound_elsewhere"),
    ("origin-builders", "casa_core.py", "build_invoke_message"): (2, FRESH, "test_bot_webhook_routes.py::test_an_invoked_turn_is_born_with_its_roles_route"),
    ("origin-builders", "casa_core.py", "main._setup_dispatch"): (1, FRESH, "test_bot_webhook_routes.py::test_a_setup_turn_stays_on_casas_bot_after_its_role_is_bound_elsewhere"),
    ("origin-builders", "casa_core.py", "notify_config_sync"): (1, RULE_C, "test_bot_boundary_invariants.py::test_casas_own_cards_and_notices_post_only_on_casas_bot"),
    ("origin-builders", "channels/telegram.py", "TelegramChannel._dispatch_button_continuation"): (3, INBOUND, "test_endpoint_isolation.py::test_every_turn_an_endpoint_builds_carries_the_bot_it_arrived_on"),
    ("origin-builders", "channels/telegram.py", "TelegramChannel._dispatch_dm_turn"): (3, INBOUND, "test_endpoint_isolation.py::test_every_turn_an_endpoint_builds_carries_the_bot_it_arrived_on"),
    ("origin-builders", "channels/telegram.py", "TelegramChannel._dispatch_summoned_turn"): (3, INBOUND, "test_endpoint_isolation.py::test_every_turn_an_endpoint_builds_carries_the_bot_it_arrived_on"),
    ("origin-builders", "channels/telegram.py", "TelegramChannel._open_summons_reply"): (1, NOISE, "a summons reply's log facts (the group it came from), not an origin"),
    ("origin-builders", "channels/telegram.py", "TelegramChannel._dispatch_guest_turn"): (3, INBOUND, "test_endpoint_isolation.py::test_every_turn_an_endpoint_builds_carries_the_bot_it_arrived_on"),
    ("origin-builders", "channels/telegram.py", "TelegramChannel._dispatch_scheduled_continuation"): (3, INBOUND, "test_endpoint_isolation.py::test_every_turn_an_endpoint_builds_carries_the_bot_it_arrived_on"),
    ("origin-builders", "channels/telegram.py", "TelegramChannel._on_non_text_message.reply"): (1, OUTBOUND, "a Bot API call on this endpoint's own bot"),
    ("origin-builders", "channels/telegram.py", "TelegramChannel._on_proposal_callback"): (1, INBOUND, "test_endpoint_isolation.py::test_a_proposal_naming_bot_111_is_refused_on_222"),
    ("origin-builders", "channels/telegram.py", "TelegramChannel._redirect_from_log_group"): (1, OUTBOUND, "a Bot API call on this endpoint's own bot"),
    ("origin-builders", "channels/telegram.py", "TelegramChannel.speak_first"): (2, FRESH, "test_own_chat_routing.py::test_the_greeting_is_casas_turn_carrying_the_opening"),
    ("origin-builders", "channels/voice/channel.py", "VoiceChannel._run_ws_utterance"): (2, FRESH, "test_bot_boundary_invariants.py::test_after_a_normal_reboot_a_turn_born_before_the_bot_connects_carries_its_id"),
    ("origin-builders", "channels/voice/channel.py", "VoiceChannel._sse_handler"): (2, FRESH, "test_bot_boundary_invariants.py::test_after_a_normal_reboot_a_turn_born_before_the_bot_connects_carries_its_id"),
    ("origin-builders", "drivers/in_casa_driver.py", "_EngagementCapture.__init__"): (1, NOISE, "reads the ingress kind (_origin_route), not a Telegram route"),
    ("origin-builders", "drivers/in_casa_driver.py", "_engagement_memory_note"): (1, NOISE, "reads the ingress kind (_origin_route), not a Telegram route"),
    ("origin-builders", "engagement_registry.py", "<module>"): (1, DERIVED, "engagement_persist"),
    ("origin-builders", "engagement_registry.py", "_persistable_origin"): (2, DERIVED, "engagement_persist"),
    ("origin-builders", "job_registry.py", "JobRegistry._authorize_actor"): (2, NOISE, "compares a job's creating peer kind"),
    ("origin-builders", "job_registry.py", "JobRegistry._decode_job"): (1, DERIVED, "delegation_row"),
    ("origin-builders", "job_registry.py", "JobRegistry._decode_snapshot"): (2, PERSISTED, "test_job_addresses.py::TestDelegationRoute::test_a_telegram_row_without_a_valid_route_is_refused"),
    ("origin-builders", "job_registry.py", "JobRegistry._encode_job"): (1, DERIVED, "delegation_row"),
    ("origin-builders", "job_registry.py", "JobRegistry._has_trusted_voice_delivery_provenance"): (1, NOISE, "compares a job's creating peer kind"),
    ("origin-builders", "job_registry.py", "JobRegistry._owes_announcement"): (1, NOISE, "compares a job's creating peer kind"),
    ("origin-builders", "job_registry.py", "JobRegistry.recover_after_restart"): (1, NOISE, "compares a job's creating peer kind"),
    ("origin-builders", "job_owner_turn.py", "owner_origin"): (2, DERIVED, "owner_turn_origin"),
    ("origin-builders", "job_registry.py", "VoiceJob"): (1, NOISE, "the creating peer field"),
    ("origin-builders", "memory/maintenance.py", "send_operator_line"): (1, RULE_C, "test_bot_boundary_invariants.py::test_the_maintenance_line_before_boot_is_sent_with_casas_own_token"),
    ("origin-builders", "operator_calls.py", "post_question"): (1, PRE_POST, "test_plugin_identities.py::test_before_casas_bot_has_an_id_nothing_is_keyed_stored_or_posted"),
    ("origin-builders", "output_boundary.py", "TurnScope.delivers_to_operator"): (1, NOISE, "reads the ingress kind (_origin_route), not a Telegram route"),
    ("origin-builders", "output_boundary.py", "TurnScope.mint"): (1, NOISE, "reads the ingress kind (_origin_route), not a Telegram route"),
    ("origin-builders", "plugin_bindings.py", "<module>"): (2, DERIVED, "binding"),
    ("origin-builders", "plugin_bindings.py", "_kept_origin"): (1, DERIVED, "binding"),
    ("origin-builders", "plugin_continuations.py", "continuation_origin"): (2, DERIVED, "continuation_origin"),
    ("origin-builders", "plugin_continuations.py", "run_continuation"): (2, DERIVED, "continuation_record"),
    ("origin-builders", "provenance.py", "<module>"): (1, NOISE, "the reserved context keys"),
    ("origin-builders", "provenance.py", "scheduled_delivery_markers"): (1, NOISE, "the helper's definition"),
    ("origin-builders", "reminders.py", "sweep_reminders"): (3, FRESH, "test_fresh_routes.py::test_a_swept_reminder_is_born_with_route_for_its_role"),
    ("origin-builders", "own_voice_lifecycle.py", "TelegramHost.post_ask"): (1, PRE_POST, "test_own_voice_invariants.py::test_casas_ask_card_before_casas_bot_has_an_id_registers_and_posts_nothing"),
    ("origin-builders", "result_broker.py", "_post_proposal"): (1, PRE_POST, "test_plugin_identities.py::test_before_casas_bot_has_an_id_nothing_is_keyed_stored_or_posted"),
    ("origin-builders", "scheduled_asks.py", "broker_meta"): (1, DERIVED, "scheduled_ask_meta"),
    ("origin-builders", "specialist_limits.py", "<module>"): (1, NOISE, "a docstring"),
    ("origin-builders", "stored_call_settler.py", "_pinned_origin"): (2, INBOUND, "test_stored_call_tap.py::test_a_tap_turn_runs_and_replies_on_the_bot_that_received_it"),
    ("origin-builders", "stored_call_settler.py", "handle_tap"): (1, INBOUND, "test_stored_call_tap.py::test_a_tap_turn_runs_and_replies_on_the_bot_that_received_it"),
    ("origin-builders", "configurator_continue.py", "_launch_origin"): (2, RULE_C, "test_configurator_continue.py::test_a_started_continuation_is_one_durable_task_with_the_operators_words"),
    ("whole-copies", "configurator_continue.py", "_continuation"): (2, DERIVED, "delegation_row"),
    ("origin-builders", "specialist_registry.py", "SpecialistRegistry.register_delegation"): (1, DERIVED, "delegation_row"),
    ("origin-builders", "specialist_registry.py", "origin_from_job"): (2, DERIVED, "delegation_row"),
    ("origin-builders", "tools.py", "JobActor"): (1, NOISE, "a job actor's peer kind"),
    ("origin-builders", "tools.py", "_actor_owns_job"): (1, NOISE, "a job actor's peer kind"),
    ("origin-builders", "tools.py", "_ask_user_scheduled"): (2, PRE_POST, "test_tools_route.py::test_a_scheduled_question_before_casas_bot_has_an_id_touches_nothing"),
    ("origin-builders", "tools.py", "_job_actor_from_origin"): (1, NOISE, "a job actor's peer kind"),
    ("origin-builders", "tools.py", "_new_voice_job"): (1, NOISE, "a voice job's peer kind"),
    ("origin-builders", "tools.py", "_origin_clearance_markers"): (2, NOISE, "reads the ingress kind (_origin_route), not a Telegram route"),
    ("origin-builders", "tools.py", "_post_task_text"): (1, OUTBOUND, "a send on the task delivery's own endpoint, the chat of its delivery"),
    ("origin-builders", "tools.py", "_settle_then_announce"): (2, DERIVED, "delegation_envelope"),
    ("origin-builders", "tools.py", "ask_user"): (1, PRE_POST, "test_ask_scopes.py::test_a_pending_question_on_one_bot_does_not_block_the_other"),
    ("origin-builders", "tools.py", "send_engagement_outcome"): (2, DERIVED, "engagement_envelope"),
    ("origin-builders", "tools.py", "start_scheduled_job"): (3, FRESH, "test_tools_route.py::test_a_scheduled_jobs_route_is_born_at_launch_and_kept"),
    ("origin-builders", "tools.py", "wait_for_delegation_slot"): (1, NOISE, "a concurrency cap's scope label"),
    ("origin-builders", "tools.py", "wipe_memory"): (1, PRE_POST, "test_bot_boundary_invariants.py::test_casas_own_cards_and_notices_post_only_on_casas_bot"),
    ("origin-builders", "trigger_registry.py", "TriggerRegistry._register_scheduled._dispatch"): (3, FRESH, "test_fresh_routes.py::test_a_fired_trigger_is_born_with_route_for_its_role"),
    # empty-contexts
    ("empty-contexts", "casa_core.py", "_make_invoke_handler.invoke_handler"): (1, FRESH, "test_bot_webhook_routes.py::test_an_invoked_turn_is_born_with_its_roles_route"),
    ("empty-contexts", "channels/voice/channel.py", "VoiceChannel._resolve_scope_id"): (1, NON_TELEGRAM, "resolves a voice scope; no delivery context"),
    # cid-only-contexts
    ("cid-only-contexts", "casa_core.py", "_notify_plugin_health_locked"): (1, RULE_C, "test_bot_boundary_invariants.py::test_casas_own_cards_and_notices_post_only_on_casas_bot"),
    ("cid-only-contexts", "casa_core.py", "notify_config_sync"): (1, RULE_C, "test_bot_boundary_invariants.py::test_casas_own_cards_and_notices_post_only_on_casas_bot"),
    ("cid-only-contexts", "casa_core.py", "notify_placeholder_rewrites"): (1, RULE_C, "test_bot_boundary_invariants.py::test_casas_own_cards_and_notices_post_only_on_casas_bot"),
    # whole-copies
    ("whole-copies", "engagement_registry.py", "EngagementRegistry.create"): (1, DERIVED, "engagement_persist"),
    ("whole-copies", "tools.py", "_launch_note"): (1, DERIVED, "launch_note"),
    ("whole-copies", "tools.py", "_run_delegated_turn"): (1, DERIVED, "delegated_run"),
    ("whole-copies", "tools.py", "_start_voice_async_job"): (1, DERIVED, "voice_job_run"),
    ("whole-copies", "tools.py", "delegate_to_agent"): (1, DERIVED, "delegated_child"),
    ("whole-copies", "tools.py", "hand_off"): (1, DERIVED, "handoff_child"),
    # broker-registrations
    ("broker-registrations", "operator_calls.py", "post_question"): (1, PRE_POST, "test_plugin_identities.py::test_before_casas_bot_has_an_id_nothing_is_keyed_stored_or_posted"),
    ("broker-registrations", "own_voice_lifecycle.py", "TelegramHost.post_ask"): (1, PRE_POST, "test_own_voice_invariants.py::test_casas_ask_card_before_casas_bot_has_an_id_registers_and_posts_nothing"),
    ("broker-registrations", "result_broker.py", "_post_proposal"): (1, PRE_POST, "test_plugin_identities.py::test_before_casas_bot_has_an_id_nothing_is_keyed_stored_or_posted"),
    ("broker-registrations", "scheduled_asks.py", "reconcile_at_boot"): (1, DERIVED, "scheduled_ask_meta"),
    ("broker-registrations", "tools.py", "_ask_user_scheduled"): (1, PRE_POST, "test_tools_route.py::test_a_scheduled_question_before_casas_bot_has_an_id_touches_nothing"),
    ("broker-registrations", "tools.py", "ask_user"): (1, PRE_POST, "test_ask_scopes.py::test_a_pending_question_on_one_bot_does_not_block_the_other"),
    ("broker-registrations", "tools.py", "wipe_memory"): (1, PRE_POST, "test_bot_boundary_invariants.py::test_casas_own_cards_and_notices_post_only_on_casas_bot"),
    # raw-bot-use
    ("raw-bot-use", "casa_core.py", "<module>"): (1, NOISE, "names the token option among the protected settings"),
    ("raw-bot-use", "casa_core.py", "_resolve_password_options"): (1, NOISE, "a docstring naming the token option"),
    ("raw-bot-use", "casa_core.py", "main"): (1, RULE_C, "Casa's own endpoint is built from the configured token"),
    ("raw-bot-use", "memory/maintenance.py", "send_operator_line"): (2, RULE_C, "test_bot_boundary_invariants.py::test_the_maintenance_line_before_boot_is_sent_with_casas_own_token"),
}


def test_every_origin_and_context_builder_is_classified(found):
    now = collections.Counter()
    for search in ("origin-builders", "empty-contexts", "cid-only-contexts", "whole-copies", "broker-registrations", "raw-bot-use"):
        for (path, func), hits in found[search].items():
            now[(search, path, func)] = hits
    recorded = {key: hits for key, (hits, _cls, _ref) in ORIGIN_SITES.items()}
    assert _diff(recorded, now, "origin site") == []


def test_every_derived_builder_names_its_case_in_the_derivation_sweep():
    import test_bot_boundary_invariants as invariants
    cases = invariants.DERIVED_CASES
    derived = {ref for _h, cls, ref in ORIGIN_SITES.values() if cls == DERIVED}
    assert derived - set(cases) == set()
    assert set(cases) - derived == set(), "a sweep case no recorded builder names"


def test_every_named_test_of_an_origin_site_exists():
    allowed = {INBOUND, DERIVED, FRESH, RULE_C, PRE_POST, PERSISTED, OUTBOUND,
               NON_TELEGRAM, NOISE}
    missing = []
    for key, (_hits, cls, ref) in ORIGIN_SITES.items():
        assert cls in allowed, key
        if cls in (FRESH, INBOUND, PRE_POST, PERSISTED) and "::" not in ref:
            missing.append((key, "names no test"))
        if "::" in ref and not _test_exists(ref):
            missing.append((key, ref))
    assert missing == []


# ===========================================================================
# Keys that could hold a value two bots share
# ===========================================================================

#: Hits of id-strings..4 together, per file and function.
IDENTITY_SITE_HITS = {
    "action_judgement.py": {
        "OperatorWords": 1, "OperatorWords.__init__": 1, "OperatorWords._sweep_locked": 2,
        "OperatorWords.get": 2, "OperatorWords.record": 3, "_login_item_reply": 1, "judge": 1,
        "parse_reply": 1,
    },
    "agent.py": {"build_restricted_webhook_options": 1},
    "agent_home.py": {"provision_agent_home": 1},
    "agent_lane.py": {"Fault": 1},
    "agent_logs.py": {"LogKey": 1},
    "agent_registry.py": {"KnownAgent": 1},
    "approval_card.py": {"<module>": 1, "_cache_get": 2, "_cache_put": 1, "tool_fallback": 1},
    "ask_retirement.py": {"retirement_headline": 1},
    "authz_grants.py": {
        "ChallengeCoordinator.__init__": 1, "ChallengeCoordinator._after_slot": 1,
        "ChallengeCoordinator._make_finish_hook._deliver": 1,
        "ChallengeCoordinator._make_finish_hook._dispatch_continuation": 1,
        "ChallengeCoordinator._make_finish_hook._finish_inner": 1,
        "ChallengeCoordinator._maybe_remove": 2, "ChallengeCoordinator.cancel_matching": 1,
        "ChallengeCoordinator.get_or_create": 1, "ChallengeCoordinator.live_kinds": 1,
        "ChallengeCoordinator.register_challenge": 3, "GrantIdentity": 2, "GrantKey": 3,
        "_live_operator_identity": 2, "make_resident_authz_hook": 1,
        "make_resident_authz_hook._hook._authorize": 1,
    },
    "background_jobs.py": {
        "JobDecl": 1, "JobHost": 2, "_declared_jobs": 2, "_job_busy_refusal": 3,
        "claim_job_start": 2, "release_plugin_job_start": 1,
    },
    "callback_acks.py": {"CallbackAckStore.record": 2, "CallbackAckStore.revoke_effective": 1},
    "callback_consent.py": {
        "CallbackConsentKey": 1, "callback_card_request": 1, "prompt_callback_consent": 2,
        "prompt_callback_consent._finish_factory._finish": 2,
        "render_callback_consent_message": 1,
    },
    "callback_episodes.py": {
        "<module>": 1, "_exhaustion_text": 1, "_is_nudgeable": 1, "_message": 2,
        "_process_unnoted_exhaustions": 2, "_removal_text": 1, "_run_nudge": 2,
        "_select_nudgeable": 1, "_select_unnoted_exhaustions": 1, "kick": 1,
    },
    "callback_http.py": {"_nudge": 1, "make_callback_handler._process": 3},
    "callback_reconcile.py": {
        "RoutedCallbacks": 1, "_default_entries": 1, "_unavailable_rows": 1,
        "compute_desired": 1,
    },
    "callback_spool.py": {
        "CallbackSpool._apply_cap": 2, "CallbackSpool._apply_collect_cap": 2,
        "CallbackSpool._attempt_inventory": 1, "CallbackSpool._attempts_plugin": 5,
        "CallbackSpool._bound_attempts": 1, "CallbackSpool._consume_ack_tokens": 1,
        "CallbackSpool._derive_attempt": 1, "CallbackSpool._infer_receipts": 2,
        "CallbackSpool._materialize_attempts": 3, "CallbackSpool._plugin_fd": 2,
        "CallbackSpool._raise_claimed": 1, "CallbackSpool._recover_claim": 12,
        "CallbackSpool._recover_plugin": 2, "CallbackSpool._recover_temps": 1,
        "CallbackSpool._rederive_attempts": 4, "CallbackSpool._repair_after_lost_deletion": 1,
        "CallbackSpool._rewrite_from_artifacts": 2, "CallbackSpool._sweep_claims": 1,
        "CallbackSpool._sweep_pending": 3, "CallbackSpool._sweep_plugin": 3,
        "CallbackSpool._sweep_results": 4, "CallbackSpool._write_ahead": 2,
        "CallbackSpool._write_removal_record": 1, "CallbackSpool.attempts_pass": 1,
        "CallbackSpool.claim": 1, "CallbackSpool.ensure_plugin_dirs": 3,
        "CallbackSpool.gc_orphan_dirs": 1, "CallbackSpool.list_results": 1,
        "CallbackSpool.recovery_pass": 1, "CallbackSpool.remove_plugin": 2,
        "CallbackSpool.sweep": 1, "CallbackSpool.update_attempt_nudge": 2, "Claim": 1,
        "Claim.key": 1, "_validate_removal": 1, "in_flight_key": 1,
    },
    "callback_owner.py": {"note_link": 1},
    "canonical_bytes.py": {"assert_json_safe": 6, "deep_freeze": 7},
    "casa_core.py": {"main": 2},
    "configurator_continue.py": {"_continuation": 1},
    "channels/telegram.py": {
        "TelegramChannel.__init__": 4, "TelegramChannel._clear_typing_loop": 2,
        "TelegramChannel._dispatch_dm_turn": 3,
        "TelegramChannel._dispatch_engagement_continuation": 2,
        "TelegramChannel._dispatch_proposal_tap": 1,
        "TelegramChannel._edit_proposal_message": 4, "TelegramChannel._ended_job_phrase": 2,
        "TelegramChannel._file_address": 5,
        "TelegramChannel._handle": 2,
        "TelegramChannel._on_non_text_message": 4,
        "TelegramChannel._on_proposal_callback": 1, "TelegramChannel._quoted_address": 1,
        "TelegramChannel._quoted_task": 1, "TelegramChannel._record_post": 1,
        "TelegramChannel._redirect_from_log_group": 2, "TelegramChannel._reply_note_for": 1,
        "TelegramChannel._resolve_file_address": 1, "TelegramChannel._retire_job_post": 1,
        "TelegramChannel._cancel_job_by_key": 1, "TelegramChannel._press_stop_key": 2,
        "TelegramChannel._route_task_file": 1, "TelegramChannel._route_task_file.tell": 1,
        "TelegramChannel._run_job_cancel": 1, "TelegramChannel._send_dormant_line": 1,
        "TelegramChannel.address": 2, "TelegramChannel.deliver_operator_file": 1,
        "TelegramChannel.deliver_operator_link": 3,
        "TelegramChannel.deliver_operator_message": 1,
        "TelegramChannel.deliver_operator_message._landed": 1,
        "TelegramChannel.deliver_operator_page": 1,
        "TelegramChannel.deliver_operator_page._landed": 1,
        "TelegramChannel.deliver_operator_proposal": 1,
        "TelegramChannel.deliver_operator_question": 1, "TelegramChannel.deliver_run_card": 1,
        "TelegramChannel.send_reply_line": 1, "TelegramChannel.mark_proposal": 1,
        "TelegramChannel.speak_first": 1,
        "TelegramChannel.proposal_finish_hook._finish": 5,
        "_summons_chat_name": 2,
    },
    "channels/telegram_transport.py": {"TelegramTransport.add_agent_endpoint": 1},
    "channels/tg_address.py": {"TgAddress": 1, "TgAddress.__post_init__": 1, "scope": 1},
    "chat_notes.py": {"_key": 1},
    "channels/tg_richtext.py": {"_table_shape": 2},
    "channels/voice/routes.py": {"BoundVoiceRoute": 1, "_RouteMetadata": 1},
    "channels/voice/session.py": {"VoiceSession": 1, "VoiceSessionPool.schedule_prewarm": 1},
    "config.py": {"AgentConfig": 1},
    "delivery_keys.py": {
        "<module>": 1, "DeliveryKeys.__init__": 1, "DeliveryKeys._prune": 5,
        "DeliveryKeys.load": 4, "DeliveryKeys.lookup": 2, "DeliveryKeys.record": 2,
    },
    "drivers/in_casa_driver.py": {
        "InCasaDriver._reconcile_live": 1, "InCasaDriver._reconcile_restart_entries": 1,
        "InCasaDriver.recover": 1,
    },
    "engagement_registry.py": {
        "EngagementRegistry.__init__": 3, "EngagementRegistry._settle_job_posts": 3,
        "EngagementRegistry.counts_as_live": 1, "EngagementRegistry.ended_job_post_owner": 2,
        "EngagementRegistry.job_occupants": 1, "EngagementRegistry.job_post_owner": 1,
        "EngagementRegistry.load": 4, "EngagementRegistry.record_job_post": 2,
        "EngagementRegistry.settle_pending": 1, "EngagementRegistry.terminal_pending": 1,
        "EngagementRegistry.try_transition_terminal": 1,
        "EngagementRegistry.try_transition_terminal._mutate_and_persist": 1,
    },
    "event_consent.py": {
        "EventConsentKey": 1, "prompt_event_consent._finish_factory._finish": 1,
    },
    "event_episodes.py": {"_removal_text": 1},
    "event_reconcile.py": {"_default_entries": 1},
    "event_spool.py": {"EventSpool._write_removal_record": 1, "_validate_removal": 1},
    "explanation_store.py": {"ExplanationRecord": 1},
    "guest_groups.py": {"GuestEntry": 1, "GuestStore._snapshot": 1},
    "job_registry.py": {
        "JobRegistry.__init__": 1, "JobRegistry._decode_job": 3,
        "JobRegistry._forget_task_posts_of_deleted": 2, "JobRegistry._task_post_job": 1,
        "JobRegistry.record_task_post": 1,
    },
    "mcp_registry.py": {
        "McpServerRegistry.register_role_sdk": 1, "McpServerRegistry.resolve": 1,
        "McpServerRegistry.role_surfaces": 1,
    },
    "memory/capture.py": {"Event": 1},
    "memory/maintenance.py": {"_reset_open_in_casa_sessions": 2, "write_restore_intent": 1},
    "memory/occurrence.py": {"message_key": 1, "post_key": 1},
    "memory/patches.py": {"apply": 1},
    "memory/search.py": {"_neighbours": 1},
    "memory/view.py": {"Obj": 1},
    "memory/writer.py": {
        "Input": 1, "Lower": 1, "_claim": 1, "_commit": 1, "_lower": 2, "_rows": 1,
        "_withdraw": 2, "assemble_input": 2,
    },
    "operator_calls.py": {
        "OperatorCall": 3, "OperatorCalls.__init__": 1, "OperatorCalls._sweep_locked": 2,
        "OperatorCalls.close": 1, "OperatorCalls.find": 1, "OperatorCalls.open": 2,
        "is_configured_operator": 1, "post_question": 1,
        "post_question._finish_factory._finish": 2,
        "post_question._post": 1,
    },
    "output_boundary.py": {"TurnScope": 1},
    "own_voice_lifecycle.py": {
        "Lifecycle._busy": 1, "Lifecycle.apply_switches": 1, "TelegramHost.open_items": 1,
        "TelegramHost.post_ask._finish_factory._finish": 1},
    "persona_pack.py": {"load_persona_pack": 2},
    "plugin_bindings.py": {
        "<module>": 1, "Binding": 3, "BindingStore.__init__": 1,
        "BindingStore._load_locked": 1, "BindingStore._prune_and_save_locked": 6,
        "BindingStore.adopt": 2, "BindingStore.for_turn": 2, "BindingStore.get": 1,
        "BindingStore.holds_session": 1, "BindingStore.note_cost": 1, "BindingStore.peek": 1,
        "BindingStore.turn_ended": 1,
    },
    "plugin_callbacks.py": {"ack_identity": 1, "effective_name": 1, "parse_and_validate": 5},
    "plugin_continuations.py": {
        "<module>": 1, "Continuations.__init__": 1, "Continuations._run": 1,
        "Continuations._run._forget": 2, "Continuations.drop": 2,
        "Continuations.pending_on_bot": 1, "Continuations.request": 2,
        "_lane_turn": 1,
    },
    "plugin_env_conf.py": {"read_entries": 1, "remove_entries": 1, "remove_entry": 1},
    "plugin_erase_consent.py": {
        "ChoiceGrants.__init__": 1, "ChoiceGrants.consume": 1, "ChoiceGrants.mint": 1,
        "EraseChoiceKey": 3, "prompt_erase_choice._finish_factory._finish": 1,
        "render_erase_choice": 2,
    },
    "plugin_erasure.py": {
        "EraseFence.__init__": 1, "EraseFence.completed": 2, "EraseFence.fenced": 3,
        "EraseFence.hold": 1, "EraseFence.lift": 2, "EraseFence.lift_completed": 3,
        "EraseFence.raise_": 1, "EraseFence.settle": 2, "EraseWatch.__init__": 1,
        "EraseWatch.arm": 1, "EraseWatch.disarm": 1, "EraseWatch.is_armed": 1,
        "EraseWatch.resolve": 1, "EraseWatch.resolve_turn_end": 1,
        "ErasureRecords.__init__": 1, "ErasureRecords.complete_kind": 1,
        "ErasureRecords.has_complete": 1, "ErasureRecords.put": 1,
        "ErasureRecords.take_complete": 2, "_outcome": 1, "parse_unrecorded_vault_items": 1,
    },
    "plugin_events.py": {
        "effective_name": 1, "parse_and_validate_emits": 5, "parse_and_validate_subscribes": 2,
    },
    "plugin_health.py": {"_normalize": 2},
    "plugin_registry.py": {"_resolve_entry": 1, "apply_owned_swap": 5},
    "plugin_requirements.py": {
        "dependents": 1, "entry_named": 1, "unmet_requirement_issues": 2,
    },
    "plugin_setup_episodes.py": {
        "<module>": 3, "_compose": 2, "_normalize": 1, "_rearm_refused_locked": 1,
        "_retire_untracked_dispatches": 1, "_run_episode": 4, "_settle_locked": 2,
        "ensure_obligation": 3, "report_dispatch_outcome": 2, "settle_from_tool_evidence": 1,
    },
    "plugin_stop.py": {"<module>": 1, "settle_ended": 1, "stop": 3, "visible_confirmation": 1},
    "plugin_store.py": {
        "manifest_callbacks": 1, "manifest_emits": 1, "manifest_requires": 2,
        "manifest_subscribes": 1, "manifest_triggers": 1,
    },
    "plugin_triggers.py": {"ack_identity": 1, "effective_name": 1, "parse_and_validate": 5},
    "reload.py": {
        "_CloseLedger.__init__": 2, "_CloseLedger.flush": 1, "_CloseLedger.record": 2,
        "_teardown_disabled_specialist.close_engagements": 1, "dispatch": 1,
    },
    "reminders.py": {
        "<module>": 1, "agent_entries": 1, "clear_placeholder_notice": 1, "remove_entry": 1,
    },
    "resident_trigger_secrets.py": {"resolve_rows": 1},
    "result_broker.py": {
        "DeliveryOutcomes.__init__": 1, "DeliveryOutcomes.record": 2,
        "DeliveryOutcomes.status": 2, "PostEvent": 1, "PostRecord": 3, "ReferenceStore.__init__": 2,
        "ReferenceStore._sweep_locked": 5, "ReferenceStore.arm": 1,
        "ReferenceStore.close_call": 1, "ReferenceStore.deposit": 2,
        "ReferenceStore.drop_call_deposits": 1, "ReferenceStore.is_escrowed": 1,
        "ReferenceStore.open_call": 1, "ReferenceStore.purge_artifact": 4,
        "ReferenceStore.purge_role": 4, "ReferenceStore.redeem": 3,
        "ReferenceStore.take_for_delivery": 1, "_post_proposal": 1, "broker_matchers": 1,
        "make_plugin_admission_hook": 1,
    },
    "role_artifact.py": {"RoleArtifactSource": 1},
    "role_slot.py": {"normalize_role_for_checksum": 1},
    "run_admission.py": {
        "<module>": 1, "Confirmation": 2, "Confirmations.__init__": 1,
        "Confirmations._load_locked": 1, "Confirmations._save_locked": 5,
        "Confirmations.add": 1, "Confirmations.by_key": 2,
        "Confirmations.by_key_on_any_bot": 2, "Confirmations.clear_card": 1,
        "Confirmations.get": 1, "Confirmations.open_cards": 1,
        "Confirmations.peek": 1, "Confirmations.set_card": 1,
        "Confirmations.settle": 1, "_spawn_status_card": 1, "_spawn_status_card._post": 1,
        "confirmation_status": 1,
        "request_admission": 3,
    },
    "scheduled_asks.py": {
        "make_finish_hook._finish": 1, "reconcile_at_boot": 1, "revoke_trigger": 1,
    },
    "scratchpad.py": {"PadStore.from_plugin": 1},
    "sdk_client_pool.py": {
        "SdkClientPool.__init__": 1, "SdkClientPool._drop": 2,
        "SdkClientPool._enforce_caps": 1, "SdkClientPool._enforce_caps._lru": 1,
        "SdkClientPool._entry_stub": 2, "SdkClientPool._evict": 2,
        "SdkClientPool._sweep_once": 1, "SdkClientPool.close_key": 1, "SdkClientPool.turn": 6,
        "interrupt_turns": 1,
    },
    "secrets_resolver.py": {"<module>": 1},
    "session_registry.py": {"SessionRegistry.all_entries": 1},
    "session_sweeper.py": {"SessionSweeper._sweep_once": 3},
    "specialist_bundle_journal.py": {
        "BundleTxn": 1, "_reconcile_journals": 1, "_valid_payload": 1, "begin": 1,
    },
    "specialist_component.py": {"SpecialistComponent": 1, "load_specialist_component": 3},
    "specialist_install.py": {
        "_assert_receipt_matches_inspection": 1, "_kept_new_version_error": 1,
        "_removed_artifact_ids": 2, "_removed_owned_names": 5, "commit_specialist_install": 1,
        "inspect_specialist_repo": 1, "rollback_specialist": 4,
        "sanitize_specialist_snapshots": 4, "uninstall_specialist": 4, "upgrade_specialist": 1,
    },
    "specialist_install_consent.py": {
        "_env_names_set": 1,
        "approval_card_hooks._finish_factory._edit": 1,
        "approval_card_hooks._finish_factory._finish": 1,
    },
    "specialist_materialize.py": {"_reconcile_specialist_operational_files_locked": 1},
    "stored_call_settler.py": {"handle_tap._notice": 1},
    "stored_calls.py": {"resolve_stored_call": 1},
    "task_model.py": {"Requester": 1},
    "tokens.py": {"format_turn_summary": 1},
    "tool_descriptions.py": {"<module>": 1, "_fill": 1, "get": 1},
    "tools.py": {
        "DelegatedOutput": 1, "DelegationCeilingExceeded.__init__": 1,
        "_apply_erasure_to_disclosure": 2, "_bounded_str_list": 1, "_bundle_seq_failure": 1,
        "_clear_env_references": 1, "_configurator_step_line": 2,
        "_configurator_step_line._send": 2, "_delegation_scope": 1,
        "_deliver_erasure_outcome": 2, "_dependency_unavailable_message": 1,
        "_episode_sentence": 1, "_erase_gate": 1, "_erase_gate._run": 1,
        "_erased_everything": 1, "_failure_message": 2, "_guest_chat_of": 3,
        "_plugin_conversation_blocks": 1,
        "_run_delegated_turn": 2,
        "_scheduled_operator_target": 1, "_settle_setup_from_delegated_results": 1,
        "_take_complete_erasures": 1, "_tool_set_plugin_env_reference": 1,
        "_tool_verify_plugin_state": 8, "ask_user._finish_factory._finish": 1,
        "consent_reprompt": 3, "start_scheduled_job.tell": 1, "stop_work": 1,
        "wipe_memory._finish_factory._finish": 1,
    },
    "trigger_consent.py": {
        "ConsentTarget": 1, "TriggerConsentKey": 1, "casa_consent_target": 1,
        "prompt_trigger_consent": 1, "prompt_trigger_consent._finish_factory._finish": 1,
        "render_trigger_consent_message": 1, "trigger_card_request": 1,
    },
    "trigger_reconcile.py": {
        "_unavailable_rows": 1, "compute_desired": 1, "pin_resolver": 2, "seal_setup_state": 6,
    },
    "trigger_registry.py": {
        "TriggerRegistry._validate_agent": 3, "TriggerRegistry.register_agent": 4,
        "TriggerRegistry.reregister_for": 1,
    },
    "vault_drop_off.py": {"item_title": 1, "store": 1},
    "verdict_broker.py": {
        "VerdictBroker.__init__": 1, "VerdictBroker._finish": 1, "VerdictBroker.cancel": 1,
        "VerdictBroker.cancel_scope": 1, "VerdictBroker.cancel_where": 1,
        "VerdictBroker.claim": 1, "VerdictBroker.get_meta": 1,
        "VerdictBroker.is_live_unclaimed": 1, "VerdictBroker.live_on_bot": 1,
        "VerdictBroker.pending": 1,
        "VerdictBroker.register": 4, "VerdictBroker.unregister": 1,
    },
}

_ISO = "test_endpoint_isolation.py::"
_IDS = "test_plugin_identities.py::"

#: The classification of every file with a hit, unless the function has its own.
IDENTITY_FILES = {
    "channels/telegram.py": (ENDPOINT, "state and keys of one endpoint instance; the module "
                                       "holds no state shared between endpoints (pinned below)"),
    "tools.py": (NOISE, "message text, tool-call counters and arguments, not keys"),
    "result_broker.py": (SAFE, "keyed by a Casa-minted reference or tool-use id"),
    "run_admission.py": (SAFE, "confirmations are keyed by their Casa-minted id"),
    "authz_grants.py": (RULE_K, "test_ask_scopes.py::"
                                "test_new_on_one_bot_leaves_the_other_bots_ask_challenge_and_grant"),
    "plugin_bindings.py": (SAFE, "keyed by the binding's Casa-minted ref; the binding keeps its "
                                 "route"),
    "engagement_registry.py": (SAFE, "keyed by the Casa-minted engagement id"),
    "verdict_broker.py": (RULE_K, "test_ask_scopes.py::"
                                  "test_a_pending_question_on_one_bot_does_not_block_the_other"),
    "operator_calls.py": (SAFE, "keyed by (client id, tool-use id), both Casa-minted"),
    "action_judgement.py": (RULE_K, _IDS + "test_words_recorded_on_one_bot_are_not_read_"
                                           "for_the_other"),
    "plugin_continuations.py": (RULE_K, _IDS + "test_an_equal_run_ref_on_two_bots_is_queued_"
                                               "twice_and_dropped_apart"),
    "delivery_keys.py": (RULE_K, _IDS + "test_an_equal_delivery_key_on_two_bots_delivers_on_both"),
    "plugin_stop.py": (RULE_K, _IDS + "test_a_stop_in_flight_on_one_bot_does_not_hold_the_other"),
    "scheduled_asks.py": (NOISE, "outcome fields, comments and arguments, not keys"),
    "trigger_consent.py": (SAFE, "Casa's own consent card: always on Casa's bot, and its key "
                                 "carries that address"),
    "callback_consent.py": (SAFE, "Casa's own consent card: always on Casa's bot, and its key "
                                  "carries that address"),
    "event_consent.py": (SAFE, "Casa's own consent card: always on Casa's bot, and its key "
                               "carries that address"),
    "specialist_install_consent.py": (NOISE, "a tap outcome field and a call's arguments"),
    "plugin_erase_consent.py": (SAFE, "Casa's own erase question: always on Casa's bot, and its "
                                      "key names it"),
    "drivers/in_casa_driver.py": (SAFE, "engagement ids, Casa-minted"),
    "channels/tg_address.py": (NOISE, "the address and scope definitions themselves"),
    "memory/occurrence.py": (RULE_K, "test_fresh_routes.py::"
                                     "test_equal_chat_and_message_on_two_bots_are_two_events"),
    "memory/maintenance.py": (NOISE, "stored flags and intents, not keys"),
    "memory/capture.py": (NOISE, "an event's role field"),
    "memory/writer.py": (NOISE, "the memory store's own internals"),
    "memory/view.py": (NOISE, "the memory store's own internals"),
    "memory/search.py": (NOISE, "the memory store's own internals"),
    "memory/patches.py": (NOISE, "the memory store's own internals"),
    "channels/voice/routes.py": (NON_TELEGRAM, "the voice channel"),
    "channels/voice/session.py": (NON_TELEGRAM, "the voice channel"),
    "channels/tg_richtext.py": (NOISE, "text rendering"),
    "sdk_client_pool.py": (SAFE, "conversation identities, qualified by role; a conversation "
                                 "does not depend on the bot"),
    "session_registry.py": (SAFE, "conversation identities, qualified by role"),
    "session_sweeper.py": (SAFE, "conversation identities, qualified by role"),
    "mcp_registry.py": (SAFE, "conversation identities, qualified by role"),
    "background_jobs.py": (SAFE, "the per-plugin job mutex, install-global by design"),
    "vault_drop_off.py": (SAFE, "shared by roles, not by bots: no Telegram component"),
    "approval_card.py": (SAFE, "the written-card cache is keyed by model, kind and a hash "
                               "of the facts, and holds only reply text: the card is "
                               "rebuilt for each request"),
}
for _file in (
        "callback_spool.py", "callback_episodes.py", "callback_http.py", "callback_acks.py",
        "callback_reconcile.py", "event_spool.py", "event_episodes.py", "event_reconcile.py",
        "trigger_reconcile.py", "trigger_registry.py", "plugin_triggers.py",
        "plugin_callbacks.py", "plugin_events.py", "reminders.py"):
    IDENTITY_FILES[_file] = (SAFE, "install-global plugin routing, consent acks and "
                                   "schedules: no Telegram component")
for _file in (
        "plugin_erasure.py", "plugin_setup_episodes.py", "specialist_install.py",
        "specialist_bundle_journal.py", "specialist_component.py", "specialist_materialize.py",
        "plugin_store.py", "plugin_requirements.py", "plugin_env_conf.py", "plugin_registry.py",
        "plugin_health.py", "reload.py", "persona_pack.py", "role_slot.py", "role_artifact.py",
        "resident_trigger_secrets.py", "secrets_resolver.py", "tokens.py", "config.py",
        "agent_registry.py", "agent_home.py", "canonical_bytes.py", "stored_calls.py",
        "explanation_store.py", "scratchpad.py", "tool_descriptions.py"):
    IDENTITY_FILES[_file] = (SAFE, "install-global plugin, artifact and configuration state: "
                                   "no Telegram component")
for _file in ("agent.py", "agent_logs.py", "casa_core.py", "job_registry.py",
              "output_boundary.py", "ask_retirement.py"):
    IDENTITY_FILES[_file] = (NOISE, "arguments, fields and flags, not keys")

#: Functions classified apart from their file.
IDENTITY_FUNCTIONS = {
    ("tools.py", "_delegation_scope"): (SAFE, "a per-(chat, role) concurrency cap, kept per "
                                               "role by design"),
    ("tools.py", "_erase_gate._run"): (SAFE, "Casa's own erase question: always on Casa's bot"),
    ("result_broker.py", "PostRecord"): (NOISE, "a record's fields"),
    ("result_broker.py", "PostEvent"): (NOISE, "an event's fields"),
    ("result_broker.py", "_post_proposal"): (NOISE, "a call's arguments"),
    ("result_broker.py", "broker_matchers"): (NOISE, "a signature"),
    ("result_broker.py", "make_plugin_admission_hook"): (NOISE, "a signature"),
    ("run_admission.py", "<module>"): (RULE_K, _IDS + "test_an_equal_request_key_on_two_bots_"
                                                     "is_two_requests"),
    ("run_admission.py", "Confirmation"): (RULE_K, _IDS + "test_an_equal_request_key_on_two_"
                                                         "bots_is_two_requests"),
    ("run_admission.py", "Confirmations.by_key"): (RULE_K, _IDS + "test_an_equal_request_key_"
                                                                 "on_two_bots_is_two_requests"),
    ("run_admission.py", "request_admission"): (RULE_K, _IDS + "test_an_equal_request_key_on_"
                                                              "two_bots_is_two_requests"),
    ("run_admission.py", "_spawn_status_card._post"): (NOISE, "a call's arguments"),
    ("run_admission.py", "confirmation_status"): (NOISE, "an answer's field"),
    ("authz_grants.py", "ChallengeCoordinator._after_slot"): (NOISE, "a call's arguments"),
    ("authz_grants.py", "ChallengeCoordinator._make_finish_hook._deliver"): (
        NOISE, "a call's arguments"),
    ("authz_grants.py", "ChallengeCoordinator._make_finish_hook._dispatch_continuation"): (
        NOISE, "a call's arguments"),
    ("authz_grants.py", "ChallengeCoordinator._make_finish_hook._finish_inner"): (
        NOISE, "an outcome field"),
    ("authz_grants.py", "_live_operator_identity"): (NOISE, "a docstring"),
    ("authz_grants.py", "make_resident_authz_hook"): (NOISE, "a signature"),
    ("authz_grants.py", "make_resident_authz_hook._hook._authorize"): (NOISE, "a signature"),
    ("plugin_bindings.py", "Binding"): (NOISE, "a binding's fields"),
    ("plugin_bindings.py", "<module>"): (NOISE, "a docstring"),
    ("engagement_registry.py", "EngagementRegistry.__init__"): (
        RULE_K, "test_job_addresses.py::TestEqualMessageIdsOnTwoBots::"
                "test_live_owners_are_distinct_per_bot"),
    ("engagement_registry.py", "EngagementRegistry._settle_job_posts"): (
        RULE_K, "test_job_addresses.py::TestEqualMessageIdsOnTwoBots::"
                "test_ended_owners_are_distinct_per_bot_and_survive_a_reload"),
    ("engagement_registry.py", "EngagementRegistry.ended_job_post_owner"): (
        RULE_K, "test_job_addresses.py::TestEqualMessageIdsOnTwoBots::"
                "test_ended_owners_are_distinct_per_bot_and_survive_a_reload"),
    ("engagement_registry.py", "EngagementRegistry.job_post_owner"): (
        RULE_K, "test_job_addresses.py::TestEqualMessageIdsOnTwoBots::"
                "test_live_owners_are_distinct_per_bot"),
    ("engagement_registry.py", "EngagementRegistry.load"): (
        RULE_K, "test_job_addresses.py::TestEqualMessageIdsOnTwoBots::"
                "test_the_live_index_is_rebuilt_per_bot_at_load"),
    ("engagement_registry.py", "EngagementRegistry.record_job_post"): (
        RULE_K, "test_job_addresses.py::TestEqualMessageIdsOnTwoBots::"
                "test_live_owners_are_distinct_per_bot"),
    ("configurator_continue.py", "_continuation"): (SAFE, "the limiter's per-(operator, role) "
                                                          "permit scope, as a delegation's"),
    ("stored_call_settler.py", "handle_tap._notice"): (NOISE, "a call's arguments"),
    ("agent_lane.py", "Fault"): (NOISE, "a fault's fields"),
    ("callback_owner.py", "note_link"): (SAFE, "the latest sign-in link of a plugin, "
                                               "install-global by design; the link keeps "
                                               "the delivery it was posted at, bot included"),
    ("channels/telegram_transport.py", "TelegramTransport.add_agent_endpoint"): (
        NOISE, "a signature"),
    ("chat_notes.py", "_key"): (RULE_K, "test_chat_notes.py::"
                                        "test_notes_are_kept_per_bot_and_chat_and_drained_once"),
    ("job_registry.py", "JobRegistry.__init__"): (RULE_K, "test_task_posts.py::TestIndex::test_equal_message_ids_on_two_bots_are_two_tasks"),
    ("job_registry.py", "JobRegistry._forget_task_posts_of_deleted"): (RULE_K, "test_task_posts.py::TestIndex::test_equal_message_ids_on_two_bots_are_two_tasks"),
    ("job_registry.py", "JobRegistry._task_post_job"): (RULE_K, "test_task_posts.py::TestIndex::test_equal_message_ids_on_two_bots_are_two_tasks"),
    ("job_registry.py", "JobRegistry.record_task_post"): (RULE_K, "test_task_posts.py::TestIndex::test_equal_message_ids_on_two_bots_are_two_tasks"),
    ("operator_calls.py", "post_question"): (NOISE, "a signature"),
    ("own_voice_lifecycle.py", "Lifecycle._busy"): (NOISE, "a call of the host's open_items"),
    ("own_voice_lifecycle.py", "Lifecycle.apply_switches"): (
        NOISE, "a call of the host's open_items"),
    ("own_voice_lifecycle.py", "TelegramHost.open_items"): (NOISE, "a signature"),
    ("own_voice_lifecycle.py", "TelegramHost.post_ask._finish_factory._finish"): (
        NOISE, "an outcome field"),
    ("plugin_stop.py", "visible_confirmation"): (NOISE, "a signature"),
    ("run_admission.py", "Confirmations.by_key_on_any_bot"): (
        SAFE, "a lookup across bots by design: its one caller (plugin_stop.visible_"
              "confirmation) takes the asking bot's first, else only work the asking agent "
              "requested; each confirmation keeps its own bot"),
    ("run_admission.py", "_spawn_status_card"): (NOISE, "a signature"),
    ("task_model.py", "Requester"): (NOISE, "a record's fields"),
    ("guest_groups.py", "GuestEntry"): (NOISE, "a record's fields: an allowance for one "
                                               "specialist in one group, not a key"),
    ("guest_groups.py", "GuestStore._snapshot"): (NOISE, "the same record's fields, as "
                                                         "written to the store's file"),
    ("tools.py", "_guest_chat_of"): (NOISE, "the summons group's fields, read off the "
                                            "server-stamped origin into a record"),
    ("channels/telegram.py", "_summons_chat_name"): (NOISE, "a chat's display fields (title, "
                                                            "the person's name), not a key"),
    ("action_judgement.py", "_login_item_reply"): (NOISE, "parsing"),
    ("action_judgement.py", "judge"): (NOISE, "a log line"),
    ("action_judgement.py", "parse_reply"): (NOISE, "parsing"),
    ("plugin_continuations.py", "_lane_turn"): (SAFE, "a per-(chat, role) concurrency cap, "
                                                      "kept per role by design"),
    ("memory/occurrence.py", "post_key"): (SAFE, "keyed by a Casa-minted tool-use id"),
    ("approval_card.py", "tool_fallback"): (NOISE, "a card title, not a key"),
}


def test_every_identity_site_is_classified(found):
    now = collections.Counter()
    for search in ("id-strings", "id-fields", "id-tuples", "id-collections"):
        now.update(found[search])
    recorded = {(path, func): hits for path, funcs in IDENTITY_SITE_HITS.items()
                for func, hits in funcs.items()}
    assert _diff(recorded, now, "identity site") == []
    unclassified = sorted(key for key in recorded if key not in IDENTITY_FUNCTIONS
                          and key[0] not in IDENTITY_FILES)
    assert unclassified == []


def test_every_bot_keyed_identity_names_a_test_that_keeps_two_bots_apart():
    allowed = {RULE_K, SAFE, ENDPOINT, NOISE, NON_TELEGRAM}
    problems = []
    for key, (cls, ref) in [*IDENTITY_FILES.items(), *IDENTITY_FUNCTIONS.items()]:
        if cls not in allowed:
            problems.append((key, cls))
        if cls == RULE_K and not _test_exists(ref):
            problems.append((key, ref))
    assert problems == []
    stale = [key for key in IDENTITY_FUNCTIONS
             if key[1] not in IDENTITY_SITE_HITS.get(key[0], {})]
    assert stale == []


# ===========================================================================
# Routes never become identities
# ===========================================================================

#: Every reader of a route: ``(file, function): (hits, classification, note)``.
ROUTE_READERS = {
    ("agent.py", "<module>"): (2, ROUTE, "the route among the turn markers copied into the turn's origin"),
    ("agent.py", "Agent._process"): (1, RECEIPT, "test_fresh_routes.py::test_chat_notes_are_per_bot"),
    ("callback_episodes.py", "_run_nudge"): (1, ROUTE, "stamped where a callback nudge is born: the bot its sign-in link was delivered on"),
    ("agent.py", "_TurnCapture._opening"): (1, RECEIPT, "test_agent_route.py::test_the_opening_key_carries_the_receiving_bot"),
    ("casa_core.py", "_make_invoke_handler.invoke_handler"): (1, ROUTE, "stamped where an /invoke, webhook or setup turn is born"),
    ("casa_core.py", "_make_webhook_handler.webhook_handler"): (1, ROUTE, "stamped where an /invoke, webhook or setup turn is born"),
    ("casa_core.py", "_setup_turn_context"): (4, ROUTE, "stamped where an /invoke, webhook or setup turn is born; a callback nudge keeps the bot it was born for"),
    ("casa_core.py", "build_invoke_message"): (1, ROUTE, "stamped where an /invoke, webhook or setup turn is born"),
    ("channels/telegram.py", "TelegramChannel._dispatch_button_continuation"): (1, ROUTE, "stamped with the receiving endpoint's bot"),
    ("channels/telegram.py", "TelegramChannel._dispatch_dm_turn"): (1, ROUTE, "stamped with the receiving endpoint's bot"),
    ("channels/telegram.py", "TelegramChannel._dispatch_summoned_turn"): (1, ROUTE, "stamped with the receiving endpoint's bot"),
    ("channels/telegram.py", "TelegramChannel._dispatch_guest_turn"): (1, ROUTE, "stamped with the receiving endpoint's bot"),
    ("channels/telegram.py", "TelegramChannel._dispatch_scheduled_continuation"): (1, ROUTE, "stamped with the receiving endpoint's bot"),
    ("channels/telegram.py", "TelegramChannel.speak_first"): (1, ROUTE, "stamped with the endpoint's own bot, where its agent's greeting is born"),
    ("channels/telegram_transport.py", "TelegramTransport"): (1, ROUTE, "resolution"),
    ("channels/telegram_transport.py", "TelegramTransport.casa_route"): (1, ROUTE, "resolution"),
    ("channels/telegram_transport.py", "TelegramTransport.endpoint_for_route"): (2, ROUTE, "resolution"),
    ("channels/telegram_transport.py", "TelegramTransport.route_for"): (2, ROUTE, "resolution"),
    ("channels/voice/channel.py", "_fresh_route"): (2, ROUTE, "stamped where a voice turn is born"),
    ("engagement_registry.py", "EngagementRegistry.load"): (2, ROUTE, "the strict loader checks the stored route"),
    ("job_registry.py", "<module>"): (1, ROUTE, "the delegation row stores and checks the route"),
    ("own_voice.py", "_delegations_on"): (1, ROUTE, "a stored delegation's route, compared with the bot an agent may leave"),
    ("own_voice.py", "_jobs_on"): (1, ROUTE, "a stored job's route, compared with the bot an agent may leave"),
    ("job_registry.py", "JobRegistry._decode_route"): (1, ROUTE, "the delegation row stores and checks the route"),
    ("job_registry.py", "JobRegistry._decode_snapshot"): (2, ROUTE, "the delegation row stores and checks the route"),
    ("job_registry.py", "VoiceJob"): (2, ROUTE, "the delegation row stores and checks the route"),
    ("plugin_bindings.py", "<module>"): (2, ROUTE, "the binding stores and checks the route"),
    ("plugin_bindings.py", "_route_ok"): (1, ROUTE, "the binding stores and checks the route"),
    ("plugin_bindings.py", "route_of"): (3, ROUTE, "the binding stores and checks the route"),
    ("provenance.py", "<module>"): (2, ROUTE, "the route key and the helper that carries it"),
    ("provenance.py", "carry_route"): (2, ROUTE, "the route key and the helper that carries it"),
    ("reminders.py", "_fresh_route"): (2, ROUTE, "stamped where a reminder turn is born"),
    ("stored_call_settler.py", "_pinned_origin"): (1, ROUTE, "stamped with the bot that delivered the card and received the tap"),
    ("configurator_continue.py", "_launch_origin"): (1, ROUTE, "Casa's own route: the continuation is a turn of the resident's chat on Casa's bot"),
    ("stored_call_settler.py", "handle_tap"): (1, ROUTE, "stamped with the receiving endpoint's bot"),
    ("task_model.py", "<module>"): (1, ROUTE, "a requester's stored route is checked against the route keys"),
    ("task_model.py", "Requester.from_json"): (1, ROUTE, "a requester's stored route is checked against the route keys"),
    ("tools.py", "start_scheduled_job"): (1, ROUTE, "stamped where a scheduled job is born"),
    ("trigger_registry.py", "_fresh_route"): (2, ROUTE, "stamped where a trigger turn is born"),
}

#: Every read of a bot id: ``(file, function): (hits, classification, note)``.
#: A PRE_POST entry names the test of the window before Casa's bot has an id.
#: an endpoint's send that carries the chat's Stop keys reads its own bot before the post
_NO_ID_KEYS = ("test_stop_keys.py::"
               "test_before_the_bot_has_an_id_nothing_that_carries_the_keys_is_posted")

BOT_ID_READS = {
    ("authz_grants.py", "ChallengeCoordinator._drive"): (1, FIELD, "the challenge key's own bot"),
    ("authz_grants.py", "ChallengeCoordinator.get_or_create"): (1, FIELD, "the challenge key's own bot"),
    ("authz_grants.py", "GrantIdentity.__post_init__"): (1, FIELD, "validation"),
    ("authz_grants.py", "GrantKey.__post_init__"): (1, FIELD, "validation"),
    ("authz_grants.py", "GrantStore.purge_chat"): (1, FIELD, "the grant key's own bot"),
    ("authz_grants.py", "_route_bot"): (1, PRE_POST, "test_bot_boundary_invariants.py::test_before_casas_bot_has_an_id_a_protected_call_is_denied_and_nothing_is_keyed"),
    ("authz_grants.py", "make_resident_authz_hook._hook"): (1, FIELD, "the grant identity's own bot"),
    ("background_jobs.py", "JobRunner._delivery_method"): (1, FIELD, "the job's delivery's own bot"),
    ("callback_consent.py", "prompt_callback_consent"): (2, PRE_POST, "test_bot_boundary_invariants.py::test_before_casas_bot_has_an_id_a_card_that_names_it_is_not_posted"),
    ("callback_episodes.py", "_run_nudge"): (1, FIELD, "the sign-in link's delivery's own bot"),
    ("channels/telegram.py", "TelegramChannel._card_agent"): (1, RECEIPT, "the endpoint's own bot, where an update or tap arrived"),
    ("channels/telegram.py", "TelegramChannel._dispatch_button_continuation"): (1, RECEIPT, "the endpoint's own bot, where an update or tap arrived"),
    ("channels/telegram.py", "TelegramChannel._dispatch_dm_turn"): (2, RECEIPT, "the endpoint's own bot, where an update or tap arrived"),
    ("channels/telegram.py", "TelegramChannel._dispatch_summoned_turn"): (1, RECEIPT, "the endpoint's own bot, where the operator's summons arrived"),
    ("channels/telegram.py", "TelegramChannel._dispatch_guest_turn"): (1, RECEIPT, "the endpoint's own bot, where a friend's summons arrived"),
    ("channels/telegram.py", "TelegramChannel._guest_reply_note_for"): (1, RECEIPT, "the endpoint's own bot, whose earlier message in the group the summons replied to"),
    ("channels/telegram.py", "TelegramChannel._opening_marker"): (2, RECEIPT, "the endpoint's own bot, whose chat the operator's turn opens"),
    ("channels/telegram.py", "TelegramChannel._dispatch_proposal_tap"): (3, RECEIPT, "the endpoint's own bot, where an update or tap arrived"),
    ("channels/telegram.py", "TelegramChannel._dispatch_scheduled_continuation"): (1, RECEIPT, "the endpoint's own bot, where an update or tap arrived"),
    ("channels/telegram.py", "TelegramChannel._guest_decision"): (1, RECEIPT, "the endpoint's own bot, where a summons arrived, whose binding decides whether it is answered"),
    ("channels/telegram.py", "TelegramChannel._hand_back"): (1, RECEIPT, "the endpoint's own bot, where an update or tap arrived"),
    ("channels/telegram.py", "TelegramChannel._handle_serialized"): (1, RECEIPT, "the endpoint's own bot, where an update or tap arrived"),
    ("channels/telegram.py", "TelegramChannel._send_turn"): (1, RECEIPT, "the endpoint's own bot, where an update or tap arrived"),
    ("channels/telegram.py", "TelegramChannel.speak_first"): (1, RECEIPT, "the endpoint's own bot, whose chat its agent's greeting opens"),
    ("channels/telegram.py", "TelegramChannel._end_greeting"): (1, RECEIPT, "the endpoint's own bot, whose agent's greeting ended"),
    ("channels/telegram.py", "TelegramChannel.stop"): (1, RECEIPT, "the endpoint's own bot, whose greeting a stop ends"),
    ("channels/telegram.py", "TelegramChannel._on_proposal_callback"): (1, RECEIPT, "the endpoint's own bot, where an update or tap arrived"),
    ("channels/telegram.py", "TelegramChannel._on_resident_callback"): (1, RECEIPT, "the endpoint's own bot, where an update or tap arrived"),
    ("channels/telegram.py", "TelegramChannel._run_stop_allowed"): (1, RECEIPT, "the endpoint's own bot, where an update or tap arrived"),
    ("channels/telegram.py", "TelegramChannel._press_stop_key"): (1, RECEIPT, "the endpoint's own bot, where an update or tap arrived"),
    ("channels/telegram.py", "TelegramChannel._spawn_run_stop._run"): (1, FIELD, "the confirmation's own bot"),
    ("channels/telegram.py", "TelegramChannel._spawn_run_stop._tell"): (1, FIELD, "the confirmation's own bot"),
    ("channels/telegram.py", "TelegramChannel._send_key_line"): (1, PRE_POST, _NO_ID_KEYS),
    ("channels/telegram.py", "TelegramChannel.post_job_start_line"): (1, PRE_POST, _NO_ID_KEYS),
    ("channels/telegram.py", "TelegramChannel.deliver_run_card"): (1, PRE_POST, _NO_ID_KEYS),
    ("channels/telegram.py", "TelegramChannel.send_reply_line"): (1, PRE_POST, _NO_ID_KEYS),
    ("channels/telegram.py", "TelegramChannel._own_address"): (1, RECEIPT, "the endpoint's own bot, where an update or tap arrived"),
    ("channels/telegram.py", "TelegramChannel._own_intake"): (1, RECEIPT, "the endpoint's own bot, where an update or tap arrived"),
    ("channels/telegram.py", "TelegramChannel._rebuild"): (1, NOISE, "a log line"),
    ("channels/telegram.py", "TelegramChannel._reply_at"): (2, FIELD, "the stored address's own bot"),
    ("channels/telegram.py", "TelegramChannel._reply_note_for"): (1, RECEIPT, "the endpoint's own bot, where an update or tap arrived"),
    ("channels/telegram.py", "TelegramChannel._retire_job_post"): (1, NOISE, "a log line"),
    ("channels/telegram.py", "TelegramChannel._stop_tap"): (1, RECEIPT, "the endpoint's own bot, where an update or tap arrived"),
    ("channels/telegram.py", "TelegramChannel.address"): (1, POST_TIME, "the endpoint's one address builder, for a message it sent or received"),
    ("channels/telegram_transport.py", "TelegramTransport.add_endpoint"): (1, RESOLUTION, "registration"),
    ("channels/telegram_transport.py", "TelegramTransport.casa_operator_address"): (1, PRE_POST, "test_telegram_transport.py::test_casa_operator_address_before_any_id_is_not_ready"),
    ("channels/telegram_transport.py", "TelegramTransport.casa_route"): (1, RESOLUTION, "Casa's route when its id is known"),
    ("channels/telegram_transport.py", "TelegramTransport.endpoint"): (1, FIELD, "the address's own bot"),
    ("channels/telegram_transport.py", "TelegramTransport.retire_ended_job_keyboards"): (1, NOISE, "a log line"),
    ("channels/telegram_transport.py", "TelegramTransport.retire_job_keyboards"): (1, FIELD, "the post address's own bot"),
    ("channels/telegram_transport.py", "TelegramTransport.route_for"): (1, RESOLUTION, "a fresh turn's route"),
    ("channels/tg_address.py", "DuplicateEndpoint.__init__"): (1, FIELD, "the exception's or address's own field"),
    ("channels/tg_address.py", "EndpointUnavailable.__init__"): (1, FIELD, "the exception's or address's own field"),
    ("channels/tg_address.py", "TgAddress.__post_init__"): (1, FIELD, "the exception's or address's own field"),
    ("channels/tg_address.py", "TgAddress.to_json"): (1, FIELD, "the exception's or address's own field"),
    ("delegated_memory.py", "capture_job_reply"): (1, RECEIPT, "the endpoint that received the reply"),
    ("drivers/in_casa_driver.py", "_EngagementCapture.operator"): (1, FIELD, "the received message's address"),
    ("engagement_registry.py", "_post_key"): (1, FIELD, "the post address's own bot"),
    ("job_registry.py", "_post_key"): (1, FIELD, "the post address's own bot"),
    ("event_consent.py", "prompt_event_consent"): (2, PRE_POST, "test_bot_boundary_invariants.py::test_before_casas_bot_has_an_id_a_card_that_names_it_is_not_posted"),
    ("operator_calls.py", "post_question"): (1, PRE_POST, "test_bot_boundary_invariants.py::test_before_casas_bot_has_an_id_a_card_that_names_it_is_not_posted"),
    ("own_voice.py", "_delegations_on"): (1, FIELD, "the delegation's delivery's own bot"),
    ("own_voice.py", "_job_cards_on"): (1, FIELD, "the job post address's own bot"),
    ("own_voice.py", "_jobs_on"): (1, FIELD, "the job's delivery's own bot"),
    ("own_voice_lifecycle.py", "TelegramHost.edit_card"): (1, FIELD, "the card address's own bot"),
    ("own_voice_lifecycle.py", "TelegramHost.post_ask"): (1, PRE_POST, "test_own_voice_invariants.py::test_casas_ask_card_before_casas_bot_has_an_id_registers_and_posts_nothing"),
    ("own_voice_lifecycle.py", "TelegramHost.post_card"): (1, POST_TIME, "the card, once posted"),
    ("persona_install_consent.py", "prompt_persona_install_consent"): (1, PRE_POST, "test_bot_boundary_invariants.py::test_before_casas_bot_has_an_id_a_card_that_names_it_is_not_posted"),
    ("plugin_bindings.py", "delivery_target"): (2, FIELD, "the binding's delivery's own bot, resolved on its endpoint"),
    ("plugin_bindings.py", "route_of"): (1, FIELD, "the grant identity's bot, resolved on an endpoint"),
    ("plugin_bindings.py", "route_target"): (1, PRE_POST, "test_plugin_identities.py::test_before_casas_bot_has_an_id_nothing_is_keyed_stored_or_posted"),
    ("plugin_continuations.py", "Continuations.pending_on_bot"): (1, FIELD, "the queued continuation's own bot"),
    ("plugin_continuations.py", "_note_knowledge"): (1, FIELD, "the binding's delivery's own bot"),
    ("plugin_erase_consent.py", "prompt_erase_choice"): (2, FIELD, "the erase key's own bot"),
    ("result_broker.py", "_deliver_and_replace"): (1, PRE_POST, "test_plugin_identities.py::test_before_casas_bot_has_an_id_nothing_is_keyed_stored_or_posted"),
    ("result_broker.py", "_endpoint_for"): (1, FIELD, "the target's own bot"),
    ("plugin_stop.py", "visible_confirmation"): (1, FIELD, "the confirmation's own bot"),
    ("plugin_stop.py", "settle_ended"): (1, FIELD, "the card address's own bot"),
    ("result_broker.py", "label_at"): (1, FIELD, "the address's own bot"),
    ("result_broker.py", "_post_proposal"): (1, PRE_POST, "test_endpoint_isolation.py::test_a_proposal_naming_bot_111_is_refused_on_222"),
    ("run_admission.py", "Confirmations.by_key"): (1, FIELD, "the confirmation's own bot"),
    ("run_admission.py", "Confirmations.open_cards"): (1, FIELD, "the confirmation's own bot"),
    ("run_admission.py", "_decide._on_settle"): (1, FIELD, "the card address's own bot"),
    ("stop_keys.py", "_live_jobs"): (1, FIELD, "the job post address's own bot"),
    ("run_admission.py", "_spawn_status_card._post"): (1, POST_TIME, "the run card, once posted"),
    ("stored_call_settler.py", "handle_tap"): (2, RECEIPT, "the endpoint that received the tap"),
    ("specialist_install_consent.py", "approval_card_hooks._note_card"): (1, POST_TIME, "the bot that posted the card, read once it is posted"),
    ("task_model.py", "delivery_for"): (2, PRE_POST, "test_task_model.py::test_the_resident_born_before_its_bot_has_an_id_delivers_nothing"),
    ("specialist_install_consent.py", "prompt_specialist_install_consent"): (1, PRE_POST, "test_bot_boundary_invariants.py::test_before_casas_bot_has_an_id_a_card_that_names_it_is_not_posted"),
    ("tools.py", "_erase_gate"): (2, FIELD, "Casa's operator address, read before the question"),
    ("tools.py", "_note_task"): (1, FIELD, "the task's delivery's own bot"),
    ("tools.py", "ask_user"): (1, PRE_POST, "test_tools_route.py::test_a_scheduled_question_before_casas_bot_has_an_id_touches_nothing"),
    ("tools.py", "start_scheduled_job.tell"): (1, POST_TIME, "the chat note of a notice, only once Casa's bot has an id"),
    ("tools.py", "send_media"): (1, FIELD, "the task's delivery's own bot, stamped as the send's route"),
    ("tools.py", "stop_work._tell"): (1, FIELD, "the card address's own bot"),
    ("tools.py", "stop_work"): (2, RESOLUTION, "the endpoint the turn's route resolves to, and the found request's own bot"),
    ("tools.py", "wipe_memory"): (1, FIELD, "Casa's operator address, read before the card"),
    ("trigger_consent.py", "prompt_trigger_consent"): (2, PRE_POST, "test_bot_boundary_invariants.py::test_before_casas_bot_has_an_id_a_card_that_names_it_is_not_posted"),
}


def test_every_route_reader_is_classified(found):
    recorded = {key: hits for key, (hits, _c, _n) in ROUTE_READERS.items()}
    assert _diff(recorded, found["ROUTE"], "route reader") == []
    assert {cls for _h, cls, _n in ROUTE_READERS.values()} <= {ROUTE, RECEIPT}
    receipts = [n for _h, cls, n in ROUTE_READERS.values() if cls == RECEIPT]
    assert all(_test_exists(ref) for ref in receipts), receipts


def test_every_bot_id_read_is_classified(found):
    recorded = {key: hits for key, (hits, _c, _n) in BOT_ID_READS.items()}
    assert _diff(recorded, found["BOT_ID"], "bot id read") == []
    assert {cls for _h, cls, _n in BOT_ID_READS.values()} <= {
        RECEIPT, POST_TIME, PRE_POST, FIELD, RESOLUTION, NOISE}
    pre_post = {key: ref for key, (_h, cls, ref) in BOT_ID_READS.items() if cls == PRE_POST}
    assert {key: ref for key, ref in pre_post.items() if not _test_exists(ref)} == {}


#: The functions that build an identity naming a bot.
_IDENTITY_BUILDERS = {"scope", "lane_scope", "tg_scope", "TgAddress", "message_key",
                      "engagement_key", "GrantIdentity", "GrantKey", "EraseChoiceKey",
                      "_echo_owner", "get_or_create"}


def test_no_route_is_passed_to_an_identity_builder():
    """A route may be ``CASA``, which names no bot; an identity names the bot
    of an endpoint. So no identity builder is ever handed a value read from
    the route key, except where the route is the bot an inbound message
    arrived on (a RECEIPT reader, whose key refuses anything but a bot id)."""
    receipts = {key for key, (_h, cls, _n) in ROUTE_READERS.items() if cls == RECEIPT}
    offenders = []
    for path in sorted(CODE.rglob("*.py")):
        _src, tree = _parsed(path)
        owners = _owners(tree)
        rel = path.relative_to(CODE).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name not in _IDENTITY_BUILDERS:
                continue
            for arg in [*node.args, *(k.value for k in node.keywords)]:
                if (any(isinstance(n, ast.Constant) and n.value == "_tg_bot"
                        for n in ast.walk(arg))
                        and (rel, _owner(owners, node.lineno)) not in receipts):
                    offenders.append(f"{rel}:{node.lineno}")
    assert offenders == []


# ===========================================================================
# One accessor, one reader of the operator chat, no shared endpoint state
# ===========================================================================


def test_the_telegram_channel_is_looked_up_by_name_in_one_place():
    sites = []
    for path in sorted(CODE.rglob("*.py")):
        _src, tree = _parsed(path)
        owners = _owners(tree)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "get"
                    and node.args and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == "telegram"):
                sites.append((path.relative_to(CODE).as_posix(), _owner(owners, node.lineno)))
    assert sites == [("channels/telegram_transport.py", "telegram_transport")]


def _class(tree: ast.Module, name: str) -> ast.ClassDef:
    return next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)


def test_the_transport_reads_the_configured_chat_only_for_the_operators_id():
    """The transport never picks a chat from configuration for a delivery:
    the configured chat is read once, to know who the operator is."""
    tree = ast.parse((CODE / "channels" / "telegram_transport.py").read_text(encoding="utf-8"))
    readers = set()
    for method in _class(tree, "TelegramTransport").body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(method):
            if ((isinstance(node, ast.Attribute) and node.attr == "chat_id")
                    or (isinstance(node, ast.Constant) and node.value == "chat_id")):
                readers.add(method.name)
    assert readers == {"operator_user_id"}


#: Reads of an endpoint's configured operator chat (``self.chat_id``) in the
#: endpoint module, by method: each is the operator's chat on that endpoint's
#: own bot, an authorisation check, or the operator's chat used as the
#: fallback target of that endpoint's own delivery.
ENDPOINT_CHAT_READS = {
    "TelegramChannel.__init__": 1,
    "TelegramChannel._handle": 1,
    "TelegramChannel._on_non_text_message": 1,
    "TelegramChannel._rebuild": 1,
    "TelegramChannel._record_post": 1,
    "TelegramChannel._user_id_is_operator": 1,
    "TelegramChannel.create_on_token": 1,
    "TelegramChannel.handle_update": 3,
    "TelegramChannel.notify_operator": 2,
    "TelegramChannel.operator_user_id": 1,
    "TelegramChannel.send": 1,
    "TelegramChannel.send_media": 1,
    "TelegramChannel.send_response": 1,
    "TelegramChannel.turn_finished": 1,
}


def test_an_endpoint_reads_its_configured_chat_only_in_the_recorded_places():
    now = collections.Counter()
    src = (CODE / "channels" / "telegram.py").read_text(encoding="utf-8")
    owners = _owners(ast.parse(src))
    for number, line in enumerate(src.splitlines(), 1):
        if re.search(r"self\.chat_id\b", line):
            now[_owner(owners, number)] += 1
    assert _diff(ENDPOINT_CHAT_READS, now, "configured-chat read") == []


_MUTATORS = {"append", "add", "update", "pop", "popitem", "setdefault", "clear", "discard",
             "remove", "extend", "insert"}


def test_the_endpoint_module_holds_no_state_shared_between_endpoints():
    """Everything an endpoint keeps lives on its instance. A module-level
    container that any function changes would be shared by every bot."""
    tree = ast.parse((CODE / "channels" / "telegram.py").read_text(encoding="utf-8"))
    containers = set()
    for node in tree.body:
        targets, value = [], None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        mutable = isinstance(value, (ast.Dict, ast.List, ast.Set, ast.DictComp,
                                     ast.ListComp, ast.SetComp)) or (
            isinstance(value, ast.Call) and getattr(value.func, "id", getattr(
                value.func, "attr", None)) in {"dict", "list", "set", "defaultdict",
                                               "OrderedDict", "deque", "Counter",
                                               "WeakValueDictionary", "WeakSet"})
        if mutable:
            containers.update(t.id for t in targets if isinstance(t, ast.Name))
    # the one module-level table, read-only
    assert containers == {"_INBOUND_KIND_WORDS"}
    changed = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            changed.append(f"global {node.names} at line {node.lineno}")
        target = None
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.Delete)):
            for t in (node.targets if hasattr(node, "targets") else [node.target]):
                if isinstance(t, ast.Subscript):
                    target = t.value
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr in _MUTATORS):
            target = node.func.value
        if isinstance(target, ast.Name) and target.id in containers:
            changed.append(f"{target.id} changed at line {node.lineno}")
    assert changed == []


# ===========================================================================
# Every persisted Telegram record is listed with its round trip
# ===========================================================================

_PERSISTS = re.compile(r"atomic_io|json\.dump\(|sqlite3")
_TELEGRAM_IDENTITY = re.compile(r'TgAddress|"_tg_bot"|ROUTING_KEYS|\bbot_id\b|\bmessage_id\b')

#: Modules that write durable state and touch a Telegram identity: the test
#: that round-trips the stored bot (and refuses a record without it), or why
#: the module stores none.
PERSISTED_WRITERS = {
    "engagement_registry.py": "test_job_addresses.py::TestEngagementRoundTrip::"
                              "test_posts_and_keyboards_round_trip_with_their_bot",
    "job_registry.py": "test_job_addresses.py::TestDelegationRoute::"
                       "test_the_route_round_trips_through_jobs_json",
    "job_inbox.py": "test_job_addresses.py::TestJournalNotice::"
                    "test_an_addressed_notice_round_trips",
    "scheduled_asks.py": "test_ask_scopes.py::test_a_stored_question_round_trips_its_bot",
    "plugin_bindings.py": "test_plugin_identities.py::test_a_binding_keeps_its_route_including_casa",
    "run_admission.py": "test_plugin_identities.py::"
                        "test_a_confirmation_keeps_its_bot_and_card_and_one_without_a_bot_is_refused",
    "delivery_keys.py": "test_plugin_identities.py::"
                        "test_delivery_keys_keep_their_bot_and_an_entry_without_one_is_refused",
    "configurator_run.py": "test_configurator_run.py::"
                           "test_the_step_line_is_kept_and_deleted_by_its_address",
    "channels/telegram_transport.py": "test_telegram_transport.py::"
                                      "test_casa_bot_id_store_round_trip",
    "own_voice_lifecycle.py": "test_own_voice_lifecycle.py::"
                              "test_an_unbound_bot_is_asked_again_after_a_restart",
    "agent_logs.py": "stores the agent log group's posts, which only Casa's bot makes",
    "casa_core.py": "stores no Telegram record; it reads routes and addresses only",
    "reminders.py": "stores reminders; a reminder's route is decided when it fires",
    "specialist_install_consent.py": "stores consent acks; the card's message ids are "
                                     "never stored",
    "approval_records.py": "test_approval_records.py::test_records_survive_a_reload",
}


def test_every_persisted_telegram_record_is_listed_with_its_round_trip():
    writers = set()
    for path in sorted(CODE.rglob("*.py")):
        src, _tree = _parsed(path)
        if _PERSISTS.search(src) and _TELEGRAM_IDENTITY.search(src):
            writers.add(path.relative_to(CODE).as_posix())
    assert writers == set(PERSISTED_WRITERS)
    tests = [ref for ref in PERSISTED_WRITERS.values() if "::" in ref]
    assert [ref for ref in tests if not _test_exists(ref)] == []


# ===========================================================================
# Every origin that reaches a durable job record carries its route
# ===========================================================================


def _functions(path: str) -> dict[str, ast.AST]:
    tree = ast.parse((CODE / path).read_text(encoding="utf-8"))
    return {n.name: n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _calls(fn: ast.AST, name: str) -> list[ast.Call]:
    return [n for n in ast.walk(fn) if isinstance(n, ast.Call)
            and (getattr(n.func, "id", None) or getattr(n.func, "attr", None)) == name]


def _callers(name: str) -> set[tuple[str, str]]:
    out = set()
    for path in sorted(CODE.rglob("*.py")):
        _src, tree = _parsed(path)
        owners = _owners(tree)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and (getattr(node.func, "id", None) or getattr(
                    node.func, "attr", None)) == name):
                out.add((path.relative_to(CODE).as_posix(), _owner(owners, node.lineno)))
    return out


def _source(node: ast.AST) -> str:
    return ast.unparse(node)


def test_every_origin_that_reaches_a_durable_job_record_carries_its_route():
    """The engagement and delegation loaders refuse a Telegram record with no
    route at the next boot, so every origin they store must carry one. The
    paths to them are closed, and each producer of an origin on them either
    copies the turn's origin whole, or stamps or carries the route."""
    import agent

    # the turn's origin copies the route from the turn's context
    assert "_tg_bot" in agent.COPIED_CONTEXT_MARKERS
    # the sinks
    assert _callers("register_delegation") == {
        ("tools.py", "delegate_to_agent"), ("plugin_continuations.py", "run_continuation"),
        ("configurator_continue.py", "_continuation")}
    engagement_creates = {
        key for key in _callers("create")
        if key[0] == "tools.py" and any(
            _source(c.func).endswith("_engagement_registry.create")
            for c in _calls(_functions("tools.py")[key[1].split(".")[-1]], "create"))}
    assert engagement_creates == {("tools.py", "_launch_interactive_engagement")}
    # the paths to the engagement sink
    assert _callers("_launch_interactive_engagement") == {("tools.py", "_start_job_on_host")}
    assert _callers("_start_job_on_host") == {("tools.py", "start_job"),
                                              ("tools.py", "start_scheduled_job")}

    tools = _functions("tools.py")
    # a job started by a turn, and a delegation: the turn's own origin, whole
    for name in ("start_job", "delegate_to_agent"):
        assert "origin = _snapshot_origin()" in _source(tools[name]), name
    assert "_launch_note(origin" in _source(tools["delegate_to_agent"])
    assert "origin=_launch_note(origin" in _source(tools["_launch_interactive_engagement"])
    # a scheduled job, born with no parent: its route is stamped at launch
    assert "origin['_tg_bot'] = transport.route_for(role)" in _source(
        tools["start_scheduled_job"])
    # a continuation's delegation: a literal that carries the binding's route
    (record,) = _calls(_functions("plugin_continuations.py")["run_continuation"],
                       "DelegationRecord")
    origin = next(k.value for k in record.keywords if k.arg == "origin")
    assert "**carry_route(binding.origin)" in _source(origin)
    # a configurator continuation Casa starts: a turn of the resident's chat on
    # Casa's own bot, its route stamped where the origin is built
    cc = _functions("configurator_continue.py")
    assert "'_tg_bot': transport.casa_route()" in _source(cc["_launch_origin"])
    assert "origin = _launch_origin(" in _source(cc["_continuation"])
    (record,) = _calls(cc["_continuation"], "DelegationRecord")
    origin = next(k.value for k in record.keywords if k.arg == "origin")
    assert _source(origin) == "dict(origin)"
    # the delegation row stores the route of the origin it is registered with
    (row,) = _calls(_functions("specialist_registry.py")["register_delegation"], "VoiceJob")
    assert "route=carry_route(origin)" in _source(row)


#: How each ``BusMessage`` that may become a Telegram turn gets its route,
#: when its context is not a literal that names one.
TURN_CONTEXTS = {
    ("agent.py", "Agent.handle_message"): "the request's own context, whole",
    ("agent.py", "Agent._synthesize_delegation_turn"): "takes the delegating origin's route",
    ("bus.py", "MessageBus.run_agent_loop._dispatch"): "a response echoing its request",
    ("casa_core.py", "build_invoke_message"): "stamped with the route the handler was given",
    ("casa_core.py", "main._setup_dispatch"): "built by _setup_turn_context, stamped Casa's",
    ("casa_core.py", "notify_config_sync"): "a notice for Casa's own endpoint, not a turn",
    ("channels/telegram.py", "TelegramChannel._dispatch_dm_turn"): "stamped with the "
                                                                   "receiving bot",
    ("channels/telegram.py", "TelegramChannel._dispatch_summoned_turn"): "stamped with the "
                                                                         "receiving bot",
    ("channels/telegram.py", "TelegramChannel._dispatch_guest_turn"): "stamped with the "
                                                                      "receiving bot",
    ("tools.py", "send_engagement_outcome"): "carries the engagement origin's route",
}

_ROUTE_IN_CONTEXT = ("'_tg_bot'", "**carry_route(", "**_fresh_route(")


def test_every_turn_that_may_reach_telegram_is_built_with_its_route():
    unrouted = set()
    for path in sorted(CODE.rglob("*.py")):
        _src, tree = _parsed(path)
        owners = _owners(tree)
        for node in _calls(tree, "BusMessage"):
            kw = {k.arg: k.value for k in node.keywords}
            if "channel" not in kw or "context" not in kw:
                continue
            context = _source(kw["context"])
            if any(mark in context for mark in _ROUTE_IN_CONTEXT):
                continue
            unrouted.add((path.relative_to(CODE).as_posix(), _owner(owners, node.lineno)))
    assert unrouted == set(TURN_CONTEXTS)


# ===========================================================================
# Every invariant of the boundary names the tests that pin it
# ===========================================================================

_INV = "test_bot_boundary_invariants.py::"

INVARIANTS = {
    "updates are numbered per bot: equal update ids on two bots are both taken, "
    "a replay on one is not": [
        "test_endpoint_isolation.py::test_equal_update_ids_on_two_bots_are_both_taken_and_a_"
        "replay_is_not",
        "test_bot_webhook_routes.py::test_equal_update_ids_on_two_routes_are_two_updates"],
    "an update enters only its bot's route, and only with that bot's secret": [
        "test_bot_webhook_routes.py::test_another_bots_secret_is_refused_on_this_route",
        "test_bot_webhook_routes.py::test_a_bot_with_no_endpoint_is_404",
        "test_bot_webhook_routes.py::test_no_configured_secret_closes_every_route"],
    "every inbound turn carries the bot it arrived on, and no ingress can set it": [
        "test_endpoint_isolation.py::test_every_turn_an_endpoint_builds_carries_the_bot_it_"
        "arrived_on",
        "test_bot_webhook_routes.py::test_a_caller_cannot_choose_an_invoked_turns_route"],
    "a reply leaves through the bot its turn arrived on": [
        "test_endpoint_isolation.py::test_a_turn_on_222_is_answered_through_222_only"],
    "a reply on one bot never reaches another bot's job or post": [
        "test_endpoint_isolation.py::test_a_reply_on_222_to_message_5_never_reaches_111s_live_job",
        "test_endpoint_isolation.py::test_a_reply_on_222_to_message_5_is_not_named_as_111s_"
        "ended_job",
        "test_endpoint_isolation.py::test_a_post_filed_on_111_is_not_222s_message_with_the_"
        "same_id",
        "test_job_addresses.py::TestEqualMessageIdsOnTwoBots::test_a_reply_on_another_bot_does_"
        "not_reach_the_live_job"],
    "equal messages on two bots are two memory events": [
        "test_fresh_routes.py::test_equal_chat_and_message_on_two_bots_are_two_events",
        "test_fresh_routes.py::test_job_replies_on_two_bots_are_two_events"],
    "albums and file armings are per bot": [
        "test_endpoint_isolation.py::test_equal_album_ids_on_two_bots_make_two_acknowledgements",
        "test_endpoint_isolation.py::test_an_arming_on_111_does_not_take_a_file_sent_to_222"],
    "lanes, grants and /new are per bot": [
        "test_ask_scopes.py::test_new_on_one_bot_leaves_the_other_bots_ask_challenge_and_grant",
        "test_ask_scopes.py::test_a_pending_question_on_one_bot_does_not_block_the_other"],
    "a tap resolves only on the bot that posted it": [
        "test_endpoint_isolation.py::test_a_question_naming_bot_111_is_refused_on_222",
        "test_endpoint_isolation.py::test_a_proposal_naming_bot_111_is_refused_on_222"],
    "one endpoint per agent and one per bot": [
        "test_telegram_transport.py::test_one_endpoint_per_role_and_per_bot",
        _INV + "test_two_endpoints_for_one_bot_are_refused",
        _INV + "test_a_token_that_turns_out_to_be_another_endpoints_bot_stays_stopped"],
    "a stored address never resolves to another bot": [
        _INV + "test_a_record_on_a_retired_bot_fails_typed_and_nothing_reaches_casas_bot",
        _INV + "test_a_record_on_a_bot_not_yet_connected_waits_on_that_bot_never_casas"],
    "a restart keeps every record on the bot it names": [
        _INV + "test_after_a_restart_a_jobs_posts_and_replies_stay_on_the_bot_it_ran_on",
        _INV + "test_a_delegation_replayed_at_boot_is_answered_through_the_bot_it_was_asked_on",
        _INV + "test_a_continuation_after_a_restart_runs_on_the_bot_its_binding_names",
        _INV + "test_a_scheduled_question_restored_at_boot_stays_in_the_lane_of_the_bot_that_"
               "asked",
        _INV + "test_a_stop_after_a_restart_is_taken_and_told_on_the_bot_that_posted_the_card"],
    "every derivation keeps the route it was built from": [
        _INV + "test_every_derivation_keeps_the_route_it_was_built_from"],
    "a route is decided once, where its turn is born": [
        _INV + "test_a_scheduled_job_born_on_casas_bot_keeps_it_after_its_role_gets_a_bot_and_"
               "casa_restarts",
        "test_fresh_routes.py::test_the_route_is_decided_at_birth_not_at_delivery",
        "test_tools_route.py::test_a_turn_born_with_no_parent_delivers_once_on_the_one_bot"],
    "Casa's own cards and notices go through Casa's bot": [
        _INV + "test_casas_own_cards_and_notices_post_only_on_casas_bot",
        _INV + "test_the_maintenance_line_before_boot_is_sent_with_casas_own_token"],
    "a conversation does not depend on the bot": [
        _INV + "test_an_agents_conversation_survives_a_change_of_its_bot_and_is_its_own"],
    "identities a shared plugin supplies are per bot": [
        "test_plugin_identities.py::test_an_equal_request_key_on_two_bots_is_two_requests",
        "test_plugin_identities.py::test_a_stop_on_one_bot_leaves_the_other_bots_request",
        "test_plugin_identities.py::test_an_equal_delivery_key_on_two_bots_delivers_on_both",
        "test_plugin_identities.py::test_an_equal_run_ref_on_two_bots_is_queued_twice_and_"
        "dropped_apart",
        "test_plugin_identities.py::test_words_recorded_on_one_bot_are_not_read_for_the_other"],
    "a delivery with no route is refused, never guessed": [
        _INV + "test_a_context_without_a_route_is_refused_by_every_route_bound_method",
        _INV + "test_a_malformed_route_is_refused_by_every_route_bound_method",
        "test_tools_route.py::test_send_message_goes_through_the_bot_of_the_turns_route",
        "test_agent_route.py::test_an_opted_in_webhook_reply_goes_through_its_route"],
    "a turn born before Casa's bot connects delivers through it once it does": [
        _INV + "test_after_a_normal_reboot_a_turn_born_before_the_bot_connects_carries_its_id",
        _INV + "test_a_turn_born_before_casas_bot_has_an_id_delivers_through_it_once_it_"
               "connects",
        _INV + "test_records_persisted_before_casas_bot_has_an_id_deliver_through_it_after_a_"
               "restart"],
    "before Casa's bot has an id, an identity that must name it is not formed": [
        _INV + "test_before_casas_bot_has_an_id_a_protected_call_is_denied_and_nothing_is_keyed",
        _INV + "test_before_casas_bot_has_an_id_a_card_that_names_it_is_not_posted",
        "test_plugin_identities.py::test_before_casas_bot_has_an_id_nothing_is_keyed_stored_or_"
        "posted"],
}


def test_every_invariant_names_tests_that_exist():
    missing = {claim: [ref for ref in refs if not _test_exists(ref)]
               for claim, refs in INVARIANTS.items()}
    assert {claim: refs for claim, refs in missing.items() if refs} == {}
