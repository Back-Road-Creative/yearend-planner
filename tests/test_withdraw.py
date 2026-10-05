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
from planner.plan.inputs import Overrides
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


@pytest.mark.engine
def test_cash_projection_keeps_the_typed_total_income(lots: Layout) -> None:  # noqa: F811
    # cash covers the target, so nothing is sold: the before and after
    # households are the same one, typed total income included
    ov = Overrides(total_income=150_000.0)
    w = withdraw.pick(lots, 2026, as_of=AS_OF, overrides=ov)
    assert (
        w.sales == []
        and w.magi_before != withdraw.pick(lots, 2026, as_of=AS_OF).magi_before
    )
    assert w.magi_after == w.magi_before and w.tax_after == w.tax_before
    # a sale still lands on top of the typed total
    w = withdraw.pick(lots, 2026, target=500_000.0, as_of=AS_OF, overrides=ov)
    assert w.sales and w.magi_after != w.magi_before


def test_proceeds_for_a_planned_gain_use_the_most_gain_per_dollar_first(
    lots: Layout,  # noqa: F811
) -> None:
    sellable = withdraw.sellable(portfolio.status(lots, 2026, AS_OF))
    assert {lot.account for lot in sellable} == {"11111111"}
    # long term: the 2019 lot carries 260,000 of gain in 560,000 of value
    assert withdraw.proceeds_for_gain(sellable, 20_000.0, "long") == (
        round(20_000 * 560 / 260, 2),
        0.0,
    )
    # past that lot's gain the next one is drawn on: 2022 carries 60,000 of gain
    got, left = withdraw.proceeds_for_gain(sellable, 290_000.0, "long")
    assert got == round(560_000 + 30_000 * 560 / 60, 2) and left == 0.0
    # more gain than the lots hold comes back as unmatched
    got, left = withdraw.proceeds_for_gain(sellable, 400_000.0, "long")
    assert got == 1_120_000.0 and left == 80_000.0
    # short term: only a loss lot, so a gain finds nothing and a loss finds it
    assert withdraw.proceeds_for_gain(sellable, 5_000.0, "short") == (0.0, 5_000.0)
    assert withdraw.proceeds_for_gain(sellable, -5_000.0, "short") == (
        round(5_000 * 280 / 10, 2),
        0.0,
    )
    assert withdraw.proceeds_for_gain(sellable, 0.0, "long") == (0.0, 0.0)


def _lot(symbol: str, basis: float, value: float, term: str = "short") -> portfolio.Lot:
    return portfolio.Lot("acct", symbol, "2026-01-02", term, 1.0, basis, value)


def test_cash_on_hand_replaces_sale_proceeds_not_gain() -> None:
    """A planned 2,000 gain from a 10,000 sale of an 8,000-basis lot: 2,000 of
    cash replaces 2,000 of the proceeds, so only a fifth of the gain is avoided."""
    one = [_lot("SYNA", 8_000.0, 10_000.0, "long")]
    assert withdraw.gain_avoided(one, 2_000.0, "long", 2_000.0) == (400.0, 2_000.0, 0.0)
    # enough cash for the whole sale avoids the whole gain and spends the proceeds
    assert withdraw.gain_avoided(one, 2_000.0, "long", 50_000.0) == (
        2_000.0,
        10_000.0,
        0.0,
    )
    # the lots not sold are those the sale would draw first (most gain per dollar)
    two = [
        _lot("SYNB", 9_000.0, 10_000.0, "long"),
        _lot("SYNA", 5_000.0, 10_000.0, "long"),
    ]
    assert withdraw.gain_avoided(two, 6_000.0, "long", 12_000.0) == (
        5_200.0,
        12_000.0,
        0.0,
    )
    # a gain no lot supplies has no known proceeds: nothing avoided, named back
    assert withdraw.gain_avoided([], 2_000.0, "long", 5_000.0) == (0.0, 0.0, 2_000.0)
    assert withdraw.gain_avoided(one, 2_000.0, "long", 0.0) == (0.0, 0.0, 0.0)


def test_proceeds_for_a_planned_loss_use_the_most_loss_per_dollar_first() -> None:
    # A loses 1.0 per dollar of value, B 0.1: drawing a 1,000 loss from A takes
    # 1,000 of value, from B 10,000. The plan may rely only on the smaller cash.
    lots = [_lot("B", 11_000.0, 10_000.0), _lot("A", 20_000.0, 10_000.0)]
    assert withdraw.proceeds_for_gain(lots, -1_000.0, "short") == (1_000.0, 0.0)
    assert withdraw.proceeds_for_gain(lots[::-1], -1_000.0, "short") == (1_000.0, 0.0)
    # past A's 10,000 of loss, B is drawn on at its own rate
    assert withdraw.proceeds_for_gain(lots, -10_500.0, "short") == (
        10_000.0 + 5_000.0,
        0.0,
    )
    # the same order holds for a gain: most gain per dollar first
    gains = [_lot("B", 9_000.0, 10_000.0, "long"), _lot("A", 0.0, 10_000.0, "long")]
    assert withdraw.proceeds_for_gain(gains, 1_000.0, "long") == (1_000.0, 0.0)
