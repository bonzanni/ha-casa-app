# Changelog

## [0.239.0] - 2026-08-26

### Fixed

- Approving an install after its configurator engagement has ended no longer
  shows a false "installing" message — the DM now says the install was not
  started automatically and points at re-running it.

## [0.238.0] - 2026-08-26

### Fixed

- Casa no longer re-announces plugin problems you have already been told
  about. A restart that could only see part of the picture used to wipe the
  record of what had already been announced, so the very next report arrived
  as though it were news. That record is now cleared only by a full health
  regeneration, which is the one pass that can actually observe a problem
  resolving.
- A plugin you removed no longer leaves a standing setup failure behind in the
  health report, and a plugin reinstalled from a different download is no
  longer blamed for the previous one's failure.
- When Casa cannot read its own health data it now says so plainly, instead of
  answering that nothing is wrong. The plugin status answer marks the parts it
  could not read rather than presenting an unreadable file as an all-clear.
- A plugin routing refresh that fails now reports the failure too. Previously
  it left plugin routing looking simply empty, which reads as healthy; the
  health report and the status answer now both disclose that the routing
  picture is unavailable.

## [0.237.0] - 2026-08-26

### Fixed

- A specialist run the CLI aborted, or that ended in a fault, is now reported
  to the caller as a failure instead of an empty success. Previously such a
  run answered `ok` with whatever text had accumulated, and the job it came
  from was settled as if it had finished; now the caller is told it failed,
  and the durable record carries the same reason the caller was given.
- A partial answer from a run that did not finish is never banked to shared
  memory. That includes an answer the model ran out of room to complete: the
  caller still gets it, but it is not written to the bank where a later turn
  would read it back as a settled fact.
- The task you asked a specialist to do is now remembered even when the
  specialist answers with nothing. Previously an empty answer discarded the
  request as well, so the exchange left no trace at all.

### Notes

- Terminal evidence the CLI reports in a shape Casa does not recognise is
  read as a failure rather than as a completed run, so a malformed report can
  no longer pass as success.


## [0.236.0] - 2026-08-26

### Fixed

- A streamed reply in an engagement topic that Telegram refuses outright is
  now reported instead of being silently lost. If the refused reply was the
  engagement's launch, the launch-death report names it; if it was a
  follow-up, the topic gets a notice saying that reply did not arrive.
  Previously such a refusal went to the log and nowhere else, leaving an
  engagement nobody could see and nobody was told about.

### Notes

- The report is only made when the refusal is established - every attempted
  piece of output was positively refused, or provably never left the app.
  When Telegram's answer is ambiguous and the text may well be on screen,
  no claim is made either way.


## [0.235.0] - 2026-08-26

### Fixed

- A message you send while an engagement is dying is now counted in that
  topic's closing post instead of vanishing silently. Whichever way the
  engagement ends - an error, a cancellation, a timeout, or your own
  `/complete` - the closing post now reports that up to N inbound
  message(s) had no turn start recorded. Previously only the agent's own
  completion looked for such a message, and only to decide whether to
  refuse; every other ending committed past it without a word.
- A `/cancel` or `/complete` that is still being processed is no longer
  miscounted as a lost message. A recognised command is marked as one the
  moment it is taken in, so it never shows up in the closing post's count -
  including when a different ending wins the race.

### Notes

- The disclosed figure is an upper bound, which is why the copy says "up
  to": a message taken in can already be safely queued by the time the
  engagement ends, and the two are not distinguishable yet.


## [0.234.0] - 2026-08-25

### Changed

- A refused persona selection now tells the operator everything the failure
  point knows, for every selection involved. The refusal record reports the
  active and the staged selection each in its own right - absent, unreadable
  (with the file and what failed), or read (with the file, the mode, the
  persona ref, and, where a pin genuinely applies, the pinned checksum and the
  exact path under the config root whose bytes must reproduce it) - together
  with the offline recovery facts: the procedure requires the app stopped, and
  its steps are in the documentation. Previously the record could name only
  one selection, and the docs had to warn that the log may not have named the
  one that fails next.
- A persona tuple file that cannot be read at all - empty, malformed,
  pathologically nested, a symlink, a directory, or any other non-regular
  file - now draws the same single diagnostic record before the failure
  propagates exactly as it did before — for regular files. The special-file
  classes are deliberate behavior changes: a symlink sitting where a tuple
  belongs is refused rather than followed, a directory is refused with a
  typed message rather than its native error, and a FIFO is refused in
  bounded time instead of blocking startup forever on a read that can never
  return.
- What the reconciliation writes back on a refusal - the rollback copy and
  the discarded-candidate archive - is now derived from the exact bytes the
  reconciliation observed and judged, never from re-reading files that may
  have changed underneath it.

### Notes

- `docs/architecture/personality.md` crosses the corpus size ceiling with
  this change - the documented first-crossing case: it lands with a notice,
  and the next change touching that document must split it first. The topical
  cut point is already established on the record.


## [0.233.0] - 2026-08-25

### Changed

- The documentation-corpus size ceiling is now a tripwire judged against a pull
  request's merge-base instead of a flat prohibition on the tree. The change
  that first pushes a document over its ceiling lands, with a visible notice;
  after that, any change touching the over-ceiling document - its file or its
  manifest row - fails until the document is split (or otherwise ends back
  under the ceiling), and a new document may not be born over the ceiling,
  which also refuses an over-ceiling rename or copy, since a renamed or copied
  file is a new path. Local runs and push events report over-ceiling documents
  without failing; the pull-request check is the enforcing caller. Anything
  unreadable at the base - the commit, a blob, the manifest - is a refusal,
  never guessed around.

### Removed

- The 20 KB "warn band" on documents and the fixed 20 KB test pins that held
  eight previously split documents below everyone else's limit. The uniform
  25 KB ceiling (40 KB for generated indexes) is the only size rule, so those
  eight documents now get the same headroom as every other document.

## [0.232.0] - 2026-08-24

### Fixed

- A follow-up message to a running engagement is no longer answered with
  silence when the turn dies part-way through its work. Casa already reported a
  turn that died before producing anything at all, but a turn cut off in the
  middle of using tools looked from the outside exactly like one that finished:
  the message was consumed and nothing came back - no reply and no explanation.
  The topic now gets one notice saying the turn did not finish, that whatever it
  already posted may be partial, and that work it started may already have taken
  effect. Casa stays quiet only where the engagement's own ending has already
  been posted into that topic; a record that merely reads as finished is not
  taken as proof that anything was said.
- An engagement's topic is no longer marked completed, cancelled or failed when
  the message explaining that outcome never reached it. Until now a completion
  summary that failed to send was logged and the outcome tick was painted
  anyway, so a topic could show a green completed tick over nothing. The mark is
  now applied only once the send is acknowledged; when it is not, Casa posts a
  short note in its place - the engagement is recorded with that outcome, its
  summary could not be confirmed as posted here, and the topic is deliberately
  left unmarked so the failure stays visible. The topic is closed either way.
  None of this re-opens a finished engagement: the work did finish, and only the
  telling failed.

## [0.231.0] - 2026-08-24

### Fixed

- An executors or plugin-environment reload no longer deadlocks against a
  plugin install, upgrade or removal. Starting one of those reloads while a
  plugin was being installed, upgraded or removed could leave the two waiting
  on each other with no timeout on either side: every later reload and every
  later plugin change then hung as well, and only restarting Casa cleared it.
  Reloads and plugin mutations now stay available. The one visible difference
  is that an executors or plugin-environment reload started while a plugin
  change is in flight now waits for that change to finish before it begins,
  instead of stopping part-way through it.

## [0.230.0] - 2026-08-22

### Fixed

- A specialist delegation that is aborted before the agent finishes its turn
  no longer files the half-written answer into Casa's shared memory as if it
  were the finished reply. Until now that fragment was banked as an ordinary
  completed exchange, so a later recall could present a partial answer as a
  whole one. It is now left out whenever the run ends without the turn
  completing — when the run hits a turn, budget or retry limit, fails
  part-way, or reports no result at all. The request that started the
  delegation is still recorded either way, so the delegation is not lost from
  memory. This changes what is written from now on; documents already in the
  bank are untouched. A run that does complete, but whose reply the model cut
  short at its own output limit, is a separate case and is not covered here.

## [0.229.0] - 2026-08-22

### Fixed

- When the files of a persona a resident is pinned to change, go missing or
  stop being readable, Casa now says which persona it was, the checksum the
  binding pinned and the checksum it actually found. Until now that failure
  either said nothing at all in the log, or reported whatever the persona
  files happened to trip over first — "Core body must contain 300-500
  characters" — naming neither the persona nor the pin, and in one case
  telling you to run persona tools that only work while Casa is running,
  which by then it is not. The refusal now records exactly what it knows: the
  resident, the persona reference, the pinned and found checksums, whether an
  active and a staged selection were present, and the underlying reason. The
  recovery procedure moved into `docs/architecture/personality.md`, where it
  can state the conditions each step actually works under. Startup still
  fails when a resident's persona cannot load — that has not changed; what
  changed is that the log now tells you why.
- A persona pack sitting in another persona's directory is no longer reported
  as that other persona. It is refused, naming the persona the pack really
  declares, so its checksum can no longer be presented as "the checksum
  found" for a persona it is not.

## [0.228.0] - 2026-08-20

### Fixed

- A delegation still running when Casa is stopped is now reported after the
  restart, instead of being silently recorded as cancelled. Stopping or
  restarting Casa used to write the running job down as cancelled on its way
  out, which left it indistinguishable from a cancellation you had asked for:
  the next boot had nothing to pick up, and the work simply vanished without
  anyone being told. Casa now leaves such a job as it stands at shutdown, so
  the restart finds it and takes it into its recovery. Cancellations you
  actually asked for are unchanged and still stay silent, and so is
  everything that finishes normally before the stop completes.

## [0.227.0] - 2026-08-20

### Fixed

- An engagement whose first turn dies without reporting now says so in its
  topic and ends as failed, instead of sitting silently active for a day.
  A first turn that was cut off part-way through its work — the agent had
  started, and something replaced it mid-task — used to look exactly like a
  healthy start: the agent that delegated the job said "started, I'll
  report back", the engagement stayed active with nothing ever posted, and
  it was only suspended as idle after a day and cleaned up a week later.
  Casa now checks that a first turn actually reached its end, posts one
  failure notice into the topic while it is still open (including anything
  you sent while the turn was dying), and ends the engagement as failed.
- A delegation cancelled while it was starting up now posts that same
  notice. It used to mark the topic failed and close it without ever
  having said a word, and it waited on the agent's shutdown — which can
  take a while for a specialist — before doing anything you could see.

### Changed

- **You will see more engagement failures reported than before.** These
  deaths were already happening; they were simply invisible. Nothing got
  worse — every new report is a real interruption that previously went
  unnoticed, and collecting them is how the underlying cause gets fixed.
- The configurator's own guidance no longer tells it that reloading Casa
  in the middle of a task is safe. It claimed a restart would be held back
  until after the agent had reported, which was never true of the reload
  tool it was reading about; it now advises reporting promptly instead of
  treating survival as guaranteed.

## [0.226.0] - 2026-08-19

### Fixed

- A scheduled question now waits through ordinary conversation. Any
  message you typed into the DM used to retire the question one of an
  agent's own schedules had left waiting — an ending v0.206.0 never
  promised, and a pointless one, because your typed text belongs to your
  own session and can never reach the scheduled run that asked. Only a
  tap, `/new`, an approval request, a new question that has actually
  reached your screen, or the question's own expiry retires it now.
- A new question raised while a scheduled one is waiting no longer
  destroys the scheduled one before it has arrived. The hand-over now
  happens once the new question is on your screen and still waiting for
  an answer, so a question that fails to post leaves the earlier one live
  and tappable instead of clearing the screen of both.

## [0.225.0] - 2026-08-19

### Fixed

- An ask that hit repeated Claude API errors now reports the failure
  instead of ending in silence.
- A conversation whose resumes keep failing now recovers on its own after
  two visible failures, instead of staying stuck until `/new`.

## [0.224.0] - 2026-08-19

### Fixed

- An approval tapped while its in-Casa engagement is finishing now
  installs. If a consent turn (or any operator turn) is still queued when
  the engagement tries to complete, the completion is refused and retried
  instead of acknowledged: the model's turn ends, the queued turn is
  delivered, and the engagement completes after the install has actually
  happened. Previously the queued approval was destroyed silently while
  the DM keyboard already said "installing".
- A turn that dies together with its engagement is now disclosed in the
  engagement's topic instead of being dropped as an internal log line, and
  a delivery failure on a live engagement always produces a visible
  notice.
- A `/cancel` or `/complete` whose result could not be persisted now
  reports the failure and asks to retry, instead of being silently
  ignored.

## [0.223.0] - 2026-08-19

### Fixed

- An engagement running inside Casa (`in_casa`) that finished by calling
  `emit_completion` could silently lose its completion: on the container's
  Python 3.11, the teardown of the engagement's own client cancelled the
  final delivery, dropping the engager's notification, the memory retains,
  a deferred Supervisor restart and the finalized engagement log. That
  final stretch now runs in a task Casa itself owns, so it survives the
  client teardown and the completion is delivered reliably. One narrow
  limitation remains and is now documented: the executor's own tool
  acknowledgement may still be lost in that teardown.

## [0.222.0] - 2026-08-19

### Fixed

- Two libraries Casa uses directly (`anyio` and `pydantic`) are now declared
  in the app's own dependency manifest. Until now they were installed only
  because other declared dependencies happened to pull them in, so a future
  update of those dependencies could have removed them and broken fresh
  installs at boot. A new repository test fails whenever any directly
  imported library is left undeclared, so this class of drift cannot return.
  No behaviour changes — the installed versions are the same ones the image
  already contained.

## [0.221.0] - 2026-08-19

### Security

- Updated the bundled `aiohttp` HTTP library from 3.14.1 to 3.14.3. This
  clears three published advisories, the most serious of which
  (GHSA-cq5v-8q36-5273, rated high) is an out-of-bounds heap read that a
  server could trigger by sending Casa a malformed chunked response. The
  other two harden WebSocket handling (request smuggling via the upgrade
  path, and acceptance of compressed frames that were never negotiated).
  No configuration changes and no behaviour changes — update and restart.

## [0.220.0] - 2026-08-18

### Added

- Casa now records which per-trigger webhook secrets it minted itself. The
  record is bound to the secret's value rather than to its name, so it is
  evidence about the bytes Casa read, not a history of the slot. The reload
  report shows, for each slot it can read, whether Casa can prove it minted the
  value there; other slots report their file condition instead. Secrets you
  supply yourself (`secret_owner: provider`) are untouched: nothing is written
  for them, and no existing secret is ever modified, moved or re-minted.

  This is groundwork, and it deliberately does **not** change the behaviour
  reported in #620: deleting a webhook trigger still leaves its secret on disk,
  and a trigger recreated under the same name still inherits it. Retiring a
  secret safely first requires being able to tell a Casa-minted token from a
  credential you provided — which Casa cannot regenerate and has no way to
  import — and the receipt is what makes that distinction possible.

## [0.219.0] - 2026-08-17

### Fixed

- Inspecting a persona now lists the sections that persona actually declares.
  Previously every persona was reported as having exactly `Core` and
  `Negative space` — the minimum the loader requires — so a pack with extra
  or nested headings was described as not having them.

## [0.218.0] - 2026-08-16

### Fixed

- **A persona's own words are no longer repeated to the model** (#611). Every
  section nested inside another one was being sent twice on the text surface —
  three times at one more level of nesting — so all three shipped personas
  said their "Negative space" paragraph twice in every text turn. The duplicate
  also counted against the persona's size limit, so an author near the ceiling
  could be refused for prose they wrote once, with nothing in the message
  pointing at the real cause. Each section's authored body now reaches the text
  projection exactly once.

- **"Core" now means the one section the persona loader validated.** A section
  merely *named* Core could reach the voice surface as though it were the
  persona's core, carrying prose that never passed the loader's own length
  check. Voice now carries the validated core and nothing else, which is what
  the documentation already said. A Core-named section written OUTSIDE that
  section is ordinary prose: it reaches text, and it does not reach voice. One
  written INSIDE it is part of the core and still reaches voice — and now
  exactly once, where it used to be sent twice.

### Changed

- The text projection now places the core first and the remaining top-level
  sections after it, in document order. Every authored body is still there —
  what changes besides the order is that the repeated copies described above
  are gone — and the reordering itself affects only personas with more than one
  top-level section. The three shipped personas keep byte-identical voice
  output; their text output is smaller by exactly the duplication removed.

## [0.217.0] - 2026-08-16

### Fixed

- **A webhook trigger's secret now exists the moment you create it** (#609).
  Asking for a webhook in chat could produce a trigger that was committed,
  reloaded and reported live while its secret did not exist yet — and the only
  thing that would create it was a call to the webhook, which could not succeed
  without it. Casa now generates the secret when the trigger is registered, so
  the setup instructions are true when you are given them. Verification never
  creates one, so an unprovisioned webhook is refused rather than quietly
  provisioning itself from whatever call arrives first.

- **A reload now tells you the state of each trigger's secret.** Reloading
  triggers reports, per trigger, whether a secret is present, still waiting for
  one you supply, or unreadable — and says which of those Casa can fix and which
  it cannot. It reports what a request would actually do rather than what the
  configuration file says, so a route running on settings the file no longer
  matches is named instead of reading healthy. The report is included when a
  reload fails too, since that is when you most want it.

- **An interrupted write can no longer leave a permanently broken secret**
  (#622). If the disk filled at exactly the wrong moment, Casa could store a
  half-written secret, report nothing wrong, and never be able to repair it —
  the webhook would refuse every call from then on and the name could never be
  reused. A partial write is now refused outright, leaving the name free.

- **Editing a webhook no longer takes over a secret you provided yourself.**
  Changing one field of a webhook trigger — its clearance, say — could silently
  hand ownership of its secret to Casa, which would then replace your value with
  its own on the next reload. Fields you do not mention are left alone.

### Known limitations

- Deleting a webhook trigger still leaves its secret behind, so a new trigger
  created later under the same name inherits it (#620). This was already true
  and is unchanged here.
- There is still no way to read a generated secret from inside Casa; it has to
  be read from the host filesystem (#621).

## [0.216.0] - 2026-08-16

### Fixed

- **A half-spoken answer that then fails is taken back out loud** (#594). Casa
  speaks a reply as it is being written, so when a turn fails partway through
  you have already heard the first half — and until now the error line simply
  followed it, leaving you holding an answer Casa never stood behind with
  nothing to say which half to believe. The error is now spoken as a correction
  of what was just said, in one breath. It is careful about when: only if
  something really was delivered to you, never for the "still working" notice,
  and never when the error itself has no words in it — being told to disregard
  something, with no reason given, would be worse than the contradiction. Each
  agent can word the retraction itself, or switch it off, alongside its other
  spoken error lines.

- **A delegated job no longer starts holding two versions of its own history**
  (#583). A job allowed to remember previous work is handed a digest of lessons
  from earlier runs. Jobs that run in their own workspace were handed that
  digest twice, looked up separately moments apart — and because the lookup is
  relevance-ranked and size-bounded, the two copies could legitimately differ.
  Such a job could therefore begin by reading two accounts of what happened
  last time, with the caveat about how much weight to give them repeated twice
  over. It is now looked up once per run.

## [0.215.0] - 2026-08-16

### Fixed

- **Applying a persona to a resident can no longer stop Casa starting** (#607).
  Asking for a persona swap wrote the new binding straight into effect without
  first checking it could be built. A persona that was otherwise perfectly valid
  but slightly too long for the voice channel was accepted, reported back as
  "staged, takes effect at the next restart" — and then that restart failed, with
  every resident down and no way to fix it from inside Casa. The persona is now
  compiled before anything is written: one that cannot be built is refused on the
  spot, with your existing setup untouched and nothing to undo. One that can be
  built is genuinely staged, so the restart really is what puts it live.

- **Asking for a shorter reply no longer looks like it worked when it did not**
  (#610). A request such as "keep your confirmations to one sentence" was written
  into `response_shape.yaml`, committed, and reported as live for the next reply.
  For a resident it reached nothing at all — that file has not been part of a
  resident's instructions since personas were introduced, and the next reply was
  only shorter by luck. The edit is now refused rather than quietly accepted, and
  you are pointed at what actually decides how a resident writes: its persona.

- **A webhook trigger no longer accepts an instruction it will ignore** (#608). If
  you described what should happen when a webhook fires, that description was
  saved onto the trigger and committed — and then discarded on every firing, since
  a webhook turn is built from the incoming request, not from anything stored on
  the trigger. Casa now says so when the trigger is created, while you are still
  in the conversation about it, instead of accepting the instruction and silently
  dropping it. Existing triggers keep working unchanged.

### Changed

- The configurator's guidance on response shape, webhook prompts and persona
  application now matches what Casa actually does; several of those notes had
  been describing changes as taking effect immediately when they never did.

## [0.214.0] - 2026-08-16

### Fixed

- **Approving a plugin's event subscription no longer leaves it looking like it
  is still waiting for you** (#582). After you tapped Approve, the subscription
  started working immediately — but the plugin health report kept saying the
  plugin was waiting for your approval, and went on saying it until some
  unrelated plugin change happened by. Asking your assistant what was wrong with
  that plugin got you the stale answer, along with an offer to re-send the
  approval request; taking the offer found nothing to re-send, so the two
  answers contradicted each other. Approving now refreshes the report at once,
  and does so even when starting delivery fails, so what you are told matches
  what actually happened.

- **The same plugin warning no longer reaches you twice in one turn** (#559). A
  plugin change that raised its first blocking problem showed you the warning as
  a direct message, then again on top of the reply that followed it. The two
  messages now divide the work: the direct message names the problems, and the
  reply carries only what that message did not name — because it was behind the
  "and N more" tail, or because it never got through. Nothing is quietly
  dropped: a warning is announced by name exactly once, a failed message still
  produces the in-reply notice, and your assistant can list everything currently
  standing whenever you ask.

- **A plugin no longer starts up holding an unusable 1Password reference in
  place of a secret** (#580). If 1Password could not be reached while Casa
  reloaded plugin secrets — an expired token, a revoked service account, no
  network — Casa put the *reference* (`op://…`) into the plugin's environment
  instead of the secret. A plugin that treats one of its settings as optional
  then started with that reference as the value, quietly behaving as if it had
  been configured. Casa now leaves the setting empty in that case, exactly as it
  already did at start-up, so a plugin that needs the secret is held back with a
  clear reason and one that has a fallback uses its fallback. The reload also
  reports how many secrets it could not apply.

## [0.213.0] - 2026-08-16

### Fixed

- **An engagement's own tools stop the moment it ends** (#599). When an
  engagement finished — completed, cancelled, or failed — Casa recorded it as
  over straight away, but the program behind it kept running while Casa tidied
  up: writing the closing summary, updating the topic, closing it. For that
  whole stretch the engagement could no longer use any of Casa's own tools, and
  yet nothing stopped it editing files on disk. In the worst case it was still
  making changes after you had cancelled it and been told it had stopped.

  Ending an engagement now shuts its work down first, before any of the closing
  messages appear, and checks that nothing is left running rather than assuming
  it. If Casa restarts mid-way, the engagement remembers it still owes that
  shutdown and finishes the job on the next start. When Casa genuinely cannot
  confirm everything stopped, it says so in the log instead of reporting
  success — and it still delivers your summary and notification either way, so
  a stubborn engagement can never leave you with no word at all.

## [0.212.0] - 2026-08-15

### Fixed

- **A message sent to an engagement just as it ends is no longer lost in
  silence** (#591). An engagement could finish while a message you had just
  sent was already on its way to it — close enough to be out of the queue, not
  yet close enough for the agent to have started reading it. Two things went
  wrong in that moment: the agent was allowed to declare the work complete, and
  the closing summary, which lists the messages that were never picked up,
  did not mention it. Both are fixed. A completion now waits for a message
  already on its way, and every engagement that ends — completed, cancelled or
  failed — lists it.

  The wording of that list changed with it. It used to say the messages were
  never read; it now says no turn started for them before the engagement ended,
  which is what Casa can actually tell. In the narrow case where the agent had
  just begun reading, the old wording was not true.

- **A very large message is delivered whole, or not at all** (#592). Anything
  bigger than a pipeful used to be handed over in pieces, and an engagement
  cancelled between two of them left the agent holding half a message it could
  still act on. Casa now makes room for the whole message before writing the
  first byte of it.

- **An engagement stopped by Claude's safety system says so** (#595). When a
  turn ended in an API-level fault — a safety refusal, a rate limit, an
  overload — the engagement recorded a generic startup failure, so a refusal
  and a crash looked identical after the fact. The specific reason is now
  carried through to the engagement's record and to whoever started it, as it
  already was everywhere else since 0.210.0. A refusal mid-engagement still
  only fails that turn; the engagement stays open for you to try again.

## [0.211.0] - 2026-08-15

### Changed

- **`opus` and `sonnet` now mean Claude Opus 5 and Claude Sonnet 5** (#568).
  The two model choices behind `primary_agent_model` and the executors move up
  a generation. The option values are unchanged — `opus`, `sonnet` and `haiku`
  still mean what they meant — and so is the price per token.

  The practical difference is room. Both models carry a one-million-token
  context window where the previous pair carried two hundred thousand, so a
  conversation, a long document or a large piece of work can stay in view five
  times longer before anything has to be summarised away to make space.

  Two things to expect. Opus 5 thinks before answering by default, which the
  previous Opus did only when asked, so a turn can spend a little more on
  reasoning and take slightly longer. And Sonnet 5 counts tokens differently
  from Sonnet 4.6 — the same text comes to roughly a third more tokens — so if
  you have tuned a specialist's memory budget by hand, it now buys less text
  than the number suggests.

  The voice agents stay on Haiku deliberately: speed is what matters on that
  channel, voice turns are short, and a bigger window buys nothing there.

  **If you have installed a specialist, re-install or upgrade it after this
  update.** A specialist's identity is bound to the model it was installed
  against, so changing the model leaves that binding stale and the specialist
  will not activate until it is re-materialised. It fails loudly and on its
  own — nothing else stops working — and the message now says which part
  moved and what to do about it. This is not new to this release: changing
  `primary_agent_model` in the add-on options has always done the same thing.
  Tracked in #597.

## [0.210.0] - 2026-08-15

### Fixed

- **A declined or failed request was answered with Claude's internal error
  text** (#568). When the Claude API declines a request, or the call fails
  in certain ways, the Claude CLI does not report an error — it writes its
  own message and hands it back as if the assistant had said it. Casa passed
  that straight on, so a household could receive something like *"API Error:
  Claude's safeguards flagged this message… Try rephrasing the request in a
  new session or change your model. Request ID: req_…"* in the resident's
  own voice, complete with an internal request identifier and instructions
  meant for someone sitting at a terminal.

  Casa now recognises those messages for what they are. A declined request
  is answered plainly — *"That request was declined by Claude's safety
  system. Rephrasing it usually helps."* — and other API failures get their
  own short, honest line. Neither is retried when retrying cannot help, and
  a declined conversation is not resumed on the next message. On the voice
  channel each has its own spoken line, so a declined request is never met
  with silence.

  The same text was also being passed off as an answer in three other
  places: a specialist you delegated to now reports a failed task instead of
  handing back an empty or half-written one; an executor asking the engager
  a question is told the answer could not be produced instead of being told
  the engager remembers nothing; and a half-finished exchange is no longer
  written to memory as if it had completed.

## [0.209.0] - 2026-08-15

### Fixed

- **An engagement resumed after a restart lost its tools for that turn**
  (#588). When Casa restarts while a message you sent to an engagement is
  still waiting to be picked up, that message is redelivered to the
  engagement once it comes back. The engagement ran the turn — but with none
  of the tools it is entitled to. Every one of them was refused, and the
  refusal blamed a missing permission, when in fact the engagement held the
  permission perfectly well; what it had lost was its live status, which the
  restart cleared and the redelivery never restored. The turn still finished,
  so nothing looked broken from the outside: the engagement simply answered
  you without being able to read the conversation or use anything it needed.

  Handing a message to an engagement now restores its live status first, at
  the moment the message is passed across and before the engagement can see
  any of it. The same step declines to deliver a message to an engagement
  that has already finished or been cancelled.

- **A refusal said "not permitted" when it meant "not running"** (#587).
  Tool calls from an engagement that has ended were refused with the same
  message used for a tool the engagement is genuinely not allowed to use.
  The two now read differently, so a log line points at the real cause
  instead of sending an investigation after a permission that was never
  missing.

### Changed

- The restart-survival test now provisions a real engagement and has *it*
  make the calls across a restart, including one it is permitted to make and
  one made after a restart that delivers nothing (#586). Every call the test
  made before was anonymous, which is not how an engagement talks to Casa —
  so the failure above could not have been caught by it. While writing it,
  the helper that substitutes a stand-in CLI for tests was found to have been
  silently ineffective: the real CLI shadowed it on the path. It is now
  replaced properly, and the image build fails if it is not.

## [0.208.0] - 2026-08-15

### Fixed

- **A memory wipe could report success while leaving a conversation
  resumable** (#578). Wiping long-term memory is meant to end every stored
  conversation as well as delete the facts. It drops each conversation's
  pointer and, before doing so, waits for any reply that is still being
  written on that conversation. It only knew how to wait for replies handled
  by Casa's pool of warm assistant processes, which is most of them but not
  all: a reply triggered by a schedule, by a webhook, or by a fallback taken
  when the pool is unavailable runs on its own process the pool has never seen.
  Such a reply finished after the wipe had already reported completion and
  restored the pointer it had just removed, leaving that conversation
  resumable with all of its pre-wipe content still in place. A conversation
  starting for the very first time was less visible still, having no stored
  pointer for the wipe to find at all.

  A wipe now suspends new replies and waits for every one already in progress,
  whichever way it is being handled, before it removes anything. If a reply
  does not finish in time the wipe stops and reports that it deleted nothing,
  rather than reporting a success it did not deliver. Starting a fresh
  conversation with `/new` is covered by the same rule, and no longer retains a
  transcript into memory that a wipe running alongside it has just emptied.

- **A configuration reload could split one conversation across two assistant
  processes** (#579). Reloading Casa replaces its assistant while any reply
  still being written continues on the old one. The replacement had no way to
  know that, so a message arriving during the changeover could start a second
  process on the same conversation. Both then wrote to it, and turns could be
  lost. Conversations are now held to one writer at a time by something that
  outlives a reload, so a reply in progress is finished before the next one
  starts, whichever assistant handles it.

## [0.207.1] - 2026-08-15

### Fixed

- **The nightly hardening test run had been failing for a week, and nothing
  said so** (#585). Casa's heaviest automated checks run once a night rather
  than on every change, and one of them — the check that restarting Casa's core
  does not sever its tool connections — had failed every night since
  2026-08-09. Nothing surfaced it: that tier is skipped on ordinary builds, so
  every release in that period looked green.

  The failure was in the test, not in Casa. A security change in v0.166.0 made
  the internal tool bridge refuse a call that does not identify which
  engagement it belongs to, apart from the one call an ending engagement is
  allowed to retry anonymously; the test had been making an anonymous call of
  the refused kind, and is now refused exactly as intended. The test has been repaired to prove the same
  restart guarantee through a call the bridge is designed to accept, and it now
  also asserts the refusal itself — both before and after a restart — so the
  behaviour that broke it is checked rather than merely avoided.

  A failing scheduled run now files a tracking issue and comments on it each
  night it stays red, so this cannot go unnoticed again. No change to how Casa
  behaves.

## [0.207.0] - 2026-08-15

### Fixed

- **Casa could still tell you "there's no record of that" about something it
  simply can't read from where you're asking** (#581). v0.172.0 fixed this for
  searches that came back *empty*, and that turned out to be the wrong half of
  the problem. When a search comes back with plenty of memories — just nothing
  on the topic you asked about — the agent drew exactly the same false
  conclusion, because a memory above your surface's clearance is filtered out
  before Casa ever sees it, leaving no trace in the result.

  Observed on the voice channel, which reads household-shared facts but not
  private ones: asked where a private note was kept, the butler answered "No
  record of that. I don't have it stored in memory." Thirty-three other
  memories had come back in the same search.

  Everything a memory search hands an agent now says what it is — the view
  readable where you are asking, not an inventory of Casa's memory — so an
  agent that can't find your topic says it has nothing it can share on that
  here, rather than that Casa has never been told. That covers all four places
  memories reach an agent: the recall tool, the memories loaded automatically
  at the start of a conversation, the ones a specialist is given when it's
  asked to help, and the notes an executor gets from past work. The wording is
  identical everywhere and at every clearance, so it never becomes a hint that
  something was withheld from a particular answer.

  The butler's and assistant's own instructions carried a stale rule from
  before v0.172.0 that let them declare "Casa doesn't have that" on an empty
  result — the opposite of what the tool now tells them. Both now say to use
  what came back and answer directly, and to claim absence in neither case.

## [0.206.1] - 2026-08-15

### Fixed

- **A schedule edited during Casa's first seconds could leave its question on
  screen** (#573 follow-up). Pending questions are restored shortly after a
  restart, once Telegram is ready. If a schedule was reloaded — or a reminder
  cancelled — in the moments before that, the cancellation found nothing to
  cancel yet, and the question was then restored anyway: still tappable, on
  behalf of a schedule that no longer existed. Such a question is now closed
  cleanly instead, and the agent is told the trigger changed — and only the
  questions that cancellation actually covered: cancelling one reminder leaves
  the other questions that agent is waiting on alone. An approval request
  raised in the same window retires a pending scheduled question too.

## [0.206.0] - 2026-08-15

### Added

- **A scheduled agent can now ask you a question** (#573). The other half of
  what v0.205.0 started: a turn fired by one of an agent's own schedules can
  put a tappable question in your DM — the weekly invoice pass that sends the
  PDF can now also ask *Confirm / Wrong / Later* in the same turn, and act on
  your answer.

  A question like this outlives the turn that asked it, so it behaves
  differently from one you prompted yourself:

  - **It waits politely.** If you already have a question open — an agent's, or
    an approval request — the scheduled one is not asked at all rather than
    replacing yours on screen. The agent is told so and can try on its next
    run. An approval request raised while a scheduled question is waiting
    retires the scheduled one first: your attention goes to the thing you are
    doing.
  - **It survives a restart.** The question is recorded on disk before the
    buttons are posted, so if Casa restarts while you are deciding, the buttons
    still work afterwards. A question that expired while Casa was down is
    closed cleanly instead of pretending to be live, and one whose operator
    changed in the meantime is never re-offered.
  - **The agent always finds out what happened.** Answered, timed out,
    superseded, or cancelled because you edited the schedule — every ending is
    delivered back into the same session that asked, so the agent is never left
    waiting on an answer that will never come.
  - **Your tap stays your tap.** The answer is reported to the agent as the
    content of a scheduled turn, not as a message from you, so a machine-run
    session is never rewritten as one you authored.

  Editing, reloading or cancelling a schedule now closes any question it left
  open, and tells the agent why.

## [0.205.0] - 2026-08-15

### Added

- **A scheduled agent can now send you a file** (#485). Until now a turn fired
  by one of an agent's own schedules — a cron pass, an interval, a reminder —
  could only write text. If it produced a PDF, a photo or a report, it had to
  ask you to say something first, and send the file on the reply. It can now
  deliver the file in the same turn: a weekly invoice pass sends the invoice
  itself.

  This applies to schedules on the Telegram channel, and delivery goes to the
  configured operator chat. With no operator configured, scheduled turns stay
  text-only exactly as before. Turns fired by a plugin's webhook are
  deliberately not included — they carry outside content, and they keep no way
  to put a file in your chat.

  Asking you an interactive *question* from a scheduled turn is not part of
  this and is tracked separately (#573): a question outlives the turn that
  asked it, which needs machinery this release does not add.

## [0.204.0] - 2026-08-14

### Fixed

- **A question Casa has already settled no longer keeps its buttons** (#569).
  When a question in your direct chat was answered, expired, superseded or
  cancelled at shutdown, Casa rewrote the message to say so — "Answered:
  Confirm", "(this question has expired)" — but the tappable buttons stayed
  underneath it. Tapping one was always safe (Casa refuses a tap on a closed
  question), but the chat kept offering a control for something that was
  already over. The buttons now go with the text.

  This affected every button question Casa asks in a direct chat: the ones an
  agent asks you, the memory-wipe confirmation, protected-action approvals, and
  every install-consent prompt — plugin callbacks, plugin events, webhook
  triggers, personas and specialists. The equivalent bug in engagement topics
  was fixed in 0.79.0; the direct-chat path was missed at the time.

## [0.203.0] - 2026-08-14

### Added

- **Personas can now be removed, listed and cleaned up** (#543). Installing a
  persona has always been a chat request away; getting rid of one was not
  possible at all. Four new tools close that:
  - `persona_list` — what is installed, what each persona is bound to, and
    whether it can be removed. Corrupt installs are listed too, rather than
    quietly missing.
  - `persona_remove` — delete one installed persona and revoke its install
    approval.
  - `persona_prune` — remove every persona nothing is bound to any more.
    Because persona versions are immutable, each upgrade left the old version
    on disk forever; this is how that space comes back. It is never automatic:
    Casa does not delete something you approved without being asked.
  - `persona_ack_revoke` — withdraw a stored approval without touching any
    files, so installing that persona again asks you afresh. The sibling of
    the existing trigger, callback and event revoke tools.

  Removal **refuses while anything is still bound to the persona**, and says
  what is holding it. That is deliberate: a resident whose persona has been
  deleted cannot start, and you would only find out at the next restart. Free
  it first — reset or apply a different persona, and restart the resident —
  then remove.

### Fixed

- An install that was approved and then revoked while it was still running
  could publish the persona anyway, undoing the removal (#543). Approval is now
  re-checked at the moment of publication, and an approval tap that arrives
  after a revoke records nothing.
- Applying a persona that was removed in the same moment could leave an agent
  bound to a persona that no longer exists — an agent that then fails to start
  (#543). Both apply paths now re-check the persona right before they commit,
  and report a clear error instead.

## [0.202.0] - 2026-08-14

### Removed

- The **Alex persona is no longer bundled in the Casa image** (#544). Alex is
  the finance specialist's persona, and Casa ships with no specialist
  preconfigured — specialists are installed, and each one brings its own
  persona with it. The finance specialist already carried Alex itself, so a
  finance install still gets Alex, with exactly the same identity; nothing you
  can reach today changes. The image now ships persona packs for its three
  resident slots and nothing else: Ellen, Tina and Gary.

  Alex was never applicable to a resident in any case — each resident slot
  accepts only its own persona — so there is no binding that this can break.

### Changed

- Retired the internal one-off tool that built the finance specialist's
  release bundle out of the image tree. The finance specialist has lived in its
  own repository since the cutover, and with Alex gone the tool had no image
  content left to read.

## [0.201.0] - 2026-08-14

### Added

- Agents and plugins can now send you **text files and zip archives** over
  Telegram (#565, #482). Until now the only document Casa could attach was a
  PDF, so anything textual it produced — a CSV ledger, a Markdown report, a
  JSON export, a diagnostics dump — could only be pasted into the chat, which
  splits anything long across message after message, and the agent pays for
  every byte of it out of the same budget it needs for thinking. Now it arrives
  as a file you can open, forward or keep, and the contents never pass through
  the agent at all.

  Text files must be valid UTF-8 and carry a `.txt`, `.md`, `.csv`, `.log`,
  `.json`, `.yaml` or `.yml` name, and are capped at 5 MB — comfortably more
  than any report, and small enough that Casa never has a huge file in memory.
  Casa checks that the file really is text, not that it is well-formed: a
  broken `.json` still arrives, as the broken `.json` it is.

  Archives are capped at 20 MB and must be named `.zip`, which is what keeps
  this to archives: a `.docx` or a `.jar` is a zip file underneath and would
  otherwise qualify. Casa checks the archive's signature and that it is long
  enough to be one, not that every entry inside it is intact — a corrupt zip
  can still be delivered, and your unzip tool is what will tell you so.
  Multi-volume archives marked as split are refused, since one volume on its
  own is unusable.

## [0.200.0] - 2026-08-14

### Changed

- A fresh install no longer claims to have a finance specialist it does not
  have (#525). The assistant shipped with a finance helper already listed as
  one of its delegates, but no such helper is included in the image — you
  install one yourself, from its own repository. Nothing misbehaved, because
  Casa hides a helper that isn't there rather than offering it, but the
  configuration file that records who the assistant can hand work to listed
  something that had never existed on your box. It now lists only what ships,
  and an installed specialist gets wired in the same way every other one
  already is: when you install it, Casa adds it.

  If you installed a finance specialist on an earlier version, updating removes
  that stale entry — and with it the assistant's ability to hand finance work
  over, quietly. That happens whether or not you have edited the delegate list
  since: an untouched list is replaced wholesale, and an edited one is merged
  entry by entry, which drops the finance entry just the same. Only an edit to
  that entry itself keeps it. Ask the configurator to *"wire finance into the
  assistant's delegates"* and it will be restored. Installing the specialist
  fresh on this version needs no such step.

## [0.199.0] - 2026-08-14

### Fixed

- Cancelling a reminder no longer quietly weakens the protection around a
  trigger file you wrote environment references into (#512). If one of your
  triggers holds a value written as `"${SOMETHING}"` — in quotes, meaning "use
  this text exactly" — Casa refuses to rewrite that file, so nothing of yours
  is retyped behind your back. Delivering or cancelling a reminder is the one
  thing it must still do, and doing it used to drop those quotes: the value
  silently became something else the next time the file was read (a `true`
  became a yes/no flag rather than the word), and, worse, the refusal stopped
  applying to that file for good, so every later change went through unchecked
  — including the reconciliation that can drop a trigger you added yourself.
  The quotes now survive the rewrite, so the value keeps its meaning and the
  protection stays on. In the one case Casa cannot carry them across — quoting
  that sits on a duplicate key the file itself discards — it says so in the
  log instead of changing the file silently.

## [0.198.0] - 2026-08-14

### Fixed

- Building Casa no longer fails because a download server had a bad moment
  (#545). The image build fetches a few small programs from the internet, and
  a momentary network hiccup on one of them used to abandon the whole build —
  which happened twice in a row on one recent release, each time for a file
  that was perfectly available seconds later. Those downloads now wait and try
  again on their own before giving up. This changes nothing about the running
  app; it only makes producing it less likely to need a second run.

## [0.197.0] - 2026-08-14

### Added

- Your assistant can now tell you what is wrong with a plugin, and what went
  wrong when one was set up (#555). Ask "why isn't the fx-setup plugin
  working?" and you get an answer in the conversation: what is currently
  blocking each plugin, and for past setups, how many attempts were made and
  the error the last one reported. Until now nothing could read that — the
  detail was recorded and kept, but no agent could open it, so the only way to
  ask was to have Casa start a configurator session on your behalf. The new
  view is read-only and changes nothing; installing and changing plugins stays
  with the configurator as before.

### Fixed

- A plugin problem is described the same way whichever way Casa tells you
  about it. A half-finished update was announced as "an update did not finish"
  in a reply but as the generic "something needs attention" by direct message,
  because the two messages were written separately and had drifted apart. They
  now share one wording. Each still names as many problems as suits it — a
  short reply names two, a direct message names five — and you can ask your
  assistant for the full list.

- A damaged plugin-health file no longer costs you a reply. If that file was
  corrupted outside Casa, reading it could fail in the middle of answering you,
  and the whole reply was lost rather than just the warning. Casa now skips the
  damaged parts and still reports every problem it can still read.

## [0.196.0] - 2026-08-14

### Fixed

- A warning about a plugin problem is no longer lost when the reply carrying it
  never reached you (#556). Casa prepends a short notice to a reply when
  something needs your attention, then goes quiet about it for an hour so it
  does not nag. If Telegram dropped that reply — a reconnect at the wrong
  moment is enough — the notice was still counted as shown, so you got an hour
  of silence about a blocking problem, with no way to ask what it said. Casa
  now checks that the message actually went out before going quiet, and offers
  the notice again on your next message if it did not. A reply that reached you
  and then failed partway is still counted as shown, so nothing is repeated
  that you have already read.

### Added

- Casa tells you when a cleanup rewrote a reminders file that uses `${...}`
  placeholders (#513). Removing a delivered reminder rewrites the file, which
  can change what a placeholder entry resolves to. That risk was only ever
  mentioned in the logs, so the file could quietly change meaning. You now get
  a one-line message naming the file, once per file, so you can check the
  entries you wrote by hand.

## [0.195.0] - 2026-08-13

### Changed

- Casa now tells you what happened, not what it did internally (#549, #550).
  The assistant's brevity and plain-language rules were written in a place a
  persona-bound assistant never reads, so setup and configuration replies
  narrated raw internals — artifact hashes, variable names, tool ids, status
  flags — and buried the one thing you had to act on. The rules now reach the
  assistant, the response limits declared for it actually apply, and requests
  are phrased as the thing you need to provide rather than the setting it
  needs to fill. Progress during a multi-step job is a short beat per step,
  with detail available on request. Engagement conversations are unchanged and
  stay technical.
- Plugin-health messages read like something you can act on (#551). The
  degraded notice and the operator DM no longer speak in internal reason codes
  or claim "an operator has been notified" to the operator reading them, and
  the DM no longer points at a file that nothing you have can open. An
  unchanged notice is no longer repeated every time a plugin change restarts
  the agent — a single setup could previously show the identical line three or
  four times — while any change to what it says appears immediately, and a
  resolved problem that returns is announced again.

### Fixed

- A failed plugin setup now says what went wrong (#554). The explanation was
  being produced and then dropped one step before it reached you, so the
  report and the notice named only an internal code. The name of a setting a
  plugin is still waiting for is carried through the same way.

## [0.194.0] - 2026-08-13

### Added

- A supported way to wipe long-term memory (#411). One operation deletes
  the memory bank, drops any pending durable retry records, and forgets
  every conversation pointer without saving it first — previously a
  hand-cleared bank could be silently re-populated by a conversation
  retiring, an engagement finishing, or a queued retry replaying. Two
  explicit doors: ask the assistant (`wipe_memory` posts an Approve/Cancel
  keyboard to the configured operator's DM and executes only on the
  operator's own tap, then reports exactly what it removed), or run
  `casactl memory-wipe --yes` from the add-on terminal. At most one wipe
  runs at a time, a wipe in flight is finished before shutdown proceeds,
  and a memory write already past the point of no return is discarded
  rather than allowed to restore deleted content afterwards.

### Fixed

- A message or button tap racing `/new` can no longer land in the
  conversation being reset (#290). While a reset (or wipe) is retiring a
  session, any concurrent turn on the same chat starts the fresh
  conversation instead of resuming the dying one, and the retiring
  session can no longer re-register itself mid-reset. A reset arriving
  while an earlier turn is still finishing now also retires the session
  that turn publishes, instead of leaving it resumable.
- Executor "lessons learned" can no longer steer against current doctrine
  (#215). Prior-engagement summaries are stamped at launch with a digest
  of the exact prompt and doctrine the engagement ran under, and the
  lessons block injected into a future launch keeps only summaries from
  its own doctrine epoch — anything older, unstamped, or from another
  executor type is dropped, and the block now states that doctrine
  prevails where they disagree.

## [0.193.0] - 2026-08-13

### Security

- Specialist config digests are now provably secret-free (#372). Before
  v0.137.0, a specialist's persisted `config_digest` could be computed over
  configuration that still contained a plaintext secret; the v0.137.0
  cleanup removed the plaintext but kept those digests, leaving an offline
  brute-force oracle for low-entropy secrets in the tuple files and in
  everything that captured them. Now the digest is derived from the
  persisted (secret-free) snapshot at three enforced layers — construction,
  the atomic write primitive, and the loader — and the first boot on this
  version removes every retained pre-guard digest from disk: affected
  tuple files are tombstoned (the specialist surfaces as an error-state
  install; uninstall and reinstall it), diagnostic/crash residue and stale
  quarantined bundle journals are deleted, and bundle-journal captures are
  sanitized both when written and when restored. Rolling back to a
  pre-guard generation is refused with a typed error instead of restoring
  it. Config-git history from before v0.137.0 may still contain pre-guard
  digests or plaintext — rotate any secret that predates it.

### Fixed

- A specialist upgrade that reclassifies a plain config key as secret no
  longer leaves the old plaintext (or a digest over it) in the retained
  rollback generation or in crash-recovery journals.
- A damaged or stale pending upgrade no longer takes the specialist's
  healthy running generation out of the fleet at boot, and no longer pins
  its consent receipt and staging trees forever.
- A crash-orphaned bundle-journal temporary file no longer causes boot
  reconciliation to quarantine every installed specialist.

## [0.192.0] - 2026-08-13

### Security

- Specialist installs: a third-party specialist bundle can no longer grant
  itself privileged casa-framework tools. Its role may declare tools only
  from a consumer-safe allowlist — a violating bundle is refused before the
  consent prompt, an already-installed one is stripped of forbidden grants
  at load, and an already-running specialist engagement is refused at tool
  dispatch on both transports. The install consent message now shows the
  role's casa-framework tool grants (`Casa tools:` line), so the approving
  operator sees the powers a specialist arrives with. (#541)
- Agent-created engagements are now capped: at most 3 engagements spawned
  from agent context (delegated turns, engagement turns, scheduled/webhook/
  synthesized turns) may be live at once — live operator requests are
  exempt and unlimited. A refused spawn performs no Telegram traffic, and
  the cap survives restarts. Interactively-engaged specialists can no
  longer delegate onward (the delegation depth cap now covers them). (#283)
- `send_media` in specialist context now carries a fixed per-context send
  budget, so a specialist cannot flood the operator or exhaust Telegram
  quota; residents and executors are unaffected.
- Workspace tools (`peek`/`list`/`delete_engagement_workspace`): an
  engagement-bound caller can now only target its own workspace, closing a
  confused-deputy path that previously relied on grant configuration
  staying correct. (#481)

### Changed

- Docs corpus: `architecture/engagements.md` split (delegation and the new
  agent-spawn boundary now live in `architecture/delegation.md`);
  `architecture/mcp-and-tools.md` split (hook resolution and the
  containment floor now live in `architecture/hook-resolution.md`). (#475)

## [0.191.0] - 2026-08-12

### Fixed

- Turn dispatch: a stale stored session hit at the exact moment the warm
  client pool is unavailable no longer fails the turn outright — the
  per-turn fallback now runs the same clear-and-retry-fresh recovery as the
  normal path, so the turn completes and the stale session pointer is
  cleaned up. (#537)
- Configurator: the 1Password vault tools now fall back to the configured
  `onepassword_default_vault` when no vault is named, instead of failing
  and guessing vault names against the account. (#535)

## [0.190.0] - 2026-08-12

### Fixed

- Plugin events: the "delivery went unanswered" operator notice is no longer
  lost forever when it fires before the Telegram channel has started (e.g. a
  delivery falling due during boot). The notice now retries until a send is
  confirmed, the removal notes gained the same guarantee, and a notice that
  sent but failed to record stops re-sending duplicates. Un-noted removal
  records are no longer age-pruned — an owed notice survives an outage of any
  length. (#532)
- Plugin events: headless event wakes no longer narrate "Event processed."
  style messages into the operator chat — wake turns are buffered and close
  silently unless the plugin's processing produced something the operator
  needs to see, matching reminder delivery. (#534)
- Plugin health: a plugin held back for unwired credentials now reports
  `env_unresolved` with the missing variable NAMES in the health report, the
  operator DM, and the first-contact notice — instead of a bare `not_ready`.
  Existing affected installs will receive one fresh (now informative) health
  DM after upgrading. (#533)

## [0.189.0] - 2026-08-12

### Fixed

- Engagement topic relay: a crash landing between a turn's closing narration
  edit and its checkpoint no longer reposts the throttled closing suffix as a
  duplicate message after restart — the relay records the landed edit's
  high-water and replays through it; a crash *before* the edit still delivers
  the suffix (at-least-once, unchanged). (#523)
- Session continuity: a transient resume failure racing a concurrent turn
  that had successfully resumed the *same* session could clear that live
  session's registration, losing conversation continuity and its save-time
  retention. Conditional session-registry mutations now also check a
  per-registration generation, so a same-id re-registration survives. (#526)
- Engagement topic state: a cancellation between the topic-title wire edit
  and the state-emoji persistence could permanently strand the topic on the
  old visual state. The persisted emoji is now explicitly uncertain across
  the wire call and settled after it, so an interrupted repaint self-heals on
  the next state change; an identical-title re-edit counts as success, and
  launch-time state initialization can no longer overwrite a terminal or
  in-flight state. (#529)
- Test suite: two async tests that failed only under parallel load now pin
  their preconditions deterministically instead of polling. (#418)

## [0.188.1] - 2026-08-12

### Fixed

- `casactl persona render` support endpoint: the `persona` field is optional
  again — omitting it renders the persona actually bound to the role, as
  before v0.187.0. The v0.187.0 ref-vs-binding check (a supplied ref must
  name the bound persona) still applies whenever a ref is given, and an
  explicit `null` or empty ref is refused. v0.187.0 had made the field
  required, which broke ref-less admin callers and left CI red on `main`.

## [0.188.0] - 2026-08-12

### Fixed

- Messages sent into an engagement's Telegram topic now respect the
  `TELEGRAM_RATE_PER_MIN` inbound rate limit (per topic, with a one-shot
  "slow down" notice). Commands such as `/cancel` are exempt so a runaway
  engagement can always be stopped. (#324)
- The engagement workspace tree listing (`peek_engagement_workspace` with no
  path) no longer follows symlinks: a link pointing outside the workspace
  used to leak outside file and directory names. Symlinks are now reported
  as their own entry type and never expanded. (#324)
- When a spool write fails during a redirect's eviction, the rollback now
  restores exactly the envelope that was evicted — previously it could
  un-mark an unrelated queued notice and leave a spurious eviction notice
  for a message that was never evicted. (#324)
- A voice specialist job whose task, context and result are near their size
  limits can now be continued: the internal continuation envelope is no
  longer re-checked against the caller-facing context limit (its stored
  components are bounded individually instead, with oversized prior results
  truncated). (#324)
- `/invoke` now honors a caller-supplied `context.cid` for trace
  continuity, as documented — it was previously always overwritten with the
  server-generated request id. (#324)
- The engagement topic's state emoji now stays 🟡 while any permission
  keyboard is awaiting a verdict: with two concurrent requests, the first
  verdict no longer flips the topic back to 🟢 early, a cancellation during
  the state edit no longer strands the topic on 🟡, and concurrent state
  edits can no longer land out of order. (#324)
- The commit-size guard now refuses a write at exactly the configured
  `max_files` uncommitted-files limit, matching its documented contract
  (the off-by-one allowed one file past the limit). (#324)
- The pre-commit config validation gate now enforces the full fixed
  resident set (assistant, butler, concierge), matching boot: a commit
  deleting a non-assistant resident used to pass the gate and then fail the
  next warm reload. (#324)

### Fixed

- The `casactl explain` sensitive-disclosure gate now accepts only real JSON
  booleans: a request carrying `"confirmed": "false"` (or any other
  non-boolean value) is refused instead of being treated as confirmation. (#356)
- `casactl persona render <ref>` now verifies that the named persona is the
  one actually bound to the role, and reports the bound persona on a
  mismatch instead of silently rendering it. (#356)
- Residents added or evicted by a warm `agents`/`agent`/`full` reload now
  appear in (or disappear from) `casactl persona inspect/render/diff`
  immediately — previously the admin views described the boot-time fleet
  until restart. (#356)
- A recall probe interrupted mid-flight no longer wedges that recall path's
  circuit breaker into permanent fast-fail until restart. (#356)
- A failed explanation-record write no longer leaves its temporary staging
  file behind, and any stranded staging files are now cleaned up by the
  routine sweep. (#356)

## [0.186.0] - 2026-08-12

### Fixed

- Recovering from a rejected session resume no longer discards a session
  that a concurrent turn on the same conversation registered in the
  meantime — the retry resumes that newer session instead of splitting the
  conversation. (#349)
- The one-time plugin-degradation notice prepended to the first reply after
  boot is no longer lost when sending that reply fails — it reappears on
  the next successful reply. (#349)
- The memory-budget overrun warning ("three turns in a row over budget") is
  no longer triggered early by transparent SDK retries: one turn now counts
  once, and only when it completes. (#349)
- The shipped speaker-provenance schema now accepts the `automation`
  speaker kind that /invoke and webhook turns are recorded with, matching
  the runtime validator. (#349)
- The live Hindsight memory contract now fails when retained context does
  not round-trip byte-equal through recall, instead of recording the probe
  without enforcing it. (#349)

## [0.185.0] - 2026-08-12

### Fixed

- A plugin whose managed install was wiped or rolled back is no longer
  reported "ready" at boot just because an unrelated binary of the same name
  exists on the image PATH — for example, a Python-based plugin whose
  virtualenv was deleted was previously satisfied by the image's own
  `python3` and started without its packages. Only the managed
  `tools/bin` entry now counts; anything else reports degraded. (#334)
- A specialist job you cancelled just before a restart is no longer
  resurrected as a "Lost on restart" failure notice — restart recovery now
  honors the durable cancellation and finalizes the job as cancelled,
  with nothing delivered. (#334)
- An engagement turn's end-of-turn bookkeeping (activity summary and spool
  lifecycle) can no longer be left hanging until an unrelated later turn
  when the internal turn-end callback fails transiently: the turn-end event
  is now durably recorded with the closing checkpoint and re-delivered on
  the next pass. (#334)
- A transient Telegram failure while updating an engagement's summary
  message no longer silences summary updates for the following throttle
  window — a failed edit now retries at the next update instead of
  consuming the window. (#334)

## [0.184.0] - 2026-08-12

### Fixed

- A plugin's one automatic setup run is no longer silently spent when the
  dispatched agent session turns out not to carry the setup tool (seen live
  when a just-updated plugin's MCP server failed to start in a session built
  right after an agent reload). The turn now reports back whether the tool
  was actually runnable; if not, the setup obligation returns to pending and
  the next agent reload re-dispatches it, bounded at three attempts before
  failing visibly with a note to run setup manually. (#521)
- A dispatched setup obligation no longer carries a stale "waiting for live
  trigger route" message from an earlier gate hold into its final state. (#521)

## [0.183.0] - 2026-08-12

### Fixed

- Config reconcile no longer re-seeds (and then re-heals) a deliberately
  deleted per-agent `delegates.yaml` on every boot — each boot was reporting
  a phantom `changed` and recording a git snapshot of an identical tree. The
  deletion now sticks while the image copy is unchanged; a changed image
  still reintroduces the file once. Every other missing tracked file keeps
  being re-seeded, since that reseed is what repairs a deleted required file
  before boot fails on it. (#311)
- A pending full reload can no longer be starved by a steady stream of
  smaller reloads (nor the reverse): reload lock admission is now
  first-in-first-out. (#311)
- Topic-retention sweeps are serialized and record each deleted topic
  immediately, so concurrent or interrupted sweeps no longer double-issue
  Telegram topic deletions or double-count results. (#311)
- Approving a protected tool call now records the grant in the store the
  caller injected (the documented dependency seam) rather than always the
  process-wide one; production wiring is unchanged. (#311)
- A memory-recall hit with malformed metadata now surfaces as the documented
  recall protocol error instead of an unhandled exception. (#311)

## [0.182.1] - 2026-08-12

### Fixed

- Test-only: deflaked the one remaining fixed-sleep assertion in the ask
  body-limit suite (the v0.124.1 deflake class), which went red on a loaded
  CI worker after the v0.182.0 merge. No runtime changes.

## [0.182.0] - 2026-08-12

### Fixed

- **A fresh install is now genuinely neutral.** The shipped concierge no
  longer carries Magic: The Gathering routing doctrine, no longer advertises
  a delegate the image does not ship (which failed with `unknown_agent` when
  invoked), and the MTG "Judge" persona pack is no longer bundled — that
  content belongs with the separately installed MTG specialist component.
  (#427)
- **Approving two pending persona-install consents no longer erases the
  first approval.** The persona-install consent ledger is now re-read before
  every write, so sibling consent prompts merge instead of clobbering each
  other's recorded approvals. (#310)
- **A failed platform or completion notice no longer freezes the progress
  message.** The in-progress narration message now stays live and editable
  when a notice's send definitely failed (nothing was posted below it),
  matching the rule every other write path already follows. (#392)

## [0.181.0] - 2026-08-12

### Security

- **The web terminal is no longer reachable by anything but the ingress
  panel.** When `enable_terminal` is on, the terminal (an unauthenticated
  root shell) now binds a root-restricted internal socket instead of a
  container-loopback network port, so only the front-door proxy — and never
  another in-container process — can reach it. A default install (terminal
  off) is unaffected. (#514)
- **Config-reload requests over the internal control socket now require a
  root caller.** The `/admin/*` control routes verify the connecting
  process's identity (`SO_PEERCRED`) and refuse anyone who is not root,
  adding a caller-identity check on top of the socket's file permissions.
  (#467)

## [0.180.0] - 2026-08-12

### Fixed

- **Reminders no longer arrive with a stray "Sent." follow-up message.**
  A fired reminder's delivery turn now closes with the silence sentinel
  instead of narrating, so each reminder is one message, not two. Applies
  to reminders set from now on; existing ones keep their stored prompt
  until re-created. (#511)
- **A specialist's boot capability line no longer misreports it as
  tool-less.** The boot `agent_capabilities` line reports the role's
  *declaration* and now says so (`declared_tools=…`); the tools a
  specialist actually receives — including its own plugin's server grants
  — are logged per delegation as `agent_capabilities_effective`, so
  post-install verification finally has an oracle that matches reality.
  (#459)

## [0.179.0] - 2026-08-12

### Fixed

- **A tier classification the model fumbles now gets a second chance
  instead of silently locking the memory to `private`.** When the
  classifier's reply omits the mandated `Tier: <word>` answer line
  (measured at ~12% of calls on a large save), Casa now re-asks once with
  the format requirement restated before falling back to the leak-safe
  `private` default — and a second opinion can never *lower* the tier
  below what the discarded reply's own answer lines said. Each session
  save also logs a single "defaulted for N of M items" line, so the rate
  is visible at a glance instead of buried in per-item warnings. (#508)

## [0.178.0] - 2026-08-12

### Added

- **Agents can send labelled hyperlinks.** `[label](url)` in agent output
  now renders as a real Telegram hyperlink (http/https destinations), so a
  long OAuth or dashboard URL no longer dominates the message as a wall of
  text. Links inside table cells survive too. A link with any other scheme,
  an empty label, or image syntax leaves its whole line untouched rather
  than guessing. (#404)

### Changed

- **Markdown tables finally render well on a phone.** Tables that used to
  arrive as raw `| pipe | rows |` (any cell containing code or bold
  markers) or as a ragged monospace box now re-render from their parsed
  cells: narrow tables become a properly padded monospace box, wide or
  link-bearing tables become per-record `Header: value` stanzas with bold
  field names, and anything the renderer cannot classify with confidence
  stays as plain readable rows — cell content and link destinations are
  never dropped. (#506)
- **Inline rendering now follows CommonMark.** The renderer's inline pass
  (code, bold/italic, escapes, links) runs on a CommonMark engine instead
  of a bespoke scanner: ``double-backtick code``, `***bold italic***`,
  intraword bold and backslash escapes now render as written. Underscores
  are still never emphasis — tool and identifier names stay literal — and
  an unclosed code fence still keeps everything after it untouched.

## [0.177.0] - 2026-08-11

### Fixed

- **Memory sensitivity tiers stopped falling to `private` when the model
  agrees with itself.** The bundled model's most common reply to the
  tier-classification prompt turned out to obey both prompt instructions at
  once — the bare tier word *and* the mandated final `Tier: <word>` line.
  The 0.176.0 parser treated that earlier answer-shaped line as ambiguity
  and defaulted the item to `private`, which in practice re-created the
  original #497 blindness for most retained facts. The parser now accepts
  an earlier answer line exactly when it names the *same* tier as the final
  line; genuine conflicts, prose-buried tier words and unlabeled multi-line
  replies still fall to `private`. The classifier also gets real turn
  headroom (the cap is a runaway backstop now, not a budget) with its
  turns made genuinely inert — no tools, no subagents — so "maximum number
  of turns" exhaustion no longer eats classifications either. (#497)

## [0.176.0] - 2026-08-11

### Fixed

- **Memory sensitivity tiers work again.** Since the bundled model began
  answering the tier-classification prompt with a short explanation instead
  of the single word the parser demanded, every remembered fact fell to the
  fail-safe `private` tier — nothing leaked, but voice and friends-level
  recall silently went blind to everything retained. The classifier prompt
  now pins an exact answer format (a final `Tier: <word>` line), the parser
  accepts precisely that — a tier word buried in prose, a decorated or
  conflicting answer, still falls to `private` — and the classifier gets a
  spare model turn for the reply shapes that previously errored out. An
  unparseable reply now logs its shape (length, line count, whether an
  answer label was present) without ever logging the reply text. (#497)

### Changed

- **Approval buttons now wait for you.** The Approve/Deny button posted when
  an agent wants to run a protected tool used to expire after two minutes —
  fine if you were watching the chat, otherwise a missed window and a whole
  re-run of the request. It now stays live for ten minutes, the same as
  consent prompts. The approval you grant is unchanged: still single-use,
  still bound to the exact action and arguments, still expiring five minutes
  after it is granted. (#498)
- **Installing a specialist now sets up its plugins' secrets in the same
  conversation.** A specialist whose bundled plugin needs configuration
  values (an API key, a vault name) used to install "successfully" and then
  refuse its first real action, sending you back for a second configuration
  session to wire what the installer already knew was needed. The install
  flow now asks for and wires those values before it reports done, and the
  install result lists exactly which variables each bundled plugin
  requires. (#499)

## [0.175.0] - 2026-08-11

### Fixed

- **The Telegram webhook route now says what it did.** `/telegram/update`
  used to answer the same bare 200 whether the update was queued for the
  agent or silently discarded as a redelivered duplicate, so test harnesses
  and diagnostic tooling driving the route could not tell the two apart
  without container log access. The response now carries an `X-Casa-Update`
  header — `accepted`, `duplicate`, or `ignored` (channel not started or
  unparseable payload) — while the status stays 200 with an empty body, as
  Telegram's redelivery contract requires. (#428)

## [0.174.0] - 2026-08-11

### Fixed

- **Approving a plugin consent now always goes somewhere.** When a consent
  prompt (the DM keyboard that opens a plugin's webhook, authorization
  callback, or event subscription) expired or was missed, agents had no way
  to re-issue it — and would relay the question as an ordinary button ask
  instead, which accepts the Approve tap and records nothing, silently
  wedging the plugin's setup until a full reload. A new `consent_reprompt`
  tool re-posts the real, committing keyboard on demand; the agents are
  instructed to use it and never to relay a consent as a plain question.
  It reports actual delivery (a keyboard that could not be posted is a
  loud, typed failure, never a silent success), never re-asks a consent
  you explicitly denied (a new plugin change or reload still does), and an
  approval that arrives after an earlier prompt expired now re-arms the
  plugin's pending setup step, so the promised "approving will run setup"
  holds on this path too. (#494)

## [0.173.0] - 2026-08-11

### Fixed

- **Installing a specialist whose bundled plugin creates its own credentials
  no longer fails and undoes itself.** A plugin that declares "my setup tool
  provisions this credential" is, on a fresh install, always unprovisioned —
  that is the state its setup tool exists to fix. The post-install check was
  reading that normal state as a failure and rolling the whole install back,
  making such specialists impossible to install. Unprovisioned-by-design now
  counts as "installed, awaiting setup", exactly as documented. (#488)
- **A full reload that also refreshes plugin secrets no longer freezes
  Casa's configuration tools.** Asking for a full reload with the
  environment refresh included made the reload wait on itself forever —
  the calling conversation hung mid-turn, and every later reload or plugin
  change queued behind it until a restart. The two steps now recognise they
  are one operation and the reload completes. (#489)
- **A rolled-back specialist install cleans up after itself completely.**
  Rolling back a fresh install used to leave a dangling link behind that
  made every subsequent reload retry — and fail — the specialist that was
  no longer there, until the link was removed by hand. The rollback now
  removes it. (#490)
- **When an install is rolled back, the result now says so.** The failure
  result used to describe only what wasn't ready, reading as "installed but
  dormant" — so a rolled-back install could be reported (and acted on) as a
  committed one. The result now states plainly that the change was undone,
  distinguishes a complete rollback from one the runtime hasn't fully
  caught up with, and the deciding verdict is logged. (#491)

## [0.172.0] - 2026-08-10

### Fixed

- **Saying the same thing twice no longer stores it twice.** Long-term memory
  is content-addressed so a repeated fact collapses to one stored document,
  but the per-turn timestamp block that rides on every sent message had crept
  into the stored text and its identity hash — so the same sentence said in
  two conversations minted two near-identical memories, and the bank slowly
  filled with duplicates. The timestamp is now split off before a turn is
  stored: the memory text is clean, the identity is timestamp-independent,
  and the turn's wall-clock time is kept on the stored item itself.
  Existing duplicates in the bank are not rewritten; new retentions
  deduplicate from here on. (#471)
- **Casa no longer tells you "there is no record in memory" when it simply
  can't read the record from where you're asking.** A memory search is
  filtered by the surface's read clearance, so an empty result can mean
  "never told" or "not readable here" — and the agent was wording both as
  non-existence. Empty search results now tell the agent exactly what it may
  claim: nothing readable here, not proof of absence — and when readable
  matches exist but were too large to render, it says that instead of
  denying them. (#472)

### Security

- **Lowering an engagement's clearance now also takes away what it had
  already read.** When someone with lower clearance steers an engagement,
  its future memory reads were already clamped down — but everything it had
  fetched earlier (its conversation so far, the memory digest injected at
  launch, even its original task text) stayed available, so it could simply
  be asked to restate private material. A downgrade now durably invalidates
  the engagement's working context: its session is rebuilt fresh at the new
  clearance floor before the steering message is answered, the original
  task/context are withheld from every later rebuild, in-flight memory reads
  are re-filtered at the new floor, and until the rebuild completes the old
  process can neither read memory nor launch new work. Restarts cannot bring
  the old session back. What was already posted in the (group-visible) topic
  stays, as does output of a turn that was already running. (#369)

## [0.171.0] - 2026-08-10

### Security

- **Casa's private runtime state is no longer readable by the isolated users
  its executor engagements run as.** Since the isolated-engagement release each
  executor engagement runs under its own restricted OS user, but several files
  Casa writes for itself were still created readable by everyone in the
  container. That included the two access tokens Casa uses to talk to the
  Supervisor — which can read every app's saved settings, so the tokens
  indirectly exposed the Claude, GitHub and 1Password credentials stored
  there — the webhook signing secret, up to ~21 MB of *another* engagement's
  captured output, the resident agent's own conversation home, and the
  `/config` snapshot history. Casa now declares every private path with the
  permissions it should have, repairs them on every start (so upgrading an
  existing install fixes files already on disk, not just newly written ones),
  and creates them correctly in the first place. If Casa cannot make one of the
  access-token files private, it refuses to start executor engagements at all
  rather than run one that could read it — Telegram, the resident agents and
  specialist engagements keep working. Two paths are deliberately left readable
  because engagements legitimately need them: the config-sync report the
  configurator reads, and installed plugin artifacts.

## [0.170.2] - 2026-08-09

### Fixed

- Internal: fixed a defense-in-depth test-injection contract in the
  engagement-cleanup path so the CI unit gate is deterministic; no runtime
  behavior change.

## [0.170.1] - 2026-08-09

### Fixed

- **Upgrading an existing install to the isolated-engagement release no longer
  blocks new engagements on the first boot.** On the very first start after
  upgrading, Casa sets up its per-engagement user bookkeeping from scratch;
  a check meant to protect against reusing a user id was too strict and
  mistook a normal pre-upgrade engagement folder for evidence that setup had
  been lost, refusing to hand out any new engagement user until the next
  boot. Casa now recognises a genuine first-time setup and initialises
  cleanly, while still refusing (as designed) if real evidence shows its
  bookkeeping was actually lost. Fresh installs and already-upgraded installs
  are unaffected.

## [0.170.0] - 2026-08-09

### Security

- **Executor engagements now run under isolated per-engagement OS users with
  dropped privileges.** Each `claude_code` engagement (plugin-developer
  today) gets its own dedicated, never-reused system user with no elevated
  capabilities and no path to regain root, and its workspace is locked down
  to that user alone. A running engagement's own process can no longer read
  another engagement's files or credentials — that boundary is now enforced
  by the operating system itself, not only by application-level checks.
  Casa's own housekeeping (reading a plugin artifact, migrating an older
  engagement after an upgrade) goes through a hardened accessor that refuses
  to follow a symlink out of a workspace. An engagement is refused outright,
  rather than started with weaker protection or left crash-looping, if any
  part of this isolation cannot be set up. Nothing about a resident's or
  specialist's day-to-day experience changes.

## [0.169.0] - 2026-08-08

### Security

- **The assistant no longer carries a shell.** `Bash` is now hard-denied for the
  primary resident — all concrete work already goes through delegation to
  specialists and executors, so the broad shell was unused authority. Nothing
  in normal use changes; a request that used to fall back to a shell command
  is delegated instead.
- **Executor hook containment is fixed at load and can no longer be weakened
  by editing a config file in place.** Every Claude-Code-driven executor
  (plugin-developer today) must now declare its two baseline guards —
  the dangerous-command guard and the filesystem scope guard — or it fails to
  load at all, rather than silently running with a narrower, easy-to-miss
  default. What is declared at load time is captured once and reused
  everywhere that guard gets enforced — when a workspace is provisioned, when
  a running executor's hook policy is resolved over the network, and when a
  session is resumed after a restart — so a hollowed-out or matcher-tampered
  hooks file on disk can no longer widen what is actually enforced, and a
  resumed executor whose enforcement has drifted from what it should be is
  safely cycled rather than left running on stale settings.

## [0.168.0] - 2026-08-08

### Security

- **Permission approvals inside engagements are now bound to the configured
  operator on every path.** Approving a tool that is not on an engagement's
  allow-list is authorization, and only the configured operator (your
  `telegram_chat_id`) holds it: the approval keyboard now binds to the
  configured operator rather than whoever started the engagement, and the
  broker that resolves the verdict rejects any answer from a different
  identity — fail-closed, so a request with no bound operator is answerable by
  nobody. The internal `permission_verdict` endpoint was removed entirely: it
  authenticated only the engagement's own credential, which let an engagement
  process approve its own pending permission request without the operator ever
  seeing it. With no operator configured ("accept all chats"), a gated tool is
  now denied immediately instead of holding the engagement on an unanswerable
  keyboard. In-engagement *questions* (`ask`) are unchanged and stay answerable
  by the engagement's creator — answering a question is interaction, not
  authorization.

### Removed

- The long-deprecated in-process permission verdict queue (superseded by the
  verdict broker in v0.75.0) is fully deleted.

## [0.167.0] - 2026-08-08

### Changed

- **Operator-gated plugin tools now work inside interactive specialist
  engagements.** Previously a plugin could not have both an engagement topic and
  an operator-confirmed (protected) destructive tool: a protected call made
  inside an engagement was refused outright, before any approval prompt. It now
  routes through the same operator authorization keyboard your 1:1 chat uses —
  the approval is bound to that exact call *and* to that specific engagement, so
  an approval granted in one engagement can never authorize a matching call in
  another, and approving resumes the engagement automatically. Engagements that
  cannot reach the configured operator (or are not an active interactive
  specialist) still refuse the call, fail-closed.

## [0.166.0] - 2026-08-08

### Security

- **The internal tool bridge now enforces each engagement's own tool grant.**
  An engagement (specialist or executor) invoking a Casa framework tool over the
  internal bridge is now checked against the exact set of tools it was granted,
  and a call that carries no valid engagement identity is refused rather than
  run. Previously the bridge authenticated the caller but did not re-check the
  grant at dispatch, relying on the calling process to stay within its
  allowlist. Enforcement now happens where the tool is dispatched, so it holds
  regardless of how the request was constructed. Lifecycle completion is
  unaffected.

## [0.165.0] - 2026-08-08

### Fixed

- **A reminder set while Casa was reloading its configuration could vanish
  silently.** The config reconciler rewrites a resident's trigger file on a
  background thread; a reminder saved in the instant between the reconciler
  reading that file and writing it back was overwritten and lost, with nothing
  to recover it from. Every writer of a trigger file — the reminder tools, the
  configurator's trigger edits, and the reconciler's whole pass — is now
  serialized under a single lock, so a reminder is always saved either before
  or after a reconcile, never into the gap. (#458)

## [0.164.0] - 2026-08-08

### Removed

- **Casa no longer carries code for upgrading from much older versions of
  itself.** Pre-1.0, with development installs expected to start fresh, the
  accumulated migration and compatibility machinery (~2,000 lines) is gone:
  the old port-8099 fallback for engagement workspaces provisioned before
  v0.14.0, the one-shot conversion of the pre-durable-job delegation file,
  the v1 session-key conversion, two boot-time config migration blocks, the
  plugin-setup store's version upgrade-on-read, and assorted single-line
  tolerances for state shapes no current Casa writes. Less code on the boot
  path means fewer places for it to go wrong.
- The boot-time purge of stored webhook sessions — a security behavior, not
  a migration — is retained as its own explicit step.

### Changed

- Boot now refreshes an engagement workspace's connection file when the
  server address in it has drifted, not only when the credential has —
  a workspace can no longer keep talking to an address Casa stopped serving.
- A plugin-setup store carrying an unsupported version now resets cleanly
  and rebuilds from live state, the same way a corrupt store does.

### Upgrade notes

- One-time cleanup on an existing install:
  `rm -f /data/delegations.json /data/callback-episodes.json` (both retired;
  a leftover file is inert but pointless). If `/data` predates v0.150, wipe
  it or expect the plugin-setup store to reset itself on first boot.

## [0.163.0] - 2026-08-08

### Fixed

- **A reminder is no longer silently thrown away when the configurator is
  editing the same agent's schedule.** Reminders live in the same file as the
  agent's other triggers, and the configurator edits that file from a separate
  process — so if you asked your assistant to remind you about something while
  a configuration change was in progress, the configurator could write the file
  back without it. You were told the reminder was set, it quietly was not, and
  nothing reported the loss. Trigger changes now go through Casa itself, which
  makes the change in one step and leaves everything else in the file alone; an
  agent editing that file by hand is refused and pointed at the proper route.
  Your own edits to the file are unaffected.
- **A plugin's setup step no longer starts on the strength of Casa not having
  looked.** The check that decides whether a plugin's endpoints are live asked
  "is anything wrong with this plugin?" — and answered "no" in two situations
  where the plugin had not been examined at all, because the plugin registry was
  unreadable or that one plugin's files could not be read. In the first of those
  no plugin's endpoints work at all. The check now requires having actually seen
  the plugin before treating silence as good news, and otherwise waits.
- **A newly renamed or newly added agent can no longer disappear from another
  agent's list of who it can hand work to.** Reloading one agent while another
  was being rescanned could briefly publish a list missing agents that were
  perfectly fine, so a hand-off would be refused as unknown until the next
  reload. The rescan is now published in one piece. Relatedly, an agent added
  after start-up could be launched without the plugins it is assigned, because
  Casa was still reading a start-up-time snapshot to decide what kind of agent
  it was; that snapshot is now refreshed with everything else.

## [0.162.0] - 2026-08-07

### Fixed

- **A plugin's setup step no longer runs against a credential Casa is about to
  replace.** Approving a plugin's permission prompt and creating the secret that
  approval authorizes are two separate moments: the approval is recorded
  immediately, while the webhook secret — and the address a plugin gives its
  provider to send you back to — are written a moment later. Casa decided the
  setup step could run from the approval alone, so on a first approval it could
  start before the secret existed, and on a re-approval after a revoke it could
  hand out the previous secret seconds before that file was rewritten. Either
  way the external service ended up pointed at something that no longer worked,
  with nothing to say so. Casa now checks for the real thing — the secret
  actually on disk, minted for this approval, and the published address — and
  simply waits when it is not there yet. The wait is short and self-clearing,
  and the plugin health report shows it while it lasts.
- **A plugin whose files could not be read no longer freezes every other
  plugin's setup.** Casa republishes the small discovery file a plugin's setup
  step reads whenever that plugin itself is healthy, instead of holding it back
  because some unrelated plugin in the list was unreadable. Left as it was, the
  new wait above would have had no way out: Casa would have waited for a file it
  had already decided not to write, and the only escape would have been
  repairing a different plugin entirely.
- **A plugin registry reload arriving mid-reconcile can no longer produce
  contradictory routing.** Working out which plugin endpoints should be open
  involved several separate reads of the plugin registry, so a change landing
  between two of them could mix an old list of plugins with new assignments —
  briefly opening an endpoint for a plugin that had just been unassigned or
  removed. Each pass now reads the registry once and answers every question from
  that one reading.

## [0.161.0] - 2026-08-07

### Changed

- **A plugin's setup step now has exactly one owner: Casa.** Some plugins ship a
  setup step that points an external service at your home — writing a freshly
  issued key into the other side's configuration, for instance. Until now that
  step could be run either by Casa itself, once you approved the plugin's
  permission prompt, or by an assistant acting on a note the installer left
  behind; which of the two happened was decided the moment the plugin was
  installed. That decision could not always be made correctly, because it
  depended on something that had not happened yet — whether you would approve
  the prompt, deny it, or never answer it at all. Two attempts to get it right
  produced the opposite mistakes: a setup step that ran before the key it needed
  existed, and one that never ran at all.
- Casa now waits instead of guessing. It records that a plugin is owed a setup
  run, and clears that run only once the permission position for that exact
  version of the plugin is actually known — at once when nothing needs
  approving, when you approve everything it declares, and not at all if you
  decline. A cleared run still waits until it can succeed: routes live, secrets
  resolved, the running agent able to load the plugin. Anything Casa cannot yet
  establish leaves the run pending and visible in the plugin health report
  rather than resolved by a guess. In practice: setup no longer runs before the
  credential it depends on exists, an updated setup step no longer gets skipped
  because an unrelated permission was unchanged, and declining a prompt no
  longer produces a message asking you to run something by hand that you have no
  way to run.
- The setup outcome now arrives as its own message once the run happens, rather
  than being folded into the install or update report. It carries the setup step's own
  words, which — as of 0.160.0 — is the only account of the connection Casa will
  vouch for.

- A permission Casa cannot currently ask you about — the plugin is unassigned,
  or its target agent lacks the right channel — now leaves the setup run pending
  instead of being treated as a permission that isn't needed.

### Fixed

- If Casa could not reach you to dispatch a setup run (no Telegram chat
  configured yet), the run was abandoned rather than retried. It now waits and
  runs once it can reach an agent, and stays listed in the plugin health report
  until it does.
- A plugin declaring the same tool as both its setup step and an
  operator-confirmed tool is now refused at verification: Casa's own setup turn
  cannot satisfy an operator confirmation, so the combination had no way to run
  at all.
- Declining a plugin's permission prompt used to produce a note calling it a
  "trigger" even when what you declined was a callback, and telling you to run
  the setup step manually — which was not possible, since a plugin's tools are
  only available to the agents it is assigned to. The note now names what was
  actually declined and tells you that approving it will run the step.
- A plugin bundled with a specialist could be installed without Casa recording
  that it was owed a setup run at all.
- With no chat available to prompt you in, Casa recorded nothing about a
  plugin's pending permission until some later restart or reload — long after it
  had already committed to who would run setup.

### Documentation

- `architecture/plugins.md` reached its size ceiling and was split. Everything
  about turning an installed plugin into a usable one — the environment its
  servers need, the setup step, and the plugin environment and media channels —
  now lives in `architecture/plugin-runtime.md`.

## [0.160.0] - 2026-08-07

### Fixed

- **Casa no longer tells you an integration is broken when it cannot see it.**
  After updating a plugin, Casa reported that the integration was dead until a
  setup step ran. That is not something Casa can know: a plugin's credential
  often survives an update untouched, and the connection keeps working
  throughout. Gmail was announced as down while it was still serving mail, and
  the operator was asked to re-authorize something that needed no
  re-authorizing. Completion messages now say only that the setup step still
  needs to run, and the assistant no longer passes on anyone else's verdict
  about a connection. An unfounded "it's fine" is treated as the same mistake as
  an unfounded "it's dead". That extends to the setup step's own report: it is
  required to configure the connection, not to test it, so Casa treats "setup
  succeeded" as covering the connection only when that step actually says so.

### Documentation

- Corrected the description of what happens when a resident agent and a
  specialist claim the same name after startup: both places that rebuild the
  name index keep the resident, and the documentation had said they disagreed.

## [0.159.0] - 2026-08-07

### Fixed

- **A mistyped safety setting in an agent's hook configuration is now refused
  instead of quietly letting everything through.** The path restrictions that
  keep an agent writing only where it is allowed are written as a list. Written
  without the list dash — `writable: /config` instead of `writable:` on its own
  line — the setting was read one character at a time, and one of those
  characters was `/`, which matches every path on the system. The restriction
  that was meant to narrow what the agent could touch was, in that state,
  allowing all of it. A setting of the wrong type is now rejected when the agent
  loads, so the agent refuses to start with a broken guard rather than running
  with an open one. The same now holds for the other hook settings — the on/off
  switch protecting resident agents from deletion takes a yes/no value and
  nothing else, and the commit-size limit takes a whole number rather than
  quietly making sense of whatever it is given.
- **An agent whose hook configuration did not load no longer falls back to
  weaker protection.** Whenever one of these files failed — it could not be
  read, it did not load, or the whole directory it lives in could not be
  scanned — the agent carried on under Casa's built-in defaults, which for the
  guard protecting Casa's own state directories forbid nothing at all. Guarded
  actions for such an agent are now refused outright rather than checked
  against a weaker rule than the one that was written. A configuration that
  was working before a failed edit is still kept, so fixing a file does not
  require interrupting work already under way.

### Changed

- Casa's own test suite now checks that its offline test double accepts every
  option the application passes to the Claude Agent SDK. The check immediately
  found one that had been missing since v0.127.0. This is internal only — no
  user-visible behaviour changes — but it closes a gap that had repeatedly let
  the end-to-end tests go silently blind.

## [0.158.0] - 2026-08-07

### Fixed

- **A trigger or reminder you added no longer disappears when an update also
  changes the shipped ones — even in a file that uses YAML anchors.** Casa
  reconciles `triggers.yaml` entry by entry so your own entries survive an
  update, but a file using an anchor or alias (`&name` / `*name`) was excluded
  from that and resolved whole-file instead, taking every locally-added entry
  with it — reported as a conflict, but reported is not the same as kept, and
  a lost entry is a trigger that stops firing or a reminder that is never
  delivered. An anchored file is now
  reconciled entry by entry, and the shipped change is applied rather than
  skipped. (Two alias shapes stay on whole-file resolution, because everything
  downstream would have to walk them forever: one that refers to itself, and
  one that expands to an astronomical size.)
- **An environment variable's punctuation can no longer change — or break — an
  agent configuration file.** A `${VAR}` reference was substituted into a
  file's text before it was read as YAML, so a value containing `#`, a quote
  character or a newline could silently truncate the setting it appeared in or
  stop the file loading altogether — which for a resident agent stops Casa
  starting. A reference is now resolved after the file has been read, so a
  variable's contents can no longer alter the file around it. **Quote a
  reference — `prompt: "${DETAIL}"` — and its value now arrives exactly as it
  is, whatever it contains.** An unquoted reference standing alone still means
  whatever its text means as YAML, so quoting is the way to ask for text.

### Changed

- A configuration file must now be valid YAML before any `${VAR}` reference is
  read, and a handful of hand-authored forms change with it. A reference
  standing in for a whole unquoted setting still supplies a number, a true/false
  flag or a list as before. What no longer works: one written where YAML itself
  needs quoting (unquoted inside `[...]`), and one under an explicit type tag
  (`!!int ${VAR}`) — both are now reported as a file error instead of depending
  on the environment. No shipped configuration uses references at all.
- A `triggers.yaml`, `delegates.yaml` or `executors.yaml` with a field whose
  whole value is a `${VAR}` reference written as text — quoted, or carrying
  YAML's string tag — is no longer reconciled entry by entry. Rewriting such a
  file cannot preserve the quoting or tag that field's meaning now depends on,
  so it takes whole-file resolution instead — which reports a conflict and keeps
  a recovery copy, rather than quietly changing what an entry says. A reference
  used any other way — inside a longer value, unquoted, or in a comment —
  reconciles as before.

## [0.157.0] - 2026-08-06

### Fixed

- **Renaming an agent no longer takes a restart to take effect everywhere.**
  After renaming a persona and reloading just that agent, the other agents
  went on displaying the old name while delegation had already switched to
  the new one — so asking for the name Casa was still showing came back as
  "not connected". Every agent now reads names from the same live source
  delegation does, so what Casa displays and what Casa accepts stay in step
  without restarting the app. A specialist is also introduced to the caller
  under the caller's current name rather than its name at boot.

### Changed

- An agent that becomes reachable (or stops being reachable) while another
  agent is mid-conversation is now offered — or withdrawn — on that agent's
  next turn, instead of on its next restart.

## [0.156.0] - 2026-08-06

### Fixed

- **Asking an assistant to consult a specialist by name failed, and the
  error blamed the wrong thing.** Casa shows each delegate as a persona —
  "Alex", "Tina" — but delegation only accepted the underlying role id, so
  addressing a specialist by the name Casa itself displays was refused as if
  the specialist were not connected. Following that advice led to
  reconfiguring something that was already correct, and the delegation still
  failed. A display name now works, provided it matches exactly one of the
  agents that assistant may delegate to; anything genuinely ambiguous is
  refused and says so.
- **A refused delegation now says what it would have accepted**, listing the
  agents that assistant can actually reach instead of only reporting a
  failure.
- **"The specialist's tools are unavailable" now explains why.** When a
  specialist cannot start because one of its plugins is missing credentials,
  the reply names the environment variables that still need wiring and how
  to wire them, instead of reporting only that a plugin is missing. The
  reason was already written to the log, where the assistant could not read
  it.

### Changed

- Casa now shows delegates as `role (Display Name)` rather than
  `Display Name (role: role)`, so the value delegation actually needs comes
  first. The assistant's built-in instructions were updated to match — they
  previously used a persona name in the delegation example itself.

## [0.155.0] - 2026-08-06

### Fixed

- **A bundled plugin could not use a default value in its MCP config.**
  `${VAR:-default}` is standard Claude Code syntax, but Casa's install check
  mistook it for a template marker and refused the whole component — while
  the same syntax was fine in a directly-installed plugin. It now works in
  both, and Casa checks what the value will actually expand to, so a marker
  cannot be smuggled past the check by splitting it around the default.
- **A plugin whose start command referenced an environment variable could be
  wrongly rejected as missing.** Introduced in 0.154.0. Such a command is
  only resolvable when the plugin actually starts, so it is reported as
  unchecked again rather than blocking the install.
- **A plugin could point outside its own verified files** by writing a
  default on a variable Casa itself supplies. Both spellings are now held to
  the same containment rule.

### Changed

- **`casa.optionalEnv` has been removed**, one release after it was added.
  Use `${MY_TOKEN:-}` in `.mcp.json` instead for a variable the plugin does
  not need: Claude Code substitutes the default, so nothing is missing and no
  placeholder reaches the plugin, and the default can be a real value rather
  than only empty. That was always the simpler answer; it just did not work
  in a bundled plugin until the fix above. `casa.setupProvides` stays — it is
  the only way to say "my setup tool provisions this", which keeps the plugin
  reported as not-ready until the value lands.

  **If you declared `casa.optionalEnv` in 0.154.0**, replace it: the field is
  now ignored, so the variables it covered would hold your plugin back again.
  0.154.0 was published the same day and no released plugin declares it, so
  this is expected to affect nobody — but check if you were quick.
- A plugin's install-consent screen and the duplicate-name check now list
  every environment variable its config mentions, including defaulted ones.

## [0.154.0] - 2026-08-06

### Fixed

- **A plugin whose setup tool creates its own credentials can now be
  installed.** 0.153.0 held such a plugin back until every credential it
  references was available — which it never could be, because the setup
  tool that creates them could not run until the plugin loaded. The
  install sat permanently at "needs attention", and any specialist that
  requires the plugin was unavailable with it. A plugin now declares which
  values its own setup provisions, and Casa loads it so setup can run
  (#429).

### Added

- **Two new plugin manifest fields.** `casa.setupProvides` lists the
  variables a plugin's setup tool creates; `casa.optionalEnv` lists ones
  the plugin genuinely does not need. Neither holds the plugin back, and
  neither is ever handed to the plugin as a placeholder — Casa passes an
  unset value as empty. A plugin still reports **not ready** while a
  `setupProvides` value is missing, so a setup run that never happened
  stays visible; an `optionalEnv` value's absence is not a problem at all.
  Anything undeclared blocks the plugin exactly as before. Declared names use
  a reserved `CASA_PLUGIN_` prefix: declaring a name binds it for the whole
  session, so the namespace is fenced (only declared names are — a plugin may
  still reference any variable in its `.mcp.json`).

### Changed

- **A missing credential that comes from an app option now says so.**
  When a plugin needs `OP_SERVICE_ACCOUNT_TOKEN`,
  `ONEPASSWORD_DEFAULT_VAULT` or `CONTEXT7_API_KEY`, Casa's message names
  the app option to set instead of pointing at the plugin credential
  store, which cannot supply it.

## [0.153.0] - 2026-08-06

### Fixed

- **A plugin's automatic setup no longer runs before its secrets are
  wired.** Approving a plugin's consent prompts while the installer was
  still storing its credentials could launch the setup tool with
  placeholder values — the Gmail plugin, for example, produced a sign-in
  link Google rejects. Setup now waits until every credential the plugin
  needs is actually available, and fires automatically the moment it is
  (#423).
- **Plugins with missing credentials are no longer loaded into agents.**
  An agent could previously reach a plugin whose credentials were absent
  and get tools that fail confusingly deep inside the external service.
  Such a plugin is now held back from the agent — with a clear health
  report naming the missing values — until its credentials are wired and
  the agent reloads (#424).

## [0.152.1] - 2026-08-06

### Fixed

- **v0.152.0 images failed to build, so the release never reached the
  store.** The image's bundled-plugin build step could not find the new
  plugin events module and aborted; no 0.152.0 image was published. The
  module now ships into the build step, and this release delivers
  everything listed under 0.152.0.

## [0.152.0] - 2026-08-05

### Added

- **Plugins can now emit domain events that wake other plugins.** A plugin
  may declare `casa.emits` in its manifest ("something happened in my
  data"), and other installed plugins may declare `casa.subscribes` on it.
  When the emitter records an occurrence, Casa wakes each subscribed
  plugin's agent in a fresh, standalone turn — no polling, no timers, and
  the emitter never needs to know who is listening. A burst of emissions
  coalesces into a single wake, and the woken agent re-reads the plugin's
  own data rather than trusting the wake itself.
- **Every subscription needs your approval.** The first time a
  subscription becomes routable you receive an Approve/Deny prompt naming
  the subscriber, the emitter, the event, and the agent that will be
  woken. Approval is bound to that exact combination: updating the
  subscriber plugin or re-assigning it to a different agent asks again
  (an emitter update does not, as long as it still declares the event).
  Revoke at any time with the configurator's `event_ack_revoke`.
- **Delivery is durable.** An unprocessed wake is redelivered on a
  widening schedule until the agent confirms it with the new `ack_event`
  tool — up to six attempts, then a single notice to you. Restarts never
  lose a pending delivery, and new `event_*` health reasons surface
  routing problems in plugin health.

## [0.151.0] - 2026-08-03

### Changed

- **The shipped agents no longer address a specific person.** The default
  prompts, personas, disclosure examples, response-shape rules and the morning
  briefing described the maintainer's own household — a name, two companies, a
  timezone. They now describe "the operator", so a fresh install starts neutral
  and learns who you are from your own conversations and memory instead of
  arriving with someone else's context. The morning briefing also stops naming a
  fixed 08:00 Europe/Amsterdam slot in its prompt text; the schedule comes from
  the trigger, where you can change it.
- **You are now identified by your own Telegram id.** Casa used to record the
  operator's turns under a fixed peer name compiled into the app; every accepted
  sender — you included — is now recorded as `telegram:<your id>`, and what
  marks you out as the operator is your read clearance, which still comes from
  `telegram_chat_id`. Behaviour on every channel is unchanged; what changed is
  the name written into memory provenance.
- **Casa now follows Home Assistant's timezone by default.** The `casa_tz`
  option shipped pre-filled with `Europe/Amsterdam` — the packager's own zone —
  which took precedence over the timezone Home Assistant already provides, so a
  fresh install anywhere else ran its schedules and told the time in Amsterdam
  local time. The option now ships empty, meaning "use Home Assistant's
  timezone", and an unrecognised value falls back to UTC instead. Set it
  explicitly only if you want Casa on a different zone from Home Assistant.
  Existing installs keep whatever value they already have; clear the option to
  pick up your Home Assistant timezone.
- **App description** updated to describe the whole fleet — assistant, butler,
  concierge and the specialists you add — rather than only the framework.

### Upgrade note

**Your existing long-term memories keep their old author name.** Memory
documents are keyed on the recorded peer, so facts stored before this release
stay under the previous name and remain searchable, while anything stored from
now on is filed under your Telegram id. The two never merge, so a fact you
repeat after updating is stored a second time rather than replacing the old
copy. Nothing is lost and nothing needs doing.

**There is no reliable one-step way to start clean, and this release does not
add one.** Clearing the memory bank is not enough on its own, because several
things write to it on their own schedule: retiring a conversation saves it
first — and because this release changes the shipped personas, your first
message after updating retires the conversation you had before it — a finishing
engagement saves its own summary, and a save that failed earlier is retried at
the next start. Any of those can land after you have emptied the bank. If you want a clean start, stop the app before clearing and expect the
odd item to reappear anyway; a supported "wipe memory" operation is tracked in
[#411](https://github.com/bonzanni/ha-casa-app/issues/411).

## [0.150.0] - 2026-08-03

### Changed

- **Reminders are now ordinary entries in your agent's own `triggers.yaml`**,
  marked as agent-managed, instead of living in a separate `reminders.yaml`.
  The separate file existed only to keep an update from deleting pending
  reminders; 0.149.0 made Casa reconcile `triggers.yaml` entry by entry, so an
  entry you or an agent added to it now survives an update on its own and the
  second file had no purpose left. Reminders behave exactly as before — durable
  across restarts and updates, delivered late if Casa was down when one was
  due, and self-removing after a one-off fires.
- **Setting a reminder is refused if the name would clash with one of your own
  triggers**, and the agent picks another name instead. Reminder names are
  random, so this is rare — but now that reminders and your triggers share one
  file, a duplicate name would stop the agent loading, so it is checked when the
  reminder is set.
- **Setting a reminder is also refused if your `triggers.yaml` uses `${...}`
  environment placeholders.** Rewriting such a file can change what an existing
  entry resolves to, so Casa declines rather than risk it. No configuration Casa
  ships uses them. Cancelling or delivering a reminder still works normally.

### Fixed

- **A one-off dated trigger you wrote yourself is no longer deleted after it
  fires.** Casa removes the entry only for reminders its own agents created;
  yours stays in your file. Note the consequence: after firing, such an entry
  remains but does nothing, because a trigger whose time has passed is not
  re-registered at startup. Remove it yourself, or ask the configurator to.

### Upgrade note

**This release requires clearing the old reminder files before you update.**
Casa refuses to load an agent whose directory contains a file it does not
recognise, and `reminders.yaml` is no longer recognised — so an installation
still holding one will fail to start. Remove `/config/agents` and reinstall, or
delete every `agents/<role>/reminders.yaml` before updating. Any pending
reminders are lost; set them again afterwards.

The same applies in reverse: **restoring a backup taken before 0.150.0, or
rolling back to 0.149.x and forward again, needs the same clearing.** This is
deliberate — the alternative was carrying migration code for a file that only
ever existed for two releases.

## [0.149.1] - 2026-08-03

### Fixed

- **The backup Casa saves before rewriting a configuration file no longer stops
  the agent it belongs to from loading.** The `.casabak` recovery copy sits
  next to the file it protects, and the strict directory check rejected it as
  an unknown file — so on the first update that preserved anything, the agent
  failed to load and Casa's own repair step undid the rewrite to fix the
  directory, throwing away exactly what the backup existed to save. Found in
  live verification of 0.149.0.

## [0.149.0] - 2026-08-03

### Fixed

- **Triggers, delegates and executors you add yourself are no longer deleted
  when an update also changes the version Casa ships.** These files are lists
  of named entries, and Casa now reconciles them entry by entry: the entries
  Casa ships still follow the update, and the ones you or an agent added are
  kept. Previously the whole file was replaced, so a reminder or a schedule you
  had asked for could disappear on an update with nothing to tell you. A file
  Casa cannot read as a clean list of uniquely-named entries falls back to the
  previous whole-file behaviour rather than guessing.
- **A configuration file that fails validation after an update now loses only
  the entries that are actually invalid**, instead of being reset in full.
- **Reconciliation now always saves the previous version of any file it
  rewrites** — as a `.casabak` file next to the original, and as a commit in
  the configuration repository, rather than only one or the other. If neither
  can be written it leaves the file alone and says so, instead of changing it
  with no way back. What it changed is named file by file in the sync report
  and in the heads-up you receive; a rewrite that only added entries and took
  nothing away is backed up but does not notify you.
- A configuration file Casa has never shipped, and so cannot repair, is now
  reported when it fails its schema instead of passing silently to a boot that
  will fail on it.

## [0.148.0] - 2026-08-03

### Added

Ellen can set reminders, and they last. Ask in plain language and she confirms
the exact time she has set, so a misread is obvious straight away:

- **"Remind me tomorrow before 9am to put the bins out"** now creates a real
  reminder that survives restarts and updates. Reminders can repeat daily, on
  weekdays, weekly or monthly; when your request is genuinely ambiguous about
  repeating, Ellen asks which you meant before setting it.
- **Cancel a reminder at any time**, and ask Ellen what is scheduled to see
  the ones you have.
- **A reminder due while Casa is restarting is no longer lost.** It arrives as
  soon as Casa is back, marked as late, instead of silently never coming.
- **Reminders survive Casa updates**, including updates that change the shipped
  default schedule for your agents.

### Fixed

- Reminders could previously be created as session-only timers that were
  quietly discarded at the next restart — after Ellen had told you they were
  set. That route is closed; reminders now always take the durable path.
- A one-off reminder for a specific date is now genuinely one-off. Dated
  reminders used to be stored as a yearly repeat that relied on a clean-up
  step to stop it coming back every year on the same day.

## [0.147.0] - 2026-08-02

### Changed

Authorization callbacks now survive restarts and missed pickups. Signing in
to an outside service used to depend on a plugin being awake at exactly the
right moment; it no longer does:

- **Every authorization gets a durable record the plugin can read whenever
  it next runs.** Casa keeps one small note per sign-in attempt beside the
  result — what was minted, what happened to it, and how it ended. A plugin
  that was not running when you tapped "allow" picks the story up later
  instead of losing it.
- **Delivery is retried until the plugin actually receives the
  authorization**, on a schedule tuned to the few minutes a provider's code
  stays valid, rather than being considered done the moment the message was
  handed off. If nobody ever collects it, you get one notification rather
  than silence.
- **Nothing is now thrown away silently.** An authorization that expires
  unread, is dropped under load, or fails to be written now says so in its
  record, so a plugin can tell "it expired" from "it never arrived" and ask
  you to try again for the right reason.
- **Removing a plugin reports the authorizations it aborted.** If a plugin
  is uninstalled while sign-ins are still in flight, Casa tells you how many
  were cut short instead of deleting them without a word.

Plugins that already use callbacks keep working unchanged; the identity of a
callback, the approval you gave it and the public URL are all untouched.

## [0.146.1] - 2026-08-02

### Fixed

- **Callback spool: uninstall + reinstall detection no longer trusts inode
  numbers.** Each plugin's callback spool directory now carries a random
  identity token minted when the directory is created; an OAuth redirect
  claimed before a plugin was removed and reinstalled is refused rather
  than deposited into the reinstalled plugin's spool. Previously this
  check compared the directory's inode number, which a filesystem is free
  to recycle — on ext4 (including the QA runners) a recreated directory
  routinely gets the same number back.
- CI is green again on real-disk filesystems: the QA test image build was
  missing `plugin_callbacks.py` in its bundle-build stage (the production
  image was fixed in 0.146.0's hotfix, the test image was not), and two
  reconcile tests asserted marker rewrites via inode numbers, which ext4
  recycles.

## [0.146.0] - 2026-08-02

### Added

Authorization callbacks — plugins can now receive OAuth-style browser
redirects at a stable public URL, so a plugin that needs you to sign in to
an outside service (a bank, Google) can complete that sign-in from your
phone:

- **A plugin declares the callback it needs at install time, and you
  approve it once in Telegram** — the same tap-to-approve flow as a plugin
  webhook. Nothing is wired by hand.
- **The callback URL is stable and public.** Set `public_url` to your
  add-on's HTTPS address (and publish the external API port through your
  reverse proxy); the plugin shows you the exact redirect URI to register
  with the provider, and it does not change when the plugin updates.
- **You stay in control.** A callback is dark until you approve it, an
  approval is withdrawn with the `callback_ack_revoke` tool, and the
  public page reveals nothing — every visit returns the same neutral
  "you can close this tab" response.
- Requires `public_url`: without it, plugins cannot be handed a callback URL,
  so the facility is unusable. See DOCS.md for the operator walkthrough.

## [0.145.0] - 2026-08-01

### Fixed

Stray-mediums batch — session resume, engagement lifecycle, topic output
ordering, guard parsing, and specialist role materialization (#301, #309,
#320, #332, #348, #355, #363):

- **A legacy session entry can no longer wedge a channel** (#309). A
  stored `last_active` timestamp without a timezone used to crash every
  turn for that channel key before a fresh session could start; it is now
  treated as an invalid entry and the channel starts fresh.
- **Deleting an engagement workspace is refused while the engagement is
  still live** (#301). If the forced cancellation cannot be persisted,
  the workspace and logs are left in place and the tool reports a
  retryable error instead of pulling files out from under a running
  service. The `force` flag now also requires a real boolean — the
  string `"false"` no longer counts as consent.
- **Two identical executor requests fired in parallel can no longer both
  launch** (#320). The duplicate-task check is re-run inside the creation
  critical section, so the second call gets the documented refusal and
  exactly one engagement does the work.
- **A cancelled engagement launch no longer leaves an orphaned Telegram
  topic** (#363). Cancellation anywhere in the topic-created-but-no-record
  window now closes the topic in the background.
- **Engagement topic output ordering fixes** (#332). The deferred-send
  timeout now starts when a send is actually armed (a slow validation no
  longer causes an immediate out-of-band post); a failed or timed-out
  discrete send no longer seals the open narration; and the turn's first
  output — narration, reply or ask — now reliably reply-threads to the
  operator's message, including after a transient send failure.
- **Pre-push and file-copy guard parsing** (#348). The self-containment
  guard now works out which repository a push targets by tokenizing the
  command the way a shell does, instead of pattern-matching it, so a `cd`
  written across a newline, behind a wrapper command, inside quotes or with
  redirections no longer hides the target; when the command is too tangled
  to model, the push is refused rather than waved through. Copying a file
  out of a managed tree with `cp -t /tmp <file>` is no longer misread as a
  write. This guard stays advisory by design — it can over-scan, it has a
  logged override, and the authoritative pre-push check is unchanged.
- **Specialists with an operator-selectable model now survive loading**
  (#355). Install froze the model at its default, so a non-default
  `PRIMARY_AGENT_MODEL` made the loader drop the specialist as a binding
  mismatch; install, upgrade, rollback and persona override now resolve
  the model exactly as the loader does. Doctrine sections no longer bleed
  across text/voice/webhook prompt projections, and agent-home
  `settings.json` is written atomically and preserved for repair when it
  does not parse.

## [0.144.0] - 2026-08-01

### Fixed

Install/workspace batch — plugin system requirements, specialist/persona
install staging, and the pending-configuration flow (#306, #308, #323,
#331, #354, #379):

- **A failed tool reinstall no longer destroys the working install**
  (#308). The tarball system-requirement installer used to delete the
  existing installation before its replacement had succeeded — a failed
  download, extraction or install command left the plugin's CLI broken
  until a manual reinstall. The replacement is now built alongside and
  swapped in only on full success; on any failure the previous install
  (and its launcher link) keeps working.
- **A malformed system-requirements declaration is now refused** (#354).
  Previously it silently read as "no requirements", so the plugin
  installed cleanly and only failed at runtime when its missing binary
  was invoked. Install/update now reject it with a clear error, and
  plugin verification shows it as a missing requirement.
- **One plugin can no longer overwrite another plugin's installed tool**
  (#354). All three install strategies now refuse to publish a launcher
  name that another plugin already owns, instead of silently repointing
  it.
- **Malformed executor hook entries no longer crash engagement startup**
  (#354). Odd shapes in a hooks file are skipped (the built-in guard is
  always emitted) instead of failing the engagement after its workspace
  was already created.
- **Install staging is cleaned up** (#306). Rejected or abandoned
  specialist and persona inspections used to leave full repository
  copies behind forever; they are now removed on failure, consumed on
  successful install, and swept at boot after seven days — unbounded
  disk growth on the config volume is gone.
- **Persona references are contained and consistent** (#323). A
  crafted persona reference can no longer load a pack from outside the
  approved directories; a pack must declare the identity its reference
  names; and the persona tools now resolve their directories through the
  same settings the loader uses, so applying a persona in a custom
  layout takes effect after restart instead of silently staging to the
  wrong place.
- **A specialist install that needs more configuration can now be
  completed** (#331). The first commit's consent receipt is retained
  while configuration is pending (it was deleted, making the follow-up
  impossible without a reinstall), and a retry that supplies only the
  still-missing settings keeps the ones already provided. A concurrent
  upgrade during a reload can no longer fail that reload with a
  spurious mismatch.
- Verified already fixed and closed: the fresh-install guard for pending
  specialists (#379, fixed in 0.134.0) and atomic-write directory
  durability (#354 sub-item, fixed in 0.138.0).

## [0.143.0] - 2026-08-01

### Fixed

Cancellation/shutdown batch — the message bus, container shutdown, boot
replay, drivers, and scheduling (#316, #342, #343, #344, #380):

- **Cancelling a request actually stops the work** (#343). A voice
  utterance (or any bus request) cancelled while still queued used to run
  its full turn anyway once the consumer got to it — tools, output and
  all, for a caller that was long gone. A cancelled queued message is now
  dropped on dequeue.
- **Evicting an agent cancels its in-flight work** (#343). Removing a
  role (deleting a resident, disabling a specialist) only stopped its
  queue consumer; handler tasks already dispatched kept running (and
  could keep sending as that role) after teardown reported complete.
  Eviction now cancels and drains them. A hot-swap reload deliberately
  still lets in-flight turns finish on the agent they started with.
- **Shutdown no longer strands late requests** (#316). A request arriving
  after the agent loops were cancelled but before the HTTP listeners
  closed used to hang until the bus timeout, stalling container shutdown
  up to aiohttp's bound. The bus now refuses new requests once shutdown
  begins and resolves any still-pending ones, so teardown stays prompt.
- **Boot replay refuses engagements whose service cannot start** (#342).
  A failed s6 service start — or a failed stdin-FIFO recreation, which
  guarantees a crash-looping service — now marks the engagement errored
  and skips it, instead of attaching messaging machinery to an engagement
  with no consumer.
- **Plugin-health alerts are no longer lost without Telegram** (#342).
  The health notifier treated "enqueued to the telegram target" as
  delivered, but that queue exists even when no Telegram channel is
  configured — so the one-shot alert was consumed unseen. Delivery is now
  a direct channel send, marked notified only on success; the topic
  permission reminder got the same treatment.
- **Numeric Sunday crons fire on Sunday** (#343). Five-field cron
  day-of-week numbers were passed to the scheduler verbatim, whose 3.x
  numbering starts at Monday — `0 9 * * 0` fired Monday. Numeric fields
  (including ranges, lists and steps) are now translated to day names.
- **A failed Home Assistant schema publication retries** (#343). If
  pushing a refreshed HA tool surface to the agent failed, later
  refreshes considered the surface unchanged and never republished,
  freezing the agent's HA tools until the next upstream change or a
  restart.
- **A cancelled s6 compile can no longer delete the live database**
  (#344). Cancelling during the compile/swap window ran cleanup while the
  worker thread carried on; if the swap then succeeded, cleanup removed
  the newly-live compiled database. Cleanup now runs inside the worker,
  beside the outcome it depends on.
- **A cancelled engagement launch closes its client** (#344). The in-casa
  driver's first-turn rollback caught only errors, so a cancelled launch
  leaked an open SDK client that later turns believed alive.
- **Executor `extra_dirs` are contained** (#344). Entries are now checked
  against the approved shared roots (`/share`, `/media`) instead of
  accepting any absolute path — a definition could previously grant an
  engagement read/write far outside its workspace (`/`, `/config`).
- **One bad workspace metadata file no longer halts the retention sweep**
  (#344), a specialist cannot silently shadow a same-named resident in
  the agent registry on reload (#343), and non-object JSON-RPC `params`/
  `arguments` now earn a typed invalid-params error instead of a crash
  (#342, #380).


## [0.142.0] - 2026-08-01

### Fixed

Config/reload batch — trigger re-registration, the scoped reload paths, and
the config-commit gate (#278, #279, #291, #307, #327, #351):

- **Replacing a role's triggers is fail-closed for real** (#307). When a
  later trigger in the replacement list was invalid, the earlier replacements
  were already installed and kept firing while the reload reported failure.
  A partial replacement is now unwound, so a failed re-registration leaves
  the role with no triggers — exactly what the error reports.
- **A resident added by a bulk agents reload gets its triggers** (#327).
  The add path wired config, bus queue and consumer but never registered the
  new resident's cron/interval/webhook triggers, so they silently never
  fired until a later per-role reload or restart.
- **A per-role reload reports a trigger failure instead of swallowing it**
  (#327). `scope=agent` used to return ok after a failed re-registration
  left the role with no triggers; it now surfaces `reregister_failed` (the
  agent swap itself stands, and the message says so). A full reload contains
  such a failure to that role instead of aborting the remaining roles.
- **Reconstructed specialists resolve their own plugins** (#327). Agents
  rebuilt by a reload were constructed against the pre-reload agent
  registry (which they retain for life), so a newly installed specialist
  missed its own plugin assignment and fell back to an empty set. Every
  reload path now builds the post-reload registry first and constructs
  against it.
- **Concurrent reloads of the same role can no longer race a cascade**
  (#327). The policies, executors and config-sync cascades now take the
  per-role/per-scope locks they fan out into, so a suspended per-role reload
  cannot overwrite a cascade's newer result while both report success.
- **A config commit validates exactly what it commits** (#351). The
  pre-commit schema gate used to validate the working tree and then stage
  and commit as a separate step — an edit landing in between (e.g. over SSH)
  was committed unvalidated and could fail the next boot. The gate now
  stages first, validates the staged snapshot, and commits only that.
- **A malformed plugin-env line no longer aborts boot** (#351). A
  hand-edited `=secret` line produced an empty variable name that crashed
  startup; invalid names are now skipped with a warning.
- **A failed executor rescan keeps the live registry** (#351). The rescan
  used to clear every executor definition before reading the directory, so
  a transient filesystem error deleted them all until a later successful
  reload; the scan now builds fresh state and swaps it in only on success.
- **Rollback can undo a file added after the target commit** (#351).
  Restoring a path that did not exist at the target used to error out;
  restoring to "absent" now removes the file and commits the removal.
- **The commit tool names the real tracked set** (#278). Its description and
  no-op warning had drifted behind the tracking whitelist — `bindings/` and
  the specialists registry/instance tuples were missing — sending agents
  that wrote there hunting for a nonexistent gitignore rule. Both strings
  now derive from one pinned source of truth.
- **Every option export is "null"-normalized** (#291). A key deleted from
  the stored options made bashio return the literal string `"null"`, which
  a few unconditional exports (1Password token and vault among them) passed
  through to truthy checks. Every read in the service run script now guards
  the sentinel, and a regression test forbids unguarded exports.
- **`log_level` ships an explicit `info` default** (#279). It was the one
  schema key with no `options:` entry; behavior is unchanged (absent still
  falls back to INFO) — the surface is just consistent now.

## [0.141.0] - 2026-08-01

### Fixed

Telegram/ask lifecycle batch — nine defects across message splitting, the
ask/permission keyboards, and delivery notices (#305, #322, #328, #347):

- **Long messages split by what Telegram actually counts** (#305). The plain
  splitter and streaming edits measured Python code points, but Telegram's
  4096 limit counts UTF-16 units — an emoji-heavy message could pass the
  local check and be rejected on send. All plain-path length checks now share
  the same UTF-16 measurement as the rich renderer.
- **Splitting no longer eats blank lines** (#305). A split at a newline used
  to strip every leading newline from the next chunk, silently deleting
  paragraph separation; exactly one newline is consumed now.
- **The authorization challenge shows bidi control characters instead of
  obeying them** (#328). A right-to-left override inside a tool argument
  could make the displayed "exact action" read differently from the value
  being approved; unsafe codepoints now render as visible `\uXXXX` escapes
  (the displayed block still parses to exactly the bound arguments).
- **The challenge size gate measures UTF-16 units** (#328), so an
  astral-heavy challenge can no longer pass the gate and then fail to post,
  permanently denying that action.
- **A queued turn no longer asks you to resend it** (#322). When the
  engagement's input pipe had no reader, the notice said "Try again" while
  the turn stayed queued for automatic redelivery — a resend ran the request
  twice. The notice now says the message is queued and will deliver
  automatically.
- **A rate-limited reply no longer expires the question it was answering**
  (#347). The "Slow down" drop used to also cancel the pending ask, losing
  both; the question now stays live for the next attempt.
- **Retrying an ask whose keyboard failed to post returns the failure
  immediately** (#347) instead of waiting the full timeout on a keyboard
  that will never appear (which could pause the engagement as unanswered).
- **No permission keyboard on a finished engagement** (#347). A permission
  request racing engagement completion could post a live Allow/Deny keyboard
  into a closed topic and wait out its timeout; it is refused instead.
- **A cancelled Telegram reconnect no longer leaks a running client** (#347),
  and a non-finite ask timeout is rejected instead of firing instantly.

Adversarial-review hardening found during the batch's six review rounds:

- **A cancelled reconnect in polling mode also stops the update poller** —
  without it the rolled-back client's poller kept running unreachably.
- **The ask question/options size check measures UTF-16 units** too, so an
  emoji-heavy question is refused up front instead of failing to post.
- **A permission request racing engagement completion is denied cleanly**
  instead of erroring internally and waiting out its timeout.
- **A finished engagement's topic can no longer be repainted as live**: topic
  state edits are serialized per engagement and a closed engagement refuses
  the green/yellow states, so the ✅/⏹/❌ marker always wins.
- **Oversized first streaming updates are held back** until the final
  message, which is split correctly, instead of being rejected by Telegram.

## [0.140.0] - 2026-08-01

### Fixed

Voice-correctness batch — nine defects across the voice channel's WebSocket
lifecycle, deferred delivery, and rendering (#303, #304, #329, #352, #357):

- **A retransmitted utterance no longer orphans the in-flight turn** (#303).
  Re-sending the same utterance id used to overwrite the internal task map
  while the first request kept running server-side, beyond the reach of any
  cancel; the original is now cancelled before the retry takes its place.
- **A malformed re-registration no longer silently unbinds a voice route**
  (#304). The previous binding survives a refused registration frame, and a
  socket that re-registers under a new route id now notifies delivery for the
  displaced one — so an answer already offered to that route is re-offered
  instead of expiring unsent.
- **An utterance pins its route identity at ingress** (#329). A registration
  frame racing an already-received utterance can no longer redirect that
  turn's deferred answer or specialist handoff to the new route.
- **A missed delivery authorization can be retried** (#329). If the client
  lost the socket between Casa authorizing a delivery and the authorization
  frame arriving, retrying the same attempt after reconnect now re-acks
  instead of revoking, so the answer plays without waiting out the lease.
- **Unacknowledged revoked delivery attempts are reclaimed** (#329) instead
  of accumulating (and pinning closed sockets) for the process lifetime.
- **Pending specialist handoffs are not replayed to a superseded socket**
  (#329) — replay stops as soon as another connection takes over the route.
- **A non-answer can no longer be delivered as silence** (#352). Every
  terminal specialist status — not just `answered` — now requires spoken
  content; an empty not-found/failed envelope is rejected and the standard
  spoken fallback is used.
- **The voice concierge hears how confident the specialist was** (#352).
  Synchronous delegation results now carry the specialist's machine-readable
  outcome status (plus citations and assumptions, under the same disclosure
  policy as the spoken text), so a tentative answer is no longer spoken as a
  confident one.
- **The `none` TTS dialect no longer deletes leading parentheticals** (#357).
  Only canonical prosody tags are stripped; substantive prose such as
  "(Important: the oven is still on.)" is spoken.
- **Overlong topic-title words are ellipsized** (#357) instead of being
  sliced mid-word, and **voice agent display names containing Unicode
  line/paragraph separators are rejected** (#357) so a configured name
  cannot forge a second line in the agent picker.

## [0.139.0] - 2026-08-01

### Security

Engagement-security batch — who may authorize a protected tool, and how an
engagement proves its identity on the hook path:

- **Protected plugin tools can now only be authorized by the configured
  operator** (#368). Previously the confirmation keyboard was answerable by
  whoever requested the call, so on an accept-all deployment
  (`telegram_chat_id` empty) any Telegram user could approve their own
  protected calls. A non-operator's protected call is now denied outright —
  no challenge is posted — and with `telegram_chat_id` empty protected tools
  are denied for every sender; the add-on logs a startup warning explaining
  the accept-all consequences.
- **Hook resolution now authenticates the engagement credential instead of
  trusting the caller's working directory** (#366). The hook proxy presents
  the per-engagement secret from its own workspace, and the resolver verifies
  it before selecting an executor's hook parameters or posting a permission
  keyboard — a forged working-directory claim can no longer surface a
  permission prompt in another engagement's Telegram topic or borrow another
  executor's hook configuration.

### Notes

- The pre-v0.137.0 config-digest residual (#372) was investigated and needs a
  designed digest-rotation migration; findings recorded on the issue.

## [0.138.0] - 2026-08-01

### Fixed

Driver-durability batch — five issues where a crash, a race, or a failed
write could lose an operator message, a specialist's answer, or a plugin:

- An engagement that repeatedly refused to finish could have its **fresh turn
  killed by a delayed forced restart**, silently losing the operator message
  that turn had just picked up. The forced restart is now single-flight and
  generation-guarded, and replayed control frames no longer trigger duplicate
  operator turns or phantom "abnormal exit" warnings.
- The engagement **inbound message queue is now crash-safe**: queued messages,
  capacity notices and delivery receipts survive a power loss, failed sends
  are retried instead of dropped, and the queue no longer grows without bound.
- **Suspended specialist conversations resume reliably** after a restart: a
  failed session-ID save is retried on the next message instead of being
  silently treated as saved (previously the conversation could not be resumed
  and looked like an orphan).
- A specialist's **finished answer is never discarded** when the final status
  write fails — the result is returned and the durable record is completed in
  the background. Cancellations and voice-deadline teardowns are equally
  durable, and an already-delivered voice job no longer replays its handoff
  on every reconnect.
- **Installed plugins survive a power loss**: published artifacts are fully
  synced to disk before anything references them, and the media-outbox
  cleanup can no longer delete a freshly published file it raced with.
- **Boot replay refuses to resume** an engagement whose workspace or pinned
  plugin artifacts are missing — previously an intact service entry could
  enter an endless restart loop.

## [0.137.0] - 2026-08-01

### Fixed / Security

Secrets-family batch — four issues where a secret value leaked into the wrong
channel, went stale, or got lost:

- **Specialist config can no longer carry secret plaintext** (#337, high): a
  config key the component's schema declares in `secret_names` is refused with
  a typed error instead of being persisted verbatim into the instance tuple
  under `/config` (which backups include). `secret_names_provided` now accepts
  only schema-declared secret names, and an upgrade strips legacy plaintext
  secret keys from the carried config snapshot.
- **Vault-backed `webhook_secret` works for voice** (#333): an `op://…` value
  is resolved before Supervisor discovery publishes it, so the companion
  integration signs voice requests with the same secret Casa verifies —
  previously every voice request failed HMAC until the secret was inlined.
- **1Password rotations take effect on reload** (#345): a plugin-env reload
  invalidates the resolver's cache instead of silently reusing the revoked
  credential. A missing `op` binary now degrades with a warning instead of
  aborting startup.
- **Session persistence robustness** (#345): a structurally corrupt
  `sessions.json` entry is quarantined at load instead of wedging the registry
  and both sweepers; a session save cancelled at shutdown releases its claim;
  a gap-superseded session whose background retain fails is spooled and
  retried durably by the freshness reaper instead of being lost.
- **`context7_api_key` accepts `op://` references** (#277), like every other
  password-typed option.

### Changed

- docs: the corpus manifest sharded into `docs/manifest.d/` (#367) — the root
  index had reached its 40 KB ceiling; the verifier, coverage ledger and CI
  now read the root plus shards.

## [0.136.0] - 2026-08-01

### Fixed / Security

Identity-family batch — three issues (#335, #336, #350) where something was
trusted for *who it claimed to be* rather than *who it proved to be*:

- **Engagement identity is now authenticated, not just claimed** (#335).
  Every engagement gets a per-engagement secret credential, provisioned only
  into its own workspace; the internal tool bridge and the engagement
  channel routes now verify it before acting with an engagement's authority.
  Previously, any in-container process that knew another engagement's id
  (ids appear in logs and configuration) could call privileged tools as that
  engagement — including configurator-only config commits. A known id with a
  missing or wrong credential is now rejected outright, the workspace
  inspection tool refuses to hand out the credential file, and both files
  that store a credential are no longer world-readable. In-flight
  engagements survive the upgrade: the credential is minted for existing
  records at boot, their workspaces are refreshed, and an engagement whose
  credential changed is restarted so it picks the new one up.
  Scope, stated plainly: this raises the bar from "know an id" to "hold a
  secret". It is not isolation between engagements — they all run as root in
  one container, so a shell-capable engagement can still read a sibling's
  credential file (#365), and hook resolution still identifies an engagement
  by working directory (#366).
- **Telegram senders are attributed individually, and private memory is
  reached only through the operator** (#336). Previously every accepted Telegram sender —
  including strangers, when `telegram_chat_id` is left empty ("accept all
  chats") — was recorded as the operator and recalled memory at the
  operator's private clearance. Now only the sender whose id matches the
  configured `telegram_chat_id` is the operator; any other sender is
  recorded under its own `telegram:<id>` identity, reads at public clearance
  only, and the agent is told the sender is not the operator. This holds for
  both ways a turn starts — a message and a button tap — and it follows the
  turn outward: a specialist it delegates to, an engagement it starts (and
  any engagement that one spawns), and the prior-engagement memory injected
  into an executor's prompt all read at that same lower clearance instead of
  the operator's private tier. With the option empty, no sender is treated
  as the operator — set your chat id to keep operator attribution.
  An engagement started after this release carries the clearance of whoever
  started it — and if anyone with a lower clearance later steers it by
  messaging its topic, it drops to *their* clearance and never climbs back,
  so it does not read above the least-privileged person taking part. Four
  limits worth knowing: engagements that were **already running** before this
  upgrade carry no such stamp, so they keep the old channel-wide clearance
  until you finish them (start a new engagement if that matters to you);
  lowering an engagement's clearance stops *new* private reads but does not
  erase what it already knows from earlier in the same engagement (#369); the
  drop is recorded on a best-effort write, so in the rare case that write
  fails, a restart restores the earlier clearance (the failure is logged);
  and a non-operator can still answer their own protected-tool confirmation
  prompt (#368).
- **A chatty memory-sensitivity classification can no longer leak a fact
  downward** (#350). The tier classifier's reply parser used to accept the
  first tier word found anywhere in the reply — so "this is not public; it
  is family" tagged a family fact *public* and made it recallable on
  public-clearance surfaces. The parser now accepts only a clean single-word
  answer; anything ambiguous falls back to the leak-safe private default.

## [0.135.0] - 2026-07-31

### Fixed

Race-family batch — four issues (#317, #319, #326, #353) closing
check-then-act windows where two things happening at once could lose a
session, resurrect finished work, or spam the operator:

- **A message sent right after `/new` no longer races the reset** (#317).
  The channel serializes `/new` with same-chat messages: once the reset is
  underway, a follow-up waits for it — it can neither resume the session
  being reset nor have its fresh session deleted by the reset's cleanup —
  and the reset's save and removal are additionally pinned to the exact
  session they snapshotted. Other chats are unaffected. (Two updates
  delivered near-simultaneously can still be ordered either way before the
  serialization applies.)
- **A `/new` reset now waits for an in-flight pool invalidation to finish
  flushing** (#319), so the saved transcript is complete and a finishing old
  turn can no longer re-register the session the reset just removed.
- **Finished engagements stay finished** (#326). The idle sweep and the
  resume-failure path can no longer overwrite a concurrently completed or
  cancelled engagement (no more resumable "zombie" topics or duplicate
  cleanup); creating an engagement now fails loudly if its crash-recovery
  record cannot be written, instead of running without one; and the
  two-strike resume-failure counter survives restarts.
- **Background sweeps no longer race live sessions** (#353). The hourly
  freshness reaper can no longer retain-and-delete a session that a new turn
  just replaced; the engagement observer's three-interjection cap holds even
  when several events fire at once (and declined evaluations still cost
  nothing); a Telegram reconnect triggered during an in-flight rebuild no
  longer tears the recovered transport down a second time; and a plugin
  health refresh can no longer erase the "already notified" marker and
  re-send the same alert.

## [0.134.0] - 2026-07-31

### Fixed

Boot-safety batch — four issues (#325, #338, #339, #346) closing paths where
an ordinary configuration edit, a damaged file or a race could stop the app
from starting or corrupt its rollback state:

- **Clearing an optional Telegram option no longer crash-loops the add-on**
  (#325). An unset optional yields the literal string "null" from the
  Supervisor config layer; the boot script now normalizes the bot token, chat
  id and engagement supergroup id like every other optional, and the
  supergroup id parse tolerates garbage (real, negative Telegram IDs are
  preserved).
- **A persona that fails prompt-admission ceilings can no longer be activated
  and brick every later boot** (#339). A resident binding candidate is now
  fully validated — compatibility, the pinned persona checksum, and the full
  compile pass — *before* it is promoted to active; a failing candidate is
  discarded and the last-known-good binding keeps running. Persona bytes that
  changed under a pinned version are refused instead of silently adopted, and
  the active/prior rotation can no longer lose the rollback generation on a
  failed write.
- **The pre-commit configuration gate now predicts boot** (#338). It replays
  trigger registration (duplicate names, undeclared channels, invalid cron
  fields are refused at commit instead of crash-looping the next boot), one
  inconsistent specialist no longer kills the whole specialist scan, and
  validating a commit can no longer activate a staged persona swap as a side
  effect.
- **Specialist install/upgrade robustness** (#346). Consent receipts are
  re-checked under the mutation lock (a concurrent duplicate bundle fails
  closed instead of desyncing rollback generations); a damaged installed
  specialist is isolated at boot as an error-state instance instead of
  aborting startup; a second fresh install can no longer silently replace a
  different pending install of the same slug; malformed fetched manifests
  surface as structured errors; the component-store path is containment-pinned
  against tampered tuple roots; and component export refuses corpus symlinks
  that would produce an uninstallable bundle.

## [0.133.0] - 2026-07-31

### Security

Four fixes hardening executor hook enforcement (the containment layer around
`claude_code` engagements such as the plugin developer and configurator):

- **An executor's `hooks_file:` pointer must now resolve to a valid hooks
  document.** Previously any present file satisfied the pointer — pointing it
  at a non-hooks file silently removed the executor's Bash-command and
  file-path containment policies with no error. The resolved file is now
  validated against the hooks schema when executors load, and a declared
  pointer whose target is missing is a load failure (the default `hooks.yaml`
  remains optional). A failing executor is reported and skipped; its siblings
  load normally.
- **Declaring the same hook policy twice now enforces both declarations on
  the HTTP enforcement path.** The SDK-side path already ran every
  declaration; the HTTP path kept only the last one, so a restriction
  declared earlier could be silently widened. Duplicate declarations now
  compose, and any declaration's refusal blocks the call on both paths.
- **Engagements of a disabled executor resumed after a restart keep their
  configured file-path scope.** They previously fell back to an empty default
  scope that denied every workspace read and write, bricking a legitimately
  resumed engagement.
- **Reloading executors now refreshes HTTP hook enforcement immediately.**
  The enforcement map was previously built once at boot: an executor added or
  edited via reload ran against stale (or deny-all default) policies until
  the add-on was restarted, and a tightened policy did not take effect until
  restart.

## [0.132.1] - 2026-07-31

### Fixed

- **An engagement turn no longer disappears from its Telegram topic after a
  restart that lands exactly on a log-segment rotation.** When a turn closed
  on the last frame of a log segment that then rotated to an archive, restart
  recovery opened the archive at its exact end, found nothing left to read
  there, and treated the entire next segment — the whole in-progress turn —
  as already-seen replay: nothing was posted to the topic, and resumption
  then skipped past the suppressed frames permanently. Recovery now
  recognizes that a scan starting at (or past) the checkpoint is already
  beyond it, and delivers the successor segment's turn normally.

## [0.132.0] - 2026-07-31

### Fixed

- **The 8:00 weekday morning briefing no longer sends an "all quiet" message
  when there is nothing to report.** The briefing prompt told the assistant to
  "stay silent," but a scheduled turn is only actually suppressed when its
  whole output is empty or the literal `<silent/>` sentinel — so a model that
  wrote a short "nothing to report" line (or even a sentence *saying* it was
  staying silent) had that prose delivered. The briefing prompt now teaches the
  sentinel explicitly, matching the hourly heartbeat trigger.
- **The silence gate tolerates whitespace and repeated sentinels.** A buffered
  turn whose output strips to nothing but one or more `<silent/>` sentinels
  (e.g. on its own line) is now suppressed, while any real text after a
  sentinel is still delivered (the recant contract is preserved).

Existing installs pick up the corrected briefing prompt automatically on
update via the config-sync reconciler (the file is an unmodified shipped
default).

## [0.131.0] - 2026-07-31

### Changed

- **Claude Agent SDK 0.2.114 → 0.2.128 and Claude Code CLI 2.1.150 → 2.1.220**
  (supersedes Dependabot #268; both pins move together — the in-process
  agents and `claude_code` engagements all run the same pinned CLI).
  Highlights from the range that matter for a long-running Casa install:
  memory-leak and performance fixes for long-lived sessions with many MCP
  tools; a hook timeout is no longer misreported as a user rejection (which
  could stall unattended agents); a batch of Bash permission-check bypasses
  was closed upstream; and the SDK now passes `--resume`/`--session-id`
  values injection-safe.
- **Subagent spawn depth is pinned to 1 for all agents.** The new CLI
  defaults to letting subagents spawn nested subagents up to depth 3.
  Casa roles that already deny subagent spawning are unaffected; the
  primary assistant does not deny it, so this pin keeps its pre-upgrade
  behavior (no nested spawning) rather than silently adopting the new
  default.

### Fixed

- **Engagement respawns validate the stored session id before resuming.**
  The respawn script used to word-split the `.session_id` file's content
  into the CLI's argument list; a corrupted or crafted file could inject
  extra CLI flags into the engagement's next spawn. The id is now accepted
  only as an exact UUID and passed as a single argument; anything else is
  ignored and the engagement starts a fresh session (with a notice in the
  app log).

## [0.130.0] - 2026-07-31

### Fixed

- **Secrets inside exception tracebacks no longer reach the logs.** Redaction
  used to inspect only a log line's message and arguments; a secret that ended
  up inside an exception message or traceback was written out verbatim at any
  log level. Casa's log formatters now redact exception text, stack text and
  structured log fields with the same rules as ordinary messages, so a
  credential passing through an error is masked wherever Casa writes its own
  logs. (#285)
- **Voice endpoints no longer crash on malformed input from an authenticated
  caller.** A family of edge cases turned a malformed request into a server
  error (or a dropped connection) instead of a clean refusal: a non-ASCII
  signature header, valid JSON whose top level is not an object, a voice
  route registration whose capability list contains a non-string entry, and
  request fields (prompt, agent role, scope, context) carrying the wrong
  type. All now get the proper "no" — 401, 400, a not-found, or a refused
  registration that leaves the connection open — and a malformed `context`
  no longer aborts `/invoke` turns either. (#287)
- **Cancelling an engagement now tells the truth when the cancellation could
  not be saved.** If writing the terminal state failed, the cancel tool still
  reported success while the engagement quietly stayed active. It now reports
  a retryable error — the same contract completion already had — so the caller
  knows to try again. (#289)
- **A memory-clearance docstring said the opposite of what the code does.**
  Unknown channels read at the *least* sensitive memory tier (fail-closed);
  the docstring claimed they defaulted to the most-trusted tier. The code was
  right, the sentence was wrong; the pair is now pinned by a test so they
  cannot drift apart again. (#282)

## [0.129.0] - 2026-07-28

### Added

- **Webhook and invoke turns now say who triggered them.** A turn arriving via
  `POST /webhook/{name}` or `POST /invoke/{agent}` used to be recorded with the
  generic, unattributed `system` identity — honest, but it lost which trigger
  fired. Each now carries its own identity: `/invoke` is recorded as
  `invoke_caller`, and every webhook trigger as `webhook:<its name>`, so two
  triggers are never confused for each other or for anything Casa said itself.
  (#204)
- **A new `automation` author kind.** Both of those surfaces are reached with a
  shared secret, which proves the caller holds a credential — never that a
  particular person wrote the text. Their turns are therefore recorded as
  automations rather than as people: honest about arriving from outside without
  claiming a human author or Casa's own authority. Recalled automation memories
  are introduced as "An automation reported: …" rather than being attributed to
  a person or to Casa. Neither surface is ever attributed to the operator.
- **Casa now refuses to start if any of its entry points cannot say who is
  speaking.** Every external entry point declares its identity in one place,
  checked at startup; an entry point that could only produce an anonymous or
  wrong author fails the boot loudly instead of quietly mislabelling months of
  memories. A single request that cannot be identified is rejected on its own
  rather than being let through unattributed. (#203)

### Changed

- Webhook trigger names are capped at 248 characters, and rejected with a
  config error rather than accepted and then failing on every request. Existing
  names are unaffected — the longest in any shipped configuration is far below
  the cap, and plugin-declared triggers were already limited to 64.

### Internal

- Per-turn author identity moved out of `channel_trust` into a new declarative
  `ingress_identity` table keyed by ingress route. The retired helper defaulted
  to the operator's peer for any channel absent from its map, so `/invoke` and
  `/webhook` would have inherited Nicola's identity by omission — third-party
  content permanently recorded as the operator's own words. Peers are now
  declared per route with no default at all.
- `automation_document_id` gives automation memories their own content-addressed
  id space (`m-x-`), domain-separated from the user (`m-`) and agent (`m-a-`)
  spaces. Without it, the agent id scheme would have folded every trigger into a
  single document by discarding `user_peer`, and an automation naming itself
  after the operator could have upserted over the operator's own memories.
- Downgrading to a build without the `automation` kind is fail-safe, not fatal:
  the older parser rejects the unknown kind and `_decode_provenance` treats the
  entry as absent, so such a session is simply treated as legacy and starts
  fresh. Rollback loses webhook/invoke attribution and nothing else.

## [0.128.0] - 2026-07-28

### Added

- **Documented that reinstalling Casa changes its container name.** Home
  Assistant derives the container name and network alias from the repository
  URL and the slug, so both change on a reinstall from a different repository.
  Anything reaching Casa by name — most often a reverse proxy in front of the
  external API port, which is not host-published by default — then returns a
  bare `502` on every request, while Casa itself is healthy and its logs are
  clean. DOCS.md now explains the symptom and how to find the current name.

### Fixed

- The local QA harness still referenced three add-on options removed in
  v0.125.0, and one of them changed its behaviour: since webhook
  authentication became mandatory, the harness's `webhook_auth_enabled` branch
  was deleting the webhook secret and booting test containers into a state a
  real installation can no longer be in. The harness now mirrors the shipped
  boot script exactly, including regenerating a secret that is missing,
  zero-byte, or literally `"null"`. No effect on a running installation.
- Two local-only test failures that read as product regressions: the voice
  WebSocket smoke test invoked the system Python, which usually lacks
  `aiohttp`, and the concurrency probe measured elapsed time with a `date`
  flag that uutils coreutils silently ignores — producing nonsense timings
  that the failure message blamed on the concurrency semaphore.

### Internal

- A unit test now fails when a harness fixture or the harness env-export still
  references an option absent from the add-on's schema, so the next option
  removal cannot leave this behind silently.

## [0.127.0] - 2026-07-27

### Changed

- **Casa is now just "Casa" everywhere.** The app was called "Casa Agent" with
  the slug `casa-agent`, while its Home Assistant integration was already plain
  `casa`. The app now matches: it appears as **Casa** in Settings → Apps, its
  slug is `casa`, and its container image is `ghcr.io/bonzanni/casa`. Both
  repositories also move to the naming convention used across the rest of the
  Home Assistant projects here — `ha-casa-app` and `ha-casa-integration`.
- **This release requires a fresh install; there is no upgrade path.** Home
  Assistant identifies an installed app by its repository *and* its slug, so
  changing either makes this a different app as far as the Supervisor is
  concerned. An existing installation cannot be migrated in place: remove the
  old app and its repository, add the new repository, and install Casa again.
  Saved options must be re-entered — no configuration keys changed, so the same
  values apply.
- **The integration is unaffected in behaviour.** Its domain, entities and
  services are unchanged; only its repository URL moves. Update the HACS custom
  repository to the new address to keep receiving updates.

## [0.126.2] - 2026-07-27

### Changed

- When an agent needs your approval for a protected action, it should no longer
  add a line of its own about it — you get the approval message with the Approve
  and Deny buttons and nothing more. Previously an agent might also send
  something like "Sent you an approval prompt — tap Approve when you're ready",
  which was already against its instructions and read as stale or simply wrong
  if you had already tapped. The refusal an agent receives internally used to
  describe your buttons to it, which is what invited it to repeat them; it now
  tells the agent to stay quiet instead. Agents follow instructions rather than
  rules we can enforce, so this makes the extra line much less likely rather
  than impossible (#221).

## [0.126.1] - 2026-07-26

### Fixed

- Spoken replies no longer lose the space between sentences. Every voice agent
  came back with sentences welded together — "Yep, I'm here.What's on your
  mind?" — in the Assist transcript and in what some voices read aloud. Casa
  streams a reply in prosodic blocks, one sentence at a time, and dropped the
  whitespace between them as noise; Home Assistant then joined the blocks back
  into one message with nothing in between. The separator now travels with the
  block, so a reply reads back exactly as the agent wrote it — including the
  spaces around an em dash, paragraph breaks, and the case where a sentence is
  cut mid-word at the length limit, where no space is invented (#257).

## [0.126.0] - 2026-07-26

### Fixed

- Clearing the "Primary agent model" or "Voice agent model" field in the add-on
  configuration no longer stops Casa from starting. Both fields are optional, so
  emptying one leaves it unset — and Casa was passing the resulting placeholder
  through as if it were a model name, which every agent then rejected as invalid
  on startup. A cleared field now falls back to its default (Opus and Haiku
  respectively), the same way the configuration validator already did. If you
  ever cleared one of these fields and the add-on would not boot, this was why
  (#205).

### Added

- Executors (the Configurator and Plugin Developer) now refuse to start if their
  editable definition file names a different model than Casa's built-in
  definition for that executor. The definition file is the one an agent can edit
  on your behalf, and its model setting was the value actually used — so an
  incorrect edit could quietly run an executor on a model it was never meant to
  use, at a different cost and capability. Casa already guarded that file's tool
  permissions this way; the model setting now gets the same treatment. A
  mismatch disables only the affected executor and is reported in the log (#205).

### Changed

- A specialist's long-term memory lookups are now tracked separately from other
  memory lookups, so a memory problem affecting one is easier to identify and no
  longer counts against the others' failure budget (#205).

## [0.125.2] - 2026-07-26

### Fixed

- Asking an assistant's helper a malformed question no longer comes back as
  "there's nothing in memory about that". A blank question skipped the memory
  search entirely but was still reported as a completed search that found
  nothing, so the helper could tell you Casa remembers nothing about a subject
  it was never asked about. A blank question is now rejected as a mistake, and
  says so (#201).

### Changed

- Casa's prompt-assembly recall paths now state, and enforce in tests, the rule
  that makes them safe: when Casa cannot reach long-term memory while preparing
  an engagement, it stays silent about memory rather than telling the agent
  there is nothing to remember. Saying "no prior engagements" because the store
  was briefly unreachable would be a false claim; omitting the section is not.
  A test now fails if any executor prompt Casa ships is given wording — or a
  section heading — that turns that silence into a claim. Prompts you have
  edited yourself are not covered by that check (#201).

## [0.125.1] - 2026-07-26

### Fixed

- Casa failed to start on 0.125.0 when a Telegram bot token is configured.
  Removing the `telegram_delivery_mode` option left one reference to the
  deleted value behind, in the log line that records the Telegram channel
  starting, and reaching it crashed the add-on before it finished booting.
  0.125.0 is withdrawn; upgrade straight to 0.125.1.

## [0.125.0] - 2026-07-26

### Removed

- Nine add-on options that were not decisions an operator can meaningfully
  make. Casa's configuration screen is now 18 options instead of 27, and each
  removed option's alternative code path is gone with it rather than left
  unreachable:
  - `sdk_client_pool`, `tina_ha_facade_enabled`, `telegram_rich_text` and
    `telegram_delivery_mode` were kill-switches kept as rollback insurance
    when those features shipped. That insurance period is over: warm SDK
    clients, Tina's ready Home Assistant tools, Markdown replies and
    streaming Telegram delivery are now simply how Casa works.
  - `voice_turn_budget_seconds`, `voice_route_freshness_seconds`,
    `voice_job_delivery_ttl_seconds` and `voice_job_route_cap` were internal
    tuning constants. The turn budget in particular was already hard-capped,
    so every value other than the default could only shorten a voice turn and
    starve specialist hand-offs. The budget is now derived from the voice
    transport's own timeout; the rest are fixed at their previous defaults.

### Changed

- **Webhook and voice authentication is now always on.** The
  `webhook_auth_enabled` option is removed and Casa always has a secret —
  the operator's `webhook_secret` override when set, otherwise one generated
  in `/data/webhook_secret`. Every external route already refused unsigned
  requests (v0.116.0, v0.117.0), so the toggle's only remaining effect was to
  turn webhooks, `/invoke` and voice off entirely, which is a broken install
  rather than a preference.

### Fixed

- The webhook secret could be exported as the literal string `"null"` when
  authentication was enabled with no override set. Home Assistant returns
  that sentinel for an unset optional value, and Casa treats a non-empty
  exported secret as authoritative over the generated one — so signing used
  the generated secret while verification used the word "null" and nothing
  verified. Found while removing the toggle above.
- A webhook secret left empty by an interrupted first start is now regenerated
  instead of being trusted forever. Casa only checked that the secret file
  existed, but the file is created before it is written — so a container
  stopped at exactly the wrong moment left a zero-byte secret that every later
  boot accepted. With authentication now mandatory that would reject every
  webhook, invoke and voice request with no way to turn it off. The secret is
  also written to a temporary file and moved into place, so the window in
  which it can be empty no longer exists.

### Upgrading

- No action required. The nine removed keys are pruned from stored
  configuration on the next boot, so Home Assistant stops reporting them as
  unknown options.
- If you had deliberately disabled any of the four kill-switches above, that
  setting no longer applies; if the feature it disabled causes you a problem,
  please open an issue rather than pinning an old version.

## [0.124.1] - 2026-07-25

### Fixed

- Test-only: removed a class of timing-sensitive waits from the engagement
  ask-gate tests that failed intermittently on slower continuous-integration
  machines. No app behaviour changes.

## [0.124.0] - 2026-07-25

### Fixed

- Specialists are now told, in the request itself, exactly which result fields
  a spoken answer must come back with — and that this replaces any result
  format their own instructions describe. A specialist whose own instructions
  told it to answer as free text could previously end its turn without ever
  filling in the fields Casa needs, and the answer was thrown away without the
  person who asked ever hearing why.
- A job that Casa's assistant runtime gave up on — because it ran out of
  turns, hit a spend ceiling, or could not produce a usable answer after
  several attempts — is no longer reported as "the specialist returned an
  invalid result". Each of those has a different remedy, and they now say so
  separately in the log and in the job record.

## [0.123.0] - 2026-07-25

### Fixed

- The voice hand-off diagnostic reported on a capability that no longer
  exists, so it always read as missing. It is the line used to work out why a
  specialist hand-off was refused, and a permanently-failing field in it points
  at a cause that cannot be true.

## [0.122.0] - 2026-07-25

### Fixed

- When a specialist answers in a shape Casa cannot accept, the log now says
  which part was wrong. It previously recorded only that the result was
  invalid, which was not enough to tell whether the specialist had misbehaved
  or the request had been malformed. The explanation names fields only and
  never includes the specialist's own output.

## [0.121.0] - 2026-07-25

### Fixed

- Asking a voice agent something that needs a specialist from a phone or tablet
  no longer ends in silence. Casa used to promise "I'll read it out when it
  lands" on any device, then try to speak the answer through an Assist
  satellite — which a phone does not have — and the finished answer was
  discarded. Casa now works out, when the question is asked, what the asking
  device can actually receive: it speaks the answer on a voice satellite, sends
  it as a notification to a phone or tablet, and on a device that can do
  neither it says up front that it cannot follow up rather than promising an
  answer that never arrives.

### Changed

- The acknowledgement and the follow-up announcement are worded to match how
  the answer will actually arrive, and vary between requests instead of
  repeating one fixed sentence.

### Upgrading

- Requires Casa Home Assistant integration **v0.8.0**. Deferred voice answers
  use a new delivery contract; upgrade Casa first, then the integration. Voice
  delegation does not deliver while the two versions are mismatched.
- Deferred answers are a WebSocket-transport feature. Agents configured for SSE
  acknowledge and complete as before, without deferred delivery.

## [0.120.0] - 2026-07-25

### Fixed

- A voice agent's specialist answer is now actually spoken back. The
  acknowledgement ("I'll ask Judge…") was playing, the specialist was finishing
  correctly — and then the finished answer was silently discarded, because the
  Home Assistant integration's confirmation of the hand-off was rejected over a
  mismatched message format and the hand-off never completed. The message
  contract is fixed on both sides, and every rejection along that path is now
  logged with a reason instead of being dropped in silence.
  **Requires the companion integration v0.7.0 or later** (update it in HACS).

### Changed

- Voice hand-off wording now sets expectations: Casa says it may take up to a
  minute, and the answer is attributed when it arrives ("Judge says: …") so it
  makes sense a minute after you asked. Privacy wording is unchanged: results
  Casa may not read out are still announced with the same protected phrasing.

## [0.119.0] - 2026-07-25

### Fixed

- Asking a voice agent a Magic: The Gathering question now works. Gary was
  passing an unrecognised value for the delegation's delivery mode, and because
  the mode was accepted as free-form text that single value silently bypassed
  the whole voice hand-off policy: no background hand-off was created, so the
  immediate spoken "I will ask ..." acknowledgement never played (the caller
  heard ~25 seconds of silence) and the specialist's answer was cut off by the
  voice turn budget instead of being delivered by the speaker. The mode is now
  a closed set the model cannot deviate from, and an unrecognised value is
  corrected to the documented default with a warning rather than carried
  through. On a capable speaker, an MTG question is now acknowledged straight
  away and answered out loud when the specialist finishes (#233, #224).

## [0.118.0] - 2026-07-25

### Added

- Diagnostics for voice specialist hand-offs. When a voice agent hands a
  question to a specialist, the log now records which delivery route was
  chosen and why, plus where a delegation spent its time (startup, connection,
  first reply, first tool call) — including when it is cancelled for exceeding
  the voice turn budget, which previously left no trace at all. Only timings,
  routing facts and tool names are recorded; never the question, the answer, or
  any tool input. Behaviour is unchanged; this is groundwork for fixing the
  silent-wait and hand-off issues (#233, #224).

## [0.117.0] - 2026-07-24

### Security

- The voice endpoints (`POST /api/converse` and the `/api/converse/ws`
  WebSocket) now reject every request when no webhook secret is configured,
  instead of accepting unsigned ones. Because the external API port can be
  published, an unsigned voice turn was a way to reach an agent that can drive
  Home Assistant without any credential. Voice now behaves like `/invoke`,
  `/telegram/update` and voice-agent discovery: no secret means the route is
  off (`401`). **Voice requires `webhook_auth_enabled`.** The companion Home
  Assistant integration signs every request and cannot be set up without a
  secret, so existing voice installations are unaffected; a hand-rolled client
  that posted unsigned turns must now sign them (#193).

## [0.116.0] - 2026-07-24

### Security

- The `POST /telegram/update` webhook endpoint now rejects requests when no
  webhook secret is configured, instead of accepting them. Previously, with
  webhook authentication disabled, a forged Telegram update posted to this
  endpoint would reach the assistant. The endpoint now fails closed (403) with
  no secret — matching the `/invoke` endpoint — which is safe because the
  Telegram webhook transport always carries a secret token, and in polling mode
  the endpoint is unused. Set `webhook_auth_enabled` (and a secret) to use the
  Telegram webhook transport. Part of #193; the voice-channel siblings in that
  issue are tracked separately pending a routing decision.

## [0.115.0] - 2026-07-24

### Fixed

- Updating a plugin that both wires an external service and declares webhook
  triggers no longer redundantly re-hands its setup step back to the agent at
  completion. The `plugin_update` result reported whether Casa now owns the
  plugin's post-consent setup by re-reading the freshly-resolved plugin
  snapshot, which could momentarily still be the pre-update artifact — so the
  flag read false and the mechanical de-duplication (added in 0.112.0) didn't
  engage. The result is now computed from the manifest of the artifact the
  update just published. (Harmless before — the setup tool is idempotent and
  the post-consent hook still ran it exactly once — but the de-duplication is
  now reliable.) Also fixes a latent error that always forced the flag false
  even when the snapshot was current (#241).

## [0.114.0] - 2026-07-24

### Fixed

- Adding or updating a plugin no longer logs a spurious warning and a
  `scope_required` reload error at the end of the engagement. A plugin
  mutation activates its change in-process (reload + reconcile) before the
  configurator persists it to git, so the completion-time safety guard saw an
  "un-activated commit" and force-called a reload that (a) was redundant — the
  change was already live — and (b) failed because it passed no reload scope.
  The guard now recognizes that the plugin mutation already activated the
  change and skips the redundant reload; and when the guard does legitimately
  need to reload (a config change committed without one), it now reloads with
  the `full` scope instead of erroring (#231, #222).

## [0.113.0] - 2026-07-24

### Security

- The Telegram webhook secret token is no longer written to container logs
  when the log level is set to debug. python-telegram-bot logs each Bot API
  call's parameters — including the `setWebhook` secret token — as a
  structured payload, which the log redactor previously only scrubbed for
  plain-text secrets and so let the token through. The redactor now masks
  credential-named fields inside structured log data (and the token is
  registered for exact masking as a second layer), so a debug-level log can
  no longer expose it (#214).

## [0.112.0] - 2026-07-24

### Added

- Plugins that wire an external service (like the ElevenLabs voicemail
  integration) can now declare their setup tool in the manifest
  (`casa.setupTool`); approving the plugin's webhook consent hands that
  setup to the responsible agent automatically — installing or updating
  such a plugin needs zero follow-up input to become fully live. The
  hand-off is queued durably through its dispatch to the agent (it
  survives restarts up to that point and fires once per fully-approved
  consent round); the agent then runs the tool and reports the outcome,
  and any failure is reported with a manual fallback. The manual setup
  tool remains available for recovery.

## [0.111.0] - 2026-07-24

### Added

- The configurator can now remove a plugin environment entry
  (`remove_plugin_env_reference`) — when a plugin update makes a variable
  optional or a key moves to an add-on option, cleanup no longer needs
  manual file editing. Removal takes effect on the plugin-env reload, same
  as setting one (#236).

## [0.110.0] - 2026-07-24

### Added

- New `context7_api_key` option: provisions the API key for the bundled
  Context7 documentation plugin (part of the plugin-developer toolbox). A
  fresh install now brings Context7 up from the add-on configuration alone —
  no manual plugin-env editing. An existing plugin env entry, if present,
  still takes precedence (#232).

## [0.109.0] - 2026-07-24

### Fixed

- Literal `**` markers and unformatted tables no longer appear in several
  Telegram surfaces that skipped rich-text rendering: the assistant's DM
  questions with tappable buttons (and their settle edits), engagement
  completion summaries, engagement notices, and keyboard-bearing topic
  posts now all render markdown as proper formatting.
- Very long engagement completion summaries now arrive as several
  formatted messages instead of one message with raw markdown.
- Bordered tables without a `|---|` separator row are now recognized and
  shown in aligned monospace (three or more consistent rows).

## [0.108.0] - 2026-07-24

### Removed

- The `primary_agent_name` and `voice_agent_name` options. Agent identity
  (name, pronouns, persona) is owned by installed personas since the
  personality system shipped; these options only influenced the built-in
  fallback identity used when no persona is applied, and that fallback is
  now fixed to Ellen (assistant) and Tina (butler/voice). If you had set
  custom names here without a persona, install a persona to name your
  agents. Stored values of the removed options are pruned automatically on
  the next start (#227).

## [0.107.0] - 2026-07-24

### Changed

- Installing or updating a plugin that ships a setup tool (one that wires an
  external service to Casa, like the ElevenLabs voicemail integration) now
  runs that setup automatically as part of the same install flow — you no
  longer need to ask for setup afterwards to make the integration live. The
  manual setup tool remains available for recovery.

## [0.106.0] - 2026-07-24

### Changed

- Repository naming is now uniform across the Casa ecosystem: specialist
  repositories follow `casa-specialist-<name>` (matching the existing
  `casa-plugin-<name>` convention), and all repositories use `main` as their
  default branch. Old repository names keep working — GitHub redirects them —
  but examples in the docs and the assistant's install guidance now show the
  new names.

## [0.105.0] - 2026-07-24

### Added

- **Specialists are now self-contained packages: one repo, one install, one
  consent.** A specialist's repository can bundle the plugins it needs (or
  declare them by repository reference), and installing the specialist installs
  everything in a single flow with a single approval tap — no separate
  plugin-install step, ever. Bundle-installed plugins are private to their
  specialist and are removed automatically when the specialist is uninstalled;
  plugins you install yourself remain yours and are never touched by a
  specialist's lifecycle.
- The install approval message now lists exactly what each bundled plugin
  brings: its tools, protected tools, and the secrets it will need — so you see
  the full surface of what one tap approves.
- Installs, upgrades, rollbacks, and uninstalls are now crash-safe: every
  bundle operation is journaled and reconciled on the next boot, so a power cut
  mid-install can never leave the system half-configured.

### Changed

- Specialist identifiers (slugs) are now bounded to 32 characters. All existing
  specialists are well within the bound; a hypothetical pre-existing install
  with a longer name would need a reinstall under a shorter one.
- Plugin health now reports which plugins belong to which specialist and
  surfaces any bundle that was quarantined by boot-time recovery.

## [0.104.0] - 2026-07-23

### Fixed

- **Voice control of Home Assistant devices works again.** The voice butler
  (Tina) could not see any devices — lights, switches, everything — when asked
  to control them by voice. Home Assistant changed the shape of its live-context
  response, and Casa's filter was still written for the old shape, so it discarded
  the entire device list. The filter now understands the current format and passes
  the device overview through, with a regression test pinned to the real shape.

## [0.103.0] - 2026-07-23

### Fixed

- **The assistant can now install specialists, plugins, and personas from their
  repositories.** Its routing guidance still described the pre-install-from-repo
  world and literally told it to say installing a component "is not yet
  supported" — so a request like "install the finance specialist from its repo"
  was declined instead of handed to the configurator. The doctrine is
  reconciled: repository installs (and specialist upgrades/rollbacks/uninstalls,
  plugin add/update/remove, and persona install/apply/reset) route to the
  configurator, while building a brand-new plugin from scratch still routes to
  the plugin-developer — the two are kept clearly distinct, and read-only
  configuration questions are still answered directly.
- Reconciled several stale configurator instructions to match how the system
  actually works today: installed specialists are managed components (not
  hand-edited directories), specialist install/uninstall require an explicit
  reload, wiring/unwiring a delegate during an install/uninstall no longer ends
  the task early, and the install recipes no longer tell the operator to send a
  second message after approving — a single approval now carries the install
  through (v0.102.0).

### Fixed

- **Tapping "Approve" on an install now finishes the job.** When you asked a
  specialist or persona to be installed from a repository and approved the
  consent prompt, the approval was recorded but the assistant sometimes waited
  for a second nudge before actually installing. Now a single Approve resumes
  the work and carries it through — commit, wiring, and reload — with no extra
  message. Under the hood the approval and the paused task are joined through
  one shared, lock-serialized resume path, so a stray follow-up message or a
  cancel can't start the install twice or revive a finished one.

## [0.101.0] - 2026-07-23

Install-from-repo, first try. A fresh-install verification run exposed why a
specialist install through chat could go wrong; this release fixes the whole
chain, from the tool the configurator sees to the doctrine it reads.

### Fixed

- **The configurator now actually has the install tools.** The specialist and
  persona pipeline tools were declared on the configurator's role but never in
  the runtime allowlist it is loaded with — so a chat-driven "install this
  specialist from its repository" could never take the consented pipeline path.
  The runtime list now carries all ten tools, and a test keeps the two lists
  identical forever.
- `plugin_add`/`plugin_assign` no longer report failure for the documented
  plugin-before-specialist install order: a target specialist that is not
  installed yet is reported as `pending_targets` (with a self-clearing
  `target_pending` health warning), not a reload error.
- Boot health reports no longer flag installed specialists with false schema
  errors: config-sync's per-file validation skips the pipeline-managed
  specialist directories (the digest-verified specialist loader is their
  authority).
- Six configurator recipes staged commit/reload/completion in the wrong order;
  all now follow the canonical commit → reload → complete sequence, enforced by
  a mechanical test over every recipe.
- Specialist and persona install inspections no longer report success when the
  operator consent prompt could not actually be delivered: the tools now verify
  the consent keyboard posted (or that consent was already granted for exactly
  this content) and return a precise, per-cause failure otherwise — instead of
  leaving the install waiting forever for a tap that was never requested.

### Changed

- **The configurator no longer has a shell.** It never needed one: files are
  edited with typed file tools, searches use the search tools, and component
  state is managed by the install pipeline. Removing shell access closes a
  whole class of policy-bypass shapes that command inspection cannot.
- **Managed component state is write-protected in depth.** A new, always-on
  guard denies hand-edits of specialist, plugin, persona, and binding state
  (and of hook-policy files) for executor agents, with denial messages that
  point to the correct typed tool and recipe. The guard is enforced in code on
  both executor drivers — configuration files can add policies but never remove
  this one.
- The configurator's system prompt now carries a recipe index with a
  mandatory-recipe rule, and five stale recipes that described the pre-0.101
  hand-authored world (create/update/delete specialist, create/delete resident)
  are retired stubs that redirect to the supported flows.

### Security

- Executor capability lists are clamped to the image-shipped role ceiling at
  load time, and executors without a matching image role refuse to load — an
  executor's editable configuration can narrow its tools but never extend them.

## [0.100.2] - 2026-07-22

### Fixed

- No functional changes. Reconciled the container e2e delegation suite to
  the v0.100.0 zero-bundled-specialists contract: a fresh boot now asserts
  an empty specialist set, and a leftover pre-0.100 specialist directory in
  user config is verified to fail loudly per-slug without breaking boot
  (the state an upgraded installation is in until the specialist is
  reinstalled from its repository).

## [0.100.1] - 2026-07-22

### Fixed

- No functional changes. Hardened three timing-sensitive tests that could
  fail on slow CI runners (wall-clock bounds and fixed sleeps replaced with
  condition waits), restoring a reliable release gate.

## [0.100.0] - 2026-07-22

Personality Phase A: residents with swappable personas, and specialists
installed from repositories instead of the app image.

### Added

- **Personas.** Each of the three residents (the assistant, the butler,
  and the new concierge) now serves with a compiled persona — a versioned,
  swappable identity pack that defines its name, voice, and character
  traits. Ellen, Tina, and Gary are the defaults; swapping a persona is an
  explicit operation and takes effect on restart, never mid-conversation.
- **The concierge (Gary) is a day-one resident**, alongside the assistant
  and the butler.
- **Speaker attribution in memory.** Memories now record who was serving
  when they were written, and recalled memories carry that attribution —
  so after a persona swap, older memories still name the persona that
  originally handled them.

### Changed

- Specialists are now installed from a component repository instead of
  being bundled with the app image. The `finance` specialist and a new
  Magic — The Gathering rules judge (`mtg`) move to their own repositories;
  reinstall `finance` via the configurator's install recipe
  (`specialist_install_inspect` → `specialist_install_commit`). The
  configurator's toolset also covers `specialist_upgrade`,
  `specialist_rollback`, and `specialist_uninstall` for an installed
  specialist, plus a `persona_install_inspect` / `persona_install_commit` /
  `persona_apply` flow for adopting a specialist's bundled default persona
  or swapping in another.

## [0.99.0] - 2026-07-20

Memory failures are no longer mistaken for "no memories found".

### Fixed

- **Agents no longer deny knowledge they have when the memory backend
  fails.** Previously every recall failure (timeout, 5xx, transport error)
  was silently reported as an empty search result, so an agent would
  confidently tell you Casa has no such information — while the memory
  backend was merely down or slow (measured at a 75–80% failure rate under
  load on 2026-07-20). Recall now has three distinct outcomes: found,
  genuinely nothing found, and *memory unavailable*. Agents are instructed
  to say memory could not be checked — never that the information doesn't
  exist — unless a search actually ran and came back empty.

### Changed

- The `recall_memory` tool returns `status: unavailable` (instead of a
  misleading `status: ok` with an empty result) when memory can't be
  checked; the same distinction flows through delegated agents, executor
  engagements, and `query_engager`.
- The automatic pre-turn recall runs under a short 5s deadline instead of
  stalling prompt construction for the full 20s HTTP timeout, never retries
  an overloaded backend, and a circuit breaker stops recall attempts after
  3 consecutive failures (recovering via a single probe once per minute).
- Recall outcomes are logged distinguishably with latency (query text and
  recalled content are never logged), so backend failure rates are now
  measurable from the logs.

## [0.98.2] - 2026-07-19

### Fixed

- Approving a plugin webhook trigger's consent now refreshes plugin health
  immediately. Previously the trigger routed correctly on approval, but the
  health report kept showing it as "pending consent" (and could re-nag) until
  the next plugin change or restart. Found live while wiring the first
  plugin-declared trigger.

## [0.98.1] - 2026-07-19

### Fixed

- The v0.98.0 image build failed: the plugin-bundle build stage's narrow
  file set was missing the new `plugin_triggers.py` module that publish-time
  trigger validation imports. No runtime behavior change.

## [0.98.0] - 2026-07-19

Plugins can now declare webhook triggers — gated by a one-time consent tap.

### Added

- **Plugin-declared webhook triggers.** A plugin's manifest may carry
  `casa.triggers`: webhook triggers targeting one resident each, served at
  `POST /webhook/plg-<plugin>--<name>` by the existing authenticated wildcard
  handler. Declarations are validated at publish time (bad ones refuse the
  install), and the `plg-` name prefix is reserved so plugin and user trigger
  names can never collide.
- **Operator consent gate.** A plugin trigger routes only after you tap
  Approve on a one-time DM ("Plugin X wants to open POST /webhook/… →
  assistant"). Consent is bound to the exact plugin artifact, trigger name,
  target, and auth policy — a plugin update re-prompts and rotates the
  trigger's secret. Until everything lines up (install, assignment, the
  resident's `webhook` channel, consent) the endpoint 404s and plugin health
  shows why (`trigger_pending_ack`, `trigger_channel_missing`, …).
- **`trigger_ack_revoke` tool.** The off-switch: revokes a plugin's trigger
  consent, unroutes its endpoints immediately, and retires its secrets —
  a later re-approval always mints fresh ones.
- Per-trigger secrets for plugin triggers are minted eagerly at consent time
  (readable at `/data/webhook_secrets/plg-…` for provider setup) and bound
  to the exact approval they were minted under — a plugin update, a
  revoke + re-approval, or any policy change always rekeys; a new version
  never inherits the old one's credentials.

### Changed

- Plugin lifecycle mutations and trigger/agent reloads now re-derive plugin
  trigger routing as their last step (a resident losing its `webhook` channel
  unroutes that plugin ingress), and plugin health recomputes trigger state
  on every refresh.

## [0.97.0] - 2026-07-19

Webhook triggers get per-trigger authentication, and webhook-origin turns run
contained.

### Added

- **Per-trigger webhook auth.** Each webhook trigger declares an `auth` block
  with one of three modes: `hmac_body` (the existing global-secret HMAC),
  `static_header` (a shared secret compared against a request header — for
  services that can only send static headers), or `timestamped_hmac` (a
  timestamped signature with a tolerance window). Per-trigger secrets live under
  `/data/webhook_secrets/`. Triggers may also declare a memory read `clearance`.
- **Webhook-origin containment.** A turn started by a `/webhook/{name}` trigger
  is treated as untrusted third-party content: it runs in a restricted runtime
  (no plugins, no external hooks, no shell/filesystem/network tools — only
  public-clearance memory recall and an operator-bound notification), reads
  memory at a reduced clearance (never private), writes no memory, and gets a
  fresh one-shot session that cannot resume another. Operator-signed `/invoke`
  keeps full trust.

### Changed

- **Breaking:** webhook trigger `path` is removed. Triggers are served at
  `POST /webhook/<name>`. A v1 config with `path` still loads (with a migration
  warning) but is served at `/webhook/<name>` only.
- Webhook auth is now fail-closed: a webhook trigger whose secret is missing, and
  `/invoke` when webhook auth is disabled, are rejected (401/403) rather than
  served open.
- Webhook request bodies are capped at 64 KiB (413 on oversize).
- Pre-upgrade webhook sessions are purged at boot migration (their trust origin
  is unknowable).

## [0.96.0] - 2026-07-19

Engagements can no longer complete past an unread operator message.

### Added

- `emit_completion` now refuses (`unread_inbound`, retryable) when an
  operator message is waiting unread — the same contract as the existing
  ask gate: end the turn, read the message, then decide whether the
  completion still stands. The check is atomic with the terminal
  transition, covers messages still in flight between Telegram acceptance
  and the spool, and repeated refusals force a real turn boundary so the
  pending message is actually delivered.
- If an engagement still terminates with unread operator messages (error
  exits, cancels, reaps), the closing topic post now says so and quotes
  bounded excerpts, instead of dropping them silently.

### Fixed

- A completion whose terminal record failed to persist is no longer
  acknowledged as successful — it now returns a retryable error while the
  engagement stays live.

## [0.95.1] - 2026-07-19

### Fixed

- Python-based plugins no longer corrupt their own installed artifact:
  the interpreter's bytecode cache is redirected outside plugin artifacts,
  artifacts are now fully frozen (directories included), any bytecode
  committed to a plugin repo is stripped at publish, and artifacts already
  poisoned by the old behavior are healed at boot when their only drift is
  bytecode. Integrity checksums remain strict — bytecode inside an
  artifact still reads as tampering, because a crafted cache file could
  otherwise silently replace checksummed code at import time.

## [0.95.0] - 2026-07-19

Plugin state directories now work as documented.

### Fixed

- `CLAUDE_PLUGIN_DATA` is provided natively by the Claude CLI as a private,
  persistent per-plugin directory — but a plugin that re-declared the
  variable in its own `.mcp.json` shadowed it with a literal placeholder
  string (the gmail plugin stored its OAuth token in a directory literally
  named `${CLAUDE_PLUGIN_DATA}`). Casa's own plugin doctrine used to
  instruct exactly that declaration. The doctrine is corrected, and the
  self-declaration is now rejected at both plugin push time and install
  verification (`mcp_reserved_env`).

## [0.94.0] - 2026-07-19

Stray `**` and `##` markers no longer leak into Telegram messages.

### Fixed

- Bold or italic text touching inline code (`**`file.py`**`, `**see
  `cmd` now**`) now renders correctly instead of leaving literal `**`
  interleaved with formatted text — the most common formatting leak in
  engagement replies and DM answers. Telegram forbids bold overlapping
  monospace, so the bold is applied around the code fragment.
- Markdown headings (`## Section`) now render as bold lines instead of
  showing literal `#` markers. Standard edge cases stay literal (no space
  after the hashes, 7+ hashes, headings inside code blocks).
- Replies longer than one Telegram message no longer fall back to raw
  markdown: long responses are now split at paragraph/line boundaries and
  every part renders formatted, within Telegram's length and entity
  limits (code blocks split across messages stay monospace).

### Added

- Plain markdown tables (header + `|---|` separator row, no formatting
  inside cells) now render monospaced so columns stay aligned. Tables with
  bold/code in their cells keep the previous inline rendering.

### Changed

- Formatting is now resolved line-by-line: a bold span can no longer pair
  across a newline, and a line with an unpaired backtick stays fully
  literal (previously other formatting on that line could still apply).

## [0.93.0] - 2026-07-19

Plugin names can no longer be guessed wrong at install time.

### Added

- The `name_mismatch` rejection from `plugin_add`/`plugin_update` now
  returns the plugin's canonical manifest name, so the installer
  self-corrects an add in one retry instead of guessing — a repo named
  `casa-plugin-gmail` hosts the plugin `gmail`, and confusing the two
  previously produced an opaque error with no path forward.
- Installer and plugin-developer doctrine now state the naming convention
  explicitly: keeper repos are `casa-plugin-<name>`, the manifest `name` is
  the canonical identity, and build handoffs must state the plugin name.

## [0.92.0] - 2026-07-18

A broken plugin MCP server is now caught at install time with a precise
reason instead of silently never registering its tools (the gmail-plugin
incident), and secrets-only repairs clear the plugin health report without
a restart.

### Added

- Plugin verification statically checks that a plugin's `.mcp.json` launch
  references (command, arguments, and `env` paths such as a vendored
  `PYTHONPATH`) actually exist in the installed artifact — a missing
  interpreter or entry file now blocks with `mcp_command_missing` instead
  of verifying green while the server can never start.
- The plugin-developer pre-push guard now rejects `.mcp.json` references
  that are not part of the pushed commit (for example a gitignored dev-only
  virtualenv), path traversals out of the plugin root, and now arms on all
  common `git push` command forms; the previously advertised but
  non-functional override was replaced by a logged
  `CASA_ALLOW_ANTI_PATTERN=1` prefix.
- Plugin-developer doctrine gained a sanctioned "Python MCP servers"
  pattern (vendored, committed dependencies + `PYTHONPATH`) and no longer
  recommends MCPB bundles, which Casa never provisions.

### Fixed

- Reloading plugin secrets (`casa_reload(scope='plugin_env')`) now
  regenerates and re-notifies the plugin health report, so a secrets-only
  repair clears a stale red health entry without a registry mutation.
- Plugin verification no longer reports a rotated-but-not-reloaded plain
  secret as resolved, and a malformed `.mcp.json` `args` shape is flagged
  as invalid instead of crashing the post-mutation verify.
- The configurator secrets recipe now reloads before verifying (the old
  order guaranteed a wrong verdict) and no longer references a
  nonexistent verification field.

## [0.91.0] - 2026-07-18

Engagement topics now show a live task checklist, and streamed updates start
cleanly.

### Added

- The pinned summary shows the agent's task list as a live checklist (☑ done,
  ▶ current, ☐ pending) that checks off as work completes, with exact counts
  for tasks outside the visible window.

### Fixed

- Streamed narration no longer starts a new message with stray blank lines
  after the agent runs tools.
- Progress narration reads as complete sentences instead of fragments ending
  in a colon whose content never arrives.

## [0.90.0] - 2026-07-18

Concierge specialist questions now acknowledge immediately and deliver their
answer later, once the originating satellite is idle. Butler home-control turns
remain direct and immediate.

### Added

- Coordinated protocol-2 voice handoff with the companion Home Assistant
  integration, so Concierge can confirm a specialist request through Assist
  before the specialist continues in the background.

## [0.89.0] - 2026-07-18

Engagement conversations now read like proper chat: formatting renders, replies
are spaced apart, questions always come as tappable buttons, and the agent can
acknowledge you with a quick reaction.

### Added

- Agents in an engagement can react to your latest message with an emoji (👍
  done, 👀 working) as a lightweight, non-blocking acknowledgement.

### Changed

- Engagement narration, questions, and their follow-up edits now render Markdown
  (**bold**, `code`, code blocks) as real formatting instead of showing the raw
  `**` and backtick characters.
- Consecutive narration updates are separated by a blank line, so streamed
  progress no longer runs together into text like "questions.Good".
- When an agent asks a question with choices, it now reliably offers tappable
  buttons — even mid-conversation and even when an underlying skill would
  otherwise ask in prose.

## [0.88.0] - 2026-07-17

Casa can now be discovered automatically by its companion Home Assistant
integration when it runs as a Supervisor app.

### Added

- An authenticated, versioned Supervisor discovery record for Casa's external
  API endpoint. Home Assistant shows the discovered endpoint for confirmation
  before connecting.

### Security

- Discovery uses Casa's existing webhook secret without logging or copying it
  into local registration state. The registration is removed only when webhook
  authentication is disabled, and a missing secret never removes a live record.

## [0.87.0] - 2026-07-17

Engagement questions now render the way operators actually write them: long,
descriptive options are accepted verbatim, buttons stay readable, and moved
questions no longer read as "asked twice".

### Added

- Agents can supply a per-option `short` — a few words rendered as the button
  caption. When every option has a usable short, the buttons show them
  verbatim; otherwise the whole set falls back to clean positional labels
  ("Option 1", "Option 2", ...) that match the numbered options in the
  question body.
- Diagnostic instrumentation for delayed question/reply posting (match-point
  timing logs, content-free).

### Changed

- The invented option/question length caps are gone. Ask validation now
  measures the real rendered message against Telegram's actual 4096-character
  limit across every lifecycle form, and a rejected ask explains exactly why
  and what to shorten.
- A re-anchored (moved) question's old copy is replaced by a compact
  "moved — answer the current copy below" marker instead of repeating the
  full question text.
- Trailing "I'll wait for your answer"-style narration after a question is
  suppressed instead of posted below it (and no longer causes the question to
  be reposted).
- Option text and questions are preserved verbatim: the framework no longer
  strips agent-written enumerators or question prefixes.

### Fixed

- Descriptive multiple-choice questions no longer degrade to free-text
  answers after repeated opaque validation failures.
- A long chain of cancellation/shutdown/crash races in question posting,
  re-anchoring, and settlement found by adversarial review (20 findings
  across 8 whole-branch review rounds) — including two-live-questions,
  lost-narration, and question-reposted-after-close scenarios.

## [0.86.0] - 2026-07-17

Casa now publishes a safe, authenticated catalog of its enabled Home Assistant
voice residents. The companion integration can use that catalog to create
separate Tina and Gary conversation entities after discovery; the Casa app
itself only publishes the catalog.

### Added

- Authenticated dynamic discovery of enabled `ha_voice` residents through a
  fixed, non-cacheable endpoint that returns only stable roles and display
  names.

### Changed

- Voice rate limits are isolated by agent role and scope, so Gary cannot
  consume Tina's allowance when both receive turns from the same scope.

### Security

- Discovery refuses access when no secret is configured or the request
  signature is invalid, and never returns prompts, tools, delegates, or other
  private agent configuration.

## [0.85.0] - 2026-07-17

Gary can now hand specialist questions off quickly, keep taking voice turns,
and announce the completed answer after the originating satellite is idle.

### Added

- Fast background specialist hand-offs for voice: Gary acknowledges the work
  immediately instead of holding the Assist request open while a judge,
  health, finance, or future specialist thinks.
- Proactive stable-idle delivery through the companion Home Assistant
  integration. Results speak immediately when the satellite is already idle,
  otherwise they wait until the current listening/processing/response cycle is
  over; per-device queues stay ordered without blocking other satellites.
- Voice controls for job status, cancellation before playback, clarification
  continuation, and explicit detail requests. Private results announce only a
  safe availability summary unless the detail request passes identity and
  clearance checks.
- Operator bounds for route reconnect grace, maximum result retention, and the
  active/ready backlog per route: `voice_route_freshness_seconds`,
  `voice_job_delivery_ttl_seconds`, and `voice_job_route_cap`.

### Changed

- Background work is capability gated, not version-string gated: Casa requires
  an HMAC-authenticated protocol-1 WebSocket route whose registration has
  acknowledged both background-job and satellite-announcement capabilities.
  SSE and older integrations continue synchronous Tina/Gary turns unchanged.
- Specialist results stay out of Gary's resident transcript and token context;
  Casa retains the durable job and sends Home Assistant only the final
  policy-approved spoken summary.

### Security

- The WebSocket HMAC is explicitly documented as authenticating the Home
  Assistant client only at HTTP upgrade, over an empty request body. It does
  not MAC individual frames, encrypt payloads, or cryptographically
  authenticate the server; keep the link on a trusted LAN/private network or
  a server-authenticated encrypted tunnel.

## [0.84.0] - 2026-07-16

Tina's Home Assistant path is now ready before a voice turn starts, with
strict loop bounds and a documented raw-MCP rollback switch.

### Added

- Tina gets an eager, role-scoped Home Assistant tool facade: Assist tools are
  discovered at boot and kept connected for her without changing the raw Home
  Assistant surface used by other agents.
- New `tina_ha_facade_enabled` option (default `true`). Disable it to fall back
  to raw Home Assistant MCP while diagnosing compatibility issues; an initial
  facade-discovery failure also degrades to that raw path instead of blocking
  Casa startup.

### Changed

- Tina's voice turns carry less tool context and no longer need an on-demand
  tool-search round before direct Home Assistant actions. State lookups accept
  a local domain filter while Casa always sends `{}` to Home Assistant.
- Home Assistant tool loops are bounded: at most one successful live-context
  lookup and one validation correction can be consumed in a voice turn.
- Casa pins every in-process agent to the verified Claude CLI executable and
  fails startup unless `/usr/local/bin/claude --version` reports exactly
  `2.1.150`.

## [0.83.0] - 2026-07-16

Engagement-topic UX round 3: no more duplicated narration, questions that
pause politely when you're away, and buttons you can actually read and
multi-select. No new options; no configuration change required.

### Fixed

- Agent narration is no longer re-posted below a question or after your
  message — the live-editing duplicate copies are gone (the root cause was
  the topic relay treating every poll as a crash recovery).
- An unanswered question no longer re-asks itself in a loop while you're
  away. When a question expires the engagement now PAUSES (`⏸ paused —
  waiting for the operator` in the pinned summary) and resumes the moment
  you reply; a runaway agent that keeps asking anyway is forcibly suspended.
- A free-text question left behind by output posted after it is re-posted
  at the bottom of the topic, so the open question is always the last item.
- Answered questions reliably clear their buttons on screen, and every
  confirmed settle is now visible in the logs.

### Changed

- Questions with several valid answers can now be MULTI-SELECT: tap to
  toggle ☐/☑ options, then ✅ Submit.
- Button labels keep the words that distinguish the options ("Single
  account…aliases", not "Single account with"), and agents can supply their
  own short labels for long options.
- Agents can no longer double-label options ("1. A — …") — leading
  letters/numbers are stripped and numbered once by Casa — and a
  multiple-choice question posted as free text is refused with guidance to
  use buttons.
- Agents are instructed to ask ONE question, stop, and wait — silently: no
  more "ending my turn…" narration, no working past their own questions.

## [0.82.0] - 2026-07-15

Fixes for the three findings of the 2026-07-15 live verification round
(voice-latency, runaway-delegation resilience, reload reporting). No new
options; no configuration change required.

### Fixed

- Voice agents no longer spend the first seconds of a fresh conversation
  silently "searching" for their own built-in abilities before acting.
  (Framework tools are now pre-loaded for every agent session instead of
  being discovered on demand — cold-session hand-offs stop timing out.)
- A voice turn that ends with nothing to say now speaks a brief apology
  line instead of going silent. The line is customizable per agent via the
  new `empty_turn` key in `voice_errors`.
- Background (async) hand-offs to specialists now have a hard time ceiling
  (10 minutes). A stuck specialist is cancelled and reported back as a
  failure instead of blocking new hand-offs indefinitely — previously two
  stuck hand-offs could freeze delegation for every agent until a restart.
- `casactl reload --scope=agents` no longer reports long-installed
  specialists as freshly "added" on the first reload after boot; the
  action list now reflects only real additions and removals.
## [0.81.0] - 2026-07-15

Engagement-topic UX round 2: the pinned summary, topic titles, and question
buttons all get more readable. No configuration change is required.

### Changed

- Engagement topic summaries now lead with the current status (working/waiting)
  and use a short 2-3 word title, so the topic header reads clearly at a
  glance.
- Questions asked in the topic — and in Ellen's DM — now show the full answer
  choices in the message itself, with short button labels underneath, so
  options are always readable even when Telegram would otherwise truncate a
  long button.

### Fixed

- The pinned summary correctly shows "waiting for your reply" while it's your
  turn (previously it could get stuck showing "working").
- Answered questions reliably settle (their buttons disappear) even if a
  Telegram edit transiently fails.
- The engagement no longer occasionally re-posts a duplicate of a message it
  already sent.

## [0.80.0] - 2026-07-15

Voice-fleet hardening: generic delegation, session, and ingress safety
improvements that let a second voice agent run alongside the butler without
crossing wires. No configuration change is required; three new options are
available for tuning.

### Security

- Delegation is now authorized against the calling agent's declared delegates:
  an agent can only hand off to the specialists it is configured to use, and
  the check is keyed to the agent actually running (not a parent it was invoked
  from). Undeclared hand-offs are refused.
- Voice and webhook entry points now only serve agents that declare the
  matching channel. An agent meant for private text channels can no longer be
  reached from a voice satellite, even if a pipeline is misconfigured.
- Conversation continuity is scoped per agent: two voice agents sharing one
  device can no longer resume each other's conversations. (Existing voice
  sessions reset once on upgrade.)

### Added

- Voice-originated hand-offs run under a turn budget: the agent speaks a brief
  "one moment" and, if a specialist can't answer in time, gives a spoken
  fallback instead of silently timing out.
- Delegated agents can declare required plugins/tools; a missing dependency
  refuses the hand-off instead of answering without its knowledge source.
- Limits on concurrent specialist work: at most one active delegation per
  specialist per voice device (different specialists may still run
  concurrently), plus an overall fleet-wide cap, with per-agent usage/cost
  logging and a configurable cost alert.
- New options: `voice_turn_budget_seconds` (default 27), `specialist_max_concurrency`
  (default 2), `specialist_cost_alert_threshold` (default 5.0).

## [0.79.2] - 2026-07-15

### Fixed

- In-Casa engagements (configurator, specialist topics) crashed on operator
  topic messages after 0.79.0 — the new reply-threading argument was not
  accepted by their driver. Caught by the end-to-end suite before any
  deployment; claude_code engagements were unaffected.

## [0.79.1] - 2026-07-15

### Fixed

- The end-to-end test harness (and any message object without an id) no
  longer crashes the engagement inbound path — reply-threading and
  ordering degrade gracefully instead; no change for real Telegram
  traffic.

## [0.79.0] - 2026-07-15

### Added

- A pinned live summary on every engagement topic: status, plan progress,
  current activity with elapsed time, and open questions, always visible at
  the top.
- Instant receipts and reply-quoting for your messages, so it's always clear
  Casa saw what you sent and what it's responding to.
- Numbered questions that visibly settle when answered.
- A STOP/`redirect:` priority lane to interrupt and redirect an agent
  mid-turn.

### Changed

- Engagement topics now read in strict chat order — the running narration
  rolls to a new message instead of editing above newer messages.
- Agents no longer ask a new question while your message is waiting to be
  read.

### Fixed

- Answered questions now drop their buttons instead of only showing a toast.
- The false "please retype" notice is gone.

## [0.78.1] - 2026-07-14

### Fixed

- The end-to-end test harness image failed to build after 0.78.0 (its own
  copy of the bundle build stage was missing the new `text_util` module);
  no runtime change.

## [0.78.0] - 2026-07-14

### Added

- Plugins can declare a plain-language summary for protected tools, shown
  as the approval headline.

### Changed

- Approval prompts lead with the agent's name and the action summary; the
  exact arguments are demoted below but always shown.

## [0.77.0] - 2026-07-14

### Changed

- Operator approval prompts and their outcome messages are now written in
  plain language; the exact tool arguments remain shown verbatim.
- Agents no longer narrate approval prompts — the button is the prompt.
- The typing indicator now also shows while an approved action is running.

## [0.76.0] - 2026-07-14

### Added

- DM button questions from Ellen and specialists.
- Operator-approved protected plugin tools with single-use argument-bound
  grants.

### Changed

- Inbound message contexts sanitized at every ingress.

### Fixed

- A verdict-broker drain loop could livelock the whole agent core on
  Python 3.12+ when a completed hook task was drained before its cleanup
  callback ran; done tasks are now discarded synchronously before waiting.
  (Harmless on the current Python 3.11 base image; fixed ahead of any
  future base-image upgrade.)
- Button-answer delivery now retries on transient send errors instead of
  aborting on the first exception, so a failed delivery is always reported
  back on the button message.

## [0.75.1] - 2026-07-14

### Fixed

- **Verbatim process requirements now mean verbatim.** The live brief-fidelity
  gate caught the assistant rewording a user's process instruction ("a
  discussion with the implementer" became "discuss with me") even though it
  correctly landed in `brief.process_requirements`. The doctrine now defines
  VERBATIM operationally — quote the user's own words as the list entry, no
  rewording, shortening, or person changes — with a worked example.
- The in-container brief-fidelity eval now resolves the deferred
  `${PRIMARY_AGENT_MODEL}` placeholder and seeds `CLAUDE_CODE_OAUTH_TOKEN`
  from the s6 container environment, so it runs correctly under `docker exec`
  (test tooling only, no runtime change).

## [0.75.0] - 2026-07-14

### Added

- **Live engagement topic streaming.** An engaged agent's narration now
  streams into its Telegram topic turn by turn as the agent works, instead of
  arriving only at the end.
- **Button questions (`ask`) for engaged executors.** An executor can pose a
  multiple-choice question to the operator as inline Telegram buttons and
  await the tap, the same broker-backed pattern the permission relay uses.
- **Structured briefs.** Engagements now carry a brief envelope that passes
  the operator's process requirements to the executor **verbatim** (never
  paraphrased into a feature requirement) and tracks completion accounting.
- **Turn-taking state.** Engagements track whether they're waiting on the
  operator or the agent, and surface it when an agent acts before the
  operator has responded.

### Changed

- The engagement CLI now runs in explicit `--print --verbose --output-format
  stream-json` mode instead of relying on implicit defaults.
- Permission-relay internals are rebuilt on a Casa-owned verdict broker
  (behavior unchanged for operators — allow/deny still works the same way).
- `bash` is now a runtime dependency of engagements (the run script uses
  process substitution).

### Fixed

- An engagement's stderr is now bounded per spawn (a ring buffer), instead of
  growing without limit across the engagement's lifetime.

## [0.74.2] - 2026-07-13

### Fixed

- A retried or duplicated `emit_completion` that arrives after the engagement
  already finalized now gets the honest `already_terminal` acknowledgment
  instead of a misleading `not_in_engagement` error (terminal-record binding
  for `emit_completion` only; privileged tools keep the active-only rule).
- claude_code engagement workspaces now receive the executor's `doctrine/`
  directory — the workspace CLAUDE.md references doctrine files that
  previously never existed in the workspace, so engaged agents worked
  without their conventions.

## [0.74.1] - 2026-07-13

### Fixed

- A plugin assigned to a **disabled specialist** no longer reports
  `reload_required` after an update (the specialist-tier analogue of the
  v0.71.1 disabled-executor rule; found live during the v0.74.0 release-flow
  verify — this was the original "Plugin degraded" incident's actual
  trigger). A disabled specialist is dormant-by-config: its target now
  verifies `state:"disabled"` and is re-checked for real when enabled.
- An agent whose role the registry does not recognize (e.g. a
  reload-constructed disabled specialist) now logs a loud warning when its
  plugin resolution falls back to the resident tier instead of silently
  resolving no plugins.

## [0.74.0] - 2026-07-13

### Added

- **Plugin release-flow hardening.** Plugin releases are now identified by an
  annotated `vX.Y.Z` tag. A plugin-developer's completion is mechanically
  validated (tag exists, is annotated, matches the built commit and the remote
  manifest version) and rejected otherwise — the engagement stays live so the
  producer can fix the release and re-emit.
- `plugin_add` / `plugin_update` accept `expected_revision` and abort before
  any change if the tag moved after the build (`revision_mismatch`) or does
  not match the plugin's own version (`tag_version_mismatch`).
- Plugin mutations report phase-aware outcomes (`activation_committed`,
  `runtime_ready`) so a "pin landed, reload pending" state is actionable
  instead of ambiguous.

### Fixed

- A missing release tag is now reported as `ref_not_found` instead of "GitHub
  temporarily unavailable" (GitHub returns 422 for missing refs; the resolver
  classifies status codes structurally, with new `resolve_auth_failed` and
  `source_empty` verdicts and bounded rate-limit retries that honor
  Retry-After).
- Removed a race that could mark a freshly-updated plugin as `reload_required`
  and warn every agent with "Plugin degraded" after a successful update;
  health reports now derive from a fresh verification pass, and duplicate
  registry-wide rows are suppressed.
- First-contact plugin notices now say precisely what is wrong ("Plugin update
  incomplete: … remains bound to the previous artifact") instead of a generic
  degraded warning.

## [0.73.0] - 2026-07-13

### Added

- **`send_media` capability** — agents can now deliver a document, photo, audio,
  or voice file to the user over the originating channel. A producer drops the
  file into a shared `/data/plugin-outbox/` and hands `send_media` only the path;
  Casa streams the bytes to Telegram, so they never enter the model context.
  Delivery is guarded by a TOCTOU-safe claim-by-atomic-rename with an
  `O_NOFOLLOW` regular-file gate, single-hardlink and size checks, and a per-kind
  magic sniff (PDF / JPEG-PNG / MP3 / Ogg-Opus); orphaned files are swept after
  2 h. Granted to the assistant and finance agents. First consumer: the
  on-demand invoice PDF preview (the plugin + n8n pieces land separately).

## [0.72.0] - 2026-07-13

### Removed

- **The one-time pre-v0.71.0 plugin migration is gone.** With the marketplace
  architecture retired and no pre-v0.71.0 install reachable any longer, the
  legacy-state migration (`plugin_migration.py`, the boot migrate-before-seed
  branch and its `.migration-done` sentinel, `/data/plugin-migration-report.json`,
  the migration-issue replay across boots and mutations, and the legacy-tree
  offline-adopt publish path) was removed — it was pure dead weight and added
  boot/health failure surface. Fresh-install seeding no longer depends on the
  migration sentinel: `seed_defaults` runs on every boot, is idempotent, and the
  registry's permanent `seeded_defaults` ledger (not any boot flag) is what
  prevents re-adding an operator-removed default. A corrupt registry is still
  left untouched rather than overwritten as if fresh. Existing v0.71.x installs
  are unaffected — their registry stays authoritative and the stale sentinel/
  report files are simply ignored. The `legacy-content:` artifact-resolution
  grammar is retained so any already-adopted legacy artifact keeps loading.

## [0.71.1] - 2026-07-13

### Fixed

- **Plugin health no longer false-alarms on a disabled executor.** A plugin
  assigned to an executor that is disabled by config (`enabled: false`, e.g.
  the plugin-developer toolbox before it is turned on) was reported
  `authorization_missing` — because a disabled executor is absent from the
  registry lookup, its `tools.allowed` read as empty and every derived MCP grant
  looked unauthorized, sending a spurious plugin-health notice on every boot.
  `verify_plugin_state` now recognises a disabled-executor target as
  dormant-by-config (`state: "disabled"`, never not-ready) and validates its
  authorization against the disabled definition, so the check is real again the
  moment the executor is enabled.
- **Enabling an executor refreshes plugin health.** `casa_reload(scope="executors")`
  (which picks up an `enabled:` flip) now regenerates the plugin-health report and
  re-notifies, so turning an executor on immediately surfaces any real
  `authorization_missing` for its assigned plugins instead of leaving the report
  stale-green until an unrelated trigger.

## [0.71.0] - 2026-07-13 — unified plugin architecture

### Changed

- **Unified plugin architecture.** One registry (`/config/plugins/registry.json`)
  is now the single plugin-assignment authority for every agent tier, resolved to
  immutable content-addressed artifacts under `/config/plugins/store/`. Residents
  and specialists load plugins directly through the Agent SDK; executor
  engagements pin their exact artifacts at launch and load them via `--plugin-dir`.
  The plugin marketplace, version-keyed cache, and `claude plugin` install
  machinery are removed end-to-end. A one-time migration converts existing
  installs automatically on first boot (report: `/data/plugin-migration-report.json`).
- Configurator plugin tools are now `plugin_add`, `plugin_update`,
  `plugin_assign`, `plugin_unassign`, `plugin_remove`, `plugin_list`, and a
  tier-aware `verify_plugin_state`; plugin versions always derive from the
  plugin's own manifest.

### Fixed

- A plugin update can no longer silently keep executing stale code
  (the v1.1.0→v1.2.0 lesina-invoice incident): artifacts are content-addressed,
  verification compares the registry's desired state against each running
  agent's actual loaded artifact, and reports `reload_required` on any mismatch.

### Added

- Plugin health report (`/data/plugin-health.json`) with an operator DM on new
  issues and a one-line first-contact notice from affected agents.

### Removed

- Marketplace catalogs, `enabledPlugins` provisioning, the build-time plugin
  seed, and the boot-time `claude plugin` bootstrap. Legacy state under
  `/config/marketplace` and `/config/cc-home/.claude/plugins` is left inert for
  one release (rollback-safe) and will be cleaned up in a later release.

## [0.70.0] - 2026-07-12 — rich text in Telegram replies

### Added

- Agent replies now render Markdown in Telegram: **bold**, *italic*, `inline
  code`, and fenced code blocks shown as an aligned monospace box — so tables
  and structured output finally line up on your phone instead of showing raw
  `**` and backticks. On by default; a new **Rich Text in Telegram**
  (`telegram_rich_text`) option turns it off to send everything as plain text.

### Notes

- Rendering is applied only to genuine agent responses; system notices,
  permission prompts, and error messages are unchanged. If a message can't be
  formatted safely it is delivered as plain text (never dropped). Very long
  messages (over one Telegram message) are sent unformatted for now.

## [0.69.12] - 2026-07-12 — Ellen can tidy finished engagement topics

### Added

- The primary agent can now clean up finished engagements' Telegram topics on
  request ("clean up the engagement group") without delegating to the
  configurator. It's limited to the safe cleanup that only removes topics past
  their retention window; purging everything immediately remains
  configurator-only.

## [0.69.11] - 2026-07-12 — interactive engagements can be resumed again

### Fixed

- The session id of an interactive (in-process) engagement is now captured
  correctly, so an engagement that goes idle or survives an add-on restart can
  actually be resumed. Previously the id was read from a client attribute that
  no longer exists in the pinned SDK, so it was never saved — the resume path
  was silently dead and a suspended engagement could not be reopened. The id is
  now read from the message stream (the same source the resident session pool
  already uses).

## [0.69.10] - 2026-07-12 — resumed engagements keep their restrictions

### Security

- Resuming a paused engagement (after an idle period or an add-on restart)
  now re-applies the agent's full set of restrictions. Previously a resumed
  specialist or executor came back with none of its configured limits — no
  tool denials, no permission guard, no restricted working directory — running
  with the default broad toolset. It now rebuilds the original configuration,
  and refuses to resume if that configuration is missing rather than falling
  back to an unrestricted session.
- The self-grant guard for `.claude/settings.json` now also covers shell
  commands that write to it (e.g. a redirect), not just the file-editing
  tools, closing an obvious bypass for agents that have shell access. (Fully
  preventing shell writes to protected files is a larger change tracked
  separately.)

### Changed

- Agent skills are now enabled through the Claude Agent SDK's dedicated
  `skills` option instead of the deprecated practice of listing `"Skill"` as
  an allowed tool. Behavior is unchanged (all skills remain available); this
  moves off an interface the SDK has deprecated, across the primary, voice,
  specialist, and executor agents.

## [0.69.8] - 2026-07-12 — permission hardening

### Security

- The voice butler and specialist agents can no longer spawn sub-agents. The
  underlying SDK exposes sub-agent-spawning tools regardless of an agent's
  configured tool list, and a spawned sub-agent runs with a broad default
  toolset rather than the restricted parent's — so a prompt-injected butler or
  specialist could reach read/enumeration tools its own configuration excludes.
  These tools are now explicitly denied for those agents. (Destructive actions
  were already blocked; the primary assistant and executors are unaffected.)
- Agents can no longer edit their own `.claude/settings.json`. Plugin
  enablement is managed by the configurator; blocking direct edits closes a
  path by which a prompt-injected agent could self-enable a cached plugin.

### Fixed

- When a plugin has more than one version cached, install and verification now
  read environment-variable requirements from the same (highest) version the
  tool grants are derived from, instead of whichever the filesystem listed
  first.
- Trying to start an engagement from a non-Telegram surface (voice, webhook)
  now returns an accurate message — engagements can only be started from
  Telegram — instead of wrongly telling the operator to set a Telegram
  supergroup option.

### Changed

- Specialist agents now log their capability summary at boot (like the primary
  agents already did), giving post-install verification a log to check.
- The offline test SDK is now guarded against field-shape drift from the real
  SDK for the permission result type, closing a gap a past review had to catch
  by hand.

### Fixed

- Auto-closing a stale developer/configurator engagement now actually stops
  its background process. Previously the daily cleanup closed the engagement's
  topic but left the underlying worker running — a leak, and these are exactly
  the engagements most likely to be auto-closed.
- The daily cleanup no longer risks closing an engagement that a user revived
  in the same instant: the "is it still stale?" check is now part of the same
  atomic step that closes it.
- After a restart, engagements reconciled from "active" to "idle" (v0.69.0)
  now have that change written to the engagement record file immediately,
  instead of only in memory until the next unrelated change — so the on-disk
  record and the health auditor reflect the true state right after boot.
- The health auditor records a clear failure on a malformed engagement file
  instead of aborting the whole audit.

## [0.69.5] - 2026-07-12 — voice butler acts on device commands instead of stalling

### Fixed

- The voice butler no longer gets stuck re-checking device state instead of
  acting when a command comes in through delegation (e.g. Ellen relaying
  "toggle the office light"). Its prompt now says to call the action tool
  directly — Home Assistant resolves the device by name — and bounds the
  read-only "what's exposed" lookup to at most once per turn, with explicit
  guidance for "toggle". Direct voice commands were already fine; this fixes
  the delegated path.

## [0.69.4] - 2026-07-12 — delegated memory recall quality restored

### Changed

- Memory recalls made on behalf of specialists, executor engagements, and
  engagement queries return to the balanced recall depth (`mid`), restoring
  recall quality. The reduced depth (`low`) shipped in 0.68.1 was a stop-gap
  for a memory-backend latency issue that has since been fixed on the backend
  side, so the deeper, higher-quality recall no longer risks a timeout. Voice
  recalls are unaffected (they keep their own `low` setting for latency).

## [0.69.3] - 2026-07-12 — completing an engagement is harder to get wrong

### Fixed

- A successful engagement no longer ends up marked as failed because the
  agent phrased its completion call differently than expected. The
  completion tool now accepts the documented status vocabulary (`ok`,
  `partial`, `failed`, `cancelled`) and maps each to its true outcome —
  previously anything other than exactly `ok` (including the documented
  `partial` and `cancelled`) failed the engagement. An unrecognized status
  or malformed arguments now return a clear tool error the agent can
  correct, with the engagement still running.
- Oversized completion summaries are truncated instead of being pushed
  through notifications and topic messages at full size.

### Changed

- The primary agent's guidance now says explicitly: when an engagement
  completes, its topic is closed — follow-ups and edits go through a fresh
  delegation, never "continue in the old topic" (which users couldn't post
  to anyway).

## [0.69.2] - 2026-07-12 — memory sensitivity classification rides out transient failures

### Fixed

- Classifying a memory's sensitivity tier now retries once (2s backoff)
  before falling back to the most restrictive tier. A transient failure used
  to mis-tier the memory permanently, making it invisible to household
  members it should have been shared with. The classification runs off the
  conversational hot path, so the retry costs nothing user-visible.
- Classification failures now log the exception type and message in the
  warning line itself (previously only in a traceback that log tooling
  truncated), and an unparseable model reply — previously indistinguishable
  from a genuine "private" verdict — now leaves a log trace.

## [0.69.1] - 2026-07-12 — plugin marketplace changes are now git-versioned

### Fixed

- The plugin marketplace manifest is now tracked by the config git repo, so
  committing after a marketplace change produces a real commit. Previously
  the file was gitignored while the configurator's own recipes required
  committing it — agents wasted minutes looping between "committed ok" and
  "file still untracked" on every marketplace operation. Existing
  installations pick up the new whitelist automatically on next start.
- When a commit finds no tracked changes, the result now explains which
  paths are tracked and that secret files (like the plugin environment
  file) are intentionally excluded, instead of returning a silent empty
  answer that reads like a failure.
- Configurator guidance no longer claims plugins can only be installed on
  primary agents — specialist installs have been supported (and in
  production) since v0.68.0.

## [0.69.0] - 2026-07-12 — engagements stop leaking state across restarts and time

### Added

- Abandoned engagements are now cleaned up automatically: a daily sweep
  cancels any engagement with no activity for `engagement_reap_days` days
  (new option, default 7; 0 disables), closing its Telegram topic and
  notifying the engaging agent — previously an interrupted engagement could
  sit "active" forever (a 25-day-old one was found this week).
- Finished engagements now leave a tombstone in the engagement registry
  file (kept 30 days) instead of vanishing on the next write, so the
  duplicate-task guard and post-mortem inspection keep working across
  add-on restarts.

### Fixed

- After a restart, engagements that were running when the container stopped
  are reconciled to "idle" (dormant, resumable) instead of claiming to be
  active with no process behind them.

## [0.68.2] - 2026-07-12 — engagement teardown stops logging a false error

### Fixed

- Closing an agent engagement no longer logs an ERROR ("Task exception was
  never retrieved: CLIConnectionError") on every successful close. The Claude
  Agent SDK answers control requests on background tasks; when one raced the
  agent subprocess shutting down, its failed write was logged by the runtime
  as an unhandled error even though nothing was wrong. That specific teardown
  race is now logged at debug level instead; genuine failures (including a
  missing claude binary) still log at ERROR.

## [0.68.1] - 2026-07-12 — delegated memory recalls stop timing out under load

### Fixed

- Memory recalls made on behalf of specialists, executor engagements, and
  engagement queries now use the fast recall budget. The previous default
  asked the memory backend to rerank 300 candidates (~12s on the appliance
  CPU), which under concurrent load crossed the 20s client timeout and made
  every delegated recall fail during busy periods. Direct (non-delegated)
  recalls are unchanged.

## [0.68.0] - 2026-07-12 — installed plugins are usable by construction

### Fixed

- Installing a plugin now actually makes it usable: each agent's allowed
  tools include a server-level grant for every plugin enabled on that agent,
  derived at session build from installed state. Previously the first tool
  call of a freshly installed plugin hit an unanswerable permission prompt.
  Uninstalling a plugin revokes its grant automatically.

### Changed

- Agents without an interactive permission channel (residents and
  specialists) now fail closed: a tool call that is not allowed is denied
  immediately with a clear log message instead of hanging on a permission
  prompt nothing can answer. Executor engagements keep their Telegram
  approval flow.

## [0.67.2] - 2026-07-11 — long-term memory recall stops dropping connections

### Fixed

- Memory recall no longer fails after an idle gap. The Hindsight client pooled
  keep-alive connections, but memory traffic is sparse (roughly one or two
  calls per turn, turns minutes apart), so a pooled connection was almost
  always idle past the server's keep-alive window; the first recall of a turn
  reused a half-closed socket and failed, silently degrading recall while the
  same-turn memory write still succeeded. The client now opens a fresh
  connection per call and retries once on a dropped connection. No
  user-facing behavior change beyond memory recall actually working.

## [0.67.1] - 2026-07-11 — reload now reaches delegations

### Fixed

- Configuration changes now take effect for specialist delegations after a
  `casa_reload`: the internal role map that `delegate_to_agent` resolves
  against was built once at boot and never refreshed, so edits such as
  granting a newly installed plugin's tools to a specialist silently
  required a full add-on restart. Reload reports a new
  `refresh_role_map` action when the map is rebuilt.

## [0.67.0] - 2026-07-11 — voice partial-message streaming

### Changed

- Voice replies now start streaming as the model generates them: the first
  spoken chunk arrives after the first sentence or clause instead of after
  the whole reply, and long answers begin speaking almost immediately.
  Text/Telegram behavior is unchanged.

### Fixed

- A voice reply could be silently truncated or garbled if the underlying
  model call was retried mid-stream; the stream now recovers cleanly.

## [0.66.0] - 2026-07-11 — resident SDK client pooling (warm-turn latency floor removed)

### Changed

- Resident turns now reuse a warm Claude Agent SDK client per conversation
  instead of spawning a new subprocess + MCP handshake every turn — the
  fixed ~2.3–3.7 s per-turn latency floor is gone; warm voice/text replies
  start streaming in well under 1.5 s. Sessions, `/new`, memory injection,
  and retry behavior are unchanged; warm clients are recycled on idle,
  age, reloads, and shutdown. Set `SDK_CLIENT_POOL=off` (env) to restore
  the previous per-turn behavior.
- New option `sdk_client_pool` (default on) — disable to fall back to
  per-turn sessions.

### Fixed

- Voice barge-in now interrupts the in-flight reply without killing the
  session's warm client, so the next utterance responds immediately.

## [0.65.2] - 2026-07-11 — retry Anthropic API overloads (resilience fix)

### Fixed

- **API overloads (HTTP 529 / `overloaded_error`) are now retried.** They are
  the most common transient Anthropic failure, but the error classifier had no
  rule for them: a 529 carries none of the "rate limit" / "429" / "timeout"
  markers, and the SDK surfaces it as a `ProcessError` whose type name lacks
  the CLI/SDK/Connection markers — so it fell through to `UNKNOWN` and was
  **never retried**, surfacing to the user as "something went wrong" on a
  blip a single backoff would have ridden out. Overloads now classify as
  retryable and use jittered exponential backoff. (Found during a live-test
  coverage survey; connection-class failures were already retried.)

## [0.65.1] - 2026-07-11 — topic cleanup works out of the box (docs correction)

No code changes. The v0.65.0 live verification found that the
**"Delete messages" grant is not needed** for Casa's topic cleanup:
Telegram lets a topic's *creator* delete it with the "Manage topics"
right the bot already has, and every engagement topic is bot-created —
verified end-to-end on a live supergroup (real topic deleted through the
shipped ledger sweep with `can_delete_messages: False`).

### Changed

- DOCS.md no longer asks for the "Delete messages" grant; it documents
  cleanup as working out of the box, with the grant as optional
  insurance. The graceful-degradation path (retry + once-per-boot ask)
  remains in place should Telegram semantics ever change. Your only
  remaining action for an existing install: the **one-time manual sweep**
  of topics from before v0.65.0.

## [0.65.0] - 2026-07-11 — engagement-topic retention & cleanup

Finished engagements used to park their Telegram forum topics in the
engagement supergroup forever — closed, but cluttering the sidebar for
good. Topics now expire the way engagement workspaces always have.

### Added

- **Automatic topic deletion after retention.** When an engagement ends,
  its topic is recorded in a persistent ledger and deleted automatically
  **7 days later** — the same retention window as the engagement's
  workspace. The durable record of the engagement remains the memory
  summary plus Ellen's completion notification. Both engagement drivers
  are covered, including the resume-failure paths after a restart, whose
  topics previously stayed open (and unrecorded) forever.
- **`cleanup_engagement_topics` — on-demand purge, configurator-only.**
  Deletes known finished topics immediately without waiting out
  retention: `scope="due"` (exactly what the next sweep would delete) or
  `scope="all_terminal"`, with a `dry_run` preview. It only ever deletes
  topics recorded in the ledger — active and idle engagements are never
  touched. Ask Ellen to "clean up the engagement group" and she delegates
  to the configurator; the tool is deliberately not granted to Ellen
  herself, because deletion is irreversible.

### Changed

- **One-time setup: grant the bot "Delete messages".** Deleting a forum
  topic requires the `can_delete_messages` admin right in the engagement
  supergroup (DOCS.md Setup now has the step). Until granted, Casa
  degrades gracefully: finished topics are still closed and marked as
  before, deletions are retried at the next sweep, and Casa asks you once
  per boot to grant the right. Note that **deletion is irreversible**: it
  removes the topic and all its messages for every member.
- **Topics from before this release need one manual sweep.** The Telegram
  Bot API cannot enumerate a group's topics, so engagements that finished
  before v0.65.0 are unknown to Casa. Clear the old pile once by hand in
  the Telegram UI; the ledger keeps the sidebar clean from then on.

## [0.64.2] - 2026-07-10 — CI gate flipped to opt-out: 776 more tests protected

No runtime changes. Closes the systemic gap behind v0.64.1's stale tests.

### Changed

- The unit gate (`make test-unit` / CI tier2) now runs **every** test except
  those marked `docker` or `slow`. Previously only `unit`-marked files ran,
  leaving ~20 unmarked files — 776 tests — silently invisible to CI, which
  is exactly how the v0.64.1 stale tests rotted. New test files are now
  gate-protected by default; the `unit` marker is legacy/optional. Gate
  grows 1089 → 1865 tests (~+25 s runtime).

### Fixed

- The one failure the wider gate surfaced: the engagement-cancel test now
  drains the background memory-retention tasks before asserting (same L33
  drift as v0.64.1's force-delete test; cancel retention itself worked all
  along).

## [0.64.1] - 2026-07-10 — test hygiene: two stale tests fixed and brought into the CI gate

No runtime changes. Two long-broken tests that CI never ran (their files
lacked the `unit` marker, so the gate silently deselected them) are fixed
and now gate-protected:

### Fixed

- The force-delete workspace test drains the background memory-retention
  tasks before asserting (retention moved off the critical path in the L33
  fix; the test predates that). The hooks-translation test feeds the
  snake_case `pre_tool_use` input schema instead of the Claude Code
  *output* shape. Live engagement workspaces were verified unaffected —
  hook guards were always written correctly in production.
- Both test files now carry the `unit` marker, growing the CI unit gate
  from 1048 to 1089 tests.

## [0.64.0] - 2026-07-10 — engagement topics: honest messaging; engagement logs: actually captured

Every Claude Code engagement topic used to receive "Remote control URL not yet
available — Telegram-only for now. Will post here if it becomes available
later." — a promise that could never be kept: the headless CLI runs in
non-interactive mode and never emits a remote-control URL, and the log pipe
the watcher tailed was never wired. Both halves fixed.

### Fixed

- **Engagement subprocess output is now actually captured** to
  `/var/log/casa-engagement-<id>/` inside the app container. The s6-rc service
  layout used a nested `log/` directory, which `s6-rc-compile` ignores — the
  CLI's stdout had gone to a pipe with no reader since v0.13.0. Each engagement
  now gets a proper producer/consumer logger pair (pipe held by
  `s6rc-fdholder`, so log lines survive per-turn respawns), the diagnostic log
  relay (`LOG_LEVEL=DEBUG`) works for the first time, and log dirs are cleaned
  up with the workspace when retention expires.
- **Removed the misleading remote-control notice** (and the never-firing
  "Remote control: <url>" companion) from engagement topics, along with the
  URL watcher behind them and the inert `--remote-control` CLI flag.
  Engagements are driven through their Telegram topic; DOCS.md no longer
  advertises iOS/claude.ai remote control (it was never functional — a real
  mechanism is a separate design).

### Changed

- Engagement service teardown, boot replay, and the compile path are now
  robust to crash-torn service pairs (a half-written pair could previously
  wedge every engagement start until manual cleanup). Engagements running
  across the upgrade are migrated to the new logging layout at the first
  restart. Per-engagement log directories are cleaned up by the retention
  sweep and the `delete_engagement_workspace` tool alike; the diagnostic
  log relay runs only when debug logging is enabled.

## [0.63.3] - 2026-07-10 — plugin-developer: private-first repos

Sets the plugin-developer repo-creation policy to **private-first**, per the
operator decision (plugins for the user's own Casa; public/sharing deferred).

### Changed

- **plugin-developer now creates plugin repos `--private`.** Casa installs
  plugins from private repos (the in-container `GITHUB_TOKEN` authenticates the
  clone), so private is sufficient for the user's own agents — and Claude Code
  hard-blocks creating a *public* repo from within an engagement regardless. The
  workspace template and `casa-conventions.md` now direct `gh repo create
  --private` and drop the blocking public/private pre-question (plugin design +
  name are settled in the `superpowers:brainstorming` step). Making a plugin
  public — to share it beyond this Casa — is a deliberate step the user runs
  themselves (`gh repo edit <repo> --visibility public`).

## [0.63.2] - 2026-07-10 — plugin lifecycle polish (uninstall cache sweep, author-object doctrine)

Two minor fixes found during the block-R plugin-lifecycle validation.

### Fixed

- **`uninstall_casa_plugin` now sweeps the shared plugin cache dir** once no
  agent-home still enables the plugin. `claude plugin uninstall` clears the
  agent-home's `enabledPlugins` but leaves the cached plugin under
  `…/plugins/cache/casa-plugins/<name>/` orphaned; the tool now removes it
  (guarded so a plugin still enabled in another agent-home keeps its cache).

### Changed

- **plugin-developer doctrine (`casa-conventions.md`):** now specifies that
  `.claude-plugin/plugin.json` `author` must be an **object** (`{"name": "…"}`),
  not a bare string — Claude Code rejects a string `author` at install time
  (`author: expected object, received string`), which fails the whole install.

## [0.63.1] - 2026-07-10 — plugin install fix (github sources pin via `ref`)

Fixes a plugin-install bug found during the block-R live lifecycle run: no
github-source plugin added via the marketplace tools could be installed.

### Fixed

- **`marketplace_add_plugin` / `marketplace_update_plugin` now pin github
  sources via `ref`, not `sha`.** The bundled Claude Code (2.1.150) rejects a
  `sha` key on a `github` source ("This plugin uses a source type your Claude
  Code version does not support"), so `install_casa_plugin`'s per-agent
  `claude plugin install <name>@casa-plugins` failed for every plugin added via
  the marketplace tools. The marketplace-defaults catalog already used `ref` for
  its github source; the user-marketplace writers now match. (`git-subdir` seed
  entries legitimately keep `sha` alongside `ref` and are unaffected.) Confirmed
  live on CC 2.1.150: a `github`+`ref` entry clears the source-type gate that a
  `github`+`sha` entry fails.

## [0.63.0] - 2026-07-10 — skill-only plugins report ready

Fixes a plugin-management bug found while building the plugin-lifecycle e2e
coverage (block R): `verify_plugin_state` could never report a skill-only
plugin as ready.

### Fixed

- **`verify_plugin_state` no longer requires an MCP server for readiness.**
  Readiness previously ANDed in `mcp_started` (i.e. an `.mcp.json` exists in the
  plugin cache), so a **skill-only** plugin — a recommended pattern that ships
  no MCP server — always reported `ready: false` even when correctly installed
  and functional. Readiness now gates on satisfied system-requirement tools +
  resolved secrets + the absence of MCP startup errors; `mcp_started` is still
  reported for information but is no longer a gate. After a successful install
  the presence of an `.mcp.json` only signals that a server is *declared* —
  whether it works is already covered by the tool and secret checks. Unblocks
  the fast skill-only plugin path for the configurator's install-time readiness
  check.

## [0.62.0] - 2026-07-10 — trust-model consistency + validator robustness

Resolves the three items left open by the cross-surface sweep, per the operator
decision that the HMAC secret is the trust boundary.

### Changed

- **`/invoke` + `/webhook` are now full-trust disclosure surfaces.** Their
  channel trust is raised to `authenticated` (from `external-authenticated`), so
  the agent may disclose private-category facts (financial, medical, contacts,
  schedule, credentials — `policies/disclosure.yaml`) to an HMAC-secret holder,
  the same as the authenticated Telegram DM. Previously the agent withheld
  ("can't share on this channel") even though the read-clearance already loaded
  the facts. **Operational note:** any holder of the webhook secret can now
  elicit private facts via `/invoke` — the secret is the trust boundary.
- **Read-clearance now fails CLOSED for unmapped channels.** `_DEFAULT_CLEARANCE`
  is `public` (was `private`); every real ingress (telegram/voice/webhook) is
  explicitly mapped, so an unknown/future channel — or the rare orphan-recovery
  notification replayed with an empty channel — reads at the least-sensitive
  tier instead of silently getting full private access.

### Fixed

- **`validate_config_repo` is env-independent.** `resolve_model` treats an
  unresolved `${VAR}` model placeholder as a deferred value rather than raising,
  so any caller that validates `runtime.yaml` without the model env exported
  (config_sync, the live invariant auditor, the configurator's pre-commit gate,
  future tooling) no longer false-positives on `Unknown model shortname
  '${PRIMARY_AGENT_MODEL}'`. Boot is unaffected (the value is substituted before
  it reaches the resolver) and still rejects genuine typos. Generalises the
  point-local v0.59.3 (D1) fix.

## [0.61.0] - 2026-07-10 — cross-surface fixes (voice recall + clearance intent)

Fixes from the 2026-07-09 cross-surface consistency sweep
(`bug-review-2026-07-09-cross-surface.md`).

### Fixed

- **Voice was memory-blind (X1).** The voice agent (butler) holds the
  `recall_memory` tool (since v0.59.2) but its prompt never told it to use it,
  so on voice it answered "each conversation starts fresh for me" and never
  recalled — not even facts it is cleared to read. Added a "Using your
  long-term memory" section to butler's system prompt directing it to
  `recall_memory` before saying it doesn't know, and correcting the "start
  fresh" misconception.

### Changed

- **Read-clearance is now explicit per channel (X2).** `CLEARANCE_BY_CHANNEL`
  maps `telegram`→private, `voice`→friends, `webhook`→private explicitly, so
  each grant is an intentional, tested decision rather than an accident of the
  fallback. Per operator decision, the HMAC secret is the trust boundary for
  `/invoke` + `/webhook`, so those read at full (private) clearance like the DM.
  (The fail-open default for genuinely-unmapped channels is unchanged pending a
  separate review.)

## [0.60.0] - 2026-07-09 — per-agent capability boot log

### Added

- **Capability boot log.** Every agent construction (boot AND reload) emits one
  INFO `agent_capabilities` line — `role`, `model`, `enabled`, tool count + the
  sorted allowed-tool list, and the declared MCP servers. Capability drift (a
  tool grant vanishing after a `config_sync` reconcile, an MCP server going
  undeclared — the shape behind the v0.59.2 `recall_memory` incident) is now
  visible in `docker logs` and diffable across deploys. Best-effort: the line
  can never break agent construction. A runtime backstop complementing the
  v0.59.3 static guards and the mode-matrix contract tests.

## [0.59.3] - 2026-07-09 — seam guards (capability parity + mock drift)

Test-only release — no runtime changes. Hardens against the *class* of bug
behind v0.59.2 and the E-block red streak: seam bugs invisible to any single
module's tests.

### Added

- **Capability-parity suite** (`tests/test_capability_parity.py`): every
  granted tool resolves to a real framework tool / known built-in / wired-or-
  plugin MCP server (catches typos, stale grants for removed tools, MCP grants
  with no server); a curated required-self-use manifest asserts agents that
  depend on a framework tool actually allow it (catches the `recall_memory`
  missing-grant class); every trigger `prompt_file`/`channel` resolves; every
  add-on option has a translation.
- **Mock-drift guard** (`tests/test_mock_telegram_ptb_contract.py`): the mock
  Telegram server's payloads are parsed through the REAL python-telegram-bot
  `de_json` in a subprocess, so a PTB bump or mock edit that would only fail in
  tier2 (as the `getChatMember` payload did across six releases) now fails in
  the fast unit gate. The mock's payloads were refactored into pure builders to
  make them testable (behaviour-identical).

## [0.59.2] - 2026-07-09 — fix: residents can recall memory on-demand

### Fixed

- **Residents could not use `recall_memory` (memory-read regression).** The
  `mcp__casa-framework__recall_memory` pull tool was missing from the assistant's
  and butler's `tools.allowed`. Auto-injected recall fires only on a *fresh*
  session, so on **resumed or scheduled turns** (heartbeat, morning-briefing) the
  primary agent had no memory-read path and reported "memory recall isn't
  permitted this session" — even though its prompts direct it to check memory.
  The **voice channel** (routes to butler, never auto-recalls, no overlay at
  `friends` clearance) had no long-term-memory path at all. Added the tool to
  both residents' allowed lists. A new invariant test asserts any agent whose
  prompt references `recall_memory` actually allows it. (Regression from the
  v0.42–v0.45 tiered-memory rework.)

## [0.59.1] - 2026-07-09 — dependency updates (security)

Dependency-only release — no code changes; re-anchors the published image.

### Changed

- **`aiohttp` 3.13.5 → 3.14.1** — clears 11 security advisories (untrusted-data
  deserialization, HTTP/1 pipelined-request queue exhaustion, websocket
  memory-limit bypass, `client_max_size` bypass, cross-origin redirect
  credential/cookie leaks, CRLF injection in multipart headers, and more), all
  fixed by ≤3.14.1 (Dependabot #97).
- **`claude-agent-sdk` 0.2.87 → 0.2.114** (Dependabot #104) — no breaking
  changes across the range (release notes reviewed); additive typed
  `TaskUpdatedMessage` (0.2.101) and a defensive `mcp<2.0.0` pin (0.2.96,
  consistent with our existing `mcp>=1.28.1,<2`).

## [0.59.0] - 2026-07-09 — memory dedup + observability (live-exploration findings)

Fixes from the 2026-07-09 live exploration session
(`bug-review-2026-07-09-exploration.md`).

### Fixed

- **Memory duplication (F1).** A repetitive conversation used to bloat the
  memory bank with near-duplicate items — one live session produced ~50 copies
  of a single trivial exchange. `transcript_to_items` now (a) collapses
  identical `(speaker, text)` turns within a transcript and (b) derives each
  Hindsight `document_id` from the content instead of `session_id:index`, so the
  same utterance retained from a later (rotated) session upserts to one document
  rather than duplicating. Saying the same thing across ten sessions is now one
  memory, not ten.
- **config_sync false-positive (D1).** The post-sync boot-parity validator ran
  in an s6 oneshot that never exported `PRIMARY_AGENT_MODEL` /
  `VOICE_AGENT_MODEL`, so it saw literal `${…}` placeholders in `runtime.yaml`
  and wrote a bogus `Unknown model shortname` into
  `config-sync-report.json`. `setup-configs.sh` now exports both (env-parity
  with `svc-casa/run`) before running the reconciler; a genuinely bad model
  still fails.

### Changed

- **Memory observability (E1).** Successful retains and recalls now log at INFO
  (`memory_retain bank=… items=…`, `memory_recall bank=… tags=… hits=…`) —
  previously only failures logged, leaving working memory operations invisible.
  Recall never logs the query text (may be sensitive).
- **Turn cost observability (E2).** `turn_done` now includes `cache_read` and
  `cache_write` token counts, so a cached prompt's low `in_tok` with real
  `cost_usd` is explainable and the prompt-cache win is visible in logs.

## [0.58.2] - 2026-07-09 — dependency updates

Dependency-only release — re-anchors the published image tag after the pin
bumps below; no code changes.

### Changed

- `mcp` pinned `>=1.28.1,<2` and `opentelemetry-api` `>=1.43.0`
  (Dependabot #102 / #103).
- CI: `actions/checkout` 4 → 7 (#100); `actions/setup-python` 5 → 6
  (supersedes #101, which conflicted after the checkout bump).

## [0.58.1] - 2026-07-09 — presentation polish (P2)

Repository/presentation release — no runtime behavior changes.

### Added

- Community health files: issue forms (bug report / feature request), PR
  template, `SECURITY.md` (GitHub private vulnerability reporting is enabled),
  `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1), and a
  social-preview banner asset.

### Changed

- Logo regenerated from the icon's palette — no more tagline clipped at the
  right edge.
- Docs wording follows HA's rename: "add-on" → "app" across DOCS.md and the
  option translations (technical identifiers like `addon_config` and
  `/addon_configs/...` paths are unchanged).
- The stale repo-level `CHANGELOG.md` at the repository root is retired; app
  history lives here and in GitHub Releases.

## [0.58.0] - 2026-07-09 — prebuilt images, aarch64, release automation

### Added

- **Prebuilt container images** on GHCR (`ghcr.io/bonzanni/casa-agent`,
  Cosign-signed multi-arch manifest): installing or updating the app now pulls
  an image instead of compiling the Dockerfile on the device. Built and
  published by the new `deploy.yml` workflow using the home-assistant/builder
  composite actions (2026.06.0); pull requests get a validation build.
- **aarch64 support** — Raspberry Pi / Home Assistant Green & Yellow class
  hardware.
- **Release automation**: every merge to master that bumps the version also
  publishes the images and creates the `vX.Y.Z` git tag + GitHub Release with
  this changelog section as the notes.
- **CI**: Home Assistant app linter (`frenck/action-addon-linter`); Dependabot
  for pip and GitHub Actions updates.

### Changed

- Dockerfile now bases on the multi-arch
  `ghcr.io/home-assistant/base-debian:bookworm` manifest via a default
  `BUILD_FROM` arg; the retired `build.yaml` is removed (Supervisor ≥2026.04 no
  longer injects `BUILD_FROM`; local dev builds use the arg default).

## [0.57.1] - 2026-07-09 — publishing readiness: repo shell + green CI

Repository/presentation release — no runtime behavior changes.

### Fixed

- **e2e mock Telegram `getChatMember` (CI red since v0.52.0).** python-telegram-bot
  22.7 parses the response strictly (`User` requires `first_name`/`is_bot`, and
  `ChatMemberAdministrator` requires the full admin-rights field set), so the mock's
  thin payload made the boot-time bot-permissions check fail and disabled
  engagements, breaking the tier2 Engagement E-block. The mock now returns a
  complete `ChatMemberAdministrator` payload. Second independent breakage from
  the same release: M9 made `handle_update` deliver driver turns as a tracked
  background task, so the harness's synchronous asserts raced the delivery —
  the E-block now drains `_turn_tasks` before asserting.

### Added

- **Store presentation:** root `README.md` (add-repository button, badges, app
  list), `casa-agent/README.md` store intro, MIT `LICENSE`.
- **Translations** for the last untranslated options: `hindsight_api_url`,
  `casa_tz`, `log_level`.
- **Dev tooling:** `setup-dev.sh` falls back to uv-managed CPython when the system
  python can't build a venv, and auto-symlinks `docs/` in linked git worktrees;
  `.worktreeinclude`; memory-accuracy eval scripts tracked under `test-local/eval/`.

### Changed

- `repository.yaml`: repository name now "Casa Apps" (HA renamed add-ons → apps);
  maintainer contact switched to the public noreply address.
- AI-attribution policy: commits now use kernel/Fedora-style `Assisted-by: Claude Code`
  trailers instead of `Co-Authored-By`; the root README discloses AI-assisted
  development. Historical trailers are left untouched.

## [0.57.0] - 2026-07-08 — Theme 10: final correctness lows (11 fixes)

### Fixed

- **`svc-casa/run` no longer execs a PATH-resolved `python3` (L1).** The user-writable
  `/config/tools/bin` is prepended ahead of `/opt/casa/venv/bin` in the s6 container PATH (intentional,
  for engagement tool overrides); `svc-casa/run` now execs `/opt/casa/venv/bin/python3` by absolute
  path, matching `svc-casa-mcp/run` and the Dockerfile's stated venv invariant, so a planted/plugin-installed
  `python3` shim can no longer hijack the main process.
- **A non-object `settings.json` no longer permanently disables agent-home provisioning (L2).**
  `provision_agent_home` only self-healed on `JSONDecodeError`; valid-but-non-object JSON (`null`,
  `[]`, a bare string/number) raised `AttributeError` and was swallowed by the per-role try/except,
  silently skipping default-plugin seeding on every boot thereafter. Non-dict JSON is now treated the
  same as invalid JSON — logged and recreated.
- **Voice client context can no longer clobber channel-computed routing identity (L8/L59).** The SSE
  and WS handlers spread the client-supplied `context` dict *after* the channel-computed
  `chat_id`/`utterance_id`/`cid`, letting a client override them and fork SDK session/rate-limiter
  keying from the transport scope and forge log-correlation `cid`s. The client context now spreads
  first so channel-computed keys always win; benign passthrough keys (e.g. `device_id`) still survive.
- **WS per-connection utterance tasks are now pruned and their exceptions retrieved (L9/L60).** The
  `tasks` dict in `_ws_handler` grew one entry per utterance for the life of the connection and never
  retrieved exceptions from failed tasks (logged as "Task exception was never retrieved"). Each task
  now carries a done-callback that prunes its dict entry and logs any exception; the frame-local
  strong reference to the task is also dropped so a finished task is collectable while the connection
  stays open.
- **`engage_executor`'s `context=` argument now reaches the workspace `CLAUDE.md` (L10/L61).** It was
  stored only in the FIFO first-turn prompt — `engagement.origin` never carried a `context` key, so
  `ClaudeCodeDriver.start`'s read of `engagement.origin.get("context", "")` was always empty and the
  persistent `## Context` section rendered blank. `context` (and the world-state summary) are now
  threaded onto the record's `origin` at creation time and read back out by the driver.
- **The "remote control URL not yet available" fallback notice now actually fires (L11/L62).** It lived
  inside the tail loop's per-line branch, so it never ran when the engagement log file never appeared
  (the production reality) or stayed quiet past the window. A detached one-shot timer now posts the
  fallback at the deadline regardless of whether the log ever yields a line, and is cancelled the
  moment a URL is found.
- **Marketplace mutations on legally hand-edited entries no longer crash (L16/L66).** A string-form
  `source` (legal in Claude Code) raised `TypeError` on `update_plugin_entry`, and an entry missing
  `name` raised `KeyError` from add/remove/update — both escaped the module's `MarketplaceError`
  contract. `load_user_marketplace` now validates entry shape up front, and `update_plugin_entry`
  guards the `source` shape before mutating, both raising a clean `MarketplaceError`.
- **The observer's 3-interjection budget is now consumed only on an actual post (L17/L68).** Declined
  or skipped evaluations (no registry record, LLM says no, notify failure) previously still counted
  against the per-engagement cap, so three declined evaluations could silence a later genuine alert.
  `_interject` now reports whether it actually posted, and only those posts increment the counter;
  per-engagement bookkeeping is also pruned on terminal transition to bound memory growth.
- **The MCP `tools/call` forwarding timeout no longer trips on legitimate slow tool calls (L21/L72).**
  The 10s default was shorter than `emit_completion`/`query_engager` routinely take (Telegram
  round-trips, `classify_tier` SDK one-shots, Hindsight recall + synthesis), producing spurious
  `casa_temporarily_unavailable` errors while the call was actually succeeding server-side. Raised to
  180s; the `/hooks/resolve` route is unaffected (keeps its own explicit `timeout_s=None`).
- **`emit_completion` can no longer double-finalize against a racing `/cancel` (L24/L75).** The
  check-then-act between the terminal-status read and the registry write could interleave with a
  concurrent `/cancel` across a real suspension point (e.g. the G-2 forced-reload await), letting both
  paths run finalize side effects (duplicate topic close, duplicate `DelegationComplete`
  notification). `EngagementRegistry.try_transition_terminal` is now the single atomic gate — only the
  first caller to flip the record terminal runs finalize; `cancel_engagement` also now replies
  `already_terminal` instead of silently no-opping against an already-finalized engagement.
- **A cross-agent webhook trigger name collision is now rejected instead of silently rerouting traffic
  (L28/L79).** `register_agent` only rejected duplicate webhook *paths* and duplicate *names within
  the same agent*; two different agents declaring a webhook trigger with the same name (different
  paths) silently overwrote the wildcard `/webhook/{name}` dispatch target, misrouting the first
  agent's webhook traffic to the second. Cross-role name collisions now raise `TriggerError` at
  registration time.

## [0.56.0] - 2026-07-08 — Prompt-cache + hot-path optimizations, Dockerfile slimming, config_sync boot backstop

### Changed

- **Prompt caching no longer defeated by the per-turn `<current_time>` block (M27).** The
  second-resolution timestamp was regenerated into the *system prompt* every turn, so the cached
  prefix changed every second and Anthropic prompt caching was invalidated for the whole
  conversation (system + replayed messages) on every resumed turn. The `<current_time>` block now
  rides on the per-turn *query text* instead, leaving the large stable system-prompt prefix
  byte-identical across turns (cache-eligible) while the agent still sees the current wall-clock time
  to second precision.

- **Hindsight memory reuses one pooled HTTP connection (L32).** `HindsightSemanticMemory` opened and
  tore down a fresh `aiohttp.ClientSession` (new TCP handshake) for every memory call on the
  per-message path. It now lazily creates and reuses a single long-lived session, closed cleanly on
  shutdown (`SemanticMemory.close()`, wired into `casa_core` teardown — no "Unclosed client session"
  warning).

- **`_finalize_engagement` no longer blocks on tier classification (L33).** The two
  `retain_delegated` calls (engagement + executor summaries) each run an LLM tier-classification
  subprocess; they now run as background tasks (strong-ref'd with exception-logging done-callbacks)
  instead of inline `await`s, so `emit_completion` / `/cancel` return promptly. The rare
  deferred-hard-reload path drains them before the Supervisor restart so the H-1 ordering invariant
  ("all retain writes have landed") still holds.

- **`/new` transcript classification parallelized + acks first (M29).** `transcript_to_items`
  classified every transcript item with a sequential full SDK query, so `/new` on a long
  conversation blocked for minutes; classification now runs with bounded concurrency
  (`asyncio.gather` + a semaphore of 4), preserving item order, tags and idempotent `document_id`s.
  The Telegram `/new` handler also sends its "Starting fresh" ack *before* the save so the user gets
  instant feedback (`reset_channel` stays awaited for crash-durability).

### Removed

- **Dead superpowers v5.0.7 baseline clone dropped from the image (L30).** The Dockerfile cloned
  `obra/superpowers@v5.0.7` into `/opt/casa/claude-plugins/base` on every build, but
  `provision_workspace` has ignored `base_plugins_root` since v0.14.x (plugin symlinks removed).
  Deleted the clone layer and both stale `ARG SUPERPOWERS_REF` pins (the live pin lives solely in
  `marketplace-defaults/.claude-plugin/marketplace.json`, superpowers v5.1.0), and dropped the dead
  `base_plugins_root` parameter from `provision_workspace`, `ClaudeCodeDriver.__init__`, and every
  call site. No add-on option removed.

### Fixed

- **Dockerfile layer order: seed install now precedes `COPY rootfs /` (L31).** The network-bound
  plugin-seed install (marketplace add + 5 GitHub plugin installs) sat *after* the broad
  `COPY rootfs /`, so any code change busted its layer cache and re-ran all installs on every
  rebuild. The seed block (with its narrow gitconfig / credential-helper / marketplace-defaults
  inputs) now runs before the broad COPY; a code edit no longer re-runs the seed install. Same
  reorder applied to `test-local/Dockerfile.test`.

- **`config_sync` post-sync boot-parity backstop (Finding 2).** Deleting an image-owned
  `agents/<role>/delegates.yaml` passes the pre-commit gate (the committed tree is internally
  valid), but `config_sync` re-injects the image-owned file post-commit, producing a
  delegates-without-delegate-tool mismatch that FATALs the next boot. After reconciling, config_sync
  now validates the POST-SYNC `/config` tree with `agent_loader.validate_config_repo` (the hardened
  v0.55.0 boot-parity loader); it self-heals the specific re-injected-delegates case (removing the
  image-owned copy, only when byte-equal to the default) and surfaces any residual error loudly in
  the sync report + logs. Best-effort and boot-safe — the backstop never itself crashes boot.

## [0.55.0] - 2026-07-08 — Boot/driver robustness + concurrency mediums

### Fixed

- **Boot-replay no longer plants a service for a vanished workspace (M7).** When a UNDERGOING
  `claude_code` engagement's s6 service dir was gone but its `/data/engagements/<id>/` workspace was
  also wiped (partial `/data` restore, operator `rm -rf`), `replay_undergoing_engagements` re-planted
  and started the service anyway; the generated run script does `set -e; cd <workspace>`, so it
  exited immediately and s6 respawned it forever. The heal loop now checks the workspace dir exists
  and warn-and-skips when it doesn't (4a.1 §7.3), matching the documented contract. Added an
  `engagements_root` kwarg (defaulted to `/data/engagements`) so the check is testable.

- **`config_git_commit` pre-commit gate now enforces boot-fatal cross-file invariants (M5).**
  `validate_config_repo` only ran per-file JSON-schema validation, so a commit could pass the gate
  yet crash-loop the add-on on the next boot (e.g. a copied resident dir still declaring
  `role: assistant`, a stray unknown file in a resident dir, a schema-valid `executors.yaml` on a
  non-assistant role, a non-empty `delegates.yaml` without the delegate MCP tool whitelisted, or a
  stray non-directory file directly under `agents/` — which `load_all_agents` fatals on).
  The gate now runs a boot-parity pass that exercises the real resident loader (`load_agent_from_dir`)
  and refuses those commits. The parity pass also refuses a committed tree with **no primary
  assistant** — only a non-assistant resident (e.g. `butler`), an empty `agents/` dir, or a sole
  disabled specialist carrying `role: assistant` — which passes every per-file check yet crash-loops
  boot on `casa_core.main`'s "No agent with role 'assistant'" `RuntimeError`.
  Known limitation (by design): the gate validates only the committed tree under `config_dir`; it does
  not simulate `config_sync`'s post-commit re-injection of image-owned defaults (e.g. a committed
  deletion of the image-owned `agents/assistant/delegates.yaml`, which is internally valid here but is
  restored by `config_sync` at boot). That reconciler mismatch is a `config_sync` backstop, not a
  gate-replay defect.

- **`_write_to_fifo` can no longer hang a pooled executor thread forever (M13).** Opening the
  engagement stdin FIFO for writing with a blocking `open()` inside `asyncio.to_thread` parked an
  (uncancellable) pool thread indefinitely when the s6 service had no reader (downed/crash-looping
  service); a handful of stuck writes starved all subprocess orchestration app-wide. It now opens and
  writes non-blocking (`O_NONBLOCK` + `select`-free poll) under a bounded deadline, drops the turn and
  notifies the topic if no reader appears in time.

- **`InCasaDriver.start` rolls back the opened SDK client when the first turn fails (M14).** A
  first-turn `_deliver_turn` failure propagated to `engage_executor` (which marks the record error),
  but error-status records are excluded from `active_and_idle()`, so no sweeper ever tore the client
  down — the opened `claude` subprocess leaked until Casa restarted. `start` now closes + deregisters
  the client via `cancel()` on first-turn failure, then re-raises (the Bug-13 rollback the
  `claude_code` driver already had).

- **Boot reconciler no longer masks a broken install as ready (M23).** `_resolves` (and the
  `verify_plugin_state` MCP tool) treated a **dangling** symlink in `/config/tools/bin` as a resolving
  `verify_bin` via `is_symlink()`, so a rolled-back/wiped plugin was reported `ready` and the boot
  exited 0. Both now use `is_file()` (which follows symlinks and is False for a dangling link), so a
  broken install is correctly reported `degraded`/`missing`.

- **`finish_save`/`clear_save_claim` no longer delete a newly-registered session (M24).** During a
  slow multi-minute freshness-reaper save, a concurrent user turn re-registers the channel with a new
  `sdk_session_id`; `finish_save` then unconditionally popped the entry, wiping the fresh
  registration (mid-conversation amnesia + an orphaned, never-retained transcript). Both methods now
  take an optional `sdk_session_id` and only mutate the entry when it still matches the saved session.

- **npm install strategy namespaces its prefix per plugin (M25).** All npm-type plugins installed into
  one shared `tools_root/npm` prefix, reported as `install_dir`; the two-stage-commit rollback
  (`shutil.rmtree(install_dir)`) therefore wiped `node_modules` for **every** npm plugin and dangled
  their symlinks. The prefix is now `tools_root/npm/<plugin>` (mirroring `venv-<plugin>`), isolating
  rollback. Existing deployments re-namespace on the next install of each plugin.

- **`peek_engagement_workspace` reads at most `max_bytes` off disk (M26).** It called `read_text()` on
  the whole file before slicing, so peeking a multi-GB workspace log loaded the entire file into RAM
  (likely OOM-killing the container) and blocked the event loop. It now reads only the capped byte
  prefix in a thread and decodes it, honouring the documented byte cap.

## [0.54.0] - 2026-07-08 — Hygiene sweep: dead config keys, resource leaks, and small correctness lows

### Removed

- **`subagent_model` add-on option removed (M1).** It was declared in `config.yaml`'s options +
  schema, exported as `SUBAGENT_MODEL` by `svc-casa/run`, and documented in `DOCS.md` /
  `translations/en.yaml` — but no code anywhere ever consumed it (executors and specialists
  hardcode `model: sonnet` in their `definition.yaml`/`runtime.yaml`). Removed the option, its
  export, and its docs; appended `subagent_model` to `DEPRECATED_OPTION_KEYS` in
  `setup-configs.sh` so any stored value is pruned on boot.

### Fixed

- **`telegram_bot_api_base` add-on option is now actually wired to the casa process (M2).** The
  option was consumed by `channels/telegram.py` via `os.environ.get("TELEGRAM_BOT_API_BASE")`, but
  `svc-casa/run` never exported it — only the local e2e test harness did — so a self-hosted Bot API
  server configured via the add-on UI was silently ignored since v0.12.0. `svc-casa/run` now reads
  and exports it (null-normalized, matching the existing optional-string pattern).
- **`webhook_auth_enabled: false` now actually disables webhook auth (L50).** `svc-casa/run`
  exported `WEBHOOK_SECRET` unconditionally from the `webhook_secret` option, so setting the toggle
  off had no effect once a secret value was configured — `casa_core`'s auth-enabled check is purely
  "is the secret non-empty". The export is now gated on `webhook_auth_enabled`.
- **`casactl reload` now accepts `--scope=config_sync` (L80/L29).** The v0.47.0 `config_sync` reload
  scope was registered server-side and advertised by the `casa_reload` MCP tool, but the operator
  CLI's argparse `choices` predated it, so `casactl reload --scope=config_sync` failed with "invalid
  choice" even though the equivalent `POST /admin/reload` succeeded. Added to `casactl` and to the
  configurator's `reload.md` doctrine table (now "eight reload scopes").
- **`_synthesize_answer` now honors its `max_tokens` argument (L76/L25).** `query_engager`'s bounded
  synthesis pass built `ClaudeAgentOptions` without ever applying the caller-supplied token budget,
  so answers were effectively unbounded. Caps output via the `CLAUDE_CODE_MAX_OUTPUT_TOKENS` CLI env
  knob, adds a budget instruction to the synthesis prompt, and hard-truncates any overshoot as a
  belt-and-braces stop; the tool-level arg is also clamped to `[1, 4000]`.
- **`casa_reload_triggers` now enforces the same privileged-role guard as `casa_reload(scope='triggers')` (L77/L26).**
  The Bug 7 (v0.14.6) defense-in-depth check — refuse callers whose effective role isn't
  `configurator` — covered `config_git_commit`, `casa_reload`, and `casa_restart_supervised`, but its
  back-compat alias `casa_reload_triggers` had no such check, so a misconfigured agent's
  `allowed_tools` could re-register another role's triggers with no refusal.
- **A failed engagement start no longer leaks an open Telegram forum topic (L74/L23).**
  `engage_executor` and `delegate_to_agent`'s interactive path create the forum topic before
  starting the driver; when the prompt template was missing or `driver.start` raised, the topic was
  never closed — only `_finalize_engagement` (never reached on these failure paths) does that. Added
  a best-effort `_abort_engagement_topic` helper that flips the topic to `failed` and closes it on
  every `no_driver` / `driver_start_failed` / `prompt_template_missing` path, without routing through
  `_finalize_engagement` (which would double-notify Ellen and run memory-retention side effects).
- **`POST /invoke/{agent}` now returns 400 instead of 500 for non-object JSON bodies and
  `"context": null` (L3).** A body that parsed to a non-dict, or an explicit `"context": null` /
  non-dict context, crashed with an unhandled `AttributeError`/`TypeError` instead of the handler's
  own 400 contract. `invoke_handler` is now extracted into a testable `_make_invoke_handler`
  factory (mirroring `_make_webhook_handler`) with both cases validated.
- **`/internal/hooks/resolve` no longer crashes with HTTP 500 on valid-JSON non-object bodies
  (L65/L14).** A body that parsed to a list/string/number, or a truthy non-dict `payload`, raised an
  unguarded `AttributeError`/`TypeError`; `svc_casa_mcp` then surfaced a misleading "forwarder error"
  deny instead of the intended structured deny. The handler now validates body/policy/payload shape
  and returns the same structured fail-closed deny used for malformed JSON.

### Leak fixes

- **`PERMISSION_QUEUES` entries are now evicted at engagement finalization (L5).** The per-engagement
  `asyncio.Queue` (and any undrained verdict inside it) previously persisted in memory for the
  process lifetime. `_finalize_engagement` now pops the entry, and the verdict-POST handler refuses
  to re-materialize a queue for an engagement that is no longer `active`/`idle`.
- **Compiled s6-rc databases in `/tmp` are now reaped (L63/L12).** Every engagement lifecycle
  compile (`s6-rc-compile` into `/tmp/s6-casa-db-<uuid>`) left the previously-live db orphaned.
  `_compile_and_update_locked` now removes the prior live db after a successful swap (or the
  just-compiled db after a failed one); a new `sweep_orphan_compiled_dbs()` also reaps stale dirs
  from a prior container run during boot replay.
- **`plugin-env.conf` is now created 0600 atomically (L69/L18).** The secrets file was written with
  default umask permissions (typically 0644) and only chmod'd to 0600 afterward — a crash or denied
  chmod between the two steps left the secrets file world-readable. It is now opened with
  `os.O_CREAT` and mode `0o600` from the first byte; the trailing chmod remains as a belt-and-braces
  repair for any legacy 0644 file.
- **`RateLimiter` buckets are now evicted when idle (L70/L19).** Every distinct rate-limit key (e.g.
  an arbitrary Telegram `chat_id` from any sender) permanently allocated a `TokenBucket`, growing the
  per-key dict without bound for the process lifetime. A periodic sweep (every 1024 checks) now
  drops buckets idle for a full `window_s`, which is behaviorally invisible — an idle bucket has
  already refilled to full capacity, identical to a fresh one.

## [0.53.0] - 2026-07-08 — Silent hangs & cross-module contract drift (bus REQUEST resolution, executor hook params, observer drain, permission-relay correlation)

### Fixed

Five defects where a request/response path never resolved, a bus queue was never drained, executor hook params never reached the enforcer, or a permission verdict reached the wrong waiter:

- **A bus REQUEST now ALWAYS resolves its caller's future (M4 + M6).** A REQUEST whose handler
  produced empty/suppressed output — `Agent.handle_message` returning `None` on a `<silent/>` or
  no-text turn — left the pending future unresolved, so voice SSE/WebSocket and `POST /invoke`
  (all `bus.request(timeout=300)`) hung the full ~300s and then surfaced a spurious timeout for a
  turn that actually completed. Fixed on both sides of the contract: `bus.run_agent_loop._dispatch`
  now resolves a REQUEST with an explicit empty `RESPONSE` when the handler returns without
  responding (guarded by `msg.id in self.pending`, so NOTIFY / fire-and-forget stay a no-op), and
  `Agent.handle_message` now returns a typed empty `RESPONSE` for REQUEST turns instead of `None`
  (channel delivery of the empty text is still suppressed). `test_bus.py::test_request_timeout`
  reworked to register a handler-less target for the genuine timeout path.

- **Executor `hooks.yaml` parameters now reach the claude_code HTTP hook path (H3).**
  `_build_cc_hook_policies` invoked every factory with no kwargs, so the `/hooks/resolve` path (the
  only enforcement path for claude_code engagements) ran default-configured policies — an empty
  `path_scope` that denied ALL Read/Write/Edit for a plugin-developer engagement, and
  `commit_size_guard` at the wrong `max_files`. New `hooks.build_policy_callbacks_from_hooks_yaml`
  + `casa_core._build_executor_cc_hook_policies` build per-executor parameterised callbacks from the
  executor's `hooks.yaml`; the resolve handler resolves the engagement from the payload `cwd` and
  prefers that executor's callback, falling back to the defaults for unknown engagements. Boot-time
  snapshot (an operator edit needs a restart to affect the HTTP path).

- **The observer bus queue is now drained (H4).** `observer.subscribe()` registered an `observer`
  target queue + handler, but the boot loop spawned `run_agent_loop` consumers only for resident
  roles + `telegram`, so every engagement event sent to `target='observer'` (subprocess_respawn,
  idle_detected, error tool_results) enqueued forever with no consumer — operator interjections
  never fired and the queue leaked for the process lifetime. New `_bus_loop_targets(agents)` adds
  `observer` (deduped) to the spawn list; the tracked task is cancelled on shutdown with the others.

- **Concurrent permission requests each receive THEIR OWN verdict (M18).** All pending permission
  requests for an engagement shared one `asyncio.Queue`, and `_await_matching_verdict` discarded any
  item whose `request_id` was not its own — so with two parallel tool calls in flight (Claude Code
  issues parallel tools), whichever waiter won `q.get()` for the operator's verdict destroyed it on
  an rid mismatch, denying the approved call by timeout. Verdicts are now correlated by request_id: a
  single per-engagement drain lock lets exactly one waiter read the queue at a time and routes a
  non-matching verdict into a per-`request_id` mailbox for its owning waiter, so cross-delivery is
  impossible and the stale-click defence still holds.

### Fixed

Seven Telegram-channel defects, all in `channels/telegram.py` (plus one agent hook):

- **Webhook ACK no longer blocks on the SDK turn (H5).** `process_webhook_update` awaited
  `Application.process_update`, which (default `block=True` handlers) ran the ENTIRE engagement
  SDK turn — minutes — before the aiohttp route could return 200. Telegram timed out and
  redelivered the update, duplicating turns. The update is now enqueued onto PTB's
  `update_queue` (the fetcher started by `app.start()` drains it) so the route returns in
  milliseconds, both message and callback handlers are registered `block=False` so one long
  turn can't stall PTB's sequential fetcher (which in polling mode also froze Ellen DMs), and a
  bounded `update_id` LRU drops any redelivery already in flight before the first ACK landed.

- **`_teardown_app` runs each step independently (M8).** A failing `delete_webhook` (common
  during the very outage that triggered the rebuild) used to skip `app.stop()`/`shutdown()`,
  leaking the started Application's fetcher task, JobQueue, and HTTPX pools on every reload.
  Each teardown step now has its own try/except, and `_rebuild` rolls back a half-started
  Application if any bring-up step raises before re-raising to the supervisor.

- **`/cancel` can interrupt an in-flight turn (M9).** The per-topic lock was held across the
  whole multi-minute turn, so `/cancel` queued behind the turn it was meant to kill. The user
  turn is now delivered in a tracked background task (strong ref + done-callback), so the lock
  is released as soon as routing/validation completes and `/cancel` acquires it immediately.
  The Bug-10 status re-check still runs under the lock before any task is spawned.

- **`/cancel@botname` is recognized (M10).** Group command menus send `/cancel@<botusername>`;
  the matcher now strips the bot's own `@mention` suffix (commands addressed to a different bot
  fall through to the agent, matching PTB's `CommandHandler`). The bot username is cached at
  engagement setup.

- **Permission-relay keyboard escapes MarkdownV2 (M11).** `post_perm_keyboard` sent tool names
  and previews as MarkdownV2 without escaping, so an MCP tool name (`mcp__x__y`) or a Bash
  preview with a backtick/backslash triggered a Telegram 400 that the relay hook turned into a
  silent auto-DENY. Reserved characters are now escaped (general escaping for the bold tool
  name, pre/code escaping for the fenced preview), with a plain-text fallback on any residual
  parse failure.

- **Typing circuit breaker no longer trips on transient outages (L6).** A transient
  `NetworkError`/`TimedOut` used to count toward the 401 breaker, which then never reset —
  killing typing indicators for the process lifetime. Transport errors now back off without
  counting toward the breaker (the reconnect supervisor owns transport recovery), and a
  successful `_rebuild` heals a previously-tripped breaker.

- **Typing indicator stops after an empty/silent turn (L7).** A turn that strips to empty or
  `<silent/>` never called `send()`/`finalize_stream()`, so the typing loop ran forever
  (permanent "typing…" plus a Bot API call every 4 s, notably in block mode). `agent.py` now
  calls a new `turn_finished()` channel hook on the suppressed-turn path, which stops the
  per-chat typing indicator.

## [0.51.0] - 2026-07-08 — crash-safe on-disk state writes (atomic writes + tolerant load)

### Fixed

On-disk state files were written with a plain truncate-in-place `open("w")` + `json.dump`
(or `write_text`) directly over the live file, so a crash or power-loss mid-write could
leave a truncated/corrupt file. In the worst case a corrupt `sessions.json` was then loaded
intolerantly and crash-looped the add-on on every boot. All such writes now route through a
new shared atomic-write helper (`atomic_io.py`): write to a same-directory temp file, fsync,
then `os.replace` — so a crash can never expose a half-written file. The helper preserves the
prior `open("w")` permission semantics — an existing file keeps its current mode and a fresh
file lands at `0o644` — so it never leaks the `tempfile.mkstemp()` `0o600` onto the replaced
inode.

- **`sessions.json` crash-loop eliminated (H12).** `SessionRegistry._write` is now atomic,
  and `__init__` loads tolerantly: a corrupt/unreadable (or wrong-shape) registry is logged,
  quarantined to `sessions.json.corrupt`, and the fleet starts from an empty registry instead
  of raising and dying on boot. Losing session pointers is recoverable; a boot crash-stop was
  not.
- **Engagement tombstone atomic (M15).** `engagement_registry._write_tombstone` no longer
  risks losing all in-flight engagement state to a truncated `engagements.json`.
- **Delegation tombstone atomic (L20).** `specialist_registry._write_tombstone` — the exact
  file that exists for delegation crash recovery — is now crash-safe.
- **Marketplace + system-requirements manifests atomic (L15, L).** `marketplace_ops._write`
  and `system_requirements/manifest._write` no longer risk bricking marketplace ops / the
  crash-recovery manifest with a truncated file.
- **Config-sync no longer silently destroys user edits when git is failing (M12).** The
  image-wins conflict/backstop paths only wrote a `.casabak` backup when git was entirely
  unavailable. `RealGit.snapshot()` now fails closed (returns `None` on any git error —
  dubious-ownership, a stale `index.lock`, a corrupt repo — instead of returning a stale
  pre-edit HEAD), and both overwrite sites now write a `.casabak` whenever no commit actually
  captured the edit, so an operator's config edit is always recoverable. The boot-time
  snapshot in `setup-configs.sh` also stops logging false success when its commit failed.

### Tests

New crash-simulation unit tests (`test_atomic_io.py` plus additions to the registry,
marketplace, manifest, and config-sync suites) assert the original file stays intact when a
crash is injected between temp-write and `os.replace`, that a corrupt `sessions.json` loads
empty and is quarantined, and that a broken-git conflict falls back to `.casabak`. The
`test_session_registry.py`, `test_engagement_registry.py`, and `test_specialist_registry.py`
suites gained the `unit` marker so the tier2 gate actually runs them.

## [0.50.0] - 2026-07-08 — security hardening: ingress source filter, auth/parsing controls

### Security

Seven security fixes closing authentication, path-traversal, command-parsing, and
secret-handling gaps. Several were controls that existed on paper but were bypassable in
practice; each fix ships with an attack-encoding regression test (the affected test files
also gained the `unit` marker so the tier2 gate actually runs them).

- **nginx ingress now restricts to the Supervisor proxy (H1).** The generated ingress
  `server` block was missing the HA-mandated source filter, so any peer container on the
  hassio bridge could reach the operator dashboard, all proxied API routes, and the web
  terminal (an unauthenticated root shell when `enable_terminal` is on) with HA's ingress
  auth fully bypassed. Added `allow 172.30.32.2; deny all;` at server scope (per
  developers.home-assistant.io), so it filters every route including `/terminal/`.
  Defense-in-depth: the aiohttp backend now binds `127.0.0.1:8099` instead of `0.0.0.0:8099`
  (its only legitimate consumer is nginx in the same container).
- **`telegram_chat_id` is now enforced as an allowlist (H6).** The option is documented as
  "restrict messages to this chat" but was never applied — any Telegram user who found the
  bot got full agent access (home control + shared memory). When `telegram_chat_id` is set,
  updates from any other chat are now dropped (logged, not answered). Empty/unset still
  accepts all chats (documented default); the engagement supergroup and its forum topics,
  and the configured DM, are unaffected. No option removed → no `DEPRECATED_OPTION_KEYS`
  change; DOCS.md already described this behavior.
- **`peek_engagement_workspace` path-traversal closed (H15).** Only the `path` argument was
  guarded; the `engagement_id` was joined into the workspace root unchecked, so `..` or an
  absolute id re-rooted the "workspace" anywhere on disk (leaking `/data/options.json`,
  `plugin-env.conf`, etc.) via the unauthenticated 8099 MCP fallback. The id is now validated
  (`[A-Za-z0-9_-]+`) and the resolved workspace must sit directly under the engagements root.
- **`block_dangerous_bash` no longer bypassed by newlines or quotes (H8 + L13).** Newlines
  are now first-class command separators (so `echo hi\nrm -rf /` is caught on line 2), with
  backslash-newline continuations collapsed first. The pipeline splitter is now quote-aware
  (shlex `punctuation_chars`), so operators inside quoted strings are data, not boundaries —
  fixing both the newline bypass and the false-positive denials of benign commands like
  `git commit -m "cleanup && rm -rf handling"`. Security review of this fix found the
  substitution/exec-wrapper class still open; the detector now also recurses into command
  substitution (`echo $(rm -rf /)`, including double-quoted), backticks, `eval "rm -rf /"`,
  and `… | xargs rm -rf` — while `awk '{print $(NF-1)}'`, `echo $((1+2))`, and
  `eval "$(ssh-agent -s)"` stay allowed (denies only when the *inner* content is dangerous).
- **`casa_config_guard` resident-deletion guard hardened (M16).** The brittle regex was
  evaded by quoted paths, long flags (`--recursive`), and `--`. Replaced with an argv-aware
  detector (shared splitter + path normalization + wrapper-shell recursion) that catches
  every spelling while still exempting `specialists/` and `executors/` subtrees. Security
  review found one residual hole: a leading `//` (which the Linux kernel resolves as `/`,
  but PurePosixPath preserves as a distinct root) slipped past every prefix check — both
  `rm -r //config/agents/<name>` and `Write //data/…`. Path normalization now collapses
  redundant slashes first, and the guard also recurses into `eval` (same wrapper class as
  `bash -c`).
- **Command-parsing guards round 2: `|&`/`;&`/`;;&` now split pipelines; exec-wrapper
  prefixes unwrapped (H8/M16 follow-up).** shlex emits `|&` (pipe stdout+stderr) and the
  case-branch terminators `;&`/`;;&` as single tokens that were missing from the
  pipeline-separator set, so `echo x |& rm -rf /` merged into one argv and the right-hand
  command was never scanned as argv[0]. True redirections (`>`, `>>`, `<`, `>&`, `&>`,
  `2>&1`) still do not split. Exec-wrapper prefixes (`nohup`, `timeout`, `env`, `stdbuf`,
  `setsid`, `time`, `nice`, `ionice`, `chrt`, `taskset`, `unbuffer`, `sudo`, `doas`) are now
  unwrapped in both `block_dangerous_bash` and the resident-deletion guard, so
  `timeout 5 rm -rf /` and `nohup rm -r /config/agents/<name>` resolve to the same decision
  as the bare command (arg-consuming forms like `timeout 5`, `env A=B`, `nice -n 5`,
  `sudo -u root` handled). These guards remain defense-in-depth behind the SDK permission
  system and workspace isolation; known residuals: command/process substitution and non-rm
  destructive verbs (e.g. `find -delete`, `truncate`, `shred`) are not decomposed.
- **Anthropic API keys are now redacted from logs (M19).** The `sk-` redaction pattern could
  never match `sk-ant-api03-…` / `sk-ant-oat01-…` (the hyphen after `ant` broke it), so
  Casa's own primary credential could leak into logs. Added an explicit `sk-ant-` pattern
  ahead of the generic one.
- **Constant-time Telegram webhook token check (L4).** The `X-Telegram-Bot-Api-Secret-Token`
  header was compared with `!=` (timing side-channel); it now uses `hmac.compare_digest` with
  both sides byte-encoded (non-ASCII header → 403, not 500). The handler was extracted into a
  unit-testable factory.

## [0.49.0] - 2026-07-08 — reload subsystem: memory wiring, resident lifecycle, lock + env-drop fixes

### Fixed

Five interconnected defects in the reload subsystem (`reload.py`). The configurator invokes
`casa_reload` routinely after config edits (scope=`agent`|`policies`|`executors`|`full`), so all
of these fired in normal operation, not edge cases. They were invisible to the unit gate because
the existing reload tests stubbed exactly the seams that were broken (`_construct_agent`,
MagicMock bus); the new regression tests drive the real factory and the real `MessageBus`.

- **Reloaded residents no longer lose Hindsight memory (H9).** `reload._construct_agent` — used
  by every reload scope — omitted `semantic_memory` when rebuilding an Agent, so from the first
  reload until the next add-on restart every resident silently fell back to
  `NoOpSemanticMemory`: per-turn overlay/auto-recall returned nothing and cold-session retains
  were permanently lost (a v0.45.0 memory-retirement regression). `CasaRuntime` now carries the
  boot-built `semantic_memory` (new defaulted field, kept last) and the factory passes it through.
- **Residents added via reload now actually consume their queue (H10).** Bus consumer tasks
  (`run_agent_loop`) were only spawned at boot, so a resident created at runtime +
  `casa_reload(scope='agents'|'full')` was registered on the bus but nothing ever read its
  queue — cron triggers, webhooks, and NOTIFICATIONs targeting it sat enqueued (and `/invoke`
  504'd) until a container restart. The bus now owns the consumer lifecycle:
  `MessageBus.start_agent_loop(name)` spawns an idempotent tracked consumer; boot and every
  reload registration path go through it.
- **Evicted residents no longer keep running as ghost agents (H11).** Eviction called
  `bus.unregister(...)` — a method `MessageBus` never had; the `AttributeError` was swallowed, so
  a deleted resident kept its queue, handler, live consumer, APScheduler jobs, and webhook
  allowlist entries, and went on executing scheduled prompts until restart. `MessageBus.unregister`
  now exists (cancels the tracked consumer task — awaited by the eviction path — and drops the
  queue + handler, so later sends silently drop), and eviction also unwinds the role's triggers
  via `trigger_registry.reregister_for(role, [], [])`. Add and remove are now symmetric:
  register + start loop ⇄ cancel loop + unregister + trigger unwind.
- **`scope='full'` is now actually exclusive (M21).** The reload `_RWLock` writer path recorded
  no lock state, so a `full` reload was not mutually exclusive with concurrent per-scope
  reloads — both could interleave their multi-step mutations of `runtime.agents` /
  `role_configs` / `agent_registry` across `to_thread` awaits. The lock now tracks an active
  writer: readers wait for it, and the writer waits for both readers and any prior writer.
- **First `plugin_env` reload can now drop boot-applied keys (M22).** The deletion diff in
  `reload_plugin_env` compared against a snapshot that started empty and was never seeded by the
  boot path, so a secret removed from `plugin-env.conf` after boot survived in `os.environ` (and
  kept reaching plugin MCP subprocesses) for the container's lifetime. Boot now seeds the
  snapshot via `reload.note_boot_plugin_env(...)` right after sourcing `plugin-env.conf`.

## [0.48.0] - 2026-07-08 — move blocking I/O off the single event loop

### Fixed

- **No more whole-add-on freezes from blocking calls on the shared event loop.** Casa runs one
  asyncio loop serving every agent and channel (Telegram, voice SSE/WebSocket, scheduler, bus),
  so any synchronous subprocess / download / heavy filesystem walk on it froze *all* conversations
  for its full duration. Seven such call sites are now dispatched off the loop (and the network
  fetch is bounded):
  - **Resident plugin resolution (H2/M20).** `Agent._process` shelled out to
    `claude plugin list --json` (a blocking Node spawn, 30s timeout) on *every* turn. It now runs
    via `asyncio.to_thread` and is cached per Agent instance — the install doctrine already makes
    `casa_reload(scope='agent')` mandatory after a plugin change, and that rebuilds the Agent, so
    the cache can never surface a stale plugin set (a degraded/empty CLI result is not cached, so
    it retries). The three delegation/executor call sites in `tools.py` are offloaded too.
  - **Plugin tarball download (H13).** `install_tarball` used `urlretrieve` with no timeout (global
    default `None`), so a stalled marketplace server hung the loop forever. Now `urlopen(timeout=…)`
    bounds every socket op, and `install_casa_plugin` / `uninstall_casa_plugin` run off the loop.
  - **Plugin / marketplace / 1Password tool handlers (H16).** `install`/`uninstall`/`marketplace_*`
    (`claude plugin …`, up to 300s per role) and the `op` CLI vault handlers now offload via
    `asyncio.to_thread`; a new `_PLUGIN_TOOLS_LOCK` preserves the mutual exclusion the single loop
    used to give the mutating handlers for free.
  - **`commit_size_guard` (M17).** The per-Write/Edit `git status --porcelain` (up to 5s) is
    offloaded.
  - **`self_containment_guard` (M28).** The per-`git push` tree scan now filters by filename
    *before* reading, caps each read at 256 KiB, and runs off the loop.
  - **`list_engagement_workspaces` (L27).** The du-style `os.walk` + `os.stat` over every retained
    workspace is offloaded.

  Deferred to a separate PR: `session_saver.transcript_to_items` sequential SDK classify queries
  (M29) — its fix is architectural (SDK-query concurrency).

## [0.47.1] - 2026-06-08 — prune deprecated add-on option keys on boot

### Added

- **Deprecated-options prune.** On boot, `setup-configs.sh` deletes add-on option keys that
  Casa has removed from its schema (via `bashio::addon.option '<key>'`), so HA Supervisor
  stops logging `Option '<key>' does not exist in the schema` after a field-removing release.
  Warning-level hygiene only — under current HA an unknown stored option is a warning, not a
  crash, and casa already ignores unknown keys; this just silences the recurring warning and
  follows HA's documented recommendation. Seeded from a git-history audit of every option ever
  removed (`github_token`, `heartbeat_enabled`, `heartbeat_interval_minutes`, `honcho_api_key`,
  `honcho_api_url`, `repos`, `scope_threshold`, `telegram_webhook_url`). Additive
  `DEPRECATED_OPTION_KEYS` list; idempotent (no-op on clean installs). Completes the add-on-
  options half of the schema-tightening drift (the `/config` half shipped in v0.47.0).

## [0.47.0] - 2026-06-08 — `/config` default-sync reconciler (no more manual `cp` after a deploy)

### Added

- **Automatic `/config` default sync.** Image-default-owned config under `/config/{agents,policies}`
  now tracks the shipped `/opt/casa/defaults` on every boot (and via
  `casa_reload(scope=config_sync)`) — **including file removals** — so a config change baked into a
  new image takes effect without the manual `cp` that the v0.46.3→v0.46.7 toolbox arc required after
  every deploy. New module `config_sync.py` does a three-way merge (baseline `/data/config-baseline`
  / new defaults / live `/config`): untouched files track the image; genuine runtime edits are
  preserved; on a true conflict the **image wins** after a commit-first snapshot to the `/config` git
  repo (the prior edit stays recoverable), and **Ellen** proactively tells the operator with a
  carry-over offer. A **schema-validation backstop** force-applies the default to any kept-live file
  that is invalid against a newly tightened schema, so casa **always boots** (closes the
  schema-tightening crash-loop class structurally). New configurator doctrine recipe
  `recipes/config/reconcile-defaults.md` drives the operator-initiated carry-over via `git diff`.

### Changed

- **`setup-configs.sh`**: the dir-level `seed_agent_dir` seeder and the warn-only `drift-check` block
  are replaced by the reconciler (which seeds, tracks, and removes per file, and *acts* instead of
  only warning). The `c1-relay-migration` content migration is retained. New persistent state:
  `/data/config-baseline/` (last-synced defaults) and `/data/config-sync-report.json` (per-boot
  result, consumed by the Ellen notification).

## [0.46.7] - 2026-06-07 — configurator secrets doctrine: gitignored plugin-env.conf → empty commit SHA is expected

### Changed

- **`recipes/plugin/secrets.md` now sets the no-SHA expectation explicitly.** A live dogfood —
  driving Ellen → the configurator to wire context7's optional `CONTEXT7_API_KEY` from
  `op://Casa/Context7/credential` — passed cleanly (read recipe → `set_plugin_env_reference` →
  `config_git_commit` → `casa_reload(scope='plugin_env')` → `emit_completion`). It surfaced one
  latent ambiguity: `plugin-env.conf` is a mode-0600 **gitignored** secrets file, so
  `config_git_commit` after a secret-only change stages nothing and returns an empty SHA. Sonnet
  handled it ("gitignored so no commit SHA"), but the canonical `emit_completion` template still said
  `committed SHA <sha>`. The doctrine now states the empty SHA is expected (not a failure) and the
  completion text should say "no SHA (secrets file gitignored)". Doctrine-only change.

## [0.46.6] - 2026-06-07 — context7 re-modeled as a proper plugin (+ configurator doctrine for its optional key)

### Changed

- **context7 is now a real CC plugin, not a driver special-case.** It *is* an official plugin
  (`claude-plugins-official` `external_plugins/context7`), so v0.46.5's driver-level HTTP wiring in
  `drivers/workspace.py` (injecting a context7 MCP server into each engagement `.mcp.json`) was the
  wrong model and is **reverted**. context7 is added to the dev marketplace (pinned sha `bd7cf41`) +
  the plugin-developer's `plugins.yaml` + the image seed install; `mcp__context7` stays allow-listed.
  The plugin brings its own MCP server (`npx @upstash/context7-mcp`).

### Added

- **Configurator doctrine for context7's optional key** (`recipes/plugin/secrets.md`): context7's
  `CONTEXT7_API_KEY` is **optional + not declared** in the plugin's `.mcp.json` (so `install`/`verify`
  don't surface it), and is a **global** env var. The new section tells the configurator to wire it
  via `set_plugin_env_reference(var_name="CONTEXT7_API_KEY", op_ref_or_value="op://…")` →
  `casa_reload(scope='plugin_env')`. Keyless works (rate-limited); the key raises limits.

### Notes

- The official context7 plugin runs `npx -y @upstash/context7-mcp` — the plugin *source* is pinned,
  but the npm package is fetched latest at runtime (needs node in the engagement). Acceptable for now;
  can pin the npm version later if the freeze matters.

## [0.46.5] - 2026-06-05 — plugin-developer: context7 (current library/SDK docs) — toolbox complete

### Added

- **The `plugin-developer` executor now has the `context7` MCP server** — current, version-accurate
  library/SDK/CLI docs (boto3, the MCP SDK, …), so it codes against today's APIs instead of stale
  training memory. The `claude_code` driver wires it into the engagement `.mcp.json` when an executor
  declares `context7` in `mcp_server_names` (per-executor, not hardcoded for all). The hosted endpoint
  `https://mcp.context7.com/mcp` works **keyless** (verified 2026-06-05); an optional
  `CONTEXT7_API_KEY` env raises the rate limits. Server-level allow (`mcp__context7`) auto-approves its
  tools (`resolve-library-id`, `query-docs`). Regression tests added (`.mcp.json` includes context7
  iff declared).

This **completes the plugin-developer toolbox** (v0.46.3 freeze + drop document-skills; v0.46.4 broad
`Bash` + web; v0.46.5 context7). A live end-to-end check happens when the executor is enabled
(currently `enabled: false`).

## [0.46.4] - 2026-06-05 — plugin-developer: broad Bash + web research (it can finally run/test + read docs)

### Changed

- **The `plugin-developer` executor now has broad `Bash` + `WebFetch`/`WebSearch`.** Previously its
  Bash was limited to `Bash(git*)`/`Bash(gh*)` and it had no web/doc access — so it authored code it
  could neither run, test, nor research (coding from stale memory). It now runs open-ended toolchains
  (python/pytest/uv/npm/ruff/tsc/…) and can read docs/examples. Safety is unchanged and lives in the
  hook stack — `block_dangerous_bash` + `path_scope` (writes confined to `/data/engagements/`) + the
  `engagement_permission_relay` (operator approval) — not in a Bash allowlist; `git push` still fires
  `self_containment_guard`. A dev sandbox (isolate *where* dev executors run) is on the roadmap.
- Widened the engagement permission filter (`drivers/workspace.py` `_VALID_CC_PERMISSION_RE`) to
  accept bare `Bash` and `WebFetch`/`WebSearch` (it previously required `Bash(...)` and dropped the
  web tools). Regression test added.

### Notes

- `context7` (structured library/SDK docs) follows in v0.46.5 — it's an MCP server and the engagement
  `.mcp.json` is hardcoded to casa-framework, so it needs a small driver change + an HTTP-vs-`npx`
  decision + a live check.

## [0.46.3] - 2026-06-05 — plugin-developer dev tooling: freeze at official pins + drop mis-bundled document-skills

### Changed

- **Re-sourced + froze the dev-tooling marketplace** (`casa-plugins-defaults`) at official, pinned
  versions — nothing floats now: `superpowers` `v5.0.7`→**`v5.1.0`** (obra/superpowers); the
  `claude-plugins-official` subdirs (`plugin-dev`/`skill-creator`/`mcp-server-dev`) re-pinned
  `020446a`→**`bd7cf41`**.

### Fixed / Removed

- **Removed `document-skills` from the plugin-developer's toolbox.** It is `xlsx/docx/pptx/pdf`
  document *processing* — not plugin-dev tooling — and was mis-bundled: its catalog description
  claimed "mcp-builder, doc-coauthoring, theme-factory", but those live in a *different*
  anthropics/skills pack (`example-skills`). The builder's workspace template even referenced a
  non-existent **`document-skills:mcp-builder`** skill — fixed to rely on `mcp-server-dev` for MCP
  building. Updated the marketplace catalog, `plugins.yaml`, the Dockerfile/test seed installs, the
  setup scripts, the configurator doctrine, `DOCS.md`, and the catalog test (which now guards both
  the removal and that every entry is pinned).

### Notes

- A fast-follow (v0.46.4) will add the plugin-developer's **broad `Bash`** + `WebFetch`/`WebSearch`
  + `context7` — those need executor driver-layer changes (the permission regex drops bare `Bash`/web
  tools, and the engagement `.mcp.json` is hardcoded), so they're handled separately with verification.

## [0.46.2] - 2026-06-04 — Fix: disabled specialists no longer advertised in a resident's delegate list

### Fixed

- **A `delegates.yaml` entry pointing at a disabled specialist is no longer shown to the resident.**
  The `<delegates>` system-prompt block was rendered straight from the static `delegates.yaml`, with
  no cross-check against the enabled-specialist registry — so a specialist set `enabled: false` (but
  still listed as a delegate) was advertised to e.g. Ellen, who would then try to delegate and get
  an `unknown_agent` rejection from the tool. `_render_delegates_block` now filters delegates through
  the live `AgentRegistry` (residents + **enabled** specialists only, via new `AgentRegistry.is_known`);
  a disabled/removed specialist is neither advertised nor callable. Back-compat preserved (no registry
  → render all). Regression test added.

## [0.46.1] - 2026-06-04 — Fix: `hindsight_api_url` actually enables long-term memory

### Fixed

- **Setting `hindsight_api_url` now turns long-term memory ON.** `casa_core` requires
  `MEMORY_BACKEND=hindsight` (anything else → `noop`), but **nothing in the add-on ever set
  `MEMORY_BACKEND`** — no option, no `environment:` block, no export in `svc-casa/run`. So
  long-term memory was effectively **unreachable**: even with `hindsight_api_url` configured, casa
  stayed on the NoOp backend (short-term only). `svc-casa/run` now derives
  `export MEMORY_BACKEND="${MEMORY_BACKEND:-hindsight}"` inside the `hindsight_api_url` conditional,
  making the URL the single toggle (set it → on; empty → off). A regression guard test asserts the
  derivation. `DOCS.md` updated accordingly.

## [0.46.0] - 2026-06-04 — Add-on config conformance: config in Supervisor-managed `/config`

Moves casa's persistent configuration to the **Supervisor-managed `addon_config` mount** at
**`/config`**, conforming to HA add-on conventions.

### Changed

- **`config.yaml` `map: all_addon_configs:rw` → `addon_config:rw`.** Casa now reads its config from
  `/config` (host `/addon_configs/{REPO}_casa-agent/`), the dir Supervisor recognizes as the add-on's
  config. Every hardcoded `/addon_configs/casa-agent` (~60 refs across code, s6 scripts, AppArmor,
  configurator/plugin-developer doctrine, and `DOCS.md`) now uses `/config`. AppArmor updated to
  `/config/** rwk`.

### Why

The old layout mapped the **entire** `/addon_configs/` tree (every add-on's config) and hardcoded
the *base* slug path `/addon_configs/casa-agent` — which Supervisor does **not** recognize as the
add-on's config dir. Consequences (observed live): an uninstall with "remove configuration" did
**not** clean casa's config, and **HA add-on backups silently missed it** (they capture the
slug-prefixed dir, which was empty). Casa never read any other add-on's config, so the broad mount
was unnecessary. Now config is backed up by HA, removed on remove-config uninstall, and conforming.

### Migration

**None — this is a path change with no auto-migration.** A fresh install seeds `/config` cleanly.
An in-place upgrade does **not** move an existing `/addon_configs/casa-agent/` config to `/config`;
re-create/seed config after upgrading (or restore from a backup of the old dir). The `/data` volume
(sessions, `webhook_secret`) is unaffected.

## [0.45.1] - 2026-06-04 — Fix: tier classifier runs as root

### Fixed

- **`tier_classifier` no longer uses `permission_mode="bypassPermissions"`** (→ `acceptEdits`).
  `bypassPermissions` makes the SDK pass `--dangerously-skip-permissions` to the bundled
  `claude` CLI, which **refuses to run as root/sudo** — and HA add-ons run as root, so every
  classification call failed and silently defaulted to `private` (leak-safe but over-restrictive:
  *all* new long-term memories ended up `private`, and the logs flooded). With `allowed_tools=[]`
  there is nothing to approve, so `acceptEdits` (the mode the rest of Casa runs as root) is
  equivalent and works. Found live on the N150 right after the v0.45.0 deploy. A regression guard
  test now asserts the classifier never uses `bypassPermissions`.

## [0.45.0] - 2026-06-04 — Tiered memory access (4/4): full legacy retirement

Completes the tiered-memory re-architecture by **deleting the entire legacy memory stack**.
`active_semantic_memory` (Hindsight) is now the only memory; short-term continuity stays on the
Claude Agent SDK session. `MEMORY_BACKEND ∈ {hindsight, noop}` (any other value resolves to
`noop`, never crashes).

### Removed

- **`memory.py`** — `MemoryProvider` / `HonchoMemoryProvider` / `SqliteMemoryProvider` /
  `CachedMemoryProvider` / legacy `NoOpMemory`, plus the per-(role,user_peer) render helpers.
- **The ONNX domain classifier** — `scope_registry.py`, the **`fastembed`** dependency, the
  per-scope read fan-out in `agent.py`, the `scopes_owned`/`scopes_readable`/`default_scope`
  `MemoryConfig` fields + their boot validation, the `policies/scopes.yaml` corpus +
  `policy-scopes.v2.json` schema, and the `scope_threshold` option/env plumbing. The whole
  scope-routing block was dead since v0.43 (its outputs fed only a telemetry log + an unread
  `origin_var["scope"]` stamp); the resident read path already uses the shared `casa` bank with
  sensitivity-tier tags. `channel_trust` (trust tokens for the system prompt + peer mapping) is
  **preserved** — it is independent of the scope registry.
- **The legacy backend-selection machinery** — `_MemoryChoice`, `resolve_memory_backend_choice`,
  `_wrap_memory_for_strategy`, the `active_memory_provider` field/seam, and `Agent`'s vestigial
  `memory: MemoryProvider` param + `self._memory`. The dashboard memory row now reads the
  surviving semantic-backend choice (defensively — `GET /` never raises on a memory misconfig).
- **Honcho** — the `honcho-ai` dependency, `honcho_ids.py`, the `honcho_api_url`/`honcho_api_key`
  add-on options + schema + translations, the `HONCHO_API_KEY`/`HONCHO_API_URL` s6 exports, and
  `HONCHO_API_KEY` from the engagement-template unset list / `_PASSWORD_ENV_VARS`. `MEMORY_BACKEND`
  no longer accepts `honcho` or `sqlite`.

### Changed

- `session_registry.build_session_key` now validates the session-key charset inline (was
  delegated to `honcho_ids.honcho_session_id`); the produced key format is **byte-identical**.
- Configurator doctrine + `DOCS.md` updated to the shared-`casa`-bank model (no SQLite default,
  no Honcho options, no per-role Honcho sessions, no scope corpus).

### Migration note (pre-1.0)

A stale user `runtime.yaml` still carrying `scopes_owned`/`scopes_readable`/`default_scope` (or a
saved `honcho_api_*` / `scope_threshold` add-on option) is now rejected/ignored. The store is cold
and these are pre-1.0 removals — set `MEMORY_BACKEND=hindsight` + `hindsight_api_url` for long-term
memory, or leave unset for short-term-only.

## [0.44.0] - 2026-06-04 — Tiered memory access (3/4): collapse specialists/executors/engagements

Folds the **specialist / executor / engagement** memory subsystem off the legacy
`MemoryProvider` (Honcho/SQLite, per-role banks) and onto the shared tier-tagged Hindsight bank
`casa` shipped in v0.43.0. Every delegated context now inherits the **originating context's BOTH
axes** — read-clearance *and* write-trust (design `2026-06-03-tiered-memory-access-design` §3):

- **Reads** become a single `recall("casa", text, tags=readable_tiers(clearance_for_channel(
  origin_channel)))` at the parent/engagement origin's clearance. A finance specialist spawned
  from a private Telegram turn recalls at `private`; one spawned from voice recalls at `friends`.
- **Writes** are explicit, tier-classified `retain`s gated by `writes_to_bank(origin_channel)` —
  because specialists/executors are **ephemeral** (no session registry → the freshness reaper
  never sees them), so the reaper can't catch their turns. **Voice-originated delegation writes
  nothing** (recall-only): no speaker auth → it cannot poison the trusted store.

### Added

- `delegated_memory.py` — the delegated-context bridge: `delegated_recall(...)` (read at the
  inherited clearance, best-effort) and `retain_delegated(...)` (explicit, write-trust-gated,
  per-item tier-classified retain with idempotent `document_id`). One place that holds the
  inheritance rule.

### Changed

- **Specialist delegation** (`_run_delegated_agent`) reads via `delegated_recall` and writes one
  tier-tagged `retain` of the exchange via `retain_delegated` — the bespoke per-turn `add_turn`
  and Ellen meta-write are gone (under shared tier memory Ellen recalls everything at her
  clearance, so the meta-session was redundant).
- **Executor archive** (`_fetch_executor_archive`) becomes a semantic recall keyed on the current
  task at the engagement's inherited clearance (was a query-less per-executor recency read).
- **Engagement finalize** (`_finalize_engagement`) retains the structured engagement summary
  (and the executor-type summary, distinct `document_id`) as tier-tagged `retain`s; the
  completion **post-back NOTIFICATION is unchanged** — the resident reaper still retains it at the
  engager's trust.
- **`query_engager`** reads via `delegated_recall` at the engager's clearance.

### Removed

- **`consult_other_agent_memory`** is retired — under shared tier memory it was an access-control
  **bypass** (it read another role's bank *unfiltered*, ignoring clearance). Removed from the
  tool registry, the assistant `runtime.yaml` allowlist, the assistant `system.md` (replaced with
  shared-bank "when to delegate vs. recall" guidance), and the configurator doctrine recipes.
- **`cross_recall`** removed from the `SemanticMemory` seam (abstract + NoOp + Hindsight) — the
  retired tool was its only consumer.
- All residual `active_memory_provider` **reads** in the delegated/engagement paths
  (`emit_completion` / `cancel_engagement` / workspace-delete / `query_engager` plumbing), plus
  the now-dead Honcho meta-summary retry loop. `active_memory_provider` itself remains an inert
  bootstrap seam (removed in 4/4).

## [0.43.0] - 2026-06-04 — Tiered memory access (2/4): tier model

Long-term memory moves onto a **sensitivity-tier access model over one shared Hindsight bank**
(`casa`), replacing the per-role banks + domain-scope tags of v0.39–v0.41. Two independent
axes (design `2026-06-03-tiered-memory-access-design`, revised 2026-06-04):

- **Read-clearance** — *who may see a fact*. Per channel: voice = `friends`, a private
  Telegram DM = `private`. Recall is a single `recall("casa", text, tags=readable_tiers(
  clearance))`; the un-tier-filterable mental-model **overlay** (`profile`) is pushed **only at
  `private` clearance**. A `private` fact is therefore invisible to voice — including on a later
  friends-present voice night.
- **Write-trust** — *may we believe & store a fact*. Per channel, distinct from clearance.
  Authenticated channels (Telegram) classify **each retained message-item at its true
  sensitivity tier** (`tier_classifier`, eval-validated `SENSITIVITY_PROMPT`, default-`private`
  on uncertainty) in the **background save path** — off the turn's hot path. **Voice is
  recall-only**: it has no speaker recognition yet, so it writes nothing (it cannot poison the
  trusted store with a guest's words / a friend's joke).

### Added

- `tier_classifier.py` — per-item tier classifier (one-shot SDK query over the converged
  `SENSITIVITY_PROMPT`; leak-safe `private` default on blank/unparseable/error). Runs in the
  reaper / backgrounded gap-retain, never on the turn's critical path.
- `channel_policy.py` — `writes_to_bank(channel)` write-trust predicate (voice → recall-only;
  unknown channels fail safe to no-write).
- `session_saver.retain_cold_session(...)` — a **claim-free, registry-decoupled** background
  retain for the next-turn-after-gap path, so the prior session's classify+retain runs off the
  new turn's hot path and cannot race the registry pointer rewrite.

### Changed

- **One shared bank `casa`** for all roles (was per-role `casa-{role}`); item tags are now
  **sensitivity tiers**, not domain scopes. Read path, the `recall_memory` pull tool, the save
  reaper, and the gap-retain all use the shared bank + clearance helpers.
- Overlay (`profile`) gated to `private` clearance; voice no longer receives it (the obsolete
  per-role voice **prewarm** was removed).
- The freshness reaper saves authenticated channels only and **drops cold voice entries**
  (registry hygiene) instead of persisting them.

### Removed

- The per-turn ONNX **`write_scope`** classification and its registry recording
  (`SessionRegistry.record_write_scope`) — tiering now happens per-item in the background save
  path. (The read-side scope routing / `origin_var["scope"]`, the ONNX classifier, `fastembed`,
  and the legacy `MemoryProvider` stack are deliberately retained for the later retirement
  plans.)

## [0.42.1] - 2026-06-03 — Sensitivity prompt tune (clear the accuracy gate with margin)

The v0.42.0 `SENSITIVITY_PROMPT` shipped before its live-LLM accuracy was ever measured (no
credentials in the build env). Measured against the 35-row eval set, it straddled the 0.90
gate (0.886–0.91 across runs — flaky at the threshold), with the **`family` tier** as the
weak point. This patch refines the prompt (eval labels unchanged) so the gate clears with
margin before the tier model is built on it.

### Changed

- `sensitivity.py` `SENSITIVITY_PROMPT` — sharpened the three boundaries the eval flagged:
  - **family** — a SHARED-SPACE secret/credential (home alarm/disarm code, the MAIN wifi
    password) is `family`: explicitly NOT `private` (not a personal-account login) and NOT
    `friends` (not guest-facing).
  - **rule 2** — finances are `private` including **invoicing/billing patterns or habits**
    (not only amounts/accounts).
  - **public** — the make/model/brand of a household device (thermostat, tap, appliance) is
    impersonal → `public`.

  Live accuracy now **0.94–0.97** across three runs (was 0.886–0.91). The lone stable miss
  is the alarm-disarm-code row over-escalating to `private` — the *safe* direction (forget,
  never leak), which the design's failure-asymmetry favors. Still inert: nothing imports
  `sensitivity.py` at runtime yet.

## [0.42.0] - 2026-06-03 — Tiered memory access (1/4): sensitivity-tier classifier foundation

First step of the tiered-memory-access work (design `2026-06-03-tiered-memory-access-design`):
the accuracy-critical classifier foundation, shipped **inert** (not yet wired into the turn
flow — that lands in the tier-model step). Long-term memory access will be gated by a
per-fact **sensitivity tier** rather than domain, since retrieval is already semantic.

### Added

- `sensitivity.py` — the access-tier vocabulary: a `private ⊃ family ⊃ friends ⊃ public`
  ladder (`TIERS`), `readable_tiers(clearance)`, `apply_ceiling(tier, ceiling)`,
  `clearance_for_channel` (voice = `friends`), `parse_tier`, and `SENSITIVITY_PROMPT` — a
  classification prompt **converged with the maintainer** via an interactive eval session
  (friends is the broad default; finances/diagnoses-meds-mental-health/personal-account
  secrets/intimate/identity-PII → private; family is narrow — shared-space secrets +
  family-internal sensitive; public = impersonal general knowledge).
- `tests/fixtures/sensitivity_eval.jsonl` — a 35-fact, all-tier **eval set** (the
  maintainer-graded ground truth) + a schema unit test and a `slow`, credential-gated
  live-LLM accuracy regression harness (threshold 0.90), kept out of the fast unit gate.

Inert: nothing imports `sensitivity.py` at runtime yet. No behavioural change.

Final step of the **resident** memory re-architecture (design spec §4.3). The resident
agents' READ path now runs on the SemanticMemory seam: a cheap mental-model **overlay**
(`profile`) at fresh-session start + a single relevance-ranked **recall** over the
readable scopes (replacing the per-scope `get_context` fan-out), plus a `recall_memory`
pull tool and the cross-agent consult re-implemented on `cross_recall`. Combined with
v0.40.0's save path, a `MEMORY_BACKEND=hindsight` instance now both **writes and reads**
its long-term memory on the self-hosted Hindsight add-on.

**Scope note:** this completes the RESIDENT memory model. The specialist / executor /
engagement memory subsystem (delegation, executors, engagements) still runs on the legacy
`MemoryProvider` (Honcho/SQLite) — migrating it onto the seam, and the full retirement of
`MemoryProvider` + Honcho + SQLite, is a **deferred follow-up** (the spec designed only the
resident model). Honcho/SQLite options therefore remain. Without `MEMORY_BACKEND=hindsight`,
residents have no long-term recall (short-term conversation continuity is unaffected — it
is owned by the SDK session).

### Added

- `recall_memory` pull tool — on-demand semantic recall against the agent's own role bank,
  trust-filtered by readable scope; voice uses `budget=low` so the cross-encoder rerank
  never stalls the first utterance.
- `agent.active_semantic_memory` + `agent.active_scope_registry` handles, wired in `main()`
  (the latter also fixes the status dashboard's scope display, previously always "(none)").

### Changed

- Resident read path (`agent.py`): `peer_overlay_context` → `profile` overlay (pushed only
  at fresh-session start; rides along on resume), and the per-scope `get_context` fan-out →
  one channel-aware `recall(tags=<readable scopes>)`. Text channels auto-recall the opening
  utterance; voice pushes the prewarmed overlay only and recalls on demand via the tool.
- `consult_other_agent_memory` now reads via `SemanticMemory.cross_recall` against the
  target role's Hindsight bank (was `MemoryProvider.cross_peer_context`).
- Voice prewarmer warms the cheap `profile` overlay instead of the per-scope session
  fan-out; `VoiceChannel`'s memory handle is now the SemanticMemory seam.

## [0.40.0] - 2026-06-03 — Memory re-arch (2/3): long-term save on Hindsight

Second step of the memory re-architecture (design spec §4.2). Wires the
session-granularity **long-term save** path onto the SemanticMemory seam: ended
conversations are retained to the self-hosted Hindsight add-on. **Saves are active** when
`MEMORY_BACKEND=hindsight` + `hindsight_api_url` are set; long-term **recall** (reads)
lands in the next step (3/3), so a hindsight-selected instance writes facts but does not
yet read them back.

**Behaviour change for non-Hindsight backends:** residents no longer write memory
per-turn on ANY backend (the per-turn `add_turn` is gone). Short-term continuity is
unaffected — it is owned by the per-channel SDK session, which resumes as before. But the
legacy Honcho/SQLite stores are no longer written by residents (they still serve reads
until retired in step 3/3), so a `sqlite`/`honcho`/`noop` instance has **no resident
long-term memory** until you switch to `MEMORY_BACKEND=hindsight`. Specialist/engagement
memory writes are unchanged. (This is the spec §7 "cold cut" — `Hindsight` is the only
backend with active long-term writes from v0.40.0 on.)

### Added

- **Freshness reaper** (`freshness_reaper.py`) — the primary save trigger: a background
  task that sweeps at boot then ~hourly and retains any conversation idle past its
  per-channel freshness window (voice ~30 min, telegram ~12 h; env-overridable via
  `FRESHNESS_VOICE_MINUTES` / `FRESHNESS_TELEGRAM_HOURS`), with crash-safe stale-claim
  recovery.
- Session-granularity save (`session_saver.py`): `save_session` (idempotent retain under
  an atomic registry claim), `transcript_to_items` (SDK transcript → Hindsight items with
  a deterministic `document_id`), `freshness_window`, and the `/new` `reset_channel`.
- Explicit `/new` reset on Telegram — retains the current conversation, then starts fresh
  ("Starting fresh — I still remember what matters.").
- Registry save-support fields + atomic helpers (`session_registry.py`): dominant
  `write_scope` and a `consolidated_at` save-claim guarding the reaper/next-turn race.
- `MEMORY_BACKEND=hindsight` is now a valid backend (the legacy read path runs cold/NoOp;
  long-term save is served by Hindsight via the SemanticMemory seam).

### Changed

- `agent.py` no longer persists memory per turn (the `add_turn` write is removed). It
  records the turn's dominant write-scope on the session registry, and the freshness
  reaper retains the whole conversation once it goes cold. The resume-vs-new decision now
  honours the per-channel freshness window and saves a cold prior session before opening a
  new one (next-turn-after-gap).
- The session sweeper now hard-deletes an evicted session's on-disk transcript via the
  SDK's `delete_session(sid, directory)` (replacing the dead `_prune_sdk_session`, which
  would have armed on the 0.2.87 SDK), guarded so a conversation inside its freshness
  window is never evicted.

## [0.39.0] - 2026-06-03 — Memory re-arch (1/3): SemanticMemory seam (inert)

First step of the memory re-architecture (design spec §5). Introduces the long-term
**SemanticMemory** seam and a self-hosted Hindsight HTTP client as building blocks.
**No runtime behaviour change** — the seam is constructed and fully unit-tested but is
*not yet wired* into the agent read/write path (that lands in the save/load steps), and
long-term Hindsight memory is **not yet user-selectable** (`hindsight_api_url` is reserved).

### Added

- `SemanticMemory` ABC (`retain` / `recall` / `profile` / `cross_recall`) with a
  `NoOpSemanticMemory` degraded implementation and pure `render_recall` /
  `render_mental_models` renderers (`semantic_memory.py`).
- `HindsightSemanticMemory` — an `aiohttp` client for the self-hosted Hindsight bank API
  (`/v1/default/banks/{bank}/...`), with fail-fast `bank_id` validation
  (`hindsight_memory.py`, `hindsight_ids.py`).
- `resolve_semantic_memory_choice()` + `build_semantic_memory()` in `casa_core.py`,
  added alongside the existing memory-backend resolution (`main()` and the legacy
  `MemoryProvider` path are unchanged).
- New add-on option `hindsight_api_url` → `HINDSIGHT_API_URL` env (configurable Hindsight
  base URL, reached via the add-on's hassio network alias/IP — never the bare host
  `hindsight`). **Reserved: not yet active.**

## [0.38.0] - 2026-06-02 — Hygiene: pin claude CLI + bump claude-agent-sdk 0.1.72 → 0.2.87

Decoupled version-hygiene PR (memory re-architecture spec §6). No memory-layer or
behavioural changes to any happy path.

### Changed

- Bumped `claude-agent-sdk` `0.1.72` → `0.2.87` (`requirements.txt`), gaining the
  v0.2.82 stderr-callback exception-isolation fix that `sdk_logging.with_stderr_callback`
  already assumes. The pip pin also pins the SDK-bundled CLI used by the residents +
  `in_casa` driver.
- Pinned the global `@anthropic-ai/claude-code` npm CLI to `2.1.150` (`Dockerfile`) —
  the version `claude-agent-sdk==0.2.87` bundles — so the two CLI consumers (SDK-bundled
  vs the global `claude` used by plugin management + the `claude_code` driver) no longer
  drift.

### Added

- `tests/test_cli_sdk_pin_assert.py` — static guard that the CLI install stays pinned and
  the SDK pin stays exact, plus a docker assertion that the pinned `claude --version`
  lands in the built image.

## [0.37.13] - 2026-05-29 — Hotfix: idle-reminder reset (C) + turn_done log de-collision (G)

Two small fixes surfaced by the current-state-spec accuracy pass (Open questions
C and G; discrepancy log D7 and D15). No behavioural change to any happy path.

### Fixed

- **Idle-reminder debounce now resets on a user turn (D7 / Open-Q C).**
  `EngagementRegistry.update_user_turn()` now sets `last_idle_reminder_ts = 0.0`
  alongside `last_user_turn_ts`. Previously the debounce was only cleared
  post-fire, so a re-engaged **specialist** (3-day reminder threshold < 7-day
  refire window) got its second idle reminder on the "7 days since last
  reminder" clock instead of the "3 days since last activity" threshold —
  delaying it a few days. The reminder now tracks activity as intended. The
  common case (a user reply dropping `idle_s` below threshold) was already
  fine; this only affects the specialist re-engagement edge.

- **`turn_done` log-line name collision resolved (D15 / Open-Q G).** Two log
  lines fired per assistant turn sharing the `turn_done` prefix but carrying
  disjoint fields: the SDK logger's `turn_done turns=/cost_usd=/ms=` and the
  per-turn token summary's `turn_done role=/cache_read=/cache_write=`. The
  token-summary line (`tokens.format_turn_summary`, emitted on the `agent`
  logger) is renamed **`turn_tokens`**, so log aggregation no longer conflates
  the two. No fields changed; only the prefix. The SDK `turn_done` line is
  unchanged.


Closes the carried-over `policies/* schema validation gap` (filed v0.31.1,
2026-05-01) and clears three stale backlog entries that already shipped in
v0.37.5 but were never struck from `docs/ROADMAP-backlog.md`.

### Fixed

- **LOW policies/* schema validation gap.** `validate_config_repo` is
  now path-aware: in addition to walking `agents/`, it walks `policies/`
  and validates `disclosure.yaml` against `policy-disclosure.v1.json`
  (NOT the agent `disclosure.v1.json` — same basename, different schema)
  and `scopes.yaml` against `policy-scopes.v2.json`. The configurator
  can edit both files per its doctrine; without this gate, schema-
  invalid YAML committed there FATALs the addon on next boot in
  `policies.py::load_policies` or `scope_registry.py`. Same blast
  radius as the original E-G repro (v0.30.0 P4.2 `TRAIT:` incident)
  but for a different file class. New `_SCHEMA_BY_POLICY_FILE` map
  stores `(schema_name, version)` tuples; `_load_schema(name, version)`
  takes an optional version suffix (default `v1`) so `policy-scopes`'s
  `.v2.json` loads correctly. The original v0.31.0 walk had a flat
  basename map that mis-applied the agent schema to `policies/
  disclosure.yaml` and falsely refused every commit; v0.31.1 scoped to
  agents/ only as a stopgap, which left this gap open until v0.37.12.

### Housekeeping

- **Backlog cleanup.** Three entries already shipped in v0.37.5 (PR #57,
  master `36d772c4`) but stayed in `ROADMAP-backlog.md`: F-1 MEDIUM
  (plugin-developer prompt for honest `is_error=true` failure
  narration), A-3-bis LOW (assistant prompt anti-pattern forbidding
  `#[role]` constructions), and the D-1-followup attribution (already
  documented in the v0.37.1 and v0.37.5 archive entries). Struck.
- **F-3 triage carry-forward.** N150 confirmed on HA core `2026.5.1`
  (latest, no GetDateTime failure path in source). No active
  reproduction in recent logs — the tool fires only when an agent
  selects it. Backlog entry updated with current state; re-test via
  a dedicated probe session in a future exploration.

### Tests

- `tests/test_agent_loader.py::TestValidateConfigRepo` gains three new
  cases (`test_invalid_policies_disclosure_caught`,
  `test_valid_policies_scopes_passes`,
  `test_invalid_policies_scopes_caught`) and renames the existing
  `test_skips_policies_dir` → `test_valid_policies_disclosure_passes`
  to reflect the new walk semantics. `test_skips_non_schema_files`
  adjusted: its previous `policies/scopes.yaml` fixture targeted the
  old "policies are skipped entirely" contract and would now fail
  validation, replaced with `policies/README.md` to keep the
  "non-schema files don't trip the gate" assertion intact. Local
  pytest non-slow non-docker 1533 PASS, 68 SKIP, 22 deselected.

## [0.37.11] - 2026-05-14 — Hotfix: DE-1 e2e harness shape mismatch

Surgical hotfix for the master-CI tier2-functional / Delegation E-block
failure that has been red since v0.37.9 (PR #61, run 25871932172) and
remained red after v0.37.10 (PR #62, run 25880387018). No product change.

### Fixed

- **DE-1 e2e harness: tuple-destructure `load_all_specialists` return
  value.** v0.37.9's O-2b fix promoted `load_all_specialists`'s return
  shape from `dict[str, AgentConfig]` to `tuple[dict, list]` (per-
  specialist isolation, mirroring v0.37.1 B-1b's `load_all_executors`
  pattern). Product callers (`casa_core.py`, `agent_registry.py`) were
  updated; the DE-1 e2e harness was missed. Result: every master CI run
  since v0.37.9 failed with `ValueError: dictionary update sequence
  element #0 has length 1; 2 is required` at `merged.update(
  specialist_configs)`. Filed 2026-05-14 in
  `docs/bug-review-2026-05-14-exploration7.md`. One-line fix at
  `test-local/e2e/test_delegation_E.sh:93`:
  `specialist_configs, _failed = load_all_specialists(...)`.

### Notes

- This is a test-harness fix only. Product code on master has been
  correct since v0.37.9; production (N150) ran healthy on 0.37.10
  through exploration7's operator-attended verifies (P31 + P32 + O-1
  all GREEN end-to-end). Not reverted per `feedback_ship_gate_doctrine`
  "revert if red" because reverting v0.37.9 + v0.37.10 would lose 7
  real bug fixes for a 1-line test-script issue.

## [0.37.10] - 2026-05-14 — Hotfix bundle: P31 + P32

Closes the 2 regressions filed in `docs/bug-review-2026-05-14-exploration6.md`
(one MEDIUM, one LOW) when 3 of 5 v0.37.9 fixes verified clean but
O-5 + O-6 were re-opened as P31 + P32.

### Fixed

- **MEDIUM P31: claude_code session_id is now captured reliably.** The
  v0.37.9 O-5 fix tailed `/var/log/casa-engagement-<id>/current` but
  that log file is never created in production — the s6-rc service
  dir's `log/` subdir lacks the `producer-for` / `consumer-for`
  wiring required to compile the producer-consumer pipe, so claude
  CLI's stdout goes to a pipe with no reader. Latent infrastructure
  gap since v0.13.0 Plan 4a. Live evidence: 2026-05-14 exploration6
  engagements `28fdeb04` + `3e44c2cf` — `.session_id` never written,
  `/var/log/casa-engagement-<id>/` never exists, post-restart claude
  CLI runs as a fresh SDK session. Fix:
  `ClaudeCodeDriver._capture_session_id` now watches the claude CLI's
  own session storage at
  `<ws>/.home/.claude/projects/-data-engagements-<id>/<uuid>.jsonl`
  — that file IS reliably written, and the filename (minus `.jsonl`)
  IS the session UUID. Persists atomically to `<ws>/.session_id`. The
  deeper s6-rc producer-consumer wiring fix that would also unlock
  Phase 4b G5 log relay + remote-control URL notice is backlogged
  for a v0.38.x design pass.
- **LOW P32: engage_executor now refuses duplicate-task spawns at the
  tool layer.** The v0.37.9 O-6 fix added a prompt section forbidding
  context bleed but the SDK conversation context's natural inertia to
  re-emit the prior turn's task overpowered it. Live evidence:
  2026-05-14 exploration6 O-6.2 turn — Ellen fired TWO `engage_executor`
  calls in one assistant message, the first a wrong configurator with
  `context="Probe O-6.1"` running the prior turn's rename task.
  Fix: a new `_jaccard_task_similarity` helper at the `engage_executor`
  MCP call site refuses spawns whose `task=` overlaps with the
  most-recent engagement for this `(channel, chat_id)` within 60s at
  word-level Jaccard ≥ 0.5. The refused envelope carries
  `kind: duplicate_task` with the offending engagement's id so the
  caller knows what to do. The v0.37.9 prompt section is retained as
  documentation but its strong-claim anchors ("ONLY", "Do not carry",
  "fire two separate engage_executor calls") are softened — the
  tool-level guard is the real enforcement, prompt is advisory.
  New `EngagementRegistry.recent_for_origin` query method.

### Tests

- 9 new tests (+2 driver session-id, +5 registry `recent_for_origin`,
  +3 engage_executor duplicate-task guard). v0.37.9 session-id capture
  tests rewritten for the new projects-dir watch approach.

### Notes

- The 3 v0.37.9 fixes that verified clean in exploration6 (O-1, O-2a,
  O-2b) are unchanged. O-3 (cross-channel memory) remains deferred
  to v0.38.0.
- Latent infrastructure gap: Phase 4b G5 "claude_code log relay"
  (`_relay_log_lines`) and "Remote control URL" topic notice
  (`_capture_url`) are also non-functional in production due to the
  same s6-rc producer-consumer wiring gap. Both backlogged with P31
  Option A for a v0.38.x design pass.

## [0.37.9] - 2026-05-14 — Hotfix bundle: O-1 + O-2a + O-2b + O-4 + O-5 + O-6

Closes 5 findings from `docs/bug-review-2026-05-14-p21-p30.md` (one
MEDIUM, four LOW). O-3 (cross-channel memory) is deferred to a v0.38.0
brainstorm — it's an architectural design choice, not a hotfix.

### Fixed

- **MEDIUM O-5: claude_code engagements now survive Casa restarts.**
  Boot-replay used to restore the s6 service but lose conversation
  context — the run script's `--resume $(cat .session_id)` plumbing
  shipped without a writer, so every restart spawned the CLI fresh.
  Live evidence: 2026-05-14 P25 cid `7a9cba59` — engagement `44389d8a`
  zombied for 7 minutes after a mid-engagement Casa restart.
  Fix: `ClaudeCodeDriver._capture_session_id` tails the per-engagement
  s6-log for `system_init session_id=<uuid>`, persists it atomically
  to `<workspace>/.session_id` (temp+os.replace), and invokes the
  `persist_session_id` callback so `EngagementRecord.sdk_session_id`
  stays in lockstep with the on-disk file. `casa_core` now wires
  `engagement_registry.persist_session_id` into the driver constructor.
- **LOW O-1: install/uninstall plugin failures now surface as MCP errors.**
  `_result()` auto-detected `is_error` only when `payload["status"] ==
  "error"`, so the `{"ok": False, "error": ...}` envelope used by
  `install_casa_plugin` and `uninstall_casa_plugin` landed as `ok=True`
  in `sdk_logging.log_tool_result` telemetry — contradicting F-7
  v0.32.0 intent. Extended the auto-detect to also recognise
  `payload.get("ok") is False`. Live evidence: 2026-05-14 P29.1 cid
  `52240634` saw `tool_result name=install_casa_plugin ok=True ms=12594`
  for a plugin-not-in-marketplace failure.
- **LOW O-2a: `--scope=executors` now refreshes residents' cached
  prompts.** `reload_executors` previously only called
  `executor_registry.load()`, leaving residents with a stale
  `<executors>` system-prompt block (rendered from
  `self.config.executors` at construct_agent time). Fan-out to
  `reload_agent` for each resident regenerates that state. Live
  evidence: 2026-05-14 P22 row5b — Ellen said "No" to "is pd enabled?"
  between an executor-scope reload and the next agent-scope reload.
- **LOW O-2b: specialist load failures now surface in casactl output.**
  `agent_loader.load_all_specialists` returns `(found, failed)` with
  per-specialist isolation (mirroring `load_all_executors` v0.37.1
  B-1b), `SpecialistRegistry.load_failures()` exposes them, and
  `reload_agents` appends `failed:<role>:<msg>` entries to the action
  trail. Pre-fix, a malformed new specialist returned `ok=True` with no
  trace — operator had to grep addon logs. Live evidence: 2026-05-14
  P22 row 4 first attempt — probe22 missing `response_shape.yaml` +
  `voice.yaml`, reload returned `ok=True`.
- **LOW O-4: playbook P21 step 2 reworded.** Engagement subprocesses
  have workspace-scoped HOME by design (per `drivers/workspace.py`
  `render_run_script` template). The H-1 verify path is `casa-main` +
  `svc-casa-mcp` `/proc/<pid>/environ`, NOT the engagement subprocess.
  Updated `docs/exploration-playbook/blocks/G-lifecycle.md`.
- **LOW O-6: Ellen now scopes engage_executor `task=` to the new task.**
  Prompt section added to `defaults/agents/assistant/prompts/system.md`
  forbidding context bleed from prior conversation turns into the
  `task=` arg. Live evidence: 2026-05-14 P27.2 cid `093a02c7` — Ellen
  spawned BOTH configurator AND pd in one turn, the configurator
  engagement received P27.1's rename task description instead of
  P27.2's repo creation task.

### Tests

+9 vs v0.37.8 baseline (1514 → 1523 PASS):
- `test_install_casa_plugin.py::test_install_plugin_failure_envelope_is_error`
- `test_reload.py::TestExecutorsScope::test_executors_scope_fans_out_to_residents`
- `test_reload.py::TestReloadAgents::test_surfaces_specialist_load_failures`
- `test_agent_loader.py::TestLoadAllSpecialists::test_per_specialist_isolation`
- `test_claude_code_driver.py::TestSessionIdCapture` (3 tests)
- `test_workspace.py::test_render_run_script_consumes_persisted_session_id`
- `test_assistant_prompts.py::test_system_prompt_forbids_engage_executor_context_bleed`

Existing `test_specialist_registry.py::test_rejects_non_empty_channels`
and `test_agent_loader.py::TestLoadAllSpecialists::test_finds_specialist`
updated for the new per-specialist isolation contract.

## [0.37.8] - 2026-05-14 — Hotfix: H-1 (HOME propagation) + N-1 (playbook 7-scope fix)

Closes the two findings from `docs/bug-review-2026-05-13-exploration4.md`:
**MEDIUM H-1** (configurator narrating a per-engagement `claude plugin
marketplace add` workaround during plugin install) and **LOW N-1**
(playbook P19 listed 6 reload scopes when `reload.py:709` registers 7).

### Fixed

- **MEDIUM H-1: `HOME=cc-home` now propagated to s6-supervised services.**
  setup-configs.sh writes `/addon_configs/casa-agent/cc-home` to
  `/run/s6/container_environment/HOME`, mirroring the existing
  GITHUB_TOKEN / CLAUDE_CODE_OAUTH_TOKEN / PATH propagation pattern.
  K-1 (v0.34.1) is the standing lesson: shell-level `export HOME=...`
  (setup-configs.sh:322) only governs the script's own claude calls;
  s6 services need `/run/s6/container_environment/`. Without this,
  casa-main + svc-casa-mcp booted with HOME=/root, so the 6
  `subprocess.run(["claude", "plugin", ...])` call sites in `tools.py`
  (install_casa_plugin, uninstall_casa_plugin, marketplace_add_plugin,
  marketplace_remove_plugin, marketplace_update_plugin — two of these
  call `claude plugin install/uninstall`, four call `claude plugin
  marketplace update`) read `/root/.claude/plugins/known_marketplaces.json`
  (empty) instead of `cc-home/.claude/plugins/known_marketplaces.json`.
  The configurator improvised a `Bash(claude plugin marketplace add ...)`
  workaround mid-engagement to make installs succeed — that workaround
  is no longer needed. 1 new test (`tests/test_setup_configs_claude_home.py`)
  mirrors the K-1 precedent.

- **LOW N-1: playbook documents `casactl --scope=executors` (7th scope).**
  `docs/exploration-testing-playbook.md::P19` and the "Granular reload
  via `casactl`" scope table both listed 6 scopes when `reload.py:709`
  has registered `executors` as a 7th since v0.37.1. `casactl --help`
  and `configurator/doctrine/reload.md` already listed all 7 — only
  the playbook was stale. Doc-only.

### Changed (cosmetic)

- `setup-configs.sh:267-273` and `agent.py:642` comments refreshed
  to reflect post-H-1 reality (HOME=cc-home instead of HOME=/root).
  Defensive `/root/.claude/projects` symlink + SDK-resume recovery
  logic unchanged.

---

## [0.37.7] - 2026-05-13 — Hotfix bundle: G-1 + G-2 + playbook doc-fixes

Closes two HIGH findings from `docs/bug-review-2026-05-13-exploration3.md`
plus the coupled seed flip for plugin-developer's default permission_mode,
and three doc-fixes to the exploration playbook.

### Fixed

- **HIGH G-1: `permission_mode: auto` now suppresses the C-1 relay hook.**
  `engagement_permission_relay` in `hooks.py` short-circuits with `{}`
  when the engagement's executor was created with
  `permission_mode in {auto, bypassPermissions}`. Plumbed via a new
  `EngagementRecord.permission_mode` field, snapshotted at engagement
  creation from `ExecutorDefinition.permission_mode` (mirrors the
  existing `tools_allowed` snapshot pattern). `acceptEdits` and
  `default` modes still fall through to the allow-list + Telegram relay
  pipeline. Autonomous claude_code engagements (P5/P12 in the
  exploration playbook) no longer block on the first ToolSearch
  permission prompt.
- **HIGH G-2: `casa_reload(scope=agent role=<new>)` now provisions
  agent-home.** Previously only `scope=agents` (plural — the diff-based
  adds/evicts path) called `agent_home.provision_agent_home`; the
  granular per-role scope used by the configurator's
  `recipes/specialist/create.md` flow skipped it, so the first
  `delegate_to_agent target=<new>` failed with
  `Working directory does not exist: /addon_configs/casa-agent/agent-home/<role>`.
  Moved provisioning into `reload._construct_agent` so it fires
  regardless of which reload scope triggered the construction
  (idempotent — no-op on existing dirs).
- **Coupled seed: plugin-developer ships `permission_mode: auto`.**
  `casa-agent/rootfs/opt/casa/defaults/agents/executors/plugin-developer/definition.yaml`
  flipped from `acceptEdits` to `auto` per operator directive
  (2026-05-13). Now operationally effective via G-1 — plugin-developer
  engagements run autonomously by default.

### Doc

- **Playbook P14 — two-line `turn_done` contract.** `sdk_logging` and
  `agent.py` emit two independent lines (sdk: cost/latency; agent:
  role/channel/tokens), not the single-line shape the spec implied.
- **Playbook P19 — `>=90s` timeout on post-reload turn probes.** The
  bare 60s urllib timeout is too tight for post-`scope=full` cold
  starts (17-19s on top of `policies:rebuild_scope_registry` +
  `agent:*:construct_agent`). Use the smoke skill OR `>=90s`. The
  `bus.register` idempotency regression is only confirmed when a `>=90s`
  retry also fails.
- **Playbook P20.2 — U3 title format.** Role-emoji is in the topic-icon
  bubble (`icon_custom_emoji_id`), not inline in the title text. State
  emoji prefixes the title.

## [0.37.6] - 2026-05-13 — Hotfix: CI tier1-smoke + tier2-functional boot timeout

Closes the pre-existing CI red that started intermittently after v0.36.1
and became 100% reliable from v0.37.1 onward. Every fresh container in
`test-local/Dockerfile.test` was downloading `intfloat/multilingual-e5-large`
(~2.24GB) on boot because `scope_registry.py` calls
`TextEmbedding(model_name=...)` without a `cache_dir`, and the image had
neither `FASTEMBED_CACHE_PATH` set nor the model pre-cached. The download
routinely exceeded the 30s `/healthz` wait_healthy ceiling between
`agent-home provisioned: role=butler` and `ScopeRegistry ready`.

### Fixed

- **CI: fastembed model pre-cached in test image.** `test-local/Dockerfile.test`
  now sets `ENV FASTEMBED_CACHE_PATH=/opt/casa/fastembed-cache` and adds a
  build-time `RUN python3 -c "TextEmbedding(model_name='intfloat/multilingual-e5-large')"`
  warm-up. The env var is honored by both build-time RUN and runtime
  container (fastembed's `define_cache_dir` reads it) so the model is
  baked into a Docker layer and reused, not re-downloaded per container.
  Image gains ~2.5GB but boot now reaches `/healthz` in seconds. No
  production image change (N150 has persistent caches across restarts).

## [0.37.5] - 2026-05-13 — Bug-bundle: E-1 + F-1 + A-3-bis

Bundles the four findings from `docs/bug-review-2026-05-13-exploration2.md`.
The load-bearing one is E-1: the v0.37.2/v0.37.3 C-1 PreToolUse permission
relay was contract-incomplete because the svc-layer forwarder cut off
operator response after 10s regardless of the policy's declared
`timeout: 600`. Yesterday's GREEN live-verify for C-1 (5 sequential Allow
taps on engagement `986f254e`) was operator-hand-speed-bound; synthetic
LAN probes reliably exceeded the 10s window and reproduced a fail-closed
deny with empty error reason.

### Fixed

- **HIGH E-1: `/hooks/resolve` forwarder timeout truncated permission relay.**
  `svc_casa_mcp._forward_to_internal` defaulted `timeout_s=10.0` and
  `_build_hooks_handler` did not override it. Result: any operator
  response taking >10s caused `engagement_permission_relay` to fail-closed
  deny with an empty `asyncio.TimeoutError()` reason; the actual verdict
  arrived later with no waiter and was silently dropped, leaving the
  engagement in a Schrödinger state (agent narrates success, topic shows
  🟢, tool actually failed). Fix: `_build_hooks_handler` now passes
  `timeout_s=None` so casa-main's policy-driven timeout (declared per
  hook in `hooks.yaml`, e.g. 600s for `engagement_permission_relay`) is
  the only effective gate. tools/call path keeps the 10s default (no
  human-in-the-loop). Five new tests in `test_svc_casa_mcp.py` cover
  the contract.

- **MEDIUM F-1: Agent misnarrated hook errors as success.**
  Two changes:
  (1) `svc_casa_mcp._build_hooks_handler` deny reasons rewritten as
  actionable text. Old `"hook forward error: "` (empty `str(exc)` on
  TimeoutError) replaced with `"Permission relay failed: forwarder error
  talking to casa-main (<ExcType>: <detail>). The tool was not run."` and
  the socket-unreachable variant gets a parallel message ending in
  `"The tool was not run. Retry shortly or check addon logs."`.
  (2) New "Tool results: honest failure narration" section in
  `defaults/agents/executors/plugin-developer/prompt.md` instructing the
  executor to treat `is_error=true` as failure verbatim, even when the
  error text mentions "hook" or "permission relay" — those words are not
  a signal the call succeeded.

- **LOW A-3-bis: Ellen still produced legacy `#[role]` topic references.**
  v0.37.1's prompt update added a description of the new U3 topic shape
  (bubble icon + state-prefixed title) but didn't explicitly forbid the
  old format. Reproduced twice in 2026-05-13 exploration2 ("Head to the
  Engagements supergroup, topic `#[plugin-developer] curl probe`").
  `defaults/agents/assistant/prompts/system.md` now carries an explicit
  anti-pattern paragraph forbidding `#[role]`, `#[role:topic]`, and
  `[role] topic-name` constructions.

### Documented

- **D-1 attribution (housekeeping).** Wire shape for engagement topic
  icons is spec-compliant as of v0.37.1 (commit `800a3516`): numeric
  `icon_custom_emoji_id` from `getForumTopicIconStickers`, state-prefixed
  title only. 2026-05-13 exploration2 confirmed the wire shape on the
  N150. Visual rendering pending operator-attended supergroup
  inspection. Memory `project_v037_1_bug_bundle_shipped` updated to
  move D-1 out of the "deferred to operator verify" list.

## [0.37.1] - 2026-05-13 — Bug-bundle: D-1 + B-1 + B-1b + A-1 + A-2 + A-3

Catches up the addon version from 0.36.1 → 0.37.1 (the v0.37.0
Phase 2 E-12 source landed on master 2026-05-12 without a release
artefact; this release bundles those Phase 2 changes plus the
six findings from `docs/bug-review-2026-05-13-exploration.md`).

### Fixed

- **HIGH D-1: Engagement topic icons silently broken since v0.37.0.**
  Telegram's Bot API requires a numeric `custom_emoji_id` from
  `getForumTopicIconStickers` for `icon_custom_emoji_id`; Casa was
  passing literal chars (`'tools'`, `'✅'`). Result: bubble fell
  back to default blue chrome AND the leading state emoji was
  silently stripped from the topic name. New `channels/topic_icons.py`
  module owns the locked role → custom_emoji_id map (📁 configurator,
  💻 plugin-developer, 💰 finance, 🤖 default), verified live
  against N150's curated set on 2026-05-13. `compose_topic_title`
  now emits `<state> <task>` (role lives in the bubble).
  `close_topic_with_check` renamed to `close_topic` and no longer
  flips the icon. Specialist engagement open path harmonised to
  U3 format (was legacy `#[<role>] <task> · id8`).

- **MEDIUM B-1: Executor schema rejects `permission_mode: auto`.**
  Casa's `executor.v1.json` enum was stuck on a 4-mode list
  (`acceptEdits`, `bypassPermissions`, `default`, `plan`) but CC
  CLI 2.1.119 supports 6 modes (adds `auto` and `dontAsk`).
  Enum expanded to match.

- **MEDIUM B-1b: One broken executor YAML wiped the entire registry.**
  `load_all_executors` now returns `(loaded, failed)`; per-executor
  parse errors are isolated (catches `LoadError`, `OSError`,
  `ValueError`, `TypeError`, `yaml.YAMLError`). `ExecutorRegistry.load`
  logs each failure at ERROR and continues. New log shape
  `Executors: loaded=[...] failed=[...] disabled=[...]`
  distinguishes "no executors configured" from "all executors
  broken".

- **MEDIUM A-1: No granular reload scope for ExecutorRegistry.**
  New 7th scope `executors` (`casa_reload(scope='executors')` /
  `casactl reload --scope=executors`) re-scans `executors/` and
  rebuilds the registry. Included in `reload_full` before the
  per-role agent loop. New `doctrine/recipes/executor/{enable,
  disable,edit-definition}.md` and a 7th row in the doctrine
  scopes table.

- **MEDIUM A-2: Residents echo stale memory of system state.**
  Ellen + butler `system.md` now include a "Stale system-state in
  memory" section: any time memory says a capability is missing
  and the user re-asks, always retry the tool call rather than
  relaying the memory'd "no" verbatim.

- **LOW A-3: Ellen's prompt referenced legacy `#[role] <task>`
  topic format.** Updated to U3 wording (icon in bubble, state in
  title prefix).

### Deferred

- **HIGH C-1** — CC CLI 2.1.119 does not emit
  `notifications/claude/channel/permission_request` for actual
  permission gates during real engagements; the U1 inline-keyboard
  relay is non-operational under real workloads. Spike + fix in
  a follow-up session.

## [0.36.1] - 2026-05-11 — Hotfix: H-2 (hook callbacks return {} not None)

### Fixed
- **LOW H-2: Casa hook callbacks return `None` from no-op paths, violating
  the SDK's `HookJSONOutput` typed contract.** The SDK's
  `_convert_hook_output_for_cli` (`claude_agent_sdk/_internal/query.py`)
  calls `hook_output.items()` unconditionally — returning `None` emits
  `'NoneType' object has no attribute 'items'` to stderr ~73× per
  ~30-min engagement window across `block_dangerous_commands`,
  `make_path_scope_hook_v2`, `make_casa_config_guard_hook`,
  `make_commit_size_guard_hook`, and `make_self_containment_guard`.
  Operationally harmless (the SDK error-responds back to the CLI which
  proceeds normally; deny payloads still route correctly per
  exploration5 P15) so this is purely log hygiene. Originally filed as
  upstream-blocked; 2026-05-10 triage during v0.36.0 confirmed the SDK
  is unchanged 0.1.72 → 0.1.80, fix is Casa-side. Changed every
  HookCallback no-op `return None` → `return {}`; tightened the
  `HookCallback` type alias and `_hook` return annotations from
  `dict[str, Any] | None` → `dict[str, Any]`. 10 new regression tests
  in `TestHookNoopReturnsEmptyDict` lock the contract per factory; the
  HTTP-proxy layer at `internal_handlers.py:_make_internal_hooks_resolve_handler`
  keeps its defensive `None → {}` translation for third-party callbacks.

## [0.35.2] - 2026-05-02 — Hotfix bundle: Q-1 + R-1 + S-1

### Fixed
- **MEDIUM Q-1: `casa_reload_triggers` returned a stale `registered`
  list.** Triggers DID register in apscheduler and DID fire on
  schedule, but `reload.py::reload_triggers` never wrote the fresh
  cfg back into `runtime.role_configs[role]`, so the back-compat
  consumer (`tools.casa_reload_triggers`) read the boot-time list.
  Configurator hallucinated failure narratives on every trigger-add
  (live evidence: P8.1 in exploration5, engagement `2cf6fb6f`
  finalized `outcome=error` despite probe-p8-sched firing twice).
  Fix mirrors the resident vs specialist branching of `reload_agent`
  at `reload.py:339-348`. Adds `TestReloadTriggers` regression
  coverage. Latent in v0.35.0.
- **LOW R-1: configurator specialist-create recipe wrote a
  non-existent default `cwd`.** Recipe template defaulted to
  `cwd: /addon_configs/casa-agent/workspace` (a directory that does
  not exist on disk); finance seed ships `cwd: ""`. Delegation to a
  newly-created specialist failed with `sdk_error (Working directory
  does not exist)`. Doctrine-only fix in
  `defaults/agents/executors/configurator/doctrine/recipes/specialist/create.md`.
  Live evidence: P11.2 in exploration5, cid `f032f185`. Latent since
  configurator shipped (v0.12.0).
- **LOW S-1: `agent_loader` rejected ANY unknown file in an agent
  directory.** Editor-backup artifacts (`.bak`/`.swp`/`.tmp`/`.orig`/`*~`)
  broke `casactl reload --scope=agent` with `LoadError: unknown
  file(s)`. Footgun for ad-hoc N150 SSH edits using `sed -i.bak`.
  Adds `_is_editor_backup()` helper that skips those suffixes,
  parallel to the existing dotfile skip. Diagnostic for genuine
  unknown files now mentions the whitelist + suggests `git restore`.
  Live evidence: P19.7v1 in exploration5. Latent since agent_loader's
  strict-mode shipped.

## [0.35.1] - 2026-05-02 — Hotfix: bus.register idempotent on queue

### Fixed
- **HIGH: post-`scope=agent` reload broke turn dispatch.** v0.35.0
  live verify on N150 hung every `/invoke/assistant` turn until 504
  after `casactl reload --scope=agent --role=...`.  Root cause:
  `MessageBus.register()` always replaced `self.queues[name]` with a
  fresh `asyncio.PriorityQueue`. The reload handler called
  `bus.register` to rebind the per-role handler — which orphaned the
  running `run_agent_loop` task on the old queue while every new
  `bus.send()` landed on the new queue. The existing dispatch loop
  already supports handler rebinding via `bus.handlers[name]`
  (intentional, per the in-source comment); the queue replacement was
  always wrong for that case.  Fix: `register()` is now idempotent on
  queue creation. Adds `TestRegisterIdempotent` regression coverage in
  `tests/test_bus.py`. Latent in v0.35.0 (2 hours).

## [0.35.0] - 2026-05-02 — Granular in-process reload

### Added
- **Granular in-process reload.** `casa_reload(scope=...)` replaces the
  no-arg Supervisor-restart shape. Six scopes: `agent`, `triggers`,
  `policies`, `plugin_env`, `agents`, `full`. Configurator engagements
  reload state in <1s instead of ~10–15s. See
  `defaults/agents/executors/configurator/doctrine/reload.md` and spec
  `docs/superpowers/specs/2026-05-02-granular-reload-design.md`.
- **`casa_restart_supervised` MCP tool** for the rare cases that need a
  full process restart (s6 service-tree edits, addon options mutations).
- **`casactl` operator CLI** at `/usr/local/bin/casactl` — same dispatch
  path as the MCP tool. `casactl reload --scope=... [--role=...]` /
  `casactl restart-supervised`.
- **`POST /admin/reload` route** on the internal aiohttp app
  (`/run/casa/internal.sock`).

### Changed
- **`casa_reload()` no-arg shape removed** (pre-1.0 license — no
  back-compat shim). Doctrine carries the rename.
- **`casa_reload_triggers(role=...)`** kept as a back-compat alias for
  `casa_reload(scope='triggers', role=...)`.
- **`CasaRuntime` dataclass** introduced as the canonical container for
  process-global Casa state; `init_tools(runtime=...)` is now the
  primary wiring point.

### Doctrine + playbook
- `executors/configurator/doctrine/reload.md` rewritten.
- All `recipes/**/*.md` updated for new tool shape.
- `docs/exploration-testing-playbook.md` adds `casactl reload` recipes.

## [0.34.3] - 2026-05-02 — Hotfix: O-1 + O-3 (P12 plugin lifecycle unblock)

Hotfix for two HIGH bugs surfaced by the 2026-05-02 P12 full plugin
lifecycle exploration session against v0.34.2 (`docs/bug-review-2026-05-02-p12-fulllifecycle.md`).
Both latent since v0.14.1 (Plan 4b ship, 2026-04-25 — ~7 days). Together
they unblock the P12 chain end-to-end: plugin-developer can now call
casa-framework MCP tools, and Casa-installed plugins now surface in
resident SDK subprocesses.

### Fixed

- **O-1 (HIGH, mcp_envelope serializes no-arg tools with empty
  `inputSchema: {}`).** `mcp_envelope.py::_tool_schema` had a
  `raw and …` short-circuit on the dict-of-types branch that fails
  for empty dicts (Python falsy semantics). No-arg tools — declared
  via `@tool(name, desc, {})` — fell through to the passthrough
  branch and emitted `inputSchema: {}` instead of
  `{"type":"object","properties":{}}`. CC v2.1.119 strict-validates
  and rejects the entire `tools/list` payload with `Invalid input:
  expected "object"`, so plugin-developer subprocesses could connect
  to svc-casa-mcp + see capabilities but could not call ANY
  casa-framework MCP tool — `mcp__casa-framework__emit_completion`
  etc. returned `No such tool available`. Affected tools today:
  `casa_reload` + `marketplace_list_plugins` (both no-arg). Latent
  since v0.14.1 because K-1 + L-1 verifications never reached
  `emit_completion` (Bash invocations only); P12 was the first
  end-to-end exercise. Fix: drop the `raw and ` short-circuit so
  `{}` falls through the dict-of-types branch and emits the correct
  shape.

- **O-3 (HIGH, plugins_binding filters out project-scope plugins).**
  `plugins_binding.build_sdk_plugins` shells out to `claude plugin
  list --json` from `HOME=cc-home` and filtered by
  `e.get("enabled")`. But Casa installs at `--scope project` to
  per-role `agent-home/<role>/.claude/settings.json`. The CLI
  evaluates the `enabled` field against the calling HOME's
  settings.json — for a cc-home call, that's cc-home's settings,
  which doesn't list any project-scope plugin from agent-home.
  Result: every Casa-installed plugin reported as `enabled: false`
  from cc-home → binding filtered it out → resident SDK subprocesses
  never saw Casa-installed plugins. Symptom: Ellen called
  `Skill(target=<plugin>:<skill>)` → `tool_result ok=False`. Latent
  since v0.14.1; P12.5 was the first end-to-end skill-use exercise.
  Fix: `build_sdk_plugins` accepts an optional `role` kwarg; when
  provided (residents at `agent.py:524`), project-scope entries
  whose `projectPath == /addon_configs/casa-agent/agent-home/<role>`
  are included regardless of the CLI's `enabled` field; user-scope
  entries still honour the `enabled` check. When `role` is None
  (specialists + executors at `tools.py:270` and `:321` — neither
  carries plugins per `install.md` doctrine), project-scope entries
  are filtered out entirely, preserving v0.34.2 behavior.

### Test plan

- 1 new unit test in `tests/test_mcp_envelope.py` for the empty
  `input_schema={}` case; pre-existing dict-of-types and passthrough
  cases unchanged and still passing (8 → 9 tests in this file).
- 3 new unit tests in `tests/test_binding_layer.py` covering the
  role-based project-scope filter: matching role includes the
  project plugin, mismatched role excludes it, no-role drops all
  project-scope entries (preserves v0.34.2 specialist + executor
  behavior). Pre-existing 4 cases continue to pass under the
  no-role branch (4 → 7 tests in this file).

### Live verification (planned)

- O-1: drive a fresh plugin-developer engagement end-to-end on N150
  post-deploy. Expect `mcp__casa-framework__emit_completion` to
  succeed (NOT `No such tool available`); engagement should finalize
  via emit_completion path, NOT via topic `/complete` slash command.
- O-3: configurator-install a probe plugin into Ellen, then DM Ellen
  to use the plugin's skill. Expect `Skill(target=<plugin>:<skill>)
  ok=True` and Ellen's reply to be the actual skill output (not a
  graceful-degradation "skill not found" narration).

### Carry-forward

- N-1 + N-2 (webhook trigger reload + name-agnostic handler) —
  reproduced this session, both already in `ROADMAP-backlog.md`.
  Address in v0.35.0.
- E-12 (claude_code driver doesn't stream incremental progress to
  engagement topic) — observed live this session as ~6min approval-
  gate silence. Already-deferred backlog.
- H-2 (claude-agent-sdk hook callback errors) — third-party.
  v0.1.72 still latest at session start; recheck `gh release list
  --repo anthropics/claude-agent-sdk-python --limit 3` at next ship
  gate.

## [0.34.2] - 2026-05-01 — Bug bundle: L-1 + L-1b + L-2 + L-3 + remove hello-driver

Closes 4 findings from
`docs/bug-review-2026-05-01-deferred-probes.md` plus 1 latent bug
surfaced during code review (L-1b).

### Fixed

- **L-1 (HIGH, claude_code engagement settings.json missing
  `permissions.allow`).** `drivers/workspace.py` now materializes
  `defn.tools_allowed` (filtered to valid CC permission patterns:
  `Bash(...)`, `Read`, `Write`, `Edit`, `Glob`, `Grep`, `Skill`,
  `mcp__*`) + `defn.permission_mode` into engagement-scoped
  `.claude/settings.json::permissions`, for both legacy and template
  provisioning paths. `drivers/claude_code_driver.py` now passes
  `workspace_template_root` + `plugins_yaml` so plugin-developer
  flows through the template path (its
  `workspace-template/CLAUDE.md.tmpl` becomes the engagement system
  prompt). HOME dir creation lifted out of the if/else for parity.
  Latently broken since v0.13.0 — every Bash invocation in
  plugin-developer engagements got "This command requires approval"
  with no TTY to escalate.

- **L-1b (HIGH, hook_bridge translator silently dropped all hooks).**
  `drivers/hook_bridge.py::translate_hooks_to_settings` was reading
  PascalCase keys (`PreToolUse`, `PostToolUse`) but on-disk hooks.yaml
  + the canonical schema (`defaults/schema/hooks.v1.json`) use
  snake_case (`pre_tool_use`, `post_tool_use`). Result: every
  claude_code engagement got `{"hooks": {}}` since v0.13.0. Sibling
  of L-1: defense-in-depth (`block_dangerous_bash`, `path_scope`,
  `casa_config_guard`) was completely absent for any claude_code
  subprocess. Existing `test_hook_bridge.py` fixture used PascalCase
  input — masked the bug. Fixed translator to read snake_case (still
  emits PascalCase per CC settings.json shape); regression test loads
  bundled `plugin-developer/hooks.yaml` and asserts non-empty
  PreToolUse block.

- **L-2 (LOW, cosmetic).** `hooks.py::_normalize_path` was producing
  `"//addon_configs/..."` instead of `"/addon_configs/..."` in
  path_scope deny payloads. Self-consistent on both sides of the
  prefix comparison so the deny logic was unaffected; only display
  cleaner now.

- **L-3 (DOCTRINE).** Configurator's `completion.md` now has a
  `## Status semantics` section explaining that `emit_completion`
  `status="ok"` reflects engagement-task outcome (a hook-deny
  correctly fired during a security probe is `ok`), and clarifying
  that the valid enum is `"ok" | "partial" | "failed" |
  "cancelled"` — not `"error"`.

### Removed

- **`hello-driver` test-harness executor.** Bundled defaults
  (`defaults/agents/executors/hello-driver/`) deleted. Smoke harness
  `test-local/smoke/test_claude_code_driver.sh` deleted. Comments in
  `hook_bridge.py` and `setup-configs.sh` updated to drop
  hello-driver references. Doctrine `scaffold.md` example listings
  cleaned. With the L-1 call-site change, plugin-developer becomes
  the only `claude_code` driver caller — hello-driver was never
  user-facing and its only role was driver validation, now covered
  by the plugin-developer P12 chain. Closes M-1 (FIFO subprocess
  hang) by deletion.

### Live verification

- L-1: plugin-developer engagement built `casa-probe-2026-05-01-greet`
  end-to-end (gh + git push round-trip) — every Bash call returned
  ok=True. Engagement workspace `.claude/settings.json` carried
  `permissions.allow` populated from defn + `permissions.defaultMode
  = "acceptEdits"` + `hooks` block + `enabledPlugins`. Engagement
  workspace CLAUDE.md was the structured `.tmpl` content.
- L-1b: same engagement subprocess transcript shows PreToolUse hooks
  fired (block_dangerous_bash + path_scope) — defense-in-depth live
  on the only remaining claude_code executor.
- L-2: P15.2 path_scope deny payload now shows
  `'/addon_configs/...'` (single slash).

### Test plan

- 9 unit tests for L-1 helper + legacy/template path permissions.
- 1 regression test in `test_hook_bridge.py` loads bundled
  `plugin-developer/hooks.yaml` end-to-end.
- 1 regression test in `test_workspace_template_renders.py` loads
  bundled `plugin-developer/definition.yaml` + `hooks.yaml` +
  `plugins.yaml` + `workspace-template/` end-to-end.
- 4 unit/integration tests for L-2 single-slash output.

## [0.34.1] - 2026-05-01 — Hotfix: K-1 (claude_code_driver auth)

Hotfix for K-1 (HIGH) discovered in
`docs/bug-review-2026-05-01-exploration4.md`. Plugin-developer (and
hello-driver, and any future Tier-3 executor using `claude_code_driver`)
has been latently broken since v0.13.0 (Plan 4a, 2026-04-23 — ~8 days)
because the Claude Code OAuth token never propagated to the engagement
subprocess. Surfaced when exploration session 4 retracted a disguised
time-budget deferral on P12 (the canonical case for the playbook's new
"Time is never a deferral reason" doctrine).

### Fixed

- **K-1 (HIGH, claude_code_driver auth) — engagement subprocesses had
  no Claude API auth.** `setup-configs.sh` propagates `GITHUB_TOKEN`
  to `/run/s6/container_environment/` so every s6-supervised child
  (including engagement subprocesses launched via `with-contenv`)
  inherits it. There was no equivalent block for `CLAUDE_CODE_OAUTH_TOKEN`
  — the token was only exported into svc-casa's process env at
  `svc-casa/run:13`, which feeds casa_core (in-process Claude API
  calls work) but NOT child s6-rc services. Result: every
  claude_code_driver subprocess started in workspace
  `/data/engagements/<id>/.home/` with a fresh `.home/.claude.json`
  and no `.credentials.json`, and the CC CLI's first turn produced
  the literal text `"Not logged in · Please run /login"`. Engagement
  hung in `status=active` until manually cancelled.

  **Fix:** new `claude-oauth-token` block in `setup-configs.sh`
  (between the `github-token` block and the `seed-copy` block).
  Mirrors the GITHUB_TOKEN propagation: read `claude_oauth_token`
  via bashio, op:// resolution via the same `op` CLI path, write
  the resolved value to `/run/s6/container_environment/CLAUDE_CODE_OAUTH_TOKEN`
  with mode 0600. If the option is unset/null, removes the target
  file (defensive — fresh installs go directly to "anonymous"
  state) and emits a WARNING that explicitly references K-1 so the
  next time this fails, the operator finds the bug review.

  **Tests:** new `tests/test_setup_configs_claude_oauth.py` (7
  cases): raw-token write, empty-value file-removal, "null"-string
  file-removal, op://-reference resolution via stubbed `op` CLI,
  op:// without OP_SERVICE_ACCOUNT_TOKEN fails-safe, run template
  doesn't UNSET the OAuth token, and a structural sanity-check
  asserting block ordering vs github-token / seed-copy markers.

### Process note

The exploration4 session originally marked P12 (full plugin
lifecycle e2e) DEFERRED with reasoning that boiled down to "ran out
of time." Operator instructed retraction; new playbook doctrine
"Time is never a deferral reason" was committed (inner docs commit
`f975967`) before re-running. Re-running P12 immediately surfaced
K-1 — the canonical example of why the rule matters. **A probe
skipped on time grounds is, sometimes, a HIGH bug not yet found.**

### J-1 (LOW, docs-only — also bundled)

`memory/feedback_phase_z_default_recipe.md` updated with an
empty-overlay verification step. The v0.34.0 ship's claimed Phase Z
at 13:30Z did not actually wipe the overlay (every config file had
pre-13:30Z mtimes), producing 4 doctrine drift WARNINGs at next
boot (filed as I-1, now CLOSED). Adding `ls -A` empty-verify
between rm and start prevents recurrence. No code change.

### Carry-forward

- **H-2** (LOW, third-party regression) — claude-agent-sdk
  v0.1.72 still latest; recheck `gh release list --repo
  anthropics/claude-agent-sdk-python` at next ship gate.

## [0.34.0] - 2026-05-01 — Bug bundle: H-1 + H-3

Bug bundle from `docs/bug-review-2026-05-01-exploration3.md`. Two NEW
HIGH findings filed during the third exploration session against
v0.33.1: H-1 (configurator engagement lifecycle race — every
hard-reload workflow leaves a stuck `status=active` engagement and no
user-DM completion message) + H-3 (soft reload of triggers on residents
permanently broken since 2026-04-22 commit `e81f264`, surfaced 9 days
later by v0.33.0's G-4 structured outcome=error logging). G-1/H-2
third-party regression carries forward — claude-agent-sdk-python
latest is still v0.1.72; no upstream CC CLI fix shipped.

### Fixed

- **H-1 (HIGH, configurator engagement lifecycle) — `casa_reload`
  Supervisor restart races SDK subprocess.** The doctrine ordering
  (`commit -> casa_reload -> emit_completion`) is correct for user
  experience, but the platform-side race was: `casa_reload` POSTed
  Supervisor's `addons/self/restart` synchronously, the POST returned
  in <1s, and Supervisor scheduled an async container kill that
  arrived ~13s later — cancelling the SDK subprocess BEFORE the model
  could call `emit_completion`. Engagement stuck `status=active
  completed_at=null sdk_session_id=null`, no `_finalize_engagement`,
  no user-DM completion message. Reproduced 100% across 3 hard-reload
  configurator engagements in exploration3 (P4.2 + P4.3 + P11.1).
  **Fix:** when `casa_reload` is called inside an active engagement
  (`engagement_var.get(None) is not None`), it now adds the
  engagement id to a new module-level
  `_ENGAGEMENTS_DEFERRED_HARD_RELOAD: set[str]`, drains the v0.33.1
  G-2 PENDING_RELOAD obligation, and returns immediately with
  `{supervisor_status: 200, deferred: true}` — NO Supervisor POST.
  At the end of `_finalize_engagement` (after the bus message + Honcho
  meta-summary have landed), if the deferred marker is present AND
  `outcome=completed`, the platform performs the actual Supervisor
  POST. The marker is drained on every terminal path
  (completed/cancelled/error) to prevent stale state. Out-of-engagement
  calls (operator-driven `/invoke`) still POST inline as before.
  Doctrine `agents/executors/configurator/doctrine/{reload.md,completion.md}`
  + recipes `specialist/create.md` + `plugin/install.md` updated to
  describe the deferred-restart mechanism. `casa_reload` tool docstring
  updated from "Only call AFTER emit_completion has been sent" to
  "Call BEFORE emit_completion. The actual addon restart is deferred"
  — pre-fix the docstring directly contradicted the doctrine.
  Tests:
  - `tests/test_h1_deferred_hard_reload.py` (8 cases) covering
    in-engagement defer, out-of-engagement inline POST,
    PENDING_RELOAD drain on call, and the four
    `_finalize_engagement` outcome combinations.
  - `tests/test_casa_reload_tool.py::test_configurator_engagement_var_path_allowed`
    updated for new `deferred: True` shape.

- **H-3 (HIGH, soft-reload broken for residents) —
  `casa_reload_triggers` always failed for residents because
  `agent_loader.load_agent_from_dir` was called with `policies=None`.**
  Latent since commit `e81f264cae103722c75970f2186076eb351b1d98`
  (2026-04-22, "feat(3.5-p3): casa_reload_triggers MCP tool"). 9 days.
  Residents have `disclosure.yaml`; `agent_loader._compose_prompt`
  raises `LoadError` when `disclosure is not None AND policies is
  None`. The pre-fix unit tests only covered specialists (no
  disclosure.yaml), so the bug slipped through. Surfaced by v0.33.0's
  G-4 structured outcome=error logging in exploration3 P8.1 — prior
  sessions hid the same failure mode as a silent error. **Fix:**
  load `PolicyLibrary` fresh from disk on each call via
  `policies.load_policies(/addon_configs/casa-agent/policies/disclosure.yaml)`
  and thread it into `load_agent_from_dir`. Stateless — no
  `init_tools` plumbing required. Returns a structured `load_error`
  with a useful message if the policy file is missing.
  Tests: `tests/test_casa_reload_triggers_resident.py` (3 cases —
  resident-with-disclosure happy path, missing-policies-file load
  error, specialist regression check).

### Carry-forward (no Casa-side fix)

- **H-2 (LOW, third-party regression).** Continuation of G-1 from
  v0.33.x: CC CLI 2.1.126 still throws `'NoneType' object has no
  attribute 'items'` on hook callbacks during Edit/tool_use; ~24
  callbacks per Edit-using configurator engagement (UP from ~13 in
  exploration2). Bytecode line unchanged at 9212. Latest
  claude-agent-sdk-python release is still v0.1.72 (2026-05-01) —
  no post-G-1 release shipped. Functionally harmless; operator log
  noise + minor log-storage cost. Recheck `gh release list` at next
  ship gate.

### Verification recipe corrections (carry forward)

The v0.33.1 verify recipe missed H-1 because it didn't inspect
`engagements.json` post-restart. The new verify shapes:

- **H-1 verify:** drive a configurator engagement that touches
  `agents/assistant/character.yaml`. Assert: artifact lands +
  activates AND `engagements.json` shows `status=completed
  completed_at=<float>` AND user DM receives a "Done"-shape relay
  via Ellen. The pre-fix recipe (just check that the addon restarted
  + the artifact is on disk) is INSUFFICIENT.
- **H-3 verify:** drive a configurator engagement that adds a trigger
  to `agents/assistant/triggers.yaml`. Assert
  `casa_reload_triggers(assistant)` returns `status=ok` with
  `registered: [trigger_name]`. Check apscheduler has the new job
  within ~5s. SOFT-RELOAD-ONLY path — no hard reload.

## [0.33.1] - 2026-05-01 — Hotfix: G-2 defensive reload guard

v0.33.0's doctrine-only fix for G-2 failed to converge live. Active
verify on cid `a9313680` (2026-05-01 11:39:57Z): the configurator
read the inverted-order `completion.md` + `reload.md`, then still
skipped the `casa_reload` tool_use (idx=15 `config_git_commit` →
idx=16 `emit_completion`, no reload between or after) and emitted
the same false-positive narration ("Reload triggered to apply.")
without the actual call. Same `committed but inert` failure mode as
v0.32.x.

### Fixed

- **G-2 (MEDIUM, ringleader) — defensive reload guard.** Per kickoff
  option (b) once the doctrine fix didn't converge, add a platform-
  side post-condition check. New module-level
  `_ENGAGEMENTS_PENDING_RELOAD: set[str]` in `tools.py` is populated
  by `config_git_commit` when its return SHA is non-empty (real
  commit landed) and drained by `casa_reload` /
  `casa_reload_triggers` on success. `emit_completion` inspects the
  set on `outcome=completed` entry — if the engagement is still
  pending a reload, it logs a WARNING citing the engagement id and
  force-calls `casa_reload.handler({})` BEFORE
  `_finalize_engagement` so the bus message lands after the
  Supervisor restart is scheduled (matching the existing
  bus-persists-across-restart contract). The set is drained
  unconditionally on every `emit_completion` exit to prevent stale
  state on idempotent re-emit / outcome=error paths.
  Tests:
  `tests/test_emit_completion_defensive_reload.py` (4 cases):
  - committed-without-reload force-calls + WARNING.
  - reload-already-called skips force-call.
  - no-commit skips force-call.
  - outcome=error skips force-call (engagement bailed; reload
    decision is the operator's, not the platform's).

## [0.33.0] - 2026-05-01 — Bug bundle: G-1 + G-2 + G-3 + G-4

Bug bundle from `docs/bug-review-2026-05-01-exploration2.md`. Four
collateral findings filed during the second exploration session against
v0.32.1: two MEDIUM (G-2 configurator doctrine compliance — ringleader,
G-4 engagement-error reason logging) + two LOW (G-1 carrying-over CC
CLI hook noise that v0.32.0 tried-but-failed to fix, G-3 sentinel-leak
on user-driven turns).

### Fixed

- **G-2 (MEDIUM, doctrine compliance) — configurator narrated
  `casa_reload(_triggers)(scope)` in completion-summary text but never
  tool-called it before `emit_completion`.** Reproduced 100% across
  P8 (trigger create) and P11 (specialist create) in exploration2.
  Trigger + specialist commits landed schema-valid in YAML but never
  activated in scheduler / agent registry — operator saw a "Done"
  message that was empirically false ("committed but inert"). Fix is
  doctrine-only: invert the canonical order in `completion.md`,
  `reload.md`, and every `recipes/*` recipe so the reload step lands
  BETWEEN `config_git_commit` and `emit_completion`. Pre-fix order was
  `commit → emit_completion → reload`; the model treated
  `emit_completion` as terminal and dropped step 3. Post-fix order is
  `commit → reload → emit_completion`, making `emit_completion` the
  natural terminal step AFTER the reload has run. New regression test
  `tests/test_configurator_doctrine_reload_order.py` parametrizes over
  every recipe and asserts the textual position of the first reload
  tool_use precedes the first `emit_completion` call.

- **G-4 (MEDIUM, configurator robustness) — engagement finalized
  outcome=error 24s after subprocess `system_init` with zero log
  evidence of why** (live: P8.2-followup cid `be9471b7` engagement
  `fa3c1486` 2026-05-01 10:30:55Z). Fix splits the finalize log line:
  outcome=error now emits at WARNING with structured `kind=` (from
  registry origin's `error_kind`, populated by `mark_error`) and
  `reason=` (text from emit_completion or registry message, with
  `no_reason_provided` sentinel as fallback). Companion fix in
  `drivers/in_casa_driver.py::_deliver_turn`: when the SDK loop
  completes without producing any `AssistantMessage` frames, emit
  `subprocess_terminated reason=no_assistant_message` at WARNING so
  hook-deny / model-refusal / subprocess-crash paths surface
  immediately instead of leaving operators chasing silent finalizes.
  Tests: `tests/test_finalize_engagement_error_reason.py` (3 cases)
  + `tests/test_in_casa_driver.py::test_start_empty_turn_logs_subprocess_terminated`.

- **G-1 (LOW, third-party regression) — F-5 was a false-positive.**
  v0.32.0's claude-agent-sdk 0.1.61 → 0.1.72 bump (CC CLI 2.1.112 →
  2.1.126) was supposed to close F-5's `'NoneType' object has no
  attribute 'items'` from hook_0/1/2/3 callbacks. Live verify in
  exploration2 (P4.2 cid `9946b835`) showed 13 hook callback errors
  during a single configurator engagement — same error class, bytecode
  line moved from 8382 to 9212 only. v0.32.0's verify was a passive
  10-min log scan with no Edit-using engagement active; the bug only
  fires while the hook bridge is processing tool_use payloads. Fix:
  no upstream patch available as of 2026-05-01 (claude-agent-sdk
  v0.1.72 is the latest tag); added a TODO comment to
  `casa-agent/requirements.txt` next to the pin. Active-verify recipe
  pinned for the next ship: drive a configurator engagement that uses
  Edit and assert `docker logs ... | grep -c "Error in hook callback
  hook_" == 0`.

- **G-3 (LOW, doctrine leak) — Ellen's outer turn echoed `<silent/>`
  literally to operator DM** after a configurator engagement
  (live: cid `dcc3c30b` 2026-05-01 10:27:02Z). The sentinel
  suppression in `agent.py::_deliver_response` was scoped to
  `MessageType.SCHEDULED` per
  `reference_scheduled_silence_contract` — Ellen had absorbed the
  heartbeat trigger's `<silent/>` doctrine via mid-engagement Read of
  triggers.yaml and emitted it on her user-DM follow-up turn where the
  gate did not fire. Fix lifts the SCHEDULED-only condition: any turn
  whose accumulated text strips to `<silent/>` (or to whitespace) is
  now suppressed regardless of trigger source. Tests:
  `test_silent_sentinel_suppresses_send_on_request_turn` +
  `test_whitespace_suppresses_send_on_request_turn` — verify both
  shapes also gate user-driven REQUEST turns.

### Not in this release

- F-1 (ha-prod-console plugin smoke skill HMAC noise) — not Casa.
- F-3 (HA MCP `GetDateTime ok=False`) — defer to next bug-review pass;
  check HA MCP server's current release first.
- policies/* schema validation gap (carried over from v0.31.1).
- Configurator UX gap on multi-file pre-existing schema offenders
  (carried over from v0.31.0).

## [0.32.1] - 2026-05-02 — Hotfix: F-7 envelope key snake_case

v0.32.0's F-7 fix used `isError` (camelCase) on the MCP envelope dict.
The Anthropic Agent SDK's MCP-server adapter reads
`result.get("is_error", False)` (snake_case) at
`claude_agent_sdk/__init__.py:512` and converts to the wire field
`isError` itself — so our `isError` key was silently dropped and
`engage_executor` for a disabled executor still showed `ok=True` in
the cid trace (live evidence: cid `6f56682c` post-v0.32.0 deploy,
`tool_result idx=3 name=mcp__casa-framework__engage_executor ok=True
ms=9125` for `target=plugin-developer`).

### Fixed

- **F-7 (LOW, contract) — envelope key swap.** `_result()` now sets
  `is_error: True` (snake_case) instead of `isError`. Test was
  updated to assert on the correct key. Behavior is now wire-correct:
  the SDK reads the dict, sets `ToolResultBlock.is_error=True`, and
  `sdk_logging.log_tool_result` emits `ok=False`.

## [0.32.0] - 2026-05-02 — Bug bundle: F-2 + F-4 + F-5 + F-6 + F-7

Bug bundle from `docs/bug-review-2026-05-02-exploration.md`. Five
collateral findings filed during the first exploration session against
v0.31.1 with NPM upstream finally bound. No HIGH-severity bugs in the
bundle — one MEDIUM doctrine drift + four LOW (telemetry, intermittent,
third-party, contract). F-1 (ha-prod-console plugin) and F-3 (HA MCP
GetDateTime) deferred — out of Casa scope.

### Fixed

- **F-6 (MEDIUM, doctrine drift) — `defaults/agents/assistant/
  executors.yaml` listed fictional `engagement` as a third
  executor_type.** Ellen counted three executors and named "engagement"
  as the third — but the executor registry has only two real types
  (configurator + plugin-developer; hello-driver is `enabled: false` by
  design). The third entry's `when:` text actually described
  `delegate_to_agent(mode='interactive')` (a Tier 2 specialist primitive),
  conceptually misclassified as a Tier 3 Executor. Fix: deleted the
  fictional entry from the seed YAML; folded its sync-vs-interactive
  delegation guidance into `defaults/agents/assistant/prompts/system.md`
  under a new "Sync vs interactive delegation" subsection. Added a
  regression test
  (`test_executors_yaml_lists_only_real_registered_executor_types`)
  that enumerates real executor directories and asserts the doctrine
  list is a subset.

- **F-2 (LOW, telemetry) — `CachedMemoryProvider._refresh` dropped
  `agent_role`.** The v0.30.0 / M3-self ship threaded `agent_role`
  through `agent.py::_one_scope` and v0.31.0 added a caller-side
  regression-locker, but the locker only asserts the kwarg-set is a
  *subset* of allowed — empty-kwargs callers passed trivially. The
  third caller in `memory.py:642` (post-turn cache refresh, fired from
  `add_turn` for every turn that hit the cache) emitted
  `memory_call ... agent_role="?"` lines on every voice prewarm and
  cached text-channel turn. Fix: plumbed `agent_role` from `add_turn`
  into `_refresh`, then into the inner backend's `get_context` call.
  Live evidence: voice-sse cid `d7378b64` from the 2026-05-02
  exploration.

- **F-7 (LOW, contract) — `engage_executor` returned `ok=True` for
  registry-rejected calls.** The MCP envelope returned by the tool
  carried no `isError` flag, so `sdk_logging.log_tool_result` emitted
  `ok=True ms=...` even when the executor type was disabled or unknown.
  Operator telemetry showed false-positive engagement spawns; user-
  facing narration was already correct. Fix: `_result()` helper now
  auto-detects `payload["status"] == "error"` and sets `isError: True`
  on the envelope. Behavior is consistent across every status:error
  return in tools.py — engage_executor was the surfaced symptom but
  the contract gap was system-wide. Live evidence: P5 cid `20a903c3`
  from the 2026-05-02 exploration (plugin-developer disabled).

- **F-4 (LOW, intermittent) — engagement finalize meta-summary write
  lost on Honcho TLS/SSL connection close.** The Honcho client reuses
  HTTPS connections; on long idles the upstream may close the TLS
  session, surfacing as `Connection error: TLS/SSL connection has been
  closed (EOF)` on the next request. Engagement still finalized
  `outcome=completed` (no user-visible impact) but the M4 meta-scope
  summary was lost. Fix: added a one-shot retry on transient
  connection-class errors at the meta-summary write site; non-
  transient errors (schema rejects, programming bugs) skip the retry.
  Live evidence: P4.2 cid `0fb4428d` engagement `9230dfd6` from the
  2026-05-02 exploration.

- **F-5 (LOW, third-party) — bundled CC CLI 2.1.112 hook callbacks
  threw `'NoneType' object has no attribute 'items'`.** Three hooks
  fired per Edit tool_use, each spewing ~6KB of minified JS source per
  error. Turn completed successfully but logs were noisy. Fix: bumped
  `claude-agent-sdk` from 0.1.61 → 0.1.72, which bundles CC CLI
  2.1.126 (past the buggy 2.1.112 version). No SDK API drift; full
  pytest passes (mod 2 known Windows installer flakes per memory
  `reference_npm_winerror_test`).

### Out of scope (filed, not fixed)

- **F-1 (not Casa) — ha-prod-console plugin smoke skill logs HMAC
  ERROR even when `webhook_auth_enabled: false`.** Fix belongs in the
  ha-prod-console plugin's smoke skill, not Casa.
- **F-3 — HA MCP `GetDateTime` returns `ok=False`.** Tool is shadowed
  by SDK `<current_time>` injection; user-visible impact is zero.
  Investigate against current HA MCP server release in a separate
  session; possibly upstream.

## [0.31.1] - 2026-05-01 — Hotfix: validate_config_repo scoping + hello-driver/hooks.yaml seed

Live N150 verify against v0.31.0's E-G gate exposed two follow-on
issues that had to be fixed before the gate works for actual
configurator engagements.

### Fixed

- **E-G follow-on (HIGH) — `validate_config_repo` walked the whole
  repo and applied `_SCHEMA_BY_FILENAME` by basename only, so
  `policies/disclosure.yaml` was validated against the per-agent
  `disclosure.v1.json` schema instead of its actual schema
  (`policy-disclosure.v1.json`). The two schemas have completely
  different shapes — agent disclosure has a single top-level `policy:`
  string, policy disclosure has a top-level `policies:` map of named
  bundles. Validation rejected
  `Additional properties are not allowed ('policies' was unexpected)`
  on the (untouched, valid) live policies file, falsely refusing
  every commit. Fix: scope the walk to `<config_dir>/agents/` only.
  Boot-time `policies.py::load_policies` and `scope_registry.py`
  catch policy-side schema violations on their own. The gate now
  fires on its intended target (configurator hallucinating fields
  under `agents/<role>/character.yaml`) without false positives on
  policy files. Verified live during the v0.31.0 ship's E-G probe
  (cid `4ee4013a`, configurator engagement `ef728344`): the gate
  correctly refused the `TRAIT:` top-level key edit; the configurator
  self-corrected to a valid edit inside the `card:` field; my v0.31.0
  bug then blocked the valid commit. v0.31.1 lets that flow through.

- **Latent hello-driver seed bug (LOW) — `defaults/agents/executors/
  hello-driver/hooks.yaml`** shipped with `PreToolUse: []` /
  `PostToolUse: []` (PascalCase, claude-code-driver style) but
  `hooks.v1.json` requires `schema_version: 1` + `pre_tool_use: []`
  (snake_case). The file would FATAL boot validation if hello-driver
  were enabled. Latent because hello-driver is `enabled: false` by
  default (per `project_3_5_plan4a_shipped`). Surfaced by v0.31.0's
  `validate_config_repo` walk; the bug had been silently shipped since
  the executor was first introduced. Fix: corrected to schema-conformant
  shape.

### Tests

- `tests/test_agent_loader.py::TestValidateConfigRepo` — 2 new tests:
  `test_no_agents_dir_returns_empty` (defensive: tool path must not
  crash on a fresh repo without the agents/ subtree); `test_skips_policies_dir`
  (regression-guard: realistic `policies/disclosure.yaml` with
  `policies:` block must NOT trip the agent gate). Existing
  `test_skips_dotgit_dir` updated to land its dotgit at
  `agents/.git/` since the gate no longer walks the repo root.

### Notes

- v0.31.0 / E-H + caller-side regression-locker shipped clean and
  was verified live: butler delegation cid `fec3a3a4` →
  `Delegation 1529c6e6 → butler ok (11.03s)`; zero
  `specialist memory read failed` WARNINGs in the 5min post-deploy
  probe window. M4b specialist memory restored.
- v0.31.0 / E-G ALSO shipped clean in its primary contract:
  configurator's first commit attempt with the `TRAIT:` invented
  key was correctly refused; configurator's full-context reasoning
  trace shows the schema error message reached the model and the
  model self-corrected to add the trait inside `card:` (the only
  free-text field in the character schema). Net result on N150:
  zero schema-invalid YAML landed in the inner addon_configs git.
  v0.31.1 just unblocks the valid commit.
- A pre-existing valid `card:` edit on `agents/assistant/
  character.yaml` plus the configurator's accidental rewrite of
  `agents/executors/hello-driver/hooks.yaml` (which corrected the
  PascalCase shape — itself the latent bug fixed in this ship) are
  both dirty in the live N150 working tree post-v0.31.1 deploy.
  Recovery: either let the next configurator engagement commit the
  card change (the now-valid hello-driver/hooks.yaml will land in
  the same commit), or manually `git -C /addon_configs/casa-agent
  checkout -- agents/assistant/character.yaml agents/executors/
  hello-driver/hooks.yaml` to discard.

## [0.31.0] - 2026-05-01 — Bug bundle: E-G + E-H + Phase Z playbook corrections

Closes the two HIGH bugs filed in
`docs/bug-review-2026-05-01-exploration.md` (the first exploration
session against v0.30.0) plus the playbook gaps that surfaced during
the same session's Phase Z. Single shipping sprint under pre-1.0.0
license; covers a stale `user_peer=` kwarg dark-spotting M4b
specialist memory since v0.26.0, and a configurator-driven write of
schema-invalid YAML that bricks the next boot until manual sed
recovery.

### Fixed

- **E-H (HIGH) — `delegate_to_agent` passes stale `user_peer` kwarg
  to `get_context`.** Pre-fix, `tools.py:454-460` (inside
  `delegate_to_agent`'s specialist-memory-read block) called
  `MemoryProvider.get_context(..., user_peer=user_peer)`, but
  v0.26.0 / E-14 dropped `user_peer` from the abstract signature.
  Every Ellen → specialist delegation since v0.26.0 (~3 days)
  raised `TypeError: HonchoMemoryProvider.get_context() got an
  unexpected keyword argument 'user_peer'`, was caught by
  `except Exception` at line 461, logged a WARNING, and silently
  degraded to empty memory context — every specialist (butler now;
  finance, eventually) ran with M4b dark. Same kwarg-drift shape as
  v0.29.0 / E-D and v0.30.0 / M3-self companion. Surfaced 2026-04-30
  23:27:13Z exploration session (cid `3407a7fb`, P2.1 butler
  delegation). Fix: drop `user_peer=user_peer` at `tools.py:459`.
  **Audit found a second offender** at
  `casa-agent/rootfs/opt/casa/channels/voice/channel.py:469` (voice
  prewarm); the original 2026-05-01 audit was scoped to tools.py +
  agent.py only and missed the voice channel. Fixed both in the same
  ship; voice prewarm has been silently failing since v0.26.0 too,
  caught by the same `except Exception` block at voice/channel.py:471.

- **E-G (HIGH) — Configurator writes schema-invalid YAML keys
  (CONFIRMED LIVE 2× on v0.29.0 + v0.30.0).** The configurator's
  `mcp__casa-framework__config_git_commit` accepted any
  structurally-valid YAML the agent produced and committed it to the
  inner addon_configs git, with NO schema validation. The model
  consistently invented YAML shapes — `TRAIT:` as a top-level key,
  `traits: [...]` collection, etc. — that are not in the schema's
  `additionalProperties: False` allowlist. Boot validation then
  FATALed on the next addon restart with `agent_loader.LoadError:
  schema violation at (root): Additional properties are not allowed
  ('TRAIT' was unexpected)`. Two prior incidents (v0.29.0 P4.2-V3 cid
  `1cef7687`; v0.30.0 P4.2 cid `cf9eb4cc`, engagement `15693b55`,
  commit `5cd731ac`) bricked the addon until manual `sed -i '/^TRAIT: /d' ...`
  recovery. Fix shape: pre-commit schema-validation gate. New
  `agent_loader.validate_config_repo(config_dir)` walks the repo for
  every schema-bearing YAML file (`character.yaml`, `voice.yaml`,
  `runtime.yaml`, `disclosure.yaml`, `delegates.yaml`,
  `executors.yaml`, `triggers.yaml`, `hooks.yaml`,
  `response_shape.yaml`, executor `definition.yaml`) and runs the
  same `_validate(...)` codepath boot uses, returning per-file error
  messages on failure. `tools.py::config_git_commit` calls this
  before `config_git.commit_config`; on any error, returns
  `{"status": "error", "kind": "schema_invalid", "errors": [...]}`
  WITHOUT committing — so the agent sees the schema error in its
  tool_result and can fix the YAML on the next iteration instead of
  bricking the addon on next boot. Defense-in-depth: same
  `_validate` codepath as boot ⇒ a passing pre-commit gate
  guarantees a green boot validation.

### Tests

- `tests/test_engage_executor_memory.py::test_get_context_callers_kwargs_match_signature`
  — caller-side regression-locker. AST-walks every `.py` under
  `casa-agent/rootfs/opt/casa/` and asserts every
  `.get_context(...)` call's kwargs ⊆
  `{session_id, tokens, search_query, agent_role}`. Verified to
  FAIL with E-H present and PASS after the fix. Complements the
  v0.29.0 signature-side `test_get_context_signature_locks_kwargs`,
  which only catches ABC-side drift.
- `tests/test_agent_loader.py::TestValidateConfigRepo` — 5 tests
  covering the new `validate_config_repo` API: clean repo returns
  empty list; the exact `TRAIT:` repro from the v0.30.0 P4.2 incident
  is caught with the expected error shape; non-schema files
  (markdown, plain text) are skipped; `.git/` is skipped; multiple
  offenders aggregate.
- `tests/test_config_git_commit_tool.py::TestConfigGitCommitSchemaGate` —
  3 tests covering the tool wiring: tool refuses with
  `kind: schema_invalid` when validation reports errors AND
  `config_git.commit_config` is NOT called; tool proceeds to commit
  when validation is clean; multiple errors aggregate in the
  response payload with `len(errors)` reflected in the message.

### Documentation

- `docs/exploration-testing-playbook.md::Phase Z` — three
  load-bearing corrections after the v0.30.0 ship's Phase Z burned
  four operator secrets (rotation deferred per pre-1.0.0 latitude)
  and FATALed on a leftover schema-invalid YAML: (a) options-backup
  step with `{"options": {...}}` envelope wrap at backup time;
  (b) **mandatory** `rm -rf /addon_configs/<slug>/*` step after
  install, before first start (E-11 persistent overlay does NOT
  survive uninstall — wait, it DOES survive, that's the whole
  problem; was never wiped); (c) options-restore via Supervisor REST
  API with envelope-shaped POST and `result`-only response extractor.
  Defense-in-depth note added: prefer HA UI Configuration panel for
  at-keyboard restores; reserve API path for unattended Phase Z.

### Notes

- v0.30.0's headline fixes (E-F engagement boot-race + M3-self
  peer_target) were live-verified clean on a fresh organic boot
  during the 2026-05-01 exploration session (engagement supergroup
  permissions registered from `_rebuild`'s tail; every Ellen DM
  produced `memory_call agent_role=assistant` across all 5 scopes).
  Coverage 14/18 PASS or PASS-with-note; 1 PARTIAL; 3 DEFERRED
  (configurator-write-risk pre-E-G fix, pre-existing NPM 502 from
  container-IP change post-reinstall).
- The v0.30.0 deploy hiccup mitigation
  (`character.yaml.bak-pre-v030` orphan) is now obsolete: the
  schema-validation gate prevents the class of bug that produced it.
  The orphan was wiped in the post-probe Phase Z; a fresh install
  carries no trace.

## [0.30.0] - 2026-04-30 — Bug bundle: E-F + M3-self peer_target

Closes the two HIGH bugs filed in
`docs/bug-review-2026-04-30-exploration3.md`. Single shipping sprint
under pre-1.0.0 license; covers a first-boot race that left every
engagement spawn refused as `engagement_not_configured` until manual
restart, and a Honcho 2.1.1 contract change that has been silently
dropping per-scope session digests on every Ellen DM since the M3
landing.

### Fixed

- **E-F (HIGH) — `setup_engagement_features` boot-race.** Pre-fix,
  `casa_core.py:1483` invoked `setup_engagement_features()` once at
  boot, immediately after `channel_manager.start_all()`. If the first
  `_rebuild` raised on `set_webhook` (transient first-boot DNS or
  network blip), `self._app` was never set, the boot call hit
  `None.get_me()`, and `engagement_permission_ok` stayed permanently
  False until manual restart. The supervisor's eventual successful
  rebuild populated `self._app`, but no path re-invoked
  `setup_engagement_features()`. Net effect: every `engage_executor`
  call returned `engagement_not_configured` — masking every
  configurator/plugin-developer/UC1/UC3 engagement spawn (P4/P5/P8/P11/
  P12/P15) on a fresh boot that hit any network blip. Fix in two parts:
  (1) `casa-agent/rootfs/opt/casa/channels/telegram.py:_rebuild` —
  `setup_engagement_features()` now runs as a tail step AFTER
  `self._app = app`, so every successful rebuild (initial OR
  supervisor-driven recovery) flips the permission flag automatically;
  (2) `casa-agent/rootfs/opt/casa/casa_core.py` — removed the redundant
  boot-time call. Belt-and-braces (3): `tools.py::engage_executor`
  failure path now attempts one in-line `setup_engagement_features()`
  retry when supergroup IS configured but the flag is still False —
  self-healing on the user's first engagement attempt without waiting
  for a probe-driven rebuild. Surfaced 2026-04-30 ~19:47Z exploration3
  (cid `45cd9e00`); workaround verified live as `ha apps restart` at
  21:36Z (cid `1cef7687`).

- **M3-self (HIGH) — `Session.context()` peer_target requirement.**
  Honcho 2.1.1's `Session.context()` validator rejects `search_query`
  without a paired `peer_target` —
  `ValueError: You must provide a peer_target when search_query is
  provided`. `memory.py::HonchoMemoryProvider.get_context` was issuing
  the SDK call without `peer_target`, so every per-scope session read
  on every Ellen DM raised; v0.29.0's E-B `exc_info=True` exposed the
  underlying exception (previously swallowed since the Honcho 2.1.1
  upgrade ~10 days). Functionally, Ellen fell through to `digest=""`
  on the M3-self path; peer_overlay carried continuity, but per-scope
  session digest was silently absent. Fix at
  `casa-agent/rootfs/opt/casa/memory.py:316-352` — thread `agent_role`
  through the abstract / Honcho / Cached / Sqlite / NoOp providers,
  and pass `peer_target=agent_role` to `session.context()` whenever
  `search_query` is set (spec § 2.3 — session memory is agent-targeted).
  Call sites updated: `agent.py::_one_scope` (the primary failing
  caller) and `tools.py::_fetch_executor_archive` (telemetry-only on
  the no-query path; consistency with the threaded contract).

### Tests

- `tests/test_memory_honcho.py` —
  `test_get_context_passes_peer_target_when_agent_role_and_search_query`
  asserts `peer_target=agent_role` is forwarded when both are
  supplied;
  `test_get_context_omits_peer_target_when_search_query_is_none`
  asserts the no-query path stays minimal (Honcho only requires the
  pairing on the search-query path).
- `tests/test_telegram_reconnect.py::TestSetupEngagementFeaturesInRebuild` —
  `test_first_set_webhook_fails_then_recovers_engagement_permission_flips_true`
  reproduces the E-F race by failing `set_webhook` once, then asserts
  `engagement_permission_ok=True` flips automatically after the
  supervisor's recovery rebuild — no external retry step.
  `test_setup_engagement_features_runs_after_app_is_published` is a
  spy-based ordering invariant: `setup_engagement_features()` MUST
  observe `self._app` already set when invoked.
- `tests/test_engage_executor_tool.py` — two new cases cover the
  defensive in-line retry: it fires once when supergroup is set but
  the flag is False, and it does NOT fire when supergroup is unset
  (the operator hasn't opted into engagements).
- Provider mocks across `tests/test_memory.py`, `tests/test_memory_cached.py`,
  `tests/test_agent_process.py`, `tests/test_engage_executor_memory.py`,
  `tests/test_notification_handling.py` updated to accept the new
  `agent_role` kwarg without changing call-shape assertions.

### Notes

- v0.29.0's E-E + E-D structural fixes were live-verified during
  exploration3 light-cleanup workaround (cid `1cef7687`, blessed-MCP
  path SHA `2b4ccab5`, no Bash fallback). E-F closure makes the
  workaround unnecessary — every fresh boot should land
  engagement-ready on the first successful `_rebuild`.
- Cosmetic-only `CLIConnectionError('ProcessTransport is not ready
  for writing')` from `claude_agent_sdk._internal.query.Query.
  _handle_control_request` after `emit_completion` finalizes is
  filed in `docs/bug-review-2026-04-30-exploration3.md` and deferred
  to a future ship — engagement outcomes are unaffected.

## [0.29.0] - 2026-04-30 — Bug bundle: E-E + E-D + E-B + E-C

Closes the four bugs filed in
`docs/bug-review-2026-04-30-exploration2.md`. Single shipping sprint
under pre-1.0.0 license; covers a CRITICAL ContextVar regression that
broke every in_casa configurator engagement since v0.20.0, a HIGH
silent kwarg-drift dropping the M4 L3 executor archive since v0.26.0,
a MEDIUM observability gap blocking M3-self root-cause investigation,
and a CRITICAL deployment-visibility gap masking every
`/opt/casa/defaults/` change shipped after first boot.

### Fixed

- **E-E (CRITICAL) — `engagement_var` ContextVar not propagating into
  SDK tool dispatch.** Pre-fix, `InCasaDriver.start()` bound
  `engagement_var` inside `_deliver_turn`, AFTER
  `ClaudeSDKClient.__aenter__()` had already called
  `claude_agent_sdk._internal.query.Query.start` — which spawns
  `_read_task` via `loop.create_task(self._read_messages())`. Per
  Python's asyncio semantics, `loop.create_task` captures the
  CURRENT context at task-creation time; the SDK's inner task
  therefore captured `engagement_var = None` and every tool callback
  it dispatched (including the privileged
  `config_git_commit` / `casa_reload` / `emit_completion`) saw
  `_effective_caller_role()` return the engager's `origin_var.role`
  ("assistant"), refusing all three. Net effect: every in_casa
  configurator engagement orphaned silently from v0.20.0 to v0.28.1
  (~5 days, missed because v0.20.0 / Phase 1's "manual configurator
  engagement test pending operator" deferred line was never
  discharged). Fix at
  `casa-agent/rootfs/opt/casa/drivers/in_casa_driver.py:74-115` —
  bind `engagement_var` BEFORE `client.__aenter__()` in `start()`,
  reset in `finally`. Same pattern applied to `resume()` at
  `:128-165`.

- **E-D (HIGH) — `_fetch_executor_archive` passes stale `agent_role`
  kwarg to `get_context()`.** v0.26.0 / E-14 dropped `agent_role` and
  `user_peer` from `MemoryProvider.get_context`'s signature; v0.27.0 /
  Bug 6 swept three call sites for the parallel `executor:<type>` →
  `executor-<type>` regex but missed the `agent_role=agent_role`
  kwarg drift here. Every executor engagement spawn (configurator,
  plugin-developer, hello-driver) raised `TypeError` against the real
  Honcho provider — silently swallowed by the function's `except
  Exception` and logged as a one-line WARNING without `exc_info=True`.
  M4 L3 cross-run executor memory was dark for every executor on
  every spawn since v0.26.0. Fix at
  `casa-agent/rootfs/opt/casa/tools.py:1179-1186` — drop
  `agent_role=agent_role`; also added `exc_info=True` to the warning
  for parity with E-B.

- **E-B (MEDIUM) — `Memory call failed` swallowed without
  `exc_info=True`.** Both warning sites in agent.py's per-turn memory
  read (`agent.py:374-379` for per-scope `_one_scope`; `agent.py:391-397`
  for `_overlay`) lost the underlying exception class + message,
  blocking root-cause investigation of M3-self failures that fired
  5× per Ellen Telegram turn from v0.x to v0.28.1. Fix is a single
  `exc_info=True` keyword on each `logger.warning(...)` call.

- **E-C (CRITICAL — visibility) — Persistent `/addon_configs/` never
  re-seeds.** `seed_agent_dir()` in `setup-configs.sh:28-34` is
  no-op when the destination dir already exists. After E-11's
  persistent ext4 bind mount (v0.19.0), every default-side change
  shipped via `/opt/casa/defaults/` after first boot was silently
  dark. Three confirmed dark-state examples spanning v0.26.1 →
  v0.27.0 → v0.28.0 (E-15 prompt-nudge missing, E-5 financial-
  arithmetic anchor missing, E-16 configurator plugin tools +
  recipes missing). Master CI runs against fresh volumes so the
  upgrade-over-existing-overlay path has zero coverage. Fix at
  `casa-agent/rootfs/etc/s6-overlay/scripts/setup-configs.sh:78-145`
  — adds an `# === drift-check ===` block that walks
  `/opt/casa/defaults/{agents,policies}/` vs the live overlay,
  byte-compares each file via `diff -rq`, and logs WARNING per
  drifted/missing file plus a one-line summary. Visibility-only;
  operator decides when to run Phase Z (uninstall+reinstall). The
  block is POSIX-clean (parallel to the existing seed-copy block)
  so it can be unit-tested via `sh -c`.

### Tests

- **`tests/test_in_casa_driver.py::TestInCasaEngagementContext::test_engagement_var_propagates_into_sdk_inner_task`**
  — models the SDK's `Query._read_task` spawn pattern (a fake client
  whose `__aenter__` calls `loop.create_task` then snapshots
  `engagement_var.get(None)` inside that task). Pre-fix the snapshot
  is `[None]`; post-fix it is `[rec]`. Catches any future regression
  of E-E.

- **`tests/test_engage_executor_memory.py::test_returns_empty_when_archive_empty`**
  updated to assert `"agent_role" not in kwargs` and
  `"user_peer" not in kwargs` on the `get_context` call.
- **`tests/test_engage_executor_memory.py::test_get_context_signature_locks_kwargs`**
  — introspection-based regression test that asserts
  `MemoryProvider.get_context`'s parameter set is exactly
  `{self, session_id, tokens, search_query}`. Locks against future
  caller-vs-ABC drift at unit-test time rather than waiting for an
  exploration session to surface it.
- **`tests/test_executor_archive_is_read_on_second_engagement`**
  in-memory `_Mp` mock updated: dropped `agent_role` and
  `user_peer` from its `get_context` signature.

- **`tests/test_agent_process_scope.py::TestMemoryFailureLogsExcInfo`**
  — two tests covering both warning sites:
  `test_one_scope_failure_includes_exc_info` raises a TypeError from
  `ensure_session` and asserts `caplog`-captured record has
  `exc_info` populated with the original exception class + message;
  `test_overlay_failure_includes_exc_info` does the same for
  `peer_overlay_context`.

- **`tests/test_setup_configs_drift_check.py`** — five tests against
  the extracted drift-check block, mirroring the seed-copy test
  shape. Covers: clean trees → INFO summary; drifted file → WARN +
  `drifted=1`; missing file in live → WARN + `missing=1`;
  operator-added file in live → ignored (no false-positive drift);
  missing default dir → graceful early-return.

### Cross-refs

- `docs/bug-review-2026-04-30-exploration2.md::{E-B, E-C, E-D, E-E}`
  — full forensic + suggested-fix-shape that drove this ship.
- v0.20.0 / Phase 1 (commit `077714d`) — E-7's
  `engagement_var.set` in `_deliver_turn` covered Ellen's path but
  missed the SDK inner-task capture-at-`__aenter__` semantics.
  Memory `project_phase1_engagement_context_shipped`'s "manual
  configurator engagement test pending operator" note would have
  caught E-E — discharged here.
- v0.26.0 / E-14 (commit `b3dac55`) — `MemoryProvider` ABC reshape
  that dropped `agent_role` from `get_context`. Memory
  `project_phase5_e14_shipped`.
- v0.19.0 / E-11 (commit `54ae912`) — addon_config map flip to
  `all_addon_configs:rw` made `/addon_configs/casa-agent/`
  persistent. E-C is the unintended consequence of that
  persistence — every fix shipped after v0.19.0 to
  `/opt/casa/defaults/` is silently dark on the live N150 until
  operator wipe.

### Success signal

Next exploration session reruns
`docs/exploration-testing-playbook.md` from P4.2 onwards. Expected:
- **P4.2:** configurator engagement closes cleanly
  (`status=completed`, `config_git_commit` + `emit_completion`
  succeed); no Bash fallback.
- **P5/P8/P11/P12/P15:** all unblocked from E-E.
- **E-B:** turn-trace shows the actual exception class for every
  `Memory call failed` warning (driving the next corrective ship).
- **E-C:** boot logs show `drift_check missing-in-live` /
  `drift_check drifted` WARN lines for any defaults the operator
  hasn't wiped.

## [0.28.1] - 2026-04-30 — E-A: Telegram channel fully broken since v0.22.0

Surfaced live during the 2026-04-30 afternoon exploration session
(`docs/bug-review-2026-04-30-exploration.md`, E-A) on the very first
DM probe. Every inbound Telegram update — DM to Ellen, supergroup-
topic message, slash-command, originator check — has been dropped
since v0.22.0 (Phase 3a, commit `7f58143`, 2026-04-30 morning) with a
silent `WARNING channels.telegram: Telegram handler error (not
retryable): TelegramChannel.handle_update() takes 2 positional
arguments but 3 were given`. PTB returned 200 to the webhook caller,
so smoke probes (`/invoke`, `/api/converse`) and master CI never
noticed; the bug was load-bearing for ~half the exploration playbook
(P4/P5/P6/P11/P12/P15 are all engagement-driven).

### Fixed

- **`casa-agent/rootfs/opt/casa/channels/telegram.py:479`** — added
  the `_context: ContextTypes.DEFAULT_TYPE | None = None` parameter
  to `handle_update`. PTB v20+ `MessageHandler` invokes its callback
  with `(update, context)`; the missing parameter raised TypeError on
  every Telegram update for ~10 hours. Context is unused — the
  channel reads everything it needs from `update` and Casa's
  bus/engagement registry — but the parameter must exist for PTB's
  dispatch contract.

### Tests

- **`tests/test_telegram_engagement_routing.py::TestPTBDispatchContract`** —
  two regression tests:
  - `test_handle_update_accepts_ptb_two_arg_callback` — calls
    `ch.handle_update(update, ptb_context)` directly, asserts no
    TypeError.
  - `test_handle_update_dispatched_through_ptb_message_handler` —
    builds a real `MessageHandler(filters.TEXT, ch.handle_update)`
    and walks a synthetic update through `handler.callback(update,
    context)` exactly the way `Application.process_update` does.
    The single-line difference between this test and the
    `handle_update(u)` unit calls already in the file is what would
    have caught E-A pre-ship.

### Cross-refs

- `docs/bug-review-2026-04-30-exploration.md::E-A` — full
  forensic + suggested-fix-shape that drove this ship.
- `docs/bug-review-2026-04-30-exploration.md::E-B` — companion
  observability gap (`agent.py:374-378` swallows `Memory call failed`
  exception without `exc_info=True`); not fixed in this ship —
  filed for a follow-up session.

## [0.28.0] - 2026-04-30 — E-16: Configurator plugin-tools gap

Closes the Plan 4b consumer-side gap surfaced by the 2026-04-30 audit
and exposed in `docs/exploration-testing-playbook.md::P12 step 3`.
Until this ship, the Configurator could neither call the plugin
install/remove tools (not in `tools.allowed`) nor walk the operator
through the flow (no `recipes/plugin/` doctrine).

### Changed

- **`agents/executors/configurator/definition.yaml::tools.allowed`** —
  added 10 plugin-lifecycle tools:
  `marketplace_{add,remove,update,list}_plugin`, `install_casa_plugin`,
  `uninstall_casa_plugin`, `verify_plugin_state`,
  `set_plugin_env_reference`, `list_vault_items`, `get_item_fields`.
  Excluded `verify_plugin_secrets` — its description marks it a
  back-compat shim for `verify_plugin_state`.

### Added

- **`agents/executors/configurator/doctrine/recipes/plugin/`** — new
  recipe directory. Four files, mirroring `recipes/trigger/` style:
  - `install.md` — five-stage install flow (marketplace →
    system-requirements → per-agent install → secrets → verify),
    with reload + common-mistakes sections.
  - `remove.md` — uninstall flow + optional full-removal sequence
    (marketplace tear-down + secret unwiring).
  - `marketplace.md` — marketplace-only operations (list, register,
    update pin, unregister) with explicit reload-not-needed contract.
  - `secrets.md` — `set_plugin_env_reference` + 1Password discovery
    helpers (`list_vault_items`, `get_item_fields`).

### Cross-refs

- **`recipes/plugin/`** linked from `architecture.md` § "Configurator
  MCP tools (v0.14.1)" and `reload.md` (added two rows: install/remove
  and `set_plugin_env_reference` — both `hard` reload).

### Tests

No new test code. This ship is doctrine + allowed_tools surface only;
no Python or YAML behavior changed beyond the configurator's
self-described tool list. Plan 4b coverage for the underlying tools
remains in place (`tests/test_marketplace_ops.py`,
`tests/test_marketplace_tools.py`, `tests/test_install_casa_plugin.py`,
`tests/test_verify_plugin_state.py`, `tests/test_plugin_env_conf.py`).
`uninstall_casa_plugin`, `set_plugin_env_reference`, `list_vault_items`,
and `get_item_fields` have no direct unit coverage but have been live
since v0.14.1; the canonical end-to-end exercise is P12 in the
exploration playbook.

### Success signal

Next exploration session reruns P12 step 3 (Configurator installs the
`casa-probe-*` plugin into Ellen's `enabledPlugins`) end-to-end.
Steps 4-7 (verify load, in-agent skill use, configurator removes,
graceful degradation) become exercisable for the first time.

## [0.27.0] - 2026-04-30 — Phase 6: Polish (E-5 + E-9 + Bug 6)

Closes the bug-review-2026-04-29 backlog. Three independent
fixes; one ship.

### Fixed

- **E-5 — Ellen LLM arithmetic on finance failure.** New
  `## Financial arithmetic` subsection in
  `assistant/prompts/system.md` forbids Ellen from computing
  financial figures herself. On `delegate_to_agent('finance')`
  failure, Ellen now declines (*"I can't compute that without
  Alex — let's try again once finance is reachable"*) rather
  than producing an LLM-computed table or total. Architectural
  invariant: no answer the user sees was computed by an LLM.
- **E-9 — Telegram engagement topic title truncation.** New
  `text_util.truncate_for_topic` helper with UTF-8 byte-strict
  word-boundary truncation and Unicode ellipsis. Replaces four
  `[:80]` hard slices in `tools.py` (delegate_to_agent +
  engage_executor, open + rename). Topic names now fit the
  128-byte Telegram Bot API limit and break on word boundaries.
- **Bug 6 — Honcho `executor:<type>` regex rejection.** Swapped
  `executor:<type>` → `executor-<type>` at three call sites in
  `tools.py` (read in `_fetch_executor_archive`, write in
  `_finalize_engagement`'s archive branch). Honcho v3 regex
  `^[A-Za-z0-9_-]+$` rejects colon; M4 L3 cross-run memory
  injection was effectively dark for executor engagements
  (configurator, hello-driver, plugin-developer). No migration
  needed — existing colon-keyed peers were unreachable.

### Added

- `casa-agent/rootfs/opt/casa/text_util.py` (new module).

### Tests

- New `tests/test_text_util.py` (6 unit tests for the helper).
- Extended `tests/test_assistant_prompts.py` with the E-5 anchor
  regression guard.
- Extended `tests/test_delegate_to_agent_interactive.py` with a
  long-task topic-name budget assertion.
- Extended `tests/test_finalize_engagement.py` with the
  executor-archive hyphen assertion.
- Updated `tests/test_engage_executor_memory.py` colon → hyphen
  at four assertion sites (Bug 6).

## [0.26.1] - 2026-04-30 — Phase 5: Memory hygiene (E-15)

### Changed

- **`consult_other_agent_memory` falls through to `cross_peer_context`** for disabled-but-known specialists. Memory is data, enablement is operational. Closes E-15 — live N150 verification of M6 cross_peer recall was BLOCKED on Finance disabled (per `project_memory_m6_shipped` Live N150 status). Now Ellen can recall a disabled specialist's accumulated memory.
- **`assistant/prompts/system.md`** — hedge inverted. Ellen now prefers `consult_other_agent_memory` (cheap memory recall) and reserves `delegate_to_agent` for fresh-data cases. Tina added to the cross-role example list.

### Added

- **`SpecialistRegistry.is_disabled(role)` / `disabled_roles()`** public accessors. Surface for E-15's fall-through and any future code that needs to distinguish disabled-but-bundled from genuinely-unknown roles.
- **Configurator doctrine** (resident/{create,update}.md) teaches operators that disabled specialists' peer-level memory remains consultable. Out-of-scope follow-up: optional `cfg.allow_memory_when_disabled: bool` for hard-gate semantics (deferred per spec § 3.3).
- **Unknown-role error message** in `consult_other_agent_memory` now lists disabled roles in the available-roles list (so Ellen self-corrects instead of bouncing off `unknown_role` for legitimate disabled targets).

### Spec / Plan

- Spec: `docs/superpowers/specs/2026-04-30-phase5-memory-hygiene-design.md` (§3)
- Plan: `docs/superpowers/plans/2026-04-30-phase5-memory-hygiene.md` (§B)

### Verification

- Local pytest (`-m "not docker and not slow"`, `tests/` scope): pass count + 7 new tests, 0 failed.
- Smoke 3/3 PASS.
- **Operator-driven Telegram probes** (manual, after deploy):
  - Probe 1 — Tina recall: "what did Tina mention about lights last week?" → expect `memory_call call_type=cross_peer observer=butler`.
  - Probe 2 — disabled-Finance recall: "what did Finance say about my Q1 invoices?" → expect `consult_other_agent_memory_call result_len > 0` (or = 0 if Honcho has no Finance peer-level data; success criterion is `unknown_role` NOT returned).

## [0.26.0] - 2026-04-30 — Phase 5: Memory hygiene (E-14)

### Changed (BREAKING — pre-1.0.0 license)

- **`MemoryProvider` ABC reshape**: `get_context(session_id, tokens, search_query)` drops `agent_role` and `user_peer` parameters; new abstract method `peer_overlay_context(observer_role, user_peer, search_query, tokens)` separates peer-level overlay reads from scope-level session reads.
- **Ellen `memory.token_budget`**: 4000 → 5000. Honest envelope for the new 40/60 overlay/scope split (5 scopes × 600 + 2000 overlay = 5000).
- **`BudgetTracker` warning text**: "Memory digest over budget … Investigate the memory backend." → "Memory digest exceeded expected envelope … Memory shape may have regressed." Reframe from cost cap to regression sentinel.
- **`CachedMemoryProvider` cache key**: `(session_id, agent_role, tokens)` → `(session_id, tokens)`. `agent_role` is no longer threaded through this layer.

### Added

- **Honcho-native two-primitive split**: per-turn memory assembly runs ONE `peer.context(target=…, search_query=…)` call (deduped peer-level overlay) + N `session.context(tokens=scope_budget, search_query=…)` calls (per-scope messages + summary). Closes the 5× peer-overlay duplication that produced `used=6210 budget=4000` warnings every Ellen turn (E-14, `bug-review-2026-04-29-exploration.md:507-512`).
- **`peer_overlay_context` method** on `HonchoMemoryProvider` (real impl) + `NoOpMemory` / `SqliteMemoryProvider` (graceful "" return) / `CachedMemoryProvider` (passthrough). Same fail-soft contract as M6's `cross_peer_context`.
- **`memory_call` `call_type: "self_overlay"` telemetry** at the new emission site, parallel to existing `self` (per-scope) and `cross_peer` (M6). Synthetic `session_id: "overlay-{role}-{user}"` shape.
- **`peer_overlay_empty` INFO log line** on empty overlay digest (cold start / Honcho deriver behind), per spec § 7 Q4.
- **Render helper split**: `_render` → `_render_session` (messages + summary only) + new `_render_peer_overlay` (peer_card + representation, self-perspective headings).

### Fixed

- **E-14 — Memory token budget overflow** (MEDIUM, `bug-review-2026-04-29-exploration.md:500-527`). Live evidence: `WARNING tokens: Memory digest over budget for session telegram-1197017861-assistant: used=6210 budget=4000 (>1.1x for 3 turns).` Steady-state every Ellen turn. Now silent for first 3 turns post-deploy; envelope warning is reframed to fire only on memory-shape regressions.
- **Latent M6 `cross_peer_context` bug** discovered during Task A.0's Honcho contract probe: `honcho-ai==2.1.1`'s `Peer.context()` rejects `tokens=N` kwarg (raises `TypeError` at signature bind via `@validate_call`). M6's `cross_peer_context` has been silently broken since shipped — `try/except` swallowed the TypeError, never fired organically (Finance disabled in production, no enabled non-Ellen peer with own memory). Fixed in this release: drop `tokens=N` from both `peer.context()` invocations, add render-side cap (chars/4) on rendered overlay. Unblocks E-15's Probe 2.

### Spec / Plan

- Spec: `docs/superpowers/specs/2026-04-30-phase5-memory-hygiene-design.md`
- Plan: `docs/superpowers/plans/2026-04-30-phase5-memory-hygiene.md` (this plan, §A)

### Verification

- Local pytest (`-m "not docker and not slow"`, `tests/` scope): pass count + 12 new tests, 0 failed.
- Smoke 3/3 PASS (healthz / turn-assistant / voice-sse).
- N150 telemetry distribution: 1× `call_type=self_overlay` per turn + N× `call_type=self` per turn (vs prior shape of N× `call_type=self` only).
- N150 `BudgetTracker` warning silent for first 3 Ellen turns post-deploy.

## [0.25.0] - 2026-04-30 — Phase 4b: SDK observability

### Added
- **Bug 3** (HIGH): every SDK turn (in_casa engagement and Ellen DM)
  emits per-message structured records. `assistant_message` and
  `turn_done` at INFO; `tool_use`, `tool_result`, `system_init` at
  DEBUG. Operators reading `docker logs` can reconstruct what the
  assistant did without raising the global level. Logger: `sdk`.
- **Bug 4** (HIGH): when the SDK CLI subprocess writes to stderr,
  output appears in Casa's `docker logs` stream tagged with `cid`
  and (where in scope) `engagement_id`. Six wiring sites covered:
  `agent.py` Ellen turn, `in_casa_driver.start` + `.resume`,
  `observer._decide_interjection`, `tools.delegate_to_agent`,
  `tools._synthesize_answer`. Logger: `subprocess_cli`.
- **Bug 5** (MEDIUM): when `agent._process`'s retry-fresh path fires
  (resume sid stale → ProcessError → clear + retry), one INFO line
  records the event with exit_code, prior_sid, stderr_tail. Logger:
  `agent`. Closes Bug 5 by side-effect of Bug 4 (root cause now
  visible) plus auditable retry telemetry.
- **G5** — `claude_code` driver per-engagement s6-log file relayed
  line-by-line into the `subprocess_cli` logger at DEBUG so when
  E-12 (claude_code topic silence) is later tackled the diagnostic
  data already exists.

### Internal
- New module `casa-agent/rootfs/opt/casa/sdk_logging.py` (~150 lines):
  `log_system_init`, `log_assistant_message`, `log_tool_use`,
  `log_tool_result`, `log_turn_done`, `make_stderr_logger`,
  `with_stderr_callback`, `extract_tool_target`. All consumers call
  through this module so log shape is identical and tested in one
  place.
- New `tests/test_sdk_logging.py` covers each function (17 tests).
- `dataclasses.replace` pattern from agent.py (clearing `resume`)
  reused for `with_stderr_callback`.
- Spec doc-rot caught at plan-write: spec § 6.6 listed two
  `ClaudeAgentOptions` construction sites; reality at master had six
  `ClaudeSDKClient` sites. All six wired in this PR (memory
  `feedback_pre_1_0_0_license` — additive change, no compat shims).

### Notes
- **Out of scope**: tool-marker rendering in topic (UX feature; future
  phase); E-12 (claude_code driver topic silence — needs its own design
  epic on whether to drop `--remote-control` vs design a tee); OTEL
  collector / exporter wiring; structured event-bus emission for SDK
  signals.
- **No engagement-data migration**; no schema change.
- **Performance**: the dispatch adds one logger.info + a few
  logger.debug calls per turn (microsecond cost). G5 relay is DEBUG-only,
  invisible in steady prod state.

## [0.24.0] - 2026-04-30 — Phase 4a: OTEL DEBUG-noise cleanup (Bug 7)

### Fixed
- **Bug 7** (LOW, cosmetic): the `claude_agent_sdk._internal.transport.subprocess_cli`
  logger no longer emits an `OTEL trace context injection failed`
  ModuleNotFoundError traceback on every CLI subprocess connect. Two
  changes: (1) `opentelemetry-api>=1.20.0` joins `requirements.txt`
  so the SDK's lazy `opentelemetry.propagate` import succeeds (no
  exception swallowed → no DEBUG traceback emitted). (2)
  `log_cid.install_logging` quiets the `opentelemetry` logger to
  WARNING as belt-and-braces against future SDK paths that emit
  through the same logger. Live evidence: 2026-04-30 06:40:22Z and
  06:40:29Z N150 v0.23.0 cids `c8fcfca1` + `c3fae47c`.

### Internal
- Single new test `test_log_cid.py::TestInstallLogging::test_otel_logger_quieted_to_warning`
  asserting the post-`install_logging()` effective level on the
  `opentelemetry` logger.

### Notes
- **Out of scope:** Phase 4b (Bugs 3 + 4 + 5 + claude_code log relay
  G5) ships separately as v0.25.0 with its own design surface.
  E-12 (claude_code driver topic silence) remains deferred to its
  own design epic.
- Cosmetic-only release. No API or schema changes. No
  config/options changes.

## [0.23.0] - 2026-04-30 — Phase 3b: engagement-topic streaming (Bug 1)

### Fixed
- **Bug 1** (HIGH): `InCasaDriver._deliver_turn` no longer buffers the
  entire SDK turn before posting to the engagement topic. Each
  `AssistantMessage` triggers a cumulative-text emit via the new
  `TopicStreamHandle` (1-second per-topic throttle, edit-in-place via
  Telegram `editMessageText`). Multi-step executor turns
  (Read → Edit → validate → reply) now show progressive visibility
  starting within seconds of the first model output, instead of 60-120s
  of silence followed by a single batch dump. Mirrors Ellen's existing
  `create_on_token` + `finalize_stream` pattern (`channels/telegram.py:739-859`)
  but parameterised by `topic_id` instead of `chat_id`.

### Internal
- New `channels.telegram.TopicStreamHandle` class + `TelegramChannel.create_topic_stream(topic_id)` factory method (~120 lines).
- `InCasaDriver.__init__` constructor signature change: `send_to_topic` kwarg removed; `topic_stream_factory` kwarg added. Pre-1.0.0 license; no shim.
- `casa_core.main` rewires `engagement_driver` to pass the factory; `claude_code_driver` keeps its `send_to_topic` kwarg unchanged (E-12 deferred to Phase 4).
- 8 new unit tests in `tests/test_telegram_topic_stream.py` covering first-emit / throttle / overflow / not-modified swallow / error logging.
- 2 new tests in `tests/test_in_casa_driver.py::TestInCasaStart` covering streaming semantics + skip-on-empty-AssistantMessage.

### Notes
- **Out of scope:** tool-marker rendering during streaming (M1 confirmed in spec §3 Q6); per-message logger lines in `_deliver_turn` (Phase 4 / morning Bug 3); CLI subprocess stderr capture (Phase 4 / morning Bug 4); claude_code driver streaming (E-12, Phase 4).
- **No engagement-data migration:** `engagements.json` schema unchanged; existing engagements (active or cancelled) read identically.

## [0.22.0] - 2026-04-30 — Phase 3a: cosmetic + cancel (E-2 + E-8 + E-13)

### Fixed
- **E-2** (LOW): Ellen's cumulative `attempt_text` in `agent.py:_attempt_sdk_turn`
  now inserts `\n\n` between successive `AssistantMessage` boundaries.
  TextBlocks within the same AssistantMessage remain joined without separator
  (one model thought). User-visible: delegation flows like "ack + Tina's
  answer" now read as discrete paragraphs instead of glued strings.
- **E-8** (MEDIUM): `InCasaDriver._deliver_turn` applies the same separator
  pattern in the configurator's buffered topic-post path. Multi-step
  executor turns ("Reading X. Now editing Y. Validating Z.") get clean
  paragraph breaks. Streaming visibility is still Phase 3b.
- **E-13** (HIGH): PTB `MessageHandler` registration now dispatches webhook
  updates to `handle_update` (engagement-aware router) instead of `_handle`
  (engagement-unaware bus-dispatch leaf). `/cancel`, `/complete`, and
  `/silent` posted in engagement topics are now intercepted as documented;
  previously they fell through to Ellen as plain turns. The engagement
  routing logic itself was already complete and tested (514 lines of
  pre-existing tests in `tests/test_telegram_engagement_routing.py`);
  only the wiring at line 247 was wrong.

### Notes
- Phase 3b (Bug 1 — engagement-topic streaming silence) ships separately as
  v0.23.0 with its own design spec at
  `docs/superpowers/specs/2026-04-30-phase3b-engagement-streaming-design.md`.
- Three orphan engagements (`52fa6ca8`, `9a78971d`, `c798e373`) from the
  v0.18.x exploration session can now be cleaned up via `/cancel` in their
  respective topics.

## [0.21.0] - 2026-04-29 — Phase 2: specialist provisioning + memory peer_id

### Fixed
- **E-4** (HIGH): `casa_core.main`'s agent-home provisioning loop now iterates
  every loaded in_casa **resident or specialist** agent, not residents only.
  Specialists (e.g. `finance`) get their `/addon_configs/casa-agent/agent-home/<role>/`
  directory created at boot, so delegations no longer fail with
  `sdk_error (Working directory does not exist: ...)`. Loop refactored into
  `agent_home.provision_all_homes()` for unit-testability. Executors remain
  excluded (they run with `cwd=/addon_configs/casa-agent`).
- **E-1** (MEDIUM): `memory._render()` now reads `peer_id` (Honcho v3 SDK
  shape per OpenAPI `components.schemas.Message`) before falling back to
  `peer_name` (legacy `_SqliteMsg`). Eliminates the
  `'Message' object has no attribute 'peer_name'` AttributeError that
  silently no-op'd M4b memory for every butler delegation on v0.20.0.

### Internal
- New helper `agent_home.provision_all_homes()` (3 unit tests in
  `tests/test_agent_home_provisioning.py`).
- Test stubs renamed to use `peer_id` (matches real Honcho SDK shape):
  `StubMessage` in `tests/test_memory_honcho.py`, `FakeMessage` in
  `tests/test_memory_render.py`. The wrong-shape stubs were why M3a's
  "real-shape coverage" failed to catch E-1.
- New regression tests `test_render_handles_honcho_v3_message_shape` and
  `test_render_handles_sqlite_message_shape` in `tests/test_memory_render.py`.

## [0.20.0] - 2026-04-29

### Fixed
- **E-6 / E-10**: `_effective_caller_role()` priority flip — `engagement_var`
  now checked before `origin_var`. Configurator engagements can again call
  `config_git_commit` and `casa_reload` without falling back to raw `git
  commit` or "manual addon restart" workarounds.
- **E-7**: `_deliver_turn` binds `engagement_var` for the duration of the
  SDK loop. `emit_completion` now resolves the active engagement instead
  of returning `not_in_engagement`.
- **Bug 2 (sdk_session_id checkpoint timing)**: `InCasaDriver` now eagerly
  persists `sdk_session_id` to the registry the first time
  `client.session_id` becomes non-null, instead of waiting for the 24h
  idle sweeper. An unclean shutdown mid-turn can no longer orphan an
  engagement with `sdk_session_id: null`.

### Changed
- `InCasaDriver.__init__` gains a new keyword arg
  `persist_session_id: Callable[[str, str], Awaitable[None]] | None`
  (default `None`). The single caller (`casa_core.main`) is updated.
  Pre-1.0 minor bump to signal the API change.

### Internal
- New `tests/test_role_gate_priority.py` (3 tests).
- `tests/test_in_casa_driver.py` extended (4 tests).
- `tests/test_engage_executor_tool.py` extended (1 leak-detection test).

## [0.19.0] - 2026-04-29 — Phase 0 / E-11: persistent addon-config mount

**BREAKING — first boot of v0.19.0 wipes and reseeds the entire
`/addon_configs/casa-agent/` tree.**

The previous map declaration paired `addon_config:rw` with `config:ro`,
both of which target `/config` inside the container. HA Supervisor
silently dropped `addon_config:rw` (the conflict loser), so
`/addon_configs/casa-agent/` was never a real bind mount — it was a
rootfs-overlay path that got wiped on every container rebuild. Every
configurator commit, every manual edit under `/addon_configs/casa-agent/`,
every plugin-marketplace install state, and every git history entry
in the addon-config tree vanished on the next `ha apps restart`. See
`docs/bug-review-2026-04-29-exploration.md` § E-11 for the full
forensic write-up + live evidence (mount-table dump, boot-log seed
trail, git-history collapse).

### Changed (BREAKING)

- **`casa-agent/config.yaml::map`** — replaced
  `addon_config:rw` + `config:ro` with a single
  `all_addon_configs:rw` directive. The container now sees
  `/addon_configs/` as a real bind mount of the supervisor's
  addon-configs root (`/mnt/data/supervisor/addon_configs/`), which
  means `/addon_configs/casa-agent/` is finally a persistent
  per-addon subdir surviving container rebuilds.
- **First-boot reseed:** because the underlying mount source changes
  from rootfs-overlay to bind mount, the existing `/addon_configs/casa-agent/`
  contents are NOT migrated. `setup-configs.sh` re-seeds defaults on
  first boot of v0.19.0 (per its existing `[ ! -d "$dst" ]`
  idempotency gate). User-edited configs from prior versions (such as
  `runtime.yaml::enabled: true` flags, custom `character.yaml` traits,
  custom plugin installs, custom marketplace overlays, and the entire
  in-tree git history under `/addon_configs/casa-agent/.git/`) WILL
  be lost. Any post-v0.19.0 customizations made through the
  configurator engagement path or by manual SSH edits will persist.

### Removed

- **`casa-agent/apparmor.txt`** — removed the dead `/config/** r,`
  rule. Casa code has zero references to `/config/` (verified by
  grep across `casa-agent/rootfs/`). The rule existed only to
  service the dropped `config:ro` mount.

### Added

- **`casa-agent/apparmor.txt`** — added `/addon_configs/ r,` rule.
  Defensive: under the new bind mount, the parent dir
  `/addon_configs/` is a real mount point and `setup-configs.sh`'s
  `mkdir -p` calls need read access to stat it. The existing
  `/addon_configs/casa-agent/** rwk,` rule does not cover the parent.

### Verification

Live-N150 smoke (post-deploy):

1. `mount | grep addon_config` → expect a line of the shape
   `/dev/<X> on /addon_configs type <fstype>` (or a `bind` flag if
   docker-info verbose). Pre-fix this returned nothing.
2. Boot logs immediately after first `ha apps update` to v0.19.0 →
   expect six `Seeded agent dir: <name>` lines (assistant, butler,
   finance, configurator, hello-driver, plugin-developer) plus
   `Initialized config git repo at /addon_configs/casa-agent` — proof
   the seed path fired against an empty mount.
3. Restart twice (`ha apps restart`); on the second boot, the seed
   lines must NOT reappear (`[ ! -d "$dst" ]` is now true → no-op).
   Pre-fix every restart re-seeded; post-fix only the first does.
4. The user-edited `runtime.yaml::enabled: true` flag for finance
   that was set on 2026-04-29 morning is GONE — must be re-set via
   the configurator engagement path (or manual edit) post-deploy.
   This is expected per the BREAKING note above.

### Out of scope

This is Phase 0 of the bugfix roadmap. Phases 1-6 are tracked in
`docs/bug-review-2026-04-29-exploration.md` § "Suggested bugfix-roadmap shape".

### Memory hooks

After verification, add a memory entry summarizing the fix-shape
choice (Option B `all_addon_configs:rw` over Option A `/config`
repoint — see plan-doc rationale) and the live-deploy result. The
memory entry `reference_v0_18_1_addon_config_fixes` is now stale
(it referenced SHAs not present in master tip `04037d0`); revisit
it post-ship to either correct or remove.

## [0.18.2] - 2026-04-29 — Engagement setup_engagement_features() ordering fix

**Latent bug since v0.11.0 surfaced by v0.18.1.** Once `TELEGRAM_ENGAGEMENT_SUPERGROUP_ID` started actually reaching `TelegramChannel.__init__` (v0.18.1 fix), `setup_engagement_features()` ran the bot-permission check at startup — but `self._app` was still `None` because `channel_manager.start_all()` hadn't fired yet. The probe failed with `'NoneType' object has no attribute 'get_me'`, leaving `engagement_permission_ok = False` permanently. Every `engage_executor` / `delegate_to_agent(mode="interactive")` then returned the misleading "set telegram_engagement_supergroup_id in addon" error.

### Fixed

- **`casa_core.py`** — `telegram_channel.setup_engagement_features()` is now called AFTER `channel_manager.start_all()`, not immediately after `register()`. The bot isn't built until `_rebuild()` runs inside `start_all()`. The deferred call is wrapped in try/except + ERROR-log to avoid blocking startup if the supergroup probe fails for an unrelated reason (e.g., Telegram API outage).

This was latent for ~7 months because v0.18.0 and earlier never actually exported `TELEGRAM_ENGAGEMENT_SUPERGROUP_ID` to the env (v0.11.0 schema-write regression that v0.18.1 fixed). Operators who set the option still hit the no-op early-return at `setup_engagement_features` line 634; the bug only manifests once the env var actually reaches `TelegramChannel.__init__`.

## [0.18.1] - 2026-04-29 — Engagement supergroup env-export fix + log_level option

**Two operator-facing fixes discovered during M6 (v0.18.0) deploy verification.**

### Fixed

- **`telegram_engagement_supergroup_id` no longer ignored at runtime.**
  The `s6-overlay/s6-rc.d/svc-casa/run` script exported 4 of the 5
  `telegram_*` config options to env vars but missed
  `TELEGRAM_ENGAGEMENT_SUPERGROUP_ID`. This caused
  `casa_core.py:1028` to read the empty env (default `"0"`),
  parse to `0`, and pass `engagement_supergroup_id=None` to
  `TelegramChannel`. Every engagement-tool call (`engage_executor`,
  `delegate_to_agent` with `mode="interactive"`) returned the
  "set telegram_engagement_supergroup_id in addon" error even when
  the option was correctly set in the addon configuration.
  Regression dates from v0.11.0 (engagement primitive ship); silent
  for ~7 months because the manual Telegram smoke probe in v0.11.0
  was performed by an operator who had also not yet set the option.
  Regression test in `tests/test_run_script_env.py` parameterizes
  every TELEGRAM_* env var the run script must export.

### Added

- **Operator-facing `log_level` addon option.** `list(debug|info|warning|error)?`
  with INFO default. Wired through `svc-casa/run` (null-normalized
  like `casa_tz` / `scope_threshold`) → `LOG_LEVEL` env var →
  `casa_core.py::install_logging(level=...)`. Operators can now flip
  to DEBUG via the HA UI without rebuilding the image.

## [0.18.0] - 2026-04-29 — Memory M6: cross-role recall

**Adds `consult_other_agent_memory(role, query)` — a read-only MCP
tool that lets a resident query another agent's accumulated
theory-of-mind of the user without delegating a full agent turn.**

### Added

- **`MemoryProvider.cross_peer_context`** — 4th method on the ABC
  at `memory.py`. `HonchoMemoryProvider` wraps Honcho v3's
  `peer.context(target=user_peer, search_query=query)` primitive;
  `NoOpMemory` and `SqliteMemoryProvider` return `""` per the
  graceful-degradation contract; `CachedMemoryProvider` is
  passthrough.
- **`_render_peer_context` helper** — renders `peer.context()`'s
  `peer_card` + `representation` shape under
  `## What {Observer} knows about you (cross-role)`.
- **`consult_other_agent_memory` MCP tool** — registered in
  `tools.py` and exposed via `CASA_TOOLS`. Validates role against
  the resident/specialist registry; structured-error strings on
  bad input.
- **`memory.cross_peer_token_budget`** — new field on resident
  `runtime.yaml::memory` (default 2000 when unset). JSON-schema
  additive update on `runtime.v1.json`.
- **System prompt — Ellen's `prompts/system.md`** — new
  "Cross-role memory recall" section teaches Case 1 (recall →
  this tool) vs Case 2 (factual lookup → `delegate_to_agent`).
- **Configurator doctrine** — `recipes/resident/create.md` and
  `recipes/resident/update.md` updated to teach the new tool +
  `cross_peer_token_budget` field.
- **`memory_call` telemetry** — new `call_type: "self" | "cross_peer"`
  field across all emission sites. New tool-side
  `consult_other_agent_memory_call` info line with role / query_len /
  result_len / t_ms.

### Changed

- **`MemoryProvider` ABC** — bumped from 3 methods to 4. Pre-1.0.0
  license invoked, no backward-compat shim. Out-of-tree providers
  (none today) would break loudly at import.
- **Ellen (`assistant/runtime.yaml::tools.allowed`)** — gains
  `mcp__casa-framework__consult_other_agent_memory`.
- **`memory_call` log line** — adds `call_type` field across 5
  emission sites in the same commit per arch-spec § 13 drift-risk
  warning.

### Trust posture

- Tina (`butler`) and Finance (specialist) ship WITHOUT the tool —
  guards Tina's guest-accessible voice channel and keeps
  specialist-to-specialist consultation as an operator-opt-in
  decision via Configurator. Regression tests at
  `tests/test_agent_loader.py` guard the omissions structurally.

### Spec / plan

- `docs/superpowers/specs/2026-04-29-memory-m6-cross-role-recall-design.md`
- `docs/superpowers/plans/2026-04-29-memory-m6-cross-role-recall.md`
- Live arch spec § 16: `docs/superpowers/specs/2026-04-26-memory-architecture.md`

## [0.17.2] - 2026-04-28 — Scheduled trigger silence (F1 follow-up)

**Fixes the v0.17.1 regression where every scheduled trigger fire
raised `ValueError` at session-id construction, plus the
longer-standing leak where Ellen's heartbeat emitted
acknowledgement-style first tokens into Telegram before her
silence-check completed.**

### Fixed

- **`trigger_registry.py:117`** — scheduled-trigger `chat_id` now
  hyphenates `{trig.type}-{trig.name}` instead of colon-joining, so
  `build_session_key` + `honcho_session_id` accept it. Eliminates the
  hourly `ERROR Agent 'Ellen' error [unknown]: part 1='interval:heartbeat'
  contains characters outside [A-Za-z0-9_-]` log line.

### Changed

- **`agent.py` `handle_message`** — `MessageType.SCHEDULED` turns no
  longer receive a `create_on_token` streaming callback. The agent
  thinks privately; only the final text is delivered. Other message
  types (`REQUEST`, `NOTIFICATION`, `RESPONSE`, `CHANNEL_IN`) are
  untouched.
- **`agent.py` `handle_message`** — sentinel-based silence gate for
  `SCHEDULED`: when the model returns `<silent/>` (exact match after
  `strip()`) or whitespace-only output, the send path is skipped and
  no `RESPONSE` BusMessage is emitted.
- **`defaults/agents/assistant/triggers.yaml`** — heartbeat prompt
  replaces the obsolete streaming warning with the
  `<silent/>` sentinel contract. Override rules and closing
  instructions unchanged.

### Tests

- `tests/test_trigger_registry.py::TestInterval::test_interval_chat_id_is_honcho_compliant`
  — roundtrip assertion that producer (trigger_registry) and validator
  (`honcho_session_id`) agree on shape.
- `tests/test_agent_process.py::TestScheduledSilence` (5 tests) —
  `create_on_token` count for SCHEDULED vs REQUEST, sentinel
  suppression, whitespace suppression, real-text passthrough.

### Not changed

- No deprecation shim for the colon-shaped `chat_id` (pre-1.0 license,
  per `feedback_pre_1_0_0_license`).
- No silent server-side sanitization in `honcho_session_id` — the
  v0.17.1 fail-fast doctrine stands.
- `morning-briefing.md` — sentinel is opt-in; prompts that always
  send simply never emit `<silent/>`.
- Voice channel user-supplied `scope_id` validation is followup, not
  blocker (see spec §7).

## [0.17.1] - 2026-04-28 — Honcho session-id format fix (F1)

**Fixes the 11-day silent Honcho-write bug discovered post-M4b deploy.**
Every Casa Honcho session-create has 422'd since v0.2.2 (2026-04-17)
because session ids contained `:`, which Honcho's server-side
`^[A-Za-z0-9_-]+$` regex rejects. Reads returned empty digests; writes
were dropped. Failures landed in `try/except → WARNING` so the bug
remained invisible until M4b's `peer_count: 0` telemetry pattern was
finally read as "writes never landed" rather than "fresh sessions".

### Added

- **`casa-agent/rootfs/opt/casa/honcho_ids.py`** — single canonical
  builder `honcho_session_id(*parts)`. Joins parts with `-` (hyphen),
  fail-fasts (`ValueError`) on inputs containing characters outside
  `[A-Za-z0-9_-]`. Strict-reject by design — silent sanitization is
  what blinded us for 11 days.
- **Regression integration test** in `tests/test_honcho_ids.py`
  asserting that the pre-fix colon shape WOULD have tripped Honcho's
  `string_pattern_mismatch` validator.

### Changed

- **All 11 Honcho session-id construction sites** flipped to call
  `honcho_session_id` instead of f-string concatenation:
  - `agent.py:332,552` (resident read/write)
  - `channels/voice/channel.py:454` (voice prewarm)
  - `tools.py:377` (coordinator meta write)
  - `tools.py:439` (M4b specialist)
  - `tools.py:1009` (executor archive read)
  - `tools.py:1326,1330` (engagement-finalize meta)
  - `tools.py:1379` (executor archive write)
  - `tools.py:1553` (query_engager)
- **`session_registry.build_session_key`** rewired through
  `honcho_session_id`. Output shape flipped from `{channel}:{scope_id}`
  to `{channel}-{scope_id}`. Now also accepts `int` `scope_id`
  (Telegram `chat_id`).
- **`session_sweeper`** partitions registry keys on `-` (was `:`).
  Pre-existing colon-shaped JSON entries fall through to the 30-day
  session TTL and age out — no migration shim per pre-1.0.0 license
  (zero data was ever persisted under the old shape; every server
  create 422'd since v0.2.2).

### Breaking

- **Channel-key on-disk format** (`{DATA_DIR}/sessions.json`) flipped
  from `{channel}:{scope_id}` to `{channel}-{scope_id}`. Pre-v0.17.1
  entries become orphans and age out via TTL — no operator action.
- **`build_session_key`** now rejects `scope_id` containing `:`,
  whitespace, or any character outside `[A-Za-z0-9_-]`. Previously
  preserved colons verbatim.

### Spec / doctrine

- `docs/superpowers/specs/2026-04-28-honcho-session-id-format-design.md`
  (new — design rationale, decision log, migration table)
- `docs/superpowers/plans/2026-04-28-honcho-session-id-format-fix.md`
  (new — task-by-task implementation plan)
- `docs/superpowers/specs/2026-04-26-memory-architecture.md` § 5/§ 14/§ 15
  swept to hyphen shape
- Configurator doctrine (`architecture.md`,
  `recipes/specialist/create.md`) swept

## [0.17.0] - 2026-04-28 — Memory M4b: Specialists become memory-bearing

Specialists (Tier 2 — Finance today; future Health/Personal/Business)
gain per-`(role, user_peer)` Honcho memory. One channel-agnostic,
scope-agnostic session per specialist accumulates messages, summary,
and `peer_representation` across all delegate-call channels.

### Added

- **Specialist memory read+write in `_run_delegated_agent`.**
  When `cfg.memory.token_budget > 0` and a memory provider is bound,
  `tools.py:_run_delegated_agent` opens a Honcho session keyed
  `f"{role}:{user_peer}"` (e.g. `finance:nicola`), fetches a digest
  via `get_context(search_query=task_text, tokens=…)`, and prepends
  a `<memory_context agent="{role}">…</memory_context>` block between
  `<delegation_context>` and `Task:`. After the SDK returns text, a
  background task writes the turn back via `add_turn(user_text=task_text,
  assistant_text=…)`. Failures fail-soft (WARNING log, no propagation).
- **`_specialist_meta_write_bg` — coordinator visibility.** Each
  `delegate_to_agent` call also writes a one-line summary to the
  parent's meta session (`{channel}:{chat_id}:meta:{parent_role}`),
  giving Ellen a unified view of specialist activity independent of
  which scope her own per-turn argmax write went to. Task and reply
  truncated to 200 chars per side.
- **Finance opted in by default.** `defaults/agents/specialists/finance/runtime.yaml`
  bumps `memory.token_budget` from 0 to 4000.

### Breaking

Pre-1.0.0 license per `feedback_pre_1_0_0_license.md`:

- `specialist_registry._validate_tier2_shape` no longer rejects
  specialists with `token_budget > 0`. Operators with stateless
  specialists (`token_budget: 0`) are unaffected; operators who set
  `token_budget > 0` will now see Honcho memory engaged.

### Internal additive (non-breaking)

- New module-level helpers in `tools.py`:
  - `_specialist_bg_tasks: set[asyncio.Task]` — GC anchor.
  - `_specialist_add_turn_bg(...)` — fail-soft background writer.
  - `_specialist_meta_write_bg(...)` — fail-soft meta-summary writer.
- `_build_specialist_options` docstring updated; SDK `resume=None`
  unchanged (memory enters via prompt injection, not SDK continuity).

### Architecture

- New 2-segment session id shape `f"{role}:{user_peer}"` joins the
  existing 4-segment `{channel}:{chat_id}:{scope}:{role}` topology.
  Specialists are channel-agnostic and scope-agnostic; both shapes
  are first-class to Honcho (sessions are id-opaque).
- Trust gating stays one level up at the resident's `delegates`
  decision — no per-call channel filter at the memory layer.

### Doctrine + spec

- **Configurator doctrine sync** (per
  `feedback_configurator_doctrine_sync.md`):
  - `recipes/specialist/create.md` — memory-bearing specialist example.
  - `recipes/specialist/update.md` — enable-memory recipe for an
    existing stateless specialist.
  - `architecture.md` — specialist memory subsection + correction to
    the v0.16.0 "stateless specialists" claim.
- **Live arch spec.** `docs/superpowers/specs/2026-04-26-memory-architecture.md`
  § 5 gains a 2-segment specialist-sessions paragraph, plus new § 15
  documenting the read path, write path, meta-scope coordinator
  visibility, and what's deferred to M5/M6.

### Deferred

- Specialist `peer_card` writes / `remember_fact` MCP tool → **M5**.
- Cross-specialist recall via `peer_perspective` → **M6**.
- `read_strategy: cached` for specialists.
- Multi-user (`user_peer != "nicola"`).

## [0.16.0] - 2026-04-27 — Memory M4: Engagement memory

Three layers, one user-visible behavior: engagement summaries flow back
into Ellen's per-turn memory and Configurator engages with prior
context.

### Added

- **L1 — `meta` declared as a system scope.** `policies/scopes.yaml`
  bumps to schema v2 with a new `kind: topical | system` field. System
  scopes are always-on after the trust filter — no embedding, no
  classifier routing. `meta` is the first system scope; assistant adds
  it to `scopes_readable`. Voice (Tina) is excluded by the
  `authenticated` trust gate.
- **L3 — Per-executor archive read at engage-start.** New
  `ExecutorMemoryConfig(enabled, token_budget)` on
  `ExecutorDefinition`; Configurator opts in. `engage_executor`
  interpolates a new `{executor_memory}` prompt slot from the
  per-(channel, chat, executor_type) Honcho session. `claude_code`
  driver-side `workspace.py` slot supported for forward-compat with
  future memory-enabled claude_code executors.
- **L4 — Free benefit.** `_finalize_engagement` already writes
  engagement summaries to the meta session for both specialist and
  executor engagements (since M2.G4, v0.15.3). L1 makes them readable
  on Ellen's normal turn. No new write code.

### Breaking

Pre-1.0.0 license per `feedback_pre_1_0_0_license.md`:

- `policies/scopes.yaml` schema bumped v1 → v2. No migration shim.
  Tenant overlays at `/addon_configs/casa-agent/policies/scopes.yaml`
  must be updated by the operator on upgrade.
- `defaults/schema/policy-scopes.v1.json` removed; replaced by
  `policy-scopes.v2.json`.

### Internal additive (non-breaking)

- `executor.v1.json` gains optional `memory` property; existing
  executor `definition.yaml` files without a `memory:` block remain
  valid (default disabled).

### Doctrine + spec

- **Configurator doctrine sync** (per
  `feedback_configurator_doctrine_sync.md`): `architecture.md`,
  `recipes/scopes/edit.md` (also fixes pre-existing list-style format
  drift), `recipes/executor/scaffold.md`,
  `recipes/resident/{create,update}.md` updated in the same commit
  set.
- **Architecture spec**
  (`docs/superpowers/specs/2026-04-26-memory-architecture.md`): § 5,
  § 6, § 11 updated; new § 14 "Engagement memory"; § 12 M4 entry
  flipped to "Shipped v0.16.0".

### Deferred to future ships

- L2 — Specialists become memory-bearing → M4b (separate brainstorm).
- Synthesized "lessons learned" archive content → future tweak after
  real archive usage patterns emerge.
- `remember_fact` via directional `peer_card` → M5.
- Cross-role recall (`consult_other_agent_memory`) → M6.
- `HONCHO_LIVE_TEST=1`-gated integration test → M3a.1 follow-up
  bundle.

## [0.15.4] - 2026-04-27 — Memory M3: Honcho contract coverage + `memory_call` telemetry

Observability + confidence-coverage release. No runtime-behaviour
changes; closes the M2-era spec § 9 "real-Honcho-response coverage
not in tests today" gap and adds per-memory-call telemetry.

### Added
- **M3a — Honcho populated-response integration test.**
  `tests/test_memory_honcho.py::test_get_context_renders_summary_and_peer_repr_when_honcho_returns_them`
  primes the SDK stub with populated `summary.content` +
  `peer_representation` + `peer_card` + recent `messages` and asserts
  all four `_render` sections appear in canonical order. Closes the
  spec § 9 wiring-coverage gap. Live `HONCHO_LIVE_TEST=1`-gated test
  deferred as M3a.1 follow-up.
- **M3b — `memory_call` info-level log line.** Emitted from each
  concrete provider's `get_context` (Honcho + SQLite) and from
  `CachedMemoryProvider`'s cache-hit branch. Fields: `backend`,
  `session_id`, `agent_role`, `t_ms`, `peer_count`,
  `summary_present`, `peer_repr_present`, `cache_hit`. NoOp provider
  intentionally silent — see new spec § 13 for the full contract.
- **Spec § 13** (`docs/superpowers/specs/2026-04-26-memory-architecture.md`)
  documents the `memory_call` field set and emission rules.

### Migration
- None. M3 adds log lines and tests; no schema, config, or runtime
  contract change. Operators relying on a regex-style log scrape that
  asserted "no `memory_call` lines exist" would need to update —
  vanishingly unlikely.

## [0.15.3] - 2026-04-26 — Memory M1+M2: spec consolidation + Honcho-side fixes

First user-visible memory ship since v0.8.4. Folds the internal-only M1
cleanup (no version bump at the time) into the same release as M2's
three Honcho-touching bug fixes.

### Added (M1)
- `docs/superpowers/specs/2026-04-26-memory-architecture.md` —
  consolidated current-state spec for the memory subsystem. Supersedes
  2.2a/2.2b/3.2/3.2.1/3.2.2 design specs for "what is true today"
  purposes.

### Removed (M1)
- `card_only` read strategy. Reserved in 2.2a, never implemented; the
  branch in `_wrap_memory_for_strategy` warned and fell back to
  `per_turn`. No default YAML used it.
- SQLite-side `peer_cards` table + reader. No code ever wrote to it;
  the deferred `remember_fact` tool stays a Honcho-only feature per the
  graceful-degradation doctrine.
- `archive_session_full` executor field. Parsed and stored but no
  reader. Plan 4a transcript archival fires unconditionally on
  `kind=executor`.

### Fixed (M2)
- **Voice prewarm cache key restored.**
  `channels/voice/channel.py::_prewarm` was building the pre-3.2
  3-segment session id `voice:{scope_id}:{role}`. The agent's read
  path uses 4 segments `{channel}:{chat_id}:{scope}:{role}`, so the
  prewarm cache key never matched the real-turn key — every wake-word
  paid the full cold-read latency. Now loops over the agent's
  `scopes_readable` and warms one entry per scope using the 4-segment
  shape with budget // len(scopes) tokens each.
- **Cancel + force-delete now write engagement summaries.**
  `cancel_engagement` and `delete_engagement_workspace(force=True)`
  passed `memory_provider=None` to `_finalize_engagement`, silently
  skipping the meta-scope summary write and the per-executor-type
  Honcho archival. Both sites now resolve `active_memory_provider`
  from the `agent` module the same way `emit_completion` does.
  Cancellations and force-deletes leave the same Honcho trace as
  normal completions.
- **`query_engager` reads from the engager's actual scope.**
  `tools.py:1357` reads `engagement.origin.get("scope", "meta")`;
  `agent.py` never set `"scope"`, so Tina's `query_engager("what did
  the user say…")` always retrieved from Ellen's meta scope — which
  only contains engagement summaries, never user conversation. The
  agent now stamps `argmax_scope(scores, default_scope)` onto
  `origin_var` after the read-path classifier runs, so engagements
  spawned during a turn carry the scope the turn was rooted in.

### Migration
- M1 migration notes still apply: existing SQLite databases keep their
  now-orphan `peer_cards` table (harmless, no longer read); existing
  `definition.yaml` files with `archive_session_full: ...` will fail
  schema validation — delete the line.
- M2: no migration. Voice prewarm change is transparent (cache hits
  start working again). Cancel-path memory writes are additive (Honcho
  gets entries it was missing). G6 stamps a new `scope` key onto
  `engagement.origin` — code reading `origin` with `.get(..., default)`
  is unaffected; any code doing exact-equality dict comparison would
  need updating but no such site exists.

## [0.15.2] - 2026-04-26 — Heartbeat noise + sweeper crash

Two production bugs visible in `addon_c071ea9c_casa-agent` logs.

`engagement_idle_sweep` (cron 08:00 daily) and `workspace_sweep`
(interval 6h) were registered as `lambda: asyncio.create_task(...)`
in `casa_core.py`. APScheduler's `AsyncIOExecutor` runs sync callables
in a worker thread, so `asyncio.create_task` raised
`RuntimeError: no running event loop` on every fire — silently no-op
since v0.13.0. Fix: pass the coroutine functions directly with
`kwargs={...}`; AsyncIOExecutor schedules them on the loop natively
(same pattern `trigger_registry._register_scheduled` already uses).

Ellen's `heartbeat` trigger fires every 60min and was producing
chatty "checking in" messages despite the prompt's "stay quiet"
instruction. The Telegram channel runs in `stream` mode — the *first
token* posts a new chat message, so any preamble Ellen drafts before
deciding to stay silent has already gone out. Rewrite the prompt:
silence is now framed as the default action, the bar for sending is
explicit and narrow, and a "no preamble, no reflection text" rule
forbids the first-token leak.

### Fixed

- `casa_core.py:1506,1519` — `engagement_idle_sweep` and
  `workspace_sweep` jobs now register the coroutine function
  directly. Adds `tests/test_scheduled_sweeper_jobs.py` to lock
  the wiring (would have caught this since v0.13.0).
- `defaults/agents/assistant/triggers.yaml` heartbeat prompt
  rewritten — silence-first framing, explicit "what NOT to send"
  list, no-preamble rule.

## [0.15.1] - 2026-04-26 — Tina HA control

Tina (butler) becomes the universal Home Assistant operator. Server-level
grant to the homeassistant MCP gives her every Assist tool the user has
exposed; new prompt sections teach her how to use them; Ellen's
delegates.yaml gains a butler entry so the Telegram-via-Ellen path
("ask Tina to turn off the lights") works end-to-end. Closes the
v0.15.0 deferred manual smoke.

### New

- `mcp__homeassistant` server-level grant in `defaults/agents/butler/runtime.yaml` —
  every HA Assist tool callable from Tina, present and future, no
  enumeration required.
- Three new prompt sections in `defaults/agents/butler/prompts/system.md`:
  `## Home Assistant tools`, `## Intent patterns`, `## Error recovery`.
- `butler` entry in `defaults/agents/assistant/delegates.yaml` so Ellen's
  `<delegates>` block advertises Tina and `delegate_to_agent("butler", ...)`
  passes the role-map gate.
- `CASA_HA_MCP_URL` env override on `casa_core.py` — defaults to
  `http://supervisor/core/api/mcp`. Used by e2e to point HA traffic at
  the mock.
- Mock HA MCP server at `test-local/e2e/mock_ha_mcp/server.py` — minimal
  JSON-RPC 2.0 implementation with `HassTurnOn`/`HassTurnOff`/
  `GetLiveContext` and `/_calls`/`/_reset` test side-channels; rejects
  unknown tool names with `-32602`.
- Mock SDK file-driven HTTP MCP tool-invoke hook
  (`MOCK_SDK_TOOL_INVOKE_FILE`) — lets tier-2 e2e exercise the
  resident-options → SDK → HTTP MCP transport chain without a live model.
- Tier-2 e2e `test-local/e2e/test_ha_delegation.sh` — H-0..H-3 covering
  the CASA_HA_MCP_URL flow, the voice-direct path, and the
  agent_loader → SDK options chain.
- Configurator doctrine recipe `recipes/resident/grant_ha_tools.md`.

### Notes

- HA integration must be enabled and entities exposed to default Assist
  pipeline by the user — Casa cannot configure these.
- "Trust the model fully" decision recorded in spec §6 — no per-tool /
  per-domain restrictions. Safety guardrails (irreversible actions
  behind confirmation read-back) tracked as future roadmap item.
- Tier-2 e2e exercises butler→HA directly via the mock-SDK hook;
  the Ellen→delegate_to_agent→butler two-hop chain stays covered by the
  J.5 manual smoke (live SDK on N150).

## [0.15.0] - 2026-04-25 — Resident-to-resident delegation

Residents can now delegate to other residents and specialists by role
via the new `delegate_to_agent` MCP tool. Lifts the previous "Ellen is
the only delegator" architectural constraint.

### New

- `delegate_to_agent(agent=<role>, task=, context=, mode={sync,async,interactive})` —
  unified delegation tool. Resolves `agent` against a merged role map of
  residents + specialists. `mode=interactive` is rejected for residents.
- `<delegates>` and `<executors>` system-prompt blocks rendered at turn
  time from each resident's `delegates.yaml` / `executors.yaml`. Closes
  the long-standing dead-data bug where `cfg.delegates` was loaded but
  never reached the model.
- `<delegation_context>` block prepended to delegated calls so target
  agents can adapt voice/text register.
- New `executors.yaml` (assistant-only) — `configurator`,
  `plugin-developer`, and `engagement` entries moved out of
  `delegates.yaml`.
- `agent_registry` module: name↔role bidirectional map for prompt
  rendering and future code paths.

### Breaking (no back-compat alias; pre-1.0.0)

- `delegate_to_specialist` removed; replace with `delegate_to_agent`.
- `mcp__casa-framework__delegate_to_specialist` removed from
  `runtime.yaml::tools.allowed` allowlists; replace with
  `…delegate_to_agent`.
- Configurator doctrine updated; recipes wire/unwire generalized.

### Behavioral

- Single-hop depth cap: a delegated agent cannot itself call
  `delegate_to_agent` (returns `delegation_depth_exceeded`). Trivially
  relaxable via the `_MAX_DELEGATION_DEPTH` constant in `tools.py`.

### Out of scope (separate specs)

- HA-control plugin / Tina's tool inventory.
- Cross-channel sending.
- Multi-hop chaining.

## [0.14.12] - 2026-04-25

Log-noise sweep — four fixes surfaced by a live N150 log audit
(2026-04-25). All changes target log signal/noise; no behavior shifts
beyond the heartbeat-delivery one called out below.

### Fixed

- **Telegram channel**: `chat_id` validation. The `context["chat_id"]`
  slot is overloaded — user-initiated messages carry a numeric Telegram
  chat id, but scheduled triggers carry session-keying labels like
  `"interval:heartbeat"`. The Telegram API rejects non-numeric values
  with `BadRequest: Chat not found`, which used to bubble through
  `finalize_stream → send` and surface as a full traceback at the bus
  dispatcher. New `_resolve_chat_id` helper falls back to the channel's
  registered default when the value isn't numeric. **Behavioral note:**
  hourly heartbeats now actually deliver to the registered chat instead
  of silently failing — if the agent prompt's "stay quiet" instruction
  isn't honored, the user will see hourly pings. Tune the prompt if so.
- **CC CLI transcript persistence**: `setup-configs.sh` now symlinks
  `/root/.claude/projects` to `/addon_configs/casa-agent/cc-home/.claude/projects`
  on boot. The bundled CC CLI uses `$HOME=/root → ~/.claude/projects/`,
  but `/root/` is wiped on every container rebuild, so the SDK's
  `--resume <sid>` path failed on every first turn after a deploy
  (visible as `claude_agent_sdk._internal.query: Fatal error in message
  reader` for `voice:probe-scope` and `telegram:interval:heartbeat`).
  One-time migration on first boot copies any pre-existing transcripts
  into the persistent location before replacing the dir with a symlink.
- **Empty `s6-rc-compile` at boot**: `replay_undergoing_engagements`
  used to call `_compile_and_update_locked()` unconditionally, which
  printed `source /data/casa-s6-services is empty` to stderr at every
  boot when no claude_code engagements were active. Now early-returns
  when both `undergoing` and `removed_orphans` are empty — the
  engagement sources dir is unchanged, so a compile would be wasted.
- **`svc-nginx/finish` and `svc-ttyd/finish`**: gate the `bashio::log.warning`
  on exit codes 0 and 256, mirroring the existing pattern in
  `svc-casa-mcp/finish`. Code 0 = clean stop (s6 told it to); code 256
  = s6 "do-not-restart" sentinel. Anything else still surfaces.

### Files

- `casa-agent/rootfs/opt/casa/channels/telegram.py` — `_resolve_chat_id`
  helper + 3 call-site updates (`send`, `create_on_token`, `finalize_stream`).
- `casa-agent/rootfs/etc/s6-overlay/scripts/setup-configs.sh` — projects
  dir symlink with first-boot migration.
- `casa-agent/rootfs/opt/casa/casa_core.py` — `replay_undergoing_engagements`
  fast path.
- `casa-agent/rootfs/etc/s6-overlay/s6-rc.d/svc-nginx/finish` and
  `.../svc-ttyd/finish` — exit-code gate.

## [0.14.11] - 2026-04-25

Test tiering — Half 1. Re-groups existing CI tests into a three-tier
structure so trivial PRs get sub-2-minute "is the system on fire"
feedback while hardening (timing/chaos) tests run nightly + on-demand.
No runtime / addon code changes; CI plumbing + test-file rearrangement
only.

### CI

- **`.github/workflows/qa.yml`** rewritten as `tier1-smoke` (every push
  + PR + nightly + manual, ~7-8 min cold-cache) / `tier2-functional`
  (push + PR + manual, ~12 min, parallel with tier 1) /
  `baseline-runtime` (unchanged, parallel with tier 2) /
  `tier3-hardening` (nightly + manual only). Tier 1 has no `needs:`
  gating against tier 2 — contributors get fail-fast smoke signal in
  parallel with the full functional sweep.
- **Nightly cron** at 04:00 UTC. Nightly skips tier 2 (already verified
  on the master push that landed the changes); runs tier 1 + tier 3.
- **Manual `workflow_dispatch`** runs all three tiers from any branch.
- **D-block + P-block CI steps** stay commented out, deferred to the
  pre-existing v0.14.10 D/P-block sweep follow-up. Their split scripts
  exist (so the sweep can re-enable them by uncommenting one block) but
  do not run in CI today.

### Tests

- **`test-local/e2e/test_engagement.sh` split into 3 files**
  (1859 lines → 3 self-contained scripts):
  - `test_engagement_E.sh` (~944 lines): E-0..E-10 Tier-2 specialist +
    Configurator. Tier 2.
  - `test_engagement_D.sh`: D-1..D-12 claude_code driver lifecycle.
    Tier 3. Requires `CASA_USE_MOCK_CLAUDE=1`; skips cleanly otherwise.
  - `test_engagement_P.sh`: P-1..P-9 plugin-developer harness. Tier 2.
    Requires `CASA_USE_MOCK_CLAUDE=1` + `CASA_PLAN_4B=1`; skips
    cleanly otherwise.
- **`start_mock_telegram_server` helper** added to
  `test-local/e2e/common.sh`; replaces the inline E-0 spawn block.
- **Checkpoint count** preserved across the split (sum of `^pass "`
  lines in the 3 new files = original + 1; the +1 is the new
  `pass "P-block container healthy"` boot line because P now boots its
  own container, where it previously reused D's).

### Removed

- **`test-local/e2e/test_migration.sh`** (57 lines) — asserted seeded
  YAML markers for behavior the v0.7.0+ pre-1.0 wipe-on-update doctrine
  explicitly does NOT do. Same fate v0.9.1 gave `test_heartbeat.sh`.
  `git log -- test-local/e2e/test_migration.sh` recovers it post-1.0
  if migrations are reintroduced.
- **`test-local/Makefile::test-migration`** target dropped.

### Build

- **`test-local/Makefile`** gains `test-tier1`, `test-tier2`,
  `test-tier3`, `test-all`; legacy `test`/`test-smoke`/`test-runtime`/
  `test-voice` targets retained.

## [0.14.10] - 2026-04-25

v0.14.9 follow-up: enable seeded plugins after seed-copy. The v0.14.9
seed-copy populates cc-home with 5 default plugins but they all carry
`enabled: false` (CC CLI's `--cache-dir`-mode install at build doesn't
auto-enable). The binding layer (`plugins_binding.py::build_sdk_plugins`)
filters out `enabled: false` entries — so engagements were getting
`plugins=[]` even though all 5 plugins were structurally present.
Live verification on N150 v0.14.9 caught this: `claude plugin list
--json` showed 5/5 plugins, all `enabled=False`.

### Fixed

- **`setup-configs.sh` seed-copy block** now runs `claude plugin enable
  <ref>` for each of the 5 default plugins after the cc-home seed-copy
  completes. Runtime `--scope user` enable persists the flag in
  cc-home's `installed_plugins.json` and is idempotent (returns clean
  on a no-op re-run because of the `|| true` and `>/dev/null 2>&1`).

- **`test-local/init-overrides/01-setup-configs.sh`** mirrors the same
  enable loop so the local e2e build matches production.

### Tests

- **`test_invoke_sessions.sh::C-4`** strengthened to assert `5/5`
  (total/enabled), not just `5` (total). Plain count-check would have
  passed on v0.14.9 even with all-disabled plugins; this catches the
  binding-layer's enabled-filter contract explicitly.

## [0.14.9] - 2026-04-25

Unified github access. Replaces the five distinct mechanisms across
build / boot / Configurator runtime / plugin-developer engagement with
one path: a system-level `/etc/gitconfig` (SSH→HTTPS rewrite + a
credential helper) plus `$GITHUB_TOKEN` propagated at addon-wide scope
via `/run/s6/container_environment/GITHUB_TOKEN`.

### Added

- **`/etc/gitconfig`** ships in the image. Contains an SSH→HTTPS
  insteadOf rewrite for github.com (no token) and a `credential.helper`
  pointing at `/opt/casa/scripts/git-credential-casa.sh`. Applies
  system-wide regardless of which user or HOME the process runs under.

- **`/opt/casa/scripts/git-credential-casa.sh`** — stateless POSIX shell
  helper. Reads `$GITHUB_TOKEN` from process env at request time, emits
  `username=x-access-token\npassword=...` on stdout. Token never
  written to any config file. Includes CR/LF strip on token to prevent
  malformed credential responses if `op read` emits trailing newlines.

### Changed

- **`setup-configs.sh`** resolves `op://${ONEPASSWORD_DEFAULT_VAULT}/GitHub/credential`
  at boot via `bashio::config` + `op read`, then writes the token to
  `/run/s6/container_environment/GITHUB_TOKEN` (mode 0600). s6-overlay
  merges this into every supervised service's environment, so casa-main,
  svc-casa-mcp, every engagement subprocess, and every `git`/`gh`/`claude
  plugin install` invocation inherits the same token automatically.

- **`setup-configs.sh`** seed-copies cc-home plugin state from the
  image-baked `/opt/claude-seed/` on first boot (idempotent — sentinel
  is `installed_plugins.json` in cc-home). Replaces the v0.14.8 boot
  install loop. Symlink-based — CC CLI tolerates `installPath` via
  symlink (verified by spike D.1 on N150). No network access required
  at boot for the 5 default plugins.

- **`Dockerfile`** pairs each `claude plugin install` with a
  `claude plugin enable` so the image-baked seed has `enabled: true`
  for all 5 default plugins. Without this, the seed-copy would
  preserve `enabled: false` from the build (CC CLI's `install` does
  not auto-enable).

### Removed

- **v0.14.8 boot install loop** in `setup-configs.sh` (the
  `claude plugin install <ref>` loop with `flock`-serialised stderr
  capture). Default-plugin install state now comes from the seed-copy.

- **`_resolve_plugin_developer_github_token`** in
  `drivers/claude_code_driver.py` and the per-engagement
  `extra_env["GITHUB_TOKEN"]` injection. The token is in the
  addon-wide environment and inherited automatically.

- **`Dockerfile`'s per-USER `git config --global url.X.insteadOf`**
  for the `casa` build user. The same rewrite ships in `/etc/gitconfig`,
  which applies to every USER.

### Tokenless mode

If `onepassword_service_account_token` or `onepassword_default_vault`
is unset/null, OR if `op read` fails for any reason, `GITHUB_TOKEN`
stays unset. Casa runs in **public-only mode**: all anonymous github
clones still work via `/etc/gitconfig`'s SSH→HTTPS rewrite; private-repo
clones return 404/403; plugin-developer's `gh repo create` fails (logged
at engagement scope). No secret material leaks anywhere.

### Notes

- Pre-1.0.0 wipe-on-update: no migration. On addon update,
  `/opt/claude-seed/` is rebuilt fresh in the new image; cc-home is
  refilled by the seed-copy on next boot.

- The unified path is verified by the v0.14.9 spike findings on N150
  (2026-04-25): all four spikes passed against the production
  `op://Casa/GitHub/credential` with the proposed credential-helper
  pattern.

## [0.14.8] - 2026-04-25

Boot-time fix — register the seed marketplace alongside the user
marketplace so default plugins actually install. Caught from the
N150 boot log: every default plugin in
`defaults/agents/**/plugins.yaml` was logging
`WARNING: plugin install skipped: <name>@casa-plugins-defaults`
and `claude plugin list --json` returned `[]`, so the binding
layer (`/opt/casa/plugins_binding.py::build_sdk_plugins`) handed
no plugins to engagements at all.

### Fixed

- **Seed marketplace was never registered with the CC CLI.**
  `setup-configs.sh` only ran
  `claude plugin marketplace add /addon_configs/casa-agent/marketplace/`
  (the user-writable overlay). The read-only seed at
  `/opt/casa/defaults/marketplace-defaults/` — which is where every
  `<name>@casa-plugins-defaults` install ref resolves — was missing
  from the install loop's environment, so all five default plugins
  (`document-skills`, `mcp-server-dev`, `plugin-dev`, `skill-creator`,
  `superpowers`) failed to install with
  `Plugin "<name>" not found in marketplace "casa-plugins-defaults"`.
  Added a second `claude plugin marketplace add` for the seed dir
  immediately before the install loop. Idempotent (`|| true` on
  re-register).

### Diagnostic

- **Surface the CC CLI's stderr in the install warning.** Replaced
  `>/dev/null 2>&1 || bashio::log.warning "plugin install skipped: $ref"`
  with `install_err=$(... 2>&1 >/dev/null) || bashio::log.warning
  "plugin install skipped: $ref — $install_err"`. Future install
  failures stay diagnosable instead of cryptic.

### CI clean-up surfaced by this ship

v0.14.1 was the first Plan 4b commit to enable CI jobs that hadn't
run before (`CASA_USE_MOCK_CLAUDE=1` D-block, `CASA_PLAN_4B=1` P-block).
Every Plan 4b master push since has been red in ways that were not
being tracked, because the unit job failed first and hid everything
downstream. Unblocking CI here surfaced four pre-existing bugs that
also ship fixed in this bump:

- **`drivers/s6_rc.py::service_pid` used the wrong `s6-svstat` flag.**
  `-u` prints the literal `true`/`false` up status; `-p` prints the
  supervised PID. The code asked for `-u` and parsed as `int()`, so
  `service_pid()` always returned `None` and
  `ClaudeCodeDriver.is_alive_async()` always reported every engagement
  as dead. Flipped to `-p`. Shipped since v0.13.0 (2026-04-23).

- **Mock SDK `ClaudeAgentOptions` missing `plugins=` field.**
  v0.14.1's binding-layer wiring in `agent.py` and `tools.py` passes
  `plugins=build_sdk_plugins(...)` into every SDK construction. The
  test-only mock dataclass had no such field, so every resident /
  specialist / executor turn raised `TypeError` on the mock, the SDK
  session id was never captured, and `/data/sessions.json` stayed
  empty — breaking the Invoke-sessions E2E. Added `plugins` to the
  mock dataclass. (Matches `reference_mock_sdk_drift` memory: v0.5.9
  precedent — new kwargs MUST be mirrored into the mock same commit.)

- **Py 3.11+ tarfile raises `AbsoluteLinkError` not "symlink".**
  `tests/test_system_requirements_installer_tarball.py::test_symlink_
  member_rejected` used `pytest.raises(UnsafeArchiveError,
  match="symlink")` but the message is wrapped from
  `tarfile.data_filter`'s "link to an absolute path". Broadened the
  regex to accept either phrasing. This is what turned master CI red
  on every Plan 4b commit; this is the fix.

- **D-block `s6-svstat -u` parse bugs in `test_engagement.sh`.** D-1,
  D-4 cancel, and D-13 restart survival all invoked `s6-svstat -u`
  and parsed stdout as `int`. D-1 / D-13 fixed (D-13 switched to
  `-p`; D-1 parses `"true"` as boolean). D-4 parses `"false"` for
  down.

### Known limitation — CI D/P block disabled for v0.14.8

Plan 4a D-block (`CASA_USE_MOCK_CLAUDE=1`) and Plan 4b P-block
(`CASA_PLAN_4B=1`) are **intentionally disabled** in `.github/workflows/
qa.yml` for this ship. They were authored without ever running on
Linux CI — D-2 alone surfaces a further JSONL-glob mismatch, and
D-3..D-8 / P-1..P-9 are unverified. Sweeping them properly exceeds
this ship's scope. Tracked for **v0.14.9 follow-up**: run D/P block
locally against real Linux s6/mock CLI behaviour, fix each harness,
re-enable the CI env vars in one go.

Plan 2 E-block (E-0..E-10) still runs in every qa.yml e2e-fast run,
which continues to verify engagement primitives end-to-end.

## [0.14.7] - 2026-04-25

Bug-review v0.14.6 follow-up — closes Bug 10, the only finding from
`docs/bug-review-2026-04-24.md` deferred from v0.14.6 because it
needed a locking design rather than a surgical patch.

### Reliability

- **Telegram `handle_update` topic-status race (Bug 10).** aiohttp
  dispatched each Telegram update as its own task, so a `/cancel`
  arriving alongside a regular turn could race: the regular turn
  passed `rec.status` while the cancel was mid-finalize, then routed
  to a driver that `_finalize_engagement` had just torn down (driver
  raised `DriverNotAliveError` or the turn landed on a closed topic).
  Fixed with a per-topic `asyncio.Lock` keyed by `message_thread_id`
  on `TelegramChannel._engagement_handler_locks`, mirroring the
  `in_casa_driver._locks: dict[id, Lock]` idiom. Updates landing on
  the same topic now serialise; different topics still run in
  parallel. Three new tests in
  `tests/test_telegram_engagement_routing.py::TestHandleUpdateConcurrencyRace`
  exercise the cancel-vs-turn race, the two-regular-turns drop-
  resistance case, and cross-topic parallelism (deadlock-detection).

### CHANGELOG cleanup

- Collapsed the inadvertent duplicate `## [0.14.6]` heading from
  commit `615eac1` into a single section; moved the `Removed` /
  `Migration` blocks below `Tests` so they sit in the same v0.14.6
  body as the other notes.

## [0.14.6] - 2026-04-25

Bug-review v0.14.6 — security and correctness sweep against findings
from `docs/bug-review-2026-04-24.md`. No new features; all changes
are surgical fixes with regression tests.

### Security

- **block_dangerous_bash regex bypass.** Replaced flat regex matcher
  with an argv-aware checker that splits on shell separators
  (`;`, `&&`, `||`, `|`, `&`), shlex-parses each piece, and recurses
  into `bash -c "..."` / `sh -c "..."`. Variants that previously
  bypassed the safety hook (`rm -r -f`, `rm --recursive --force`,
  `rm -rfv`, `rm -fR`, `/usr/bin/rm -rf`, `bash -c "rm -rf /"`,
  `; rm -rf /`) are now all blocked. Verified live on N150 v0.14.5
  before the fix.
- **Tarball zip-slip / symlink-escape (`system_requirements/tarball.py`).**
  `tarfile.extractall` and `zipfile.extractall` now validate every
  member up front: symlinks/hardlinks/devices/fifos refused, and any
  member whose resolved path leaves the extract dir is rejected. Uses
  the `data` filter on Python 3.11.4+, falls back to manual member
  validation on the production 3.11.2 runtime. Also: the `extract:`
  field is resolved-path-checked, and unsafe URL schemes (`file://`,
  `ftp://`, `jar:`) are refused before download.
- **Tarball `install_cmd` shell injection.** `install_cmd` is now an
  argv list (`list[str]`) only — `subprocess.run(..., shell=True)` is
  gone. Backwards-incompatible with any marketplace entry that supplied
  a shell string (the first-party manifest does not).
- **Workspace `extra_dirs` shell injection.** Each entry must be an
  absolute path with no shell-special characters
  (`; | & ` ` $ < > ' "` newline / null). Values are still
  `shlex.quote`'d at render time.
- **Workspace `extra_env` key injection.** Keys must match
  `^[A-Z_][A-Z0-9_]*$` (the same convention used by
  `plugin_env_conf.py`); a newline or `$(...)` in the key would
  otherwise escape the rendered `export` line.
- **`casa_reload` / `config_git_commit` defense in depth.** Both tools
  now verify the calling agent's role (`origin_var` for SDK path,
  `engagement_var.role_or_type` for engagement-bridge path) and refuse
  unless it's `configurator`. Pre-fix they relied solely on each
  agent's `runtime.yaml::tools.allowed`, which is a single point of
  failure if a permissive default sneaks into a new role.
- **Telegram `/cancel` and `/complete` are originator-only.** Pre-fix
  any user in the engagement supergroup could fire either command and
  terminate someone else's engagement. Bus context now carries
  `user_id`, propagates through `origin_var` → `engagement.origin`,
  and the slash-command handler refuses unless `from_user.id` matches.
  `/silent` stays open (local to the topic). Legacy engagements with
  no `user_id` in origin still work.

### Reliability

- **`emit_completion` idempotency.** Re-emitting completion (SDK retry
  / hook misfire) is a recognised no-op now: the second call returns
  `{"status": "acknowledged", "kind": "already_terminal"}` without
  re-NOTIFYing Ellen, re-closing the topic, or re-writing the
  meta-scope summary into Honcho.
- **`delete_engagement_workspace` covers `idle`.** The live-state
  guard previously checked only `"active"`; an idle engagement
  (SDK-suspended after 24h) had its s6 service still running, but a
  non-`force` delete still tore down the workspace under it. Now both
  `active` and `idle` require `force=true`.
- **`_tail_file` follows log rotation.** The s6-log 1 MB rotation of
  `/var/log/casa-engagement-<id>/current` no longer drops the new
  file's content. Tracks `st_ino`; on inode change resets `pos = 0`.
  Also resets if the file shrinks below `pos` (truncate-in-place).
- **`ClaudeCodeDriver.start` rolls back on failure.** If
  `provision_workspace`, `write_service_dir`, `_compile_and_update`,
  or `start_service` raises, the partial workspace + s6 service dir +
  s6-rc compile are best-effort cleaned up before the original
  exception is re-raised. No more orphan UNDERGOING ghosts that the
  sweeper skips forever and that boot replay tries to resurrect.
- **Invalid `casa_tz` no longer crashes every turn.** `resolve_tz()`
  catches `ZoneInfoNotFoundError`, logs a warning naming the bad
  value, falls back to `Europe/Amsterdam`. `lru_cache` does not cache
  exceptions, which pre-fix meant every single turn re-raised.

### Tests

- 37 new regression tests covering all of the above. The live N150
  bypass for `block_dangerous_bash` is captured directly in
  `test_rm_recursive_force_all_blocked`; the rest mirror their bug
  preconditions one-for-one.

### Removed

- **`github_token` addon option.** Plugin-developer now resolves
  `op://${onepassword_default_vault}/GitHub/credential` directly at
  engagement-spawn time. Vault is configurable via
  `onepassword_default_vault`; item title (`GitHub`) and field label
  (`credential`) are conventional. One fewer addon option to configure;
  1P is the single source of truth.

### Migration

- Users with `github_token` set in addon options: remove the entry,
  then ensure your 1P vault contains a `GitHub` item with a
  `credential` field holding a GitHub PAT (`repo` scope).

## [0.14.5] - 2026-04-24

### Fixed
- **N150 turn-assistant failure after plan 4b:** `assistant/runtime.yaml`
  had `cwd: /addon_configs/casa-agent/workspace` (legacy), but F.2 dropped
  the `mkdir -p workspace/...` block from setup-configs.sh. SDK spawn
  failed with `CLIConnectionError: Working directory does not exist`.
  Change cwd to empty so B.4 agent-home fallback takes effect.

## [0.14.4] - 2026-04-24

### Removed
- **Partial `ellen/` and `tina/` default agent dirs** (created by Plan 4b B.7
  with only `plugins.yaml`). These are plan-hypothetical agents not yet
  implemented; their partial dirs failed `agent_loader` required-file check
  in CI (`missing required file runtime.yaml`). Delete cleanly — can be
  re-added when the agents are fully specified.

## [0.14.3] - 2026-04-24

### Fixed
- **N150 boot crash on v0.14.2:** `assistant/delegates.yaml` plugin-developer
  entry used `{executor_type, description, typical_task, engagement_mode}`
  shape but `delegates.v1.json` schema only accepts `{agent, purpose, when}`.
  Rewrote the entry in the correct shape. All 4 delegate entries now match
  the schema.

## [0.14.2] - 2026-04-24

### Fixed
- **N150 boot crash on v0.14.1:** `agent_loader._check_file_set` rejected
  `plugins.yaml` (added by Plan 4b B.7) as "unknown file", crashing
  casa-main with exit 1 before SDK clients spawned. Added `plugins.yaml`
  to the `optional` file set for resident, specialist, and executor tiers.

## [0.14.1] - 2026-04-24

### Added
- **plugin-developer executor** (Tier 3 claude_code driver) that authors
  Claude Code plugins in dedicated per-plugin GitHub repos. Default plugin
  pack: superpowers + plugin-dev + skill-creator + mcp-server-dev +
  document-skills. Produces 100% CC-native plugins — installable into Casa
  agents via Configurator OR into any regular CC session.
- **Two-marketplace model** — `casa-plugins-defaults` (seed-managed, read-only,
  ships with the image) + `casa-plugins` (user-writable via Configurator).
- **Binding layer** at `/opt/casa/plugins_binding.py` — resolves
  `enabledPlugins` → `plugins=[{type:"local",path:...}]` for in_casa agents
  via `claude plugin list --json::installPath`. SDK does not auto-consume
  plugins; this closes the gap.
- **Workspace-template** pattern for claude_code executors
  (`defaults/agents/executors/<type>/workspace-template/` rendered into
  every engagement workspace).
- **Seven Configurator MCP tools** — `marketplace_add_plugin` /
  `marketplace_remove_plugin` / `marketplace_update_plugin` /
  `marketplace_list_plugins` / `install_casa_plugin` (two-stage commit) /
  `uninstall_casa_plugin` / `verify_plugin_state`.
- **`casa.systemRequirements`** — tarball/venv/npm install strategies
  into `/addon_configs/casa-agent/tools/`. apt/dpkg declarations rejected
  at add-time (§4.3.2).
- **Boot-time reconciler** — idempotent, non-blocking; records status to
  `system-requirements.status.yaml`.
- **self_containment_guard** pre-push hook policy — greps for hardcoded
  non-baseline paths, "please install X manually" README strings,
  `apt install` in shell scripts.
- **Universal 1P resolver** — all password-typed addon options accept
  `op://vault/item/field`. `op` CLI installed at image build.
- **`github_token` addon option** (required for plugin-developer).
- **Self-containment axiom** (§2.0) codified — plugins fully operational
  on fresh Casa install solely by marketplace-add + install_casa_plugin.

### Removed

- `repos:` addon option + `sync-repos.sh` script. This was a half-built
  scratch-sync mechanism with no runtime consumer (§9 of Plan 4b spec).
  **Migration:** users with non-empty `repos:` entries must remove them
  from the addon config before upgrading. No data migration needed.
- `/opt/casa/claude-plugins/` symlink tree (Tier 1/2 bundled plugins).
  Replaced by seed-managed `casa-plugins-defaults` marketplace.

### Changed
- Resident SDK construction now sets `cwd=/addon_configs/casa-agent/agent-home/<role>/`
  and injects `plugins=[...]` from the binding layer. `"Skill"` added to
  `allowed_tools` automatically.
- Claude_code engagement subprocesses inherit `CLAUDE_CODE_PLUGIN_SEED_DIR=
  /opt/claude-seed` + `CLAUDE_CODE_PLUGIN_CACHE_DIR=
  /addon_configs/casa-agent/cc-home/.claude/plugins`.
- Casa-main HOME moved from `/root` to `/addon_configs/casa-agent/cc-home/`.

### Notes
- Pre-1.0 wipe-on-update doctrine — no migration code shipped.
- Plan 4b spec: `docs/superpowers/specs/2026-04-24-3.5-plan4b-plugin-developer.md`.
- Plan: `docs/superpowers/plans/2026-04-24-3.5-plan4b-plugin-developer.md`.

## [0.14.0] — Phase 3.6 — `casa-framework` MCP extraction

### Added
- `svc-casa-mcp` — new s6-rc-supervised standalone service (s6 service
  files at `etc/s6-overlay/s6-rc.d/svc-casa-mcp/`, Python entry at
  `rootfs/opt/casa/svc_casa_mcp.py`). Listens on `127.0.0.1:8100`,
  serves `POST /mcp/casa-framework` (JSON-RPC 2.0) and `POST /hooks/resolve`,
  forwards every request to casa-main over a Unix domain socket at
  `/run/casa/internal.sock`.
- Casa-main second `aiohttp.AppRunner` on the Unix socket exposing
  `POST /internal/tools/call` and `POST /internal/hooks/resolve`. New
  helper `start_internal_unix_runner()` in `casa_core.py`.
- New module `mcp_envelope.py` — JSON-RPC envelope helpers + tool schema
  translation, shared between svc-casa-mcp and the public-port fallback.
- New module `internal_handlers.py` — pure aiohttp handler factories
  bound to the Unix socket and consumed in-process by the public-8099
  fallback.
- `CASA_FRAMEWORK_MCP_URL` and `CASA_HOOK_RESOLVE_URL` env-var overrides
  for ops-time port redirection.
- E2E coverage: `test-local/e2e/test_mcp_restart_survival.sh` (D-13)
  proves bouncing casa-main does not drop engagement-subprocess MCP
  connections; new D-11 + D-12 blocks in `test_engagement.sh` exercise
  svc-casa-mcp on port 8100.

### Changed
- `drivers/workspace.py` `.mcp.json` writer now points at
  `127.0.0.1:8100/mcp/casa-framework` for newly-provisioned workspaces
  (was 8099). Existing pre-v0.14.0 workspaces unaffected.
- `scripts/hook_proxy.sh` default URL bumped from 8099 → 8100 with
  `CASA_HOOK_RESOLVE_URL` env override.
- Casa-main public port 8099 continues to serve `/mcp/casa-framework`
  and `/hooks/resolve` as a back-compat fallback for pre-v0.14.0
  workspaces. Removed in v0.14.2 or later (one-release migration).

### Removed
- `casa-agent/rootfs/opt/casa/mcp_bridge.py` — logic split between
  `mcp_envelope.py`, `internal_handlers.py`, `svc_casa_mcp.py`, and
  `casa_core.py`'s public-fallback wrappers. Net coverage unchanged.
- `tests/test_mcp_bridge.py` — coverage migrated to
  `tests/test_mcp_envelope.py`, `test_internal_handlers.py`,
  `test_svc_casa_mcp.py`, and `test_public_fallback_routes.py`.

### Notes
- Restart-survival semantics are Level 1 only: mid-restart tool calls
  return `casa_temporarily_unavailable`; the model handles retry. No
  buffering, no replay, no idempotency guarantees beyond what individual
  tool handlers already provide.
- The pre-existing v0.13.1 known limitation (per-executor hook params
  on the HTTP path use factory defaults) is unchanged in v0.14.0 — that
  wiring is a later item.

## [0.13.1] — 2026-04-23

### Added
- **MCP JSON-RPC 2.0 HTTP bridge at `POST /mcp/casa-framework`.** Real
  `claude` CLI subprocesses can now reach Casa MCP tools via the in-process
  bridge. `X-Casa-Engagement-Id` request header binds `engagement_var` for
  the tool call's duration; missing/unknown id binds `None` and tools that
  guard on engagement context return `not_in_engagement`. GET returns 405.
  Stateless (no session, no SSE). New module: `mcp_bridge.py` (244 LoC).
- **`X-Casa-Engagement-Id` header** written into per-engagement `.mcp.json`
  by `provision_workspace` so the CC CLI forwards it on every `tools/call`.
- **Workspace sweeper.** APScheduler job every 6 hours removes
  `/data/engagements/<id>/` for COMPLETED/CANCELLED engagements past
  `retention_until` (default 7 days from terminal transition).
  `_finalize_engagement` writes `.casa-meta.json` with terminal status +
  retention at terminal-transition time for claude_code driver engagements.
- **Three new MCP tools** exposed on both the SDK path and the HTTP bridge:
  - `list_engagement_workspaces(status?)` — enumerate workspaces with
    status + size, truncated at 100 entries.
  - `delete_engagement_workspace(engagement_id, force=false)` — delete
    a workspace; refuses UNDERGOING without `force=true`.
  - `peek_engagement_workspace(engagement_id, path?, max_bytes?)` —
    read-only tree listing or file read with path-traversal guard.
- **Boot-replay heal path.** When an UNDERGOING engagement's s6 service
  dir is missing and the executor type is in the registry,
  `replay_undergoing_engagements` re-renders the run + log/run scripts
  and re-plants the dir (workspace must still exist — missing workspace
  stays warn-and-skip per §7.3 of the 4a.1 spec). Missing executor →
  warn-and-skip. Takes new optional `executor_registry` kwarg.
- **MCP-blip spike harness** at `test-local/spike/mcp_blip/` — throwaway
  aiohttp server + driver script that simulates mid-`tools/call` connection
  loss to empirically classify CC's MCP client as pessimistic (retries) or
  optimistic (no retry). Runs on N150, not CI. Result feeds the
  Plan 4b / 3.6 decision.

### Changed
- **`HOOK_POLICIES` refactored** from `{name: factory_returning_HookMatcher}`
  to two-tier `{name: {"matcher": regex, "factory":
  factory_returning_HookCallback}}`. The SDK path builds the `HookMatcher`
  once, at `resolve_hooks` time; the new HTTP path reuses the same raw
  `HookCallback`. Four `_policy_*` thin-wrapper helpers dropped; four
  slimmer `_*_factory` helpers replace them.
- **`/hooks/resolve`** replaces the v0.13.0 pass-through stub with real
  policy enforcement: `block_dangerous_bash`, `path_scope`,
  `casa_config_guard`, `commit_size_guard` all produce real deny/allow
  decisions for `claude_code` engagements. Returns CC-native
  `{"hookSpecificOutput": {...}}` shape. Defensive matcher regex re-check
  before dispatch. Callback exceptions return deny, not fail-open.
- **`CASA_TOOLS`** extracted to a module-level tuple in `tools.py` for
  iteration by both the SDK server and the HTTP bridge. Adding a tool to
  the tuple exposes it on both transports automatically.

### Fixed
- **`scripts/hook_proxy.sh` port 8080 → 8099.** Casa binds on 8099; the
  stale shim would have always failed open to "casa unreachable". The
  v0.13.0 stub handler hid this bug; flipping to real enforcement without
  the port fix would have wedged all engagements behind the fail-open
  path.

### Spike findings
- (Fill in from `test-local/spike/mcp_blip/README.md` Result section
  after running the spike on the N150. Include the 1-line verdict:
  `retry_observed=yes|no`, and the ROADMAP implication for 3.6 /
  Plan 4b.)

### Known limitations
- Per-executor hook parameters (e.g. `casa_config_guard.forbid_write_paths`)
  on the HTTP path use factory defaults — the Configurator's defaults
  happen to match what that executor wants. Wiring per-executor YAML
  params into the HTTP path is a later item.

## [0.13.0] — 2026-04-23

### Added
- **Plan 4a — `claude_code` driver.** Replaces the v0.11.0 stub.
  Per-engagement s6-rc-supervised `claude` CLI process (instead of
  Casa-main child) — engagement subprocesses outlive Casa-main restarts.
  New modules: `drivers/s6_rc.py`, `drivers/workspace.py`,
  `drivers/hook_bridge.py`, `scripts/hook_proxy.sh`,
  `scripts/engagement_run_template.sh`.
- **Remote control infrastructure.** Each engagement posts its
  `--remote-control` URL to the Telegram topic when it becomes available;
  users can attach via Claude iOS app or claude.ai/code and drive the
  engagement from anywhere.
- **Tier 1 baseline plugin pack.** `superpowers@v5.0.7` bundled at
  `/opt/casa/claude-plugins/base/superpowers/`. Symlinked into every
  `claude_code`-driver engagement's isolated `$HOME` at provisioning.
- **`hello-driver` test harness executor type.** `enabled: false`;
  validates the driver lifecycle in CI via mock CLI.
- **Boot replay for UNDERGOING engagements.** `replay_undergoing_engagements`
  in `casa_core.py` sweeps orphan service dirs, recompiles the s6 db,
  starts each UNDERGOING engagement's service, and spawns URL-capture +
  respawn-poller tasks.
- **Transcript archival to Honcho keyed by executor type.** Retrofits
  the already-shipped v0.12.0 Configurator — every engagement's completion
  summary lands under peer `executor:<type>` for future "Ellen primes a
  new engagement with past lessons" (Plan 4b+).
- **`/hooks/resolve` loopback endpoint.** Routes CC hook decisions through
  Casa's `HOOK_POLICIES` registry via `hook_proxy.sh` — same policy code
  governs `in_casa` and `claude_code` executors.
- **Sensitive-env blocklist.** The per-engagement `run` script unsets
  `TELEGRAM_BOT_TOKEN` / `HONCHO_API_KEY` / `WEBHOOK_SECRET` /
  `SUPERVISOR_TOKEN` / `HASSIO_TOKEN` before spawning the CLI.
  `CLAUDE_CODE_OAUTH_TOKEN` is preserved (CLI needs it). Future sensitive
  vars must be added to this list in the same commit.

### Changed
- `ExecutorDefinition` gains four optional fields: `extra_dirs`,
  `mirror_chat_to_topic`, `archive_session_full`, `plugins_dir`.
- `engage_executor` now dispatches to the `claude_code` driver for
  `driver: claude_code` executor types instead of raising NotImplementedError.
- `_finalize_engagement` now routes `driver.cancel()` to the per-engagement
  driver based on `engagement.driver`.

### Infrastructure
- Dockerfile clones superpowers at build time (adds ~30 MB to image).
- `setup-configs.sh` pre-creates `/data/casa-s6-services/` and
  `/data/engagements/`.

### Notes
- §10.2 of the design spec — `emit_completion` landing during a Casa-main
  restart's ~30s MCP blip is a known sharp edge in v0.13.0. If Plan 4a.1
  spike-milestone-3 discovers the CLI's MCP client is optimistic (silently
  drops the call on connection loss), ROADMAP 3.6 (`casa-framework` MCP
  extraction to its own s6 service) is re-prioritized as a co-requisite
  before Plan 4b's `plugin-developer` ships. Until then: accept-the-gap.
- `/hooks/resolve` endpoint routes policies through a pass-through stub
  at the HTTP boundary; HOOK_POLICIES values are SDK HookMatcher factories
  not directly HTTP-callable. Real enforcement via in-process hook
  callbacks still works. Future iteration will ship an HTTP-native policy
  layer.
- TelegramChannel now skips InCasaDriver's resume/orphan logic for
  `claude_code` engagements (which have no `sdk_session_id`).
- **MCP HTTP bridge deferred to Plan 4a.1.** The `casa-framework` MCP server
  is currently an in-process SDK server (via `create_sdk_mcp_server`) with
  no HTTP surface. `ClaudeCodeDriver` writes `.mcp.json` pointing at
  `http://127.0.0.1:8099/mcp/casa-framework`, but that route is not yet
  implemented. The v0.13.0 infrastructure (s6-rc, workspace, boot replay,
  hook bridge, hello-driver) is fully reviewed and green in CI via the
  mock CLI, but a real `claude` CLI subprocess cannot yet reach the Casa
  MCP tools. Plan 4a.1 will add an aiohttp MCP JSON-RPC bridge at
  `/mcp/casa-framework` and propagate engagement context via an
  `X-Casa-Engagement-Id` request header so `emit_completion` /
  `query_engager` can resolve the calling engagement.

## 0.12.0 — 2026-04-??

### Added — Phase 3.5 Plan 3: UC1 Configurator

- First Tier 3 Executor type: configurator - knows Casa's configuration surface and CRUDs it via engagement topic.
- ExecutorRegistry + ExecutorDefinition + agent_loader.load_all_executors.
- executor.v1.json JSON schema.
- Three new MCP tools: config_git_commit, casa_reload (Supervisor addon restart), casa_reload_triggers(role) (in-process).
- TriggerRegistry.reregister_for(role, triggers, channels) - soft-reload primitive.
- Two new hook policies: casa_config_guard (blocks /data/, /schema/, /opt/casa/, resident deletion) and commit_size_guard (ask above N files).
- engage_executor real implementation (was stub in v0.11.0).
- TELEGRAM_BOT_API_BASE env override in channels/telegram.py - retires Plan 2's deferred e2e coverage.
- Configurator defaults at defaults/agents/executors/configurator/: definition.yaml, prompt.md, hooks.yaml, observer.yaml + 20 doctrine markdown files (~3000 lines).
- Ellen prompt updates: runtime.yaml (engage_executor allowlisted), delegates.yaml (configurator entry), prompts/system.md (Configuration requests section).
- Setup-configs.sh + test override: seed agents/executors/ subtree.
- DOCS.md: "Configurator (v0.12.0)" section.
- E2E: test_engagement.sh E-1..E-8 fleshed (Plan 2 deferred cleared) + E-9 happy path + E-10 hook-blocked.
- Manual smoke: test-local/smoke/test_configurator_engagement.sh.
- Addon option: telegram_bot_api_base (default empty).

## 0.11.0 — 2026-04-22 — Engagement primitive + Tier 2 Specialist interactive mode

### Added

- **Engagements — bounded conversational threads in a Telegram forum supergroup.**
  New addon option `telegram_engagement_supergroup_id` binds Casa to a
  dedicated supergroup; each engagement spawns a forum topic via
  `createForumTopic`. See DOCS.md "Engagements" section for setup.
- **`delegate_to_specialist(mode="interactive")`**. New branch: instead of
  one-shot sync/async invocation, opens an engagement topic where the
  specialist (e.g. Alex) works with the user turn-by-turn. Completion is
  agent-driven via the new `emit_completion` tool; the user can end early
  via `/complete` or `/cancel` in the topic.
- **`engage_executor` MCP tool** (stub — returns `kind=no_executor_types`
  until Tier 3 types land in Plan 3+). Wires Ellen for the future
  engage flow; Plan 3 fleshes out with the configurator executor type.
- **`query_engager` MCP tool** — specialist-side retrieval. Bounded LLM
  synthesis over the engager's scope-filtered memory; returns `unknown`
  when context is insufficient.
- **`emit_completion` MCP tool** — specialist-side completion funnel.
  Publishes a structured summary (`text`, `artifacts`, `next_steps`),
  closes the topic (✅ icon), writes the summary to Ellen's meta-scope
  memory, and NOTIFIES Ellen for in-main-chat narration.
- **`cancel_engagement` MCP tool** — Ellen-callable. Tears down the
  driver and finalizes the record.
- **Observer module.** Static classifier + rate limiter (3 per engagement)
  + `/silent` per-engagement override. Trigger events (errors, warnings,
  idle-detected, unknown query_engager) run a bounded haiku-class LLM
  pass that may NOTIFY Ellen to interject in the main 1:1 chat.
  Per-type YAML override arrives with Plan 3.
- **Idle + suspension scheduler.** New APScheduler job
  (`engagement_idle_sweep`, daily 08:00) emits `idle_detected` bus
  events after 3 days of no user turn (specialists; 7 days for
  executors — Plan 3+); weekly re-fire. Live SDK clients torn down
  after 24h idle with `sdk_session_id` persisted for seamless resume
  on next user turn.
- **`in_casa` driver** (full impl) and **`claude_code` driver stub**
  (raises `NotImplementedError`, Plan 5 fills in).
- **Slash commands** `/cancel`, `/complete`, `/silent` registered in the
  engagement supergroup via `setMyCommands` for in-UI discoverability.
- **Addon option** `telegram_engagement_supergroup_id` (int?, 0 = disabled).

### Infrastructure

- New `casa-agent/rootfs/opt/casa/engagement_registry.py` — mirrors
  `specialist_registry.py` pattern. Persists live records to
  `/data/engagements.json`; finished records drop from disk (Ellen's
  meta-scope memory is the durable log).
- New `casa-agent/rootfs/opt/casa/drivers/` subpackage: `driver_protocol.py`,
  `in_casa_driver.py`, `claude_code_driver.py`.
- Ellen's shipped `runtime.yaml` + `delegates.yaml` + `prompts/system.md`
  updated to explain engagements and the new tools.
- Mock Telegram Bot API server at `test-local/e2e/mock_telegram/server.py`
  used by the new `test_engagement.sh` (CI).
- Manual Telegram smoke at `test-local/smoke/test_telegram_engagement.sh`
  exercises the real Bot API; not in CI — run pre-N150 deploy.
- `.github/workflows/qa.yml` adds the engagement e2e step.

### Breaking — acceptable pre-1.0.0

- `init_tools` signature adds a new kwarg `engagement_registry`. Internal
  to Casa; no external consumers.

### Deferred

- Tier 3 executor types (configurator, ha-developer, plugin-developer)
  — Plans 3, 4, 5.
- Per-type `observer.yaml` override — Plan 3.
- `claude_code` driver implementation — Plan 5.
- `next_steps` auto-chain by Ellen — Plan 3 (no Tier 3 types to chain to yet).
- Engagement topic archival/housekeeping — Plan 6+.
- `test_engagement.sh` E-1..E-8 checkpoints — scaffolded but not functional;
  flesh in follow-up commits as `TELEGRAM_BOT_API_BASE` override lands.

### Version

- `casa-agent/config.yaml`: `0.10.0` → `0.11.0`.

## 0.10.0 — 2026-04-22 — Rename: Tier 2 "Executor" → "Specialist"

Preparation for Phase 3.5 engagement primitive + Tier 3 Executors (see
`docs/superpowers/specs/2026-04-22-3.5-engagement-and-executors.md` §10).
The "Executor" term shipped in v0.6.2 is renamed to "Specialist" to
free the name for the ephemeral, task-bounded Tier 3 agents coming in
Plan 2. Zero behavior change — pure terminology refactor.

### Breaking (acceptable pre-1.0.0)

- **Directory:** `/addon_configs/casa-agent/agents/executors/` →
  `/addon_configs/casa-agent/agents/specialists/`. Migration on first
  boot under v0.10.0 is by convention — the overlay is wipe-acceptable
  per the pre-1.0.0 doctrine. An empty `agents/executors/` directory
  is now reserved for Plan 2+ Tier 3 Executor types.
- **MCP tool:** `mcp__casa-framework__delegate_to_agent` →
  `mcp__casa-framework__delegate_to_specialist`. Tool argument key
  `agent=...` → `specialist=...`. Error kind `unknown_agent` →
  `unknown_specialist`. Ellen's shipped `runtime.yaml` tool allow-list
  updated accordingly.
- **Python imports:** `from executor_registry import ExecutorRegistry` →
  `from specialist_registry import SpecialistRegistry`. Internal to
  Casa — affects nobody outside the codebase.

### Code

- `executor_registry.py` → `specialist_registry.py` (class
  `ExecutorRegistry` → `SpecialistRegistry`).
- `agent_loader.py`: `load_all_executors` → `load_all_specialists`;
  `TIER_FILES["executor"]` → `TIER_FILES["specialist"]`; `_DELEGATE_MCP_TOOL`
  constant updated; all error messages updated; `load_all_agents` now
  skips BOTH `specialists/` (Tier 2 home) and `executors/` (reserved
  for Plan 2 Tier 3).
- `tools.py`: `delegate_to_agent` handler → `delegate_to_specialist`;
  `_executor_registry` state var renamed; `_build_executor_options` →
  `_build_specialist_options`; `_run_executor` → `_run_specialist`;
  `init_tools` signature updated.
- `casa_core.py`, `agent.py`: import updates, variable renames,
  comment sweep.
- `defaults/agents/executors/` → `defaults/agents/specialists/`
  (including `finance/`). Finance prompt and character card updated.
  Ellen's character card and `runtime.yaml` tool allow-list updated.
  `defaults/schema/agent.v1.json` meta-doc updated to match the new
  TIER_FILES key.
- `setup-configs.sh` + `test-local/init-overrides/01-setup-configs.sh`:
  seed `agents/specialists/` from defaults; reserve empty
  `agents/executors/` for Plan 2.

### Tests

- `tests/test_executor_registry.py` → `test_specialist_registry.py`.
- `tests/test_delegate_to_agent.py` → `test_delegate_to_specialist.py`.
- `tests/test_agent_loader.py`, `test_agent_process.py`, `test_config.py`,
  `test_get_schedule_tool.py`, `test_notification_handling.py`,
  `test_casa_core_agent_loading.py`: reference updates.
- `test-local/mock-claude-sdk/claude_agent_sdk/__init__.py`: comment update.
- `test-local/e2e/test_delegation.sh` → `test_specialist_delegation.sh`;
  fixture dir `test-local/fixtures/delegation-enabled/agents/executors/`
  → `agents/specialists/`. `.github/workflows/qa.yml` updated to the
  new script name.

### Freed

- `agents/executors/` is now reserved for Tier 3 Executor types, arriving
  in Plan 2 (engagement primitive). Empty in v0.10.0.

## 0.9.1 — 2026-04-22 — Drop dead pre-v0.7.0 heartbeat config

### Removed

- **`heartbeat_enabled` / `heartbeat_interval_minutes` addon options.**
  Zero runtime consumers — the global heartbeat block was removed in
  v0.7.0 (Phase 4.x refactor, replaced by per-agent
  `agents/<role>/triggers.yaml`). Since then the options have been
  visible in the HA UI but had no effect. Removed from `config.yaml`
  (both `options:` and `schema:` blocks), `DOCS.md` Features table,
  `translations/en.yaml`, `test-local/options.json.example`, and the
  `test-local/init-overrides/03-export-env.sh` export loop.
- **`e2e-slow` nightly CI job + `test-local/e2e/test_heartbeat.sh`.**
  Same v0.7.0 rot — the test referenced `defaults/webhooks.yaml` and a
  top-level `schedules.yaml`/`heartbeat:` block that no longer exist.
  Also dropped the `schedule: cron "0 4 * * *"` workflow trigger and
  the `test-slow` Makefile target. (Landed earlier today on master in
  commit `2ffa4a6`; called out here for completeness.)

### Changed

- **DOCS.md "How it works" bullet 5** rewritten from the
  global-heartbeat narrative to the current per-agent trigger
  architecture.

### Migration

- **Pre-1.0.0, no migration block.** `/addon_configs/casa-agent/` is
  wipe-acceptable; if a user had explicit `heartbeat_enabled: ...` in
  their options YAML, the HA UI will surface it as "unused option" on
  next restart and they can delete it. Nothing in the runtime depended
  on the value.

## 0.9.0 — 2026-04-21 — Phase 3.3: Scheduling v2 + builder-first config ergonomics

### Added

- **`get_schedule` framework tool** on `casa-framework` MCP server.
  Returns the caller's own upcoming interval + cron triggers as a
  markdown bullet list within a configurable `within_hours` window
  (default 24, clamped to [1, 720]). Own-role visibility only.
- **Unified `<field>_file:` prose externalization idiom.** New shared
  `_resolve_prose` helper in `agent_loader.py` reads either an inline
  YAML field or a relative markdown file under the agent dir. Applies
  `_substitute_env` so external prompts see the same env-var
  substitutions as inline strings. Path traversal + non-`.md`
  extension rejected at load time.
- **Schema support** for `prompt_file` and `card_file` alternatives
  via `oneOf` branches in `character.v1.json` and `triggers.v1.json`.
- **APScheduler hardening**: explicit timezone (`resolve_tz()`:
  `CASA_TZ` → `TZ` → `Europe/Amsterdam`), `misfire_grace_time=600`,
  `coalesce=True`, `max_instances=1`. Restart-safe and wall-clock
  correct.
- **`<current_time>` system-prompt block** — every agent turn gets an
  ISO-8601 timestamp with weekday, time-of-day, and ISO week number
  injected into the composed system prompt. Same timezone source as
  the scheduler.
- **`casa_tz` addon option** in `config.yaml`. Default
  `Europe/Amsterdam`. Propagated to Python via `CASA_TZ` env var.
- **`TriggerRegistry.list_jobs_for(role, within_hours)`** public method
  backing the tool.
- **Seeded defaults**: `assistant/prompts/system.md`,
  `butler/prompts/system.md`, `executors/finance/prompts/system.md`
  — system prompts extracted from inline. `assistant/triggers.yaml`
  gains `morning-briefing` cron at `"0 8 * * 1-5"` Europe/Amsterdam
  using `prompt_file: prompts/morning-briefing.md`.

### Changed

- `_build_character` and `_build_triggers` take `agent_dir` kwarg for
  relative-path resolution.
- `init_tools` in `tools.py` takes a new optional
  `trigger_registry` kwarg.
- Scheduler + trigger registry construction moved ahead of `init_tools`
  call in `casa_core.py` so the tool has a live registry reference.
- `_check_file_set` now skips subdirectories inside an agent dir (so
  `prompts/` doesn't trigger the unknown-file guard).

### Migration

Pre-1.0.0 doctrine: no migration script. Existing
`/addon_configs/casa-agent/agents/*/character.yaml` files using
inline `prompt:` still validate and load. Users who want to benefit
from markdown-editable system prompts can either delete the overlay
(next boot re-seeds the updated defaults) or hand-move their prompt
to `<agent>/prompts/system.md` and switch `character.yaml` to
`prompt_file:`.

## 0.8.6 — 2026-04-21 — Pre-1.0.0 migration cleanup

Codebase slimming pass. Removes every version-migration block in
`setup-configs.sh` + the matching test-mode override + the v0.8.5
existing-instance e2e scenario + a pre-2.2a lazy-migration `.pop` in
`SessionRegistry`. Net -303 lines across the branch.

Driver: **pre-1.0.0 doctrine.** Casa is in full development mode
until v1.0.0. `/addon_configs/casa-agent/` is expected to be wiped
between addon updates; breaking changes ship by updating the shipped
defaults, not by migrating user state. Migration blocks + `.applied`
markers + `.pre-vX.Y.Z.bak` backups are over-engineering at this
stage — v0.8.5 proved it: the scope-corpus migration block shipped
with v0.8.5 never fired on the N150 deploy because the overlay was
fresh on update; seed-if-missing produced an identical outcome.

Removed:
- `casa-agent/rootfs/etc/s6-overlay/scripts/setup-configs.sh` —
  the v0.8.5 `SCOPE_MIGRATION_MARKER` block (lines 62-76),
  `migrate_default_scope()` + its two invocations (lines 83-128),
  `migrate_butler_disclosure_v2()` + invocation (lines 130-153).
  Seed-if-missing blocks retained — those are idempotent seeding,
  not migrations.
- `test-local/init-overrides/01-setup-configs.sh` — same blocks
  mirrored from prod.
- `test-local/e2e/test_migration.sh` — M-7 (v0.8.5 marker absent),
  M-9 (backup absent). Reworked M-8 → M-6 as a generic seed-content
  check (`scopes.yaml == shipped defaults` on fresh install).
- `test-local/e2e/test_migration_v085_existing.sh` — whole 68-line
  script deleted (the existing-overlay → migrate → backup scenario
  is dead code).
- `casa-agent/rootfs/opt/casa/session_registry.py` — the
  `.pop("memory_session_id", None)` in `touch()` + the matching
  docstring notes about lazy migration from pre-2.2a entries.
- `tests/test_session_registry.py::TestMigration` class.

Ship-gate doctrine saved to
`memory/feedback_ship_gate_doctrine.md` (new this session):
9-gate sequence per version bump; Monitor as the default for tests
and long-running tasks; `/ha-prod-console:*` as the first choice for
N150 interaction; pre-1.0.0 = no migrations.

Unchanged (NOT migrations):
- `executor_registry.py` orphan-recovery tombstone — runtime
  crash-recovery, not version migration.
- `log_cid.py` boot-time filter cleanup — idempotence, not
  version migration.

## 0.8.5 — 2026-04-21 — Phase 3.2.2: scope-routing hardening

Scope-routing accuracy hardening + structured `scope_route` emission.
Spec at `docs/superpowers/specs/2026-04-21-3.2.2-scope-routing-hardening.md`.

- **scopes.yaml description hardening** — Replaced the v0.8.0 prose
  corpora with comma-separated keyword phrase clusters targeting the
  7 cross-cutting probe failures the v0.8.4 sweep exposed. Generic
  only — no personal names, organizations, or place names — so the
  addon stays shippable to other households. Tenant-specific signals
  belong in the per-instance overlay at
  `/addon_configs/casa-agent/policies/scopes.yaml`, which Builder
  (Phase 3.5) is authorized to edit. The new authoring contract is
  documented as a top-of-file comment block in the defaults file
  itself.
- **`ACCURACY_BASELINE` 0.80 → 0.85** in `tests/test_scope_routing_eval.py`.
  The flat-curve finding from v0.8.4 still holds — threshold tuning is
  a no-op at this fixture scale; the gain comes entirely from the
  description corpus change.
- **Structured `scope_route` log emission.** `agent.py:455` now emits
  via `logger.info("scope_route", extra={"channel": ..., "winner": ...,
  "winner_score": ..., "second_score": ..., "threshold": ...})`. New
  `_winner_pair()` helper computes the read-side winner from the
  `scores` dict.
- **Generic `extra={...}` flow in `log_cid.py`.** `JsonFormatter` now
  flattens non-standard `LogRecord` attributes into the JSON payload;
  new `HumanFormatter` appends them as `key=val` suffix. Benefits any
  future structured log call, not just `scope_route`. New
  `STANDARD_LOGRECORD_ATTRS` constant + `_record_extras()` helper.
- **`scripts/eval_scope_dist.py` works against live N150 logs** —
  the parser was always ready for this shape; the upstream emission
  is the change that unblocks it.
- **One-shot v0.8.5 migration** in `setup-configs.sh` — refreshes
  the per-instance overlay at `/addon_configs/casa-agent/policies/scopes.yaml`
  on first boot, gated by marker file
  `migrations/scope_corpus_v0.8.5.applied`. Pre-migration overlay is
  preserved as `scopes.yaml.pre-v0.8.5.bak`. Manual edits made AFTER
  the marker is written are preserved across all later boots.
- **`ScopeRegistry.threshold` exposed as a public read-only property.**
  Was `_threshold` private; agent.py needed read access for the new
  emission. Constructor signature unchanged.
- **Tests.** New `TestExtrasFlatten` (4 cases) in `tests/test_log_cid.py`;
  new `TestScopeRouteEmission` in `tests/test_agent_process_scope.py`;
  new `TestThresholdProperty` in `tests/test_scope_registry.py`. New
  e2e scenario `test-local/e2e/test_migration_v085_existing.sh` plus
  M-7..M-9 in `test_migration.sh`. 594 unit tests green; full-mode
  accuracy gate 0.943 (baseline 0.85); all local e2e scripts green
  after Dockerfile.test infra catch-up (see below).
- **Test-infra catch-up — `test-local/Dockerfile.test` migrated to
  Debian bookworm.** The main `casa-agent/Dockerfile` switched to
  `amd64-base-debian:bookworm` in v0.8.1 when fastembed pulled
  onnxruntime (no musllinux wheel) — but the test Dockerfile was
  left on Alpine/musl, breaking the local e2e harness and
  `.github/workflows/qa.yml` CI from v0.8.1 onward. v0.8.5 mirrors
  the v0.8.1 migration recipe into the test image so e2e can run
  again. Also adds the v0.8.5 migration block to
  `test-local/init-overrides/01-setup-configs.sh` (the test-mode
  setup-configs override that replaces the bashio-dependent prod
  script) — without this the test container would skip the
  migration entirely since the prod script never runs there.

Rollback: §10 of the spec. Backup file + marker removal restore v0.8.4
runtime behaviour; reverting the formatter changes and `agent.py:455`
restore prior log shape.

## 0.8.4 — 2026-04-21 — Scope-routing evaluation harness

### Added
- `casa_eval/` framework — pluggable `Tester` ABC +
  `Suite`/`Case`/`Report`/`Failure`/`Recommendation` dataclasses, all
  JSON-round-trippable. Designed so a future Builder MCP tool can call
  the same `Tester.run()` / `Tester.sweep()` / `recommend_from_sweep()`
  surface with a thin JSON wrapper.
- `ScopeRoutingTester` — evaluates scope-routing accuracy on a labelled
  probe suite with a tunable threshold. Emits `accuracy`,
  `top2_accuracy`, `fallback_rate`, `mean_winner_score`, `mean_margin`,
  `p50_latency_ms`, `p95_latency_ms`. `optimization_axes = ["threshold"]`;
  `optimization_bounds = {"threshold": (0.20, 0.50)}`. Model is frozen
  (see CHANGELOG 0.8.2 rationale).
- `tests/fixtures/eval/scope_routing/default.yaml` — 35-case probe
  suite across the four shipped scopes. Grows by hand when Nicola spots
  a misroute in prod (`metadata.source='real-misroute'`).
- Three pytest run modes: fast (mocked `_FakeEmbedder`, always-on in
  CI); full (`CASA_REAL_EMBED=1`, asserts `accuracy >= 0.85`,
  `fallback_rate <= 0.20`); sweep (`CASA_EVAL_SWEEP=1
  CASA_REAL_EMBED=1`, informational table + recommendation).
- `scripts/eval_scope_dist.py` — audits live `scope_route` log lines,
  emits per-channel winner-score histograms (text or `--json`), flags
  channels whose winners cluster within ±0.05 of the threshold.

### Changed
- `scope_threshold` promoted from a silent env-var fallback (the
  `CASA_SCOPE_THRESHOLD` default `0.35` at `casa_core.py:427`) to a
  first-class HA addon option in `config.yaml`. Default unchanged;
  users can now tune it via the HA UI and Builder will be able to tune
  it via `supervisor.addon_options_set` in 3.5. Runtime read semantics
  at `casa_core.py:427` are untouched — the env var is now sourced
  from `bashio::config 'scope_threshold'` in
  `etc/s6-overlay/s6-rc.d/svc-casa/run`. Restart-required, matching
  every other addon option (restart cost on N150 ≈ 3 sec).

### Known limitations
- `scripts/eval_scope_dist.py` expects JSON-structured `scope_route`
  log records with `winner_score`/`second_score`/`threshold` fields.
  The live addon at v0.8.4 emits `scope_route` as a formatted-string
  log line (see `agent.py:441`) without `winner_score`, so the script
  reports "total records: 0" against unmodified production logs. A
  follow-up commit will extend the upstream emission (either JSON
  `extra=` or additional score fields in the format string) to unblock
  the audit tool. Parser logic is fully tested against synthetic logs
  and will work the moment the emission ships the expected fields.
- Measured sweep on the 35-case seed fixture shows accuracy is
  **threshold-invariant over [0.20, 0.45]** — `mean_winner_score ~= 0.787`,
  so every case sits above the entire optimization range and `argmax`
  never falls back. `recommend_from_sweep` picks 0.20 by tiebreak only;
  this is not a real improvement. `scope_threshold` stays at 0.35.
  `ACCURACY_BASELINE` was measured at 0.80 on the seed fixture (not
  0.85 as initially scoped) — raising it requires either dropping
  cross-cutting probes from the default set or hardening
  `scopes.yaml` descriptions to better differentiate
  finance/business/personal. Tracked as a 3.2.2 follow-up.

### Notes — post-deploy recipe
- Full-mode pytest on the live N150:
  `sudo docker exec addon_c071ea9c_casa-agent sh -c \
   'cd /opt/casa && CASA_REAL_EMBED=1 python3 -m pytest \
    /opt/casa/tests/test_scope_routing_eval.py::TestScopeRoutingTesterFull -v'`
  (run via `/ha-prod-console:exec` after each deploy that touches
  `scopes.yaml` descriptions or the threshold).

## 0.8.3 — 2026-04-21 — Voice-latency optimizations

### Added
- Per-process LRU cache for query embeddings in `ScopeRegistry` (256
  entries, keyed on `text.strip().lower()`). Voice retriggers and
  repeat commands are frequent — hits skip the ~90 ms ONNX forward
  pass and drop `score()` cost from ~90 ms to ~1 ms (just the cosine
  dot-products).
- `scope_route` telemetry now includes `embed_cache=N/M` where `M` is
  total calls this process has seen. Use to verify the cache is
  actually paying off after a few hours of real use.
- `ScopeRegistry.cache_stats()` returns `(hits, misses)` for tests
  and telemetry.

### Changed
- Write-path classifier now short-circuits when `owned_and_readable`
  contains exactly one scope — argmax over a single candidate is
  trivially that scope. Saves ~90 ms on every butler (voice) turn,
  since Tina only owns `house`. Assistant (3 owned scopes) still
  classifies.

### Latency impact (measured on N150 with e5-large)
- Butler voice critical path: ~90 ms → ~1 ms on cache hit
- Butler voice total per-turn overhead: ~180 ms → ~0-90 ms
  (write-path classifier removed unconditionally, read-path when
  cached)
- Assistant telegram: unchanged on first call, ~90 ms saved on any
  repeat of the same user text

## 0.8.2 — 2026-04-21 — Post-deploy hotfixes (model + trust bypass)

### Fixed
- Embedding model name — `intfloat/multilingual-e5-small` is not in
  fastembed 0.4's supported-model catalog (only `-large` ships). v0.8.1
  was silently booting in degraded mode with the "model not supported"
  error on first init. Switched `_DEFAULT_MODEL_NAME` (and the
  setup-configs pre-warm invocation) to `intfloat/multilingual-e5-large`
  so the classifier comes up non-degraded. The large variant is ~500 MB
  (vs ~200 MB for small) — still well within N150 capacity.
- Write-path trust bypass — when the channel's trust tier filters out
  every scope the agent owns (`scopes_owned ∩ readable == []`), the
  write path was falling back to `default_scope` and persisting the
  exchange into a scope the channel cannot see. Now skips the write
  entirely. Regression test
  `TestWritePath::test_write_skipped_when_owned_and_readable_empty`
  covers this. Observed in v0.8.1: webhook → assistant turn was logging
  `scope_route ... active=[house] write=personal`.

## 0.8.1 — 2026-04-21 — Debian base image (onnxruntime compatibility)

### Changed
- Base image migrated from `amd64-base-python:3.12-alpine3.22` to
  `amd64-base-debian:bookworm`. Alpine ships no `musllinux` wheels for
  `onnxruntime` (a transitive dep of `fastembed>=0.4`), forcing a
  from-source build that failed under the addon's build constraints.
  Debian/glibc pulls the prebuilt `manylinux_2_17_x86_64` wheel.
- Container Python is now 3.11 (Debian bookworm default), down from
  3.12. Casa's code uses only 3.9+ features; dev-host test suite runs
  on 3.11.9 so container Python now matches.
- Python deps installed into a virtualenv at `/opt/casa/venv` (PEP 668
  "externally managed" environment on Debian prevents direct `pip
  install` to system site-packages). The venv's `bin/` is prepended to
  `PATH` so all `python3` invocations in s6 service + setup scripts
  resolve to the venv interpreter without script changes.

### Dependencies
- Node.js 18 (Debian bookworm apt) replaces Alpine's nodejs (identical
  major version; `@anthropic-ai/claude-code` engine constraint
  `>=18.0.0` still satisfied).

### Image size
- Uncompressed image grows by ~200-350 MB (Debian base + Python stack
  larger than Alpine). No impact on the N150's 120 GB storage.

## 0.8.0 — 2026-04-20 — Phase 3.2: Domain scope runtime

### Added
- Domain scope as the authoritative memory visibility layer. Four scopes
  ship by default (`personal`, `business`, `finance`, `house`) declared in
  `/addon_configs/casa-agent/policies/scopes.yaml` with editable
  natural-language descriptions and `minimum_trust` tiers.
- `ScopeRegistry` with a local `fastembed` embedding model
  (`intfloat/multilingual-e5-small`, ~200 MB, downloaded to `/data/fastembed/`
  on first boot). Scores user text per readable scope; fan-out reads above
  threshold; end-of-turn classifies the full exchange for the write target.
- Per-scope Honcho session topology: `{channel}:{chat_id}:{scope}:{role}`.
  Per-turn telemetry line `scope_route role=... channel=... active=[...]
  write=... (t=Nms)`.
- `memory.default_scope` field in resident `runtime.yaml` (required for
  residents with `scopes_readable`; forbidden on executors).
- `channel_trust()` now returns a canonical token; `channel_trust_display()`
  preserves the human-readable form for the `<channel_context>` prompt block.

### Changed
- **Breaking (internal): memory session topology.** Pre-v0.8.0 Honcho /
  SQLite sessions (keyed `{channel}:{chat_id}:{role}`) are orphaned. Fresh
  scoped sessions accumulate from turn 1 after upgrade; prior transcripts
  remain visible in the Honcho dashboard but Casa does not read from them.
- Butler `disclosure.yaml` override shortened — `categories: {}`,
  `safe_on_any_channel` and `deflection_patterns` inherit from the shared
  `standard` policy. Scope-at-retrieval enforcement makes the confidential
  category listing redundant for Tina.
- `Agent` constructor now takes `scope_registry` as a required argument.

### Environment
- New: `CASA_SCOPE_THRESHOLD` (default `0.35`). Raise to make routing
  stricter (fewer scopes pulled per turn); lower to be more inclusive.

### Dependencies
- `fastembed>=0.4,<0.5`.

### Non-goals carried forward
- No scope-aware tool gating — 3.x follow-up.
- No legacy memory migration — cold start on upgrade.
- No remote embedding provider — local only in v0.8.x.
- No `/finance ...` user-prefix syntax.

### Deployment note
- First boot downloads the embedding model (~200 MB, ~30 s). Subsequent boots
  reuse `/data/fastembed/`. Offline first-boot degrades gracefully (fan-out
  to every readable scope) with a WARNING log.

## 0.7.0 — 2026-04-20 — Agent-definition refactor (Spec X / Phase 4.x)

### Added

- **Per-agent directory format.** Each resident and executor lives in its
  own directory under `/addon_configs/casa-agent/agents/<role>/` with
  one file per concern: `character.yaml`, `voice.yaml`,
  `response_shape.yaml`, `runtime.yaml`, and optionally
  `disclosure.yaml`, `delegates.yaml`, `triggers.yaml`, `hooks.yaml`.
  Flat `agents/<role>.yaml` files are no longer loaded.
- **Strict-mode loader** (`agent_loader.py`) with JSON Schema
  validation: unknown field / unknown file / missing required
  `schema_version` / unknown `disclosure.policy` all fail-fast at boot.
- **Shared policy library** (`policies.py`) resolves
  `disclosure.policy: <name>` references against
  `/addon_configs/casa-agent/policies/disclosure.yaml`.
- **Per-agent trigger registry** (`trigger_registry.py`) replacing the
  global heartbeat block. Residents declare their own
  `interval` / `cron` / `webhook` triggers in `triggers.yaml`.
- **`HOOK_POLICIES` registry + `resolve_hooks`** in `hooks.py`; per-agent
  hook wiring via `hooks.yaml`, resolved at `Agent.__init__`. Default
  bundle (`block_dangerous_bash` + `path_scope`) applies when the file
  is absent or empty.
- **`config_git`** module — initialises a local git repo under
  `/addon_configs/casa-agent/` and snapshots manual edits on every boot
  for free history / rollback.

### Removed (breaking)

- Flat agent YAMLs: `defaults/agents/{assistant,butler,subagents}.yaml`
  and `defaults/agents/executors/finance.yaml`.
- Global schedules/webhooks files: `defaults/schedules.yaml`,
  `defaults/webhooks.yaml`.
- All one-shot migrations from `setup-configs.sh` (`migrate_rename`,
  `migrate_memory_fields`, `migrate_voice_fields`,
  `migrate_disclosure_clause`, `migrate_scope_metadata`,
  `migrate_channels`, `migrate_executor_rename`, `migrate_mcp_allowed`)
  and their six regression test modules. Migrations are no longer
  needed — the new file format is the only format the loader
  understands.
- `config.ROLE_ALIASES`, `config._normalize_role`,
  `config.load_agent_config`, the `_build_*` helpers, and the legacy
  `name` / `personality` / `description` fields on `AgentConfig`.
- `casa_core._log_subagents_deprecation_if_present`,
  `casa_core._load_agents_by_role`, `casa_core.init_heartbeat_defaults`,
  `casa_core.build_heartbeat_message`, and the inline global heartbeat
  scheduler block.
- `hooks.AGENT_PATH_RULES`, `hooks._check_path_scope`,
  `hooks.make_path_scope_hook` (replaced by the parameterized
  `make_path_scope_hook_v2`).

### Migration

No production users — this is a hard cut. Existing installations will
find their old flat YAMLs unread; seed the new tree by deleting
`/addon_configs/casa-agent/agents/*.yaml` and letting
`setup-configs.sh` copy the bundled directory defaults on next boot.

## 0.6.2 — 2026-04-20 — Phase 3.4: disabled-executor pattern (plumbing)

### Added

- **Glob-based executor seeding.** `setup-configs.sh` now discovers
  `defaults/agents/executors/*.yaml` at first boot, seeding each to the
  user's config directory if absent. Adding a new bundled-disabled
  executor is now a single-file drop — no Casa code edit. Residents
  and top-level config files stay hand-enumerated (they are individually
  required by startup). Mirrored into
  `test-local/init-overrides/01-setup-configs.sh`.

- **`n8n-workflows` MCP server registration.** New
  `_maybe_register_n8n(mcp_registry, env=None)` helper in
  `casa_core.py`, wired into `main()` after the existing
  `homeassistant` block. When `N8N_URL` is set, registers the
  `n8n-workflows` HTTP MCP server (with `Authorization: Bearer ...`
  header if `N8N_API_KEY` is also set). Generic shared infrastructure:
  any agent (resident or executor) that declares `n8n-workflows` in
  `mcp_server_names` can reach it; per-agent tool whitelisting via
  `tools.allowed` governs which workflows each agent may actually
  invoke.

- **Executor enabled/disabled summary log.**
  `ExecutorRegistry.load()` now emits one INFO line at the tail of
  loading: `Executors: enabled=[...] disabled=[...]`. Operator
  visibility into the executor landscape and a stable grep target for
  future automation.

- **User-facing docs.** New "Enabling a bundled-disabled executor"
  section in `DOCS.md` walks users through flipping
  `enabled: false` → `true` on `finance.yaml` (or any future
  disabled-by-default executor YAML) and restarting the addon.

### Tests

- 4 new unit tests in `tests/test_n8n_registration.py` covering the
  helper's env-gated behavior (URL unset, URL set with/without API
  key, whitespace-only URL).
- 2 new unit tests in `tests/test_executor_registry.py::TestSummaryLog`
  for the summary log output (mixed state + empty state).
- New `test-local/e2e/test_delegation.sh` with three scenarios
  (D-1/D-2/D-3) proving bundled-disabled, flip-to-enabled, and
  config-not-code discovery contracts. All assertions are log-line +
  file-presence; tool-behavior contracts remain at the unit level
  (`test_delegate_to_agent.py`) because the offline mock SDK doesn't
  dispatch tool calls.

### Non-breaking

- Default-env startup is unchanged for users who don't set `N8N_URL`
  and don't edit `finance.yaml`. `finance` continues to be bundled as
  `enabled: false`.

### Deferred

- `finance`'s tool whitelist, prompt polish, and n8n workflow bindings
  ship in a separate capabilities session. Plumbing only.

## 0.6.1 — 2026-04-20 — Phase 3.1 follow-ups: role-over-name + 3.4 prerequisites

### Fixed

- **Executor role/name cleanup.** v0.6.0 shipped the Alex executor with
  `role: alex`, conflating human-facing name and functional role. Every
  other resident uses `role=<function>` (assistant, butler) with
  `name=<persona>` (Ellen, Tina). Renamed the bundled default file
  `defaults/agents/executors/alex.yaml` → `finance.yaml`, set
  `role: finance` + `name: Alex`. New one-shot `migrate_executor_rename`
  in `setup-configs.sh` moves any existing
  `/addon_configs/casa-agent/agents/executors/alex.yaml` → `finance.yaml`
  and patches the `role:` + `name:` lines. Idempotent by file existence.
  Delegation API change: `delegate_to_agent(agent="finance", ...)` is
  now the canonical invocation; `agent="alex"` returns `unknown_agent`.

### Added

- **Phase 3.4 prerequisite: MCP registry wiring in
  `_build_executor_options`.** v0.6.0 hardcoded `mcp_servers={}` for
  executor invocations, which would have left a future-enabled Alex
  with zero MCP tools. `init_tools()` now accepts an optional
  `mcp_registry: McpServerRegistry`; when passed, `_build_executor_options`
  resolves `cfg.mcp_server_names` through it. `casa_core.main()`
  passes the registry. Legacy 3-arg `init_tools` still works (degrades
  to empty mcp_servers) for test harnesses.

- **Phase 3.4 prerequisite: Ellen can now call `delegate_to_agent`.**
  The Claude Agent SDK blocks MCP tools unless explicitly whitelisted
  by their `mcp__<server>__<tool>` name. v0.6.0's bundled
  `assistant.yaml::tools.allowed` didn't list
  `mcp__casa-framework__delegate_to_agent`, so Ellen refused to invoke
  it even though the tool was registered. Added both
  `mcp__casa-framework__delegate_to_agent` and
  `mcp__casa-framework__send_message` to the bundled default. New
  `migrate_mcp_allowed` one-shot in `setup-configs.sh` backfills both
  entries into existing users' `assistant.yaml::tools.allowed`, gated
  by `# casa: mcp-tools v1` marker. Handles inline list (`allowed: [...]`)
  and block list (`allowed:\n  - ...`) forms; preserves existing entries.

### Notes

- No deployment steps beyond `ha apps update`. Both new migrations
  (`migrate_executor_rename`, `migrate_mcp_allowed`) are idempotent.
  The rename migration only fires if you upgraded to v0.6.0 and had
  the Alex executor seeded at `/addon_configs/casa-agent/agents/
  executors/alex.yaml` (which is the case for anyone who ran v0.6.0).
- Updated Ellen's personality (`Delegation:` section) to reference
  `delegate_to_agent(agent="finance")` instead of the deprecated
  "spawn Alex subagent" / "spawn automation-builder subagent" wording.
  Non-functional prose — Ellen's behaviour is governed by the tool
  surface, not the prose.

---

## 0.6.0 — 2026-04-20 — Phase 3.1: Residents, Executors, delegate_to_agent

### Added

- **Phase 3.1: Residents, Executors, `delegate_to_agent`**
  (spec `docs/superpowers/specs/2026-04-20-3.1-residents-executors-delegation.md`,
  taxonomy foundation `2026-04-20-agent-taxonomy.md`).
  - Tier 1 resident loader relaxed: any `agents/<role>.yaml` with
    non-empty `channels:` loads as a resident. No code change required
    to add new user-defined residents.
  - Tier 2 executor loader + `ExecutorRegistry` at
    `casa-agent/rootfs/opt/casa/executor_registry.py`. Scans
    `agents/executors/*.yaml`; rejects Tier-1-shaped YAMLs; honours
    `enabled: false` gating.
  - New framework tool `delegate_to_agent(agent, task, context, mode)`
    in `casa-framework` MCP. Sync-with-degradation (60s
    `asyncio.wait` — never `asyncio.wait_for`) + explicit async. Late
    completions post a bus NOTIFICATION to the delegating resident;
    Ellen's NOTIFICATION branch synthesizes a fresh turn and replies
    via the origin channel.
  - In-flight delegations persisted to `/data/delegations.json`;
    orphaned records on restart fire a synthetic "lost on restart"
    NOTIFICATION exactly once.
  - Alex ships bundled at `defaults/agents/executors/alex.yaml` with
    `enabled: false`. Becomes functional when 3.4 registers the n8n MCP.
  - YAML metadata migration: `scopes_owned` + `scopes_readable` on
    Ellen + Tina, gated by `# casa: scopes v1` marker. Fields parse at
    runtime but are **unread in v0.6.0** — scope-aware retrieval ships
    in 3.2.

### Changed

- `subagents.yaml` entries (`automation-builder`, `plugin-builder`,
  inline `alex`) are no longer loaded. Re-classified as Tier 3
  Builders (deferred spec). One-time deprecation log on startup if
  the file is present; no auto-migration.

### Fixed

- Upgrade-path regression: pre-2.1 YAMLs that went through
  `migrate_rename` (ellen.yaml → assistant.yaml / tina.yaml →
  butler.yaml) lacked a `channels:` key. Task 3's tightened Tier 1
  loader would have skipped them at startup. New idempotent
  `migrate_channels` one-shot in `setup-configs.sh` backfills
  sensible defaults (`[telegram, webhook]` for assistant;
  `[ha_voice]` for butler) on upgrade, gated by `# casa: channels v1`
  marker. Non-destructive: existing channels are preserved.

### Notes

- No deployment steps for existing N150 users beyond `ha apps update`.
  The scope-metadata and channels-backfill migrations are idempotent;
  running an older Casa after upgrading YAMLs is a no-op (extra
  fields ignored by pre-3.1 loaders).

---

## 0.5.10 — 2026-04-20 — Phase 5.7: Close public dashboard

### Fixed
- On the public hostname (`agent.oudekamp.bonzanni.casa`, addon-nginx
  `:18065`), `GET /` no longer proxies to casa-core and returns
  the dashboard HTML. It now returns a static **nginx 404** via an
  exact-match `location = /` rule placed immediately before the
  existing catch-all. No Python hop, no aiohttp handler invocation.
  The dashboard remains reachable via HA ingress on the separate
  `listen $INGRESS_PORT` server block.
- Test-infra gap from v0.5.9: `test-local/mock-claude-sdk/` was
  missing `ProcessError`. v0.5.9's resume-resilience code added a
  `ProcessError` import in `agent.py`, which unit tests tolerated
  (they resolved the real pip-installed SDK on the host) but the
  Docker e2e image crashed at container start with `ImportError:
  cannot import name 'ProcessError'`. Mock now mirrors the real
  SDK's 3-arg `(message, exit_code, stderr)` signature so tests
  that construct `ProcessError(..., exit_code=N)` work unchanged.
  Unblocks all local e2e on this release; no production impact.

### Tests
- New e2e: `test-local/e2e/test_external_surface.sh` — maps both
  ingress (`:8080`) and external (`:18065`) ports, asserts three
  outcomes:
  1. Ingress `GET /` → 200 (dashboard still alive internally).
  2. External `GET /` → 404 (new contract).
  3. External `GET /healthz` → 200 (uptime contract pinned).
- Wired into `.github/workflows/qa.yml` e2e-fast as the final step.
- `test-local/e2e/common.sh::start_container()` now accepts an opt-in
  `EXT_PORT` env var that maps a second host port to container `:18065`.
  Default behaviour unchanged — all eight pre-existing e2e scripts
  still map only `:8080`.

### Not changed
- `casa-core` aiohttp routes — `app.router.add_get("/", dashboard)`
  stays intact.
- Ingress server block — the `listen $INGRESS_PORT` block is
  untouched; HA-authenticated dashboard access works as before.
- All other external routes — `/invoke/*`, `/webhook/*`,
  `/api/converse`, `/api/converse/ws`, `/telegram/update` continue to
  match the catch-all `location /` and hit their existing gates
  (HMAC, secret token, anonymous `/healthz`).
- Nginx `/terminal/` rule on the external block — pre-existing 404
  unchanged.

---

## 0.5.9 — 2026-04-19 — Phase 5.8: SDK session resume resilience

### Added
- **`SessionRegistry.clear_sdk_session(channel_key)`** — drops only
  the `sdk_session_id` field from a registry entry; keeps
  `last_active` and `agent` intact so the session sweeper and
  downstream consumers still see the scope. Idempotent and no-op on
  missing keys.
- **`Agent._process` resume fallback.** When
  `claude_agent_sdk.ProcessError` fires on a turn that attempted to
  resume a prior SDK session (`resume_session_id` was set), Casa now:
  1. Logs a `WARNING` — `SDK resume failed (key=<k> sid=<sid>); clearing and retrying fresh`.
  2. Clears the stale `sdk_session_id` via `clear_sdk_session`.
  3. Rebuilds `ClaudeAgentOptions` with `resume=None` via
     `dataclasses.replace`.
  4. Re-runs `retry_sdk_call(_attempt_sdk_turn)` once. On success the
     fresh `sdk_session_id` is persisted via the existing `register`
     path.
  If `resume_session_id` was `None` or the fresh retry also raises
  `ProcessError`, the exception propagates to the caller — no
  infinite loop.

### Fixed
- `/data/sessions.json` persists across `ha apps rebuild` (bind-
  mounted), but the claude CLI's own session state under
  `/root/.claude/` does NOT (container-local). Every rebuild
  therefore orphaned every `sdk_session_id` recorded in
  `sessions.json`. Subsequent resume attempts crashed claude CLI
  with exit 1 + `No conversation found with session ID: <uuid>`,
  manifesting in Casa as `ProcessError` and a user-facing `sdk_error`
  persona line. Tripped by the v0.5.8 post-deploy `voice-sse` smoke
  probe (`voice:probe-scope` → butler agent Tina). The fix recovers
  transparently: the first post-rebuild turn on any stale scope
  logs one `SDK resume failed` warning and proceeds on a fresh
  session. Agent memory is unaffected (Honcho / SQLite memory is
  keyed on user peer + channel key, not `sdk_session_id`).

### Tests
- New: `tests/test_session_registry.py::TestClearSdkSession` — 4
  tests (field removal, metadata preservation, missing-key no-op,
  disk persistence).
- New: `tests/test_agent_process.py::TestResumeResilience` — 5
  tests (stale resume cleared and retried, second attempt sees
  `resume=None`, no-resume re-raises, double-ProcessError re-raises
  with cleared stale id, fallback logs a single prefixed WARNING).
- Count: 426 → 435 unit tests green.

### Not changed
- `retry.py` — stays a pure policy module. `ProcessError` remains
  classified as `ErrorKind.UNKNOWN` (not in `RETRY_KINDS`). The
  fallback runs at the outer `Agent._process` layer.
- `error_kinds.py` — no classification changes.
- `SessionSweeper` — TTL-based eviction unchanged.
- `sessions.json` persistence — unchanged shape. The fallback
  mutates entries only when it fires.
- Memory providers — unchanged. Honcho / SQLite remain orthogonal
  to `sdk_session_id`.

### Plan / spec
- Spec: `docs/superpowers/specs/2026-04-19-5.8-session-resume-resilience.md`
- Plan: `docs/superpowers/plans/2026-04-19-5.8-session-resume-resilience.md`

## 0.5.8 — 2026-04-19 — Phase 5.5: log hygiene

### Added
- **aiohttp cid middleware** in new `casa_core_middleware.py`
  (`cid_middleware`). Every inbound HTTP request now gets an 8-char
  lowercase-hex correlation id at ingress. Operators may override
  via `X-Request-Cid` header (accepts 8–32 hex chars,
  case-insensitive; uppercase normalised to lowercase; invalid shape
  silently ignored in favour of a fresh allocation). The middleware
  binds `log_cid.cid_var` with a scoped ContextVar token for the
  handler's task. `asyncio.create_task` snapshots contextvars, so
  any task the handler spawns (notably `bus.request`'s inner
  dispatch task) inherits the same cid — access-log lines, ingress
  INFO lines, and the `turn_done` budget summary all share one cid.
- **Custom aiohttp `AccessLogger`** (`CasaAccessLogger`) in the same
  module. Emits one `logger.info(...)` on the `casa.access` logger,
  which picks up Casa's installed root handler (5.2-H) — so access
  lines share the active formatter (human/JSON), carry the current
  cid, and run through `RedactingFilter`. Line format:
  `access method=<M> path=<P> status=<S> duration_ms=<D> bytes=<B>`.
  Replaces aiohttp's default CLF output (which double-stamped the
  timestamp and always logged `cid=-`). Wired into `AppRunner` via
  `access_log_class=CasaAccessLogger,
  access_log=logging.getLogger("casa.access")`.
- **Telegram `_handle` inherit-or-allocate cid** — unifies webhook
  and polling transport. When the aiohttp middleware has bound
  `cid_var` (webhook mode via `/telegram/update`), PTB's `_handle`
  inherits it via contextvars. When running in polling mode (no HTTP
  ingress), `_handle` allocates a fresh cid via `new_cid()` as
  before. Pattern:
  `cid = cid_var.get(); cid = cid if cid != "-" else new_cid()`.

### Changed
- **[BREAKING] `LOG_FORMAT` default flipped from human to JSON.**
  Unset `LOG_FORMAT` now yields JSON; any value other than `"human"`
  (case-insensitive — includes typos like `LOG_FORMAT=JSON` or
  `LOG_FORMAT=true`) also resolves to JSON. **For prior behaviour,
  set `LOG_FORMAT=human`** in the addon options. Casa does not
  consume its own logs, so no internal callers break; only operators
  reading raw `docker logs` by eye are affected. Motivation:
  operators running a log aggregator (Loki+Promtail, Vector, Fluent
  Bit) no longer need to opt-in to JSON — the default is now
  parseable out of the box.
- **Addon nginx `error_log` level `info` → `warn`** in
  `casa-agent/rootfs/etc/s6-overlay/scripts/setup-nginx.sh`. The
  "closed keepalive connection" info-spam (one line per external
  request at idle) disappears; real config errors and upstream
  timeouts still surface. This is the fix the dropped-as-YAGNI 5.6
  (NPM upstream connection reuse) was reaching for — at zero
  operational debt versus a template fork or migration-off-NPM.
- **ttyd `-d 0`** in `svc-ttyd/run`. Routine connection/session
  chatter silenced; fatal errors stay visible. Only affects
  deployments that enable the `web_terminal` addon option.
- **HTTP handler cid reads** — `webhook_handler` (`casa_core.py`),
  `invoke_handler` (`casa_core.py`), voice SSE handler
  (`channels/voice/channel.py:_sse_handler`), Telegram webhook POST
  `telegram_update_handler` — all now read `request["cid"]` instead
  of calling `new_cid()` inline. `invoke_handler` additionally
  primes `payload["context"]["cid"]` from `request["cid"]` before
  calling `build_invoke_message`, so the builder's defensive
  fallback is a no-op on the normal HTTP path.

### Infra
- **Bashio ANSI strip (defensive).** Every s6 `run`/`finish` script
  plus `setup-configs.sh`, `setup-nginx.sh`, `validate-config.sh`,
  and `sync-repos.sh` now exports `BASHIO_LOG_NO_COLORS=true` and
  `NO_COLOR=1` at the top (after the shebang, before any `bashio::*`
  call). Idempotent — re-sourcing on s6 respawn costs nothing.
  Baseline ANSI count on v0.5.7 prod was not captured this session
  (SSH-agent auth failure); defensive exports ship regardless so
  future bashio/s6 TTY-detection changes cannot reintroduce ANSI.

### Not changed
- **`bus._dispatch` cid binding** — the defensive
  `cid_var.set(msg.context["cid"])` from 5.2-H stays. It's a no-op
  when the middleware already bound the same cid; authoritative
  when a non-HTTP caller sets a different cid in `context` (e.g.
  scheduler heartbeat). Removing it would silently break non-HTTP
  cid paths.
- **`CidFilter` utility class** in `log_cid.py` remains available
  for manual LogRecord construction. Not auto-wired — the
  LogRecord factory in `install_logging` handles cid tagging at
  creation time.
- **Voice WS per-utterance cid allocation** stays manual. One WS
  connection, many utterances, one cid per utterance per the 5.2-H
  contract. The middleware allocates a connection-level cid for the
  WS upgrade request; the utterance loop then overrides per frame
  via `new_cid()`.
- **Scheduler heartbeat cid** stays. It runs in the scheduler
  task, no HTTP request, so the middleware never sees it.
  `bus._dispatch` picks up `context["cid"]` as today.
- **`build_invoke_message` defensive fallback** stays. On the
  normal HTTP path `invoke_handler` primes `payload.context.cid`
  from the middleware, so the fallback is a no-op. Non-HTTP
  callers (future) can still rely on it.

### Tests
- New: `tests/test_cid_middleware.py` (12 tests across
  `TestDefaultAllocation`, `TestHeaderOverride`,
  `TestContextVarBinding`, `TestExceptionSafety`,
  `TestSpawnedTaskInherits`) — uses Casa's existing
  `TestClient(TestServer(app))` pattern, no `pytest-aiohttp`
  dependency.
- New: `tests/test_casa_access_logger.py` (6 tests across
  `TestFormat`, `TestCidInRecord`, `TestLoggerWiring`,
  `TestJsonMode`).
- Extended: `tests/test_telegram_split.py::TestInheritOrAllocateCid`
  (2 tests — inherits pre-bound cid, allocates when default).
- Extended: `tests/test_log_cid.py::TestFormatDefaultIsJson`
  (2 tests — unset env yields JSON, `LOG_FORMAT=human` yields
  human). Existing `test_human_format_default` updated to set
  `LOG_FORMAT=human` explicitly (preserves human-format
  coverage).
- Updated: `tests/test_voice_channel_sse.py` — 3 fixture
  `web.Application()` calls now register `cid_middleware` so
  handlers see `request["cid"]`.
- Count: 403 → 425 (+22 new/extended tests).

### Plan / spec
- Spec: `docs/superpowers/specs/2026-04-19-5.5-log-hygiene.md`
- Plan: `docs/superpowers/plans/2026-04-19-5.5-log-hygiene.md`

## 0.5.7 — 2026-04-18 — Phase 5.3 infra hygiene (partial: items A + K)

### Added
- `.dockerignore` at the repo root. Excludes `**/build/`,
  `**/*.egg-info/`, `**/.eggs/`, `**/dist/`, `**/__pycache__/`,
  `**/*.pyc`, `**/.pytest_cache/`, `**/.mypy_cache/`,
  `**/.ruff_cache/`, `.spike-venv/`, `.venv/`, `venv/`, `.git/`,
  `.worktrees/`, `docs/`, `.claude/`, `.env`, `.env.*`, and
  `test-local/options.json`. Docker does NOT honor `.gitignore`, so
  host-side pip-install artifacts (seen in 5.2 item F: stale
  `test-local/mock-claude-sdk/build/lib/` COPYd into the test image
  and masked product changes) now can't poison Docker builds. This
  closes the backlog item filed during 5.2-F. Verified with
  `docker buildx build --progress=plain` on a tiny `COPY . /tmp`
  probe: **context transfer 360.33 MB → 12.22 kB** (the bulk was
  the `.spike-venv` virtualenv under `.gitignore` that Docker was
  shipping to the daemon on every build).

### Changed
- `test-local/Dockerfile.test` — `ARG BUILD_FROM` now pins the HA
  base image by sha256 digest
  (`ghcr.io/home-assistant/amd64-base-python@sha256:cb37b54…`)
  instead of the floating `3.12-alpine3.22` tag. Pinned 2026-04-18
  against HA base 2026.04.0 (`org.opencontainers.image.created
  2026-04-13`). Refresh process documented in-file via a block
  comment (`docker buildx imagetools inspect … | jq
  '.manifest.digest'`). Rationale: pre-pin, a silent HA base
  republish under the same tag could change test behaviour with no
  record in our repo; CI results stop being reproducible across
  time. Spec §4 / decision H4.
  - Scope note: production `casa-agent/Dockerfile` stays unpinned
    per spec §2 non-goal / H3. It inherits `BUILD_FROM` from the HA
    builder pipeline, which pins its own base per release.
    Re-pinning would fight the HA release machinery.

### Deferred
- **Item J — narrow AppArmor `file,` rule** (spec §3). Requires the
  complain-mode discovery loop on real Linux hardware with AppArmor
  enabled (the N150 production box). Casa's dev machine is Windows
  / Docker Desktop; kernel AppArmor is unreachable. Spec decision
  H1 explicitly warns against shipping a theoretical path list
  without the `aa-logprof` / kernel-audit capture. Left on the 5.3
  roadmap entry; no code change on this release.

### Plan / spec
- Spec: `docs/superpowers/specs/2026-04-18-infra-hygiene-5.3.md`
- No separate plan file (mechanical sweep; two edits).

## 0.5.6 — 2026-04-18 — Phase 5.2 item I: inbound rate limiting

### Added
- `rate_limit.py` — pure policy module. `TokenBucket`: single-key
  refill-on-check bucket with an injectable clock; `capacity<=0`
  short-circuits every `check()` to allowed. `RateLimiter`:
  `dict[str, TokenBucket]` with lazy bucket creation AND a
  disabled-state short-circuit so disabled limiters never grow the
  per-key dict. `RateDecision` (frozen dataclass): `allowed`,
  `should_notify` (fires on the FIRST reject after any allow — the
  signal Telegram uses for its reply-once-per-streak semantic),
  `retry_after_s`. `rate_limit_response(limiter, key)` — aiohttp 429
  helper returning `None` (allowed) or a `web.Response` with
  `Retry-After` integer-seconds header rounded up from the underlying
  bucket's `retry_after_s`.
- Three `RateLimiter` instances constructed in `casa_core.main()`:
  `TELEGRAM_RATE_PER_MIN` (default 30) keyed on `chat_id`,
  `VOICE_RATE_PER_MIN` (default 20) keyed on `scope_id`,
  `WEBHOOK_RATE_PER_MIN` (default 60) on a single shared `"global"`
  key across `/webhook/{name}` + `/invoke/{agent}` per spec §8.2.
  All three env vars via the `_env_int_or` helper from item G with
  `min_value=0`; setting the value to 0 disables the limit.
- Startup log line `Rate limits: telegram=30/min, voice=20/min,
  webhook=60/min` (values rendered as `off` when the channel's limit
  is disabled).
- Centralised `telegram.*` stub install in `tests/conftest.py` with
  canonical `_FakeNetworkError` / `_FakeTimedOut` / `_FakeTelegramError`
  classes. Previously `tests/test_telegram_reconnect.py` and
  `tests/test_telegram_split.py` each installed their own stubs with
  locally-defined exception classes — pytest's alphabetical discovery
  could let one file's classes "win" and diverge from what production
  code would catch. Now all Telegram-adjacent test files share the
  same class identities regardless of load order.

### Changed
- `TelegramChannel.__init__` gains an optional
  `rate_limiter: RateLimiter | None = None` kwarg. In `_handle`,
  immediately after deriving `chat_id` (and before `_start_typing`),
  the channel consults the limiter. On reject it drops the message
  and — only on `should_notify=True` — sends one
  `"Slow down — try again in a minute."` reply via
  `bot.send_message` (wrapped in a try/except that logs at DEBUG and
  does not raise). Pre-existing callers that don't pass a limiter
  keep unlimited behaviour.
- `VoiceChannel.__init__` gains the same `rate_limiter` kwarg. On
  SSE the handler opens a 200 SSE stream and writes one
  `event: error` with `kind=rate_limit` + persona line from
  `voice_errors["rate_limit"]` (falls back to `_DEFAULT_ERROR_LINES`);
  no `event: done` is emitted. On WS `_run_ws_utterance` sends one
  `{type:"error", utterance_id, kind:"rate_limit", spoken:…}` and
  returns — no `bus.request`, no stream open.
- `casa_core.webhook_handler` and `casa_core.invoke_handler` each
  call `rate_limit_response(webhook_rate_limiter, "global")` as the
  FIRST step (before HMAC verification). On reject returns 429 with
  JSON body `{"error": "rate_limited"}` + `Retry-After` integer
  seconds. Rationale for before-HMAC: an unauthenticated flood still
  burns zero Claude quota (the real protection) and gets throttled
  cheaply without the HMAC hash.
- `tests/test_telegram_reconnect.py` aliases its local
  `_FakeNetworkError` / `_FakeTimedOut` / `_FakeTelegramError` names
  from `sys.modules["telegram.error"]` so the exceptions it raises
  via AsyncMock `side_effect=` match the class `channels.telegram`'s
  `except NetworkError:` catches, regardless of whether its own stub
  install ran first or conftest's did.

### Tests
- `tests/test_rate_limit.py` — 16 unit tests across
  `TestTokenBucket` (8), `TestRateLimiter` (5), `TestRateLimitResponse` (3).
- `tests/test_telegram_rate_limit.py` — 6 integration tests driving
  `TelegramChannel._handle` against a fake Update: burst under cap
  reaches bus; reject emits exactly ONE reply then drops silently;
  per-`chat_id` isolation; `capacity=0` disables; pre-existing
  no-limiter callers unaffected; rejected messages don't start the
  typing indicator.
- `tests/test_voice_channel_sse.py::TestRateLimit` — 3 tests
  (exhaust+reject emits `event: error kind=rate_limit` with no
  `event: done`; `capacity=0` is unlimited; per-`scope_id` isolation).
- `tests/test_voice_channel_ws.py::TestRateLimit` — 2 tests
  (exhaust+reject emits `type:error kind=rate_limit` on the socket
  with no `type:done`; `capacity=0` is unlimited).
- `tests/test_casa_core_helpers.py::TestWebhookRateLimit` — 4 tests
  (burst-then-429 with integer `Retry-After`, global bucket shared
  across `/webhook/*` and `/invoke/*`, `capacity=0` disables, 429
  body shape).
- 403 unit/integration tests green. E2E smoke, invoke-sessions, and
  concurrency scenarios still green; "Rate limits: …" startup line
  verified in a standalone container for both the default and
  all-off paths.

### Not changed
- Bus, agent, retry, memory, session_registry, session_sweeper,
  log_cid, log_redact, mcp_registry, config, tools, channel_trust,
  telegram_supervisor, voice/{session,prosodic,tts_adapter} — all
  untouched. Rate limiting is a pre-filter at each ingress; nothing
  downstream of the bus sees the reject path.
- No dashboard row, no `/metrics` endpoint (spec §5.3 precedent
  carries to §8). Logs + HTTP status codes + the Telegram reply
  text are the operator-facing surface.
- No per-agent-role override on the webhook bucket (spec §8.2 is
  explicit: "all names and agents share one bucket").
- No persistence of bucket state across restarts. A restart resets
  all three buckets to full capacity; this is intentional —
  webhook also requires valid HMAC as primary authN; rate limit is
  defense-in-depth against accidental self-DoS + a flooded
  leaked-secret.
- No eviction of idle buckets from the per-key dict. On a
  single-user Casa the set of unique keys is bounded by real
  Telegram chats + voice devices + 1 (global webhook). Add an
  idle-bucket sweep only if the dict footprint becomes a concern.
- No E2E shell scenario. The webhook 429 path is trivially
  reproducible (`for i in $(seq 1 61); do curl -X POST …/webhook/t; done`
  → last response is 429) but faithfully replaying per-chat_id
  Telegram rate limits or per-scope_id voice rate limits from a
  shell harness is out of proportion to value. Matches item D/E/G
  precedent.

## 0.5.5 — 2026-04-18 — Phase 5.2 item G: session rotation + cleanup

### Added
- `session_sweeper.py` — `SessionSweeper`: pure async policy module
  that runs a periodic TTL sweep over `SessionRegistry`. Every 6 h
  (hard-coded per spec R5) it iterates `_data` under the 5.1 lock,
  drops entries whose `last_active` is older than
  `SESSION_TTL_DAYS` (default 30), and — for `webhook:*` entries
  whose scope_id parses as a UUID (the one-shot pattern fabricated
  by `build_invoke_message`) — applies the shorter
  `WEBHOOK_SESSION_TTL_DAYS` (default 1). Non-UUID webhook scopes
  (e.g. deliberately-pinned `webhook:ha-automation-daily`) keep the
  standard TTL. Unparseable / missing `last_active` is treated as
  garbage and evicted.
- `_prune_sdk_session()` helper — forward-compat seam: `getattr`
  lookup of `claude_agent_sdk.delete_session`; no-op when absent
  (today), one-line flip when Anthropic's SDK grows it. Exceptions
  swallowed at DEBUG — the local eviction is source of truth.
- `casa_core._env_int_or` — clamping int-from-env helper matching
  `retry._env_int`'s shape; kept local until a second caller
  appears (item I will reuse it — §9.3), then promote to `env.py`.

### Changed
- `casa_core.main()` constructs a `SessionSweeper` immediately after
  the `SessionRegistry`, using env vars `SESSION_TTL_DAYS` and
  `WEBHOOK_SESSION_TTL_DAYS`. Sweeper starts alongside the
  APScheduler and stops during the shutdown sequence — before
  `channel_manager.stop_all()` — so any in-flight sweep completes
  before the registry quiesces.

### Tests
- `tests/test_session_sweeper.py` — 18 async tests across four
  classes. `TestEvictionPolicy` (9): active survive, expired
  evicted, inclusive-keep boundary, webhook UUID → short TTL,
  webhook non-UUID → standard TTL, non-webhook ignores webhook TTL,
  unparseable `last_active` evicted, no-evictions = no-save,
  one-info-log-per-pass-with-count. `TestConcurrency` (2):
  sweep + concurrent register preserves both; lock is genuinely
  held during the eviction critical section. `TestSdkSessionPrune`
  (3): forward-compat seam called when method present, no-op when
  absent, resilient to SDK exceptions. `TestLifecycle` (4): start
  schedules recurring sweeps, stop-before-start is safe, double
  start is idempotent, stop cleanly cancels the task.

### Not changed
- `SessionRegistry` public API is untouched. The sweeper uses
  underscore-prefixed attributes (`_lock`, `_data`, `_save_locked`)
  by design — the 5.1 internal-consumer seams.
- Sweep cadence is not on the env-var surface. Spec §9.3 lists
  only the two TTL knobs; the 6-h interval is hard-coded (R5: one
  pass over < 100 entries is cheap; adding a knob expands the
  support matrix for no operator benefit).
- No E2E shell scenario — a real TTL pass is days-scale; faking
  wall-clock from the harness is out of proportion. Matches item
  E / item H precedent.

## 0.5.4 — 2026-04-18 — Phase 5.2 item E: Telegram reconnect with backoff

### Added
- `channels/telegram_supervisor.py` — `ReconnectSupervisor`: pure async
  policy module that wraps a rebuild callback with 1s → 60s jittered
  exponential backoff (reuses `retry.compute_backoff_ms`). Retries
  forever per spec §4.2. Logs exactly one `ERROR` per outage and one
  `INFO` on recovery — not one line per attempt. Coalesces concurrent
  triggers (single-task design); idempotent `start()`; clean `stop()`.
- `TelegramChannel._rebuild()` — idempotent build-and-handshake: tears
  down any existing `Application` (best-effort; exceptions swallowed)
  then constructs, initializes, starts, and registers webhook or
  polling. Replaces the inline block that used to live in `start()`.
- `TelegramChannel._health_probe_loop()` — periodic `bot.get_me()`
  probe (`_PROBE_INTERVAL = 45s`, `_PROBE_TIMEOUT = 10s`). On
  `NetworkError` / `TimedOut` / `asyncio.TimeoutError`, triggers the
  supervisor. Non-transport exceptions are logged at DEBUG and the
  probe continues.
- `TelegramChannel._on_ptb_error` — registered via
  `Application.add_error_handler`. Routes `NetworkError` and
  `TimedOut` to the supervisor; other handler errors are logged at
  WARNING without triggering a rebuild.

### Changed
- `TelegramChannel.start()` no longer silently falls back from webhook
  to polling on `set_webhook` failure (that path was dead once the
  supervisor retries forever; it also downgraded a user who explicitly
  configured webhook). On `NetworkError` / `TimedOut` during initial
  bring-up, the supervisor takes over.
- `TelegramChannel.stop()` cancels the probe task and stops the
  supervisor in addition to the existing cleanup.

### Removed
- `_POLL_STALL_THRESHOLD` constant and `_poll_stall_watchdog` method —
  the old "watchdog" only refreshed its own timestamp and performed no
  actual detection. Replaced by `_health_probe_loop`.

### Tests
- `tests/test_telegram_supervisor.py` — 11 pure-asyncio tests for
  `ReconnectSupervisor` covering trigger/no-trigger, backoff on
  failure, unbounded retry, single error log per outage, single info
  log on recovery, state reset between outages, clean stop before and
  after start, idempotent start.
- `tests/test_telegram_reconnect.py` — 6 integration tests using the
  same `telegram.*` stub pattern as `test_telegram_split.py`. Covers
  initial `set_webhook` failure, probe failure, PTB error handler
  routing, non-transport errors ignored, full-cycle teardown, and
  log-once semantics at channel level.
- `tests/test_telegram_split.py` — stub module extended with
  `NetworkError` / `TimedOut` symbols (required by the new imports in
  `channels/telegram.py`).

### Not changed
- `_TYPING_BACKOFF_*` and `_TYPING_CIRCUIT_BREAK` remain as-is —
  orthogonal to reconnect (spec §4.3).
- No new env vars — reconnect schedule is hard-coded per spec §9.3.

## 0.5.3 — 2026-04-18 — Phase 5.2 item F: token budget monitoring (descoped — no cost estimate under Max)

### Added
- `tokens.py` — pure accounting module. Exports `estimate_tokens(text)`
  (`len(text) // 4`, treats `None`/`""` as 0), `extract_usage(result_msg)`
  (defensive read of `input_tokens / output_tokens / cache_read_input_tokens /
  cache_creation_input_tokens` off the SDK `ResultMessage`; missing or
  non-numeric values default to 0), `BudgetTracker` (per-`session_id`
  consecutive-overrun streak; emits one WARNING per session_id per
  process lifetime when the digest exceeds `token_budget * 1.1` for
  three turns in a row; under-budget turns reset the streak;
  `budget <= 0` short-circuits), and `format_turn_summary(role, channel,
  usage)` (renders `turn_done role=… channel=… input=… output=…
  cache_read=… cache_write=…`; cache fields kept separate so a
  `cache_write > 0` per-turn pattern surfaces as a stable-prefix
  regression).
- `Agent` instantiates a per-instance `BudgetTracker` in `__init__` so
  assistant (4000-budget) and butler (800-budget) keep independent
  warning state. After `memory.get_context` returns successfully,
  `Agent._process` records the digest size; the broken-memory branch is
  silent (no digest to measure).
- `Agent._process._attempt_sdk_turn` now captures `ResultMessage.usage`
  via `extract_usage` (resets per attempt — partial usage from a failed
  attempt cannot leak into the summary); after `retry_sdk_call`
  returns, emits one `turn_done` INFO line carrying the role, channel
  (or `-` when missing), and input/output/cache_read/cache_write token
  counts.
- `test-local/mock-claude-sdk` — `ResultMessage` gains an optional
  `usage: dict[str, int]` field populated from `MOCK_SDK_USAGE_INPUT`,
  `MOCK_SDK_USAGE_OUTPUT`, `MOCK_SDK_USAGE_CACHE_READ`,
  `MOCK_SDK_USAGE_CACHE_WRITE` (each defaults to 0). The `build/lib`
  copy is gitignored and regenerates from `setup.py`; only the
  source-tree mock is tracked.

### Descoped from spec
- **No `cost_estimate` and no `MODEL_PRICES` table.** Casa runs on a
  Claude Max subscription — Anthropic does not bill per token, so a
  USD `cost_est` log line would be theatre against list prices we
  don't pay. Operators wanting spend modelling can do it out-of-band
  against the same `turn_done` line. Spec §5.2 wording around cost is
  therefore not implemented.

### Changed
- (none — purely additive instrumentation; no env vars new per spec
  §9.3, no dashboard surface per spec §5.3.)

### Tests
- `test_tokens.py` — 23 unit tests across 4 classes (`TestEstimateTokens`,
  `TestExtractUsage`, `TestBudgetTracker`, `TestFormatTurnSummary`).
- `test_agent_process.py::TestTokenBudgetMonitoring` — 5 integration
  tests (memory recorder per turn, broken-memory skip, three-turn
  warning fires once, turn_done line carries usage, usage resets across
  retries).
- Full unit suite: 335 passed.

## 0.5.2 — 2026-04-18 — Phase 5.2 item H: structured logging with correlation IDs

### Added
- `log_cid.py` — pure logging module. `cid_var` (contextvars), `new_cid()`
  (8-char hex), `CidFilter` (standalone utility: injects `record.cid`
  from the current context var — not auto-attached by `install_logging`,
  kept for callers that construct records manually), `JsonFormatter`
  (one-line JSON with `ts/level/logger/cid/msg[/exc]` fields),
  `_human_formatter()` (ISO UTC human format `... cid=X: msg`), and
  `install_logging()` — idempotent root-logger setup that (a) installs
  a `logging.setLogRecordFactory` wrapper which tags every record with
  `record.cid = cid_var.get()` at creation time (works for all
  loggers, including caplog, because the factory runs inside
  `Logger.makeRecord`), (b) attaches a single Casa-owned StreamHandler
  with `RedactingFilter` on the handler (not root — root-level filters
  do not fire for records from descendants). Spec 5.2 §7.
- Every ingress-built `BusMessage` carries a fresh `context["cid"]`:
  Telegram `_handle`, voice SSE + WS, webhook `/webhook/{name}`,
  `/invoke/{agent}` (`build_invoke_message`), and scheduler heartbeat
  (`build_heartbeat_message`). Caller-supplied `context.cid` in
  payloads wins so external systems can thread their own trace ids.
- Env var `LOG_FORMAT` — `json` switches root formatter to one-line
  JSON; anything else (incl. unset) uses the human format. Read at
  `install_logging()` call time.

### Changed
- `MessageBus._dispatch` sets `log_cid.cid_var` from
  `msg.context["cid"]` with a scoped token before invoking the
  handler and resets it in `finally`. Cross-task contamination is
  impossible: each dispatch runs in its own `asyncio.create_task`
  whose context is a snapshot. Messages without a cid in their
  context read as `cid=-` (backward-compat).
- `casa_core.main` logging setup — a single `install_logging()` call
  replaces the prior `logging.basicConfig(...)` +
  `addFilter(RedactingFilter())` +
  `getLogger("httpx").setLevel(WARNING)` sequence. Behaviour parity
  for the single-handler case Casa ships today: same stdout stream,
  same level, same redaction, same httpx quieting. Log format gains a
  `cid=XX` field per record. Note: `RedactingFilter` now lives on
  Casa's StreamHandler rather than the root logger (which was a
  pre-existing no-op for records from descendant loggers); future
  handlers that want redaction must attach it themselves.
- Timestamps are now ISO-UTC with `Z` suffix
  (`2026-04-18T14:32:01Z`), not the previous
  `2026-04-18 14:32:01,123`. Downstream log tooling that parses the
  old format may need an update.

### Not changed
- `Agent._process`, `retry.py`, and the memory path are untouched —
  item H is strictly a logging-layer change.
- `RedactingFilter` logic unchanged; it is re-attached to Casa's
  StreamHandler via `install_logging` alongside the new factory.
- No new dependency: `json`, `uuid`, and `contextvars` are stdlib.

## 0.5.1 — 2026-04-18 — Phase 5.2 item D: SDK retry + backoff

### Added
- `retry.py` — pure policy module. `RETRY_KINDS` (TIMEOUT, RATE_LIMIT,
  SDK_ERROR), `compute_backoff_ms()` jittered exponential backoff,
  `parse_retry_after_ms()` for server-supplied Retry-After hints,
  `retry_sdk_call()` async coroutine runner. Spec 5.2 §3.
- Env vars `SDK_RETRY_MAX_ATTEMPTS` (default 3), `SDK_RETRY_INITIAL_MS`
  (500), `SDK_RETRY_CAP_MS` (8000). Read at import time — adjust via
  add-on options + restart. Malformed or below-minimum values are
  logged and clamped, never crash module import.
- Server-supplied `Retry-After` hints are clamped at `10 * CAP_MS`
  (default 80 s) to prevent a misbehaving upstream from parking the
  worker indefinitely.

### Changed
- `Agent._process` — the `ClaudeSDKClient` turn is now wrapped in
  `retry_sdk_call`. Each attempt builds a fresh client and resets
  the streaming accumulator, so `on_token` replays cumulative text
  from scratch on retry. Cancellation (e.g. voice barge-in) bypasses
  the retry loop. Non-retryable exceptions (MEMORY_ERROR,
  CHANNEL_ERROR, UNKNOWN) surface unchanged. Spec 5.2 §3.2–§3.3.
- One `logger.warning` per retry attempt emitted via the new
  `Agent._log_retry` hook; log line carries role, attempt number,
  kind, delay_ms, exc repr.
- Internal refactor: `ErrorKind`, `_classify_error`, and
  `_USER_MESSAGES` moved from `agent.py` to a new `error_kinds.py`
  module to break an `agent ↔ retry` import cycle. `agent.py`
  re-exports them so `from agent import ErrorKind` continues to
  work unchanged for all existing consumers.

### Not changed
- Memory path is still silent-degrade (spec 2.2a §11 retained — no
  retry wrapper there per spec 5.2 §2).
- Channel modules untouched; retry is strictly at the SDK layer.
- `MAX_CONCURRENT_AGENTS` / `MAX_CONCURRENT_VOICE` seams untouched.

## 0.5.0 — 2026-04-18 — Phase 5.1: Concurrency correctness + disclosure v2

### Fixed
- `SessionRegistry` — mutate+save serialised via a single `asyncio.Lock`.
  Closes the lost-register / torn-touch race reachable since v0.2.1's
  concurrent bus dispatch. Public `save()` acquires; new internal
  `_save_locked()` assumes the lock is held. Spec 5.1 §3.
- `CachedMemoryProvider` — per-key `asyncio.Lock` with double-checked
  cache in the miss path. Concurrent cold reads on the same key now
  collapse to a single backend call; cache hits remain lock-free.
  Spec 5.1 §4.

### Changed
- `butler.yaml` default personality — layer-1 `Disclosure:` clause
  tightened with concrete per-category examples, stronger deflection
  wording aligned to the `<channel_context>` trust prefix, and an
  explicit positive list of topics safe on any channel. Spec 5.1 §5.

### Migration
- `migrate_disclosure_clause` one-shot in `setup-configs.sh` replaces
  the v1 disclosure block in existing `butler.yaml` files on upgrade.
  Gated by the trailing marker comment `# casa: disclosure v2`;
  idempotent. Mirrored into `test-local/init-overrides/01-setup-configs.sh`.
- No code-level migration for 5.1 Items A and B — the asyncio locks
  are in-memory only and take effect on next process start.

### Deferred
- `MAX_CONCURRENT_AGENTS` / `MAX_CONCURRENT_VOICE` caps — seams
  preserved (`VoiceSession.gate: Semaphore(10)`, architecture §3).
  Spec 5.1 §6.
- Layer-2 post-response disclosure backstop — beyond 5.x per spec 5.1
  §9 C7.

## 0.4.0 — 2026-04-17 — Phase 2.2b: SQLite memory drop-in

### Added
- `SqliteMemoryProvider` — durable local-storage backend for the
  3-method `MemoryProvider` ABC. Single `sqlite3` connection, WAL
  journal mode, schema versioned at `1`. Stores a thin log
  (`messages`, `sessions`, `peer_cards`); no summariser, no dialectic
  (spec §3 / S1).
- `_SqliteCtx` duck-typed wrapper so the existing `_render` produces
  `## What I know about you` + `## Recent exchanges` for SQLite without
  a second rendering code path.
- `MEMORY_BACKEND` env var — `honcho` / `sqlite` / `noop`. Resolution:
  explicit value wins; else `HONCHO_API_KEY` → honcho; else sqlite.
  Invalid values fail fast at startup. `MEMORY_BACKEND=honcho` without
  an API key also fails fast.
- `MEMORY_DB_PATH` env var — SQLite file location, default
  `/data/memory.sqlite`. Parent directory is created if missing.
- Dashboard "Memory" row now renders SQLite / Honcho / none.
- `casa_core.resolve_memory_backend_choice()` + `_wrap_memory_for_strategy()` — pure helpers lifted out of `main()` and unit-tested.

### Changed (behaviour change — documented fallout)
- Fresh installs without `HONCHO_API_KEY` now persist memory to
  `/data/memory.sqlite` by default. Previously: no memory at all. Opt
  out with `MEMORY_BACKEND=noop`.
- `CachedMemoryProvider` wrap is skipped when the backend is SQLite
  (native reads are ~1 ms; caching adds staleness and a background
  task for no measurable benefit). Butler YAMLs keep
  `read_strategy: cached` unchanged — the selector silently degrades
  to bare with a one-time INFO log at startup (spec §2 / S5).

### Migration
- None. No schema changes; no YAML changes. SQLite initialises itself
  on first open via `CREATE TABLE IF NOT EXISTS`. Switching backends
  = fresh start in the new backend (spec §7 / S7).

### Deferred
- LLM summariser (2.2c seam reserved: `_SqliteCtx.summary=None`).
- `remember_fact` tool writing to `peer_cards` (4.x).
- Export/import CLI between backends.
- Retention / pruning policy.

### Tests
- New: `tests/test_memory_sqlite.py` (schema, ensure_session, add_turn
  transactional, get_context rendering, peer_card scoping, topology
  visibility), `tests/test_memory_backend_select.py`
  (resolve + wrap policy), `tests/test_agent_process_sqlite.py`
  (`Agent._process` loop integration), `test-local/e2e/test_sqlite_memory.sh`
  (persistence across restart).
- Existing Honcho unit + integration tests still green (regression
  coverage for 2.2a).

## 0.3.0 — 2026-04-17 — Phase 2.3: voice pipeline

### Added
- `VoiceChannel` — dual ingress: generic SSE at `POST /api/converse`
  and HA-optimised WebSocket at `/api/converse/ws`. Both default on.
  Toggle with `VOICE_SSE_ENABLED` / `VOICE_WS_ENABLED`; paths override
  with `VOICE_SSE_PATH` / `VOICE_WS_PATH`. Idle eviction via
  `VOICE_IDLE_TIMEOUT_SECONDS` (defaults to `butler.session.idle_timeout`).
- `ProsodicSplitter` — delta-fed, tag-opaque sentence splitter that
  treats `[…] (…) {…} <…>` as atomic. Flushes on `.`, `!`, `?`, `…`,
  paragraph break. Safety-caps at 1.5 s / 200 chars with rightmost-
  clause-mark fallback (`,`, `;`, em-dash).
- `TagDialectAdapter` — canonical `[tag]` rewriter for three dialects:
  `square_brackets` (identity), `parens` (global `[tag]→(tag)`),
  `none` (strips leading tag atoms). Agents stay in canonical form;
  rewriting happens at the transport edge.
- `VoiceSessionPool` — process-local pool keyed on `scope_id`.
  Background sweeper evicts idle sessions every 30 s at
  `butler.session.idle_timeout`. `MAX_CONCURRENT_VOICE` gate seam
  reserved (defaults to 10 slots; 5.x hardening flips to 1).
- `stt_start` WebSocket prewarm hook — calls `memory.ensure_session`
  + `memory.get_context` on `CachedMemoryProvider` so the first
  utterance lands on a warm cache. Dedup'd against repeated
  `stt_start` frames for the same scope.
- Persona-voice error lines per `ErrorKind` in `butler.yaml`
  (`voice_errors:` block with `timeout`, `rate_limit`, `sdk_error`,
  `memory_error`, `channel_error`, `unknown`). Rendered through
  `TagDialectAdapter`. Empty string = silent degrade.
- Channel-supplied error hook: `channel.emit_error_line(kind, context,
  cfg)` duck-typed method. `Agent.handle_message`'s error branch
  prefers it over plain-text delivery when present. Non-voice
  channels (Telegram) unchanged.
- `TTSConfig` on `AgentConfig` (`tts.tag_dialect`, default
  `square_brackets`).
- Dashboard row for Voice channel status (transports + on/off).

### Changed
- `MessageBus.request()` now propagates caller cancellation to the
  dispatch task (previously: only the caller's future was cancelled,
  and downstream handlers kept running). Required for voice cancel /
  barge-in semantics (spec §10.2). Backward-compatible — all existing
  bus tests still green.
- `MessageBus._dispatch` resolves the handler per-message from
  `self.handlers[name]` rather than capturing it at `run_agent_loop`
  startup. Enables dynamic handler reconfiguration at the cost of a
  dict lookup per dispatch; same-loop asyncio keeps the lookup safe.

### Migration
- `setup-configs.sh` one-shot: injects `tts:` and `voice_errors:`
  blocks into existing `butler.yaml` if absent. Idempotent. Mirrored
  in `test-local/init-overrides/`.

### Deferred to 5.x hardening
- `MAX_CONCURRENT_VOICE=1` enforcement (seam reserved via
  `VoiceSession.gate`).
- Voice-ID promotion (`voice_speaker → nicola` peer when HA voice-ID
  matures).
- Personality hot-reload.
- Concurrent-cold-key dedup in `CachedMemoryProvider`.

### Tests
- 62 new unit/integration tests (config, migration, splitter, adapter,
  pool, SSE, WS) + 2 new E2E scenarios (SSE smoke + WS smoke under
  Docker). Full voice+agent suite: 192 passed at merge.

## 0.2.2 — 2026-04-17 — Phase 2.2a: Honcho v3 memory redesign

### Changed (breaking for pre-release users)
- `MemoryProvider` is now a 3-method ABC: `ensure_session`, `get_context`,
  `add_turn`. The pre-v3 `store_message` / `create_session` /
  `close_session` surface is removed.
- `HonchoMemoryProvider` rewritten for the honcho-ai 2.1.x peer/session
  model. The pre-v3 apps/users API is gone; `.initialize()` is no
  longer needed (v3 is lazy).
- Agent YAML `memory` block: `peer_name` and `exclude_tags` are
  removed; `read_strategy` (`per_turn` | `cached` | `card_only`) is
  added. Existing user YAMLs are migrated on first boot.
- `SessionRegistry.register()` no longer takes `memory_session_id`
  (the Honcho session is derived from `{channel_key}:{role}`).
  Existing `sessions.json` entries are migrated on first write.

### Added
- `CachedMemoryProvider` — warm cache + background refresh wrapper for
  the voice path; default `read_strategy` for `butler`.
- `<channel_context>` block in every system prompt, so agents can
  condition disclosure on the ingress channel's trust level.
- Personality baselines for `assistant` and `butler` include a
  disclosure clause referencing `<channel_context>`.

### Internal
- `voice_speaker` peer for unauthenticated voice ingress; `nicola`
  peer for authenticated channels (Telegram, webhook). Future
  voice-ID can upgrade a recognised speaker's attribution without
  touching agent code.
- Storage is unconditional: write-side filtering is gone (spec §4.3).
  Visibility is enforced by session/peer topology; disclosure is
  enforced by the agent on the output side.

## 0.2.1

- Fix bus serialisation: `MessageBus.run_agent_loop` no longer awaits
  each handler inline. Each message is now dispatched via
  `asyncio.create_task`, so concurrent `/invoke` calls to a single agent
  run in parallel instead of queuing behind one another. Handler
  exceptions are logged and REQUEST callers receive an error response
  instead of hanging until the 300 s timeout.
- Test-only: added offline mock `claude_agent_sdk` package and
  Dockerized E2E suite under `test-local/e2e/`. The mock replaces the
  real SDK inside the test image so runtime tests can run without an
  OAuth token. E2E suite covers smoke, YAML migration scenarios,
  `/invoke/{agent}` session isolation, heartbeat delivery, and
  concurrent dispatch.

## 0.2.0

- Fix heartbeat silent failure: scheduled ticks now use `channel: scheduler`
  and resolve to a valid session key (`build_session_key` rejects empty
  channels, which previously swallowed every tick).
- Fix dashboard startup race: a request landing on `/` between HTTP server
  start and scheduler init no longer raises `UnboundLocalError` on
  `heartbeat_enabled` / `heartbeat_interval`.
- Fix `/invoke/{agent}` session collision: each invocation gets a distinct
  `chat_id` (caller-supplied via `context.chat_id` or a fresh UUID),
  replacing the shared `webhook:default` session key.
- Harden agent-YAML migration: the migration script now force-sets the
  canonical role on rename (no longer assumes the legacy role value) and
  strips CR first so YAMLs saved with CRLF line endings migrate cleanly.
- Pin Python runtime dependencies.

## 0.1.22

- Role-based agent refactor. Agent YAML filenames and internal identifiers
  now use structural roles (`assistant`, `butler`) instead of display names
  (Ellen, Tina). Display names remain configurable via
  `primary_agent_name` / `voice_agent_name` and are used for personality
  text and the dashboard.
- Session keys formalised as `{channel}:{scope_id}` via
  `build_session_key()`.
- One-shot migration on boot: `agents/ellen.yaml` -> `agents/assistant.yaml`
  (with `role: main` -> `role: assistant` and `peer_name` update);
  `agents/tina.yaml` -> `agents/butler.yaml`.

## 0.1.3

- Add Tina (voice agent) wiring in core startup
- Add APScheduler heartbeat with configurable interval
- Add webhook endpoints (`/webhook/{name}`, `/invoke/{agent}`) with HMAC verification
- Add Telegram message splitting for responses over 4096 characters
- Add error classification with structured user-facing messages
- Add log redaction filter for secrets and tokens
- Make SessionRegistry I/O async (non-blocking)
- Add explicit sys.path management for reliable imports
- Add `apparmor: true` and `url` to config.yaml
- Add store assets: DOCS.md, CHANGELOG.md, translations, icons

## 0.1.2

- Add safety hooks: dangerous command blocking and per-agent path scope enforcement
- Add Honcho memory provider with async SDK wrapper
- Add MCP server registry (HTTP and SDK-based servers)
- Add session registry with JSON persistence
- Add unit tests for all core modules

## 0.1.1

- Add asyncio message bus with priority queues and request/response pattern
- Add channel abstraction with Telegram implementation (python-telegram-bot v20+)
- Add agent config loading with YAML, env var substitution, and model resolution
- Add Ellen agent config with personality prompt and tool permissions

## 0.1.0

- Initial add-on scaffold
- Dockerfile with Python 3.12 Alpine base, Node.js, nginx, ttyd
- S6-overlay init scripts: config validation, default setup, nginx generation
- S6 services: casa (Python core), nginx (ingress proxy), ttyd (web terminal)
- AppArmor profile
- Multi-repo workspace sync script
- Local Docker testing setup with mock Supervisor
