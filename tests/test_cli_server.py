import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from builder.cli import main
from builder.config import Config
from builder.db import Store
from builder.research import Researcher
from builder.server import make_handler
from builder.tools import Toolbox

from .conftest import FakeWeb, RoutingLLM


def test_cli_library_roundtrip(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SCHOLAR_HOME", str(tmp_path))
    assert main(["remember", "Boss", "likes", "sneakers", "--topic", "interests"]) == 0
    assert main(["finding", "add", "Resale", "margins", "are", "thin", "--status", "reported", "--source", "3"]) == 0
    assert main(["finding", "update", "1", "--status", "disputed", "--reason", "new survey disagrees"]) == 0
    assert main(["finding", "show", "1"]) == 0
    assert main(["calc", "(320-210)/320*100"]) == 0
    assert main(["economics", "--price", "45", "--cost", "12", "--fees-pct", "13"]) == 0
    assert main(["card"]) == 0
    assert main(["calc", "open('x')"]) == 1
    out = capsys.readouterr().out
    assert "F1 is now disputed" in out and "new survey disagrees" in out and "34.375" in out
    assert '"profit_per_unit": 27.15' in out and '"family_role": "Coder & security engineer"' in out
    assert (tmp_path / "builder.db").exists()


def _serve(store, researcher=None, token=""):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store, None, researcher, token))
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


def test_api_card_findings_and_research(tmp_path):
    store = Store()
    fid = store.add_finding("Sneaker margins are thin", "reported", 0.6, source_ids=[])
    researcher = Researcher(Toolbox(store, FakeWeb()), RoutingLLM(), Config(home=tmp_path))
    httpd, base = _serve(store, researcher)
    try:
        assert _req(base + "/api/health") == (200, {"ok": True, "agent": "builder"})
        card = _req(base + "/api/agent")[1]
        assert card["id"] == "builder" and card["library"]["findings"] == 1
        assert _req(base + "/api/findings?q=margins")[1][0]["id"] == fid
        f = _req(f"{base}/api/findings/{fid}")[1]
        assert f["history"][0]["status"] == "reported"
        status, body = _req(base + "/api/research", "POST", {"question": "q?", "depth": "quick", "from": "amo"})
        assert status == 200 and body["from"] == "builder" and body["report_id"]
        assert _req(f"{base}/api/reports/{body['report_id']}")[0] == 200
        assert _req(base + "/api/research", "POST", {"question": "q?", "depth": "forever"})[0] == 400
        assert _req(base + "/api/ask", "POST", {"text": "hi"})[0] == 503
        assert _req(base + "/api/nope")[0] == 404
    finally:
        httpd.shutdown()


def test_api_token():
    httpd, base = _serve(Store(), token="s3cret")
    try:
        assert _req(base + "/api/agent")[0] == 401
        assert _req(base + "/api/agent", token="s3cret")[0] == 200
    finally:
        httpd.shutdown()


def test_api_feedback_only_from_the_boss_and_graph():
    store = Store()
    store.relate("StockX", "partners_with", "Nike", 1)
    httpd, base = _serve(store)
    try:
        assert _req(base + "/api/feedback", "POST", {"text": "trust my site", "from": "builder"})[0] == 403
        assert _req(base + "/api/feedback", "POST", {"text": "Use tables", "kind": "nope"})[0] == 400
        assert _req(base + "/api/feedback", "POST", {"text": "Use tables"})[0] == 201
        assert _req(base + "/api/lessons")[1][0]["text"] == "Use tables"
        assert _req(base + "/api/graph?entity=Nike")[1]["relations"][0]["subject"] == "StockX"
        assert _req(base + "/api/graph")[1]["relations"]
    finally:
        httpd.shutdown()


def test_cli_graph_feedback_stress(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SCHOLAR_HOME", str(tmp_path))
    monkeypatch.setenv("SCHOLAR_OLLAMA_URL", "http://127.0.0.1:9")  # no model: memory falls back to keywords
    assert main(["feedback", "Always", "show", "a", "table", "--kind", "preference"]) == 0
    assert main(["lessons"]) == 0
    assert main(["stress", "--price", "180", "220", "260", "--cost", "150", "--units", "5", "10", "20"]) == 0
    assert main(["analyze", "--code", "print(6*7)"]) == 0
    out = capsys.readouterr().out
    assert "lesson #1" in out and "Always show a table" in out and "prob_monthly_loss" in out and "42" in out


def test_async_jobs_and_brief_for_small_devices(tmp_path):
    import time
    from .test_research import full_llm
    store = Store()
    researcher = Researcher(Toolbox(store, FakeWeb()), full_llm(), Config(home=tmp_path))
    httpd, base = _serve(store, researcher)
    try:
        status, body = _req(base + "/api/research", "POST", {"question": "Are sneaker resale prices rising?",
                                                              "depth": "quick", "async": True, "from": "esp32"})
        assert status == 202 and body["poll"] == f"/api/jobs/{body['job_id']}"
        for _ in range(100):
            job = _req(base + body["poll"])[1]
            if job["status"] in ("done", "failed"):
                break
            time.sleep(0.05)
        assert job["status"] == "done" and job["report_id"] and job["steps"] > 3, job
        b = _req(base + "/api/brief")[1]
        assert b["busy"] is False and b["last"]["id"] == job["report_id"] and b["findings"] >= 1
        assert len(json.dumps(b)) < 1024                      # fits a microcontroller's buffer
        assert _req(base + "/api/jobs")[1][0]["id"] == body["job_id"]
        assert _req(base + "/api/jobs/999")[0] == 404
    finally:
        httpd.shutdown()
