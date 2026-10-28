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


# A variant contract for pygame games: pygame IS allowed (it's installed), but the window must stay out of
# import time and out of the tests, so the headless sandbox can still run them.
PYGAME_FILE_CONTRACT = """

You are writing a small, self-contained PYTHON GAME using pygame. Reply with:
1. A short plain-language note: what you built and how its test checks it.
2. On the LAST line, exactly one JSON object mapping each file path to its full contents:
   FILES_JSON: {"game.py": "....", "test_game.py": "...."}

Rules:
- Use the Python standard library and PYGAME only. Do NOT import any other third-party package (numpy,
  pymunk, requests, …) — they are not installed and cannot be installed.
- Put the real game LOGIC in plain functions (e.g. move(pos, dx, dy), collides(a, b), spawn(...)) so the
  test can call them directly. Open the window and run the loop ONLY inside main(), guarded by
  `if __name__ == "__main__":`.
- Importing game.py must NOT open a window: do NOT call pygame.init(), pygame.display.set_mode(), load
  images/sounds, or start the loop at module top level. Put `os.environ.setdefault(
  "PYGAME_HIDE_SUPPORT_PROMPT", "1")` before `import pygame`.
- The pytest test imports the LOGIC functions and checks them with plain asserts and CONCRETE values. It
  must NOT open a window, call main(), init the display, or load assets.
- Fully implement every function — no `pass`, `...`, `TODO` or empty bodies. Give the COMPLETE contents of
  every file. Use relative paths only."""


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
    "pygame": (
        "\n\nThis is a PYTHON GAME using pygame (pygame IS installed) — make it actually PLAYABLE:\n"
        "- game.py: the game. Put the LOGIC in plain functions (move, collides, spawn, step). Open the\n"
        "  window and run the loop ONLY inside main(), guarded by `if __name__ == \"__main__\":`.\n"
        "- test_game.py: import those logic functions and assert on them. It must NOT open a window, call\n"
        "  main(), init the display, or load images/sounds (the sandbox is headless).\n"
        "- Keep it to game.py + test_game.py."),
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


def pygame_available() -> bool:
    """True only when pygame is actually installed, so Builder offers it only when it can really run it."""
    import importlib.util
    return importlib.util.find_spec("pygame") is not None


def _wants_pygame(t: str) -> bool:
    """A real Python game: the request says 'pygame', or 'python' together with a game word."""
    has_game_word = any(w in t for w in _KIND_WORDS["game"])
    return ("pygame" in t) or ("python" in t and has_game_word)


def classify(task: str) -> str:
    """Guess the kind of project from a plain request, so Builder adds the right build recipe himself."""
    t = " " + task.lower() + " "
    # A Python game with pygame — but only when pygame is installed; otherwise a game stays HTML/JS (which
    # always runs offline with no install), so builds never depend on a package that isn't there.
    if _wants_pygame(t) and pygame_available():
        return "pygame"
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
    if memory is not None:
        # Pull in relevant passages from the boss's uploaded reference library (coding books, notes), so
        # Builder's code is informed by what he's been taught. Offline, keyword-based.
        ref_note = memory.reference_note(task, max_chars=1800)
        if ref_note:
            note = (note + "\n\n" + ref_note) if note else ref_note
            log("  (using relevant passages from your reference library)")

    # If the folder ALREADY has a project in it, Builder MODIFIES it: its current files become the starting
    # point, so "make the ball faster" / "add a second paddle" adapts the real code instead of starting over.
    kind = classify(task)
    is_modify = False
    starter = None
    try:
        existing = project.existing_files()
    except Exception:  # noqa: BLE001
        existing = {}
    if existing:
        is_modify = True
        log("  (this folder already has a project — changing what's there instead of starting fresh)")
        starter = {"task": f"the existing project in this folder: {task}", "files": existing}
    elif memory is not None and kind != "game":
        # For a GAME the specific built-in template (pong/snake/breakout) is the authoritative starting
        # point, so we DON'T pull a remembered past build in — that's what used to drag every "pong" back
        # to an old generic game. For everything else, reuse a close past solution when there is one.
        try:
            starter = memory.recall_solution(task)
        except Exception:  # noqa: BLE001
            starter = None

    def changed_nothing() -> bool:
        return not (project.diff() or "").strip()

    # For an EDIT, following the instruction matters more than speed, so use the STRONGER model first when
    # we have one (the fast model is the one that keeps "changing nothing"). A fresh build keeps the fast
    # model first with the stronger one as the fallback.
    primary, secondary = llm, fallback_llm
    if is_modify and fallback_llm is not None:
        try:
            if fallback_llm.available():
                primary, secondary = fallback_llm, llm
                log("  (edit — using the stronger model first so the change actually gets applied)")
        except Exception:  # noqa: BLE001
            pass

    propose = make_propose(_generator(primary), task, log=log, memory_note=note, starter=starter,
                           is_modify=is_modify)
    result = build_and_test(project, propose, test_cmd=test_cmd, max_tries=tries, log=log)

    # If the primary couldn't do it (or, for an edit, changed nothing), try the OTHER model.
    no_change = is_modify and changed_nothing()
    if (not result["ok"] or no_change) and secondary is not None:
        try:
            available = secondary.available()
        except Exception:  # noqa: BLE001
            available = False
        if available:
            log("  that didn't apply the change — trying the other model…"
                if no_change else
                "  that model couldn't finish it — trying the other model…")
            current = {name: project.read(name) for name in project.changed()}
            files2 = current or (starter or {}).get("files", {})
            starter2 = {"task": (starter or {}).get("task", task), "files": files2} if files2 else starter
            propose2 = make_propose(_generator(secondary), task, log=log, memory_note=note,
                                    starter=starter2, is_modify=is_modify)
            result = build_and_test(project, propose2, test_cmd=test_cmd, max_tries=tries, log=log)
            no_change = is_modify and changed_nothing()

    if is_modify:
        # NEVER drop a template over the boss's existing project. If the model still changed nothing, say so
        # honestly instead of reporting a success with "(no changes)".
        if no_change:
            log("  couldn't get either model to apply that change — leaving your project untouched. "
                "Try rephrasing it more concretely (e.g. name the file/colour/number to change).")
            result = {"ok": False, "attempts": result.get("attempts", tries), "test": result.get("test"),
                      "diff": "", "failures": result.get("failures", []), "no_change": True}
    elif not result["ok"]:
        # Fresh build last-resort: drop in the clean working template for this kind (or the reused past
        # solution) so a build ALWAYS leaves a runnable result.
        from . import sandbox
        if starter and starter.get("files"):
            baseline = starter["files"]
        elif kind == "game":
            baseline = template_files(game_template_for(task))   # the SPECIFIC game, not a generic one
        else:
            baseline = template_files(kind)
        if baseline:
            log("  couldn't get the model's version to pass — using a clean working template so you still "
                "get something that runs.")
            project.write_all(baseline)
            test = sandbox.run_tests(project.root, test_cmd)
            result = {"ok": bool(test.get("ok")), "attempts": result.get("attempts", tries), "test": test,
                      "diff": project.diff(), "failures": result.get("failures", []), "used_template": True}

    if memory is not None:
        try:
            # Only remember the FILES as a reusable solution when the model actually produced a passing
            # project — never when we fell back to the built-in template. Saving the template as a
            # "solution" is what poisoned future builds (every pong recalled an old generic game).
            save_files = result["ok"] and not result.get("used_template")
            files = {name: project.read(name) for name in project.changed()} if save_files else None
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


