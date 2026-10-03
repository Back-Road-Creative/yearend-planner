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
from typing import Any

from planner.ingest.pdf import ParsedForm, Unmatched, normalize
from planner.paths import Layout

PageTexts = Callable[[Path], Sequence[str]]
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
PAGE_DPI = 200
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

    def __new__(cls, text: str, lines: Sequence[Line]) -> Read:
        obj = super().__new__(cls, text)
        obj.lines = tuple(lines)
        return obj


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


def _read(image: Any) -> str:
    import numpy as np

    result, _elapsed = _engine()(np.asarray(image.convert("RGB")))
    lines = layout(result)
    return Read(normalize("\n".join(line.text for line in lines)), lines)


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
            with self._image(page - 1) as image:
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
