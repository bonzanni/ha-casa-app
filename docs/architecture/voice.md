---
last_reviewed: 2026-08-25
---

# The voice channel

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How spoken input becomes a turn and how the answer gets back: the two transports, what
authenticates them, the turn budget, and the deferred-delivery path that lets an answer
arrive after the turn is over. It does not cover speech recognition or synthesis, which
happen outside this application, nor the companion integration's own internals. What the
voice prompt carries, and the voice surface's persona/token admission ceilings, are
[`personality.md`](personality.md)'s.

## Mental model

**Two transports, and they are not equivalent.** A request-shaped path streams one answer
back. A socket path additionally carries route binding, specialist handoff, and deferred
delivery. Anything about background work applies only to the socket path — the request path
can never qualify for it.

**Authentication is at the door, not on each message.** Every voice route fails closed
without a configured secret. The request path signs its body; the socket upgrade and the
agent catalog both sign empty bytes, which means one signature authenticates both of those
and there is no per-frame signature after an upgrade succeeds. Once a socket is open, its
frames are trusted because the connection was.

**The critical trust boundary: a route's declared capabilities describe the client's
transport, not the device.** Registration checks that a client speaks the expected protocol
and claims the full capability set — but nothing in it establishes that any particular
speaker or screen can actually play or show an answer. This application cannot inspect the
home's device registry to find out.

What actually decides delivery is **per-utterance**: the utterance frame supplies the device
id, and the frame's delivery offer supplies the modality and receipt strength. An
authenticated client can overclaim there, and the code accepts that knowingly: the trust
boundary is the authenticated connection, and a dishonest client is out of scope. Delivery
can therefore still fail later, at the endpoint rather than at the decision.

**The turn is bounded by an absolute deadline anchored at true ingress.** On the request path
it is captured in the handler's first statements — *before* the body is read and before
authentication — so that slow I/O counts against the budget rather than hiding from it. On
the socket path it is captured when a decoded frame is dispatched as an utterance — so the
handshake, frame receipt and JSON parsing are all outside the budget there. Deferred jobs deliberately outlive it.

**An utterance's route identity is pinned when its frame is received.** The reader stamps
the server-bound route, capabilities and job-control identity onto the frame at ingress, so
a registration frame that races an already-received utterance cannot redirect that turn's
deferred answer or handoff to the new binding.

**Sessions are keyed by role and scope together**, so the same speaker talking to two agents
gets two sessions rather than one shared context.

**The transports themselves are environment-configured.** `VOICE_SSE_ENABLED` /
`VOICE_WS_ENABLED` decide which routes exist at all (both off removes the channel),
`VOICE_SSE_PATH` / `VOICE_WS_PATH` set the public paths, and
`VOICE_IDLE_TIMEOUT_SECONDS` bounds session-pool idle eviction. A proxy or integration
pointed at the default paths breaks silently if these move.

**Direct HA voice turns run under a tool-loop guard.** On the `ha_direct` attempt shape, a
second successful live-context call — or a second validation-correction failure —
terminates the turn rather than looping. A prompt or tool change can trip this on
otherwise valid turns; other voice roles do not carry the guard.

## Contracts & invariants

**INV-VOICE-001**: Every voice route refuses a request when no secret is configured or the signature does not match.

Enforced by the channel's verification helper, called by the request handler, the socket
upgrade and the catalog alike. A missing secret returns false before any comparison.

What it does not cover: there is no per-frame signature, nonce, timestamp, or binding to
method or path. The empty-body signature that authenticates the catalog also authenticates a
socket upgrade. "Refuses" includes a malformed header: the verification helper rejects a
non-ASCII signature before the comparison primitive sees it, so all three routes return
unauthorised rather than a server error.

**INV-VOICE-002**: An agent is reachable over voice only if its configuration declares the voice capability.

Enforced at dispatch on both transports. Unknown agents and agents that exist but are not
voice-capable are deliberately indistinguishable to the caller.

