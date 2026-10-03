"""Phase 4b: the spending band and the glide path with its monthly cash line,
on the synthetic household (single, NC, 55 at year end; a taxable account, a
cash account and a traditional IRA)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from planner.cli import app
from planner.ingest import ingest
from planner.ingest.needs import enter, profile_path
from planner.ledger import portfolio
from planner.paths import Layout
from planner.plan import glidepath, spending

runner = CliRunner()
AS_OF = date(2026, 7, 10)
FUND = "VANGUARD TOTAL STOCK MARKET INDEX ADMIRAL"
TXN = (
    "Account Number,Trade Date,Settlement Date,Transaction Type,"
    "Transaction Description,Investment Name,Symbol,Shares,Share Price,"
    "Principal Amount,Commission Fees,Net Amount,Accrued Interest,Account Type,"
)
DOWNLOAD = "\n".join(
    [
        "Account Number,Investment Name,Symbol,Shares,Share Price,Total Value,",
        f'11111111,{FUND},VTSAX,12500.0,112.00,"1,400,000.00",',
        "",
        TXN,
        f"11111111,03/14/2026,03/14/2026,Dividend,Dividend Received,{FUND},VTSAX,"
        "0.0,0.0,900.00,0.0,900.00,0.0,BROKERAGE,",
        f"11111111,06/12/2026,06/12/2026,Dividend,Dividend Received,{FUND},VTSAX,"
        "0.0,0.0,600.00,0.0,600.00,0.0,BROKERAGE,",
        f"33333333,06/20/2026,06/20/2026,Reinvestment,Dividend Reinvestment,{FUND},"
        "VTSAX,3.0,110.00,-330.00,0.0,-330.00,0.0,BROKERAGE,",
        "",
    ]
)
REALIZED = "\n".join(
    [
        "Account Number,Investment Name,Symbol,Date Acquired,Date Sold,Shares,"
        "Proceeds,Cost Basis,Term",
        f'11111111,{FUND},VTSAX,01/15/2019,06/02/2026,200.000,"22,000.00",'
        '"25,000.00",Long-term',
        f'11111111,{FUND},VTSAX,01/15/2019,02/02/2026,100.000,"12,000.00",'
        '"11,000.00",Long-term',
        "",
    ]
)
BANK = """\
Transaction ID,Date,Description,Amount,Account
T-1,01/05/2026,CLIENT PAYMENT ACME,"4,000.00",Checking
T-2,03/06/2026,CLIENT PAYMENT ACME,"8,000.00",Checking
T-3,03/07/2026,CARD PURCHASE,(45.10),Checking
"""


@pytest.fixture
def lay(planner_home: Path) -> Layout:
    lay = Layout(planner_home)
    lay.ensure()
    for key, text in (
        ("birth_date", "1971-06-15"),
        ("filing_status", "single"),
        ("state", "NC"),
        ("spending_floor", "50,000"),
        ("spending_ceiling", "65,000"),
        ("withdrawal_rate", "0.035"),
        ("inflation", "0.025"),
        ("return_floor", "0.0"),
        ("return_track", "0.04"),
        ("cash_target", "20,000"),
        ("ss_claim_age", "67"),
        ("ss_estimate_67", "2,500"),
        ("mortgage_monthly", "1,500"),
        ("premium_monthly", "300"),
        ("se_income", "80,000"),
        ("interest", "1,000"),
        ("ordinary_dividends", "3,000"),
        ("qualified_dividends", "3,000"),
    ):
        enter(lay, 2026, key, text)
    prof = yaml.safe_load(profile_path(lay).read_text(encoding="utf-8"))
    prof["irregular"] = [{"label": "property tax", "month": 9, "amount": 2400}]
    profile_path(lay).write_text(yaml.safe_dump(prof), encoding="utf-8")
    portfolio.save_account(lay, "11111111", type="taxable")
    portfolio.save_account(lay, "22222222", type="cash", balance=200_000.0)
    portfolio.save_account(lay, "33333333", type="trad_ira", balance=200_000.0)
    for name, text in (
        ("dl.csv", DOWNLOAD),
        ("real.csv", REALIZED),
        ("bank.csv", BANK),
    ):
        (lay.data / "inbox" / name).write_text(text, encoding="utf-8")
    ingest(lay)
    return lay


def test_spending_band_clamps_and_the_drawdown_rule(lay: Layout) -> None:
    sp = spending.plan(lay, 2026, AS_OF, years=3)
    assert sp.balance == 1_800_000.0 and sp.spending == 63_000.0
    assert not sp.in_drawdown and sp.peak == 1_800_000.0
    assert [rw.year for rw in sp.rows] == [2026, 2027, 2028]
    assert sp.rows[0].spend_track == 63_000.0 and sp.rows[0].age == 55
    assert sp.rows[1].balance_track == round((1_800_000 - 63_000) * 1.04, 2)
    assert sp.rows[1].balance_floor == 1_800_000 - 63_000
    # the ceiling binds on a big balance, the floor on a small one
    assert spending.plan(lay, 2026, AS_OF, balance=3_000_000.0).spending == 65_000.0
    assert spending.plan(lay, 2026, AS_OF, balance=900_000.0).spending == 50_000.0
    # a balance under 90% of the inflation-adjusted peak holds at the floor
    low = spending.plan(lay, 2026, AS_OF, balance=1_500_000.0)
    assert low.in_drawdown and low.spending == 50_000.0
    assert low.peak == 1_800_000.0 and any("drawdown" in n for n in low.notes)
    assert spending.adjusted_peak(
        100_000.0, "2025-07-10", AS_OF, 0.025
    ) == pytest.approx(102_500.0, abs=10)


def test_glide_path_table_stresses_and_monthly_cash(lay: Layout) -> None:
    g = glidepath.glide(lay, 2026, AS_OF)
    assert g.age == 55 and g.balance == 1_800_000.0
    assert g.access_age == 59.5 and 4.9 < g.years_to_access < 5.0
    # accessible = taxable + cash; the floor through 59.5 at a 0% floor return
    assert g.accessible == 1_600_000.0
    assert g.floor_needed == pytest.approx(50_000 * g.years_to_access, abs=300)
    assert g.floor_shortfall == 0.0
    assert [rw.age for rw in g.rows][:2] == [55, 56] and g.rows[-1].age == 95
    first, at67 = g.rows[0], next(rw for rw in g.rows if rw.age == 67)
    assert first.ss == 0.0 and first.withdrawal == first.spend == 63_000.0
    assert first.balance_nominal == first.balance_real
    assert at67.ss == 30_000.0 and at67.withdrawal == round(at67.spend - 30_000, 2)
    assert at67.balance_nominal == round(at67.balance_real * 1.025**12, 2)
    assert [s.name for s in g.stresses] == [
        "30% drop in year one",
        "5% inflation",
        "floor returns",
    ]
    assert all(s.runs_out_age is None for s in g.stresses)
    assert g.stresses[2].balance_at_horizon < g.stresses[0].balance_at_horizon
    # months: 24 rows; actual months carry the ledger's rows, later months the run-rate
    assert len(g.months) == 24 and g.months[0].actual and not g.months[7].actual
    jan, mar, sep = g.months[0], g.months[2], g.months[8]
    assert (jan.se, mar.se, mar.dividends) == (4_000.0, 8_000.0, 900.0)
    assert sep.irregular == 2_400.0 and jan.irregular == 0.0
    assert g.months[12].irregular == 0.0 or g.months[20].irregular == 2_400.0
    assert jan.mortgage == 1_500.0 and jan.premiums == 300.0
    assert jan.living == round(63_000 / 12, 2)
    assert g.months[3].est_tax > 0 and jan.est_tax == 0.0 and g.months[12].est_tax > 0
    assert g.months[7].se == round(12_000 / 7, 2)  # seven months done, two deposits
    assert jan.cash == round(200_000 + jan.net, 2)
    assert g.first_short_month is None
    # a cash-in in a month lands there; a gift that keeps the bucket above target
    g2 = glidepath.glide(lay, 2026, AS_OF, cash_in={"2026-11": 25_000.0})
    assert g2.months[10].cash_in == 25_000.0
    # a small cash bucket trips the target
    portfolio.save_account(lay, "22222222", balance=15_000.0)
    g3 = glidepath.glide(lay, 2026, AS_OF)
    assert g3.first_short_month == "2026-01"
    assert any("bridge is not funded" in n for n in g3.notes)


def test_glide_ss_fallback_and_accessible_shortfall(lay: Layout) -> None:
    enter(lay, 2026, "ss_claim_age", "70")
    g = glidepath.glide(lay, 2026, AS_OF, balance=300_000.0)
    assert any("ss_estimate_67 stands in for 70" in n for n in g.notes)
    assert next(rw for rw in g.rows if rw.age == 70).ss == 30_000.0
    # the holdings snapshot owns the taxable balance; retype the account instead
    portfolio.save_account(lay, "11111111", type="trad_ira")
    g = glidepath.glide(lay, 2026, AS_OF)
    assert g.accessible == 200_000.0 and g.floor_shortfall == round(
        g.floor_needed - 200_000, 2
    )
    assert any("short of the floor" in n for n in g.notes)


def test_cli_spend_and_glide(lay: Layout) -> None:
    r = runner.invoke(app, ["spend", "--year", "2026", "--as-of", "2026-07-10"])
    assert r.exit_code == 0, r.output
    assert "spending 63,000.00" in r.output and "2035" in r.output
    r = runner.invoke(
        app,
        [
            "glide",
            "--year",
            "2026",
            "--as-of",
            "2026-07-10",
            "--cash-in",
            "2026-11:25,000",
        ],
    )
    assert r.exit_code == 0, r.output
    assert "covered" in r.output and "stress floor returns" in r.output
    assert "2026-11  " in r.output and "25,000.00" in r.output
