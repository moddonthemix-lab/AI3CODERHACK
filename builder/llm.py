"""Minimal Ollama client (standard library only).

Ollama runs the model locally on the MacBook (later the Pi), so there is
no API key and no monthly bill.

Three things here matter a lot on CPU-only machines (Intel Macs, Raspberry Pi):

- ``schema``: Ollama constrains the model's output to an exact JSON schema, so even a
  small model can't return malformed or wrongly-shaped answers.
- ``think``: reasoning models (Qwen3, DeepSeek-R1, gpt-oss) "think out loud" before
  answering. That can be thousands of extra tokens — minutes on a CPU — so it is off
  for routine reading and only used where judgement matters (``SCHOLAR_THINK``).
- ``stats``: prompt/answer token counts and timings for every call, so you can see
  where the time goes (``builder bench`` uses them).
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request

# Families that support turning thinking off with think=false.
_THINK_BOOL = re.compile(r"^(qwen3|qwen3\.\d|deepseek-r1|magistral|cogito)", re.I)
# gpt-oss can't turn thinking off, only down.
_THINK_LEVEL = re.compile(r"^gpt-oss", re.I)


class LLMError(RuntimeError):
    pass


# One model in memory at a time. A 16GB machine can't hold Scholar's thinker *and* reader (let alone Amo's
# models too): past the limit it swaps to disk and a 5-second answer takes 7 minutes. So before switching
# models, Scholar asks Ollama to unload the one he used last. Shared by every Ollama client in the process.
_switch_lock = threading.Lock()
_last_model: dict[str, str] = {}   # Ollama URL -> the chat model Scholar used last
_mine: dict[str, set[str]] = {}    # Ollama URL -> Scholar's own chat models (never touch other apps' models)


def loaded_models(url: str, timeout: float = 5) -> list[dict]:
    """What Ollama has in memory right now (any app's models): [{"name", "size_gb"}]."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/ps", timeout=timeout) as resp:
            models = json.loads(resp.read()).get("models", [])
    except (urllib.error.URLError, OSError, ValueError):
        return []
    return [{"name": m.get("name", ""), "size_gb": round(m.get("size", 0) / 1e9, 1)} for m in models]


class Ollama:
    def __init__(self, url: str, model: str, timeout: float = 900, num_ctx: int = 8192,
                 think: bool = False, max_output: int = 2500, one_at_a_time: bool = True):
        self.url = url.rstrip("/")
        self.model = model
        self.one_at_a_time = one_at_a_time  # unload the previous model before using this one
        _mine.setdefault(self.url, set()).add(model)
        self.timeout = timeout
        # Ollama's default context is only 2-4k tokens and it silently drops the start of longer
        # prompts — ask for a real window. Keep it fixed per model: changing it reloads the model.
        self.num_ctx = num_ctx
        self.think = think
        self.max_output = max_output
        self.last: dict = {}
        self.totals = {"calls": 0, "prompt_tokens": 0, "output_tokens": 0, "seconds": 0.0}
        self._think_supported = True

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            self.url + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise LLMError(f"Ollama returned {e.code}: {e.read().decode(errors='replace')}") from e
        except urllib.error.URLError as e:
            raise LLMError(
                f"Can't reach Ollama at {self.url} ({e.reason}). Is it running? Try `ollama serve`."
            ) from e
        except (TimeoutError, OSError) as e:
            raise LLMError(f"Ollama took longer than {self.timeout:.0f}s to answer ({e}). A smaller --depth "
                           "or a faster model helps; raise SCHOLAR_LLM_TIMEOUT if your machine is slow.") from e

    def _think_value(self, want: bool | None):
        want = self.think if want is None else want
        if not self._think_supported:
            return None
        if _THINK_LEVEL.match(self.model):
            return "medium" if want else "low"
        if _THINK_BOOL.match(self.model):
            return bool(want)
        return None  # model doesn't think; don't send the flag

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             json_mode: bool = False, schema: dict | None = None, think: bool | None = None,
             max_tokens: int | None = None) -> dict:
        """Return the assistant message dict (``content`` and maybe ``tool_calls``).

        ``schema`` (a JSON schema) constrains the answer exactly; ``json_mode`` only asks for some JSON.
        """
        # Steady, not creative — but with a repeat penalty so a small model can't get stuck looping the
        # same tokens (which Ollama aborts with "token repeat limit reached").
        options = {"temperature": 0.3, "num_ctx": self.num_ctx, "repeat_penalty": 1.15, "repeat_last_n": 256}
        structured = json_mode or schema is not None
        if structured or max_tokens:
            options["num_predict"] = max_tokens or self.max_output  # a stuck model can't ramble forever
        payload = {"model": self.model, "messages": messages, "stream": False, "options": options}
        if tools:
            payload["tools"] = tools
        if schema is not None:
            payload["format"] = schema
        elif json_mode:
            payload["format"] = "json"
        t = self._think_value(think)
        if t is not None:
            payload["think"] = t
        self._make_room()
        start = time.monotonic()
        try:
            data = self._post("/api/chat", payload)
        except LLMError as e:
            if "think" in payload and "think" in str(e).lower():  # model/version without thinking control
                self._think_supported = False
                payload.pop("think")
                data = self._post("/api/chat", payload)
            else:
                raise
        self._record(data, time.monotonic() - start)
        return data["message"]

    def _make_room(self) -> None:
        """Unload the model Scholar used last if it's a different one (one model in memory at a time)."""
        if not self.one_at_a_time:
            return
        with _switch_lock:
            previous = _last_model.get(self.url)
            _last_model[self.url] = self.model
        if previous is None:
            # First call since Scholar started: one of his other models may still be loaded from before.
            others = _mine.get(self.url, set()) - {self.model}
            for m in loaded_models(self.url):
                if m["name"] in others or m["name"].removesuffix(":latest") in others:
                    self.unload(m["name"])
        elif previous != self.model:
            self.unload(previous)

    def unload(self, model: str | None = None) -> None:
        """Ask Ollama to drop a model from memory now (keep_alive 0). Harmless if it isn't loaded."""
        try:
            self._post("/api/generate", {"model": model or self.model, "keep_alive": 0})
        except LLMError:
            pass

    def _record(self, data: dict, wall: float) -> None:
        ns = 1e9
        self.last = {
            "model": self.model,
            "prompt_tokens": data.get("prompt_eval_count", 0),
            "output_tokens": data.get("eval_count", 0),
            "load_s": round(data.get("load_duration", 0) / ns, 2),
            "read_s": round(data.get("prompt_eval_duration", 0) / ns, 2),
            "write_s": round(data.get("eval_duration", 0) / ns, 2),
            "seconds": round(wall, 2),
        }
        self.last["read_tps"] = round(self.last["prompt_tokens"] / self.last["read_s"], 1) if self.last["read_s"] else None
        self.last["write_tps"] = round(self.last["output_tokens"] / self.last["write_s"], 1) if self.last["write_s"] else None
        self.totals["calls"] += 1
        self.totals["prompt_tokens"] += self.last["prompt_tokens"]
        self.totals["output_tokens"] += self.last["output_tokens"]
        self.totals["seconds"] = round(self.totals["seconds"] + wall, 1)

    def talks_plainly(self) -> bool:
        """Does the model answer directly when thinking is off? (Some editions reason out loud regardless.)"""
        msg = self.chat([{"role": "user", "content": "Reply with exactly one word: OK"}], think=False, max_tokens=40)
        reply = (msg.get("content") or "").strip()
        return len(reply) <= 12 and "ok" in reply.lower()

    def available(self) -> bool:
        try:
            req = urllib.request.Request(self.url + "/api/tags")
            with urllib.request.urlopen(req, timeout=3) as resp:
                names = [m["name"] for m in json.loads(resp.read()).get("models", [])]
            return any(n == self.model or n.split(":")[0] == self.model or n == self.model + ":latest"
                       for n in names)
        except (urllib.error.URLError, OSError, ValueError):
            return False


