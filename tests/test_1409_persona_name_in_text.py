"""#1409: text a model or the operator reads names the CONFIGURED assistant,
never the default persona hard-coded."""
from types import SimpleNamespace


def _role_map(**names):
    return {r: SimpleNamespace(character=SimpleNamespace(name=n))
            for r, n in names.items()}


def test_persona_name_reads_the_configured_persona(monkeypatch):
    import tools
    monkeypatch.setattr(tools, "_agent_role_map", _role_map(assistant="Marta",
                                                            butler="Alfred"))
    assert tools.persona_name() == "Marta"
    assert tools.persona_name("assistant") == "Marta"
    assert tools.persona_name("butler") == "Alfred"
    monkeypatch.setattr(tools, "_agent_role_map", {})
    assert tools.persona_name() == "the assistant"


def test_no_default_persona_name_in_static_text():
    import tools
    assert "Ellen" not in tools.emit_completion.description
    assert "Ellen" not in tools.SPECIALIST_OPEN_CONVERSATION_NOTICE_TEMPLATE

