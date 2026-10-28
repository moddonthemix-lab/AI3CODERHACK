"""Builder's memory — what he's learned and what he's found, so he's smarter next time (offline too).

Two things, both in the local SQLite store (no embeddings, no extra software, works with the Wi-Fi off):

- **lessons**: the boss's standing preferences and corrections (always applied), plus anything relevant
  to the task at hand;
- **reviews**: past defensive security findings, so he remembers issues he's seen and the fixes that
  worked.

``recall(task)`` gathers both for a prompt; ``as_prompt`` renders them compactly; ``learn`` records a
lesson from the boss's feedback.
"""

from __future__ import annotations

from .config import Config
from .db import Store


def build_index(config=None, db=None):
    """Kept for call-site compatibility; Builder's memory is keyword-based, so there's no index to build."""
    return None


class Memory:
    def __init__(self, db: Store, index=None, config: Config | None = None):
        self.db = db
        self.config = config or Config()

    # ------------------------------------------------------------------ learning
    def learn(self, text: str, kind: str = "preference", topic: str = "general",
              origin: str = "user", ref: str = "") -> int:
        return self.db.add_lesson(text, kind, topic, origin, ref)

    def remember_review(self, issue: str, severity: str, fix: str = "", topic: str = "security") -> int | None:
        if self.db.review_exists(issue):
            return None
        return self.db.add_review(issue, severity, fix, topic)

    def remember_build(self, task: str, dir: str, ok: bool, attempts: int, problem: str = "",
                       files: dict | None = None) -> int:
        """Record a finished build; when it passed, save the working files so he can reuse them next time."""
        import json
        blob = json.dumps(files) if (ok and files) else ""
        return self.db.add_build(task, dir, ok, attempts, problem, blob)

    # ------------------------------------------------------------------ reference library
    def learn_doc(self, path: str, title: str = "") -> dict:
        """Read a document the boss uploaded and store its passages for Builder to refer to (offline)."""
        from . import references
        return references.ingest(self.db, path, title=title)

    def learn_docs(self, paths, title: str = "", log=None) -> dict:
        """Learn from several files/folders/URLs at once (books, repos, PDFs). Returns a summary dict."""
        from . import references
        return references.ingest_many(self.db, paths, title=title, log=log)

    def reference_note(self, task: str, max_chars: int = 1800) -> str:
        """The passages from the reference library most relevant to ``task``, rendered for the prompt."""
        from . import references
        try:
            return references.as_prompt(references.recall(self.db, task), max_chars=max_chars)
        except Exception:  # noqa: BLE001 — reference recall must never break a build
            return ""

    def documents(self) -> list[dict]:
        """The documents Builder has read (name, title, passage count)."""
        return self.db.ref_sources()

    def forget_doc(self, source: str) -> int:
        return self.db.delete_refs(source)

    def recall_solution(self, task: str) -> dict | None:
        """The closest PAST build that passed, as {task, files} — so a similar new build adapts instead of starting over."""
        import json
        best, best_score = None, 0
        tw = _words(task)
        if not tw:
            return None
        for b in self.db.passed_builds_with_files():
            score = len(tw & _words(b["task"]))
            if score > best_score:
                try:
                    files = json.loads(b["files"])
                except (ValueError, TypeError):
                    continue
                if isinstance(files, dict) and files:
                    best, best_score = {"task": b["task"], "files": files}, score
        return best if best_score >= 1 else None

    # ------------------------------------------------------------------ recall
    def recall(self, task: str, limit: int = 6) -> dict:
        """Everything relevant to ``task``: standing preferences, matching lessons, and past findings."""
        prefs = self.db.lessons("preference", limit=8)
        matched = [l for l in self.db.lessons(limit=limit) if _overlaps(task, l["text"])]
        seen, lessons = set(), []
        for l in prefs + matched:
            if l["id"] not in seen:
                seen.add(l["id"])
                lessons.append({"id": l["id"], "kind": l["kind"], "text": l["text"]})
        # Reviews and builds surface on ANY shared keyword (a single strong word like "flask" is enough).
        reviews = [r for r in self.db.reviews(limit=60)
                   if _overlaps(task, r["issue"] + " " + r.get("fix", "") + " " + r.get("topic", ""), need=1)][:limit]
        builds = [b for b in self.db.builds(limit=60)
                  if _overlaps(task, b["task"] + " " + b.get("problem", ""), need=1)][:4]
        return {"lessons": lessons[:limit + 4], "reviews": reviews, "builds": builds}

    @staticmethod
    def as_prompt(mem: dict, max_chars: int = 2000) -> str:
        parts = []
        if mem.get("lessons"):
            parts.append("Lessons from the boss (follow these):\n"
                         + "\n".join(f"- {l['text']}" for l in mem["lessons"]))
        if mem.get("reviews"):
            parts.append("Security issues you've found before (and the fixes):\n" + "\n".join(
                f"- [{r['severity']}] {r['issue']}" + (f" → {r['fix']}" if r.get("fix") else "")
                for r in mem["reviews"]))
        if mem.get("builds"):
            parts.append("Projects you've built before (learn from them):\n" + "\n".join(
                f"- {b['task']}" + (f" — passed in {b['attempts']} tr{'y' if b['attempts'] == 1 else 'ies'}"
                                    if b["ok"] else " — did NOT pass")
                + (f"; problem you hit: {b['problem']}" if b.get("problem") else "")
                for b in mem["builds"]))
        return "\n\n".join(parts)[:max_chars]


# Back-compat alias for call sites that still say ResearchMemory.
ResearchMemory = Memory


_STOP = {"with", "that", "this", "from", "your", "have", "into", "will", "make", "want", "need", "code",
         "file", "test", "tests", "app", "page", "using", "small", "simple", "build"}


def _words(text: str) -> set[str]:
    import re
    return {w for w in re.findall(r"[a-z]{4,}", text.lower())} - _STOP


def _overlaps(a: str, b: str, need: int = 2) -> bool:
    return len(_words(a) & _words(b)) >= need
