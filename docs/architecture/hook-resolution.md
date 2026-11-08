---
last_reviewed: 2026-09-22
---

# Hook resolution and the executor hooks document

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How an agent's hooks are resolved in process, how an executor's hooks document is validated
and captured at load, the one read grant a hook bundle can carry, and the post-tool matchers
that record evidence. The tool surface and its grants live in
[`architecture/mcp-and-tools.md`](mcp-and-tools.md).

## Mental model

**An executor's hooks document is validated and captured once, at load.** The hooks document
an executor carries is a mutable trust surface — `hooks_file:` is a pointer the configurator
may legitimately rewrite — so `load_all_executors` reads it once, validates it against the
schema, builds its policy callbacks once to prove every policy constructible
(INV-MCP-007), and captures what loaded verbatim onto `ExecutorDefinition.hooks_document`
(`{}` only when the executor has no hooks file). The in-casa executor build reads that
snapshot rather than the file on disk, so an edit to `hooks.yaml` after load changes nothing
a session enforces until a reload or a fresh load sees it, and is validated there exactly as
at boot. The build then appends the code-mandatory guards — the managed-component, agent-home
settings, trigger-file, response-shape and resident-prompt guards — regardless of what the
document declares; yaml policies are additive only.

**An in-process agent's hooks are resolved once, at construction, and one agent's carry a
read grant.** `resolve_hooks` turns a hooks config into SDK matchers; a config with no
`pre_tool_use` entries yields the default bundle — `block_dangerous_bash` plus a `path_scope`
whose readable and writable lists are the agent's `cwd`, or empty when it has none, and an
empty list denies every path. Its `extra_readable` argument is appended to the readable list
of *every* `path_scope` entry it builds — default bundle and explicit hooks file alike — and
never to `writable`. `Agent` passes it only for the Telegram default agent, with exactly that
role's own inbound-file `ready/` directory for a role that has an inbox — the Telegram default
agent and the specialists it declares as delegates — and an empty tuple for every other role;
the in-casa executor build passes nothing, so it never resolves a wider scope than its
snapshot. The grant's contract is INV-FILE-002 in [`inbound-files.md`](inbound-files.md).

**Two post-tool matchers record evidence rather than deciding anything.**
`read_evidence_matchers(role)` adds a `PostToolUse` and a `PostToolUseFailure` matcher on
`Read` to every resident's bundle in `Agent._build_options`: for a path under
`agent_inbox.readable_prefixes(role)` — normalised exactly as the `path_scope` check
normalises — the running turn's scope is told `read_ok` or `read_failed`, and a read aimed
at an inbox file the turn never listed makes it a file turn too. A read the `path_scope`
guard denies produces no post event, so a denied read is never evidence; a role without an
inbox records nothing; no scope bound records nothing; and a failure inside the recorder is
logged and returns an empty decision, so evidence never breaks the tool. What the evidence
discharges is [`output-boundary.md`](output-boundary.md)'s INV-OUT-002; the
delegated-specialist and in-casa executor builds do not carry these matchers.

## Contracts & invariants

**INV-MCP-007**: A hook policy parameter of the wrong type fails the build of that policy, and an executor whose hooks document carries one fails to load.

The hooks schema leaves per-policy parameters open, so a mistyped value is schema-valid and
reaches the policy factory intact. The scope parameters are lists of path prefixes and every
consumer iterates them — and a string is iterable. A `writable: /config` written without the
list dash therefore expanded character by character into a prefix set containing a bare `/`,
which prefix-matches every absolute path: the scope guard admitted precisely the writes it
was configured to refuse. A wrong type now raises out of the factory, where an unrecognised
parameter *name* already did, and because the loader builds every executor's callbacks once
as a constructibility check, the executor fails closed at load rather than enforcing a
widened scope at runtime. The rule is deliberately uniform across every parameter in the
registry, including where coercion would have been available and harmless-looking: the
boolean gating resident deletion is required to be a boolean, because truthiness is not a
type check in either direction — a falsy non-boolean would silently disable the guard and a
truthy one silently enable it — and the commit-size limit is required to be a whole number
rather than coerced, because a coercion is a guess at what the author meant.

What it does not cover: a well-typed but wrong value. A list of prefixes that is simply too
broad builds and enforces exactly what it says — this rule constrains the shape of a
parameter, never its meaning.

**INV-MCP-012**: `path_scope` admits a Read of a file in the calling session's own `tool-results/` directory — the session the runtime names on that very call, under the projects root its reported transcript sits in, in any project folder — and nothing else outside its readable prefixes; it never widens `writable`.

Claude Code does not hand an oversized tool result to the model: it writes it to
`<claude home>/projects/<project>/<session>/tool-results/` and tells the model to Read that
file. The Claude home is private, so a `path_scope` whose readable list is the agent's `cwd`
denied that Read and the result was lost after the tool had already run. The hook now also
admits a Read whose path is `<root>/<any project>/<session>/tool-results/<file>`, where
`<root>` is the folder two levels above the reported `transcript_path`
(`<root>/<project>/<session>.jsonl`) and `<session>` is the reported `session_id` or the
transcript's own stem. The project folder is left free because a session resumed under
another working directory writes its results under that directory's project folder; copying
Claude Code's folder naming into Casa instead would drift. No usable transcript path (absent,
empty, relative, not a `.jsonl`) grants nothing. A transcript, another session's results,
the directory itself and any sibling directory stay denied, and Write/Edit are unaffected.

What it does not cover: whether Claude Code diverts a result at all, or how large one may
be — Casa sets no MCP output limit, so the runtime's defaults decide.

## Failure behavior

**An executor's hooks document does not build.** The executor fails to load (INV-MCP-007);
nothing falls back to a default-configured policy set for it.

## Extension points

**A new hook policy** joins the registry with typed parameters; the factory's uniform
type-refusal (INV-MCP-007) then covers it — do not coerce, and do not accept a policy
parameter shape the registry does not declare.

**A new surface that builds hooks from an executor's document** must read the load-time
snapshot, never the file, and keep the code-mandatory guards appended.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/hooks.py::resolve_hooks`
- `casa/rootfs/opt/casa/agent_loader.py::read_hooks_document`
- `casa/rootfs/opt/casa/hooks.py::build_policy_callbacks_from_hooks_yaml`

**Tests**
- `tests/test_hooks_policy_param_types.py`
- `tests/test_hooks.py`
- `tests/test_hooks_policies.py`

**Related**
- [`architecture/mcp-and-tools.md`](../architecture/mcp-and-tools.md)
- [`architecture/engagements.md`](../architecture/engagements.md)
<!-- END SOURCEMAP -->
