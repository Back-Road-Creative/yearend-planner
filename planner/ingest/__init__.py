"""``planner ingest``: read everything in ``data/inbox/`` into the ledger.

Per file: fingerprint, classify, parse, commit the facts in one transaction,
then move the original to ``data/archive/<year>/``. Anything unreadable
moves to ``data/inbox/UNMATCHED/`` beside a ``.reason.txt``. A re-run after
an interruption reaches the same state: a fingerprint already in the ledger
is archived without a second import.
"""

from __future__ import annotations

import contextlib
import hashlib
import shutil
import zipfile
import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from planner.ingest import ocr as ocr_engine
from planner.ingest.csvfile import load_csv_templates, parse_csv
from planner.ingest.derive import derive, row_years
from planner.ingest.pdf import (
    ParsedForm,
    Unmatched,
    load_templates,
    parse_pdf,
    parse_texts,
)
from planner.ledger import db
from planner.paths import Layout
from planner.taxprep import capgains, hsa, schedule_c

# Templates ship with the code (swapped by ``planner update``), not under data/.
TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates" / "forms"
CSV_TEMPLATES_DIR = TEMPLATES_DIR.parent / "csv"
# ZIPs inside ZIPs are opened this many levels deep; past it the outer file is
# set aside rather than unpacked without end (a ZIP bomb nests on purpose).
MAX_ZIP_DEPTH = 5
KINDS = {
    ".pdf": "pdf",
    ".csv": "csv",
    ".jpg": "image",
    ".jpeg": "image",
    ".png": "image",
}


@dataclass(frozen=True)
class Imported:
    file_name: str
    forms: tuple[str, ...]
    archived_as: str


@dataclass
class IngestReport:
    batch: str
    imported: list[Imported] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    unmatched: list[tuple[str, str]] = field(default_factory=list)
    notes: list[tuple[str, str]] = field(default_factory=list)  # read, with a caveat
    pending: list[Imported] = field(default_factory=list)  # OCR, awaiting confirm
    derived: dict[int, int] = field(default_factory=dict)  # year -> YTD facts


