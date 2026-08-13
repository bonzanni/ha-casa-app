"""#215 — the procedural epoch: digest inputs, stability, and the archive
epoch filter."""

from types import SimpleNamespace

import pytest

from executor_epoch import (
    compute_procedural_epoch, epoch_application_tag, make_archive_epoch_filter,
)

pytestmark = [pytest.mark.unit]


def _defn(tmp_path, *, driver="in_casa", checksum="rc-1",
          prompt_name="prompt.md", doctrine=True):
    exec_dir = tmp_path / "executors" / "configurator"
    exec_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = exec_dir / prompt_name
    if not prompt_path.exists():
        prompt_path.write_text("Do the thing.\n{task}\n{executor_memory}\n")
    doctrine_dir = ""
    if doctrine:
        ddir = exec_dir / "doctrine"
        ddir.mkdir(exist_ok=True)
        if not (ddir / "safety.md").exists():
            (ddir / "safety.md").write_text("never rm -rf\n")
        doctrine_dir = str(ddir)
    return SimpleNamespace(
        role_checksum=checksum,
        prompt_template_path=str(prompt_path),
        doctrine_dir=doctrine_dir,
        driver=driver,
    )


def _epoch(defn):
    with open(defn.prompt_template_path, encoding="utf-8") as fh:
        return compute_procedural_epoch(defn, prompt_template=fh.read())


class TestDigestInputs:
    def test_stable_across_identical_launches(self, tmp_path):
        defn = _defn(tmp_path)
        assert _epoch(defn) == _epoch(defn)

    def test_doctrine_edit_changes_epoch(self, tmp_path):
        """Design r1 (Sol S-B1): role_checksum alone missed the doctrine dir
        — the digest must move when a doctrine file changes."""
        defn = _defn(tmp_path)
        before = _epoch(defn)
        (tmp_path / "executors" / "configurator" / "doctrine" / "safety.md"
         ).write_text("new procedure\n")
        assert _epoch(defn) != before

    def test_new_doctrine_file_changes_epoch(self, tmp_path):
        defn = _defn(tmp_path)
        before = _epoch(defn)
        (tmp_path / "executors" / "configurator" / "doctrine" / "reload.md"
         ).write_text("reload steps\n")
        assert _epoch(defn) != before

    def test_prompt_indirection_changes_epoch(self, tmp_path):
        """Design r2 (Sol/Terra convergent): repointing prompt_template_file
        to a different template with different bytes is a procedural change."""
        defn = _defn(tmp_path)
        before = _epoch(defn)
        alt = tmp_path / "executors" / "configurator" / "prompt-alt.md"
        alt.write_text("Do a DIFFERENT thing.\n{task}\n")
        defn.prompt_template_path = str(alt)
        assert _epoch(defn) != before

    def test_role_checksum_changes_epoch(self, tmp_path):
        defn = _defn(tmp_path)
        before = _epoch(defn)
        defn.role_checksum = "rc-2"
        assert _epoch(defn) != before

    def test_workspace_template_source_is_input_for_claude_code(self, tmp_path):
        """Design r4/r5 (Terra): the SOURCE workspace instruction bytes are an
        input for claude_code — .tmpl when present, the plain-copy CLAUDE.md
        fallback otherwise — and only for claude_code."""
        defn = _defn(tmp_path, driver="claude_code")
        ws = tmp_path / "executors" / "configurator" / "workspace-template"
        ws.mkdir()
        (ws / "CLAUDE.md.tmpl").write_text("workspace rules v1 {task}\n")
        before = _epoch(defn)
        (ws / "CLAUDE.md.tmpl").write_text("workspace rules v2 {task}\n")
        assert _epoch(defn) != before

        # Fallback: no .tmpl, a plain CLAUDE.md is consumed and hashed.
        (ws / "CLAUDE.md.tmpl").unlink()
        (ws / "CLAUDE.md").write_text("plain workspace rules v1\n")
        plain_before = _epoch(defn)
        (ws / "CLAUDE.md").write_text("plain workspace rules v2\n")
        assert _epoch(defn) != plain_before

        # in_casa ignores the workspace template entirely.
        in_casa = _defn(tmp_path, driver="in_casa")
        e1 = _epoch(in_casa)
        (ws / "CLAUDE.md").write_text("plain workspace rules v3\n")
        assert _epoch(in_casa) == e1

    def test_doctrineless_executor_digests(self, tmp_path):
        defn = _defn(tmp_path, doctrine=False)
        assert _epoch(defn) == _epoch(defn)


class TestArchiveEpochFilter:
    def _hit(self, tags):
        return SimpleNamespace(application_tags=tuple(tags))

    def test_keeps_matching_drops_mismatched_and_untagged(self):
        """#215 red case: on the pre-fix tree a mismatched-epoch summary was
        injected into the executor prompt; the filter must keep ONLY the
        current epoch's lessons."""
        tag = epoch_application_tag("configurator", "a" * 64)
        other = epoch_application_tag("configurator", "b" * 64)
        keep = self._hit(["private", tag])
        stale = self._hit(["private", other])
        legacy = self._hit(["private"])          # pre-#215 item: no epoch tag
        out = make_archive_epoch_filter(tag)((keep, stale, legacy))
        assert out == (keep,)

    def test_cross_type_lessons_dropped(self):
        tag_cfg = epoch_application_tag("configurator", "a" * 64)
        tag_dev = epoch_application_tag("plugin-developer", "a" * 64)
        out = make_archive_epoch_filter(tag_cfg)((self._hit([tag_dev]),))
        assert out == ()

    def test_empty_epoch_tag_matches_nothing_stamped(self):
        """A legacy record without a persisted epoch filters everything —
        never mislabels (claude_code resume path)."""
        tag = epoch_application_tag("configurator", "")
        stamped = self._hit([epoch_application_tag("configurator", "a" * 64)])
        assert make_archive_epoch_filter(tag)((stamped,)) == ()
