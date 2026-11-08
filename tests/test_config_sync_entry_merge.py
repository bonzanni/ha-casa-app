"""Entry-level reconciliation for list-of-named-entries config files (#398).

`config_sync.reconcile()` resolves divergence per FILE, byte for byte, so anything
an operator or an agent ADDS to a shipped config file is destroyed by an update
that also changed the shipped default. For the handful of files that are really
*lists of named entries*, the image's copy is a seed rather than the whole truth,
and resolution belongs at the entry.

These tests pin the two halves of that: the shape gate that decides whether a file
may be merged at all (and refuses on any irregularity rather than judging what it
means), and the three-way table that resolves each entry.

Spec: `config-entry-reconcile-spec.md` (private repo). Deferred: #402, #403.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import config_sync


# --------------------------------------------------------------------------
# The merge-eligible table
# --------------------------------------------------------------------------


def test_merge_eligible_table_is_exactly_the_two_list_files():
    """Explicit, not derived from the schemas.

    Deriving eligibility from "the schema contains an array of objects" would
    silently enrol any future file of that shape, including files whose entries
    are NOT order-independent. These two are, which is a fact about them
    rather than about their shape. (``executors.yaml`` left the table in
    v0.351.0: nothing reads it.)
    """
    assert config_sync.MERGE_ELIGIBLE == {
        ("agents", "triggers.yaml"): ("triggers", "name"),
        ("agents", "delegates.yaml"): ("delegates", "agent"),
    }


@pytest.mark.parametrize("rel,expected", [
    ("agents/assistant/triggers.yaml", ("triggers", "name")),
    ("agents/butler/delegates.yaml", ("delegates", "agent")),
])
def test_merge_spec_for_eligible_paths(rel, expected):
    assert config_sync._merge_spec(rel) == expected


@pytest.mark.parametrize("rel", [
    # Not a list-of-entries file.
    "agents/assistant/runtime.yaml",
    "agents/assistant/character.yaml",
    "agents/executors/configurator/hooks.yaml",
    # No longer read (since v0.351.0), so no longer merged.
    "agents/assistant/executors.yaml",
    "agents/assistant/prompts/morning-briefing.md",
    # A nested mapping, not a list.
    "policies/disclosure.yaml",
    # The basename trap: `_make_validator` is already path-aware because
    # policies/disclosure.yaml reuses the basename of the per-agent file while
    # binding to a DIFFERENT schema. Matching on basename alone would enrol
    # anything called triggers.yaml anywhere in the tree.
    "policies/triggers.yaml",
    "bindings/triggers.yaml",
    "specialists/x/delegates.yaml",
    "triggers.yaml",
])
def test_merge_spec_is_none_outside_the_agents_tree_or_the_table(rel):
    assert config_sync._merge_spec(rel) is None


# --------------------------------------------------------------------------
# The shape gate
# --------------------------------------------------------------------------

_GOOD = """schema_version: 1
triggers:
  - name: heartbeat
    type: interval
    minutes: 60
  - name: nightly
    type: cron
    schedule: "0 3 * * *"
"""


def test_entry_doc_returns_entries_in_document_order():
    doc = config_sync._entry_doc(_GOOD, "triggers", "name")
    assert doc is not None
    assert list(doc.entries) == ["heartbeat", "nightly"]
    assert doc.entries["heartbeat"]["minutes"] == 60
    assert doc.top_level == {"schema_version": 1}


def test_entry_doc_keeps_every_top_level_field_but_the_list():
    doc = config_sync._entry_doc(
        "schema_version: 2\nextra: keepme\ntriggers: []\n", "triggers", "name")
    assert doc is not None
    assert doc.top_level == {"schema_version": 2, "extra": "keepme"}
    assert doc.entries == {}


# One test per irregularity, because the gate's contract is that ANY of them
# refuses the merge rather than being handled. A combined test would let a
# regression in one case hide behind another.

@pytest.mark.parametrize("label,text", [
    ("unparseable yaml", "triggers: [unclosed\n"),
    ("tab indentation", "triggers:\n\t- name: x\n"),
    ("top level not a mapping", "- just\n- a\n- list\n"),
    ("top level is a scalar", "42\n"),
    ("empty document", ""),
    ("list key absent", "schema_version: 1\nother: []\n"),
    ("list key not a list", "schema_version: 1\ntriggers: {a: b}\n"),
    ("list key null", "schema_version: 1\ntriggers:\n"),
    ("element not a mapping", "schema_version: 1\ntriggers:\n  - just-a-string\n"),
    ("element is null", "schema_version: 1\ntriggers:\n  -\n"),
    ("identity missing", "schema_version: 1\ntriggers:\n  - type: cron\n"),
    ("identity not a string", "schema_version: 1\ntriggers:\n  - name: 7\n"),
    ("identity is null", "schema_version: 1\ntriggers:\n  - name:\n"),
    ("identity empty", 'schema_version: 1\ntriggers:\n  - name: ""\n'),
    ("duplicate identity",
     "schema_version: 1\ntriggers:\n  - name: dup\n  - name: dup\n"),
])
def test_entry_doc_refuses_on_any_irregularity(label, text):
    """The gate REFUSES; it never decides what an irregularity means.

    Deciding — what a duplicate name means, whether a malformed entry can be
    salvaged — is a judgment that grows a new finding every time it is patched.
    There is none here: the merge runs on a known-good shape or does not run,
    and the fallback is byte-level reconcile, which already exists and is
    already tested.
    """
    assert config_sync._entry_doc(text, "triggers", "name") is None, label


# The canonical legitimate use of a YAML anchor: two webhook triggers sharing
# one auth block. The anchor is on a MAPPING deliberately — `safe_dump` never
# aliases a scalar, so an anchored string would be re-emitted alias-free and
# would not exercise the second half of #408 at all.
_ANCHORED = """schema_version: 1
triggers:
  - name: heartbeat
    type: interval
    minutes: 60
    channel: telegram
    prompt: old shipped prompt
  - name: doorbell
    type: webhook
    path: /hook/doorbell
    channel: telegram
    prompt: someone is at the door
    auth: &shared_auth
      mode: timestamped_hmac
      tolerance_secs: 300
  - name: postbox
    type: webhook
    path: /hook/postbox
    channel: telegram
    prompt: the post has arrived
    auth: *shared_auth
