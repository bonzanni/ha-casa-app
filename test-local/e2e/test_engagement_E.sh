#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

REPO_ROOT="$(cd "$HERE/../.." && pwd)"

MOCK_PORT=8081
MOCK_PID=""
NAME="casa-eng-$$"
SUPERGROUP_ID=-1001

cleanup_all() {
    docker ps -q --filter "name=casa-eng-.*-$$" | xargs -r docker stop >/dev/null 2>&1 || true
    docker stop "$NAME" >/dev/null 2>&1 || true
    if [ -n "$MOCK_PID" ]; then
        kill "$MOCK_PID" 2>/dev/null || true
    fi
}
trap cleanup_all EXIT

# --- E-0: start mock Telegram server ------------------------------------
log "E-0: start mock Telegram server"
MOCK_PID="$(start_mock_telegram_server "$MOCK_PORT")" \
    || fail "E-0: mock TG never started"
pass "E-0 mock TG up"

build_image

# --- Boot one long-lived container with engagement wiring ---------------
# We run the engagement e2e as a python harness executed via `docker exec`.
# The container doesn't need a live Telegram application — the harness
# constructs a TelegramChannel directly with bot_token="test" and a mock-TG
# base URL so the agent log's `createForumTopic` / `sendMessage` and the
# group's pointer land in the mock's state.
log "E-0b: boot container with engagement wiring"
MSYS_NO_PATHCONV=1 docker run -d --rm --name "$NAME" \
    -p "${HOST_PORT}:8080" \
    -e TELEGRAM_ENGAGEMENT_SUPERGROUP_ID="$SUPERGROUP_ID" \
    -e TELEGRAM_BOT_API_BASE="http://host.docker.internal:${MOCK_PORT}" \
    --add-host=host.docker.internal:host-gateway \
    "$IMAGE" >/dev/null
wait_healthy "$NAME"
pass "E-0b container healthy"

# ---------------------------------------------------------------------------
# Helper: run_harness <label> <py>
# Copies the python source into /tmp in the container and runs it inside
# the casa venv. Fails (assert_contains style) if the harness's last line
# is not "OK".
# ---------------------------------------------------------------------------
# Every harness starts with this prelude. Casa reaches a Telegram bot only
# through the transport registered as the "telegram" channel, so a harness
# registers the endpoint it builds there, as casa_core does at boot.
read -r -d '' HARNESS_PRELUDE <<'PY' || true
import sys
sys.path.insert(0, "/opt/casa")


def register_casa_endpoint(ch, tag):
    """Register *ch* as Casa's endpoint of a fresh Telegram transport (its
    verified bot id persisted under /tmp, keyed by *tag*). Returns the
    ChannelManager that holds the transport as its "telegram" channel."""
    from pathlib import Path
    from channels import ChannelManager
    from channels.telegram_transport import CasaBotIdStore, TelegramTransport
    store_path = Path(f"/tmp/_casa_bot_{tag}.json")
    store_path.unlink(missing_ok=True)
    transport = TelegramTransport(
        casa_bot_ids=CasaBotIdStore(store_path, ch.bot_token))
    transport.add_endpoint(ch, casa=True)
    manager = ChannelManager()
    manager.register(transport)
    return manager
PY

run_harness() {
    local label="$1"; shift
    local py="$1"; shift
    local host_tmp
    host_tmp="$(mktemp)"
    printf '%s\n\n%s\n' "$HARNESS_PRELUDE" "$py" > "$host_tmp"
    MSYS_NO_PATHCONV=1 docker cp "$host_tmp" "$NAME:/tmp/_harness.py" >/dev/null
    rm -f "$host_tmp"
    local out
    if ! out=$(MSYS_NO_PATHCONV=1 docker exec -e MOCK_TG_BASE="http://host.docker.internal:${MOCK_PORT}" \
               -e SUPERGROUP_ID="$SUPERGROUP_ID" \
               "$NAME" /opt/casa/venv/bin/python /tmp/_harness.py 2>&1); then
        printf '%s\n' "$out" >&2
        fail "$label harness exited non-zero"
    fi
    printf '%s\n' "$out" | tail -20 >&2
    printf '%s\n' "$out" | tail -1 | grep -qF "OK" \
        || fail "$label harness did not print OK as final line"
}

