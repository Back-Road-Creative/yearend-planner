"""The glide path: the age/year table from the current balance under the
planning return (real and nominal dollars, the household's Social Security from
each claim age, a spouse's benefit on the other's record, this year's earnings
test),
the accessible-bucket floor through the IRA access age, the comfort-floor line
beside the on-track line, three stress rows, and
a month-by-month cash line for this year and next so the bridge to 59½ is
proven fundable by month, not only in annual bands.

Stress rows, all in real dollars: a 30% drop in year one; 5% inflation with
returns that do not rise with it (the real return falls by the excess over the
profile's inflation); the pessimistic floor return every year.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

from planner.engine.household import MissingInputError
from planner.engine.tax import r
from planner.ingest.derive import _classify_income
from planner.ingest.needs import load_profile
from planner.ledger import db, portfolio
from planner.paths import Layout
from planner.plan import (
    calendar,
    esttax,
    inputs,
    reserve,
    socialsec,
    spending,
    withdraw,
)
from planner.plan.inputs import Overrides, age_at_year_end
from planner.taxprep import schedule_c

HORIZON_AGE = 95
STRESS_DROP = 0.30
STRESS_INFLATION = 0.05
MONTHLY = ("mortgage_monthly", "premium_monthly")  # typed costs on the cash line


@dataclass(frozen=True)
class YearRow:
    year: int
    age: int
    balance_real: float
    balance_nominal: float
    ss: float
    spend: float
    withdrawal: float


@dataclass(frozen=True)
class Stress:
    name: str
    runs_out_age: int | None
    balance_at_horizon: float


@dataclass(frozen=True)
class MonthRow:
    year: int
    month: int
    se: float
    dividends: float
    cash_in: float
    living: float
    mortgage: float
    premiums: float
    est_tax: float  # estimated payments made or due this month (esttax)
    irregular: float
    cash: float  # end-of-month cash bucket
    actual: bool  # income columns from rows (True) or run-rate (False)
    planned_in: float = 0.0  # proceeds of the planned sales, at year-end settlement
    balance_due: float = 0.0  # tax the installments leave unpaid, due with the return
    # deposits that are not business income: pay, refunds, loans, unclassed
    other_in: float = 0.0

    @property
    def net(self) -> float:
        return r(
            self.se
            + self.other_in
            + self.dividends
            + self.cash_in
            + self.planned_in
            - self.living
            - self.mortgage
            - self.premiums
            - self.est_tax
            - self.balance_due
            - self.irregular
        )


@dataclass
class Glide:
    year: int
    age: int
    balance: float
    accessible: float
    access_age: float
    years_to_access: float
    floor_needed: float
    floor_shortfall: float
    rows: list[YearRow] = field(default_factory=list)
    stresses: list[Stress] = field(default_factory=list)
    months: list[MonthRow] = field(default_factory=list)
    target: float = 0.0  # the reserve the monthly line is held against
    first_short_month: str | None = None
    notes: list[str] = field(default_factory=list)
    # the comfort-floor line: the same spending rule run at the pessimistic
    # floor return, row for row beside ``rows`` (the on-track line)
    floor_rows: list[YearRow] = field(default_factory=list)
    spending: float = 0.0  # this year's spend under the band
    band: str = ""  # where that spend sits: floor, inside, ceiling, drawdown


def band_name(sp: spending.Spending) -> str:
    """Which band this year's spending sits in."""
    if sp.in_drawdown:
        return "drawdown, held at the floor"
    if sp.spending <= sp.floor:
        return "at the floor"
    if sp.spending >= sp.ceiling:
        return "at the ceiling"
    return "inside the band"


