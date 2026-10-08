"""OCR fallback for photos and scanned pages.

A value read by OCR is never accepted on its own: it lands in the ledger as a
*pending* fact and waits for ``planner confirm``, where it can be corrected box
by box before it counts. The engine (rapidocr-onnxruntime) runs offline with
the models shipped in its wheel; nothing is uploaded.
"""

from __future__ import annotations

import io
import re
import shutil
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

from planner.ingest.pdf import (
    CHECK_SIDE,
    FIGURE,
    NEAR,
    ParsedForm,
    Ruled,
    Unmatched,
    grid_lines,
    normalize,
)
from planner.paths import Layout

PageTexts = Callable[[Path], Sequence[str]]
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
PAGE_DPI = 200
# A figure whose thousands commas the engine read as points ("5.000.00").
POINTED = re.compile(r"\d{1,3}(?:\.\d{3})+\.\d\d")
# A figure with its cents read, and the return's preprinted cents after them
# ("2,850.00.00").
CENTS_TWICE = re.compile(r"[\d.,]*\d\.\d\d\.00")
# The engine's mean confidence below which a page may be upside down: upright
# forms read at 0.95 or more, the same pages turned over at 0.7 to 0.89.
UPRIGHT_SCORE = 0.9
# How sure the engine must be that a spot holds text before it reads it: below
# its default (0.5), as a lone letter in a box (a 1099-DA's "J") scores near 0.5.
BOX_THRESH = 0.4
# A word in capitals with an "l" the engine read for an "I" ("MlCHlGAN",
# "Ml-1040", "(VAGl)"), taken whole: "WallSt" has none.
CAPITAL_L = re.compile(r"(?<![A-Z])(?=[A-Zl]*[A-Z])(?=[A-Zl]*l)[A-Zl]{2,}+(?![a-z])")
# A zero among capitals, read for an "O" ("REFUND0RN0", "B0X", "SACRAMENT0"):
# after a capital and before no digit, or opening a word of capitals.
CAPITAL_O = re.compile(r"(?<=[A-Z])0(?!\d)|(?<![\w$])0(?=[A-Z]{2})")


def capitals(text: str) -> str:
    """``text`` with each capital "I" the engine read as "l", and each capital
    "O" read as "0", put back."""
    text = CAPITAL_L.sub(lambda m: m.group().replace("l", "I"), text)
    return CAPITAL_O.sub("O", text)


NOT_INSTALLED = "OCR engine (rapidocr-onnxruntime) is not installed"


def available() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True


@lru_cache(maxsize=1)
def _engine() -> Any:
    from rapidocr_onnxruntime import RapidOCR

    return RapidOCR()


@dataclass(frozen=True)
class Line:
    """One reading-order line of a page and where it sits in the image, in
    pixels: ``(left, top, right, bottom)`` around all of the line's boxes."""

    text: str
    box: tuple[float, float, float, float]


class Read(str):
    """A page's text that also remembers where each of its lines was read. The
    text is what the parser sees; the lines are what the confirm crop is cut
    from. A plain ``str`` (a test double, a text layer) has no lines."""

    lines: tuple[Line, ...] = ()
    turned = False  # read upside down: the lines are placed in the turned page

    def __new__(cls, text: str, lines: Sequence[Line]) -> Read:
        obj = super().__new__(cls, text)
        obj.lines = tuple(lines)
        return obj


class Grid(Ruled):
    """A scanned grid of boxes read one box at a time (``pdf.grid_lines``),
    with the page's plain OCR text as ``plain``. ``lines`` holds where each of
    the cells' lines was read and then the plain text's, as the parser counts
    them."""

    lines: tuple[Line, ...] = ()
    turned = False

    def __new__(cls, text: str, plain: Read, lines: Sequence[Line]) -> Grid:
        obj = cast(Grid, super().__new__(cls, text, plain))
        obj.lines = tuple(lines) + plain.lines
        return obj