def fingerprint(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _free_name(target: Path) -> Path:
    """``target``, or ``stem (2).ext``, ``stem (3).ext`` … when it is taken."""
    stem, suffix = target.stem, target.suffix
    n = 2
    while target.exists():
        target = target.with_name(f"{stem} ({n}){suffix}")
        n += 1
    return target


def _hidden(rel: PurePosixPath | Path) -> bool:
    """A dotted name or ``__MACOSX`` (a Mac ZIP's resource forks) is not a document."""
    return any(part.startswith(".") or part == "__MACOSX" for part in rel.parts)


def _label(path: Path, inbox: Path) -> str:
    """The file as the person dropped it: its path inside the inbox."""
    return path.relative_to(inbox).as_posix()


def owner_of(label: str) -> str:
    """Whose a dropped file is: under ``inbox/spouse/`` (any case) the joint
    spouse's, anywhere else the head's."""
    parts = PurePosixPath(label).parts
    return "spouse" if len(parts) > 1 and parts[0].lower() == "spouse" else "you"


def _zip_members(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """The files worth extracting; ``BadZipFile`` for an entry that must not be."""
    keep: list[zipfile.ZipInfo] = []
    for member in zf.infolist():
        name = PurePosixPath(member.filename.replace("\\", "/"))
        if name.is_absolute() or ".." in name.parts or ":" in (name.parts or ("",))[0]:
            raise zipfile.BadZipFile(f"entry escapes the folder: {member.filename}")
        if member.is_dir() or _hidden(name):
            continue
        if member.flag_bits & 0x1:
            raise zipfile.BadZipFile(
                f"{member.filename} is password-protected; unlock it and drop it again"
            )
        keep.append(member)
    return keep


def _extract(z: Path) -> list[Path]:
    """Unpack ``z`` beside itself; all of it, or none of it. Never overwrites."""
    made: list[Path] = []
    try:
        with zipfile.ZipFile(z) as zf:
            for member in _zip_members(zf):
                rel = PurePosixPath(member.filename.replace("\\", "/"))
                target = _free_name(z.parent.joinpath(*rel.parts))
                target.parent.mkdir(parents=True, exist_ok=True)
                made.append(target)
                with zf.open(member) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
    except BaseException:
        for f in made:
            f.unlink(missing_ok=True)
        raise
    return made


def inbox_zips(inbox: Path) -> list[Path]:
    return [p for p in inbox_files(inbox) if p.suffix.lower() == ".zip"]


def unpack_zips(inbox: Path) -> list[tuple[str, str]]:
    """Unpack every ZIP in the inbox, subfolders and ZIPs inside ZIPs included,
    and remove it. A ZIP that cannot be read whole (corrupt, password-protected,
    an entry that would escape, nested past ``MAX_ZIP_DEPTH``) moves to
    ``UNMATCHED`` with a reason, extracts nothing, and does not stop the others.
    Returns ``(name, reason)`` for each ZIP set aside."""
    failed: list[tuple[str, str]] = []
    for depth in range(MAX_ZIP_DEPTH + 1):
        zips = inbox_zips(inbox)
        if not zips:
            break
        for z in zips:
            label = _label(z, inbox)
            if depth >= MAX_ZIP_DEPTH:
                reason = f"ZIPs nested more than {MAX_ZIP_DEPTH} deep"
            else:
                try:
                    _extract(z)
                except (
                    zipfile.BadZipFile,
                    zlib.error,
                    EOFError,
                    NotImplementedError,
                    RuntimeError,
                    OSError,
                ) as exc:
                    reason = _zip_reason(exc)
                else:
                    z.unlink()
                    continue
            to_unmatched(z, inbox, reason)
            failed.append((label, reason))
    return failed


def _zip_reason(exc: Exception) -> str:
    if isinstance(exc, zipfile.BadZipFile) and "entry escapes" in str(exc):
        return f"unsafe ZIP: {exc}"
    if isinstance(exc, zipfile.BadZipFile) and "password" in str(exc):
        return f"password-protected ZIP: {exc}"
    return f"not a readable ZIP ({type(exc).__name__}: {exc})"


def inbox_files(inbox: Path) -> list[Path]:
    """Every file under the inbox, folders included, except ``UNMATCHED`` and
    dotted names; a folder of statements is read like a pile of them."""
    found: list[Path] = []
    for p in inbox.rglob("*"):
        rel = p.relative_to(inbox)
        if rel.parts[0] == "UNMATCHED" or _hidden(rel) or not p.is_file():
            continue
        found.append(p)
    return sorted(found, key=lambda p: p.relative_to(inbox).as_posix())


def prune_empty_folders(inbox: Path) -> None:
    """Remove the folders a ZIP or a dropped folder left empty."""
    for d in sorted(
        (p for p in inbox.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    ):
        if d.relative_to(inbox).parts[0] == "UNMATCHED":
            continue
        with contextlib.suppress(OSError):  # not empty: leave it
            d.rmdir()


def to_unmatched(path: Path, inbox: Path, reason: str) -> None:
    dest = inbox / "UNMATCHED"
    dest.mkdir(exist_ok=True)
    target = _free_name(dest / path.name)
    where = _label(path, inbox)
    if "/" in where:
        reason = f"{reason} (from {where})"
    shutil.move(str(path), str(target))
    (dest / f"{target.name}.reason.txt").write_text(reason + "\n", encoding="utf-8")


def archive(path: Path, archive_root: Path, year: int, fp: str) -> Path:
    """Move into ``archive/<year>/``; a different file of the same name keeps both."""
    folder = archive_root / str(year)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / path.name
    if target.exists():
        if fingerprint(target) == fp:
            path.unlink()
            return target
        target = folder / f"{path.stem}.{fp[:8]}{path.suffix}"
    shutil.move(str(path), str(target))
    return target


def _facts(forms: list[ParsedForm]) -> list[db.Fact]:
    return [
        db.Fact(
            form=f.form,
            tax_year=f.tax_year,
            issuer=f.issuer,
            box=box,
            label=label,
            value=0.0 if isinstance(value, str) else value,
            page=f.page,
            status="pending" if f.ocr else "accepted",
            text=value if isinstance(value, str) else None,
        )
        for f in forms
        for box, (label, value) in sorted(f.boxes.items())
    ]


def _remember(
    ocr: ocr_engine.PageTexts, seen: list[Sequence[str]]
) -> ocr_engine.PageTexts:
    """``ocr`` that keeps what it returned, so the crops can be cut from it."""

    def read(path: Path) -> Sequence[str]:
        pages = ocr(path)
        seen.append(pages)
        return pages

    return read


def ingest(
    lay: Layout,
    templates_dir: Path | None = None,
    ocr: ocr_engine.PageTexts | None = None,
) -> IngestReport:
    """``ocr`` reads pages the PDF text layer cannot; by default the installed
    engine, or none when it is missing (those files go to UNMATCHED)."""
    if ocr is None and ocr_engine.available():
        ocr = ocr_engine.page_texts
    inbox = lay.data / "inbox"
    archive_root = lay.data / "archive"
    templates = load_templates(templates_dir or TEMPLATES_DIR)
    csv_templates = load_csv_templates(CSV_TEMPLATES_DIR)
    report = IngestReport(batch=datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    conn = db.connect(lay.data / "ledger" / "planner.db")
    report.unmatched.extend(unpack_zips(inbox))
    for path in inbox_files(inbox):
        kind = KINDS.get(path.suffix.lower())
        fp = fingerprint(path)
        label = _label(path, inbox)
        if kind is None:
            to_unmatched(path, inbox, f"unknown file type {path.suffix!r}")
            report.unmatched.append((label, f"unknown file type {path.suffix!r}"))
            continue
        if db.has_fingerprint(conn, fp):
            row = conn.execute(
                "SELECT MIN(y) AS y FROM (SELECT f.tax_year AS y FROM facts f "
                "JOIN documents d ON d.id = f.document_id WHERE d.fingerprint = ? "
                "UNION ALL SELECT r.tax_year FROM rows r JOIN documents d "
                "ON d.id = r.document_id WHERE d.fingerprint = ?)",
                (fp, fp),
            ).fetchone()
            archive(path, archive_root, int(row["y"] or 0), fp)
            report.duplicates.append(path.name)
            continue
        forms: list[ParsedForm] = []
        rows: list[db.Row] = []
        scans: list[Sequence[str]] = []  # what the engine read, to cut crops from
        reader = None if ocr is None else _remember(ocr, scans)
        try:
            if kind == "pdf":
                notes: list[str] = []
                forms = parse_pdf(path, templates, reader, notes)
                report.notes.extend((label, n) for n in notes)
            elif kind == "csv":
                rows = parse_csv(path, csv_templates)
            elif reader is None:
                raise Unmatched(f"image: {ocr_engine.NOT_INSTALLED}")
            else:
                forms = parse_texts(list(reader(path)), templates, ocr=True)
        except Unmatched as exc:
            to_unmatched(path, inbox, str(exc))
            report.unmatched.append((label, str(exc)))
            continue
        doc_id = db.add_document(
            conn,
            fingerprint=fp,
            file_name=path.name,
            kind=kind,
            pages=max((f.page for f in forms), default=1),
            batch=report.batch,
            facts=_facts(forms),
            rows=rows,
            owner=owner_of(label),
        )
        years = [f.tax_year for f in forms] + [r.tax_year for r in rows if r.tax_year]
        year = min(years) if years else datetime.now(UTC).year
        if any(f.ocr for f in forms):
            made, wanted = ocr_engine.write_crops(
                lay, conn, doc_id, forms, scans[-1] if scans else None
            )
            if made < wanted:
                report.notes.append(
                    (label, f"{wanted - made} OCR value(s) have no image crop")
                )
        archived = archive(path, archive_root, year, fp)
        db.set_archived(conn, doc_id, archived.relative_to(lay.data).as_posix())
        labels = [f"{f.form} {f.tax_year} ({f.issuer})" for f in forms]
        for source in dict.fromkeys(r.source for r in rows):
            n = sum(1 for r in rows if r.source == source)
            labels.append(f"{source} {n} rows")
        item = Imported(
            file_name=path.name,
            forms=tuple(labels),
            archived_as=archived.relative_to(lay.data).as_posix(),
        )
        (report.pending if any(f.ocr for f in forms) else report.imported).append(item)
    prune_empty_folders(inbox)
    if report.imported:
        for year in row_years(conn):
            n = derive(conn, year, report.batch)
            if n:
                report.derived[year] = n
            schedule_c.store(conn, lay, year)
            capgains.store(conn, lay, year)
            hsa.store(conn, lay, year)
    conn.close()
    return report