"""


def test_entry_doc_accepts_a_document_using_yaml_anchors(tmp_path):
    """#408. An anchor/alias is ordinary YAML that `agent_loader` loads without
    complaint, so refusing it HERE did not make the file safer — it only cost
    the operator entry-level reconciliation on the one file where losing an
    entry means a reminder that is never delivered."""
    doc = config_sync._entry_doc(_ANCHORED, "triggers", "name")
    assert doc is not None
    assert list(doc.entries) == ["heartbeat", "doorbell", "postbox"]
    # The alias resolved to the anchored value, exactly as the loader sees it.
    assert doc.entries["postbox"]["auth"]["mode"] == "timestamped_hmac"


def test_an_anchored_file_keeps_local_entries_and_gets_the_shipped_change(
        tmp_path):
    """#408, BOTH halves — and the second is why one assertion is not enough.

    Fixing only the shape gate would leave the "did anything change?"
    comparison parsing with the alias-refusing loader: both sides come back as
    the same "could not parse" value, the merge reads as "nothing to apply",
    and the shipped change is never written while the baseline still advances
    to the new image. The next boot then sees baseline and image agreeing and
    never reconciles the file again, so the change is lost PERMANENTLY rather
    than retried. A test asserting only that the local entry survived passes in
    exactly that failure mode.
    """
    _write(tmp_path / "baseline", REL, _SHIPPED_V1)
    _write(tmp_path / "live", REL, _ANCHORED)
    _write(tmp_path / "defaults", REL, _SHIPPED_V2)

    report = _reconcile(tmp_path)

    doc = config_sync._entry_doc(
        (tmp_path / "live" / REL).read_text(), "triggers", "name")
    assert doc is not None
    assert "doorbell" in doc.entries, "the local entry was destroyed"
    assert doc.entries["postbox"]["auth"]["tolerance_secs"] == 300
    assert doc.entries["heartbeat"]["prompt"] == "IMPROVED shipped prompt", (
        "the shipped change was skipped")
    assert [m["path"] for m in report.merged] == [REL]
    # The baseline advanced — which is only safe BECAUSE the change was applied.
    assert (tmp_path / "baseline" / REL).read_text() == _SHIPPED_V2


_CYCLIC = "schema_version: 1\ntriggers:\n  - &e {name: x, self: *e}\n"

# `&a1 [*a0, *a0]` chained doubles the expanded node count each level: 30
# levels is ~2^30 leaves from a few hundred bytes on disk.
_EXPANDING = ("schema_version: 1\n_a0: &a0 []\n"
              + "".join(f"_a{i}: &a{i} [*a{i-1}, *a{i-1}]\n"
                        for i in range(1, 31))
              + "triggers:\n  - {name: x, auth: *a30}\n")


# The same cycle behind an `!!omap`, whose entries SafeLoader builds as tuples.
# A walk that recognises only dicts and lists reads the tuple as a scalar and
# never sees what is inside it.
_CYCLIC_OMAP = ("schema_version: 1\ntriggers:\n"
                "  - name: x\n    auth: &a !!omap [{k: *a}]\n")


@pytest.mark.parametrize("label,text", [
    ("aliases an ancestor", _CYCLIC),
    ("aliases an ancestor through an !!omap tuple", _CYCLIC_OMAP),
    ("expands astronomically", _EXPANDING),
])
def test_admitting_aliases_does_not_admit_the_two_documents_a_tree_cannot_be(
        label, text):
    """Accepting anchors means accepting what an alias can build that a tree
    cannot, so both are refused at the gate rather than met further in.

    A document aliasing one of its own ancestors is walked by everything after
    this point — `==` between entries, `safe_dump`, jsonschema — and each would
    recurse until it raised, aborting the whole reconciliation pass and with it
    every later file. One aliasing a shared subtree repeatedly is tiny on disk
    and astronomically large once walked. Refusing is the fallback this gate
    already has for every other irregularity: the file takes byte-level
    resolution, exactly as it did before #408.
    """
    assert config_sync._parse(text) is config_sync._UNPARSEABLE, label
    assert config_sync._entry_doc(text, "triggers", "name") is None, label
    assert not config_sync._same_document(text, text), label


def test_an_ordinary_shared_anchor_is_still_accepted():
    """The refusal above must not swallow the case #408 exists for: an anchor
    shared by two entries is a tree walk of a handful of nodes."""
    assert config_sync._entry_doc(_ANCHORED, "triggers", "name") is not None
    assert config_sync._same_document(_ANCHORED, _ANCHORED)


_WITH_PLACEHOLDER = _SHIPPED_V1_TEXT = (
    "schema_version: 1\ntriggers:\n"
    "  - name: heartbeat\n    type: interval\n    minutes: 60\n"
    "    channel: telegram\n    prompt: old shipped prompt\n"
    "  - name: garbage-reminder\n    type: cron\n    schedule: \"0 20 * * 2\"\n"
    "    channel: telegram\n    prompt: \"${DETAIL}\"\n")


def test_a_file_using_a_placeholder_is_not_merged_at_all(tmp_path, monkeypatch):
    """A `${VAR}` anywhere in the file takes it off the entry-level path.

    This module parses WITHOUT resolving — it rewrites these files, and
    resolving first would bake today's environment into one permanently — so it
    re-emits through `safe_dump`, which does not preserve quote style. Quoting
    is what tells the loader a placeholder is text (#409), so a rewrite retypes
    a locally-added entry authored as `prompt: "${DETAIL}"` the moment the value
    looks like a number or a flag; per-entry validation then judges that entry
    invalid and DROPS it — the exact loss this whole mechanism exists to
    prevent. Refusing hands the file to byte-level resolution instead, which is
    loud: recovery copy, conflict record, operator notification.

    The value is what makes it bite, and the refusal is decided on the RAW TEXT
    so it does not depend on today's environment.
    """
    monkeypatch.setenv("DETAIL", "true")
    _write(tmp_path / "baseline", REL, _SHIPPED_V1)
    _write(tmp_path / "live", REL, _WITH_PLACEHOLDER)
    _write(tmp_path / "defaults", REL, _SHIPPED_V2)

    assert config_sync._entry_doc(_WITH_PLACEHOLDER, "triggers", "name") is None
    report = _reconcile(tmp_path)
    assert report.merged == []
    assert report.entries_dropped == [], "an entry must not be silently dropped"
    assert [c["path"] for c in report.conflicts] == [REL]
    # Recoverable: byte-level resolution takes the pre-sync snapshot first, and
    # the conflict is on the record rather than an entry vanishing quietly.
    assert report.pre_sync_sha


@pytest.mark.parametrize("label,text", [
    ("in a comment", "# template: ${NAME}\n" + _SHIPPED_V1_TEXT.replace(
        '"${DETAIL}"', "put the bins out")),
    ("embedded in a larger scalar", _SHIPPED_V1_TEXT.replace(
        '"${DETAIL}"', '"Send ${DETAIL}"')),
    ("embedded, unquoted", _SHIPPED_V1_TEXT.replace(
        '"${DETAIL}"', "Send ${DETAIL}")),
    ("lone but PLAIN", _SHIPPED_V1_TEXT.replace('"${DETAIL}"', "${DETAIL}")),
    ("not a placeholder", _SHIPPED_V1_TEXT.replace(
        '"${DETAIL}"', '"literal ${not-a-var}"')),
    ("not a placeholder either", _SHIPPED_V1_TEXT.replace(
        '"${DETAIL}"', '"${NAME-default}"')),
])
def test_the_placeholder_door_closes_only_on_what_a_rewrite_can_damage(
        label, text):
    """The refusal has to be PRECISE, because refusing is not the cheap
    direction: it sends the file to byte-level resolution, which is image-wins,
    and that destroys every locally-added entry — far more than the single
    retyped entry the door exists to protect.

    Only a lone placeholder DECLARED TEXT — quoted, or carrying YAML's string
    tag — loses anything to a rewrite. A comment reaches no loader; an embedded
    placeholder is a string under every style; a plain lone one is read back as
    a value both before and after, because a dump re-emits it plain; and
    `${NOT-A-VAR}` is never resolved at all.
    """
    assert config_sync._entry_doc(text, "triggers", "name") is not None, label


@pytest.mark.parametrize("label,prompt", [
    ("quoted", '"${DETAIL}"'),
    ("tagged as text", "!!str ${DETAIL}"),
])
def test_the_placeholder_door_closes_on_every_way_of_declaring_text(
        label, prompt):
    """Quoting is not the only way an author says "this is text", and a
    rewrite drops a tag exactly as it drops quotes."""
    text = _SHIPPED_V1_TEXT.replace('"${DETAIL}"', prompt)
    assert config_sync._entry_doc(text, "triggers", "name") is None, label


def test_a_merge_only_pass_commits_what_it_wrote(tmp_path):
    """The post-reconcile snapshot arm has to name every arm that WROTE.

    `merged` and `entries_dropped` were missing from it, so a pass whose only
    change was an entry-level merge committed the PRE-sync snapshot and then
    left the merged file uncommitted in /config/.git indefinitely — until some
    later pass swept it up under a message about a different reconcile.
    """
    _write(tmp_path / "baseline", REL, _SHIPPED_V1)
    _write(tmp_path / "live", REL, _SHIPPED_V1 + _LOCAL_ADDITION)
    _write(tmp_path / "defaults", REL, _SHIPPED_V2)
    git = _FakeGit()

    report = _reconcile(tmp_path, git=git)

    assert len(report.merged) == 1 and not report.updated
    assert git.snapshots[-1] == "casa-sync: default reconcile v0.149.0"


def test_a_pass_that_only_wrote_a_RECOVERY_COPY_still_commits_it(tmp_path,
                                                                 monkeypatch):
    """A sidecar is a write too, and `.casabak` files live under `agents/**`,
    which the tracking whitelist admits.

    The merge takes its recovery copy BEFORE attempting the file write, so a
    write that then fails leaves a sidecar and nothing else — and with the
    recovery arms missing from the snapshot condition, that sidecar stayed
    uncommitted until some later pass swept it up under another reconcile's
    message.
    """
    _write(tmp_path / "baseline", REL, _SHIPPED_V1)
    _write(tmp_path / "live", REL, _SHIPPED_V1 + _LOCAL_ADDITION)
    _write(tmp_path / "defaults", REL, _SHIPPED_V2)
    git = _FakeGit()

    real = config_sync.atomic_write_text

    def failing(path, text, *a, **kw):
        if str(path).endswith(REL):
            raise OSError("disk full")
        return real(path, text, *a, **kw)

    monkeypatch.setattr(config_sync, "atomic_write_text", failing)
    report = _reconcile(tmp_path, git=git)

    assert report.merge_refused and not report.merged
    assert git.snapshots[-1] == "casa-sync: default reconcile v0.149.0"


def test_a_cyclic_live_file_falls_back_to_whole_file_resolution(tmp_path):
    """End to end: the refusal reaches `reconcile()` as a byte-level pass that
    completes, rather than as a `RecursionError` that aborts it."""
    _write(tmp_path / "baseline", REL, _SHIPPED_V1)
    _write(tmp_path / "live", REL, _CYCLIC)
    _write(tmp_path / "defaults", REL, _SHIPPED_V2)
    report = _reconcile(tmp_path)
    assert report.merged == []
    assert (tmp_path / "live" / REL).read_text() == _SHIPPED_V2
    assert [c["path"] for c in report.conflicts] == [REL]


def test_texts_that_do_not_parse_are_never_the_same_document():
    """The silent-skip generator, pinned at its source. "Neither side parsed"
    is not evidence that the two mean the same thing, and treating it as such
    is what makes a merge write nothing while reporting success."""
    assert config_sync._same_document("a: 1\n", "a:  1\n")     # same meaning
    assert not config_sync._same_document("a: [\n", "b: [\n")  # both broken
    assert not config_sync._same_document("a: [\n", "a: [\n")  # identical, broken
    assert not config_sync._same_document("a: 1\n", "b: [\n")


def test_entry_doc_never_raises_on_pathological_input():
    """`reconcile()` runs at boot. The gate folds every parse failure into
    None so a malformed file cannot raise out of the merge branch."""
    deeply_nested = "triggers: " + "[" * 5000 + "]" * 5000
    assert config_sync._entry_doc(deeply_nested, "triggers", "name") is None
    assert config_sync._entry_doc("\x00\x01binary", "triggers", "name") is None


def test_entry_doc_honours_the_identity_key():
    text = "schema_version: 1\ndelegates:\n  - agent: finance\n    purpose: p\n"
    assert config_sync._entry_doc(text, "delegates", "agent") is not None
    # Same document read with the wrong identity key has no identity at all.
    assert config_sync._entry_doc(text, "delegates", "name") is None


def test_entry_doc_accepts_an_empty_entry_list():
    """An empty list is well-formed: it means the operator removed everything,
    which the three-way table has rows for."""
    doc = config_sync._entry_doc("schema_version: 1\ntriggers: []\n",
                                 "triggers", "name")
    assert doc is not None and doc.entries == {}


# --------------------------------------------------------------------------
# The entry three-way (spec §6.2)
#
# One test per row. `baseline` mirrors the PREVIOUS image's defaults, so
# "in baseline" means precisely "the image owned this name at the last
# reconcile" — the only ownership evidence there is.
# --------------------------------------------------------------------------


def _doc(top=None, **entries):
    """An EntryDoc from `name=entry-dict` kwargs, in kwargs order."""
    return config_sync.EntryDoc(
        top_level=dict(top or {"schema_version": 1}),
        entries={k: {"name": k, **v} for k, v in entries.items()},
    )


def _merge(new, base, live):
    return config_sync._merge_entries(new, base, live, "name")


def test_untouched_entry_tracks_the_image():
    """defaults ✓ baseline ✓ live ✓, live == base."""
    merged, out = _merge(_doc(a={"v": 2}), _doc(a={"v": 1}), _doc(a={"v": 1}))
    assert merged["a"]["v"] == 2
    assert out.tracked_image == ["a"]
    assert not out.destroys_local()


def test_edited_entry_survives_when_the_image_did_not_change_it():
    """defaults ✓ baseline ✓ live ✓, live edited, image unchanged."""
    merged, out = _merge(_doc(a={"v": 1}), _doc(a={"v": 1}), _doc(a={"v": 9}))
    assert merged["a"]["v"] == 9
    assert out.kept_local == ["a"]
    assert not out.destroys_local()


def test_edited_entry_loses_to_a_changed_image_entry():
    """defaults ✓ baseline ✓ live ✓, both changed → image wins."""
    merged, out = _merge(_doc(a={"v": 2}), _doc(a={"v": 1}), _doc(a={"v": 9}))
    assert merged["a"]["v"] == 2
    assert out.conflicted == ["a"]
    assert out.destroys_local()


def test_locally_deleted_entry_stays_deleted_when_the_image_did_not_change_it():
    """defaults ✓ baseline ✓ live ✗, image unchanged.

    Deletion is an extreme edit and takes the same rule every other row takes:
    the image only overrides a local change when the image itself changed.
    """
    merged, out = _merge(_doc(a={"v": 1}), _doc(a={"v": 1}), _doc())
    assert "a" not in merged
    assert out.kept_local == ["a"]
    assert not out.destroys_local()


def test_locally_deleted_entry_returns_when_the_image_changed_it():
    """defaults ✓ baseline ✓ live ✗, image changed → image wins.

    Resolving deletion as "always sticks" would be a SECOND principle
    competing with image-wins; this keeps one.
    """
    merged, out = _merge(_doc(a={"v": 2}), _doc(a={"v": 1}), _doc())
    assert merged["a"]["v"] == 2
    assert out.reinserted == ["a"]
    assert out.destroys_local()


def test_name_collision_resolves_to_the_image_and_records_the_displacement():
    """defaults ✓ baseline ✗ live ✓ — no ownership evidence either way.

    The image reserves the names it ships. Both entries cannot share one
    identity and renaming either would break references to it. This is NOT
    ownership-neutral (INV-CFG-006 says so), which is why the displacement is
    recorded rather than merely resolved.
    """
    merged, out = _merge(_doc(a={"v": 2}), _doc(), _doc(a={"v": 9}))
    assert merged["a"]["v"] == 2
    assert out.displaced_local == ["a"]
    assert out.destroys_local()


def test_new_image_entry_is_inserted():
    """defaults ✓ baseline ✗ live ✗."""
    merged, out = _merge(_doc(a={"v": 1}), _doc(), _doc())
    assert merged["a"]["v"] == 1
    assert out.tracked_image == ["a"]


def test_untouched_entry_dropped_from_defaults_is_deleted():
    """defaults ✗ baseline ✓ live ✓, live == base.

    Recorded as `deleted` rather than `tracked_image`: both follow the image,
    but only one leaves the file shorter, and the report is read by someone
    asking where an entry went.
    """
    merged, out = _merge(_doc(), _doc(a={"v": 1}), _doc(a={"v": 1}))
    assert merged == {}
    assert out.deleted == ["a"]
    assert out.tracked_image == []
    assert not out.destroys_local()


def test_edited_entry_dropped_from_defaults_is_kept():
    """defaults ✗ baseline ✓ live ✓, live edited."""
    merged, out = _merge(_doc(), _doc(a={"v": 1}), _doc(a={"v": 9}))
    assert merged["a"]["v"] == 9
    assert out.kept_local == ["a"]
    assert not out.destroys_local()


def test_locally_added_entry_is_kept():
    """defaults ✗ baseline ✗ live ✓ — THE FIX.

    This is the only row whose outcome differs from what byte-level reconcile
    would produce for a file whose other entries are in conflict, and it is
    the whole point of #398.
    """
    merged, out = _merge(_doc(a={"v": 1}), _doc(a={"v": 1}),
                         _doc(a={"v": 1}, mine={"v": 7}))
    assert merged["mine"]["v"] == 7
    assert out.kept_local == ["mine"]
    assert not out.destroys_local()


def test_ordering_is_image_entries_then_local_additions():
    """Image entries in the NEW DEFAULT's order, then local-only entries in
    their live order. Chosen so the live file reads as "what the image ships,
    then what I added"."""
    new = _doc(one={}, two={})
    base = _doc(one={}, two={})
    # Live deliberately reverses the image order and interleaves its own, so a
    # merge that echoed live order (or sorted) would produce a different list.
    live = config_sync.EntryDoc(
        top_level={"schema_version": 1},
        entries={
            "mine_b": {"name": "mine_b"},
            "two": {"name": "two"},
            "mine_a": {"name": "mine_a"},
            "one": {"name": "one"},
        },
    )
    merged, _ = _merge(new, base, live)
    assert list(merged) == ["one", "two", "mine_b", "mine_a"]


def test_outcome_record_separates_every_disposition():
    new = _doc(track={"v": 2}, insert={"v": 1}, clash={"v": 1})
    base = _doc(track={"v": 1}, keep={"v": 1}, gone={"v": 1})
    live = _doc(track={"v": 1}, keep={"v": 9}, clash={"v": 9}, mine={"v": 1},
                gone={"v": 1})
    _, out = _merge(new, base, live)
    # `gone` is in baseline and untouched in live but absent from the new
    # defaults, so it is deleted — not "tracked". Record lists are sorted;
    # merged ORDER is a separate concern pinned above.
    assert out.tracked_image == ["insert", "track"]
    assert out.deleted == ["gone"]
    assert out.kept_local == ["keep", "mine"]
    assert out.displaced_local == ["clash"]
    assert out.conflicted == []
    assert out.destroys_local()


def test_a_purely_additive_merge_destroys_nothing():
    """The common shape on a real install: the image adds an entry, the
    operator has one of their own, nothing collides."""
    merged, out = _merge(_doc(a={"v": 1}, b={"v": 1}), _doc(a={"v": 1}),
                         _doc(a={"v": 1}, mine={"v": 1}))
    assert set(merged) == {"a", "b", "mine"}
    assert not out.destroys_local()


# --------------------------------------------------------------------------
# Composition, validation and write selection (spec §6.4)
# --------------------------------------------------------------------------

_LIVE_WITH_COMMENT = """# operator's own notes — must survive a no-op merge
schema_version: 1
triggers:
  - name: heartbeat
    type: interval
    minutes: 60
  - name: mine        # added by hand
    type: cron
    schedule: "0 9 * * *"
"""

_DEFAULT_SAME_HEARTBEAT = """schema_version: 1
triggers:
  - name: heartbeat
    type: interval
    minutes: 60
"""


def _write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _apply(tmp_path, *, default, baseline, live, validate_text=None):
    """Run just the merge-and-write step for one file."""
    rel = "agents/assistant/triggers.yaml"
    d, b, c = tmp_path / "def", tmp_path / "base", tmp_path / "cfg"
    _write(d, rel, default)
    _write(b, rel, baseline)
    _write(c, rel, live)
    result = config_sync._merge_file(
        rel=rel, defaults_dir=d, baseline_dir=b, config_dir=c,
        list_key="triggers", identity_key="name",
        validate_text=validate_text or (lambda _rel, _text: None),
    )
    # _merge_file COMPUTES; applying is the caller's job so that preservation
    # always precedes the write. Mirror that here.
    if result is not None:
        report = config_sync.SyncReport(image_version="test")
        config_sync._apply_merge(rel, result, report, d, c, lambda: "SHA1")
    return result, (c / rel), (d / rel)


def test_output_carries_the_live_documents_top_level(tmp_path):
    """INV-CFG-008 red case.

    The live file says `schema_version: 2` (what the configurator writes)
    while the shipped default says 1. A version-normalizing merge rewrites
    that; this one must not. Live-ahead-of-default is the ORDINARY state,
    which is why three review rounds died on drafts that normalized.
    """
    live = _LIVE_WITH_COMMENT.replace("schema_version: 1", "schema_version: 2")
    result, live_path, _ = _apply(
        tmp_path,
        default=_DEFAULT_SAME_HEARTBEAT.replace("minutes: 60", "minutes: 30"),
        baseline=_DEFAULT_SAME_HEARTBEAT,
        live=live,
    )
    assert result.wrote
    text = live_path.read_text()
    assert "schema_version: 2" in text
    assert "mine" in text


def test_a_no_op_merge_leaves_the_live_bytes_untouched(tmp_path):
    """Nothing to apply → nothing rewritten, so comments and formatting
    survive. This is the common case for an install with local entries and an
    unchanged shipped default."""
    before = _LIVE_WITH_COMMENT
    result, live_path, _ = _apply(
        tmp_path, default=_DEFAULT_SAME_HEARTBEAT,
        baseline=_DEFAULT_SAME_HEARTBEAT, live=before)
    assert not result.wrote
    assert live_path.read_text() == before
    assert "operator's own notes" in live_path.read_text()


def test_convergence_on_the_default_copies_the_defaults_bytes(tmp_path):
    """No local entries → the file ends byte-identical to the shipped default,
    exactly as today's byte-level reconcile would leave it. Keeps the blast
    radius of this change to installs that actually have local entries."""
    new_default = _DEFAULT_SAME_HEARTBEAT.replace("minutes: 60", "minutes: 30")
    result, live_path, default_path = _apply(
        tmp_path, default=new_default, baseline=_DEFAULT_SAME_HEARTBEAT,
        live=_DEFAULT_SAME_HEARTBEAT.replace("minutes: 60", "minutes: 45"))
    assert result.wrote
    assert live_path.read_bytes() == default_path.read_bytes()


def test_a_real_merge_writes_yaml_carrying_both_sides(tmp_path):
    result, live_path, _ = _apply(
        tmp_path,
        default=_DEFAULT_SAME_HEARTBEAT.replace("minutes: 60", "minutes: 30"),
        baseline=_DEFAULT_SAME_HEARTBEAT,
        live=_LIVE_WITH_COMMENT)
    assert result.wrote
    doc = config_sync._entry_doc(live_path.read_text(), "triggers", "name")
    assert doc is not None
    assert doc.entries["heartbeat"]["minutes"] == 30   # image tracked
    assert doc.entries["mine"]["schedule"] == "0 9 * * *"  # local kept
    assert list(doc.entries) == ["heartbeat", "mine"]


def test_falls_back_to_the_defaults_top_level_when_live_top_level_fails(tmp_path):
    """Live and default may legitimately carry different versions, and an
    entry authored under one may be invalid under the other. Rather than drop
    the offender, compose once under the other side's top level."""
    seen = []

    def validate_text(rel, text):
        doc = config_sync._parse(text)
        seen.append(doc["schema_version"])
        return "bad" if doc["schema_version"] == 2 else None

    result, live_path, _ = _apply(
        tmp_path,
        default=_DEFAULT_SAME_HEARTBEAT.replace("minutes: 60", "minutes: 30"),
        baseline=_DEFAULT_SAME_HEARTBEAT,
        live=_LIVE_WITH_COMMENT.replace("schema_version: 1", "schema_version: 2"),
        validate_text=validate_text)
    # Live's top level is tried first, then the default's. (A third call
    # follows: the belt-and-braces re-validation of the serialised bytes.)
    assert seen[:2] == [2, 1]
    assert result.wrote and not result.dropped
    assert "schema_version: 1" in live_path.read_text()
    assert "mine" in live_path.read_text()


def test_entries_failing_under_both_top_levels_are_dropped_and_named(tmp_path):
    def validate_text(rel, text):
        doc = config_sync._parse(text)
        names = [t["name"] for t in doc["triggers"]]
        return "bad entry" if "mine" in names else None

    result, live_path, _ = _apply(
        tmp_path,
        default=_DEFAULT_SAME_HEARTBEAT.replace("minutes: 60", "minutes: 30"),
        baseline=_DEFAULT_SAME_HEARTBEAT,
        live=_LIVE_WITH_COMMENT, validate_text=validate_text)
    assert result.wrote
    assert result.dropped == ["mine"]
    assert "mine" not in live_path.read_text()
    assert "heartbeat" in live_path.read_text()


def test_refuses_the_merge_when_nothing_validates(tmp_path):
    """An unwritten merge always beats an invalid one (INV-CFG-007). The
    caller then falls back to the byte-level branch."""
    result, live_path, _ = _apply(
        tmp_path,
        default=_DEFAULT_SAME_HEARTBEAT.replace("minutes: 60", "minutes: 30"),
        baseline=_DEFAULT_SAME_HEARTBEAT,
        live=_LIVE_WITH_COMMENT,
        validate_text=lambda rel, text: "always bad")
    assert result is None or not result.wrote
    assert result is None or result.refused
    assert live_path.read_text() == _LIVE_WITH_COMMENT   # untouched


def test_the_shape_gate_refuses_before_any_write(tmp_path):
    result, live_path, _ = _apply(
        tmp_path, default=_DEFAULT_SAME_HEARTBEAT,
        baseline=_DEFAULT_SAME_HEARTBEAT,
        live="schema_version: 1\ntriggers:\n  - name: dup\n  - name: dup\n")
    assert result is None
    assert "dup" in live_path.read_text()


def test_serialised_output_round_trips_through_the_gate(tmp_path):
    """Whatever the merge writes must itself be mergeable next time —
    otherwise the second reconcile silently degrades to byte-level."""
    _, live_path, _ = _apply(
        tmp_path,
        default=_DEFAULT_SAME_HEARTBEAT.replace("minutes: 60", "minutes: 30"),
        baseline=_DEFAULT_SAME_HEARTBEAT, live=_LIVE_WITH_COMMENT)
    again = config_sync._entry_doc(live_path.read_text(), "triggers", "name")
    assert again is not None and set(again.entries) == {"heartbeat", "mine"}


# --------------------------------------------------------------------------
# Wired into reconcile() (spec §6.4 step 1 / the conflict arm)
# --------------------------------------------------------------------------

_SHIPPED_V1 = """schema_version: 1
triggers:
  - name: heartbeat
    type: interval
    minutes: 60
    channel: telegram
    prompt: old shipped prompt
"""

_SHIPPED_V2 = """schema_version: 1
triggers:
  - name: heartbeat
    type: interval
    minutes: 60
    channel: telegram
    prompt: IMPROVED shipped prompt
"""

_LOCAL_ADDITION = """  - name: garbage-reminder
    type: cron
    schedule: "0 20 * * 2"
    channel: telegram
    prompt: put the bins out
"""


class _FakeGit:
    def __init__(self, available=True):
        self.available = available
        self.snapshots = []
    def snapshot(self, message):
        if not self.available:
            return None
        self.snapshots.append(message)
        return f"SHA{len(self.snapshots)}"
    def head(self):
        return None


def _reconcile(tmp_path, *, git=None, validate=None, validate_text=None):
    return config_sync.reconcile(
        defaults_dir=tmp_path / "defaults",
        config_dir=tmp_path / "live",
        baseline_dir=tmp_path / "baseline",
        image_version="v0.149.0",
        git=git or _FakeGit(),
        validate=validate or (lambda _rel: None),
        validate_text=validate_text or (lambda _rel, _text: None),
    )


REL = "agents/assistant/triggers.yaml"


def test_issue_398_a_locally_added_entry_survives_a_changed_shipped_default(tmp_path):
    """THE REGRESSION TEST. Spec §1, reproduced against the real reconcile().

    Before this change: `conflicts: [agents/assistant/triggers.yaml]`, and the
    locally-added entry is gone. This is the exact shape found on a live
    install — one configurator-authored cron entry, one qualifying update from
    silent deletion.
    """
    _write(tmp_path / "baseline", REL, _SHIPPED_V1)
    _write(tmp_path / "live", REL, _SHIPPED_V1 + _LOCAL_ADDITION)
    _write(tmp_path / "defaults", REL, _SHIPPED_V2)       # image changed too

    report = _reconcile(tmp_path)

    doc = config_sync._entry_doc(
        (tmp_path / "live" / REL).read_text(), "triggers", "name")
    assert doc is not None
    assert "garbage-reminder" in doc.entries, "the local entry was destroyed"
    assert doc.entries["heartbeat"]["prompt"].strip() == "IMPROVED shipped prompt", \
        "the image's improved entry did not arrive"
    assert report.conflicts == []
    assert not report.casabak


def test_merge_records_the_file_and_its_dispositions(tmp_path):
    _write(tmp_path / "baseline", REL, _SHIPPED_V1)
    _write(tmp_path / "live", REL, _SHIPPED_V1 + _LOCAL_ADDITION)
    _write(tmp_path / "defaults", REL, _SHIPPED_V2)
    report = _reconcile(tmp_path)
    assert len(report.merged) == 1
    rec = report.merged[0]
    assert rec["path"] == REL
    assert rec["tracked_image"] == ["heartbeat"]
    assert rec["kept_local"] == ["garbage-reminder"]


def test_a_merge_eligible_file_failing_the_gate_falls_back_to_byte_level(tmp_path):
    """Duplicate identities → not mergeable → today's behaviour exactly:
    conflict, image wins, whole file replaced."""
    dup = _SHIPPED_V1 + "  - name: heartbeat\n    type: cron\n"
    _write(tmp_path / "baseline", REL, _SHIPPED_V1)
    _write(tmp_path / "live", REL, dup)
    _write(tmp_path / "defaults", REL, _SHIPPED_V2)

    report = _reconcile(tmp_path)

    assert (tmp_path / "live" / REL).read_text() == _SHIPPED_V2
    assert [c["path"] for c in report.conflicts] == [REL]
    assert report.merged == []


def test_a_non_eligible_file_is_untouched_by_the_new_code(tmp_path):
    rel = "agents/assistant/runtime.yaml"
    _write(tmp_path / "baseline", rel, "OLD")
    _write(tmp_path / "live", rel, "EDITED")
    _write(tmp_path / "defaults", rel, "NEW")
    report = _reconcile(tmp_path)
    assert (tmp_path / "live" / rel).read_text() == "NEW"
    assert [c["path"] for c in report.conflicts] == [rel]
    assert report.merged == []


def test_merge_is_disabled_when_no_doc_validator_is_injected(tmp_path):
    """Fail closed. Without a way to validate what it would write, the merge
    must not run — INV-CFG-007 — so the file takes the byte-level branch."""
    _write(tmp_path / "baseline", REL, _SHIPPED_V1)
    _write(tmp_path / "live", REL, _SHIPPED_V1 + _LOCAL_ADDITION)
    _write(tmp_path / "defaults", REL, _SHIPPED_V2)
    report = config_sync.reconcile(
        defaults_dir=tmp_path / "defaults", config_dir=tmp_path / "live",
        baseline_dir=tmp_path / "baseline", image_version="v0.149.0",
        git=_FakeGit(), validate=lambda _rel: None,
    )
    assert [c["path"] for c in report.conflicts] == [REL]


def test_baseline_is_still_mirrored_and_the_report_round_trips(tmp_path):
    import json
    _write(tmp_path / "baseline", REL, _SHIPPED_V1)
    _write(tmp_path / "live", REL, _SHIPPED_V1 + _LOCAL_ADDITION)
    _write(tmp_path / "defaults", REL, _SHIPPED_V2)
    report = _reconcile(tmp_path)
    assert (tmp_path / "baseline" / REL).read_text() == _SHIPPED_V2
    assert json.loads(report.to_json())["merged"][0]["path"] == REL


def test_an_unchanged_default_still_leaves_the_live_file_alone(tmp_path):
    """No conflict arm reached, so the merge never runs and the operator's
    formatting survives — the same as before this change."""
    live = _SHIPPED_V1 + _LOCAL_ADDITION
    _write(tmp_path / "baseline", REL, _SHIPPED_V1)
    _write(tmp_path / "live", REL, live)
    _write(tmp_path / "defaults", REL, _SHIPPED_V1)
    report = _reconcile(tmp_path)
    assert (tmp_path / "live" / REL).read_text() == live
    assert report.merged == []


# --------------------------------------------------------------------------
# The entry-level backstop (spec §8) — release 1's answer to Path B
# --------------------------------------------------------------------------


def test_path_b_a_tightening_costs_only_the_entries_that_fail_it(tmp_path):
    """PATH B. Today a kept-live file failing the new schema is force-reset in
    full, taking every locally-added entry with it. Now it loses only the
    entries that are actually invalid.

    Note the shipped default is UNCHANGED here — that is what makes this
    Path B rather than Path A, and why merging alone would not have covered it.
    """
    _write(tmp_path / "baseline", REL, _SHIPPED_V1)
    _write(tmp_path / "live", REL, _SHIPPED_V1 + _LOCAL_ADDITION + """  - name: bad-entry
    type: nonsense
""")
    _write(tmp_path / "defaults", REL, _SHIPPED_V1)

    def validate(rel):
        return "invalid" if rel == REL else None

    def validate_text(rel, text):
        doc = config_sync._parse(text) or {}
        names = [t.get("name") for t in doc.get("triggers", [])]
        return "invalid" if "bad-entry" in names else None

    report = _reconcile(tmp_path, validate=validate, validate_text=validate_text)

    doc = config_sync._entry_doc(
        (tmp_path / "live" / REL).read_text(), "triggers", "name")
    assert doc is not None
    assert "garbage-reminder" in doc.entries, "a valid local entry was destroyed"
    assert "heartbeat" in doc.entries
    assert "bad-entry" not in doc.entries
    assert report.schema_forced == []
    assert report.entries_dropped == [
        {"path": REL, "names": ["bad-entry"],
         "reason": "invalid against the current schema"},
    ]


def test_backstop_drop_preserves_the_prior_file(tmp_path):
    _write(tmp_path / "baseline", REL, _SHIPPED_V1)
    live = _SHIPPED_V1 + "  - name: bad-entry\n    type: nonsense\n"
    _write(tmp_path / "live", REL, live)
    _write(tmp_path / "defaults", REL, _SHIPPED_V1)
    _reconcile(
        tmp_path, validate=lambda rel: "invalid" if rel == REL else None,
        validate_text=lambda rel, text: (
            "invalid" if "bad-entry" in
            [t.get("name") for t in
             (config_sync._parse(text) or {}).get("triggers", [])]
            else None))
    bak = tmp_path / "live" / (REL + ".casabak")
    assert bak.read_text() == live


def test_backstop_falls_back_to_whole_file_when_the_gate_refuses(tmp_path):
    """Not mergeable → today's behaviour: force the default."""
    _write(tmp_path / "baseline", REL, _SHIPPED_V1)
    _write(tmp_path / "live", REL,
           "schema_version: 1\ntriggers:\n  - name: dup\n  - name: dup\n")
    _write(tmp_path / "defaults", REL, _SHIPPED_V1)
    report = _reconcile(tmp_path,
                        validate=lambda rel: "invalid" if rel == REL else None)
    assert (tmp_path / "live" / REL).read_text() == _SHIPPED_V1
    assert [c["path"] for c in report.schema_forced] == [REL]


def test_backstop_falls_back_when_no_entry_survives(tmp_path):
    _write(tmp_path / "baseline", REL, _SHIPPED_V1)
    _write(tmp_path / "live", REL, _SHIPPED_V1 + _LOCAL_ADDITION)
    _write(tmp_path / "defaults", REL, _SHIPPED_V1)
    report = _reconcile(tmp_path,
                        validate=lambda rel: "invalid" if rel == REL else None,
                        validate_text=lambda rel, text: "everything is invalid")
    assert (tmp_path / "live" / REL).read_text() == _SHIPPED_V1
    assert [c["path"] for c in report.schema_forced] == [REL]


def test_backstop_keeps_whole_file_behaviour_for_non_eligible_files(tmp_path):
    rel = "agents/assistant/runtime.yaml"
    _write(tmp_path / "baseline", rel, "OLD")
    _write(tmp_path / "live", rel, "EDITED")
    _write(tmp_path / "defaults", rel, "OLD")
    report = _reconcile(tmp_path,
                        validate=lambda r: "invalid" if r == rel else None)
    assert (tmp_path / "live" / rel).read_text() == "OLD"
    assert [c["path"] for c in report.schema_forced] == [rel]


# --------------------------------------------------------------------------
# Preservation and reporting (spec §8)
# --------------------------------------------------------------------------


def test_a_purely_additive_merge_is_backed_up_but_does_not_alert(tmp_path):
    """Preservation is unconditional; ALERTING is not.

    Five review rounds each found a different write that the old
    "is this destructive?" test called safe — the last being the top-level
    fields replaced while every entry outcome looked additive. The test was
    the defect, so the backup is now taken whenever the merge writes. What
    stays narrow is what the operator is TOLD: a merge that only added the
    image's entries alongside theirs is the ordinary case, and alerting on it
    would train them to ignore the alert.
    """
    git = _FakeGit()
    live = _SHIPPED_V1 + _LOCAL_ADDITION
    _write(tmp_path / "baseline", REL, _SHIPPED_V1)
    _write(tmp_path / "live", REL, live)
    _write(tmp_path / "defaults", REL, _SHIPPED_V2)
    report = _reconcile(tmp_path, git=git)

    assert (tmp_path / "live" / (REL + ".casabak")).read_text() == live
    assert report.merge_backup == [REL]
    assert report.casabak == [], "an additive merge is not an overwrite"
    assert not report.has_overwrites(), "the operator must not be alerted"


def test_replacing_the_live_top_level_is_preserved_and_reported(tmp_path):
    """The round-5 finding: composing under the DEFAULT's top level rewrites
    the operator's `schema_version` while every entry outcome reads as
    additive. Byte-level resolution preserved and reported the same change,
    so this must too.
    """
    git = _FakeGit()
    live = (_SHIPPED_V1 + _LOCAL_ADDITION).replace("schema_version: 1",
                                                   "schema_version: 2")
    _write(tmp_path / "baseline", REL, _SHIPPED_V1)
    _write(tmp_path / "live", REL, live)
    _write(tmp_path / "defaults", REL, _SHIPPED_V2)

    # Only the default's top level validates, so the merge falls back to it.
    def validate_text(rel, text):
        doc = config_sync._parse(text) or {}
        return "bad" if doc.get("schema_version") == 2 else None

    report = _reconcile(tmp_path, git=git, validate_text=validate_text)

    assert "schema_version: 1" in (tmp_path / "live" / REL).read_text()
    assert (tmp_path / "live" / (REL + ".casabak")).read_text() == live, \
        "the operator's top level was replaced with nothing to recover it from"
    assert git.snapshots


def test_a_displacing_merge_writes_a_sidecar_even_though_git_succeeded(tmp_path):
    """Deliberately unlike the byte-level rule, which writes a `.casabak` only
    when git is unavailable. A commit inside /config/.git is invisible to the
    person who lost the entry, and invisibility is the complaint in #398.
    """
    git = _FakeGit(available=True)
    _write(tmp_path / "baseline", REL, _SHIPPED_V1)
    live = _SHIPPED_V1 + _LOCAL_ADDITION
    _write(tmp_path / "live", REL, live)
    # The image now ships an entry whose name the operator already used.
    _write(tmp_path / "defaults", REL, _SHIPPED_V2 + """  - name: garbage-reminder
    type: interval
    minutes: 5
    channel: telegram
    prompt: shipped
""")
    report = _reconcile(tmp_path, git=git)
    assert (tmp_path / "live" / (REL + ".casabak")).read_text() == live
    assert report.casabak == [REL]
    assert report.merged[0]["displaced_local"] == ["garbage-reminder"]
    assert report.merged[0]["pre_sync_sha"] is not None
    assert report.has_overwrites()


def test_has_overwrites_covers_dropped_entries(tmp_path):
    r = config_sync.SyncReport(image_version="v")
    assert not r.has_overwrites()
    r.merged.append({"path": REL, "displaced_local": [], "conflicted": [],
                     "reinserted": [], "tracked_image": [], "deleted": [],
                     "kept_local": ["mine"], "pre_sync_sha": None})
    assert not r.has_overwrites(), "an additive merge is not an overwrite"
    r.entries_dropped.append({"path": REL, "names": ["x"], "reason": "r"})
    assert r.has_overwrites()


def test_a_destructive_merge_alone_sets_has_overwrites():
    r = config_sync.SyncReport(image_version="v")
    r.merged.append({"path": REL, "displaced_local": ["mine"],
                     "conflicted": [], "reinserted": [], "tracked_image": [],
                     "deleted": [], "kept_local": [], "pre_sync_sha": "SHA1"})
    assert r.has_overwrites()


def test_the_sidecar_is_invisible_to_a_second_reconcile(tmp_path):
    """`_list_tree_files` skips `.casabak`, so the sidecar never becomes an
    adopted config file of its own."""
    _write(tmp_path / "baseline", REL, _SHIPPED_V1)
    _write(tmp_path / "live", REL, _SHIPPED_V1 + _LOCAL_ADDITION)
    _write(tmp_path / "defaults", REL, _SHIPPED_V2)
    _reconcile(tmp_path)
    after_first = (tmp_path / "live" / REL).read_text()
    report2 = _reconcile(tmp_path)
    assert (tmp_path / "live" / REL).read_text() == after_first
    assert report2.merged == [] and report2.conflicts == []


# --------------------------------------------------------------------------
# Preconditions for #402 (the deferred migration registry)
#
# These pass today and change no behaviour. They exist because tightening a
# schema in place is boot-fatal through paths the reconciler cannot repair,
# and the failure is currently SILENT. Whoever implements #402 should trip a
# test rather than a boot loop.
# --------------------------------------------------------------------------


def test_an_adopted_file_that_fails_its_schema_is_reported(tmp_path):
    """A resident `triggers.yaml` with no shipped default (only `assistant`
    ships one) has no baseline, so reconcile ADOPTS it and the backstop skips
    it — `rel not in new_files` means there is nothing to fall back to.

    `agent_loader` then validates it at boot and raises, stopping the process.
    Nothing can repair it here, but it must not be silent: #402 tightens a
    schema, and this is the class of file that makes that boot-fatal.
    """
    rel = "agents/butler/triggers.yaml"
    _write(tmp_path / "live", rel, _SHIPPED_V1)      # no default, no baseline
    report = _reconcile(tmp_path,
                        validate=lambda r: "invalid" if r == rel else None)
    assert (tmp_path / "live" / rel).exists(), "an adopted file is never deleted"
    assert [a["path"] for a in report.adopted_invalid] == [rel]
    assert "next boot" in report.adopted_invalid[0]["detail"].lower() or \
        "invalid" in report.adopted_invalid[0]["detail"].lower()


def test_an_empty_baseline_leaves_only_default_less_files_unvalidated(tmp_path):
    """Destroying /data (which holds the baseline) while /config survives — an
    ordinary add-on reinstall — leaves `base_files` empty, so every file takes
    the adopt branch in the main loop.

    The backstop still catches those that HAVE a shipped default, because it
    keys on `new_files` rather than on the baseline. What stays unvalidated is
    exactly the default-less file, which is the same hole as the test above
    rather than a second one — worth pinning so #402 does not go looking for
    two separate fixes.
    """
    withdefault = REL
    defaultless = "agents/butler/triggers.yaml"
    _write(tmp_path / "live", withdefault, _SHIPPED_V1 + _LOCAL_ADDITION)
    _write(tmp_path / "defaults", withdefault, _SHIPPED_V1)
    _write(tmp_path / "live", defaultless, _SHIPPED_V1)
    # No baseline directory at all.

    report = _reconcile(
        tmp_path, validate=lambda r: "invalid",
        validate_text=lambda r, text: (
            "invalid" if "garbage-reminder" in
            [t.get("name") for t in
             (config_sync._parse(text) or {}).get("triggers", [])]
            else None))

    # Has a default → the backstop reached it and salvaged what it could.
    assert [d["path"] for d in report.entries_dropped] == [withdefault]
    # No default → nothing could reach it; it is reported, not repaired.
    assert [a["path"] for a in report.adopted_invalid] == [defaultless]


def test_a_valid_adopted_file_is_not_reported(tmp_path):
    rel = "agents/butler/triggers.yaml"
    _write(tmp_path / "live", rel, _SHIPPED_V1)
    report = _reconcile(tmp_path)
    assert report.adopted_invalid == []


# --------------------------------------------------------------------------
# Review findings (diff review round 1) — red cases
# --------------------------------------------------------------------------


def test_preservation_happens_before_the_write(tmp_path, monkeypatch):
    """Commit-first is the module's whole safety property, and an earlier
    draft inverted it for the merge path: it wrote the merged file and
    recorded afterwards, so a crash between the two lost the local content
    with neither the snapshot nor the sidecar taken.

    Simulated by making the write fail: preservation must already have
    happened by then.
    """
    git = _FakeGit()
    _write(tmp_path / "baseline", REL, _SHIPPED_V1)
    # A displaced entry AND a surviving local one, so the result differs from
    # the shipped default and takes the merged-write path rather than the
    # copy-the-default's-bytes path.
    live = _SHIPPED_V1 + _LOCAL_ADDITION + """  - name: mine-only
    type: cron
    schedule: "0 6 * * *"
    channel: telegram
    prompt: mine
"""
    _write(tmp_path / "live", REL, live)
    _write(tmp_path / "defaults", REL, _SHIPPED_V2 + """  - name: garbage-reminder
    type: interval
    minutes: 5
    channel: telegram
    prompt: shipped
""")

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(config_sync, "atomic_write_text", boom)
    report = _reconcile(tmp_path, git=git)

    # The write never happened, but what it would have destroyed is safe.
    assert (tmp_path / "live" / (REL + ".casabak")).read_text() == live
    assert git.snapshots, "no pre-sync snapshot was taken before the write"
    # And the failure is contained to this file rather than aborting the pass.
    assert [m["path"] for m in report.merge_refused] == [REL]


def test_a_later_stage_does_not_clobber_an_earlier_sidecar(tmp_path):
    """The FIRST sidecar in a run holds what the operator actually authored.
    A second write for the same file would only hold this run's own
    intermediate output, which is not a recovery artifact at all."""
    report = config_sync.SyncReport(image_version="v")
    rel = REL
    (tmp_path / rel).parent.mkdir(parents=True)
    config_sync._write_casabak(rel, "ORIGINAL", tmp_path, report)
    config_sync._write_casabak(rel, "INTERMEDIATE", tmp_path, report)
    assert (tmp_path / (rel + ".casabak")).read_text() == "ORIGINAL"
    assert report.casabak == [rel]


def test_the_file_validator_reports_an_unreadable_file_instead_of_raising(tmp_path):
    """`_read_yaml` opens the file itself and guards only YAMLError. #398
    widened the set of paths the backstop validates to include ADOPTED files,
    so an escape here would abort the whole reconcile pass — skipping every
    later file and the baseline mirror — rather than costing one file."""
    rel = "agents/assistant/triggers.yaml"
    p = tmp_path / rel
    p.parent.mkdir(parents=True)
    p.write_bytes(b"\xff\xfe not utf-8 \xff")
    err = config_sync._make_validator(tmp_path)(rel)
    assert err and "cannot read" in err


def test_reconcile_survives_an_unreadable_adopted_file(tmp_path):
    """The whole-pass consequence of the above, end to end: a later file is
    still reconciled and the baseline is still mirrored."""
    bad = "agents/butler/triggers.yaml"
    (tmp_path / "live" / bad).parent.mkdir(parents=True)
    (tmp_path / "live" / bad).write_bytes(b"\xff\xfe \xff")
    good = "agents/assistant/runtime.yaml"
    _write(tmp_path / "baseline", good, "OLD")
    _write(tmp_path / "live", good, "OLD")
    _write(tmp_path / "defaults", good, "NEW")

    report = config_sync.reconcile(
        defaults_dir=tmp_path / "defaults", config_dir=tmp_path / "live",
        baseline_dir=tmp_path / "baseline", image_version="v",
        git=_FakeGit(), validate=config_sync._make_validator(tmp_path / "live"),
        validate_text=lambda _r, _t: None,
    )
    assert (tmp_path / "live" / good).read_text() == "NEW"
    assert (tmp_path / "baseline" / good).read_text() == "NEW"
    assert [a["path"] for a in report.adopted_invalid] == [bad]


# --------------------------------------------------------------------------
# Review findings (diff review round 2) — red cases
# --------------------------------------------------------------------------


def test_salvage_never_drops_an_entry_the_image_ships(tmp_path):
    """Local content may be sacrificed here — preserved and reported. The
    image's own entries may not: byte-level resolution would have delivered
    them by copying the default wholesale, so salvaging around one silently
    withholds what the update was carrying.

    Reachable when live and default carry different schema versions and
    neither complete composition validates.
    """
    _write(tmp_path / "baseline", REL, _SHIPPED_V1)
    _write(tmp_path / "live", REL, _SHIPPED_V1 + _LOCAL_ADDITION)
    shipped_new = _SHIPPED_V2 + """  - name: shipped-webhook
    type: webhook
    channel: telegram
"""
    _write(tmp_path / "defaults", REL, shipped_new)

    # The image's new entry is the one that fails; the local one is fine.
    def validate_text(rel, text):
        doc = config_sync._parse(text) or {}
        names = [t.get("name") for t in doc.get("triggers", [])]
        return "invalid" if "shipped-webhook" in names else None

    report = _reconcile(tmp_path, validate_text=validate_text)

    # Refused the merge → byte-level took it → the shipped entry arrived.
    assert (tmp_path / "live" / REL).read_text() == shipped_new
    assert [c["path"] for c in report.conflicts] == [REL]
    assert report.merged == []


def test_backstop_salvage_never_drops_an_entry_the_image_ships(tmp_path):
    """Same rule on the backstop's salvage, which has to consult the defaults
    itself because it runs outside the three-way."""
    shipped = _SHIPPED_V1 + """  - name: shipped-bad
    type: webhook
    channel: telegram
"""
    _write(tmp_path / "baseline", REL, shipped)
    _write(tmp_path / "live", REL, shipped + _LOCAL_ADDITION)
    _write(tmp_path / "defaults", REL, shipped)

    def validate_text(rel, text):
        doc = config_sync._parse(text) or {}
        names = [t.get("name") for t in doc.get("triggers", [])]
        return "invalid" if "shipped-bad" in names else None

    report = _reconcile(
        tmp_path, validate=lambda r: "invalid" if r == REL else None,
        validate_text=validate_text)
    assert (tmp_path / "live" / REL).read_text() == shipped
    assert [c["path"] for c in report.schema_forced] == [REL]
    assert report.entries_dropped == []


def test_a_destructive_merge_is_refused_when_nothing_can_preserve_it(tmp_path,
                                                                    monkeypatch):
    """Git unavailable AND the sidecar unwritable. Proceeding would destroy
    local content with nothing to recover it from — the one outcome the
    commit-first design exists to prevent."""
    _write(tmp_path / "baseline", REL, _SHIPPED_V1)
    live = _SHIPPED_V1 + _LOCAL_ADDITION
    _write(tmp_path / "live", REL, live)
    _write(tmp_path / "defaults", REL, _SHIPPED_V2 + """  - name: garbage-reminder
    type: interval
    minutes: 5
    channel: telegram
    prompt: shipped
""")

    def no_sidecar(*_a, **_k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(Path, "write_text", no_sidecar)
    report = _reconcile(tmp_path, git=_FakeGit(available=False))

    assert (tmp_path / "live" / REL).read_text() == live, \
        "the file was overwritten with no way to recover the prior entry"
    assert [m["path"] for m in report.merge_refused] == [REL]


def test_a_refused_merge_stays_refused_through_the_backstop(tmp_path, monkeypatch):
    """A fix that voids its own verdict: refusing in the main loop is
    worthless if the backstop then destroys the same entries with a smaller
    write that succeeds where the backup did not."""
    _write(tmp_path / "baseline", REL, _SHIPPED_V1)
    live = _SHIPPED_V1 + _LOCAL_ADDITION
    _write(tmp_path / "live", REL, live)
    _write(tmp_path / "defaults", REL, _SHIPPED_V2 + """  - name: garbage-reminder
    type: interval
    minutes: 5
    channel: telegram
    prompt: shipped
""")

    monkeypatch.setattr(Path, "write_text",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("ro")))
    # The merge composes fine and IS destructive (the image claims the local
    # name), so the main loop refuses for want of a recovery artifact. The
    # backstop then finds the file schema-invalid and would otherwise salvage
    # it — destroying exactly what the refusal protected.
    report = _reconcile(
        tmp_path, git=_FakeGit(available=False),
        validate=lambda r: "invalid" if r == REL else None,
        validate_text=lambda r, t: None)

    assert (tmp_path / "live" / REL).read_text() == live
    assert [m["path"] for m in report.merge_refused] == [REL]
    assert report.entries_dropped == []
    assert report.schema_forced == []


def test_a_refused_merge_holds_the_baseline_back(tmp_path, monkeypatch):
    """Advancing the baseline would record the new defaults as reconciled when
    they are not, so the next boot would see no divergence and never retry."""
    _write(tmp_path / "baseline", REL, _SHIPPED_V1)
    _write(tmp_path / "live", REL, _SHIPPED_V1 + _LOCAL_ADDITION)
    _write(tmp_path / "defaults", REL, _SHIPPED_V2 + """  - name: garbage-reminder
    type: interval
    minutes: 5
    channel: telegram
    prompt: shipped
""")
    monkeypatch.setattr(Path, "write_text",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("ro")))
    report = _reconcile(tmp_path, git=_FakeGit(available=False))

    assert report.merge_refused
    assert (tmp_path / "baseline" / REL).read_text() == _SHIPPED_V1, \
        "the baseline advanced past a file that was never reconciled"


