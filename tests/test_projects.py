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


def test_builds_work_without_pytest_installed(tmp_path, monkeypatch):
    # Simulate a machine where pytest isn't installed — the built-in runner must take over.
    monkeypatch.setattr(sandbox, "_PYTEST_OK", False)
    proj_dir = tmp_path / "Projects" / "calc"
    proj_dir.mkdir(parents=True)
    (proj_dir / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (proj_dir / "test_app.py").write_text("from app import add\ndef test_add():\n    assert add(2, 3) == 5\n",
                                          encoding="utf-8")
    good = sandbox.run_tests(proj_dir, allow_network=True)
    assert good["ok"] is True and "passed" in good["stdout"]

    (proj_dir / "app.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")  # now wrong
    bad = sandbox.run_tests(proj_dir, allow_network=True)
    assert bad["ok"] is False and "FAILED" in bad["stdout"]


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
    second = propose({"attempt": 2, "failure": "AssertionError: boom", "diff": "",
                      "files": {"app.py": "ok=0\n"}})
    assert first == {"app.py": "ok=0\n"} and second == {"app.py": "ok=1\n"}
    assert "FAILING" in seen[1] and "boom" in seen[1]                # the error output is given to the model
    assert "ok=0" in seen[1] and "app.py" in seen[1]                 # ...along with his current code, so he edits it


def test_builder_picks_the_project_shape_from_a_plain_request():
    from builder.buildagent import classify, recipe_for
    # the boss just says the plain thing; Builder works out the shape
    assert classify("build a pong game") == "game"
    assert classify("make me a snake game") == "game"
    assert classify("a portfolio website") == "web"
    assert classify("a REST api for todos") == "api"
    assert classify("a command line tool to rename files") == "cli"
    assert classify("a desktop app with tkinter") == "gui"
    assert classify("a tool to convert a csv to json") == "data"
    assert classify("a reusable library for dates") == "library"
    assert classify("add two numbers") == "default"
    assert "tkinter" in recipe_for("a tkinter desktop app")
    assert "sample data" in recipe_for("convert csv to json")
    # and the recipe tells the model to make a game playable + tested, without the boss spelling it out
    assert "PLAYABLE" in recipe_for("a pong game") and "index.html" in recipe_for("a pong game")
    assert recipe_for("add two numbers") == ""


def test_plain_game_request_injects_the_playable_recipe():
    seen = []
    propose = make_propose(lambda p: (seen.append(p), 'ok\nFILES_JSON: {}')[1], "build a pong game",
                           log=lambda m: None)
    propose({"attempt": 1, "failure": None, "diff": ""})
    assert "PLAYABLE" in seen[0] and "index.html" in seen[0]         # the boss didn't have to ask for HTML


def test_game_build_starts_from_the_builtin_playable_template():
    from builder.buildagent import GAME_STARTER_FILES
    # the template itself is a valid, complete playable game (has canvas, a loop, controls)
    html = GAME_STARTER_FILES["index.html"]
    assert "<canvas" in html and "requestAnimationFrame" in html and "addEventListener" in html
    # a game build with no past solution adapts that working template instead of starting from nothing
    seen = []
    propose = make_propose(lambda p: (seen.append(p), 'x\nFILES_JSON: {}')[1], "a snake game",
                           log=lambda m: None, starter=None)
    propose({"attempt": 1, "failure": None, "diff": ""})
    assert "ADAPT it" in seen[0] and "getContext" in seen[0]       # the working template is in the prompt


def test_game_request_picks_the_matching_specific_template():
    from builder.templates import game_template_for
    assert game_template_for("a space pong game") == "pong"
    assert game_template_for("make me a snake game") == "snake"
    assert game_template_for("breakout with bricks") == "breakout"
    assert game_template_for("a fun little arcade game") == "game"   # generic when nothing specific


def test_pong_request_starts_from_real_pong_not_snake():
    # "a pong game" must adapt actual Pong (two paddles), not the snake-ish generic starter
    seen = []
    propose = make_propose(lambda p: (seen.append(p), 'x\nFILES_JSON: {}')[1], "a space pong game",
                           log=lambda m: None, starter=None)
    propose({"attempt": 1, "failure": None, "diff": ""})
    assert "playerY" in seen[0] and "cpuY" in seen[0]               # the real Pong template is in the prompt
    assert "Eat the red dots" not in seen[0]                        # NOT the snake starter


def test_build_in_an_existing_folder_adapts_what_is_there(tmp_path):
    # the modify path: an existing project's files become the starting point for the next build
    from builder.buildagent import run_build

    class Cfg:
        code_dirs = [str(tmp_path)]

    (tmp_path / "index.html").write_text("<canvas id='g'></canvas><!-- MY OWN PONG -->")
    (tmp_path / "test_game.py").write_text("def test_it():\n    assert '<canvas' in open('index.html').read()\n")
    proj = Project(tmp_path, Cfg())

    seen = []
    class LLM:
        def available(self): return True
        def chat(self, msgs):
            seen.append(msgs[-1]["content"])
            return {"content": "ok\nFILES_JSON: {}"}       # model changes nothing; template fallback keeps it runnable

    result = run_build("make the ball faster", proj, LLM(), tries=1, log=lambda m: None)
    assert seen, "the model should have been asked"
    assert "MY OWN PONG" in seen[0]                         # his current file was handed back to adapt
    assert "EXISTING project files" in seen[0]              # he's told to edit what's there, not start over
    # the model here changed nothing, so he must report that honestly, not fake a pass
    assert result.get("no_change") is True
    assert result["ok"] is False
    assert "MY OWN PONG" in (tmp_path / "index.html").read_text()   # project left untouched, not clobbered


def test_game_build_ignores_a_poisoned_recalled_solution(tmp_path):
    # Regression: an old build that fell back to a generic template used to be recalled for every game,
    # dragging "a pong game" back to the generic starter. A game must start from its OWN template instead.
    from builder.buildagent import run_build

    class Cfg:
        code_dirs = [str(tmp_path)]

    proj = Project(tmp_path / "pong-fresh", Cfg())

    class PoisonedMemory:
        def as_prompt(self, *a, **k): return ""
        def recall(self, *a, **k): return []
        def reference_note(self, *a, **k): return ""
        def recall_solution(self, task):
            return {"task": "old game", "files": {"index.html": "<canvas></canvas> Eat the red dots"}}
        def remember_build(self, *a, **k): pass

    seen = []
    class LLM:
        def available(self): return True
        def chat(self, msgs):
            seen.append(msgs[-1]["content"])
            return {"content": "ok\nFILES_JSON: {}"}

    run_build("a space pong game", proj, LLM(), memory=PoisonedMemory(), tries=1, log=lambda m: None)
    assert "playerY" in seen[0] and "cpuY" in seen[0]      # real Pong template was used
    assert "Eat the red dots" not in seen[0]               # NOT the poisoned recalled generic game


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
    # the loop reports the problems it had to solve along the way
    assert result["failures"] and "AssertionError" in "\n".join(result["failures"])


def test_problem_signature_pulls_out_the_error():
    from builder.projects import problem_signature
    assert problem_signature(["Traceback...\nModuleNotFoundError: No module named 'flask'\n"]) \
        .startswith("ModuleNotFoundError")
    assert "AssertionError" in problem_signature(["E   AssertionError: 2 != 3"])
    assert problem_signature([]) == ""


def test_every_template_is_a_working_project():
    import os
    import subprocess
    import sys
    import tempfile
    from pathlib import Path as _P
    from builder.buildagent import pygame_available
    from builder.templates import TEMPLATES
    env = {**os.environ, "SDL_VIDEODRIVER": "dummy", "SDL_AUDIODRIVER": "dummy",
           "PYGAME_HIDE_SUPPORT_PROMPT": "1"}
    for kind, t in TEMPLATES.items():
        if kind == "pygame" and not pygame_available():
            continue                                     # pygame is optional; skip when it isn't installed
        d = _P(tempfile.mkdtemp())
        for name, content in t["files"].items():
            (d / name).write_text(content, encoding="utf-8")
        r = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=d, capture_output=True, text=True, env=env)
        assert r.returncode == 0, f"template {kind} does not pass its own test:\n{r.stdout[-400:]}"


