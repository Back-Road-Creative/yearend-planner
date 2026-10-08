"""SQLite storage for imported documents and the facts read from them.

Phase 2a schema: ``documents`` (one row per imported file, keyed by content
hash) and ``facts`` (one row per form box read from a page). Money is stored
as integer cents. A box that holds words, not dollars (a filing status, a
state, a distribution code) keeps them in ``value_text`` (schema 4) and
``value_cents`` is 0; ``facts_for`` returns only the money facts unless asked.
Each document is one person's (``owner``, schema 5): the head's (``you``) or a
joint return's spouse's, so a corrected W-2 replaces only its owner's copy.
Phase 2b adds CSV rows; Phase 3 adds ``conversions`` (each Roth
conversion with the date its money becomes penalty-free) and ``peak`` (the
all-time high of the portfolio, persisted so a drawdown is measured from it).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

SCHEMA_VERSION = 5
OWNERS = ("you", "spouse")  # whose a document is: the head, or a joint spouse
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
    archived_as TEXT,
    owner TEXT NOT NULL DEFAULT 'you' CHECK (owner IN ('you', 'spouse'))
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
    value_text TEXT,
    UNIQUE (document_id, form, tax_year, issuer, box)
);
CREATE INDEX IF NOT EXISTS facts_by_year ON facts (tax_year, form, status);
CREATE TABLE IF NOT EXISTS rows (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    row_key TEXT NOT NULL UNIQUE,
    account TEXT NOT NULL,
    date TEXT,
    tax_year INTEGER,
    type TEXT NOT NULL,
    description TEXT NOT NULL,
    symbol TEXT NOT NULL,
    quantity REAL,
    price_cents INTEGER,
    amount_cents INTEGER,
    basis_cents INTEGER,
    acquired TEXT,
    term TEXT,
    line INTEGER NOT NULL,
    raw TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS rows_by_year ON rows (tax_year, source, kind);
CREATE TABLE IF NOT EXISTS conversions (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    taxable_cents INTEGER NOT NULL,
    source_account TEXT NOT NULL,
    accessible_date TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS peak (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    total_cents INTEGER NOT NULL,
    as_of TEXT NOT NULL
);
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
    value: float  # dollars; 0.0 for a text fact
    page: int
    status: str = "accepted"
    text: str | None = None  # the words a text box holds; None for money


@dataclass(frozen=True)
class FactRow(Fact):
    document_id: int = 0
    file_name: str = ""
    id: int = 0
    owner: str = "you"


@dataclass(frozen=True)
class Row:
    """One normalized CSV row: a holding, lot, transaction, realized sale, income
    item or bank line. Money in cents; ``raw`` keeps the source row verbatim."""

    source: str
    kind: str
    row_key: str
    account: str
    date: str | None
    tax_year: int | None
    type: str
    description: str
    symbol: str
    quantity: float | None
    price_cents: int | None
    amount_cents: int | None
    basis_cents: int | None
    acquired: str | None
    term: str | None
    line: int
    raw: str


@dataclass(frozen=True)
class LedgerRow(Row):
    document_id: int = 0
    file_name: str = ""


class LedgerOutOfDate(RuntimeError):
    """The ledger's schema is not the one this planner reads."""


class LedgerTooNew(LedgerOutOfDate):
    """The ledger was written by a newer planner; this one must not touch it."""


def _to_4(conn: sqlite3.Connection) -> None:
    if "value_text" not in {
        r["name"] for r in conn.execute("PRAGMA table_info(facts)")
    }:
        conn.execute("ALTER TABLE facts ADD COLUMN value_text TEXT")


def _to_5(conn: sqlite3.Connection) -> None:
    if "owner" not in {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}:
        conn.execute(
            "ALTER TABLE documents ADD COLUMN owner TEXT NOT NULL DEFAULT 'you' "
            "CHECK (owner IN ('you', 'spouse'))"
        )


# Each step brings a ledger from schema n to n + 1, after the tables a later
# schema adds are created. v0.1.0 shipped schema 4; a new schema adds its step
# here and tests/test_upgrade.py opens the old release's data/ through it.
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    3: _to_4,
    4: _to_5,
}


