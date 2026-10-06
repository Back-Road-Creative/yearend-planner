"""Phase 4b: the spending band and the glide path with its monthly cash line,
on the synthetic household (single, NC, 55 at year end; a taxable account, a
cash account and a traditional IRA)."""

from __future__ import annotations

from dataclasses import replace
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
from planner.taxprep import schedule_c

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
    schedule_c.add_rule(lay, "CLIENT PAYMENT", "receipts")
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
    # estimated tax is esttax's: nothing was paid by April, so April shows none and
    # the September installment makes up the first three quarters (tests/test_esttax.py)
    assert g.months[3].est_tax == 0.0 and jan.est_tax == 0.0
    assert sep.est_tax > 0 and g.months[12].est_tax > 0
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


def test_glide_comfort_floor_line_and_band(lay: Layout) -> None:
    g = glidepath.glide(lay, 2026, AS_OF)
    # the comfort-floor line is the same rule run at the pessimistic floor return
    assert len(g.floor_rows) == len(g.rows)
    assert [rw.age for rw in g.floor_rows] == [rw.age for rw in g.rows]
    assert g.floor_rows[0].balance_real == g.rows[0].balance_real
    assert g.floor_rows[1].balance_real == 1_800_000 - 63_000  # 0% floor return
    assert g.floor_rows[-1].balance_real == g.stresses[2].balance_at_horizon
    assert g.floor_rows[-1].balance_real < g.rows[-1].balance_real
    # which band the household is in: inside, at the floor, at the ceiling, drawdown
    assert g.band == "inside the band" and g.spending == 63_000.0
    assert glidepath.glide(lay, 2026, AS_OF, balance=3_000_000.0).band == (
        "at the ceiling"
    )
    for low in (900_000.0, 1_500_000.0):  # under 90% of the 1.8M peak
        assert glidepath.glide(lay, 2026, AS_OF, balance=low).band == (
            "drawdown, held at the floor"
        )
    # the glide table agrees with the band label: under 90% of the (inflation-
    # adjusted) peak, year one is spent at the floor on both lines, and the
    # comfort-floor line matches the spending panel's floor-return column
    held = glidepath.glide(lay, 2026, AS_OF, balance=1_500_000.0)
    banded = spending.plan(lay, 2026, AS_OF, balance=1_500_000.0)
    assert held.rows[0].spend == held.floor_rows[0].spend == held.spending == 50_000.0
    for i in range(3):
        assert held.floor_rows[i].spend == banded.rows[i].spend_floor
        assert held.floor_rows[i].balance_real == banded.rows[i].balance_floor
    # at the floor without a drawdown (the rate times the balance is under it)
    sp = spending.plan(lay, 2026, AS_OF)
    pinned = replace(sp, spending=sp.floor)
    assert glidepath.band_name(pinned) == "at the floor"


def test_glide_ss_fallback_and_accessible_shortfall(lay: Layout) -> None:
    enter(lay, 2026, "ss_claim_age", "70")
    g = glidepath.glide(lay, 2026, AS_OF, balance=300_000.0)
    assert any("the age-70 benefit 3,100.00/month" in n for n in g.notes)
    assert next(rw for rw in g.rows if rw.age == 70).ss == 37_200.0  # 24% credits
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
    assert "comfort floor" in r.output and "(inside the band)" in r.output
    assert "2026-11  " in r.output and "25,000.00" in r.output


def test_a_balance_above_the_ledger_peak_is_the_peak(lay: Layout) -> None:
    """The ledger peak is 1.8M; a 3M January balance is the new peak, so a 30% drop
    in year one (2.1M, under 90% of 3M) holds spending at the floor."""
    rows = glidepath.run(
        2026,
        55,
        3_000_000.0,
        0.035,
        50_000.0,
        65_000.0,
        0.0,
        0.0,
        {},
        drop_year1=0.3,
        peak=1_800_000.0,
    )
    assert rows[0].spend == 50_000.0
    g = glidepath.glide(lay, 2026, AS_OF, balance=3_000_000.0)
    sp = spending.plan(lay, 2026, AS_OF, balance=3_000_000.0)
    assert sp.peak_adjusted == 3_000_000.0
    # the comfort-floor line (0% return) holds at the floor once under 2.7M
    held = [rw for rw in g.floor_rows if rw.balance_real < 2_700_000.0]
    assert held and all(rw.spend == 50_000.0 for rw in held)
    assert all(
        rw.spend_floor == 50_000.0 for rw in sp.rows if rw.balance_floor < 2_700_000.0
    )


