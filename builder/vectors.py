"""Semantic memory: find things by meaning, not just by matching words.

Embeddings come from a local Ollama model (``nomic-embed-text`` by default).
They are stored in Qdrant when ``SCHOLAR_QDRANT_URL`` is set — fast at
millions of chunks — and otherwise in a table inside scholar.db, which is
fine for a personal library and needs no extra software.

Everything goes in one collection with a ``kind`` on each point
(chunk, finding, investigation, query, lesson, glossary, entity, failed), so
one search can pull back "everything relevant to this question" and the
caller filters by kind.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import urllib.error
import urllib.request
import uuid
from array import array
from typing import Protocol

from .llm import LLMError


class VectorError(RuntimeError):
    pass


def point_id(kind: str, ref: str | int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"scholar:{kind}:{ref}"))


def _http(method: str, url: str, body: dict | None = None, headers: dict | None = None,
          timeout: float = 60) -> dict:
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"status": "not_found", "_code": 404}
        raise VectorError(f"{method} {url} → HTTP {e.code}: {e.read().decode(errors='replace')[:300]}") from e
    except (urllib.error.URLError, OSError) as e:
        raise VectorError(f"can't reach {url}: {getattr(e, 'reason', e)}") from e


# ------------------------------------------------------------------ embeddings
class Embedder:
    def __init__(self, ollama_url: str, model: str = "nomic-embed-text", timeout: float = 120):
        self.url = ollama_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        # nomic-style models are trained with task prefixes
        self._prefix = "nomic" in model or "arctic" in model

    def embed(self, texts: list[str], query: bool = False) -> list[list[float]]:
        if not texts:
            return []
        if self._prefix:
            texts = [("search_query: " if query else "search_document: ") + t for t in texts]
        try:
            data = _http("POST", self.url + "/api/embed", {"model": self.model, "input": texts},
                         timeout=self.timeout)
        except VectorError as e:
            raise LLMError(f"embedding failed ({e}). Pull the model: ollama pull {self.model}") from e
        if data.get("_code") == 404 or "embeddings" not in data:
            raise LLMError(f"embedding model {self.model} not available. Run: ollama pull {self.model}")
        return data["embeddings"]


# ---------------------------------------------------------------------- stores
class VectorStore(Protocol):
    def upsert(self, points: list[dict]) -> None: ...
    def search(self, vector: list[float], limit: int, kinds: list[str] | None,
               match: dict | None = None) -> list[dict]: ...
    def delete(self, ids: list[str]) -> None: ...
    def count(self) -> int: ...


class QdrantStore:
    """Qdrant over its REST API (no client library needed)."""

    def __init__(self, url: str, collection: str, api_key: str = ""):
        self.url = url.rstrip("/")
        self.collection = collection
        self.headers = {"api-key": api_key} if api_key else {}
        self._ready = False

    def _c(self, path: str = "") -> str:
        return f"{self.url}/collections/{self.collection}{path}"

    def _ensure(self, dim: int) -> None:
        if self._ready:
            return
        info = _http("GET", self._c(), headers=self.headers)
        if info.get("_code") == 404:
            _http("PUT", self._c(), {"vectors": {"size": dim, "distance": "Cosine"}}, self.headers)
            _http("PUT", self._c("/index"), {"field_name": "kind", "field_schema": "keyword"}, self.headers)
        else:
            size = (((info.get("result") or {}).get("config") or {}).get("params") or {}).get("vectors", {}).get("size")
            if size and size != dim:
                raise VectorError(f"Qdrant collection {self.collection} has {size}-dim vectors but the "
                                  f"embedding model makes {dim}-dim ones")
        self._ready = True

    def upsert(self, points: list[dict]) -> None:
        if not points:
            return
        self._ensure(len(points[0]["vector"]))
        for i in range(0, len(points), 128):
            _http("PUT", self._c("/points?wait=true"), {"points": points[i:i + 128]}, self.headers)

    def search(self, vector: list[float], limit: int = 10, kinds: list[str] | None = None,
               match: dict | None = None) -> list[dict]:
        self._ensure(len(vector))
        body: dict = {"query": vector, "limit": limit, "with_payload": True}
        must = [{"key": "kind", "match": {"any": kinds}}] if kinds else []
        must += [{"key": k, "match": {"value": v}} for k, v in (match or {}).items()]
        if must:
            body["filter"] = {"must": must}
        res = _http("POST", self._c("/points/query"), body, self.headers).get("result") or {}
        return [{"id": p["id"], "score": p["score"], **(p.get("payload") or {})} for p in res.get("points", [])]

    def delete(self, ids: list[str]) -> None:
        if ids and self._ready:
            _http("POST", self._c("/points/delete?wait=true"), {"points": ids}, self.headers)

    def count(self) -> int:
        info = _http("GET", self._c(), headers=self.headers)
        return ((info.get("result") or {}).get("points_count")) or 0


class LocalVectorStore:
    """Vectors in scholar.db with brute-force cosine search — no extra software."""

    SCHEMA = """CREATE TABLE IF NOT EXISTS vectors (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL, vec BLOB NOT NULL, norm REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS vectors_kind ON vectors(kind);"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        conn.executescript(self.SCHEMA)

    def upsert(self, points: list[dict]) -> None:
        rows = []
        for p in points:
            v = array("f", p["vector"])
            rows.append((p["id"], p["payload"].get("kind", ""), json.dumps(p["payload"], default=str),
                         v.tobytes(), math.sqrt(sum(x * x for x in v)) or 1.0))
        with self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO vectors(id, kind, payload, vec, norm) VALUES (?,?,?,?,?)", rows)

    def search(self, vector: list[float], limit: int = 10, kinds: list[str] | None = None,
               match: dict | None = None) -> list[dict]:
        qn = math.sqrt(sum(x * x for x in vector)) or 1.0
        sql, params = "SELECT id, payload, vec, norm FROM vectors", []
        if kinds:
            sql += f" WHERE kind IN ({','.join('?' * len(kinds))})"
            params = list(kinds)
        scored = []
        for pid, payload, blob, norm in self.conn.execute(sql, params):
            if match:
                p = json.loads(payload)
                if any(p.get(k) != v for k, v in match.items()):
                    continue
            v = array("f")
            v.frombytes(blob)
            if len(v) != len(vector):
                continue
            scored.append((sum(a * b for a, b in zip(vector, v)) / (qn * norm), pid, payload))
        scored.sort(reverse=True)
        return [{"id": pid, "score": round(s, 4), **json.loads(payload)} for s, pid, payload in scored[:limit]]

    def delete(self, ids: list[str]) -> None:
        with self.conn:
            self.conn.executemany("DELETE FROM vectors WHERE id = ?", [(i,) for i in ids])

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]


