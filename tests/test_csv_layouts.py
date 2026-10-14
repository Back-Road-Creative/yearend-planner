"""Unit 7h3b: the CSV templates against their issuers' real export layouts
(tests/fixtures/real/csv/layouts.yaml, synthetic figures). A real Vanguard
download parts each account's holdings with a blank line and carries the
employer plan's holdings and transactions as blocks of their own."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml

from planner.ingest import CSV_TEMPLATES_DIR
from planner.ingest.csvfile import load_csv_templates, parse_csv, read_blocks

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
