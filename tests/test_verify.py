from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import yaml

from planner.engine import verify
from planner.engine.verify import REFERENCE, verify_file, verify_return


@pytest.mark.engine
def test_reference_cases_verify_line_by_line() -> None:
    results = verify_file(REFERENCE, tolerance=1.0)
    assert [name for name, _ in results] == list(verify.reference_cases())
    for name, report in results:
        bad = [line for line in report.lines if not line.ok]
        assert report.passed, (name, bad)
    by_name = dict(results)
    assert by_name["single_wages_2025"].year == 2025
    assert {line.name for line in by_name["single_wages_2025"].lines} >= {
        "agi",
        "state_tax",
        "fed_total_tax",
    }


def test_the_reference_cases_ship_inside_the_package() -> None:
    assert REFERENCE == Path(verify.__file__).with_name("reference.yaml")
    cases = verify.reference_cases()
    assert len(cases) >= 4 and {"year", "household", "filed"} <= set(
        cases["single_wages_2025"]
    )


FPL_CASES = (
    "fpl_138_under_2026",
    "fpl_138_over_2026",
    "fpl_138_at_2025",
    "fpl_400_under_2026",
    "fpl_400_at_2026",
    "fpl_400_over_2026",
)


@pytest.mark.engine
def test_fpl_cases_run_in_the_candidate_regression() -> None:
    """The 138% and 400% FPL cases ship in reference.yaml, so ``selfcheck
    --regression`` prints their credit, poverty percentages and Medicaid result and
    ``planner update`` holds a release that moves one."""
    assert set(FPL_CASES) <= set(verify.reference_cases())
    got = verify.parse_regression(verify.format_regression(verify.regression_values()))
    for case in FPL_CASES:
        for figure in (
            "aca_ptc",
            "aca_fpl_pct",
            "medicaid_fpl_pct",
            "medicaid_eligible",
        ):
            assert f"{case}.{figure}" in got, (case, figure)
    assert got["fpl_138_over_2026.aca_ptc"] == pytest.approx(8812.76, abs=0.01)
    assert got["fpl_400_at_2026.aca_fpl_pct"] == pytest.approx(400.0, abs=0.01)
    assert got["fpl_138_under_2026.medicaid_eligible"] == 1.0
    assert got["fpl_138_over_2026.medicaid_eligible"] == 0.0


def test_a_single_return_file_still_verifies_as_one_report(tmp_path: Path) -> None:
    case = verify.reference_cases()["single_wages_2025"]
    p = tmp_path / "2025.yaml"
    p.write_text(yaml.safe_dump(case), encoding="utf-8")
    assert [name for name, _ in verify_file(p)] == ["2025"]


def test_unknown_filed_line_is_an_error(tmp_path: Path) -> None:
    p = tmp_path / "r.yaml"
    p.write_text(
        "year: 2025\nhousehold: {age: 50, filing_status: SINGLE, state: NC}\n"
        "filed: {line_99: 1}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown filed lines"):
        verify_return(p)


def test_regression_text_round_trips() -> None:
    values = {"a.agi": 50000.0, "b.state_tax": -1.5, "b.fpg": 0.125}
    text = verify.format_regression(values)
    assert text.splitlines()[0] == "regression a.agi 50000.00"
    assert verify.parse_regression("noise\n" + text + "\nmore noise") == {
        "a.agi": 50000.0,
        "b.state_tax": -1.5,
        "b.fpg": 0.12,
    }
    assert verify.parse_regression("no values here") == {}


def test_drifts_name_what_moved_beyond_five_dollars() -> None:
    base = {"a.x": 100.0, "a.y": 200.0, "a.z": 300.0}
    got = {"a.x": 105.0, "a.y": 194.99, "a.z": 300.0, "new.w": 9.0}
    found = verify.drifts(base, got)
    assert [(d.key, d.baseline, d.candidate) for d in found] == [("a.y", 200.0, 194.99)]
    assert verify.drifts(base, {"a.x": 100.0}) == [
        verify.Drift("a.y", 200.0, None),
        verify.Drift("a.z", 300.0, None),
    ]