def run(
    year: int,
    age: int,
    balance: float,
    rate: float,
    floor: float,
    ceiling: float,
    ret: float,
    inflation: float,
    ss: dict[int, float],
    drop_year1: float = 0.0,
    horizon: int = HORIZON_AGE,
    peak: float | None = None,
) -> list[YearRow]:
    """Real-dollar path: spend at the start of the year (Social Security first,
    ``ss`` by age, the portfolio for the rest), grow what is left; the drawdown
    rule holds spending at the floor while the balance sits under 90% of its
    peak. ``peak`` is the inflation-adjusted all-time peak the spending panel
    uses; a starting balance above it is the peak."""
    rows: list[YearRow] = []
    bal = balance * (1 - drop_year1)
    peak = balance if peak is None else max(peak, balance)
    for i in range(max(horizon - age + 1, 1)):
        a = age + i
        benefit = ss.get(a, 0.0)
        spend = (
            floor
            if bal < spending.DRAWDOWN * peak
            else spending.clamp(rate, bal, floor, ceiling)
        )
        withdrawal = min(max(spend - benefit, 0.0), bal)
        rows.append(
            YearRow(
                year + i,
                a,
                r(bal),
                r(bal * (1 + inflation) ** i),
                r(benefit),
                r(spend),
                r(withdrawal),
            )
        )
        bal = max((bal - withdrawal) * (1 + ret), 0.0)
        peak = max(peak, bal)
    return rows


def runs_out(rows: list[YearRow]) -> int | None:
    for row in rows:
        if row.balance_real <= 0:
            return row.age
    return None


def floor_needed(floor: float, years: float, ret_floor: float) -> float:
    """Present value of the floor through the access age at the floor return."""
    whole, part = int(years), years - int(years)
    total = sum(floor / (1 + ret_floor) ** i for i in range(whole))
    return r(total + part * floor / (1 + ret_floor) ** whole)


def _by_month(
    conn: sqlite3.Connection, year: int, kind: str, pick: Any
) -> dict[int, float]:
    out: dict[int, int] = defaultdict(int)
    for row in db.rows_for(conn, year, kind=kind):
        if row.date and row.amount_cents is not None and pick(row):
            out[int(row.date[5:7])] += row.amount_cents
    return {m: db.from_cents(c) for m, c in out.items()}


PAY = "pay"  # wages paid into the account: cash in each month, never business income


def _deposits(
    conn: sqlite3.Connection, lay: Layout, year: int, notes: list[str]
) -> tuple[dict[int, float], dict[int, float], dict[int, float]]:
    """The year's bank deposits by month, each counted once by its category
    (``planner categorize``): receipts are business income, pay recurs, a
    transfer between your own accounts is no cash in at all, and everything else
    (a refund, a loan, an unclassed deposit) is cash in that month only."""
    rules, assigned = schedule_c.load_rules(lay)
    se: dict[int, int] = defaultdict(int)
    pay: dict[int, int] = defaultdict(int)
    once: dict[int, int] = defaultdict(int)
    moved = loose = 0
    for row in db.rows_for(conn, year, kind="bank"):
        if not row.date or not row.amount_cents or row.amount_cents <= 0:
            continue
        month, cents = int(row.date[5:7]), row.amount_cents
        category = schedule_c.category_of(row, rules, assigned)
        if category == schedule_c.RECEIPTS:
            se[month] += cents
        elif category == PAY:
            pay[month] += cents
        elif category == "transfer":
            moved += cents
        else:
            once[month] += cents
            loose += cents if category is None else 0
    if loose:
        notes.append(
            f"{db.from_cents(loose):,.2f} of bank deposits have no category: counted "
            "as cash in the month they came, not business income and not repeated "
            f"(planner categorize --year {year})"
        )
    if moved:
        notes.append(
            f"{db.from_cents(moved):,.2f} of deposits classed transfer (between your "
            "own accounts): not cash in"
        )

    def dollars(d: dict[int, int]) -> dict[int, float]:
        return {m: db.from_cents(c) for m, c in d.items()}

    return dollars(se), dollars(pay), dollars(once)


def _anchor(
    rows: list[MonthRow], start: float, since: date | None, year: int
) -> tuple[list[MonthRow], str]:
    """End-of-month cash from a balance dated ``since``: that month's flows after
    the date run forward from it (by the share of the month left), later months
    add their net, earlier months are worked back from it so no flow the
    balance already holds is counted twice. Undated, or dated outside the year,
    the balance starts the line on January 1."""
    nets = [row.net for row in rows]
    ends: list[float] = []
    i0 = -1 if since is None else (since.year - year) * 12 + since.month - 1
    if since is None or not 0 <= i0 < len(rows):
        cash = start
        for n in nets:
            cash = r(cash + n)
            ends.append(cash)
        note = (
            f"cash balance {start:,.2f} has no date: the line starts from it on "
            f"January 1 {year} (set the account's balance_date)"
            if since is None
            else f"cash balance {start:,.2f} is dated {since}, outside {year}: the "
            f"line starts from it on January 1 {year}"
        )
        return [replace(row, cash=c) for row, c in zip(rows, ends, strict=True)], note
    days = _month_days(since)
    after = (days - since.day) / days
    ends = [0.0] * len(rows)
    ends[i0] = r(start + nets[i0] * after)
    for i in range(i0 + 1, len(rows)):
        ends[i] = r(ends[i - 1] + nets[i])
    back = r(start - nets[i0] * (1 - after))
    for i in range(i0 - 1, -1, -1):
        ends[i] = back
        back = r(back - nets[i])
    note = (
        f"cash starts from the {since} balance {start:,.2f}; months before it are "
        "worked back from it, so deposits it already holds are not counted twice"
    )
    return [replace(row, cash=c) for row, c in zip(rows, ends, strict=True)], note


