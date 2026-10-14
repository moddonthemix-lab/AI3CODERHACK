import json
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from builder.memory import ResearchMemory
from builder.vectors import LocalVectorStore, QdrantStore, SemanticIndex, point_id

WORDS = ["pi", "power", "watts", "sneaker", "resale", "margin", "fees", "table", "format", "report", "electricity",
         "shoes", "profit", "deadstock", "stockx", "survey"]
SYN = {"electricity": "power", "shoes": "sneaker", "profit": "margin", "watts": "power"}


class FakeEmbedder:
    """Bag-of-words vectors with a few synonyms — enough to test retrieval by meaning."""

    def embed(self, texts, query=False):
        out = []
        for t in texts:
            words = [SYN.get(w, w) for w in re.findall(r"[a-z]+", t.lower())]
            out.append([float(words.count(w)) + 0.01 for w in WORDS])
        return out


def test_local_vector_store_search_and_kinds():
    ix = SemanticIndex(FakeEmbedder(), LocalVectorStore(sqlite3.connect(":memory:")))
    ix.add([{"kind": "chunk", "ref": 1, "text": "Pi 5 draws 7 watts of power"},
            {"kind": "chunk", "ref": 2, "text": "Sneaker resale margin after StockX fees"},
            {"kind": "lesson", "ref": 3, "text": "Report format: use a table"}])
    assert ix.search("electricity use", 1)[0]["ref"] == 1      # by meaning, no shared word
    assert ix.search("shoes profit", 1)[0]["ref"] == 2
    assert [h["ref"] for h in ix.search("shoes profit", 5, kinds=["lesson"])] == [3]
    ix.remove("chunk", [1])
    assert all(h["ref"] != 1 for h in ix.search("electricity", 5))


def test_semantic_index_degrades_quietly():
    class Broken:
        def embed(self, texts, query=False):
            from builder.llm import LLMError
            raise LLMError("model not pulled")
    ix = SemanticIndex(Broken(), LocalVectorStore(sqlite3.connect(":memory:")))
    assert ix.add([{"kind": "chunk", "ref": 1, "text": "x"}]) == 0
    assert not ix.available and "model not pulled" in ix.error
    assert ix.search("x") == []


def test_qdrant_store_speaks_the_rest_api():
    calls = []

    class Fake(BaseHTTPRequestHandler):
        collections = {}

        def log_message(self, *a):
            pass

        def _reply(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n)) if n else None

        def do_GET(self):
            calls.append(("GET", self.path, None))
            if self.path == "/collections/c" and "c" in self.collections:
                return self._reply(200, {"result": {"config": {"params": {"vectors": {"size": 16}}}, "points_count": 1}})
            self._reply(404, {"status": {"error": "not found"}})

        def do_PUT(self):
            body = self._body()
            calls.append(("PUT", self.path, body))
            if self.path == "/collections/c":
                self.collections["c"] = body
            self._reply(200, {"result": True})

        def do_POST(self):
            body = self._body()
            calls.append(("POST", self.path, body))
            self._reply(200, {"result": {"points": [{"id": "x", "score": 0.9, "payload": {"kind": "chunk", "ref": 1}}]}})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        store = QdrantStore(f"http://127.0.0.1:{srv.server_address[1]}", "c", api_key="k")
        ix = SemanticIndex(FakeEmbedder(), store)
        assert ix.add([{"kind": "chunk", "ref": 1, "text": "pi power"}]) == 1
        hits = ix.search("power", 3, kinds=["chunk"])
        assert hits[0]["score"] == 0.9 and hits[0]["ref"] == 1
    finally:
        srv.shutdown()
    paths = [(m, p) for m, p, _ in calls]
    assert ("PUT", "/collections/c") in paths and ("PUT", "/collections/c/index") in paths
    create = next(b for m, p, b in calls if p == "/collections/c" and m == "PUT")
    assert create == {"vectors": {"size": 16, "distance": "Cosine"}}
    upsert = next(b for m, p, b in calls if p.startswith("/collections/c/points?"))
    assert upsert["points"][0]["id"] == point_id("chunk", 1) and upsert["points"][0]["payload"]["kind"] == "chunk"
    query = next(b for m, p, b in calls if p == "/collections/c/points/query")
    assert query["filter"] == {"must": [{"key": "kind", "match": {"any": ["chunk"]}}]} and query["limit"] == 3