# ---------------------------------------------------------------------------
# E-1 — The agent log: with the forum group set up, the channel's rebuild
# turns the log on, and an agent's first line opens that agent's topic and
# lands in it (never in General).
# ---------------------------------------------------------------------------
log "E-1: an agent's log line opens its topic and lands there"
curl -s -X POST "http://localhost:${MOCK_PORT}/_reset" >/dev/null

read -r -d '' E1_PY <<'PY' || true
import asyncio, os, sys, json, urllib.request
sys.path.insert(0, "/opt/casa")
from agent_logs import AgentLogs, LogKey
from channels.telegram import TelegramChannel

def inspect():
    with urllib.request.urlopen(os.environ["MOCK_TG_BASE"] + "/_inspect") as r:
        return json.loads(r.read())

async def main():
    os.environ["TELEGRAM_BOT_API_BASE"] = os.environ["MOCK_TG_BASE"]
    supergroup = int(os.environ["SUPERGROUP_ID"])
    ch = TelegramChannel(
        bot_token="test-token", chat_id="999", default_agent="assistant",
        bus=None, webhook_url="",
        engagement_supergroup_id=supergroup,
    )
    register_casa_endpoint(ch, "e1")
    logs = AgentLogs(display_name=lambda role: "Finance",
                     path="/tmp/_logs_e1.json")
    ch._agent_logs = logs
    # Stand up the PTB Application against the mock TG URL; the rebuild
    # checks the group (getChatMember: Manage topics) and starts the writer.
    await ch._rebuild()
    assert logs.enabled, "the agent log did not turn on"
    logs.log(LogKey("agent", "finance"), "E-1 run", "▶ started")
    state = {}
    for _ in range(200):
        state = inspect()
        if any(state.get("messages_by_thread", {}).values()):
            break
        await asyncio.sleep(0.05)
    topics = state.get("topics", {})
    assert len(topics) == 1, f"expected one log topic, got {topics}"
    (tid,) = topics
    lines = state["messages_by_thread"].get(tid, [])
    assert lines and "▶ started" in lines[0], f"log line not in its topic: {state}"
    assert state["main_feed_messages"] == [], "a log line reached General"
    await logs.stop()
    await ch._teardown_app()
    print("OK")

asyncio.run(main())
PY
run_harness "E-1" "$E1_PY"
pass "E-1 agent log line lands in the agent's topic"

# ---------------------------------------------------------------------------
# E-2 — The group is read-only: a person's message there, in any topic, gets
# one pointer to the direct chat in the thread they wrote in, and reaches no
# job.
# ---------------------------------------------------------------------------
log "E-2: a message in the log group draws one pointer and reaches no job"
curl -s -X POST "http://localhost:${MOCK_PORT}/_reset" >/dev/null

read -r -d '' E2_PY <<'PY' || true
import asyncio, os, sys, json, urllib.request
sys.path.insert(0, "/opt/casa")
from types import SimpleNamespace
from engagement_registry import EngagementRegistry
from channels.telegram import TelegramChannel

