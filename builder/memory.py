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
        return {"lessons": lessons[:limit + 4], "reviews": self.db.reviews(task, limit=limit)}

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
        return "\n\n".join(parts)[:max_chars]


# Back-compat alias for call sites that still say ResearchMemory.
ResearchMemory = Memory


def _overlaps(a: str, b: str, need: int = 2) -> bool:
    import re
    wa = {w for w in re.findall(r"[a-z]{4,}", a.lower())}
    wb = {w for w in re.findall(r"[a-z]{4,}", b.lower())}
    return len(wa & wb) >= need
