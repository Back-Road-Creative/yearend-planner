"""``planner ingest``: read everything in ``data/inbox/`` into the ledger.

Per file: fingerprint, classify, parse, commit the facts in one transaction,
then move the original to ``data/archive/<year>/``. Anything unreadable
moves to ``data/inbox/UNMATCHED/`` beside a ``.reason.txt``. A re-run after
an interruption reaches the same state: a fingerprint already in the ledger
is archived without a second import.
"""

from __future__ import annotations

import hashlib
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from planner.ingest.csvfile import load_csv_templates, parse_csv
from planner.ingest.pdf import ParsedForm, Unmatched, load_templates, parse_pdf
from planner.ledger import db
from planner.paths import Layout

# Templates ship with the code (swapped by ``planner update``), not under data/.
TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates" / "forms"
CSV_TEMPLATES_DIR = TEMPLATES_DIR.parent / "csv"
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


def fingerprint(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def unpack_zips(inbox: Path) -> None:
    """A dropped ZIP is unpacked in place and removed; entries may not escape."""
    for z in sorted(inbox.glob("*.zip")):
        base = inbox.resolve()
        with zipfile.ZipFile(z) as zf:
            for member in zf.infolist():
                target = (inbox / member.filename).resolve()
                if target != base and base not in target.parents:
                    raise Unmatched(f"{z.name}: entry escapes inbox: {member.filename}")
            zf.extractall(inbox)
        z.unlink()


def inbox_files(inbox: Path) -> list[Path]:
    return sorted(
        p for p in inbox.iterdir() if p.is_file() and not p.name.startswith(".")
    )


def to_unmatched(path: Path, inbox: Path, reason: str) -> None:
    dest = inbox / "UNMATCHED"
    dest.mkdir(exist_ok=True)
    target = dest / path.name
    shutil.move(str(path), str(target))
    (dest / f"{path.name}.reason.txt").write_text(reason + "\n", encoding="utf-8")


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
            value=value,
            page=f.page,
        )
        for f in forms
        for box, (label, value) in sorted(f.boxes.items())
    ]


def ingest(lay: Layout, templates_dir: Path | None = None) -> IngestReport:
    inbox = lay.data / "inbox"
    archive_root = lay.data / "archive"
    templates = load_templates(templates_dir or TEMPLATES_DIR)
    csv_templates = load_csv_templates(CSV_TEMPLATES_DIR)
    report = IngestReport(batch=datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        unpack_zips(inbox)
    except (Unmatched, zipfile.BadZipFile) as exc:
        report.unmatched.append(("zip", str(exc)))
    for path in inbox_files(inbox):
        kind = KINDS.get(path.suffix.lower())
        fp = fingerprint(path)
        if kind is None:
            to_unmatched(path, inbox, f"unknown file type {path.suffix!r}")
            report.unmatched.append((path.name, f"unknown file type {path.suffix!r}"))
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
        try:
            if kind == "pdf":
                forms = parse_pdf(path, templates)
            elif kind == "csv":
                rows = parse_csv(path, csv_templates)
            else:
                raise Unmatched("image: OCR with confirm is not available yet")
        except Unmatched as exc:
            to_unmatched(path, inbox, str(exc))
            report.unmatched.append((path.name, str(exc)))
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
        )
        years = [f.tax_year for f in forms] + [r.tax_year for r in rows if r.tax_year]
        year = min(years) if years else datetime.now(UTC).year
        archived = archive(path, archive_root, year, fp)
        db.set_archived(conn, doc_id, str(archived.relative_to(lay.data)))
        labels = [f"{f.form} {f.tax_year} ({f.issuer})" for f in forms]
        for source in dict.fromkeys(r.source for r in rows):
            n = sum(1 for r in rows if r.source == source)
            labels.append(f"{source} {n} rows")
        report.imported.append(
            Imported(
                file_name=path.name,
                forms=tuple(labels),
                archived_as=str(archived.relative_to(lay.data)),
            )
        )
    conn.close()
    return report
