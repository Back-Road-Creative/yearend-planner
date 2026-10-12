"""The long-term path per account (master plan unit 4e): every account to the
horizon age in today's dollars, with the tax each year's withdrawals owe,
required minimum distributions, and the debts paid on schedule. Fixed stress
cases sit beside it; there is no success percentage.

Each year, in order: spending by the band rule (planner.plan.spending) on the
total, plus the year's debt payments (``data/profile/goals.yaml``), less Social
Security (the glide path's schedule, earnings test applied). RMDs come out
first. The rest is drawn in this order: cash above the reserve, taxable,
inherited IRAs, tax-deferred accounts once their owner reaches the access age
(a governmental 457(b) once its employer is left), Roth (contributions and
seasoned conversions before the access age, all of it after), the HSA from 65
(IRC 223(f)(4)(C): taxed, no additional tax), the reserve last. What is left
grows at the case's real return. An RMD larger than the year needs is taxed
and the rest reinvested in a taxable account.

Sources:
- RMD age: IRC 401(a)(9)(C)(v): 73 for someone who reaches 72 after 2022 and
  73 before 2033, 75 for someone who reaches 74 after 2032 (born 1959 reads 73,
  Prop. Reg. 1.401(a)(9)-2(b)(2)(v)). Born 1950 or earlier: 72, already begun.
- RMD amount: last year-end balance / the Uniform Lifetime Table, Pub. 590-B
  (2025) Appendix B, Table III.
- Inherited IRA: empty by the end of the 10th year after death (IRC
  401(a)(9)(H)); drained in equal parts, which covers any yearly RMD.
- Federal tax: this year's brackets, standard deduction, extra deduction at 65,
  and 0/15/20% gains rates from the engine's own parameters, held in today's
  dollars (IRC 1(f) indexes them to inflation). The Social Security thresholds
  of IRC 86(c) are not indexed, so they shrink in today's dollars each year.
- State tax: this year's engine projection as a share of AGI, applied to each
  year's AGI.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date

from planner import goals as goals_
from planner.engine.household import MissingInputError
from planner.engine.tax import _param, r
from planner.ingest.needs import load_profile
from planner.ledger import portfolio
from planner.paths import Layout
from planner.plan import reserve, spending
from planner.plan.inputs import FILING, OverrideError, Overrides

# Pub. 590-B (2025) Appendix B, Table III (Uniform Lifetime): age -> divisor
UNIFORM = {
    72: 27.4, 73: 26.5, 74: 25.5, 75: 24.6, 76: 23.7, 77: 22.9, 78: 22.0,
    79: 21.1, 80: 20.2, 81: 19.4, 82: 18.5, 83: 17.7, 84: 16.8, 85: 16.0,
    86: 15.2, 87: 14.4, 88: 13.7, 89: 12.9, 90: 12.2, 91: 11.5, 92: 10.8,
    93: 10.1, 94: 9.5, 95: 8.9, 96: 8.4, 97: 7.8, 98: 7.3, 99: 6.8, 100: 6.4,
    101: 6.0, 102: 5.6, 103: 5.2, 104: 4.9, 105: 4.6, 106: 4.3, 107: 4.1,
    108: 3.9, 109: 3.7, 110: 3.5, 111: 3.4, 112: 3.3, 113: 3.1, 114: 3.0,
    115: 2.9, 116: 2.8, 117: 2.7, 118: 2.5, 119: 2.3, 120: 2.0,
}  # fmt: skip
KIND = {
    "cash": "cash",
    "taxable": "taxable",
    "trad_ira": "deferred",
    "simple_ira": "deferred",
    "gov_457b": "deferred",
    "inherited_ira": "inherited",
    "roth": "roth",
    "hsa": "hsa",
}
COLUMNS = ("cash", "taxable", "deferred", "inherited", "roth", "hsa")
LABEL = {"deferred": "tax-deferred", "roth": "Roth", "hsa": "HSA"}
HSA_AGE = 65
ROTH_AGE = 59.5  # IRC 408A(d)(2)(A)(i)
INHERITED_YEARS = portfolio.INHERITED_YEARS
STRESS_DROP = 0.30
STRESS_INFLATION = 0.05


def rmd_age(birth: date) -> int:
    if birth.year >= 1960:
        return 75
    return 73 if birth.year >= 1951 else 72


def divisor(age: int) -> float:
    return UNIFORM[min(max(age, 72), 120)]


@dataclass
class Account:
    number: str
    kind: str  # one of COLUMNS
    owner: str  # self | spouse
    balance: float
    basis: float = 0.0  # taxable: cost basis, in today's dollars
    last_year: int | None = None  # inherited: the year it must be empty
    open_early: bool = False  # 457(b) left employer: no access age


@dataclass(frozen=True)
class Rules:
    """Federal brackets and the state share, from this year's engine."""

    year: int
    fs: str
    std: float
    aged: float  # the extra standard deduction per person 65 or over
    ordinary: tuple[tuple[float, float], ...]  # (top, rate) lowest first
    gains: tuple[tuple[float, float], ...]  # (top, rate) for 0/15/20%
    ss: tuple[float, float]  # IRC 86(c) base and adjusted base, not indexed
    state_rate: float | None

    def federal(
        self, ordinary: float, gains: float, ss: float, deflate: float, over65: int
    ) -> tuple[float, float]:
        """(federal tax, AGI) in today's dollars; ``deflate`` is prices
        relative to this year (the IRC 86 thresholds shrink by it)."""
        b1, b2 = self.ss[0] / deflate, self.ss[1] / deflate
        prov = ordinary + gains + ss / 2
        if prov <= b1:
            tss = 0.0
        elif prov <= b2:
            tss = min(ss / 2, (prov - b1) / 2)
        else:
            tss = min(0.85 * ss, 0.85 * (prov - b2) + min(ss / 2, (b2 - b1) / 2))
        agi = ordinary + gains + tss
        taxable = max(agi - self.std - self.aged * over65, 0.0)
        gain_part = min(max(gains, 0.0), taxable)
        tax = _scale(self.ordinary, 0.0, taxable - gain_part)
        return tax + _scale(self.gains, taxable - gain_part, taxable), agi

    def both(
        self, ordinary: float, gains: float, ss: float, deflate: float, over65: int
    ) -> tuple[float, float]:
        """(federal, state) tax; state 0 when its share is unknown."""
        fed, agi = self.federal(ordinary, gains, ss, deflate, over65)
        return fed, agi * (self.state_rate or 0.0)