def test_the_baseline_hold_is_per_file_not_whole_tree(tmp_path, monkeypatch):
    """Holding the WHOLE baseline would anchor successfully reconciled files
    to stale defaults. A later operator edit to one of those would then read
    as "both sides changed" and be overwritten as a conflict, even though the
    image had not moved since. Only the refused file is held back.
    """
    other = "agents/butler/delegates.yaml"
    _write(tmp_path / "baseline", REL, _SHIPPED_V1)
    _write(tmp_path / "live", REL, _SHIPPED_V1 + _LOCAL_ADDITION)
    _write(tmp_path / "defaults", REL, _SHIPPED_V2 + """  - name: garbage-reminder
    type: interval
    minutes: 5
    channel: telegram
    prompt: shipped
""")
    _write(tmp_path / "baseline", other, "schema_version: 1\ndelegates: []\n")
    _write(tmp_path / "live", other, "schema_version: 1\ndelegates: []\n")
    _write(tmp_path / "defaults", other,
           "schema_version: 1\ndelegates:\n  - agent: finance\n")

    monkeypatch.setattr(Path, "write_text",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("ro")))
    report = _reconcile(tmp_path, git=_FakeGit(available=False))

    assert [m["path"] for m in report.merge_refused] == [REL]
    # Refused → held back so the next boot retries.
    assert (tmp_path / "baseline" / REL).read_text() == _SHIPPED_V1
    # Reconciled fine → its baseline MUST advance.
    assert "finance" in (tmp_path / "baseline" / other).read_text()