async def main():
    os.environ["TELEGRAM_BOT_API_BASE"] = os.environ["MOCK_TG_BASE"]
    supergroup = int(os.environ["SUPERGROUP_ID"])
    reg = EngagementRegistry(tombstone_path="/tmp/_eng_e2.json")
    await reg.load()
    ch = TelegramChannel(
        bot_token="test-token", chat_id="999", default_agent="assistant",
        bus=None, webhook_url="",
        engagement_supergroup_id=supergroup,
    )
    register_casa_endpoint(ch, "e2")
    await ch._rebuild()

    class StubDriver:
        def __init__(self): self.turns = []
        def submit_operator(self, rec, text, *, tg_message=None, notice=None):
            self.turns.append((rec.id, text))
    drv = StubDriver()
    ch._engagement_registry = reg
    ch._engagement_driver = drv
    await reg.create(kind="specialist", role_or_type="finance",
                     driver="in_casa", task="", origin={"channel": "telegram"})

    for text in ("hello from user", "/cancel"):
        update = SimpleNamespace(
            message=SimpleNamespace(
                message_id=90001,
                chat=SimpleNamespace(id=supergroup),
                message_thread_id=1234,
                text=text,
                from_user=SimpleNamespace(id=7777, is_bot=False),
            ),
        )
        await ch.handle_update(update)
    assert drv.turns == [], f"a group message reached a job: {drv.turns}"
    with urllib.request.urlopen(os.environ["MOCK_TG_BASE"] + "/_inspect") as r:
        state = json.loads(r.read())
    assert state["messages_by_thread"].get("1234") == [
        "This group is a read-only log. Talk to me in our direct chat."], state
    await ch._teardown_app()
    print("OK")

asyncio.run(main())
PY
run_harness "E-2" "$E2_PY"
pass "E-2 log group is read-only"

# ---------------------------------------------------------------------------
# E-3 — emit_completion happy path with no group needed: _finalize_engagement
# marks the record completed and posts the NOTIFICATION; no topic is opened.
# ---------------------------------------------------------------------------
log "E-3: emit_completion completes the job and NOTIFIES Ellen"
curl -s -X POST "http://localhost:${MOCK_PORT}/_reset" >/dev/null

read -r -d '' E3_PY <<'PY' || true
import asyncio, os, sys, json, urllib.request
sys.path.insert(0, "/opt/casa")
from engagement_registry import EngagementRegistry
from channels.telegram import TelegramChannel

async def main():
    os.environ["TELEGRAM_BOT_API_BASE"] = os.environ["MOCK_TG_BASE"]
    reg = EngagementRegistry(tombstone_path="/tmp/_eng_e3.json")
    ch = TelegramChannel(
        bot_token="test-token", chat_id="999", default_agent="assistant",
        bus=None, webhook_url="",
    )
    manager = register_casa_endpoint(ch, "e3")
    await ch._rebuild()
    ch._engagement_registry = reg

    class StubBus:
        def __init__(self): self.notifs = []
        async def notify(self, msg): self.notifs.append(msg)
    bus = StubBus()

    import tools
    tools._engagement_registry = reg
    tools._bus = bus
    tools._channel_manager = manager

    rec = await reg.create(kind="specialist", role_or_type="finance",
                           driver="in_casa", task="",
                           origin={"channel": "telegram", "role": "assistant"})

    class StubDriver:
        async def cancel(self, rec): pass
    await tools._finalize_engagement(
        rec, outcome="completed", text="all done",
        next_steps=[],
        driver=StubDriver(),
    )

    with urllib.request.urlopen(os.environ["MOCK_TG_BASE"] + "/_inspect") as r:
        state = json.loads(r.read())
    assert state["topics"] == {}, f"a topic was opened: {state['topics']}"
    assert len(bus.notifs) == 1, f"bus notifs: {len(bus.notifs)}"
    assert bus.notifs[0].target == "assistant"
    assert reg.get(rec.id).status == "completed"
    await ch._teardown_app()
    print("OK")

asyncio.run(main())
PY
run_harness "E-3" "$E3_PY"
pass "E-3 emit_completion completes + NOTIFY Ellen, no topic"

# ---------------------------------------------------------------------------
# E-7 — Session suspend after 24h; the operator's next reply to the job, in
# the direct chat, is handed to the driver.
# ---------------------------------------------------------------------------
log "E-7: 24h idle suspends client; the next reply goes to the driver"
curl -s -X POST "http://localhost:${MOCK_PORT}/_reset" >/dev/null

read -r -d '' E7_PY <<'PY' || true
import asyncio, os, sys
sys.path.insert(0, "/opt/casa")
from engagement_registry import EngagementRegistry
from channels.telegram import TelegramChannel

