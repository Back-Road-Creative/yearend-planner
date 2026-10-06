"""The intake pass on synthetic PDFs: parse, commit, archive, supersede, unmatch."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from planner.cli import app
from planner.ingest import MAX_ZIP_DEPTH, ingest
from planner.ingest.pdf import (
    Unmatched,
    load_templates,
    parse_amount,
    parse_pdf,
    parse_texts,
)
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


def test_div_capital_gain_subsets_parse() -> None:
    """Boxes 2b-2d split box 2a for the Schedule D line 18 and 19 worksheets;
    a form that leaves them blank still parses."""
    templates = load_templates(
        Path(__file__).resolve().parent.parent / "templates" / "forms"
    )
    text = "\n".join(
        [
            *DIV_2025[:-1],
            "2b Unrecap. Sec. 1250 gain $ 40.00",
            "2c Section 1202 gain $ 0.00",
            "2d Collectibles (28%) gain $ 25.00",
            DIV_2025[-1],
        ]
    )
    (f,) = parse_texts([text], templates)
    assert f.boxes["2a"][1] == 150.0
    assert f.boxes["2b"][1] == 40.0
    assert f.boxes["2c"][1] == 0.0
    assert f.boxes["2d"][1] == 25.0
    (bare,) = parse_texts(["\n".join(DIV_2025)], templates)
    assert not {"2b", "2c", "2d"} & set(bare.boxes)


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


def zip_of(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


def left_in_inbox(lay: Layout) -> list[str]:
    """Everything still in the inbox, apart from the UNMATCHED folder."""
    return sorted(p.name for p in inbox(lay).iterdir() if p.name != "UNMATCHED")


def pdf_bytes(lay: Layout, lines: list[str]) -> bytes:
    return make_pdf(lay.root / "scratch.pdf", [lines]).read_bytes()


def test_zip_with_subfolder_imports(lay: Layout) -> None:
    zip_of(
        inbox(lay) / "stmts.zip",
        {
            "stmts/1099-div.pdf": pdf_bytes(lay, DIV_2025),
            "stmts/deep/1099-int.pdf": pdf_bytes(lay, INT_2025),
        },
    )
    rep = ingest(lay)
    assert sorted(i.file_name for i in rep.imported) == ["1099-div.pdf", "1099-int.pdf"]
    assert rep.unmatched == []
    assert (lay.data / "archive" / "2025" / "1099-div.pdf").exists()
    assert left_in_inbox(lay) == []  # the emptied folders are tidied away


def test_loose_subfolders_in_the_inbox_are_read(lay: Layout) -> None:
    (inbox(lay) / "from-bank").mkdir()
    make_pdf(inbox(lay) / "from-bank" / "int.pdf", [INT_2025])
    rep = ingest(lay)
    assert [i.file_name for i in rep.imported] == ["int.pdf"]
    assert left_in_inbox(lay) == []


def test_corrupt_zip_goes_to_unmatched_once_with_a_reason(lay: Layout) -> None:
    (inbox(lay) / "broken.zip").write_bytes(b"PK\x03\x04 this is not a zip")
    make_pdf(inbox(lay) / "int.pdf", [INT_2025])
    rep = ingest(lay)
    assert [i.file_name for i in rep.imported] == ["int.pdf"]
    ((name, why),) = rep.unmatched
    assert name == "broken.zip" and "not a readable ZIP" in why
    um = inbox(lay) / "UNMATCHED"
    assert (um / "broken.zip").exists()
    assert "not a readable ZIP" in (um / "broken.zip.reason.txt").read_text()
    assert not (inbox(lay) / "broken.zip").exists()
    assert ingest(lay).unmatched == []  # not retried or re-reported every run


def test_a_bad_zip_does_not_hold_back_a_good_one(lay: Layout) -> None:
    zip_of(
        inbox(lay) / "evil.zip",
        {"ok.pdf": pdf_bytes(lay, INT_2025), "../escape.pdf": b"x"},
    )
    zip_of(inbox(lay) / "good.zip", {"div.pdf": pdf_bytes(lay, DIV_2025)})
    rep = ingest(lay)
    assert [i.file_name for i in rep.imported] == ["div.pdf"]
    ((name, why),) = rep.unmatched
    assert name == "evil.zip" and "escapes" in why
    assert (inbox(lay) / "UNMATCHED" / "evil.zip").exists()
    assert not (lay.data / "escape.pdf").exists()  # nothing from it was extracted
    assert not (lay.data / "archive" / "2025" / "ok.pdf").exists()


def test_a_password_protected_zip_is_unmatched_with_that_reason(lay: Layout) -> None:
    path = zip_of(inbox(lay) / "locked.zip", {"secret.pdf": b"ciphertext"})
    data = bytearray(path.read_bytes())
    for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        at = data.index(signature)
        data[at + offset] |= 0x1  # general-purpose flag bit 0: encrypted
    path.write_bytes(bytes(data))
    ((name, why),) = ingest(lay).unmatched
    assert name == "locked.zip" and "password" in why


def test_zip_name_clash_does_not_overwrite(lay: Layout) -> None:
    make_pdf(inbox(lay) / "stmt.pdf", [INT_2025])
    zip_of(inbox(lay) / "more.zip", {"stmt.pdf": pdf_bytes(lay, DIV_2025)})
    rep = ingest(lay)
    assert len(rep.imported) == 2 and rep.unmatched == []
    conn = db.connect(lay.data / "ledger" / "planner.db")
    assert db.facts_for(conn, 2025, "1099-INT") and db.facts_for(conn, 2025, "1099-DIV")


def test_unreadable_files_of_one_name_keep_each_reason(lay: Layout) -> None:
    for folder, text in (("a", "first"), ("b", "second"), ("c", "third"), ("d", "4th")):
        (inbox(lay) / folder).mkdir()
        (inbox(lay) / folder / "notes.txt").write_text(text)
    rep = ingest(lay)
    assert sorted(n for n, _ in rep.unmatched) == [
        "a/notes.txt",
        "b/notes.txt",
        "c/notes.txt",
        "d/notes.txt",
    ]
    um = inbox(lay) / "UNMATCHED"
    kept = [p.name for p in um.glob("notes*.txt") if "reason" not in p.name]
    assert sorted(kept) == [
        "notes (2).txt",
        "notes (3).txt",
        "notes (4).txt",
        "notes.txt",
    ]
    reasons = {p.name: p.read_text() for p in um.glob("*.reason.txt")}
    assert len(reasons) == 4 and all("from " in r for r in reasons.values())


def test_nested_zip_is_unpacked(lay: Layout) -> None:
    inner = zip_of(lay.root / "inner.zip", {"q/div.pdf": pdf_bytes(lay, DIV_2025)})
    zip_of(inbox(lay) / "outer.zip", {"wrap/inner.zip": inner.read_bytes()})
    rep = ingest(lay)
    assert [i.file_name for i in rep.imported] == ["div.pdf"]
    assert rep.unmatched == []


def test_zip_nested_too_deep_is_unmatched(lay: Layout) -> None:
    data = pdf_bytes(lay, DIV_2025)
    name = "pdf"
    for depth in range(MAX_ZIP_DEPTH + 1):
        data = zip_of(lay.root / f"z{depth}.zip", {f"in.{name}": data}).read_bytes()
        name = "zip"
    (inbox(lay) / "deep.zip").write_bytes(data)
    rep = ingest(lay)
    assert rep.imported == []
    assert len(rep.unmatched) == 1 and "nested" in rep.unmatched[0][1]


def test_mac_zip_debris_is_ignored(lay: Layout) -> None:
    zip_of(
        inbox(lay) / "mac.zip",
        {
            "div.pdf": pdf_bytes(lay, DIV_2025),
            "__MACOSX/._div.pdf": b"resource fork",
            ".DS_Store": b"x",
        },
    )
    rep = ingest(lay)
    assert [i.file_name for i in rep.imported] == ["div.pdf"] and rep.unmatched == []
    assert left_in_inbox(lay) == []


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


def test_text_facts_are_stored_as_text_and_kept_out_of_numeric_reads(
    lay: Layout,
) -> None:
    from tests.test_forms import F1099R

    make_pdf(inbox(lay) / "r.pdf", [F1099R])
    ingest(lay)
    conn = db.connect(lay.data / "ledger" / "planner.db")
    raw = conn.execute(
        "SELECT value_cents, value_text FROM facts WHERE box = '7'"
    ).fetchone()
    assert (raw[0], raw[1]) == (0, "G")
    assert [f.box for f in db.facts_for(conn, 2025, "1099-R")] == ["1", "2a", "4"]
    (code,) = db.facts_for(conn, 2025, "1099-R", text=True)
    assert (code.box, code.text) == ("7", "G")
    assert {f.box for f in db.facts_for(conn, 2025, "1099-R", text=None)} == {
        "1",
        "2a",
        "4",
        "7",
    }
    r = runner.invoke(app, ["facts", "--year", "2025", "--form", "1099-R"])
    assert r.exit_code == 0, r.output
    assert "box 7" in r.output and "G" in r.output.split("box 7")[1].split("\n")[0]
    assert "4 facts" in r.output


V3_LEDGER = """
CREATE TABLE schema_version (version INTEGER NOT NULL);
INSERT INTO schema_version VALUES (3);
CREATE TABLE documents (
    id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE,
    file_name TEXT NOT NULL, kind TEXT NOT NULL, pages INTEGER NOT NULL DEFAULT 0,
    imported_at TEXT NOT NULL, batch TEXT NOT NULL, archived_as TEXT);