def make_propose(generate, task: str, log=print, memory_note: str = "", starter: dict | None = None,
                 is_modify: bool = False):
    """Build a ``propose(ctx)`` for ``build_and_test`` from a ``generate(prompt) -> text`` function.

    ``generate`` is anything that answers a prompt (the local Ollama coder model, or the family brain).
    The first call writes the project; later calls carry the failing test output so the model can fix it.
    ``memory_note`` (what Builder remembers — the boss's stack, past fixes, lessons) is included so his
    work benefits from memory even offline. ``starter`` is the working files from a similar past build, so
    he ADAPTS proven code instead of starting from scratch — faster and more reliable. ``is_modify`` means
    the ``starter`` files are the boss's EXISTING project and the task is a change to make to them, so the
    prompt tells the model to edit those files and return them changed (never unchanged).
    """
    note = (f"\n\nWhat you remember (apply it):\n{memory_note}\n" if memory_note else "")
    recipe = recipe_for(task)          # Builder works out the right project shape from the plain request
    kind = classify(task)
    base_contract = PYGAME_FILE_CONTRACT if kind == "pygame" else FILE_CONTRACT
    contract = base_contract + recipe
    if kind != "default":
        log(f"  (building this as a {kind} — making it "
            f"{'playable' if kind in ('game', 'pygame') else 'runnable'} and tested)")
    # Every build starts from a WORKING template (a past solution if there's a close one, else the built-in
    # template for this kind). It's kept as the guaranteed baseline, so a build can't come out empty/broken.
    if starter and starter.get("files"):
        baseline_files, baseline_task = dict(starter["files"]), starter.get("task", "")
    elif kind == "game":
        # Start from the SPECIFIC game the request names (pong/breakout/snake), not a generic starter,
        # so "a pong game" adapts real Pong instead of the snake-ish default.
        gkind = game_template_for(task)
        baseline_files, baseline_task = template_files(gkind), f"a working {gkind} game"
    elif kind == "pygame":
        baseline_files, baseline_task = template_files("pygame"), "a working pygame game"
    else:
        baseline_files, baseline_task = template_files(kind), f"a working {kind} starter"
    files_txt = "".join(f"\n=== {n} ===\n{c}\n" for n, c in baseline_files.items())
    if is_modify:
        starter_block = (
            f"\n\nThese are the EXISTING project files. Your job is to CHANGE them to do this: {task!r}.\n"
            f"Return the COMPLETE updated files with the change actually applied — edit the real code "
            f"(paddle colours, speeds, levels, text, whatever the change needs). Do NOT return them "
            f"unchanged, and do NOT just describe the change — make it in the code:\n{files_txt[:8000]}")
    else:
        starter_block = (f"\n\nHere is a WORKING starting point ({baseline_task!r}) that already runs and passes "
                         f"its test — ADAPT it to the task, keeping what works (don't start from scratch):\n{files_txt[:8000]}")
    log("  (changing your existing project: " + ", ".join(list(baseline_files)[:4]) + ")" if is_modify
        else f"  (starting from {baseline_task or 'a working template'} and adapting it)")

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
            log("  asking the coder model to make the change…" if is_modify
                else "  asking the coder model to write the project…")
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
            if is_modify:
                log("  (model returned no file changes this attempt)")
            else:
                log("  (model gave nothing usable — using the working template so you still get a result)")
            return dict(baseline_files)
        return files
    return propose
