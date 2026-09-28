# tests/test_specialist_materialize.py
import os
from pathlib import Path

import yaml

from specialist_materialize import (
    materialize_specialist_operational_files,
    reconcile_specialist_roles_overlay,
)


class _FakeInstalledIndex:
    """Test double — real InstalledSpecialistIndex is exercised in
    tests/test_specialist_registry_roles_overlay.py; this file isolates the
    overlay-building logic from InstanceDir/CAS file I/O."""

    def __init__(self, slugs: dict[str, Path]) -> None:
        self._slugs = slugs  # slug -> component_dir holding role/{role.yaml,doctrine.md}

    def installed_component_role_dirs(self) -> dict[str, Path]:
        return dict(self._slugs)


def _write_role_dir(root: Path, slug: str, kind: str = "specialist") -> Path:
    role_dir = root / "role"
    role_dir.mkdir(parents=True)
    (role_dir / "role.yaml").write_text(
        yaml.safe_dump({"id": f"{kind}:{slug}", "kind": kind, "slot": slug}), encoding="utf-8")
    (role_dir / "doctrine.md").write_text("# Core doctrine\n\nTest.\n", encoding="utf-8")
    return root


def test_overlay_includes_the_bundled_image_specialist(tmp_path: Path) -> None:
    # NOTE: unlike an installed component (whose role artifact lives one
    # level down, under <component_dir>/role/), the image's bundled roles
    # tree is FLAT: defaults/roles/specialist/<slug>/{role.yaml,doctrine.md}
    # directly (verified on disk: defaults/roles/specialist/finance/role.yaml
    # has no intervening "role/" directory) — so this fixture writes flat,
    # not via the nested-`role/` `_write_role_dir` helper the installed-
    # component tests below use.
    image_roles = tmp_path / "image-roles"
    finance_dir = image_roles / "specialist" / "finance"
    finance_dir.mkdir(parents=True)
    (finance_dir / "role.yaml").write_text(
        yaml.safe_dump({"id": "specialist:finance", "kind": "specialist", "slot": "finance"}),
        encoding="utf-8")
    (finance_dir / "doctrine.md").write_text("# Core doctrine\n\nTest.\n", encoding="utf-8")
    overlay_root = tmp_path / "overlay"
    result = reconcile_specialist_roles_overlay(
        installed_index=_FakeInstalledIndex({}), overlay_root=overlay_root,
        image_roles_dir=str(image_roles),
    )
    assert (result / "specialist" / "finance" / "role.yaml").is_file()
    assert (result / "specialist" / "finance" / "doctrine.md").is_file()


def test_overlay_includes_an_installed_specialist(tmp_path: Path) -> None:
    image_roles = tmp_path / "image-roles"
    (image_roles / "specialist").mkdir(parents=True)
    component_dir = tmp_path / "component"
    _write_role_dir(component_dir, "mtg")
    overlay_root = tmp_path / "overlay"
    result = reconcile_specialist_roles_overlay(
        installed_index=_FakeInstalledIndex({"mtg": component_dir}), overlay_root=overlay_root,
        image_roles_dir=str(image_roles),
    )
    assert (result / "specialist" / "mtg" / "role.yaml").is_file()
    installed_role = yaml.safe_load((result / "specialist" / "mtg" / "role.yaml").read_text())
    assert installed_role["slot"] == "mtg"


def test_overlay_is_fully_rebuilt_each_call_never_accretes_stale_entries(tmp_path: Path) -> None:
    image_roles = tmp_path / "image-roles"
    (image_roles / "specialist").mkdir(parents=True)
    overlay_root = tmp_path / "overlay"
    component_a = tmp_path / "a"
    _write_role_dir(component_a, "old-slug")
    reconcile_specialist_roles_overlay(
        installed_index=_FakeInstalledIndex({"old-slug": component_a}), overlay_root=overlay_root,
        image_roles_dir=str(image_roles),
    )
    assert (overlay_root / "specialist" / "old-slug").exists()
    # A reconcile with a DIFFERENT installed set (e.g. after an uninstall)
    # must not leave the old slug behind.
    reconcile_specialist_roles_overlay(
        installed_index=_FakeInstalledIndex({}), overlay_root=overlay_root,
        image_roles_dir=str(image_roles),
    )
    assert not (overlay_root / "specialist" / "old-slug").exists()


