"""Phase 3: accounts typed through the Needed panel, balances and lots from the
newest exports, accessible vs locked money, conversions and the persisted peak."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from planner.cli import app
from planner.ingest import ingest
from planner.ingest.needs import enter, need_for, needed
from planner.ledger import db, portfolio
from planner.paths import Layout
from tests.test_csv import BANK, COST_BASIS, FUND, VANGUARD_DOWNLOAD, drop

runner = CliRunner()
HEADER = "Account Number,Investment Name,Symbol,Shares,Share Price,Total Value,"
RETIREMENT = "\n".join(
    [
        HEADER,
        f'22222222,{FUND},VTSAX,100.000,120.50,"12,050.00",',
        f'33333333,{FUND},VTSAX,500.000,120.50,"60,250.00",',
        f'44444444,{FUND},VTSAX,200.000,120.50,"24,100.00",',
        "",
    ]
)


@pytest.fixture
def lay(planner_home: Path) -> Layout:
    lay = Layout(planner_home)
    lay.ensure()
    return lay


def loaded(lay: Layout) -> Layout:
    drop(lay, "vanguard.csv", VANGUARD_DOWNLOAD)
    drop(lay, "basis.csv", COST_BASIS)
    drop(lay, "bank.csv", BANK)
    drop(lay, "retirement.csv", RETIREMENT)
    ingest(lay)
    return lay


def missing(lay: Layout) -> set[str]:
    return {s.need.key for s in needed(lay, 2026).items if s.state == "missing"}


def test_every_exported_account_is_asked_for_its_type_once(lay: Layout) -> None:
    loaded(lay)
    asked = {k for k in missing(lay) if k.startswith("account:")}
    assert asked == {
        "account:12345678",
        "account:22222222",
        "account:33333333",
        "account:44444444",
        "account:Checking",
    }
    enter(lay, 2026, "account:12345678", "taxable")
    enter(lay, 2026, "account:Checking", "cash")
    enter(lay, 2026, "account:22222222", "roth")
    enter(lay, 2026, "account:33333333", "trad_ira")
    enter(lay, 2026, "account:44444444", "inherited_ira")
    with pytest.raises(ValueError):
        enter(lay, 2026, "account:12345678", "brokerage")
    with pytest.raises(KeyError):
        need_for("account:")
    left = {k for k in missing(lay) if k.startswith("account:")}
    assert left == {"account:44444444:death"}
    enter(lay, 2026, "account:44444444:death", "2024-03-01")
    assert not {k for k in missing(lay) if k.startswith("account:")}
    assert portfolio.load_accounts(lay)["44444444"] == {
        "type": "inherited_ira",
        "date_of_death": "2024-03-01",
    }


def test_status_splits_accessible_and_locked_and_keeps_the_peak(lay: Layout) -> None:
    loaded(lay)
    for number, kind in (
        ("12345678", "taxable"),
        ("22222222", "roth"),
        ("33333333", "trad_ira"),
        ("44444444", "inherited_ira"),
    ):
        portfolio.save_account(lay, number, type=kind)
    portfolio.save_account(lay, "44444444", date_of_death="2024-03-01")
    portfolio.save_account(lay, "Checking", type="cash", balance=10000.0)
    st = portfolio.status(lay, 2026, date(2026, 10, 2))
    assert [p.account for p in st.positions] == [
        "12345678",
        "22222222",
        "33333333",
        "44444444",
        "Checking",
    ]
    assert st.total == pytest.approx(125514.82 + 12050 + 60250 + 24100 + 10000)
    # Roth contributions unknown: the whole Roth is locked and the status says so
    assert st.accessible == pytest.approx(125514.82 + 10000)
    assert st.locked == pytest.approx(12050 + 60250 + 24100)
    assert st.untyped == 0
    assert st.inherited == [("44444444", "2024-03-01", "2034-12-31")]
    assert any("Roth contributions unknown" in n for n in st.notes)
    assert [(lot.term, lot.gain) for lot in st.lots] == [
        ("long", 20250.0),
        ("short", 2264.82),
    ]
    assert st.unrealized == (2264.82, 20250.0)
    assert st.carryforward is None
    assert st.peak == pytest.approx(st.total) and st.peak_date == "2026-10-02"
    # typed Roth contributions become accessible, capped at the Roth balance
    enter(lay, 2026, "roth_basis_contributions", "15,000")
    enter(lay, 2026, "prior_capital_loss_carryforward", "1,200")
    st = portfolio.status(lay, 2026, date(2026, 10, 3))
    assert st.accessible == pytest.approx(125514.82 + 10000 + 12050)
    assert st.carryforward == 1200
    assert "dividends" in st.ytd_income or st.ytd_income == {}
    # a newer, smaller holdings export replaces the balance; the peak stays
    drop(
        lay, "vanguard2.csv", VANGUARD_DOWNLOAD.replace('"120,514.82"', '"100,000.00"')
    )
    ingest(lay)
    st2 = portfolio.status(lay, 2026, date(2026, 10, 4))
    assert st2.total == pytest.approx(st.total - 20514.82)
    assert st2.peak == pytest.approx(st.total) and st2.peak_date == "2026-10-02"


def test_accessible_date_is_the_earlier_of_the_clock_and_the_access_age() -> None:
    assert portfolio.accessible_date(date(2026, 6, 1), None, 59.5) == date(2031, 1, 1)
    assert portfolio.accessible_date(date(2026, 6, 1), "1970-01-31", 59.5) == date(
        2029, 7, 31
    )
    assert portfolio.accessible_date(date(2026, 6, 1), "1990-01-01", 59.5) == date(
        2031, 1, 1
    )
    assert portfolio.add_months(date(2024, 8, 31), 6) == date(2025, 2, 28)


def test_cli_account_convert_and_status(lay: Layout) -> None:
    loaded(lay)
    env = {"PLANNER_HOME": str(lay.root)}
    r = runner.invoke(
        app,
        ["account", "33333333", "--type", "trad_ira", "--name", "Trad IRA"],
        env=env,
    )
    assert r.exit_code == 0 and "type=trad_ira" in r.output
    r = runner.invoke(app, ["account", "44444444", "--type", "inherited_ira"], env=env)
    assert r.exit_code == 0
    r = runner.invoke(
        app, ["account", "Checking", "--type", "cash", "--balance", "$9,000"], env=env
    )
    assert r.exit_code == 0 and "balance=9000" in r.output
    r = runner.invoke(app, ["account", "12345678", "--type", "savings"], env=env)
    assert r.exit_code == 2 and "refused" in r.output
    r = runner.invoke(app, ["account"], env=env)
    assert "33333333" in r.output and "Trad IRA" in r.output
    r = runner.invoke(
        app, ["convert", "2026-06-01", "25000", "--from", "44444444"], env=env
    )
    assert r.exit_code == 2 and "inherited IRA" in r.output
    r = runner.invoke(
        app, ["convert", "2026-06-01", "25000", "--from", "12345678"], env=env
    )
    assert r.exit_code == 2 and "untyped" in r.output
    r = runner.invoke(
        app, ["convert", "2026-06-01", "25,000", "--from", "33333333"], env=env
    )
    assert r.exit_code == 0 and "penalty-free 2031-01-01" in r.output
    conn = db.connect(lay.data / "ledger" / "planner.db")
    assert [(c.amount, c.taxable, c.accessible_date) for c in db.conversions(conn)] == [
        (25000.0, 25000.0, "2031-01-01")
    ]
    conn.close()
    r = runner.invoke(
        app, ["status", "--year", "2026", "--as-of", "2031-01-01"], env=env
    )
    assert r.exit_code == 0, r.output
    assert "seasoned conversions 25,000.00" in r.output
    assert "conversion   2026-06-01    25,000.00 from 33333333" in r.output
    assert "untyped" in r.output and "12345678" in r.output
    assert "unrealized   2 lots: short 2,264.82, long 20,250.00" in r.output
    assert "capital loss carryforward: unknown" in r.output
    assert "date of death not entered" in r.output
