import pytest

from builder.config import Config
from builder.db import Store


@pytest.fixture
def db():
    return Store()


@pytest.fixture
def config(tmp_path):
    c = Config(home=tmp_path)
    c.ensure_home()
    return c


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Tests never talk to a real Ollama, whatever is running on the machine."""
    monkeypatch.setenv("BUILDER_OLLAMA_URL", "http://127.0.0.1:9")
    for var in ("BUILDER_PROFILE", "BUILDER_MODEL", "BUILDER_FAST_MODEL", "BUILDER_NUM_CTX",
                "BUILDER_READ_CHARS", "BUILDER_THINK", "SCHOLAR_OLLAMA_URL", "SCHOLAR_MODEL"):
        monkeypatch.delenv(var, raising=False)
