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

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store, FakeAssistant(cfg), token=""))
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


def test_preview_serves_a_built_site_in_scope_and_refuses_outside(tmp_path):
    import urllib.request
    allowed = tmp_path / "Projects"
    site = allowed / "site"
    site.mkdir(parents=True)
    (site / "index.html").write_text("<h1>My Site</h1>", encoding="utf-8")
    cfg = Config(home=tmp_path, code_dirs=[str(allowed)])

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Store(cfg.db_path), FakeAssistant(cfg), token=""))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/preview?path=" + str(site)) as r:  # a folder -> its index.html
            assert r.status == 200 and b"My Site" in r.read()
            assert r.headers.get("Content-Type", "").startswith("text/html")
        try:
            urllib.request.urlopen(base + "/preview?path=/etc/hosts")
            raise AssertionError("expected 403")
        except urllib.error.HTTPError as e:
            assert e.code == 403                                   # outside the workspace -> refused
    finally:
        httpd.shutdown()


def test_learn_doc_upload_list_and_forget(tmp_path):
    import base64
    from builder.memory import Memory
    cfg = Config(home=tmp_path, code_dirs=[str(tmp_path)])
    store = Store(cfg.db_path)
    memory = Memory(store, None, cfg)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store, FakeAssistant(cfg), token="", memory=memory))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        content = b"Pong needs two paddles and a bouncing ball. Space invaders has marching aliens."
        status, body = _req(base + "/api/learn-doc", "POST",
                            {"name": "guide.txt", "b64": base64.b64encode(content).decode()})
        assert status == 201 and body["chunks"] >= 1 and body["source"] == "guide.txt"

        status, docs = _req(base + "/api/refs")
        assert status == 200 and any(d["source"] == "guide.txt" for d in docs)

        # the uploaded passage is now recallable for a related build
        assert "aliens" in memory.reference_note("space invaders").lower()

        status, body = _req(base + "/api/refs/forget", "POST", {"source": "guide.txt"})
        assert status == 200 and body["removed"] >= 1
        _, docs = _req(base + "/api/refs")
        assert docs == []
    finally:
        httpd.shutdown()


def test_offense_mode_dashboard_toggle_is_password_gated(tmp_path):
    from builder.memory import Memory
    cfg = Config(home=tmp_path, code_dirs=[str(tmp_path)], lab_password="s3cret")
    store = Store(cfg.db_path)
    memory = Memory(store, None, cfg)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store, FakeAssistant(cfg), token="", memory=memory))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        status, body = _req(base + "/api/lab")
        assert status == 200 and body["enabled"] is False and body["has_password"] is True

        # wrong password refused; missing ownership confirmation refused
        assert _req(base + "/api/lab/on", "POST", {"target": "vm", "i_own_this": True, "password": "x"})[0] == 403
        assert _req(base + "/api/lab/on", "POST", {"target": "vm", "i_own_this": False, "password": "s3cret"})[0] == 400

        # correct password + confirmation enables it
        status, body = _req(base + "/api/lab/on", "POST",
                            {"target": "my-vm", "kind": "vm", "i_own_this": True, "password": "s3cret"})
        assert status == 200 and body["enabled"] is True
        assert _req(base + "/api/lab")[1]["enabled"] is True

        # turning off needs the password too
        assert _req(base + "/api/lab/off", "POST", {"password": "s3cret"})[0] == 200
        assert _req(base + "/api/lab")[1]["enabled"] is False
    finally:
        httpd.shutdown()


def test_offense_mode_needs_a_password_configured(tmp_path):
    cfg = Config(home=tmp_path, code_dirs=[str(tmp_path)])   # no lab_password set
    store = Store(cfg.db_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store, FakeAssistant(cfg), token=""))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        assert _req(base + "/api/lab")[1]["has_password"] is False
        # with no password configured, the dashboard can't toggle it at all
        assert _req(base + "/api/lab/on", "POST", {"target": "x", "i_own_this": True, "password": ""})[0] == 403
    finally:
        httpd.shutdown()
