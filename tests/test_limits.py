"""Phase 6a: limits refreshed from the installed engine on each launch.

Hand-sourced rows in config/thresholds.yaml win; the engine fills the years
and rows they lack, published or projected; disagreements are reported; and
the whole launch runs with the network cut off."""

from __future__ import annotations

import socket
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from planner.cli import app
from planner.config import ENGINE_THRESHOLDS, load_thresholds
from planner.dashboard import page
from planner.engine import limits
from planner.engine.tax import engine_value
from planner.paths import Layout
from planner.plan.levers import year_thresholds

runner = CliRunner()


@pytest.fixture
def lay(planner_home: Path) -> Layout:
    lay = Layout(planner_home)
    lay.ensure()
    return lay


@pytest.mark.engine
def test_published_and_projected_values_are_told_apart() -> None:
    # pinned to policyengine-us 2.21.0 (uv.lock): re-check on every engine update
    assert engine_value("std_deduction_single", 2026) == (16100.0, True)
    assert engine_value("std_deduction_single", 2027) == (16550.0, False)
    # the engine's file stops at 2024 for the IRA limit, so 2026 is its projection
    assert engine_value("ira_contribution_limit", 2026) == (7000.0, False)
    assert engine_value("nc_income_tax_rate", 2026) == (0.0399, True)


@pytest.mark.engine
def test_hand_rows_win_engine_fills_next_year_and_drift_is_reported(
    lay: Layout,
) -> None:
    rep = limits.refresh(lay, 2026)
    assert rep.coverage == {2026: "config", 2027: "engine projection + carried"}
    drift = " | ".join(rep.drift[2026])
    assert "ira_contribution_limit 2026: 7,500" in drift and "projects 7,000" in drift
    assert "hsa_limit_self" in rep.carried[2027]
    assert "std_deduction_single" in rep.projected[2027]
    merged = load_thresholds(lay.config / "thresholds.yaml")
    hand = load_thresholds(lay.config / "thresholds.yaml", merged=False)
    assert 2027 not in hand and merged[2026] == hand[2026]
    std = merged[2027]["std_deduction_single"]
    assert std["value"] == 16550 and "projected" in std["source"]
    assert "confirm for 2027" in merged[2027]["hsa_limit_self"]["source"]
    # the levers now read complete 2027 limits instead of falling back to 2026
    rows, notes = year_thresholds(lay, 2027)
    assert notes == [] and rows["ira_contribution_limit"] == 7500
    assert limits.summary(rep) == "2026 config; 2027 engine projection + carried"


@pytest.mark.engine
def test_a_year_missing_from_the_hand_file_is_filled_and_flagged(
    lay: Layout,
) -> None:
    path = lay.config / "thresholds.yaml"
    data: dict[Any, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    path.write_text(yaml.safe_dump({2025: data[2026]}), encoding="utf-8")
    rep = limits.refresh(lay, 2026, write=False)
    assert not (lay.config / ENGINE_THRESHOLDS).exists()
    assert rep.coverage[2026] == "engine projection + carried"
    assert "hsa_limit_self" in rep.carried[2026]
    pg = page.gather(lay, 2026, date(2026, 7, 10))
    soft = [a.text for a in pg.alerts if a.kind == "thresholds"]
    assert any(t.startswith("2026 limits not yet confirmed") for t in soft)
    # next year's set is raised only from October
    assert not any(t.startswith("2027") for t in soft)
    q4 = page.gather(lay, 2026, date(2026, 10, 1))
    assert any(a.text.startswith("2027 limits not yet confirmed") for a in q4.alerts)


@pytest.mark.engine
def test_launch_runs_with_the_network_cut_off(
    lay: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: object, **__: object) -> None:
        raise OSError("network is off in this test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    r = runner.invoke(
        app, ["run", "--year", "2026", "--as-of", "2026-07-10", "--quiet"]
    )
    assert r.exit_code == 0, r.output
    assert "limits: 2026 config; 2027 engine projection + carried" in r.output
    html = (lay.out / "index.html").read_text(encoding="utf-8")
    assert "limits 2026 (config), 2027 (engine projection + carried)" in html
    assert (lay.config / ENGINE_THRESHOLDS).exists()
