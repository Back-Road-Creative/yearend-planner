from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _no_update_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never reach the real release feed; test_feed sets its own."""
    monkeypatch.setenv("PLANNER_UPDATE_FEED", "off")
    monkeypatch.delenv("PLANNER_LAUNCHER", raising=False)


@pytest.fixture
def repo_root() -> Path:
    return ROOT


@pytest.fixture
def planner_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway planner folder with the repo's config/ copied in."""
    import shutil

    home = tmp_path / "planner home ü"
    home.mkdir()
    shutil.copytree(ROOT / "config", home / "config")
    monkeypatch.setenv("PLANNER_HOME", str(home))
    return home