def _version(conn: sqlite3.Connection) -> int | None:
    try:
        row = conn.execute("SELECT version FROM schema_version").fetchone()
    except sqlite3.OperationalError:
        return None
    return None if row is None else int(row[0])


def _too_new(found: int) -> LedgerTooNew:
    return LedgerTooNew(
        f"the ledger was written by a newer planner (schema {found}; this one "
        f"reads {SCHEMA_VERSION}). Use that release, or restore a backup this "
        "release made; nothing was changed"
    )


def connect(path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the ledger, bringing an older one up to date.

    A ledger from an older schema is first copied to ``planner.db.schemaN.bak``
    beside it, then taken through :data:`MIGRATIONS`. One from a newer planner
    is refused with :class:`LedgerTooNew` and left byte for byte as it was."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.is_file()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    found = _version(conn) if existed else None
    if found is not None and found > SCHEMA_VERSION:
        conn.close()
        raise _too_new(found)
    if found is not None and found < SCHEMA_VERSION:
        backup = path.with_name(f"{path.name}.schema{found}.bak")
        if not backup.exists():
            with sqlite3.connect(backup) as copy:
                conn.backup(copy)
            copy.close()
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    for step in range(found if found is not None else SCHEMA_VERSION, SCHEMA_VERSION):
        if step in MIGRATIONS:
            MIGRATIONS[step](conn)
    if conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0:
        conn.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
    conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
    conn.commit()
    return conn


def connect_readonly(path: Path) -> sqlite3.Connection | None:
    """Open the ledger for reading only, or ``None`` when there is none yet.

    Never creates the file, applies the schema or commits, so it takes no write
    lock and runs beside a writer. A ledger from an older schema is refused
    with :class:`LedgerOutOfDate`; any writing command brings it up to date.
    One from a newer planner is refused with :class:`LedgerTooNew`."""
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    found = _version(conn)
    if found is not None and found > SCHEMA_VERSION:
        conn.close()
        raise _too_new(found)
    if found != SCHEMA_VERSION:
        conn.close()
        raise LedgerOutOfDate(
            "the ledger was written by an older planner; run a writing command "
            "(for example `planner derive`) to bring it up to date"
        )
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
    facts: list[Fact] | None = None,
    rows: list[Row] | None = None,
    owner: str = "you",
) -> int:
    """Insert one document with its facts and rows in a single transaction.

    A newer document for the same (form, issuer, year) and ``owner`` supersedes
    the older one's accepted facts: a corrected 1099 replaces the original, but
    a spouse's W-2 from the same employer stands beside the head's. A row whose
    key is already in the ledger (a broker transaction id seen in an earlier
    export) is skipped, so overlapping exports do not double-count.
    """
    if owner not in OWNERS:
        raise ValueError(f"a document is {' or '.join(OWNERS)}'s, not {owner!r}")
    facts = facts or []
    rows = rows or []
    with conn:
        cur = conn.execute(
            "INSERT INTO documents (fingerprint, file_name, kind, pages, "
            "imported_at, batch, owner) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                fingerprint,
                file_name,
                kind,
                pages,
                datetime.now(UTC).isoformat(timespec="seconds"),
                batch,
                owner,
            ),
        )
        doc_id = int(cur.lastrowid or 0)
        accepted = [f for f in facts if f.status == "accepted"]
        for key in {(f.form, f.tax_year, f.issuer) for f in accepted}:
            supersede(conn, key, owner)
        conn.executemany(
            "INSERT INTO facts (document_id, form, tax_year, issuer, box, label, "
            "value_cents, page, status, value_text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    f.text,
                )
                for f in facts
            ],
        )
        conn.executemany(
            "INSERT OR IGNORE INTO rows (document_id, source, kind, row_key, account, "
            "date, tax_year, type, description, symbol, quantity, price_cents, "
            "amount_cents, basis_cents, acquired, term, line, raw) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    doc_id,
                    r.source,
                    r.kind,
                    r.row_key,
                    r.account,
                    r.date,
                    r.tax_year,
                    r.type,
                    r.description,
                    r.symbol,
                    r.quantity,
                    r.price_cents,
                    r.amount_cents,
                    r.basis_cents,
                    r.acquired,
                    r.term,
                    r.line,
                    r.raw,
                )
                for r in rows
            ],
        )
    return doc_id


