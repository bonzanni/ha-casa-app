"""#398 release 2 — the agent's own entries inside a role's triggers.yaml.

The subject changed: this file used to test a private, agent-owned
``reminders.yaml`` that only this module wrote. It now tests a writer editing
the OPERATOR's shared trigger file, which changes what must be proven:

* an operator entry must survive every operation byte-for-byte in meaning,
  including a malformed one the module cannot even read;
* ownership comes from ``managed_by`` alone — never the ``reminder-`` prefix,
  the ``date`` type or the ``one_shot`` flag, each of which an operator may
  legitimately author;
* **``remove_entry`` must never refuse for a reason ``past_due`` tolerates.**
  The sweep delivers what ``past_due`` selects and then removes it, so any
  check in one and not the other means the sweep can deliver an entry it cannot
  clean up — and redelivers it every five minutes forever.
"""
from __future__ import annotations

import pathlib
from datetime import datetime, timedelta, timezone

import pytest
import yaml

import reminders

pytestmark = pytest.mark.unit

CEST = timezone(timedelta(hours=2))
NOW = datetime(2026, 8, 3, 12, 0, tzinfo=CEST)
OVERDUE = "2026-08-03T08:00:00+02:00"
LATER = "2099-08-03T20:00:00+02:00"

HEARTBEAT = {"name": "heartbeat", "type": "interval", "minutes": 60,
             "channel": "telegram", "prompt": "hb"}


def _write(tmp_path, triggers=(HEARTBEAT,), version=1):
    p = tmp_path / "triggers.yaml"
    p.write_text(yaml.safe_dump({"schema_version": version,
                                 "triggers": list(triggers)},
                                sort_keys=False), encoding="utf-8")
    return str(p)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _names(path):
    return [e.get("name") if isinstance(e, dict) else e
            for e in _read(path)["triggers"]]


def _mine(name="reminder-a1b2c3d4", at=LATER, **over):
    entry = {"name": name, "type": "date", "at": at, "one_shot": True,
             "channel": "telegram", "prompt": 'Send this: "Bins."',
             "managed_by": "agent"}
    entry.update(over)
    return entry


def _operators_lookalike(name="reminder-bins", at=OVERDUE):
    """An entry wearing every mark inference used to read as agent-owned.

    The schema permits exactly this, which is why ownership must be data.
    """
    return {"name": name, "type": "date", "at": at, "one_shot": True,
            "channel": "telegram", "prompt": "operator's own"}


# --- add_entry -------------------------------------------------------------


