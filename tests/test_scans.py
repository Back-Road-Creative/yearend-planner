"""Unit 7h1b: the official forms of unit 7h read back from scans. A scan has
no text layer, so the OCR engine reads it: the ruled boxes come from the image,
the check boxes too, and a label the engine misreads or runs together is put
back from the template's own wording. Each fixture is scanned the way an office
scanner would (``scripts/real_forms.py`` ``scan``: greyscale, a little crooked,
speckled) and must read back what its text layer does."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from planner.ingest import ocr, pdf
from planner.ingest.pdf import Ruled, parse_texts
from tests.test_ingest import INT_2025
from tests.test_real_forms import FORMS, REAL, TEMPLATES, real_forms

needs_engine = pytest.mark.skipif(
    not ocr.available(), reason="OCR engine not installed"
)


def test_a_label_the_scan_misread_is_put_back() -> None:
    phrases = ("1 Rents", "2 Royalties", "PAYER'S name, street address")
    assert pdf.repaired("¦ 1Rerts\n  $ 7,200.00", phrases) == "¦ 1 Rents\n  $ 7,200.00"
    assert (
        pdf.repaired("¦ 2 Royalti es\n  $ 5.00", phrases) == "¦ 2 Royalties\n  $ 5.00"
    )
    assert (
        pdf.repaired("¦ PAYERS name ,street addres\n  Example Co", phrases)
        == "¦ PAYER'S name, street address\n  Example Co"
    )
    # the box number that was read stands; a line like no label is left alone
    assert pdf.repaired("¦ 2Rerts\n  $ 7,200.00", phrases) == "¦ 2Rerts\n  $ 7,200.00"
    assert pdf.repaired("¦ Somethingelse\n  1", phrases) == "¦ Somethingelse\n  1"


def test_a_thousands_comma_the_scan_read_as_a_stop_still_reads_whole() -> None:
    # Windows renders the scan so the engine may take "8,450.00" for "8.450.00"
    amount = re.compile(r"1 Mortgage interest\s+" + pdf.AMOUNT)
    for read, value in (
        ("8.450.00", 8450.0),
        ("$12.000.00", 12000.0),
        ("1.234.567.89", 1234567.89),
        ("(1.500.00)", -1500.0),
        ("8,450.00", 8450.0),
        ("8.45", 8.45),
    ):
        m = amount.search(f"1 Mortgage interest {read}")
        assert m and pdf.parse_amount(m.group(1)) == value, read
    # a return line whose number the scan lost still takes the whole amount
    line = re.compile(r"\b10\s+Adjustments[^\n]*?\b10\s+" + pdf.AMOUNT, re.I)
    loose = pdf._loose_line(line)
    m = loose.search("10 Adjustments to income 4.189.00\n")
    assert m and pdf.parse_amount(m.group(1)) == 4189.0


def test_the_labels_come_from_the_templates_patterns_and_check_boxes() -> None:
    assert pdf._literal(r"\bPAYER'?S\s+name") == "PAYERS name"
    assert pdf._literal(r"\b1 Interest income\s+(\d)") == "1 Interest income"
    assert pdf._literal(r"(?:Short\-term\s+gain)") == "Short-term gain"


def test_a_scan_may_run_a_labels_words_together() -> None:
    assert pdf.split_name("ExampleBank(synthetic)") == "Example Bank (synthetic)"
    assert pdf.split_name("ABCHoldings") == "ABC Holdings"


def test_two_letter_words_one_above_the_other_are_kept() -> None:
    def word(text: str, top: float) -> dict[str, Any]:
        return {"text": text, "x0": 10.0, "x1": 20.0, "top": top, "bottom": top + 6}

    assert not pdf._stacked([word("or", 0), word("or", 7)])


def item(text: str, spans: Sequence[tuple[str, float]] | None = None) -> list[Any]:
    """An engine box, with each letter's place when ``spans`` gives them."""
    box = [[0, 0], [300, 0], [300, 20], [0, 20]]
    if spans is None:
        return [box, text, 0.99]
    places = [[[x, 0], [x + 8, 0], [x + 8, 20], [x, 20]] for _c, x in spans]
    return [box, text, 0.99, places, [c for c, _x in spans], [0.99] * len(spans)]