def test_a_merge_write_failure_does_not_abort_the_whole_pass(tmp_path, monkeypatch):
    """Propagating was deliberate while it was the only thing stopping the
    baseline advancing past an unreconciled file. The per-file hold now gives
    that guarantee directly, so a failed write costs one file instead of every
    file after it — including the baseline mirror for all of them."""
    other = "agents/butler/delegates.yaml"
    _write(tmp_path / "baseline", REL, _SHIPPED_V1)
    _write(tmp_path / "live", REL, _SHIPPED_V1 + _LOCAL_ADDITION)
    _write(tmp_path / "defaults", REL, _SHIPPED_V2)
    _write(tmp_path / "baseline", other, "schema_version: 1\ndelegates: []\n")
    _write(tmp_path / "live", other, "schema_version: 1\ndelegates: []\n")
    _write(tmp_path / "defaults", other,
           "schema_version: 1\ndelegates:\n  - agent: finance\n")

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(config_sync, "atomic_write_text", boom)
    report = _reconcile(tmp_path)

    assert [m["path"] for m in report.merge_refused] == [REL]
    # The unrelated file was still reconciled, and its baseline advanced.
    assert "finance" in (tmp_path / "live" / other).read_text()
    assert "finance" in (tmp_path / "baseline" / other).read_text()
    # The failed one is held back so the next boot retries it.
    assert (tmp_path / "baseline" / REL).read_text() == _SHIPPED_V1


