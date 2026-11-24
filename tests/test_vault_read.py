"""The shared reader of one field of a vault item, through the ``op`` front."""
from __future__ import annotations

import subprocess

import pytest

import vault_read


class _Run:
    def __init__(self, result):
        self.result = result
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _done(code: int, out: str = "", err: str = ""):
    return subprocess.CompletedProcess(["op"], code, out, err)


@pytest.fixture(autouse=True)
def _vault(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEPASSWORD_DEFAULT_VAULT", "Casa")


def test_read_field_runs_op_and_strips(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Run(_done(0, "  key-123\n"))
    monkeypatch.setattr(vault_read.subprocess, "run", fake)
    assert vault_read.read_field("OpenAI Voice", "credential") == "key-123"
    assert fake.calls[0][0] == [
        "op", "item", "get", "OpenAI Voice", "--vault", "Casa",
        "--fields", "label=credential", "--reveal"]


@pytest.mark.parametrize("result", [
    _done(1, "", "[ERROR] x"),
    _done(0, "\n"),
    subprocess.TimeoutExpired(["op"], 10),
    OSError("no op"),
])
def test_read_field_failures_are_none(monkeypatch: pytest.MonkeyPatch,
                                      result) -> None:
    monkeypatch.setattr(vault_read.subprocess, "run", _Run(result))
    assert vault_read.read_field("OpenAI Voice", "credential") is None


def test_read_field_without_vault_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEPASSWORD_DEFAULT_VAULT")
    fake = _Run(_done(0, "x"))
    monkeypatch.setattr(vault_read.subprocess, "run", fake)
    assert vault_read.read_field("OpenAI Voice", "credential") is None
    assert fake.calls == []


def test_contacts_reader_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    from contacts import service
    fake = _Run(_done(0, "fictional-id\n"))
    monkeypatch.setattr(vault_read.subprocess, "run", fake)
    assert service.read_icloud_secret("username") == "fictional-id"
    assert fake.calls[0][0][3] == "iCloud Contacts"
