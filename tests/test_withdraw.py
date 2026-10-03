"""Phase 4c: raising cash from lots and wash-sale flags, on the synthetic
household of test_spending plus a cost-basis export."""

from __future__ import annotations

from datetime import date

import pytest
from typer.testing import CliRunner

from planner.cli import app
from planner.ingest import ingest
from planner.ledger import db, portfolio
from planner.paths import Layout
from planner.plan import washsale, withdraw
from tests.test_spending import AS_OF, FUND, lay  # noqa: F401

runner = CliRunner()
COST_BASIS = "\n".join(
    [
        "Account Number,Investment Name,Symbol,Date Acquired,Shares,Total Cost,"
        "Market Value,Term",
        f'11111111,{FUND},VTSAX,01/15/2019,5000.000,"300,000.00","560,000.00",'
        "Long-term",
        f'11111111,{FUND},VTSAX,03/01/2022,5000.000,"500,000.00","560,000.00",'
        "Long-term",
        f'11111111,{FUND},VTSAX,11/20/2025,2500.000,"290,000.00","280,000.00",'
        "Short-term",
        f'33333333,{FUND},VTSAX,01/15/2019,1000.000,"50,000.00","112,000.00",Long-term',
        "",
    ]
)


@pytest.fixture
def lots(lay: Layout) -> Layout:  # noqa: F811
    (lay.data / "inbox" / "basis.csv").write_text(COST_BASIS, encoding="utf-8")
    ingest(lay)
    assert portfolio.status(lay, 2026, AS_OF).lots
    return lay


def test_withdraw_cash_first_then_cheapest_gain_lots(lots: Layout) -> None:
    # cash already covers the target: nothing to sell
    w = withdraw.pick(lots, 2026, as_of=AS_OF)
    assert (w.target, w.cash_now, w.need, w.sales) == (20_000.0, 200_000.0, 0.0, [])
    # a bigger target: the loss lot first (least gain per dollar), then the
    # 2022 lot (basis 500k on 560k), never the IRA's lot
    w = withdraw.pick(lots, 2026, target=500_000.0, as_of=AS_OF)
    assert w.need == 300_000.0 and w.from_cash == 200_000.0
    assert [(s.acquired, s.proceeds) for s in w.sales] == [
        ("2025-11-20", 280_000.0),
        ("2022-03-01", 20_000.0),
    ]
    assert w.gain_st == -10_000.0 and w.gain_lt == round(20_000 * 60 / 560, 2)
    assert w.short == 0.0 and w.magi_after < w.magi_before
    # a gain budget caps the sale; a specific-ID lot goes first
    w = withdraw.pick(
        lots,
        2026,
        target=1_000_000.0,
        budget=5_000.0,
        as_of=AS_OF,
        specific=["11111111:VTSAX:2019-01-15"],
    )
    assert w.sales[0].acquired == "2019-01-15" and w.sales[0].gain == 5_000.0
    assert w.short > 0 and any("gain budget" in n for n in w.notes)


def test_wash_sales_across_accounts_and_open_windows(lots: Layout) -> None:
    conn = db.connect(lots.data / "ledger" / "planner.db")
    try:
        losses = washsale.loss_sales(conn, 2026)
        assert [(ls.date, ls.loss) for ls in losses] == [("2026-06-02", 3_000.0)]
        flags = washsale.check(conn, 2026)
        assert len(flags) == 1
        assert (flags[0].buy.account, flags[0].buy.date, flags[0].days) == (
            "33333333",
            "2026-06-20",
            18,
        )
        assert washsale.open_windows(conn, date(2026, 6, 30)) == [
            ("VTSAX", "2026-07-03", 3_000.0)
        ]
        assert washsale.open_windows(conn, date(2026, 7, 3)) == []
    finally:
        conn.close()
    # a loss lot sold inside the window is flagged by the withdraw planner
    w = withdraw.pick(lots, 2026, target=250_000.0, as_of=date(2026, 7, 1))
    assert any("wash sale" in n for n in w.notes)


def test_cli_withdraw_and_washsales(lots: Layout) -> None:
    r = runner.invoke(
        app,
        ["withdraw", "--year", "2026", "--target", "500000", "--as-of", "2026-07-10"],
    )
    assert r.exit_code == 0, r.output
    assert "sell" in r.output and "2025-11-20" in r.output and "ACA MAGI" in r.output
    r = runner.invoke(app, ["washsales", "--year", "2026", "--as-of", "2026-06-30"])
    assert r.exit_code == 0, r.output
    assert "WASH VTSAX" in r.output and "open window VTSAX" in r.output
