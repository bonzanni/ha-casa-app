"""What the voice model, the in-call helper and the decision step are told.

The shipped defaults are read from the code tree; ``CASA_CONFIG_DIR`` points
at a temporary directory, so an operator override is a file written there."""
from __future__ import annotations

import logging
import re
from dataclasses import replace
from pathlib import Path

import pytest

from contacts.lookup import CallerInfo
from phone import prompt
from phone.settings import Options

DEFAULTS = (Path(__file__).resolve().parents[1]
            / "casa" / "rootfs" / "opt" / "casa" / "defaults")

KNOWN = CallerInfo(True, False, "Ann Example", "Ann", "en")
PRIORITY = CallerInfo(True, True, "Bea Sample", "Bea", "nl")
UNKNOWN = CallerInfo(False, False, "", "", "it")


@pytest.fixture(autouse=True)
def config_dir(tmp_path, monkeypatch):
    root = tmp_path / "config"
    root.mkdir()
    monkeypatch.setenv("CASA_CONFIG_DIR", str(root))
    monkeypatch.setenv("CASA_DEFAULTS_DIR", str(DEFAULTS))
    return root


def ctx(info: CallerInfo, caller_class: str, *, transfer_level: str = "urgent",
        returned: bool = False) -> prompt.Context:
    return prompt.Context(assistant_name="Robin", owner_name="Sam Owner",
                          info=info, caller_class=caller_class,
                          language=info.language, transfer_level=transfer_level,
                          returned=returned)


def sections() -> dict[str, str]:
    return prompt.parse(prompt.load("agent.md"))[1]


def filled(text: str, info: CallerInfo) -> str:
    return (text.replace("{owner_name}", "Sam Owner")
            .replace("{assistant_name}", "Robin")
            .replace("{name}", info.name)
            .replace("{first_name}", info.first_name or info.name))


@pytest.mark.parametrize("info, caller_class, language_name", [
    (PRIORITY, "priority", "Dutch"),
    (KNOWN, "known", "English"),
    (UNKNOWN, "unknown", "Italian"),
])
def test_each_class_gets_its_paragraph_owner_and_language(
        info, caller_class, language_name):
    text = prompt.instructions(ctx(info, caller_class))
    assert "Sam Owner" in text
    assert language_name in text
    assert filled(sections()[f"who: {caller_class}"], info) in text
    assert filled(sections()["transfer: urgent"], info) in text
    assert "{" not in text and "}" not in text


@pytest.mark.parametrize("build", [prompt.instructions,
                                   prompt.helper_instructions,
                                   prompt.decision_instructions])
@pytest.mark.parametrize("info, caller_class", [
    (PRIORITY, "priority"), (KNOWN, "known"), (UNKNOWN, "unknown")])
def test_no_placeholder_is_left_in_any_text(build, info, caller_class):
    for level in ("off", "emergency", "urgent", "open"):
        text = build(ctx(info, caller_class, transfer_level=level))
        assert "{" not in text and "}" not in text


def test_a_known_caller_is_named_and_nothing_else_about_them_reaches_the_texts():
    # Only name, first name and language may come from the lookup. The
    # fields of CallerInfo are exactly those plus two flags; a new field
    # would have to be considered here.
    assert set(CallerInfo.__dataclass_fields__) == {
        "known", "priority", "name", "first_name", "language"}
    text = prompt.instructions(ctx(KNOWN, "known"))
    assert "Ann" in text and "Ann Example" in text


def test_the_full_name_appears_only_for_known_callers():
    info = CallerInfo(False, False, "Ann Example", "Ann", "en")
    for build in (prompt.instructions, prompt.helper_instructions,
                  prompt.decision_instructions):
        assert "Ann Example" in build(ctx(KNOWN, "known"))
        text = build(ctx(info, "unknown"))
        assert "Ann" not in text


def test_a_returned_caller_is_not_named_even_when_known(config_dir):
    for build in (prompt.instructions, prompt.helper_instructions,
                  prompt.decision_instructions):
        text = build(ctx(KNOWN, "known", returned=True))
        assert "Ann" not in text
    # Even a returned paragraph that asks for the name does not get it.
    edited = prompt.load("agent.md").replace(
        "## who: returned\n", "## who: returned\nName: {name} {first_name}.\n")
    target = config_dir / "phone" / "prompts" / "agent.md"
    target.parent.mkdir(parents=True)
    target.write_text(edited, encoding="utf-8")
    text = prompt.instructions(ctx(KNOWN, "known", returned=True))
    assert "Name:  ." in text
    assert "Ann" not in text


def test_an_unknown_caller_is_given_no_name():
    text = prompt.instructions(ctx(UNKNOWN, "unknown"))
    assert filled(sections()["who: unknown"], UNKNOWN) in text


def test_a_name_with_braces_stays_as_written():
    info = CallerInfo(True, False, "Ann {owner_name}", "Ann {language_name}",
                      "en")
    text = prompt.instructions(ctx(info, "known"))
    assert "Ann {owner_name}" in text
    assert "Ann {language_name}" in text


def test_a_returned_call_gets_the_returned_paragraph_and_no_transfer():
    text = prompt.instructions(ctx(PRIORITY, "priority", transfer_level="open",
                                   returned=True))
    assert filled(sections()["who: returned"], PRIORITY) in text
    assert filled(sections()["transfer: off"], PRIORITY) in text
    assert filled(sections()["transfer: open"], PRIORITY) not in text
    assert filled(sections()["who: priority"], PRIORITY) not in text


