"""The glide path: the age/year table from the current balance under the
planning return (real and nominal dollars, Social Security from the claim age),
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
from planner.plan import calendar, esttax, spending, withdraw
from planner.plan.inputs import Overrides, age_at_year_end

HORIZON_AGE = 95
SS_AGES = (62, 67, 70)
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

    @property
    def net(self) -> float:
        return r(
            self.se
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


def ss_monthly(profile: dict[str, Any], claim_age: int) -> tuple[float, str | None]:
    """The SSA statement figure for the claim age, else the nearest lower age."""
    for age in sorted(SS_AGES, reverse=True):
        est = profile.get(f"ss_estimate_{age}")
        if age <= claim_age and est is not None:
            note = (
                None
                if age == claim_age
                else f"ss_estimate_{age} stands in for {claim_age}"
            )
            return float(est), note
    return 0.0, f"no SSA estimate at or below claim age {claim_age}"


def run(
    year: int,
    age: int,
    balance: float,
    rate: float,
    floor: float,
    ceiling: float,
    ret: float,
    inflation: float,
    ss_annual: float,
    claim_age: int | None,
    drop_year1: float = 0.0,
    horizon: int = HORIZON_AGE,
    peak: float | None = None,
) -> list[YearRow]:
    """Real-dollar path: spend at the start of the year (SS first, the portfolio
    for the rest), grow what is left; the drawdown rule holds spending at the
    floor while the balance sits under 90% of its peak. ``peak`` is the
    inflation-adjusted all-time peak the spending panel uses; a starting balance
    above it is the peak."""
    rows: list[YearRow] = []
    bal = balance * (1 - drop_year1)
    peak = balance if peak is None else max(peak, balance)
    for i in range(max(horizon - age + 1, 1)):
        a = age + i
        ss = ss_annual if claim_age is not None and a >= claim_age else 0.0
        spend = (
            floor
            if bal < spending.DRAWDOWN * peak
            else spending.clamp(rate, bal, floor, ceiling)
        )
        withdrawal = min(max(spend - ss, 0.0), bal)
        rows.append(
            YearRow(
                year + i,
                a,
                r(bal),
                r(bal * (1 + inflation) ** i),
                r(ss),
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
            out[(when.year, when.month)][1 if f.kind == "balance due" else 0] += (
                f.amount
            )
    total = sum(ag.current_tax for ag in et.agencies)
    due = sum(f.amount for f in flows if f.kind == "balance due")
    notes.append(
        "estimated payments are esttax's: payments made on their dates, each "
        "later installment at what its safe-harbor figure still lacks, and "
        "next year's at 90% of this year's tax less withholding; "
        f"{due:,.2f} of the projected {total:,.2f} falls due with the return"
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
                f"{added:,.2f} of federal and NC tax to the year, paid through "
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
) -> list[MonthRow]:
    profile = load_profile(lay)
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        se_act = _by_month(conn, year, "bank", lambda rw: rw.amount_cents > 0)
        div_act = _income_by_month(conn, year)
        div_last = _income_by_month(conn, year - 1)
    finally:
        conn.close()
    done = [m for m in range(1, 13) if date(year, m, 1) <= as_of]
    se_rate = sum(se_act.get(m, 0.0) for m in done) / len(done) if done else 0.0
    div_rate = sum(div_act.get(m, 0.0) for m in done) / len(done) if done else 0.0
    if not se_act:
        notes.append("no bank deposits in the ledger: SE income by month is unknown")
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
    cash = cash_start
    for y in (year, year + 1):
        for m in range(1, 13):
            actual = y == year and m in done
            se = se_act.get(m, 0.0) if actual else se_rate
            div = div_act.get(m, 0.0) if actual else div_last.get(m, div_rate)
            irr = sum(a for yy, mm, a, _ in irregular if mm == m and yy in (None, y))
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
            )
            cash = r(cash + row.net)
            rows.append(replace(row, cash=cash))
    return rows


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
    claim_age = profile.get("ss_claim_age")
    ss_annual = 0.0
    if claim_age is not None:
        monthly, note = ss_monthly(profile, int(claim_age))
        ss_annual = monthly * 12
        if note:
            g.notes.append(note)
    else:
        g.notes.append("ss_claim_age not set: no Social Security in the table")
    inflation = float(profile["inflation"])
    ret_track = float(profile["return_track"])
    claim = int(claim_age) if claim_age is not None else None
    g.rows = run(
        year,
        age,
        sp.balance,
        sp.rate,
        sp.floor,
        sp.ceiling,
        ret_track,
        inflation,
        ss_annual,
        claim,
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
            ss_annual,
            claim,
            drop,
            peak=sp.peak_adjusted,
        )
        g.stresses.append(Stress(name, runs_out(rows), rows[-1].balance_real))
        if name == "floor returns":
            g.floor_rows = rows  # the comfort-floor line is the floor-returns run
    cash_start = r(sum(p.value for p in st.positions if p.type == "cash"))
    g.months = months(
        lay, year, today, sp.spending, cash_start, cash_in or {}, overrides, g.notes
    )
    target = float(profile.get("cash_target") or 0)
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
