from __future__ import annotations

from pathlib import Path

import pytest

from planner.config import (
    ASSUMPTION_FIELDS,
    ConfigError,
    load_assumptions,
    load_capabilities,
    load_thresholds,
    missing_assumptions,
)
from planner.engine.tax import CONFIG_PARAMS


def test_example_assumptions_cover_every_field_and_are_unknown(repo_root: Path) -> None:
    a = load_assumptions(repo_root / "config" / "assumptions.example.yaml")
    assert set(a) == set(ASSUMPTION_FIELDS)
    # The example ships no personal values: all but the statutory access age are null.
    assert set(missing_assumptions(a)) == set(ASSUMPTION_FIELDS) - {"ira_access_age"}


def test_unknown_assumption_field_is_rejected(tmp_path: Path) -> None:
    p = tmp_path / "a.yaml"
    p.write_text("birth_date: null\nnet_worth: 5\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="net_worth"):
        load_assumptions(p)


def test_thresholds_every_value_has_a_source(repo_root: Path) -> None:
    t = load_thresholds(repo_root / "config" / "thresholds.yaml")
    assert 2026 in t
    assert t[2026]["std_deduction_single"]["value"] == 16100
    for name, row in t[2026].items():
        assert row["source"], name


# the hand rows the levers read for the year planned (planner/plan/levers.py
# _phase and the HSA lever); the engine carries none of them
LEVER_ROWS = {
    f"{kind}_phaseout_{who}_{part}"
    for kind in ("ira", "roth")
    for who in ("single", "joint")
    for part in ("start", "width")
} | {"hsa_limit_self", "hsa_limit_family", "hsa_catchup_55plus"}


def test_every_hand_year_lists_every_lever_row(repo_root: Path) -> None:
    """A year missing one of these fails mid-run, so the file itself must be
    complete. A 2025 return is prepared in 2026, so 2025 is listed too."""
    t = load_thresholds(repo_root / "config" / "thresholds.yaml", merged=False)
    assert {2025, 2026} <= set(t)
    assert not LEVER_ROWS & set(CONFIG_PARAMS)
    for year, rows in t.items():
        assert LEVER_ROWS <= set(rows), (year, sorted(LEVER_ROWS - set(rows)))


def test_threshold_without_source_is_rejected(tmp_path: Path) -> None:
    p = tmp_path / "t.yaml"
    p.write_text("2026:\n  x:\n    value: 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="needs value and source"):
        load_thresholds(p)


def test_capabilities_statuses(repo_root: Path, tmp_path: Path) -> None:
    c = load_capabilities(repo_root / "config" / "capabilities.yaml")
    assert set(c.values()) <= {"verified", "partial", "unsupported"}
    bad = tmp_path / "c.yaml"
    bad.write_text("federal_income_tax: yes\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_capabilities(bad)


def test_yaml_is_never_executed(tmp_path: Path) -> None:
    p = tmp_path / "evil.yaml"
    p.write_text("!!python/object/apply:os.system ['echo pwned']\n", encoding="utf-8")
    with pytest.raises(Exception):  # noqa: B017 - any constructor error is the point
        load_assumptions(p)
