"""Part 3: Builder's project workspace and the build-and-test loop.

These prove the first real enforced gate (scope: Builder only touches the boss's listed folders), that
every edit is reversible (backup / diff / undo), that the sandbox runs a project's OWN tests and reports
pass/fail, and that the loop fixes-and-retries, bounded.
"""

import sys

import pytest

from builder.buildagent import files_from_reply, make_propose
from builder.config import Config
from builder import sandbox
from builder.projects import (Project, ScopeError, build_and_test, check_scope,
                              restore_backup, save_backup, within_scope)


def cfg_for(*dirs, home):
    return Config(home=home, code_dirs=[str(d) for d in dirs])


# --------------------------------------------------------------------------- scope gate
def test_scope_allows_inside_and_refuses_outside(tmp_path):
    allowed = tmp_path / "Projects"
    allowed.mkdir()
    dirs = [allowed.resolve()]
    assert within_scope(allowed / "app" / "main.py", dirs) is True
    assert within_scope(tmp_path / "secrets.txt", dirs) is False
    check_scope(allowed / "x.py", dirs)                      # no raise
    with pytest.raises(ScopeError):
        check_scope(tmp_path / "x.py", dirs)                 # outside the allowed folder


def test_scope_refused_when_no_folders_set(tmp_path):
    with pytest.raises(ScopeError):
        check_scope(tmp_path / "x.py", [])                   # boss hasn't opted in: Builder can touch nothing


def test_project_refuses_a_root_outside_scope(tmp_path):
    allowed = tmp_path / "Projects"
    allowed.mkdir()
    cfg = cfg_for(allowed, home=tmp_path)
    with pytest.raises(ScopeError):
        Project(tmp_path / "elsewhere", cfg)                 # a project outside the allowed folders is refused


def test_project_refuses_path_escape(tmp_path):
    allowed = tmp_path / "Projects"
    allowed.mkdir()
    proj = Project(allowed / "app", cfg_for(allowed, home=tmp_path))
    with pytest.raises(ScopeError):
        proj.write("../../escape.py", "boom")                # ../.. out of scope is refused


# --------------------------------------------------------------------------- reversible edits
def test_write_backup_diff_and_undo(tmp_path):
    allowed = tmp_path / "Projects"
    allowed.mkdir()
    proj = Project(allowed / "app", cfg_for(allowed, home=tmp_path))
    (proj.root / "existing.py").write_text("print('old')\n", encoding="utf-8")

    proj.write("existing.py", "print('new')\n")              # modify an existing file
    proj.write("brand_new.py", "x = 1\n")                    # create a new file

    assert set(proj.changed()) == {"existing.py", "brand_new.py"}
    diff = proj.diff()
    assert "-print('old')" in diff and "+print('new')" in diff
    assert "+x = 1" in diff

    proj.undo()
    assert (proj.root / "existing.py").read_text() == "print('old')\n"   # restored
    assert not (proj.root / "brand_new.py").exists()                     # created file removed
    assert proj.changed() == []


# --------------------------------------------------------------------------- real test runner
def test_run_tests_detects_and_reports_pass_then_fail(tmp_path):
    allowed = tmp_path / "Projects"
    proj_dir = allowed / "calc"
    proj_dir.mkdir(parents=True)
    (proj_dir / "test_calc.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8")

    assert sandbox.detect_test_command(proj_dir) == [sys.executable, "-m", "pytest", "-q"]
    good = sandbox.run_tests(proj_dir, allow_network=True)   # allow_network: test the runner, not net isolation
    assert good["ok"] is True and good["exit_code"] == 0

    (proj_dir / "test_calc.py").write_text("def test_bad():\n    assert 1 + 1 == 3\n", encoding="utf-8")
    bad = sandbox.run_tests(proj_dir, allow_network=True)
    assert bad["ok"] is False and "assert" in (bad["stdout"] + bad["stderr"]).lower()


def test_run_tests_reports_when_no_tests_found(tmp_path):
    empty = tmp_path / "Projects" / "empty"
    empty.mkdir(parents=True)
    res = sandbox.run_tests(empty)
    assert res["ok"] is False and "No test command" in res["stderr"]


# --------------------------------------------------------------------------- the loop
def test_build_and_test_fixes_and_retries(tmp_path):
    allowed = tmp_path / "Projects"
    allowed.mkdir()
    proj = Project(allowed / "app", cfg_for(allowed, home=tmp_path))
    calls = []

    def propose(ctx):
        calls.append(ctx["attempt"])
        # First attempt writes a buggy add(); the fix comes after it sees the failure.
        if ctx["failure"] is None:
            return {"app.py": "def add(a, b):\n    return a - b\n"}
        return {"app.py": "def add(a, b):\n    return a + b\n"}

    def fake_run(root, cmd):
        from builder.projects import Project as _P
        ok = "a + b" in (root / "app.py").read_text()
        return {"ok": ok, "exit_code": 0 if ok else 1, "stdout": "", "stderr": "" if ok else "AssertionError"}

    result = build_and_test(proj, propose, run=fake_run, max_tries=3)
    assert result["ok"] is True and result["attempts"] == 2           # failed once, fixed, passed
    assert calls == [1, 2]
    assert "def add" in result["diff"]