def _scale(rows: tuple[tuple[float, float], ...], lo: float, hi: float) -> float:
    tax, low = 0.0, 0.0
    for top, rate in rows:
        tax += max(min(hi, top) - max(lo, low), 0.0) * rate
        low = top
    return tax


def rules(year: int, fs: str, state_rate: float | None) -> Rules:
    ordinary = []
    for n in range(1, 8):
        top = _param(f"gov.irs.income.bracket.thresholds.{n}.{fs}", year)
        ordinary.append((top, _param(f"gov.irs.income.bracket.rates.{n}", year)))
    cg = "gov.irs.capital_gains"
    gains = (
        (_param(f"{cg}.thresholds.1.{fs}", year), _param(f"{cg}.rates.1", year)),
        (_param(f"{cg}.thresholds.2.{fs}", year), _param(f"{cg}.rates.2", year)),
        (math.inf, _param(f"{cg}.rates.3", year)),
    )
    base = "gov.irs.social_security.taxability.threshold"
    return Rules(
        year,
        fs,
        _param(f"gov.irs.deductions.standard.amount.{fs}", year),
        _param(f"gov.irs.deductions.standard.aged_or_blind.amount.{fs}", year),
        tuple(ordinary),  # the top bracket's threshold is inf
        gains,
        (
            _param(f"{base}.base.main.{fs}", year),
            _param(f"{base}.adjusted_base.main.{fs}", year),
        ),
        state_rate,
    )


