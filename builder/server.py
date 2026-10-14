"""Tiny JSON HTTP API (standard library only).

This is how the rest of the AI family — and later the Family Hub forum —
talks to Scholar: structured requests in, structured results out.
Set SCHOLAR_API_TOKEN to require ``Authorization: Bearer <token>``.

Small devices (an ESP32-S3 screen, a Raspberry Pi terminal) can't hold a
connection open through a 20-minute investigation, so research can run as a
background job (``"async": true``) that they poll, and ``GET /api/brief`` returns
a tiny status summary sized for a microcontroller.
"""

from __future__ import annotations

import email.parser
import email.policy
import itertools
import json
import queue
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .agent import CARD
from .assistant import Assistant
from . import offline
from .db import LESSON_KINDS, Store
from .llm import LLMError
from .memory import ResearchMemory
from .research import DEPTHS, Researcher


# Lessons change how Scholar works, so only the boss can give them — not other agents, not web pages.
BOSS = ("", "boss", "user")


class Jobs:
    """Background investigations, run one at a time in order (a CPU-only machine can't do two at once).

    ``factory(log)`` returns the Researcher to use; the CLI gives each job its own database connection.
    """

    def __init__(self, factory, on_done=None):
        self.factory = factory
        self.on_done = on_done  # called with the finished job (e.g. to post it to the inbox)
        self.jobs: dict[int, dict] = {}
        self._ids = itertools.count(1)
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        threading.Thread(target=self._worker, daemon=True).start()

    def submit(self, question: str, depth: str, mode: str, sender: str = "") -> dict:
        with self._lock:
            jid = next(self._ids)
            self.jobs[jid] = {"id": jid, "status": "queued", "question": question, "depth": depth, "mode": mode,
                              "from": sender, "step": "", "steps": 0, "report_id": None, "investigation_id": None,
                              "error": None, "created": _now(), "started": None, "finished": None}
        self._queue.put(jid)
        return self.get(jid)

    def get(self, jid: int) -> dict | None:
        with self._lock:
            job = self.jobs.get(jid)
            if job is None:
                return None
            ahead = sum(1 for j in self.jobs.values() if j["status"] == "queued" and j["id"] < jid)
            return {**job, "queue_position": ahead if job["status"] == "queued" else None}

    def all(self) -> list[dict]:
        return [self.get(j) for j in sorted(self.jobs, reverse=True)[:50]]

    def running(self) -> dict | None:
        return next((self.get(j["id"]) for j in self.jobs.values() if j["status"] == "running"), None)

    def queued(self) -> int:
        return sum(1 for j in self.jobs.values() if j["status"] == "queued")

    def _worker(self) -> None:
        while True:
            jid = self._queue.get()
            job = self.jobs[jid]

            def log(msg: str, job=job) -> None:
                job["step"], job["steps"] = msg.strip()[:200], job["steps"] + 1

            job.update(status="running", started=_now())
            try:
                r = self.factory(log).investigate(job["question"], job["depth"], job["mode"])
                job.update(status="done", report_id=r["report_id"], investigation_id=r["investigation_id"],
                           answer=r["data"].get("answer", "")[:2000], step="done")
            except Exception as e:  # noqa: BLE001 — report it to whoever is polling
                job.update(status="failed", error=f"{type(e).__name__}: {e}"[:500])
            job["finished"] = _now()
            if self.on_done:
                try:
                    self.on_done(dict(job))
                except Exception:  # noqa: BLE001 — a notification must never kill the worker
                    pass


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _cut(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def brief(store: Store, jobs: Jobs | None, online: bool | None = None) -> dict:
    """A status summary small enough for a microcontroller screen (well under 1 KB)."""
    run = jobs.running() if jobs else None
    stats = store.stats()
    latest = store.inbox(unread_only=True, limit=1)
    last = store.reports(1)
    rep = store.get_report(last[0]["id"]) if last else None
    return {
        "agent": "scholar", "busy": bool(run), "queue": jobs.queued() if jobs else 0,
        "job": {"id": run["id"], "q": _cut(run["question"], 60), "step": _cut(run["step"], 60),
                "steps": run["steps"]} if run else None,
        "last": {"id": rep["id"], "q": _cut(rep["question"], 60),
                 "answer": _cut(rep["data"].get("answer", ""), 140)} if rep else None,
        "findings": stats["findings"], "verified": stats["findings_by_status"]["verified"],
        "open_q": stats["open_questions"], "online": online, "pending": stats["pending"],
        "unread": stats["unread"], "news": _cut(latest[0]["text"], 120) if latest else None,
    }


def make_handler(store: Store, assistant: Assistant | None, researcher: Researcher | None,
                 token: str = "", memory: ResearchMemory | None = None, jobs: Jobs | None = None,
                 voice=None, web=None, dashboard: Path | None = None):
    lock = threading.Lock()    # quick database work (reads, small writes): the dashboard never waits long
    brain = threading.Lock()   # one conversation with the model at a time — a CPU can't do two
    online = (lambda: web.online()) if web is not None and hasattr(web, "online") else (lambda: None)
    dashboard = dashboard or Path(__file__).with_name("static") / "index.html"

    def finished(job: dict) -> None:
        if job["status"] == "done":
            text = f"Research done: {job['question']}\n\n{job.get('answer') or '(no answer — see the report)'}"
            ref = f"report:{job['report_id']}"
        else:
            text, ref = f"Research failed: {job['question']}\n{job.get('error')}", f"job:{job['id']}"
        with lock:
            store.notify(text, ref)

    if jobs is None and researcher is not None:
        def shared(log):  # tests / simple setups: reuse the one researcher, one at a time
            researcher.log = log
            return _Locked(researcher, brain)
        jobs = Jobs(shared, finished)
    elif jobs is not None and jobs.on_done is None:
        jobs.on_done = finished

    def ask(text: str, sender: str = "") -> str:
        with brain:
            return assistant.ask(text, sender) if assistant is not None else ""

    def submit(kind: str, text: str, depth: str, origin: str) -> str:
        job = jobs.submit(text, depth if depth in DEPTHS else "quick", "plan" if kind == "plan" else "research", origin)
        return f"job:{job['id']}"

    def catch_up() -> dict:
        return offline.process(store, lambda: online() is not False, ask, submit if jobs else None)

    class Handler(BaseHTTPRequestHandler):
        server_version = "Scholar/0.1"

        def log_message(self, fmt, *args):  # quieter default logging
            pass

        def _send(self, status: int, body) -> None:
            data = json.dumps(body, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            if not token:
                return True
            if self.headers.get("Authorization", "") == f"Bearer {token}":
                return True
            self._send(401, {"error": "unauthorized"})
            return False

        def _send_bytes(self, status: int, data: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _raw(self, limit: int = 20 * 2**20) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            if length > limit:
                raise ValueError("upload too large")
            return self.rfile.read(length) if length else b""

        def _voice(self, path: str, text: str | None = None):
            """Voice terminal endpoints (e.g. an ESP32-S3 with a mic and speaker)."""
            from .voice import VoiceUnavailable, wav_info
            if voice is None:
                return self._send(503, {"error": "voice not configured"})
            try:
                if path == "/api/speak":
                    body = {} if text is not None else self._body()
                    text = text if text is not None else body.get("text", "")
                    if not text:
                        return self._send(400, {"error": "send {\"text\": \"...\"}"})
                    wav = voice.synthesize(text)
                    if str(body.get("rate", "")).isdigit():  # e.g. 16000 for an ESP32 speaker
                        from .voice import to_rate
                        wav = to_rate(wav, int(body["rate"]))
                    return self._send_bytes(200, wav, "audio/wav")
                audio = self._raw()  # /api/listen: the body is a WAV recording
                try:
                    info = wav_info(audio)
                except Exception:  # noqa: BLE001 — anything that isn't a WAV
                    return self._send(400, {"error": "send a WAV recording (16 kHz mono works best)"})
                text = voice.transcribe(audio)
                reply = ""
                if text and assistant is not None:
                    reply = ask(text, self.headers.get("X-From", ""))
                from .voice import speakable
                return self._send(200, {"heard": text, "reply": reply, "speak": speakable(reply), "audio": info})
            except VoiceUnavailable as e:
                return self._send(503, {"error": str(e)})
            except LLMError as e:
                return self._send(502, {"error": str(e)})

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
            return body

        def do_GET(self):
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):  # the dashboard page itself; its API calls carry the token
                try:
                    return self._send_bytes(200, dashboard.read_bytes(), "text/html; charset=utf-8")
                except OSError:
                    return self._send(404, {"error": "dashboard file missing"})
            if not self._authorized():
                return
            qs = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path == "/api/brief":
                is_online = online()  # may take a moment to check; don't hold the lock for it
                with lock:
                    return self._send(200, brief(store, jobs, is_online))
            if url.path == "/v1/models":
                return self._send(200, {"object": "list", "data": [{"id": MODEL_ID, "object": "model", "created": 0,
                                                                    "owned_by": "local", "name": CARD["name"]}]})
            with lock:
                if url.path == "/api/health":
                    return self._send(200, {"ok": True, "agent": CARD["id"]})
                if url.path == "/api/stats":
                    return self._send(200, store.stats())
                if url.path == "/api/inbox":
                    return self._send(200, store.inbox(qs.get("unread") in ("1", "true"), int(qs.get("limit", 30))))
                if url.path == "/api/pending":
                    return self._send(200, store.pending(qs.get("all") in ("1", "true")))
                if url.path == "/api/jobs":
                    return self._send(200, jobs.all() if jobs else [])
                if m := re.fullmatch(r"/api/jobs/(\d+)", url.path):
                    job = jobs.get(int(m.group(1))) if jobs else None
                    return self._send(200 if job else 404, job or {"error": "not found"})
                if url.path == "/api/agent":
                    return self._send(200, {**CARD, "library": store.stats()})
                if url.path == "/api/findings":
                    return self._send(200, store.findings(qs.get("q", ""), int(qs.get("limit", 20)),
                                                          qs.get("status")))
                if m := re.fullmatch(r"/api/findings/(\d+)", url.path):
                    f = store.get_finding(int(m.group(1)))
                    if f:
                        f["history"] = store.finding_history(f["id"])
                        f["evidence"] = store.evidence(f["id"])
                        f["sources"] = [_brief(store.get_source(i)) for i in f["source_ids"]]
                    return self._send(200 if f else 404, f or {"error": "not found"})
                if url.path == "/api/sources":
                    return self._send(200, [_brief(s) for s in store.sources(qs.get("q", ""))])
                if url.path == "/api/investigations":
                    return self._send(200, store.investigations())
                if m := re.fullmatch(r"/api/investigations/(\d+)", url.path):
                    inv = store.get_investigation(int(m.group(1)))
                    if inv:
                        inv["questions"] = store.questions(inv["id"])
                        inv["graph"] = store.relations(inv["id"])
                    return self._send(200 if inv else 404, inv or {"error": "not found"})
                if url.path == "/api/questions":
                    return self._send(200, store.questions(status=qs.get("status", "unanswered")))
                if url.path == "/api/graph":
                    if qs.get("entity"):
                        n = store.neighbors(qs["entity"])
                        return self._send(200 if n else 404, n or {"error": "not in the graph"})
                    return self._send(200, {"entities": store.entities(qs.get("q", ""), 100),
                                            "relations": store.relations(limit=500)})
                if url.path == "/api/tables":
                    return self._send(200, store.tables(int(qs["source"]) if qs.get("source") else None))
                if url.path == "/api/glossary":
                    return self._send(200, store.glossary(qs.get("q", "")))
                if url.path == "/api/lessons":
                    return self._send(200, store.lessons())
                if url.path == "/api/reports":
                    return self._send(200, store.reports())
                if m := re.fullmatch(r"/api/reports/(\d+)", url.path):
                    r = store.get_report(int(m.group(1)))
                    return self._send(200 if r else 404, r or {"error": "not found"})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._authorized():
                return
            path = urlparse(self.path).path
            if path == "/v1/audio/transcriptions":
                try:
                    return self._transcriptions()
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
            if path in ("/api/speak", "/api/listen"):
                try:
                    return self._voice(path)
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
            try:
                body = self._body()
            except ValueError:
                return self._send(400, {"error": "invalid JSON"})
            sender = str(body.get("from", ""))
            if path == "/v1/chat/completions":
                return self._chat_completions(body)
            if path == "/v1/audio/speech":
                return self._voice("/api/speak", body.get("input", ""))
            if path == "/api/sync":
                return self._sync(body)
            if path == "/api/catch-up":
                return self._send(200, catch_up())
            if path == "/api/ask" and body.get("text"):
                if assistant is None:
                    return self._send(503, {"error": "assistant not configured"})
                try:
                    return self._send(200, {"from": "scholar", "reply": ask(body["text"], sender)})
                except LLMError as e:
                    return self._send(502, {"error": str(e)})
            with lock:
                try:
                    if path == "/api/inbox/read":
                        return self._send(200, {"marked": store.mark_read(body.get("id"))})
                    if path == "/api/feedback" and body.get("text"):
                        if sender.lower() not in BOSS:
                            return self._send(403, {"error": "lessons come only from the boss — pass suggestions to them"})
                        kind = body.get("kind", "preference")
                        if kind not in LESSON_KINDS:
                            return self._send(400, {"error": f"kind must be one of {', '.join(LESSON_KINDS)}"})
                        lid = (memory.learn(body["text"], kind, ref=str(body.get("ref", ""))) if memory
                               else store.add_lesson(body["text"], kind, ref=str(body.get("ref", ""))))
                        return self._send(201, {"lesson_id": lid})
                    if path in ("/api/research", "/api/plan") and (body.get("question") or body.get("idea")):
                        if researcher is None:
                            return self._send(503, {"error": "researcher not configured"})
                        depth = body.get("depth", "standard")
                        if depth not in DEPTHS:
                            return self._send(400, {"error": f"depth must be one of {', '.join(DEPTHS)}"})
                        mode = "plan" if path == "/api/plan" else "research"
                        if body.get("async"):
                            job = jobs.submit(body.get("question") or body["idea"], depth, mode, sender)
                            return self._send(202, {"from": "scholar", "job_id": job["id"],
                                                    "status": job["status"], "poll": f"/api/jobs/{job['id']}"})
                        if jobs and jobs.running():
                            return self._send(409, {"error": "busy with another investigation — send "
                                                             "\"async\": true to queue this one"})
                except LLMError as e:
                    return self._send(502, {"error": str(e)})
            if path in ("/api/research", "/api/plan") and (body.get("question") or body.get("idea")) and researcher:
                try:
                    with brain:
                        r = researcher.investigate(body.get("question") or body["idea"],
                                                   body.get("depth", "standard"), "plan" if path == "/api/plan" else "research")
                except LLMError as e:
                    return self._send(502, {"error": str(e)})
                return self._send(200, {"from": "scholar", "report_id": r["report_id"],
                                        "markdown": r["markdown"], "data": r["data"]})
            self._send(404, {"error": "not found"})

        # ------------------------------------------------------- offline sync
        def _sync(self, body: dict):
            """A device reconnected: here's what it was asked while it couldn't reach Scholar."""
            items = body.get("items")
            if not isinstance(items, list):
                return self._send(400, {"error": 'send {"device": "...", "items": [{"kind": "ask|research", "text": "...", "at": "..."}]}'})
            with lock:
                rows = offline.receive(store, [i for i in items if isinstance(i, dict)], str(body.get("device") or "device"))
            if rows:
                threading.Thread(target=catch_up, daemon=True).start()
            return self._send(200, {"queued": len(rows), "say": offline.summary(rows)})

        # --------------------------------------------- OpenAI-compatible chat
        def _chat_completions(self, body: dict):
            """So Open WebUI (or any OpenAI-style app) can use Scholar as a model, next to Amo."""
            messages = [m for m in body.get("messages", []) if isinstance(m, dict)]
            text = lambda m: m["content"] if isinstance(m.get("content"), str) else " ".join(  # noqa: E731
                p.get("text", "") for p in m.get("content") or [] if isinstance(p, dict))
            last = text(messages[-1]) if messages else ""
            try:
                if last.lstrip().startswith("### Task:") and assistant is not None:
                    # Open WebUI's housekeeping (chat titles, tags): straight to the model, no tools or memory
                    with brain:
                        content = assistant.llm.chat([{"role": "user", "content": last}]).get("content", "")
                elif re.match(r"^\s*/?(research|plan)\b[:\s]", last, re.I) and jobs:
                    kind = "plan" if re.match(r"^\s*/?plan", last, re.I) else "research"
                    topic = re.sub(r"^\s*/?(research|plan)\b[:\s]*", "", last, flags=re.I)
                    job = jobs.submit(topic, "standard", kind, "webui")
                    content = (f"On it — investigation queued as job #{job['id']}. I'll post the report to the "
                               f"dashboard inbox when it's done (it can take a while on this machine).")
                elif assistant is None:
                    return self._send(503, {"error": "assistant not configured"})
                else:
                    with brain:
                        assistant.history = [{"role": m["role"], "content": text(m)} for m in messages[:-1]
                                             if m.get("role") in ("user", "assistant")][-30:]
                        content = assistant.ask(last, "")
            except LLMError as e:
                return self._send(502, {"error": {"message": str(e)}})
            cid, created = f"chatcmpl-{uuid.uuid4().hex[:12]}", int(time.time())
            if not body.get("stream"):
                return self._send(200, {"id": cid, "object": "chat.completion", "created": created, "model": MODEL_ID,
                                        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                                     "finish_reason": "stop"}],
                                        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            for delta, finish in (({"role": "assistant"}, None), ({"content": content}, None), ({}, "stop")):
                chunk = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": MODEL_ID,
                         "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")

        def _transcriptions(self):
            """OpenAI-style speech-to-text upload (multipart form with a 'file' field)."""
            if voice is None:
                return self._send(503, {"error": "voice not configured"})
            raw = self._raw()
            msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
                b"Content-Type: " + self.headers.get("Content-Type", "").encode() + b"\r\n\r\n" + raw)
            audio = next((part.get_payload(decode=True) for part in msg.iter_parts()
                          if part.get_param("name", header="content-disposition") == "file"), None) \
                if msg.is_multipart() else None
            if not audio:
                return self._send(400, {"error": "send the recording as multipart form field 'file'"})
            from .voice import VoiceUnavailable
            try:
                return self._send(200, {"text": voice.transcribe(audio)})
            except VoiceUnavailable as e:
                return self._send(503, {"error": str(e)})

    Handler.catch_up = staticmethod(catch_up)
    return Handler


MODEL_ID = "scholar"


class _Locked:
    """Run a shared researcher's investigation one at a time (single-connection setups)."""

    def __init__(self, researcher: Researcher, lock: threading.Lock):
        self.researcher, self.lock = researcher, lock

    def investigate(self, *a, **kw):
        with self.lock:
            return self.researcher.investigate(*a, **kw)


def _brief(s: dict | None) -> dict | None:
    if s is None:
        return None
    return {k: s[k] for k in ("id", "title", "url", "domain", "kind", "tier", "published", "fetched_at")}


def serve(store: Store, assistant: Assistant | None, researcher: Researcher | None, host: str, port: int,
          token: str = "", memory: ResearchMemory | None = None, jobs: Jobs | None = None, voice=None,
          web=None) -> None:
    handler = make_handler(store, assistant, researcher, token, memory, jobs, voice, web)
    httpd = ThreadingHTTPServer((host, port), handler)
    handler.sync_loop = offline.SyncLoop(handler.catch_up)  # catch up on queued work when the internet is back
    shown = "localhost" if host in ("0.0.0.0", "127.0.0.1") else host
    print(f"Scholar dashboard: http://{shown}:{port}   ·   API for the family: http://{host}:{port}/api  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
