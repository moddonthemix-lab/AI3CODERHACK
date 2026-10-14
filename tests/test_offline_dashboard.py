import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from builder import offline
from builder.db import Store
from builder.server import make_handler

from .test_cli_server import FakeAssistant


# --------------------------------------------------------------------------- offline
def test_offline_queues_questions_and_answers_them_when_back():
    db = Store()
    rows = offline.receive(db, [{"kind": "ask", "text": "What did we decide about the API port?"},
                                {"kind": "bogus", "text": "x"}, {"text": ""}], "pocket")
    assert [r["kind"] for r in rows] == ["ask"]                      # bogus/empty dropped

    # A question doesn't need the internet, so it's answered and lands in the boss's inbox.
    out = offline.process(db, lambda: False, lambda t, d: f"answer to {t}", None)
    assert out["done"] == [rows[0]["id"]]
    assert "answer to What did we decide" in db.inbox()[0]["text"]
    assert db.pending() == []                                        # nothing left parked


# --------------------------------------------------------------------------- server
def _serve(store, assistant=None):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store, assistant))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def _get(url):
    with urllib.request.urlopen(url) as r:
        return r.status, r.read()


def test_openai_compatible_chat_for_open_webui():
    httpd, base = _serve(Store(), FakeAssistant())
    try:
        body = _post(base + "/v1/chat/completions",
                     {"model": "builder", "messages": [{"role": "user", "content": "how do I hash a password?"}]})
        assert body["model"] == "builder"
        assert "reply to" in body["choices"][0]["message"]["content"]
        models = _post.__self__ if False else json.loads(urllib.request.urlopen(base + "/v1/models").read())
        assert models["data"][0]["id"] == "builder"
    finally:
        httpd.shutdown()


def test_dashboard_page_is_served():
    httpd, base = _serve(Store(), FakeAssistant())
    try:
        status, page = _get(base + "/")
        assert status == 200 and b"<title>Builder Dashboard</title>" in page
        assert b"/api/brief" in page
    finally:
        httpd.shutdown()


def test_brief_is_builder_shaped():
    store = Store()
    store.add_review("Hardcoded secret", "high", "use env vars")
    httpd, base = _serve(store, FakeAssistant())
    try:
        brief = json.loads(urllib.request.urlopen(base + "/api/brief").read())
        assert brief["agent"] == "builder" and brief["reviews"] == 1 and brief["high"] == 1
    finally:
        httpd.shutdown()