def test_a_salvage_write_failure_is_contained_like_a_merge_write(tmp_path,
                                                                monkeypatch):
    """The containment rule applies to every write the merge branch makes, not
    just the one it was first written for — the salvage in the backstop was
    missed on the first pass."""
    _write(tmp_path / "baseline", REL, _SHIPPED_V1)
    live = _SHIPPED_V1 + _LOCAL_ADDITION + "  - name: bad\n    type: nonsense\n"
    _write(tmp_path / "live", REL, live)
    _write(tmp_path / "defaults", REL, _SHIPPED_V1)

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(config_sync, "atomic_write_text", boom)
    report = _reconcile(
        tmp_path, validate=lambda r: "invalid" if r == REL else None,
        validate_text=lambda r, t: (
            "invalid" if "bad" in
            [e.get("name") for e in
             (config_sync._parse(t) or {}).get("triggers", [])]
            else None))

    assert (tmp_path / "live" / REL).read_text() == live
    assert [m["path"] for m in report.merge_refused] == [REL]
    assert report.schema_forced == []


def test_a_top_level_override_reaches_the_report_even_without_a_sidecar(tmp_path):
    """`overrode_local()` decides where the sidecar is filed, so when the
    sidecar cannot be written the override has to be visible in the merge
    record itself — otherwise the operator is told nothing at all."""
    live = (_SHIPPED_V1 + _LOCAL_ADDITION).replace("schema_version: 1",
                                                   "schema_version: 2")
    _write(tmp_path / "baseline", REL, _SHIPPED_V1)
    _write(tmp_path / "live", REL, live)
    _write(tmp_path / "defaults", REL, _SHIPPED_V2)
    report = _reconcile(
        tmp_path,
        validate_text=lambda r, t: (
            "bad" if (config_sync._parse(t) or {}).get(
                "schema_version") == 2 else None))

    assert report.merged[0]["top_level_changed"] is True
    assert report.has_overwrites()
    assert REL in report.destroyed_paths()


