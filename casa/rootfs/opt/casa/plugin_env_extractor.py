"""Parse a plugin's .mcp.json and return the set of ${VAR} references
not in CC's built-in allowlist. Plan 4b §4.2 + §7.3 step 6.

#423 r2 (Sol 5): the scan covers every string value of a server's LAUNCH
fields — ``command``, ``args``, ``url``, ``headers`` and ``env`` — because
the CLI expands ``${VAR}`` in all of them, and an unresolved reference in
any of those positions reaches the spawned server as the literal
placeholder string. Unknown extension fields are NOT scanned (r3, Sol
r2-4): the parser tolerates them, but they are not launch configuration.
"""
from __future__ import annotations

import re
from pathlib import Path

CC_BUILTIN_VARS: frozenset[str] = frozenset({
    "CLAUDE_PLUGIN_ROOT",
    "CLAUDE_PLUGIN_DATA",
    "HOME",
    "PATH",
    "USER",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "PWD",
})

# The bare form ONLY — a reference Casa must withhold the plugin for.
_VAR_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")
# #431: both documented forms. ``${VAR:-default}`` is NOT a requirement (the
# CLI substitutes the default, so nothing is missing and no placeholder can
# leak) — but it IS a reference, and the surfaces that answer "which env
# names does this tree touch?" must see it: the cross-plugin
# ENV_NAME_COLLISION preflight, and the install consent's DISCLOSURE of
# referenced names. Keeping them on the bare-form pattern would let a
# bundled plugin quietly reuse a name another plugin owns simply by writing
# the defaulted form. #994: the consent's "Secrets required" line and the
# commit result's `required_env_vars` are NOT such a surface — they are
# asks, and they take the requirement set (minus casa.setupProvides).
_ANY_VAR_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-[^{}]*)?\}")


# #423 r3 (Sol r2-4): the CLI expands ${VAR} in the LAUNCH fields only — an
# unknown extension key the parser tolerates (metadata etc.) is not part of
# the launch configuration, and a reference there must not become a
# requirement that withholds the plugin.
_EXPANDED_FIELDS = ("command", "args", "url", "headers", "env")


def _iter_strings(node):
    """Every string LEAF value under *node* (dict values, list members,
    nested arbitrarily). Dict keys are not scanned — the CLI expands
    values, not keys."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for val in node.values():
            yield from _iter_strings(val)
    elif isinstance(node, (list, tuple)):
        for val in node:
            yield from _iter_strings(val)


def _extract(mcp_json_path: Path | str, pattern) -> set[str]:
    # Sol CI-review: resolve servers via the ONE shared parser so secrets are
    # extracted for BOTH the mcpServers wrapper AND the top-level shape (context7
    # ships the latter) — otherwise a top-level plugin's required secrets would be
    # silently missed and verification could report ready without them.
    # #330's non-mapping-env tolerance is inherited: _iter_strings walks any
    # shape without raising.
    from plugin_store import mcp_servers_map
    vars_found: set[str] = set()
    for server in mcp_servers_map(mcp_json_path).values():
        if not isinstance(server, dict):
            continue
        for field in _EXPANDED_FIELDS:
            for val in _iter_strings(server.get(field)):
                for match in pattern.finditer(val):
                    vars_found.add(match.group(1))
    return vars_found - CC_BUILTIN_VARS


def extract_env_vars(mcp_json_path: Path | str) -> set[str]:
    """The REQUIRED references — bare ``${VAR}`` only. This is what the
    withhold gate and the verify secrets rows consume: a ``${VAR:-default}``
    is satisfied by its own default, so withholding for it would be wrong."""
    return _extract(mcp_json_path, _VAR_PATTERN)


def extract_referenced_env_vars(mcp_json_path: Path | str) -> set[str]:
    """EVERY referenced name, both forms (#431) — for the surfaces that ask
    "which env names does this tree touch?" rather than "what must resolve":
    the cross-plugin collision preflight and the consent's disclosure line.
    Never for an ask (#994): what the operator is asked to supply is
    :func:`extract_env_vars` minus the manifest's ``casa.setupProvides``."""
    return _extract(mcp_json_path, _ANY_VAR_PATTERN)