class CloudBrain:
    """Optional 'stronger brain' — the family cloud model (Claude via the ``anthropic`` SDK).

    PLACEHOLDER / OFF by default. It only does anything when BOTH a model and an API key are configured
    (``BUILDER_CLOUD_MODEL`` + a key). It presents the same ``model`` / ``available()`` / ``chat()``
    interface as :class:`Ollama`, so it drops straight in as Builder's stronger model for the thinking
    steps (plan, self-review, escalation) — and whatever it works out is distilled into local memory, so
    the offline model learns from it. Nothing here reaches the network until it's configured and used.
    """

    def __init__(self, model: str, api_key: str, base_url: str = "", timeout: float = 120,
                 max_output: int = 4000):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self.max_output = max_output
        self.last: dict = {}
        self.totals = {"calls": 0, "prompt_tokens": 0, "output_tokens": 0, "seconds": 0.0}

    def available(self) -> bool:
        """True only when it's actually usable: a model, a key, and the optional ``anthropic`` SDK present."""
        if not (self.model and self.api_key):
            return False
        try:
            import anthropic  # noqa: F401 — optional extra: pip install 'builder[cloud]'
        except ImportError:
            return False
        return True

    def chat(self, messages: list[dict], tools=None, json_mode: bool = False, schema=None,
             think: bool | None = None, max_tokens: int | None = None) -> dict:
        """Answer like Ollama.chat: returns {"role": "assistant", "content": "..."}. Raises LLMError on failure."""
        try:
            from anthropic import Anthropic
        except ImportError as e:
            raise LLMError("the cloud brain needs the anthropic SDK: pip install 'builder[cloud]'") from e
        system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
        convo = [{"role": m["role"], "content": m["content"]}
                 for m in messages if m.get("role") in ("user", "assistant")]
        if not convo:
            convo = [{"role": "user", "content": system}]
            system = ""
        kwargs = {"model": self.model, "max_tokens": max_tokens or self.max_output, "messages": convo}
        if system:
            kwargs["system"] = system
        start = time.monotonic()
        try:
            client = Anthropic(api_key=self.api_key, timeout=self.timeout,
                               **({"base_url": self.base_url} if self.base_url else {}))
            resp = client.messages.create(**kwargs)
            text = "".join(getattr(b, "text", "") for b in resp.content)
        except Exception as e:  # noqa: BLE001 — any SDK/network error becomes a clean LLMError
            raise LLMError(f"cloud brain error: {e}") from e
        self.totals["calls"] += 1
        self.totals["seconds"] += time.monotonic() - start
        return {"role": "assistant", "content": text}