def test_materialize_operational_files_writes_the_required_tier_file_set(tmp_path: Path) -> None:
    from role_slot import RoleSlot, ResolvedModel

    role = RoleSlot(
        role_id="specialist:mtg", kind="specialist", slot="mtg",
        mission="Answer MTG rules questions.",
        resolved_model=ResolvedModel(source="fixed", effective="sonnet",
                                      sdk_model="claude-sonnet-4-6", option=None),
        normalized={
            "model": {"source": "fixed", "value": "sonnet"},
            "tools": {"allowed": [], "disallowed": ["Bash", "Write", "Edit"],
                       "permission_mode": "dontAsk", "max_turns": 8, "skills": "none",
                       "voice_guard": "none"},
            "mcp_servers": [], "memory": {"token_budget": 0, "read_strategy": "per_turn"},
            "session": {"strategy": "ephemeral", "idle_timeout_seconds": 0},
            "tts": {"tag_dialect": "none", "error_phrases": {}},
            "response": {"text": {"register": "precise"}, "voice": {"register": "spoken"},
                          "restricted_webhook": {"register": "plain"}},
            "requires": {"plugins": ["mtg"], "tools": ["mcp__plugin_mtg_mtg__lookup_rule"]},
        },
        doctrine="# Core doctrine\n\nAnswer questions.\n", checksum="sha256:" + "1" * 64,
    )
    from persona_pack import PersonaPack, PersonaManifest
    persona = PersonaPack(
        persona_id="casa/judge", version="0.1.0", trait_schema_version=1,
        identity={"display_name": "Judge", "pronouns": {
            "subject": "they", "object": "them", "possessive_adjective": "their",
            "possessive_pronoun": "theirs", "reflexive": "themself"}},
        relationship_posture="established", archetype="adjudicator",
        traits={"warmth": 2, "formality": 4, "candor": 5, "attunement": 3,
                 "curiosity": 3, "levity": 1, "social_energy": 2, "optimism": 3},
        quirks=(), markdown="# Core\n\nJudges rules.\n\n## Negative space\n\nNever guesses.\n",
        examples=(), manifest=PersonaManifest(files=(), checksum="sha256:" + "3" * 64),
        checksum="sha256:" + "2" * 64,
    )
    materialize_specialist_operational_files(
        agents_specialists_dir=tmp_path / "agents-specialists", slug="mtg", role=role, persona=persona,
    )
    slug_dir = tmp_path / "agents-specialists" / "mtg"
    # Round-4 fix (finding #1): slug_dir is a symlink to a versioned content
    # directory, never a real directory the swap has to rmtree/rename.
    assert slug_dir.is_symlink()
    assert slug_dir.is_dir()  # pathlib follows the symlink for is_dir/is_file/open transparently
    for name in ("character.yaml", "voice.yaml", "response_shape.yaml", "runtime.yaml"):
        assert (slug_dir / name).is_file(), name
    runtime = yaml.safe_load((slug_dir / "runtime.yaml").read_text())
    assert runtime["kind"] == "specialist"
    assert runtime["model"] == {"source": "fixed", "value": "sonnet"}
    assert runtime["channels"] == []
    assert runtime["session"]["strategy"] == "ephemeral"
    assert runtime["requires"]["plugins"] == ["mtg"]
    character = yaml.safe_load((slug_dir / "character.yaml").read_text())
    assert character["name"] == "Judge"
    assert character["role"] == "mtg"