async def main():
    os.environ["TELEGRAM_BOT_API_BASE"] = os.environ["MOCK_TG_BASE"]
    reg = EngagementRegistry(tombstone_path="/tmp/_eng_e7.json")
    ch = TelegramChannel(
        bot_token="test-token", chat_id="999", default_agent="assistant",
        bus=None, webhook_url="",
    )
    register_casa_endpoint(ch, "e7")
    await ch._rebuild()

    # Stub driver: alive flag flips after cancel.
    class StubDriver:
        def __init__(self):
            self._alive = {}
            self.turns = []
            self.addresses = []
        def is_alive(self, rec): return self._alive.get(rec.id, False)
        def get_session_id(self, rec): return "mock-session-e7"
        async def cancel(self, rec): self._alive.pop(rec.id, None)
        def submit_operator(self, rec, text, *, tg_message=None, notice=None):
            self.turns.append(text)
            self.addresses.append(tg_message)
    drv = StubDriver()
    ch._engagement_registry = reg
    ch._engagement_driver = drv

    rec = await reg.create(kind="specialist", role_or_type="finance",
                           driver="in_casa", task="",
                           origin={"channel": "telegram", "chat_id": "999"})
    drv._alive[rec.id] = True

    # Fast-forward 25h; sweep should suspend.
    future = rec.last_user_turn_ts + 25 * 3600
    await reg.sweep_idle_and_suspend(driver=drv, now_override=future)
    assert reg.get(rec.id).status == "idle", \
        f"status after sweep: {reg.get(rec.id).status}"
    assert not drv.is_alive(rec), "driver still alive after sweep"
    assert reg.get(rec.id).sdk_session_id == "mock-session-e7", \
        "sdk_session_id not persisted"

    # The operator replies to the job's post in the direct chat: the seam
    # hands it to the driver, whose delivery worker resumes the session.
    accepted = await ch.deliver_operator_reply(
        reg.get(rec.id), "are you there?", chat_id=999, message_id=90001,
        user_id=999)
    assert accepted, "the reply was not accepted"
    assert drv.turns == ["are you there?"], f"turns: {drv.turns}"
    # The reply is addressed as the bot that received it saw it.
    (addr,) = drv.addresses
    assert (addr.bot_id, addr.chat_id, addr.message_id) == (
        ch.bot_id, 999, 90001), f"reply address: {addr}"
    await ch._teardown_app()
    print("OK")

asyncio.run(main())
PY
run_harness "E-7" "$E7_PY"
pass "E-7 suspend, then the next reply is handed to the driver"

# ---------------------------------------------------------------------------
# E-8 — Orphan recovery: pre-seed /data/engagements.json with a row that
# still holds an older version's topic_id, restart, verify EngagementRegistry
# loads the record on startup. A Telegram job's origin names the bot it was
# started on (``_tg_bot``, here Casa's own route); a row without one is
# skipped at load, so the seed carries it.
# ---------------------------------------------------------------------------
log "E-8: startup with pre-seeded engagements.json"

SEED_JSON='[{"id":"e8orphan0000000000000000000000","kind":"specialist","role_or_type":"finance","driver":"in_casa","status":"active","topic_id":9999,"started_at":1000.0,"last_user_turn_ts":1000.0,"last_idle_reminder_ts":0,"completed_at":null,"sdk_session_id":"mock-session-e8","origin":{"channel":"telegram","_tg_bot":"casa"},"task":"E-8 orphan"}]'
MSYS_NO_PATHCONV=1 docker exec "$NAME" sh -c \
    "printf '%s' '$SEED_JSON' > /data/engagements.json"

read -r -d '' E8_PY <<'PY' || true
import asyncio, os, sys, json
sys.path.insert(0, "/opt/casa")
from engagement_registry import EngagementRegistry

async def main():
    # Load straight from the container's /data tombstone — same path casa_core uses.
    reg = EngagementRegistry(tombstone_path="/data/engagements.json")
    await reg.load()
    survivors = reg.active_and_idle()
    assert len(survivors) == 1, f"expected 1 survivor, got {len(survivors)}"
    rec = survivors[0]
    assert rec.id == "e8orphan0000000000000000000000"
    assert rec.sdk_session_id == "mock-session-e8"
    # The older row's topic_id is read by nothing and not written back.
    with open("/data/engagements.json") as fh:
        rows = json.load(fh)
    assert "topic_id" not in rows[0], rows[0]
    print("OK")