def test_drops_files_that_import_missing_libraries():
    from builder.buildagent import _usable_files, make_propose
    files = {"index.html": "<canvas></canvas>", "test_game.py": "def test_x():\n    assert 1",
             "app.py": "from selenium import webdriver\n", "test_app.py": "import totally_missing_pkg_xyz\n"}
    kept, dropped = _usable_files(files)
    assert set(kept) == {"index.html", "test_game.py"}              # files importing absent libs dropped
    assert {m for _, m in dropped} == {"selenium", "totally_missing_pkg_xyz"}
    # a local sibling import is NOT dropped
    k2, _ = _usable_files({"app.py": "x=1\n", "test_app.py": "from app import x\n"})
    assert set(k2) == {"app.py", "test_app.py"}
    # through propose: the model's good files are kept, the bad ones stripped
    reply = ('ok\nFILES_JSON: {"index.html": "<canvas></canvas>", "test_game.py": "def test_x():\\n assert 1",'
             ' "test_app.py": "from selenium import webdriver"}')
    propose = make_propose(lambda p: reply, "a snake game", log=lambda m: None)
    out = propose({"attempt": 1, "failure": None, "diff": ""})
    assert "test_app.py" not in out and "index.html" in out


def test_build_always_lands_a_working_result_even_if_the_model_is_useless(tmp_path):
    from builder.buildagent import run_build

    class UselessLLM:
        model = "useless"

        def available(self):
            return True

        def chat(self, messages, **kw):
            return {"content": "sorry, I cannot."}      # no FILES_JSON at all

    allowed = tmp_path / "Projects"
    allowed.mkdir()
    proj = Project(allowed / "snake", cfg_for(allowed, home=tmp_path))
    result = run_build("a snake game", proj, UselessLLM(), tries=1)
    assert result["ok"] is True                           # the working game template was dropped in
    assert (proj.root / "index.html").exists()
    html = (proj.root / "index.html").read_text()
    assert "<canvas" in html and "requestAnimationFrame" in html   # it's a real playable game


