# Recipe: unwire an agent from a resident's delegates

The target may be a resident or a specialist — same edit either way.

## Edit agents/<resident-role>/delegates.yaml

Remove the matching entry. Leave `delegates: []` if it was the last.

## Don't forget the reverse cases

- Unwiring an INSTALLED specialist being removed: recipes/specialist/uninstall.md handles it.
- Unwiring because this resident shouldn't delegate (but another does): confirm which resident.

## Finishing — depends on HOW you got here

**As a SUBROUTINE of a larger recipe** (e.g. `specialist/uninstall.md` step 1
sends you here to unwire before removing the specialist): do ONLY the
`delegates.yaml` edit above, then RETURN to the calling recipe. Do NOT commit
or `emit_completion` here — the caller performs a SINGLE commit +
`emit_completion` for the whole operation. Calling `emit_completion`
here would terminate the engagement mid-uninstall.

**As a STANDALONE request** (the user directly asked to unwire and nothing
else): `delegates.yaml` is part of the resident's AgentConfig, and the commit
reloads that resident. Canonical order:

    config_git_commit(message="unwire <target> from <resident>'s delegates")
    emit_completion(status="ok", text="I removed <target> from <resident>'s helpers.", artifacts=[<the commit>])
