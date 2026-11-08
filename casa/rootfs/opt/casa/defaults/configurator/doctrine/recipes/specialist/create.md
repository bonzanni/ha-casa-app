# Recipe: create a new specialist — RETIRED

Hand-authored specialists are no longer supported. Specialists are managed
components: installed from a repository, digest-verified, and materialized by the
install pipeline. The loader refuses hand-created directories under
`agents/specialists/`, and hooks deny raw writes there.

- To add a specialist: follow `recipes/specialist/install.md` (ask the operator for
  the component repository `owner/repo` + ref).
- If the specialist does not exist as a published component yet, tell the operator
  it must be built and published as a component repository first — authoring a new
  component is not a configurator task.
