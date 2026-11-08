# How a run ends

## The order — MANDATORY

A run that changed something ends with the same two steps, in this order:

1. **Commit.** Call `config_git_commit(message="<imperative sentence>")`. Skip only if nothing you did changed a file — a tool that writes one counts (`config_trigger_upsert`, `specialist_install_commit`, …), not just your own edits. When unsure, call it: a commit with nothing to record returns an empty SHA, which is harmless. (The `plugin_*` tools commit `plugins/registry.json` themselves.) The commit also reloads what it changed, so the change is live when it returns; its result lists those reloads under `reloads`. When it carries a `warning`, a reload did not fully succeed: deal with it before answering, and say so in your answer.
2. **Answer.** End the run with your answer to the operator, after your last tool call.

You do not call a reload after a commit. See `reload.md` for the few reloads you still call yourself.

A change made in several commits goes live commit by commit, so commit when the
change is whole enough to run, not after every file.

## A task that only asks for information

A task that asks you to check, review or report, and to change nothing, is
finished once you have gathered the answer: there is nothing to commit, so
step 1 is skipped. The answer carries the findings themselves, in plain words.

## A question

When you need the operator, end the run with your question. Your run ends
there; his reply, or what he tells Ellen, starts a new run that sees this
exchange. Re-inspect before acting on it: nothing you staged in memory survives,
only what is on disk.

## Waiting on Casa

When a tool tells you Casa continues you once something happens (an approval
card, an erasure), end the run with one short line saying what is waiting.
Casa starts the next run itself, with what happened in its task.

## After the commit — do not linger

The commit's reload is in-process: no reload restarts the add-on. A reload
that touches an agent replaces it; do not interpose work you do not need
between the commit and your answer. A Read in between is fine; a long stretch
of further work is not.

## A restart

Only `casa_restart_supervised` restarts Casa, and inside your run it restarts
nothing: Casa puts a Restart now button under your answer, and the operator
taps it when it suits him. Say in your answer what the restart is for and what
it costs (for a resident persona change: that resident's conversations start
fresh, and voice history is not carried).

## The answer

Write it for the operator. Make it:

- Factually complete.
- Plain words: what you found or changed, whether it is live, and anything he
  still has to do (a setting to give, a caveat such as conversations that keep
  the old version).
- Free of machinery: no commit SHA, no tool names, no result fields such as
  `state=active`, no reload scopes.
- Not rhetorical. No "I have successfully completed your request" fluff.
- Terse. A change takes one paragraph, 2-5 sentences; findings take the room
  they need and no more.

E.g. `"I upgraded Fido to 0.1.28. Nothing for you to set up."`.

A plugin's `setup_*` MCP tool is NEVER handed back — Casa owns a declared
`casa.setupTool` and runs it itself (`recipes/plugin/add.md` / `update.md`).
Say in your answer that setup is queued.

## Stopping part-way

If the operator asks you to stop, or you decide to abort, do NOT call
`config_git_commit` if you made zero edits; DO call it if you made edits
before stopping. Every commit reloads what it changed, so whatever you
committed is live: say in your answer what is now in effect, so the operator
can decide whether to keep it.
