"""The enforcing proxy process (``svc-browser-proxy``, user ``casa-proxy``; Plan 1 Task 6).

``python -m casa_browser.proxy_main`` runs mitmproxy in-process with a curated addon set and
``proxy_addon.EnforcingAddon`` last, three loopback listeners (browser data, broker, the
controller's downloader) and the
owner-checked control socket. It generates its private CA at start in a per-boot directory it
alone can read and exports only the public certificate.

The event loop is a ``PinnedLoop``: mitmproxy dials upstream with ``asyncio.open_connection``,
which resolves through the loop's ``getaddrinfo`` — here answered only from the pins an
allowing decision left (``proxy_addon.Pins``). The addon's own resolution uses the real
resolver (``resolve_unpinned``). There is no option, flag or environment variable that admits
private destinations; tests construct ``Proxy`` with their own resolver.
"""
from __future__ import annotations

import asyncio
import gc
import json
import logging
import os
import shutil
import socket
import stat
import sys
from pathlib import Path

from casa_browser import netfloor
from casa_browser.proxy_addon import EnforcingAddon, Pins
from casa_browser.proxy_client import (
    CONTROL_SOCKET, MAX_LINE, ControlError, decode_lease, decode_policy, encode_event, peer_uid,
)

PRIVATE_DIR = Path("/run/casa-browser/proxy-private")  # 0700 casa-proxy (root bootstrap)
CA_DIR = PRIVATE_DIR / "ca"                             # the CA key: proxy only
HOSTS_FILE = PRIVATE_DIR / "hosts.json"                 # the account host (root bootstrap)
CA_PUBLIC = Path("/run/casa-browser/ca/ca.pem")         # public certificate, world-readable
GC_PERIOD = 2.0  # seconds between full garbage collections (see Proxy._collect_garbage)
BINDING_KEY = Path('/data/proxy/binding.key')


class PinnedLoop(asyncio.SelectorEventLoop):
    """An event loop whose ``getaddrinfo`` resolves only pinned, checked names."""

    pins: Pins | None = None

    async def resolve_unpinned(self, host: str, port: int) -> list[str]:
        infos = await super().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return [info[4][0] for info in infos]

    async def getaddrinfo(self, host, port, *, family=0, type=0, proto=0, flags=0):
        name = host.decode("ascii") if isinstance(host, bytes) else str(host)
        literal = name[1:-1] if name.startswith("[") else name
        if netfloor._parse_ip(literal) is not None:
            if not netfloor.is_public_address(literal):
                raise socket.gaierror(socket.EAI_NONAME, "not admitted")
            return await super().getaddrinfo(literal, port, family=family, type=type,
                                             proto=proto, flags=flags | socket.AI_NUMERICHOST)
        addresses = self.pins.fresh(name, int(port)) if self.pins is not None else None
        if not addresses:
            raise socket.gaierror(socket.EAI_NONAME, "not admitted")
        out = []
        for ip in addresses:
            fam = socket.AF_INET if ip.version == 4 else socket.AF_INET6
            if family not in (0, socket.AF_UNSPEC, fam):
                continue
            sockaddr = (str(ip), int(port)) if fam == socket.AF_INET else (str(ip), int(port), 0, 0)
            out.append((fam, type or socket.SOCK_STREAM, proto or socket.IPPROTO_TCP, "", sockaddr))
        if not out:
            raise socket.gaierror(socket.EAI_NONAME, "not admitted")
        return out


def _out_of_memory() -> None:
    """At the memory limit the proxy exits rather than run on half-working (refusing everything,
    its control socket dead, the memory still held): s6 restarts it holding no policy."""
    try:
        os.write(2, b"casa-browser proxy: out of memory; exiting\n")
    finally:
        os._exit(70)


class _Withhold(logging.Filter):
    """No library record keeps its text: hosts and paths never reach the log. Every exception
    mitmproxy or asyncio catches is logged with it, so this is also where a MemoryError — which
    mitmproxy would otherwise swallow per connection — ends the process."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info and isinstance(record.exc_info[1], MemoryError):
            _out_of_memory()
        if not record.name.startswith("casa_browser"):
            record.msg = f"{record.name}: {record.levelname.lower()} event (details withheld)"
            record.args = ()
            record.exc_info = record.exc_text = record.stack_info = None
        return True


def quiet_logging(level=logging.WARNING) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(_Withhold())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def load_extra_hosts(path: Path = HOSTS_FILE) -> frozenset[str]:
    """The 1Password account host root bootstrap validated from the options (may be absent)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    host = data.get("onepassword_account_host") if isinstance(data, dict) else None
    return frozenset({host}) if isinstance(host, str) and netfloor.valid_hostname(host) == host else frozenset()


