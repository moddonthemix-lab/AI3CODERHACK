"""Command line: talk to Scholar, run research, or browse his library directly."""

from __future__ import annotations

import argparse
import json
import sys

from pathlib import Path

from . import calc, sandbox
from .agent import CARD
from .assistant import Assistant
from .config import Config
from .credibility import TIER_LABELS
from .db import LESSON_KINDS, STATUSES, Store
from .documents import docling_available
from .llm import LLMError, Ollama
from .memory import ResearchMemory, build_index
from .research import DEPTHS, Researcher, to_json
from .tools import Toolbox
from .web import Web, WebError


def _print_rows(rows: list[dict], cols: list[str], width: int = 70) -> None:
    if not rows:
        print("(none)")
        return

    def cell(r, c):
        v = r.get(c)
        v = "" if v is None else ", ".join(f"S{i}" for i in v) if isinstance(v, list) else str(v)
        return v if len(v) <= width else v[:width - 1] + "…"

    widths = {c: max(len(c), *(len(cell(r, c)) for r in rows)) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    for r in rows:
        print("  ".join(cell(r, c).ljust(widths[c]) for c in cols))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="scholar", description="Scholar — the Researcher of your AI family.")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("chat", help="Talk to Scholar in the terminal")
    v = sub.add_parser("voice", help="Talk to Scholar out loud (press Enter, then speak)")
    v.add_argument("--hands-free", action="store_true", help="listen continuously, no Enter needed")
    v = sub.add_parser("say", help="Hear Scholar read something (e.g. a report's answer)")
    v.add_argument("text", nargs="*")
    v.add_argument("--report", type=int, help="read the answer of this report aloud")
    for name, helptext in (("research", "Investigate a question and write a cited report"),
                           ("plan", "Turn a business idea into a researched plan with numbers")):
        r = sub.add_parser(name, help=helptext)
        r.add_argument("text", nargs="+")
        r.add_argument("--depth", choices=list(DEPTHS), default="standard")
        r.add_argument("--json", action="store_true", help="print the structured data instead of markdown")

    s = sub.add_parser("search", help="Search without the model (web, papers or wiki)")
    s.add_argument("query", nargs="+")
    s.add_argument("--in", dest="where", choices=["web", "papers", "wiki", "filings"], default="web")
    s.add_argument("--limit", type=int, default=8)
    s = sub.add_parser("read", help="Read a URL or document into the library (tables, references) and print it")
    s.add_argument("url")
    s.add_argument("--chars", type=int, default=3000)
    s = sub.add_parser("citations", help="Follow a paper's citations through OpenAlex")
    s.add_argument("doi")
    s.add_argument("--cited-by", action="store_true", help="newer work citing it, instead of what it cites")
    s = sub.add_parser("ingest", help="Add your own documents (file or folder) to Scholar's library")
    s.add_argument("path")
    s.add_argument("--tier", type=int, choices=[1, 2, 3, 4], default=2, help="how much to trust them (default 2)")

    f = sub.add_parser("finding", help="Findings in the library").add_subparsers(dest="action", required=True)
    a = f.add_parser("list")
    a.add_argument("query", nargs="*")
    a.add_argument("--status", choices=STATUSES)
    a = f.add_parser("show")
    a.add_argument("id", type=int)
    a = f.add_parser("add")
    a.add_argument("claim", nargs="+")
    a.add_argument("--status", choices=STATUSES, default="assumption")
    a.add_argument("--confidence", type=float, default=0.5)
    a.add_argument("--topic", default="general")
    a.add_argument("--source", type=int, action="append", default=[], help="source id (repeatable)")
    a = f.add_parser("update")
    a.add_argument("id", type=int)
    a.add_argument("--status", choices=STATUSES)
    a.add_argument("--confidence", type=float)
    a.add_argument("--source", type=int, action="append", default=[])
    a.add_argument("--reason", required=True)

    s = sub.add_parser("sources", help="Sources Scholar has read")
    s.add_argument("query", nargs="*")
    sub.add_parser("reports", help="List research reports")
    s = sub.add_parser("report", help="Print a report")
    s.add_argument("id", type=int)
    sub.add_parser("investigations", help="Past investigations")
    s = sub.add_parser("questions", help="The question tree of an investigation (or all unanswered ones)")
    s.add_argument("investigation", type=int, nargs="?")
    s = sub.add_parser("graph", help="The research graph: top entities, or one entity's connections")
    s.add_argument("entity", nargs="*")
    s = sub.add_parser("tables", help="Tables extracted from documents")
    s.add_argument("--source", type=int)
    s = sub.add_parser("table", help="Print one extracted table")
    s.add_argument("id", type=int)
    s = sub.add_parser("analyze", help="Run Python in the sandbox on extracted tables")
    s.add_argument("script", nargs="?", help="a .py file (or use --code)")
    s.add_argument("--code")
    s.add_argument("--table", type=int, action="append", default=[], help="table id, copied in as T<id>.csv")
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
    s = sub.add_parser("glossary", help="Terms Scholar has learned")
    s.add_argument("term", nargs="*")
    s = sub.add_parser("feedback", help="Teach Scholar: a preference, correction, strategy or note about a source")
    s.add_argument("text", nargs="+")
    s.add_argument("--kind", choices=LESSON_KINDS, default="preference")
    s.add_argument("--report", type=int)
    s.add_argument("--finding", type=int)
    s = sub.add_parser("lessons", help="What Scholar has learned from your feedback")
    s.add_argument("--retire", type=int, help="stop applying this lesson")
    sub.add_parser("reindex", help="Rebuild semantic memory (e.g. after switching to Qdrant)")
    sub.add_parser("pending", help="Work waiting for the internet (offline rechecks, questions from devices)")
    sub.add_parser("sync", help="Catch up now: answer queued questions, run queued research if online")
    s = sub.add_parser("inbox", help="Messages from Scholar (finished research, answers to queued questions)")
    s.add_argument("--all", action="store_true", help="include read messages")

    r = sub.add_parser("remember", help="Save a note")
    r.add_argument("text", nargs="+")
    r.add_argument("--topic", default="general")
    r = sub.add_parser("recall", help="Search notes")
    r.add_argument("query", nargs="*")
    r = sub.add_parser("forget", help="Delete a note by id")
    r.add_argument("id", type=int)

    c = sub.add_parser("calc", help="Exact arithmetic, e.g. scholar calc '(320-210)/320*100'")
    c.add_argument("expression", nargs="+")
    e = sub.add_parser("economics", help="Unit economics for a product")
    e.add_argument("--price", type=float, required=True)
    e.add_argument("--cost", type=float, required=True)
    e.add_argument("--fees-pct", type=float, default=0)
    e.add_argument("--fee-fixed", type=float, default=0)
    e.add_argument("--shipping", type=float, default=0)
    e.add_argument("--fixed", type=float, default=0, help="fixed costs per month")
    st = sub.add_parser("stress", help="Monte Carlo stress test; each input is a value or 'low likely high'")
    for name in ("price", "cost", "units"):
        st.add_argument(f"--{name}", type=float, nargs="+", required=True)
    st.add_argument("--fees-pct", type=float, nargs="+", default=[0])
    st.add_argument("--shipping", type=float, nargs="+", default=[0])
    st.add_argument("--fixed", type=float, nargs="+", default=[0])
    st.add_argument("--startup", type=float, nargs="+", default=[0])
    st.add_argument("--months", type=int, default=12)

    sub.add_parser("card", help="Scholar's identity card and library stats")
    s = sub.add_parser("serve", help="Run the JSON API for the rest of the family")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    sub.add_parser("doctor", help="Check Ollama, the model and internet access")
    h = sub.add_parser("hub", help="Join the Family Hub forum: take research tasks, post findings")
    h.add_argument("--once", action="store_true", help="process the inbox once and exit (for testing/cron)")
    h.add_argument("--check", action="store_true", help="just check the hub connection and who I am")
    b = sub.add_parser("bench", help="Measure accuracy and speed of models on this machine")
    b.add_argument("--models", help="comma-separated, e.g. qwen3:4b-instruct,qwen3:8b,gpt-oss:20b (default: your two)")
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
            print("(conversation cleared — the library is kept)")
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