asyncio.run(main())
PY
run_harness "E-8" "$E8_PY"
# Tombstone cleanup so subsequent runs (or post-test poke-around) stay clean.
MSYS_NO_PATHCONV=1 docker exec "$NAME" sh -c "rm -f /data/engagements.json" || true
pass "E-8 orphan recovery: pre-seeded record survives load()"

# ---------------------------------------------------------------------------
# E-10 — Configurator hook-blocked (resident deletion) path.
#
# Structural harness verifying the casa_config_guard PreToolUse hook denies
# resident-directory deletions AND that a subsequent _finalize_engagement
# with outcome="cancelled" emits the correct NOTIFICATION shape. Mirrors the
# shape of the real path (Ellen asks configurator to delete butler →
# configurator proposes rm -rf → hook denies → configurator emits cancel).
#
# Assertion points:
#   1. hook denies   rm -rf /config/agents/butler
#      → permissionDecision == "deny"
#   2. hook allows   non-resident paths (specialists/ subtree + benign bash)
#      → hook returns None
#   3. butler/ directory is still present after the hook fires
#   4. _finalize_engagement(outcome="cancelled") emits one NOTIFICATION
#      whose content.kind == "cancelled" and status == "error"
# ---------------------------------------------------------------------------
log "E-10: configurator hook-blocked (resident deletion) structural harness"
curl -s -X POST "http://localhost:${MOCK_PORT}/_reset" >/dev/null

# Seed a butler resident directory so the "still exists after deny" check has
# something to look at (mirrors a real installation with an enabled resident).
MSYS_NO_PATHCONV=1 docker exec "$NAME" sh -c \
    "mkdir -p /config/agents/butler && \
     printf 'role: butler\n' > /config/agents/butler/runtime.yaml"

read -r -d '' E10_PY <<'PY' || true
import asyncio, os, sys
sys.path.insert(0, "/opt/casa")
from engagement_registry import EngagementRegistry
from channels.telegram import TelegramChannel
from hooks import make_casa_config_guard_hook