def test_an_engine_box_across_two_boxes_of_the_form_is_cut_in_two() -> None:
    cells = [(0.0, 0.0, 100.0, 50.0), (100.0, 0.0, 300.0, 50.0)]
    spans = [("1", 70), ("1", 80), ("2", 110), ("T", 120), (" ", 130), ("a", 140)]
    words = ocr.words_of([item("112 Ta", spans)], cells)
    assert [w["text"] for w in words] == ["11", "2T", "a"]
    assert (words[1]["x0"], words[1]["x1"]) == (110, 128)
    whole = ocr.words_of([item("no letters")], cells)
    assert [w["text"] for w in whole] == ["no letters"]


def test_a_square_the_engine_read_onto_a_label_comes_off_it() -> None:
    # Windows renders the square's edge so the engine reads it as a bracket on
    # the label beside it: "Long-term gain or loss]"
    square = (1055.0, 536.0, 1080.0, 562.0, True)
    near = {"text": "loss]", "x0": 1017.0, "x1": 1081.0, "top": 540.0, "bottom": 558.0}
    texts = [w["text"] for w in ocr.marked([near], [square])]
    assert texts == ["loss", "[X]"]
    before = {**near, "text": "[Ordinary", "x0": 1060.0, "x1": 1140.0}
    assert [w["text"] for w in ocr.marked([before], [square])][0] == "Ordinary"
    # a bracket clear of every square is the form's own
    clear = {**near, "text": "(loss]", "x1": 1050.0}
    assert [w["text"] for w in ocr.marked([clear], [square])][0] == "(loss]"


def upside_down_engine(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """An engine double that reads well only when the page's dark corner is at
    the top left, as an upright page has it; records whether each call was."""
    calls: list[bool] = []

    def engine(array: Any, **_kw: object) -> tuple[list[list[Any]], list[float]]:
        upright = int(array[2][2][0]) < 128
        calls.append(upright)
        box = [[30, 40], [300, 40], [300, 60], [30, 60]]
        return [[box, "Form 1099-INT", 0.99 if upright else 0.6]], [0.0]

    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "_engine", lambda: engine)
    return calls


def test_a_page_upside_down_is_read_turned_and_cropped_turned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from PIL import Image, ImageDraw

    calls = upside_down_engine(monkeypatch)
    image = Image.new("RGB", (400, 300), "white")
    ImageDraw.Draw(image).rectangle([0, 0, 20, 20], fill="black")
    upright = ocr._read(image)
    assert "Form 1099-INT" in upright and not upright.turned and calls == [True]  # type: ignore[attr-defined]
    calls.clear()
    image.rotate(180).save(tmp_path / "over.png")
    pages = ocr.page_texts(tmp_path / "over.png")
    assert "Form 1099-INT" in pages[0] and pages[0].turned  # type: ignore[attr-defined]
    assert calls == [False, True]


def test_a_spot_in_the_plain_text_counts_on_past_the_cells() -> None:
    plain = "\n".join(INT_2025)
    (alone,) = parse_texts([plain], TEMPLATES, ocr=True)
    ruled = Ruled("¦ nothing a template knows", plain)
    (shifted,) = parse_texts([ruled], TEMPLATES, ocr=True)
    assert alone.spots
    assert shifted.spots == {k: (p, n + 1) for k, (p, n) in alone.spots.items()}


@needs_engine
@pytest.mark.parametrize("entry", FORMS, ids=[e["name"] for e in FORMS])
def test_a_scan_of_a_real_form_reads_back_its_synthetic_values(
    entry: dict[str, Any], tmp_path: Path
) -> None:
    scan = real_forms.scan(REAL / f"{entry['name']}.pdf", tmp_path / "scan.pdf")
    (got,) = parse_texts(list(ocr.page_texts(scan)), TEMPLATES, ocr=True)
    want = entry["expect"]
    assert (got.form, got.tax_year, got.issuer, got.ocr) == (
        want["form"],
        want["year"],
        want["issuer"],
        True,
    )
    assert {k: v[1] for k, v in got.boxes.items()} == want["boxes"]


@needs_engine
def test_a_scan_fed_in_upside_down_reads_the_same(tmp_path: Path) -> None:
    (entry,) = [e for e in FORMS if e["name"] == "1099-int"]
    scan = real_forms.scan(REAL / "1099-int.pdf", tmp_path / "over.pdf", turn=180.4)
    texts = ocr.page_texts(scan)
    (got,) = parse_texts(list(texts), TEMPLATES, ocr=True)
    assert texts[0].turned  # type: ignore[attr-defined]
    assert {k: v[1] for k, v in got.boxes.items()} == entry["expect"]["boxes"]
