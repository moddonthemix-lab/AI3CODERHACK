"""Part 6: Builder's HTTP review endpoint — scope-checked, read-only, offline (no model needed)."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from builder.config import Config
from builder.db import Store
from builder.server import make_handler


class FakeAssistant:
    """Just enough for the review endpoint: it reads assistant.config for the allowed folders."""

    def __init__(self, config):
        self.config = config


def _req(url, method="GET", body=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_review_endpoint_scans_in_scope_and_refuses_outside(tmp_path):
    allowed = tmp_path / "Projects"
    proj = allowed / "app"
    proj.mkdir(parents=True)
    (proj / "main.py").write_text('password = "leakyleaky"\n', encoding="utf-8")
    cfg = Config(home=tmp_path, code_dirs=[str(allowed)])
    store = Store(cfg.db_path)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store, FakeAssistant(cfg), None, token=""))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        status, body = _req(base + "/api/review", "POST", {"path": str(proj)})
        assert status == 200 and body["from"] == "builder"
        assert any(f["issue"] == "Hardcoded secret in source" and f["severity"] == "high"
                   for f in body["findings"])

        status2, _ = _req(base + "/api/review", "POST", {"path": str(tmp_path / "outside")})
        assert status2 == 403                                      # a path outside the allowed folders is refused
    finally:
        httpd.shutdown()
