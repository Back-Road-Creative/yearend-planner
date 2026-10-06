"""Estimated tax: the safe harbor, the four installments, what was paid and
when, the next due date and amount. Federal and the household's state (when
it taxes income) separately, each on its own installment rules (``states``:
the dates, shares, de minimis and safe harbor from the state's instructions;
a state without its own rules in the planner is on the federal ones, and says
so). A payment is
credited to the installment whose window it falls in, so a late payment never
cures an earlier shortfall. The federal Form 2210 penalty is the lower of the
regular method (``penalty``) and the annualized method (Schedule AI, when
income came unevenly or a planned year-end item is entered) on the payments
made plus the plan's later installments; the states' own penalties are
reported as unavailable."""

from __future__ import annotations

import calendar
import re
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any

from planner import states
from planner.engine.tax import compute, r
from planner.ingest.needs import load_manual, manual_path, need_value
from planner.ledger import db
from planner.paths import Layout
from planner.plan.calendar import shift
from planner.plan.inputs import UNKNOWN, Overrides, build
from planner.plan.magi import project
from planner.taxprep import schedule_ai

PAYMENTS = "payments"  # top-level list in the year's manual file
FED = "fed"
FED_PAYEE = re.compile(r"IRS|USATAXPYMT|US TREASURY|EFTPS", re.I)
LUMPY_SHARE = 0.50
# IRC 6621(a)(2) underpayment rate for each calendar quarter as the IRS
# publishes it (irs.gov/payments/quarterly-interest-rates; IRB 2023-49,
# 2024-10, 2024-24, 2024-37, 2024-49, 2025-13, 2025-23, 2025-37, 2025-48,
# 2026-08, 2026-22 and 2026-36). IRC 6654(a) charges the rate of each quarter
# the underpayment is outstanding, so a Form 2210 rate period that spans a
# change (2025's January 1-April 15, 2026, printed at 7% before April's 6%
# was set) is split at the quarter.
UNDERPAYMENT_RATES: dict[tuple[int, int], float] = {
    (2024, 1): 0.08,
    (2024, 2): 0.08,
    (2024, 3): 0.08,
    (2024, 4): 0.08,
    (2025, 1): 0.07,
    (2025, 2): 0.07,
    (2025, 3): 0.07,
    (2025, 4): 0.07,
    (2026, 1): 0.07,
    (2026, 2): 0.06,
    (2026, 3): 0.07,
    (2026, 4): 0.07,
}


def agencies(state: str) -> tuple[str, ...]:
    """fed, then the state's key ("nc") when the state taxes income; a value
    that is not a state code adds none (the coverage gate names it)."""
    st = states.STATES.get(state.upper()) if state else None
    return (FED, st.agency) if st is not None and st.income_tax else (FED,)


def rules(agency: str) -> tuple[states.EstRules, bool]:
    """The agency's installment rules, and whether they are its own."""
    return (states.FEDERAL, True) if agency == FED else states.rules(agency)


def withheld_key(agency: str) -> str:
    """The Needed item holding the agency's withholding for the year."""
    return {FED: "fed_withheld", "nc": "nc_withheld"}.get(agency, "state_withheld")


def prior_key(agency: str) -> str:
    """The Needed item holding the agency's prior-year tax."""
    return {FED: "prior_total_tax", "nc": "prior_nc_tax"}.get(agency, "prior_state_tax")


def schedule(year: int, agency: str = FED) -> list[tuple[int, date, float]]:
    """(number, due date, cumulative share) of the agency's installments for
    tax year ``year``, each date moved to the next business day when it falls
    on a weekend or a federal holiday; an installment with no share of its own
    (CA's September) is left out."""
    return [
        (n, shift(date(year + off, m, d)), share)
        for n, (off, m, d), share in rules(agency)[0].installments()
    ]


def due_dates(year: int, agency: str = FED) -> list[date]:
    """The agency's installment due dates for tax year ``year``."""
    return [due for _, due, _ in schedule(year, agency)]


