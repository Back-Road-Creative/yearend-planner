"""Write the real-layout fixtures: official blank IRS and state forms,
downloaded from the IRS or the state's revenue department and checked against
the SHA-256 in the manifest, with synthetic values printed into their fields.
Each fixture is one recipient-copy page, or a return's pages. The
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
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from planner.config import safe_load

ROOT = Path(__file__).resolve().parent.parent
REAL = ROOT / "tests" / "fixtures" / "real"
CACHE = Path.home() / ".cache" / "yearend-planner" / "blanks"
# Where an official blank may come from: the IRS and the revenue department of
# each state whose return is drafted.
BLANK_HOSTS = frozenset(
    {
        "www.irs.gov",
        "www.ftb.ca.gov",
        "apps.dor.ga.gov",
        "tax.illinois.gov",
        "www.michigan.gov",
        "www.ncdor.gov",
        "www.nj.gov",
        "www.tax.ny.gov",
        "dam.assets.ohio.gov",
        "www.pa.gov",
        "www.tax.virginia.gov",
    }
)
# Michigan's site refuses a download that names no browser (403).
AGENT = "Mozilla/5.0 (compatible; yearend-planner)"
SIZE = 9.0  # the largest point size a value is printed at
COMB_SIDE = 20.0  # the widest box that may be one box of a comb
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


def _width(text: str, size: float) -> float:
    from pdfminer.fontmetrics import FONT_METRICS

    widths = FONT_METRICS["Helvetica"][1]
    return sum(widths.get(c, 556) for c in text) * size / 1000


def _ops(rect: list[float], value: str, quad: int = 0) -> list[str]:
    """Text operators that print ``value`` inside ``rect``: "X" centred in a
    check box, other values one line per newline from the top, aligned as the
    field's ``quad`` says (0 left, 1 centred, 2 right), as a viewer prints it:
    the IL-1040 sets its amounts right, clear of the line number at the left."""
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

    def left(line: str) -> float:
        room = x1 - x0 - 4 - _width(line, size)
        return x0 + 2 + (room if quad == 2 else room / 2 if quad == 1 else 0)

    return [
        f"BT /PX {size:.1f} Tf 1 0 0 1 {left(line):.1f} {top - n * size * 1.15:.1f}"
        f" Tm ({_escape(line)}) Tj ET"
        for n, line in enumerate(lines)
    ]


def _quad(widget: Any) -> int:
    """The field's alignment (/Q), which a widget may take from its field."""
    node = widget.get_object()
    while node is not None:
        if "/Q" in node:
            return int(node["/Q"])
        parent = node.get("/Parent")
        node = parent.get_object() if parent is not None else None
    return 0


def fill(
    blank: Path, page_no: int | list[int], values: dict[str, str], out: Path
) -> Path:
    """The page (or pages, for a return) of ``blank`` with each value printed
    in the field named by its key: the one whose full name is the key, else the
    one whose name ends with it; a key must name exactly one field across the
    pages. A field that is the first box of a comb (one box per character, each
    its own field, as the NJ-1040 draws its amounts) takes one digit per box."""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(blank)
    numbers = page_no if isinstance(page_no, list) else [page_no]
    found = [fields_on(reader.pages[n]) for n in numbers]
    ops: list[list[str]] = [[] for _ in numbers]
    for key, value in values.items():
        hits = [(i, name) for i, f in enumerate(found) for name in f if name == key]
        hits = hits or [
            (i, name) for i, f in enumerate(found) for name in f if name.endswith(key)
        ]
        if len(hits) != 1:
            raise ValueError(f"{blank.name}: field {key!r} matches {hits}")
        i, name = hits[0]
        rects = [_rect(w) for w in found[i].values()]
        rect = _rect(found[i][name])
        cells = _comb(rect, rects)
        if cells and len(str(value)) > 1:
            for cell, digit in zip(
                cells, _digits(str(value), len(cells), key), strict=True
            ):
                ops[i] += _ops(cell, digit) if digit else []
        else:
            ops[i] += _ops(rect, str(value), _quad(found[i][name]))
    writer = PdfWriter()
    for i, n in enumerate(numbers):
        writer.add_page(reader.pages[n])
        _print(writer, writer.pages[i], ops[i])
    writer.compress_identical_objects()
    with out.open("wb") as fh:
        writer.write(fh)
    return out


def _rect(widget: Any) -> list[float]:
    """The widget's box as x0, y0, x1, y1, lower left first, whichever corners
    the form gives (the NJ-1040 gives some top left first)."""
    x0, y0, x1, y1 = (float(v) for v in widget["/Rect"])
    return [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]