def _month_days(d: date) -> int:
    nxt = date(d.year + d.month // 12, d.month % 12 + 1, 1)
    return (nxt - date(d.year, d.month, 1)).days


def _income_by_month(conn: sqlite3.Connection, year: int) -> dict[int, float]:
    income = _by_month(
        conn, year, "income", lambda rw: _classify_income("income", rw.type) is not None
    )
    if income:
        return income
    return _by_month(
        conn,
        year,
        "transaction",
        lambda rw: _classify_income("transaction", rw.type) is not None,
    )


def _irregular(
    profile: dict[str, Any], notes: list[str]
) -> list[tuple[int | None, int, float, str]]:
    out: list[tuple[int | None, int, float, str]] = []
    for item in profile.get("irregular") or []:
        try:
            out.append(
                (
                    int(item["year"]) if item.get("year") is not None else None,
                    int(item["month"]),
                    float(item["amount"]),
                    str(item.get("label", "")),
                )
            )
        except (KeyError, TypeError, ValueError):
            notes.append(f"irregular item skipped (needs month and amount): {item!r}")
    return out


def _tax_by_month(
    lay: Layout,
    year: int,
    as_of: date,
    overrides: Overrides | None,
    notes: list[str],
) -> dict[tuple[int, int], tuple[float, float]]:
    """(estimated payments, balance due) by (year, month), from esttax's own
    installments and payments made, with the planned sales and conversion in
    the tax. Left empty, and said so, when the projection lacks an input."""
    try:
        et = esttax.estimate(lay, year, as_of, overrides)
    except MissingInputError as exc:
        notes.append(f"estimated payments left at zero: {exc}")
        return {}
    flows, flow_notes = esttax.cash_flows(et)
    out: dict[tuple[int, int], list[float]] = defaultdict(lambda: [0.0, 0.0])
    for f in flows:
        when = date.fromisoformat(f.when)
        if when.year in (year, year + 1):
            due_now = f.kind in ("balance due", "penalty")
            out[(when.year, when.month)][1 if due_now else 0] += f.amount
    total = sum(ag.current_tax for ag in et.agencies)
    due = sum(f.amount for f in flows if f.kind == "balance due")
    notes.append(
        "estimated payments are esttax's: payments made on their dates, each "
        "later installment at what its safe-harbor figure still lacks, and "
        "next year's at 90% of this year's tax less withholding; "
        f"{due:,.2f} of the projected {total:,.2f} falls due with the return"
    )
    penalty = sum(f.amount for f in flows if f.kind == "penalty")
    if penalty:
        notes.append(
            f"the balance due with the return carries the {penalty:,.2f} Form "
            "2210 penalty"
        )
    notes.extend(flow_notes)
    notes.extend(n for ag in et.agencies for n in ag.notes if "were short" in n)
    ov = overrides or Overrides()
    if ov.planned_conversion:
        try:
            without = esttax.estimate(
                lay, year, as_of, replace(ov, planned_conversion=0.0)
            )
        except MissingInputError:
            without = None
        if without is not None:
            added = r(total - sum(ag.current_tax for ag in without.agencies))
            notes.append(
                f"the planned conversion {ov.planned_conversion:,.2f} adds "
                f"{added:,.2f} of federal and state tax to the year, paid through "
                "the installments and the balance due above"
            )
    return {k: (r(v[0]), r(v[1])) for k, v in out.items()}


def _planned_sales(
    lay: Layout,
    year: int,
    as_of: date,
    overrides: Overrides | None,
    notes: list[str],
) -> dict[tuple[int, int], float]:
    """Proceeds of the planned sales by (year, month): they settle on the
    plan's year-end cut-off (the last business day of December). The overrides
    carry gains, so the cash comes from the taxable lots that would produce
    them; a gain no lot can supply brings in nothing and is named."""
    ov = overrides or Overrides()
    st = portfolio.status(lay, year, as_of)
    lots = withdraw.sellable(st)
    total = 0.0
    for name, term in (("planned_st_sales", "short"), ("planned_lt_sales", "long")):
        gain = float(getattr(ov, name))
        if not gain:
            continue
        got, left = withdraw.proceeds_for_gain(lots, gain, term)
        total += got
        if left:
            notes.append(
                f"{name} {gain:,.2f}: no taxable lot supplies {left:,.2f} of it "
                f"(planner withdraw); {got:,.2f} of proceeds counted"
            )
        else:
            notes.append(
                f"{name} {gain:,.2f} brings in {got:,.2f}, priced from the taxable "
                "lots with the most gain per dollar (the least cash for the gain)"
            )
    if not total:
        return {}
    settle = calendar.shift(date(year, 12, 31), calendar.PRIOR)
    return {(settle.year, settle.month): r(total)}


def months(
    lay: Layout,
    year: int,
    as_of: date,
    living_annual: float,
    cash_start: float,
    cash_in: dict[str, float],
    overrides: Overrides | None,
    notes: list[str],
    cash_since: date | None = None,
) -> list[MonthRow]:
    profile = load_profile(lay)
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        se_act, pay_act, once_act = _deposits(conn, lay, year, notes)
        div_act = _income_by_month(conn, year)
        div_last = _income_by_month(conn, year - 1)
    finally:
        conn.close()
    done = [m for m in range(1, 13) if date(year, m, 1) <= as_of]
    se_rate = sum(se_act.get(m, 0.0) for m in done) / len(done) if done else 0.0
    pay_rate = sum(pay_act.get(m, 0.0) for m in done) / len(done) if done else 0.0
    div_rate = sum(div_act.get(m, 0.0) for m in done) / len(done) if done else 0.0
    if not se_act:
        notes.append(
            "no bank deposit is classed receipts: SE income by month is unknown"
        )
    if not div_act:
        notes.append("no dividend or interest rows in the ledger for the year")
    taxes = _tax_by_month(lay, year, as_of, overrides, notes)
    planned = _planned_sales(lay, year, as_of, overrides, notes)
    mortgage = float(profile.get("mortgage_monthly") or 0)
    premiums = float(profile.get("premium_monthly") or 0)
    for key in MONTHLY:
        if profile.get(key) is None:
            notes.append(
                f"{key} unknown: left out of the cash line, which runs high by "
                f"that much a month (planner needed --year {year})"
            )
    irregular = _irregular(profile, notes)
    rows: list[MonthRow] = []
    for y in (year, year + 1):
        for m in range(1, 13):
            actual = y == year and m in done
            se = se_act.get(m, 0.0) if actual else se_rate
            div = div_act.get(m, 0.0) if actual else div_last.get(m, div_rate)
            irr = sum(a for yy, mm, a, _ in irregular if mm == m and yy in (None, y))
            other = pay_act.get(m, 0.0) + once_act.get(m, 0.0) if actual else pay_rate
            ym = (y, m)
            row = MonthRow(
                y,
                m,
                r(se),
                r(div),
                r(cash_in.get(f"{y}-{m:02d}", 0.0)),
                r(living_annual / 12),
                mortgage,
                premiums,
                taxes.get(ym, (0.0, 0.0))[0],
                r(irr),
                0.0,
                actual,
                planned.get(ym, 0.0),
                taxes.get(ym, (0.0, 0.0))[1],
                r(other),
            )
            rows.append(row)
    rows, note = _anchor(rows, cash_start, cash_since, year)
    notes.append(note)
    return rows


def _earnings_tests(
    lay: Layout,
    year: int,
    overrides: Overrides | None,
    head: socialsec.Person,
    spouse: socialsec.Person | None,
) -> list[socialsec.EarningsTest]:
    """This year's earnings test for each person drawing benefits under full
    retirement age, from their wages and self-employment on the return."""
    people = [p for p in (head, spouse) if p is not None and p.own]
    due = [p for p in people if socialsec.earnings_test(p, p.own, 1e12, year)]
    if not due:
        return []
    try:
        hh = inputs.build(lay, year, overrides).household
    except (MissingInputError, ValueError):
        return []
    out = []
    for p in due:
        who = hh if p is head else hh.spouse
        if who is None:
            continue
        test = socialsec.earnings_test(p, p.own, float(who.wages + who.se_income), year)
        if test is not None:
            out.append(test)
    return out


def glide(
    lay: Layout,
    year: int,
    as_of: date | None = None,
    balance: float | None = None,
    cash_in: dict[str, float] | None = None,
    overrides: Overrides | None = None,
) -> Glide:
    profile = load_profile(lay)
    spending.require(profile, spending.REQUIRED, year)
    today = as_of or date.today()
    sp = spending.plan(lay, year, today, balance, years=1)
    st = portfolio.status(lay, year, today)
    age = age_at_year_end(str(profile["birth_date"]), year)
    access_age = float(profile.get("ira_access_age") or 59.5)
    birth = date.fromisoformat(str(profile["birth_date"]))
    exact_age = (date(year, 1, 1) - birth).days / 365.25
    years_to_access = max(access_age - exact_age, 0.0)
    ret_floor = float(profile["return_floor"])
    needed = floor_needed(sp.floor, years_to_access, ret_floor)
    g = Glide(
        year,
        age,
        sp.balance,
        st.accessible,
        access_age,
        round(years_to_access, 2),
        needed,
        r(max(needed - st.accessible, 0.0)),
        notes=list(sp.notes),
        spending=sp.spending,
        band=band_name(sp),
    )
    head, spouse, ss_notes = socialsec.household(profile, year)
    g.notes += ss_notes
    ss = socialsec.schedule(head, spouse, year, HORIZON_AGE)
    for test in _earnings_tests(lay, year, overrides, head, spouse):
        g.notes.append(test.note)
        ss[age] = max(ss.get(age, 0.0) - test.withheld, 0.0)
    inflation = float(profile["inflation"])
    ret_track = float(profile["return_track"])
    g.rows = run(
        year,
        age,
        sp.balance,
        sp.rate,
        sp.floor,
        sp.ceiling,
        ret_track,
        inflation,
        ss,
        peak=sp.peak_adjusted,
    )
    for name, ret, drop in (
        ("30% drop in year one", ret_track, STRESS_DROP),
        ("5% inflation", ret_track - max(STRESS_INFLATION - inflation, 0.0), 0.0),
        ("floor returns", ret_floor, 0.0),
    ):
        rows = run(
            year,
            age,
            sp.balance,
            sp.rate,
            sp.floor,
            sp.ceiling,
            ret,
            inflation,
            ss,
            drop,
            peak=sp.peak_adjusted,
        )
        g.stresses.append(Stress(name, runs_out(rows), rows[-1].balance_real))
        if name == "floor returns":
            g.floor_rows = rows  # the comfort-floor line is the floor-returns run
    held = [p for p in st.positions if p.type == "cash"]
    cash_start = r(sum(p.value for p in held))
    dated = sorted(p.as_of[:10] for p in held if p.as_of)
    since = date.fromisoformat(dated[-1]) if held and len(dated) == len(held) else None
    if since is not None and dated[0] != dated[-1]:
        g.notes.append(
            f"cash balances are dated {dated[0]} to {dated[-1]}: the line starts "
            f"from {dated[-1]}; update the older balance for an exact start"
        )
    g.months = months(
        lay,
        year,
        today,
        sp.spending,
        cash_start,
        cash_in or {},
        overrides,
        g.notes,
        since,
    )
    target = g.target = reserve.load(lay, year, today, profile, overrides).amount
    for row in g.months:
        if row.cash < target:
            g.first_short_month = f"{row.year}-{row.month:02d}"
            g.notes.append(
                f"cash falls below the target {target:,.2f} in {g.first_short_month} "
                f"({row.cash:,.2f}); the bridge is not funded by month"
            )
            break
    if g.floor_shortfall:
        g.notes.append(
            f"accessible money {st.accessible:,.2f} is short of the floor through "
            f"{access_age} by {g.floor_shortfall:,.2f}"
        )
    return g