def test_materialize_operational_files_repeat_call_swaps_atomically_and_gcs_old_version(
    tmp_path: Path,
) -> None:
    """Round-4 fix (finding #1): a second materialize call for the SAME slug
    retargets the slug_dir symlink in one os.replace — never leaves slug_dir
    absent, never leaves the old content directory behind, and the new
    content fully replaces (not merges with) the old.

    Pins INV-SPEC-004. Red case demonstrated: skipping the prior-content GC after the atomic retarget fails this test.
    """
    from role_slot import RoleSlot, ResolvedModel
    from persona_pack import PersonaPack, PersonaManifest

    def _role(archetype_marker: str) -> "RoleSlot":
        return RoleSlot(
            role_id="specialist:mtg", kind="specialist", slot="mtg", mission="x",
            resolved_model=ResolvedModel(source="fixed", effective="sonnet",
                                          sdk_model="claude-sonnet-4-6", option=None),
            normalized={}, doctrine="Doctrine.\n", checksum="sha256:" + "1" * 64,
        )

    def _persona(archetype: str) -> "PersonaPack":
        return PersonaPack(
            persona_id="casa/judge", version="0.1.0", trait_schema_version=1,
            identity={"display_name": "Judge", "pronouns": {
                "subject": "they", "object": "them", "possessive_adjective": "their",
                "possessive_pronoun": "theirs", "reflexive": "themself"}},
            relationship_posture="established", archetype=archetype,
            traits={"warmth": 2, "formality": 4, "candor": 5, "attunement": 3,
                     "curiosity": 3, "levity": 1, "social_energy": 2, "optimism": 3},
            quirks=(), markdown="# Core\n\nJudges rules.\n", examples=(),
            manifest=PersonaManifest(files=(), checksum="sha256:" + "3" * 64),
            checksum="sha256:" + "2" * 64,
        )

    agents_specialists_dir = tmp_path / "agents-specialists"
    materialize_specialist_operational_files(
        agents_specialists_dir=agents_specialists_dir, slug="mtg",
        role=_role("adjudicator"), persona=_persona("adjudicator"))
    slug_dir = agents_specialists_dir / "mtg"
    first_target = os.readlink(slug_dir)
    assert (agents_specialists_dir / first_target).is_dir()

    materialize_specialist_operational_files(
        agents_specialists_dir=agents_specialists_dir, slug="mtg",
        role=_role("mentor"), persona=_persona("mentor"))
    second_target = os.readlink(slug_dir)
    assert second_target != first_target  # retargeted to a fresh content dir
    assert not (agents_specialists_dir / first_target).exists()  # old version GC'd
    voice = yaml.safe_load((slug_dir / "voice.yaml").read_text())
    assert voice["tone"] == ["mentor"]  # content fully replaced, not merged


def test_materialize_operational_files_migrates_a_legacy_real_directory_slug_dir(
    tmp_path: Path,
) -> None:
    """Round-4 fix (finding #1)'s documented one-time exception: an
    image-provided REAL (non-symlink) slug_dir — e.g. the bundled `finance`
    specialist's pre-cutover layout — is migrated into the symlink scheme on
    its first materialize call, and every call after that goes through the
    ordinary single-os.replace path."""
    from role_slot import RoleSlot, ResolvedModel
    from persona_pack import PersonaPack, PersonaManifest

    role = RoleSlot(
        role_id="specialist:finance", kind="specialist", slot="finance", mission="x",
        resolved_model=ResolvedModel(source="fixed", effective="sonnet",
                                      sdk_model="claude-sonnet-4-6", option=None),
        normalized={}, doctrine="Doctrine.\n", checksum="sha256:" + "1" * 64,
    )
    persona = PersonaPack(
        persona_id="casa/finance", version="0.1.0", trait_schema_version=1,
        identity={"display_name": "Finance", "pronouns": {
            "subject": "they", "object": "them", "possessive_adjective": "their",
            "possessive_pronoun": "theirs", "reflexive": "themself"}},
        relationship_posture="established", archetype="advisor",
        traits={"warmth": 2, "formality": 4, "candor": 5, "attunement": 3,
                 "curiosity": 3, "levity": 1, "social_energy": 2, "optimism": 3},
        quirks=(), markdown="# Core\n", examples=(),
        manifest=PersonaManifest(files=(), checksum="sha256:" + "3" * 64),
        checksum="sha256:" + "2" * 64,
    )
    agents_specialists_dir = tmp_path / "agents-specialists"
    legacy_dir = agents_specialists_dir / "finance"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "character.yaml").write_text("legacy: true\n", encoding="utf-8")

    materialize_specialist_operational_files(
        agents_specialists_dir=agents_specialists_dir, slug="finance", role=role, persona=persona)

    assert legacy_dir.is_symlink()  # migrated, not left as a real dir
    character = yaml.safe_load((legacy_dir / "character.yaml").read_text())
    assert character["name"] == "Finance"  # legacy content replaced, not merged
    # No stray `.finance.prior-*` backup left behind on success.
    assert not any(p.name.startswith(".finance.prior-") for p in agents_specialists_dir.iterdir())


