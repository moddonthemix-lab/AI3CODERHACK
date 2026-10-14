import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from builder.llm import Ollama, parse_json
from builder.research import SCHEMAS, Researcher

from .conftest import RoutingLLM
from .test_research import full_llm


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
        assert parse_json(llm.chat([{"role": "user", "content": "x"}], schema=SCHEMAS["support"], think=False)["content"]) == {"ok": 1}
        body = seen[-1]
        assert body["format"] == SCHEMAS["support"] and body["think"] is False
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


class Recorder(RoutingLLM):
    def __init__(self, name, **kw):
        super().__init__(**kw)
        self.name = name
        self.num_ctx = 8192


def test_fast_and_smart_models_split_the_work(tb, config):
    from .test_research import _extract, _gaps, _plan, _skeptic_queries, _skeptic_read, _synth
    smart = Recorder("smart", plan=_plan, synth=_synth, gaps=_gaps, skeptic_queries=_skeptic_queries,
                     support=lambda p: {"checks": []})
    fast = Recorder("fast", extract=_extract, skeptic_read=_skeptic_read)
    Researcher(tb, smart, config, fast_llm=fast).investigate("Are sneaker resale prices rising?", "standard")
    assert set(fast.kinds()) == {"extract", "skeptic_read"}
    assert {"plan", "gaps", "synth", "skeptic_queries", "support"} <= set(smart.kinds())
    assert all(s is not None and s.get("type") == "object" for s in smart.schemas + fast.schemas)


def test_fact_check_downgrades_unsupported_claims(tb, config):
    def support(prompt):  # one check per finding, in order; the ids it echoes are deliberately wrong
        import re
        claims = re.findall(r"^F\d+: (.+)$", prompt, re.M)
        return {"checks": [{"finding_id": 7, "verdict": "unsupported" if c.startswith("Market is") else "supported",
                            "reason": "quote is about a different market"} for c in claims]}
    llm = full_llm()
    llm.handlers["support"] = support
    d = Researcher(tb, llm, config).investigate("Are sneaker resale prices rising?", "quick")["data"]
    market = next(f for f in d["findings"] if f["claim"] == "Market is ~$10B")
    assert market["status"] == "assumption" and market["confidence"] <= 0.3  # straight to assumption
    assert any(c.startswith("evidence does not support the claim") for c in market["checks"])


def test_window_respects_budget_and_keeps_relevant_parts(tb, config):
    r = Researcher(tb, RoutingLLM(), config)
    filler = "Unrelated paragraph about gardening and weather patterns in the spring season. " * 40
    text = "Title and abstract of the report.\n\n" + filler + "\n\nKEY: resale sneaker prices rose 12 percent.\n\n" + filler
    w = r.window({"id": 1, "text": text}, {"resale", "sneaker", "prices"}, budget=2500)
    assert len(w) <= 2500 and "Title and abstract" in w and "KEY: resale sneaker prices rose" in w


def test_bench_never_recommends_a_model_that_fell_for_the_trap():
    from builder.bench import recommend
    rows = [{"model": "tiny", "score": 80, "honest": 67, "sec_per_read": 10},
            {"model": "mid", "score": 77, "honest": 100, "sec_per_read": 30},
            {"model": "big", "score": 83, "honest": 100, "sec_per_read": 60}]
    out = recommend(rows)
    assert "tiny" not in out and "big" in out and "reader  (SCHOLAR_FAST_MODEL): mid" in out


def test_thinking_only_on_the_fact_check_by_default(tb, config):
    from .test_research import _extract, _gaps, _plan, _skeptic_queries, _skeptic_read, _synth
    seen = []

    class ThinkSpy(Recorder):
        def chat(self, messages, tools=None, json_mode=False, schema=None, think=None, max_tokens=None):
            out = super().chat(messages, tools, json_mode, schema, think)
            seen.append((self.kinds()[-1], think))
            return out
    llm = ThinkSpy("one", plan=_plan, extract=_extract, synth=_synth, gaps=_gaps, skeptic_queries=_skeptic_queries,
                   skeptic_read=_skeptic_read, support=lambda p: {"checks": []})
    Researcher(tb, llm, config).investigate("Are sneaker resale prices rising?", "quick")
    assert {k for k, t in seen if t} == {"support"}
    config.think = "off"
    seen.clear()
    Researcher(tb, llm, config).investigate("Is sneaker resale growing?", "quick")
    assert not any(t for _, t in seen)


def test_one_model_in_memory_at_a_time():
    seen = []
    srv, url = _fake_ollama({"ok": 1}, seen)
    try:
        thinker, reader = Ollama(url, "qwen3:8b"), Ollama(url, "qwen3:4b-instruct")
        for llm in (thinker, thinker, reader, thinker):
            llm.chat([{"role": "user", "content": "x"}], json_mode=True)
        unloads = [(b["model"], b["keep_alive"]) for b in seen if "keep_alive" in b]
        # thinker twice (no switch), then reader (drop the thinker), then thinker (drop the reader)
        assert unloads == [("qwen3:8b", 0), ("qwen3:4b-instruct", 0)]
        seen.clear()
        relaxed = Ollama(url, "qwen3:14b", one_at_a_time=False)
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
        monkeypatch.setattr(L, "loaded_models", lambda u, timeout=5: [{"name": "qwen3:8b", "size_gb": 6.8},
                                                                      {"name": "amo-model:latest", "size_gb": 5.0}])
        Ollama(url, "qwen3:8b")  # Scholar's thinker, still loaded from the last run
        Ollama(url, "qwen3:4b-instruct").chat([{"role": "user", "content": "x"}], json_mode=True)
        assert [b["model"] for b in seen if "keep_alive" in b] == ["qwen3:8b"]  # not Amo's
    finally:
        srv.shutdown()