class TestAddEntry:

    def test_it_appends_and_preserves_the_operator_entries(self, tmp_path):
        path = _write(tmp_path, [HEARTBEAT, _operators_lookalike()])
        reminders.add_entry(path, _mine())
        assert _names(path) == ["heartbeat", "reminder-bins",
                                "reminder-a1b2c3d4"]

    def test_every_operator_entry_keeps_its_resolved_meaning(self, tmp_path):
        """The whole point of sharing the file: the operator's configuration
        must mean the same thing afterwards. Compared through the LOADER's own
        view, which is what boot actually reads."""
        import agent_loader
        path = _write(tmp_path, [HEARTBEAT, _operators_lookalike()])
        before = agent_loader._read_yaml(path)["triggers"]

        reminders.add_entry(path, _mine())

        after = agent_loader._read_yaml(path)["triggers"]
        assert after[:len(before)] == before
        assert len(after) == len(before) + 1

    def test_it_keeps_an_existing_schema_version(self, tmp_path):
        path = _write(tmp_path, version=1)
        reminders.add_entry(path, _mine())
        assert _read(path)["schema_version"] == 1

    def test_a_first_reminder_creates_the_file_at_version_2(self, tmp_path):
        """``triggers.yaml`` is optional for a resident, so the first reminder
        for a role may have to create it. 2 is what the configurator's own add
        recipe writes, and it keeps a future schema tightening (#402) from
        being boot-fatal on a file this writer created."""
        path = str(tmp_path / "triggers.yaml")
        reminders.add_entry(path, _mine())
        assert _read(path) == {"schema_version": 2, "triggers": [_mine()]}

    def test_it_refuses_an_entry_it_would_not_own(self, tmp_path):
        """``managed_by`` is written HERE or the entry is not ours to manage —
        an unmarked entry would be invisible to the sweep and to cancellation,
        i.e. a reminder that can never be delivered late nor removed."""
        path = _write(tmp_path)
        entry = _mine()
        del entry["managed_by"]
        with pytest.raises(ValueError, match="managed_by"):
            reminders.add_entry(path, entry)
        assert _names(path) == ["heartbeat"]

    def test_it_refuses_a_duplicate_of_an_OPERATOR_name(self, tmp_path):
        """``register_agent`` raises on a duplicate name and that is uncaught
        at boot — a crash loop, not a lost reminder."""
        path = _write(tmp_path)
        with pytest.raises(ValueError, match="already exists"):
            reminders.add_entry(path, _mine(name="heartbeat"))
        assert _names(path) == ["heartbeat"]

    def test_it_refuses_a_placeholder_in_the_reminders_own_text(self, tmp_path):
        """A stored ``${VAR}`` is substituted by the loader at boot, so the
        reminder would not say what the user asked for."""
        path = _write(tmp_path)
        with pytest.raises(ValueError, match=r"\$\{\.\.\.\}"):
            reminders.add_entry(path, _mine(prompt="Send ${HOME}"))
        assert _names(path) == ["heartbeat"]

    def test_a_refusal_writes_nothing_at_all(self, tmp_path):
        """Fail closed: no partial write, no truncated file."""
        path = _write(tmp_path, [HEARTBEAT, _operators_lookalike()])
        before = pathlib.Path(path).read_bytes()
        with pytest.raises(ValueError):
            reminders.add_entry(path, _mine(name="heartbeat"))
        assert pathlib.Path(path).read_bytes() == before

    def test_a_schema_violation_surfaces_as_ValueError(self, tmp_path):
        """``agent_loader._validate`` raises ``LoadError``, a direct
        ``Exception`` subclass that ``set_reminder`` does not catch (Sol r2 #2).
        Unfolded it would escape as an unstructured crash instead of the
        ``write_failed`` result the tool promises."""
        path = _write(tmp_path)
        with pytest.raises(ValueError):
            reminders.add_entry(path, {"name": "reminder-x", "type": "date",
                                       "managed_by": "agent"})
        assert _names(path) == ["heartbeat"]

    def test_a_PRE_EXISTING_schema_failure_does_not_block_the_write(
            self, tmp_path):
        """The other arm of Sol r2 #1, which named only ``remove_entry``.

        The running process holds the snapshot it booted with, so the file on
        disk can already be invalid while Casa runs: ``config_git_commit``
        refuses a commit failing the boot-parity check and leaves the working
        tree edited. Refusing then protects nothing — that file already fails to
        load — while making reminders unavailable for the role and naming an
        entry the user never touched.

        Red case: schema-validate the whole candidate document and this raises,
        citing ``half-edited`` — an entry the reminder has nothing to do with.
        """
        import agent_loader
        path = _write(tmp_path, [
            HEARTBEAT,
            # A cron entry missing its schedule: the shape a refused
            # configurator commit leaves behind.
            {"name": "half-edited", "type": "cron", "channel": "telegram",
             "prompt": "x"},
        ])
        assert agent_loader._validate.__name__  # the loader is the authority
        with pytest.raises(agent_loader.LoadError):
            agent_loader._validate(agent_loader._read_yaml(path), "triggers",
                                   path)

        reminders.add_entry(path, _mine())      # must NOT raise

        assert _names(path) == ["heartbeat", "half-edited",
                                "reminder-a1b2c3d4"]

    def test_a_failure_THIS_write_causes_is_still_refused(self, tmp_path):
        """The distinction that keeps the above from being a hole: when the
        prior document was fine, a new complaint is ours and refuses."""
        path = _write(tmp_path)
        with pytest.raises(ValueError, match="schema violation"):
            reminders.add_entry(path, _mine(at=None))
        assert _names(path) == ["heartbeat"]

    def test_an_invalid_entry_is_refused_EVEN_beside_a_pre_existing_defect(
            self, tmp_path):
        """Terra impl r2 — the inverse case, and the hole in the first fix.

        The first attempt at the permissive path compared the whole candidate
        against the whole PRIOR document, so *any* pre-existing complaint waved
        through *any* new one: both documents fail, so "the prior fails too" read
        as "this write is blameless" — while the write was in fact adding a
        second, brand-new boot defect that would outlive the operator fixing the
        first.

        Judging the ADDED ENTRY alone answers the question directly instead.

        Red case: infer blamelessness from whole-document failure and this write
        succeeds, persisting an invalid reminder.
        """
        path = _write(tmp_path, [
            HEARTBEAT,
            {"name": "half-edited", "type": "cron", "channel": "telegram",
             "prompt": "x"},                      # pre-existing defect
        ])
        before = pathlib.Path(path).read_bytes()

        with pytest.raises(ValueError, match="schema violation"):
            reminders.add_entry(path, _mine(at=None))   # invalid: date, no `at`

        assert pathlib.Path(path).read_bytes() == before

    def test_a_pre_existing_TOP_LEVEL_defect_does_refuse(self, tmp_path):
        """The boundary of the rule above, pinned so it is not mistaken for a
        leak (Sol + Terra, impl r3).

        The added entry is judged under the document's REAL top level, because
        ``schema_version`` decides what is legal. So a defect in the top level
        itself is not separable from the judgment and does refuse — unlike a
        defect in a sibling entry, which does not. Deliberate: such a file cannot
        boot either way, so neither refusing nor writing helps the operator, and
        the alternative is validating the entry against a top level the file does
        not actually have.
        """
        import yaml as _yaml
        p = tmp_path / "triggers.yaml"
        p.write_text(_yaml.safe_dump({
            "schema_version": 1, "operator_note": "an unknown root key",
            "triggers": [HEARTBEAT]}, sort_keys=False), encoding="utf-8")
        before = p.read_bytes()

        with pytest.raises(ValueError, match="root"):
            reminders.add_entry(str(p), _mine())

        assert p.read_bytes() == before

    def test_a_VALID_entry_still_lands_beside_a_pre_existing_defect(
            self, tmp_path):
        """And the permissive path must survive the fix above — otherwise the
        cure re-creates the block it was meant to remove."""
        path = _write(tmp_path, [
            HEARTBEAT,
            {"name": "half-edited", "type": "cron", "channel": "telegram",
             "prompt": "x"},
        ])
        reminders.add_entry(path, _mine())
        assert "reminder-a1b2c3d4" in _names(path)

    def test_a_BROKEN_SCHEMA_fails_closed_instead_of_waving_the_write_through(
            self, tmp_path, monkeypatch):
        """The prior-document comparison must not become a validation bypass.

        If validation itself cannot run, treating that as an ordinary
        "this document is invalid" verdict would silently disable the one check
        that stops a boot-breaking file being written. Only a schema VERDICT
        (``LoadError``) is a statement about the document; anything else means
        the check did not happen, and must refuse.

        Red case: fold every exception into a returned complaint and this write
        succeeds with no validation at all.
        """
        import agent_loader
        path = _write(tmp_path)

        def broken(*a, **k):
            raise RuntimeError("schema file is corrupt")

        monkeypatch.setattr(agent_loader, "_validate", broken)
        with pytest.raises(ValueError, match="cannot validate"):
            reminders.add_entry(path, _mine())
        assert _names(path) == ["heartbeat"]

    def test_the_written_entry_loads_back_through_the_real_loader(
            self, tmp_path):
        """A reminder the loader would reject at boot is not durable — and a
        writer validated only against its own idea of the schema is exactly how
        that ships."""
        import agent_loader
        path = _write(tmp_path)
        reminders.add_entry(path, _mine())
        doc = agent_loader._read_yaml(path)
        agent_loader._validate(doc, "triggers", path)
        specs = agent_loader._build_triggers(doc, agent_dir=str(tmp_path))
        assert [s.managed_by for s in specs] == ["", "agent"]


# --- remove_entry ----------------------------------------------------------


