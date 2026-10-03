from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from planner import __version__
from planner.cli import app

runner = CliRunner()


def test_version() -> None:
    r = runner.invoke(app, ["version"])
    assert r.exit_code == 0
    assert __version__ in r.output


def test_paths_creates_folders_under_home(planner_home: Path) -> None:
    r = runner.invoke(app, ["paths"])
    assert r.exit_code == 0, r.output
    assert (planner_home / "data" / "inbox" / "UNMATCHED").is_dir()


def test_paths_refuses_onedrive(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    home = tmp_path / "OneDrive" / "Planner"
    home.mkdir(parents=True)
    monkeypatch.setenv("PLANNER_HOME", str(home))
    r = runner.invoke(app, ["paths"])
    assert r.exit_code == 2
    assert "refused" in r.output


def test_check_config(planner_home: Path) -> None:
    r = runner.invoke(app, ["check-config"])
    assert r.exit_code == 0, r.output
    assert "thresholds: years [2026]" in r.output
