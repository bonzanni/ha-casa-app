"""The ingress identity table is validated at boot (#203).

A route dropped from the table would send its ingress back to the
unattributed ``system`` identity — the silent degradation #203 exists to
prevent. The check must therefore run at STARTUP, not on first use.

It also has to run EARLY and UNCONDITIONALLY. v0.125.0 crash-looped production
because a boot-path defect sat inside ``main()``'s ``if telegram_token:``
branch, which no unit test enters; a check buried behind a channel condition
would inherit exactly that blindness.
"""

from __future__ import annotations

import asyncio

import pytest


class _Sentinel(Exception):
    pass


class TestBootWiring:
    def test_main_validates_the_table_before_doing_anything_else(self, monkeypatch):
        import casa_core

        calls: list[str] = []

        def fake_validate():
            calls.append("validated")
            raise _Sentinel("stop here")

        monkeypatch.setattr(
            casa_core, "validate_ingress_identity_table", fake_validate)

        # If the check is early and unconditional, main() reaches it before any
        # config load, CLI probe or channel construction can intervene.
        with pytest.raises(_Sentinel):
            asyncio.run(casa_core.main())

        assert calls == ["validated"]

    def test_the_validator_is_bound_in_casa_core(self):
        import casa_core
        import ingress_identity

        assert (
            casa_core.validate_ingress_identity_table
            is ingress_identity.validate_ingress_identity_table
        )

    def test_the_shipped_table_validates_with_the_guest_route(self):
        # The route a friend's summons arrives on is in the shipped table and
        # under contract; the check that runs at boot accepts it.
        import ingress_identity

        assert "telegram_guest" in ingress_identity._INGRESS_IDENTITY
        assert "telegram_guest" in ingress_identity._ROUTE_CONTRACT
        ingress_identity.validate_ingress_identity_table()
