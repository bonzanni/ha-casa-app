"""Opaque handles for page targets (Plan 1 Task 7).

The model never sees a selector or an address: it sees a random handle
per visible, actionable element of one observation, and the controller keeps the real target.
A handle is bound to its run and to the page generation it was issued for; any navigation of
the page (a new document, a client-side route or a fragment change, a frame navigation)
advances the generation, and every older handle stops resolving.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field


class StaleHandle(Exception):
    """The handle is unknown, another run's, or from an older page generation."""

    code = "stale_handle"


@dataclass(frozen=True)
class PrivateTarget:
    kind: str                                     # 'element' | 'link' | 'page'
    role: str = ""
    element: object = field(default=None, repr=False)   # the browser's element handle
    href: str | None = field(default=None, repr=False)  # a link's raw address
    frame_url: str | None = field(default=None, repr=False)
    label: str = ""                               # what the observation called it, masked


@dataclass(frozen=True)
class _Entry:
    run_id: str
    site_id: str | None
    generation: int
    target: PrivateTarget = field(repr=False)


class HandleRegistry:
    def __init__(self, token=lambda: secrets.token_urlsafe(12)):
        self._token = token
        self._entries: dict[str, _Entry] = {}

    def issue(self, run_id: str, site_id: str | None, page_generation: int,
              target: PrivateTarget) -> str:
        while True:
            handle = "h" + self._token().replace("=", "")
            if handle not in self._entries:
                break
        self._entries[handle] = _Entry(run_id, site_id, page_generation, target)
        return handle

    def resolve(self, handle: str, run_id: str, generation: int) -> PrivateTarget:
        entry = self._entries.get(handle) if isinstance(handle, str) else None
        if entry is None or entry.run_id != run_id or entry.generation != generation:
            raise StaleHandle()
        return entry.target

    def invalidate_page(self, generation: int | None = None) -> None:
        """Drop every handle of ``generation`` and older (all handles when ``None``)."""
        self._entries = {h: e for h, e in self._entries.items()
                         if generation is not None and e.generation > generation}

    def drop_run(self, run_id: str) -> None:
        self._entries = {h: e for h, e in self._entries.items() if e.run_id != run_id}

    def __len__(self) -> int:
        return len(self._entries)
