"""``planner derive``: year-to-date facts computed from the imported CSV rows.

The broker's 1099 arrives in February; until then the only picture of the year is
the rows already in the ledger. This module folds them into facts under the form
``YTD`` (issuer = the CSV source that produced them) so the engine and the Needed
panel read one table whether a figure came from a form or from rows. Every run
recomputes the year and supersedes the previous run's facts, so a later export
corrects an earlier estimate. Nothing is inferred: a figure with no rows behind it
is simply absent.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import UTC, datetime

from planner.ledger import db

FORM = "YTD"

# (box, label): the realized boxes mirror the 1099-B summary template so the two
# can be compared line by line once the form arrives.
LABELS = {
    "st_proceeds": "Short-term proceeds (YTD)",
    "st_basis": "Short-term cost basis (YTD)",
    "st_gain": "Short-term gain or loss (YTD)",
    "lt_proceeds": "Long-term proceeds (YTD)",
    "lt_basis": "Long-term cost basis (YTD)",
    "lt_gain": "Long-term gain or loss (YTD)",
    "dividends": "Dividends (YTD)",
    "interest": "Interest (YTD)",
    "capital_gain_distributions": "Capital gain distributions (YTD)",
    "deposits": "Bank deposits (YTD)",
    "withdrawals": "Bank withdrawals (YTD)",
}


def _classify_income(kind: str, typ: str) -> str | None:
    low = typ.lower()
    if "capital gain" in low:
        return "capital_gain_distributions"
    if "dividend" in low and "reinvest" not in low:
        return "dividends"
    if "interest" in low:
        return "interest"
    return None


def derive_year(rows: list[db.LedgerRow], year: int) -> list[db.Fact]:
    """Fold one year's rows into YTD facts, one set per CSV source.

    Realized sales come from ``realized`` rows (term short/long). Dividends and
    interest come from ``income`` rows when any income export covers the year,
    else from ``transaction`` rows typed Dividend/Interest, so a download and an
    income export for the same year never double-count. Bank rows give deposits
    (positive) and withdrawals (negative, reported as a positive figure).
    """
    cents: dict[tuple[str, str], int] = defaultdict(int)
    has_income = any(r.kind == "income" and r.tax_year == year for r in rows)
    for r in rows:
        if r.tax_year != year or r.amount_cents is None:
            continue
        if r.kind == "realized" and r.term in ("short", "long"):
            pre = "st" if r.term == "short" else "lt"
            cents[(r.source, f"{pre}_proceeds")] += r.amount_cents
            if r.basis_cents is not None:
                cents[(r.source, f"{pre}_basis")] += r.basis_cents
        elif r.kind == "income" or (r.kind == "transaction" and not has_income):
            box = _classify_income(r.kind, r.type)
            if box is not None:
                cents[(r.source, box)] += r.amount_cents
        elif r.kind == "bank":
            box = "deposits" if r.amount_cents > 0 else "withdrawals"
            cents[(r.source, box)] += abs(r.amount_cents)
    for source in {s for s, _ in cents}:
        for pre in ("st", "lt"):
            if (source, f"{pre}_proceeds") in cents and (
                source,
                f"{pre}_basis",
            ) in cents:
                cents[(source, f"{pre}_gain")] = (
                    cents[(source, f"{pre}_proceeds")] - cents[(source, f"{pre}_basis")]
                )
    return [
        db.Fact(
            form=FORM,
            tax_year=year,
            issuer=source,
            box=box,
            label=LABELS[box],
            value=db.from_cents(value),
            page=0,
        )
        for (source, box), value in sorted(cents.items())
    ]


def derive(conn: sqlite3.Connection, year: int, batch: str | None = None) -> int:
    """Recompute and store the YTD facts for ``year``; returns how many were written.

    The facts hang off a synthetic ``derived`` document so the ledger's one
    source-per-fact rule holds; each run is a new document and supersedes the
    last run's accepted YTD facts for the same year and source.
    """
    facts = derive_year(db.rows_for(conn, year), year)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    with conn:
        conn.execute(
            "UPDATE facts SET status = 'superseded' WHERE form = ? AND tax_year = ? "
            "AND status = 'accepted'",
            (FORM, year),
        )
    if facts:
        db.add_document(
            conn,
            fingerprint=f"derived:{year}:{stamp}",
            file_name=f"derived {year}",
            kind="derived",
            pages=0,
            batch=batch or stamp,
            facts=facts,
        )
    return len(facts)


def row_years(conn: sqlite3.Connection) -> list[int]:
    return [
        int(r["tax_year"])
        for r in conn.execute(
            "SELECT DISTINCT tax_year FROM rows WHERE tax_year IS NOT NULL "
            "ORDER BY tax_year"
        )
    ]