def _comb(rect: list[float], rects: list[list[float]]) -> list[list[float]] | None:
    """The boxes of the comb that starts at ``rect``, left to right, or None if
    it is a field of its own. A comb's boxes stand in one row, a box's width
    apart or a little more; a box whose field sits off the row (one NJ-1040 box
    does) is put back where the gap shows it."""
    x0, y0, x1, _y1 = rect
    side = x1 - x0
    if side > COMB_SIDE:
        return None
    row = sorted(
        r
        for r in rects
        if abs(r[1] - y0) <= 3 and r[2] - r[0] <= COMB_SIDE and r[0] >= x0
    )
    cells = [rect]
    for r in row[1:]:
        steps = round((r[0] - cells[-1][0]) / (side * 1.15))
        if not 1 <= steps <= 2:
            break
        if steps == 2:
            mid = (cells[-1][0] + r[0]) / 2
            cells.append([mid, r[1], mid + side, r[3]])
        cells.append(r)
    return cells if len(cells) > 1 else None


def _digits(value: str, boxes: int, key: str) -> list[str]:
    """One character per box, right-aligned: "60,735.00" puts its cents in the
    last two boxes, the form printing its own comma and point; a value with no
    point fills from the last box."""
    whole, _, cents = value.partition(".")
    digits = "".join(c for c in whole if c.isdigit()) + cents
    if len(digits) > boxes:
        raise ValueError(f"{key!r}: {value!r} has more digits than its {boxes} boxes")
    return [""] * (boxes - len(digits)) + list(digits)


def _print(writer: Any, copy: Any, ops: list[str]) -> None:
    """Drop the page's fields and draw ``ops`` over it in Helvetica."""
    from pypdf.generic import (
        ArrayObject,
        DecodedStreamObject,
        DictionaryObject,
        NameObject,
    )

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
    # The page's own content is wrapped in q ... Q: a form that leaves a
    # transform set at its end (the NC D-400's page 2) would move the values.
    before = DecodedStreamObject()
    before.set_data(b"q\n")
    stream = DecodedStreamObject()
    stream.set_data(("Q\nq 0 g\n" + "\n".join(ops) + "\nQ\n").encode("cp1252"))
    contents = copy.get("/Contents")
    joined = ArrayObject([writer._add_object(before)])
    if isinstance(contents.get_object(), ArrayObject):
        joined.extend(contents.get_object())
    else:
        joined.append(contents)
    joined.append(writer._add_object(stream))
    copy[NameObject("/Contents")] = joined


def scan(src: Path, out: Path, seed: int = 0, turn: float = SCAN_TURN) -> Path:
    """An image-only PDF of ``src``'s pages as a scanner makes them:
    greyscale at ``SCAN_DPI``, turned ``turn`` degrees, with speckle."""
    import numpy as np
    import pdfplumber
    from PIL import Image

    rng = np.random.default_rng(seed)
    pages = []
    with pdfplumber.open(src) as doc:
        for page in doc.pages:
            image = page.to_image(resolution=SCAN_DPI).original.convert("L")
            image = image.rotate(turn, Image.Resampling.BICUBIC, fillcolor=255)
            noise = rng.normal(0, SCAN_SPECKLE, image.size[::-1])
            pixels = np.clip(np.asarray(image, dtype=float) + noise, 0, 255)
            pages.append(Image.fromarray(pixels.astype(np.uint8)))
    pages[0].save(
        out, "PDF", resolution=SCAN_DPI, save_all=True, append_images=pages[1:]
    )
    return out


def blank(entry: dict[str, Any], cache: Path) -> Path:
    """The entry's official blank, downloaded once; its hash must match. A
    link that does not end in the file's name (the NC D-400's ends ".../open")
    is cached under the entry's name."""
    url = str(entry["url"])
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in BLANK_HOSTS:
        raise ValueError(
            f"{entry['name']}: blanks come from the IRS or a state revenue "
            f"department, over https, only: not {url}"
        )
    file = PurePosixPath(parts.path).name
    path = cache / (file if file.lower().endswith(".pdf") else f"{entry['name']}.pdf")
    if not path.exists():
        cache.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(url, headers={"User-Agent": AGENT})  # noqa: S310
        with urllib.request.urlopen(request, timeout=60) as resp:  # noqa: S310
            path.write_bytes(resp.read())
    got = hashlib.sha256(path.read_bytes()).hexdigest()
    if got != entry["sha256"]:
        raise ValueError(
            f"{entry['name']}: {path.name} is sha256 {got}, the manifest says "
            f"{entry['sha256']}; the issuer changed the form, so re-check its layout"
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
        print(fill(blank(entry, args.cache), entry["page"], entry["fields"], out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
