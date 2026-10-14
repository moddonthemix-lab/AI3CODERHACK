"""A controlled Python environment for quantitative analysis.

Scholar writes analysis code (statistics, financial models, simulations, data
cleaning, charts); this runs it in a separate process with:

- its own empty working folder (``~/.scholar/analysis/run-N``) holding copies of the input tables
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
        return ["sandbox-exec", "-p", MAC_PROFILE.format(work=str(workdir.resolve()))]
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
