"""Unit 7h2b: what reading the official 2025 returns of ten states took (the
returns themselves are fixtures of tests/fixtures/real/forms.yaml, read back by
test_real_forms and test_scans). A state
form may print a row of spaces under a field, draw its amounts one digit to a
box, set a filled figure a little above its line, or turn a word sideways
partway down the page; a scan may read a capital I as "l", a line's number
before its label, or a line number where an amount would be."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from planner.ingest import ocr, pdf
from tests.pdfgen import make_pdf
from tests.test_real_forms import TEMPLATES, real_forms


def page(tmp_path: Path, *ops: str) -> str:
    """The text read from a page drawn with ``ops`` (Helvetica as ``/F1``)."""
    path = make_pdf(tmp_path / "p.pdf", ["\n".join(ops).encode("latin-1")])
    return str(pdf.page_texts(path)[0])


def text(x: float, y: float, s: str, size: float = 10) -> str:
    return f"BT /F1 {size} Tf 1 0 0 1 {x} {y} Tm ({s}) Tj ET"


def test_a_word_turned_sideways_partway_down_reads_after_the_page(
    tmp_path: Path,
) -> None:
    got = page(
        tmp_path,
        text(50, 700, "1. First line 1. 100.00"),
        "BT /F1 10 Tf 0 1 -1 0 300 600 Tm (PAID PREPARER) Tj ET",
        text(50, 500, "2. Second line 2. 200.00"),
    )
    assert got.splitlines()[:2] == [
        "1. First line 1. 100.00",
        "2. Second line 2. 200.00",
    ]
    turned = "".join(got.splitlines()[2:]).replace(" ", "")
    assert turned in ("PAIDPREPARER", "PAIDPREPARER"[::-1])


def test_a_row_of_spaces_under_a_field_does_not_split_its_digits(
    tmp_path: Path,
) -> None:
    got = page(
        tmp_path,
        text(50, 700, "5 Total 5"),
        text(150, 700, " " * 30),
        text(160, 700, "2,900.00"),
    )
    assert "5 Total 5 2,900.00" in got


def test_a_comb_of_boxes_reads_as_one_figure_on_its_labels_line(
    tmp_path: Path,
) -> None:
    boxes = [f"{200 + 14 * n} 700 12 12 re S" for n in range(8)]
    # the first box stays empty; the form prints its comma and point below
    digits = [text(217 + 14 * n, 702, d) for n, d in enumerate("6073500")]
    marks = [text(241, 698, ","), text(283, 698, ".")]
    got = page(tmp_path, text(50, 698, "15 Total income 15"), *boxes, *digits, *marks)
    assert "15 Total income 15 60,735.00" in got


@pytest.mark.parametrize(
    ("label", "gap", "joined"),
    [
        ("8. Total income ........ 8.", 20, True),
        # the GA-500 sets its amounts right, well clear of the line's number
        ("8. Total income ........ 8.", 160, True),
        ("8. Total income ........ 8.", 260, False),
        # no stop after the number: the NC D-400's "12a" is a label's word
        ("Add Line 12a", 20, False),
    ],
)
def test_a_figure_set_above_its_line_moves_down_onto_it(
    tmp_path: Path, label: str, gap: float, joined: bool
) -> None:
    end = 50 + real_forms._width(label, 10)
    got = page(tmp_path, text(50, 600, label), text(end + gap, 605, "5,000.00"))
    assert (f"{label} 5,000.00" in got) is joined, got


def test_a_scanned_figure_set_above_its_line_moves_down_too() -> None:
    def word(t: str, x0: float, x1: float, top: float) -> dict[str, Any]:
        return {"text": t, "x0": x0, "x1": x1, "top": top, "bottom": top + 30}

    # at 200 dpi: the label's last word is its number, with no stop
    words = [word("4Totalincome. 4", 100, 600, 1000), word("60,735.00", 900, 1100, 985)]
    low = ocr._lowered(words)
    assert low[1]["top"] == 1000 and low[0] == words[0]
    beside = [*words, word("Step 2", 700, 850, 985)]
    assert ocr._lowered(beside)[1]["top"] == 985
    # a line's own number, with its label close on its right, stays put
    number = [
        word("Lines 30 and 29.", 100, 600, 1000),
        word("30", 700, 740, 985),
        word("Refund", 800, 950, 985),
    ]
    assert ocr._lowered(number)[1]["top"] == 985
    # a figure whose commas were read as points gets its commas back
    assert ocr._lowered([word("5.000.00", 900, 1100, 985)])[0]["text"] == "5,000.00"


def test_a_capital_i_read_as_l_is_put_back() -> None:
    assert ocr.capitals("2025MlCHlGAN TaxReturnMl-1040 (VAGl)") == (
        "2025MICHIGAN TaxReturnMI-1040 (VAGI)"
    )
    assert ocr.capitals("Illinois Alex small Bill total l 400WallSt") == (
        "Illinois Alex small Bill total l 400WallSt"
    )
    # and a capital O read as a zero; a figure's zeros stay
    assert ocr.capitals("115REFUND0RN0AM0uNTDUE P0B0X942840 SACRAMENT0 CA") == (
        "115REFUNDORNOAMOuNTDUE POBOX942840 SACRAMENTO CA"
    )
    assert ocr.capitals("1040-SR W-2 $0 0RN 10,000.00 CA94240-0001 A01") == (
        "1040-SR W-2 $0 ORN 10,000.00 CA94240-0001 A01"
    )


def test_a_line_number_read_before_its_label_is_kept() -> None:
    phrases = ("Add Lines 10, 11, 12, and 13", "Your Virginia withholding")
    assert pdf.repaired("14.AddLines1D,11,12,and13 14", phrases) == (
        "14. Add Lines 10, 11, 12, and 13 14"
    )
    assert pdf.repaired("19a.YourVirginia withholding.. 19a", phrases) == (
        "19a. Your Virginia withholding.. 19a"
    )
    # words read before a label do not leave its end behind
    assert pdf.repaired(
        "and STA amount on Line 17. 17", ("STA amount on Line 17. 17",)
    ) == ("STA amount on Line 17. 17")


@pytest.mark.parametrize(
    ("line", "want"),
    [
        ("13 1,200.00 Next", "1,200.00"),
        ("13 12.Unpaid tax", None),
        ("13 19a. Other", None),
        ("13 00", None),
    ],
)
def test_a_line_number_or_printed_cents_is_never_an_amount(
    line: str, want: str | None
) -> None:
    m = re.compile(r"13\s+" + pdf.AMOUNT).search(line)
    assert (m.group(1) if m else None) == want


def test_an_empty_lines_amount_is_not_the_next_lines_figure() -> None:
    line = re.compile(r"instructions\. 10[ \t]*" + pdf.AMOUNT)
    assert not line.search("See instructions. 10\n8,500 00\n11. If you do not")
    got = line.search("See instructions. 10 $\n8,500 00")
    assert got and got.group(1) == "$\n8,500"


def test_a_private_use_glyph_is_dropped() -> None:
    assert pdf.normalize("12,500.00") == "12,500.00"


@pytest.mark.parametrize(
    ("form", "page_text"),
    [
        ("OH-IT1040", "Ohio IT 1040 2025 Individual Income Tax Return"),
        ("OH-IT1040", "2025 Ohio IT 1040 Individual Income Tax Return"),
        ("GA-500", "Georgia Department of Revenue YOUR SOCIAL SECURITY NUMBER 2025"),
    ],
)
def test_a_year_reads_wherever_a_scan_puts_it(form: str, page_text: str) -> None:
    (tpl, *_) = [t for t in TEMPLATES if t.form == form and t.filed]
    assert tpl.find_year(page_text) == 2025


def test_a_state_return_does_not_pass_for_a_1040_page_2() -> None:
    state = (
        "Line 1 Federal adjusted gross income, U.S. Form 1040 (see instructions) Page 2"
    )
    assert not [t for t in TEMPLATES if t.form == "1040" and t.matches(state)]


def test_a_value_is_set_as_its_field_asks() -> None:
    rect = [100.0, 600.0, 300.0, 620.0]
    size = real_forms.SIZE

    def x(quad: int) -> float:
        (op,) = real_forms._ops(rect, "1.00", quad)
        return float(op.split()[8])

    width = real_forms._width("1.00", size)
    assert x(0) == pytest.approx(102, abs=0.06)
    assert x(1) + width / 2 == pytest.approx(200, abs=0.06)
    assert x(2) + width == pytest.approx(298, abs=0.06)


def test_a_widget_takes_its_alignment_and_box_from_its_field() -> None:
    from pypdf.generic import ArrayObject, DictionaryObject, FloatObject, NameObject
    from pypdf.generic import NumberObject as Num

    field = DictionaryObject({NameObject("/Q"): Num(2)})
    widget = DictionaryObject({NameObject("/Parent"): field})
    assert real_forms._quad(widget) == 2
    assert real_forms._quad(DictionaryObject()) == 0
    widget[NameObject("/Rect")] = ArrayObject(
        FloatObject(v) for v in (300, 640, 100, 600)
    )
    assert real_forms._rect(widget) == [100, 600, 300, 640]


def test_a_comb_takes_one_digit_a_box_and_no_more() -> None:
    assert real_forms._digits("60,735.00", 8, "15") == ["", *"6073500"]
    with pytest.raises(ValueError, match="more digits than its 6 boxes"):
        real_forms._digits("60,735.00", 6, "15")


def test_a_key_naming_a_field_exactly_wins_over_one_it_ends(tmp_path: Path) -> None:
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, DictionaryObject, FloatObject, NameObject
    from pypdf.generic import TextStringObject as Text

    base = make_pdf(tmp_path / "base.pdf", [["Form T-2"]])
    writer = PdfWriter(clone_from=base)
    annots = ArrayObject()
    for n, name in enumerate(("f1_1[0]", "Line.f1_1[0]")):
        widget = DictionaryObject()
        widget[NameObject("/Subtype")] = NameObject("/Widget")
        widget[NameObject("/T")] = Text(name)
        widget[NameObject("/Rect")] = ArrayObject(
            FloatObject(v) for v in (100, 600 - 40 * n, 300, 620 - 40 * n)
        )
        annots.append(writer._add_object(widget))
    writer.pages[0][NameObject("/Annots")] = annots
    blank = tmp_path / "blank.pdf"
    with blank.open("wb") as fh:
        writer.write(fh)
    out = real_forms.fill(blank, 0, {"f1_1[0]": "7.00"}, tmp_path / "out.pdf")
    assert "7.00" in PdfReader(out).pages[0].extract_text()
    import pdfplumber

    with pdfplumber.open(out) as doc:
        (word,) = [w for w in doc.pages[0].extract_words() if w["text"] == "7.00"]
    assert 792 - 620 < word["top"] < 792 - 600  # in f1_1[0], not Line.f1_1[0]