def test_build_and_test_is_bounded(tmp_path):
    allowed = tmp_path / "Projects"
    allowed.mkdir()
    proj = Project(allowed / "app", cfg_for(allowed, home=tmp_path))

    def propose(ctx):
        return {"app.py": "still broken\n"}

    def always_fail(root, cmd):
        return {"ok": False, "exit_code": 1, "stdout": "", "stderr": "nope"}

    result = build_and_test(proj, propose, run=always_fail, max_tries=3)
    assert result["ok"] is False and result["attempts"] == 3          # gives up after max_tries, no infinite loop


# --------------------------------------------------------------------------- model adapter
def test_files_from_reply_parses_and_rejects_bad_paths():
    summary, files = files_from_reply(
        'Built a tip calculator with a test.\nFILES_JSON: '
        '{"tip.py": "def tip(x):\\n    return x*0.2\\n", "../evil.py": "bad", "/abs.py": "nope"}')
    assert summary == "Built a tip calculator with a test."
    assert "tip.py" in files and "../evil.py" not in files            # path escapes dropped
    assert "abs.py" in files and not any(p.startswith("/") for p in files)   # leading slash stripped
    assert files_from_reply("no json here") == ("no json here", {})


def test_make_propose_feeds_failure_back_to_the_model():
    seen = []

    def generate(prompt):
        seen.append(prompt)
        if "FAILING" in prompt:
            return 'Fixed it.\nFILES_JSON: {"app.py": "ok=1\\n"}'
        return 'First draft.\nFILES_JSON: {"app.py": "ok=0\\n"}'

    propose = make_propose(generate, "build a thing", log=lambda m: None)
    first = propose({"attempt": 1, "failure": None, "diff": ""})
    second = propose({"attempt": 2, "failure": "AssertionError: boom", "diff": ""})
    assert first == {"app.py": "ok=0\n"} and second == {"app.py": "ok=1\n"}
    assert "FAILING" in seen[1] and "boom" in seen[1]                # the error output is given to the model


# --------------------------------------------------------------------------- persisted undo
def test_save_and_restore_backup_across_runs(tmp_path):
    allowed = tmp_path / "Projects"
    allowed.mkdir()
    cfg = cfg_for(allowed, home=tmp_path)
    proj = Project(allowed / "app", cfg)
    (proj.root / "keep.py").write_text("v1\n", encoding="utf-8")
    proj.write("keep.py", "v2\n")                                     # modified
    proj.write("made.py", "new\n")                                   # created

    rec = tmp_path / "backup.json"
    save_backup(proj, rec)
    # a fresh run (no in-memory state) restores from the saved record
    restored = restore_backup(rec, cfg)
    assert set(restored) == {"keep.py", "made.py"}
    assert (proj.root / "keep.py").read_text() == "v1\n"             # modified file rolled back
    assert not (proj.root / "made.py").exists()                     # created file removed


# --------------------------------------------------------------------------- end to end (real pytest)
def test_end_to_end_write_fail_fix_pass_with_real_tests(tmp_path):
    """The whole loop against the REAL sandbox: a canned 'model' writes a bug, sees the pytest failure, fixes it."""
    allowed = tmp_path / "Projects"
    allowed.mkdir()
    proj = Project(allowed / "adder", cfg_for(allowed, home=tmp_path))
    test_code = "from app import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"

    def generate(prompt):
        if "FAILING" in prompt:                                      # second pass: fix the bug
            return 'Fixed.\nFILES_JSON: ' + _json_files({"app.py": "def add(a, b):\n    return a + b\n",
                                                          "test_app.py": test_code})
        return 'First draft.\nFILES_JSON: ' + _json_files({"app.py": "def add(a, b):\n    return a - b\n",
                                                           "test_app.py": test_code})

    propose = make_propose(generate, "an add() function with a test", log=lambda m: None)
    result = build_and_test(proj, propose, max_tries=3, log=lambda m: None)   # real sandbox.run_tests
    assert result["ok"] is True and result["attempts"] == 2
    assert "def add(a, b):\n    return a + b" in (proj.root / "app.py").read_text()


def _json_files(files: dict) -> str:
    import json
    return json.dumps(files)
