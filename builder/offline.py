"""Working without the internet, and catching up when it's back.

Scholar's brain (Ollama), memory and library are all on the machine, so most of him works offline:
chat, reading his library, reports, findings, the graph, analysis, voice. What needs the internet is
*new* sources. So:

- research while offline answers from the library and memory, says so plainly, and queues the full
  investigation (``pending``) to run automatically once the internet is back;
- a device that was away from Scholar (an ESP32 terminal off Wi-Fi, a Pi on the road) uploads what it
  was asked when it reconnects (``/api/sync``) — questions are answered straight away, research is
  queued — and gets back a short "while I was offline you asked me to…" line to say, like Amo's pocket pet.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from .db import Store

KINDS = ("ask", "research", "plan")


def receive(db: Store, items: list[dict], device: str = "device") -> list[dict]:
    """Store what a device was asked while offline. Returns the stored pending rows."""
    out = []
    for it in items[:50]:
        text = str(it.get("text", "")).strip()
        kind = str(it.get("kind", "ask")).lower()
        if not text or kind not in KINDS:
            continue
        depth = str(it.get("depth", "quick")) if kind != "ask" else ""
        pid = db.add_pending(kind, text[:2000], depth or "quick", device[:40], "", it.get("at"))
        out.append({"id": pid, "kind": kind, "text": text})
    return out


def summary(rows: list[dict]) -> str:
    """What Scholar says back to a device that just reconnected."""
    if not rows:
        return "I'm back. Nothing was waiting."
    asks = [r["text"] for r in rows if r["kind"] == "ask"]
    research = [r["text"] for r in rows if r["kind"] != "ask"]
    parts = []
    if asks:
        parts.append("asked me " + (f"“{_short(asks[0])}”" if len(asks) == 1 else f"{len(asks)} questions"))
    if research:
        parts.append("wanted research on " + (f"“{_short(research[0])}”" if len(research) == 1
                                              else f"{len(research)} topics"))
    return "While I was offline you " + " and ".join(parts) + ". I'm on it — the answers will be on the dashboard."


def _short(text: str, n: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n - 1] + "…"


def process(db: Store, online: Callable[[], bool], ask: Callable[[str, str], str] | None,
            submit: Callable[[str, str, str, str], str] | None, log: Callable[[str], None] = lambda m: None) -> dict:
    """Work through the queue.

    ``ask(text, device)`` answers a question; ``submit(kind, text, depth, origin)`` starts research and
    returns a reference (e.g. "job:3"). Questions are answered even offline (the brain is local);
    research waits for the internet, because that is what it needs.
    """
    done, waiting = [], 0
    is_online = online()
    for p in db.pending():
        if p["kind"] == "ask":
            if ask is None:
                waiting += 1
                continue
            log(f"answering “{_short(p['text'])}” (asked {p['asked_at']} on {p['origin'] or 'a device'})")
            answer = ask(p["text"], p["origin"])
            db.finish_pending(p["id"], answer[:4000])
            db.notify(f"You asked (on {p['origin'] or 'a device'}, {p['asked_at'][:16]}): {p['text']}\n\n{answer}",
                      f"pending:{p['id']}")
            done.append(p["id"])
        elif is_online and submit is not None:
            ref = submit(p["kind"], p["text"], p["depth"], p["origin"])
            db.finish_pending(p["id"], ref)
            done.append(p["id"])
            log(f"{'rechecking' if p['origin'] == 'offline-recheck' else 'researching'} “{_short(p['text'])}” → {ref}")
        else:
            waiting += 1
    return {"online": is_online, "done": done, "waiting": waiting}


class SyncLoop:
    """Server-side: check the queue every few minutes and catch up when the internet is back."""

    def __init__(self, run: Callable[[], dict], every: float = 120):
        self.run, self.every = run, every
        self.last: dict = {}
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while True:
            time.sleep(self.every)
            try:
                self.last = self.run()
            except Exception as e:  # noqa: BLE001 — keep the loop alive; show the problem on the dashboard
                self.last = {"error": f"{type(e).__name__}: {e}"}
