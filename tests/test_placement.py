"""Phase 10, unit 4c: tax-aware placement toward the household's own target mix,
on the synthetic household of test_spending (taxable VTSAX 1.4M, a 200k cash
account, a 200k traditional IRA, cash_target 20k) and test_withdraw's lots."""

from __future__ import annotations

from datetime import date

import pytest
from typer.testing import CliRunner

from planner import goals
from planner.cli import app
from planner.paths import Layout
from planner.plan import placement, year
from tests.test_spending import AS_OF, lay  # noqa: F401
from tests.test_withdraw import lots  # noqa: F401

runner = CliRunner()


def test_no_target_mix_proposes_nothing(lay: Layout) -> None:  # noqa: F811
    pl = placement.plan(lay, 2026, AS_OF)
    assert pl.target is None and not pl.moves
    assert pl.lines()[0].startswith("no target mix chosen")


def test_an_untyped_holding_is_named_and_left_out(lay: Layout) -> None:  # noqa: F811
    goals.save_mix(lay, {"stocks": 60.0, "bonds": 30.0, "cash": 10.0})
    pl = placement.plan(lay, 2026, AS_OF)
    # only the cash account is classed (a cash account is cash); 20k reserve held
    assert {h.symbol for h in pl.untyped} == {"VTSAX", "account:33333333"}
    assert pl.held == 20_000 and pl.base == 180_000
    assert any("class not typed: VTSAX in 11111111 1,400,000.00" in n for n in pl.notes)
    # spare cash buys the under-target classes, the largest gap first; no tax
    assert [(m.step, m.buy_class, m.amount) for m in pl.moves] == [
        ("cash", "stocks", 108_000.0),
        ("cash", "bonds", 54_000.0),
    ]
    assert pl.lines()[-1].endswith(": no tax")


def test_inside_the_ira_first_then_spare_cash(lay: Layout) -> None:  # noqa: F811
    goals.save_class(lay, "VTSAX", "stocks")
    goals.save_class(lay, "account:33333333", "stocks")
    goals.save_mix(lay, {"stocks": 80.0, "bonds": 10.0, "cash": 10.0})
    pl = placement.plan(lay, 2026, AS_OF)
    # stocks 1.6M of a 1.78M base: 176k over; bonds 178k under; cash 2k over
    assert [(m.step, m.account, m.sell, m.buy_class, m.amount) for m in pl.moves] == [
        ("sheltered", "33333333", "account:33333333", "bonds", 176_000.0),
        ("cash", "22222222", "cash", "bonds", 2_000.0),
    ]
    assert pl.tax_loss is None and pl.tax_gain is None
    text = "\n".join(pl.lines())
    assert "stocks: 1,600,000.00 (89.9%), target 1,424,000.00 (80%), over" in text


def test_a_taxable_holding_without_basis_is_not_proposed(
    lay: Layout,  # noqa: F811
) -> None:
    goals.save_class(lay, "VTSAX", "stocks")
    goals.save_class(lay, "account:33333333", "bonds")
    goals.save_mix(lay, {"stocks": 50.0, "bonds": 40.0, "cash": 10.0})
    pl = placement.plan(lay, 2026, AS_OF)
    assert all(m.step in ("sheltered", "cash") for m in pl.moves)
    assert any("VTSAX in 11111111: no cost basis on file" in n for n in pl.notes)


@pytest.mark.engine
def test_a_loss_lot_bought_inside_30_days_is_skipped_for_a_gain_lot(
    lots: Layout,  # noqa: F811
) -> None:
    goals.save_class(lots, "VTSAX", "stocks")
    goals.save_class(lots, "account:33333333", "bonds")
    goals.save_mix(lots, {"stocks": 60.0, "bonds": 30.0, "cash": 10.0})
    # the IRA reinvested VTSAX on 2026-06-20: a loss sale on 07-10 would wash
    pl = placement.plan(lots, 2026, AS_OF)
    assert any("2025-11-20 loss lot skipped" in n for n in pl.notes)
    assert [(m.step, m.sell, m.buy_class, m.amount) for m in pl.moves] == [
        ("cash", "cash", "bonds", 2_000.0),
        ("gain", "VTSAX 2022-03-01", "bonds", 332_000.0),
    ]
    assert pl.moves[1].gain == round(332_000 * 60_000 / 560_000, 2)
    assert pl.tax_loss is None and pl.tax_gain is not None and pl.tax_gain > 0


@pytest.mark.engine
def test_loss_lots_first_once_the_window_closes(lots: Layout) -> None:  # noqa: F811
    goals.save_class(lots, "VTSAX", "stocks")
    goals.save_class(lots, "account:33333333", "bonds")
    goals.save_mix(lots, {"stocks": 60.0, "bonds": 30.0, "cash": 10.0})
    pl = placement.plan(lots, 2026, date(2026, 8, 15))
    sales = [(m.step, m.sell, m.amount) for m in pl.moves if m.step != "cash"]
    assert sales == [
        ("loss", "VTSAX 2025-11-20", 280_000.0),
        ("gain", "VTSAX 2022-03-01", 52_000.0),
    ]
    assert pl.moves[1].gain == -10_000.0 and pl.moves[1].term == "short"
    assert pl.tax_loss is not None and pl.tax_loss < 0
    assert any(
        "no buys of VTSAX in any account, IRAs and dividend reinvestment included, "
        "until 2026-09-15 (it is held in 11111111)" in n
        for n in pl.notes
    )
    section = year.assemble(lots, 2026, date(2026, 8, 15)).section("placement")
    assert section is not None and section.ok
    assert any(ln.startswith("tax effect of the loss sales: -") for ln in section.lines)


def test_classes_are_checked_and_typed_by_command(lay: Layout) -> None:  # noqa: F811
    with pytest.raises(goals.GoalsError, match="must be one of"):
        goals.parse({"classes": {"VTSAX": "crypto"}})
    res = runner.invoke(app, ["classify", "VTSAX", "stocks"], env={})
    assert res.exit_code == 0, res.output
    assert goals.load(lay).classes == {"VTSAX": "stocks"}
    res = runner.invoke(app, ["classify"])
    assert "VTSAX" in res.output and "stocks" in res.output
    res = runner.invoke(app, ["classify", "VTSAX", "gold"])
    assert res.exit_code == 2 and goals.load(lay).classes == {"VTSAX": "stocks"}
    res = runner.invoke(app, ["classify", "VTSAX", "--remove"])
    assert res.exit_code == 0 and goals.load(lay).classes == {}