def test_build_falls_back_to_the_stronger_model(tmp_path):
    from builder.buildagent import run_build

    class FakeLLM:
        def __init__(self, reply, model):
            self._r, self.model = reply, model

        def available(self):
            return True

        def chat(self, messages, **kw):
            return {"content": self._r}

    test = "from app import add\\ndef test_add():\\n    assert add(2, 3) == 5\\n"
    fast = FakeLLM('draft\nFILES_JSON: {"app.py": "def add(a, b):\\n    return a - b\\n", "test_app.py": "'
                   + test + '"}', "fast-3b")                 # buggy: a - b -> test fails
    strong = FakeLLM('fixed\nFILES_JSON: {"app.py": "def add(a, b):\\n    return a + b\\n", "test_app.py": "'
                     + test + '"}', "strong-7b")             # correct

    allowed = tmp_path / "Projects"
    allowed.mkdir()
    proj = Project(allowed / "adder", cfg_for(allowed, home=tmp_path))
    # fast model alone can't pass; with the stronger fallback it does
    assert run_build("an add function", proj, fast, tries=2)["ok"] is False
    proj2 = Project(allowed / "adder2", cfg_for(allowed, home=tmp_path))
    result = run_build("an add function", proj2, fast, tries=2, fallback_llm=strong)
    assert result["ok"] is True
    assert "return a + b" in (proj2.root / "app.py").read_text()


