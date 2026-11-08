"""Shared minimal RoleArtifactSource stub for unit tests that construct
AgentConfig directly (not through agent_loader.load_agent_from_dir).

Personality Phase A, Task 6 makes ``AgentConfig.role_artifact`` a required
keyword-only constructor field with no default (see ``config.py``) — every
direct-construction call site that is not itself testing the role-artifact
wiring needs SOME value there. These
tests don't exercise role_artifact content, so one shared, frozen, minimal
stand-in is enough; they must NOT reach into ``role_slot.materialize_role`` or
``role_artifact.load_role_artifact`` (which would validate/schema-check it).
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

from role_artifact import RoleArtifactSource

STUB_ROLE_ARTIFACT = RoleArtifactSource(
    role=MappingProxyType({
        "api_version": "casa.role/v1",
        "id": "resident:assistant",
        "kind": "resident",
        "slot": "assistant",
    }),
    doctrine="# Core doctrine\n\nUnit-test stub doctrine body.\n",
    role_path=Path("/nonexistent/stub/role.yaml"),
    doctrine_path=Path("/nonexistent/stub/doctrine.md"),
)
