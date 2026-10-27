"""Turn the coder model into the build-and-test loop's ``propose`` callback.

Builder asks the model for the whole set of files to write, as one JSON object, and runs them through the
project workspace and the test sandbox. On a test failure the same contract is used again, now with the
error output, so the model fixes its own code. ``files_from_reply`` is the tolerant parser; it's kept
separate so it can be tested without a running model.
"""

from __future__ import annotations

import json
import re

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
  tested part."""


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


def make_propose(generate, task: str, log=print, memory_note: str = ""):
    """Build a ``propose(ctx)`` for ``build_and_test`` from a ``generate(prompt) -> text`` function.

    ``generate`` is anything that answers a prompt (the local Ollama coder model, or the family brain).
    The first call writes the project; later calls carry the failing test output so the model can fix it.
    ``memory_note`` (what Builder remembers — the boss's stack, past fixes, lessons) is included so his
    work benefits from memory even offline.
    """
    note = (f"\n\nWhat you remember (apply it):\n{memory_note}\n" if memory_note else "")

    def propose(ctx: dict) -> dict[str, str]:
        if ctx.get("failure"):
            prompt = (f"Task: {task}{note}\n\nYour code's tests are FAILING. Here is the output:\n\n"
                      f"{ctx['failure'][:6000]}\n\nFix the code (and tests if wrong) and return the full files."
                      + FILE_CONTRACT)
            log(f"  asking the coder model to fix attempt {ctx['attempt'] - 1}'s failures…")
        else:
            prompt = f"Task: {task}{note}" + FILE_CONTRACT
            log("  asking the coder model to write the project…")
        summary, files = files_from_reply(generate(prompt))
        if summary:
            log(f"  model: {summary.splitlines()[0][:100]}")
        return files
    return propose
