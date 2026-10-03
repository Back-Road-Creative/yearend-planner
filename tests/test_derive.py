"""YTD facts derived from ledger rows (Phase 2c ``planner derive``)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from planner.cli import app
from planner.ingest import ingest
from planner.ingest.derive import derive, derive_year
from planner.ledger import db
from planner.paths import Layout
from tests.test_csv import BANK, FUND, REALIZED, VANGUARD_DOWNLOAD, drop

runner = CliRunner()

INCOME = "\n".join(
    [
        "Account Number,Investment Name,Symbol,Income Type,Amount,Payment Date",
        f"12345678,{FUND},VTSAX,Dividend,412.33,03/14/2025",
        f"12345678,{FUND},VTSAX,Dividend,420.10,06/13/2025",
        "12345678,VANGUARD FEDERAL MONEY MARKET FUND,VMFXX,Interest,15.25,06/30/2025",
        f"12345678,{FUND},VTSAX,Capital Gain,150.00,12/20/2025",
        "",
    ]
)


@pytest.fixture
def lay(planner_home: Path) -> Layout:
    lay = Layout(planner_home)
    lay.ensure()
    return lay


def facts(conn: sqlite3.Connection, year: int = 2025) -> dict[tuple[str, str], float]:
    return {(f.issuer, f.box): f.value for f in db.facts_for(conn, year, "YTD")}


def test_ingest_derives_ytd_facts_from_rows(lay: Layout) -> None:
    drop(lay, "realized.csv", REALIZED)
    drop(lay, "ofxdownload.csv", VANGUARD_DOWNLOAD)
    drop(lay, "bank.csv", BANK)
    rep = ingest(lay)
    assert rep.derived == {2025: 6}  # the 2026 holdings snapshot yields none
    conn = db.connect(lay.data / "ledger" / "planner.db")
    got = facts(conn)
    assert got[("vanguard_realized", "lt_proceeds")] == 25000.0
    assert got[("vanguard_realized", "lt_basis")] == 16000.0
    assert got[("vanguard_realized", "lt_gain")] == 9000.0
    # the download's Dividend row counts; its Reinvestment row does not
    assert got[("vanguard_transactions", "dividends")] == 412.33
    assert got[("bank", "deposits")] == 2500.0 and got[("bank", "withdrawals")] == 45.10
    assert ("vanguard_realized", "st_proceeds") not in got


def test_income_export_wins_over_transactions_and_rerun_supersedes(lay: Layout) -> None:
    drop(lay, "ofxdownload.csv", VANGUARD_DOWNLOAD)
    ingest(lay)
    conn = db.connect(lay.data / "ledger" / "planner.db")
    assert facts(conn)[("vanguard_transactions", "dividends")] == 412.33
    drop(lay, "income.csv", INCOME)
    rep = ingest(lay)
    assert rep.imported[0].forms == ("vanguard_income 4 rows",)
    got = facts(conn)
    assert got[("vanguard_income", "dividends")] == 832.43
    assert got[("vanguard_income", "interest")] == 15.25
    assert got[("vanguard_income", "capital_gain_distributions")] == 150.0
    # the earlier transaction-based figure was superseded, not kept beside it
    assert ("vanguard_transactions", "dividends") not in got
    superseded = db.facts_for(conn, 2025, "YTD", status="superseded")
    assert [f.box for f in superseded] == ["dividends"]
    assert derive(conn, 2024) == 0


def test_derive_year_is_pure_and_bank_signs(lay: Layout) -> None:
    drop(lay, "bank.csv", BANK)
    ingest(lay)
    conn = db.connect(lay.data / "ledger" / "planner.db")
    rows = db.rows_for(conn, 2025)
    out = derive_year(rows, 2025)
    assert [(f.issuer, f.box, f.value) for f in out] == [
        ("bank", "deposits", 2500.0),
        ("bank", "withdrawals", 45.10),
    ]
    assert derive_year(rows, 2024) == []


def test_cli_derive_and_facts(lay: Layout) -> None:
    drop(lay, "realized.csv", REALIZED)
    r = runner.invoke(app, ["ingest"])
    assert r.exit_code == 0 and "derived   2025: 3 YTD facts from rows" in r.output
    r = runner.invoke(app, ["derive", "--year", "2025"])
    assert (
        r.exit_code == 0 and r.output.strip() == "derived 2025: 3 YTD facts from rows"
    )
    r = runner.invoke(app, ["facts", "--year", "2025", "--form", "YTD"])
    assert r.exit_code == 0 and "9,000.00" in r.output and "3 facts" in r.output
