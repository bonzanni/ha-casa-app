"""The shared reader of one field of a vault item, through the ``op`` front."""
from __future__ import annotations

import logging
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


def test_read_field_takes_an_item_id_as_well_as_a_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _Run(_done(0, "fictional-id\n"))
    monkeypatch.setattr(vault_read.subprocess, "run", fake)
    assert vault_read.read_field(item="abc123def456", field="username") \
        == "fictional-id"
    assert fake.calls[0][0] == [
        "op", "item", "get", "abc123def456", "--vault", "Casa",
        "--fields", "label=username", "--reveal"]
    assert fake.calls[0][1]["timeout"] == vault_read.OP_TIMEOUT_S == 10


def test_read_field_logs_nothing_op_printed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    leak = "secret-looking-op-diagnostic"
    for result in (_done(1, "", f"[ERROR] {leak}"), _done(0, "\n", leak),
                   _done(0, "abcd-efgh\n", leak),
                   subprocess.TimeoutExpired(["op"], 10, output=leak,
                                             stderr=leak)):
        monkeypatch.setattr(vault_read.subprocess, "run", _Run(result))
        with caplog.at_level(logging.DEBUG):
            vault_read.read_field("iCloud Contacts", "password")
    assert leak not in caplog.text
    assert "abcd-efgh" not in caplog.text
    assert all(leak not in str(r.args) for r in caplog.records)
