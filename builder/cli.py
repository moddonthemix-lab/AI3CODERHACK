"""Command line for Builder: build and test projects, review code defensively, and run the family API."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import sandbox
from .assistant import Assistant
from .config import Config, load_env
from .db import LESSON_KINDS, Store
from .llm import LLMError, Ollama
from .memory import Memory


def _print_rows(rows: list[dict], cols: list[str], width: int = 70) -> None:
    if not rows:
        print("(none)")
        return

    def cell(r, c):
        v = r.get(c)
        v = "" if v is None else str(v)
        return v if len(v) <= width else v[:width - 1] + "…"

    widths = {c: max(len(c), *(len(cell(r, c)) for r in rows)) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    for r in rows:
        print("  ".join(cell(r, c).ljust(widths[c]) for c in cols))


_BUILD_SYSTEM = (
    "You are Builder, a careful, skilled coder. You write small, correct, self-contained projects and the "
    "tests that prove they work. You explain briefly, then give the complete files. You never touch the "
    "network or files outside the project. If something is unsafe or unclear, you say so instead of guessing."
)

_REVIEW_SYSTEM = (
    "You are Builder doing a DEFENSIVE security review of code the owner wrote and wants hardened. You point "
    "out real, specific weaknesses and give a concrete fix for each. You never explain how to attack anything "
    "and never write exploit code — only how to defend. If the code looks fine, you say so."
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="builder",
        description="Builder — the coder & security engineer of your AI family. Builds and tests things, "
                    "and defends your own systems. Runs locally on Ollama.",
        epilog="First: set BUILDER_CODE_DIRS to the folders Builder may touch (e.g. ~/Projects).\n"
               "Try:   builder doctor   ·   builder build \"a tip calculator with tests\" --dir ~/Projects/tip",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("build", help="Build a small project: write it, run its tests, fix failures, show the diff")
    s.add_argument("task", nargs="*", help="what to build, e.g. \"a tip calculator with tests\"")
    s.add_argument("--dir", required=True, help="project folder (must be inside BUILDER_CODE_DIRS)")
    s.add_argument("--test", help="test command, e.g. \"pytest -q\" (auto-detected if omitted)")
    s.add_argument("--tries", type=int, default=3, help="how many fix-and-retry rounds (default 3)")
    s.add_argument("--undo", action="store_true", help="undo Builder's last build in --dir and exit")

    s = sub.add_parser("review", help="Defensive security review of your own code/config (findings + fixes)")
    s.add_argument("path", help="a file or folder inside BUILDER_CODE_DIRS")
    s.add_argument("--no-model", action="store_true", help="pattern scan only (skip the local model pass)")
    s.add_argument("--no-save", action="store_true", help="don't record the findings in Builder's memory")

    lab = sub.add_parser("lab", help="Authorized-lab mode: practice security ONLY on targets you confirm are yours")
    labsub = lab.add_subparsers(dest="action", required=True)
    labsub.add_parser("status", help="show whether lab mode is on and which targets are authorized")
    on = labsub.add_parser("on", help="turn it on for a target you own / a practice target")
    on.add_argument("target", help="the target (a CTF name, a local VM address, a practice app)")
    on.add_argument("--kind", choices=["ctf", "vm", "local-app", "file", "lab"], default="lab")
    on.add_argument("--note", default="")
    on.add_argument("--i-own-this", action="store_true",
                    help="confirm you own this target or it's made for practice (required to turn lab mode on)")
    labsub.add_parser("off", help="turn it off and clear all authorizations")
    lg = labsub.add_parser("log", help="show the security audit log (what Builder allowed/refused)")
    lg.add_argument("--limit", type=int, default=20)

    sub.add_parser("doctor", help="Check Ollama, the coder models, the sandbox and the code-folder scope")

    s = sub.add_parser("preview", help="Open a built website/folder in your browser")
    s.add_argument("path", help="a file or folder inside BUILDER_CODE_DIRS")

    s = sub.add_parser("reviews", help="Security findings Builder has recorded")
    s.add_argument("query", nargs="*")
    s.add_argument("--severity", choices=["high", "med", "low"])

    s = sub.add_parser("builds", help="Projects Builder has built and the problems he solved")
    s.add_argument("query", nargs="*")

    sub.add_parser("chat", help="Talk to Builder in the terminal")
    v = sub.add_parser("voice", help="Talk to Builder out loud (press Enter, then speak)")
    v.add_argument("--hands-free", action="store_true", help="listen continuously, no Enter needed")
    v = sub.add_parser("say", help="Hear Builder read something out loud")
    v.add_argument("text", nargs="+")

    s = sub.add_parser("feedback", help="Teach Builder: a preference, correction or strategy")
    s.add_argument("text", nargs="+")
    s.add_argument("--kind", choices=LESSON_KINDS, default="preference")
    sub.add_parser("lessons", help="What Builder has learned from your feedback")

    s = sub.add_parser("remember", help="Save a note about the boss (stack, preferences)")
    s.add_argument("text", nargs="+")
    s.add_argument("--topic", default="general")
    s = sub.add_parser("recall", help="Search Builder's notes about the boss")
    s.add_argument("query", nargs="*")
    s = sub.add_parser("forget", help="Delete a note by id")
    s.add_argument("id", type=int)

    sub.add_parser("pending", help="Work parked while offline")
    sub.add_parser("card", help="Builder's identity card and store stats")

    s = sub.add_parser("serve", help="Start the HTTP API + dashboard")
    s.add_argument("--host")
    s.add_argument("--port", type=int)

    s = sub.add_parser("hub", help="Join the Family Hub forum and work on tasks")
    s.add_argument("--check", action="store_true", help="just check the connection and cloud status, then exit")
    s.add_argument("--once", action="store_true", help="process the inbox once and exit (don't wait for events)")

    s = sub.add_parser("bench", help="Measure coder models on this machine and suggest the best one")
    s.add_argument("--models", help="comma-separated model names (default: your configured thinker + reader)")
    return p


def chat_loop(assistant: Assistant) -> None:
    name = assistant.config.assistant_name
    print(f"{name} is at his desk. /reset clears the conversation, /quit exits.")
    while True:
        try:
            text = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not text:
            continue
        if text in ("/quit", "/exit"):
            return
        if text == "/reset":
            assistant.reset()
            print("(conversation cleared)")
            continue
        try:
            print(f"\n{name.lower()}> {assistant.ask(text)}")
        except LLMError as e:
            print(f"error: {e}", file=sys.stderr)


def voice_loop(assistant: Assistant, config: Config, hands_free: bool) -> None:
    from .voice import Voice, VoiceUnavailable

    voice = Voice(config.whisper_model, config.piper_voice, config.say_voice)
    name = config.assistant_name
    print(f"{name} voice mode ({voice.engine} voice). {'Just talk.' if hands_free else 'Press Enter, then speak.'} "
          "Ctrl+C to quit.")
    try:
        while True:
            if not hands_free:
                input("\n[Enter to talk] ")
            print("listening…")
            text = voice.listen()
            if not text:
                continue
            print(f"you> {text}")
            try:
                reply = assistant.ask(text)
            except LLMError as e:
                print(f"error: {e}", file=sys.stderr)
                continue
            print(f"{name.lower()}> {reply}")
            voice.say(reply)
    except VoiceUnavailable as e:
        print(e, file=sys.stderr)
        sys.exit(1)
    except (EOFError, KeyboardInterrupt):
        print()


def _cmd_build(args, config, llm, memory=None) -> int:
    """`builder build`: write a small project, run its tests, fix failures, show the diff. All inside scope."""
    from .buildagent import make_propose
    from .projects import (Project, ScopeError, build_and_test, problem_signature,
                           restore_backup, save_backup)

    backup_path = config.home / "backups" / "last_build.json"
    err = lambda m: print(m, file=sys.stderr)  # noqa: E731

    if args.undo:
        try:
            restored = restore_backup(backup_path, config)
        except FileNotFoundError:
            err("Nothing to undo — no saved build found.")
            return 1
        except ScopeError as e:
            err(f"✗ {e}")
            return 1
        print(f"Undone. Restored {len(restored)} file(s): {', '.join(restored) or '(none)'}")
        return 0

    task = " ".join(args.task).strip()
    if not task:
        err('Tell me what to build, e.g. builder build "a tip calculator with tests" --dir ~/Projects/tip')
        return 1
    try:
        proj = Project(args.dir, config)
    except ScopeError as e:
        err(f"✗ {e}\n  Set BUILDER_CODE_DIRS to the folders Builder may touch (e.g. ~/Projects).")
        return 1
    if not llm.available():
        err("✗ The local coder model isn't reachable. Is Ollama running? (try: builder bench)")
        return 1

    def generate(prompt: str) -> str:
        return llm.chat([{"role": "system", "content": _BUILD_SYSTEM},
                         {"role": "user", "content": prompt}]).get("content", "")

    memory_note = ""
    if memory is not None:
        try:
            memory_note = memory.as_prompt(memory.recall(task), max_chars=1500)
        except Exception:  # noqa: BLE001 — memory is a help, never a blocker
            memory_note = ""
    if memory_note:
        err("  (using what I remember about your stack and past fixes)")

    test_cmd = args.test.split() if args.test else None
    propose = make_propose(generate, task, log=err, memory_note=memory_note)
    result = build_and_test(proj, propose, test_cmd=test_cmd, max_tries=args.tries, log=err)
    save_backup(proj, backup_path)

    # Remember this build — and the problem he hit — so he learns from his projects over time.
    if memory is not None:
        try:
            problem = problem_signature(result.get("failures", []))
            memory.remember_build(task, str(proj.root), result["ok"], result["attempts"], problem)
        except Exception:  # noqa: BLE001 — recording must never fail a build
            pass

    print(result["diff"] or "(no changes written)")
    t = result["test"] or {}
    err(f"\n{'✓ tests pass' if result['ok'] else '✗ tests still failing'} "
        f"after {result['attempts']} attempt(s) in {proj.root}")
    if not result["ok"] and (t.get("stderr") or t.get("stdout")):
        err((t.get("stderr") or t.get("stdout"))[-1500:])
    err(f"Undo with:  builder build --undo --dir {args.dir}")
    return 0 if result["ok"] else 1


def _cmd_review(args, config, db, memory, llm) -> int:
    """`builder review`: defensive audit of your OWN code/config — findings with severity + fix. Offline-first."""
    from . import audit
    from .projects import ScopeError

    err = lambda m: print(m, file=sys.stderr)  # noqa: E731
    path = args.path

    if memory is not None:
        try:
            note = memory.as_prompt(memory.recall(f"security review {Path(path).name}"), max_chars=900)
        except Exception:  # noqa: BLE001
            note = ""
        if note:
            print("What I remember:\n" + note + "\n")

    generate = None
    if not args.no_model and llm.available():
        err("  (also asking the local coder model for a context-aware pass)")

        def generate(prompt: str) -> str:  # noqa: F811
            return llm.chat([{"role": "system", "content": _REVIEW_SYSTEM},
                             {"role": "user", "content": prompt}]).get("content", "")

    try:
        findings = audit.review(path, config, generate=generate, use_model=generate is not None)
    except ScopeError as e:
        err(f"✗ {e}\n  Set BUILDER_CODE_DIRS to the folders Builder may review (e.g. ~/Projects).")
        return 1

    if not findings:
        print(f"✓ No defensive issues found in {path}. (Heuristics — still review by hand for logic flaws.)")
        return 0

    mark = {"high": "🔴 HIGH", "med": "🟠 MED ", "low": "🟡 LOW "}
    print(f"{len(findings)} issue(s) in {path}, most serious first:\n")
    for f in findings:
        where = f"{f.file}:{f.line}" if f.line else f.file
        print(f"{mark.get(f.severity, f.severity)}  {f.issue}" + (f"  ({where})" if where else ""))
        if f.fix:
            print(f"        fix: {f.fix}")
    if not args.no_save:
        saved = 0
        for f in findings:
            try:
                if db.review_exists(f.issue):
                    continue
                db.add_review(f.issue, f.severity, f.fix, topic=(Path(path).name or "security")[:80])
                saved += 1
            except Exception:  # noqa: BLE001
                pass
        if saved:
            err(f"\n(remembered {saved} new finding(s) in my local store)")
    return 1  # non-zero so this can gate a pre-commit hook or CI


def _cmd_lab(args, config, db) -> int:
    """`builder lab`: turn authorized-lab mode on/off, see authorized targets, and read the audit log."""
    from .safety import ApprovalRequired, Safety, SafetyError

    err = lambda m: print(m, file=sys.stderr)  # noqa: E731
    s = Safety(db, config)
    action = getattr(args, "action", "status")

    if action == "status":
        st = s.status()
        print("Authorized-lab mode: " + ("ON" if st["lab_enabled"] else "OFF (defensive mode only)"))
        if st["targets"]:
            print("Authorized targets:")
            for t in st["targets"]:
                print(f"  - {t['target']} ({t['kind']})" + (f" — {t['note']}" if t["note"] else ""))
        else:
            print("No authorized targets. Builder will only help defensively until you add one.")
        return 0
    if action == "on":
        try:
            rec = s.enable_lab(args.target, args.kind, args.note, confirmed=args.i_own_this)
        except ApprovalRequired as e:
            err(f"✗ {e}\n  Re-run with --i-own-this to confirm you own this target or it's for practice.")
            return 1
        except SafetyError as e:
            err(f"✗ {e}")
            return 1
        print(f"✓ Authorized-lab mode ON for {rec['target']} ({rec['kind']}).")
        print("  Builder will still ask before anything destructive, network-reaching, or installing software.")
        print("  Turn it off any time with:  builder lab off")
        return 0
    if action == "off":
        s.disable_lab()
        print("✓ Authorized-lab mode OFF; all authorizations cleared. Back to defensive mode only.")
        return 0
    if action == "log":
        _print_rows(db.safety_events(args.limit), ["at", "action", "decision", "target", "detail", "reason"], width=40)
        return 0
    return 0


def _installed_models(url: str) -> list[str]:
    """Models Ollama has pulled (from /api/tags). Empty list if Ollama can't be reached."""
    import json as _json
    import urllib.request
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=4) as r:
            return [m.get("name", "") for m in _json.loads(r.read()).get("models", [])]
    except Exception:  # noqa: BLE001
        return []