def build_cloud_brain(config):
    """Construct the cloud brain from config, or None when cloud isn't configured (the default)."""
    model = getattr(config, "cloud_model", "") or ""
    key = getattr(config, "cloud_api_key", "") or ""
    if not model or not key:
        return None
    return CloudBrain(model, key, getattr(config, "cloud_base_url", ""),
                      getattr(config, "llm_timeout", 120))


def stronger_model(config, base_model: str):
    """Builder's stronger 'brain' for the thinking steps: the cloud brain when it's configured AND usable,
    otherwise the local stronger model (the 7b). Returns None when neither is available."""
    cloud = build_cloud_brain(config)
    if cloud is not None and cloud.available():
        return cloud
    fm = getattr(config, "fallback_model", "")
    if fm and fm != base_model:
        return Ollama(config.ollama_url, fm, config.llm_timeout, config.num_ctx,
                      one_at_a_time=config.one_model_at_a_time)
    return None


def parse_json(text: str) -> dict:
    """Best-effort JSON from a model reply (tolerates code fences, chatter and stray think tags)."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        out = json.loads(text)
        return out if isinstance(out, dict) else {}
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                out = json.loads(text[start:end + 1])
                return out if isinstance(out, dict) else {}
            except json.JSONDecodeError:
                pass
    return {}


def estimate_tokens(text: str) -> int:
    """Rough token count (~3.6 characters per token for English prose)."""
    return int(len(text) / 3.6) + 1