def test_materialized_finance_specialist_round_trips_through_the_real_loader(
    tmp_path: Path,
) -> None:
    """N1b slice A regression (task-review Critical): the operational files
    ``_write_specialist_operational_files`` writes must be schema-valid for
    the REAL ``agent_loader.load_agent_from_dir`` — the module's entire
    purpose — not merely internally consistent with the writer's own
    assumptions. Exercises the full pipeline against a SYNTHETIC role
    artifact holding the exact finance role.yaml/doctrine.md content the
    image shipped pre-Task-N2-cutover (finance's bundled role directory no
    longer exists on disk — Task N2 exported it and removed it from the
    image; this test preserves the loader round-trip's full power on the
    same bytes) plus a real-shaped persona, materializes operational files,
    builds a real roles-overlay via ``reconcile_specialist_roles_overlay``
    pointed at the synthetic tree, and then calls the REAL loader. Must
    FAIL on the pre-fix writer: role.yaml's ``session.idle_timeout_seconds``
    / ``tts.error_phrases`` violate ``runtime.v1.json``'s
    ``additionalProperties: false`` sub-schemas, and the hardcoded
    ``card: ""`` / ``prompt: ""`` violate ``character.v1.json``'s
    ``minLength: 1``."""
    import agent_loader
    import role_artifact
    import role_slot
    from persona_pack import PersonaManifest, PersonaPack

    finance_role_dir = tmp_path / "synthetic-image-roles" / "specialist" / "finance"
    finance_role_dir.mkdir(parents=True)
    (finance_role_dir / "role.yaml").write_text("""\
api_version: casa.role/v1
id: specialist:finance
kind: specialist
slot: finance
mission: Retrieve and explain household financial records using deterministic arithmetic.
enabled: false
model: {source: fixed, value: sonnet}
tools:
  allowed: [Read, Skill, mcp__casa-framework__get_schedule, mcp__casa-framework__send_media, mcp__casa-framework__ask_user]
  disallowed: [Bash, Write, Edit]
  permission_mode: acceptEdits
  max_turns: 10
  skills: all
  voice_guard: none
mcp_servers: [n8n-workflows, casa-framework]
channels: []
memory: {token_budget: 4000, read_strategy: per_turn}
session: {strategy: ephemeral, idle_timeout_seconds: 900}
disclosure: {policy: delegated, overrides: {}}
delegates: []
executors: []
triggers: []
hooks: {pre_tool_use: []}
tts: {tag_dialect: none, error_phrases: {timeout: "One moment while I check.", failure: "I could not complete that."}}
response:
  text: {register: precise, max_status_sentences: 3}
  voice: {register: spoken, max_status_sentences: 2}
  restricted_webhook: {register: plain, max_status_sentences: 2}
persona:
  policy: optional-but-bound
  compatibility: ["casa/alex@>=0.1.0 <1.0.0"]
requires: {plugins: [], tools: []}
doctrine_file: doctrine.md
""", encoding="utf-8")
    (finance_role_dir / "doctrine.md").write_text("""\
# Core doctrine

Answer only finance-scoped delegations. Retrieve source records through assigned tools, route every arithmetic operation through the deterministic finance calculation path, distinguish source data from conclusions, and return a precise task-focused result. Treat recalled material as attributed prior evidence, not personal recollection.

## Text projection

Use concise prose and tables only when they make the figures easier to audit.

## Voice projection

Lead with the result, then give at most the essential supporting figures.

## Restricted webhook projection

Do not expose financial records or persona identity.
""", encoding="utf-8")
    source = role_artifact.load_role_artifact(finance_role_dir)
    role = role_slot.materialize_role(source=source, options={})

    persona = PersonaPack(
        persona_id="casa/alex", version="0.1.0", trait_schema_version=1,
        identity={"display_name": "Alex", "pronouns": {
            "subject": "they", "object": "them", "possessive_adjective": "their",
            "possessive_pronoun": "theirs", "reflexive": "themself"}},
        relationship_posture="established", archetype="advisor",
        traits={"warmth": 2, "formality": 4, "candor": 5, "attunement": 3,
                 "curiosity": 3, "levity": 1, "social_energy": 2, "optimism": 3},
        quirks=(),
        markdown="# Core\n\nHandles household finances precisely.\n\n"
                 "## Negative space\n\nNever guesses at numbers.\n",
        examples=(), manifest=PersonaManifest(files=(), checksum="sha256:" + "3" * 64),
        checksum="sha256:" + "2" * 64,
    )

    agents_specialists_dir = tmp_path / "agents-specialists"
    materialize_specialist_operational_files(
        agents_specialists_dir=agents_specialists_dir, slug="finance",
        role=role, persona=persona,
    )
    slug_dir = agents_specialists_dir / "finance"

    overlay_root = tmp_path / "overlay"
    reconcile_specialist_roles_overlay(
        installed_index=_FakeInstalledIndex({}), overlay_root=overlay_root,
        image_roles_dir=str(tmp_path / "synthetic-image-roles"),
    )

    cfg = agent_loader.load_agent_from_dir(
        str(slug_dir), policies=None, roles_dir=str(overlay_root),
    )

    assert cfg.role == "finance"
    assert cfg.kind == "specialist"
    assert cfg.role_artifact.role["id"] == "specialist:finance"

    # Mb (whole-branch review): a schema-VALID round-trip is not enough — the
    # projected values must be CORRECT, not merely loadable. A wrong
    # field-name mapping (role.yaml's `idle_timeout_seconds`/`error_phrases`/
    # descriptive `register` vs the operational schema's `idle_timeout`/
    # top-level `voice_errors`/coarse enum) would still load but silently
    # drop or misplace the value. Assert the concrete mappings.
    assert cfg.session.strategy == "ephemeral"
    assert cfg.session.idle_timeout == 900  # idle_timeout_seconds -> idle_timeout
    # error_phrases (role.yaml tts sub-block) moves to the top-level
    # voice_errors runtime key, preserved verbatim — NON-EMPTY fixture proves
    # it is carried, not merely defaulted-empty.
    assert cfg.voice_errors == {
        "timeout": "One moment while I check.",
        "failure": "I could not complete that.",
    }
    # role.yaml's descriptive `register: precise` (text/written projection)
    # maps to the coarse operational enum value `written`, never passed through.
    assert cfg.response_shape.register == "written"


