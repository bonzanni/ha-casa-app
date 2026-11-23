"""Bounds for proactive voice-job delivery.

v0.125.0 (#228): these were three add-on options
(``voice_route_freshness_seconds``, ``voice_job_delivery_ttl_seconds``,
``voice_job_route_cap``) read from env. They are internal tuning constants —
an operator has no basis on which to choose a route-freshness grace period or
a per-route job cap — so they are now fixed here.
"""

from __future__ import annotations


#: Grace period in which an authenticated, background-capable route may
#: briefly disconnect while Casa still accepts specialist work for it.
ROUTE_FRESHNESS_S = 60
#: Maximum time a completed background voice result is retained for delivery.
#: A specialist may request a shorter privacy expiry; the shorter value wins.
DELIVERY_TTL_S = 900
#: Maximum active-or-ready specialist jobs held for one voice route.
ROUTE_CAP = 5


__all__ = ["DELIVERY_TTL_S", "ROUTE_CAP", "ROUTE_FRESHNESS_S"]
