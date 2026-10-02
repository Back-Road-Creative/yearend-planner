"""Write small text-only PDFs for the ingest tests. No third-party writer needed.

Each page is a list of lines, drawn top to bottom in Helvetica 10pt, so
``pdfplumber`` extracts them back in order. Synthetic fixtures only.
"""

from __future__ import annotations

from pathlib import Path


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _content(lines: list[str]) -> bytes:
    ops = ["BT", "/F1 10 Tf"]
    y = 760
    for line in lines:
        ops.append(f"1 0 0 1 50 {y} Tm ({_esc(line)}) Tj")
        y -= 14
    ops.append("ET")
    return "\n".join(ops).encode("latin-1")


def make_pdf(path: Path, pages: list[list[str]]) -> Path:
    """Write ``pages`` (lists of text lines) to ``path`` as a one-font PDF."""
    objs: list[bytes] = []
    # 1 catalog, 2 pages, 3 font, then (page, content) pairs
    n_pages = len(pages)
    page_ids = [4 + 2 * i for i in range(n_pages)]
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, lines in enumerate(pages):
        content = _content(lines)
        objs.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> "
                f"/Contents {page_ids[i] + 1} 0 R >>"
            ).encode()
        )
        objs.append(
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"
        )
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for num, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{num} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    path.write_bytes(bytes(out))
    return path
