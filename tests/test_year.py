"""Phase 4e: the deadline calendar and the year-end plan on one page, on the
Phase 4c household (test_withdraw's lots fixture)."""

from __future__ import annotations

from datetime import date

import pytest
import yaml
from typer.testing import CliRunner

from planner.cli import app
from planner.ingest.needs import enter, profile_path
from planner.paths import Layout
from planner.plan import (
    calendar,
    conversion,
    esttax,
    glidepath,
    magi,
    spending,
    year,
)
from planner.plan.inputs import OverrideError, Overrides
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
    ds = calendar.deadlines(2026, "NC")
    assert [d.date for d in ds] == sorted(d.date for d in ds)
    by_item = {d.item: d for d in ds}
    q4 = next(d for d in ds if d.item.startswith("Q4 estimated"))
    assert (q4.date, q4.nominal) == ("2027-01-15", "2027-01-15")
    assert any("Medicaid work requirement" in i for i in by_item)
    assert all(d.nominal == d.date for d in calendar.deadlines(2026, "NC"))
    # 2028-01-15 is a Saturday, then MLK Day: the Q4 payment lands on the 18th
    q4 = next(d for d in calendar.deadlines(2027, "NC") if d.item.startswith("Q4"))
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
    # the cash section still runs; only its monthly line (the glide run) is not drawn
    cash = yp.section("cash")
    assert cash.ok and cash.tables == []
    assert any(
        "monthly cash line not drawn: the plan needs spending_floor" in n
        for n in cash.notes
    )


@pytest.mark.engine
def test_plan_text_carries_the_tables_aligned(lots: Layout) -> None:  # noqa: F811
    yp = year.assemble(lots, 2026, AS_OF)
    text = year.render(yp)
    assert "Age and year table, 2026 to 2066" in text
    assert "Monthly cash line, 2026-01 to 2027-12" in text
    assert "Return bands, 2026 to 2035" in text
    glide_tbl = yp.section("glide").tables[0]
    widths = {len(ln) for ln in year.text_table(glide_tbl)}
    assert len(widths) == 1  # right-aligned numbers: every row the same width
    assert "2026-01*" in text and "2027-12 " in text
    assert len(year.text_table(yp.section("cash").tables[0])) == 25


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


def _objective(
    home: Layout, objective: str = "bracket_12", margin: str = "1,000"
) -> None:
    enter(home, 2026, "conversion_objective", objective)
    enter(home, 2026, "conversion_margin", margin)
    enter(home, 2026, "conversion_cap", "150,000")


@pytest.mark.engine
def test_auto_conversion_feeds_magi_and_esttax(lots: Layout) -> None:  # noqa: F811
    _objective(lots)
    manual = year.assemble(lots, 2026, AS_OF)
    sz = conversion.size(lots, 2026)
    assert sz.recommendation is not None
    extra = sz.recommendation.amount - sz.already
    assert extra > 0
    auto = year.assemble(lots, 2026, AS_OF, Overrides(conversion_target="auto"))
    by_hand = year.assemble(lots, 2026, AS_OF, Overrides(planned_conversion=extra))
    # MAGI and estimated tax price the adopted conversion, exactly as if it were typed
    assert auto.section("magi").lines == by_hand.section("magi").lines
    assert auto.section("magi").lines != manual.section("magi").lines
    assert auto.section("esttax").lines == by_hand.section("esttax").lines
    assert auto.section("esttax").lines != manual.section("esttax").lines
    pj = magi.project(lots, 2026, Overrides(planned_conversion=extra))
    assert f"AGI {pj.result.agi:,.2f}" in auto.section("magi").lines[0]
    # the conversion section says what was adopted and keeps the candidates
    lines = auto.section("conversion").lines
    assert lines[0].startswith(
        f"adopted {sz.recommendation.name} {sz.recommendation.amount:,.2f}"
    )
    assert f"+{extra:,.2f}" in lines[0]
    assert any(ln.startswith("bracket_12") for ln in lines)
    assert not any(
        ln.startswith("adopted") for ln in manual.section("conversion").lines
    )


