"""Estimated tax: the safe harbor, the four installments, what was paid and
when, the next due date and amount. Federal and NC separately. A payment is
credited to the installment whose window it falls in, so a late payment never
cures an earlier shortfall. The Form 2210 penalty itself is reported as
unavailable."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from planner.engine.tax import r
from planner.ingest.needs import load_manual, manual_path, need_value
from planner.ledger import db
from planner.paths import Layout
from planner.plan.inputs import Overrides
from planner.plan.magi import project

PAYMENTS = "payments"  # top-level list in the year's manual file
AGENCIES = ("fed", "nc")
DESCRIPTIONS = {
    "fed": re.compile(r"IRS|USATAXPYMT|US TREASURY|EFTPS", re.I),
    "nc": re.compile(r"NCDOR|NC ?DOR|N\.?C\.? DEPT\.? OF REV|NC DEPT REVENUE", re.I),
}
DE_MINIMIS = {"fed": 1_000.0, "nc": 1_000.0}
HIGH_INCOME_AGI = 150_000.0
HIGH_INCOME_FACTOR = {"fed": 1.10, "nc": 1.00}
LUMPY_SHARE = 0.50


def due_dates(year: int) -> list[date]:
    return [
        date(year, 4, 15),
        date(year, 6, 15),
        date(year, 9, 15),
        date(year + 1, 1, 15),
    ]


@dataclass(frozen=True)
class Payment:
    agency: str
    date: str
    amount: float
    origin: str  # "bank <file>" | "typed"

    @property
    def installment(self) -> int:
        """1-4: the first installment whose due date is on or after the date."""
        paid = date.fromisoformat(self.date)
        for n, due in enumerate(
            due_dates(paid.year if paid.month > 1 else paid.year - 1), 1
        ):
            if paid <= due:
                return n
        return 4


@dataclass(frozen=True)
class Installment:
    n: int
    due: str
    required: float  # cumulative through this installment
    paid: float  # cumulative, dated on or before the due date
    shortfall: float


@dataclass
class Agency:
    name: str
    current_tax: float
    prior_tax: float | None
    prior_agi: float | None
    required: float  # the safe-harbor annual figure, after withholding
    basis: str
    withheld: float
    de_minimis: bool
    payments: list[Payment] = field(default_factory=list)
    installments: list[Installment] = field(default_factory=list)
    next_due: str | None = None
    next_amount: float = 0.0
    penalty: float | None = None  # Form 2210: unavailable
    notes: list[str] = field(default_factory=list)


@dataclass
class EstTax:
    year: int
    as_of: str
    agi: float
    annualized: bool
    agencies: list[Agency] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def record(lay: Layout, year: int, agency: str, paid: str, amount: float) -> Payment:
    """Type a payment the bank export does not show (or did not match)."""
    if agency not in AGENCIES:
        raise ValueError(f"agency must be one of {', '.join(AGENCIES)}")
    date.fromisoformat(paid)
    data = load_manual(lay, year)
    data.setdefault(PAYMENTS, []).append(
        {"agency": agency, "date": paid, "amount": float(amount)}
    )
    from planner.ingest.needs import _write

    _write(manual_path(lay, year), data)
    return Payment(agency, paid, float(amount), "typed")


def payments(conn: sqlite3.Connection, lay: Layout, year: int) -> list[Payment]:
    """Bank withdrawals to the IRS or NCDOR inside the year's windows, plus the
    typed ones; the January window belongs to the prior tax year."""
    out: list[Payment] = []
    lo, hi = date(year, 1, 16), date(year + 1, 1, 15)
    for row in db.rows_for(conn, kind="bank"):
        if row.date is None or row.amount_cents is None or row.amount_cents >= 0:
            continue
        when = date.fromisoformat(row.date)
        if not lo <= when <= hi:
            continue
        for agency, pat in DESCRIPTIONS.items():
            if pat.search(row.description or ""):
                out.append(
                    Payment(
                        agency,
                        row.date,
                        db.from_cents(-row.amount_cents),
                        f"bank {row.file_name}",
                    )
                )
    for item in load_manual(lay, year).get(PAYMENTS, []):
        out.append(
            Payment(
                str(item["agency"]), str(item["date"]), float(item["amount"]), "typed"
            )
        )
    return sorted(out, key=lambda p: (p.date, p.agency))


def safe_harbor(
    name: str, current: float, prior: float | None, prior_agi: float | None
) -> tuple[float, str]:
    """The lesser of 90% of this year's tax and 100% of last year's (110% when
    last year's AGI topped 150,000); 90% of current when last year is unknown."""
    cur = r(0.9 * current)
    if prior is None:
        return cur, "90% of this year's projected tax (prior year unknown)"
    factor = (
        HIGH_INCOME_FACTOR[name]
        if prior_agi is not None and prior_agi > HIGH_INCOME_AGI
        else 1.0
    )
    pri = r(factor * prior)
    if pri <= cur:
        return pri, f"{factor:.0%} of last year's tax"
    return cur, "90% of this year's projected tax"


def _quarters(conn: sqlite3.Connection, year: int) -> list[float]:
    """Bank deposits by quarter: the lumpiness test for the annualized flag."""
    q = [0, 0, 0, 0]
    for row in db.rows_for(conn, year, kind="bank"):
        if row.date and row.amount_cents and row.amount_cents > 0:
            q[(int(row.date[5:7]) - 1) // 3] += row.amount_cents
    return [db.from_cents(c) for c in q]


def _agency(
    name: str,
    current: float,
    prior: float | None,
    prior_agi: float | None,
    withheld: float,
    paid: list[Payment],
    year: int,
    as_of: date,
) -> Agency:
    annual, basis = safe_harbor(name, current, prior, prior_agi)
    required = r(max(annual - withheld, 0.0))
    de_minimis = r(current - withheld) < DE_MINIMIS[name]
    ag = Agency(name, current, prior, prior_agi, required, basis, withheld, de_minimis)
    ag.payments = [p for p in paid if p.agency == name]
    for n, due in enumerate(due_dates(year), 1):
        req = r(required * n / 4) if not de_minimis else 0.0
        by_due = r(
            sum(p.amount for p in ag.payments if date.fromisoformat(p.date) <= due)
        )
        ag.installments.append(
            Installment(n, due.isoformat(), req, by_due, r(max(req - by_due, 0.0)))
        )
    total_paid = r(sum(p.amount for p in ag.payments))
    for inst in ag.installments:
        if date.fromisoformat(inst.due) >= as_of:
            ag.next_due = inst.due
            ag.next_amount = r(max(inst.required - total_paid, 0.0))
            break
    if de_minimis:
        ag.notes.append(
            f"{name}: tax after withholding under {DE_MINIMIS[name]:,.0f}; "
            "no estimated payments required"
        )
    missed = [
        i for i in ag.installments if i.shortfall and date.fromisoformat(i.due) < as_of
    ]
    if missed:
        ag.notes.append(
            f"{name}: installment(s) {', '.join(str(i.n) for i in missed)} were short "
            "on their due dates; a later payment does not cure that"
        )
    ag.notes.append(
        f"{name}: Form 2210 underpayment penalty not computed (unavailable)"
    )
    return ag


def estimate(
    lay: Layout,
    year: int,
    as_of: date | None = None,
    overrides: Overrides | None = None,
) -> EstTax:
    today = as_of or date.today()
    pj = project(lay, year, overrides)
    res = pj.result
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        paid = payments(conn, lay, year)
        quarters = _quarters(conn, year)
        prior: dict[str, Any] = {
            k: need_value(conn, lay, year, k)
            for k in ("prior_agi", "prior_total_tax", "prior_nc_tax")
        }
        withheld = {
            "fed": float(need_value(conn, lay, year, "fed_withheld") or 0),
            "nc": float(need_value(conn, lay, year, "nc_withheld") or 0),
        }
    finally:
        conn.close()
    ov = overrides or Overrides()
    lump = ov.planned_conversion + ov.planned_lt_sales + ov.planned_st_sales
    total_dep = sum(quarters)
    lumpy = (total_dep > 0 and max(quarters) / total_dep > LUMPY_SHARE) or (
        res.agi > 0 and lump / res.agi > 1 - LUMPY_SHARE
    )
    et = EstTax(year, today.isoformat(), res.agi, lumpy)
    for name, current, prior_key in (
        ("fed", res.fed_total_tax, "prior_total_tax"),
        ("nc", res.state_tax, "prior_nc_tax"),
    ):
        pri = prior[prior_key]
        et.agencies.append(
            _agency(
                name,
                current,
                float(pri) if pri is not None else None,
                float(prior["prior_agi"]) if prior["prior_agi"] is not None else None,
                withheld[name],
                paid,
                year,
                today,
            )
        )
    if lumpy:
        et.notes.append(
            "income is lumpy (over half in one quarter, or a planned year-end "
            "lump): the annualized method (Schedule AI) may cut an earlier "
            "installment's shortfall; not computed"
        )
    for key in ("fed_withheld", "nc_withheld"):
        if key not in pj.inputs.origins:
            et.notes.append(f"{key} unknown: counted as zero")
    if pj.inputs.unknown:
        et.notes.append("projection leaves out: " + ", ".join(pj.inputs.unknown))
    return et