# ---------------------------------------------------------------- the memory
class SemanticIndex:
    """Embed + store + search, degrading quietly to "unavailable" if embeddings can't be made."""

    def __init__(self, embedder: Embedder | None, store: VectorStore | None):
        self.embedder = embedder
        self.store = store
        self.error = "" if embedder and store else "semantic memory not configured"

    @property
    def available(self) -> bool:
        return not self.error

    def add(self, items: list[dict]) -> int:
        """items: {kind, ref, text, **payload}. Returns how many were indexed."""
        if not self.available or not items:
            return 0
        try:
            vecs = []
            for i in range(0, len(items), 32):
                vecs += self.embedder.embed([it["text"][:4000] for it in items[i:i + 32]])
            self.store.upsert([{"id": point_id(it["kind"], it["ref"]), "vector": v, "payload": it}
                               for it, v in zip(items, vecs)])
            return len(items)
        except (LLMError, VectorError) as e:
            self.error = str(e)
            return 0

    def search(self, query: str, limit: int = 10, kinds: list[str] | None = None,
               min_score: float = 0.0, match: dict | None = None) -> list[dict]:
        if not self.available or not query.strip():
            return []
        try:
            vec = self.embedder.embed([query], query=True)[0]
            return [h for h in self.store.search(vec, limit, kinds, match) if h["score"] >= min_score]
        except (LLMError, VectorError) as e:
            self.error = str(e)
            return []

    def remove(self, kind: str, refs: list) -> None:
        if self.available:
            try:
                self.store.delete([point_id(kind, r) for r in refs])
            except VectorError:
                pass


def collection_name(model: str) -> str:
    return "scholar_" + re.sub(r"[^a-z0-9]+", "_", model.lower()).strip("_")