@pytest.mark.engine
def test_auto_without_a_recommendation_adopts_nothing(lots: Layout) -> None:  # noqa: F811
    manual = year.assemble(lots, 2026, AS_OF)
    auto = year.assemble(lots, 2026, AS_OF, Overrides(conversion_target="auto"))
    assert auto.section("magi").lines == manual.section("magi").lines
    assert any(
        "auto: no recommendation, nothing adopted" in n
        for n in auto.section("conversion").notes
    )
    with pytest.raises(OverrideError, match="not both"):
        year.assemble(
            lots,
            2026,
            AS_OF,
            Overrides(planned_conversion=5000, conversion_target="auto"),
        )


@pytest.mark.engine
def test_total_income_override_reaches_the_page(lots: Layout) -> None:  # noqa: F811
    base = year.assemble(lots, 2026, AS_OF)
    yp = year.assemble(lots, 2026, AS_OF, Overrides(total_income=150_000))
    assert yp.section("magi").lines != base.section("magi").lines
    pj = magi.project(lots, 2026, Overrides(total_income=150_000))
    assert f"AGI {pj.result.agi:,.2f}" in yp.section("magi").lines[0]
    too_low = year.assemble(lots, 2026, AS_OF, Overrides(total_income=1))
    assert "magi" in too_low.blocked
    assert "below the other income" in too_low.section("magi").lines[0]


@pytest.mark.engine
def test_page_conversion_section_carries_the_spill_warning(lots: Layout) -> None:  # noqa: F811
    # 3,000 of qualified dividends sit on top of the ordinary income; filling
    # to the 12% bracket top (past the 0% gains ceiling) pushes them into 15%
    enter(lots, 2026, "interest", "1,000")
    enter(lots, 2026, "ordinary_dividends", "3,000")
    enter(lots, 2026, "qualified_dividends", "3,000")
    enter(lots, 2026, "long_term_gains", "0")  # no realized loss netting the gains
    enter(lots, 2026, "se_income", "70,000")  # room under the 0% ceiling first
    _objective(lots, "bracket_12", "0")
    sz = conversion.size(lots, 2026)
    rec = sz.recommendation
    assert rec is not None and rec.qualified_spill
    yp = year.assemble(lots, 2026, AS_OF)
    sec = yp.section("conversion")
    assert "WARNING" in sec.lines[0] and "15%" in sec.lines[0]
    assert any(
        ln.startswith("bracket_12") and "WARNING: qualified" in ln for ln in sec.lines
    )
    assert any(ln.startswith("ltcg_0pct") and "WARNING" not in ln for ln in sec.lines)
    # no spill, no warning: the 0% objective stays under the ceiling
    _objective(lots, "ltcg_0pct")
    first = year.assemble(lots, 2026, AS_OF).section("conversion").lines[0]
    assert "WARNING" not in first


@pytest.mark.engine
def test_cli_plan_flags_and_prompts(lots: Layout) -> None:  # noqa: F811
    _objective(lots)
    args = ["plan", "--year", "2026", "--as-of", "2026-07-10", "--no-write"]
    r = runner.invoke(
        app,
        [*args, "--no-ask", "--conversion-target", "auto"],
    )
    assert r.exit_code == 0, r.output
    assert "adopted bracket_12" in r.output
    r = runner.invoke(app, [*args, "--no-ask", "--total-income", "150000"])
    assert r.exit_code == 0, r.output
    assert "total_income 150,000 typed" in r.output
    r = runner.invoke(app, [*args, "--no-ask", "--conversion-target", "sometimes"])
    assert r.exit_code == 2 and "manual or auto" in r.output
    r = runner.invoke(
        app, [*args, "--no-ask", "--conversion", "5000", "--conversion-target", "auto"]
    )
    assert r.exit_code == 2 and "not both" in r.output
    # prompts: total income, Q4 dividends, short-term, long-term, target
    r = runner.invoke(app, [*args, "--ask"], input="\n250\n\n(500)\nauto\n")
    assert r.exit_code == 0, r.output
    assert "total income" in r.output and "conversion target" in r.output
    assert "adopted bracket_12" in r.output
    # a bad answer is explained and asked again; flags given are not asked
    r = runner.invoke(
        app,
        [
            *args,
            "--ask",
            "--total-income",
            "150000",
            "--sales-st",
            "0",
            "--sales-lt",
            "0",
        ],
        input="lots\n300\nmanual\n",
    )
    assert r.exit_code == 0, r.output
    assert "not a number" in r.output and "total income" not in r.output


