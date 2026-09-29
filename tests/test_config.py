from __future__ import annotations

import os
from pathlib import Path

import pytest

from paper_fetch.config import env_dir, load_env


def test_env_dir_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("PAPER_FETCH_ENV_DIR")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert env_dir() == tmp_path / "paper-fetch"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert env_dir() == Path.home() / ".config" / "paper-fetch"


def test_load_env_takes_only_known_keys_and_never_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "a.env").write_text(
        "# comment\n"
        "export OPENALEX_API_KEY='k1'\n"
        "PAPER_FETCH_EMAIL=someone@example.org\n"
        "UNRELATED_CLOUD_TOKEN=secret\n"
        "SEARXNG_URL=http://127.0.0.1:8888\n"
        "not a line\n"
    )
    (tmp_path / "ignored.txt").write_text("CORE_API_KEY=nope\n")
    monkeypatch.setenv("SEARXNG_URL", "https://already.example")
    loaded = load_env(tmp_path)
    assert loaded == ["OPENALEX_API_KEY", "PAPER_FETCH_EMAIL"]
    assert os.environ["OPENALEX_API_KEY"] == "k1"
    assert "UNRELATED_CLOUD_TOKEN" not in os.environ
    assert os.environ["SEARXNG_URL"] == "https://already.example"
    assert "CORE_API_KEY" not in os.environ
    monkeypatch.delenv("OPENALEX_API_KEY")
    monkeypatch.delenv("PAPER_FETCH_EMAIL")


def test_missing_directory_is_fine(tmp_path: Path) -> None:
    assert load_env(tmp_path / "absent") == []
    assert load_env() == []
