"""The real-Chrome passkey pin (slice 5v), local and opt-in: ``test/run-live-passkey.sh``.

Not a pytest module: it needs the internet, a real 1Password guest account, its browsing vault's
service account and a passkey that the vault's item holds for webauthn.io (the site keeps a
credential 24 hours after its registration or last use). It runs in the app container with the
production services up (the enforcing proxy, the launcher, Chrome as the browser user): the
production ``RunController`` as the controller user, and the production ``Broker`` with the real
SDK as the broker user, its options read from this script's stdin and held in memory only.

Two account runs on one fresh site profile: the first finds the extension signed out and signs the
guest in; the second (the browser restarted, the extension locked again) unlocks it. Each run's
start page then meets 1Password's passkey prompt, which the broker answers with the site's linked
item. Prints one ``RESULT`` JSON line (no secret); exits 0 only when every check passed.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SITE = "https://webauthn.io"
SIGNED_IN = "You're logged in!"


async def _serve_broker(config: dict) -> None:
    """The broker user's process: the production broker, real SDK, options from ``config``."""
    import logging
    logging.disable(logging.CRITICAL)
    from casa_browser.broker.journal import Journal
    from casa_browser.broker.items import Vault
    from casa_browser.broker.main import Broker, BrokerServer, DriverClient
    from casa_browser.options import BrokerOptions
    assert os.getuid() == 1102
    root = Path(config["root"])
    options = BrokerOptions(**config["options"])
    broker = Broker(Vault(None, options.vault, options.op_token),
                    DriverClient(Path(config["driver_socket"])), Journal(root / "journal.db"),
                    root / "registry.json", options)
    await broker.start()
    if config.get("debug"):
        # Diagnostics for a failing pin: the broker's own handler errors as type and place
        # (traceback frames name code, never values), on stderr, which the pin scans.
        import traceback
        for name in ("begin_run", "register_item", "login", "unlock_guest", "end_run"):
            original = getattr(broker, name)

            async def traced(*a, _original=original, **k):
                try:
                    return await _original(*a, **k)
                except Exception as error:
                    frames = traceback.extract_tb(error.__traceback__)
                    print("broker error", type(error).__name__,
                          [(f.name, f.lineno) for f in frames][-6:], file=sys.stderr, flush=True)
                    raise
            setattr(broker, name, traced)
    server = BrokerServer(broker, root / "rpc.sock", allowed_uid=1103)
    await server.start()
    os.chmod(root / "rpc.sock", 0o666)
    print("READY", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await server.close()


def _start_broker(config: dict):
    import selectors
    proc = subprocess.Popen(
        ["setpriv", "--reuid=casa-broker", "--regid=casa-broker", "--clear-groups",
         "/opt/venv/bin/python", __file__, "broker"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": "/opt:/opt/casa_browser_tests/tests",
             "HOME": config["root"], "LANG": "C.UTF-8",
             # As the broker service: its SDK reaches 1Password only through the proxy.
             "HTTPS_PROXY": "http://127.0.0.1:3129", "https_proxy": "http://127.0.0.1:3129",
             "SSL_CERT_FILE": "/run/casa-browser/ca/ca.pem"})
    proc.stdin.write(json.dumps(config) + "\n")
    proc.stdin.close()
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ)
        ready = selector.select(60)
    if not ready or proc.stdout.readline().strip() != "READY":
        proc.terminate()
        raise RuntimeError("live broker did not start")
    return proc


def _dirs(tmp: Path) -> dict:
    tmp.chmod(0o755)
    root = tmp / "broker"
    root.mkdir(mode=0o711)
    os.chown(root, 1102, 1102)
    controller = tmp / "controller"
    controller.mkdir(mode=0o711)
    os.chown(controller, 1103, 1103)
    return {"root": str(root), "driver_socket": str(controller / "driver.sock")}


