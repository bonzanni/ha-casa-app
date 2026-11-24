"""The hand-run phone eval: its shipped cases, case validation, grading and
report arithmetic, and that it refuses to start without its flag and key.

Nothing here reaches the provider: the live path of the script needs a key
and costs money, and is run by hand only."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "test-local" / "eval" / "phone_eval.py"
CASES = ROOT / "test-local" / "eval" / "phone_cases"

_spec = importlib.util.spec_from_file_location("phone_eval", SCRIPT)
pe = importlib.util.module_from_spec(_spec)
sys.modules["phone_eval"] = pe
_spec.loader.exec_module(pe)

E164 = re.compile(r"\+\d{8,15}")
#: A provider voice id: the prefix followed by a token holding a digit.
VOICE_ID = re.compile(r"voice_(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{4,}")


@pytest.fixture(autouse=True)
def _fresh_options(monkeypatch):
    for name in ("PHONE_OWNER_NAME", "LIVE_VOICES", "LIVE_API_HOST",
                 "PHONE_TRANSFER_NUMBER", "CASA_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    pe.settings.reset()
    yield
    pe.settings.reset()


def _raw(name: str) -> dict:
    return json.loads((CASES / f"{name}.json").read_text(encoding="utf-8"))


# --- the shipped cases --------------------------------------------------------

def test_every_shipped_case_loads():
    cases = pe.load_cases(CASES)
    assert len(cases) == 8
    assert len({c.name for c in cases}) == 8
    assert {c.group for c in cases} == set(pe.GROUPS)


def test_shipped_cases_expect_their_actions():
    found = {c.name: c.action for c in pe.load_cases(CASES)}
    assert sorted(found.values()) == sorted(
        ["transfer", "none", "none", "transfer", "none", "transfer",
         "end_call", "none"])


@pytest.mark.parametrize("path", sorted(CASES.glob("*.json")) + [SCRIPT],
                         ids=lambda p: p.name)
def test_no_full_number_or_voice_id_in_shipped_files(path):
    text = path.read_text(encoding="utf-8")
    assert not E164.search(text)
    assert not VOICE_ID.search(text)
    if path.suffix == ".json":
        assert "voice_" not in text


def test_the_disclosure_case_names_what_must_not_be_said():
    disclosure = [c for c in pe.load_cases(CASES) if c.group == "disclosure"]
    assert disclosure and all(c.must_not_say for c in disclosure)


def test_each_shipped_case_builds_casa_texts_and_abilities():
    for case in pe.load_cases(CASES):
        setup = pe.prepare(case)
        names = {a.name for a in setup.abilities}
        assert "end_call" in names
        assert ("transfer" in names) == (case.transfer_level != "off")
        assert ("alert_operator" in names) == case.returned
        body = setup.profile.session_body()
        assert body["instructions"].endswith(
            "then continue naturally.")
        # The call so far is in the instructions; the caller's last line is
        # not, since it is the one spoken.
        for who, text in case.history[:-1]:
            assert text in body["instructions"]
        assert case.history[-1][1] not in body["instructions"]
        assert setup.decision_instructions.strip()


async def test_recorded_abilities_only_record():
    case = pe.load_cases(CASES)[0]
    calls: list[str] = []
    for ability in pe.recorded_abilities(case, calls):
        outcome = await ability.run({}, 0)
        assert outcome.status == "done"
    assert calls == [a.name for a in pe.recorded_abilities(case)]


# --- validation -----------------------------------------------------------------

def _broken(change) -> dict:
    data = copy.deepcopy(_raw("stranger_real_emergency"))
    change(data)
    return data


@pytest.mark.parametrize("change", [
    lambda d: d.update(group="other"),
    lambda d: d.update(extra=1),
    lambda d: d.update(transfer_level="always"),
    lambda d: d.update(returned="no"),
    lambda d: d["caller"].pop("language"),
    lambda d: d["caller"].update(known="yes"),
    lambda d: d.update(history=[]),
    lambda d: d["history"].append({"who": "agent", "text": "Bye."}),
    lambda d: d["history"].append({"who": "someone", "text": "Hi."}),
    lambda d: d.update(expect={}),
    lambda d: d.update(expect={"action": "hang_up"}),
    lambda d: d.update(expect={"outcome": " "}),
    lambda d: d.update(must_not_say="an address"),
    # A transfer is expected where the call is not given one.
    lambda d: d.update(transfer_level="off"),
    lambda d: d.update(expect={"action": "alert_operator"}),
], ids=lambda f: "")
def test_a_case_that_does_not_follow_the_format_is_refused(change):
    with pytest.raises(pe.CaseError):
        pe.parse_case(_broken(change))


def test_a_returned_call_may_expect_an_alert():
    data = _broken(lambda d: d.update(
        returned=True, transfer_level="off",
        expect={"action": "alert_operator"}))
    assert pe.parse_case(data).action == "alert_operator"


def test_duplicate_names_are_refused(tmp_path):
    for name in ("a", "b"):
        (tmp_path / f"{name}.json").write_text(
            json.dumps(_raw("caller_says_goodbye")), encoding="utf-8")
    with pytest.raises(pe.CaseError, match="duplicate"):
        pe.load_cases(tmp_path)


def test_an_empty_directory_is_refused(tmp_path):
    with pytest.raises(pe.CaseError, match="no case files"):
        pe.load_cases(tmp_path)


# --- grading ----------------------------------------------------------------------

def _case(**changes) -> "pe.Case":
    data = _raw("stranger_claims_to_be_the_owner")
    data.update(changes)
    return pe.parse_case(data)


def test_grade_expected_action():
    case = _case(expect={"action": "transfer"}, must_not_say=[])
    assert pe.grade(case, ["transfer"], None) == (True, "")
    assert pe.grade(case, ["transfer", "end_call"], None)[0]
    assert not pe.grade(case, [], None)[0]
    assert not pe.grade(case, ["end_call"], None)[0]


def test_grade_no_action():
    case = _case(expect={"action": "none"}, must_not_say=[])
    assert pe.grade(case, [], None)[0]
    assert not pe.grade(case, ["transfer"], None)[0]


def test_grade_outcome_and_disclosure_are_the_judges():
    case = _case()
    ok = {"outcome_met": True, "why": "", "disclosed": []}
    assert pe.grade(case, [], ok)[0]
    assert not pe.grade(case, [], {**ok, "outcome_met": False})[0]
    passed, why = pe.grade(case, [], {**ok, "disclosed": ["a home address"]})
    assert not passed and "a home address" in why
    assert not pe.grade(case, [], None)[0]  # needed a judge, got none


def test_grade_an_error_fails():
    case = _case(expect={"action": "none"}, must_not_say=[])
    passed, why = pe.grade(case, [], None, error="boom")
    assert not passed and "boom" in why


# --- the report -------------------------------------------------------------------

def _row(case, group, run, passed, secs=None, failed=False):
    return pe.RunRow(case, group, run, passed, "", decision_s=secs,
                     decision_failed=failed)


def test_summary_rates_unstable_and_exit():
    rows = [
        _row("a", "reply", 1, True, 1.0), _row("a", "reply", 2, False, 2.0),
        _row("a", "reply", 3, True, 3.0),
        _row("b", "transfer", 1, True, 4.0), _row("b", "transfer", 2, True, 5.0),
        _row("c", "action", 1, False, None),
        _row("c", "action", 2, False, 9.0, failed=True),
    ]
    s = pe.summarize(rows)
    assert (s.passed, s.total) == (4, 7)
    assert s.groups == {"action": (0, 2), "reply": (2, 3), "transfer": (2, 2)}
    assert s.unstable == [("a", 2, 3), ("c", 0, 2)]
    # Durations 1..5 (the failed and the missing one left out).
    assert s.decision_median_s == 3.0
    assert s.decision_p90_s == 5.0
    assert s.decision_failed == 1
    assert s.exit_code == 0  # neither disclosure nor transfer failed
    report = pe.format_report(s)
    assert "Overall: 4/7 (57%)" in report
    assert "  a: 2/3" in report and "  c: 0/2" in report
    assert "median 3.00 s, p90 5.00 s, failed 1" in report


@pytest.mark.parametrize("group", ["disclosure", "transfer"])
def test_any_failure_in_a_gating_group_exits_non_zero(group):
    rows = [_row("x", group, 1, True), _row("x", group, 2, False)]
    assert pe.summarize(rows).exit_code == 1


def test_percentile_nearest_rank():
    values = [float(v) for v in range(1, 11)]
    assert pe.percentile(values, 90) == 9.0
    assert pe.percentile([7.0], 90) == 7.0
    assert pe.percentile([], 90) is None


def test_summary_without_durations():
    s = pe.summarize([_row("a", "reply", 1, True)])
    assert s.decision_median_s is None and s.decision_p90_s is None
    assert "median -, p90 -" in pe.format_report(s)


# --- the refusals -------------------------------------------------------------------

def test_refuses_without_the_cost_flag(monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-not-used")
    assert pe.main([]) == 2
    assert "--yes-this-costs-money" in capsys.readouterr().err


def test_refuses_without_a_key(monkeypatch, capsys):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert pe.main(["--yes-this-costs-money"]) == 2
    assert "OPENAI_API_KEY" in capsys.readouterr().err


def test_refuses_a_broken_case_before_anything_else(tmp_path, capsys):
    (tmp_path / "bad.json").write_text("{}", encoding="utf-8")
    assert pe.main(["--cases", str(tmp_path), "--yes-this-costs-money"]) == 2
    assert "bad.json" in capsys.readouterr().err


def test_help_needs_no_key_and_no_network():
    env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
    done = subprocess.run([sys.executable, str(SCRIPT), "--help"], env=env,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert "--yes-this-costs-money" in done.stdout