def grid_cells(image: Any) -> list[tuple[float, float, float, float]]:
    """The ruled boxes of a scanned form, ``(left, top, right, bottom)`` in
    pixels: the white areas its long rules close off, leaving out the page
    around the form and the check-box squares inside a box."""
    import cv2
    import numpy as np

    ink = _ink(image)
    high, wide = ink.shape
    rules = cv2.bitwise_or(
        cv2.morphologyEx(
            ink,
            cv2.MORPH_OPEN,
            cv2.getStructuringElement(cv2.MORPH_RECT, (wide // 30, 1)),
        ),
        cv2.morphologyEx(
            ink,
            cv2.MORPH_OPEN,
            cv2.getStructuringElement(cv2.MORPH_RECT, (1, high // 60)),
        ),
    )
    rules = cv2.dilate(rules, np.ones((3, 3), np.uint8))
    _count, _labels, stats, _centres = cv2.connectedComponentsWithStats(
        255 - rules, connectivity=4
    )
    side = CHECK_SIDE * wide / 612  # a check box's side, scaled from a letter page
    return [
        (float(x), float(y), float(x + w), float(y + h))
        for x, y, w, h, _area in stats[1:]
        if x > 0 and y > 0 and x + w < wide and y + h < high and max(w, h) >= side
    ]


def _ink(image: Any) -> Any:
    """The page's ink as white on black, evened out across uneven lighting."""
    import cv2
    import numpy as np

    gray = np.asarray(image.convert("L"))
    return cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 25, 15
    )


# What the engine reads a check box as, empty or marked.
BOX_GLYPHS = "口区回☐☑☒□■"
# A return's line number ("46", "4b"), boxed: the square is its cell.
LINE_NUMBER = re.compile(r"\d{2,}|\d{1,2}[a-z]")
# What the engine reads a check box's edge as, run onto the word beside it
# ("loss]").
EDGE_GLYPHS = "[]|" + BOX_GLYPHS


def check_squares(image: Any) -> list[tuple[float, float, float, float, bool]]:
    """The check boxes of a scanned form, ``(left, top, right, bottom,
    marked)`` in pixels: small square outlines, marked when ink crosses the
    middle. The engine reads a mark as a stray glyph or not at all. A digit
    box (a comb, "[ | | ]") may be larger than a check box, and squares that
    share their walls outline as one box, which is cut at its walls. A box
    taller than it is wide (a return's preprinted cents, "00") is no square."""
    import cv2

    ink = _ink(image)
    pt = ink.shape[1] / 612  # pixels per point, from a letter page's width
    found: list[tuple[int, int, int, int, int]] = []  # and how many squares across
    contours, _tree = cv2.findContours(ink, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if not (
            6 * pt <= h <= 2 * CHECK_SIDE * pt
            and 6 * pt <= w
            and cv2.contourArea(c) > 0.85 * w * h
        ):
            continue
        across = max(1, round(w / h))
        walls = [x + w * k // across for k in range(1, across)]
        if (
            abs(w - across * h)
            > (0.15 if max(w, h) > 1.5 * CHECK_SIDE * pt else 0.25) * h
        ):
            continue  # not whole squares: a cell
        if max(w, h) > 1.5 * CHECK_SIDE * pt and not all(
            ink[y + h // 4 : y + 3 * h // 4, max(at - 3, 0) : at + 4].mean(axis=0).max()
            > 180
            for at in walls
        ):
            continue  # a wide box with no walls inside: a field, not squares
        found.append((x, y, x + w, y + h, across))
    squares = []
    for x0, y0, x1, y1, across in found:
        if any(
            (a, b, c, d) != (x0, y0, x1, y1)
            and a <= x0
            and b <= y0
            and x1 <= c
            and y1 <= d
            for a, b, c, d, _n in found
        ):
            continue  # the inside edge of a square already found
        for k in range(across):
            left, right = (
                x0 + (x1 - x0) * k // across,
                x0 + (x1 - x0) * (k + 1) // across,
            )
            inset = max(2, (right - left) // 4)
            middle = ink[y0 + inset : y1 - inset, left + inset : right - inset]
            squares.append(
                (float(left), float(y0), float(right), float(y1), middle.mean() > 20)
            )
    return squares


def cents_boxes(image: Any) -> list[tuple[float, float, float, float]]:
    """A return's preprinted cents boxes ("|00|" after each amount box, as on
    the CA 540), ``(left, top, right, bottom)`` in pixels: outlines as tall as
    a check box and narrower than they are tall."""
    import cv2

    ink = _ink(image)
    pt = ink.shape[1] / 612
    contours, _tree = cv2.findContours(ink, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if (
            CHECK_SIDE * pt <= h <= 1.5 * CHECK_SIDE * pt
            and 0.45 * h <= w <= 0.8 * h
            and cv2.contourArea(c) > 0.85 * w * h
        ):
            out.append((float(x), float(y), float(x + w), float(y + h)))
    return out


# The cents box's "00" as the engine reads it with a wall: "100", "[00", "l00]".
WALLED_CENTS = re.compile(r"[\[(|1lI]?\.?00[\])|1lI]?")


def uncented(
    words: list[dict[str, Any]], boxes: Sequence[tuple[float, float, float, float]]
) -> list[dict[str, Any]]:
    """The words with each read of a preprinted cents box put back as "00": its
    wall read as a "1" would make an empty amount box read 100."""
    out = []
    for w in words:
        mid = (w["x0"] + w["x1"]) / 2, (w["top"] + w["bottom"]) / 2
        if WALLED_CENTS.fullmatch(w["text"]) and any(
            a <= mid[0] <= c and b <= mid[1] <= d for a, b, c, d in boxes
        ):
            w = dict(w, text="00")
        out.append(w)
    return out


def _cell(cells: Sequence[Sequence[float]], x: float, y: float) -> int:
    """The smallest of ``cells`` around a point, or -1."""
    best, area = -1, float("inf")
    for n, (x0, y0, x1, y1) in enumerate(cells):
        if x0 <= x <= x1 and y0 <= y <= y1 and (x1 - x0) * (y1 - y0) < area:
            best, area = n, (x1 - x0) * (y1 - y0)
    return best


def _word(text: str, x0: float, x1: float, top: float, bottom: float) -> dict[str, Any]:
    return {"text": text, "x0": x0, "x1": x1, "top": top, "bottom": bottom}


def words_of(
    result: list[list[Any]] | None, cells: Sequence[Sequence[float]]
) -> list[dict[str, Any]]:
    """The engine's boxes as words. The engine reads across a form's rules
    ("11b State identification no.12 State income tax withheld" is two boxes'
    labels), so a box is cut where its letters (the engine reports where each
    one sits) cross from one of ``cells`` into another, and at each space. A
    box without letter places is one word."""
    words: list[dict[str, Any]] = []
    for box, text, _score, *letters in result or []:
        xs = [float(p[0]) for p in box]
        top, bottom = min(float(p[1]) for p in box), max(float(p[1]) for p in box)
        spots, chars = (letters + [[], []])[:2]
        if not spots or len(spots) != len(chars):
            words.append(_word(str(text), min(xs), max(xs), top, bottom))
            continue
        runs: list[list[tuple[str, float, float]]] = [[]]
        where = -1
        for spot, ch in zip(spots, chars, strict=True):
            x0, x1 = min(float(p[0]) for p in spot), max(float(p[0]) for p in spot)
            here = _cell(cells, (x0 + x1) / 2, (top + bottom) / 2)
            if ch.isspace() or here != where:
                runs.append([])
            if not ch.isspace():
                runs[-1].append((ch, x0, x1))
                where = here
        words += [
            _word("".join(c for c, _a, _b in r), r[0][1], r[-1][2], top, bottom)
            for r in runs
            if r
        ]
    return words


def marked(
    words: list[dict[str, Any]],
    squares: Sequence[tuple[float, float, float, float, bool]],
) -> list[dict[str, Any]]:
    """The words with the check boxes read from the image instead: a word
    inside a square goes, a glyph the engine read for a square comes off its
    word, and each marked square reads ``[X]``. A square the engine read a
    number of two or more digits in (a return's line number, boxed: "46") is
    the number's cell, not a check box. A word running into a square loses
    the wall it read there."""

    def inside(w: dict[str, Any], square: Sequence[float]) -> bool:
        cx, cy = (w["x0"] + w["x1"]) / 2, (w["top"] + w["bottom"]) / 2
        return bool(square[0] <= cx <= square[2] and square[1] <= cy <= square[3])

    squares = [
        q
        for q in squares
        if not any(
            LINE_NUMBER.fullmatch(w["text"].strip()) and inside(w, q) for w in words
        )
    ]
    out = []
    for w in words:
        cx, cy = (w["x0"] + w["x1"]) / 2, (w["top"] + w["bottom"]) / 2
        text = w["text"].strip(BOX_GLYPHS)
        if any(a <= cx <= c and b <= cy <= d for a, b, c, d, _m in squares):
            continue
        for a, b, c, d, _m in squares:
            if w["top"] < d and b < w["bottom"]:
                # the square's edge read as a bracket on a word reaching into it
                if w["x0"] < a < w["x1"] or (
                    a < w["x1"] <= c + (c - a) / 2 and b <= cy <= d
                ):
                    text = text.rstrip(EDGE_GLYPHS)
                if w["x0"] < c < w["x1"]:
                    text = text.lstrip(EDGE_GLYPHS)
        if not text:
            continue
        out.append({**w, "text": text})
    out += [_word("[X]", a, c, b, d) for a, b, c, d, m in squares if m]
    return out


# What a comb square's one digit is read as when a stroke touches its walls.
ZERO_LIKE = str.maketrans("DOoQ", "0000")


def combs(
    image: Any, squares: Sequence[tuple[float, float, float, float, bool]]
) -> tuple[list[dict[str, Any]], list[tuple[float, float, float, float, bool]]]:
    """The amounts written one digit to a square (a comb: three or more squares
    side by side) as words, and the squares with the combs' unmarked, so they
    read as no check box. The engine finds a lone digit in a square unreliably,
    so the ink inside each square is recognized alone, cropped close (a thin
    "1" inks too little to count as a check mark). The squares come in groups set
    apart by the printed separators; a last group of two is the cents. A
    square found by its inside edge is grown to the comb's outside first, so
    the squares of a group touch."""
    ink, page = _ink(image), image.convert("RGB")
    rows: list[list[tuple[float, float, float, float, bool]]] = []
    for sq in sorted(squares):  # left to right: a crooked scan staggers the tops
        side = sq[3] - sq[1]
        row = next(
            (
                r
                for r in rows
                if abs(r[-1][1] - sq[1]) < side / 2
                and 0 <= sq[0] - r[-1][2] < 0.6 * side
            ),
            None,
        )
        if row is None:
            rows.append([sq])
        else:
            row.append(sq)
    words, rest = [], []
    for row in rows:
        if len(row) < 3:
            rest += row
            continue
        rest += [(a, b, c, d, False) for a, b, c, d, _m in row]
        side = max(q[3] - q[1] for q in row)
        grown = [
            (q[0] - (side - q[3] + q[1]) / 2, q[2] + (side - q[3] + q[1]) / 2)
            for q in row
        ]
        groups: list[list[str]] = [[]]
        for n, (a, b, c, d, _inked) in enumerate(row):
            if n and grown[n][0] - grown[n - 1][1] > 0.06 * side:
                groups.append([])
            groups[-1].append(_digit(page, ink, (a, b, c, d)))
        cents = (
            "".join(groups.pop()) if len(groups) > 1 and len(groups[-1]) == 2 else ""
        )
        whole = "".join("".join(g) for g in groups)
        text = whole + ("." + cents if cents else "")
        if whole or cents:
            top, bottom = min(q[1] for q in row), max(q[3] for q in row)
            words.append(_word(text, row[0][0], row[-1][2], top, bottom))
    return words, rest


# How sure the recognizer must be of a comb square's digit (it is least sure of
# a "0").
DIGIT_SCORE = 0.3


def _digit(page: Any, ink: Any, square: tuple[float, float, float, float]) -> str:
    """The digit written in a comb square, or "" for none: read from the
    square inside its walls and from its ink cropped close, the surer of the
    two."""
    import numpy as np

    a, b, c, d = (int(v) for v in square)
    inset = max(2, (c - a) // 6)  # clear of the walls
    x0, y0, x1, y1 = a + inset, b + inset, c - inset, d - inset
    ys, xs = np.nonzero(ink[y0:y1, x0:x1])
    if not len(xs):
        return ""
    margin = 3
    close = (
        x0 + max(int(xs.min()) - margin, 0),
        y0 + max(int(ys.min()) - margin, 0),
        x0 + min(int(xs.max()) + margin + 1, x1 - x0),
        y0 + min(int(ys.max()) + margin + 1, y1 - y0),
    )
    best, sure = "", DIGIT_SCORE
    for box in ((x0, y0, x1, y1), close):
        read, _elapsed = _engine()(
            np.asarray(page.crop(box)), use_det=False, use_cls=False
        )
        for text, score in read or []:
            digits = [ch for ch in str(text).translate(ZERO_LIKE) if ch.isdigit()]
            if digits and float(score) >= sure:
                best, sure = digits[0], float(score)
    return best


def layout(result: list[list[Any]] | None) -> list[Line]:
    """Reading order from the engine's boxes: boxes whose tops sit within half a
    line height of each other form one line, left to right."""
    items: list[tuple[float, float, float, float, float, str]] = []
    for box, text, _score in result or []:
        ys = [float(p[1]) for p in box]
        xs = [float(p[0]) for p in box]
        items.append((min(ys), max(ys) - min(ys), min(xs), max(xs), max(ys), str(text)))
    items.sort(key=lambda i: (i[0], i[1], i[2]))
    lines: list[Line] = []
    cur: list[tuple[float, float, float, float, str]] = []  # x0, x1, y0, y1, text
    top = height = 0.0

    def close() -> None:
        ordered = sorted(cur, key=lambda c: (c[0], c[4]))
        lines.append(
            Line(
                " ".join(c[4] for c in ordered),
                (
                    min(c[0] for c in cur),
                    min(c[2] for c in cur),
                    max(c[1] for c in cur),
                    max(c[3] for c in cur),
                ),
            )
        )

    for y, h, x0, x1, y1, text in items:
        if cur and y - top < max(height, h) / 2:
            cur.append((x0, x1, y, y1, text))
            continue
        if cur:
            close()
        cur, top, height = [(x0, x1, y, y1, text)], y, h
    if cur:
        close()
    return lines


def lines_from(result: list[list[Any]] | None) -> str:
    return normalize("\n".join(line.text for line in layout(result)))


def _ocr(image: Any) -> tuple[list[list[Any]], float]:
    """The engine's boxes for an image, and their total confidence. Each line is
    read the way up the page is: the engine's own per-line turn reads a short
    smudged label upside down ("code" as "apoa")."""
    import numpy as np

    result, _elapsed = _engine()(
        np.asarray(image.convert("RGB")),
        use_cls=False,
        return_word_box=True,
        box_thresh=BOX_THRESH,
    )
    return result or [], sum(float(r[2]) for r in result or [])


# A return's preprinted cents beside its amount box, as a scan reads them.
PRINTED_CENTS = re.compile(r"\[?\.?00\]?")


def _lowered(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The scan's words with each filled amount set level with the word
    nearest it on its left (its line's number, or the label's end), as
    ``pdf._lowered`` does the text layer's. A filled figure sits a little off
    its line, and a section heading on the line above ("Step 2: Income" on the
    IL-1040) would otherwise take it. A number with words close on its right
    is a line's own number, and stays; a return's preprinted cents on its right
    ("00") do not count, and those just beside it move with it. A figure may
    sit up to its own height off its line (CA 540). The reach, in points
    there, is in pixels here. A figure read with points for its commas gets
    them back, and one read with the printed
    cents after its own loses them."""
    reach = NEAR * PAGE_DPI / 72
    out = [dict(w) for w in words]
    moved: dict[int, float] = {}  # a figure's printed cents, and how far it moved
    for f in out:
        height = f["bottom"] - f["top"]
        mid = (f["top"] + f["bottom"]) / 2

        def off(w: dict[str, Any], mid: float = mid) -> float:
            return float(abs((w["top"] + w["bottom"]) / 2 - mid))

        left = [
            w
            for w in words
            if off(w) <= height and f["x0"] - reach < w["x1"] <= f["x0"]
        ]
        right = [
            k
            for k, w in enumerate(words)
            if off(w) <= height / 2 and f["x1"] <= w["x0"] < f["x1"] + reach
        ]
        cents = [k for k in right if PRINTED_CENTS.fullmatch(words[k]["text"])]
        right = [k for k in right if k not in cents]
        cents = [k for k in cents if words[k]["x0"] - f["x1"] <= 2 * height]
        if CENTS_TWICE.fullmatch(f["text"]):
            f["text"] = f["text"][: -len(".00")]
        if POINTED.fullmatch(f["text"]):
            whole, point = f["text"].rsplit(".", 1)
            f["text"] = whole.replace(".", ",") + "." + point
        printed = PRINTED_CENTS.fullmatch(f["text"])
        if FIGURE.fullmatch(f["text"]) and not printed and left and not right:
            dy = min(left, key=off)["top"] - f["top"]
            f["top"], f["bottom"] = f["top"] + dy, f["bottom"] + dy
            moved.update(dict.fromkeys(cents, dy))
    for k, dy in moved.items():  # the cents go with their figure
        out[k]["top"], out[k]["bottom"] = words[k]["top"] + dy, words[k]["bottom"] + dy
    return out


def _read(image: Any) -> str:
    result, total = _ocr(image)
    turned = False
    if total < UPRIGHT_SCORE * len(result):
        over = image.rotate(180)
        again, more = _ocr(over)
        if more > total:
            image, result, turned = over, again, True
    ruled = grid_cells(image)
    digits, squares = combs(image, check_squares(image))
    words = _lowered(
        uncented(marked(words_of(result, ruled), squares), cents_boxes(image)) + digits
    )
    lines = layout(
        [[[[w["x0"], w["top"]], [w["x1"], w["bottom"]]], w["text"], 1.0] for w in words]
    )
    plain = Read(capitals(normalize("\n".join(line.text for line in lines))), lines)
    plain.turned = turned
    cells = grid_lines(words, ruled)
    if not cells:
        return plain
    boxes = [
        Line(
            text,
            (
                min(w["x0"] for w in row),
                min(w["top"] for w in row),
                max(w["x1"] for w in row),
                max(w["bottom"] for w in row),
            ),
        )
        for text, row in cells
    ]
    grid = Grid(capitals(normalize("\n".join(t for t, _ in cells))), plain, boxes)
    grid.turned = turned
    return grid


class ScannedPages(Sequence[str]):
    """The pages of a scanned PDF or the one page of a photo, each read by OCR
    the first time it is asked for. A PDF with a few scanned pages among text
    pages costs one read per scanned page, not one per page. The pages that were
    read can be cropped to the line a value came from (``crops``)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._done: dict[int, str] = {}
        self._photo = path.suffix.lower() in IMAGE_SUFFIXES
        if self._photo:
            self._count = 1
            return
        try:
            import pdfplumber

            with pdfplumber.open(path) as pdf:
                self._count = len(pdf.pages)
        except Exception as exc:  # a bad PDF; the reason goes to UNMATCHED
            raise Unmatched(
                f"OCR could not read the file ({type(exc).__name__})"
            ) from exc

    def __len__(self) -> int:
        return self._count

    def __iter__(self) -> Iterator[str]:
        return (self[i] for i in range(self._count))

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(self._count))]
        if not -self._count <= index < self._count:
            raise IndexError(index)
        index %= self._count
        if index not in self._done:
            self._done[index] = self._read_page(index)
        return self._done[index]

    def _read_page(self, index: int) -> str:
        try:
            if self._photo:
                from PIL import Image

                with Image.open(self.path) as im:
                    return _read(im)
            import pdfplumber

            with pdfplumber.open(self.path) as pdf:
                return _read(pdf.pages[index].to_image(resolution=PAGE_DPI).original)
        except Exception as exc:
            raise Unmatched(
                f"OCR could not read the file ({type(exc).__name__})"
            ) from exc

    def crops(self, spots: Iterable[tuple[int, int]]) -> dict[tuple[int, int], bytes]:
        """A PNG of each ``(page, line)`` spot (1-based page, 0-based line of the
        text OCR returned for it): the line and a margin of one line around it,
        so the label beside the value shows too. A spot OCR did not locate
        (a page not read, a line past the end) is left out. Each page is
        rendered once, however many spots it holds."""
        by_page: dict[int, dict[int, tuple[float, float, float, float]]] = {}
        for page, line in dict.fromkeys(spots):
            lines: tuple[Line, ...] = getattr(self._done.get(page - 1), "lines", ())
            if 0 <= line < len(lines):
                by_page.setdefault(page, {})[line] = lines[line].box
        out: dict[tuple[int, int], bytes] = {}
        for page, boxes in by_page.items():
            with self._image(page - 1) as rendered:
                turned = getattr(self._done.get(page - 1), "turned", False)
                image = rendered.rotate(180) if turned else rendered
                for line, (left, top, right, bottom) in boxes.items():
                    pad = max(bottom - top, 8.0)
                    region = image.crop(
                        (
                            int(max(left - pad, 0)),
                            int(max(top - pad, 0)),
                            int(min(right + pad, image.width)),
                            int(min(bottom + pad, image.height)),
                        )
                    )
                    buf = io.BytesIO()
                    region.convert("RGB").save(buf, "PNG")
                    out[(page, line)] = buf.getvalue()
        return out

    def _image(self, index: int) -> Any:
        """The page as the engine saw it, to be used as a context manager."""
        from PIL import Image

        if self._photo:
            return Image.open(self.path)
        import pdfplumber

        with pdfplumber.open(self.path) as pdf:
            rendered = pdf.pages[index].to_image(resolution=PAGE_DPI).original
        return rendered


def page_texts(path: Path) -> Sequence[str]:
    """One OCR text per page for an image file or a scanned PDF. A PDF's pages
    are read when asked for, so a caller that needs only some pays only for them."""
    if not available():
        raise Unmatched(NOT_INSTALLED)
    pages = ScannedPages(path)
    if path.suffix.lower() in IMAGE_SUFFIXES:
        _ = pages[0]  # a bad photo is refused here, not when the parser asks
    return pages


def crop_dir(lay: Layout, doc_id: int) -> Path:
    return lay.data / "crops" / str(doc_id)


def crop_path(lay: Layout, doc_id: int, fact_id: int) -> Path:
    return crop_dir(lay, doc_id) / f"{fact_id}.png"


def forget_crops(lay: Layout, doc_id: int) -> None:
    shutil.rmtree(crop_dir(lay, doc_id), ignore_errors=True)


def write_crops(
    lay: Layout,
    conn: sqlite3.Connection,
    doc_id: int,
    forms: Sequence[ParsedForm],
    pages: Sequence[str] | None,
) -> tuple[int, int]:
    """Save, for each value of a document waiting for confirm, the crop of the
    scan it was read from, as ``data/crops/<document>/<fact>.png``. Returns
    ``(written, wanted)``: a value whose line the engine did not locate has no
    crop and the page says so (a reader that returns text only has none to
    make, so wants none). Crops only ever hold what is already in the scan."""
    forget_crops(lay, doc_id)
    ids = {
        (r["form"], r["tax_year"], r["issuer"], r["box"]): int(r["id"])
        for r in conn.execute(
            "SELECT id, form, tax_year, issuer, box FROM facts "
            "WHERE document_id = ? AND status = 'pending'",
            (doc_id,),
        )
    }
    spots = {
        ids[(f.form, f.tax_year, f.issuer, box)]: spot
        for f in forms
        if f.ocr
        for box, spot in f.spots.items()
        if (f.form, f.tax_year, f.issuer, box) in ids
    }
    if not isinstance(pages, ScannedPages):
        return 0, 0  # a reader that gives text only: no image to cut from
    wanted = len(ids)
    if not spots:
        return 0, wanted
    try:
        images = pages.crops(spots.values())
    except Exception:  # a crop is an aid: the value stays pending without it
        return 0, wanted
    written = 0
    for fact_id, spot in spots.items():
        png = images.get(spot)
        if png is None:
            continue
        target = crop_path(lay, doc_id, fact_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(png)
        written += 1
    return written, wanted
