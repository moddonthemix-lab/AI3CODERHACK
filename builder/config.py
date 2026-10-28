"""Runtime configuration, read from environment variables.

Everything has a sane default so `builder build` works out of the box on a MacBook running Ollama. The
same settings carry over to the Raspberry Pi later — usually only the model name changes.

Builder's own variables are ``BUILDER_*``. For a smooth move from the Scholar fork, each one also falls
back to the old ``SCHOLAR_*`` name, so an existing .env keeps working; prefer the ``BUILDER_*`` names.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def load_env(path: str = ".env") -> None:
    """Load a .env file (KEY=VALUE lines) into the environment, without overriding anything already set.

    Minimal and dependency-free: blank lines and # comments are ignored, surrounding quotes are stripped,
    and a key already in the environment always wins. Looks in the current folder first, then the repo root,
    and stops at the first file it finds. Call this once at startup (the CLI does).
    """
    for p in (Path(path), Path(__file__).resolve().parent.parent / path):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if value[:1] in ("'", '"'):                 # quoted value: take what's inside the quotes
                end = value.find(value[0], 1)
                value = value[1:end] if end != -1 else value[1:]
            else:                                        # strip an inline "  # comment", then whitespace
                value = value.split(" #", 1)[0].split("\t#", 1)[0].strip()
                if value.startswith("#"):
                    value = ""
            if key and value and key not in os.environ:  # empty settings fall through to the profile defaults
                os.environ[key] = value
        return


def _env(name: str, default: str = "") -> str:
    """Read BUILDER_<name>, falling back to the old SCHOLAR_<name>, then the default."""
    return os.environ.get("BUILDER_" + name) or os.environ.get("SCHOLAR_" + name) or default


# Hardware profiles. Anything set explicitly in the environment wins over the profile.
#   cpu  — Intel Macs and other machines without a usable GPU (the default; a 16GB Intel Mac is cpu)
#   pi   — Raspberry Pi 5 (8-16GB): small models, short reads; or point BUILDER_OLLAMA_URL at a bigger machine
#   gpu  — Apple Silicon with 32GB+ or a desktop GPU: bigger models, longer reads
# Builder is a CODER, so his models are the Qwen2.5-Coder family (code + config + security reasoning),
# not Scholar's general research models. `model` is the thinker (planning, review, security audits) and
# `fast_model` is the reader (quick edits, explanations). Confirm the best fit for your Mac with `builder bench`.
PROFILES = {
    "cpu": {"model": "qwen2.5-coder:7b", "fast_model": "qwen2.5-coder:3b", "num_ctx": 8192, "read_chars": 6000, "whisper": "base.en"},
    "pi":  {"model": "qwen2.5-coder:3b", "fast_model": "qwen2.5-coder:1.5b", "num_ctx": 4096, "read_chars": 3500, "whisper": "tiny.en"},
    "gpu": {"model": "qwen2.5-coder:14b", "fast_model": "qwen2.5-coder:7b", "num_ctx": 16384, "read_chars": 10000, "whisper": "small.en"},
}


def _profile() -> dict:
    return PROFILES.get(_env("PROFILE", "cpu").lower(), PROFILES["cpu"])


def _p(key: str, env: str):
    return lambda: _env(env) or _profile()[key]


def _toint(value, default: int) -> int:
    """Parse an int from an env value, falling back to the default if it's blank or malformed."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _p_int(key: str, env: str):
    return lambda: _toint(_env(env), _profile()[key])


def _default_home() -> Path:
    # _env("HOME") reads BUILDER_HOME or the old SCHOLAR_HOME — never the system $HOME.
    return Path(_env("HOME", "") or str(Path.home() / ".builder")).expanduser()


def _code_dirs() -> list[str]:
    """The folders Builder is allowed to touch, as absolute paths. Empty by default — the boss opts in."""
    raw = _env("CODE_DIRS", "")
    return [str(Path(p.strip()).expanduser()) for p in raw.split(",") if p.strip()]