def test_calendar_q2_q3_are_conditional_on_esttax() -> None:
    def q(ds: list[calendar.Deadline], n: int) -> str:
        return next(d.item for d in ds if d.item.startswith(f"Q{n} estimated"))

    # nothing known: the dates stay, marked "if required"
    unknown = calendar.deadlines(2026, "NC")
    assert q(unknown, 2) == "Q2 estimated payments (federal and NC) if required"
    assert q(unknown, 3) == "Q3 estimated payments (federal and NC) if required"
    # esttax says who owes: federal only in Q2, nobody in Q3
    known = calendar.deadlines(2026, "NC", {2: ("fed",), 3: ()})
    assert q(known, 2).endswith("if required: required for federal")
    assert "if required: not required" in q(known, 3)
    # nobody owes and the reason is given: the line states it
    why = "withholding covers the safe harbor"
    reasoned = calendar.deadlines(2026, "NC", {2: (), 3: ()}, why)
    assert q(reasoned, 3).endswith(f"if required: not required ({why})")
    assert "de minimis" not in q(reasoned, 2)
    both = calendar.deadlines(2026, "NC", {2: ("fed", "nc"), 3: ("fed", "nc")})
    assert q(both, 3).endswith("if required: required for federal and NC")
    # Q1 and Q4 are not conditional
    assert q(known, 4) == "Q4 estimated payments (federal and NC)"
    assert "if required" not in next(
        d.item for d in known if d.item.startswith("Q1 estimated")
    )
    # the dates themselves do not move
    assert [d.date for d in known] == [d.date for d in unknown]


@pytest.mark.engine
def test_plan_calendar_marks_q2_q3_from_the_esttax_result(lots: Layout) -> None:  # noqa: F811
    cal = year.assemble(lots, 2026, AS_OF).section("calendar").lines
    q2 = next(ln for ln in cal if "Q2 estimated" in ln)
    assert q2.endswith("if required: required for federal and NC"), q2
    # tax after withholding under 1,000 for both: nothing is required
    enter(lots, 2026, "se_income", "6,000")
    enter(lots, 2026, "fed_withheld", "500")
    enter(lots, 2026, "nc_withheld", "500")
    sec = year.assemble(lots, 2026, AS_OF).section("calendar")
    q3 = next(ln for ln in sec.lines if "Q3 estimated" in ln)
    assert "if required: not required" in q3, q3
    assert "under the de minimis" in q3 and "safe harbor" not in q3, q3
    assert any("rests on 2026" in n for n in sec.notes)


@pytest.mark.engine
def test_calendar_q2_q3_skip_an_agency_whose_withholding_covers_next_year(
    lots: Layout,  # noqa: F811
) -> None:
    enter(lots, 2026, "se_income", "120,000")
    fed = esttax.estimate(lots, 2026, AS_OF).agencies[0]
    assert fed.current_tax >= 15_000.0, fed.current_tax
    # 1,500 left to pay is not under the de minimis, yet the withholding is over
    # 90% of the tax, so next year's installments are zero (cash_flows agrees)
    enter(lots, 2026, "fed_withheld", f"{fed.current_tax - 1_500.0:.2f}")
    et = esttax.estimate(lots, 2026, AS_OF)
    assert not et.agencies[0].de_minimis
    assert esttax.next_year_required(et.agencies[0], et.agi) == 0.0
    flows, _ = esttax.cash_flows(et)
    assert [
        f.amount for f in flows if f.agency == "fed" and f.kind.startswith("next year")
    ] == [0.0] * 3
    cal = year.assemble(lots, 2026, AS_OF).section("calendar").lines
    for n in (2, 3):
        line = next(ln for ln in cal if f"Q{n} estimated" in ln)
        assert line.endswith("if required: required for NC"), line