class Proxy:
    """The mitmproxy master, the addon and the control server, on one event loop."""

    def __init__(self, *, addon: EnforcingAddon, confdir: Path, control_path: Path,
                 controller_uid: int, browser_port: int = netfloor.PROXY_BROWSER_PORT,
                 broker_port: int = netfloor.PROXY_BROKER_PORT,
                 download_port: int = netfloor.PROXY_DOWNLOAD_PORT,
                 listen_host: str = "127.0.0.1"):
        self.addon = addon
        self.confdir = confdir
        self.control_path = control_path
        self.controller_uid = controller_uid
        self.browser_port = browser_port
        self.broker_port = broker_port
        self.download_port = download_port
        self.listen_host = listen_host
        self.master = None
        self._control = None
        self._collector = None

    def build(self):
        from mitmproxy import master, options
        from mitmproxy.addons import block, core, disable_h2c, next_layer, proxyserver, tlsconfig

        opts = options.Options()
        m = master.Master(opts, event_loop=asyncio.get_running_loop(), with_termlog=False)
        m.addons.add(core.Core(), block.Block(), disable_h2c.DisableH2C(),
                     proxyserver.Proxyserver(), next_layer.NextLayer(), tlsconfig.TlsConfig(),
                     self.addon)
        opts.update(
            mode=[f"regular@{self.listen_host}:{self.browser_port}",
                  f"regular@{self.listen_host}:{self.broker_port}",
                  f"regular@{self.listen_host}:{self.download_port}"],
            confdir=str(self.confdir),
            connection_strategy="lazy",
            upstream_cert=False,
            rawtcp=False,
            # No body_size_limit: mitmproxy enforces it on responses before any hook can mark
            # them for streaming, which would cut every download over the limit. The addon
            # bounds requests itself and streams large responses.
            ssl_insecure=False,
        )
        self.addon.attach(m.addons.get("proxyserver"))
        self.master = m
        return m

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            sock = writer.get_extra_info("socket")
            if sock is None or peer_uid(sock) != self.controller_uid:
                return
            line = await reader.readline()
            reply = self.dispatch(line)
            writer.write(json.dumps(reply).encode() + b"\n")
            await writer.drain()
        except (OSError, ValueError, asyncio.LimitOverrunError):
            pass
        finally:
            writer.close()

    def dispatch(self, line: bytes) -> dict:
        """One control request. Runs on the proxy's event loop, between two hook calls."""
        a = self.addon
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ControlError("unsupported")
            op = request.get("op")
            if op == "install":
                # ``start``: the proxy start the controller expects. A restarted proxy holds no
                # policy and refuses the install, so a run is never reinstalled after a restart.
                if set(request) - {"op", "policy", "start"}:
                    raise ControlError("unsupported")
                start = request.get("start")
                if start is not None and start != a.start_nonce:
                    raise ControlError("restarted")
                return {"ok": True, "revision": a.install(decode_policy(request["policy"]))}
            if op == "revoke":
                a.revoke(str(request["run_id"]))
                return {"ok": True}
            if op in ("begin_manual", "begin_login"):
                expected = {'op', 'lease', 'purpose'} if op == 'begin_manual' else {'op', 'lease'}
                if set(request) != expected:
                    raise ControlError('unsupported')
                lease = decode_lease(request["lease"])
                if lease is None:
                    raise ControlError('unsupported')
                token = (a.begin_manual(lease, purpose=request['purpose']) if op == 'begin_manual'
                         else a.begin_login(lease))
                return {"ok": True, 'token': token}
            if op == 'open_window':
                if set(request) != {'op'}:
                    raise ControlError('unsupported')
                return {'ok': True, 'token': a.open_window()}
            if op == 'close_window':
                token = request.get('token')
                if (set(request) != {'op', 'token'} or not isinstance(token, list)
                        or len(token) != 2 or not isinstance(token[0], str)
                        or type(token[1]) is not int):
                    raise ControlError('unsupported')
                a.close_window(token)
                return {'ok': True}
            if op in ("end_manual", "arm_login_close", "end_login"):
                getattr(a, op)(str(request["lease_id"]))
                return {"ok": True}
            if op == "events":
                after = request["after"]
                if not isinstance(after, int):
                    raise ControlError("unsupported")
                return {"ok": True, "events": [encode_event(e) for e in a.events_after(after)],
                        "dropped": a.dropped}
            if op == "status":
                p = a.policy
                return {"ok": True, "run_id": p.run_id if p else None,
                        'site_id': p.active_session.site_id if p else None,
                        'window_open': a.window_open, 'token': a.token,
                        'binding_key_id': a.key_id,
                        "revision": p.revision if p else None,
                        "site_origins": [origin for site in p.sites
                                         if site.site_id == p.active_session.site_id
                                         for origin in site.origins] if p else []}
            raise ControlError("unsupported")
        except ControlError as err:
            return {"ok": False, "error": err.code}
        except LookupError as err:
            return {"ok": False, "error": str(err.args[0]) if err.args else "refused"}
        except MemoryError:
            _out_of_memory()
        except (KeyError, TypeError, ValueError, AttributeError):
            return {"ok": False, "error": "unsupported"}

    async def start(self):
        self.build()
        try:
            self.control_path.unlink()
        except FileNotFoundError:
            pass
        self._control = await asyncio.start_unix_server(self._handle, path=str(self.control_path),
                                                        limit=MAX_LINE)
        os.chmod(self.control_path, 0o666)  # SO_PEERCRED is the check; the mode only lets uids connect
        self._task = asyncio.create_task(self.master.run())
        self._collector = asyncio.create_task(self._collect_garbage())
        # Wait until the three listeners accept connections.
        for _ in range(200):
            servers = self.master.addons.get("proxyserver").servers
            if all(s.is_running for s in servers) and len(list(servers)) == 3:
                break
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError("proxy listeners did not start")

    @staticmethod
    async def _collect_garbage(period: float = GC_PERIOD) -> None:
        """mitmproxy's finished flows sit in reference cycles holding their request bodies, and
        Python's full collection runs by allocation counts, not bytes: under repeated uploads
        hundreds of MB of finished bodies waited for it. A full collection on a short timer
        frees them; the objects alive at start-up are frozen out of it, so each pass is short."""
        gc.freeze()
        while True:
            await asyncio.sleep(period)
            gc.collect()

    async def stop(self):
        if self._collector is not None:
            self._collector.cancel()
        if self._control is not None:
            self._control.close()
        if self.master is not None:
            # mitmproxy closes its listeners only when told to serve no modes; its done hook
            # leaves them open until the process exits.
            try:
                await self.master.addons.get("proxyserver").servers.update([])
            except Exception:
                pass
            self.master.shutdown()
            try:
                await asyncio.wait_for(self._task, 10)
            except (asyncio.TimeoutError, Exception):
                pass


