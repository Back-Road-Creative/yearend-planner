"""OCR fallback for photos and scanned pages.

A value read by OCR is never accepted on its own: it lands in the ledger as a
*pending* fact and waits for ``planner confirm``, where it can be corrected box
by box before it counts. The engine (rapidocr-onnxruntime) runs offline with
the models shipped in its wheel; nothing is uploaded.
"""

from __future__ import annotations

import io
import shutil
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

from planner.ingest.pdf import (
    CHECK_SIDE,
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
# The engine's mean confidence below which a page may be upside down: upright
# forms read at 0.95 or more, the same pages turned over at 0.7 to 0.89.
UPRIGHT_SCORE = 0.9
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


def check_squares(image: Any) -> list[tuple[float, float, float, float, bool]]:
    """The check boxes of a scanned form, ``(left, top, right, bottom,
    marked)`` in pixels: small square outlines, marked when ink crosses the
    middle. The engine reads a mark as a stray glyph or not at all."""
    import cv2

    ink = _ink(image)
    pt = ink.shape[1] / 612  # pixels per point, from a letter page's width
    found: list[tuple[int, int, int, int]] = []
    contours, _tree = cv2.findContours(ink, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if (
            6 * pt <= min(w, h)
            and max(w, h) <= 1.5 * CHECK_SIDE * pt
            and cv2.contourArea(c) > 0.85 * w * h
        ):
            found.append((x, y, x + w, y + h))
    squares = []
    for x0, y0, x1, y1 in found:
        if any(
            (a, b, c, d) != (x0, y0, x1, y1)
            and a <= x0
            and b <= y0
            and x1 <= c
            and y1 <= d
            for a, b, c, d in found
        ):
            continue  # the inside edge of a square already found
        inset = max(2, (x1 - x0) // 4)
        middle = ink[y0 + inset : y1 - inset, x0 + inset : x1 - inset]
        squares.append((float(x0), float(y0), float(x1), float(y1), middle.mean() > 20))
    return squares


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
    word, and each marked square reads ``[X]``."""
    out = []
    for w in words:
        cx, cy = (w["x0"] + w["x1"]) / 2, (w["top"] + w["bottom"]) / 2
        text = w["text"].strip(BOX_GLYPHS)
        if not text or any(a <= cx <= c and b <= cy <= d for a, b, c, d, _m in squares):
            continue
        out.append({**w, "text": text})
    out += [_word("[X]", a, c, b, d) for a, b, c, d, m in squares if m]
    return out


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
        np.asarray(image.convert("RGB")), use_cls=False, return_word_box=True
    )
    return result or [], sum(float(r[2]) for r in result or [])


def _read(image: Any) -> str:
    result, total = _ocr(image)
    turned = False
    if total < UPRIGHT_SCORE * len(result):
        over = image.rotate(180)
        again, more = _ocr(over)
        if more > total:
            image, result, turned = over, again, True
    ruled = grid_cells(image)
    words = marked(words_of(result, ruled), check_squares(image))
    lines = layout(
        [[[[w["x0"], w["top"]], [w["x1"], w["bottom"]]], w["text"], 1.0] for w in words]
    )
    plain = Read(normalize("\n".join(line.text for line in lines)), lines)
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
    grid = Grid(normalize("\n".join(t for t, _ in cells)), plain, boxes)
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