def de_minimis(agency: str, owe: float, separate: bool = False) -> bool:
    """True when the tax after withholding is too small for installments: under
    the agency's figure, or not over it where its rule says "more than"."""
    rule = rules(agency)[0]
    limit = rule.threshold(separate)
    owe = r(owe)
    return owe <= limit if rule.over else owe < limit


@dataclass(frozen=True)
class Payment:
    agency: str
    date: str
    amount: float
    origin: str  # "bank <file>" | "typed"

    @property
    def installment(self) -> int:
        """1-4: the first installment whose due date is on or after the date
        (the last one after them all)."""
        paid = date.fromisoformat(self.date)
        dated = schedule(paid.year if paid.month > 1 else paid.year - 1, self.agency)
        for n, due, _ in dated:
            if paid <= due:
                return n
        return dated[-1][0]


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
    penalty: float | None = None  # Form 2210 (federal); None: unavailable
    schedule_ai: list[float] | None = None  # Schedule AI line 27, when figured
    notes: list[str] = field(default_factory=list)
    separate: bool = False  # married filing separately: the halved AGI lines


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
    st = states.STATES.get(agency.upper())
    if agency != FED and (st is None or not st.income_tax or agency != st.agency):
        raise ValueError(
            "agency must be fed or a state that taxes income, lowercase (nc, ca)"
        )
    date.fromisoformat(paid)
    data = load_manual(lay, year)
    data.setdefault(PAYMENTS, []).append(
        {"agency": agency, "date": paid, "amount": float(amount)}
    )
    from planner.ingest.needs import _write

    _write(manual_path(lay, year), data)
    return Payment(agency, paid, float(amount), "typed")


def payments(conn: sqlite3.Connection, lay: Layout, year: int) -> list[Payment]:
    """Bank withdrawals to the IRS or a state revenue department the planner
    knows by name (``states``: NCDOR) inside the year's windows, plus the typed
    ones; the January window belongs to the prior tax year."""
    payees = {FED: FED_PAYEE}
    for st in states.STATES.values():
        pat = states.payee(st.code)
        if pat is not None:
            payees[st.agency] = pat
    out: list[Payment] = []
    lo, hi = date(year, 1, 16), date(year + 1, 1, 15)
    for row in db.rows_for(conn, kind="bank"):
        if row.date is None or row.amount_cents is None or row.amount_cents >= 0:
            continue
        when = date.fromisoformat(row.date)
        if not lo <= when <= hi:
            continue
        for agency, pat in payees.items():
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
    name: str,
    current: float,
    prior: float | None,
    prior_agi: float | None,
    *,
    separate: bool = False,
    agi: float | None = None,
) -> tuple[float, str]:
    """The lesser of the agency's share of this year's tax (90%; NJ 80%, GA 70%)
    and 100% of last year's (110% where the agency steps up once last year's
    AGI topped 150,000, 75,000 married filing separately: IRC 6654(d)(1)(C));
    this year's share alone when last year is unknown, or when this year's
    ``agi`` reaches the agency's line for it (CA: 1,000,000)."""
    rule = rules(name)[0]
    half = 2 if separate else 1
    cur = r(rule.current_factor * current)
    pct = f"{rule.current_factor:.0%} of this year's projected tax"
    if prior is None:
        return cur, f"{pct} (prior year unknown)"
    if (
        rule.current_only_agi is not None
        and agi is not None
        and agi >= rule.current_only_agi / half
    ):
        return cur, (
            f"{pct} (this year's AGI is over the line past which last year's "
            "tax is not a safe harbor)"
        )
    factor = (
        rule.high_income_factor
        if prior_agi is not None and prior_agi > rule.high_income_agi / half
        else 1.0
    )
    pri = r(factor * prior)
    if pri <= cur:
        return pri, f"{factor:.0%} of last year's tax"
    return cur, pct


