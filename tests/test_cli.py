from __future__ import annotations

from pathlib import Path

import pytest
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


def test_init_makes_the_folders_and_is_safe_to_repeat(planner_home: Path) -> None:
    for _ in range(2):
        r = runner.invoke(app, ["init"])
        assert r.exit_code == 0, r.output
        assert "ready" in r.output
    assert (planner_home / "data" / "inbox" / "UNMATCHED").is_dir()
    assert (planner_home / "out").is_dir()


def test_init_refuses_onedrive(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    home = tmp_path / "OneDrive" / "Planner"
    home.mkdir(parents=True)
    monkeypatch.setenv("PLANNER_HOME", str(home))
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 2
    assert "refused" in r.output and "inside a cloud-sync folder" in r.output
    assert "Move the planner folder somewhere local" in r.output
    assert not (home / "data").exists()


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
    assert "thresholds: years [2025, 2026]" in r.output


@pytest.mark.engine
def test_compute_and_sweep_cli(repo_root: Path) -> None:
    import json

    from planner.cli import app

    fx = repo_root / "tests" / "fixtures" / "household_2026.yaml"
    res = CliRunner().invoke(app, ["compute", str(fx)])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["year"] == 2026 and out["agi"] == 44000.0
    res = CliRunner().invoke(
        app, ["sweep", str(fx), "--lo", "0", "--hi", "10000", "--step", "10000"]
    )
    assert res.exit_code == 0, res.output
    assert len(json.loads(res.output)) == 2


@pytest.mark.engine
def test_verify_cli_passes_on_the_shipped_reference_cases(repo_root: Path) -> None:
    from planner.cli import app

    for args in (
        ["verify"],  # no file: the cases the release ships
        ["verify", str(repo_root / "planner/engine/reference.yaml")],
    ):
        res = CliRunner().invoke(app, args)
        assert res.exit_code == 0, res.output
        assert "DIFF" not in res.output
        assert "single_wages_2025" in res.output and "2025: 6/6" in res.output


def test_update_without_args_exits_2(planner_home: Path) -> None:
    from planner.cli import app

    res = CliRunner().invoke(app, ["update"])
    assert res.exit_code == 2


@pytest.mark.engine
def test_first_run_records_the_engine_baseline_and_prints_the_delta(
    planner_home: Path,
) -> None:
    import json

    from planner.cli import app
    from planner.engine import verify

    args = ["run", "--quiet", "--no-update-check", "--year", "2025"]
    first = CliRunner().invoke(app, args)
    assert first.exit_code == 0, first.output
    assert "engine baseline recorded for policyengine-us" in first.output
    assert "delta from the filed return:" in first.output
    assert "reference cases: 40/40 lines within $1.00" in first.output
    saved = json.loads((planner_home / "data" / "engine-baseline.json").read_text())
    version = saved["pinned"]
    assert set(saved["engines"]) == {version}
    assert set(saved["engines"][version]["values"]) == set(verify.regression_values())
    again = CliRunner().invoke(app, args)
    assert again.exit_code == 0 and "baseline recorded" not in again.output
