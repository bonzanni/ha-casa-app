---
last_reviewed: 2026-07-31
---

# Observability: logging, correlation and redaction

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How work is traced across components and what reaches a log. It covers correlation ids,
log structure, redaction and what the health surfaces actually assert. It does not cover
metrics, of which there are none to speak of.

## Mental model

**A correlation id threads a request across components**, bound into context so that log
lines from the same work can be tied together.

**The emitted records are structured, and JSON is the default.** Output is one-line JSON
unless `LOG_FORMAT=human` selects UTC human-readable text; structured extras are flattened
into the record, and the access line carries method, path with query, status, duration and
bytes. A log consumer configured from guesswork parses production output wrong.

**Per-turn token telemetry is the cost signal.** Every SDK turn emits a token summary
(input, output, cache counters), and a budget tracker warns — once per session — after
three consecutive completed turns land above 110% of the memory-envelope budget; a turn
counts once however many times its SDK call was retried, and a failed turn counts not at
all. Those lines are the
principal evidence for cost, cache and context regressions; nothing else reports them.

**Two different ids can describe one request, and a caller can supply one of them.** The
inbound header is validated to a strict shape. A correlation id supplied inside an invocation
payload is *not* validated. The lack of validation is deliberate, so an external system can
thread its own identifier through; only a missing, empty or non-string payload id is
replaced — on the HTTP invoke route with the request's own id, so turn and access log share
one id exactly when the caller supplied none. The consequence is worth
knowing: the value bound for the turn can be arbitrary caller text, and it may differ from
the one on the HTTP access line for the same request.

**Redaction is layered where each piece of a record becomes text.** The filter on the
application's own handler inspects the message and its arguments — the ordinary case of a
secret interpolated into a log line. Exception text and structured extras only become
strings later, inside the formatters, so the application's formatters redact them at render
time: formatted tracebacks pass through the same redaction as messages, and extras get the
same per-value walk as dict arguments (credential-named keys masked wholesale, benign
labels preserved). One deliberate exemption: numeric values under token-*count* keys —
per-turn token telemetry — stay legible, while a numeric value under any other
credential-named key is masked like everything else. Stack text differs by format: the
human formatter renders it redacted; the JSON formatter does not emit stack text at all.

**It is still not a guarantee.** Outside its reach: output from subprocesses, and any
handler that is not the application's own — a foreign handler is *not guaranteed* to be
covered. It may incidentally see redacted values (the filter mutates the record's message
and arguments in place, and the human formatter caches redacted exception text on the
record), but nothing promises it, and a foreign handler that formats first sees raw text.
The filter is deliberately on the handler rather than the root logger, because root-logger
filters do not run for records propagated from descendants.

**Health surfaces assert less than their names suggest.** The health endpoint returns a fixed
success response — it says the process is answering requests, and nothing about agents,
channels, memory or dependencies. The dashboard reports *configuration presence*: which
transports are configured, which model is selected, how many triggers exist. Neither is an
operational health check, and treating either as one reads in something that is not there.

## Contracts & invariants

**INV-OBS-001**: A correlation id arriving on the request header is validated to a fixed shape; one supplied inside an invocation payload is not.

The asymmetry is intentional — external systems thread their own ids — but it means one of
the two paths accepts arbitrary text.

**INV-OBS-004**: Redaction runs at every point the application's own logging pipeline turns record content into text — the filter for message and arguments, the formatters for exception text, stack text and structured extras.

The id replaces a retired predecessor (OBS 002) whose meaning inverted: the old statement
pinned the *absence* of exception coverage as if it were the contract, and the fix
flipped what the sentence asserted. A flipped assertion gets a fresh id.

What it does not cover, and this is the operative part: the pipeline is installed on the
application's handler rather than globally, so a record reaching any other handler has no
redaction guarantee, and subprocess output never passes through it at all. Within the
pipeline, redaction recognises patterns, registered exact values and credential-named
keys — an unregistered, pattern-less secret under a benign key still passes.

**INV-OBS-003**: The health endpoint returns a fixed success response without consulting any subsystem.

What it does not cover: everything. It is a liveness signal for the process, not a readiness
signal for the system.

## Failure behavior

**A secret appears in an exception.** The formatter redacts it on the application's own
handler — if the value matches a known pattern or was registered at load. An unregistered,
pattern-less secret still passes through, so exception logging near secret material remains
a place to be deliberate.

**A caller supplies an unusual correlation id.** It is used. Downstream consumers that
validate identifiers more strictly may then reject it, so the failure surfaces late and away
from its cause.

**Logging itself fails.** Standard library behaviour applies; the application adds no handling
of its own.

## Extension points

**A new log call near secret material** should still redact at the call site rather than
relying on the pipeline: pattern and exact-value redaction only catch what they recognise,
and registering the value at load is what makes an opaque secret recognisable.

**Widening redaction** now means extending the pattern list or registering values — the
formatter already covers exception text and extras. What cannot be widened from here is
coverage of handlers the application does not own.

**A real readiness check** would be a new surface. Neither existing one can be extended into
it without changing what it means, and other things depend on the current behaviour.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/log_cid.py::install_logging`
- `casa/rootfs/opt/casa/log_cid.py::new_cid`
- `casa/rootfs/opt/casa/log_cid.py::HumanFormatter`
- `casa/rootfs/opt/casa/log_cid.py::JsonFormatter`
- `casa/rootfs/opt/casa/log_cid.py::_RedactingRenderMixin`
- `casa/rootfs/opt/casa/log_redact.py::RedactingFilter`
- `casa/rootfs/opt/casa/log_redact.py::redact`
- `casa/rootfs/opt/casa/log_redact.py::redact_extras`
- `casa/rootfs/opt/casa/casa_core.py::healthz`

**Tests**
- `tests/test_log_redact.py`
- `tests/test_log_cid.py`

**Related**
- [`architecture/http-surface.md`](../architecture/http-surface.md)
- [`architecture/overview.md`](../architecture/overview.md)
<!-- END SOURCEMAP -->
