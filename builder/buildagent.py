"""Turn the coder model into the build-and-test loop's ``propose`` callback.

Builder asks the model for the whole set of files to write, as one JSON object, and runs them through the
project workspace and the test sandbox. On a test failure the same contract is used again, now with the
error output, so the model fixes its own code. ``files_from_reply`` is the tolerant parser; it's kept
separate so it can be tested without a running model.
"""

from __future__ import annotations

import json
import re

from .templates import game_template_for, template_files, template_names  # noqa: F401

FILE_CONTRACT = """

You are writing a small, self-contained project. Reply with:
1. A short plain-language note: what you built (or fixed) and how its tests check it.
2. On the LAST line, exactly one JSON object mapping each file path to its full contents:
   FILES_JSON: {"app.py": "....", "test_app.py": "...."}

Rules:
- Include the project's OWN tests (e.g. a test_*.py using pytest) so the work can be verified.
- Give the COMPLETE contents of every file you want written — not a diff, not "...".
- Keep it to the files that matter. Use relative paths only (never a leading / or ..).

This runs in a HEADLESS sandbox with NO internet, NO new packages, and a short time limit, so:
- Use ONLY the Python standard library (or plain HTML/CSS/JavaScript). Do NOT import third-party packages
  like pygame, flask, numpy, requests — they are not installed and cannot be installed.
- Put the real logic in PLAIN FUNCTIONS the test can call directly (e.g. move(state), is_collision(...)).
  Never put a `while True:` loop, `input()`, a game window, or a server at module top level — importing the
  file must return immediately, or the test will hang and fail.
- The test must import those functions and check them WITHOUT opening a window, a screen or a network
  connection, and WITHOUT running the main loop.
- For a GAME or a WEBSITE, prefer a SINGLE self-contained .html file (canvas/JS) for what the user runs,
  AND a small testable .py (or the pure logic) with a pytest test. Keep the drawing/animation out of the
  tested part.

NEVER ship stubs or broken tests — this is the most common reason a build fails:
- Fully implement every function. Do NOT leave `pass`, `...`, `TODO`, or an empty body anywhere.
- Tests use ONLY the standard library + plain `assert`. NEVER import selenium, playwright, pytest
  plugins, requests, or any third-party package in a test — a missing import fails the whole run. To
  check an HTML file, open it as text and assert on the string; do not launch a browser.
- Tests must use CONCRETE values and call functions with real arguments. Do NOT reference an undefined
  name (like `expected_result`), and do NOT give a test function parameters unless you define that pytest
  fixture yourself. A plain test is: `def test_x():` then set up real values and `assert`.
- In HTML/JS, DECLARE every variable before you use it (e.g. the food/target position), so the page runs
  with no ReferenceError when opened."""