# --------------------------------------------------------------------------
# Cross-file consistency (diff review round 8) — a NEW shape, not preservation
# --------------------------------------------------------------------------


def test_a_merge_that_leaves_the_tree_boot_invalid_is_reverted(tmp_path):
    """Individually valid, jointly invalid.

    Preserving a local delegate entry while `runtime.yaml` is byte-level
    updated to drop the delegate tool produces two files that each pass their
    own schema and together stop the agent loading. Per-file validation cannot
    see it; the existing post-sync boot-parity pass can — but its heal is
    guarded on the file being byte-equal to the default, precisely so it never
    deletes operator content, so a MERGED file was invisible to it.

    Reverting is safe without asking because the merge captured the prior
    content before writing.
    """
    rel = "agents/assistant/delegates.yaml"
    shipped_old = ("schema_version: 1\ndelegates:\n"
                   "  - agent: finance\n    purpose: p\n    when: w\n")
    shipped_new = "schema_version: 1\ndelegates: []\n"
    live = shipped_old + "  - agent: mine\n    purpose: p\n    when: w\n"
    _write(tmp_path / "baseline", rel, shipped_old)
    _write(tmp_path / "live", rel, live)
    _write(tmp_path / "defaults", rel, shipped_new)

    errors = ["agent 'assistant': delegates.yaml is non-empty but "
              "runtime.yaml tools.allowed is missing 'mcp__casa__delegate'"]

    def validate_repo():
        # Fails while the live file still has delegates; clean once reverted.
        doc = config_sync._parse(
            (tmp_path / "live" / rel).read_text()) or {}
        return errors if doc.get("delegates") else []

    report = config_sync.reconcile(
        defaults_dir=tmp_path / "defaults", config_dir=tmp_path / "live",
        baseline_dir=tmp_path / "baseline", image_version="v",
        git=_FakeGit(), validate=lambda _r: None,
        validate_text=lambda _r, _t: None, validate_repo=validate_repo,
    )

    assert (tmp_path / "live" / rel).read_text() == shipped_new, \
        "the merge left the tree unable to boot"
    assert rel in report.post_sync_healed
    assert report.post_sync_errors == []
    # The preserved entries are still recoverable.
    assert (tmp_path / "live" / (rel + ".casabak")).read_text() == live