def test_build_survives_a_model_error(tmp_path):
    from builder.buildagent import make_propose
    from builder.projects import build_and_test

    def boom(prompt):
        raise RuntimeError("Ollama returned 500: token repeat limit reached")

    allowed = tmp_path / "Projects"
    allowed.mkdir()
    proj = Project(allowed / "x", cfg_for(allowed, home=tmp_path))
    propose = make_propose(boom, "a thing", log=lambda m: None)
    result = build_and_test(proj, propose, run=lambda r, c: {"ok": False, "stderr": "", "stdout": ""},
                            max_tries=2, log=lambda m: None)
    assert result["ok"] is False and result["attempts"] == 2   # a generation error doesn't crash the build


def test_reuses_a_past_working_solution_to_build_faster(tmp_path):
    from builder.db import Store
    from builder.memory import Memory
    db = Store()
    mem = Memory(db, None, Config(home=tmp_path, code_dirs=[str(tmp_path)]))
    # a past pong build that PASSED, with its working files saved
    mem.remember_build("a retro pong game", str(tmp_path), ok=True, attempts=2,
                       files={"index.html": "<canvas>…pong…</canvas>", "pong.py": "def move(): ..."})
    # a FAILED build saves no files (nothing to reuse)
    mem.remember_build("a broken thing", str(tmp_path), ok=False, attempts=3, problem="boom", files={"x": "y"})

    sol = mem.recall_solution("make a pong game for me")       # similar task -> finds the working pong
    assert sol and "index.html" in sol["files"]
    assert mem.recall_solution("a spreadsheet budget tracker") is None   # unrelated -> nothing to reuse

    # and that starter code is handed to the model on the first draft, so he adapts instead of starting over
    seen = []
    propose = make_propose(lambda p: (seen.append(p), 'ok\nFILES_JSON: {}')[1], "build a pong game",
                           log=lambda m: None, starter=sol)
    propose({"attempt": 1, "failure": None, "diff": ""})
    assert "ADAPT it" in seen[0] and "index.html" in seen[0]


def test_build_memory_is_recorded_and_recalled(tmp_path):
    from builder.db import Store
    from builder.memory import Memory
    db = Store()
    mem = Memory(db, None, Config(home=tmp_path, code_dirs=[str(tmp_path)]))
    mem.remember_build("a flask login app", str(tmp_path), ok=False, attempts=3,
                       problem="ModuleNotFoundError: No module named 'flask'")
    mem.remember_build("a tip calculator", str(tmp_path), ok=True, attempts=1)

    recalled = mem.recall("flask login")
    assert any("flask login" in b["task"] for b in recalled["builds"])
    note = mem.as_prompt(recalled)
    assert "Projects you've built before" in note and "ModuleNotFoundError" in note


def _json_files(files: dict) -> str:
    import json
    return json.dumps(files)


def test_pygame_kind_only_when_pygame_is_installed():
    from builder.buildagent import classify, pygame_available, _wants_pygame
    # the wording detector is pure (no install needed)
    assert _wants_pygame(" a pygame space shooter ")
    assert _wants_pygame(" a python snake game ")
    assert not _wants_pygame(" a pong game ")           # no python/pygame word -> stays HTML
    # classify only returns pygame when pygame is actually importable
    want = "a python pygame space shooter"
    if pygame_available():
        assert classify(want) == "pygame"
    else:
        assert classify(want) == "game"                 # falls back to the always-works HTML game
    # plain game requests never become pygame
    assert classify("a pong game") == "game"


def test_pygame_build_uses_the_pygame_contract_and_template():
    from builder.buildagent import make_propose, pygame_available
    if not pygame_available():
        import pytest
        pytest.skip("pygame not installed in this environment")
    seen = []
    propose = make_propose(lambda p: (seen.append(p), "x\nFILES_JSON: {}")[1],
                           "a pygame space shooter", log=lambda m: None)
    propose({"attempt": 1, "failure": None, "diff": ""})
    assert "pygame" in seen[0].lower()
    assert "game.py" in seen[0]                         # the pygame template is the starting point
    assert "__main__" in seen[0]                        # window stays under __main__ (headless-safe)
