"""``planner confirm``: values OCR read wait here until a person accepts them."""

from __future__ import annotations

import shutil
import sqlite3

from planner.ledger import db
from planner.paths import Layout


def pending(conn: sqlite3.Connection) -> list[db.FactRow]:
    return db.facts_for(conn, status="pending", text=None)


def accept(
    conn: sqlite3.Connection, doc_id: int, edits: dict[str, float | str] | None = None
) -> list[db.FactRow]:
    """Take one document's pending facts into the ledger, after any typed
    corrections (a dollar figure for a money box, words for a text box); the
    accepted facts they replace are superseded, as a fresh drop of the form
    would do."""
    pend = [f for f in pending(conn) if f.document_id == doc_id]
    if not pend:
        raise KeyError(f"document {doc_id} has no values awaiting confirm")
    edits = edits or {}
    unknown = sorted(set(edits) - {f.box for f in pend})
    if unknown:
        raise KeyError(f"document {doc_id} has no box {', '.join(unknown)}")
    with conn:
        for box, value in edits.items():
            for f in (f for f in pend if f.box == box):
                if f.text is None:
                    conn.execute(
                        "UPDATE facts SET value_cents = ? WHERE document_id = ? "
                        "AND box = ? AND form = ? AND status = 'pending' "
                        "AND value_text IS NULL",
                        (db.to_cents(_money(box, value)), doc_id, box, f.form),
                    )
                else:
                    conn.execute(
                        "UPDATE facts SET value_text = ? WHERE document_id = ? "
                        "AND box = ? AND form = ? AND status = 'pending' "
                        "AND value_text IS NOT NULL",
                        (_words(box, value), doc_id, box, f.form),
                    )
        for key in {(f.form, f.tax_year, f.issuer) for f in pend}:
            conn.execute(
                "UPDATE facts SET status = 'superseded' WHERE form = ? AND "
                "tax_year = ? AND issuer = ? AND status = 'accepted'",
                key,
            )
        conn.execute(
            "UPDATE facts SET status = 'accepted' WHERE document_id = ? "
            "AND status = 'pending'",
            (doc_id,),
        )
    return [f for f in db.facts_for(conn, text=None) if f.document_id == doc_id]


def _money(box: str, value: float | str) -> float:
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").replace("$", ""))
        except ValueError:
            raise ValueError(f"box {box}: a dollar amount, got {value!r}") from None
    return value


def _words(box: str, value: float | str) -> str:
    words = " ".join(str(value).split())
    if not words or not words.isprintable():
        raise ValueError(f"box {box}: words with no control characters")
    return words


def reject(lay: Layout, conn: sqlite3.Connection, doc_id: int) -> str:
    """Drop one document's pending facts and forget the document, so a better
    scan (or the same file) can be dropped again; the file returns to
    ``data/inbox/UNMATCHED/`` with the reason."""
    pend = [f for f in pending(conn) if f.document_id == doc_id]
    if not pend:
        raise KeyError(f"document {doc_id} has no values awaiting confirm")
    row = conn.execute(
        "SELECT file_name, archived_as FROM documents WHERE id = ?", (doc_id,)
    ).fetchone()
    with conn:
        conn.execute("DELETE FROM facts WHERE document_id = ?", (doc_id,))
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    dest = lay.data / "inbox" / "UNMATCHED"
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / str(row["file_name"])
    if row["archived_as"]:
        shutil.move(str(lay.data / str(row["archived_as"])), str(target))
    (dest / f"{target.name}.reason.txt").write_text(
        "OCR values rejected at confirm; drop a clearer scan or type the values "
        "with planner enter\n",
        encoding="utf-8",
    )
    return target.relative_to(lay.data).as_posix()
