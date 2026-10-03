"""Phase 4i: the draft return on a synthetic 2025 household (single, NC, 54 at
year end, $40,000 self-employment income, $1,000 interest with $100 withheld,
a $1,500 federal estimated payment, a marketplace plan all year whose advance
credit runs ahead of the credit allowed)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from planner.cli import app
from planner.ingest.needs import enter
from planner.ledger import db
from planner.paths import Layout
from planner.plan import esttax
from planner.taxprep import draft

runner = CliRunner()
YEAR = 2025
PREMIUM, SLCSP, APTC = 450.0, 520.0, 540.0


@pytest.fixture
def lay(planner_home: Path) -> Layout:
    lay = Layout(planner_home)
    lay.ensure()
    for key, text in (
        ("birth_date", "1971-06-15"),
        ("filing_status", "single"),
        ("state", "NC"),
        ("se_income", "40,000"),
        ("wages", "0"),
        ("ordinary_dividends", "0"),
        ("qualified_dividends", "0"),
    ):
        enter(lay, YEAR, key, text)
    facts = [
        db.Fact(
            "1099-INT", YEAR, "Example Bank (synthetic)", "1", "Interest", 1000.0, 1
        ),
        db.Fact(
            "1099-INT", YEAR, "Example Bank (synthetic)", "4", "Withheld", 100.0, 1
        ),
    ]
    for m in range(1, 13):
        for box, value in (("premium", PREMIUM), ("slcsp", SLCSP), ("aptc", APTC)):
            facts.append(
                db.Fact("1095-A", YEAR, "NC-synthetic", f"{box}_{m:02d}", box, value, 1)
            )
    conn = db.connect(lay.data / "ledger" / "planner.db")
    db.add_document(
        conn,
        fingerprint="synthetic",
        file_name="synthetic.pdf",
        kind="pdf",
        pages=1,
        batch="b1",
        facts=facts,
    )
    conn.close()
    esttax.record(lay, YEAR, "fed", "2025-04-15", 1500.0)
    return lay


def test_repayment_cap_table() -> None:
    assert draft.repayment_cap(2025, "SINGLE", 150) == 375.0
    assert draft.repayment_cap(2025, "SINGLE", 299) == 975.0
    assert draft.repayment_cap(2025, "JOINT", 350) == 3250.0
    assert draft.repayment_cap(2025, "SINGLE", 400) is None
    assert draft.repayment_cap(2026, "SINGLE", 150) is None  # P.L. 119-21
    assert draft.repayment_cap(2024, "SINGLE", 150) is None  # not tabled: repay all


def test_draft_return_ties_out(lay: Layout) -> None:
    d = draft.build(lay, YEAR)
    g = d.get
    assert not [n for n in d.notes if n.startswith("CHECK")], d.notes
    # income and the schedules that feed it
    assert g("Sch 1", "3") == 40000.0 and g("1040", "8") == 40000.0
    assert g("1040", "2b") == 1000.0
    assert g("1040", "9") == 41000.0
    se = g("Sch SE", "12")
    assert se == pytest.approx(40000 * 0.9235 * 0.153, abs=1)
    assert g("Sch 1", "15") == pytest.approx(se / 2, abs=0.01)
    assert g("1040", "11a") == pytest.approx(41000 - g("1040", "10"), abs=0.01)
    assert g("1040", "14") == pytest.approx(
        g("1040", "12e") + g("1040", "13a") + g("1040", "13b"), abs=0.01
    )
    assert g("1040", "12e") == 15750.0  # 2025 single standard deduction (P.L. 119-21)
    # Form 8962: twelve months, the advance runs ahead, the 2025 cap applies
    pct = g("8962", "5")
    assert 200 <= pct < 300
    assert 0.02 < g("8962", "7") < 0.06 and g("8962", "7") != round(g("8962", "7"), 2)
    assert g("8962", "8a") == round(g("8962", "3") * g("8962", "7"))
    assert g("8962", "8b") == round(g("8962", "8a") / 12)
    allowed = min(PREMIUM, max(SLCSP - g("8962", "8b"), 0))
    assert g("8962", "12e") == pytest.approx(allowed, abs=0.01)
    assert g("8962", "24") == pytest.approx(12 * allowed, abs=0.05)
    assert g("8962", "25") == 12 * APTC
    assert g("8962", "28") == 975.0
    assert g("8962", "29") == min(g("8962", "27"), 975.0)
    assert g("Sch 2", "1a") == g("8962", "29") and g("Sch 3", "9") == 0.0
    assert g("1040", "17") == g("Sch 2", "3")
    # payments and the bottom line
    assert g("1040", "25b") == 100.0 and g("1040", "26") == 1500.0
    assert g("1040", "24") == pytest.approx(g("1040", "22") + g("1040", "23"), abs=0.01)
    owe, refund = g("1040", "37"), g("1040", "34")
    assert (owe or 0) - (refund or 0) == pytest.approx(
        g("1040", "24") - g("1040", "33"), abs=0.01
    )
    assert "1099-NEC from each client" in d.missing
    assert "short_term_gains" in d.unknown and "interest" not in d.unknown
    src = {(ln.form, ln.line): ln.source for ln in d.lines}
    assert "1099-INT" in src[("1040", "2b")] and "1099-INT" in src[("1040", "25b")]
    text = draft.render(d)
    assert "Form 8962" in text and "Schedule SE" in text
    assert "line numbers follow the 2025 forms" in text


def test_cli_draft_json(lay: Layout) -> None:
    r = runner.invoke(app, ["draft", "--year", str(YEAR), "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["year"] == YEAR
    lines = {(ln["form"], ln["line"]): ln for ln in data["lines"]}
    assert lines[("1040", "2b")]["source"]
    assert lines[("1040", "26")]["value"] == 1500.0
