"""Builder's controlled run environments.

Two modes live here:

- ``run_python`` — locks a single snippet down hard (no network, no subprocesses, writes only in its own
  fresh folder). Good for a quick, throwaway script.
- ``run_tests`` — runs a real project's OWN test suite in its folder, so Builder can write code, run the
  tests, read the failures and fix them. Network is off by default; CPU/memory/time limits still apply.
  The caller (``projects.Project``) guarantees the folder is inside Builder's allowed code dirs.

``run_python`` runs it in a separate process with:

- its own empty working folder (``~/.builder/analysis/run-N``) holding copies of the input tables
- no network: socket connections are blocked by an audit hook (and on macOS also by
  ``sandbox-exec``; on Linux by a fresh network namespace when ``unshare`` allows it)
- no subprocesses, no writing outside the working folder
- CPU-time, memory and file-size limits, and a wall-clock timeout
- ``python -I`` (isolated mode: ignores environment variables and user site-packages)

This is a guard-rail for model-written code, not a security boundary against a
determined attacker — run Scholar as your normal user, never as root.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

GUARD = r'''
import builtins, os, sys
_WORK = os.path.realpath(os.getcwd())
_PY = os.path.realpath(sys.prefix)

def _inside(p):
    try:
        rp = os.path.realpath(os.fspath(p))
    except TypeError:
        return True  # file descriptors
    return rp == _WORK or rp.startswith(_WORK + os.sep)

def _hook(event, args):
    if event in ("socket.connect", "socket.bind", "socket.getaddrinfo", "socket.sendto"):
        raise PermissionError("network access is disabled in Scholar's sandbox")
    if event in ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork",
                 "os.forkpty", "pty.spawn"):
        raise PermissionError("starting other programs is disabled in Scholar's sandbox")
    if event == "open":
        path, mode = args[0], (args[1] or "r")
        if isinstance(mode, str) and any(c in mode for c in "wax+") and not _inside(path):
            raise PermissionError(f"writing outside the sandbox folder is not allowed: {path}")
    if event in ("os.remove", "os.rename", "os.rmdir", "os.mkdir", "shutil.rmtree", "os.chmod", "os.symlink"):
        if not _inside(args[0]):
            raise PermissionError(f"changing files outside the sandbox folder is not allowed: {args[0]}")

sys.addaudithook(_hook)
try:
    import matplotlib
    matplotlib.use("Agg")
except Exception:
    pass
'''

MAC_PROFILE = """(version 1)
(allow default)
(deny network*)
(deny file-write* (require-not (subpath "{work}")))
(allow file-write* (subpath "/private/var/folders") (subpath "/dev"))
"""


def _limits(cpu_seconds: int, mem_mb: int):
    def apply():  # runs in the child before exec
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
            resource.setrlimit(resource.RLIMIT_FSIZE, (200 * 2**20, 200 * 2**20))
            if sys.platform != "darwin":  # macOS ignores/rejects RLIMIT_AS
                resource.setrlimit(resource.RLIMIT_AS, (mem_mb * 2**20, mem_mb * 2**20))
        except (ImportError, ValueError, OSError):
            pass
    return apply


def _wrapper(workdir: Path) -> list[str]:
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        prefix = ["sandbox-exec", "-p", MAC_PROFILE.format(work=str(workdir.resolve()))]
        # sandbox-exec is deprecated and errors on recent macOS; only use it if it can actually run a
        # process, so a broken sandbox never makes correct code look like a failing test.
        try:
            probe = subprocess.run([*prefix, sys.executable, "-c", "pass"], cwd=str(workdir),
                                   capture_output=True, timeout=20)
            if probe.returncode == 0:
                return prefix
        except (OSError, subprocess.SubprocessError):
            pass
        return []  # fall back to a normal run (network isolation is best-effort; the file-scope gate still holds)
    if sys.platform.startswith("linux") and shutil.which("unshare"):
        probe = subprocess.run(["unshare", "-rn", "true"], capture_output=True)
        if probe.returncode == 0:
            return ["unshare", "-rn"]
    return []


def next_run_dir(base: Path) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    n = 1 + max((int(p.name.split("-")[1]) for p in base.glob("run-*") if p.name.split("-")[1].isdigit()), default=0)
    d = base / f"run-{n}"
    d.mkdir()
    return d


def run_python(code: str, base_dir: Path, inputs: dict[str, str | Path] | None = None, timeout: int = 60,
               mem_mb: int = 2048) -> dict:
    """Run ``code`` in a fresh sandbox folder. ``inputs`` maps file names to files to copy in."""
    work = next_run_dir(base_dir)
    for name, src in (inputs or {}).items():
        shutil.copy(src, work / Path(name).name)
    (work / "analysis.py").write_text(textwrap.dedent(code), encoding="utf-8")
    (work / "_guard.py").write_text(GUARD, encoding="utf-8")
    before = {p.name for p in work.iterdir()}
    boot = "exec(open('_guard.py').read()); __name__='__main__'; exec(compile(open('analysis.py').read(), 'analysis.py', 'exec'))"
    cmd = [*_wrapper(work), sys.executable, "-I", "-c", boot]
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(work), "MPLBACKEND": "Agg",
           "PYTHONHASHSEED": "0", "OPENBLAS_NUM_THREADS": "2"}
    try:
        p = subprocess.run(cmd, cwd=work, env=env, capture_output=True, text=True, timeout=timeout,
                           # CPU limit a little above the wall-clock one, so the clock decides (no race)
                           preexec_fn=_limits(timeout + 5, mem_mb) if os.name == "posix" else None)
        ok, out, err, code_ = p.returncode == 0, p.stdout, p.stderr, p.returncode
        if code_ in (-24, -9) and "timed out" not in err:  # SIGXCPU / SIGKILL from a resource limit
            err += f"\nstopped: exceeded its time or memory limit ({timeout}s)"
    except subprocess.TimeoutExpired as e:
        ok, out, err, code_ = False, (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""), \
            f"timed out after {timeout}s", -1
    files = sorted(p.name for p in work.iterdir() if p.name not in before)
    result = {"ok": ok, "exit_code": code_, "stdout": out[-12000:], "stderr": err[-4000:],
              "files": [str(work / f) for f in files], "workdir": str(work)}
    (work / "result.json").write_text(json.dumps({k: v for k, v in result.items() if k != "stdout"}, indent=2))
    return result


def detect_test_command(project_dir: Path) -> list[str] | None:
    """Guess how to run a project's own tests from what's in its folder. Returns an argv list, or None.

    Builder runs only these known test commands — never an arbitrary shell string from the model.
    """
    project_dir = Path(project_dir)
    if (project_dir / "pytest.ini").exists() or (project_dir / "pyproject.toml").exists() \
            or (project_dir / "tests").is_dir() or list(project_dir.glob("test_*.py")) \
            or list(project_dir.glob("*_test.py")):
        return [sys.executable, "-m", "pytest", "-q"]
    pkg = project_dir / "package.json"
    if pkg.exists():
        try:
            if '"test"' in pkg.read_text(encoding="utf-8"):
                return ["npm", "test", "--silent"]
        except OSError:
            pass
    if (project_dir / "Makefile").exists():
        try:
            if "test:" in (project_dir / "Makefile").read_text(encoding="utf-8"):
                return ["make", "test"]
        except OSError:
            pass
    return None


# A tiny, dependency-free test runner used when pytest isn't installed. It imports every test_*.py in the
# project folder and runs each `test_*` function, so Builder's builds work out of the box without pytest.
BUILTIN_RUNNER = r'''
import importlib.util, os, sys, traceback
sys.path.insert(0, os.getcwd())
ran = failed = 0
for fn in sorted(os.listdir(".")):
    if not (fn.startswith("test_") and fn.endswith(".py")):
        continue
    spec = importlib.util.spec_from_file_location(fn[:-3], fn)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        print("ERROR importing", fn); traceback.print_exc(); failed += 1; continue
    for name in sorted(dir(mod)):
        obj = getattr(mod, name)
        if name.startswith("test_") and callable(obj):
            ran += 1
            try:
                obj(); print("PASS", fn + "::" + name)
            except Exception:
                print("FAILED", fn + "::" + name); traceback.print_exc(); failed += 1
if ran == 0:
    print("no tests ran"); sys.exit(5)
print("%d passed, %d failed" % (ran - failed, failed))
sys.exit(1 if failed else 0)
'''

_PYTEST_OK = None


def _pytest_available() -> bool:
    global _PYTEST_OK
    if _PYTEST_OK is None:
        import importlib.util
        _PYTEST_OK = importlib.util.find_spec("pytest") is not None
    return _PYTEST_OK


def run_tests(project_dir: Path, cmd: list[str] | None = None, timeout: int = 180, mem_mb: int = 4096,
              allow_network: bool = False) -> dict:
    """Run a project's OWN test suite inside its folder and report pass/fail with the error output.

    This is Builder's controlled run environment for real projects (apps, games, sites). Unlike
    ``run_python`` (which locks a single snippet down hard), running a test suite needs to start the test
    runner as a subprocess and read the whole project tree — so the guard-rails here are:

    - the caller passes a ``project_dir`` already checked to be inside Builder's allowed folders;
    - network is off by default (a fresh network namespace on Linux / ``sandbox-exec`` deny on macOS, when
      available) unless ``allow_network`` is set and the boss approved it;
    - CPU-time, file-size, memory and wall-clock limits apply.

    ``cmd`` is an argv list (never a shell string); when None it is auto-detected from the project.
    Returns ``{ok, exit_code, stdout, stderr, cmd, timed_out}``.
    """
    project_dir = Path(project_dir)
    cmd = cmd or detect_test_command(project_dir)
    if not cmd:
        return {"ok": False, "exit_code": None, "stdout": "", "cmd": None, "timed_out": False,
                "stderr": "No test command found (no pytest/tests, package.json test script, or Makefile test target)."}
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(project_dir),
           "PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1",
           # Let pygame-based tests import and run with no real display/audio (headless sandbox), and skip
           # its startup banner. Harmless for projects that don't use pygame.
           "SDL_VIDEODRIVER": "dummy", "SDL_AUDIODRIVER": "dummy", "PYGAME_HIDE_SUPPORT_PROMPT": "1"}

    # If the test command is pytest but pytest isn't installed, use the built-in dependency-free runner,
    # so builds don't fail just because the user hasn't `pip install pytest`.
    runner_tmp = None
    if "pytest" in cmd and not _pytest_available():
        import tempfile
        fd, runner_tmp = tempfile.mkstemp(suffix="_builder_runner.py")
        with os.fdopen(fd, "w") as f:
            f.write(BUILTIN_RUNNER)
        cmd = [sys.executable, runner_tmp]

    def _run(wrapper):
        full = [*wrapper, *cmd]
        try:
            p = subprocess.run(full, cwd=project_dir, env=env, capture_output=True, text=True, timeout=timeout,
                               preexec_fn=_limits(timeout + 5, mem_mb) if os.name == "posix" else None)
            return {"ok": p.returncode == 0, "exit_code": p.returncode, "stdout": p.stdout,
                    "stderr": p.stderr, "timed_out": False}
        except subprocess.TimeoutExpired as e:
            out = (e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""))
            return {"ok": False, "exit_code": None, "stdout": out, "stderr": f"tests timed out after {timeout}s",
                    "timed_out": True}
        except (FileNotFoundError, OSError) as e:
            return {"ok": False, "exit_code": None, "stdout": "",
                    "stderr": f"could not run the test command {cmd[0]!r}: {e}", "timed_out": True}

    try:
        wrapper = [] if allow_network else _wrapper(project_dir)
        r = _run(wrapper)
        # If the isolation wrapper was used and the run failed WITHOUT the test runner actually producing any
        # output (e.g. a broken/denied macOS sandbox-exec), re-run once unwrapped so a bad sandbox never masks
        # working code. A real test failure prints a report and is left as-is.
        if wrapper and not r["ok"] and not _looks_like_test_output(r["stdout"] + r["stderr"]):
            r = _run([])
    finally:
        if runner_tmp:
            try:
                os.unlink(runner_tmp)
            except OSError:
                pass
    return {"ok": r["ok"], "exit_code": r["exit_code"], "stdout": r["stdout"][-12000:],
            "stderr": r["stderr"][-6000:], "cmd": cmd, "timed_out": r["timed_out"]}


def _looks_like_test_output(text: str) -> bool:
    """True if the output is clearly from the test runner actually running (not a sandbox/exec error)."""
    t = text.lower()
    return any(m in t for m in ("passed", "failed", "error", "collected", "test session",
                                "no tests ran", "assert", " pass ", "ok"))