CREATE TABLE facts (
    id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES documents(id),
    form TEXT NOT NULL, tax_year INTEGER NOT NULL, issuer TEXT NOT NULL,
    box TEXT NOT NULL, label TEXT NOT NULL, value_cents INTEGER NOT NULL,
    page INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('accepted', 'pending', 'superseded')),
    UNIQUE (document_id, form, tax_year, issuer, box));
INSERT INTO documents VALUES (1, 'abc', 'old.pdf', 'pdf', 1, '2025-01-01', 'b', NULL);
INSERT INTO facts VALUES (1, 1, '1099-INT', 2024, 'Bank', '1', 'Interest', 123456, 1,
    'accepted');
"""


def test_a_v3_ledger_migrates_to_text_facts(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "ledger" / "planner.db"
    path.parent.mkdir()
    old = sqlite3.connect(path)
    old.executescript(V3_LEDGER)
    old.commit()
    old.close()
    conn = db.connect(path)
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 4
    assert db.SCHEMA_VERSION == 4
    (kept,) = db.facts_for(conn, 2024, "1099-INT")
    assert (kept.box, kept.value, kept.text) == ("1", 1234.56, None)
    db.add_document(
        conn,
        fingerprint="def",
        file_name="new.pdf",
        kind="pdf",
        pages=1,
        batch="b",
        facts=[db.Fact("1099-R", 2025, "Custodian", "7", "Code", 0.0, 1, text="G")],
    )
    (code,) = db.facts_for(conn, 2025, "1099-R", text=True)
    assert code.text == "G"
    conn.close()
    db.connect(path).close()  # opening again changes nothing
