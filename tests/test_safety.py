"""Part 5: the two security modes and their gates — enforced in code, with an audit log.

Covers the acceptance criteria from BUILDER_SPEC §8:
- authorized-lab mode is OFF unless the boss turns it on AND confirms the target;
- a request against an unapproved/remote target is refused;
- a destructive/network/install action asks for approval first;
- every decision is written to the audit log.
"""

import pytest

from builder.config import Config
from builder.db import Store
from builder.safety import (ApprovalRequired, LabModeOff, Safety, SafetyError,
                            TargetNotAuthorized)


@pytest.fixture
def db():
    return Store()


def sf(db, approver=None, lab_mode=False, **kw):
    cfg = Config(lab_mode=lab_mode, **kw)
    return Safety(db, cfg, approver=approver, log=lambda m: None)


# --------------------------------------------------------------------------- lab mode off by default
def test_lab_mode_is_off_by_default_and_offense_is_refused(db):
    s = sf(db)
    assert s.lab_enabled() is False
    with pytest.raises(LabModeOff):
        s.guard_lab()
    assert db.safety_events()[0]["decision"] == "refused"       # the refusal is logged


def test_env_flag_alone_does_not_enable_lab_without_a_target(db):
    # BUILDER_LAB_MODE=1 is not enough on its own — there must be a confirmed target on record.
    s = sf(db, lab_mode=True)
    assert s.lab_enabled() is False
    with pytest.raises(LabModeOff):
        s.guard_lab()


# --------------------------------------------------------------------------- turning it on needs confirmation
def test_enabling_lab_requires_explicit_confirmation(db):
    s = sf(db)
    with pytest.raises(ApprovalRequired):
        s.enable_lab("my-test-vm", kind="vm")                   # no confirmed=True -> refused
    assert s.lab_enabled() is False

    with pytest.raises(SafetyError):
        s.enable_lab("", kind="vm", confirmed=True)             # must name a target

    rec = s.enable_lab("my-test-vm", kind="vm", note="local practice box", confirmed=True)
    assert rec["target"] == "my-test-vm" and s.lab_enabled() is True
    assert db.safety_events()[0]["decision"] == "enabled"       # logged


# --------------------------------------------------------------------------- target authorization
def test_only_authorized_targets_are_allowed(db):
    s = sf(db)
    s.enable_lab("10.0.0.5", kind="vm", confirmed=True)         # the boss's own lab VM
    s.guard_target("10.0.0.5")                                  # authorized -> no raise
    with pytest.raises(TargetNotAuthorized):
        s.guard_target("example.com")                          # never authorized -> refused
    with pytest.raises(TargetNotAuthorized):
        s.guard_target("8.8.8.8")                              # arbitrary remote -> refused
    decisions = {(e["target"], e["decision"]) for e in db.safety_events()}
    assert ("10.0.0.5", "allowed") in decisions and ("example.com", "refused") in decisions


def test_disabling_lab_clears_authorizations(db):
    s = sf(db)
    s.enable_lab("10.0.0.5", kind="vm", confirmed=True)
    s.disable_lab()
    assert s.lab_enabled() is False
    with pytest.raises(TargetNotAuthorized):
        s.guard_target("10.0.0.5")                             # authorization was cleared


# --------------------------------------------------------------------------- approval gate
def test_risky_actions_need_approval_and_deny_by_default(db):
    s = sf(db, approver=None)                                  # no approver -> Builder must ask
    for kind in ("destructive", "network", "install", "important_files", "offense"):
        with pytest.raises(ApprovalRequired):
            s.guard_action(kind, detail=f"a {kind} action")
    assert all(e["decision"] == "refused" for e in db.safety_events())


def test_safe_actions_pass_without_approval(db):
    s = sf(db, approver=None)
    assert s.guard_action("read") is True                      # not a risky kind -> allowed, no prompt
    assert db.safety_events() == []                            # nothing to log


def test_approver_can_allow_a_risky_action_and_it_is_logged(db):
    seen = []

    def approver(kind, detail, target):
        seen.append((kind, detail, target))
        return kind == "network"                              # the boss says yes to network, no to the rest

    s = sf(db, approver=approver)
    assert s.guard_action("network", detail="fetch a library's docs") is True
    with pytest.raises(ApprovalRequired):
        s.guard_action("install", detail="pip install requests")
    decisions = [(e["action"], e["decision"]) for e in db.safety_events()]
    assert ("network", "approved") in decisions and ("install", "denied") in decisions
    assert seen[0][0] == "network"


def test_a_broken_approver_means_not_approved(db):
    def boom(kind, detail, target):
        raise RuntimeError("approver crashed")

    s = sf(db, approver=boom)
    with pytest.raises(ApprovalRequired):
        s.guard_action("destructive", detail="rm -rf build/")   # a crash is treated as "no", never "yes"
