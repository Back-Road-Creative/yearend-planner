"""``planner acceptance``: read a person's own documents the way ``planner
ingest`` would and say, file by file, whether each one would import. Nothing
moves, nothing is unpacked beside a ZIP and nothing reaches the ledger. A
passing file is named by its form and year (a CSV by its source and row count),
never by a figure, a payer or an account; the result is kept under
``data/private/acceptance/`` and goes nowhere else."""

from __future__ import annotations

import shutil
import tempfile
import zipfile
import zlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from planner.ingest import (
    CSV_TEMPLATES_DIR,
    KINDS,
    MAX_ZIP_DEPTH,
    TEMPLATES_DIR,
    _hidden,
    _zip_members,
    _zip_reason,
)
from planner.ingest import ocr as ocr_engine
from planner.ingest.csvfile import load_csv_templates, parse_csv
from planner.ingest.pdf import Unmatched, load_templates, parse_pdf, parse_texts
from planner.paths import Layout

_ZIP_ERRORS = (
    zipfile.BadZipFile,
    zlib.error,
    EOFError,
    NotImplementedError,
    RuntimeError,
    OSError,
)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


class _Reader:
    def __init__(self, ocr: ocr_engine.PageTexts | None) -> None:
        self.ocr = ocr
        self.templates = load_templates(TEMPLATES_DIR)
        self.csv_templates = load_csv_templates(CSV_TEMPLATES_DIR)

    def check(self, path: Path, name: str) -> Check:
        kind = KINDS.get(path.suffix.lower())
        if kind is None:
            return Check(name, False, f"unknown file type {path.suffix!r}")
        try:
            if kind == "csv":
                rows = parse_csv(path, self.csv_templates)
                labels = [
                    f"{source} {sum(1 for r in rows if r.source == source)} rows"
                    for source in dict.fromkeys(r.source for r in rows)
                ]
                return Check(name, True, ", ".join(labels))
            if kind == "pdf":
                forms = parse_pdf(path, self.templates, self.ocr, [])
            elif self.ocr is None:
                raise Unmatched(f"image: {ocr_engine.NOT_INSTALLED}")
            else:
                forms = parse_texts(list(self.ocr(path)), self.templates, ocr=True)
        except Unmatched as exc:
            return Check(name, False, str(exc))
        labels = [f"{f.form} {f.tax_year}" for f in forms]
        scan = " (scan)" if any(f.ocr for f in forms) else ""
        return Check(name, True, ", ".join(dict.fromkeys(labels)) + scan)

    def zipped(self, z: Path, name: str, depth: int = 0) -> Iterator[Check]:
        if depth >= MAX_ZIP_DEPTH:
            yield Check(name, False, f"ZIPs nested more than {MAX_ZIP_DEPTH} deep")
            return
        with tempfile.TemporaryDirectory() as tmp:
            try:
                with zipfile.ZipFile(z) as zf:
                    members = _zip_members(zf)
                    for n, member in enumerate(members):
                        dest = Path(tmp) / str(n) / PurePosixPath(member.filename).name
                        dest.parent.mkdir()
                        with zf.open(member) as src, dest.open("wb") as dst:
                            shutil.copyfileobj(src, dst)
            except _ZIP_ERRORS as exc:
                yield Check(name, False, _zip_reason(exc))
                return
            for n, member in enumerate(members):
                inner = f"{name}!{member.filename.replace(chr(92), '/')}"
                dest = Path(tmp) / str(n) / PurePosixPath(member.filename).name
                if dest.suffix.lower() == ".zip":
                    yield from self.zipped(dest, inner, depth + 1)
                else:
                    yield self.check(dest, inner)


def _files(paths: Iterable[Path]) -> Iterator[tuple[Path, str]]:
    for top in paths:
        if top.is_file():
            yield top, top.name
            continue
        found = [
            p for p in top.rglob("*") if p.is_file() and not _hidden(p.relative_to(top))
        ]
        for p in sorted(found, key=lambda p: p.relative_to(top).as_posix()):
            yield p, p.relative_to(top).as_posix()


def check(
    paths: Iterable[Path], ocr: ocr_engine.PageTexts | None = None
) -> list[Check]:
    """One ``Check`` per document under ``paths`` (files or folders; each file
    in a ZIP is one). ``ocr`` reads scans; ``None`` means no engine."""
    reader = _Reader(ocr)
    out: list[Check] = []
    for path, name in _files(paths):
        if path.suffix.lower() == ".zip":
            out.extend(reader.zipped(path, name))
        else:
            out.append(reader.check(path, name))
    return out


def tally(checks: list[Check]) -> str:
    passed = sum(c.ok for c in checks)
    return f"{passed} passed, {len(checks) - passed} failed"


def write_report(lay: Layout, checks: list[Check]) -> Path:
    """The result, kept under ``data/private/acceptance/`` and nowhere else."""
    folder = lay.data / "private" / "acceptance"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    out = folder / f"{stamp}.txt"
    lines = [f"{'PASS' if c.ok else 'FAIL'} {c.name}: {c.detail}" for c in checks]
    out.write_text("\n".join([*lines, tally(checks)]) + "\n", encoding="utf-8")
    return out