# Builder works out the right shape of project from a plain request, so the boss doesn't have to spell out
# "single HTML file, canvas, with a test" every time. Each kind adds concrete, buildable-and-testable specs.
RECIPES = {
    "game": (
        "\n\nThis is a GAME — make it actually PLAYABLE, and keep it simple to verify:\n"
        "- index.html: ONE self-contained file (inline CSS + JS, a <canvas>), keyboard/mouse controls, an\n"
        "  on-screen score, and a game loop with requestAnimationFrame. DECLARE every variable before using\n"
        "  it (the snake, the food/target, the score) so it runs with NO errors when opened. This is the\n"
        "  thing the user plays — make it complete and fun.\n"
        "- test_game.py: a SIMPLE pytest test that opens index.html as TEXT and asserts it contains the\n"
        "  essentials — e.g. `html = open('index.html').read()` then `assert '<canvas' in html`,\n"
        "  `assert 'requestAnimationFrame' in html`, `assert 'addEventListener' in html`. Use plain\n"
        "  `def test_...()` functions with no parameters. Do NOT import the HTML, run the game, or write a\n"
        "  separate logic.py — the single playable index.html plus this structural test is all you need."),
    "web": (
        "\n\nThis is a WEBSITE/PAGE — make it viewable:\n"
        "- index.html: ONE self-contained file with inline CSS (and JS if needed). It must look right when\n"
        "  opened directly in a browser.\n"
        "- If there's any logic/data, put it in a small .py with a pytest test (test_*.py)."),
    "api": (
        "\n\nThis is a small API/SERVER — standard library only:\n"
        "- Use http.server from the standard library (NEVER flask/fastapi — not installed).\n"
        "- Put the request handling logic in plain FUNCTIONS, and add a pytest test that calls those\n"
        "  functions directly (do NOT start the server at import time or in the test)."),
    "gui": (
        "\n\nThis is a DESKTOP GUI app — tkinter is in the standard library, so use it (never PyQt/wx):\n"
        "- Put ALL the logic in plain functions; build the window under `if __name__ == '__main__':` only.\n"
        "- The pytest test imports and checks the logic functions and NEVER opens a window (the sandbox is\n"
        "  headless, so a test that creates a Tk() will fail)."),
    "data": (
        "\n\nThis is a DATA tool — read/transform/output using only the standard library (csv, json, re):\n"
        "- Put the transformation in plain functions that take data in and return data out.\n"
        "- The pytest test feeds in small sample data and checks the result. Don't read real files or take\n"
        "  input at import time."),
    "library": (
        "\n\nThis is a REUSABLE module — a single .py with well-named functions/classes and short docstrings.\n"
        "- No CLI or main loop unless asked.\n"
        "- Add a THOROUGH pytest test covering normal cases AND edge cases (empty, zero, negative, bad input)."),
    "cli": (
        "\n\nThis is a TOOL/SCRIPT:\n"
        "- Put the real work in plain functions in a .py, with a tiny argparse CLI under\n"
        "  `if __name__ == '__main__':` (never run work or input() at import time).\n"
        "- Add a pytest test that imports and checks those functions."),
}

_KIND_WORDS = {
    "game": ("game", "pong", "snake", "tetris", "breakout", "arcade", "puzzle", "platformer", "shooter",
             "flappy", "2048", "tic-tac-toe", "tic tac toe", "maze"),
    "gui": ("gui", "desktop app", "tkinter", "window app", "app with buttons", "desktop application"),
    "web": ("website", "web site", "web page", "webpage", "landing page", "portfolio", "html page",
            "site", "homepage", "blog"),
    "api": ("api", "endpoint", "rest", "server", "webhook", "microservice"),
    "data": ("parser", "parse ", "convert", "converter", "csv", "json", "spreadsheet", "data processing",
             "clean the data", "transform"),
    "library": ("library", "module", "package", "reusable", "helper functions"),
    "cli": ("cli", "command line", "command-line", "script", "tool", "utility", "bot"),
}


# Builder starts every game build from this complete, PLAYABLE template (defined in templates.py),
# so even the small model only has to adapt working code instead of writing a game from scratch.
GAME_STARTER_FILES = template_files("game")


def classify(task: str) -> str:
    """Guess the kind of project from a plain request, so Builder adds the right build recipe himself."""
    t = " " + task.lower() + " "
    for kind, words in _KIND_WORDS.items():
        if any(w in t for w in words):
            return kind
    return "default"


def recipe_for(task: str) -> str:
    """The extra, concrete build spec for this kind of task (empty for a generic project)."""
    return RECIPES.get(classify(task), "")


BUILD_SYSTEM = (
    "You are Builder, a careful, skilled coder. You write small, correct, self-contained projects and the "
    "tests that prove they work. You explain briefly, then give the complete files. You never touch the "
    "network or files outside the project. If something is unsafe or unclear, you say so instead of guessing.")


def _imported_modules(content: str) -> set[str]:
    """Top-level module names a Python file imports (best effort)."""
    import ast
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return set()
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                mods.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.add(node.module.split(".")[0])
    return mods


