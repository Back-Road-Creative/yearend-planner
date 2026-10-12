"""Phase 4a: the ledger as a household, the MAGI projection and the conversion
sizer, on the synthetic household (single, NC, 55 at year end, $1,000 interest,
$3,000 qualified dividends, a $40,000 conversion already recorded)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from planner.cli import app
from planner.engine.household import Household, MissingInputError
from planner.engine.tax import compute
from planner.ingest.needs import enter, needed
from planner.ledger import db, portfolio
from planner.paths import Layout
from planner.plan import conversion, inputs, levers, magi

runner = CliRunner()


@pytest.fixture
def lay(planner_home: Path) -> Layout:
    lay = Layout(planner_home)
    lay.ensure()
    for key, text in (
        ("birth_date", "1971-06-15"),
        ("filing_status", "single"),
        ("state", "NC"),
        ("interest", "1,000"),
        ("ordinary_dividends", "3,000"),
        ("qualified_dividends", "3,000"),
    ):
        enter(lay, 2026, key, text)
    portfolio.save_account(lay, "33333333", type="trad_ira", balance=200000.0)
    conn = db.connect(lay.data / "ledger" / "planner.db")
    db.add_conversion(
        conn,
        date="2026-03-01",
        amount_cents=4_000_000,
        taxable_cents=4_000_000,
        source_account="33333333",
        accessible_date="2031-01-01",
    )
    conn.commit()
    conn.close()
    return lay


def test_inputs_mirror_the_needed_panel_and_the_recorded_conversions(
    lay: Layout,
) -> None:
    inp = inputs.build(lay, 2026)
    assert inp.household == Household(
        age=55,
        filing_status="SINGLE",
        state="NC",
        interest=1000,
        qualified_dividends=3000,
        non_qualified_dividends=0,
        roth_conversion=40000,
        savers_eligible=True,
    )
    assert inp.origins["roth_conversion"] == "conversions recorded this year"
    assert "wages" in inp.unknown and "qualified_dividends" not in inp.unknown
    # the recorded conversion with no Form 8606 basis typed is taxed in full
    assert inp.notes == [
        "ira_basis not given: your IRA distributions and conversions are taxed in "
        "full, as if there were no basis (type none when there is none)"
    ]
    # unknown qualified dividends: priced as ordinary, and said so
    from planner.ingest.needs import _write, load_manual, manual_path

    data = load_manual(lay, 2026)
    del data["values"]["qualified_dividends"]
    _write(manual_path(lay, 2026), data)
    inp = inputs.build(lay, 2026)
    assert inp.household.non_qualified_dividends == 3000
    assert inp.household.qualified_dividends == 0
    assert any("qualified_dividends unknown" in n for n in inp.notes)
    ov = inputs.Overrides(
        q4_dividend_estimate=500, planned_lt_sales=10000, planned_conversion=5000
    )
    inp = inputs.build(lay, 2026, ov)
    assert inp.household.non_qualified_dividends == 3500
    assert inp.household.long_term_gains == 10000
    assert inp.household.roth_conversion == 45000
    assert ov.describe() == [
        "q4_dividend_estimate 500",
        "planned_lt_sales 10,000",
        "planned_conversion 5,000",
    ]


def test_inputs_refuse_without_the_profile_basics(planner_home: Path) -> None:
    lay = Layout(planner_home)
    lay.ensure()
    with pytest.raises(MissingInputError, match="birth_date, filing_status, state"):
        inputs.build(lay, 2026)


@pytest.mark.parametrize(
    ("typed", "words"),
    [
        ("married_joint", "spouse"),
        ("married_separate", "spouse"),
        ("head_of_household", "qualifying person"),
    ],
)
def test_a_household_of_more_than_one_is_tagged_not_handled(
    lay: Layout, typed: str, words: str
) -> None:
    """Only one person is modelled: a status whose answer turns on a spouse or
    a dependent says so on every output instead of passing for a full plan."""
    enter(lay, 2026, "filing_status", typed)
    inp = inputs.build(lay, 2026)
    (gap,) = inp.scope
    assert gap.startswith("Not handled:") and words in gap
    assert gap in inp.notes


def test_a_single_filer_has_no_scope_gap(lay: Layout) -> None:
    inp = inputs.build(lay, 2026)
    assert inp.scope == [] and not any(n.startswith("Not handled") for n in inp.notes)


@pytest.mark.engine
def test_projection_is_the_engine_on_the_fixture_household(lay: Layout) -> None:
    pj = magi.project(lay, 2026)
    assert pj.result == compute(2026, pj.inputs.household)
    assert pj.aca_addbacks == 0
    by = {ln.name: ln for ln in pj.lines}
    assert by["standard deduction"].limit == 16100
    assert by["0% LTCG / qualified-dividend ceiling"].room == pytest.approx(
        49450 - pj.result.taxable_income
    )
    # the credit's lines use the guideline of the year before (Form 8962 line 4):
    # 2025's $15,650, so 250% = 39,125 and 400% = 62,600; Medicaid uses 2026's
    assert pj.result.aca_fpg == 15650 and pj.result.fpg == 15960
    assert by["ACA 400% FPL cliff"].limit == 62600
    assert by["ACA CSR 250% FPL"].limit == 39125
    assert by["Medicaid 138% FPL (monthly)"].limit == pytest.approx(1835.40)
    assert by["Medicaid 138% FPL (monthly)"].over is True
    assert by["NIIT"].limit == 200000 and by["NIIT"].over is False
    with_hsa = magi.project(lay, 2026, inputs.Overrides(planned_hsa=4400))
    assert with_hsa.result.agi == pytest.approx(pj.result.agi - 4400)


def _add_box_12(lay: Layout, amount: float) -> None:
    """A synthetic 1099-DIV whose only figure is box 12 (exempt-interest dividends)."""
    conn = db.connect(lay.data / "ledger" / "planner.db")
    db.add_document(
        conn,
        fingerprint="synthetic-div-12",
        file_name="synthetic-div.pdf",
        kind="pdf",
        pages=1,
        batch="b2",
        facts=[
            db.Fact(
                "1099-DIV", 2026, "Example Fund (synthetic)", "12", "Exempt", amount, 1
            )
        ],
    )
    conn.close()


@pytest.mark.engine
def test_aca_magi_adds_exempt_interest(lay: Layout) -> None:
    before = magi.project(lay, 2026)
    assert before.inputs.household.tax_exempt_interest == 0
    assert "tax_exempt_interest" in before.inputs.unknown
    _add_box_12(lay, 6_000.0)
    pj = magi.project(lay, 2026)
    assert pj.inputs.household.tax_exempt_interest == 6000
    assert pj.inputs.origins["tax_exempt_interest"].startswith("1099-DIV 2026")
    # box 12 is not taxable income: AGI is unchanged, ACA MAGI is AGI plus it
    assert pj.result.agi == pytest.approx(before.result.agi)
    assert pj.result.aca_magi == pytest.approx(pj.result.agi + 6000)
    assert pj.aca_addbacks == pytest.approx(6000)
    by, was = {ln.name: ln for ln in pj.lines}, {ln.name: ln for ln in before.lines}
    for name in ("ACA CSR 250% FPL", "ACA 400% FPL cliff"):
        assert by[name].room == pytest.approx(was[name].room - 6000)
    assert by["Medicaid 138% FPL (monthly)"].value == pytest.approx(
        was["Medicaid 138% FPL (monthly)"].value + 500
    )


@pytest.mark.engine
def test_conversion_room_shrinks_by_the_exempt_interest(lay: Layout) -> None:
    enter(lay, 2026, "conversion_margin", "1,000")
    enter(lay, 2026, "conversion_cap", "120,000")
    base = {c.name: c for c in conversion.size(lay, 2026, step=1000).candidates}
    _add_box_12(lay, 6_000.0)
    shrunk = {c.name: c for c in conversion.size(lay, 2026, step=1000).candidates}
    assert shrunk["aca_400"].amount == base["aca_400"].amount - 6000
    assert shrunk["aca_400"].aca_magi == pytest.approx(base["aca_400"].aca_magi)


def test_total_income_override_sets_wages_from_the_other_lines(lay: Layout) -> None:
    # the ledger counts 1,000 interest + 3,000 qualified dividends + 40,000
    # recorded conversion = 44,000 besides wages
    ov = inputs.Overrides(total_income=100_000, planned_lt_sales=2_000)
    inp = inputs.build(lay, 2026, ov)
    assert inp.household.wages == 56_000
    assert inp.household.long_term_gains == 2_000  # planned items add on top
    assert inp.origins["wages"].startswith("total_income override")
    assert "wages" not in inp.unknown
    assert any("total_income 100,000" in n for n in inp.notes)
    assert ov.describe() == ["planned_lt_sales 2,000", "total_income 100,000"]
    with pytest.raises(inputs.OverrideError, match="below the other income"):
        inputs.build(lay, 2026, inputs.Overrides(total_income=30_000))
    with pytest.raises(inputs.OverrideError, match="conversion_target"):
        inputs.Overrides(conversion_target="sometimes")


def test_total_income_counts_a_net_capital_loss_only_up_to_the_limit(
    lay: Layout,
) -> None:
    # Form 1040 line 7 carries a net capital loss only up to 3,000 (1,500 MFS):
    # IRC 1211(b), Schedule D line 21. A 10,000 loss must not push wages up by
    # 10,000, or the page's total income would not be the typed figure.
    enter(lay, 2026, "short_term_gains", "(10,000)")
    inp = inputs.build(lay, 2026, inputs.Overrides(total_income=100_000))
    hh = inp.household
    assert hh.short_term_gains == -10_000
    # 1,000 interest + 3,000 qualified dividends + 40,000 conversion, less the
    # loss counted on line 7 (3,000 of the 10,000)
    assert hh.wages == 100_000 - 44_000 + 3_000
    line_9 = (
        hh.wages
        + hh.interest
        + hh.qualified_dividends
        + hh.non_qualified_dividends
        + hh.roth_conversion
        + max(hh.short_term_gains + hh.long_term_gains, -3_000)
    )
    assert line_9 == 100_000
    # a long-term gain nets against the short-term loss before the limit applies
    enter(lay, 2026, "long_term_gains", "8,000")
    hh = inputs.build(lay, 2026, inputs.Overrides(total_income=100_000)).household
    assert hh.wages == 100_000 - 44_000 + 2_000
    # married filing separately: the limit is 1,500
    enter(lay, 2026, "filing_status", "married_separate")
    enter(lay, 2026, "long_term_gains", "0")
    hh = inputs.build(lay, 2026, inputs.Overrides(total_income=100_000)).household
    assert hh.wages == 100_000 - 44_000 + 1_500


@pytest.mark.engine
def test_conversion_candidates_are_priced_from_one_sweep(lay: Layout) -> None:
    enter(lay, 2026, "conversion_margin", "1,000")
    enter(lay, 2026, "conversion_cap", "120,000")
    enter(lay, 2026, "slcsp_monthly", "800")
    sz = conversion.size(lay, 2026, step=1000)
    assert sz.already == 40000 and sz.cap == 120000 and sz.trad_ira_balance == 200000
    by = {c.name: c for c in sz.candidates}
    assert set(by) >= {"ltcg_0pct", "bracket_12", "aca_400", "medicaid_over", "cap"}
    fill = by["ltcg_0pct"]
    assert fill.amount > 40000
    assert fill.taxable_income <= 49450 - 1000
    assert fill.taxable_income + 1000 > 49450 - 1000  # the next step would cross
    assert fill.fed_delta > 0 and fill.state_delta > 0
    assert fill.cash_needed == pytest.approx(
        fill.fed_delta + fill.state_delta - fill.ptc_delta
    )
    assert fill.qualified_spill is False
    # the credit's cliff is 400% of LAST year's guideline (IRC 36B, Form 8962 line 4):
    # 4 x 15,650 = 62,600, less the 1,000 margin = 61,600. Priced from this year's
    # guideline (15,960) it would be 63,840 - 1,000 and land past the real line.
    aca = by["aca_400"]
    assert aca.aca_magi <= 62600 - 1000
    assert aca.aca_magi + 1000 > 62600 - 1000  # the next step would cross
    assert aca.ptc_delta > -sz.base.result.aca_ptc  # the credit is kept, not lost
    assert by["bracket_12"].amount >= fill.amount
    assert by["cap"].amount == 120000
    assert by["cap"].qualified_spill is True
    # recurring income is ~333/month; the Medicaid line is ~1,800/month, margin 1,000
    assert by["medicaid_under"].amount == 40000  # nothing more this month
    assert by["medicaid_under"].medicaid_month_over is False
    assert by["medicaid_over"].amount == 43000
    assert by["medicaid_over"].medicaid_month_over is True
    assert sz.recommendation is None
    assert any("conversion_objective not set" in n for n in sz.notes)
    enter(lay, 2026, "conversion_objective", "ltcg_0pct")
    sz = conversion.size(lay, 2026, step=1000)
    assert sz.recommendation is not None and sz.recommendation.name == "ltcg_0pct"


@pytest.mark.engine
def test_spill_only_when_conversion_adds_15pct(lay: Layout) -> None:
    # 70,000 converted and 60,000 of long-term gains: the ordinary income alone
    # is past the 0% ceiling, so every gain dollar is already taxed at 15% and
    # no larger conversion adds any gains tax. Comparing only the run with the
    # conversion against zero would call each of these a spill.
    conn = db.connect(lay.data / "ledger" / "planner.db")
    db.add_conversion(
        conn,
        date="2026-04-01",
        amount_cents=3_000_000,
        taxable_cents=3_000_000,
        source_account="33333333",
        accessible_date="2031-04-01",
    )
    conn.commit()
    conn.close()
    enter(lay, 2026, "long_term_gains", "60,000")
    enter(lay, 2026, "conversion_cap", "150,000")
    sz = conversion.size(lay, 2026, step=5000)
    assert sz.already == 70000 and sz.base.result.ltcg_tax > 0
    by = {c.name: c for c in sz.candidates}
    assert by["cap"].amount == 150000 > sz.already
    assert by["cap"].fed_delta > 0  # the conversion still costs ordinary tax
    assert [c.name for c in sz.candidates if c.qualified_spill] == []


def test_aca_400_candidate_may_sit_on_the_line_at_margin_zero(
    lay: Layout,
) -> None:
    """With no margin a sweep row can land exactly on 400% (62,600). Income that does
    not exceed 400% keeps the credit (IRC 36B(c)(1)(A)), so that row is "under"."""
    enter(lay, 2026, "conversion_cap", "58,600")
    enter(lay, 2026, "slcsp_monthly", "800")
    # recurring MAGI is 4,000 (interest + dividends), so a 58,600 conversion is
    # exactly 62,600; a 2,930 step puts a sweep row on it
    sz = conversion.size(lay, 2026, step=2930)
    assert sz.margin == 0
    aca = {c.name: c for c in sz.candidates}["aca_400"]
    assert aca.aca_magi == 62600  # on the line, not one step short of it
    assert aca.ptc_delta > -sz.base.result.aca_ptc  # the credit is kept
    assert aca.aca_ptc > 0


@pytest.mark.engine
def test_cli_magi_and_conversions(lay: Layout) -> None:
    enter(lay, 2026, "conversion_objective", "bracket_12")
    env = {"PLANNER_HOME": str(lay.root)}
    r = runner.invoke(app, ["magi", "--year", "2026", "--sales-lt", "2000"], env=env)
    assert r.exit_code == 0, r.output
    assert "2026 SINGLE NC age 55" in r.output
    assert "override  planned_lt_sales 2,000" in r.output
    assert "0% LTCG / qualified-dividend ceiling" in r.output
    assert "unknown (left out, not zero): " in r.output and "wages" in r.output
    r = runner.invoke(app, ["conversions", "--year", "2026", "--step", "2000"], env=env)
    assert r.exit_code == 0, r.output
    assert "already converted 40,000.00; traditional IRA 200,000.00" in r.output
    assert "* bracket_12" in r.output
    assert "recommendation: bracket_12" in r.output
    assert "cash needed" in r.output


def test_a_typed_county_reaches_the_engine_by_its_engine_name(lay: Layout) -> None:
    enter(lay, 2026, "county", "Macon")
    assert inputs.build(lay, 2026).household.county == "MACON_COUNTY_NC"
    enter(lay, 2026, "county", "Wake County, NC")
    assert inputs.build(lay, 2026).household.county == "WAKE_COUNTY_NC"


def test_a_county_that_is_not_the_states_blocks_the_plan_with_the_reason(
    lay: Layout,
) -> None:
    enter(lay, 2026, "county", "Harris")
    with pytest.raises(MissingInputError, match="not a county of NC"):
        inputs.build(lay, 2026)


@pytest.mark.engine
def test_levers_room_shrinks_by_the_exempt_interest(lay: Layout) -> None:
    ctx, found, _ = levers.catalog(lay, 2026)
    by = {lv.key: lv for lv in found}
    assert ctx.room_line is not None and ctx.room is not None
    assert by["conversion"].available
    _add_box_12(lay, 6_000.0)
    ctx2, found2, _ = levers.catalog(lay, 2026)
    by2 = {lv.key: lv for lv in found2}
    assert ctx2.room_line is not None and ctx2.room_line.name == ctx.room_line.name
    assert ctx2.room == pytest.approx(ctx.room - 6000)
    assert by2["conversion"].amount == pytest.approx(by["conversion"].amount - 6000)
    assert levers.menu(lay, 2026).room == pytest.approx(ctx.room - 6000)


def test_watch_lines_present(lay: Layout) -> None:
    """The safe-harbor 110% trigger and the NC rate step are watched every
    year; IRMAA only from 63 (its two-year lookback), Social Security taxation
    only with benefits, the Medicaid work requirement only from its start year
    and only when the household targets the Medicaid line."""
    pj = magi.project(lay, 2026)
    res = pj.result
    by = {ln.name: ln for ln in pj.lines}
    sh = by["safe harbor 110% for 2027 (AGI over 150,000)"]
    assert (sh.limit, sh.measure, sh.value, sh.direction) == (
        150000,
        "AGI",
        res.agi,
        "watch",
    )
    nc = by["NC rate 3.99%, 3.49% scheduled 2027"]
    assert nc.value == res.state_tax and nc.direction == "watch"
    assert nc.limit == pytest.approx(res.state_tax * 0.0349 / 0.0399, abs=0.01)
    assert not [
        n for n in by if n.startswith(("IRMAA", "Social Security", "Medicaid work"))
    ]

    enter(lay, 2026, "birth_date", "1963-03-01")  # 63 at the end of 2026
    enter(lay, 2026, "social_security", "24,000")
    pj = magi.project(lay, 2026)
    res = pj.result
    by = {ln.name: ln for ln in pj.lines}
    irmaa = by["IRMAA first tier (Medicare premiums in 2028)"]
    assert (irmaa.limit, irmaa.value, irmaa.direction) == (109000, res.agi, "get under")
    untaxed = res.aca_magi - res.agi  # no exempt interest here
    provisional = res.agi - (24000 - untaxed) + 12000
    half, most = by["Social Security 50% taxable"], by["Social Security 85% taxable"]
    assert (half.limit, most.limit) == (25000, 34000)
    assert half.measure == "provisional income"
    assert half.value == pytest.approx(provisional, abs=0.01)

    # the work requirement: from its start year, with the Medicaid objective
    w = magi.Watch(
        work_requirement_from=2027, medicaid_target=True, se_hours={1: 85, 2: 60}
    )
    assert not [
        ln for ln in magi.lines(res, "SINGLE", w) if ln.name.startswith("Medicaid work")
    ]
    (work,) = [
        ln
        for ln in magi.lines(replace(res, year=2027), "SINGLE", w)
        if ln.name.startswith("Medicaid work")
    ]
    assert (work.limit, work.value, work.over) == (80, 60, False)
    assert work.measure == "fewest SE hours logged in a month"
    quiet = replace(w, medicaid_target=False)
    assert not [
        ln
        for ln in magi.lines(replace(res, year=2027), "SINGLE", quiet)
        if ln.name.startswith("Medicaid work")
    ]


def test_se_hours_log_asked_from_2027_when_targeting_medicaid(lay: Layout) -> None:
    def keys(year: int) -> set[str]:
        return {s.need.key for s in needed(lay, year).items}

    assert "se_hours" not in keys(2027)
    enter(lay, 2027, "conversion_objective", "medicaid_under")
    assert "se_hours" in keys(2027) and "se_hours" not in keys(2026)
    assert enter(lay, 2027, "se_hours", "1:85, 2: 60") == {1: 85.0, 2: 60.0}
    with pytest.raises(ValueError, match="month"):
        enter(lay, 2027, "se_hours", "13:5")
    w = magi.watch_for(lay, 2027, inputs.build(lay, 2026))
    assert (w.work_requirement_from, w.medicaid_target) == (2027, True)
    assert w.se_hours == {1: 85.0, 2: 60.0}
