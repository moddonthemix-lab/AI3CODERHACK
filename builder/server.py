"""Tiny JSON HTTP API (standard library only).

How the rest of the AI family — and the Family Hub forum — talks to Builder: structured requests in,
structured results out. Set BUILDER_API_TOKEN to require ``Authorization: Bearer <token>``.

Endpoints: ``GET /api/agent`` (identity card), ``GET /api/health``, ``GET /api/brief`` (tiny status for
ESP32 screens), ``POST /api/ask`` (a question), ``POST /api/build`` (build + test a project, in scope,
on the local coder model), ``POST /api/review`` (read-only defensive audit, in scope), plus the dashboard,
an OpenAI-compatible ``/v1/chat/completions`` for apps like Open WebUI, offline sync, and optional voice.
"""

from __future__ import annotations

import email.parser
import email.policy
import json
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import offline
from .agent import CARD
from .assistant import Assistant
from .db import LESSON_KINDS, Store
from .llm import LLMError
from .memory import Memory

MODEL_ID = CARD["id"]
# Lessons change how Builder works, so only the boss can give them — not other agents, not web pages.
BOSS = ("", "boss", "user")


def _cut(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def brief(store: Store, online: bool | None = None) -> dict:
    """A status summary small enough for a microcontroller screen (well under 1 KB)."""
    stats = store.stats()
    latest = store.inbox(unread_only=True, limit=1)
    return {
        "agent": CARD["id"], "role": CARD["family_role"],
        "reviews": stats["reviews"], "high": stats["reviews_by_severity"]["high"],
        "lessons": stats["lessons"], "lab_targets": stats["lab_targets"],
        "online": online, "pending": stats["pending"], "unread": stats["unread"],
        "news": _cut(latest[0]["text"], 120) if latest else None,
    }


def make_handler(store: Store, assistant: Assistant | None, token: str = "",
                 memory: Memory | None = None, voice=None, web=None, dashboard: Path | None = None):
    lock = threading.Lock()    # quick database work (reads, small writes)
    brain = threading.Lock()   # one conversation with the model at a time — a CPU can't do two
    online = (lambda: web.online()) if web is not None and hasattr(web, "online") else (lambda: None)
    dashboard = dashboard or Path(__file__).with_name("static") / "index.html"
    cfg = getattr(assistant, "config", None)

    def ask(text: str, sender: str = "") -> str:
        with brain:
            return assistant.ask(text, sender) if assistant is not None else ""

    def catch_up() -> dict:
        return offline.process(store, lambda: online() is not False, ask, None)

    class Handler(BaseHTTPRequestHandler):
        server_version = "Builder/0.1"

        def log_message(self, fmt, *args):
            pass

        def _send(self, status: int, body) -> None:
            data = json.dumps(body, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_bytes(self, status: int, data: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
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

        def _raw(self, limit: int = 20 * 2**20) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            if length > limit:
                raise ValueError("upload too large")
            return self.rfile.read(length) if length else b""

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
            return body

        def _voice(self, path: str, text: str | None = None):
            from .voice import VoiceUnavailable, speakable, to_rate, wav_info
            if voice is None:
                return self._send(503, {"error": "voice not configured"})
            try:
                if path == "/api/speak":
                    body = {} if text is not None else self._body()
                    text = text if text is not None else body.get("text", "")
                    if not text:
                        return self._send(400, {"error": "send {\"text\": \"...\"}"})
                    wav = voice.synthesize(text)
                    if str(body.get("rate", "")).isdigit():
                        wav = to_rate(wav, int(body["rate"]))
                    return self._send_bytes(200, wav, "audio/wav")
                audio = self._raw()
                try:
                    info = wav_info(audio)
                except Exception:  # noqa: BLE001
                    return self._send(400, {"error": "send a WAV recording (16 kHz mono works best)"})
                text = voice.transcribe(audio)
                reply = ask(text, self.headers.get("X-From", "")) if (text and assistant is not None) else ""
                return self._send(200, {"heard": text, "reply": reply, "speak": speakable(reply), "audio": info})
            except VoiceUnavailable as e:
                return self._send(503, {"error": str(e)})
            except LLMError as e:
                return self._send(502, {"error": str(e)})

        def do_GET(self):
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                try:
                    return self._send_bytes(200, dashboard.read_bytes(), "text/html; charset=utf-8")
                except OSError:
                    return self._send(404, {"error": "dashboard file missing"})
            if not self._authorized():
                return
            qs = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path == "/api/brief":
                is_online = online()
                with lock:
                    b = brief(store, is_online)
                b["space"] = (cfg.code_dirs[0] if cfg and cfg.code_dirs else "")
                return self._send(200, b)
            if url.path == "/v1/models":
                return self._send(200, {"object": "list", "data": [{"id": MODEL_ID, "object": "model",
                                        "created": 0, "owned_by": "local", "name": CARD["name"]}]})
            with lock:
                if url.path == "/api/health":
                    return self._send(200, {"ok": True, "agent": CARD["id"]})
                if url.path == "/api/stats":
                    return self._send(200, store.stats())
                if url.path == "/api/agent":
                    return self._send(200, {**CARD, "store": store.stats()})
                if url.path == "/api/reviews":
                    return self._send(200, store.reviews(qs.get("q", ""), qs.get("severity"),
                                                         int(qs.get("limit", 50))))
                if url.path == "/api/builds":
                    return self._send(200, store.builds(qs.get("q", ""), int(qs.get("limit", 25))))
                if url.path == "/api/lessons":
                    return self._send(200, store.lessons())
                if url.path == "/api/inbox":
                    return self._send(200, store.inbox(qs.get("unread") in ("1", "true"), int(qs.get("limit", 30))))
                if url.path == "/api/pending":
                    return self._send(200, store.pending(qs.get("all") in ("1", "true")))
                if url.path == "/api/lab":
                    return self._send(200, {"targets": store.lab_targets(True), "log": store.safety_events(20)})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._authorized():
                return
            path = urlparse(self.path).path
            if path in ("/api/speak", "/api/listen"):
                try:
                    return self._voice(path)
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
            if path == "/v1/audio/transcriptions":
                try:
                    return self._transcriptions()
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
                    return self._send(200, {"from": CARD["id"], "reply": ask(body["text"], sender)})
                except LLMError as e:
                    return self._send(502, {"error": str(e)})
            if path == "/api/review" and body.get("path"):
                # Defensive review: read-only pattern scan, offline, inside Builder's allowed folders only.
                if cfg is None:
                    return self._send(503, {"error": "builder not configured for review"})
                from . import audit
                from .projects import ScopeError
                try:
                    findings = audit.scan_path(body["path"], cfg)
                except ScopeError as e:
                    return self._send(403, {"error": str(e)})
                return self._send(200, {"from": CARD["id"], "path": body["path"],
                                        "findings": [f.as_dict() for f in findings]})
            if path == "/api/build" and body.get("task") and body.get("dir"):
                # Build a small project and run its tests, inside scope, on the local coder model.
                if assistant is None or cfg is None:
                    return self._send(503, {"error": "builder not configured for build"})
                from .buildagent import make_propose
                from .projects import Project, ScopeError, build_and_test
                try:
                    proj = Project(body["dir"], cfg)
                except ScopeError as e:
                    return self._send(403, {"error": str(e)})
                blm = assistant.llm
                if not blm.available():
                    return self._send(503, {"error": "local coder model not reachable (is Ollama running?)"})

                def _gen(prompt):
                    sysmsg = ("You are Builder, a careful coder. You write small, correct, self-contained "
                              "projects and the tests that prove them, and never touch the network or files "
                              "outside the project.")
                    return blm.chat([{"role": "system", "content": sysmsg},
                                     {"role": "user", "content": prompt}]).get("content", "")

                try:
                    with brain:
                        result = build_and_test(proj, make_propose(_gen, body["task"], log=lambda m: None),
                                                max_tries=int(body.get("tries", 3)))
                except LLMError as e:
                    return self._send(502, {"error": str(e)})
                return self._send(200, {"from": CARD["id"], "ok": result["ok"], "attempts": result["attempts"],
                                        "diff": result["diff"], "dir": str(proj.root)})
            with lock:
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
            self._send(404, {"error": "not found"})

        # ------------------------------------------------------- offline sync
        def _sync(self, body: dict):
            items = body.get("items")
            if not isinstance(items, list):
                return self._send(400, {"error": 'send {"device": "...", "items": [{"kind": "ask", "text": "...", "at": "..."}]}'})
            with lock:
                rows = offline.receive(store, [i for i in items if isinstance(i, dict)], str(body.get("device") or "device"))
            if rows:
                threading.Thread(target=catch_up, daemon=True).start()
            return self._send(200, {"queued": len(rows), "say": offline.summary(rows)})

        # --------------------------------------------- OpenAI-compatible chat
        def _chat_completions(self, body: dict):
            messages = [m for m in body.get("messages", []) if isinstance(m, dict)]
            text = lambda m: m["content"] if isinstance(m.get("content"), str) else " ".join(  # noqa: E731
                p.get("text", "") for p in m.get("content") or [] if isinstance(p, dict))
            last = text(messages[-1]) if messages else ""
            try:
                if assistant is None:
                    return self._send(503, {"error": "assistant not configured"})
                if last.lstrip().startswith("### Task:"):
                    with brain:
                        content = assistant.llm.chat([{"role": "user", "content": last}]).get("content", "")
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


def serve(store: Store, assistant: Assistant | None, host: str, port: int, token: str = "",
          memory: Memory | None = None, voice=None, web=None) -> None:
    handler = make_handler(store, assistant, token, memory, voice, web)
    try:
        httpd = ThreadingHTTPServer((host, port), handler)
    except OSError as e:
        import errno
        if e.errno in (errno.EADDRINUSE, 48):
            print(f"Port {port} is already in use — another app (or an earlier `builder serve`) is on it.\n"
                  f"  Try another port:   builder serve --port {port + 13}\n"
                  f"  Or set BUILDER_API_PORT in your .env.\n"
                  f"  To see what's using it:   lsof -i :{port}", file=sys.stderr)
            raise SystemExit(1) from None
        raise
    handler.sync_loop = offline.SyncLoop(handler.catch_up)
    shown = "localhost" if host in ("0.0.0.0", "127.0.0.1") else host
    print(f"Builder dashboard: http://{shown}:{port}   ·   API for the family: http://{host}:{port}/api  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
