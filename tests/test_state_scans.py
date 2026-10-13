"""Unit 7h2b: what reading the ten states' returns from scans took. A state
form draws its own cents boxes ("|00|") beside each amount, its check boxes in
the same ink as a box holding a line's number, and some amounts one digit to a
box; a figure may sit off its line with its printed cents beside it; and the
engine misreads the letters and stops a template's wording leans on."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from planner.ingest import ocr, pdf
from planner.ingest.ocr import _word as word

PAGE = (1224, 400)  # a strip of a letter page at two pixels a point


def strip(*boxes: tuple[int, int, int, int]) -> Any:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", PAGE, "white")
    for box in boxes:
        ImageDraw.Draw(image).rectangle(box, outline="black", width=2)
    return image


def test_a_figure_off_its_line_takes_its_printed_cents_along() -> None:
    label = word("10", 1360, 1400, 1814, 1843)
    figure = word("930", 1498, 1541, 1799, 1828)
    low = ocr._lowered([label, figure, word("00", 1563, 1600, 1795, 1831)])
    assert (low[1]["top"], low[2]["top"]) == (1814, 1810)
    # cents well clear of the figure are some other box's, and stay
    far = ocr._lowered([label, figure, word("00", 1700, 1737, 1795, 1831)])
    assert (far[1]["top"], far[2]["top"]) == (1814, 1795)


def test_a_figure_read_with_the_printed_cents_after_its_own_loses_them() -> None:
    (got,) = ocr._lowered([word("2,850.00.00", 900, 1100, 985, 1015)])
    assert got["text"] == "2,850.00"


def test_a_cents_box_read_with_its_wall_reads_as_its_cents() -> None:
    image = strip((100, 100, 114, 124), (200, 100, 224, 124), (300, 100, 360, 124))
    boxes = ocr.cents_boxes(image)
    assert boxes == [(100.0, 100.0, 115.0, 125.0)]  # not the square, nor the cell
    got = ocr.uncented(
        [word("100", 101, 113, 104, 120), word("100", 400, 440, 104, 120)], boxes
    )
    assert [w["text"] for w in got] == ["00", "100"]


def test_a_box_holding_a_line_number_is_no_check_box() -> None:
    square = [(200.0, 100.0, 224.0, 124.0, True)]
    number = ocr.marked([word("46", 202, 222, 104, 120)], square)
    assert [w["text"] for w in number] == ["46"]
    lettered = ocr.marked([word("4b", 202, 222, 104, 120)], square)
    assert [w["text"] for w in lettered] == ["4b"]
    mark = ocr.marked([word("4", 202, 222, 104, 120)], square)
    assert [w["text"] for w in mark] == ["[X]"]


def test_a_word_running_into_a_check_box_loses_the_wall_read_there() -> None:
    square = [(200.0, 100.0, 224.0, 124.0, False)]
    got = ocr.marked([word("loss]", 150, 225, 104, 120)], square)
    assert [w["text"] for w in got] == ["loss"]
    # a word ending well short of the square keeps its bracket
    clear = ocr.marked([word("(loss)]", 100, 180, 104, 120)], square)
    assert [w["text"] for w in clear] == ["(loss)]"]


def test_a_square_is_a_check_box_and_a_tall_or_open_box_is_not() -> None:
    image = strip((200, 100, 224, 124), (300, 100, 360, 124), (400, 100, 415, 130))
    assert [q[:4] for q in ocr.check_squares(image)] == [(200.0, 100.0, 225.0, 125.0)]
    # squares sharing their walls are cut at them
    comb = strip(*[(100 + 24 * k, 100, 124 + 24 * k, 124) for k in range(5)])
    assert len(ocr.check_squares(comb)) == 5


def test_a_comb_reads_its_groups_and_its_last_two_as_cents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 2 squares, a separator, 3, a separator, 2: "60,735.00" drawn a digit a box
    lefts = [100, 124, 156, 180, 204, 236, 260]
    digits = dict(zip(lefts, "6073500", strict=True))
    monkeypatch.setattr(ocr, "_digit", lambda _p, _i, sq: digits[int(sq[0])])
    squares = [(float(x), 100.0, x + 24.0, 124.0, True) for x in lefts]
    stray = (600.0, 100.0, 624.0, 124.0, True)
    words, rest = ocr.combs(strip(), [*squares, stray])
    assert [w["text"] for w in words] == ["60735.00"]
    assert (words[0]["x0"], words[0]["x1"]) == (100, 284)
    # the comb's squares read as no check box; a square alone keeps its mark
    assert sorted(m for *_box, m in rest) == [False] * 7 + [True]


@pytest.mark.parametrize(
    ("read", "want"),
    [([("O", 0.5)], "0"), ([("7", 0.2)], ""), ([("x", 0.9), ("4", 0.4)], "4")],
)
def test_a_comb_square_takes_the_surer_digit_read(
    monkeypatch: pytest.MonkeyPatch, read: list[tuple[str, float]], want: str
) -> None:
    from PIL import ImageDraw

    image = strip((100, 100, 140, 140))
    ImageDraw.Draw(image).line((120, 108, 120, 132), fill="black", width=3)
    monkeypatch.setattr(ocr, "_engine", lambda: lambda *_a, **_k: (read, 0.0))
    assert ocr._digit(image, ocr._ink(image), (100, 100, 140, 140)) == want
    assert ocr._digit(strip(), ocr._ink(strip()), (100, 100, 140, 140)) == ""


@pytest.mark.parametrize(
    ("line", "want"),
    [
        ("13\n14 Total payments", None),  # the next line's number, not an amount
        ("13\n300 on Line 21c", "300"),
        ("13 76Total payments", None),
    ],
)
def test_a_number_opening_a_line_before_a_capital_is_that_lines(
    line: str, want: str | None
) -> None:
    m = re.compile(r"13\s+" + pdf.AMOUNT).search(line)
    assert (m.group(1) if m else None) == want


SYNTHETIC = r"""
form: T-9
years: [2025]
match: ["Form T-9 Synthetic Return"]
year_pattern: '(20\d\d) Form T-9'
issuer: T
return: true
boxes:
  "23":
    label: Total tax
    pattern: >-
      \b23\. Total Tax\. Add Lines 19, 20, 21, and 22[\s.]*23\.?\s+AMOUNT