def replace_derived(
    conn: sqlite3.Connection,
    form: str,
    year: int,
    lines: dict[str, float],
    labels: dict[str, str],
    name: str,
    issuer: str = "planner",
) -> bool:
    """Replace the planner's own ``form`` facts for a year with ``lines``
    (dollars) as one derived document named ``name``; the old facts are kept as
    superseded. False, and nothing written, when the lines are unchanged."""
    if {f.box: f.value for f in facts_for(conn, year, form)} == lines:
        return False
    with conn:
        conn.execute(
            "UPDATE facts SET status = 'superseded' WHERE form = ? AND tax_year = ? "
            "AND status = 'accepted'",
            (form, year),
        )
    if lines:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        add_document(
            conn,
            fingerprint=f"{name.lower().replace(' ', '-')}:{stamp}",
            file_name=name,
            kind="derived",
            pages=0,
            batch=stamp,
            facts=[
                Fact(form, year, issuer, line, labels[line], value, 0)
                for line, value in lines.items()
            ],
        )
    return True


def supersede(
    conn: sqlite3.Connection,
    key: tuple[str, int, str],
    owner: str,
    keep: int | None = None,
) -> None:
    """Retire the accepted facts of one (form, tax year, issuer) that ``owner``'s
    documents hold, except document ``keep``'s. The caller holds the transaction."""
    conn.execute(
        "UPDATE facts SET status = 'superseded' WHERE form = ? AND tax_year = ? "
        "AND issuer = ? AND status = 'accepted' AND document_id != ? AND "
        "document_id IN (SELECT id FROM documents WHERE owner = ?)",
        (*key, -1 if keep is None else keep, owner),
    )


def set_owner(conn: sqlite3.Connection, name: str, owner: str) -> int:
    """Make the document named ``name`` (its file name, or its archived path
    when two share a name) ``owner``'s, and settle which copy of each of its
    forms stands: per owner, the newest document's facts are accepted and the
    older ones superseded. Pending facts wait for ``planner confirm`` as before.
    Returns the document's id."""
    if owner not in OWNERS:
        raise ValueError(f"a document is {' or '.join(OWNERS)}'s, not {owner!r}")
    found = conn.execute(
        "SELECT id FROM documents WHERE file_name = ? OR archived_as = ?",
        (name, name),
    ).fetchall()
    if not found:
        raise KeyError(f"no document named {name!r} (planner facts lists them)")
    if len(found) > 1:
        raise KeyError(
            f"more than one document is named {name!r}: give its archived path "
            "(data/archive/...) instead"
        )
    doc_id = int(found[0]["id"])
    with conn:
        conn.execute("UPDATE documents SET owner = ? WHERE id = ?", (owner, doc_id))
        keys = conn.execute(
            "SELECT DISTINCT form, tax_year, issuer FROM facts WHERE document_id = ? "
            "AND status IN ('accepted', 'superseded')",
            (doc_id,),
        ).fetchall()
        for k in keys:
            key = (k["form"], k["tax_year"], k["issuer"])
            conn.execute(
                "UPDATE facts SET status = 'accepted' WHERE form = ? AND tax_year = ? "
                "AND issuer = ? AND status = 'superseded' AND document_id IN "
                "(SELECT MAX(f.document_id) FROM facts f JOIN documents d ON "
                "d.id = f.document_id WHERE f.form = ? AND f.tax_year = ? AND "
                "f.issuer = ? AND f.status IN ('accepted', 'superseded') "
                "GROUP BY d.owner)",
                (*key, *key),
            )
            for who in OWNERS:
                newest = conn.execute(
                    "SELECT MAX(f.document_id) FROM facts f JOIN documents d ON "
                    "d.id = f.document_id WHERE f.form = ? AND f.tax_year = ? AND "
                    "f.issuer = ? AND f.status = 'accepted' AND d.owner = ?",
                    (*key, who),
                ).fetchone()[0]
                if newest is not None:
                    supersede(conn, key, who, keep=int(newest))
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
    text: bool | None = False,
) -> list[FactRow]:
    """Facts of one status. ``text`` picks the kind: False (default) the money
    facts every total sums, True the text facts, None both."""
    sql = (
        "SELECT f.*, d.file_name, d.owner FROM facts f "
        "JOIN documents d ON d.id = f.document_id "
        "WHERE f.status = ?"
    )
    args: list[object] = [status]
    if text is not None:
        sql += f" AND f.value_text IS {'NOT ' if text else ''}NULL"
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
            text=r["value_text"],
            document_id=r["document_id"],
            file_name=r["file_name"],
            id=r["id"],
            owner=r["owner"],
        )
        for r in conn.execute(sql, args)
    ]