# ---------------------------------------------------------------------------
# Whole-branch review ROUND 2 — F4: resolve_material_content_dir must bind the
# symlink target to THE SLUG'S OWN content dir, never a foreign slug's, so a
# cross-pointed link can't GC another slug's live content.
# ---------------------------------------------------------------------------


def test_resolve_material_content_dir_accepts_this_slugs_own_target(tmp_path: Path) -> None:
    from specialist_materialize import resolve_material_content_dir

    agents_dir = tmp_path / "agents-specialists"
    content = agents_dir / (".finance.material-" + "a" * 32)
    content.mkdir(parents=True)
    link = agents_dir / "finance"
    os.symlink(content.name, link)

    resolved = resolve_material_content_dir(link, agents_dir)
    assert resolved is not None
    assert resolved.resolve() == content.resolve()


def test_resolve_material_content_dir_refuses_a_cross_slug_target(tmp_path: Path) -> None:
    """slug A's link pointing at slug B's `.{B}.material-...` dir must fail
    closed (None) — otherwise uninstall/rematerialize of A would GC B's dir."""
    from specialist_materialize import resolve_material_content_dir

    agents_dir = tmp_path / "agents-specialists"
    b_content = agents_dir / (".finance.material-" + "b" * 32)  # slug B's live content
    b_content.mkdir(parents=True)
    a_link = agents_dir / "legal"  # slug A's op symlink...
    os.symlink(b_content.name, a_link)  # ...cross-pointed at B's content dir

    assert resolve_material_content_dir(a_link, agents_dir) is None
    assert b_content.is_dir()  # untouched by the resolve


