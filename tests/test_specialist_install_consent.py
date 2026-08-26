from pathlib import Path
from types import SimpleNamespace

import pytest

from specialist_install import DependencyResolution, InspectionResult
from specialist_install_consent import (
    SpecialistInstallAckStore,
    install_consent_identity,
    prompt_specialist_install_consent,
    render_install_consent_message,
)


def _inspection(**overrides) -> InspectionResult:
    base = dict(
        component_id="casa-test/mtg", version="0.1.0", slug="mtg",
        component_checksum="sha256:" + "1" * 64, root_digest="sha256:" + "4" * 64,
        mission="Answer test questions.",
        default_persona_ref="casa/judge@0.1.0", default_persona_checksum="sha256:" + "2" * 64,
        required_config_names=(), required_secret_names=(),
        dependencies=(DependencyResolution(kind="persona", identifier="casa/judge@0.1.0",
                                            digest="sha256:" + "2" * 64, available=True, detail=""),),
        staged_dir=Path("/config/specialists/.staging/x"),
    )
    base.update(overrides)
    return InspectionResult(**base)


def test_identity_is_stable_for_the_same_inputs() -> None:
    a = install_consent_identity(component_id="casa-test/mtg", version="0.1.0",
                                  root_digest="sha256:" + "1" * 64, slug="mtg")
    b = install_consent_identity(component_id="casa-test/mtg", version="0.1.0",
                                  root_digest="sha256:" + "1" * 64, slug="mtg")
    assert a == b


def test_identity_changes_when_component_checksum_changes() -> None:
    a = install_consent_identity(component_id="casa-test/mtg", version="0.1.0",
                                  root_digest="sha256:" + "1" * 64, slug="mtg")
    b = install_consent_identity(component_id="casa-test/mtg", version="0.1.0",
                                  root_digest="sha256:" + "9" * 64, slug="mtg")
    assert a != b


def test_ack_store_is_unacked_until_recorded(tmp_path: Path) -> None:
    store = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    identity = install_consent_identity(component_id="casa-test/mtg", version="0.1.0",
                                         root_digest="sha256:" + "1" * 64, slug="mtg")
    assert store.is_acked(identity) is False
    store.record(identity=identity, component_id="casa-test/mtg", version="0.1.0",
                 component_checksum="sha256:" + "1" * 64, slug="mtg")
    assert store.is_acked(identity) is True


def test_ack_store_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "acks.json"
    identity = install_consent_identity(component_id="casa-test/mtg", version="0.1.0",
                                         root_digest="sha256:" + "1" * 64, slug="mtg")
    SpecialistInstallAckStore(path=path).record(
        identity=identity, component_id="casa-test/mtg", version="0.1.0",
        component_checksum="sha256:" + "1" * 64, slug="mtg")
    reopened = SpecialistInstallAckStore(path=path)
    assert reopened.is_acked(identity) is True


def test_ack_store_fails_closed_on_a_hand_edited_key(tmp_path: Path) -> None:
    import json

    path = tmp_path / "acks.json"
    identity = install_consent_identity(component_id="casa-test/mtg", version="0.1.0",
                                         root_digest="sha256:" + "1" * 64, slug="mtg")
    SpecialistInstallAckStore(path=path).record(
        identity=identity, component_id="casa-test/mtg", version="0.1.0",
        component_checksum="sha256:" + "1" * 64, slug="mtg")
    raw = json.loads(path.read_text(encoding="utf-8"))
    # Tamper: change the recorded component_checksum without updating the key.
    list(raw["acks"].values())[0]["component_checksum"] = "sha256:" + "0" * 64
    path.write_text(json.dumps(raw), encoding="utf-8")
    tampered = SpecialistInstallAckStore(path=path)
    assert tampered.is_acked(identity) is False  # whole-store fail-closed, never partial trust


