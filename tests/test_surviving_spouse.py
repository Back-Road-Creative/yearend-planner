"""Phase 10, unit 3b-1: qualifying surviving spouse. Joint-return rates for the
two years after the spouse's death, while a dependent child lives at home (2025
Form 1040 instructions, Qualifying Surviving Spouse: "Your spouse died in 2023
or 2024 and you didn't remarry before the end of 2025"; "You have a child or
stepchild (not a foster child) whom you can claim as a dependent"; "If your
spouse died in 2025, you can't file as qualifying surviving spouse. Instead,
see the instructions for Married Filing Jointly"). Synthetic household only."""

from __future__ import annotations

from pathlib import Path

import pytest

from planner import coverage
from planner.engine.tax import compute, thresholds
from planner.ingest.needs import enter, need_for, needed, parse_value
from planner.paths import Layout
from planner.plan import inputs, levers


def _home(planner_home: Path, died: str | None = "2025") -> Layout:
    home = Layout(planner_home)
    home.ensure()
    for key, text in (
        ("birth_date", "1978-06-15"),
        ("filing_status", "qualifying_surviving_spouse"),
        ("state", "NC"),
        ("wages", "100,000"),
        ("dependents", "2018-03-02"),
    ):
        enter(home, 2026, key, text)
    if died:
        enter(home, 2026, "spouse_death_year", died)
    return home


def test_a_surviving_spouse_is_asked_the_year_of_death_and_the_children(
    planner_home: Path,
) -> None:
    home = _home(planner_home, died=None)
    asked = {s.need.key: s.state for s in needed(home, 2026).items}
    assert asked["spouse_death_year"] == "missing"
    assert asked["dependents"] == "actual"
    assert "spouse_birth_date" not in asked


def test_the_year_of_death_is_a_year() -> None:
    assert parse_value(need_for("spouse_death_year"), "2025") == 2025
    with pytest.raises(ValueError, match="spouse_death_year"):
        parse_value(need_for("spouse_death_year"), "20255")


def test_a_surviving_spouse_with_a_child_has_no_household_gap(
    planner_home: Path,
) -> None:
    inp = inputs.build(_home(planner_home), 2026)
    assert inp.household.filing_status == "SURVIVING_SPOUSE"
    assert inp.household.spouse is None
    assert inp.coverage == []


@pytest.mark.parametrize(
    ("died", "dependents", "words"),
    [
        (2025, 0, "planner enter dependents"),
        (2026, 1, "married_joint"),
        (2023, 1, "head_of_household"),
    ],
)
def test_a_surviving_spouse_who_does_not_qualify_is_not_handled(
    planner_home: Path, died: int, dependents: int, words: str
) -> None:
    home = _home(planner_home)
    (gap,) = coverage.gate(
        home,
        "NC",
        "SURVIVING_SPOUSE",
        dependents=dependents,
        death_year=died,
        year=2026,
    )
    assert gap.area == "household" and gap.touches == coverage.PRICED
    assert words in gap.needed
    assert "qualifying surviving spouse" in gap.reason


def test_the_ira_and_roth_phase_outs_are_the_joint_ones() -> None:
    """Pub. 590-A Table 1-2 and the Roth worksheet: "married filing jointly or
    qualifying surviving spouse"."""
    th = {
        "ira_phaseout_joint_start": 126_000.0,
        "ira_phaseout_joint_width": 20_000.0,
        "ira_phaseout_single_start": 79_000.0,
        "ira_phaseout_single_width": 10_000.0,
    }
    assert levers._phase(th, "ira", "SURVIVING_SPOUSE") == (126_000.0, 20_000.0)


@pytest.mark.engine
def test_a_surviving_spouse_is_priced_at_joint_rates(planner_home: Path) -> None:
    """100,000 of wages, one child under 17 (2026, Rev. Proc. 2025-32): the
    surviving spouse's standard deduction is the joint 32,200, so taxable is
    67,800 and the tax the joint 7,643 (tax table) less one 2,200 child tax
    credit; the 0% capital-gains band is the joint one."""
    res = compute(2026, inputs.build(_home(planner_home), 2026).household)
    assert res.taxable_income == pytest.approx(67_800, abs=1)
    assert res.fed_income_tax_after_credits == pytest.approx(7_643 - 2_200, abs=1)
    assert thresholds(2026, "SURVIVING_SPOUSE") == thresholds(2026, "JOINT")
