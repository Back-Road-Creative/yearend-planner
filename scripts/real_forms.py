"""Write the real-layout fixtures: official blank IRS forms, downloaded from
irs.gov and checked against the SHA-256 in the manifest, with synthetic values
printed into their fields. Each fixture is one recipient-copy page. The
values are page text (Helvetica drawn at each field's box) rather than form
field values, because a printed or downloaded form carries its figures that
way; the fields themselves are dropped. Synthetic values only: names say
"(synthetic)" and every TIN and account field stays blank.

    uv run --extra dev python scripts/real_forms.py [--cache DIR] [NAME ...]
    uv run --extra dev python scripts/real_forms.py --scan DIR [NAME ...]

The manifest is tests/fixtures/real/forms.yaml; the PDFs land beside it.
``--scan`` writes each fixture to DIR as a scanner would, an image-only PDF,
for trying the OCR reader by hand; the tests make the same scans themselves.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path
from typing import Any

from planner.config import safe_load

ROOT = Path(__file__).resolve().parent.parent
REAL = ROOT / "tests" / "fixtures" / "real"
CACHE = Path.home() / ".cache" / "yearend-planner" / "irs-blanks"
SIZE = 9.0  # the largest point size a value is printed at
# A scan as an office scanner makes one: greyscale, a little crooked, speckled.
SCAN_DPI = 150
SCAN_TURN = 0.4  # degrees
SCAN_SPECKLE = 12.0  # standard deviation of the noise, out of 255


def fields_on(page: Any) -> dict[str, Any]:
    """Each widget on the page by its full field name (parent names first)."""
    out: dict[str, Any] = {}
    for ref in page.get("/Annots", []):
        widget = ref.get_object()
        if widget.get("/Subtype") != "/Widget":
            continue
        parts: list[str] = []
        node = widget
        while node is not None:
            if node.get("/T") is not None:
                parts.append(str(node["/T"]))
            parent = node.get("/Parent")
            node = parent.get_object() if parent is not None else None
        out[".".join(reversed(parts))] = widget
    return out


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _ops(rect: list[float], value: str) -> list[str]:
    """Text operators that print ``value`` inside ``rect``: "X" centred in a
    check box, other values left-aligned, one line per newline from the top."""
    x0, y0, x1, y1 = rect
    if value == "X":
        size = min(y1 - y0, x1 - x0) * 0.8
        x = x0 + (x1 - x0 - size * 0.6) / 2
        y = y0 + (y1 - y0 - size * 0.7) / 2
        return [f"BT /PX {size:.1f} Tf 1 0 0 1 {x:.1f} {y:.1f} Tm (X) Tj ET"]
    lines = value.split("\n")
    size = min(SIZE, (y1 - y0) * 0.75 / len(lines))
    if len(lines) == 1:
        top = y0 + (y1 - y0 - size * 0.7) / 2
    else:
        top = y1 - size - 1
    return [
        f"BT /PX {size:.1f} Tf 1 0 0 1 {x0 + 2:.1f} {top - n * size * 1.15:.1f} Tm "
        f"({_escape(line)}) Tj ET"
        for n, line in enumerate(lines)
    ]


def fill(blank: Path, page_no: int, values: dict[str, str], out: Path) -> Path:
    """One page of ``blank`` with each value printed in the field whose full
    name ends with its key; a key must name exactly one field."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import (
        ArrayObject,
        DecodedStreamObject,
        DictionaryObject,
        NameObject,
    )

    page = PdfReader(blank).pages[page_no]
    found = fields_on(page)
    ops: list[str] = []
    for key, value in values.items():
        hits = [name for name in found if name.endswith(key)]
        if len(hits) != 1:
            raise ValueError(f"{blank.name}: field {key!r} matches {hits}")
        ops += _ops([float(v) for v in found[hits[0]]["/Rect"]], str(value))
    writer = PdfWriter()
    writer.add_page(page)
    copy = writer.pages[0]
    if "/Annots" in copy:
        del copy["/Annots"]
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
            NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
        }
    )
    resources = copy["/Resources"].get_object()
    if "/Font" not in resources:
        resources[NameObject("/Font")] = DictionaryObject()
    resources["/Font"].get_object()[NameObject("/PX")] = writer._add_object(font)
    stream = DecodedStreamObject()
    stream.set_data(("q 0 g\n" + "\n".join(ops) + "\nQ\n").encode("cp1252"))
    contents = copy.get("/Contents")
    joined = ArrayObject()
    if isinstance(contents.get_object(), ArrayObject):
        joined.extend(contents.get_object())
    else:
        joined.append(contents)
    joined.append(writer._add_object(stream))
    copy[NameObject("/Contents")] = joined
    writer.compress_identical_objects()
    with out.open("wb") as fh:
        writer.write(fh)
    return out


def scan(src: Path, out: Path, seed: int = 0, turn: float = SCAN_TURN) -> Path:
    """An image-only PDF of ``src``'s first page as a scanner makes it:
    greyscale at ``SCAN_DPI``, turned ``turn`` degrees, with speckle."""
    import numpy as np
    import pdfplumber
    from PIL import Image

    with pdfplumber.open(src) as doc:
        image = doc.pages[0].to_image(resolution=SCAN_DPI).original.convert("L")
    image = image.rotate(turn, Image.Resampling.BICUBIC, fillcolor=255)
    noise = np.random.default_rng(seed).normal(0, SCAN_SPECKLE, image.size[::-1])
    pixels = np.clip(np.asarray(image, dtype=float) + noise, 0, 255)
    Image.fromarray(pixels.astype(np.uint8)).save(out, "PDF", resolution=SCAN_DPI)
    return out


def blank(entry: dict[str, Any], cache: Path) -> Path:
    """The entry's official blank, downloaded once; its hash must match."""
    url = str(entry["url"])
    if not url.startswith("https://www.irs.gov/"):
        raise ValueError(f"{entry['name']}: blanks come from irs.gov only")
    path = cache / url.rsplit("/", 1)[1]
    if not path.exists():
        cache.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310
            path.write_bytes(resp.read())
    got = hashlib.sha256(path.read_bytes()).hexdigest()
    if got != entry["sha256"]:
        raise ValueError(
            f"{entry['name']}: {path.name} is sha256 {got}, the manifest says "
            f"{entry['sha256']}; the IRS changed the form, so re-check its layout"
        )
    return path


def manifest() -> list[dict[str, Any]]:
    with (REAL / "forms.yaml").open(encoding="utf-8") as fh:
        forms: list[dict[str, Any]] = safe_load(fh)["forms"]
    return forms


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cache", type=Path, default=CACHE)
    ap.add_argument("--scan", type=Path, help="write scans of the fixtures here")
    ap.add_argument("names", nargs="*")
    args = ap.parse_args(argv)
    for entry in manifest():
        if args.names and entry["name"] not in args.names:
            continue
        out = REAL / f"{entry['name']}.pdf"
        if args.scan:
            args.scan.mkdir(parents=True, exist_ok=True)
            print(scan(out, args.scan / out.name))
            continue
        print(fill(blank(entry, args.cache), int(entry["page"]), entry["fields"], out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