def test_uninstall_with_cross_pointed_symlink_leaves_the_other_slugs_dir_intact(
    tmp_path: Path,
) -> None:
    """End-to-end F4: uninstalling slug A whose symlink was cross-pointed at
    slug B's content dir unlinks only A's symlink and leaves B's dir alive."""
    import shutil as _shutil

    from specialist_install import uninstall_specialist

    agents_dir = tmp_path / "agents-specialists"
    specialists_dir = tmp_path / "specialists"
    b_content = agents_dir / (".finance.material-" + "c" * 32)
    b_content.mkdir(parents=True)
    (b_content / "runtime.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    a_link = agents_dir / "legal"
    os.symlink(b_content.name, a_link)

    uninstall_specialist(
        slug="legal", specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)

    assert not os.path.lexists(a_link)  # A's cross-pointed symlink removed
    assert b_content.is_dir()  # B's live content survives
    assert (b_content / "runtime.yaml").is_file()
    _shutil.rmtree(agents_dir, ignore_errors=True)


def _fixed_role_and_persona(archetype: str = "adjudicator"):
    from role_slot import RoleSlot, ResolvedModel
    from persona_pack import PersonaPack, PersonaManifest

    role = RoleSlot(
        role_id="specialist:mtg", kind="specialist", slot="mtg", mission="x",
        resolved_model=ResolvedModel(source="fixed", effective="sonnet",
                                      sdk_model="claude-sonnet-4-6", option=None),
        normalized={"tools": {"allowed": ["Read"]}, "mcp_servers": ["mtg"]},
        doctrine="Doctrine.\n", checksum="sha256:" + "1" * 64,
    )
    persona = PersonaPack(
        persona_id="casa/judge", version="0.1.0", trait_schema_version=1,
        identity={"display_name": "Judge", "pronouns": {
            "subject": "they", "object": "them", "possessive_adjective": "their",
            "possessive_pronoun": "theirs", "reflexive": "themself"}},
        relationship_posture="established", archetype=archetype,
        traits={"warmth": 2, "formality": 4, "candor": 5, "attunement": 3,
                 "curiosity": 3, "levity": 1, "social_energy": 2, "optimism": 3},
        quirks=(), markdown="# Core\n\nJudges rules.\n", examples=(),
        manifest=PersonaManifest(files=(), checksum="sha256:" + "3" * 64),
        checksum="sha256:" + "2" * 64,
    )
    return role, persona


def _entry_identities(agents_specialists_dir: Path) -> dict:
    """Every entry under the op-file root, with the identity a rewrite changes:
    the link text for a symlink, (inode, mtime_ns) for a directory or file."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(agents_specialists_dir):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            st = os.lstat(p)
            out[str(p.relative_to(agents_specialists_dir))] = (
                os.readlink(p) if p.is_symlink() else (st.st_ino, st.st_mtime_ns))
    return out


def test_identical_materialization_preserves_target_without_mutation(tmp_path: Path) -> None:
    """#1085, INV-SPEC-019: re-materialising from inputs whose rendered bytes
    already sit under the slug's own contained target writes nothing — same
    link, same content directory, same file inodes — so a boot or reload
    reconcile leaves no tracked-path churn in the config repo."""
    agents_specialists_dir = tmp_path / "agents-specialists"
    role, persona = _fixed_role_and_persona()
    kwargs = dict(agents_specialists_dir=agents_specialists_dir, slug="mtg", role=role,
                  persona=persona, binding_digest="sha256:" + "d" * 64,
                  component_root="specialist:acme/mtg@1.0.0:sha256:" + "e" * 64)
    materialize_specialist_operational_files(**kwargs)
    first = _entry_identities(agents_specialists_dir)
    content_dirs = [k for k in first if k.startswith(".mtg.material-") and "/" not in k]
    assert len(content_dirs) == 1
    assert sum(k.startswith(content_dirs[0] + "/") for k in first) == 5

    materialize_specialist_operational_files(**kwargs)
    second = _entry_identities(agents_specialists_dir)
    assert (len(second), sum(second[k] != first.get(k) for k in second)) == (len(first), 0)
