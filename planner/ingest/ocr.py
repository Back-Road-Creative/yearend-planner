"""OCR fallback for photos and scanned pages.

A value read by OCR is never accepted on its own: it lands in the ledger as a
*pending* fact and waits for ``planner confirm``, where it can be corrected box
by box before it counts. The engine (rapidocr-onnxruntime) runs offline with
the models shipped in its wheel; nothing is uploaded.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

from planner.ingest.pdf import Unmatched, normalize

PageTexts = Callable[[Path], Sequence[str]]
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
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


def lines_from(result: list[list[Any]] | None) -> str:
    """Reading order from the engine's boxes: boxes whose tops sit within half a
    line height of each other form one line, left to right."""
    items: list[tuple[float, float, float, str]] = []
    for box, text, _score in result or []:
        ys = [float(p[1]) for p in box]
        xs = [float(p[0]) for p in box]
        items.append((min(ys), max(ys) - min(ys), min(xs), str(text)))
    items.sort()
    lines: list[str] = []
    cur: list[tuple[float, str]] = []
    top = height = 0.0
    for y, h, x, text in items:
        if cur and y - top < max(height, h) / 2:
            cur.append((x, text))
            continue
        if cur:
            lines.append(" ".join(t for _, t in sorted(cur)))
        cur, top, height = [(x, text)], y, h
    if cur:
        lines.append(" ".join(t for _, t in sorted(cur)))
    return normalize("\n".join(lines))


def _read(image: Any) -> str:
    import numpy as np

    result, _elapsed = _engine()(np.asarray(image.convert("RGB")))
    return lines_from(result)


class ScannedPages(Sequence[str]):
    """A PDF's pages, each read by OCR the first time it is asked for. A PDF with
    a few scanned pages among text pages costs one read per scanned page, not
    one per page."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._done: dict[int, str] = {}
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
            import pdfplumber

            with pdfplumber.open(self.path) as pdf:
                return _read(pdf.pages[index].to_image(resolution=200).original)
        except Exception as exc:
            raise Unmatched(
                f"OCR could not read the file ({type(exc).__name__})"
            ) from exc


def page_texts(path: Path) -> Sequence[str]:
    """One OCR text per page for an image file or a scanned PDF. A PDF's pages
    are read when asked for, so a caller that needs only some pays only for them."""
    if not available():
        raise Unmatched(NOT_INSTALLED)
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        return ScannedPages(path)
    try:
        from PIL import Image

        with Image.open(path) as im:
            return [_read(im)]
    except Unmatched:
        raise
    except Exception as exc:  # a bad image; the reason goes to UNMATCHED
        raise Unmatched(f"OCR could not read the file ({type(exc).__name__})") from exc