@pytest.fixture
def stub_engine(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    """A fixed engine: its version and its regression output."""
    values = {"single_wages_2025.agi": 50000.0, "single_wages_2025.state_tax": 1580.13}
    monkeypatch.setattr(verify, "engine_version", lambda: "2.21.0")
    monkeypatch.setattr(verify, "regression_values", lambda path=REFERENCE: values)
    return values


def test_first_run_records_the_baseline_and_prints_the_delta_from_filed(
    tmp_path: Path, stub_engine: dict[str, float], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        verify,
        "filed_deltas",
        lambda root: ["reference cases: 1/1 within $1.00", "2025: 6/6"],
    )
    note = verify.ensure_baseline(tmp_path, date(2026, 10, 3))
    saved = json.loads((tmp_path / "data" / "engine-baseline.json").read_text())
    assert saved == {
        "pinned": "2.21.0",
        "engines": {"2.21.0": {"recorded": "2026-10-03", "values": stub_engine}},
    }
    assert "baseline recorded for policyengine-us 2.21.0" in note
    assert "within $5.00" in note
    assert "delta from the filed return" in note and "2025: 6/6" in note
    assert verify.ensure_baseline(tmp_path) == ""  # recorded once per engine version


def test_filed_deltas_cover_reference_cases_and_your_own_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = verify.reference_cases()["single_wages_2025"]
    monkeypatch.setattr(verify, "reference_cases", lambda path=REFERENCE: {"x": case})
    # an engine that lands $2 above the filed state tax and exactly on the rest
    fake = dict(case["filed"], state_tax=case["filed"]["state_tax"] + 2)

    def fake_report(name: str, data: dict[str, object], tolerance: float) -> object:
        filed = data["filed"]
        assert isinstance(filed, dict)
        lines = [
            verify.Line(
                k, float(v), float(fake[k]), abs(float(v) - fake[k]) <= tolerance
            )
            for k, v in filed.items()
        ]
        return verify.Report(year=int(str(data["year"])), lines=lines)

    monkeypatch.setattr(verify, "check_case", fake_report)
    returns = tmp_path / "data" / "private" / "returns"
    returns.mkdir(parents=True)
    (returns / "2025.yaml").write_text(yaml.safe_dump(case), encoding="utf-8")
    (returns / "2025-closed.yaml").write_text("year: 2025", encoding="utf-8")
    (returns / "2024.yaml").write_text("year: [", encoding="utf-8")
    lines = verify.filed_deltas(tmp_path)
    assert lines[0] == "reference cases: 5/6 lines within $1.00 (off: x state_tax)"
    assert lines[1].startswith("2024: unreadable")
    assert lines[2] == "2025: 5/6 lines within $1.00 (off: state_tax)"
    assert len(lines) == 3  # the -closed record is not a verify file


def test_a_later_engine_inside_five_dollars_is_recorded_without_a_note(
    tmp_path: Path, stub_engine: dict[str, float], monkeypatch: pytest.MonkeyPatch
) -> None:
    verify.ensure_baseline(tmp_path, date(2026, 10, 3))
    monkeypatch.setattr(verify, "engine_version", lambda: "2.22.0")
    monkeypatch.setattr(
        verify,
        "regression_values",
        lambda path=REFERENCE: {k: v + 4.0 for k, v in stub_engine.items()},
    )
    assert verify.ensure_baseline(tmp_path, date(2026, 11, 1)) == ""
    saved = verify.load_baseline(tmp_path)
    assert (
        saved is not None and saved["pinned"] == "2.21.0"
    )  # the first stays the baseline
    assert set(saved["engines"]) == {"2.21.0", "2.22.0"}
    assert verify.pinned_baseline(tmp_path) == ("2.21.0", stub_engine)


def test_a_later_engine_off_the_baseline_is_named(
    tmp_path: Path, stub_engine: dict[str, float], monkeypatch: pytest.MonkeyPatch
) -> None:
    verify.ensure_baseline(tmp_path, date(2026, 10, 3))
    monkeypatch.setattr(verify, "engine_version", lambda: "2.22.0")
    monkeypatch.setattr(
        verify,
        "regression_values",
        lambda path=REFERENCE: {
            "single_wages_2025.agi": 50006.0,
            "single_wages_2025.state_tax": 1580.13,
        },
    )
    note = verify.ensure_baseline(tmp_path, date(2026, 11, 1))
    assert "policyengine-us 2.22.0 is off the engine baseline (2.21.0)" in note
    assert "single_wages_2025.agi" in note and "50,006.00" in note


def test_a_damaged_baseline_file_is_an_error_not_a_fresh_start(tmp_path: Path) -> None:
    path = tmp_path / "data" / "engine-baseline.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="engine-baseline.json"):
        verify.load_baseline(tmp_path)
    assert path.read_text(encoding="utf-8") == "{not json"


@pytest.mark.engine
def test_the_real_engine_reproduces_its_own_regression(tmp_path: Path) -> None:
    first = verify.regression_values()
    assert first == verify.regression_values()
    cases = verify.reference_cases()
    assert all(k.split(".")[0] in cases for k in first)
    assert first["single_wages_2025.agi"] == pytest.approx(50000, abs=1)
    note = verify.ensure_baseline(tmp_path, date(2026, 10, 3))
    assert f"{len(first)} values from {len(cases)} reference cases" in note
    assert "reference cases: " in note