def main() -> int:
    secrets = json.loads(sys.stdin.readline())
    item = secrets["item_id"]
    options = {k: secrets[k] for k in ("op_token", "vault", "guest_sign_in_address",
                                       "guest_email", "guest_password", "guest_secret_key")}
    values = [v for k, v in options.items() if k != "vault" and k != "guest_sign_in_address"]
    sys.path.insert(0, str(Path(__file__).parent))
    from lifecycleprobe import controller_home, run_lifecycle

    tmp = Path(tempfile.mkdtemp(prefix="live-passkey-", dir="/tmp"))
    paths = _dirs(tmp)
    if os.environ.get("LIVE_DEBUG") == "1":
        paths["debug"] = True
    broker = _start_broker({**paths, "options": options,
                            "debug": os.environ.get("LIVE_DEBUG") == "1"})
    site = {"site_id": "wa", "name": "webauthn.io", "origins": [SITE], "login": [SITE],
            "item": item, "start": SITE + "/"}
    steps = [{"site": site},
             {"broker": {"op": "register_item", "site_id": "wa", "item_id": item,
                         "login_origins": [SITE], "operator_ref": "live"}}]
    for name in ("first", "second"):
        steps += [{"run": {"name": name, "request_id": "live-" + name, "sites": [["wa", "account"]]}},
                  {"wait_state": {"name": name, "state": ["running", "failed", "interrupted"],
                                  "timeout": 300}},
                  {"sleep": 4},
                  {"observe": {"name": name}},
                  {"events": {"name": name}},
                  {"tabs": True},
                  {"finish": {"name": name}}]
    home = controller_home(tmp)
    logs: dict = {}
    try:
        out = run_lifecycle(steps, home=home, broker_fixture=paths, timeout=900, logs=logs,
                            start_broker=lambda _fixture: broker)
    finally:
        broker.terminate()
        try:
            broker.wait(10)
        except subprocess.TimeoutExpired:
            broker.kill()
    controller_log = (home / "controller.log").read_text() if (home / "controller.log").exists() else ""
    checks = {}
    for i, name in enumerate(("first", "second")):
        base = 2 + 7 * i
        observed = out[base + 3]
        events = out[base + 4]
        tabs = out[base + 5]
        checks[name] = {
            "running": out[base + 1] == "running",
            "signed_in": isinstance(observed, dict) and SIGNED_IN in observed.get("text", ""),
            "no_login_needed": not [k for k, _ in events if k == "login_needed"],
            # Slice 5w: the guest flow leaves no tab behind and the site's page in front.
            "one_tab_site_in_front": tabs == {"tabs": 1, "front": "visible"},
        }
    checks["guest_completed"] = "guest unlock not completed" not in controller_log
    texts = [controller_log, *logs.values()]
    checks["no_secret_in_logs"] = not any(v and v in t for v in values for t in texts)
    ok = all(all(c.values()) if isinstance(c, dict) else c for c in checks.values())
    result = {"ok": ok, "checks": checks}
    if not ok and checks["no_secret_in_logs"]:
        # What went wrong, without a value: run states, event kinds, the page's opening text,
        # the controller log's last lines (scanned clean above).
        result["diagnostics"] = {
            name: {"state": out[2 + 7 * i + 1], "events": out[2 + 7 * i + 4],
                   "tabs": out[2 + 7 * i + 5],
                   "page": (out[2 + 7 * i + 3] or {}).get("text", "")[:300]
                   if isinstance(out[2 + 7 * i + 3], dict) else out[2 + 7 * i + 3]}
            for i, name in enumerate(("first", "second"))}
        result["controller_log"] = controller_log.splitlines()[-25:]
        result["setup"] = out[:2]
        result["broker_stderr"] = logs.get("broker stderr", "").splitlines()[-10:]
        result["controller_stderr"] = [l for l in logs.get("controller stderr", "").splitlines()
                                       if l.startswith("driver error")][-10:]
    print("RESULT " + json.dumps(result), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    if sys.argv[1:] == ["broker"]:
        asyncio.run(_serve_broker(json.loads(sys.stdin.readline())))
    else:
        sys.exit(main())
