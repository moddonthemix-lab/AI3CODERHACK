import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

from builder import offline
from builder.config import Config
from builder.db import Store
from builder.research import Researcher
from builder.server import make_handler
from builder.tools import Toolbox
from builder.web import OfflineError, Web

from .conftest import FakeWeb, RoutingLLM
from .test_research import full_llm
from .test_voice import FakeVoice, _wav


def test_forced_offline_fails_fast():
    w = Web(offline=True)
    assert w.online() is False
    t = time.monotonic()
    try:
        w.search("anything")
        raise AssertionError("expected OfflineError")
    except OfflineError:
        pass
    assert time.monotonic() - t < 0.5


def test_offline_research_uses_the_library_and_queues_a_recheck(tb, config, tmp_path):
    (tmp_path / "notes.md").write_text("# Sneaker notes\n\nThe Bureau reports that resale sneaker prices rose 12 percent "
                                       "in 2025 according to the annual consumer survey of footwear markets.\n")
    tb.memory.ingest(tmp_path / "notes.md")
    tb.web.online = lambda: False
    llm = full_llm()
    llm.handlers["extract"] = lambda p: {"facts": [{"fact": "prices rose 12%", "answers": [],
                                                    "quote": "resale sneaker prices rose 12 percent in 2025"}]}
    llm.handlers["synth"] = lambda p: {"answer": "Up 12% [S1].", "key_findings": [
        {"claim": "Resale prices rose 12% in 2025", "source_ids": [1], "confidence": 0.8}]}
    r = Researcher(tb, llm, config).investigate("Are sneaker resale prices rising?", "quick")
    assert all(kind == "library" for kind, _ in tb.web.searches) or not tb.web.searches
    assert r["data"]["offline"] is True and "📴 **Offline:**" in r["markdown"]
    assert r["data"]["findings"] and r["data"]["sources"][0]["domain"] == "my library"
    pending = tb.db.pending()
    assert pending[0]["origin"] == "offline-recheck" and pending[0]["ref"] == f"investigation:{r['investigation_id']}"


def test_process_answers_now_and_waits_for_the_internet_for_research():
    db = Store()
    rows = offline.receive(db, [{"kind": "ask", "text": "What did we learn about lithium?"},
                                {"kind": "research", "text": "E-bike battery resale", "at": "2026-10-13 09:00:00"},
                                {"kind": "bogus", "text": "x"}, {"text": ""}], "pocket")
    assert [r["kind"] for r in rows] == ["ask", "research"]
    assert offline.summary(rows).startswith("While I was offline you asked me “What did we learn about lithium?” "
                                            "and wanted research on “E-bike battery resale”")
    started = []
    out = offline.process(db, lambda: False, lambda t, d: f"answer to {t}", lambda *a: started.append(a) or "job:1")
    assert out == {"online": False, "done": [rows[0]["id"]], "waiting": 1} and not started
    assert "answer to What did we learn" in db.inbox()[0]["text"]
    out = offline.process(db, lambda: True, None, lambda *a: started.append(a) or "job:1")
    assert out["done"] == [rows[1]["id"]] and started[0][:2] == ("research", "E-bike battery resale")
    assert db.pending() == [] and db.pending(include_done=True)[1]["result"] == "job:1"


class FakeAssistant:
    def __init__(self):
        self.history, self.asked = [], []

        class L:
            def chat(self, messages, **kw):
                return {"content": "Chat title"}
        self.llm = L()

    def ask(self, text, from_agent=""):
        self.asked.append((list(self.history), text))
        return f"Answer [S1] to: {text}"


def _serve(store, assistant=None, researcher=None, voice=None, web=None):
    handler = make_handler(store, assistant, researcher, voice=voice, web=web)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}", handler


def _post(url, body, raw=False, headers=None):
    req = urllib.request.Request(url, data=body if raw else json.dumps(body).encode(), method="POST",
                                 headers=headers or {"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return r.read().decode()


def test_openai_compatible_chat_for_open_webui(tmp_path):
    a = FakeAssistant()
    store = Store()
    researcher = Researcher(Toolbox(store, FakeWeb()), full_llm(), Config(home=tmp_path))
    httpd, base, _ = _serve(store, a, researcher)
    try:
        models = json.loads(urllib.request.urlopen(base + "/v1/models").read())
        assert models["data"][0]["id"] == "scholar"
        msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"},
                {"role": "user", "content": [{"type": "text", "text": "what about lithium?"}]}]
        r = json.loads(_post(base + "/v1/chat/completions", {"model": "scholar", "messages": msgs}))
        assert r["choices"][0]["message"]["content"] == "Answer [S1] to: what about lithium?"
        assert a.asked[-1][0] == msgs[:2]  # conversation history comes from the app, not stale memory
        stream = _post(base + "/v1/chat/completions", {"messages": msgs, "stream": True})
        chunks = [json.loads(line[6:]) for line in stream.splitlines() if line.startswith("data: {")]
        assert "".join(c["choices"][0]["delta"].get("content", "") for c in chunks).startswith("Answer")
        assert stream.strip().endswith("data: [DONE]")
        task = json.loads(_post(base + "/v1/chat/completions", {"messages": [{"role": "user", "content": "### Task: title"}]}))
        assert task["choices"][0]["message"]["content"] == "Chat title"
        res = json.loads(_post(base + "/v1/chat/completions", {"messages": [{"role": "user", "content": "research: sneaker resale"}]}))
        assert "queued as job #" in res["choices"][0]["message"]["content"]
    finally:
        httpd.shutdown()


def test_device_sync_dashboard_and_voice_upload(tmp_path):
    a = FakeAssistant()
    store = Store()
    web = Web(offline=True)
    httpd, base, handler = _serve(store, a, voice=FakeVoice(), web=web)
    try:
        page = urllib.request.urlopen(base + "/").read().decode()
        assert "<title>Scholar Dashboard</title>" in page and "/api/brief" in page
        out = json.loads(_post(base + "/api/sync", {"device": "pocket", "items": [
            {"kind": "ask", "text": "How big is the e-bike battery market?"}]}))
        assert out["queued"] == 1 and out["say"].startswith("While I was offline you asked me")
        for _ in range(100):  # answered in the background, even with the internet off (the brain is local)
            if store.inbox():
                break
            time.sleep(0.02)
        assert "e-bike battery market" in store.inbox()[0]["text"]
        b = json.loads(urllib.request.urlopen(base + "/api/brief").read())
        assert b["online"] is False and b["unread"] == 1 and b["news"].startswith("You asked")
        assert len(json.dumps(b)) < 1024
        _post(base + "/api/inbox/read", {})
        assert json.loads(urllib.request.urlopen(base + "/api/inbox?unread=1").read()) == []
        boundary = "XyZ"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nwhisper-1\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\n"
                f"Content-Type: audio/wav\r\n\r\n").encode() + _wav() + f"\r\n--{boundary}--\r\n".encode()
        t = json.loads(_post(base + "/v1/audio/transcriptions", body, raw=True,
                             headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}))
        assert t == {"text": "how big is the battery market"}
        req = urllib.request.Request(base + "/v1/audio/speech", data=json.dumps({"input": "Hello"}).encode(), method="POST")
        with urllib.request.urlopen(req) as r:
            assert r.headers["Content-Type"] == "audio/wav"
    finally:
        httpd.shutdown()