@dataclass(frozen=True)
class Penalty:
    amount: float
    underpaid: list[float]  # Form 2210 line 17, each installment
    notes: list[str]


def _interest(
    amount: float, start: date, end: date, missing: set[tuple[int, int]]
) -> float:
    """``amount`` unpaid from ``start`` to ``end`` at each calendar quarter's
    rate, its days counted from the later of ``start`` and the day before the
    quarter to the earlier of ``end`` and the quarter's last day, over the
    calendar year's 365 or 366 (the penalty worksheet's lines 3-13). A quarter
    the IRS has not published takes the last published rate, added to
    ``missing``."""
    total = 0.0
    y, q = start.year, (start.month - 1) // 3 + 1
    while True:
        first = date(y, 3 * q - 2, 1)
        last = (date(y + 1, 1, 1) if q == 4 else date(y, 3 * q + 1, 1)) - timedelta(
            days=1
        )
        days = (min(end, last) - max(start, first - timedelta(days=1))).days
        if days > 0:
            rate = UNDERPAYMENT_RATES.get((y, q))
            if rate is None:
                missing.add((y, q))
                rate = UNDERPAYMENT_RATES[max(UNDERPAYMENT_RATES)]
            total += amount * rate * days / (366 if calendar.isleap(y) else 365)
        if end <= last:
            return total
        y, q = (y + 1, 1) if q == 4 else (y, q + 1)


def _settle(
    amount: float,
    when: date,
    shortfalls: list[list[Any]],
    end: date,
    missing: set[tuple[int, int]],
) -> tuple[float, float]:
    """Apply a payment to the earliest open shortfalls first: (what is left of
    it, the penalty on what it paid off)."""
    owed = 0.0
    for item in shortfalls:
        if amount <= 0:
            break
        pay = min(item[1], amount)
        if pay > 0:
            owed += _interest(pay, item[0], min(when, end), missing)
            item[1] -= pay
            amount -= pay
    shortfalls[:] = [s for s in shortfalls if s[1] > 0.005]
    return amount, owed