def test_an_override_file_wins(config_dir):
    default = prompt.load("agent.md")
    override = default.replace("{owner_name}", "{owner_name} (override marker)", 1)
    target = config_dir / "phone" / "prompts" / "agent.md"
    target.parent.mkdir(parents=True)
    target.write_text(override, encoding="utf-8")
    assert prompt.load("agent.md") == override
    assert "(override marker)" in prompt.instructions(ctx(KNOWN, "known"))


def test_an_override_missing_a_section_falls_back_to_the_default(
        config_dir, caplog):
    default = prompt.load("agent.md")
    broken = re.sub(r"## who: known\n", "## who: someone\n", default)
    broken = broken.replace("{owner_name}", "BROKEN-OVERRIDE", 1)
    target = config_dir / "phone" / "prompts" / "agent.md"
    target.parent.mkdir(parents=True)
    target.write_text(broken, encoding="utf-8")
    call = ctx(KNOWN, "known")
    with caplog.at_level(logging.WARNING):
        text = prompt.instructions(call)
        texts = [prompt.helper_instructions(call),
                 prompt.decision_instructions(call)]
    assert "BROKEN-OVERRIDE" not in text
    assert filled(sections_of_default()["who: known"], KNOWN) in text
    for other in texts:
        assert "BROKEN-OVERRIDE" not in other
        assert filled(sections_of_default()["who: known"], KNOWN) in other
    # One call builds three texts from the file; it is warned about once.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1

    # A new version of the file is read again, and warned about again.
    caplog.clear()
    target.write_text(broken + "\nStill broken.\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        prompt.instructions(call)
    assert len([r for r in caplog.records
                if r.levelno == logging.WARNING]) == 1


def sections_of_default() -> dict[str, str]:
    text = (DEFAULTS / "phone" / "prompts" / "agent.md").read_text(encoding="utf-8")
    return prompt.parse(text)[1]


def test_a_broken_shipped_default_raises(tmp_path, monkeypatch):
    shipped = tmp_path / "defaults" / "phone" / "prompts"
    shipped.mkdir(parents=True)
    (shipped / "agent.md").write_text("Only a body.\n", encoding="utf-8")
    monkeypatch.setenv("CASA_DEFAULTS_DIR", str(tmp_path / "defaults"))
    with pytest.raises(prompt.PromptError):
        prompt.instructions(ctx(KNOWN, "known"))


def test_a_missing_prompt_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("CASA_DEFAULTS_DIR", str(tmp_path / "nowhere"))
    with pytest.raises(prompt.PromptError):
        prompt.load("agent.md")


def test_the_shipped_default_has_every_section():
    found = sections_of_default()
    for key in ("who: priority", "who: known", "who: unknown", "who: returned",
                "transfer: off", "transfer: emergency", "transfer: urgent",
                "transfer: open"):
        assert found[key].strip()


def test_record_prompt_is_shipped():
    assert prompt.load("record.md").strip()


def test_spoken_lines_in_italian_and_the_english_fallback():
    italian = prompt.spoken_lines("it", "Sam Owner")
    english = prompt.spoken_lines("en", "Sam Owner")
    assert set(italian) == set(english) == {"silence", "too_long",
                                            "transfer_failed"}
    assert italian != english
    assert prompt.spoken_lines("de", "Sam Owner") == english
    assert prompt.spoken_lines("nl", "Sam Owner") != english
    for language in ("en", "nl", "it"):
        lines = prompt.spoken_lines(language, "Sam Owner")
        assert "Sam Owner" in lines["transfer_failed"]
        assert all("{" not in text for text in lines.values())


def test_spoken_lines_override_missing_a_key_falls_back(config_dir, caplog):
    target = config_dir / "phone" / "prompts" / "lines.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("en:\n  silence: Hello?\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        lines = prompt.spoken_lines("en", "Sam Owner")
    assert lines["silence"] != "Hello?"
    assert set(lines) == {"silence", "too_long", "transfer_failed"}
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_spoken_lines_override_wins(config_dir):
    default = (DEFAULTS / "phone" / "prompts" / "lines.yaml").read_text(
        encoding="utf-8")
    target = config_dir / "phone" / "prompts" / "lines.yaml"
    target.parent.mkdir(parents=True)
    import yaml
    data = yaml.safe_load(default)
    data["en"]["silence"] = "Anyone there? Goodbye."
    target.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    assert prompt.spoken_lines("en", "Sam Owner")["silence"] == \
        "Anyone there? Goodbye."


def test_assistant_and_owner_names(config_dir):
    assert prompt.assistant_name() == "the assistant"
    character = config_dir / "agents" / "assistant" / "character.yaml"
    character.parent.mkdir(parents=True)
    character.write_text('name: "Robin"\nrole: x\n', encoding="utf-8")
    assert prompt.assistant_name() == "Robin"
    options = Options(enabled=True, transfer_number="", owner_name="",
                      seed_voices={}, api_host="")
    assert prompt.owner_name(options) == "the person you called"
    assert prompt.owner_name(replace(options, owner_name="Sam")) == "Sam"


def test_the_shipped_prompts_carry_no_address_or_number():
    for path in (DEFAULTS / "phone" / "prompts").iterdir():
        text = path.read_text(encoding="utf-8")
        assert "@" not in text
        assert not re.search(r"\+?\d{6,}", text)