def test_ingest_personal_library(tmp_path, db, config):
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "notes.md").write_text("# Sourcing\n\nMy supplier sells deadstock sneakers at 40 percent below retail.\n")
    (lib / "prices.csv").write_text("item,cost,sold\nJordan 4,210,300\nDunk,110,160\n")
    (lib / "page.html").write_text("<html><title>Fees</title><p>StockX charges sellers a 9 percent transaction fee "
                                   "plus a 3 percent payment processing fee on every sale.</p></html>")
    (lib / "ignore.bin").write_bytes(b"\x00\x01")
    mem = ResearchMemory(db, SemanticIndex(FakeEmbedder(), LocalVectorStore(db.conn)), config)
    out = mem.ingest(lib)
    assert len(out) == 3 and all("error" not in r for r in out)
    csv_row = next(r for r in out if r["file"].endswith("prices.csv"))
    assert csv_row["tables"] and db.get_table(csv_row["tables"][0]["table_id"])["n_rows"] == 3
    assert db.get_source(out[0]["source_id"])["kind"] == "library"
    hit = mem.search_library("what fees does stockx charge", 1)[0]
    assert "9 percent" in hit["text"] and hit["tier"] == 2
    # keyword fallback when there are no embeddings
    plain = ResearchMemory(db, None, config)
    assert any("supplier" in c["text"] for c in plain.search_library("supplier deadstock"))


def test_recall_returns_only_relevant_memory(db, config):
    mem = ResearchMemory(db, SemanticIndex(FakeEmbedder(), LocalVectorStore(db.conn)), config)
    good = db.get_finding(db.add_finding("Sneaker resale margin is about 12 percent", "verified", 0.8))
    bad = db.get_finding(db.add_finding("Pi power draw is 1 watt", "refuted", 0.1))
    mem.remember_finding(good)
    mem.remember_finding(bad)
    mem.learn("Use a table for prices", "preference")
    mem.learn("StockX fees changed in 2025 — check current fee pages", "correction")
    inv = db.start_investigation("Is sneaker resale profitable?")
    db.finish_investigation(inv, "done", "margins are thin")
    mem.remember_investigation(inv, "Is sneaker resale profitable?", "margins are thin")

    r = mem.recall("shoes resale profit and stockx fees")
    assert [f["id"] for f in r["findings"]] == [good["id"]]
    assert r["failed"] == []                                   # the pi finding isn't relevant here
    assert any(l["text"].startswith("Use a table") for l in r["lessons"])     # preferences always apply
    assert any("StockX fees changed" in l["text"] for l in r["lessons"])      # relevant correction
    assert r["investigations"][0]["id"] == inv
    r2 = mem.recall("pi electricity watts")
    assert [f["id"] for f in r2["failed"]] == [bad["id"]]
    text = mem.as_prompt(r2)
    assert "FAILED before" in text and "Use a table" in text and "StockX fees changed" not in text


def test_lessons_only_from_user_or_verified(db):
    import pytest
    with pytest.raises(ValueError):
        db.add_lesson("trust example.com", "source_note", origin="web")
    lid = db.add_lesson("prefer primary sources", "strategy")
    assert db.retire_lesson(lid) and db.lessons() == []


def test_graph_and_questions(db):
    inv = db.start_investigation("q")
    db.relate("StockX", "Partners With", "Nike", source_id=3, date="2025", investigation_id=inv,
              subject_type="company", object_type="company")
    db.relate("Nike", "supplies", "Foot Locker", source_id=4, investigation_id=inv)
    n = db.neighbors("nike")
    assert {r["predicate"] for r in n["relations"]} == {"partners_with", "supplies"} and sorted(n["source_ids"]) == [3, 4]
    assert db.get_entity("StockX")["type"] == "company"
    assert db.entities(investigation_id=inv)[0]["name"] == "Nike"   # most-mentioned first
    assert db.add_question(inv, "What are StockX fees?") is not None
    assert db.add_question(inv, "what are the stockx fees") is None   # duplicate in other words
    assert db.stats()["relations"] == 2
