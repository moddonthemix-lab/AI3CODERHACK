"""Builder's project workspace: the folders he may touch, safe edits, and the build-and-test loop.

Two guardrails live here, enforced in code (not by trusting the model):

1. **Scope** — Builder may only read or write inside the folders the boss listed in ``BUILDER_CODE_DIRS``
   (``Config.code_dirs``). Anything outside — the rest of the disk, system files — is refused with a
   ``ScopeError``. If no folders are set, Builder can touch nothing until the boss opts in.

2. **Reversible edits** — every file Builder writes is backed up first (the original content, or the fact
   that it didn't exist). ``undo()`` restores them, so "undo the fix" always works and the boss can see an
   exact diff of what changed before keeping it.

The loop itself (``build_and_test``) is model-agnostic: it takes a ``propose`` callback that returns the
files to write, writes them, runs the project's own tests, and on failure asks ``propose`` again with the
error output — bounded, so it can't loop forever. The CLI wires the family brain in as ``propose``.
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path


class ScopeError(PermissionError):
    """Raised when a path is outside the folders Builder is allowed to touch."""


def allowed_dirs(config) -> list[Path]:
    """The folders Builder may touch, as resolved absolute paths (from Config.code_dirs)."""
    out = []
    for d in getattr(config, "code_dirs", []) or []:
        try:
            out.append(Path(d).expanduser().resolve())
        except OSError:
            continue
    return out


def within_scope(path: str | Path, dirs: list[Path]) -> bool:
    """True only if ``path`` resolves to one of ``dirs`` or something inside it."""
    try:
        rp = Path(path).expanduser().resolve()
    except OSError:
        return False
    for d in dirs:
        if rp == d or d in rp.parents:
            return True
    return False


def check_scope(path: str | Path, dirs: list[Path]) -> Path:
    """Return the resolved path if it's in scope, else raise ScopeError. The one gate every write goes through."""
    rp = Path(path).expanduser().resolve()
    if not dirs:
        raise ScopeError("Builder has no allowed folders yet — set BUILDER_CODE_DIRS (e.g. ~/Projects) first.")
    if not within_scope(rp, dirs):
        allowed = ", ".join(str(d) for d in dirs)
        raise ScopeError(f"outside Builder's allowed folders ({allowed}): {rp}")
    return rp


class Project:
    """A single project directory Builder is working in. All reads/writes are scope-checked and reversible."""

    def __init__(self, root: str | Path, config):
        self.dirs = allowed_dirs(config)
        self.root = check_scope(root, self.dirs)
        if not self.root.exists():
            self.root.mkdir(parents=True, exist_ok=True)
        # relpath -> original content (str), or None if the file did not exist before Builder touched it.
        self._backups: dict[str, str | None] = {}

    def _abs(self, relpath: str | Path) -> Path:
        p = (self.root / relpath).resolve()
        check_scope(p, self.dirs)          # a relpath like "../../etc/passwd" is refused here
        return p

    def read(self, relpath: str | Path) -> str:
        p = self._abs(relpath)
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def write(self, relpath: str | Path, content: str) -> Path:
        """Write a file inside the project, backing up its previous state first (once) so undo works."""
        p = self._abs(relpath)
        key = str(p.relative_to(self.root))
        if key not in self._backups:
            self._backups[key] = p.read_text(encoding="utf-8") if p.exists() else None
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def write_all(self, files: dict[str, str]) -> list[str]:
        return [str(self.write(name, content)) for name, content in files.items()]

    def changed(self) -> list[str]:
        """Files Builder has written this session (relative paths)."""
        return sorted(self._backups)

    def diff(self) -> str:
        """A unified diff of everything Builder changed this session — what the boss reviews before keeping it."""
        chunks = []
        for key in sorted(self._backups):
            before = self._backups[key] or ""
            p = self.root / key
            after = p.read_text(encoding="utf-8") if p.exists() else ""
            if before == after:
                continue
            chunks.append("".join(difflib.unified_diff(
                before.splitlines(keepends=True), after.splitlines(keepends=True),
                fromfile=f"a/{key}", tofile=f"b/{key}")))
        return "".join(chunks)

    def undo(self) -> list[str]:
        """Restore every file Builder changed this session to its original state. Returns the restored paths."""
        restored = []
        for key, original in self._backups.items():
            p = self.root / key
            if original is None:               # Builder created it — remove it again
                if p.exists():
                    p.unlink()
            else:
                p.write_text(original, encoding="utf-8")
            restored.append(key)
        self._backups.clear()
        return restored


def save_backup(project: Project, path: str | Path) -> None:
    """Persist this session's backups so a later `builder build --undo` can restore the project."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"root": str(project.root), "backups": project._backups}, indent=2),
                    encoding="utf-8")


def restore_backup(path: str | Path, config) -> list[str]:
    """Undo a saved build: restore each backed-up file (and delete files Builder created). Scope-checked."""
    rec = json.loads(Path(path).read_text(encoding="utf-8"))
    dirs = allowed_dirs(config)
    root = check_scope(rec["root"], dirs)
    restored = []
    for key, original in rec.get("backups", {}).items():
        p = check_scope(root / key, dirs)
        if original is None:
            if p.exists():
                p.unlink()
        else:
            p.write_text(original, encoding="utf-8")
        restored.append(key)
    return restored


def build_and_test(project: Project, propose, test_cmd=None, run=None, max_tries: int = 3, log=print) -> dict:
    """Write files, run the project's tests, and on failure fix and retry — bounded by ``max_tries``.

    ``propose(context) -> dict[relpath, content]`` returns the files to write. ``context`` is
    ``{"attempt": n, "failure": <last test output or None>, "diff": <current diff>}``. The first call has
    ``failure=None`` (write it); later calls carry the failing output (fix it). ``run(root, test_cmd) ->
    result dict`` runs the tests (defaults to sandbox.run_tests). Returns the final result with the diff.
    """
    from . import sandbox
    run = run or sandbox.run_tests
    result = {"ok": False, "attempts": 0, "test": None, "diff": "", "failures": []}
    failure, failures = None, []
    for attempt in range(1, max_tries + 1):
        current = {name: project.read(name) for name in project.changed()}   # what he wrote so far
        files = propose({"attempt": attempt, "failure": failure, "diff": project.diff(), "files": current})
        if files:
            project.write_all(files)
        test = run(project.root, test_cmd)
        result = {"ok": bool(test.get("ok")), "attempts": attempt, "test": test,
                  "diff": project.diff(), "failures": failures}
        if result["ok"]:
            log(f"  tests pass on attempt {attempt}.")
            return result
        failure = (test.get("stderr") or "") + "\n" + (test.get("stdout") or "")
        failures.append(failure.strip())
        log(f"  tests failed on attempt {attempt}; reading the error and trying a fix…")
    return result


def problem_signature(failures: list[str]) -> str:
    """A short, memorable summary of the first real error hit during a build (its exception/message line)."""
    import re
    for text in failures:
        for line in text.splitlines():
            m = re.search(r"\b([A-Za-z_]+(?:Error|Exception|Warning)|FAILED|AssertionError)\b.*", line)
            if m:
                return m.group(0).strip()[:200]
    return (failures[0].strip().splitlines()[0][:200] if failures and failures[0].strip() else "")