def _progress(msg: str) -> None:
    print(f"  · {msg}", file=sys.stderr, flush=True)


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


def _cmd_build(args, config, llm, memory=None) -> int:
    """`builder build`: write a small project, run its tests, fix failures, show the diff. All inside scope."""
    from .buildagent import make_propose
    from .projects import Project, ScopeError, build_and_test, restore_backup, save_backup

    backup_path = config.home / "backups" / "last_build.json"
    err = lambda m: print(m, file=sys.stderr)

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

    # What Builder remembers about this kind of work (the boss's stack, past fixes, lessons) — works offline.
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

    err = lambda m: print(m, file=sys.stderr)
    path = args.path

    # What Builder already remembers about this area (lessons + past findings) — local, works offline.
    if memory is not None:
        try:
            mem = memory.recall(f"security review {Path(path).name}")
            note = memory.as_prompt({**mem, "library": [], "queries": [], "glossary": [], "entities": [],
                                     "investigations": []}, max_chars=900)
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
    # Save into Builder's personal store (local memory) unless told not to — the hub side mirrors via the connector.
    if not args.no_save:
        saved = 0
        for f in findings:
            try:
                if db.conn.execute("SELECT 1 FROM findings WHERE claim = ? LIMIT 1", (f.issue,)).fetchone():
                    continue
                db.add_review_finding(f.issue, f.severity, f.fix, topic=(Path(path).name or "security")[:80])
                saved += 1
            except Exception:  # noqa: BLE001
                pass
        if saved:
            err(f"\n(remembered {saved} new finding(s) in my local store)")
    return 1  # non-zero so this can gate a pre-commit hook or CI


