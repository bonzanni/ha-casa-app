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
# base URL so `createForumTopic` / `sendMessage` land in the mock's state.
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
run_harness() {
    local label="$1"; shift
    local py="$1"; shift
    local host_tmp
    host_tmp="$(mktemp)"
    printf '%s\n' "$py" > "$host_tmp"
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
# E-1 — Engagement topic open (a specialist job's): channel.open_engagement_topic creates a
# forum topic in the mock-TG supergroup and the registry records it.
# ---------------------------------------------------------------------------
log "E-1: an engagement opens a topic"
curl -s -X POST "http://localhost:${MOCK_PORT}/_reset" >/dev/null

read -r -d '' E1_PY <<'PY' || true
import asyncio, os, sys, json
sys.path.insert(0, "/opt/casa")
from engagement_registry import EngagementRegistry
from channels.telegram import TelegramChannel

async def main():
    os.environ["TELEGRAM_BOT_API_BASE"] = os.environ["MOCK_TG_BASE"]
    supergroup = int(os.environ["SUPERGROUP_ID"])
    reg = EngagementRegistry(tombstone_path="/tmp/_eng_e1.json")
    await reg.load()
    ch = TelegramChannel(
        bot_token="test-token", chat_id="999", default_agent="assistant",
        bus=None, webhook_url="",
        engagement_supergroup_id=supergroup,
    )
    # Stand up the PTB Application against the mock TG URL.
    await ch._rebuild()
    ch._engagement_registry = reg
    tid = await ch.open_engagement_topic(name="E-1 test topic")
    rec = await reg.create(
        kind="specialist", role_or_type="finance", driver="in_casa",
        task="E-1 task", origin={"channel": "telegram", "chat_id": "999"},
        topic_id=tid,
    )
    assert tid is not None, "createForumTopic returned None"
    assert reg.by_topic_id(tid) is rec, "by_topic_id did not resolve"
    assert os.path.exists("/tmp/_eng_e1.json"), "tombstone not written"
    with open("/tmp/_eng_e1.json") as fh:
        assert json.load(fh), "tombstone empty"
    await ch._teardown_app()
    print("OK")

asyncio.run(main())
PY
run_harness "E-1" "$E1_PY"

# Mock TG saw createForumTopic land (topic count ≥ 1).
TOPICS=$(curl -s "http://localhost:${MOCK_PORT}/_inspect" \
    | python3 -c 'import sys,json;print(len(json.load(sys.stdin).get("topics",{})))')
[ "$TOPICS" -ge 1 ] || fail "E-1: expected ≥1 topic in mock TG, got $TOPICS"
pass "E-1 engagement opens topic (mock TG topics=$TOPICS)"

# ---------------------------------------------------------------------------
# E-2 — User turn routed: a user message inside an engagement topic reaches
# the driver. We inject a real Telegram Update shape through handle_update
# and assert the driver's send_user_turn path was reached.
# ---------------------------------------------------------------------------
log "E-2: user turn in topic routed to driver"
curl -s -X POST "http://localhost:${MOCK_PORT}/_reset" >/dev/null

read -r -d '' E2_PY <<'PY' || true
import asyncio, os, sys
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
    await ch._rebuild()

    # Stub driver: record turns, always alive.
    class StubDriver:
        def __init__(self): self.turns = []
        def is_alive(self, rec): return True
        async def send_user_turn(self, rec, text, *, tg_message_id=None): self.turns.append((rec.id, text))
        async def resume(self, rec, sid): pass
        def get_session_id(self, rec): return None
        async def cancel(self, rec): pass
    drv = StubDriver()
    ch._engagement_registry = reg
    ch._engagement_driver = drv
    ch._driver_send_user_turn = drv.send_user_turn

    tid = await ch.open_engagement_topic(name="E-2")
    rec = await reg.create(kind="specialist", role_or_type="finance",
                            driver="in_casa", task="", origin={"channel":"telegram"},
                            topic_id=tid)

    # Synthetic Telegram update: user message in the topic.
    update = SimpleNamespace(
        message=SimpleNamespace(
            message_id=90001,
            chat=SimpleNamespace(id=supergroup),
            message_thread_id=tid,
            text="hello from user",
            from_user=SimpleNamespace(id=7777),
        ),
    )
    await ch.handle_update(update)
    # M9 (v0.52.0): handle_update spawns the driver turn as a tracked
    # background task; drain it before asserting.
    while ch._turn_tasks:
        await asyncio.gather(*list(ch._turn_tasks), return_exceptions=True)
    assert len(drv.turns) == 1, f"expected 1 driver turn, got {len(drv.turns)}"
    assert drv.turns[0][1] == "hello from user", f"wrong text: {drv.turns[0]}"
    # last_user_turn_ts should advance.
    assert reg.get(rec.id).last_user_turn_ts > 0
    await ch._teardown_app()
    print("OK")

asyncio.run(main())
PY
run_harness "E-2" "$E2_PY"
pass "E-2 user turn routed to driver"

# ---------------------------------------------------------------------------
# E-3 — emit_completion happy path: _finalize_engagement closes the topic
# (icon=✅ on mock TG), marks record completed, and posts NOTIFICATION.
# ---------------------------------------------------------------------------
log "E-3: emit_completion closes topic, ✅ icon, NOTIFICATION posted"
curl -s -X POST "http://localhost:${MOCK_PORT}/_reset" >/dev/null

read -r -d '' E3_PY <<'PY' || true
import asyncio, os, sys, json
sys.path.insert(0, "/opt/casa")
from engagement_registry import EngagementRegistry
from channels.telegram import TelegramChannel

async def main():
    os.environ["TELEGRAM_BOT_API_BASE"] = os.environ["MOCK_TG_BASE"]
    supergroup = int(os.environ["SUPERGROUP_ID"])
    reg = EngagementRegistry(tombstone_path="/tmp/_eng_e3.json")
    ch = TelegramChannel(
        bot_token="test-token", chat_id="999", default_agent="assistant",
        bus=None, webhook_url="",
        engagement_supergroup_id=supergroup,
    )
    await ch._rebuild()
    # v0.37.1 D-1: update_topic_state (called inside _finalize_engagement)
    # reads ch._engagement_registry to look up the record's task/current
    # state emoji. Without this wire it early-returns and the title
    # never gets the ✅ state prefix.
    ch._engagement_registry = reg

    # Minimal bus that captures NOTIFICATIONs.
    class StubBus:
        def __init__(self): self.notifs = []
        async def notify(self, msg): self.notifs.append(msg)
    bus = StubBus()

    # Minimal channel_manager.get(...) that returns our channel.
    class StubChanMgr:
        def get(self, name): return ch
    cm = StubChanMgr()

    # Inject bus + channel_manager + registry into the tools module.
    import tools
    tools._engagement_registry = reg
    tools._bus = bus
    tools._channel_manager = cm

    tid = await ch.open_engagement_topic(name="E-3")
    rec = await reg.create(kind="specialist", role_or_type="finance",
                            driver="in_casa", task="",
                            origin={"channel":"telegram", "role":"assistant"},
                            topic_id=tid)

    # Stub driver (cancel is called inside _finalize_engagement)
    class StubDriver:
        async def cancel(self, rec): pass
    await tools._finalize_engagement(
        rec, outcome="completed", text="all done",
        next_steps=[],
        driver=StubDriver(),
    )

    # Inspect mock TG: topic must be closed with ✅ in the title prefix.
    # v0.37.1 D-1: bubble icon is set once at open (role-based, numeric
    # custom_emoji_id) and never edited. State transition lives in the
    # title — compose_topic_title returns "✅ <short_task>" on completion.
    import urllib.request
    with urllib.request.urlopen(os.environ["MOCK_TG_BASE"] + "/_inspect") as r:
        state = json.loads(r.read())
    topic = state["topics"][str(tid)]
    assert topic["closed"] is True, f"topic not closed: {topic}"
    assert topic["name"].startswith("✅"), f"title missing ✅ state prefix: {topic}"
    # Bus got one NOTIFICATION targeting assistant.
    assert len(bus.notifs) == 1, f"bus notifs: {len(bus.notifs)}"
    assert bus.notifs[0].target == "assistant"
    # Record transitioned to completed.
    assert reg.get(rec.id).status == "completed"
    await ch._teardown_app()
    print("OK")

asyncio.run(main())
PY
run_harness "E-3" "$E3_PY"
pass "E-3 emit_completion closes topic with ✅ + NOTIFY Ellen"

# ---------------------------------------------------------------------------
# E-4 — /cancel: user-driven cancellation closes the topic and marks the
# record cancelled.
# ---------------------------------------------------------------------------
log "E-4: /cancel user-driven"
curl -s -X POST "http://localhost:${MOCK_PORT}/_reset" >/dev/null

read -r -d '' E4_PY <<'PY' || true
import asyncio, os, sys
sys.path.insert(0, "/opt/casa")
from types import SimpleNamespace
from engagement_registry import EngagementRegistry
from channels.telegram import TelegramChannel

async def main():
    os.environ["TELEGRAM_BOT_API_BASE"] = os.environ["MOCK_TG_BASE"]
    supergroup = int(os.environ["SUPERGROUP_ID"])
    reg = EngagementRegistry(tombstone_path="/tmp/_eng_e4.json")
    ch = TelegramChannel(
        bot_token="test-token", chat_id="999", default_agent="assistant",
        bus=None, webhook_url="",
        engagement_supergroup_id=supergroup,
    )
    await ch._rebuild()

    import tools
    class StubBus:
        def __init__(self): self.notifs = []
        async def notify(self, msg): self.notifs.append(msg)
    class StubChanMgr:
        def get(self, name): return ch
    tools._engagement_registry = reg
    tools._bus = StubBus()
    tools._channel_manager = StubChanMgr()

    # Stub driver to satisfy _finalize_engagement path triggered by _finalize_cancel.
    class StubDriver:
        def is_alive(self, rec): return False
        async def cancel(self, rec): pass
    # Bind into agent module so tools._finalize_cancel finds it.
    import agent as agent_mod
    agent_mod.active_engagement_driver = StubDriver()
    agent_mod.active_memory_provider = None

    ch._engagement_registry = reg
    # Inject _finalize_cancel collaborator (prod wiring lives in casa_core.py).
    drv = agent_mod.active_engagement_driver
    async def _finalize_cancel(rec, reason="user"):
        await tools._finalize_engagement(
            rec, outcome="cancelled", text=f"Cancelled by {reason}.",
            next_steps=[],
            driver=drv,
        )
    ch._finalize_cancel = _finalize_cancel

    tid = await ch.open_engagement_topic(name="E-4")
    rec = await reg.create(kind="specialist", role_or_type="finance",
                            driver="in_casa", task="",
                            origin={"channel":"telegram","role":"assistant"},
                            topic_id=tid)

    update = SimpleNamespace(
        message=SimpleNamespace(
            message_id=90001,
            chat=SimpleNamespace(id=supergroup),
            message_thread_id=tid, text="/cancel",
            from_user=SimpleNamespace(id=7777),
        ),
    )
    await ch.handle_update(update)

    # Record must be cancelled; topic closed in mock TG.
    assert reg.get(rec.id).status == "cancelled", \
        f"status: {reg.get(rec.id).status}"
    import urllib.request, json as _json
    with urllib.request.urlopen(os.environ["MOCK_TG_BASE"] + "/_inspect") as r:
        state = _json.loads(r.read())
    assert state["topics"][str(tid)]["closed"] is True
    await ch._teardown_app()
    print("OK")

asyncio.run(main())
PY
run_harness "E-4" "$E4_PY"
pass "E-4 /cancel marks record cancelled + closes topic"

# ---------------------------------------------------------------------------
# E-7 — Session suspend after 24h, resume on next user turn. Asserts the
# driver transitions dead → alive via the resume path.
# ---------------------------------------------------------------------------
log "E-7: 24h idle suspends client; next turn resumes"
curl -s -X POST "http://localhost:${MOCK_PORT}/_reset" >/dev/null

read -r -d '' E7_PY <<'PY' || true
import asyncio, os, sys
sys.path.insert(0, "/opt/casa")
from types import SimpleNamespace
from engagement_registry import EngagementRegistry
from channels.telegram import TelegramChannel

async def main():
    os.environ["TELEGRAM_BOT_API_BASE"] = os.environ["MOCK_TG_BASE"]
    supergroup = int(os.environ["SUPERGROUP_ID"])
    reg = EngagementRegistry(tombstone_path="/tmp/_eng_e7.json")
    ch = TelegramChannel(
        bot_token="test-token", chat_id="999", default_agent="assistant",
        bus=None, webhook_url="",
        engagement_supergroup_id=supergroup,
    )
    await ch._rebuild()

    # Stub driver: alive flag flips after cancel; resume restores it.
    class StubDriver:
        def __init__(self):
            self._alive = {}
            self.turns = []
        def is_alive(self, rec): return self._alive.get(rec.id, False)
        def get_session_id(self, rec): return "mock-session-e7"
        async def cancel(self, rec): self._alive.pop(rec.id, None)
        async def resume(self, rec, session_id):
            assert session_id == "mock-session-e7", f"resume got {session_id}"
            self._alive[rec.id] = True
        async def send_user_turn(self, rec, text, *, tg_message_id=None): self.turns.append(text)
    drv = StubDriver()
    ch._engagement_registry = reg
    ch._engagement_driver = drv
    ch._driver_send_user_turn = drv.send_user_turn

    tid = await ch.open_engagement_topic(name="E-7")
    rec = await reg.create(kind="specialist", role_or_type="finance",
                            driver="in_casa", task="",
                            origin={"channel":"telegram"}, topic_id=tid)
    drv._alive[rec.id] = True

    # Fast-forward 25h; sweep should suspend.
    future = rec.last_user_turn_ts + 25 * 3600
    await reg.sweep_idle_and_suspend(driver=drv, now_override=future)
    assert reg.get(rec.id).status == "idle", \
        f"status after sweep: {reg.get(rec.id).status}"
    assert not drv.is_alive(rec), "driver still alive after sweep"
    assert reg.get(rec.id).sdk_session_id == "mock-session-e7", \
        "sdk_session_id not persisted"

    # User turn arrives — handle_update should resume + forward the turn.
    update = SimpleNamespace(
        message=SimpleNamespace(
            message_id=90001,
            chat=SimpleNamespace(id=supergroup),
            message_thread_id=tid, text="are you there?",
            from_user=SimpleNamespace(id=7777),
        ),
    )
    await ch.handle_update(update)
    # M9 (v0.52.0): turn delivery is a tracked background task — drain first.
    while ch._turn_tasks:
        await asyncio.gather(*list(ch._turn_tasks), return_exceptions=True)
    assert drv.is_alive(rec), "driver not alive after resume"
    assert drv.turns == ["are you there?"], f"turns: {drv.turns}"
    assert reg.get(rec.id).status == "active", \
        f"status after resume: {reg.get(rec.id).status}"
    await ch._teardown_app()
    print("OK")

asyncio.run(main())
PY
run_harness "E-7" "$E7_PY"
pass "E-7 suspend-then-resume on next turn"

# ---------------------------------------------------------------------------
# E-8 — Orphan recovery: pre-seed /data/engagements.json, restart container,
# verify EngagementRegistry loads the record on startup.
# ---------------------------------------------------------------------------
log "E-8: startup with pre-seeded engagements.json"

# Write a valid tombstone containing one active engagement.
SEED_JSON='[{"id":"e8orphan0000000000000000000000","kind":"specialist","role_or_type":"finance","driver":"in_casa","status":"active","topic_id":9999,"started_at":1000.0,"last_user_turn_ts":1000.0,"last_idle_reminder_ts":0,"completed_at":null,"sdk_session_id":"mock-session-e8","origin":{"channel":"telegram"},"task":"E-8 orphan"}]'
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
    assert rec.topic_id == 9999
    assert rec.sdk_session_id == "mock-session-e8"
    assert reg.by_topic_id(9999) is rec, "topic index not built"
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
    await ch._rebuild()
    ch._engagement_registry = reg

    class StubBus:
        def __init__(self): self.notifs = []
        async def notify(self, msg): self.notifs.append(msg)
    class StubChanMgr:
        def get(self, _name): return ch
    bus = StubBus()
    import tools as _tools
    _tools._engagement_registry = reg
    _tools._bus = bus
    _tools._channel_manager = StubChanMgr()

    class StubDriver:
        async def cancel(self, rec): pass

    tid = await ch.open_engagement_topic(
        name="#[configurator] delete butler", role="configurator",
    )
    rec = await reg.create(
        kind="executor", role_or_type="configurator", driver="in_casa",
        task="delete butler resident",
        origin={"channel": "telegram", "chat_id": "999", "role": "assistant"},
        topic_id=tid,
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
