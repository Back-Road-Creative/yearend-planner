"""Phase 10, unit 4d: the "Before Dec 31" list on the synthetic household of
test_spending and test_withdraw's lots (no real figures)."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from planner import goals
from planner.cli import app
from planner.ledger import db
from planner.paths import Layout
from planner.plan import year, yearend
from tests.test_spending import lay  # noqa: F401
from tests.test_withdraw import lots  # noqa: F401

runner = CliRunner()
DAY = date(2026, 8, 15)
LOSS = "sell:11111111:VTSAX:2025-11-20"
GAIN = "sell:11111111:VTSAX:2022-03-01"


def test_trade_by_and_settle_follow_the_nyse_year_end() -> None:
    # 2026: Thursday trade, settles past New Year's Day and the weekend
    assert yearend.last_trading_day(2026) == date(2026, 12, 31)
    assert yearend.settle_day(date(2026, 12, 31)) == date(2027, 1, 4)
    # 2022: December 31 a Saturday; New Year's Day observed Monday January 2
    assert yearend.last_trading_day(2022) == date(2022, 12, 30)
    assert yearend.settle_day(date(2022, 12, 30)) == date(2023, 1, 3)
    # 2027: a Saturday New Year's Day is not observed on Friday December 31
    assert yearend.last_trading_day(2027) == date(2027, 12, 31)
    assert yearend.settle_day(date(2027, 12, 31)) == date(2028, 1, 3)
    # Christmas on a Saturday is observed Friday the 24th
    assert yearend.settle_day(date(2021, 12, 23)) == date(2021, 12, 27)


def test_nothing_proposed_without_a_target_mix(lay: Layout) -> None:  # noqa: F811
    ye = yearend.build(lay, 2026, DAY)
    assert ye.lines() == ["no year-end move proposed"]


def _mix(lots: Layout) -> None:  # noqa: F811
    goals.save_class(lots, "VTSAX", "stocks")
    goals.save_class(lots, "account:33333333", "bonds")
    goals.save_mix(lots, {"stocks": 60.0, "bonds": 30.0, "cash": 10.0})


@pytest.mark.engine
def test_each_sale_with_dates_tax_cash_and_do_nothing(lots: Layout) -> None:  # noqa: F811
    _mix(lots)
    ye = yearend.build(lots, 2026, DAY)
    sales = [i for i in ye.items if i.kind == "sale"]
    assert [i.id for i in sales] == [GAIN, LOSS] or [i.id for i in sales] == [
        LOSS,
        GAIN,
    ]
    loss = next(i for i in sales if i.id == LOSS)
    assert (loss.trade_by, loss.settles, loss.amount) == (
        "2026-12-31",
        "2027-01-04",
        280_000.0,
    )
    assert loss.lot == "VTSAX 2025-11-20" and loss.gain == -10_000.0
    assert loss.tax_effect is not None and loss.tax_effect < 0
    assert loss.status == "proposed"
    text = "\n".join(ye.lines())
    assert (
        "[proposed] " + LOSS in text
        and "trade by 2026-12-31, settles 2027-01-04" in text
    )
    assert "do nothing: the 10,000.00 loss stays unrealized" in text
    # cash after: the 200k cash account plus each sale, less its tax effect
    first, second = sales
    assert first.cash_after == round(
        200_000 + first.amount - (first.tax_effect or 0), 2
    )
    assert second.cash_after == round(
        first.cash_after + second.amount - (second.tax_effect or 0), 2
    )
    assert any("no buys of VTSAX in any account" in n for n in ye.notes)
    section = year.assemble(lots, 2026, DAY).section("yearend")
    assert section is not None and section.ok


@pytest.mark.engine
def test_status_runs_proposed_chosen_done_reconciled(
    lots: Layout,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mix(lots)
    with pytest.raises(yearend.YearEndError, match="must be chosen"):
        yearend.advance(lots, 2026, LOSS, "done", DAY)
    assert yearend.advance(lots, 2026, LOSS, "chosen", DAY).status == "chosen"
    with pytest.raises(yearend.YearEndError, match="already chosen"):
        yearend.advance(lots, 2026, LOSS, "chosen", DAY)
    assert yearend.advance(lots, 2026, LOSS, "done", DAY).status == "done"
    assert yearend.advance(lots, 2026, LOSS, "proposed", DAY).status == "chosen"
    yearend.advance(lots, 2026, LOSS, "done", DAY)
    ye = yearend.build(lots, 2026, DAY)
    assert next(i for i in ye.items if i.id == LOSS).status == "done"
    # the imported sale of that lot reconciles it
    sold = SimpleNamespace(
        account="11111111", symbol="VTSAX", acquired="2025-11-20", date="2026-12-01"
    )
    monkeypatch.setattr(yearend, "_realized", lambda *a: [sold])
    ye = yearend.build(lots, 2026, date(2026, 12, 2))
    loss = next(i for i in ye.items if i.id == LOSS)
    assert loss.status == "reconciled" and ye.items[-1].id == LOSS
    with pytest.raises(yearend.YearEndError, match="nothing to undo"):
        yearend.advance(lots, 2026, LOSS, "proposed", DAY)


def test_a_conversion_reconciles_once_recorded(lay: Layout) -> None:  # noqa: F811
    it = yearend.Item(
        "conversion",
        "conversion",
        "33333333",
        "",
        30_000.0,
        "2026-12-31",
        "2026-12-31",
        3_000.0,
        status="done",
        at="2026-11-01",
    )
    assert not yearend._reconciled(lay, 2026, date(2026, 12, 5), it)
    conn = db.connect(lay.data / "ledger" / "planner.db")
    db.add_conversion(
        conn,
        date="2026-12-01",
        amount_cents=3_000_000,
        taxable_cents=3_000_000,
        source_account="33333333",
        accessible_date="2031-01-01",
    )
    conn.close()
    assert yearend._reconciled(lay, 2026, date(2026, 12, 5), it)
    assert it.nothing().startswith("do nothing: no tax now; 30,000.00 stays")


def test_a_malformed_status_file_is_refused(lay: Layout) -> None:  # noqa: F811
    p = yearend.path(lay, 2026)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x: {status: maybe}\n", encoding="utf-8")
    res = runner.invoke(app, ["yearend", "--year", "2026", "--as-of", "2026-08-15"])
    assert res.exit_code == 2 and "malformed" in res.output
    p.unlink()
    res = runner.invoke(app, ["yearend", "--year", "2026", "--as-of", "2026-08-15"])
    assert res.exit_code == 0 and "no year-end move proposed" in res.output
    res = runner.invoke(
        app, ["yearend", "--year", "2026", "--done", "a", "--undo", "b"]
    )
    assert res.exit_code == 2 and "one of" in res.output
