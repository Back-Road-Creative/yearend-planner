"""Unit 7h3b: the CSV templates against their issuers' real export layouts
(tests/fixtures/real/csv/layouts.yaml, synthetic figures). A real Vanguard
download parts each account's holdings with a blank line and carries the
employer plan's holdings and transactions as blocks of their own."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
import yaml

from planner.ingest import CSV_TEMPLATES_DIR
from planner.ingest.csvfile import load_csv_templates, parse_csv, read_blocks
from planner.ingest.derive import derive_year
from planner.ledger import db

LAYOUTS = Path(__file__).parent / "fixtures" / "real" / "csv"
MANIFEST: dict[str, Any] = yaml.safe_load(
    (LAYOUTS / "layouts.yaml").read_text(encoding="utf-8")
)
TEMPLATES = load_csv_templates(CSV_TEMPLATES_DIR)


@pytest.mark.parametrize(
    "entry", MANIFEST["layouts"], ids=[e["name"] for e in MANIFEST["layouts"]]
)
def test_a_real_layout_reads_with_its_templates(entry: dict[str, Any]) -> None:
    assert str(entry["source"]).startswith("https://")
    rows = parse_csv(LAYOUTS / entry["file"], TEMPLATES)
    assert dict(Counter(r.source for r in rows)) == entry["expect"]


def test_every_csv_template_has_a_real_layout_or_says_why_not() -> None:
    read = {s for e in MANIFEST["layouts"] for s in e["expect"]}
    for tpl in TEMPLATES:
        assert tpl.path is not None
        sourced = tpl.source in read
        unsourced = tpl.path.stem in MANIFEST["unsourced"]
        assert sourced != unsourced, tpl.path.name


def test_an_account_after_a_blank_line_stays_in_its_block() -> None:
    blocks = read_blocks(LAYOUTS / "vanguard-download.csv")
    assert [len(b.rows) for b in blocks] == [4, 3, 1, 1]
    rows = parse_csv(LAYOUTS / "vanguard-download.csv", TEMPLATES)
    holdings = [r for r in rows if r.source == "vanguard_holdings"]
    assert [(h.account, h.symbol, h.amount_cents) for h in holdings] == [
        ("12345678", "BND", 74320),
        ("12345678", "VMFXX", 100),
        ("23456789", "VTI", 198305),
        ("23456789", "VMFXX", 22),
    ]


def test_the_employer_plan_blocks_read_as_holdings_and_transactions() -> None:
    rows = parse_csv(LAYOUTS / "vanguard-download.csv", TEMPLATES)
    (plan,) = [r for r in rows if r.source == "vanguard_plan_holdings"]
    assert (plan.kind, plan.account, plan.quantity, plan.amount_cents) == (
        "holding",
        "000123",
        1.0,
        966,
    )
    assert plan.description.startswith("Vanguard Total Bond Market")
    (paid,) = [r for r in rows if r.source == "vanguard_plan_transactions"]
    assert (paid.kind, paid.date, paid.description, paid.amount_cents) == (
        "transaction",
        "2025-09-08",
        "Plan Contribution",
        5580,
    )
    assert paid.quantity == 23.644 and paid.price_cents == 236


def test_a_new_header_after_a_blank_line_still_starts_a_block(tmp_path: Path) -> None:
    p = tmp_path / "two.csv"
    p.write_text("Date,Description,Amount\n01/02/2025,Coffee,-3.00\n\nName,Note\nA,b\n")
    assert [b.headers for b in read_blocks(p)] == [
        ["Date", "Description", "Amount"],
        ["Name", "Note"],
    ]


def _rows(name: str) -> list[db.Row]:
    return parse_csv(LAYOUTS / name, TEMPLATES)


def test_a_title_line_names_the_account_and_a_total_is_not_a_row() -> None:
    rows = _rows("schwab-positions.csv")
    assert [(r.account, r.symbol, r.amount_cents) for r in rows] == [
        ("Individual ...321", "VTI", 103340),
        ("Individual ...321", "BND", 73400),
        ("Individual ...321", "Cash & Cash Investments", 2520),
        ("Roth ...432", "BND", 14680),
        ("Roth ...432", "Cash & Cash Investments", 600),
    ]
    (txn,) = {r.account for r in _rows("schwab-transactions.csv")}
    assert txn == "Individual ...321"


def test_a_dash_n_a_or_incomplete_cell_is_no_value_not_zero() -> None:
    schwab = _rows("schwab-positions.csv")
    assert (schwab[0].basis_cents, schwab[1].basis_cents) == (None, 70000)
    assert (schwab[2].quantity, schwab[2].price_cents) == (None, None)
    fidelity = {r.symbol: r for r in _rows("fidelity-positions.csv")}
    assert fidelity["SPAXX**"].basis_cents is None
    assert (fidelity["VTI"].basis_cents, fidelity["VTI"].price_cents) == (250000, 19798)


def test_notes_before_and_after_a_block_are_not_rows() -> None:
    history = _rows("fidelity-history.csv")
    assert [(r.date, r.amount_cents) for r in history] == [
        ("2025-01-27", -10000),
        ("2025-01-23", 125050),
    ]
    assert [b.headers[0] for b in read_blocks(LAYOUTS / "fidelity-positions.csv")] == [
        "Account Number"
    ]


def test_an_as_of_date_is_the_date_the_row_counts_from() -> None:
    (interest,) = [r for r in _rows("schwab-transactions.csv") if "Interest" in r.type]
    assert (interest.date, interest.tax_year) == ("2025-12-31", 2025)


def test_the_most_specific_template_claims_a_block() -> None:
    # Schwab's transaction header also holds bank-generic's Date/Description/Amount.
    assert {r.source for r in _rows("schwab-transactions.csv")} == {
        "schwab_transactions"
    }


def _ledger(rows: list[db.Row]) -> list[db.LedgerRow]:
    return [db.LedgerRow(**asdict(r)) for r in rows]


def test_each_issuers_dividends_and_interest_reach_the_ytd_facts() -> None:
    def ytd(name: str) -> dict[str, float]:
        return {f.box: f.value for f in derive_year(_ledger(_rows(name)), 2025)}

    # Schwab books a reinvested dividend's cash as "Reinvest Dividend".
    assert ytd("schwab-transactions.csv")["dividends"] == 2.38
    assert ytd("schwab-transactions.csv")["interest"] == 0.03
    assert ytd("fidelity-transactions.csv")["dividends"] == 71.30
    assert ytd("chase-checking.csv") == {"deposits": 2100.0, "withdrawals": 262.0}
    assert ytd("schwab-checking.csv") == {"deposits": 1.0, "withdrawals": 500.0}
