import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from builder.cli import main
from builder.config import Config
from builder.db import Store
from builder.server import make_handler


class FakeAssistant:
    """Enough of an assistant for the server tests: a config (for /api/review) and a canned reply."""

    def __init__(self, config=None):
        self.config = config
        self.history = []

        class L:
            def chat(self, messages, **kw):
                return {"content": "Chat reply"}

            def available(self):
                return False
        self.llm = L()

    def ask(self, text, from_agent=""):
        return f"reply to: {text}"


def _serve(store, assistant=None, token=""):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store, assistant, token))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _req(url, method="GET", body=None, token=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# --------------------------------------------------------------------------- CLI
def test_cli_memory_feedback_and_reviews(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BUILDER_HOME", str(tmp_path))
    assert main(["remember", "Boss", "prefers", "Python", "--topic", "stack"]) == 0
    assert main(["feedback", "Always", "add", "a", "test", "--kind", "preference"]) == 0
    assert main(["lessons"]) == 0
    assert main(["card"]) == 0
    assert main(["recall", "Python"]) == 0
    out = capsys.readouterr().out
    assert "lesson #1" in out and '"family_role": "Coder & security engineer"' in out
    assert (tmp_path / "builder.db").exists()


# --------------------------------------------------------------------------- API
def test_api_health_card_and_review(tmp_path):
    allowed = tmp_path / "Projects"
    proj = allowed / "app"
    proj.mkdir(parents=True)
    (proj / "main.py").write_text('password = "leakyleaky"\n', encoding="utf-8")
    cfg = Config(home=tmp_path, code_dirs=[str(allowed)])
    store = Store(cfg.db_path)

    httpd, base = _serve(store, FakeAssistant(cfg))
    try:
        assert _req(base + "/api/health") == (200, {"ok": True, "agent": "builder"})
        card = _req(base + "/api/agent")[1]
        assert card["id"] == "builder" and card["family_role"] == "Coder & security engineer"
        assert "store" in card

        status, body = _req(base + "/api/ask", "POST", {"text": "hi", "from": "amo"})
        assert status == 200 and body["from"] == "builder" and "reply to" in body["reply"]

        status, body = _req(base + "/api/review", "POST", {"path": str(proj)})
        assert status == 200 and any(f["issue"] == "Hardcoded secret in source" for f in body["findings"])
        assert _req(base + "/api/review", "POST", {"path": str(tmp_path / "outside")})[0] == 403
    finally:
        httpd.shutdown()


def test_api_requires_token_when_set():
    store = Store()
    httpd, base = _serve(store, FakeAssistant(), token="secret")
    try:
        assert _req(base + "/api/health")[0] == 401                  # no token -> refused
        assert _req(base + "/api/health", token="secret")[0] == 200  # right token -> ok
    finally:
        httpd.shutdown()
