"""SQLite storage for imported documents and the facts read from them.

Phase 2a schema: ``documents`` (one row per imported file, keyed by content
hash) and ``facts`` (one row per form box read from a page). Money is stored
as integer cents. Phase 2b adds CSV rows; Phase 3 adds the planner views.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE,
    file_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    pages INTEGER NOT NULL DEFAULT 0,
    imported_at TEXT NOT NULL,
    batch TEXT NOT NULL,
    archived_as TEXT
);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    form TEXT NOT NULL,
    tax_year INTEGER NOT NULL,
    issuer TEXT NOT NULL,
    box TEXT NOT NULL,
    label TEXT NOT NULL,
    value_cents INTEGER NOT NULL,
    page INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('accepted', 'pending', 'superseded')),
    UNIQUE (document_id, form, tax_year, issuer, box)
);
CREATE INDEX IF NOT EXISTS facts_by_year ON facts (tax_year, form, status);
"""


def to_cents(value: float) -> int:
    """Money is stored as integer cents; half-cents round away from zero."""
    return int(Decimal(str(value)).quantize(Decimal("1.00"), ROUND_HALF_UP) * 100)


def from_cents(cents: int) -> float:
    return cents / 100


@dataclass(frozen=True)
class Fact:
    form: str
    tax_year: int
    issuer: str
    box: str
    label: str
    value: float
    page: int
    status: str = "accepted"


@dataclass(frozen=True)
class FactRow(Fact):
    document_id: int = 0
    file_name: str = ""


def connect(path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the ledger; schema is applied idempotently."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    if conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0:
        conn.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()
    return conn


def has_fingerprint(conn: sqlite3.Connection, fingerprint: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM documents WHERE fingerprint = ?", (fingerprint,)
    ).fetchone()
    return row is not None


def add_document(
    conn: sqlite3.Connection,
    *,
    fingerprint: str,
    file_name: str,
    kind: str,
    pages: int,
    batch: str,
    facts: list[Fact],
) -> int:
    """Insert one document and its facts in a single transaction.

    A newer document for the same (form, issuer, year) supersedes the older
    one's accepted facts: a corrected 1099 replaces the original.
    """
    with conn:
        cur = conn.execute(
            "INSERT INTO documents (fingerprint, file_name, kind, pages, "
            "imported_at, batch) VALUES (?, ?, ?, ?, ?, ?)",
            (
                fingerprint,
                file_name,
                kind,
                pages,
                datetime.now(UTC).isoformat(timespec="seconds"),
                batch,
            ),
        )
        doc_id = int(cur.lastrowid or 0)
        for key in {(f.form, f.tax_year, f.issuer) for f in facts}:
            conn.execute(
                "UPDATE facts SET status = 'superseded' WHERE form = ? AND "
                "tax_year = ? AND issuer = ? AND status = 'accepted'",
                key,
            )
        conn.executemany(
            "INSERT INTO facts (document_id, form, tax_year, issuer, box, label, "
            "value_cents, page, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    doc_id,
                    f.form,
                    f.tax_year,
                    f.issuer,
                    f.box,
                    f.label,
                    to_cents(f.value),
                    f.page,
                    f.status,
                )
                for f in facts
            ],
        )
    return doc_id


def set_archived(conn: sqlite3.Connection, doc_id: int, archived_as: str) -> None:
    with conn:
        conn.execute(
            "UPDATE documents SET archived_as = ? WHERE id = ?", (archived_as, doc_id)
        )


def facts_for(
    conn: sqlite3.Connection,
    tax_year: int | None = None,
    form: str | None = None,
    status: str = "accepted",
) -> list[FactRow]:
    sql = (
        "SELECT f.*, d.file_name FROM facts f JOIN documents d ON d.id = f.document_id "
        "WHERE f.status = ?"
    )
    args: list[object] = [status]
    if tax_year is not None:
        sql += " AND f.tax_year = ?"
        args.append(tax_year)
    if form is not None:
        sql += " AND f.form = ?"
        args.append(form)
    sql += " ORDER BY f.tax_year, f.form, f.issuer, f.box"
    return [
        FactRow(
            form=r["form"],
            tax_year=r["tax_year"],
            issuer=r["issuer"],
            box=r["box"],
            label=r["label"],
            value=from_cents(r["value_cents"]),
            page=r["page"],
            status=r["status"],
            document_id=r["document_id"],
            file_name=r["file_name"],
        )
        for r in conn.execute(sql, args)
    ]
