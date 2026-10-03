"""Phase 4a: the ledger as a household, the MAGI projection and the conversion
sizer, on the synthetic household (single, NC, 55 at year end, $1,000 interest,
$3,000 qualified dividends, a $40,000 conversion already recorded)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from planner.cli import app
from planner.engine.household import Household, MissingInputError
from planner.engine.tax import compute
from planner.ingest.needs import enter
from planner.ledger import db, portfolio
from planner.paths import Layout
from planner.plan import conversion, inputs, magi

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
    )
    assert inp.origins["roth_conversion"] == "conversions recorded this year"
    assert "wages" in inp.unknown and "qualified_dividends" not in inp.unknown
    assert inp.notes == []
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
    assert by["ACA 400% FPL cliff"].limit == pytest.approx(4 * pj.result.fpg)
    assert by["Medicaid 138% FPL (monthly)"].over is True
    assert by["NIIT"].limit == 200000 and by["NIIT"].over is False
    with_hsa = magi.project(lay, 2026, inputs.Overrides(planned_hsa=4400))
    assert with_hsa.result.agi == pytest.approx(pj.result.agi - 4400)


@pytest.mark.engine
def test_conversion_candidates_are_priced_from_one_sweep(lay: Layout) -> None:
    enter(lay, 2026, "conversion_margin", "1,000")
    enter(lay, 2026, "conversion_cap", "120,000")
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