@dataclass
class Config:
    home: Path = field(default_factory=_default_home)
    ollama_url: str = field(default_factory=lambda: _env("OLLAMA_URL", "http://localhost:11434"))
    profile: str = field(default_factory=lambda: _env("PROFILE", "cpu").lower())
    # The thinker (planning, review, the security audit) and the reader (quick edits and explanations).
    model: str = field(default_factory=_p("model", "MODEL"))
    fast_model: str = field(default_factory=_p("fast_model", "FAST_MODEL"))
    # When a build fails on the main model, retry once on this stronger one (slower but sharper). Empty = off.
    fallback_model: str = field(default_factory=lambda: _env("FALLBACK_MODEL", "qwen2.5-coder:7b"))
    # Optional cloud brain (the stronger model). OFF by default: set BOTH a model and a key to turn it on.
    # When set, it becomes Builder's stronger brain for planning/self-review/escalation, and what it works
    # out is distilled into local memory so the offline model learns from it. (Needs: pip install 'builder[cloud]'.)
    cloud_model: str = field(default_factory=lambda: _env("CLOUD_MODEL", ""))
    cloud_api_key: str = field(default_factory=lambda: _env("CLOUD_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", ""))
    cloud_base_url: str = field(default_factory=lambda: _env("CLOUD_BASE_URL"))
    num_ctx: int = field(default_factory=_p_int("num_ctx", "NUM_CTX"))
    read_chars: int = field(default_factory=_p_int("read_chars", "READ_CHARS"))
    # Let the thinker reason step by step before answering — slower on a CPU, more accurate on judgement calls.
    #   checks (default): only for the review/security pass   all: every thinker step   off: never
    think: str = field(default_factory=lambda: {"1": "all", "true": "all", "yes": "all", "0": "off", "false": "off",
                                                "no": "off"}.get(_env("THINK", "checks").lower(),
                                                                 _env("THINK", "checks").lower()))
    # Keep only one of Builder's models in memory at a time (unload the last one before switching).
    # Strongly recommended on 16GB machines; 0 lets Ollama keep several loaded (32GB+ machines).
    one_model_at_a_time: bool = field(default_factory=lambda: _env("ONE_MODEL", "1").lower()
                                      not in ("0", "false", "no"))
    llm_timeout: int = field(default_factory=lambda: _toint(_env("LLM_TIMEOUT"), 900))
    assistant_name: str = field(default_factory=lambda: _env("NAME", "Builder"))
    user_name: str = field(default_factory=lambda: _env("USER", "boss"))
    # Never touch the internet (Builder also notices on his own when it's down)
    offline: bool = field(default_factory=lambda: _env("OFFLINE", "0").lower() in ("1", "true", "yes"))
    # Optional read-only helpers kept from the fork (fetching a library's docs, behind approval).
    searxng_url: str = field(default_factory=lambda: _env("SEARXNG_URL"))
    tor_proxy: str = field(default_factory=lambda: _env("TOR_PROXY"))
    openalex_key: str = field(default_factory=lambda: _env("OPENALEX_KEY"))
    contact: str = field(default_factory=lambda: _env("CONTACT"))
    # Semantic memory: embeddings from Ollama, stored in Qdrant if configured, else in builder.db.
    embed_model: str = field(default_factory=lambda: _env("EMBED_MODEL", "nomic-embed-text"))
    qdrant_url: str = field(default_factory=lambda: _env("QDRANT_URL"))
    qdrant_api_key: str = field(default_factory=lambda: _env("QDRANT_API_KEY"))
    # Sandbox for running snippets / a project's own tests
    sandbox_timeout: int = field(default_factory=lambda: _toint(_env("SANDBOX_TIMEOUT"), 60))
    # Voice (optional extras): Whisper to listen; Piper, macOS `say` or espeak-ng to speak
    whisper_model: str = field(default_factory=_p("whisper", "WHISPER_MODEL"))
    piper_voice: str = field(default_factory=lambda: _env("PIPER_VOICE"))
    say_voice: str = field(default_factory=lambda: _env("SAY_VOICE", "Daniel"))
    # HTTP API (how the rest of the family reaches Builder) — port 8767 (Amo 8765, Scholar 8766)
    api_host: str = field(default_factory=lambda: _env("API_HOST", "127.0.0.1"))
    api_port: int = field(default_factory=lambda: _toint(_env("API_PORT"), 8767))
    api_token: str = field(default_factory=lambda: _env("API_TOKEN"))
    # --- Builder: coding & security scope (enforced by the sandbox and the security gates) ---
    # The ONLY folders Builder may read or write. Comma-separated; empty means "nothing until the boss sets it".
    # e.g. BUILDER_CODE_DIRS="~/Projects,~/code".  Never the whole disk, never system files.
    code_dirs: list[str] = field(default_factory=lambda: _code_dirs())
    # Authorized-lab mode is OFF unless the boss turns it on for a session AND confirms the target (Part 5).
    lab_mode: bool = field(default_factory=lambda: _env("LAB_MODE", "0").lower() in ("1", "true", "yes"))

    @property
    def db_path(self) -> Path:
        return self.home / "builder.db"

    @property
    def reports_dir(self) -> Path:
        return self.home / "reports"

    @property
    def documents_dir(self) -> Path:
        """Downloaded originals and extracted tables (kept from the fork; rarely used by Builder)."""
        return self.home / "documents"

    @property
    def analysis_dir(self) -> Path:
        """One folder per sandboxed Python run: the code, its inputs and its outputs."""
        return self.home / "analysis"

    def ensure_home(self) -> None:
        for d in (self.reports_dir, self.documents_dir, self.analysis_dir):
            d.mkdir(parents=True, exist_ok=True)
