"""Scholar's persistent research memory — the four layers working together.

    Layer 1  live knowledge        web.py (search, papers, filings, documents)
    Layer 2  personal library      ingest(): your PDFs, manuals, reports, notes → chunks
    Layer 3  structured knowledge  db.py: findings + evidence, entities + relations, questions
    Layer 4  experience            lessons from your feedback, queries that worked, failed hypotheses

``recall(question)`` is the important part: before any research, Scholar pulls
back only what is *relevant* — similar past investigations, findings (and the
hypotheses that failed), searches that produced evidence before, glossary
terms, your preferences and corrections, and matching passages from your own
library — instead of loading everything every time.

This is retrieval, not training: the model's weights never change. Scholar
"learns" by remembering and looking things up, which is also what lets you
inspect and correct what he knows.
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import Config
from .db import Store
from .documents import Document, chunk, parse_file
from .vectors import Embedder, LocalVectorStore, QdrantStore, SemanticIndex, collection_name

INGEST_SUFFIXES = {".pdf", ".docx", ".pptx", ".xlsx", ".html", ".htm", ".md", ".txt", ".csv", ".rst"}


def build_index(config: Config, db: Store) -> SemanticIndex:
    embedder = Embedder(config.ollama_url, config.embed_model)
    if config.qdrant_url:
        store = QdrantStore(config.qdrant_url, collection_name(config.embed_model), config.qdrant_api_key)
    else:
        store = LocalVectorStore(db.conn)
    return SemanticIndex(embedder, store)


class ResearchMemory:
    def __init__(self, db: Store, index: SemanticIndex | None, config: Config | None = None):
        self.db = db
        self.index = index or SemanticIndex(None, None)
        self.config = config or Config()

    # ------------------------------------------------------------ documents
    def store_document(self, source_id: int, doc: Document, raw: bytes | None = None,
                       ext: str = "", title: str = "", url: str = "", tier: int = 3, domain: str = "") -> dict:
        """Keep the original, save its tables as CSV, and index its text for semantic search."""
        ddir = self.config.documents_dir
        ddir.mkdir(parents=True, exist_ok=True)
        saved = None
        if raw is not None and ext:
            saved = ddir / f"S{source_id}{ext}"
            saved.write_bytes(raw)
        tables = []
        tdir = ddir / "tables"
        tdir.mkdir(exist_ok=True)
        for i, t in enumerate(doc.tables):
            r, c = t.shape
            if r < 2 or c < 2:
                continue
            path = tdir / f"S{source_id}_T{i + 1}.csv"
            path.write_text(t.to_csv(), encoding="utf-8")
            tid = self.db.add_table(source_id, i + 1, t.caption, r, c, str(path), t.to_markdown(6))
            tables.append({"table_id": tid, "caption": t.caption, "rows": r, "cols": c})
        chunks = chunk(doc.text)
        indexed = self.index.add([
            {"kind": "chunk", "ref": f"{source_id}:{i}", "text": c, "source_id": source_id,
             "title": title or doc.title, "url": url, "tier": tier, "domain": domain}
            for i, c in enumerate(chunks[:400])])
        return {"saved_to": str(saved) if saved else None, "tables": tables, "chunks": len(chunks),
                "indexed": indexed}

    def ingest(self, path: str | Path, tier: int = 2, topic: str = "library") -> list[dict]:
        """Layer 2: add your own documents (a file or a whole folder) to Scholar's library."""
        path = Path(path).expanduser()
        files = [path] if path.is_file() else sorted(
            p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in INGEST_SUFFIXES)
        out = []
        for f in files:
            try:
                doc = parse_file(f)
            except Exception as e:  # noqa: BLE001 — report and keep going through the folder
                out.append({"file": str(f), "error": f"{type(e).__name__}: {e}"})
                continue
            url = f.resolve().as_uri()
            sid = self.db.add_source(url, doc.title or f.stem, "my library", "library", tier, doc.text[:400_000],
                                     doc.published)
            info = self.store_document(sid, doc, title=doc.title or f.stem, url=url, tier=tier, domain="my library")
            out.append({"file": str(f), "source_id": sid, "parser": doc.parser, **info})
        return out

    def search_library(self, query: str, limit: int = 8, own_only: bool = False) -> list[dict]:
        """Passages from documents by meaning (keyword fallback).

        ``own_only`` limits the result to the boss's ingested documents — what research rounds use, since
        sources found on the web in this same investigation are already in its search results.
        """
        hits = self.index.search(query, limit * (3 if own_only else 1), kinds=["chunk"], min_score=0.3)
        if own_only:
            hits = [h for h in hits if h.get("domain") == "my library"][:limit]
        if hits:
            return [{"source_id": h["source_id"], "title": h.get("title", ""), "url": h.get("url", ""),
                     "domain": h.get("domain", ""), "tier": h.get("tier", 3), "score": h["score"],
                     "text": h["text"]} for h in hits]
        out = []
        for s in self.db.sources(query, limit * (4 if own_only else 1)):
            if own_only and s["kind"] != "library":
                continue
            text = s.get("text") or ""
            words = [w for w in re.findall(r"\w{4,}", query.lower())]
            pos = min((text.lower().find(w) for w in words if w in text.lower()), default=0)
            out.append({"source_id": s["id"], "title": s["title"], "url": s["url"], "domain": s["domain"],
                        "tier": s["tier"], "score": None, "text": text[max(0, pos - 300):pos + 900]})
        return out[:limit]

    # ------------------------------------------------------------- indexing
    def remember_finding(self, f: dict) -> None:
        kind = "failed" if f["status"] in ("refuted", "disputed") else "finding"
        self.index.remove("finding" if kind == "failed" else "failed", [f["id"]])
        self.index.add([{"kind": kind, "ref": f["id"], "text": f["claim"], "finding_id": f["id"],
                         "status": f["status"], "confidence": f["confidence"]}])

    def remember_investigation(self, inv_id: int, question: str, summary: str) -> None:
        self.index.add([{"kind": "investigation", "ref": inv_id, "text": f"{question}\n{summary}"[:3000],
                         "investigation_id": inv_id, "question": question}])

    def remember_queries(self, rows: list[dict]) -> None:
        self.index.add([{"kind": "query", "ref": r["id"], "text": r["text"], "backend": r["backend"],
                         "facts": r["facts"]} for r in rows if r.get("facts")])

    def remember_terms(self, terms: list[dict]) -> None:
        self.index.add([{"kind": "glossary", "ref": t["term"].lower(), "text": f"{t['term']}: {t['definition']}",
                         "term": t["term"]} for t in terms])

    def remember_entities(self, ents: list[dict]) -> None:
        self.index.add([{"kind": "entity", "ref": e["id"], "text": f"{e['name']} ({e['type']}) {e.get('description', '')}",
                         "entity_id": e["id"], "name": e["name"]} for e in ents])

    def learn(self, text: str, kind: str = "preference", topic: str = "general", origin: str = "user",
              ref: str = "") -> int:
        """Layer 4: a lesson from the boss (or a verified outcome). Never from a web page."""
        lid = self.db.add_lesson(text, kind, topic, origin, ref)
        self.index.add([{"kind": "lesson", "ref": lid, "text": text, "lesson_kind": kind, "lesson_id": lid}])
        return lid

    def reindex(self, log=lambda m: None) -> dict:
        """Rebuild the semantic index from the database (e.g. after switching to Qdrant)."""
        counts = {}
        for f in self.db.findings(limit=100_000):
            self.remember_finding(f)
        counts["findings"] = len(self.db.findings(limit=100_000))
        invs = self.db.investigations(100_000)
        for i in invs:
            self.remember_investigation(i["id"], i["question"], i["summary"])
        counts["investigations"] = len(invs)
        self.remember_queries(self.db.useful_queries(100_000))
        self.remember_terms(self.db.glossary(limit=100_000))
        self.remember_entities(self.db.entities(limit=100_000))
        for l in self.db.lessons(limit=100_000):
            self.index.add([{"kind": "lesson", "ref": l["id"], "text": l["text"], "lesson_kind": l["kind"],
                             "lesson_id": l["id"]}])
        n = 0
        for s in self.db.sources(limit=100_000):
            if s.get("text") and len(s["text"]) > 200:
                log(f"indexing [S{s['id']}] {s['title'][:60]}")
                self.index.add([{"kind": "chunk", "ref": f"{s['id']}:{i}", "text": c, "source_id": s["id"],
                                 "title": s["title"], "url": s["url"], "tier": s["tier"], "domain": s["domain"]}
                                for i, c in enumerate(chunk(s["text"])[:400])])
                n += 1
        counts["sources"] = n
        if self.index.error:
            counts["error"] = self.index.error
        return counts

    # --------------------------------------------------------------- recall
    def recall(self, question: str, limit: int = 6) -> dict:
        """Everything relevant to a question — and only that."""
        mem: dict[str, list] = {"investigations": [], "findings": [], "failed": [], "queries": [],
                                "glossary": [], "lessons": [], "library": [], "entities": []}
        hits = self.index.search(question, limit * 6, min_score=0.45)
        for h in hits:
            k = h.get("kind")
            if k == "investigation" and len(mem["investigations"]) < 3:
                inv = self.db.get_investigation(h["investigation_id"])
                if inv:
                    mem["investigations"].append(inv)
            elif k in ("finding", "failed"):
                f = self.db.get_finding(h["finding_id"])
                target = "failed" if f and f["status"] in ("refuted", "disputed") else "findings"
                if f and len(mem[target]) < limit:
                    mem[target].append(f)
            elif k == "query" and len(mem["queries"]) < limit:
                mem["queries"].append({"text": h["text"], "backend": h.get("backend", "web"), "facts": h.get("facts", 0)})
            elif k == "glossary" and len(mem["glossary"]) < limit:
                mem["glossary"].append(h["text"])
            elif k == "lesson" and len(mem["lessons"]) < limit:
                mem["lessons"].append({"id": h.get("lesson_id"), "kind": h.get("lesson_kind"), "text": h["text"]})
            elif k == "entity" and len(mem["entities"]) < limit:
                mem["entities"].append(h.get("name", h["text"]))
        mem["library"] = [c for c in self.search_library(question, 4) if c.get("score") is None or c["score"] >= 0.5][:4] \
            if self.index.available else []

        # Keyword fallbacks so memory still works without embeddings.
        if not mem["findings"]:
            for f in self.db.findings(question, limit):
                (mem["failed"] if f["status"] in ("refuted", "disputed") else mem["findings"]).append(f)
        if not mem["glossary"]:
            words = [w for w in re.findall(r"[A-Za-z]{4,}", question)][:6]
            for w in words:
                mem["glossary"] += [f"{g['term']}: {g['definition']}" for g in self.db.glossary(w, 2)]
            mem["glossary"] = list(dict.fromkeys(mem["glossary"]))[:limit]
        # Standing preferences are few and always apply; corrections/strategies only when relevant.
        prefs = [{"id": l["id"], "kind": l["kind"], "text": l["text"]} for l in self.db.lessons("preference", limit=8)]
        seen = {l["id"] for l in mem["lessons"]}
        mem["lessons"] = [p for p in prefs if p["id"] not in seen] + mem["lessons"]
        if not mem["queries"]:
            qwords = set(re.findall(r"[a-z]{4,}", question.lower()))
            mem["queries"] = [{"text": q["text"], "backend": q["backend"], "facts": q["facts"]}
                              for q in self.db.useful_queries(200)
                              if len(qwords & set(re.findall(r"[a-z]{4,}", q["text"].lower()))) >= 2][:limit]
        return mem

    @staticmethod
    def as_prompt(mem: dict, max_chars: int = 5000) -> str:
        """Render recalled memory as a compact block for a prompt."""
        parts = []
        if mem["lessons"]:
            parts.append("Lessons from the boss (follow these):\n" + "\n".join(f"- {l['text']}" for l in mem["lessons"]))
        if mem["investigations"]:
            parts.append("Related past investigations:\n" + "\n".join(
                f"- #{i['id']} {i['question']} — {i['summary'][:300]}" for i in mem["investigations"]))
        if mem["findings"]:
            parts.append("Relevant findings already in the library:\n" + "\n".join(
                f"- F{f['id']} [{f['status']}, {f['confidence']:.0%}] {f['claim']} "
                f"(sources: {', '.join(f'S{i}' for i in f['source_ids']) or 'none'}; updated {f['updated_at'][:10]})"
                for f in mem["findings"]))
        if mem["failed"]:
            parts.append("Hypotheses that FAILED before (don't repeat them without new evidence):\n" + "\n".join(
                f"- F{f['id']} [{f['status']}] {f['claim']}" for f in mem["failed"]))
        if mem["queries"]:
            parts.append("Searches that produced evidence before:\n" + "\n".join(
                f"- ({q['backend']}) {q['text']}" for q in mem["queries"]))
        if mem["glossary"]:
            parts.append("Glossary:\n" + "\n".join(f"- {g}" for g in mem["glossary"]))
        if mem["entities"]:
            parts.append("Known entities: " + ", ".join(mem["entities"]))
        if mem["library"]:
            parts.append("From the library:\n" + "\n".join(
                f"- [S{c['source_id']}] {c['title']}: {c['text'][:400]}" for c in mem["library"]))
        text = "\n\n".join(parts)
        return text[:max_chars]
