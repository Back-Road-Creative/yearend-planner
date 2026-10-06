"""Phase 10, unit 3b-3: a spouse who died during the year. The survivor who
didn't remarry files a joint return carrying the spouse's income to the date of
death and their own for the whole year, checks the spouse's "Deceased" box and
signs "Filing as surviving spouse" (2025 Form 1040 instructions, Married Filing
Jointly and Death of a Taxpayer). The spouse counts as 65 or older only when 65
at death, reached on the day before the 65th birthday: born February 14, 1960
and died February 13, 2025 is 65; died February 12 is not (Pub. 501, Death of
spouse). The 2025 joint standard deduction is $31,500, $33,100 with one box
checked for 65 (Form 1040 instructions, Standard Deduction Chart for People Who
Were Born Before January 2, 1961). After the year of death a joint return is
gone: qualifying surviving spouse for the two years after, else single or head
of household. Synthetic households only."""

from __future__ import annotations

from pathlib import Path

import pytest

from planner import coverage
from planner.ingest.needs import enter, need_for, needed, parse_value
from planner.paths import Layout
from planner.plan import inputs
from planner.taxprep import draft

YEAR = 2025


def _home(home: Path, died: str | None) -> Layout:
    lay = Layout(home)
    lay.ensure()
    for key, text in (
        ("birth_date", "1962-03-01"),
        ("filing_status", "married_joint"),
        ("spouse_birth_date", "1960-02-14"),
        ("state", "NC"),
        ("wages", "60,000"),
        ("se_income", "0"),
        ("ordinary_dividends", "0"),
        ("qualified_dividends", "0"),
        ("dependents", "none"),
    ):
        enter(lay, YEAR, key, text)
    if died:
        enter(lay, YEAR, "spouse_death_date", died)
    return lay


def test_a_joint_filer_is_asked_whether_the_spouse_died(planner_home: Path) -> None:
    asked = {s.need.key: s.state for s in needed(_home(planner_home, None), YEAR).items}
    assert asked["spouse_death_date"] == "missing"


def test_the_date_of_death_is_a_date_or_none() -> None:
    need = need_for("spouse_death_date")
    assert parse_value(need, "2025-02-13") == "2025-02-13"
    assert parse_value(need, "None") == "none"
    with pytest.raises(ValueError, match="spouse_death_date"):
        parse_value(need, "2025")


@pytest.mark.parametrize(
    ("died", "age", "deduction"),
    [
        ("none", 65, 33_100.0),
        ("2025-02-13", 65, 33_100.0),
        ("2025-02-12", 64, 31_500.0),
    ],
)
def test_the_spouse_is_65_only_when_65_at_death(
    planner_home: Path, died: str, age: int, deduction: float
) -> None:
    lay = _home(planner_home, died)
    inp = inputs.build(lay, YEAR)
    assert inp.spouse_tax_age == age
    assert inp.coverage == []
    assert draft.build(lay, YEAR).get("1040", "12e") == deduction


def test_the_joint_return_names_the_death(planner_home: Path) -> None:
    lay = _home(planner_home, "2025-02-13")
    inp = inputs.build(lay, YEAR)
    assert inp.spouse_death == "2025-02-13"
    assert any("qualifying_surviving_spouse" in n and "2027" in n for n in inp.notes)
    notes = draft.build(lay, YEAR).notes
    assert any("Deceased" in n and "02/13/2025" in n for n in notes), notes
    assert any("Filing as surviving spouse" in n for n in notes)


def test_a_death_next_year_before_filing_keeps_the_joint_return(
    planner_home: Path,
) -> None:
    inp = inputs.build(_home(planner_home, "2025-02-13"), 2024)
    assert inp.coverage == []
    assert inp.spouse_tax_age == 64  # alive all of 2024: age on December 31
    assert inp.spouse_death is None


def test_a_joint_return_after_the_year_of_death_is_not_handled(
    planner_home: Path,
) -> None:
    inp = inputs.build(_home(planner_home, "2025-02-13"), 2026)
    (gap,) = inp.coverage
    assert gap.area == "household"
    assert "2025" in gap.reason
    assert "qualifying_surviving_spouse" in gap.needed
    assert gap.touches == coverage.PRICED
