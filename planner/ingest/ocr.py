"""OCR fallback for photos and scanned pages.

A value read by OCR is never accepted on its own: it lands in the ledger as a
*pending* fact and waits for ``planner confirm``, where it can be corrected box
by box before it counts. The engine (rapidocr-onnxruntime) runs offline with
the models shipped in its wheel; nothing is uploaded.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

from planner.ingest.pdf import Unmatched, normalize

PageTexts = Callable[[Path], list[str]]
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


def page_texts(path: Path) -> list[str]:
    """One OCR text per page for an image file or a scanned PDF."""
    if not available():
        raise Unmatched(NOT_INSTALLED)
    try:
        if path.suffix.lower() in IMAGE_SUFFIXES:
            from PIL import Image

            with Image.open(path) as im:
                return [_read(im)]
        import pdfplumber

        with pdfplumber.open(path) as pdf:
            return [_read(page.to_image(resolution=200).original) for page in pdf.pages]
    except Unmatched:
        raise
    except Exception as exc:  # a bad image or PDF; the reason goes to UNMATCHED
        raise Unmatched(f"OCR could not read the file ({type(exc).__name__})") from exc