def _row(r: sqlite3.Row) -> LedgerRow:
    return LedgerRow(
        source=r["source"],
        kind=r["kind"],
        row_key=r["row_key"],
        account=r["account"],
        date=r["date"],
        tax_year=r["tax_year"],
        type=r["type"],
        description=r["description"],
        symbol=r["symbol"],
        quantity=r["quantity"],
        price_cents=r["price_cents"],
        amount_cents=r["amount_cents"],
        basis_cents=r["basis_cents"],
        acquired=r["acquired"],
        term=r["term"],
        line=r["line"],
        raw=r["raw"],
        document_id=r["document_id"],
        file_name=r["file_name"],
    )


def rows_for(
    conn: sqlite3.Connection,
    tax_year: int | None = None,
    source: str | None = None,
    kind: str | None = None,
) -> list[LedgerRow]:
    sql = "SELECT r.*, d.file_name FROM rows r JOIN documents d ON d.id = r.document_id"
    clauses: list[str] = []
    args: list[object] = []
    for col, val in (("tax_year", tax_year), ("source", source), ("kind", kind)):
        if val is not None:
            clauses.append(f"r.{col} = ?")
            args.append(val)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY r.date, r.source, r.line"
    return [_row(r) for r in conn.execute(sql, args)]


def latest_rows(conn: sqlite3.Connection, kind: str) -> list[LedgerRow]:
    """The rows of one kind from each account's most recent import: a holdings
    or cost-basis export is a snapshot, so only the newest one per account
    describes the account now."""
    sql = (
        "SELECT r.*, d.file_name FROM rows r JOIN documents d ON d.id = r.document_id "
        "WHERE r.kind = ? AND r.document_id = ("
        "  SELECT r2.document_id FROM rows r2"
        "  JOIN documents d2 ON d2.id = r2.document_id"
        "  WHERE r2.kind = r.kind AND r2.account = r.account"
        "  ORDER BY d2.imported_at DESC, d2.id DESC LIMIT 1) "
        "ORDER BY r.account, r.symbol, r.line"
    )
    return [_row(r) for r in conn.execute(sql, (kind,))]


@dataclass(frozen=True)
class Conversion:
    id: int
    date: str
    amount: float
    taxable: float
    source_account: str
    accessible_date: str


def add_conversion(
    conn: sqlite3.Connection,
    *,
    date: str,
    amount_cents: int,
    taxable_cents: int,
    source_account: str,
    accessible_date: str,
) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO conversions (date, amount_cents, taxable_cents, "
            "source_account, accessible_date, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                date,
                amount_cents,
                taxable_cents,
                source_account,
                accessible_date,
                datetime.now(UTC).isoformat(timespec="seconds"),
            ),
        )
    return int(cur.lastrowid or 0)


def conversions(conn: sqlite3.Connection) -> list[Conversion]:
    return [
        Conversion(
            id=r["id"],
            date=r["date"],
            amount=from_cents(r["amount_cents"]),
            taxable=from_cents(r["taxable_cents"]),
            source_account=r["source_account"],
            accessible_date=r["accessible_date"],
        )
        for r in conn.execute("SELECT * FROM conversions ORDER BY date, id")
    ]


def record_peak(
    conn: sqlite3.Connection, total_cents: int, as_of: str
) -> tuple[int, str]:
    """Persist the all-time high; returns (peak_cents, date it was set)."""
    row = conn.execute("SELECT total_cents, as_of FROM peak WHERE id = 1").fetchone()
    if row is not None and row["total_cents"] >= total_cents:
        return int(row["total_cents"]), str(row["as_of"])
    with conn:
        conn.execute(
            "INSERT INTO peak (id, total_cents, as_of) VALUES (1, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET total_cents = excluded.total_cents, "
            "as_of = excluded.as_of",
            (total_cents, as_of),
        )
    return total_cents, as_of