def test_record_aborts_rather_than_wiping_on_transient_read_failure(
    tmp_path: Path, monkeypatch,
) -> None:
    """#310 (Sol r1): a transient ledger read failure (an OSError that is not
    file-missing) during a mutation's read-modify-write must abort — never
    persist the fail-closed empty view over previously recorded acks."""
    path = tmp_path / "acks.json"
    store = SpecialistInstallAckStore(path=path)
    ident_a = install_consent_identity(component_id="casa-test/a", version="0.1.0",
                                       root_digest="sha256:" + "1" * 64, slug="a")
    ident_b = install_consent_identity(component_id="casa-test/b", version="0.1.0",
                                       root_digest="sha256:" + "2" * 64, slug="b")
    store.record(identity=ident_a, component_id="casa-test/a", version="0.1.0",
                 component_checksum="sha256:" + "1" * 64, slug="a")

    real_read_text = Path.read_text

    def _flaky(self, *args, **kwargs):
        if self == path:
            raise PermissionError("transient read failure")
        return real_read_text(self, *args, **kwargs)

    with monkeypatch.context() as mp:
        mp.setattr(Path, "read_text", _flaky)
        with pytest.raises(OSError):
            store.record(identity=ident_b, component_id="casa-test/b", version="0.1.0",
                         component_checksum="sha256:" + "2" * 64, slug="b")

    assert SpecialistInstallAckStore(path=path).is_acked(ident_a)


def test_revoke_removes_an_ack(tmp_path: Path) -> None:
    store = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    identity = install_consent_identity(component_id="casa-test/mtg", version="0.1.0",
                                         root_digest="sha256:" + "1" * 64, slug="mtg")
    store.record(identity=identity, component_id="casa-test/mtg", version="0.1.0",
                 component_checksum="sha256:" + "1" * 64, slug="mtg")
    assert store.revoke(identity) is True
    assert store.is_acked(identity) is False


def test_render_message_names_slug_and_dependencies() -> None:
    text = render_install_consent_message(_inspection())
    assert "mtg" in text
    assert "casa/judge@0.1.0" in text


def test_render_message_discloses_role_casa_tool_grants() -> None:
    # #541: the operator approving the install must see the casa-framework
    # powers the specialist arrives with.
    text = render_install_consent_message(_inspection(role_tool_grants=(
        "mcp__casa-framework__recall_memory",
        "mcp__casa-framework__send_media",
    )))
    assert ("Casa tools: mcp__casa-framework__recall_memory, "
            "mcp__casa-framework__send_media") in text


def test_render_message_omits_casa_tools_line_when_role_grants_none() -> None:
    text = render_install_consent_message(_inspection())
    assert "Casa tools:" not in text

# --- #662 red case -------------------------------------------------------
#
# Specified by the red-case reviewer at 1c8033bb; frozen once accepted. The
# approval edit must be SELECTED from the reconciliation outcome rather than
# written before it, and only a literal True may select the success text — a
# bare None (what the pre-fix production callback and deliver_system_turn both
# return on the defect path) must not.
_662_SUCCESS = (
    "✅ Approved — requested an automatic configurator continuation for 'mtg'"
)
_662_CORRECTIVE = (
    "⚠️ Approved and saved — but the install of 'mtg' was not "
    "started automatically. Start a new configurator engagement and re-run "
    "the install; the approval recorded for this exact version is reused "
    "if it still applies."
)


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (True, _662_SUCCESS),
        (False, _662_CORRECTIVE),
        (None, _662_CORRECTIVE),
        (1, _662_CORRECTIVE),
    ],
    ids=["true", "false", "none", "truthy-one"],
)
async def test_662_only_a_literal_true_outcome_selects_the_success_edit(
    outcome, expected,
) -> None:
    class _Coordinator:
        def register_challenge(self, key, **kwargs):
            self.on_commit_sync = kwargs["on_commit_sync"]
            self.finish_factory = kwargs["finish_factory"]
            return SimpleNamespace(created=True)

    class _Acks:
        def __init__(self):
            self.records = []

        def record(self, **kwargs):
            self.records.append(kwargs)

    class _Channel:
        def __init__(self):
            self.edits = []

        async def edit_dm_message(self, chat_id, message_id, text):
            self.edits.append((chat_id, message_id, text))

    coordinator = _Coordinator()
    acks = _Acks()
    channel = _Channel()
    reconcile_calls = []

    async def _reconcile_cb():
        reconcile_calls.append(True)
        return outcome

    prompt_specialist_install_consent(
        coordinator=coordinator, channel=channel, chat_id=701, operator_id=701,
        inspection=_inspection(), acks=acks, reconcile_cb=_reconcile_cb,
    )

    req = SimpleNamespace(meta={})
    coordinator.on_commit_sync(0, req.meta)
    finish = coordinator.finish_factory(88, req)
    await finish({"outcome": "answered", "option_index": 0})

    actual = (
        len(acks.records), len(reconcile_calls), len(channel.edits),
        channel.edits[0][2],
    )
    assert actual == (1, 1, 1, expected), f"consent facts: {actual!r}"