def penalty(
    year: int,
    agency: str,
    annual: float,
    withheld: float,
    paid: list[tuple[date, float]],
    required: list[float] | None = None,
) -> Penalty:
    """The Form 2210 penalty on the regular method (Part III and the penalty
    worksheet): each installment owes its share of ``annual`` (line 9; a
    quarter each federally), withholding counts the same share on each due
    date, a payment on the business day a weekend or holiday moved the date
    to is on time, a payment first pays off the earliest open shortfall and
    what is left carries forward, and each shortfall is charged from its due
    date until paid or the 15th of the following April (IRC 6654(a), (b)).
    ``required``: each installment's own amount instead (Schedule AI line 27,
    the annualized method)."""
    rule = rules(agency)[0]
    dated = schedule(year, agency)
    nominal = [date(year + off, m, d) for _, (off, m, d), _ in rule.installments()]
    shares = [share for _, _, share in dated]
    parts = [b - a for a, b in zip([0.0, *shares[:-1]], shares, strict=True)]
    end = date(year + 1, 4, 15)
    credits: list[tuple[date, float]] = []
    for when, amount in paid:
        for due, (_, moved, _) in zip(nominal, dated, strict=True):
            if due < when <= moved:
                when = due
                break
        credits.append((when, amount))
    for due, part in zip(nominal, parts, strict=True):
        credits.append((due, withheld * part))
    credits.sort(key=lambda c: c[0])
    shortfalls: list[list[Any]] = []
    missing: set[tuple[int, int]] = set()
    pool = owed = 0.0
    underpaid: list[float] = []
    i = 0
    owes = required or [annual * part for part in parts]
    for due, owe in zip(nominal, owes, strict=True):
        while i < len(credits) and credits[i][0] <= due:
            left, cost = _settle(credits[i][1], credits[i][0], shortfalls, end, missing)
            pool += left
            owed += cost
            i += 1
        short = max(owe - pool, 0.0)
        pool = max(pool - owe, 0.0)
        underpaid.append(r(short))
        if short > 0.005:
            shortfalls.append([due, short])
    for when, amount in credits[i:]:
        owed += _settle(amount, when, shortfalls, end, missing)[1]
    for start, amount in shortfalls:
        owed += _interest(amount, start, end, missing)
    notes = []
    if missing:
        quarters = ", ".join(f"{y} Q{q}" for y, q in sorted(missing))
        last = UNDERPAYMENT_RATES[max(UNDERPAYMENT_RATES)]
        notes.append(
            f"the IRS has not published the underpayment rate for {quarters}; "
            f"the last published {last:.0%} stands in"
        )
    return Penalty(r(owed), underpaid, notes)


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
    separate: bool = False,
    agi: float | None = None,
    periods: list[float] | None = None,
) -> Agency:
    rule, own = rules(name)
    annual, basis = safe_harbor(
        name, current, prior, prior_agi, separate=separate, agi=agi
    )
    required = r(max(annual - withheld, 0.0))
    small = de_minimis(name, current - withheld, separate)
    ag = Agency(name, current, prior, prior_agi, required, basis, withheld, small)
    ag.separate = separate
    ag.payments = [p for p in paid if p.agency == name]
    for n, due, share in schedule(year, name):
        req = r(required * share) if not small else 0.0
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
    if not own:
        ag.notes.append(
            f"{name}: Estimated: the state's own installment rules are not in the "
            "planner; the federal dates, shares, de minimis and safe harbor "
            "stand in"
        )
    ag.notes.extend(f"{name}: {note}" for note in rule.notes)
    if name != FED and states.payee(name) is None:
        ag.notes.append(
            f"{name}: bank payments to the state are not matched by name; type "
            f"each with planner paid --agency {name}"
        )
    if small:
        word = "not over" if rule.over else "under"
        ag.notes.append(
            f"{name}: tax after withholding {word} "
            f"{rule.threshold(separate):,.0f}; no estimated payments required"
        )
    missed = [
        i for i in ag.installments if i.shortfall and date.fromisoformat(i.due) < as_of
    ]
    if missed:
        ag.notes.append(
            f"{name}: installment(s) {', '.join(str(i.n) for i in missed)} were short "
            "on their due dates; a later payment does not cure that"
        )
    if name == FED:
        # the payments made, then the plan's own later installments on their
        # due dates (what cash_flows pays); the balance with the return is
        # paid on the April date the penalty stops at anyway
        credits = [(date.fromisoformat(p.date), p.amount) for p in ag.payments]
        running = total_paid
        for inst in ag.installments:
            if date.fromisoformat(inst.due) >= as_of:
                need = r(max(inst.required - running, 0.0))
                running = r(running + need)
                if need:
                    credits.append((date.fromisoformat(inst.due), need))
        pen = penalty(year, name, 0.0 if small else annual, withheld, credits)
        method = "regular"
        if periods is not None and pen.amount:
            ai_req = schedule_ai.installments(periods, annual)
            alt = penalty(year, name, annual, withheld, credits, ai_req)
            ag.schedule_ai = ai_req
            if alt.amount < pen.amount:
                ag.notes.append(
                    f"{name}: Form 2210 penalty {alt.amount:,.2f} on the annualized "
                    "method (Schedule AI): required installments "
                    + ", ".join(f"{x:,.2f}" for x in ai_req)
                    + f" (box C), under the regular method's {pen.amount:,.2f}; "
                    "the remaining installments paid in full on their due dates"
                )
                pen, method = alt, "annualized"
            else:
                ag.notes.append(
                    f"{name}: the annualized method (Schedule AI) gives "
                    f"{alt.amount:,.2f}, no lower than the regular method"
                )
        ag.penalty = pen.amount
        ag.notes.extend(f"{name}: {note}" for note in pen.notes)
        if method == "annualized":
            pass
        elif pen.amount:
            ag.notes.append(
                f"{name}: Form 2210 penalty {pen.amount:,.2f} (regular method: a "
                "quarter of the required annual payment due on each date, "
                "withholding counted a quarter on each, a later payment applied "
                "to the earliest shortfall first; the remaining installments "
                "paid in full on their due dates)"
            )
        else:
            ag.notes.append(f"{name}: no Form 2210 penalty (regular method)")
    else:
        ag.notes.append(
            f"{name}: the state's underpayment penalty not computed (unavailable)"
        )
    return ag


