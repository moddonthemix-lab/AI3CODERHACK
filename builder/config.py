"""Runtime configuration, read from environment variables.

Everything has a sane default so `scholar chat` works out of the box on a
MacBook running Ollama. The same settings carry over to the Raspberry Pi
later — usually only the model name changes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


# Hardware profiles. Anything set explicitly in the environment wins over the profile.
#   cpu  — Intel Macs and other machines without a usable GPU (the default)
#   pi   — Raspberry Pi 5 (8-16GB): small models, short reads; or point SCHOLAR_OLLAMA_URL at a bigger machine
#   gpu  — Apple Silicon with 32GB+ or a desktop GPU: bigger models, longer reads
PROFILES = {
    # Note: plain "qwen3:4b" is now Qwen's *Thinking* edition, which reasons out loud even when told not to —
    # minutes per chat reply on a CPU. The Instruct edition answers directly.
    "cpu": {"model": "qwen3:8b", "fast_model": "qwen3:4b-instruct", "num_ctx": 8192, "read_chars": 6000, "whisper": "base.en"},
    "pi":  {"model": "qwen3:4b-instruct", "fast_model": "qwen3:1.7b", "num_ctx": 4096, "read_chars": 3500, "whisper": "tiny.en"},
    "gpu": {"model": "qwen3:14b", "fast_model": "qwen3:8b", "num_ctx": 16384, "read_chars": 10000, "whisper": "small.en"},
}


def _profile() -> dict:
    return PROFILES.get(_env("SCHOLAR_PROFILE", "cpu").lower(), PROFILES["cpu"])


def _p(key: str, env: str):
    return lambda: _env(env) or _profile()[key]


def _p_int(key: str, env: str):
    return lambda: int(_env(env) or _profile()[key])


def _default_home() -> Path:
    return Path(_env("SCHOLAR_HOME", str(Path.home() / ".scholar"))).expanduser()


def _code_dirs() -> list[str]:
    """The folders Builder is allowed to touch, as absolute paths. Empty by default — the boss opts in."""
    raw = _env("BUILDER_CODE_DIRS", "")
    return [str(Path(p.strip()).expanduser()) for p in raw.split(",") if p.strip()]


@dataclass
class Config:
    home: Path = field(default_factory=_default_home)
    ollama_url: str = field(default_factory=lambda: _env("SCHOLAR_OLLAMA_URL", "http://localhost:11434"))
    profile: str = field(default_factory=lambda: _env("SCHOLAR_PROFILE", "cpu").lower())
    # The thinker (planning, synthesis, the skeptic) and the reader (pulling facts out of every source).
    model: str = field(default_factory=_p("model", "SCHOLAR_MODEL"))
    fast_model: str = field(default_factory=_p("fast_model", "SCHOLAR_FAST_MODEL"))
    num_ctx: int = field(default_factory=_p_int("num_ctx", "SCHOLAR_NUM_CTX"))
    read_chars: int = field(default_factory=_p_int("read_chars", "SCHOLAR_READ_CHARS"))
    # Let the thinker reason step by step before answering — slower on a CPU, more accurate on judgement calls.
    #   checks (default): only for the fact-check (one call per investigation)   all: every thinker step   off: never
    think: str = field(default_factory=lambda: {"1": "all", "true": "all", "yes": "all", "0": "off", "false": "off",
                                                "no": "off"}.get(_env("SCHOLAR_THINK", "checks").lower(),
                                                                 _env("SCHOLAR_THINK", "checks").lower()))
    # Keep only one of Scholar's models in memory at a time (unload the last one before switching).
    # Strongly recommended on 16GB machines; 0 lets Ollama keep several loaded (32GB+ machines).
    one_model_at_a_time: bool = field(default_factory=lambda: _env("SCHOLAR_ONE_MODEL", "1").lower()
                                      not in ("0", "false", "no"))
    llm_timeout: int = field(default_factory=lambda: int(_env("SCHOLAR_LLM_TIMEOUT", "900")))
    assistant_name: str = field(default_factory=lambda: _env("SCHOLAR_NAME", "Scholar"))
    user_name: str = field(default_factory=lambda: _env("SCHOLAR_USER", "boss"))
    # Never touch the internet (Scholar also notices on his own when it's down)
    offline: bool = field(default_factory=lambda: _env("SCHOLAR_OFFLINE", "0").lower() in ("1", "true", "yes"))
    # Search: a self-hosted SearXNG is the most private and reliable; DuckDuckGo is the fallback.
    searxng_url: str = field(default_factory=lambda: _env("SCHOLAR_SEARXNG_URL"))
    # Optional Tor SOCKS5 proxy (e.g. 127.0.0.1:9050) — enables reading .onion sources.
    tor_proxy: str = field(default_factory=lambda: _env("SCHOLAR_TOR_PROXY"))
    # OpenAlex (250M+ scholarly works) wants a free API key: https://openalex.org/settings/api
    openalex_key: str = field(default_factory=lambda: _env("SCHOLAR_OPENALEX_KEY"))
    # SEC EDGAR asks every automated client to identify itself with contact info.
    contact: str = field(default_factory=lambda: _env("SCHOLAR_CONTACT"))
    # Semantic memory: embeddings from Ollama, stored in Qdrant if configured, else in scholar.db.
    embed_model: str = field(default_factory=lambda: _env("SCHOLAR_EMBED_MODEL", "nomic-embed-text"))
    qdrant_url: str = field(default_factory=lambda: _env("SCHOLAR_QDRANT_URL"))
    qdrant_api_key: str = field(default_factory=lambda: _env("SCHOLAR_QDRANT_API_KEY"))
    # Python sandbox for quantitative analysis
    sandbox_timeout: int = field(default_factory=lambda: int(_env("SCHOLAR_SANDBOX_TIMEOUT", "60")))
    # Voice (optional extras): Whisper to listen; Piper, macOS `say` or espeak-ng to speak
    whisper_model: str = field(default_factory=_p("whisper", "SCHOLAR_WHISPER_MODEL"))
    piper_voice: str = field(default_factory=lambda: _env("SCHOLAR_PIPER_VOICE"))
    say_voice: str = field(default_factory=lambda: _env("SCHOLAR_SAY_VOICE", "Daniel"))
    # HTTP API (how the rest of the family will reach Scholar)
    api_host: str = field(default_factory=lambda: _env("SCHOLAR_API_HOST", "127.0.0.1"))
    api_port: int = field(default_factory=lambda: int(_env("SCHOLAR_API_PORT", "8766")))
    api_token: str = field(default_factory=lambda: _env("SCHOLAR_API_TOKEN"))
    # --- Builder: coding & security scope (enforced by the sandbox and the security gates) ---
    # The ONLY folders Builder may read or write. Comma-separated; empty means "nothing until the boss sets it".
    # e.g. BUILDER_CODE_DIRS="~/Projects,~/code".  Never the whole disk, never system files.
    code_dirs: list[str] = field(default_factory=lambda: _code_dirs())
    # Authorized-lab mode is OFF unless the boss turns it on for a session AND confirms the target (Part 5).
    lab_mode: bool = field(default_factory=lambda: _env("BUILDER_LAB_MODE", "0").lower() in ("1", "true", "yes"))

    @property
    def db_path(self) -> Path:
        return self.home / "scholar.db"

    @property
    def reports_dir(self) -> Path:
        return self.home / "reports"

    @property
    def documents_dir(self) -> Path:
        """Downloaded originals (PDFs, filings) and the tables extracted from them."""
        return self.home / "documents"

    @property
    def analysis_dir(self) -> Path:
        """One folder per sandboxed Python run: the code, its inputs and its outputs."""
        return self.home / "analysis"

    def ensure_home(self) -> None:
        for d in (self.reports_dir, self.documents_dir, self.analysis_dir):
            d.mkdir(parents=True, exist_ok=True)