def _cmd_doctor(config, llm) -> int:
    """`builder doctor`: check Ollama, the coder models, the sandbox and the code-folder scope."""
    from .safety import Safety

    ok = True

    def line(good, label, detail=""):
        nonlocal ok
        ok = ok and good
        print(f"  {'✓' if good else '✗'} {label}" + (f" — {detail}" if detail else ""))

    print("Builder doctor:\n")
    installed = _installed_models(config.ollama_url)
    reachable = llm.available()
    line(reachable, "Ollama is running", config.ollama_url if reachable else f"not reachable at {config.ollama_url}")
    for role, m in (("thinker", config.model), ("reader", config.fast_model)):
        have = any(name == m for name in installed)
        line(have, f"{role} model {m}",
             "installed" if have else (f"not pulled — run: ollama pull {m}" if reachable else "can't check (Ollama down)"))
    try:
        import tempfile
        r = sandbox.run_python("print('sandbox-ok')", Path(tempfile.mkdtemp()))
        line(r["ok"] and "sandbox-ok" in r["stdout"], "sandbox runs code", "no network, writes only in its own folder")
    except Exception as e:  # noqa: BLE001
        line(False, "sandbox runs code", str(e))
    dirs = config.code_dirs
    if not dirs:
        line(False, "allowed code folders (BUILDER_CODE_DIRS)",
             "not set — Builder can touch nothing until you set it (e.g. export BUILDER_CODE_DIRS=~/Projects)")
    else:
        missing = [d for d in dirs if not Path(d).expanduser().exists()]
        line(not missing, "allowed code folders (BUILDER_CODE_DIRS)",
             ", ".join(dirs) if not missing else f"these don't exist: {', '.join(missing)}")
    try:
        s = Safety(Store(config.db_path), config)
        print(f"  • Authorized-lab mode: {'ON' if s.lab_enabled() else 'OFF (defensive mode only)'}")
    except Exception:  # noqa: BLE001
        pass

    print("\n" + ("Builder is ready." if ok else "Some checks failed — see above. Builder still works for anything that passed."))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_env()  # read .env (settings like BUILDER_CODE_DIRS) so you don't have to export them every time
    config = Config()
    config.ensure_home()
    db = Store(config.db_path)
    memory = Memory(db, None, config)
    llm = Ollama(config.ollama_url, config.model, config.llm_timeout, config.num_ctx,
                 think=config.think == "all", one_at_a_time=config.one_model_at_a_time)
    cmd = args.cmd

    try:
        if cmd in (None, "chat"):
            chat_loop(Assistant(llm, config, memory, db))
        elif cmd == "voice":
            voice_loop(Assistant(llm, config, memory, db), config, args.hands_free)
        elif cmd == "say":
            from .voice import Voice
            Voice(config.whisper_model, config.piper_voice, config.say_voice).say(" ".join(args.text))
        elif cmd == "build":
            return _cmd_build(args, config, llm, memory)
        elif cmd == "review":
            return _cmd_review(args, config, db, memory, llm)
        elif cmd == "lab":
            return _cmd_lab(args, config, db)
        elif cmd == "doctor":
            return _cmd_doctor(config, llm)
        elif cmd == "preview":
            import webbrowser
            from .projects import ScopeError, allowed_dirs, check_scope
            try:
                p = check_scope(args.path, allowed_dirs(config))
            except ScopeError as e:
                print(f"✗ {e}", file=sys.stderr)
                return 1
            if p.is_dir():
                idx = p / "index.html"
                if not idx.exists():
                    htmls = sorted(p.glob("*.html"))
                    idx = htmls[0] if htmls else idx
                p = idx
            if not p.exists():
                print(f"✗ nothing to preview at {args.path} (no index.html / .html file)", file=sys.stderr)
                return 1
            print(f"opening {p} in your browser…")
            webbrowser.open(p.as_uri())
        elif cmd == "reviews":
            _print_rows(db.reviews(" ".join(args.query), args.severity, 50),
                        ["id", "severity", "topic", "issue", "fix"], 60)
        elif cmd == "builds":
            rows = [{**b, "ok": "✓" if b["ok"] else "✗"} for b in db.builds(" ".join(args.query), 50)]
            _print_rows(rows, ["id", "ok", "attempts", "created_at", "task", "problem"], 50)
        elif cmd == "feedback":
            print(f"lesson #{memory.learn(' '.join(args.text), args.kind)} — Builder will apply it from now on")
        elif cmd == "lessons":
            _print_rows(db.lessons(), ["id", "kind", "origin", "created_at", "text"], 90)
        elif cmd == "remember":
            print(f"note #{db.remember(' '.join(args.text), args.topic)} saved")
        elif cmd == "recall":
            _print_rows(db.recall(" ".join(args.query), limit=20), ["id", "topic", "created_at", "content"], 90)
        elif cmd == "forget":
            print("forgotten" if db.forget(args.id) else "no note with that id")
        elif cmd == "pending":
            _print_rows(db.pending(), ["id", "kind", "origin", "asked_at", "depth", "text"], 90)
        elif cmd == "card":
            import json as _json
            from .agent import CARD
            print(_json.dumps({**CARD, "store": db.stats()}, indent=2))
        elif cmd == "serve":
            from .server import serve
            from .voice import Voice
            serve(db, Assistant(llm, config, memory, db), args.host or config.api_host,
                  args.port or config.api_port, config.api_token, memory,
                  Voice(config.whisper_model, config.piper_voice, config.say_voice))
        elif cmd == "hub":
            return hub_command(config, args)
        elif cmd == "bench":
            from . import bench
            models = [m.strip() for m in (args.models or "").split(",") if m.strip()] or \
                list(dict.fromkeys([config.model, config.fast_model]))
            print(f"Benchmarking {', '.join(models)} on this machine (num_ctx {config.num_ctx})…")
            results = bench.run(models, config.ollama_url, config.num_ctx, config.llm_timeout)
            if results:
                print()
                _print_rows(results, ["model", "ok", "score", "sec", "note"])
                print("\nSuggested:\n" + bench.recommend(results))
    except (LLMError, ValueError, SyntaxError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def hub_command(config: Config, args) -> int:
    from .connector import HubClientError, HubOffline, build
    try:
        conn = build(config, log=lambda m: print(m, file=sys.stderr))
    except ImportError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    hub = conn.hub
    if not hub.token:
        print("No hub token. Set HUB_TOKEN in Builder's .env (make it on the hub: python3 -m hub agent token builder).",
              file=sys.stderr)
        return 1
    try:
        me = hub.me()
    except HubOffline:
        print(f"Can't reach the hub at {hub.url}. Is it running, and is HUB_URL right "
              "(use Amo's relay http://127.0.0.1:8771)?", file=sys.stderr)
        return 1
    except HubClientError as e:
        print(f"The hub refused the connection ({e.status}: {e.message}). Is the token right?", file=sys.stderr)
        return 1
    member = me.get("member", {})
    print(f"Connected to the hub at {hub.url} as {member.get('name', member.get('id', '?'))}. "
          f"Channels: {', '.join(me.get('channels', []))}. Unread: {me.get('unread', 0)}.", file=sys.stderr)
    cs = conn.cloud_status()
    if cs["ready"]:
        rem = (cs.get("budget") or {}).get("remaining")
        rem = f" (about ${rem:.2f} of cloud budget left)" if isinstance(rem, (int, float)) else ""
        print(f"☁️  Cloud brain ready: family work will use Claude{rem}.", file=sys.stderr)
    else:
        why = ("the anthropic package isn't installed (pip install 'builder[cloud]')" if not cs["sdk"]
               else "no ANTHROPIC_API_KEY in Builder's .env" if not cs["key"]
               else "no internet" if not cs["online"]
               else "the hub budget is $0 or used up (set one on the hub's Admin page)")
        print(f"🏠 Cloud brain off — {why}. Family work will use the local coder model.", file=sys.stderr)
    if args.check:
        return 0
    conn.run(once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
