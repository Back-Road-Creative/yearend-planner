"""CSV intake: Vanguard download blocks, cost basis, realized, income, bank."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from planner.cli import app
from planner.ingest import CSV_TEMPLATES_DIR, ingest
from planner.ingest.csvfile import Unmatched, load_csv_templates, parse_csv, read_blocks
from planner.ledger import db
from planner.paths import Layout

runner = CliRunner()

FUND = "VANGUARD TOTAL STOCK MARKET INDEX ADMIRAL"
TXN_HEADER = (
    "Account Number,Trade Date,Settlement Date,Transaction Type,"
    "Transaction Description,Investment Name,Symbol,Shares,Share Price,"
    "Principal Amount,Commission Fees,Net Amount,Accrued Interest,Account Type,"
)
SELL = f"12345678,06/02/2025,06/03/2025,Sell,Sell,{FUND},VTSAX,-100.0,125.00,12500.00,0.0,12500.00,0.0,BROKERAGE,"  # noqa: E501
VANGUARD_DOWNLOAD = "\n".join(
    [
        "Account Number,Investment Name,Symbol,Shares,Share Price,Total Value,",
        f'12345678,{FUND},VTSAX,1000.123,120.50,"120,514.82",',
        "12345678,VANGUARD FEDERAL MONEY MARKET FUND,VMFXX,5000.00,1.00,5000.00,",
        "",
        TXN_HEADER,
        f"12345678,03/14/2025,03/14/2025,Dividend,Dividend Received,{FUND},VTSAX,"
        "0.0,0.0,412.33,0.0,412.33,0.0,BROKERAGE,",
        f"12345678,03/14/2025,03/14/2025,Reinvestment,Dividend Reinvestment,{FUND},"
        "VTSAX,3.421,120.53,-412.33,0.0,-412.33,0.0,BROKERAGE,",
        SELL,
        SELL,
        "",
    ]
)

COST_BASIS = "\n".join(
    [
        "Account Number,Investment Name,Symbol,Date Acquired,Shares,Total Cost,"
        "Market Value,Term",
        f'12345678,{FUND},VTSAX,01/15/2019,500.000,"40,000.00","60,250.00",Long-term',
        f'12345678,{FUND},VTSAX,11/20/2024,500.123,"58,000.00","60,264.82",Short-term',
        "",
    ]
)

REALIZED = "\n".join(
    [
        "Account Number,Investment Name,Symbol,Date Acquired,Date Sold,Shares,"
        "Proceeds,Cost Basis,Term",
        f'12345678,{FUND},VTSAX,01/15/2019,06/02/2025,200.000,"25,000.00",'
        '"16,000.00",Long-term',
        "",
    ]
)

BANK = """\
Transaction ID,Date,Description,Amount,Account
T-1001,01/05/2025,CLIENT PAYMENT ACME,"2,500.00",Checking
T-1002,01/06/2025,CARD PURCHASE,(45.10),Checking
"""

BANK_DC = """\
Date,Description,Debit,Credit
2025-01-07,ZELLE FROM CLIENT,,900.00
2025-01-08,ELECTRIC BILL,120.00,
"""


@pytest.fixture
def lay(planner_home: Path) -> Layout:
    lay = Layout(planner_home)
    lay.ensure()
    return lay


def drop(lay: Layout, name: str, text: str) -> Path:
    p = lay.data / "inbox" / name
    p.write_text(text, encoding="utf-8")
    return p


def test_vanguard_download_has_two_blocks(tmp_path: Path) -> None:
    p = tmp_path / "ofxdownload.csv"
    p.write_text(VANGUARD_DOWNLOAD)
    blocks = read_blocks(p)
    assert [len(b.rows) for b in blocks] == [2, 4]
    assert blocks[0].headers[-1] == "Total Value"  # trailing comma dropped
    rows = parse_csv(p, load_csv_templates(CSV_TEMPLATES_DIR))
    holdings = [r for r in rows if r.kind == "holding"]
    txns = [r for r in rows if r.kind == "transaction"]
    assert [h.amount_cents for h in holdings] == [12051482, 500000]
    assert holdings[0].quantity == 1000.123 and holdings[0].date is not None
    assert [t.type for t in txns] == ["Dividend", "Reinvestment", "Sell", "Sell"]
    assert txns[0].amount_cents == 41233 and txns[1].amount_cents == -41233
    assert txns[0].tax_year == 2025 and txns[0].date == "2025-03-14"
    # two identical real sells on one day stay two rows
    assert txns[2].row_key != txns[3].row_key


def test_cost_basis_realized_and_income(tmp_path: Path) -> None:
    tpls = load_csv_templates(CSV_TEMPLATES_DIR)
    p = tmp_path / "basis.csv"
    p.write_text(COST_BASIS)
    lots = parse_csv(p, tpls)
    assert [(lot.term, lot.basis_cents, lot.acquired) for lot in lots] == [
        ("long", 4000000, "2019-01-15"),
        ("short", 5800000, "2024-11-20"),
    ]
    assert lots[0].date is None and lots[0].tax_year is None
    p = tmp_path / "realized.csv"
    p.write_text(REALIZED)
    (sale,) = parse_csv(p, tpls)
    assert (sale.kind, sale.amount_cents, sale.basis_cents, sale.tax_year) == (
        "realized",
        2500000,
        1600000,
        2025,
    )


def test_bank_signed_and_debit_credit(tmp_path: Path) -> None:
    tpls = load_csv_templates(CSV_TEMPLATES_DIR)
    p = tmp_path / "bank.csv"
    p.write_text(BANK)
    rows = parse_csv(p, tpls)
    assert [r.amount_cents for r in rows] == [250000, -4510]
    assert rows[0].row_key == "bank:T-1001"
    p = tmp_path / "bank-dc.csv"
    p.write_text(BANK_DC)
    with pytest.raises(Unmatched, match="does not match"):
        parse_csv(p, tpls)  # ISO dates need a template with date_format


def test_unknown_headers_are_unmatched_with_the_headers(lay: Layout) -> None:
    drop(lay, "mystery.csv", "Foo,Bar\n1,2\n")
    rep = ingest(lay)
    assert rep.unmatched == [
        ("mystery.csv", "no CSV template matches headers: Foo, Bar")
    ]


def test_ingest_rows_idempotent_and_overlapping_ids(lay: Layout) -> None:
    drop(lay, "ofxdownload.csv", VANGUARD_DOWNLOAD)
    drop(lay, "bank.csv", BANK)
    rep = ingest(lay)
    assert [i.forms for i in rep.imported] == [
        ("bank 2 rows",),
        ("vanguard_holdings 2 rows", "vanguard_transactions 4 rows"),
    ]
    assert (lay.data / "archive" / "2025" / "ofxdownload.csv").exists()
    conn = db.connect(lay.data / "ledger" / "planner.db")
    assert len(db.rows_for(conn, 2025)) == 6
    assert len(db.rows_for(conn, source="vanguard_holdings")) == 2
    # same file again: duplicate, archived, nothing added
    drop(lay, "ofxdownload.csv", VANGUARD_DOWNLOAD)
    rep2 = ingest(lay)
    assert rep2.duplicates == ["ofxdownload.csv"]
    assert len(db.rows_for(conn, 2025)) == 6
    # a later bank export repeating T-1002 with a new line is not double counted
    drop(lay, "bank2.csv", BANK.replace("T-1001", "T-1003"))
    ingest(lay)
    assert [r.row_key for r in db.rows_for(conn, source="bank")] == [
        "bank:T-1001",
        "bank:T-1003",
        "bank:T-1002",
    ]


def test_cli_rows(lay: Layout) -> None:
    drop(lay, "realized.csv", REALIZED)
    assert runner.invoke(app, ["ingest"]).exit_code == 0
    r = runner.invoke(app, ["rows", "--year", "2025", "--kind", "realized"])
    assert r.exit_code == 0, r.output
    assert (
        "25,000.00" in r.output
        and "realized.csv:2" in r.output
        and "1 rows" in r.output
    )