"""


def synthetic(tmp_path: Path) -> list[pdf.Template]:
    (tmp_path / "t-9.yaml").write_text(SYNTHETIC, encoding="utf-8")
    return pdf.load_templates(tmp_path)


def test_a_scan_matches_a_form_through_letters_the_engine_confuses(
    tmp_path: Path,
) -> None:
    (tpl,) = synthetic(tmp_path)
    page = "2025 Forrn T-9 Synthetic Returm"  # "m" read as "rn", "n" as "m"
    assert tpl.matches(page, ocr=True) and not tpl.matches(page)


def test_a_loosened_pattern_reads_a_comma_as_a_point_but_not_in_a_class() -> None:
    assert pdf._loose_source(r"a, b[,;]{1,3}\, c") == r"a[,.] b[,;]{1,3}\, c"
    assert re.search(pdf.CENTS_AT_END + r"\S+", "4,189\n")
    assert not re.search(pdf.CENTS_AT_END, "41\n")


def test_a_misread_label_is_put_back_from_the_templates_own_wording(
    tmp_path: Path,
) -> None:
    templates = synthetic(tmp_path)
    page = (
        "2025 Form T-9 Synthetic Return\n23.TotalTax.AddLines19,2O,2l,and22 23 2,618.00"
    )
    (got,) = pdf.parse_texts([page], templates, ocr=True)
    assert got.boxes == {"23": ("Total tax", 2618.0)}
