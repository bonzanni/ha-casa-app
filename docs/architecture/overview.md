---
last_reviewed: 2026-08-07
---

# System shape

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What Casa is made of and how the pieces relate: the supervised services, the three agent
tiers, and where configuration enters. It is the file to read first and the one to leave
quickly — each area has its own document, and this one deliberately stops at the boundary
of each.

## Mental model

Casa is a Home Assistant app: a container running several supervised services, inside which
a fleet of Claude-powered agents answers messages arriving over Telegram and a voice
channel.

Four services run under s6, each with a single job:

| Service | Job |
|---|---|
| `svc-casa` | the main async application — channels, agents, HTTP surface |
| `svc-casa-mcp` | an MCP bridge, supervised as its own service with its own lifetime |
| `svc-nginx` | the front door — two listeners: HA ingress (Supervisor-source-restricted) and the published external API port, which has no source restriction and relies on route-level refusals |
| `svc-ttyd` | an optional terminal, off unless enabled — an unauthenticated root shell, so it binds a root-restricted UNIX socket (not TCP loopback) that only the nginx worker and root can reach, keeping it away from a dropped-uid engagement |

The static service table is not the whole process story: executor engagements compile and
supervise *dynamic* s6 service/logger pairs of their own — created, recovered and forcibly
terminated per engagement, outside the four services above. And two environment switches
shape the process family broadly: `LOG_FORMAT` selects the log output shape (the format
itself belongs to `architecture/observability.md`), and the `CASA_*` path/version variables
(`CASA_CONFIG_DIR`, `CASA_DATA_DIR`, `CASA_DEFAULTS_DIR`, `CASA_BINDINGS_DIR`,
`CASA_VERSION`, `CASA_IMAGE_VERSION`) redirect *specific loaders and reports*, not the
application uniformly — treating them as global root overrides silently splits reads from
writes.

Four `init-*` one-shots exist — config validation, config materialisation, nginx setup and
the plugin store — but they do **not** all precede every service. Each service names only
the one-shots it needs: the nginx setup gates `svc-nginx` alone, the plugin store gates both
the main application and the MCP service, and the terminal depends only on config
materialisation. Read the ordering in the `dependencies.d` directories, not here and not in
a log statement; it is data, and it is what is actually true.

Agents come in three tiers. Tier is the main axis along which lifetime, reach, validation
strictness and failure isolation all vary together — it is not only about what an agent can
do:

| Tier | Lifetime | Reach |
|---|---|---|
| **Resident** | long-lived, a fixed set of slots | memory, delegation, channel reach |
| **Specialist** | ephemeral per delegated invocation, role-keyed | no channel of its own |
| **Executor** | ephemeral, task-bounded | outside the role registry entirely |

One boundary matters more than the table: **the role registry models residents and
specialists only.** Its tier type admits those two, and executors load separately. Reasoning
about executors through the registry is the likeliest way to be wrong here;
`architecture/agent-taxonomy.md` sets that boundary out.

## Contracts & invariants

**INV-SYS-001**: Config materialisation depends on config validation, so the validating one-shot runs first and a failure there stops what depends on it.

The dependency is declared in `init-setup-configs/dependencies.d` — check it there rather
than taking this sentence's word for it.

**INV-SYS-002**: A role claimed by both a resident and a specialist is a boot failure, raised when the role registry is built.

Scoped deliberately to those two tiers, because that is what the registry models. Only
`_build_role_registry` refuses the collision; `AgentRegistry.build` itself registers residents
first and then *skips* a colliding specialist with a warning, so a collision reaching it after
boot keeps the resident instead of raising. See `architecture/agent-taxonomy.md`
(INV-AGENT-001) for that tie-break and its limits.

## Failure behavior

**Validation fails.** Boot stops at the `init-validate-config` one-shot. Nothing later runs,
so the app does not come up in a partly-configured state.

**The one-shots do not treat failure alike.** All four sit in dependency chains, so any of
them failing blocks what depends on it — that part is uniform. What differs is how hard each
one tries to fail. Config validation is the only one that reports an ordinary configuration
defect as a non-zero exit; that is its job. The others mask most of what goes wrong
internally, degrading rather than stopping — the plugin store forces a success exit outright,
because a non-zero exit there would block the services that depend on it.

That is a statement about intent, not a guarantee. A shell parse error or an unexecutable
script still fails a one-shot that "cannot fail" by design. Read "one-shot" as "gates what
follows", never as "must succeed" or as "will report a problem".

**A service fails.** Each longrun is supervised separately, so their lifetimes are
independent, and what happens on failure differs per service — one stops the whole app,
others log and let the supervisor restart them, and the terminal deliberately exits in a way
that suppresses restart when it is disabled. That is set by the service definition; read it
there rather than assuming a policy.

**Boot refuses for a reason not in the manifest.** Configuration is not the only fatal
class. The application also refuses to start when its declared ingress-identity table and
its independently hand-written route contract disagree — a coherence check between two
declared tables, not an audit of the routes actually registered — and that check runs
before almost anything else. See `architecture/http-surface.md`.

**An option is removed from the manifest.** Removing the schema key is not sufficient on its
own — the pruning path in the config-materialisation script is what discards a stored value.
Both edits belong in the same change.

## Extension points

A new service means a new `s6-rc.d` directory and the dependency files that place it in the
ordering; nothing in Python decides startup order.

A new agent means a directory of configuration artifacts under the tier it belongs to — but
what else it requires depends heavily on the tier, and the resident slots are a fixed set
that cannot simply be extended. `architecture/agent-taxonomy.md` sets out what each tier
actually allows; treating them alike is the common mistake.

A new option means the app manifest, the translations file, and whatever reads it. An
option that is only read by shell during boot still belongs in the manifest, because that
is what the host renders.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/casa_core.py::main`
- `casa/rootfs/opt/casa/runtime.py::CasaRuntime`
- `casa/config.yaml::options`

**Tests**
- `tests/test_casa_core_agent_loading.py`
- `tests/test_setup_nginx_ingress.py`

**Related**
- [`architecture/agent-taxonomy.md`](../architecture/agent-taxonomy.md)
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
<!-- END SOURCEMAP -->