What it does not cover: the *enabled* flag. Being disabled removes an agent from the catalog
but not from dispatch — dispatch authorises on the declared capability alone, so a disabled
agent that still declares voice remains reachable by a caller that knows its role.

**INV-VOICE-003**: The agent catalog is complete or it fails; it never returns a partial list.

Enforced when the catalog is built — a malformed *enabled, voice-capable* entry or too many
entries raises (disabled and non-voice configs are skipped before validation), and the
handler turns that into a service error. The reasoning is that a silently short catalog would
present a missing agent as a nonexistent one.

What it does not cover: appearing in the catalog says nothing about whether that agent will
handle a turn successfully.

**INV-VOICE-004**: A route registers only with the expected protocol version and exactly the full capability set.

Enforced by the route registry, which checks containment in both directions — so a client
offering extra capabilities or missing one is refused, and a refusal is acknowledged with an
empty accepted set rather than silently ignored.

What it does not cover: it cannot prove the client implements what it claims.

**INV-VOICE-005**: Caller-supplied context cannot mint trusted route, device, job-control, handoff or delivery values.

Enforced by sanitising external context and then overwriting the trusted keys from the
connection and the frame. This is why a payload cannot promise itself a delivery route.

What it does not cover, and the distinction is easy to lose: the *frame itself* legitimately
supplies a device id and a delivery offer. Those are trusted because the connection is
authenticated, not because they were verified.

**INV-VOICE-006**: Deferred delivery sends only the modality recorded on the job; an unrecognised or absent modality is never guessed.

Enforced in the delivery coordinator's offer path. A job whose modality is unknown waits and
expires rather than being sent as speech on the assumption that speech is what was wanted.

**INV-VOICE-007**: An error line that follows speech already voiced in the same turn is spoken as a retraction of it, in one frame.

A voice write is irreversible — the bytes are on the wire and the listener has heard them.
A turn can stream a partial answer and *then* fault, so the error line that follows is a
correction of something Casa already said, not a substitute for it. Without one, the
listener is left holding a statement Casa never stood behind.

