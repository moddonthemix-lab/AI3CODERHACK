import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from builder.llm import Ollama, parse_json

SCHEMA = {"type": "object", "properties": {"ok": {"type": "integer"}}, "required": ["ok"]}


def _fake_ollama(reply: dict, seen: list, fail_on_think: bool = False):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            if fail_on_think and "think" in body:
                data = b'{"error":"model does not support thinking"}'
                self.send_response(400)
            else:
                data = json.dumps({"message": {"role": "assistant", "content": json.dumps(reply)},
                                   "prompt_eval_count": 1200, "eval_count": 80, "load_duration": 0,
                                   "prompt_eval_duration": 4e9, "eval_duration": 2e9}).encode()
                self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_ollama_schema_think_and_stats():
    seen = []
    srv, url = _fake_ollama({"ok": 1}, seen)
    try:
        llm = Ollama(url, "qwen3:4b", num_ctx=8192)
        assert parse_json(llm.chat([{"role": "user", "content": "x"}], schema=SCHEMA, think=False)["content"]) == {"ok": 1}
        body = seen[-1]
        assert body["format"] == SCHEMA and body["think"] is False
        assert body["options"]["num_ctx"] == 8192 and body["options"]["num_predict"] > 0
        assert llm.last["prompt_tokens"] == 1200 and llm.last["read_tps"] == 300.0 and llm.last["write_tps"] == 40.0
        Ollama(url, "gpt-oss:20b").chat([{"role": "user", "content": "x"}], json_mode=True)
        assert seen[-1]["think"] == "low" and seen[-1]["format"] == "json"
        Ollama(url, "llama3.1:8b").chat([{"role": "user", "content": "x"}])
        assert "think" not in seen[-1] and "format" not in seen[-1]
    finally:
        srv.shutdown()


def test_ollama_retries_without_think_when_unsupported():
    seen = []
    srv, url = _fake_ollama({"ok": 1}, seen, fail_on_think=True)
    try:
        llm = Ollama(url, "qwen3:4b")
        llm.chat([{"role": "user", "content": "x"}], json_mode=True)
        assert "think" in seen[0] and "think" not in seen[1]
        llm.chat([{"role": "user", "content": "x"}], json_mode=True)
        assert "think" not in seen[2]  # remembered
    finally:
        srv.shutdown()


def test_parse_json_ignores_think_tags():
    assert parse_json('<think>hmm {"no": 1}</think>{"yes": 2}') == {"yes": 2}


def test_one_model_in_memory_at_a_time():
    seen = []
    srv, url = _fake_ollama({"ok": 1}, seen)
    try:
        thinker, reader = Ollama(url, "qwen2.5-coder:7b"), Ollama(url, "qwen2.5-coder:3b")
        for llm in (thinker, thinker, reader, thinker):
            llm.chat([{"role": "user", "content": "x"}], json_mode=True)
        unloads = [(b["model"], b["keep_alive"]) for b in seen if "keep_alive" in b]
        assert unloads == [("qwen2.5-coder:7b", 0), ("qwen2.5-coder:3b", 0)]
        seen.clear()
        relaxed = Ollama(url, "qwen2.5-coder:14b", one_at_a_time=False)
        relaxed.chat([{"role": "user", "content": "x"}], json_mode=True)
        assert not any("keep_alive" in b for b in seen)
    finally:
        srv.shutdown()


def test_after_a_restart_his_other_model_is_unloaded_but_not_other_apps_models(monkeypatch):
    import builder.llm as L
    seen = []
    srv, url = _fake_ollama({"ok": 1}, seen)
    try:
        monkeypatch.setattr(L, "_last_model", {})
        monkeypatch.setattr(L, "loaded_models", lambda u, timeout=5: [{"name": "qwen2.5-coder:7b", "size_gb": 6.8},
                                                                      {"name": "amo-model:latest", "size_gb": 5.0}])
        Ollama(url, "qwen2.5-coder:7b")  # Builder's thinker, still loaded from the last run
        Ollama(url, "qwen2.5-coder:3b").chat([{"role": "user", "content": "x"}], json_mode=True)
        assert [b["model"] for b in seen if "keep_alive" in b] == ["qwen2.5-coder:7b"]  # not Amo's
    finally:
        srv.shutdown()


def test_bench_recommends_a_working_model_not_a_broken_one():
    from builder.bench import recommend
    rows = [{"model": "broken", "ok": False, "score": 0, "sec": 5.0},
            {"model": "fast-ok", "ok": True, "score": 100, "sec": 12.0},
            {"model": "slow-ok", "ok": True, "score": 100, "sec": 40.0}]
    out = recommend(rows)
    assert "broken" not in out
    assert "reader  (BUILDER_FAST_MODEL): fast-ok" in out   # fastest that works is the reader
    assert "BUILDER_MODEL" in out
