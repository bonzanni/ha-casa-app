"""The proxy's control plane, shared by both ends (Plan 1 Task 6).

The controller installs a ``RunPolicy`` into the enforcing proxy and reads its private
request events over an owner-checked Unix socket (``SO_PEERCRED``): newline-delimited JSON,
one request, one reply. ``encode_policy``/``decode_policy`` are the one wire form of a
policy; ``decode_policy`` refuses anything it does not recognise, so a malformed install
leaves the proxy's current policy in place.

``PrivateRequestEvent`` carries a request's origin and decision to controller-private memory
only; it has no ``to_wire`` and its private members are excluded from ``repr``.
"""
from __future__ import annotations

import asyncio
import json
import socket
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from casa_browser.policy import Lease, RunPolicy, SessionRef
from casa_browser.protocol import ProviderBinding, Site, SiteGrant

CONTROL_SOCKET = Path("/run/casa-browser/proxy/control.sock")
MAX_LINE = 8 * 1024 * 1024  # one control request or reply (an events reply is the longest)


class ControlError(Exception):
    """A refused or failed control call. ``code`` is fixed text, never request data."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PrivateRequestEvent:
    seq: int
    run_id: str | None
    revision: int | None
    listener: str  # 'browser' | 'broker' | 'download'
    allow: bool
    reason: str
    event_kind: str
    method: str
    origin: str | None = field(repr=False)
    resource_type: str
    is_navigation: bool
    at: float
    binding: dict | None = field(default=None, repr=False)


def encode_event(event: PrivateRequestEvent) -> dict:
    return {"seq": event.seq, "run_id": event.run_id, "revision": event.revision,
            "listener": event.listener, "allow": event.allow, "reason": event.reason,
            "event_kind": event.event_kind, "method": event.method, "origin": event.origin,
            "resource_type": event.resource_type, "is_navigation": event.is_navigation,
            "at": event.at, "binding": event.binding}


def decode_event(data: dict) -> PrivateRequestEvent:
    return PrivateRequestEvent(
        seq=data["seq"], run_id=data["run_id"], revision=data["revision"],
        listener=data["listener"], allow=data["allow"], reason=data["reason"],
        event_kind=data["event_kind"], method=data["method"], origin=data["origin"],
        resource_type=data["resource_type"], is_navigation=data["is_navigation"],
        at=data["at"], binding=data.get("binding"))


# --------------------------------------------------------------------------- policy wire form


def _lease(lease: Lease | None) -> dict | None:
    if lease is None:
        return None
    return {"lease_id": lease.lease_id, "run_id": lease.run_id, "site_id": lease.site_id,
            "generation": lease.generation, "kind": lease.kind}


def encode_policy(policy: RunPolicy) -> dict:
    return {
        "run_id": policy.run_id,
        "mode": policy.mode,
        "revision": policy.revision,
        "site_grants": [{"site_id": g.site_id, "access": g.access} for g in policy.site_grants],
        "sites": [{
            "site_id": s.site_id,
            "origins": list(s.origins),
            "login_origins": list(s.login_origins),
            "provider_bindings": [{"provider_origin": b.provider_origin,
                                   "path_tag": b.path_tag,
                                   "client_id_tag": b.client_id_tag, "redirect_uri_tag": b.redirect_uri_tag}
                                  for b in s.provider_bindings],
            "revision": s.revision,
        } for s in policy.sites],
        "login_lease": _lease(policy.login_lease),
        "manual_lease": _lease(policy.manual_lease),
        "active_session": {"kind": policy.active_session.kind,
                           "site_id": policy.active_session.site_id,
                           "generation": policy.active_session.generation},
    }


def _keys(obj: Any, keys: set[str]) -> dict:
    if not isinstance(obj, dict) or set(obj) != keys:
        raise ControlError("unsupported")
    return obj


def _s(value: Any) -> str:
    if not isinstance(value, str):
        raise ControlError("unsupported")
    return value


def _opt_s(value: Any) -> str | None:
    return None if value is None else _s(value)


def _i(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ControlError("unsupported")
    return value


def _strs(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ControlError("unsupported")
    return tuple(_s(v) for v in value)



def decode_lease(data: Any) -> Lease | None:
    if data is None:
        return None
    d = _keys(data, {"lease_id", "run_id", "site_id", "generation", "kind"})
    kind = _s(d["kind"])
    if kind not in ("login", "manual"):
        raise ControlError("unsupported")
    return Lease(_s(d["lease_id"]), _s(d["run_id"]), _s(d["site_id"]), _i(d["generation"]), kind)


def decode_policy(data: Any) -> RunPolicy:
    try:
        d = _keys(data, {"run_id", "mode", "revision", "site_grants", "sites",
                         "login_lease", "manual_lease", "active_session"})
        if not all(isinstance(d[k], list) for k in ("site_grants", "sites")):
            raise ControlError("unsupported")
        grants = tuple(SiteGrant(_s(g["site_id"]), _s(g["access"]))
                       for g in (_keys(x, {"site_id", "access"}) for x in d["site_grants"]))
        sites = []
        for raw in d["sites"]:
            s = _keys(raw, {"site_id", "origins", "login_origins", "provider_bindings", "revision"})
            bindings = tuple(
                ProviderBinding(_s(b["provider_origin"]), _s(b["path_tag"]),
                                _s(b["client_id_tag"]), _s(b["redirect_uri_tag"]))
                for b in (_keys(x, {"provider_origin", "path_tag", "client_id_tag",
                                    "redirect_uri_tag"}) for x in s["provider_bindings"]))
            sites.append(Site(site_id=_s(s["site_id"]), display_name="",
                              origins=_strs(s["origins"]), login_origins=_strs(s["login_origins"]),
                              vault_item_id=None, provider_bindings=bindings,
                              start_page_private=None, revision=_i(s["revision"])))
        session = _keys(d["active_session"], {"kind", "site_id", "generation"})
        return RunPolicy(
            run_id=_s(d["run_id"]), mode=_s(d["mode"]), site_grants=grants, sites=tuple(sites),
            login_lease=decode_lease(d["login_lease"]),
            manual_lease=decode_lease(d["manual_lease"]), revision=_i(d["revision"]),
            active_session=SessionRef(_s(session["kind"]), _opt_s(session["site_id"]),
                                      _i(session["generation"])))
    except ControlError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ControlError("unsupported") from None


# --------------------------------------------------------------------------- peer credentials


def peer_uid(sock: socket.socket) -> int:
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", creds)[1]


# --------------------------------------------------------------------------- controller side


class ProxyClient:
    """Controller-side client. Each call opens one connection, sends one line and waits for
    the reply, so ``install`` returns only after the proxy holds the new policy."""

    def __init__(self, path: Path = CONTROL_SOCKET, timeout: float = 10.0):
        self.path = path
        self.timeout = timeout

    async def _call(self, op: str, **args) -> dict:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self.path), limit=MAX_LINE), self.timeout)
        except (OSError, asyncio.TimeoutError):
            raise ControlError("proxy_unavailable") from None
        try:
            writer.write(json.dumps({"op": op, **args}).encode() + b"\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), self.timeout)
        except (OSError, asyncio.TimeoutError, ValueError):
            raise ControlError("proxy_unavailable") from None
        finally:
            writer.close()
        try:
            reply = json.loads(line)
        except ValueError:
            raise ControlError("proxy_unavailable") from None
        if not isinstance(reply, dict) or not reply.get("ok"):
            raise ControlError(str(reply.get("error", "refused")) if isinstance(reply, dict)
                               else "refused")
        return reply

    async def install(self, policy: RunPolicy, *, start: str | None = None) -> int:
        """``start``: the proxy start nonce (``status()['token'][0]``) the caller expects; a
        proxy that restarted since refuses with ``restarted`` and keeps no policy."""
        args = {"policy": encode_policy(policy)}
        if start is not None:
            args["start"] = start
        reply = await self._call("install", **args)
        if reply.get("revision") != policy.revision:
            raise ControlError("not_acknowledged")
        return reply["revision"]

    async def revoke(self, run_id: str) -> None:
        await self._call("revoke", run_id=run_id)

    async def begin_manual(self, lease: Lease, *, purpose: str):
        reply = await self._call("begin_manual", lease=_lease(lease), purpose=purpose)
        return self._token(reply)

    async def end_manual(self, lease_id: str) -> None:
        await self._call("end_manual", lease_id=lease_id)

    async def begin_login(self, lease: Lease):
        reply = await self._call("begin_login", lease=_lease(lease))
        return self._token(reply)

    async def arm_login_close(self, lease_id: str) -> None:
        await self._call("arm_login_close", lease_id=lease_id)

    async def end_login(self, lease_id: str) -> None:
        await self._call("end_login", lease_id=lease_id)

    async def events(self, after_seq: int) -> tuple[list[PrivateRequestEvent], int]:
        reply = await self._call("events", after=after_seq)
        return [decode_event(e) for e in reply["events"]], reply["dropped"]

    async def status(self) -> dict:
        return await self._call("status")

    async def open_window(self):
        reply = await self._call('open_window')
        return self._token(reply)

    @staticmethod
    def _token(reply):
        token = reply.get('token')
        if (not isinstance(token, (list, tuple)) or len(token) != 2
                or not isinstance(token[0], str) or type(token[1]) is not int):
            raise ControlError('not_acknowledged')
        return tuple(token)

    async def close_window(self, token):
        await self._call('close_window', token=list(token))
