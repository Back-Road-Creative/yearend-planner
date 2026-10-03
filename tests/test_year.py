"""Phase 4e: the deadline calendar and the year-end plan on one page, on the
Phase 4c household (test_withdraw's lots fixture)."""

from __future__ import annotations

from datetime import date

import pytest
import yaml
from typer.testing import CliRunner

from planner.cli import app
from planner.ingest.needs import profile_path
from planner.paths import Layout
from planner.plan import calendar, esttax, spending, year
from tests.test_spending import AS_OF, lay  # noqa: F401
from tests.test_withdraw import lots  # noqa: F401

runner = CliRunner()


def test_holidays_and_the_business_day_shift() -> None:
    h = calendar.holidays(2026)
    assert h[date(2026, 1, 19)].startswith("Birthday of Martin Luther King")
    assert h[date(2026, 11, 26)] == "Thanksgiving Day"
    assert h[date(2026, 12, 25)] == "Christmas Day"
    assert h[date(2026, 7, 3)] == "Independence Day"  # July 4 is a Saturday
    assert date(2026, 7, 4) not in h
    # a weekday stays; a Saturday moves to Monday; a holiday Monday moves past it
    assert calendar.shift(date(2026, 4, 15)) == date(2026, 4, 15)
    assert calendar.shift(date(2028, 4, 15)) == date(2028, 4, 18)  # Emancipation
    assert calendar.shift(date(2027, 12, 25)) == date(2027, 12, 27)
    # a year-end cut-off walks back to the last business day
    assert calendar.shift(date(2027, 12, 31), -1) == date(2027, 12, 30)
    assert esttax.due_dates(2029) == [
        date(2029, 4, 17),
        date(2029, 6, 15),
        date(2029, 9, 17),
        date(2030, 1, 15),
    ]


def test_deadlines_in_order_with_the_rule_date_kept() -> None:
    ds = calendar.deadlines(2026)
    assert [d.date for d in ds] == sorted(d.date for d in ds)
    by_item = {d.item: d for d in ds}
    q4 = next(d for d in ds if d.item.startswith("Q4 estimated"))
    assert (q4.date, q4.nominal) == ("2027-01-15", "2027-01-15")
    assert any("Medicaid work requirement" in i for i in by_item)
    assert all(d.nominal == d.date for d in calendar.deadlines(2026))
    # 2028-01-15 is a Saturday, then MLK Day: the Q4 payment lands on the 18th
    q4 = next(d for d in calendar.deadlines(2027) if d.item.startswith("Q4"))
    assert (q4.date, q4.nominal) == ("2028-01-18", "2028-01-15")


@pytest.mark.engine
def test_plan_page_composes_every_planner(lots: Layout) -> None:  # noqa: F811
    yp = year.assemble(lots, 2026, AS_OF)
    assert [s.name for s in yp.sections] == list(year.SECTIONS)
    assert yp.blocked == []
    assert yp.section("needed").lines[0].endswith("actual")
    assert any("ACA MAGI" in ln for ln in yp.section("magi").lines)
    assert any("already converted" in ln for ln in yp.section("conversion").lines)
    sp = spending.plan(lots, 2026, AS_OF, years=3)
    assert f"spending {sp.spending:,.2f}" in yp.section("spending").lines[0]
    assert yp.section("glide").lines[0].startswith("accessible")
    assert yp.section("cash").lines[0].endswith("covered")
    assert any(ln.startswith("fed: required") for ln in yp.section("esttax").lines)
    assert yp.section("washsales").lines == [
        "WASH VTSAX loss 3,000.00 sold 2026-06-02; bought 2026-06-20 in 33333333 "
        "(+18 days)"
    ]
    cal = yp.section("calendar").lines
    assert any("2026-12-31" in ln for ln in cal) and not any(
        "done?" in ln for ln in cal
    )
    text = year.render(yp)
    assert text.startswith("# Year-end plan 2026 (as of 2026-07-10)")
    assert "## esttax\n" in text and "blocked" not in text
    path = year.write(lots, yp)
    assert path.endswith("plan-2026.md") and (lots.out / "plan-2026.md").exists()


@pytest.mark.engine
def test_plan_page_names_what_a_blocked_planner_needs(lots: Layout) -> None:  # noqa: F811
    # the spending band and everything downstream of it need the band inputs
    prof = yaml.safe_load(profile_path(lots).read_text(encoding="utf-8"))
    prof["spending_floor"] = None
    profile_path(lots).write_text(yaml.safe_dump(prof), encoding="utf-8")
    yp = year.assemble(lots, 2026, AS_OF)
    assert "spending" in yp.blocked and "glide" in yp.blocked
    assert "magi" not in yp.blocked and "calendar" not in yp.blocked
    assert yp.section("spending").lines == [
        "needs: the plan needs spending_floor (planner needed --year 2026)"
    ]
    assert "blocked until the Needed panel is answered: spending, glide" in year.render(
        yp
    )


@pytest.mark.engine
def test_cli_plan(lots: Layout) -> None:  # noqa: F811
    r = runner.invoke(app, ["plan", "--year", "2026", "--as-of", "2026-07-10"])
    assert r.exit_code == 0, r.output
    assert "# Year-end plan 2026" in r.output and "## calendar" in r.output
    assert "written" in r.output and (lots.out / "plan-2026.md").exists()
    r = runner.invoke(
        app, ["plan", "--year", "2026", "--as-of", "2026-07-10", "--no-write"]
    )
    assert r.exit_code == 0 and "written" not in r.output