@dataclass(frozen=True)
class Row:
    year: int
    age: int
    balances: dict[str, float]  # by COLUMNS, start of year, today's dollars
    ss: float
    spend: float
    debt: float
    rmd: float
    drawn: float  # everything taken out, RMDs included
    fed: float
    state: float
    short: float  # what the accounts could not cover
    inflation: float = 0.0
    year0: int = 0

    @property
    def total(self) -> float:
        return r(sum(self.balances.values()))

    @property
    def nominal(self) -> float:
        return r(self.total * (1 + self.inflation) ** (self.year - self.year0))


@dataclass(frozen=True)
class Case:
    name: str
    runs_out_age: int | None  # the first age a year is not covered
    total_at_horizon: float
    tax: float  # federal and state over the path


@dataclass
class Path:
    year: int
    rows: list[Row] = field(default_factory=list)
    floor_rows: list[Row] = field(default_factory=list)
    cases: list[Case] = field(default_factory=list)
    rmd_lines: list[str] = field(default_factory=list)
    debt_lines: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        if not self.rows:
            return ["no long-term path: no account balances"]
        first = self.rows[0]
        out = [
            "path per account from "
            + ", ".join(
                f"{LABEL.get(k, k)} {v:,.2f}" for k, v in first.balances.items() if v
            )
        ]
        out += self.rmd_lines + self.debt_lines
        fed = sum(rw.fed for rw in self.rows)
        state = sum(rw.state for rw in self.rows)
        out.append(
            f"tax on the path, today's dollars: federal {fed:,.2f}, state {state:,.2f}"
        )
        for c in self.cases:
            end = f"runs out at {c.runs_out_age}" if c.runs_out_age else "lasts"
            out.append(
                f"case {c.name:22} {end}; tax {c.tax:,.2f}; "
                f"left at the horizon {c.total_at_horizon:,.2f}"
            )
        out.append("fixed cases, not odds: no success percentage is figured")
        return out


def debt_schedule(
    debts: list[goals_.Debt], as_of: date, years: int, inflation: float
) -> tuple[list[float], list[str]]:
    """Each year's debt payments in today's dollars (a fixed payment shrinks
    with inflation), month by month from ``as_of``, and a payoff line each."""
    out = [0.0] * years
    lines = []
    for d in debts:
        bal, i, paid = d.balance, d.rate / 100 / 12, None
        for k in range(years * 12):
            if bal <= 0.005:
                break
            y, m = divmod(as_of.month - 1 + k, 12)
            pay = min(d.payment, bal * (1 + i))
            bal = bal * (1 + i) - pay
            if y < years:
                out[y] += pay / (1 + inflation) ** (y + k % 12 / 12)
            if bal <= 0.005:
                paid = f"{as_of.year + y}-{m + 1:02d}"
        if paid:
            lines.append(f"debt {d.name}: paid off {paid} at {d.payment:,.2f} a month")
        else:
            lines.append(
                f"debt {d.name}: not paid off on this path at {d.payment:,.2f} a "
                "month; payments run every year of the table"
            )
    return [r(v) for v in out], lines


def accounts(
    st: portfolio.Status, lay: Layout, spouse: bool, notes: list[str]
) -> list[Account]:
    typed = portfolio.load_accounts(lay)
    basis: dict[str, float] = {}
    for lot in st.lots:
        basis[lot.account] = basis.get(lot.account, 0.0) + lot.basis
    deadline = {acct: date.fromisoformat(by).year for acct, _, by in st.inherited}
    out: dict[str, Account] = {}
    for p in st.positions:
        kind = KIND.get(p.type)
        if kind is None:
            notes.append(f"account {p.account} type not typed: left off the path")
            continue
        entry = typed.get(p.account, {})
        owner = str(entry.get("owner") or "self")
        if spouse and kind in ("deferred", "roth") and not entry.get("owner"):
            notes.append(
                f"account {p.account}: owner not entered, read as yours "
                f"(planner account {p.account} --owner spouse)"
            )
        a = out.setdefault(p.account, Account(p.account, kind, owner, 0.0))
        a.balance += p.value
        a.open_early = bool(entry.get("separated")) and p.type == "gov_457b"
        if kind == "inherited":
            a.last_year = deadline.get(p.account)
    for a in out.values():
        if a.kind != "taxable":
            continue
        if a.number in basis:
            a.basis = min(basis[a.number], a.balance)
        else:
            notes.append(
                f"taxable account {a.number} has no cost basis on file: every "
                "dollar drawn is taxed as gain"
            )
    return [a for a in out.values() if a.balance > 0]


