"""Authorized-lab execution: every safety gate must hold before a tool ever runs."""
import types

import pytest

from builder import labtools
from builder.config import Config
from builder.db import Store
from builder.safety import LabModeOff, TargetNotAuthorized


def _setup(tmp_path, authorize=None):
    cfg = Config(home=tmp_path, code_dirs=[str(tmp_path)])
    db = Store(cfg.db_path)
    if authorize:
        db.authorize_lab_target(authorize, "ctf", "owned/practice")
    return cfg, db


def _fake_run(monkeypatch, output="PORT 22 open"):
    monkeypatch.setattr(labtools, "tool_available", lambda t: True)
    def fake(cmd, capture_output, text, timeout):
        return types.SimpleNamespace(stdout=output, stderr="", returncode=0)
    monkeypatch.setattr(labtools.subprocess, "run", fake)


def test_refuses_when_lab_mode_off(tmp_path, monkeypatch):
    _fake_run(monkeypatch)
    cfg, db = _setup(tmp_path)                      # no authorized target -> lab off
    with pytest.raises(LabModeOff):
        labtools.run_tool(cfg, db, "nmap", "10.10.1.1", approve=lambda *a: True)


def test_refuses_tool_not_in_allowlist(tmp_path, monkeypatch):
    _fake_run(monkeypatch)
    cfg, db = _setup(tmp_path, authorize="10.10.1.1")
    with pytest.raises(labtools.LabToolError):
        labtools.run_tool(cfg, db, "metasploit", "10.10.1.1", approve=lambda *a: True)


def test_refuses_unsafe_args_and_other_hosts(tmp_path, monkeypatch):
    _fake_run(monkeypatch)
    cfg, db = _setup(tmp_path, authorize="10.10.1.1")
    with pytest.raises(labtools.LabToolError):          # shell metacharacter
        labtools.run_tool(cfg, db, "nmap", "10.10.1.1", args=["; rm -rf /"], approve=lambda *a: True)
    with pytest.raises(labtools.LabToolError):          # a different host in args
        labtools.run_tool(cfg, db, "nmap", "10.10.1.1", args=["8.8.8.8"], approve=lambda *a: True)


def test_refuses_target_not_authorized(tmp_path, monkeypatch):
    _fake_run(monkeypatch)
    cfg, db = _setup(tmp_path, authorize="10.10.1.1")
    with pytest.raises(TargetNotAuthorized):
        labtools.run_tool(cfg, db, "nmap", "9.9.9.9", approve=lambda *a: True)   # different (unauthorized) target


def test_refuses_when_not_approved(tmp_path, monkeypatch):
    from builder.safety import ApprovalRequired
    _fake_run(monkeypatch)
    cfg, db = _setup(tmp_path, authorize="10.10.1.1")
    with pytest.raises(ApprovalRequired):
        labtools.run_tool(cfg, db, "nmap", "10.10.1.1", approve=lambda *a: False)   # user says no


def test_runs_and_logs_when_everything_passes(tmp_path, monkeypatch):
    _fake_run(monkeypatch, output="22/tcp open ssh")
    cfg, db = _setup(tmp_path, authorize="10.10.1.1")
    result = labtools.run_tool(cfg, db, "nmap", "10.10.1.1", args=["-sV"], approve=lambda *a: True)
    assert "ssh" in result["output"]
    assert result["cmd"] == ["nmap", "-sV", "10.10.1.1"]          # target is always appended last
    assert any(e["action"] == "offense" and e["decision"] in ("approved", "allowed")
               for e in db.safety_events(10))                     # it's in the audit log
