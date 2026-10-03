"""Wash sales: any buy of a symbol within 30 days either side of a loss sale,
across every account including IRAs (Rev. Rul. 2008-5), flagged from the
ledger's realized sales and transactions. Dividend reinvestments are buys."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from planner.engine.tax import r
from planner.ledger import db

WINDOW = 30
NOT_A_BUY = ("sell", "dividend", "interest", "capital gain", "sweep", "transfer")


@dataclass(frozen=True)
class LossSale:
    symbol: str
    date: str
    account: str
    loss: float  # positive dollars


@dataclass(frozen=True)
class Buy:
    symbol: str
    date: str
    account: str
    quantity: float
    type: str


@dataclass(frozen=True)
class Flag:
    sale: LossSale
    buy: Buy

    @property
    def days(self) -> int:
        return (
            date.fromisoformat(self.buy.date) - date.fromisoformat(self.sale.date)
        ).days


def loss_sales(conn: sqlite3.Connection, year: int | None = None) -> list[LossSale]:
    out: list[LossSale] = []
    for row in db.rows_for(conn, year, kind="realized"):
        if row.date is None or row.amount_cents is None or row.basis_cents is None:
            continue
        gain = row.amount_cents - row.basis_cents
        if gain < 0 and row.symbol:
            out.append(
                LossSale(row.symbol, row.date, row.account, db.from_cents(-gain))
            )
    return out


def buys(conn: sqlite3.Connection) -> list[Buy]:
    out: list[Buy] = []
    for row in db.rows_for(conn, kind="transaction"):
        low = row.type.lower()
        if not row.symbol or row.date is None or (row.quantity or 0) <= 0:
            continue
        if any(word in low for word in NOT_A_BUY) and "reinvest" not in low:
            continue
        out.append(
            Buy(row.symbol, row.date, row.account, row.quantity or 0.0, row.type)
        )
    return out


def check(conn: sqlite3.Connection, year: int | None = None) -> list[Flag]:
    """Every loss sale paired with each buy of the same symbol inside the window."""
    purchases = buys(conn)
    flags: list[Flag] = []
    for sale in loss_sales(conn, year):
        sold = date.fromisoformat(sale.date)
        for buy in purchases:
            if buy.symbol != sale.symbol:
                continue
            if abs((date.fromisoformat(buy.date) - sold).days) <= WINDOW:
                flags.append(Flag(sale, buy))
    return sorted(flags, key=lambda f: (f.sale.date, f.sale.symbol, f.buy.date))


def open_windows(conn: sqlite3.Connection, as_of: date) -> list[tuple[str, str, float]]:
    """Symbols sold at a loss in the last 30 days: (symbol, no buys until, loss)."""
    out: list[tuple[str, str, float]] = []
    for sale in loss_sales(conn):
        sold = date.fromisoformat(sale.date)
        until = sold + timedelta(days=WINDOW + 1)
        if sold <= as_of < until:
            out.append((sale.symbol, until.isoformat(), r(sale.loss)))
    return sorted(set(out))