def _usable_files(files: dict) -> tuple[dict, list]:
    """Drop any .py file that imports a library that isn't installed (e.g. selenium, pygame, flask) — the
    small model keeps ignoring 'standard library only', and one bad import fails the whole test run."""
    import importlib.util
    import sys
    local = {name.rsplit("/", 1)[-1][:-3] for name in files if name.endswith(".py")}
    stdlib = getattr(sys, "stdlib_module_names", set())
    kept, dropped = {}, []
    for path, content in files.items():
        if not path.endswith(".py"):
            kept[path] = content
            continue
        bad = [m for m in _imported_modules(content)
               if m not in local and m not in stdlib and importlib.util.find_spec(m) is None]
        if bad:
            dropped.append((path, bad[0]))
        else:
            kept[path] = content
    return kept, dropped


def _generator(llm):
    """A prompt -> text function backed by an Ollama client."""
    def gen(prompt: str) -> str:
        return llm.chat([{"role": "system", "content": BUILD_SYSTEM},
                         {"role": "user", "content": prompt}]).get("content", "")
    return gen


def run_build(task, project, llm, memory=None, test_cmd=None, tries=3, log=print, fallback_llm=None):
    """Build + test a project: recall memory, build on ``llm``, and if it fails retry once on
    ``fallback_llm`` (the stronger/slower model) adapting what the first model produced. Records the
    result (and the working files on success) in memory. Returns the build result dict.
    """
    from .projects import build_and_test, problem_signature

    note = memory.as_prompt(memory.recall(task), max_chars=1500) if memory is not None else ""

    # If the folder ALREADY has a project in it, Builder MODIFIES it: its current files become the starting
    # point, so "make the ball faster" / "add a second paddle" adapts the real code instead of starting over.
    starter = None
    try:
        existing = project.existing_files()
    except Exception:  # noqa: BLE001
        existing = {}
    if existing:
        log("  (this folder already has a project — adapting what's there instead of starting fresh)")
        starter = {"task": f"the existing project in this folder (modify it): {task}", "files": existing}
    elif memory is not None:
        try:
            starter = memory.recall_solution(task)   # no project here yet — reuse a close past build if any
        except Exception:  # noqa: BLE001
            starter = None

    propose = make_propose(_generator(llm), task, log=log, memory_note=note, starter=starter)
    result = build_and_test(project, propose, test_cmd=test_cmd, max_tries=tries, log=log)

    if not result["ok"] and fallback_llm is not None:
        try:
            available = fallback_llm.available()
        except Exception:  # noqa: BLE001
            available = False
        if available:
            log("  the fast model couldn't finish it — retrying with the stronger model (slower)…")
            current = {name: project.read(name) for name in project.changed()}
            propose2 = make_propose(_generator(fallback_llm), task, log=log, memory_note=note,
                                    starter={"task": task, "files": current} if current else starter)
            result = build_and_test(project, propose2, test_cmd=test_cmd, max_tries=tries, log=log)

    # Last-resort guarantee: if neither model could pass, drop in the clean working template for this kind
    # (or the reused past solution) and ship that — so a build ALWAYS leaves a runnable result.
    if not result["ok"]:
        from . import sandbox
        baseline = starter["files"] if (starter and starter.get("files")) else template_files(classify(task))
        if baseline:
            log("  couldn't get the model's version to pass — using a clean working template so you still "
                "get something that runs.")
            project.write_all(baseline)
            test = sandbox.run_tests(project.root, test_cmd)
            result = {"ok": bool(test.get("ok")), "attempts": result.get("attempts", tries), "test": test,
                      "diff": project.diff(), "failures": result.get("failures", []), "used_template": True}

    if memory is not None:
        try:
            files = {name: project.read(name) for name in project.changed()} if result["ok"] else None
            memory.remember_build(task, str(project.root), result["ok"], result["attempts"],
                                  problem_signature(result.get("failures", [])), files=files)
        except Exception:  # noqa: BLE001 — recording must never fail a build
            pass
    return result


