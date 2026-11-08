"""#215 — the procedural epoch: digest inputs and stability. The archive's
epoch filter is pinned in tests/test_executor_memory_tiers.py."""

from types import SimpleNamespace

import pytest

from executor_epoch import compute_procedural_epoch

pytestmark = [pytest.mark.unit]


def _defn(tmp_path, *, checksum="rc-1",
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

    def test_doctrineless_executor_digests(self, tmp_path):
        defn = _defn(tmp_path, doctrine=False)
        assert _epoch(defn) == _epoch(defn)
