"""Phase 10, unit 3a-2: the profile names the household's people. A joint
filer is asked for the spouse's birth date and a joint or head-of-household
filer for the dependents; the household the planners price carries them, and
the coverage gate stops tagging every figure "not handled" once they are named.
Synthetic household only."""

from __future__ import annotations

from pathlib import Path

import pytest

from planner import coverage
from planner.engine.household import Dependent, Person
from planner.engine.tax import compute
from planner.ingest.needs import enter, need_for, needed, parse_value
from planner.paths import Layout
from planner.plan import inputs


def _home(planner_home: Path, status: str) -> Layout:
    home = Layout(planner_home)
    home.ensure()
    for key, text in (
        ("birth_date", "1971-06-15"),
        ("filing_status", status),
        ("state", "NC"),
        ("wages", "100,000"),
    ):
        enter(home, 2026, key, text)
    return home


def _asked(home: Layout) -> dict[str, str]:
    return {s.need.key: s.state for s in needed(home, 2026).items}


def test_a_joint_filer_is_asked_for_the_spouse_and_the_dependents(
    planner_home: Path,
) -> None:
    home = _home(planner_home, "married_joint")
    asked = _asked(home)
    assert asked["spouse_birth_date"] == "missing"
    assert asked["dependents"] == "missing"


def test_a_single_filer_is_asked_for_neither(planner_home: Path) -> None:
    asked = _asked(_home(planner_home, "single"))
    assert "spouse_birth_date" not in asked and "dependents" not in asked


def test_head_of_household_is_asked_for_the_dependents_only(
    planner_home: Path,
) -> None:
    asked = _asked(_home(planner_home, "head_of_household"))
    assert asked["dependents"] == "missing" and "spouse_birth_date" not in asked


def test_dependents_are_birth_dates_with_student_and_disabled_marks() -> None:
    need = need_for("dependents")
    assert parse_value(need, "2018-03-02, 2006-07-01 student; 1950-01-09 Disabled") == [
        {"birth_date": "2018-03-02", "student": False, "disabled": False},
        {"birth_date": "2006-07-01", "student": True, "disabled": False},
        {"birth_date": "1950-01-09", "student": False, "disabled": True},
    ]
    assert parse_value(need, "none") == []
    with pytest.raises(ValueError, match="dependents: a date as YYYY-MM-DD"):
        parse_value(need, "2018-13-01")
    with pytest.raises(ValueError, match="dependents: unknown mark 'kid'"):
        parse_value(need, "2018-03-02 kid")
    with pytest.raises(ValueError, match="dependents: at most 20"):
        parse_value(need, ", ".join(["2018-03-02"] * 21))


def test_the_household_carries_the_spouse_and_the_dependents(
    planner_home: Path,
) -> None:
    home = _home(planner_home, "married_joint")
    enter(home, 2026, "spouse_birth_date", "1962-01-01")
    enter(home, 2026, "dependents", "2018-03-02, 2006-07-01 student")
    inp = inputs.build(home, 2026)
    hh = inp.household
    # 64 on December 31, but 65 for the tax tests (Pub. 501: the day before)
    assert hh.spouse == Person(age=64)
    assert inp.spouse_tax_age == 65
    assert hh.dependents == (
        Dependent(age=8),
        Dependent(age=20, full_time_student=True),
    )
    assert not [g for g in inp.coverage if set(g.touches) == set(coverage.PRICED)]


def test_none_answers_the_dependents(planner_home: Path) -> None:
    home = _home(planner_home, "married_joint")
    enter(home, 2026, "dependents", "none")
    assert _asked(home)["dependents"] == "actual"
    assert inputs.build(home, 2026).household.dependents == ()


def test_a_spouse_typed_under_another_status_is_not_priced(
    planner_home: Path,
) -> None:
    """The profile keeps an old answer; only a joint return carries a spouse."""
    home = _home(planner_home, "married_joint")
    enter(home, 2026, "spouse_birth_date", "1975-05-05")
    enter(home, 2026, "filing_status", "married_separate")
    inp = inputs.build(home, 2026)
    assert inp.household.spouse is None and inp.spouse_tax_age is None
    assert [g.area for g in inp.coverage] == ["household"]
    assert "married filing separately" in inp.coverage[0].reason


@pytest.mark.parametrize(
    ("status", "spouse", "dependents", "needed_words"),
    [
        ("JOINT", False, 0, "planner enter spouse_birth_date"),
        ("HEAD_OF_HOUSEHOLD", False, 0, "planner enter dependents"),
        ("SEPARATE", False, 0, "preparer"),
    ],
)
def test_until_the_people_are_named_every_figure_is_not_handled(
    planner_home: Path, status: str, spouse: bool, dependents: int, needed_words: str
) -> None:
    home = _home(planner_home, "single")
    (gap,) = coverage.gate(home, "NC", status, spouse=spouse, dependents=dependents)
    assert gap.touches == coverage.PRICED and needed_words in gap.needed


@pytest.mark.parametrize(
    ("status", "spouse", "dependents"),
    [("JOINT", True, 0), ("JOINT", True, 2), ("HEAD_OF_HOUSEHOLD", False, 1)],
)
def test_once_named_only_the_per_person_lines_stay_not_handled(
    planner_home: Path, status: str, spouse: bool, dependents: int
) -> None:
    """Income is still priced as the first person's until each document names
    its owner (unit 2c): the sections with per-person lines alone stay
    tagged."""
    home = _home(planner_home, "single")
    (gap,) = coverage.gate(home, "NC", status, spouse=spouse, dependents=dependents)
    assert gap.area == "household" and gap.touches == coverage.PEOPLE_TOUCH
    assert "Not handled:" in gap.reason and "Schedule SE" in gap.reason
    assert coverage.tag([gap], "magi", "verified") == coverage.VERIFIED
    assert coverage.tag([gap], "draft", "verified") == coverage.NOT_HANDLED


@pytest.mark.engine
def test_a_joint_household_with_two_children_is_priced_as_the_unit(
    planner_home: Path,
) -> None:
    """100,000 of wages, joint, two children under 17 (2026, Rev. Proc. 2025-32):
    taxable 100,000 - 32,200 = 67,800; Table 1 tax 2,480 + 12% x 43,000 = 7,640
    (the tax table's midpoint gives 7,643); less 2 x 2,200 child tax credit."""
    home = _home(planner_home, "married_joint")
    enter(home, 2026, "spouse_birth_date", "1983-02-02")
    enter(home, 2026, "dependents", "2018-03-02, 2014-07-01")
    res = compute(2026, inputs.build(home, 2026).household)
    assert res.taxable_income == pytest.approx(67_800, abs=1)
    assert res.fed_income_tax_after_credits == pytest.approx(7_643 - 4_400, abs=1)