@dataclass(frozen=True)
class Flow:
    """One estimated-tax cash movement on the glide path's monthly line."""

    when: str
    agency: str
    amount: float
    kind: (
        str  # "paid" | "installment <n>" | "next year installment <n>" | "balance due"
        # | "penalty"
    )


def next_year_required(ag: Agency, agi: float) -> float:
    """The plan year + 1 annual payment the installments must cover, from the
    year's projected tax repeated: its safe harbor (the lesser of 90% of that
    tax and 100% of this year's, ``safe_harbor`` with this year's tax as both
    legs) less the same withholding, floored at zero (IRC 6654(d)(1)(B): no
    installment is due when withholding covers it), and zero under the de
    minimis test. ``cash_flows`` and the year plan's calendar both read it."""
    if de_minimis(ag.name, ag.current_tax - ag.withheld, ag.separate):
        return 0.0
    annual, _ = safe_harbor(
        ag.name, ag.current_tax, ag.current_tax, agi, separate=ag.separate, agi=agi
    )
    return r(max(annual - ag.withheld, 0.0))


def cash_flows(et: EstTax) -> tuple[list[Flow], list[str]]:
    """What the tax costs in cash, dated, from this estimate: payments already
    made (bank rows and typed ones, on their own dates); each installment due
    on or after the estimate's date at what its cumulative safe-harbor figure
    still lacks, so an earlier shortfall is made up at the next due date; the
    plan year + 1 installments that fall inside the following year; and the
    tax the installments leave unpaid, due with the return.

    Next year repeats this year's projected tax: its safe harbor is the lesser
    of 90% of that tax and 100% of this year's (``safe_harbor`` with this
    year's tax as both legs), less the same withholding, and the de minimis
    test applies as it does here. Its January installment falls after the
    two-year window and is left out. A refund is not counted as cash."""
    as_of = date.fromisoformat(et.as_of)
    flows: list[Flow] = []
    notes: list[str] = []
    filing = shift(date(et.year + 1, 4, 15))
    for ag in et.agencies:
        for p in ag.payments:
            flows.append(Flow(p.date, ag.name, p.amount, "paid"))
        running = r(sum(p.amount for p in ag.payments))
        for inst in ag.installments:
            if date.fromisoformat(inst.due) < as_of:
                continue
            need = r(max(inst.required - running, 0.0))
            running = r(running + need)
            flows.append(Flow(inst.due, ag.name, need, f"installment {inst.n}"))
        owed = r(ag.current_tax - ag.withheld - running)
        if owed < 0:
            notes.append(
                f"{ag.name}: the installments overpay by {-owed:,.2f}; the refund "
                "is not counted as cash"
            )
        else:
            flows.append(Flow(filing.isoformat(), ag.name, owed, "balance due"))
        if ag.penalty:
            if owed < 0:
                notes.append(
                    f"{ag.name}: the Form 2210 penalty {ag.penalty:,.2f} comes "
                    "out of the refund"
                )
            else:
                flows.append(Flow(filing.isoformat(), ag.name, ag.penalty, "penalty"))
        required = next_year_required(ag, et.agi)
        before = 0.0
        rule = rules(ag.name)[0]
        dated = zip(rule.installments(), schedule(et.year + 1, ag.name), strict=True)
        # the installments that fall inside the following year (not January's)
        for (n, (off, _month, _day), _), (_, due, share) in dated:
            if off:
                continue
            cum = r(required * share)
            flows.append(
                Flow(
                    due.isoformat(),
                    ag.name,
                    r(cum - before),
                    f"next year installment {n}",
                )
            )
            before = cum
    return flows, notes


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
        typed = need_value(conn, lay, year, schedule_ai.KEY) or {}
        names = agencies(pj.inputs.household.state)
        separate = pj.inputs.household.filing_status == "SEPARATE"
        prior_agi = need_value(conn, lay, year, "prior_agi")
        prior: dict[str, Any] = {
            a: need_value(conn, lay, year, prior_key(a)) for a in names
        }
        withheld = {
            a: float(need_value(conn, lay, year, withheld_key(a)) or 0) for a in names
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
    # Schedule AI line 19 by column: the engine's tax (net of refundable
    # credits, as Part I line 4 is) on each period's annualized household,
    # the planned year-end items in the last period only; with nothing typed
    # and nothing planned it can only match the regular method
    planned = {
        k: getattr(ov, k)
        for k in (
            "q4_dividend_estimate",
            "planned_st_sales",
            "planned_lt_sales",
            "planned_conversion",
        )
        if getattr(ov, k)
    }
    periods = None
    if typed or planned:
        plain = (
            build(
                lay,
                year,
                replace(
                    ov,
                    q4_dividend_estimate=0.0,
                    planned_st_sales=0.0,
                    planned_lt_sales=0.0,
                    planned_conversion=0.0,
                ),
            ).household
            if planned
            else pj.inputs.household
        )
        periods = [
            max(r(t.fed_total_tax - t.refundable_credits), 0.0)
            for t in (compute(year, h) for h in schedule_ai.households(plain, typed))
        ] + [r(res.fed_total_tax - res.refundable_credits)]
    # Form 2210 Part I and the 1040-ES worksheet test the 90% leg on line 24 less
    # the refundable credits (EIC, additional child tax credit, refundable AOTC);
    # compute's fed_total_tax is the gross line 24.
    for name in names:
        current = (
            r(res.fed_total_tax - res.refundable_credits)
            if name == FED
            else res.state_tax
        )
        pri = prior[name]
        et.agencies.append(
            _agency(
                name,
                current,
                float(pri) if pri is not None else None,
                float(prior_agi) if prior_agi is not None else None,
                withheld[name],
                paid,
                year,
                today,
                separate,
                res.agi,
                periods if name == FED else None,
            )
        )
    fed = et.agencies[0]
    if planned and fed.schedule_ai is not None:
        fed.notes.append(
            f"{FED}: Schedule AI counts the planned year-end items ("
            + ", ".join(f"{k} {v:,.0f}" for k, v in planned.items())
            + ") in the last period (September-December)"
        )
    if lumpy and periods is None:
        et.notes.append(
            "income is lumpy (over half in one quarter, or a planned year-end "
            "lump): the annualized method (Schedule AI) may cut an earlier "
            f"installment's shortfall; type {schedule_ai.KEY} (each income line "
            "through March, May and August) to figure it"
        )
    if len(names) == 1 and res.state_tax > 0:
        et.notes.append(
            f"{pj.inputs.household.state}: the engine's {res.state_tax:,.2f} of "
            "state tax has no installments here and is not on the cash line "
            "(a state with no income tax: a capital gains excise, say)"
        )
    for key in (withheld_key(a) for a in names):
        if pj.inputs.state(key) == UNKNOWN:
            et.notes.append(
                f"{key} unknown: the amounts due assume nothing was withheld "
                "(the most that could be due), not a figure to pay from"
            )
    if pj.inputs.unknown:
        et.notes.append("projection leaves out: " + ", ".join(pj.inputs.unknown))
    return et