class TestRemoveEntry:

    def test_it_removes_only_that_entry(self, tmp_path):
        path = _write(tmp_path, [HEARTBEAT, _mine()])
        assert reminders.remove_entry(path, "reminder-a1b2c3d4") == "removed"
        assert _names(path) == ["heartbeat"]

    def test_an_absent_name_is_not_found(self, tmp_path):
        path = _write(tmp_path)
        assert reminders.remove_entry(path, "reminder-nope0000") == "not_found"

    def test_a_missing_file_is_not_found(self, tmp_path):
        path = str(tmp_path / "triggers.yaml")
        assert reminders.remove_entry(path, "reminder-nope0000") == "not_found"
        assert not pathlib.Path(path).exists(), "must not create the file"

    def test_an_operator_trigger_is_not_owned_and_is_untouched(self, tmp_path):
        path = _write(tmp_path)
        before = pathlib.Path(path).read_bytes()
        assert reminders.remove_entry(path, "heartbeat") == "not_owned"
        assert pathlib.Path(path).read_bytes() == before

    def test_an_operator_LOOKALIKE_is_not_owned(self, tmp_path):
        """THE pin. This entry carries the reserved prefix, ``type: date`` and
        ``one_shot: true`` — every mark three rounds of #396 findings inferred
        ownership from. Only the absent ``managed_by`` distinguishes it."""
        path = _write(tmp_path, [_operators_lookalike()])
        before = pathlib.Path(path).read_bytes()
        assert reminders.remove_entry(path, "reminder-bins") == "not_owned"
        assert pathlib.Path(path).read_bytes() == before

    def test_an_unrelated_duplicate_name_does_not_block_removal(
            self, tmp_path):
        """Sol r2 #1. A whole-file duplicate check would raise here, and the
        sweep — which has ALREADY delivered — would redeliver every five
        minutes forever. The running process can hold a valid snapshot while
        the on-disk file is invalid: ``config_git_commit`` refuses such a
        commit but leaves the working tree edited.

        Red case: reinstate a whole-file duplicate check and this raises while
        ``past_due`` below still selects the entry.
        """
        path = _write(tmp_path, [
            HEARTBEAT, dict(HEARTBEAT, minutes=30), _mine(at=OVERDUE)])
        assert reminders.past_due(path, NOW), "precondition: sweep would deliver"

        assert reminders.remove_entry(path, "reminder-a1b2c3d4") == "removed"

        assert _names(path) == ["heartbeat", "heartbeat"], \
            "the operator's duplicates are preserved, not silently deduped"
        assert reminders.past_due(path, NOW) == [], "no redelivery next pass"

    def test_removal_tolerates_everything_past_due_tolerates(self, tmp_path):
        """The module's standing rule, stated as one test. Any state in which
        ``past_due`` yields an entry must be one in which that entry can be
        removed."""
        path = _write(tmp_path, [
            "a bare string, not a mapping",
            {"name": ["not", "a", "string"]},
            dict(HEARTBEAT, one_shot=1),
            _mine(at=OVERDUE),
        ])
        assert [e["name"] for e in reminders.past_due(path, NOW)] == [
            "reminder-a1b2c3d4"]
        assert reminders.remove_entry(path, "reminder-a1b2c3d4") == "removed"
        # Every malformed operator entry is still there — skipped by selection,
        # never dropped from the document.
        assert len(_read(path)["triggers"]) == 3


# --- the ${VAR} door -------------------------------------------------------


