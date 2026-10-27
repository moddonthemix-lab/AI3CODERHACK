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
