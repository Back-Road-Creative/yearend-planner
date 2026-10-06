"""Phase 10, unit 3d-5: the planner prices a full-year resident of one state
with no local income tax. Moving in or out of the state, income from another
state, and a city, county or school district income tax are each asked, and
when they apply they are named gaps, never figures passed off as complete."""

from __future__ import annotations

from pathlib import Path

import pytest

from planner import coverage
from planner.dashboard import page
from planner.ingest.needs import enter, need_for, parse_value
from tests.test_coverage import _home
from tests.test_spending import AS_OF

KEPT = (*coverage.PRICED[:-1], coverage.STATE_RETURN)


def test_both_are_asked_in_every_state() -> None:
    residency = need_for("state_residency")
    local = need_for("local_income_tax")
    assert residency.choices == ("full_year", "moved", "other_state")
    assert local.choices == ("no", "yes")
    for code in ("NC", "CA", "TX", "WA"):
        assert residency.asked is not None and residency.asked({"state": code})
        assert local.asked is not None and local.asked({"state": code})
    assert residency.asked is not None and not residency.asked({})
    assert local.asked is not None and not local.asked({"state": "ZZ"})
    assert parse_value(residency, "Moved") == "moved"
    with pytest.raises(ValueError, match="full_year"):
        parse_value(residency, "part year")


def test_a_full_year_resident_with_no_local_tax_has_no_gap(
    planner_home: Path,
) -> None:
    home = _home(planner_home)
    assert coverage.gate(home, "NC", "SINGLE", residency="full_year", local="no") == []


def test_a_move_is_a_part_year_gap_kept_off_the_federal_draft(
    planner_home: Path,
) -> None:
    home = _home(planner_home)
    (gap,) = coverage.gate(home, "NC", "SINGLE", residency="moved")
    assert gap.area == "state" and gap.reason.startswith("Not handled:")
    assert "part-year" in gap.reason and "NC" in gap.reason
    assert "part-year NC return" in gap.needed
    assert gap.touches == KEPT and "draft" not in gap.touches
    plan, act, prep = coverage.readiness(
        [gap], open_items=0, set_aside=[], estimates=[], year=2026, year_open=False
    )
    assert plan.blockers == (gap.reason,) and prep.blockers == (gap.reason,)


@pytest.mark.parametrize(("code", "credit"), [("NC", True), ("TX", False)])
def test_income_from_another_state_names_its_return_and_the_credit(
    planner_home: Path, code: str, credit: bool
) -> None:
    home = _home(planner_home, state=code)
    (gap,) = coverage.gate(home, code, "SINGLE", residency="other_state")
    assert "nonresident return" in gap.reason
    assert (f"{code} credit for tax paid" in gap.reason) is credit
    assert gap.touches == KEPT


def test_a_local_income_tax_is_a_gap_in_every_figure_but_the_1040(
    planner_home: Path,
) -> None:
    home = _home(planner_home, state="TX")
    (gap,) = coverage.gate(home, "TX", "SINGLE", residency="full_year", local="yes")
    assert "city, county or school district" in gap.reason
    assert "local return" in gap.needed
    assert gap.touches == KEPT


@pytest.mark.engine
def test_the_page_reads_both_answers(planner_home: Path) -> None:
    home = _home(planner_home)
    enter(home, 2026, "state_residency", "moved")
    enter(home, 2026, "local_income_tax", "yes")
    pg = page.gather(home, 2026, AS_OF)
    reasons = [g.reason for g in pg.coverage]
    assert any("part-year" in r for r in reasons)
    assert any("school district" in r for r in reasons)
    tags = {p.name: p.coverage for p in pg.panels}
    assert tags.get("magi") == coverage.NOT_HANDLED