def test_a_dated_cash_balance_anchors_the_line_once(lay: Layout) -> None:
    """A June 30 statement already holds January's and March's deposits: the
    line shows the statement in June and works earlier months back from it."""
    portfolio.save_account(lay, "22222222", balance_date="2026-06-30")
    g = glidepath.glide(lay, 2026, AS_OF)
    m = g.months
    assert m[5].cash == 200_000.0
    assert m[6].cash == round(200_000 + m[6].net, 2)
    assert m[4].cash == round(200_000 - m[5].net, 2)
    assert m[0].cash == pytest.approx(200_000 - sum(x.net for x in m[1:6]), abs=0.05)
    assert any("2026-06-30" in n and "worked back" in n for n in g.notes)
    # a mid-month statement: the rest of its month runs forward from it
    portfolio.save_account(lay, "22222222", balance_date="2026-06-15")
    m = glidepath.glide(lay, 2026, AS_OF).months
    assert m[5].cash == round(200_000 + m[5].net * 15 / 30, 2)
    assert m[4].cash == round(200_000 - m[5].net * 15 / 30, 2)
    # an undated balance still starts the line on January 1, and says so
    portfolio.save_account(lay, "22222222", balance_date="")
    g = glidepath.glide(lay, 2026, AS_OF)
    assert g.months[0].cash == round(200_000 + g.months[0].net, 2)
    assert any("no date" in n and "balance_date" in n for n in g.notes)


MORE_BANK = """\
Transaction ID,Date,Description,Amount,Account
T-4,04/02/2026,PAYROLL EMPLOYER,"2,000.00",Checking
T-5,04/09/2026,IRS TREAS REFUND,500.00,Checking
T-6,05/01/2026,LOAN ADVANCE,"10,000.00",Checking
T-7,05/03/2026,FROM SAVINGS,"3,000.00",Checking
T-8,06/01/2026,MYSTERY DEPOSIT,700.00,Checking
"""


def test_only_deposits_classed_receipts_are_business_income(lay: Layout) -> None:
    (lay.data / "inbox" / "bank2.csv").write_text(MORE_BANK, encoding="utf-8")
    ingest(lay)
    for match, category in (
        ("CLIENT PAYMENT", "receipts"),
        ("PAYROLL", "pay"),
        ("REFUND", "refund"),
        ("LOAN", "loan"),
        ("FROM SAVINGS", "transfer"),
    ):
        schedule_c.add_rule(lay, match, category)
    g = glidepath.glide(lay, 2026, AS_OF)
    apr, may, jun = g.months[3], g.months[4], g.months[5]
    assert (g.months[0].se, g.months[0].other_in) == (4_000.0, 0.0)
    assert (apr.se, apr.other_in) == (0.0, 2_500.0)
    assert (may.se, may.other_in) == (0.0, 10_000.0)  # the transfer is left out
    assert (jun.se, jun.other_in) == (0.0, 700.0)  # unclassed: counted once
    assert jun.net == round(
        700.0
        + jun.dividends
        - (
            jun.living
            + jun.mortgage
            + jun.premiums
            + jun.est_tax
            + jun.balance_due
            + jun.irregular
        ),
        2,
    )
    # the run rate carries receipts and pay forward, never a one-off
    assert g.months[7].se == round(12_000 / 7, 2)
    assert g.months[7].other_in == round(2_000 / 7, 2)
    assert any(
        "700.00" in n and "not business income" in n and "planner categorize" in n
        for n in g.notes
    )
    assert any("3,000.00" in n and "transfer" in n for n in g.notes)