def files_from_reply(text: str) -> tuple[str, dict[str, str]]:
    """Split a model reply into (summary, {path: content}). Returns {} if no valid FILES_JSON is found."""
    m = re.search(r"FILES_JSON:\s*(\{.*\})\s*$", text, re.S)
    if not m:
        return text.strip(), {}
    summary = text[:m.start()].strip()
    try:
        raw = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return summary, {}
    files = {}
    for path, content in (raw.items() if isinstance(raw, dict) else []):
        path = str(path).strip().lstrip("/")
        if not path or path.startswith("..") or "/.." in path or not isinstance(content, str):
            continue                       # relative paths only; the Project gate re-checks scope anyway
        files[path] = content
    return summary, files


def make_propose(generate, task: str, log=print, memory_note: str = "", starter: dict | None = None):
    """Build a ``propose(ctx)`` for ``build_and_test`` from a ``generate(prompt) -> text`` function.

    ``generate`` is anything that answers a prompt (the local Ollama coder model, or the family brain).
    The first call writes the project; later calls carry the failing test output so the model can fix it.
    ``memory_note`` (what Builder remembers — the boss's stack, past fixes, lessons) is included so his
    work benefits from memory even offline. ``starter`` is the working files from a similar past build, so
    he ADAPTS proven code instead of starting from scratch — faster and more reliable.
    """
    note = (f"\n\nWhat you remember (apply it):\n{memory_note}\n" if memory_note else "")
    recipe = recipe_for(task)          # Builder works out the right project shape from the plain request
    contract = FILE_CONTRACT + recipe
    kind = classify(task)
    if kind != "default":
        log(f"  (building this as a {kind} — making it {'playable' if kind == 'game' else 'runnable'} and tested)")
    # Every build starts from a WORKING template (a past solution if there's a close one, else the built-in
    # template for this kind). It's kept as the guaranteed baseline, so a build can't come out empty/broken.
    if starter and starter.get("files"):
        baseline_files, baseline_task = dict(starter["files"]), starter.get("task", "")
    elif kind == "game":
        # Start from the SPECIFIC game the request names (pong/breakout/snake), not a generic starter,
        # so "a pong game" adapts real Pong instead of the snake-ish default.
        gkind = game_template_for(task)
        baseline_files, baseline_task = template_files(gkind), f"a working {gkind} game"
    else:
        baseline_files, baseline_task = template_files(kind), f"a working {kind} starter"
    files_txt = "".join(f"\n=== {n} ===\n{c}\n" for n, c in baseline_files.items())
    starter_block = (f"\n\nHere is a WORKING starting point ({baseline_task!r}) that already runs and passes "
                     f"its test — ADAPT it to the task, keeping what works (don't start from scratch):\n{files_txt[:8000]}")
    log("  (starting from a working template and adapting it)")

    def propose(ctx: dict) -> dict[str, str]:
        if ctx.get("failure"):
            current = "".join(f"\n=== {name} ===\n{content}\n" for name, content in (ctx.get("files") or {}).items())
            prompt = (f"Task: {task}{note}\n\nHere are the files you wrote so far:\n{current[:9000]}\n\n"
                      f"Their tests are FAILING. Here is the test output:\n\n{ctx['failure'][:6000]}\n\n"
                      f"Find the actual cause, change ONLY what's needed to fix it, and return the full "
                      f"updated files (keep the parts that already work)." + contract)
            log(f"  asking the coder model to fix attempt {ctx['attempt'] - 1}'s failures…")
        else:
            prompt = f"Task: {task}{note}{starter_block}" + contract
            log("  asking the coder model to write the project…")
        summary, files = files_from_reply(generate(prompt))
        if summary:
            log(f"  model: {summary.splitlines()[0][:100]}")
        # Drop files that import libraries that aren't installed (selenium, pygame, flask, …) — the model
        # keeps ignoring "standard library only", and one bad import fails the whole test run.
        files, dropped = _usable_files(files)
        for path, mod in dropped:
            log(f"  dropped {path}: it imports '{mod}', which isn't installed (standard library only).")
        # If nothing usable is left on the first draft, write the working template (run_build's last-resort
        # also drops in the full template if the model's version never passes).
        if not files and not ctx.get("failure"):
            log("  (model gave nothing usable — using the working template so you still get a result)")
            return dict(baseline_files)
        return files
    return propose