@dataclass
class Household:
    """What one year needs to know about the people."""

    year: int
    birth: date
    spouse_birth: date | None
    access_age: float
    ss: dict[int, float]  # by the head's age
    roth_basis: float

    def age(self, owner: str, y: int) -> float:
        b = self.spouse_birth if owner == "spouse" and self.spouse_birth else self.birth
        return y - b.year

    def over65(self, y: int) -> int:
        people = [self.birth] + ([self.spouse_birth] if self.spouse_birth else [])
        return sum(y - b.year >= HSA_AGE for b in people)


def _open(a: Account, hh: Household, y: int) -> bool:
    if a.kind in ("cash", "taxable", "inherited"):
        return True
    if a.kind == "deferred":
        return a.open_early or hh.age(a.owner, y) >= hh.access_age
    if a.kind == "hsa":
        return hh.age(a.owner, y) >= HSA_AGE
    return True  # roth: limited to basis before ROTH_AGE in _draw


def run(
    accts: list[Account],
    hh: Household,
    sp: spending.Spending,
    rl: Rules,
    debt: list[float],
    held: float,
    ret: float,
    inflation: float,
    horizon_age: int,
    drop: float = 0.0,
) -> list[Row]:
    accts = [replace(a, balance=a.balance * (1 - drop)) for a in accts]
    roth_basis = hh.roth_basis
    peak = max(sp.peak_adjusted, sum(a.balance for a in accts))
    age0 = hh.year - hh.birth.year
    rows: list[Row] = []
    for i in range(max(horizon_age - age0 + 1, 1)):
        y, age = hh.year + i, age0 + i
        start = {k: 0.0 for k in COLUMNS}
        for a in accts:
            start[a.kind] += a.balance
        total = sum(start.values())
        spend = (
            sp.floor
            if total < spending.DRAWDOWN * peak
            else spending.clamp(sp.rate, total, sp.floor, sp.ceiling)
        )
        benefit = hh.ss.get(age, 0.0)
        need = spend + debt[i] - benefit
        ordinary = gains = rmd = 0.0
        for a in accts:  # forced first: RMDs and the inherited window
            if a.kind == "deferred" and hh.age(a.owner, y) >= rmd_age(
                hh.spouse_birth if a.owner == "spouse" and hh.spouse_birth else hh.birth
            ):
                take = a.balance / divisor(int(hh.age(a.owner, y)))
            elif a.kind == "inherited" and a.last_year is not None:
                take = a.balance / max(a.last_year - y + 1, 1)
            else:
                continue
            a.balance -= take
            rmd += take
        ordinary += rmd
        raised = rmd
        deflate, n65 = (1 + inflation) ** i, hh.over65(y)
        order = _order(accts, hh, y, held)
        for _ in range(200):
            fed, state = rl.both(ordinary, gains, benefit, deflate, n65)
            gap = need + fed + state - raised
            if gap <= 0.005 or not order:
                break
            a, cap = order[0]
            room = (
                min(a.balance - cap, roth_basis)
                if _roth_limited(a, hh, y)
                else (a.balance - cap)
            )
            take = min(gap, max(room, 0.0))
            if take <= 0.005:
                order.pop(0)
                continue
            if a.kind == "taxable" and a.balance:
                share = a.basis / a.balance
                gains += take * (1 - share)
                a.basis -= take * share
            elif a.kind in ("deferred", "inherited", "hsa"):
                ordinary += take
            elif a.kind == "roth" and _roth_limited(a, hh, y):
                roth_basis -= take
            a.balance -= take
            raised += take
        fed, state = rl.both(ordinary, gains, benefit, deflate, n65)
        short = max(need + fed + state - raised, 0.0)
        extra = raised - need - fed - state
        if extra > 0.005:  # an RMD beyond the year's need, after its tax
            home = next((a for a in accts if a.kind == "taxable"), None)
            if home is None:
                home = Account("reinvested", "taxable", "self", 0.0)
                accts.append(home)
            home.balance += extra
            home.basis += extra
        rows.append(
            Row(
                y,
                age,
                {k: r(v) for k, v in start.items()},
                r(benefit),
                r(spend),
                r(debt[i]),
                r(rmd),
                r(raised),
                r(fed),
                r(state),
                r(short),
                inflation,
                hh.year,
            )
        )
        for a in accts:
            a.balance = max(a.balance, 0.0) * (1 + ret)
            a.basis = a.basis / (1 + inflation)  # basis is fixed dollars
        peak = max(peak, sum(a.balance for a in accts))
    return rows