class TestEnvPlaceholderDoor:
    """``safe_dump`` re-emits a lone-placeholder string UNQUOTED, and the writer
    cannot rewrite such a file without risking an existing entry.

    #409 changed WHY, and it is worth stating because that issue expected this
    door to close with it. It no longer truncates: resolution happens inside the
    YAML constructor, so the value is not re-lexed into the document. What
    survives is that QUOTING decides whether a scalar that is NOTHING BUT a
    placeholder is text — quoted it is a string whatever it holds, plain it has
    its value read back — and a rewrite cannot preserve a style it can no longer
    see. An entry authored as `prompt: "${DETAIL}"` therefore still changes
    meaning once a rewrite drops the quotes and the value looks like a number, a
    flag or a list. The hazard is narrower; it is not gone.

    Narrower in both directions, and the door has to match it exactly, because
    refusing has a cost of its own — a reminder the user asked for and did not
    get. `Send ${DETAIL}` is a string under every quoting style, and a `${VAR}`
    in a comment reaches no loader at all; neither can be retyped by a rewrite,
    so neither is refused.

    The check is on the TEXT, hence environment-INDEPENDENT. Sol r1 killed the
    alternative — comparing resolved values — because it passes with a benign
    value today and the rewritten file still changes meaning tomorrow.
    """

    # Written as LITERAL TEXT, with the quotes an operator would have authored.
    # Going through ``safe_dump`` here would strip them before the test starts —
    # which is the very transformation under examination.
    AUTHORED = (
        'schema_version: 1\n'
        'triggers:\n'
        '  - name: op-alert\n'
        '    type: cron\n'
        '    schedule: "0 8 * * *"\n'
        '    channel: telegram\n'
        '    prompt: "${DETAIL}"\n'
    )

    def _authored(self, tmp_path, extra="", prompt=None):
        p = tmp_path / "triggers.yaml"
        text = self.AUTHORED
        if prompt is not None:
            text = text.replace('prompt: "${DETAIL}"\n', f"prompt: {prompt}\n")
        p.write_text(text + extra, encoding="utf-8")
        return str(p)

    def test_add_refuses_and_leaves_the_file_alone(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DETAIL", "bins tonight")   # deliberately BENIGN
        path = self._authored(tmp_path)
        before = pathlib.Path(path).read_bytes()

        with pytest.raises(ValueError, match="interpolation"):
            reminders.add_entry(path, _mine())

        assert pathlib.Path(path).read_bytes() == before

    @pytest.mark.parametrize("label,prompt,extra", [
        ("embedded in a larger scalar", '"Send ${DETAIL}"', ""),
        ("embedded, unquoted", "Send ${DETAIL}", ""),
        ("lone but PLAIN", "${DETAIL}", ""),
        ("only in a comment", '"put the bins out"',
         "# an example of the form: ${DETAIL}\n"),
        ("not the loader's pattern", '"${NOT-A-VAR}"', ""),
    ])
    def test_the_door_does_not_close_on_what_a_rewrite_cannot_damage(
            self, label, prompt, extra, tmp_path, monkeypatch):
        """Refusing costs the user a reminder, so the door must be exact.

        None of these can be retyped by a rewrite: a placeholder with text
        around it is a string under every quoting style, a PLAIN lone one is
        read back as a value both before and after (a dump re-emits it plain),
        a comment reaches no loader, and `${NOT-A-VAR}` is not a placeholder.
        """
        monkeypatch.setenv("DETAIL", "true")
        path = self._authored(tmp_path, extra=extra, prompt=prompt)
        reminders.add_entry(path, _mine())
        assert "reminder-a1b2c3d4" in _names(path), label

    def test_the_refusal_does_not_depend_on_the_current_value(
            self, tmp_path, monkeypatch):
        """Sol r1's red case. With a benign value a resolved-value comparison
        would PASS and write the entry; the damage appears only once the value
        later looks like a number or a flag. This proves the door closes on the
        text, before any value is consulted."""
        monkeypatch.delenv("DETAIL", raising=False)
        path = self._authored(tmp_path)
        with pytest.raises(ValueError, match="interpolation"):
            reminders.add_entry(path, _mine())

    def test_the_hazard_the_door_prevents_is_real(self, tmp_path, monkeypatch):
        """Shown through the CURRENT loader, not the one this door was written
        against. A value containing `#` no longer truncates — that half is fixed
        (#409). What remains: the authored file QUOTES the lone placeholder, so
        it is the string "true"; a `safe_dump` rewrite of the very same data
        drops the quotes, and the same entry then means the boolean True, which
        fails the prompt's string schema."""
        import agent_loader
        monkeypatch.setenv("DETAIL", "bins # tonight")
        path = self._authored(tmp_path, prompt='"Send ${DETAIL}"')
        assert agent_loader._read_yaml(path)["triggers"][0]["prompt"] == \
            "Send bins # tonight", "no longer truncated at the #"

        monkeypatch.setenv("DETAIL", "true")
        lone = self._authored(tmp_path)
        assert agent_loader._read_yaml(lone)["triggers"][0]["prompt"] == \
            "true", "quoted in the authored file — text"
        rewritten = tmp_path / "rewritten.yaml"
        rewritten.write_text(yaml.safe_dump(_read(lone), sort_keys=False),
                             encoding="utf-8")
        assert agent_loader._read_yaml(str(rewritten))["triggers"][0][
            "prompt"] is True, "unquoted by safe_dump — RETYPED"

    def test_remove_PROCEEDS_rather_than_stranding_a_reminder(
            self, tmp_path, monkeypatch):
        """Terra r1's red case. Refusing here is what creates the permanent
        redelivery loop: the sweep has already dispatched, presence is the
        ledger, so a blocked removal redelivers every five minutes forever.

        Reachable because the operator can add a placeholder entry AFTER a
        reminder is already pending.
        """
        monkeypatch.setenv("DETAIL", "bins # tonight")
        path = self._authored(tmp_path, extra=(
            '  - {name: reminder-a1b2c3d4, type: date, '
            f'at: "{OVERDUE}", one_shot: true, channel: telegram, '
            'prompt: x, managed_by: agent}\n'))
        assert reminders.past_due(path, NOW), "precondition: sweep would deliver"

        assert reminders.remove_entry(path, "reminder-a1b2c3d4") == "removed"
        assert reminders.past_due(path, NOW) == [], "no redelivery next pass"

    def test_the_rewrite_is_recorded_for_the_operator(
            self, tmp_path, monkeypatch):
        """#513: warn-and-proceed leaves the operator's declared-text intent
        silently altered. The log line reaches nobody, so the rewrite is
        recorded for an on-channel notice."""
        reminders._placeholder_pending.clear()
        monkeypatch.setenv("DETAIL", "bins tonight")
        path = self._authored(tmp_path, extra=(
            '  - {name: reminder-a1b2c3d4, type: date, '
            f'at: "{OVERDUE}", one_shot: true, channel: telegram, '
            'prompt: x, managed_by: agent}\n'))

        assert reminders.remove_entry(path, "reminder-a1b2c3d4") == "removed"
        assert reminders.peek_placeholder_notices() == [path]

    def test_a_failed_write_records_nothing(self, tmp_path, monkeypatch):
        """A notice claiming 'Updated <file>' for a file that was never
        written is a false report about operator-authored configuration.
        The write can raise before os.replace (atomic_io), leaving the original
        untouched — so the record follows the write, not the check.

        Patched at ``atomic_write_text``, which is the actual write: #512 moved
        emission ahead of it in ``remove_entry`` (the disarm check needs the
        emitted text), so patching the old ``_save`` wrapper would no longer
        intercept anything."""
        reminders._placeholder_pending.clear()
        monkeypatch.setenv("DETAIL", "bins tonight")
        path = self._authored(tmp_path, extra=(
            '  - {name: reminder-a1b2c3d4, type: date, '
            f'at: "{OVERDUE}", one_shot: true, channel: telegram, '
            'prompt: x, managed_by: agent}\n'))

        def _boom(*_a, **_kw):
            raise OSError("no space left on device")
        monkeypatch.setattr(reminders, "atomic_write_text", _boom)

        with pytest.raises(OSError):
            reminders.remove_entry(path, "reminder-a1b2c3d4")
        assert reminders.peek_placeholder_notices() == []

    def test_a_non_placeholder_rewrite_records_nothing(
            self, tmp_path, monkeypatch):
        """Only placeholder-bearing files are worth telling the operator
        about; an ordinary cleanup must stay silent."""
        reminders._placeholder_pending.clear()
        path = _write(tmp_path, [_mine("reminder-a1b2c3d4", OVERDUE)])

        assert reminders.remove_entry(path, "reminder-a1b2c3d4") == "removed"
        assert reminders.peek_placeholder_notices() == []

    def test_clear_removes_only_the_named_path(self, tmp_path):
        reminders._placeholder_pending.clear()
        reminders._placeholder_pending.update({"/a/triggers.yaml",
                                               "/b/triggers.yaml"})
        reminders.clear_placeholder_notice("/a/triggers.yaml")
        assert reminders.peek_placeholder_notices() == ["/b/triggers.yaml"]


# --- the door survives its own cleanup (#512) -------------------------------


class TestTheDoorSurvivesItsOwnCleanup:
    """`remove_entry` warns and PROCEEDS, and used to disarm the door doing it.

    Its rewrite re-emitted `prompt: "${DETAIL}"` as `prompt: ${DETAIL}`, which
    is the one shape whose meaning depends on quoting — so the operator's entry
    changed resolution class, and `text_has_lone_placeholder` stopped matching
    the file. One cancel therefore converted a guarded file into an unguarded
    one for every consumer at once: this writer's own refusal, the
    configurator's trigger edits, and `config_sync`'s choice between the loud
    byte-level reconcile and the entry-level one that can DROP an entry.

    These are the inverse of `TestEnvPlaceholderDoor`'s hazard case: same file,
    same value, asserted after the module's own rewrite rather than after a
    hand-rolled `safe_dump`, and through the LIVE loader.
    """

    OVERDUE_MINE = ('  - {name: reminder-a1b2c3d4, type: date, '
                    f'at: "{OVERDUE}", one_shot: true, channel: telegram, '
                    'prompt: x, managed_by: agent}\n')

    def _authored(self, tmp_path, prompt='"${DETAIL}"', head="", extra=""):
        p = tmp_path / "triggers.yaml"
        p.write_text(
            "schema_version: 1\n"
            f"{head}"
            "triggers:\n"
            "  - name: op-alert\n"
            "    type: cron\n"
            '    schedule: "0 8 * * *"\n'
            "    channel: telegram\n"
            f"    prompt: {prompt}\n"
            + self.OVERDUE_MINE + extra,
            encoding="utf-8")
        return str(p)

    def _live(self, path):
        import agent_loader
        return agent_loader._read_yaml(path)["triggers"][0]["prompt"]

    @pytest.mark.parametrize("form", [
        '"${DETAIL}"',                  # double-quoted
        "'${DETAIL}'",                  # single-quoted
        "!!str ${DETAIL}",              # the tag instead of quoting
        "|-\n      ${DETAIL}",          # a block scalar
    ])
    def test_the_operator_s_entry_still_means_what_it_meant(
            self, form, tmp_path, monkeypatch):
        """The value is `true` precisely because that is where the two
        readings diverge: declared text it is the string "true", re-emitted
        plain it is the boolean True — which then fails the prompt's schema."""
        monkeypatch.setenv("DETAIL", "true")
        path = self._authored(tmp_path, prompt=form)
        assert self._live(path) == "true", "precondition: declared text"

        assert reminders.remove_entry(path, "reminder-a1b2c3d4") == "removed"

        assert self._live(path) == "true", form
        assert self._live(path) is not True

    def test_the_guard_still_matches_the_file_afterwards(
            self, tmp_path, monkeypatch):
        """The consequence the issue is about: the predicate is what every
        other writer consults, so a rewrite that erased it disarmed them all."""
        import config
        monkeypatch.setenv("DETAIL", "true")
        path = self._authored(tmp_path)

        assert reminders.remove_entry(path, "reminder-a1b2c3d4") == "removed"

        assert config.text_has_lone_placeholder(
            pathlib.Path(path).read_text(encoding="utf-8"))

    def test_the_next_writer_still_refuses(self, tmp_path, monkeypatch):
        """Read through a consumer rather than the predicate: after a cleanup,
        `add_entry` must still refuse this file. It began succeeding once the
        first cancel stripped the quoting."""
        monkeypatch.setenv("DETAIL", "true")
        path = self._authored(tmp_path)
        reminders.remove_entry(path, "reminder-a1b2c3d4")

        with pytest.raises(ValueError, match="interpolation"):
            reminders.add_entry(path, _mine("reminder-b2c3d4e5"))

    def test_config_sync_still_takes_the_LOUD_path(self, tmp_path, monkeypatch):
        """The consumer whose failure is the silent one (`config_sync.py:217`):
        an unguarded file reconciles per entry, and per-entry validation DROPS
        an entry it judges invalid. Asserted on the real gate, not the
        predicate it calls."""
        import config_sync
        monkeypatch.setenv("DETAIL", "true")
        path = self._authored(tmp_path)
        reminders.remove_entry(path, "reminder-a1b2c3d4")

        assert config_sync._entry_doc(
            pathlib.Path(path).read_text(encoding="utf-8"),
            "triggers", "name") is None, "must stay byte-level"

    def test_a_plain_lone_placeholder_is_left_plain(self, tmp_path,
                                                    monkeypatch):
        """The fix must not quote what the operator left unquoted: a plain
        lone placeholder has its value read back, and quoting it would retype
        `minutes: ${EVERY}` from 60 the integer to "60" the string — the same
        defect pointed the other way, on the same file."""
        monkeypatch.setenv("EVERY", "60")
        path = self._authored(tmp_path, prompt='"put the bins out"', extra=(
            "  - name: op-tick\n    type: interval\n"
            "    minutes: ${EVERY}\n    channel: telegram\n"
            "    prompt: tick\n"))
        reminders.remove_entry(path, "reminder-a1b2c3d4")

        import agent_loader
        entry = [e for e in agent_loader._read_yaml(path)["triggers"]
                 if e["name"] == "op-tick"][0]
        assert entry["minutes"] == 60 and isinstance(entry["minutes"], int)

    def test_repeated_cleanups_do_not_wear_the_declaration_away(
            self, tmp_path, monkeypatch):
        """It was a ONE-SHOT loss, so a fix that only survived the first
        rewrite would look identical in every other test here."""
        monkeypatch.setenv("DETAIL", "true")
        path = self._authored(tmp_path, extra=(
            '  - {name: reminder-b2c3d4e5, type: date, '
            f'at: "{OVERDUE}", one_shot: true, channel: telegram, '
            'prompt: y, managed_by: agent}\n'))

        assert reminders.remove_entry(path, "reminder-a1b2c3d4") == "removed"
        assert reminders.remove_entry(path, "reminder-b2c3d4e5") == "removed"

        assert self._live(path) == "true"

    def test_a_declaration_the_document_DISCARDS_is_reported_not_carried(
            self, tmp_path, monkeypatch, caplog):
        """The residual bound, pinned so it stays a decision.

        The predicate scans TOKENS and the rewrite carries what the document
        KEEPS, so the two can only disagree about a scalar construction throws
        away — here a duplicate key's loser. Nothing surviving can have been
        retyped (the surviving `prompt` is the boolean it already was, before
        any rewrite), but the guard is off for this file from now on, and the
        operator's log says so rather than nothing.
        """
        import config
        monkeypatch.setenv("DETAIL", "true")
        path = self._authored(tmp_path, prompt='"${DETAIL}"\n    prompt: true')
        assert self._live(path) is True, "precondition: already the loser"

        with caplog.at_level("WARNING"):
            assert reminders.remove_entry(path, "reminder-a1b2c3d4") == "removed"

        assert self._live(path) is True, "the survivor is unchanged"
        assert not config.text_has_lone_placeholder(
            pathlib.Path(path).read_text(encoding="utf-8"))
        assert any("declared-text guard is off" in r.getMessage()
                   for r in caplog.records)

    def test_an_ordinary_file_is_written_exactly_as_before(self, tmp_path):
        """No placeholder anywhere: the emitted bytes must be what `safe_dump`
        produced before this change, or every cleanup of every ordinary
        triggers.yaml becomes a diff in the operator's config repo."""
        path = _write(tmp_path, [HEARTBEAT,
                                 _mine("reminder-a1b2c3d4", OVERDUE)])
        reminders.remove_entry(path, "reminder-a1b2c3d4")

        assert pathlib.Path(path).read_text(encoding="utf-8") == yaml.safe_dump(
            {"schema_version": 1, "triggers": [HEARTBEAT]},
            sort_keys=False, allow_unicode=True)


# --- past_due --------------------------------------------------------------


class TestPastDue:

    def test_only_overdue_agent_owned_date_entries(self, tmp_path):
        path = _write(tmp_path, [
            HEARTBEAT,
            _mine("reminder-old11111", OVERDUE),
            _mine("reminder-new22222", LATER),
            _mine("reminder-rec33333", type="cron", schedule="0 7 * * thu",
                  one_shot=False, at=""),
        ])
        assert [e["name"] for e in reminders.past_due(path, NOW)] == [
            "reminder-old11111"]

    def test_an_operator_lookalike_is_never_swept(self, tmp_path):
        """The negative the live probe repeats: a hand-authored
        ``reminder-``-prefixed past-dated one-shot with no ``managed_by`` is
        neither delivered nor removed."""
        path = _write(tmp_path, [_operators_lookalike()])
        assert reminders.past_due(path, NOW) == []
        assert reminders.remove_entry(path, "reminder-bins") == "not_owned"

    def test_an_unparseable_at_is_skipped_not_raised(self, tmp_path):
        path = _write(tmp_path, [_mine("reminder-bad44444", "not-a-time"),
                                 _mine("reminder-old11111", OVERDUE)])
        assert [e["name"] for e in reminders.past_due(path, NOW)] == [
            "reminder-old11111"]

    def test_a_date_entry_missing_one_shot_is_still_swept(self, tmp_path):
        """Membership is decided on the type alone. Requiring the flag here
        too would mean an entry lacking it is skipped at registration (past)
        AND by the sweep — silently never delivered."""
        entry = _mine(at=OVERDUE)
        del entry["one_shot"]
        path = _write(tmp_path, [entry])
        assert len(reminders.past_due(path, NOW)) == 1

    def test_a_missing_file_is_empty(self, tmp_path):
        assert reminders.past_due(str(tmp_path / "nope.yaml"), NOW) == []


# --- read failures ---------------------------------------------------------


class TestReadFailures:
    """Every failure folds into ``ValueError`` because readers and writers here
    catch exactly ``(OSError, ValueError)``. An unfolded ``yaml.YAMLError``
    would escape and abort the whole sweep, so later roles' overdue reminders
    would go undelivered."""

    @pytest.mark.parametrize("text", [
        "{{{ not: valid: yaml\n",
        "- a list, not a mapping\n",
        "schema_version: 2\ntriggers: not-a-list\n",
    ])
    def test_a_bad_document_suspends_both_directions(self, tmp_path, text):
        p = tmp_path / "triggers.yaml"
        p.write_text(text, encoding="utf-8")
        path = str(p)

        # None, NOT [] — an empty list would authorise reverse reconciliation
        # to drop every reminder job on one bad read.
        assert reminders.agent_entries(path) is None
        assert reminders.past_due(path, NOW) == []
        assert reminders.existing_names(path) == set()
        # And removal fails the SAME way, so the sweep never delivers something
        # it cannot then clean up.
        with pytest.raises(ValueError):
            reminders.remove_entry(path, "reminder-a1b2c3d4")

    def test_a_document_that_parses_but_cannot_be_RE_EMITTED_still_folds(
            self, tmp_path):
        """Sol impl r1. Parsing tolerance and EMISSION tolerance differ.

        Measured with the pinned PyYAML 6.0.3 at the default recursion limit:
        ~200 levels of nesting parses and dumps; **~400 parses but makes
        ``safe_dump`` raise ``RecursionError``**; ~800 fails to parse at all. So
        there is a real window where ``past_due`` selects an entry that cleanup
        then cannot write back.

        ``RecursionError`` is a ``RuntimeError``, so unfolded it escapes every
        ``except (OSError, ValueError)`` here AND in the sweep — aborting the
        pass after a delivery, which skips every later role and redelivers the
        entry on each subsequent pass.

        Red case: drop ``_emit``'s fold and this raises ``RecursionError``
        instead of ``ValueError``, and the sweep test below aborts.
        """
        deep = "[" * 400 + "]" * 400
        p = tmp_path / "triggers.yaml"
        p.write_text(
            "schema_version: 1\n"
            "triggers:\n"
            f'  - {{name: reminder-a1b2c3d4, type: date, at: "{OVERDUE}", '
            "one_shot: true, channel: telegram, prompt: x, "
            "managed_by: agent}\n"
            f"  - {{name: deep, type: interval, minutes: 1, "
            f"channel: telegram, prompt: {deep}}}\n", encoding="utf-8")
        path = str(p)

        # It genuinely parses, so the sweep WOULD select and deliver it.
        assert [e["name"] for e in reminders.past_due(path, NOW)] == [
            "reminder-a1b2c3d4"]

        # ...and the cleanup failure is a ValueError, inside the contract.
        with pytest.raises(ValueError, match="cannot be re-emitted"):
            reminders.remove_entry(path, "reminder-a1b2c3d4")
        with pytest.raises(ValueError, match="cannot be re-emitted"):
            reminders.add_entry(path, _mine("reminder-bbbb2222"))

    def test_triggers_not_a_list_is_never_coerced_to_empty(self, tmp_path):
        """Coercing it to ``[]`` would silently erase every operator trigger on
        the next write."""
        p = tmp_path / "triggers.yaml"
        p.write_text("schema_version: 2\ntriggers: {a: 1}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="not a list"):
            reminders.add_entry(str(p), _mine())
        assert "a: 1" in p.read_text(encoding="utf-8")

    def test_an_alias_is_read_the_way_the_LOADER_reads_it(self, tmp_path):
        """The writer must see exactly the document boot sees, and
        ``agent_loader._read_yaml`` uses plain ``safe_load``, which permits
        aliases. Refusing them here would add no security — the loader parses
        the same file — while making a legitimate file unwritable.
        """
        import agent_loader
        p = tmp_path / "triggers.yaml"
        p.write_text(
            "schema_version: 2\n"
            "triggers:\n"
            "  - &hb {name: heartbeat, type: interval, minutes: 60,\n"
            "         channel: telegram, prompt: hb}\n", encoding="utf-8")
        path = str(p)
        assert reminders.existing_names(path) == \
            {e["name"] for e in agent_loader._read_yaml(path)["triggers"]}


# --- agent_entries ---------------------------------------------------------


class TestAgentEntries:

    def test_it_returns_only_agent_owned_entries(self, tmp_path):
        path = _write(tmp_path, [HEARTBEAT, _operators_lookalike(),
                                 _mine("reminder-aaaa1111")])
        assert [e["name"] for e in reminders.agent_entries(path)] == [
            "reminder-aaaa1111"]

    def test_a_missing_file_is_empty_not_none(self, tmp_path):
        """Absent is a genuine "the agent owns nothing here", which must still
        authorise dropping orphaned jobs. Only an unreadable file is None."""
        assert reminders.agent_entries(str(tmp_path / "nope.yaml")) == []


# ---------------------------------------------------------------------------
# #403 — the general mutators the configurator's trigger edits go through
# ---------------------------------------------------------------------------


AGENT_REMINDER = {"name": "reminder-abc", "type": "date", "one_shot": True,
                  "at": LATER, "channel": "telegram", "prompt": "bins",
                  "managed_by": "agent"}

WEBHOOK = {"name": "paypal", "type": "webhook", "clearance": "public",
           "auth": {"mode": "hmac_body"}}


class TestUpsertEntry:
    def test_adds_a_new_entry_and_keeps_the_others(self, tmp_path):
        path = _write(tmp_path, [HEARTBEAT, AGENT_REMINDER], version=2)
        assert reminders.upsert_entry(path, dict(WEBHOOK)) == "added"
        names = [e["name"] for e in _read(path)["triggers"]]
        assert names == ["heartbeat", "reminder-abc", "paypal"]

    def test_replaces_in_place(self, tmp_path):
        """Order is not semantically load-bearing, but the diff the operator
        reads is: an update that moved the entry to the end would show as a
        delete plus an add in the config repo's history."""
        path = _write(tmp_path, [HEARTBEAT, WEBHOOK, AGENT_REMINDER], version=2)
        changed = dict(HEARTBEAT, minutes=15)
        assert reminders.upsert_entry(path, changed) == "replaced"
        doc = _read(path)
        assert [e["name"] for e in doc["triggers"]] == [
            "heartbeat", "paypal", "reminder-abc"]
        assert doc["triggers"][0]["minutes"] == 15

    def test_a_replacement_that_omits_secret_owner_does_not_flip_it_to_casa(self, tmp_path):
        """#609 (Sol, design r1 S1). `secret_owner: provider` says Casa must
        NEVER mint here — the operator places that credential by hand and Casa
        has no way to regenerate or import it. A whole-entry replacement that
        merely omitted the field silently flipped ownership to the `casa`
        default, and with minting moved to registration the very next reload
        then occupied the slot with a Casa token. Worse, that token validates
        under the PROVIDER rule too, so the report read the route healthy while
        every request signed with the operator's real credential 401'd.

        Omission means "leave it alone", not "make it mine". Changing owner
        stays possible — by saying so.
        """
        provider = {"name": "el-postcall", "type": "webhook",
                    "clearance": "family",
                    "auth": {"mode": "timestamped_hmac",
                             "header": "ElevenLabs-Signature",
                             "tolerance_secs": 300, "secret_owner": "provider"}}
        path = _write(tmp_path, [provider], version=2)

        # The shape config_trigger_upsert emits for a clearance-only edit:
        # every field present EXCEPT secret_owner.
        replacement = {"name": "el-postcall", "type": "webhook",
                       "clearance": "public",
                       "auth": {"mode": "timestamped_hmac",
                                "header": "ElevenLabs-Signature",
                                "tolerance_secs": 300}}
        assert reminders.upsert_entry(path, replacement) == "replaced"

        stored = _read(path)["triggers"][0]
        assert stored["auth"]["secret_owner"] == "provider"
        assert stored["clearance"] == "public", "the intended edit still applied"

    def test_a_replacement_that_omits_auth_entirely_keeps_the_prior_block(self, tmp_path):
        """#609 (Terra, diff review). The sibling of the case above, and the
        wider one: `config_trigger_upsert` projects only the fields it was
        given, so a caller editing just `clearance` omits `auth` ALTOGETHER.
        Carrying forward only the nested `secret_owner` missed that — the whole
        block vanished, the loader defaulted the trigger to `hmac_body`/`casa`,
        and the route silently began verifying against the global secret while
        the integrator's existing signatures 401'd. The report then called it
        `global_secret`, which reads healthy.

        Omission means leave it alone here too. Changing the auth mode stays
        available by sending an `auth` block that says so.
        """
        provider = {"name": "el-postcall", "type": "webhook",
                    "clearance": "family",
                    "auth": {"mode": "timestamped_hmac", "header": "S",
                             "tolerance_secs": 300, "secret_owner": "provider"}}
        path = _write(tmp_path, [provider], version=2)

        assert reminders.upsert_entry(path, {
            "name": "el-postcall", "type": "webhook", "clearance": "public",
        }) == "replaced"

        stored = _read(path)["triggers"][0]
        assert stored["auth"] == provider["auth"]
        assert stored["clearance"] == "public", "the intended edit still applied"

    def test_a_replacement_may_still_change_the_auth_mode_explicitly(self, tmp_path):
        """The carry-forward must not become a one-way door."""
        provider = {"name": "el-postcall", "type": "webhook",
                    "clearance": "family",
                    "auth": {"mode": "timestamped_hmac", "header": "S",
                             "tolerance_secs": 300, "secret_owner": "provider"}}
        path = _write(tmp_path, [provider], version=2)
        assert reminders.upsert_entry(path, dict(provider, auth={
            "mode": "hmac_body", "header": "X-Webhook-Signature",
            "tolerance_secs": 300, "secret_owner": "casa"})) == "replaced"
        assert _read(path)["triggers"][0]["auth"]["mode"] == "hmac_body"

    def test_a_webhook_turned_into_a_schedule_does_not_keep_an_auth_block(self, tmp_path):
        """Carrying the block forward must not attach auth to a trigger type
        that has none — the replacement's own `type` decides."""
        provider = {"name": "el-postcall", "type": "webhook",
                    "clearance": "family",
                    "auth": {"mode": "timestamped_hmac", "header": "S",
                             "tolerance_secs": 300, "secret_owner": "provider"}}
        path = _write(tmp_path, [provider], version=2)
        assert reminders.upsert_entry(path, {
            "name": "el-postcall", "type": "cron", "schedule": "0 9 * * *",
            "channel": "main", "prompt": "Morning.",
        }) == "replaced"
        assert "auth" not in _read(path)["triggers"][0]

    def test_a_replacement_may_still_change_the_owner_explicitly(self, tmp_path):
        """The carry-forward must not become a one-way door: an operator who
        SAYS `casa` gets `casa`."""
        provider = {"name": "el-postcall", "type": "webhook",
                    "clearance": "family",
                    "auth": {"mode": "timestamped_hmac", "header": "S",
                             "tolerance_secs": 300, "secret_owner": "provider"}}
        path = _write(tmp_path, [provider], version=2)
        replacement = dict(provider, auth={"mode": "timestamped_hmac",
                                           "header": "S", "tolerance_secs": 300,
                                           "secret_owner": "casa"})
        assert reminders.upsert_entry(path, replacement) == "replaced"
        assert _read(path)["triggers"][0]["auth"]["secret_owner"] == "casa"

    def test_refuses_to_write_the_agent_marker(self, tmp_path):
        path = _write(tmp_path, [], version=2)
        with pytest.raises(ValueError, match="managed_by"):
            reminders.upsert_entry(path, dict(AGENT_REMINDER))
        assert _read(path)["triggers"] == []

    def test_refuses_to_replace_an_agent_owned_entry(self, tmp_path):
        """The ownership bound is on the STORED entry, not only on the one
        submitted — otherwise an upsert naming a reminder erases it without any
        interleaving at all, which is the very loss this change exists to
        stop."""
        path = _write(tmp_path, [AGENT_REMINDER], version=2)
        impostor = {"name": "reminder-abc", "type": "interval", "minutes": 5,
                    "channel": "telegram", "prompt": "mine now"}
        with pytest.raises(ValueError, match="resident owns"):
            reminders.upsert_entry(path, impostor)
        assert _read(path)["triggers"] == [AGENT_REMINDER]

    def test_refuses_when_the_name_is_already_duplicated(self, tmp_path):
        path = _write(tmp_path, [HEARTBEAT, dict(HEARTBEAT)], version=2)
        with pytest.raises(ValueError, match="cannot boot"):
            reminders.upsert_entry(path, dict(HEARTBEAT, minutes=9))
        assert len(_read(path)["triggers"]) == 2

    def test_refuses_an_entry_the_schema_rejects_and_writes_nothing(
        self, tmp_path,
    ):
        path = _write(tmp_path, [HEARTBEAT], version=2)
        before = _read(path)
        with pytest.raises(ValueError):
            # cron without a schedule
            reminders.upsert_entry(path, {"name": "bad", "type": "cron",
                                          "channel": "telegram",
                                          "prompt": "x"})
        assert _read(path) == before

    def test_refuses_a_lone_placeholder_but_allows_an_embedded_one(
        self, tmp_path,
    ):
        """Only the LONE form is unwritable: safe_dump re-emits it unquoted,
        and quoting is what tells the loader such a scalar is text (#409). An
        embedded placeholder has surrounding text and reads back as a string,
        so refusing it would ban legitimate operator configuration."""
        path = _write(tmp_path, [], version=2)
        with pytest.raises(ValueError, match="exactly a"):
            reminders.upsert_entry(path, {"name": "a", "type": "cron",
                                          "schedule": "0 7 * * *",
                                          "channel": "telegram",
                                          "prompt": "${DETAIL}"})
        assert reminders.upsert_entry(
            path, {"name": "a", "type": "cron", "schedule": "0 7 * * *",
                   "channel": "telegram", "prompt": "say ${DETAIL} now"}
        ) == "added"

    def test_refuses_to_rewrite_a_file_holding_a_lone_placeholder(
        self, tmp_path,
    ):
        path = tmp_path / "triggers.yaml"
        path.write_text(
            'schema_version: 2\n'
            'triggers:\n'
            '  - name: heartbeat\n'
            '    type: interval\n'
            '    minutes: 60\n'
            '    channel: telegram\n'
            '    prompt: "${HB}"\n', encoding="utf-8")
        with pytest.raises(ValueError, match="interpolation"):
            reminders.upsert_entry(str(path), dict(WEBHOOK))


class TestDeleteEntry:
    def test_removes_an_operator_entry(self, tmp_path):
        path = _write(tmp_path, [HEARTBEAT, WEBHOOK, AGENT_REMINDER],
                      version=2)
        assert reminders.delete_entry(path, "paypal") == "removed"
        assert [e["name"] for e in _read(path)["triggers"]] == [
            "heartbeat", "reminder-abc"]

    def test_reports_not_found(self, tmp_path):
        path = _write(tmp_path, [HEARTBEAT], version=2)
        assert reminders.delete_entry(path, "nope") == "not_found"

    def test_refuses_an_agent_owned_entry(self, tmp_path):
        """"That is the resident's reminder" is a different answer from "no
        such trigger", and the caller relays it to the operator."""
        path = _write(tmp_path, [AGENT_REMINDER], version=2)
        assert reminders.delete_entry(path, "reminder-abc") == "not_owned"
        assert _read(path)["triggers"] == [AGENT_REMINDER]
