"""Phase 8d: a double-clicked planner.cmd passes no arguments, which opens the
dashboard (planner run) instead of printing help into a window that closes."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from planner import cli


def test_no_arguments_is_planner_run(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(cli, "app", lambda args: seen.append(args))
    assert cli.main([]) == 0
    cli.main(["needed", "--year", "2026"])
    monkeypatch.setattr(sys, "argv", ["planner"])
    cli.main()
    assert seen == [["run"], ["needed", "--year", "2026"], ["run"]]


def test_the_launcher_keeps_a_failed_double_click_open() -> None:
    text = (Path(cli.__file__).parent.parent / "planner.cmd").read_text()
    lines = [ln.strip() for ln in text.splitlines()]
    run = lines.index('"%PLANNER_PY%" -m planner %*')
    assert lines[run + 1] == 'set "PLANNER_RC=%errorlevel%"'
    assert 'if "%~1"=="" if %PLANNER_RC% NEQ 0 if %PLANNER_RC% NEQ 75 pause' in lines
    assert "if %PLANNER_RC% NEQ 75 exit /b %PLANNER_RC%" in lines


def test_launcher_warns_on_cloud_sync_path() -> None:
    """planner.cmd warns before it downloads or runs anything when its own
    folder is under a sync client; the Python side refuses the same names."""
    from planner.paths import CLOUD_SYNC_PARTS

    text = (Path(cli.__file__).parent.parent / "planner.cmd").read_text()
    lines = [ln.strip() for ln in text.splitlines()]
    find = next(ln for ln in lines if ln.startswith('echo "%~dp0" | findstr'))
    assert " /i " in find and find.endswith(">nul")
    for part in CLOUD_SYNC_PARTS:
        assert f'/c:"{part}"' in find.lower()
    for name in ("OneDrive", "Dropbox", "iCloudDrive", "Google Drive"):
        assert f'/c:"{name}"' in find
    i = lines.index(find)
    assert lines[i + 1] == "if errorlevel 1 goto :not_synced"
    assert any("cloud-sync" in ln for ln in lines[i + 2 : i + 5])
    assert ":not_synced" in lines[i + 2 : i + 8]
    # the warning comes before anything is downloaded or run
    assert i < lines.index('"%PLANNER_PY%" -m planner %*')
    assert i < next(k for k, ln in enumerate(lines) if ln.startswith("where uv"))
