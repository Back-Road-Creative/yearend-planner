"""Unit 7h2a: the official 2025 Form 1040 and its Schedules 1, 2, 3, C, D and SE
filled with synthetic figures (tests/fixtures/real/forms.yaml, read back by
test_real_forms and test_scans). These tests pin what that took: a return's
pages are printed together, a filed return owns its page against the forms it
names, a schedule's total may sit on its second page, and a ruled page whose
cells hold none of a form's boxes is read again from its plain text. A scanned
return reads its own way: a line's number stays apart from its amount, and the
plain text, which keeps each line's number, goes first."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from planner.ingest import pdf
from planner.ingest.pdf import Ruled, Unmatched, parse_texts
from tests.pdfgen import make_pdf
from tests.test_forms import SCH1
from tests.test_real_forms import TEMPLATES, real_forms


def two_page_blank(tmp_path: Path) -> Path:
    """A blank of two pages with one named field on each."""
    from pypdf import PdfWriter
    from pypdf.generic import ArrayObject, DictionaryObject, FloatObject, NameObject
    from pypdf.generic import TextStringObject as Text

    base = make_pdf(tmp_path / "base.pdf", [["Form T-1 page 1"], ["Form T-1 page 2"]])
    writer = PdfWriter(clone_from=base)
    for n, name in enumerate(("Page1[0].f1_1[0]", "Page2[0].f2_1[0]")):
        widget = DictionaryObject()
        widget[NameObject("/Subtype")] = NameObject("/Widget")
        widget[NameObject("/T")] = Text(name)
        widget[NameObject("/Rect")] = ArrayObject(
            FloatObject(v) for v in (100, 600, 300, 640)
        )
        writer.pages[n][NameObject("/Annots")] = ArrayObject(
            [writer._add_object(widget)]
        )
    blank = tmp_path / "blank.pdf"
    with blank.open("wb") as fh:
        writer.write(fh)
    return blank


def test_a_return_prints_its_pages_together(tmp_path: Path) -> None:
    from pypdf import PdfReader

    blank = two_page_blank(tmp_path)
    values = {"f1_1[0]": "Alex (synthetic)", "f2_1[0]": "5,100.00"}
    out = real_forms.fill(blank, [0, 1], values, tmp_path / "out.pdf")
    pages = [p.extract_text() for p in PdfReader(out).pages]
    assert "Alex (synthetic)" in pages[0] and "5,100.00" not in pages[0]
    assert "5,100.00" in pages[1]
    # a key must name one field across all the pages
    with pytest.raises(ValueError, match="matches"):
        real_forms.fill(blank, [0, 1], {"_1[0]": "1"}, tmp_path / "x.pdf")
    # scanned, every page comes through
    scan = real_forms.scan(out, tmp_path / "scan.pdf")
    assert len(PdfReader(scan).pages) == 2


def test_a_filed_return_owns_its_page_against_the_forms_it_names() -> None:
    page = [
        "Form 1040 (2025) U.S. Individual Income Tax Return",
        "Attach Sch. B if required. Attach Forms W-2G and 1099-R if tax was withheld.",
        "11a This is your adjusted gross income 11a 60,735.00",
    ]
    (got,) = parse_texts(["\n".join(page)], TEMPLATES)
    assert (got.form, got.issuer, got.boxes["11"][1]) == ("1040", "self", 60735.0)


def test_a_schedule_total_on_its_second_page_is_required_there() -> None:
    first = [
        "SCHEDULE 1 Additional Income and Adjustments to Income",
        "3 Business income or (loss). Attach Schedule C . . . . . 3 14,000.00",
        "Schedule 1 (Form 1040) 2025",
    ]
    second = [
        "Schedule 1 (Form 1040) 2025 Page 2",
        "Part II Adjustments to Income",
        "11 Educator expenses . . . . . . . 11",
        "15 Deductible part of self-employment tax. Attach Schedule SE . 15 989.00",
        "26 Add lines 11 through 23 and 25. These are your adjustments to income."
        " Enter here and on Form",
        "1040, 1040-SR, or 1040-NR, line 10 . . . . . . 26 4,189.00",
    ]
    (got,) = parse_texts(["\n".join(first), "\n".join(second)], TEMPLATES)
    assert {k: v[1] for k, v in got.boxes.items()} == {
        "3": 14000.0,
        "15": 989.0,
        "26": 4189.0,
    }
    with pytest.raises(Unmatched, match="required boxes not found: 26"):
        parse_texts(["\n".join(first), "\n".join(second[:4])], TEMPLATES)


def test_cells_with_none_of_a_forms_boxes_give_way_to_the_plain_text() -> None:
    ruled = Ruled("¦ " + SCH1[0], "\n".join(SCH1))
    (got,) = parse_texts([ruled], TEMPLATES)
    assert got.form == "1040-SCH1" and got.boxes["16"][1] == 6152.0


def amount(pattern: re.Pattern[str], text: str) -> str | None:
    m = pattern.search(text)
    return m.group(1) if m else None


def test_a_scanned_return_line_keeps_its_number_apart_from_its_amount() -> None:
    line = re.compile(r"\b10\s+Adjustments[^\n]*?\b10\s+" + pdf.AMOUNT, re.I)
    loose = pdf._loose_line(line)
    # "1040" is not line 10's 40, wherever the scan closes up the spaces
    assert not loose.search("10 Adjustments to income, Form 1040")
    assert amount(loose, "10Adjustments to income 10 4,189.00") == "4,189.00"
    # the scan may lose the number at the right: the amount ends the line, cents too
    assert amount(loose, "10 Adjustments to income 4,189.00\n") == "4,189.00"
    assert not loose.search("10 Adjustments to income 41\n")
    # a label alone ("Taxable amount" is 4b, 5b and 6b) still needs the number
    shared = pdf._loose_line(
        re.compile(r"Taxable amount[\s.]*5b\s+" + pdf.AMOUNT, re.I)
    )
    assert not shared.search("4a 5,000.00 b Taxable amount. 5,000.00")
    assert amount(shared, "bTaxable amount. 5b 900.00") == "900.00"


def test_a_fullwidth_bracket_from_the_scan_reads_as_ascii() -> None:
    assert pdf.normalize("Schedule 3（Form 1040）") == "Schedule 3(Form 1040)"


def test_a_scanned_return_reads_its_plain_text_first_and_its_cells_after() -> None:
    plain = [
        "Form 1040 (2025) U.S. Individual Income Tax Return",
        "9 Add lines 1z through 8. This is your total income 9 70,000.00",
        "11a Subtract line 10 from line 9. This is your adjusted gross income 11a",
    ]
    cells = "¦ This is your adjusted gross income 11a 60,735.00"
    (got,) = parse_texts([Ruled(cells, "\n".join(plain))], TEMPLATES, ocr=True)
    assert got.form == "1040" and got.ocr
    assert got.boxes["9"][1] == 70000.0 and got.boxes["11"][1] == 60735.0
    # a spot in the cells counts from the top; one in the plain text, past them
    assert got.spots["11"] == (1, 0) and got.spots["9"] == (1, 1 + 1)