def test_a_no_op_merge_is_preserved_before_the_heal_reverts_it(tmp_path):
    """A merge whose result equals the live file writes nothing, so it has no
    sidecar yet. Reverting it without one would silently destroy exactly the
    entries the merge had preserved."""
    rel = "agents/assistant/delegates.yaml"
    shipped_old = ("schema_version: 1\ndelegates:\n"
                   "  - agent: finance\n    purpose: p\n    when: w\n")
    live = ("schema_version: 1\ndelegates:\n"
            "  - agent: finance\n    purpose: EDITED\n    when: w\n")
    _write(tmp_path / "baseline", rel, shipped_old)
    _write(tmp_path / "live", rel, live)
    _write(tmp_path / "defaults", rel, "schema_version: 1\ndelegates: []\n")

    def validate_repo():
        doc = config_sync._parse(
            (tmp_path / "live" / rel).read_text()) or {}
        return ["agent 'assistant': broken"] if doc.get("delegates") else []

    report = config_sync.reconcile(
        defaults_dir=tmp_path / "defaults", config_dir=tmp_path / "live",
        baseline_dir=tmp_path / "baseline", image_version="v",
        git=_FakeGit(), validate=lambda _r: None,
        validate_text=lambda _r, _t: None, validate_repo=validate_repo,
    )
    assert rel in report.post_sync_healed
    assert (tmp_path / "live" / (rel + ".casabak")).read_text() == live, \
        "the heal reverted content that was never backed up"
    assert report.post_sync_errors == []


def test_the_heal_reverts_any_merged_file_not_just_delegates(tmp_path):
    """Two review rounds found two siblings of the same cross-file class
    (delegates vs the delegate tool; triggers vs a removed channel), so the
    heal does not enumerate file types — any merged file is revertible."""
    _write(tmp_path / "baseline", REL, _SHIPPED_V1)
    live = _SHIPPED_V1 + _LOCAL_ADDITION
    _write(tmp_path / "live", REL, live)
    _write(tmp_path / "defaults", REL, _SHIPPED_V2)

    def validate_repo():
        doc = config_sync._parse(
            (tmp_path / "live" / REL).read_text()) or {}
        names = [t.get("name") for t in doc.get("triggers", [])]
        return (["agent 'assistant': trigger channel is not declared"]
                if "garbage-reminder" in names else [])

    report = config_sync.reconcile(
        defaults_dir=tmp_path / "defaults", config_dir=tmp_path / "live",
        baseline_dir=tmp_path / "baseline", image_version="v",
        git=_FakeGit(), validate=lambda _r: None,
        validate_text=lambda _r, _t: None, validate_repo=validate_repo,
    )
    assert (tmp_path / "live" / REL).read_text() == _SHIPPED_V2
    assert REL in report.post_sync_healed
    assert report.post_sync_errors == []
    assert (tmp_path / "live" / (REL + ".casabak")).read_text() == live


def test_the_heal_reverts_every_merge_including_unimplicated_ones(tmp_path):
    """The heal reverts ALL merges together rather than searching for the
    guilty one. This test states the cost rather than hiding it: a merge that
    was not the cause is reverted too.

    An earlier draft tried candidates one at a time and restored the ones that
    did not help. That was more precise and strictly worse in a boot path,
    because the restore is itself a write that can fail — leaving the shipped
    default live while the report says the trial was abandoned. This version
    has no such half-state. The cost is bounded: the entries are in the
    sidecar and the pre-sync commit, and every revert is reported.
    """
    innocent = "agents/aaa/delegates.yaml"
    culprit = "agents/zzz/delegates.yaml"
    base = ("schema_version: 1\ndelegates:\n"
            "  - agent: a\n    purpose: p\n    when: w\n")
    live_extra = "  - agent: mine\n    purpose: p\n    when: w\n"
    for rel in (innocent, culprit):
        _write(tmp_path / "baseline", rel, base)
        _write(tmp_path / "live", rel, base + live_extra)
        _write(tmp_path / "defaults", rel, base.replace("purpose: p",
                                                        "purpose: NEW"))

    def validate_repo():
        doc = config_sync._parse(
            (tmp_path / "live" / culprit).read_text()) or {}
        names = [d.get("agent") for d in doc.get("delegates", [])]
        return ["boom"] if "mine" in names else []

    report = config_sync.reconcile(
        defaults_dir=tmp_path / "defaults", config_dir=tmp_path / "live",
        baseline_dir=tmp_path / "baseline", image_version="v",
        git=_FakeGit(), validate=lambda _r: None,
        validate_text=lambda _r, _t: None, validate_repo=validate_repo,
    )

    assert report.post_sync_errors == []          # the tree loads: the point
    assert sorted(report.post_sync_healed) == sorted([innocent, culprit])
    for rel in (innocent, culprit):
        assert "mine" not in (tmp_path / "live" / rel).read_text()
        assert live_extra in (tmp_path / "live" / (rel + ".casabak")).read_text()
        assert rel in report.casabak


def test_a_validator_exception_is_not_read_as_a_healthy_tree(tmp_path):
    """Folding an exception into "no errors" would let a validator crash
    authorise a revert on the strength of a tree nobody actually checked."""
    _write(tmp_path / "baseline", REL, _SHIPPED_V1)
    _write(tmp_path / "live", REL, _SHIPPED_V1 + _LOCAL_ADDITION)
    _write(tmp_path / "defaults", REL, _SHIPPED_V2)

    calls = {"n": 0}

    def validate_repo():
        calls["n"] += 1
        if calls["n"] == 1:
            return []          # clean: nothing to heal, nothing to revert
        raise RuntimeError("validator exploded")

    report = config_sync.reconcile(
        defaults_dir=tmp_path / "defaults", config_dir=tmp_path / "live",
        baseline_dir=tmp_path / "baseline", image_version="v",
        git=_FakeGit(), validate=lambda _r: None,
        validate_text=lambda _r, _t: None, validate_repo=validate_repo,
    )
    assert report.post_sync_healed == []
    assert "garbage-reminder" in (tmp_path / "live" / REL).read_text()


