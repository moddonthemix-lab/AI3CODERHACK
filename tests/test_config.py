"""Config: the .env loader so settings work without exporting them every time."""

import os

from builder.config import Config, load_env


def test_load_env_sets_missing_keys_but_never_overrides(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        '# Builder settings\nBUILDER_CODE_DIRS="~/BuilderSpace"\nBUILDER_API_PORT=9999\n'
        "BUILDER_NAME=Builder\n", encoding="utf-8")
    monkeypatch.delenv("BUILDER_CODE_DIRS", raising=False)
    monkeypatch.setenv("BUILDER_API_PORT", "8767")   # already set -> .env must NOT override it

    load_env()
    assert os.environ["BUILDER_CODE_DIRS"] == "~/BuilderSpace"   # blank -> filled from .env
    assert os.environ["BUILDER_API_PORT"] == "8767"             # already set -> kept
    assert Config().api_port == 8767


def test_load_env_is_quiet_when_there_is_no_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    load_env()   # no .env here, no repo-root .env found -> must not raise


def test_offline_knowledge_persists_across_restarts_and_recalls(tmp_path):
    """His memory lives in a local file, grows as he's used, and comes back on the next run — all offline."""
    from builder.db import Store
    from builder.memory import Memory
    dbfile = tmp_path / "builder.db"

    # Session 1: teach him and record work.
    db = Store(dbfile)
    mem = Memory(db, None, Config(home=tmp_path))
    mem.learn("always add type hints and a test", "preference")
    mem.remember_build("a flask login app", str(tmp_path), ok=False, attempts=3,
                       problem="ModuleNotFoundError: No module named 'flask'")
    db.add_review("Hardcoded secret in config.py", "high", "load it from an env var", "login")
    db.close()

    # Session 2: a fresh process reopens the same file — everything is still there.
    db2 = Store(dbfile)
    mem2 = Memory(db2, None, Config(home=tmp_path))
    note = mem2.as_prompt(mem2.recall("a flask login with secrets"))   # keyword recall, no model/network
    assert "always add type hints" in note                 # lessons survived
    assert "flask" in note and "ModuleNotFoundError" in note           # build memory survived
    assert "Hardcoded secret" in note                      # security findings survived
    # and it keeps growing
    mem2.learn("prefer bcrypt for passwords", "preference")
    assert len(db2.lessons()) == 2
