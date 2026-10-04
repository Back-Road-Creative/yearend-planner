"""Write the synthetic 1099-INT and 1099-DIV PDFs the test suite uses into a
folder, for the Windows release proof's end-to-end step. Synthetic fixtures
only: never a real document.

    uv run python scripts/synthetic_inbox.py <folder>
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.pdfgen import make_pdf  # noqa: E402
from tests.test_ingest import DIV_2025, INT_2025  # noqa: E402


def write(folder: Path) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    return [
        make_pdf(folder / "int.pdf", [INT_2025]),
        make_pdf(folder / "div.pdf", [DIV_2025]),
    ]


if __name__ == "__main__":
    for path in write(Path(sys.argv[1])):
        print(path)