def test_a_committed_revert_is_reported_as_destructive(tmp_path):
    """An additive merge files its sidecar under `merge_backup`, which does
    not notify. Once it is reverted the entries really are gone, so it must be
    promoted — otherwise the one case where a kept entry vanished is the one
    case nobody is told about."""
    _write(tmp_path / "baseline", REL, _SHIPPED_V1)
    live = _SHIPPED_V1 + _LOCAL_ADDITION
    _write(tmp_path / "live", REL, live)
    _write(tmp_path / "defaults", REL, _SHIPPED_V2)

    def validate_repo():
        doc = config_sync._parse(
            (tmp_path / "live" / REL).read_text()) or {}
        names = [t.get("name") for t in doc.get("triggers", [])]
        return ["boom"] if "garbage-reminder" in names else []

    report = config_sync.reconcile(
        defaults_dir=tmp_path / "defaults", config_dir=tmp_path / "live",
        baseline_dir=tmp_path / "baseline", image_version="v",
        git=_FakeGit(), validate=lambda _r: None,
        validate_text=lambda _r, _t: None, validate_repo=validate_repo,
    )
    assert report.casabak == [REL]
    assert report.merge_backup == []
    assert report.merged[0]["reverted"] is True
    assert report.has_overwrites(), "the operator would not be notified"


def test_a_stale_error_does_not_revert_an_unrelated_merge(tmp_path):
    """Round 10: the byte-equal delegates heal fixes the tree, but acting on
    the pre-heal error list would then revert an unrelated merge for a fault
    already repaired."""
    reseeded = "agents/butler/delegates.yaml"
    merged = REL
    default_delegates = ("schema_version: 1\ndelegates:\n"
                         "  - agent: a\n    purpose: p\n    when: w\n")
    # Byte-equal to the default → the existing heal deletes it.
    _write(tmp_path / "defaults", reseeded, default_delegates)
    _write(tmp_path / "live", reseeded, default_delegates)
    _write(tmp_path / "baseline", merged, _SHIPPED_V1)
    _write(tmp_path / "live", merged, _SHIPPED_V1 + _LOCAL_ADDITION)
    _write(tmp_path / "defaults", merged, _SHIPPED_V2)

    def validate_repo():
        if (tmp_path / "live" / reseeded).exists():
            return ["agent 'butler': delegates.yaml is non-empty but "
                    "runtime.yaml tools.allowed is missing 'x'"]
        return []

    report = config_sync.reconcile(
        defaults_dir=tmp_path / "defaults", config_dir=tmp_path / "live",
        baseline_dir=tmp_path / "baseline", image_version="v",
        git=_FakeGit(), validate=lambda _r: None,
        validate_text=lambda _r, _t: None, validate_repo=validate_repo,
    )
    assert report.post_sync_errors == []
    assert "garbage-reminder" in (tmp_path / "live" / merged).read_text(), \
        "an unrelated merge was reverted for an already-repaired fault"


def test_an_unreadable_file_is_never_reverted_over_an_empty_backup(tmp_path,
                                                                   monkeypatch):
    """`_read_or_none` used to return "" on a read failure, so the heal wrote
    an EMPTY sidecar, concluded the content was preserved, and overwrote the
    real file with the shipped default — unrecoverably. Preservation has to be
    able to fail."""
    _write(tmp_path / "baseline", REL, _SHIPPED_V1)
    live = _SHIPPED_V1 + _LOCAL_ADDITION
    _write(tmp_path / "live", REL, live)
    _write(tmp_path / "defaults", REL, _SHIPPED_V2)

    real_read = Path.read_text
    state = {"merged": False}

    def flaky(self, *a, **k):
        # Fail only the heal's re-read of the live file, after the merge wrote.
        if state["merged"] and self.name == "triggers.yaml" and "live" in str(self):
            raise OSError("transient read failure")
        return real_read(self, *a, **k)

    def validate_repo():
        state["merged"] = True
        return ["boom"]

    monkeypatch.setattr(Path, "read_text", flaky)
    report = config_sync.reconcile(
        defaults_dir=tmp_path / "defaults", config_dir=tmp_path / "live",
        baseline_dir=tmp_path / "baseline", image_version="v",
        git=_FakeGit(available=False), validate=lambda _r: None,
        validate_text=lambda _r, _t: None, validate_repo=validate_repo,
    )
    monkeypatch.undo()

    assert report.post_sync_healed == [], "reverted without a readable backup"
    bak = tmp_path / "live" / (REL + ".casabak")
    assert not bak.exists() or bak.read_text() != "", "an empty backup was accepted"
    # A revert the heal could not PREPARE holds the baseline too, not only one
    # whose copy failed — otherwise the next boot sees image == baseline,
    # records no merge, and the cross-file fault persists forever.
    assert [m["path"] for m in report.merge_refused] == [REL]
    assert (tmp_path / "baseline" / REL).read_text() == _SHIPPED_V1


def test_the_file_validator_survives_a_non_enumerated_exception(tmp_path,
                                                                monkeypatch):
    """Enumerating exception types is how the ValueError out of env
    substitution was missed. A file this cannot read or parse IS invalid."""
    import agent_loader as al
    rel = "agents/assistant/triggers.yaml"
    _write(tmp_path, rel, _SHIPPED_V1)
    monkeypatch.setattr(
        al, "_read_yaml",
        lambda _p: (_ for _ in ()).throw(ValueError("bad substitution")))
    err = config_sync._make_validator(tmp_path)(rel)
    assert err and "cannot read" in err


def test_a_failed_revert_is_reported_and_holds_its_baseline(tmp_path,
                                                            monkeypatch):
    """`shutil.copy2` writes content then metadata, so a failure can leave the
    file already overwritten. Accounting the revert only on success meant a
    destructive overwrite could go unreported; and mirroring the baseline
    before the heal meant the next boot saw image == baseline, re-derived
    nothing, and a transient error became a permanent boot failure."""
    _write(tmp_path / "baseline", REL, _SHIPPED_V1)
    _write(tmp_path / "live", REL, _SHIPPED_V1 + _LOCAL_ADDITION)
    _write(tmp_path / "defaults", REL, _SHIPPED_V2)

    def boom(*_a, **_k):
        raise OSError("copystat failed after writing the content")

    monkeypatch.setattr(config_sync, "_copy", boom)
    report = config_sync.reconcile(
        defaults_dir=tmp_path / "defaults", config_dir=tmp_path / "live",
        baseline_dir=tmp_path / "baseline", image_version="v",
        git=_FakeGit(), validate=lambda _r: None,
        validate_text=lambda _r, _t: None,
        validate_repo=lambda: ["boom"],
    )

    assert REL in report.post_sync_healed, "an attempted revert went unreported"
    assert report.has_overwrites(), "the operator would not be notified"
    assert [m["path"] for m in report.merge_refused] == [REL]
    assert (tmp_path / "baseline" / REL).read_text() == _SHIPPED_V1, \
        "the baseline advanced past a revert that did not complete"


def test_a_raising_merge_falls_back_to_byte_level_instead_of_aborting(tmp_path,
                                                                     monkeypatch):
    """`yaml.safe_dump` and jsonschema can both raise on pathologically nested
    content the shape gate accepts — it checks that an entry is a mapping with
    a string identity, not how deep its values go. An escape would abort the
    pass mid-loop, skipping every later file. One containment point, same rule
    as the gate: any irregularity means byte-level."""
    _write(tmp_path / "baseline", REL, _SHIPPED_V1)
    _write(tmp_path / "live", REL, _SHIPPED_V1 + _LOCAL_ADDITION)
    _write(tmp_path / "defaults", REL, _SHIPPED_V2)
    other = "agents/butler/delegates.yaml"
    _write(tmp_path / "baseline", other, "OLD")
    _write(tmp_path / "live", other, "OLD")
    _write(tmp_path / "defaults", other, "NEW")

    def boom(*_a, **_k):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(config_sync, "_dump", boom)
    report = _reconcile(tmp_path)

    # Byte-level took the file, exactly as before this change.
    assert (tmp_path / "live" / REL).read_text() == _SHIPPED_V2
    assert [c["path"] for c in report.conflicts] == [REL]
    # ...and the pass was not aborted: later files still reconciled.
    assert (tmp_path / "live" / other).read_text() == "NEW"
    assert (tmp_path / "baseline" / other).read_text() == "NEW"


def test_a_raising_salvage_falls_back_to_the_force_default(tmp_path, monkeypatch):
    _write(tmp_path / "baseline", REL, _SHIPPED_V1)
    _write(tmp_path / "live", REL, _SHIPPED_V1 + _LOCAL_ADDITION)
    _write(tmp_path / "defaults", REL, _SHIPPED_V1)

    real = config_sync._drop_invalid_entries

    def boom(**_k):
        raise RecursionError("boom")

    monkeypatch.setattr(config_sync, "_drop_invalid_entries", boom)
    report = _reconcile(tmp_path,
                        validate=lambda r: "invalid" if r == REL else None)
    assert (tmp_path / "live" / REL).read_text() == _SHIPPED_V1
    assert [c["path"] for c in report.schema_forced] == [REL]
    monkeypatch.setattr(config_sync, "_drop_invalid_entries", real)


# --------------------------------------------------------------------------
# Live-verification finding: the reconciler's own sidecar broke the loader
# --------------------------------------------------------------------------


def test_the_reconcilers_own_sidecar_does_not_make_an_agent_unloadable(tmp_path):
    """The gap that let a live-only bug through.

    Every other test in this file injects a FAKE `validate_repo`, so none of
    them ever asked the real loader what it thinks of a directory the
    reconciler has just written. It thought the `.casabak` was an unknown file
    and refused to load the agent — so the merge's own recovery copy triggered
    the boot-parity heal, which reverted the merge and destroyed the preserved
    entry. The feature defeated itself the first time it ran on real data.

    Asserted against `_check_file_set` directly, which is the function that
    raises. An earlier version of this test called `validate_config_repo` and
    filtered its errors for "casabak" — with an invalid fixture it never
    reached the file-set check, and filtering-then-asserting-empty passes
    vacuously, so it went green against the unfixed code.
    """
    import agent_loader as al

    role_dir = tmp_path / "agents" / "assistant"
    role_dir.mkdir(parents=True)
    for name in ("character.yaml", "voice.yaml", "response_shape.yaml",
                 "disclosure.yaml", "runtime.yaml", "triggers.yaml"):
        (role_dir / name).write_text("schema_version: 1\n")
    (role_dir / "triggers.yaml.casabak").write_text(_SHIPPED_V1)

    # Raises LoadError("unknown file(s) in directory") without the fix.
    al._check_file_set(str(role_dir), "resident", "assistant")


def test_the_placeholder_gate_uses_the_LOADER_S_OWN_pattern():
    """Not a restated copy. A second pattern is one edit away from disagreeing
    with the loader about what a placeholder is, and disagreeing in the miss
    direction costs an operator an entry silently."""
    import config
    assert config_sync._ENV_RE is config._ENV_RE