def _cmd_lab(args, config, db) -> int:
    """`builder lab`: turn authorized-lab mode on/off, see authorized targets, and read the audit log."""
    from .safety import ApprovalRequired, Safety, SafetyError

    err = lambda m: print(m, file=sys.stderr)
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
        _print_rows(db.safety_events(args.limit),
                    ["at", "action", "decision", "target", "detail", "reason"], width=40)
        return 0
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = Config()
    config.ensure_home()
    db = Store(config.db_path)
    web = Web(config.searxng_url, config.tor_proxy, openalex_key=config.openalex_key, contact=config.contact,
              offline=config.offline)
    memory = ResearchMemory(db, build_index(config, db), config)
    tb = Toolbox(db, web, memory, config)
    llm = Ollama(config.ollama_url, config.model, config.llm_timeout, config.num_ctx, think=config.think == "all",
                 one_at_a_time=config.one_model_at_a_time)
    fast = llm if config.fast_model == config.model else \
        Ollama(config.ollama_url, config.fast_model, config.llm_timeout, config.num_ctx,
               one_at_a_time=config.one_model_at_a_time)
    cmd, action = args.cmd, getattr(args, "action", None)

    try:
        if cmd in (None, "chat"):
            chat_loop(Assistant(tb, llm, config))
        elif cmd == "voice":
            voice_loop(Assistant(tb, llm, config), config, args.hands_free)
        elif cmd == "say":
            from .voice import Voice
            text = " ".join(args.text)
            if args.report:
                r = db.get_report(args.report)
                text = (r["data"].get("answer") if r else "") or "I couldn't find that report."
            Voice(config.whisper_model, config.piper_voice, config.say_voice).say(text or "Hello. I'm Scholar.")
        elif cmd in ("research", "plan"):
            result = Researcher(tb, llm, config, log=_progress, fast_llm=fast).investigate(
                " ".join(args.text), args.depth, cmd)
            t = llm.totals if fast is llm else {k: llm.totals[k] + fast.totals[k] for k in llm.totals}
            print(f"(model time: {t['seconds'] / 60:.1f} min over {t['calls']} calls, "
                  f"{t['prompt_tokens']:,} tokens read, {t['output_tokens']:,} written)", file=sys.stderr)
            print(to_json(result) if args.json else result["markdown"])
            if result["path"]:
                print(f"(saved as report #{result['report_id']}: {result['path']})", file=sys.stderr)
        elif cmd == "search":
            q = " ".join(args.query)
            fn = {"web": web.search, "papers": web.papers, "wiki": web.wikipedia,
                  "filings": lambda q, n: web.filings(q, limit=n)}[args.where]
            _print_hits(db, fn(q, args.limit), {"papers": "paper", "wiki": "reference"}.get(args.where, args.where))
        elif cmd == "citations":
            _print_hits(db, (web.cited_by if args.cited_by else web.references_of)(args.doi, 15), "paper")
        elif cmd == "ingest":
            for r in memory.ingest(args.path, args.tier):
                if "error" in r:
                    print(f"✗ {r['file']}: {r['error']}")
                else:
                    print(f"✓ [S{r['source_id']}] {r['file']} — {r['parser']}, {r['chunks']} passages, "
                          f"{len(r['tables'])} tables" + ("" if r["indexed"] else " (keyword search only)"))
        elif cmd == "read":
            from .tools import call
            r = json.loads(call(tb, "read_source", {"url": args.url, "max_chars": args.chars}))
            if "error" in r:
                raise WebError(r["error"])
            print(f"[S{r['source_id']}] {r['title']}\n{r['url']}\ntier {r['tier']}: {r['tier_label']}"
                  + (f" · published {r['published']}" if r["published"] else "") + f" · parsed by {r['parser']}")
            for t in r["tables"]:
                print(f"  table T{t['table_id']}: {t['caption'] or 'untitled'} ({t['rows']}×{t['cols']})")
            refs = r["references"]
            if refs["dois"] or refs["arxiv"]:
                print(f"  cites {len(refs['dois'])} DOIs, {len(refs['arxiv'])} arXiv papers")
            print("\n" + r["text"])
        elif cmd == "finding":
            if action == "list":
                _print_rows(db.findings(" ".join(args.query), 50, args.status),
                            ["id", "status", "confidence", "claim", "source_ids", "updated_at"])
            elif action == "show":
                f = db.get_finding(args.id)
                if not f:
                    print("not found")
                    return 1
                print(f"F{f['id']}  [{f['status']}, {f['confidence']:.0%}]  {f['claim']}\n")
                for i in f["source_ids"]:
                    s = db.get_source(i)
                    if s:
                        print(f"  [S{i}] tier {s['tier']}  {s['title']}  {s['url']}")
                for e in db.evidence(f["id"]):
                    mark = "+" if e["stance"] == "supports" else "−"
                    check = "✓" if e["quote_ok"] else "?"
                    print(f"  {mark} [S{e['source_id']}] {check} \"{e['quote'][:140]}\"")
                if f["checks"]:
                    print("\nSkeptic's flags:\n" + "\n".join(f"  - {c}" for c in f["checks"]))
                print("\nHistory:")
                for h in db.finding_history(f["id"]):
                    print(f"  {h['changed_at']}  {h['status']:<10} {h['confidence']:.0%}  {h['reason']}")
            elif action == "add":
                print(f"finding #{db.add_finding(' '.join(args.claim), args.status, args.confidence, args.topic, args.source)}")
            else:
                f = db.update_finding(args.id, args.status, args.confidence, args.source, args.reason)
                print(f"F{f['id']} is now {f['status']} ({f['confidence']:.0%})" if f else "not found")
        elif cmd == "sources":
            _print_rows(db.sources(" ".join(args.query), 50), ["id", "tier", "domain", "title", "fetched_at"])
        elif cmd == "reports":
            _print_rows(db.reports(50), ["id", "mode", "created_at", "question"])
        elif cmd == "report":
            r = db.get_report(args.id)
            print(r["body"] if r else "not found")
        elif cmd == "investigations":
            _print_rows(db.investigations(50), ["id", "status", "depth", "questions", "open", "created_at", "question"])
        elif cmd == "questions":
            rows = db.questions(args.investigation) if args.investigation else db.questions(status="unanswered", limit=100)
            _print_rows(rows, ["id", "investigation_id", "status", "origin", "dimension", "text"], 90)
        elif cmd == "graph":
            name = " ".join(args.entity)
            if not name:
                _print_rows(db.entities(limit=40), ["id", "type", "sources", "name"])
            else:
                n = db.neighbors(name) or next((db.neighbors(e["id"]) for e in db.entities(name, 1)), {})
                if not n:
                    print("not in the graph yet")
                    return 1
                print(f"{n['name']} ({n['type']}) — mentioned in {len(n['source_ids'])} sources")
                for r_ in n["relations"]:
                    print(f"  {r_['subject']} —{r_['predicate'].replace('_', ' ')}→ {r_['object']}"
                          + (f" ({r_['date']})" if r_["date"] else "") + (f" [S{r_['source_id']}]" if r_["source_id"] else ""))
        elif cmd == "tables":
            _print_rows(db.tables(args.source), ["id", "source_id", "n_rows", "n_cols", "caption"])
        elif cmd == "table":
            t = db.get_table(args.id)
            print(f"T{t['id']} from [S{t['source_id']}] {t['caption']}\n{t['csv_path']}\n\n{t['preview']}" if t else "not found")
        elif cmd == "analyze":
            code = args.code or (Path(args.script).read_text() if args.script else None)
            if not code:
                print("give a script file or --code", file=sys.stderr)
                return 1
            inputs = {f"T{t}.csv": db.get_table(t)["csv_path"] for t in args.table if db.get_table(t)}
            r = sandbox.run_python(code, config.analysis_dir, inputs, timeout=config.sandbox_timeout)
            print(r["stdout"], end="")
            if r["stderr"]:
                print(r["stderr"], file=sys.stderr, end="")
            print(f"\n({'ok' if r['ok'] else 'failed'} — files in {r['workdir']})", file=sys.stderr)
            for f in r["files"]:
                print(f"  {f}", file=sys.stderr)
            return 0 if r["ok"] else 1
        elif cmd == "build":
            return _cmd_build(args, config, llm, memory)
        elif cmd == "review":
            return _cmd_review(args, config, db, memory, llm)
        elif cmd == "lab":
            return _cmd_lab(args, config, db)
        elif cmd == "glossary":
            _print_rows(db.glossary(" ".join(args.term)), ["term", "domain", "definition"], 100)
        elif cmd == "feedback":
            ref = f"report:{args.report}" if args.report else f"finding:{args.finding}" if args.finding else ""
            print(f"lesson #{memory.learn(' '.join(args.text), args.kind, ref=ref)} — Scholar will apply it from now on")
        elif cmd == "lessons":
            if args.retire:
                print("retired" if db.retire_lesson(args.retire) else "not found")
            else:
                _print_rows(db.lessons(), ["id", "kind", "origin", "ref", "created_at", "text"], 90)
        elif cmd == "pending":
            _print_rows(db.pending(), ["id", "kind", "origin", "asked_at", "depth", "text"], 90)
            print("internet: " + ("online" if web.online() else "offline"))
        elif cmd == "sync":
            from . import offline
            ask = lambda text, device: Assistant(tb, llm, config).ask(text, device)  # noqa: E731

            def run_now(kind, text, depth, origin):
                r = Researcher(tb, llm, config, log=_progress, fast_llm=fast).investigate(text, depth, kind)
                db.notify(f"Research done: {text}\n\n{r['data'].get('answer', '')}", f"report:{r['report_id']}")
                return f"report:{r['report_id']}"
            out = offline.process(db, web.online, ask, run_now, _progress)
            print(f"{'online' if out['online'] else 'offline'}: {len(out['done'])} done, {out['waiting']} still waiting")
        elif cmd == "inbox":
            for m in reversed(db.inbox(not args.all, 50)):
                print(f"— #{m['id']} {m['created_at']}" + ("" if m["read"] else "  (new)") + f"\n{m['text']}\n")
            db.mark_read()
        elif cmd == "reindex":
            print(json.dumps(memory.reindex(_progress), indent=2))
        elif cmd == "stress":
            print(json.dumps(calc.monte_carlo(args.price, args.cost, args.units, args.fees_pct, args.shipping,
                                              args.fixed, args.startup, args.months), indent=2))
        elif cmd == "remember":
            print(f"saved #{db.remember(' '.join(args.text), args.topic)}")
        elif cmd == "recall":
            _print_rows(db.recall(" ".join(args.query), limit=20), ["id", "topic", "created_at", "content"])
        elif cmd == "forget":
            print("deleted" if db.forget(args.id) else "not found")
        elif cmd == "calc":
            print(calc.calculate(" ".join(args.expression)))
        elif cmd == "economics":
            print(json.dumps(calc.unit_economics(args.price, args.cost, args.fees_pct, args.fee_fixed,
                                                 args.shipping, args.fixed), indent=2))
        elif cmd == "card":
            print(json.dumps({**CARD, "library": db.stats()}, indent=2))
        elif cmd == "serve":
            from .server import serve
            from .server import Jobs

            def job_researcher(log):  # each background job gets its own database connection
                jdb = Store(config.db_path)
                jmem = ResearchMemory(jdb, build_index(config, jdb), config)
                return Researcher(Toolbox(jdb, web, jmem, config), llm, config, log=log, fast_llm=fast)

            from .voice import Voice
            serve(db, Assistant(tb, llm, config), Researcher(tb, llm, config, log=_progress, fast_llm=fast),
                  args.host or config.api_host, args.port or config.api_port, config.api_token, memory,
                  Jobs(job_researcher), Voice(config.whisper_model, config.piper_voice, config.say_voice), web)
        elif cmd == "doctor":
            return doctor(config, llm, web, memory, fast)
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
                _print_rows(results, ["model", "score", "recall", "quotes", "honest", "judgement", "synthesis",
                                      "sec_per_read", "sec_per_think", "read_tps", "write_tps"])
                print("\nSuggested:\n" + bench.recommend(results))
    except (LLMError, WebError, ValueError, SyntaxError) as e:
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
        print("No hub token. Set HUB_TOKEN in Scholar's .env (make it on the hub: python3 -m hub agent token scholar).",
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
        print(f"☁️  Cloud brain ready: family research will use Claude{rem}.", file=sys.stderr)
    else:
        why = ("the anthropic package isn't installed (pip install 'scholar[cloud]')" if not cs["sdk"]
               else "no ANTHROPIC_API_KEY in Scholar's .env" if not cs["key"]
               else "no internet" if not cs["online"]
               else "the hub budget is $0 or used up (set one on the hub's Admin page)")
        print(f"🏠 Cloud brain off — {why}. Family work will use the local model "
              "(research tasks wait for the web).", file=sys.stderr)
    if args.check:
        return 0
    conn.run(once=args.once)
    return 0


def _print_hits(db: Store, hits: list[dict], kind: str) -> None:
    for r in hits:
        sid = db.add_source(r["url"], r.get("title", ""), r.get("domain", ""), kind, r.get("tier", 3),
                            r.get("snippet", ""), r.get("published", ""))
        extra = "".join(f" · {k} {r[k]}" for k in ("published", "cited_by_count", "form") if r.get(k))
        flag = "  ⚠ RETRACTED" if r.get("is_retracted") else ""
        print(f"[S{sid}] tier {r['tier']}  {r.get('title', '')}{flag}\n      {r['url']}{extra}")
        if r.get("snippet"):
            print(f"      {r['snippet'][:200]}")


def doctor(config: Config, llm: Ollama, web: Web, memory: ResearchMemory | None = None,
           fast: Ollama | None = None) -> int:
    ok = True
    print(f"library: {config.db_path}")
    print(f"reports: {config.reports_dir}")
    print(f"profile: {config.profile} (context {config.num_ctx} tokens, reads {config.read_chars} characters per source)")
    for role, m in (("thinker", llm), ("reader", fast or llm)):
        if role == "reader" and m is llm:
            print("reader:  same model as the thinker")
            continue
        print(f"{role}: {m.model} via {config.ollama_url}"
              + (f"  (thinking: {config.think})" if role == "thinker" else ""))
        if m.available():
            print("  ✓ Ollama is reachable and the model is installed")
            try:
                if not m.talks_plainly():
                    print(f"  ⚠ {m.model} thinks out loud even with thinking off — chat replies will be very slow. "
                          "Use an Instruct edition (e.g. qwen3:4b-instruct) or a hybrid model (qwen3:8b).")
            except LLMError as e:
                print(f"  ⚠ couldn't test the model: {e}")
        else:
            ok = False
            print(f"  ✗ not ready — install Ollama (https://ollama.com), then: ollama pull {m.model}")
    print("         run `scholar bench` to measure accuracy and speed of models on this machine")
    print("memory:  " + ("one model at a time (Scholar unloads the last one before switching)"
                         if config.one_model_at_a_time else "several models may stay loaded (SCHOLAR_ONE_MODEL=0)"))
    from .llm import loaded_models
    loaded = [m for m in loaded_models(config.ollama_url) if "embed" not in m["name"]]
    if loaded:
        print("         loaded right now: " + ", ".join(f"{m['name']} ({m['size_gb']} GB)" for m in loaded))
    if len(loaded) > 1:
        print(f"  ⚠ {len(loaded)} chat models are in memory at once (maybe Amo's too). On a 16GB machine that "
              "makes everything swap to disk and crawl. Make it a rule for every app on this Mac:\n"
              "      launchctl setenv OLLAMA_MAX_LOADED_MODELS 1   (then quit and reopen Ollama)")
    print("internet: " + ("off (SCHOLAR_OFFLINE=1)" if config.offline else "online" if web.online()
                          else "OFFLINE — he'll answer from his library and queue research for later"))
    print(f"search:  {'SearXNG at ' + config.searxng_url if config.searxng_url else 'DuckDuckGo (set SCHOLAR_SEARXNG_URL for your own SearXNG)'}")
    for label, fn in (("web search", lambda: web.search("open source research tools", 3)),
                      ("OpenAlex", lambda: web.openalex("information retrieval", 2)),
                      ("arXiv", lambda: web._arxiv("information retrieval", 1)),
                      ("Crossref", lambda: web._crossref("information retrieval", 1)),
                      ("wikipedia", lambda: web.wikipedia("Library science", 1)),
                      ("SEC filings", lambda: web.filings("annual report", limit=1))):
        try:
            print(f"  {'✓' if fn() else '?'} {label}")
        except WebError as e:
            print(f"  ✗ {label}: {e}")
    if not config.openalex_key:
        print("  · OpenAlex works best with a free key: SCHOLAR_OPENALEX_KEY (https://openalex.org/settings/api)")
    print(f"documents: {'Docling ✓ (tables, layout)' if docling_available() else 'basic parsers — for real tables: pip install -e .[docs]'}")
    if memory is not None:
        where = f"Qdrant at {config.qdrant_url}" if config.qdrant_url else "built-in (scholar.db)"
        n = memory.index.add([{"kind": "probe", "ref": "doctor", "text": "probe"}])
        memory.index.remove("probe", ["doctor"])
        if n:
            print(f"memory:  semantic ✓ — {config.embed_model} → {where}")
        else:
            print(f"memory:  keyword-only ✗ — {memory.index.error or 'unavailable'}")
            print(f"         fix: ollama pull {config.embed_model}" + ("" if not config.qdrant_url else ", and check Qdrant is running"))
    wrapper = sandbox._wrapper(config.analysis_dir)
    print(f"sandbox: python {sys.version.split()[0]}, no network" + (f" + {wrapper[0]}" if wrapper else " (audit hook only)"))
    if config.tor_proxy:
        print(f"tor:     SOCKS proxy {config.tor_proxy} (.onion sources: tier 4 — {TIER_LABELS[4]})")
    else:
        print("tor:     off (set SCHOLAR_TOR_PROXY=127.0.0.1:9050 to let Scholar read .onion sources)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