@pytest.mark.engine
def test_calendar_names_withholding_when_every_agency_is_covered_not_de_minimis(
    lots: Layout,  # noqa: F811
) -> None:
    enter(lots, 2026, "se_income", "400,000")
    et = esttax.estimate(lots, 2026, AS_OF)
    for key, ag in zip(("fed_withheld", "nc_withheld"), et.agencies, strict=True):
        assert ag.current_tax >= 12_000.0, ag.current_tax
        # 1,100 left to pay: over the 1,000 de minimis, under 10% of the tax
        enter(lots, 2026, key, f"{ag.current_tax - 1_100.0:.2f}")
    et = esttax.estimate(lots, 2026, AS_OF)
    for ag in et.agencies:
        assert not ag.de_minimis
        assert esttax.next_year_required(ag, et.agi) == 0.0
    cal = year.assemble(lots, 2026, AS_OF).section("calendar").lines
    for n in (2, 3):
        line = next(ln for ln in cal if f"Q{n} estimated" in ln)
        assert "if required: not required" in line, line
        assert "withholding covers the safe harbor" in line, line
        assert "de minimis" not in line, line


@pytest.mark.engine
def test_planned_sales_and_conversion_tax_reach_the_cash_line(lots: Layout) -> None:  # noqa: F811
    base = glidepath.glide(lots, 2026, AS_OF)
    assert all(m.planned_in == 0.0 for m in base.months)
    ov = Overrides(planned_lt_sales=20_000.0, planned_conversion=30_000.0)
    g = glidepath.glide(lots, 2026, AS_OF, overrides=ov)
    # no --cash-in typed; the sale settles in the last business month of the year
    assert all(m.cash_in == 0.0 for m in g.months)
    dec = g.months[11]
    assert (dec.year, dec.month) == (2026, 12)
    # the 2019 lot carries 260,000 of gain in 560,000 of value: the most gain per
    # dollar, so the least cash for the planned gain
    assert dec.planned_in == round(20_000 * 560 / 260, 2)
    assert sum(m.planned_in for m in g.months) == dec.planned_in
    assert dec.net == round(
        base.months[11].net + dec.planned_in - (dec.est_tax - base.months[11].est_tax),
        2,
    )
    # the year's whole tax, conversion included, reaches the line: installments
    # through January plus the balance due with the return
    et = esttax.estimate(lots, 2026, AS_OF, ov)
    through_jan = sum(m.est_tax for m in g.months if (m.year, m.month) <= (2027, 1))
    due = sum(m.balance_due for m in g.months if m.year == 2027 and m.month == 4)
    assert through_jan + due == pytest.approx(
        sum(ag.current_tax - ag.withheld for ag in et.agencies), abs=0.02
    )
    base_et = esttax.estimate(lots, 2026, AS_OF)
    assert through_jan + due > sum(
        ag.current_tax - ag.withheld for ag in base_et.agencies
    )
    assert any("planned conversion" in n and "adds" in n for n in g.notes)
    assert any("planned_lt_sales" in n and "20,000" in n for n in g.notes)


@pytest.mark.engine
def test_planned_sale_without_a_lot_to_price_is_named(lots: Layout) -> None:  # noqa: F811
    # the only short-term lot sits at a loss: a planned short-term gain has no
    # lot to come from, so no proceeds are counted and the note says so
    g = glidepath.glide(
        lots, 2026, AS_OF, overrides=Overrides(planned_st_sales=5_000.0)
    )
    assert all(m.planned_in == 0.0 for m in g.months)
    assert any("planned_st_sales" in n and "no taxable lot" in n for n in g.notes), (
        g.notes
    )


def test_glide_panel_says_the_cash_falls_under_the_target(lay: Layout) -> None:  # noqa: F811
    """first_short_month is the first month under the cash target, not under zero:
    with 15,000 in the cash bucket the glide and cash panels say the same thing."""
    from planner.ledger import portfolio

    portfolio.save_account(lay, "22222222", balance=15_000.0)
    g = glidepath.glide(lay, 2026, AS_OF)
    assert g.first_short_month == "2026-01" and g.months[0].cash > 0
    lines = year._glide(lay, 2026, AS_OF, Overrides(), g).lines
    assert "cash line falls under the target in 2026-01" in lines
    assert not any("negative" in ln for ln in lines)
