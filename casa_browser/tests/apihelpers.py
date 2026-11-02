"""Shared helpers for the API tests: a paired fake app and a valid RunSpec body."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from casa_browser.api import make_app
from casa_browser.protocol import PROTOCOL_HEADER, Site, spec_digest

from fake_controller import FakeController


class Clock:
    def __init__(self, start: datetime | None = None):
        self.now = start or datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


SITE = Site("site_a", "Site A", ("https://a.example",), ("https://login.a.example",),
            "item_a", (), "https://a.example/start", 1)
PUBLIC_SITE = Site("site_pub", "Public", ("https://pub.example",), (), None, (), None, 1)


def run_members(request_id: str = "req-1", **over) -> dict:
    members = {"request_id": request_id, "requester_id": "finance", "mode": "account",
               "task_text": "Download the March invoice", "task_kind": "invoice",
               "sites": [{"site_id": "site_a", "access": "account"}],
               "result_expectation": "one PDF"}
    members.update(over)
    return members


def run_body(request_id: str = "req-1", **over) -> dict:
    members = run_members(request_id, **over)
    return {**members, "admission": {"origin_kind": "operator_telegram", "origin_ref": "tg-msg-1",
                                     "admitted_spec_digest": spec_digest(members)}}


def headers(secret: str | None, protocol: str | None = "1") -> dict:
    out = {}
    if secret is not None:
        out["Authorization"] = f"Bearer {secret}"
    if protocol is not None:
        out[PROTOCOL_HEADER] = protocol
    return out


def make_fake(tmp_path, clock: Clock | None = None, **kw) -> FakeController:
    fake = FakeController(tmp_path / "state.db", clock=clock or Clock(), **kw)
    fake.store.put_site(SITE)
    fake.store.put_site(PUBLIC_SITE)
    return fake


def pair(fake: FakeController) -> str:
    fake.pairing.arm()
    secret = fake.pairing.claim("Casa (Fido plugin)", "172.30.32.5")
    assert secret
    return secret


async def paired_client(aiohttp_client, tmp_path, clock: Clock | None = None, **kw):
    fake = make_fake(tmp_path, clock, **kw)
    secret = pair(fake)
    client = await aiohttp_client(make_app(fake))
    return client, fake, secret