Enforced by one composition step that every error path shares — the sink used by the
agent's classified error branch, and each transport's own last-resort handler, which is
where a turn that times out in the bus lands. That path composed its own frame and so
skipped the retraction until the reviewers reproduced it; the *text* is now shared while
each path keeps writing its own frame. That separation is deliberate, and the reason is
selection, not delivery: the socket sink suppresses a foreground error once the handoff
future is resolved, and a last resort must not inherit that guard — because the ordinary
way into that branch is a turn that faulted in the bus, arriving after the *unused* handoff
future was cancelled and so already counted as resolved, on a socket that still works,
where the frame is the listener's only telling. When the handoff's own write was refused by the
transport's closing-state guard, the last-resort frame is attempted and cannot arrive
either: that guard refuses before any byte and never unlatches. The durable job is
re-offered on reconnect instead (#619).

**Delivery, not selection.** The predicate is a per-turn witness that only a *completed*
speech write records. It is deliberately not the flag the handlers already keep for handoff
selection — which the socket sets *before* starting a write, so that no tool can claim a
handoff while speech is in flight, and which SSE, having no handoff to protect, sets after.
Those are different questions, and answering the second with the first retracts speech
nobody heard: a socket write the transport rejects marks speech selected though nothing
arrived, and on either transport the final tail block — the only speech an answer with no
sentence boundary produces — delivers without changing selection at all. Both were
reproduced against the borrowed flag before the witness replaced it. The "still working"
progress notice records nothing, because it is not an answer, so a turn that voiced only
that gets its error line unchanged.

**One frame, not two.** The retraction and its reason are a single `error` frame whose
spoken text carries both sentences. Emitting them as two writes would let the first land
and the second fail — a client disconnecting between them leaves the listener with a
retraction of nothing, which is worse than the contradiction it was meant to fix.

**Never a retraction with no reason.** An error line can be schema-valid and still say
nothing aloud — whitespace, or a bare `[tag]`, which is a delivery instruction rather than a
sentence. Retracting into that silence produces the very outcome above, so a line with
nothing speakable in it is left exactly as it was, and a retraction with nothing speakable
in it is not prefixed. Both are judged on the canonical text rather than the rendered
string, because a tag-preserving dialect renders `[flat]` to a non-empty string the listener
still hears nothing of.

What it does not cover: text already spoken cannot be unsaid, only corrected. The wording
is overridable per persona through `voice_errors`; setting `retraction` to an empty string
switches retractions off for that persona, while omitting the key takes the default.

## Failure behavior

**No secret, or a bad signature.** All voice routes return unauthorised for a missing,
mismatched or non-ASCII signature. This is a configuration failure that presents as an
authentication failure.

**A malformed request or an unknown agent.** Invalid JSON — including valid JSON whose top
level is not an object — and a missing or non-string prompt return client errors; an
unknown agent, and a role field that is not a string at all, get the same generic
not-found. A malformed scope or context field falls back to the anonymous scope rather than
failing the request. The socket path emits a typed error frame before dispatching anything
to an agent.

**Malformed socket frames.** Frames that fail to parse, are not objects, or carry an
unrecognised type are skipped rather than closing the connection. A registration frame
whose capability list is malformed — wrong container, non-string or non-hashable elements,
or the wrong capability set — is refused with an empty accepted set while the socket stays
open, and a non-string utterance or cancel id is treated as absent. A refused
re-registration leaves the connection's previous binding in place; a duplicate utterance id
cancels the in-flight original before the replacement runs. This is an *input*
guarantee, not a blanket one: an internal failure while handling a well-formed frame still
aborts the reader and closes the socket, deliberately, so the client reconnects to a clean
connection rather than talking to a wedged one.

**Rate limiting.** Both transports report the refusal in their own idiom, and neither reaches
an agent.

**The turn exceeds its budget.** Waits are computed from the remaining budget and refused
when there is not enough left, rather than being started and abandoned.

**Delivery fails at the endpoint.** Send failures are absorbed and logged, and the sweeper
survives them. Endpoint failure is reported by the client rather than discovered here — so
"delivered" means "handed over", not "heard".

## Extension points

**A new voice agent** must declare the voice capability and have a well-formed role and
display name; being enabled additionally puts it in the catalog (see INV-VOICE-002 for why
those differ). Note the blast radius: because the catalog is complete-or-fail, one malformed
agent takes the catalog down for every caller.

**A new capability** means changing the exact set on both sides. Registration enforces
equality against a fixed set, so a one-sided change refuses every route rather than
degrading.

**A new delivery modality** touches several places that nothing centrally ties together —
the offer sanitiser, the modality selection, the deliverable set, and the spoken phrasing. No
single check will tell you one was missed.

**Anything that reasons about where an answer can go** should read the per-utterance offer,
not the route's capabilities. Those answer different questions.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/channels/voice/channel.py::VoiceChannel`
- `casa/rootfs/opt/casa/channels/voice/channel.py::sanitize_delivery_offer`
- `casa/rootfs/opt/casa/channels/voice/catalog.py::build_voice_agent_catalog`
- `casa/rootfs/opt/casa/channels/voice/routes.py::VoiceRouteRegistry`
- `casa/rootfs/opt/casa/channels/voice/delivery.py::VoiceDeliveryCoordinator`
- `casa/rootfs/opt/casa/channels/voice/session.py::VoiceSessionPool`
- `casa/rootfs/opt/casa/channel_authz.py::agent_allowed_on`

**Tests**
- `tests/test_voice_auth_failclosed.py`
- `tests/test_voice_malformed_input.py`
- `tests/test_voice_agent_catalog.py`
- `tests/test_voice_channel_sse.py`
- `tests/test_voice_channel_ws.py`
- `tests/test_voice_context_sanitize.py`

**Related**
- [`architecture/overview.md`](../architecture/overview.md)
- [`architecture/http-surface.md`](../architecture/http-surface.md)
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
- [`architecture/personality.md`](../architecture/personality.md)
<!-- END SOURCEMAP -->