def _roth_limited(a: Account, hh: Household, y: int) -> bool:
    return a.kind == "roth" and hh.age(a.owner, y) < ROTH_AGE


def _order(
    accts: list[Account], hh: Household, y: int, held: float
) -> list[tuple[Account, float]]:
    """(account, the floor it is drawn to) in draw order; the reserve is held
    in cash until everything else is spent."""
    rank = ("cash", "taxable", "inherited", "deferred", "roth", "hsa")
    open_ = sorted(
        (a for a in accts if _open(a, hh, y)), key=lambda a: rank.index(a.kind)
    )
    cash = [a for a in open_ if a.kind == "cash"]
    keep = held
    floors: dict[str, float] = {}
    for a in cash:
        floors[a.number] = min(a.balance, keep)
        keep -= floors[a.number]
    out = [(a, floors.get(a.number, 0.0)) for a in open_]
    return out + [(a, 0.0) for a in cash if floors.get(a.number)]


def runs_out(rows: list[Row]) -> int | None:
    return next((rw.age for rw in rows if rw.short > 0.5), None)


def path(
    lay: Layout,
    year: int,
    as_of: date,
    overrides: Overrides | None,
    ss: dict[int, float],
    horizon_age: int,
) -> Path:
    """The path from today's balances; ``ss`` is the glide path's yearly
    Social Security by the head's age."""
    from planner.plan.magi import project

    profile = load_profile(lay)
    spending.require(profile, spending.REQUIRED, year)
    out = Path(year)
    st = portfolio.status(lay, year, as_of)
    sp = spending.plan(lay, year, as_of, years=1)
    spouse_raw = profile.get("spouse_birth_date")
    joint = profile.get("filing_status") == "married_joint" and spouse_raw
    accts = accounts(st, lay, bool(joint), out.notes)
    if not accts:
        return out
    if st.roth_contributions is None and any(a.kind == "roth" for a in accts):
        out.notes.append(
            "Roth contributions not entered: before 59 1/2 only seasoned "
            "conversions are drawn from the Roth"
        )
    hh = Household(
        year,
        date.fromisoformat(str(profile["birth_date"])),
        date.fromisoformat(str(spouse_raw)) if joint else None,
        float(profile.get("ira_access_age") or ROTH_AGE),
        ss,
        (st.roth_contributions or 0.0) + st.seasoned,
    )
    fs = FILING[str(profile.get("filing_status") or "single")]
    try:
        pj = project(lay, year, overrides)
        rate = pj.result.state_tax / pj.result.agi if pj.result.agi > 0 else 0.0
        out.notes.append(
            f"state tax at this year's share of AGI, {rate:.2%} (the engine's "
            "projection), every year"
        )
    except (MissingInputError, OverrideError) as exc:
        rate = None
        out.notes.append(f"state tax left out of the path (not 0 owed): {exc}")
    rl = rules(year, fs, rate)
    inflation = float(profile["inflation"])
    ret_track, ret_floor = (
        float(profile["return_track"]),
        float(profile["return_floor"]),
    )
    try:
        debts = goals_.load(lay).debts
    except goals_.GoalsError as exc:
        debts = []
        out.notes.append(f"debts left out: goals refused: {exc}")
    if debts and profile.get("mortgage_monthly"):
        out.notes.append(
            "mortgage_monthly is typed and debts are entered: a mortgage in both "
            "is paid twice on this path"
        )
    held = reserve.load(lay, year, as_of, profile, overrides).amount
    years = max(horizon_age - (year - hh.birth.year) + 1, 1)
    base_debt, out.debt_lines = debt_schedule(debts, as_of, years, inflation)
    hot_debt, _ = debt_schedule(debts, as_of, years, STRESS_INFLATION)
    for who, birth in (("you", hh.birth), ("spouse", hh.spouse_birth)):
        owner = "self" if who == "you" else "spouse"
        if birth is None or not any(
            a.kind == "deferred" and a.owner == owner for a in accts
        ):
            continue
        start = rmd_age(birth)
        out.rmd_lines.append(
            f"RMDs ({who}): from age {start} in {birth.year + start}, last year-end "
            f"balance / Pub. 590-B Table III ({divisor(start)} at {start})"
        )
    for a in accts:
        if a.kind == "inherited":
            out.rmd_lines.append(
                f"inherited IRA {a.number}: drawn in equal parts, empty by "
                f"{a.last_year}"
                if a.last_year
                else f"inherited IRA {a.number}: date of death not entered, not "
                "drained on a schedule"
            )
    cases = (
        ("on track", ret_track, inflation, base_debt, 0.0),
        ("30% drop in year one", ret_track, inflation, base_debt, STRESS_DROP),
        (
            "5% inflation",
            ret_track - max(STRESS_INFLATION - inflation, 0.0),
            STRESS_INFLATION,
            hot_debt,
            0.0,
        ),
        ("floor returns", ret_floor, inflation, base_debt, 0.0),
    )
    for name, ret, infl, dt, drop in cases:
        rows = run(accts, hh, sp, rl, dt, held, ret, infl, horizon_age, drop)
        if name == "on track":
            out.rows = rows
        else:
            out.cases.append(
                Case(
                    name,
                    runs_out(rows),
                    rows[-1].total,
                    r(sum(rw.fed + rw.state for rw in rows)),
                )
            )
        if name == "floor returns":
            out.floor_rows = rows
    if (age := runs_out(out.rows)) is not None:
        out.notes.append(
            f"on track, the accounts stop covering the year at age {age}: see the "
            "short column"
        )
    return out


def row_cells(rw: Row, floor: Row) -> list[str]:
    cells = [str(rw.year), str(rw.age)]
    cells += [f"{rw.balances[k]:,.2f}" for k in COLUMNS]
    cells += [
        f"{v:,.2f}"
        for v in (
            rw.total,
            rw.nominal,
            rw.ss,
            rw.spend,
            rw.debt,
            rw.rmd,
            rw.drawn,
            rw.fed + rw.state,
            rw.short,
            floor.total,
        )
    ]
    return cells


HEADERS = (
    ["year", "age"]
    + [LABEL.get(k, k) for k in COLUMNS]
    + [
        "on-track real",
        "on-track nominal",
        "SS",
        "spend",
        "debt",
        "RMD",
        "drawn",
        "tax",
        "short",
        "comfort-floor real",
    ]
)


def table_rows(p: Path) -> list[list[str]]:
    return [row_cells(a, b) for a, b in zip(p.rows, p.floor_rows, strict=True)]
