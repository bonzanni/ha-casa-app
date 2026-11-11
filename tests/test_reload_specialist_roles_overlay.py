"""Round-2 (finding #1) unit-level proof, ahead of Task 16's Docker e2e
test_install_from_repo_makes_a_delegatable_specialist — this test can run
in the fast unit gate and pinpoints the exact reload.py call site if it
regresses."""


def test_reload_agents_passes_roles_dir_so_an_installed_specialist_reloads(tmp_path):
    from specialist_component import load_specialist_component
    from specialist_install import InspectionResult, commit_specialist_install, compute_install_root_digest, resolve_dependency_closure
    from approval_values import record_specialist_ack
    from specialist_install_consent import SpecialistInstallAckStore
    from test_specialist_install import _write_component

    specialists_dir = tmp_path / "specialists"
    agents_specialists_dir = tmp_path / "config" / "agents" / "specialists"
    staged = _write_component(tmp_path / "staged", slug="mtg")
    component = load_specialist_component(staged, staged / "manifest.json")
    deps = resolve_dependency_closure(component, staged)
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=(staged / "manifest.json").read_bytes())
    inspection = InspectionResult(
        component_id=component.component_id, version=component.version, slug=component.slug,
        component_checksum=component.checksum, root_digest=root_digest, mission="x",
        default_persona_ref=component.default_persona_ref,
        default_persona_checksum=component.default_persona_checksum,
        required_config_names=(), required_secret_names=(), dependencies=deps, staged_dir=staged,
    )
    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    values = record_specialist_ack(acks, inspection)
    commit_specialist_install(
        inspection=inspection, config={}, secret_names_provided=frozenset(), acks=acks,
        approved_values=values,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_specialists_dir,
    )

    from specialist_registry import InstalledSpecialistIndex, SpecialistRegistry
    from job_registry import JobRegistry

    index = InstalledSpecialistIndex(specialists_dir=str(specialists_dir))
    index.load()

    job_registry = JobRegistry(str(tmp_path / "jobs.json"))
    registry = SpecialistRegistry(
        str(agents_specialists_dir), job_registry=job_registry)
    # BEFORE the fix: registry.load() with no roles_dir sees NOTHING (mtg's
    # role artifact only exists in the CAS-backed overlay, never in the
    # image) — proves the overlay is load-bearing, not incidental.
    registry.load()
    assert "mtg" not in registry.all_configs()

    # AFTER the fix: reload.py's reload_agents threads current_specialist_
    # roles_dir() through — reproduced directly here (not via the full
    # reload.py dispatcher, which needs a live CasaRuntime) to keep this a
    # fast unit test; Task 16's Docker e2e exercises the REAL dispatcher.
    import specialist_materialize
    # N1b slice-C deviation (disclosed in the slice report): the brief's own
    # snippet calls current_specialist_roles_dir(installed_index=index) with
    # NO specialists_dir/agents_specialists_dir override, which defaults to
    # the real host /config/specialists — a mismatch against THIS test's
    # tmp_path-based specialists_dir/agents_specialists_dir (where the
    # tuple + CAS store actually live), and a PermissionError outside a
    # container besides. Passing the SAME dirs used above is required for
    # the self-heal reconcile to find the CAS content it just committed.
    roles_dir = specialist_materialize.current_specialist_roles_dir(
        installed_index=index, specialists_dir=specialists_dir,
        agents_specialists_dir=agents_specialists_dir,
    )
    registry.load(roles_dir=roles_dir)
    assert "mtg" in registry.all_configs()
