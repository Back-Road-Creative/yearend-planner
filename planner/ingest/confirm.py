"""``planner confirm``: values OCR read wait here until a person accepts them."""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterable

from planner.ingest import ocr
from planner.ledger import db
from planner.paths import Layout


def pending(conn: sqlite3.Connection) -> list[db.FactRow]:
    return db.facts_for(conn, status="pending", text=None)


def shared_boxes(facts: Iterable[db.FactRow]) -> frozenset[str]:
    """The boxes that more than one form (a form, tax year and payer) carries
    among ``facts``, one document's pending values. A correction names only a
    box, so it cannot say which of those it means."""
    owners: dict[str, set[tuple[str, int, str]]] = {}
    for f in facts:
        owners.setdefault(f.box, set()).add((f.form, f.tax_year, f.issuer))
    return frozenset(box for box, forms in owners.items() if len(forms) > 1)


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
    ambiguous = sorted(set(edits) & shared_boxes(pend))
    if ambiguous:
        raise ValueError(
            f"box {', '.join(ambiguous)} is on more than one form, payer or year "
            f"in document {doc_id}; a correction cannot say which one it means"
        )
    with conn:
        for box, value in edits.items():
            for f in (f for f in pend if f.box == box):
                if f.text is None:
                    conn.execute(
                        "UPDATE facts SET value_cents = ? WHERE document_id = ? "
                        "AND box = ? AND form = ? AND tax_year = ? AND issuer = ? "
                        "AND status = 'pending' AND value_text IS NULL",
                        (
                            db.to_cents(money(box, value)),
                            doc_id,
                            box,
                            f.form,
                            f.tax_year,
                            f.issuer,
                        ),
                    )
                else:
                    conn.execute(
                        "UPDATE facts SET value_text = ? WHERE document_id = ? "
                        "AND box = ? AND form = ? AND tax_year = ? AND issuer = ? "
                        "AND status = 'pending' AND value_text IS NOT NULL",
                        (
                            _words(box, value),
                            doc_id,
                            box,
                            f.form,
                            f.tax_year,
                            f.issuer,
                        ),
                    )
        for key in {(f.form, f.tax_year, f.issuer) for f in pend}:
            db.supersede(conn, key, pend[0].owner, keep=doc_id)
        conn.execute(
            "UPDATE facts SET status = 'accepted' WHERE document_id = ? "
            "AND status = 'pending'",
            (doc_id,),
        )
    return [f for f in db.facts_for(conn, text=None) if f.document_id == doc_id]


def money(box: str, value: float | str) -> float:
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
    """Drop one document's pending facts. A document with nothing accepted (a
    scan or photo) is forgotten as well, so a better scan (or the same file)
    can be dropped again; the file returns to ``data/inbox/UNMATCHED/`` with
    the reason. A document that also holds accepted facts (a PDF whose text
    pages were read straight from the file) keeps them, itself and its archive
    copy: forgetting it would take those values out of the ledger and leave
    the earlier copies it superseded retired. The values of its scanned pages
    can be typed with ``planner enter``. Returns where the file now is."""
    pend = [f for f in pending(conn) if f.document_id == doc_id]
    if not pend:
        raise KeyError(f"document {doc_id} has no values awaiting confirm")
    row = conn.execute(
        "SELECT file_name, archived_as FROM documents WHERE id = ?", (doc_id,)
    ).fetchone()
    kept = conn.execute(
        "SELECT COUNT(*) FROM facts WHERE document_id = ? AND status != 'pending'",
        (doc_id,),
    ).fetchone()[0]
    with conn:
        conn.execute(
            "DELETE FROM facts WHERE document_id = ? AND status = 'pending'",
            (doc_id,),
        )
        if not kept:
            conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    ocr.forget_crops(lay, doc_id)
    if kept:
        return str(row["archived_as"])
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
