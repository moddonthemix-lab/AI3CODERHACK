"""`builder bench` — measure coder models on THIS machine and suggest the best one.

A 16GB Intel Mac runs models on the CPU, so the right model is a trade-off between quality and speed.
This gives each candidate a tiny coding task, checks the code it writes actually compiles, and times it,
so you can pick a thinker (quality) and a reader (speed) with real numbers instead of guessing.
"""

from __future__ import annotations

import time

from .llm import LLMError, Ollama

TASK = ("Write a single Python function `add(a, b)` that returns their sum. "
        "Reply with ONLY the code, no explanation, no markdown fences.")


def _works(code: str) -> bool:
    """Does the reply contain a function that compiles and actually adds two numbers?"""
    code = code.replace("```python", "").replace("```", "").strip()
    if "def add" not in code:
        return False
    try:
        ns: dict = {}
        compile(code, "<bench>", "exec")
        exec(code, ns)  # noqa: S102 — our own throwaway namespace, benchmarking the model's output
        return ns.get("add", lambda a, b: None)(2, 3) == 5
    except Exception:  # noqa: BLE001
        return False


def time_model(model: str, url: str, num_ctx: int = 8192, timeout: int = 900) -> dict:
    llm = Ollama(url, model, timeout, num_ctx, one_at_a_time=True)
    start = time.monotonic()
    try:
        reply = llm.chat([{"role": "user", "content": TASK}], max_tokens=256).get("content", "")
    except LLMError as e:
        return {"model": model, "ok": False, "score": 0, "sec": 0.0, "note": str(e)[:60]}
    sec = round(time.monotonic() - start, 1)
    ok = _works(reply)
    return {"model": model, "ok": ok, "score": 100 if ok else 0, "sec": sec, "note": "" if ok else "wrong/failed"}


def run(models: list[str], url: str, num_ctx: int = 8192, timeout: int = 900) -> list[dict]:
    return [time_model(m, url, num_ctx, timeout) for m in models]


def recommend(rows: list[dict]) -> str:
    """Suggest a thinker (best quality) and a reader (fastest that still works) from benchmark rows."""
    good = [r for r in rows if r.get("ok")]
    if not good:
        return "No model produced working code here. Pull a coder model, e.g. `ollama pull qwen2.5-coder:7b`."
    thinker = max(good, key=lambda r: (r["score"], -r["sec"]))
    reader = min(good, key=lambda r: r["sec"])
    return (f"thinker (BUILDER_MODEL):      {thinker['model']}  — writes working code in {thinker['sec']}s\n"
            f"reader  (BUILDER_FAST_MODEL): {reader['model']}  — fastest that works, {reader['sec']}s")
