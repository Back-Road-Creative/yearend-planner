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