async def main():
    os.environ["TELEGRAM_BOT_API_BASE"] = os.environ["MOCK_TG_BASE"]
    supergroup = int(os.environ["SUPERGROUP_ID"])

    # 1. Build the hook as configurator/hooks.yaml wires it: forbid resident
    #    deletion, and forbid writes into the usual runtime-state prefixes.
    hook = make_casa_config_guard_hook(
        forbid_write_paths=["/data/", "/opt/casa/schema/"],
        forbid_delete_residents=True,
    )

    # 2. rm -rf against a resident directory must be DENIED.
    deny_input = {
        "tool_name": "Bash",
        "tool_input": {
            "command": "rm -rf /config/agents/butler",
        },
    }
    deny_out = await hook(deny_input, "tuid-1", {})
    assert deny_out is not None, "E-10.1 hook returned None for resident rm"
    spec = deny_out.get("hookSpecificOutput", {})
    assert spec.get("permissionDecision") == "deny", \
        f"E-10.1 expected deny, got {spec}"
    assert "resident" in spec.get("permissionDecisionReason", "").lower(), \
        f"E-10.1 deny reason missing 'resident': {spec}"

    # Helper: a hook "didn't deny" if it returned no decision (None or {})
    # or a non-deny hookSpecificOutput. v0.36.1 H-2 changed the no-op return
    # from None to {} so the SDK's dict-conversion can't trip; both shapes
    # mean "no policy decision applied — proceed".
    def _denied(decision):
        if decision in (None, {}):
            return False
        spec = (decision.get("hookSpecificOutput") or {}) if isinstance(decision, dict) else {}
        return spec.get("permissionDecision") == "deny"

    # 3. Hook must NOT deny specialist-subtree deletions (not a resident).
    allow_specialist = {
        "tool_name": "Bash",
        "tool_input": {
            "command": "rm -rf /config/agents/specialists/foo",
        },
    }
    out = await hook(allow_specialist, "tuid-2", {})
    assert not _denied(out), f"E-10.2 specialist path wrongly denied: {out}"

    # 3b. Hook must NOT deny executor-subtree deletions either.
    allow_executor = {
        "tool_name": "Bash",
        "tool_input": {
            "command": "rm -rf /config/agents/executors/foo",
        },
    }
    out = await hook(allow_executor, "tuid-3", {})
    assert not _denied(out), f"E-10.2 executor path wrongly denied: {out}"

    # 3c. A benign Bash command must pass straight through.
    benign = {"tool_name": "Bash", "tool_input": {"command": "ls -la /tmp"}}
    out = await hook(benign, "tuid-4", {})
    assert not _denied(out), f"E-10.2 benign bash wrongly denied: {out}"

    # 4. butler/ directory still exists on disk (hook didn't somehow delete it,
    #    and the deny-decision is structural; the SDK would have aborted the
    #    tool call before the rm ever ran).
    butler_dir = "/config/agents/butler"
    assert os.path.isdir(butler_dir), f"E-10.3 butler dir missing: {butler_dir}"

    # 5. Simulate the tail of the cancelled path: configurator's proposed
    #    destructive tool was denied → it emits a cancel outcome → Ellen is
    #    NOTIFIED with kind="cancelled".
    reg = EngagementRegistry(tombstone_path="/tmp/_eng_e10.json")
    ch = TelegramChannel(
        bot_token="test-token", chat_id="999", default_agent="assistant",
        bus=None, webhook_url="",
        engagement_supergroup_id=supergroup,
    )
    manager = register_casa_endpoint(ch, "e10")
    await ch._rebuild()
    ch._engagement_registry = reg

    class StubBus:
        def __init__(self): self.notifs = []
        async def notify(self, msg): self.notifs.append(msg)
    bus = StubBus()
    import tools as _tools
    _tools._engagement_registry = reg
    _tools._bus = bus
    _tools._channel_manager = manager

    class StubDriver:
        async def cancel(self, rec): pass

    rec = await reg.create(
        kind="executor", role_or_type="configurator", driver="in_casa",
        task="delete butler resident",
        origin={"channel": "telegram", "chat_id": "999", "role": "assistant"},
    )

    await _tools._finalize_engagement(
        rec, outcome="cancelled",
        text="resident deletion denied by casa_config_guard",
        next_steps=[],
        driver=StubDriver(),
    )

    # Assertion point 4: NOTIFICATION emitted with cancelled-shaped content.
    assert reg.get(rec.id).status == "cancelled", \
        f"E-10.4 status: {reg.get(rec.id).status}"
    assert len(bus.notifs) == 1, f"E-10.4 expected 1 notif, got {len(bus.notifs)}"
    content = bus.notifs[0].content
    assert getattr(content, "kind", None) == "cancelled", \
        f"E-10.4 notif kind: {getattr(content, 'kind', None)!r}"
    assert getattr(content, "status", None) == "error", \
        f"E-10.4 notif status: {getattr(content, 'status', None)!r}"
    assert bus.notifs[0].target == "assistant", \
        f"E-10.4 notif target: {bus.notifs[0].target}"

    # butler/ still there after the whole flow.
    assert os.path.isdir(butler_dir), \
        f"E-10.3 butler dir vanished during finalize: {butler_dir}"

    await ch._teardown_app()
    print("OK")

asyncio.run(main())
PY
run_harness "E-10" "$E10_PY"

# Cross-boundary filesystem check: butler dir is untouched.
MSYS_NO_PATHCONV=1 docker exec "$NAME" test -d \
    /config/agents/butler \
    || fail "E-10.3 butler dir missing after harness (should still exist)"

pass "E-10 hook denies resident rm + cancelled NOTIFICATION wired"

stop_container "$NAME"
