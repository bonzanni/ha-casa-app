"""S6 §2.3 — an inbox for every specialist the Telegram default agent declares as a
delegate, provisioned at boot and on a reload that adds one; a failure leaves that role
without an inbox and redirects nothing."""
from __future__ import annotations

import os

import pytest

import agent_inbox as ai
from config import AgentConfig, DelegateEntry
from test_delegate_to_agent import _specialist_cfg, STUB_ROLE_ARTIFACT


@pytest.fixture
def clean(monkeypatch, tmp_path):
    monkeypatch.setattr(ai, "_inboxes", {})
    return tmp_path / "inbox"


def _resident(*delegates: str) -> AgentConfig:
    cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, role="assistant")
    cfg.delegates = [DelegateEntry(agent=d, purpose="p", when="w") for d in delegates]
    return cfg


def test_declared_delegates_that_are_loaded_specialists_get_an_inbox_and_nobody_else(clean):
    done = ai.provision_delegate_inboxes(str(clean), _resident("finance", "records", "ghost"),
                                         specialist_roles={"finance", "records", "other"})
    assert set(done) == {"finance", "records"}
    assert ai.get_inbox("finance") is not None and ai.get_inbox("records") is not None
    assert ai.get_inbox("ghost") is None and ai.get_inbox("other") is None
    assert ai.get_inbox("assistant") is None                       # the resident's own is wired elsewhere
    assert os.path.isdir(ai.get_inbox("finance").ready_dir)


def test_provisioning_is_idempotent_and_keeps_the_existing_inbox_object(clean):
    first = ai.provision_delegate_inboxes(str(clean), _resident("finance"), specialist_roles={"finance"})
    inbox = ai.get_inbox("finance")
    again = ai.provision_delegate_inboxes(str(clean), _resident("finance"), specialist_roles={"finance"})
    assert first == ["finance"] and again == []                    # nothing re-provisioned
    assert ai.get_inbox("finance") is inbox


def test_a_reload_that_adds_a_delegate_provisions_only_the_new_one(clean):
    ai.provision_delegate_inboxes(str(clean), _resident("finance"), specialist_roles={"finance", "records"})
    added = ai.provision_delegate_inboxes(str(clean), _resident("finance", "records"),
                                          specialist_roles={"finance", "records"})
    assert added == ["records"]


def test_a_failed_provision_leaves_that_role_without_an_inbox_and_the_others_intact(clean, monkeypatch):
    real = ai.open_inbox

    def failing(role, root):
        if role == "records":
            raise OSError("disk")
        return real(role, root)
    monkeypatch.setattr(ai, "open_inbox", failing)
    done = ai.provision_delegate_inboxes(str(clean), _resident("finance", "records"),
                                         specialist_roles={"finance", "records"})
    assert done == ["finance"]
    assert ai.get_inbox("records") is None and ai.get_inbox("finance") is not None
    assert ai.grants_for("records") == ((), ())                    # no grant, no redirect