def export_ca(confdir: Path, public: Path = CA_PUBLIC) -> None:
    """Copy the public certificate (never the key) to the world-readable location."""
    src = confdir / "mitmproxy-ca-cert.pem"
    public.parent.mkdir(parents=True, exist_ok=True)
    tmp = public.with_suffix(".tmp")
    shutil.copyfile(src, tmp)
    os.chmod(tmp, 0o644)
    os.replace(tmp, public)


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise SystemExit("casa-browser proxy: CA directory is not ours")
    os.chmod(path, 0o700)


async def _main() -> int:  # pragma: no cover — exercised by the image integration tests
    quiet_logging()
    loop = asyncio.get_running_loop()
    import pwd
    controller_uid = pwd.getpwnam("casa-controller").pw_uid
    account = load_extra_hosts()
    from casa_browser.infra import onepassword_extension_hosts
    addon = EnforcingAddon(resolver=loop.resolve_unpinned, broker_hosts=account,
                           extension_hosts=onepassword_extension_hosts(next(iter(account), None)),
                           binding_key_path=BINDING_KEY)
    loop.pins = addon.pins
    _ensure_private_dir(CA_DIR)
    proxy = Proxy(addon=addon, confdir=CA_DIR, control_path=CONTROL_SOCKET,
                  controller_uid=controller_uid)
    await proxy.start()
    export_ca(CA_DIR)
    # s6 readiness (notification-fd 3): listeners up and CA exported.
    try:
        os.write(3, b"\n")
        os.close(3)
    except OSError:
        pass
    await asyncio.Event().wait()
    return 0


def main() -> int:  # pragma: no cover
    with asyncio.Runner(loop_factory=PinnedLoop) as runner:
        return runner.run(_main())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