async def test_662_a_raising_callback_is_contained_and_selects_the_corrective_edit(
) -> None:
    """The callback contract is never-raise, so the hook's own try/except is
    defence in depth — and defence in depth nobody tests is defence nobody
    has. A raise must not propagate out of the tap-callback finish hook, and
    must not be laundered into a success edit."""
    class _Coordinator:
        def register_challenge(self, key, **kwargs):
            self.on_commit_sync = kwargs["on_commit_sync"]
            self.finish_factory = kwargs["finish_factory"]
            return SimpleNamespace(created=True)

    class _Acks:
        def __init__(self):
            self.records = []

        def record(self, **kwargs):
            self.records.append(kwargs)

    class _Channel:
        def __init__(self):
            self.edits = []

        async def edit_dm_message(self, chat_id, message_id, text):
            self.edits.append((chat_id, message_id, text))

    coordinator = _Coordinator()
    acks = _Acks()
    channel = _Channel()

    async def _reconcile_cb():
        raise RuntimeError("contract violation")

    prompt_specialist_install_consent(
        coordinator=coordinator, channel=channel, chat_id=701, operator_id=701,
        inspection=_inspection(), acks=acks, reconcile_cb=_reconcile_cb,
    )
    req = SimpleNamespace(meta={})
    coordinator.on_commit_sync(0, req.meta)
    finish = coordinator.finish_factory(88, req)
    await finish({"outcome": "answered", "option_index": 0})  # must not raise

    actual = (len(acks.records), len(channel.edits), channel.edits[0][2])
    assert actual == (1, 1, _662_CORRECTIVE), f"raise facts: {actual!r}"


async def test_662_an_absent_callback_selects_the_corrective_edit() -> None:
    """No callback means no continuation was ever requested — the DM must not
    claim one was."""
    class _Coordinator:
        def register_challenge(self, key, **kwargs):
            self.on_commit_sync = kwargs["on_commit_sync"]
            self.finish_factory = kwargs["finish_factory"]
            return SimpleNamespace(created=True)

    class _Acks:
        def __init__(self):
            self.records = []

        def record(self, **kwargs):
            self.records.append(kwargs)

    class _Channel:
        def __init__(self):
            self.edits = []

        async def edit_dm_message(self, chat_id, message_id, text):
            self.edits.append((chat_id, message_id, text))

    coordinator = _Coordinator()
    acks = _Acks()
    channel = _Channel()
    prompt_specialist_install_consent(
        coordinator=coordinator, channel=channel, chat_id=701, operator_id=701,
        inspection=_inspection(), acks=acks,
    )
    req = SimpleNamespace(meta={})
    coordinator.on_commit_sync(0, req.meta)
    finish = coordinator.finish_factory(88, req)
    await finish({"outcome": "answered", "option_index": 0})

    actual = (len(acks.records), len(channel.edits), channel.edits[0][2])
    assert actual == (1, 1, _662_CORRECTIVE), f"absent-callback facts: {actual!r}"
