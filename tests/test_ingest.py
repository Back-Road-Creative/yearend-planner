"""The intake pass on synthetic PDFs: parse, commit, archive, supersede, unmatch."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from planner.cli import app
from planner.ingest import ingest
from planner.ingest.pdf import Unmatched, load_templates, parse_amount, parse_pdf
from planner.ledger import db
from planner.paths import Layout
from tests.pdfgen import make_pdf

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates" / "forms"
runner = CliRunner()

INT_2025 = [
    "Form 1099-INT Interest Income",
    "Tax year 2025",
    "PAYER'S name: Example Bank (synthetic)",
    "1 Interest income $ 1,234.56",
    "4 Federal income tax withheld $ 0.00",
]
DIV_2025 = [
    "Form 1099-DIV Dividends and Distributions",
    "Tax year 2025",
    "PAYER'S name: Example Brokerage (synthetic)",
    "1a Total ordinary dividends $ 9,800.00",
    "1b Qualified dividends $ 8,100.00",
    "2a Total capital gain distr. $ 150.00",
    "12 Exempt-interest dividends $ 0.00",
]
B_2025 = [
    "Form 1099-B Proceeds From Broker and Barter Exchange Transactions",
    "Tax year 2025",
    "PAYER'S name: Example Brokerage (synthetic)",
    "Short-term transactions total proceeds $ 12,000.00",
    "Short-term transactions total cost basis $ 12,500.00",
    "Long-term transactions total proceeds $ 60,000.00",
    "Long-term transactions total cost basis $ 40,000.00",
    "Wash sale loss disallowed $ 200.00",
]
A_2025 = [
    "Form 1095-A Health Insurance Marketplace Statement",
    "Calendar year 2025",
    "Marketplace identifier: NC-synthetic",
    "21 January 450.00 520.00 300.00",
    "22 February 450.00 520.00 300.00",
    "33 Annual Totals 5,400.00 6,240.00 3,600.00",
]


@pytest.fixture
def lay(planner_home: Path) -> Layout:
    lay = Layout(planner_home)
    lay.ensure()
    return lay


def inbox(lay: Layout) -> Path:
    return lay.data / "inbox"


def test_parse_amount_forms() -> None:
    assert parse_amount("$ 1,234.56") == 1234.56
    assert parse_amount("(12.00)") == -12.0
    assert parse_amount("-7") == -7.0


def test_single_form_parses_with_issuer_and_year(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "int.pdf", [INT_2025])
    forms = parse_pdf(pdf, load_templates(TEMPLATES))
    assert len(forms) == 1
    f = forms[0]
    assert (f.form, f.tax_year, f.issuer) == (
        "1099-INT",
        2025,
        "Example Bank (synthetic)",
    )
    assert f.boxes["1"] == ("Interest income", 1234.56)
    assert f.boxes["4"][1] == 0.0
    assert "3" not in f.boxes  # optional box absent is absent, not zero


def test_missing_required_box_is_unmatched(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "bad.pdf", [DIV_2025[:4]])  # no 1b
    with pytest.raises(Unmatched, match="required boxes not found: 1b"):
        parse_pdf(pdf, load_templates(TEMPLATES))


def test_consolidated_1099_yields_three_forms(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "consolidated.pdf", [DIV_2025, INT_2025, B_2025])
    forms = parse_pdf(pdf, load_templates(TEMPLATES))
    assert sorted(f.form for f in forms) == ["1099-B", "1099-DIV", "1099-INT"]
    b = next(f for f in forms if f.form == "1099-B")
    assert b.boxes["lt_basis"][1] == 40000.0 and b.boxes["wash_sale"][1] == 200.0


def test_copy_b_and_copy_c_must_agree(tmp_path: Path) -> None:
    other = INT_2025[:3] + ["1 Interest income $ 1,234.57"]
    pdf = make_pdf(tmp_path / "copies.pdf", [INT_2025, other])
    with pytest.raises(Unmatched, match="page 1 says 1234.56 but page 2 says 1234.57"):
        parse_pdf(pdf, load_templates(TEMPLATES))


def test_1095a_monthly_columns(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "a.pdf", [A_2025])
    f = parse_pdf(pdf, load_templates(TEMPLATES))[0]
    assert f.issuer == "NC-synthetic"
    assert f.boxes["slcsp_01"][1] == 520.0 and f.boxes["aptc_01"][1] == 300.0
    assert f.boxes["slcsp_annual"][1] == 6240.0
    assert f.boxes["premium_01"][1] == 450.0 and f.boxes["premium_annual"][1] == 5400.0


def test_ingest_commits_archives_and_is_idempotent(lay: Layout) -> None:
    make_pdf(inbox(lay) / "int.pdf", [INT_2025])
    make_pdf(inbox(lay) / "div.pdf", [DIV_2025])
    (inbox(lay) / "notes.txt").write_text("not a document")
    rep = ingest(lay)
    assert [i.file_name for i in rep.imported] == ["div.pdf", "int.pdf"]
    assert rep.unmatched == [("notes.txt", "unknown file type '.txt'")]
    assert (lay.data / "archive" / "2025" / "int.pdf").exists()
    assert (inbox(lay) / "UNMATCHED" / "notes.txt.reason.txt").exists()
    assert inbox_files_left(lay) == []
    conn = db.connect(lay.data / "ledger" / "planner.db")
    rows = db.facts_for(conn, 2025)
    assert {(r.form, r.box, r.value) for r in rows} >= {
        ("1099-INT", "1", 1234.56),
        ("1099-DIV", "1a", 9800.0),
        ("1099-DIV", "1b", 8100.0),
    }
    # the same file dropped again is a no-op, and the inbox still empties
    make_pdf(inbox(lay) / "int.pdf", [INT_2025])
    rep2 = ingest(lay)
    assert rep2.duplicates == ["int.pdf"] and rep2.imported == []
    assert inbox_files_left(lay) == []
    assert len(db.facts_for(conn, 2025, "1099-INT")) == 2


def inbox_files_left(lay: Layout) -> list[str]:
    return sorted(p.name for p in inbox(lay).iterdir() if p.is_file())


def test_corrected_form_supersedes_by_form_issuer_year(lay: Layout) -> None:
    make_pdf(inbox(lay) / "int.pdf", [INT_2025])
    ingest(lay)
    corrected = INT_2025[:3] + ["1 Interest income $ 1,300.00", "CORRECTED"]
    make_pdf(inbox(lay) / "int-corrected.pdf", [corrected])
    ingest(lay)
    conn = db.connect(lay.data / "ledger" / "planner.db")
    live = db.facts_for(conn, 2025, "1099-INT")
    assert [(r.box, r.value, r.file_name) for r in live] == [
        ("1", 1300.0, "int-corrected.pdf")
    ]
    old = db.facts_for(conn, 2025, "1099-INT", status="superseded")
    assert [(r.box, r.value, r.file_name) for r in old] == [
        ("1", 1234.56, "int.pdf"),
        ("4", 0.0, "int.pdf"),
    ]


def test_zip_scanned_and_garbage_land_in_unmatched(lay: Layout) -> None:
    inner = make_pdf(lay.root / "inner.pdf", [DIV_2025])
    with zipfile.ZipFile(inbox(lay) / "drop.zip", "w") as zf:
        zf.write(inner, "inner.pdf")
    (inbox(lay) / "garbage.pdf").write_bytes(b"%PDF-1.4 nope")
    make_pdf(inbox(lay) / "blank.pdf", [[]])
    (inbox(lay) / "photo.jpg").write_bytes(b"\xff\xd8\xff")
    rep = ingest(lay)
    assert [i.file_name for i in rep.imported] == ["inner.pdf"]
    reasons = dict(rep.unmatched)
    assert "no text layer" in reasons["blank.pdf"]
    assert "not a readable PDF" in reasons["garbage.pdf"]
    assert "OCR" in reasons["photo.jpg"]
    assert not (inbox(lay) / "drop.zip").exists()


def test_cli_ingest_and_facts(lay: Layout) -> None:
    make_pdf(inbox(lay) / "a.pdf", [A_2025])
    r = runner.invoke(app, ["ingest"])
    assert r.exit_code == 0, r.output
    assert "imported  a.pdf: 1095-A 2025 (NC-synthetic)" in r.output
    r = runner.invoke(app, ["facts", "--year", "2025", "--form", "1095-A"])
    assert r.exit_code == 0, r.output
    assert "slcsp_01" in r.output and "520.00" in r.output


def test_money_is_stored_as_integer_cents(lay: Layout) -> None:
    make_pdf(inbox(lay) / "int.pdf", [INT_2025])
    ingest(lay)
    conn = db.connect(lay.data / "ledger" / "planner.db")
    raw = conn.execute("SELECT value_cents FROM facts WHERE box = '1'").fetchone()[0]
    assert raw == 123456 and isinstance(raw, int)
    assert db.to_cents(0.015) == 2 and db.to_cents(-12.345) == -1235
