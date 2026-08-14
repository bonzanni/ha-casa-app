# Recipe: wire an agent into a resident's delegates

Makes agent X (resident or specialist) callable by resident Y.

## Ask the user (only if ambiguous)

Usually a follow-up after installing a specialist, or when wiring one
resident to delegate to another (e.g. Ellen → Tina for device control).

## Edit agents/<resident-role>/delegates.yaml

FIRST Read the file and check whether the `delegates:` list already has an entry
with `agent: <target-role>`. An entry may already be there from an earlier
operator or configurator action, so wiring a specialist after installing it must
NOT append a second entry. If an entry for `<target-role>` already exists, leave
it (or refine its `purpose`/`when` in place) — do NOT add a duplicate; the wiring
is already done. Only when no entry for `<target-role>` is present, add to the
`delegates:` list:

    - agent: <target-role>          # resident OR specialist role
      purpose: <one-sentence description>
      when: <trigger phrase or criteria>

purpose and when are surfaced in the resident's system prompt - make them specific.

## Also: add the delegate tool to resident's allowed tools

In agents/<resident-role>/runtime.yaml::tools.allowed, ensure mcp__casa-framework__delegate_to_agent is listed.

## Finishing — depends on HOW you got here

**As a SUBROUTINE of a larger recipe** (e.g. `specialist/install.md` step 6
sends you here to wire the just-installed specialist): do ONLY the
`delegates.yaml` edit and the tools-allowed check above, then RETURN to the
calling recipe. Do NOT commit, reload, or `emit_completion` here — the caller
performs a SINGLE commit + reload + `emit_completion` for the whole operation.
Calling `emit_completion` here would terminate the engagement mid-install.

**As a STANDALONE request** (the user directly asked to wire X into Y's
delegates and nothing else): `delegates.yaml` is part of the resident's
AgentConfig — use the `agent` scope for that role. Canonical order:

    config_git_commit(message="wire <target> into <resident>'s delegates")
    casa_reload(scope="agent", role="<resident>")
    emit_completion(status="ok", text="...committed SHA <sha>, called casa_reload(scope='agent') for delegates rebuild.")
