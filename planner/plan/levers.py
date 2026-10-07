"""Levers: every move left in the year that changes the tax bill or the ACA
credit, sized from the ledger and priced through the engine.

Two menus. *Get under a line* holds the moves that lower MAGI or tax (a loss
harvest, deferring a planned sale, an HSA contribution, the SE health
deduction, a deductible IRA contribution, last year's capital-loss
carryforward). Each is ranked by what it is worth inside the combined set:
the bill with every such move, against the bill with all of them but this one,
so overlapping moves are not counted twice. *Use the room* holds the moves
that add income on purpose (a Roth conversion, a 0% gain harvest, an
inherited-IRA withdrawal, and a Roth contribution, which moves no income).
They compete for the same room, so each is sized to the room under the next
line, priced alone and ranked by what each dollar moved costs now.

Net dollars are federal tax (income + SE) plus state tax minus the ACA credit,
against doing nothing. Friction is shown beside the number, never folded into
it. A lever missing an input says what it needs; nothing is guessed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any

from planner import states
from planner.config import load_thresholds
from planner.engine.household import Household, MissingInputError
from planner.engine.tax import CONFIG_PARAMS, TaxResult, compute, engine_value, r
from planner.ingest.needs import load_profile, need_values
from planner.ledger import db, portfolio
from planner.paths import Layout
from planner.plan import calendar, conversion, feasible, reserve, washsale, withdraw
from planner.plan.inputs import Inputs, OverrideError, Overrides, build
from planner.plan.magi import Line, Projection, Watch, lines, watch_for
from planner.plan.reserve import Reserve

LOWER = "get under a line"
ROOM = "use the room"
AUTOMATIC = "automatic"
TRADE = "a trade"
WASH = "a trade inside a wash-sale window"
CASH = "needs outside cash"
IRREVERSIBLE = "irreversible"
DO_NOTHING = "do nothing"
MEDICAID = "Medicaid 138% FPL (monthly)"
TOP = 3  # rows per menu on the plan page
# Moves that are real but not this year's numbers: shown, not priced.
ADVICE = (
    "half of SE tax and the QBI deduction are claimed automatically (already in "
    "every figure above)",
    "specific-lot selection: the cash planner already sells loss lots and the "
    "highest-basis lots first (planner withdraw)",
)
# Funds whose payouts are mostly ordinary income (interest, REIT dividends):
# better held in the IRA, with broad index funds in taxable (Pub. 550).
INEFFICIENT = ("BOND", "TREASURY", "INCOME", "REIT", "REAL ESTATE", "HIGH YIELD")
SWAP_TOLERANCE = 0.01  # a gain within 1% of the lot's value is "about 0"


@dataclass(frozen=True)
class Lever:
    key: str
    mode: str  # LOWER | ROOM
    label: str
    amount: float  # dollars moved; 0 when not available
    deadline: str
    friction: str
    why: str  # how it was sized, or what it needs
    side_effects: str = ""
    delta: tuple[tuple[str, int], ...] = ()  # Household field -> dollars added
    priced: bool = True  # False: moves no income (a Roth contribution)
    overlaps: tuple[str, ...] = ()  # levers moving the same dollars: not stacked
    cap: float | None = None  # the most --set may size it to; None: its amount
    need: feasible.Need = feasible.Need()  # cash, shares, gain, IRA room it takes

    @property
    def available(self) -> bool:
        return self.amount > 0 and (bool(self.delta) or not self.priced)

    def apply(self, hh: Household) -> Household:
        return replace(hh, **{k: getattr(hh, k) + v for k, v in self.delta})

    def resized(self, amount: float) -> Lever:
        """The same move at a chosen size (``whatif --set``)."""
        scale = amount / self.amount
        return replace(
            self,
            amount=r(amount),
            delta=tuple((k, int(round(v * scale))) for k, v in self.delta),
            why=f"set to {amount:,.0f} (sized {self.amount:,.0f}: {self.why})",
            need=self.need.scaled(scale),
        )


@dataclass(frozen=True)
class Row:
    lever: Lever | None  # None: do nothing
    net: float  # saved (+) or spent (-) now
    result: TaxResult
    crosses: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return self.lever.key if self.lever else DO_NOTHING

    @property
    def rate(self) -> float | None:
        """What each dollar moved costs now (room levers)."""
        if not self.lever or self.lever.mode != ROOM or not self.lever.priced:
            return None
        return round(-self.net / self.lever.amount, 4)


@dataclass(frozen=True)
class Target:
    """A conversion that lands income on a coverage line on purpose (Medicaid
    or the marketplace credit), priced by the engine for the owner to choose."""

    name: str
    amount: float
    aca_magi: float
    fed_delta: float
    state_delta: float
    ptc_delta: float
    medicaid_months: int  # of 12, under the Medicaid line by month
    note: str


TARGETS = ("medicaid_under", "medicaid_over", "aca_400")


@dataclass(frozen=True)
class Swap:
    """A tax-inefficient fund in a taxable account that can be sold for about
    no tax now: its ordinary payouts leave next year's MAGI."""

    account: str
    symbol: str
    name: str
    value: float
    gain: float


def swaps(st: portfolio.Status) -> tuple[list[Swap], list[str]]:
    typed = {p.account: p.type for p in st.positions}
    found: list[Swap] = []
    notes: list[str] = []
    for lot in st.lots:
        name = lot.name.upper()
        if typed.get(lot.account) != "taxable" or not any(
            k in name for k in INEFFICIENT
        ):
            continue
        if abs(lot.gain) <= SWAP_TOLERANCE * lot.value:
            found.append(Swap(lot.account, lot.symbol, lot.name, lot.value, lot.gain))
        else:
            notes.append(
                f"{lot.symbol} ({lot.name}) in {lot.account}: selling realizes "
                f"{lot.gain:,.0f} of gain; hold it, or move new money into a broad "
                "index fund instead"
            )
    return found, notes


def swap_lines(found: list[Swap]) -> list[str]:
    return [
        f"  swap {s.symbol} in {s.account} ({s.value:,.0f}, gain {s.gain:+,.0f}): "
        "sell for about no tax and buy a broad index fund; hold the "
        f"{s.name.title()} in the IRA instead, so its payouts leave next year's MAGI"
        for s in found
    ]


def targeting(lay: Layout, year: int, ov: Overrides) -> tuple[list[Target], list[str]]:
    """The Medicaid line beside the just-under-400% cliff. Never selected by
    the optimizer: Medicaid tests income month by month when you apply."""
    try:
        sz = conversion.size(lay, year, replace(ov, conversion_target="manual"))
    except (MissingInputError, OverrideError) as exc:
        return [], [f"income targeting: {exc}"]
    if sz.trad_ira_balance is None:
        return [], []
    out = []
    for c in sz.candidates:
        if c.name not in TARGETS:
            continue
        under = sz.recurring_monthly <= sz.medicaid_month
        months = (12 - int(c.medicaid_month_over)) if under else 0
        out.append(
            Target(
                c.name,
                c.amount,
                c.aca_magi,
                c.fed_delta,
                c.state_delta,
                c.ptc_delta,
                months,
                c.note,
            )
        )
    out.sort(key=lambda t: TARGETS.index(t.name))
    missing = [n for n in TARGETS if n not in {t.name for t in out}]
    return out, [f"income targeting: {n} is out of reach this year" for n in missing]


@dataclass
class Menu:
    year: int
    as_of: str
    inputs: Inputs
    base: TaxResult
    lines: list[Line]
    target: Line | None  # the nearest get-under line you are over
    room: float | None  # income dollars to the next line
    room_line: Line | None
    levers: list[Lever] = field(default_factory=list)
    lower: list[Row] = field(default_factory=list)
    rooms: list[Row] = field(default_factory=list)
    together: Row | None = None
    targeting: list[Target] = field(default_factory=list)
    swaps: list[Swap] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class WhatIf:
    year: int
    applied: list[Lever]
    before: TaxResult
    after: TaxResult
    lines_before: list[Line]
    lines_after: list[Line]
    short: str | None = None  # the set takes more cash than is spare

    @property
    def net(self) -> float:
        return r(cost(self.before) - cost(self.after))


def cost(res: TaxResult) -> float:
    return r(res.fed_total_tax + res.state_tax - res.refundable_credits - res.aca_ptc)


def crossings(before: list[Line], after: list[Line]) -> tuple[str, ...]:
    return tuple(
        f"{a.name} ({'now over' if a.over else 'now under'})"
        for b, a in zip(before, after, strict=True)
        if b.over != a.over
    )


def income_room(ln: Line, res: TaxResult, std: float) -> float:
    """Income dollars before ``ln`` is crossed (negative: over by that much)."""
    if ln.measure == "ACA MAGI / 12":
        return r(ln.room * 12)
    if ln.measure == "taxable income" and res.taxable_income <= 0:
        return r(ln.limit + max(std - res.agi, 0.0))
    return ln.room


def year_thresholds(lay: Layout, year: int) -> tuple[dict[str, float], list[str]]:
    rows = load_thresholds(lay.config / "thresholds.yaml")
    have = sorted(y for y in rows if y <= year)
    if not have:
        raise MissingInputError(f"config/thresholds.yaml has no values for {year}")
    notes = []
    if have[-1] != year:
        notes.append(
            f"limits for {year} not in config/thresholds.yaml: {have[-1]} values "
            "used (planner thresholds)"
        )
    th = {k: v["value"] for k, v in rows[have[-1]].items()}
    # a hand year lists only the rows the engine lacks; the engine's own rows
    # come from the installed engine when its file does not cover this year
    for name in CONFIG_PARAMS:
        th.setdefault(name, engine_value(name, have[-1])[0])
    return th, notes


def _year_end(year: int) -> str:
    return calendar.shift(date(year, 12, 31), calendar.PRIOR).isoformat()


def _filing(year: int) -> str:
    return calendar.shift(date(year + 1, 4, 15)).isoformat()


def _phase(th: dict[str, float], kind: str, status: str) -> tuple[float, float]:
    # Pub. 590-A Table 1-2 and the Roth worksheet: "married filing jointly or
    # qualifying surviving spouse"
    key = "joint" if status in ("JOINT", "SURVIVING_SPOUSE") else "single"
    if status == "SEPARATE":
        return 0.0, 10000.0  # IRC 219(g)(2)(A)(iii), 408A(c)(3)(B)(ii)
    return float(th[f"{kind}_phaseout_{key}_start"]), float(
        th[f"{kind}_phaseout_{key}_width"]
    )


def _phased(limit: float, magi: float, start: float, width: float) -> float:
    """IRS phase-out: reduced pro rata, rounded up to $10, never under $200."""
    if magi <= start:
        return limit
    left = limit * max(start + width - magi, 0.0) / width
    if left <= 0:
        return 0.0
    return max(math.ceil(left / 10) * 10, 200.0)


@dataclass
class _Ctx:
    lay: Layout
    year: int
    today: date
    ov: Overrides
    inputs: Inputs
    base: TaxResult
    lines: list[Line]
    th: dict[str, float]
    st: portfolio.Status
    profile: dict[str, Any]
    recent: dict[str, str]  # symbol -> last buy date inside the wash window
    carryforward: float | None
    giving: float | None  # planned_giving: the year's cash gifts to charity
    roth_ytd: float
    room: float | None
    room_line: Line | None
    hsa_employer: float  # W-2 box 12 code W: counts against the HSA limit
    watch: Watch  # what the watched lines read beyond the engine result
    reserve: Reserve  # the cash no move may spend (unit 4b)

    @property
    def hh(self) -> Household:
        return self.inputs.household

    def lots(self, gain: bool) -> list[portfolio.Lot]:
        typed = {p.account: p.type for p in self.st.positions}
        return [
            lot
            for lot in self.st.lots
            if typed.get(lot.account) == "taxable"
            and (lot.gain > 0) == gain
            and lot.gain != 0
        ]

    def balance(self, kind: str) -> float:
        return r(sum(p.value for p in self.st.positions if p.type == kind))

    def earned(self) -> float:
        return r(self.hh.wages + max(self.hh.se_income - self.base.se_tax / 2, 0.0))

    def funds(self) -> feasible.Funds:
        used = self.hh.traditional_ira_contribution + self.roth_ytd
        left = min(_ira_limit(self), max(self.earned(), 0.0)) - used
        return feasible.with_ira(feasible.funds(self.st, self.reserve), left)


def _none(key: str, mode: str, label: str, deadline: str, why: str) -> Lever:
    return Lever(key, mode, label, 0.0, deadline, AUTOMATIC, why)


Part = tuple[portfolio.Lot, float]  # a loss lot and the loss taken from it


def _realized(c: _Ctx) -> int:
    return max(c.hh.short_term_gains + c.hh.long_term_gains, 0)


def _loss_parts(c: _Ctx) -> tuple[list[Part], list[Part]]:
    """The taxable losses split in two: the part that nets the year's realized
    gain to zero (lots clear of a wash window first, then the biggest loss),
    and the rest. A lot can be split: sell part of it."""
    left = float(_realized(c))
    losers = sorted(
        c.lots(gain=False), key=lambda lot: (lot.symbol in c.recent, lot.gain)
    )
    pair: list[Part] = []
    rest: list[Part] = []
    for lot in losers:
        use = min(-lot.gain, left)
        left -= use
        if use > 0:
            pair.append((lot, use))
        if -lot.gain - use > 0:
            rest.append((lot, -lot.gain - use))
    return pair, rest


def _loss_lever(
    c: _Ctx, key: str, label: str, parts: list[Part], why: str, side: str
) -> Lever:
    short = r(sum(loss for lot, loss in parts if lot.term == "short"))
    long = r(sum(loss for lot, loss in parts if lot.term != "short"))
    washed = sorted({lot.symbol for lot, _ in parts} & set(c.recent))
    for s in washed:
        clear = date.fromisoformat(c.recent[s]) + timedelta(days=washsale.WINDOW + 1)
        why += f"; {s} was bought {c.recent[s]}: selling before {clear} washes it"
    delta = tuple(
        (k, -int(round(v)))
        for k, v in (("short_term_gains", short), ("long_term_gains", long))
        if v
    )
    side += (
        "; lowers basis (the gain returns when sold later); no buy of the same fund "
        "in any account, IRA included, for 30 days"
    )
    return Lever(
        key,
        LOWER,
        label,
        r(short + long),
        _year_end(c.year),
        WASH if washed else TRADE,
        why,
        side,
        delta,
    )


def _pair(c: _Ctx) -> Lever:
    key, label, due = "pair_losses", "pair losses against gains", _year_end(c.year)
    gain = _realized(c)
    if not gain:
        return _none(key, LOWER, label, due, "no realized gain this year to pair")
    pair, _ = _loss_parts(c)
    if not pair:
        return _none(key, LOWER, label, due, "no taxable lot is below its basis")
    amount = r(sum(loss for _, loss in pair))
    symbols = ", ".join(sorted({lot.symbol for lot, _ in pair}))
    why = f"sell {amount:,.0f} of loss in {symbols}: "
    why += (
        f"nets the year's realized gain of {gain:,.0f} to 0"
        if amount >= gain
        else f"covers {amount:,.0f} of the year's {gain:,.0f} realized gain"
    )
    return _loss_lever(
        c, key, label, pair, why, "the paired gains are no longer taxed this year"
    )


def _harvest(c: _Ctx) -> Lever:
    key, label, due = "harvest_losses", "tax-loss harvest", _year_end(c.year)
    losers = c.lots(gain=False)
    if not losers:
        return _none(key, LOWER, label, due, "no taxable lot is below its basis")
    _, rest = _loss_parts(c)
    if not rest:
        return _none(
            key, LOWER, label, due, "every loss nets this year's gains (pair_losses)"
        )
    lots_ = {id(lot) for lot, _ in rest}
    short = r(sum(loss for lot, loss in rest if lot.term == "short"))
    long = r(sum(loss for lot, loss in rest if lot.term != "short"))
    why = (
        f"{len(lots_)} lot(s) below basis in "
        f"{', '.join(sorted({lot.symbol for lot, _ in rest}))}: short {short:,.0f}, "
        f"long {long:,.0f}"
    )
    if _realized(c):
        why += " (past what pair_losses nets against the year's gains)"
    return _loss_lever(
        c,
        key,
        label,
        rest,
        why,
        "loss past this year's gains and 3,000 of income carries forward",
    )


def _gift_lots(c: _Ctx, amount: float) -> tuple[float, float, list[str]]:
    """Long-term taxable lots with a gain, the most gain per dollar first, to
    cover ``amount``: (value given, gain in it, symbols)."""
    held = sorted(
        (lot for lot in c.lots(gain=True) if lot.term != "short"),
        key=lambda lot: -lot.gain / lot.value,
    )
    value = gain = 0.0
    symbols: list[str] = []
    for lot in held:
        if value >= amount:
            break
        use = min(lot.value, amount - value)
        value += use
        gain += lot.gain * use / lot.value
        symbols.append(lot.symbol)
    return r(value), r(gain), sorted(set(symbols))


def _giving(c: _Ctx, key: str, label: str) -> Lever | float:
    due = _year_end(c.year)
    if c.giving is None:
        return _none(key, LOWER, label, due, "needs planned_giving (planner needed)")
    if c.giving <= 0:
        return _none(key, LOWER, label, due, "no gift to charity planned this year")
    return float(c.giving)


def _itemized(res: TaxResult) -> str:
    if res.itemizes:
        return (
            f"itemized {res.itemized_deductions:,.0f} beats the standard "
            f"{res.standard_deduction:,.0f}"
        )
    return (
        f"the standard deduction is larger ({res.standard_deduction:,.0f} vs "
        f"{res.itemized_deductions:,.0f} itemized): a gift is worth only what the "
        "engine shows"
    )


def _donate(c: _Ctx) -> Lever:
    key, label = "donate_shares", "give shares instead of cash"
    gift = _giving(c, key, label)
    if isinstance(gift, Lever):
        return gift
    value, gain, symbols = _gift_lots(c, gift)
    if not value:
        return _none(
            key,
            LOWER,
            label,
            _year_end(c.year),
            "no long-term taxable lot with a gain to give",
        )
    amount = int(round(value))
    return Lever(
        key,
        LOWER,
        label,
        float(amount),
        _year_end(c.year),
        TRADE,
        f"give {amount:,} of {', '.join(symbols)} (held over a year) in place of "
        f"the cash gift: deducted at market value, {gain:,.0f} of gain is never "
        f"taxed; {_itemized(c.base)}",
        "shares count up to 30% of AGI (cash up to 60%), the rest carries forward "
        "5 years; the charity or fund must take a share transfer",
        (("charitable_cash", -amount), ("charitable_shares", amount)),
        need=feasible.Need(shares=float(amount)),
    )


def _daf(c: _Ctx) -> Lever:
    key, label = "daf_bunch", "bunch next year's gift into a donor-advised fund"
    gift = _giving(c, key, label)
    if isinstance(gift, Lever):
        return gift
    amount = int(round(gift))
    value, _, _ = _gift_lots(c, gift)
    field_ = "charitable_shares" if value >= gift else "charitable_cash"
    after = compute(c.year, replace(c.hh, **{field_: getattr(c.hh, field_) + amount}))
    return Lever(
        key,
        LOWER,
        label,
        float(amount),
        _year_end(c.year),
        IRREVERSIBLE,
        f"give next year's {amount:,} (this year's gift again) now through a "
        f"donor-advised fund ({'shares' if field_ == 'charitable_shares' else 'cash'})"
        f": two years' gifts in one; {_itemized(after)}",
        "the fund grants it to charities later, on your schedule; next year you "
        "take the standard deduction",
        ((field_, amount),),
        need=feasible.Need(
            **{"shares" if field_ == "charitable_shares" else "cash": float(amount)}
        ),
    )


def zero_magi_funds(st: portfolio.Status) -> tuple[float, float]:
    """Cash, and Roth basis (contributions plus seasoned conversions, no more
    than the Roth balance): money spent with no MAGI."""
    cash = r(sum(p.value for p in st.positions if p.type == "cash"))
    basis = (st.roth_contributions or 0.0) + st.seasoned
    return cash, r(min(st.roth_balance, basis))


def _spend_basis(c: _Ctx) -> Lever:
    key, label, due = "spend_basis", "spend cash or Roth basis first", _year_end(c.year)
    cash, basis = zero_magi_funds(c.st)
    funds = f"{cash:,.0f} of cash and {basis:,.0f} of Roth basis"
    planned = [
        (k, v)
        for k, v in (
            ("short_term_gains", c.ov.planned_st_sales),
            ("long_term_gains", c.ov.planned_lt_sales),
        )
        if v > 0
    ]
    if not planned:
        return _none(
            key,
            LOWER,
            label,
            due,
            f"no planned sale to replace (--st/--lt); {funds} spend with no MAGI",
        )
    if cash + basis <= 0:
        return _none(key, LOWER, label, due, "no cash and no Roth basis to spend")
    lots = withdraw.sellable(c.st)
    left, spent, delta, avoided, unmatched = cash + basis, 0.0, [], 0.0, 0.0
    for k, v in planned:
        term = "short" if k == "short_term_gains" else "long"
        gain, use, none = withdraw.gain_avoided(lots, v, term, left)
        left, spent, unmatched = left - use, spent + use, unmatched + none
        avoided += gain
        if gain > 0:
            delta.append((k, -int(round(gain))))
    amount = r(avoided)
    if amount <= 0:
        return _none(
            key,
            LOWER,
            label,
            due,
            f"no taxable lot supplies the planned gain; {funds} spend with no MAGI",
        )
    short = f"; {unmatched:,.0f} of it no lot supplies" if unmatched else ""
    return Lever(
        key,
        LOWER,
        label,
        amount,
        due,
        AUTOMATIC,
        f"spend from {funds} in place of {spent:,.0f} of sale proceeds: the lots "
        f"left unsold hold {amount:,.0f} of the planned gain, never realized (no "
        f"MAGI){short}",
        "the cash or Roth basis is gone for later; Roth contributions come out "
        "first and tax-free, conversions only once seasoned (5 years)",
        tuple(delta),
        overlaps=("defer_sales",),
        need=feasible.Need(cash=r(min(spent, cash))),
    )


def _defer(c: _Ctx) -> Lever:
    key, label, due = "defer_sales", "defer the planned sale", _year_end(c.year)
    planned = [
        (k, v)
        for k, v in (
            ("short_term_gains", c.ov.planned_st_sales),
            ("long_term_gains", c.ov.planned_lt_sales),
        )
        if v > 0
    ]
    if not planned:
        return _none(key, LOWER, label, due, "no planned gain this year (--st/--lt)")
    amount = r(sum(v for _, v in planned))
    return Lever(
        key,
        LOWER,
        label,
        amount,
        due,
        TRADE,
        f"move {amount:,.0f} of planned gain to January",
        "the gain lands in next year's MAGI instead; the price can move meanwhile",
        tuple((k, -int(round(v))) for k, v in planned),
    )


def _hsa(c: _Ctx) -> Lever:
    key, label, due = "hsa", "HSA contribution", _filing(c.year)
    cover = c.profile.get("hsa_coverage")
    if cover is None:
        return _none(key, LOWER, label, due, "needs hsa_coverage (planner needed)")
    if cover == "none":
        return _none(key, LOWER, label, due, "no HSA-eligible plan this year")
    limit = float(c.th[f"hsa_limit_{cover}"])
    if c.hh.age >= 55:
        limit += float(c.th["hsa_catchup_55plus"])
    amount = r(max(limit - c.hsa_employer - c.hh.hsa_contribution, 0.0))
    if not amount:
        return _none(key, LOWER, label, due, f"already at the {limit:,.0f} limit")
    return Lever(
        key,
        LOWER,
        label,
        amount,
        due,
        CASH,
        f"{cover} limit {limit:,.0f}"
        f"{' with the 55+ catch-up' if c.hh.age >= 55 else ''} less "
        f"{c.hsa_employer + c.hh.hsa_contribution:,.0f} already in"
        f"{f' ({c.hsa_employer:,.0f} through payroll)' if c.hsa_employer else ''}",
        "pro-rated for months without HSA-eligible coverage",
        (("hsa_contribution", int(round(amount))),),
        need=feasible.Need(cash=amount),
    )


def _se_health(c: _Ctx) -> Lever:
    key, label, due = "se_health", "SE health insurance deduction", _filing(c.year)
    if c.hh.se_income <= 0:
        return _none(key, LOWER, label, due, "no self-employment income")
    monthly = c.profile.get("premium_monthly")
    if monthly is None:
        return _none(key, LOWER, label, due, "needs premium_monthly (planner needed)")
    # premium_monthly is what you pay after the advance credit; the household
    # carries the premiums before it (the engine takes the credit off, Pub. 974)
    gross = r(float(monthly) * 12 + c.hh.aptc)
    amount = r(gross - c.hh.se_health_premiums)
    if amount <= 0:
        return _none(key, LOWER, label, due, "already claimed in full")
    return Lever(
        key,
        LOWER,
        label,
        amount,
        due,
        AUTOMATIC,
        f"premiums for the year {gross:,.0f} (your {float(monthly) * 12:,.0f} plus "
        f"the {c.hh.aptc:,.0f} advance credit) less "
        f"{c.hh.se_health_premiums:,.0f} already counted",
        "the deduction is the premiums less the premium tax credit you are allowed "
        "and no more than your profit less half the SE tax; the two settle together "
        "(IRS Pub. 974), so the move is worth less than the premium; not allowed for "
        "months you could join an employer's plan",
        (("se_health_premiums", int(round(amount))),),
    )


def _ira_limit(c: _Ctx) -> float:
    limit = float(c.th["ira_contribution_limit"])
    return limit + (float(c.th["ira_catchup_50plus"]) if c.hh.age >= 50 else 0.0)


def _traditional_ira(c: _Ctx) -> Lever:
    key, label, due = "traditional_ira", "deductible IRA contribution", _filing(c.year)
    earned = c.earned()
    if earned <= 0:
        return _none(key, LOWER, label, due, "needs earned income (wages or SE)")
    limit = _ira_limit(c)
    already = c.hh.traditional_ira_contribution
    left = r(min(limit, earned) - already - c.roth_ytd)
    if left <= 0:
        return _none(key, LOWER, label, due, "this year's IRA limit is used")
    plan = c.profile.get("workplace_plan")
    if plan is None:
        return _none(key, LOWER, label, due, "needs workplace_plan (planner needed)")
    why = f"limit {limit:,.0f} (earned {earned:,.0f}) less {already + c.roth_ytd:,.0f}"
    if plan == "yes":
        start, width = _phase(c.th, "ira", c.hh.filing_status)
        allowed = _phased(limit, c.base.agi + already, start, width)
        left = r(min(left, max(allowed - already, 0.0)))
        why += f"; with a workplace plan {allowed:,.0f} is deductible"
        if left <= 0:
            return _none(key, LOWER, label, due, why + ": none left")
    return Lever(
        key,
        LOWER,
        label,
        left,
        due,
        CASH,
        why + " already in",
        "traditional and Roth IRA contributions share one limit; converting this "
        "money later is taxed then",
        (("traditional_ira_contribution", int(round(left))),),
        need=feasible.Need(cash=left, ira=left),
    )


def _carryforward(c: _Ctx) -> Lever:
    key, label, due = "carryforward", "capital-loss carryforward", _filing(c.year)
    if c.carryforward is None:
        return _none(
            key,
            LOWER,
            label,
            due,
            "needs prior_capital_loss_carryforward (last year's Schedule D)",
        )
    if c.carryforward <= 0:
        return _none(key, LOWER, label, due, "no carryforward from last year")
    amount = r(c.carryforward)
    return Lever(
        key,
        LOWER,
        label,
        amount,
        due,
        AUTOMATIC,
        f"last year's unused loss {amount:,.0f}: offsets gains, then 3,000 of income",
        "priced as long-term; a short-term carryforward offsets short-term gains "
        "first (same AGI)",
        (("long_term_gains", -int(round(amount))),),
    )


def _roomed(c: _Ctx, available: float) -> tuple[float, str]:
    if c.room is None or c.room_line is None or c.room <= 0:
        return 0.0, "every watched line is already crossed: no room to fill"
    amount = r(min(available, c.room))
    return amount, f"fills to the {c.room_line.name} ({c.room:,.0f} of room)"


def _conversion(c: _Ctx) -> Lever:
    key, label, due = "conversion", "Roth conversion", _year_end(c.year)
    balance = c.st.convertible
    if not balance:
        return _none(key, ROOM, label, due, "no traditional IRA balance")
    if c.profile.get("conversion_objective") is None:
        amount, why = _roomed(c, balance)
        if amount <= 0:
            return _none(key, ROOM, label, due, why)
        return _conversion_lever(c, amount, why, balance)
    # sized from the resolved household: an adopted (auto) conversion is part of
    # ``already``, so only what is left beyond it is proposed
    sz = conversion.size(
        c.lay,
        c.year,
        replace(c.ov, conversion_target="manual"),
        as_of=c.today,
        rs=c.reserve,
    )
    rec = sz.recommendation
    if rec is None:
        return _none(key, ROOM, label, due, "; ".join(sz.notes) or "nothing to size")
    amount = r(rec.amount - sz.already)
    if amount <= 0:
        return _none(key, ROOM, label, due, f"objective {rec.name}: already met")
    return _conversion_lever(
        c, amount, f"objective {rec.name}: {rec.note}", balance - sz.already
    )


def _conversion_lever(c: _Ctx, amount: float, why: str, cap: float) -> Lever:
    effects = (
        "taxed now as ordinary income; each conversion starts its own 5-year clock "
        "before it can be spent penalty-free"
    )
    now, nxt = c.watch.nc_rate, c.watch.nc_next_rate
    if now and nxt is not None and nxt < now:
        effects += (
            f"; NC tax on it would be {round(amount * (now - nxt)):,} lower in "
            f"{c.year + 1} at {nxt:.2%} (scheduled): a conversion that can wait "
            "saves that"
        )
    return Lever(
        "conversion",
        ROOM,
        "Roth conversion",
        amount,
        _year_end(c.year),
        IRREVERSIBLE,
        why,
        effects,
        (("roth_conversion", int(round(amount))),),
        cap=r(cap),  # past the room is the owner's call, past the balance is not
    )


def _gain_harvest(c: _Ctx) -> Lever:
    key, label, due = "gain_harvest", "0% gain harvest", _year_end(c.year)
    winners = [lot for lot in c.lots(gain=True) if lot.term != "short"]
    if not winners:
        return _none(key, ROOM, label, due, "no long-term gain in taxable lots")
    zero = next(ln for ln in c.lines if ln.name.startswith("0% LTCG"))
    std = next(ln.limit for ln in c.lines if ln.name == "standard deduction")
    to_zero = income_room(zero, c.base, std)
    if to_zero <= 0:
        return _none(key, ROOM, label, due, "already past the 0% gain ceiling")
    gains = r(sum(lot.gain for lot in winners))
    amount, why = _roomed(c, min(gains, to_zero))
    if amount <= 0:
        return _none(key, ROOM, label, due, why)
    return Lever(
        key,
        ROOM,
        label,
        amount,
        due,
        TRADE,
        f"{why}; {gains:,.0f} of long-term gain available",
        "sell and buy back at once (no wash rule for gains): basis steps up; NC "
        "still taxes the gain; ACA MAGI rises",
        (("long_term_gains", int(round(amount))),),
        cap=gains,
        need=feasible.Need(gain=amount),
    )


def _inherited(c: _Ctx) -> Lever:
    key, label, due = "inherited_ira", "inherited-IRA withdrawal", _year_end(c.year)
    balance = c.balance("inherited_ira")
    if not balance:
        return _none(key, ROOM, label, due, "no inherited IRA")
    amount, why = _roomed(c, balance)
    if amount <= 0:
        return _none(key, ROOM, label, due, why)
    ends = ", ".join(
        f"{acct} empty by {by}"
        + (f", {rmd}" if (rmd := portfolio.rmd_text(c.st, acct)) else "")
        for acct, _, by in c.st.inherited
    )
    return Lever(
        key,
        ROOM,
        label,
        amount,
        due,
        IRREVERSIBLE,
        why + (f"; {ends}" if ends else ""),
        "ordinary income; the 10-year window (and yearly RMDs if the owner had "
        "started them) still applies",
        (("ira_distributions", int(round(amount))),),
        cap=r(balance),
    )


def _roth_contribution(c: _Ctx) -> Lever:
    key, label, due = "roth_contribution", "Roth IRA contribution", _filing(c.year)
    earned = c.earned()
    if earned <= 0:
        return _none(key, ROOM, label, due, "needs earned income (wages or SE)")
    limit = _ira_limit(c)
    start, width = _phase(c.th, "roth", c.hh.filing_status)
    allowed = _phased(limit, c.base.aca_magi, start, width)
    used = r(c.roth_ytd + c.hh.traditional_ira_contribution)
    left = r(min(allowed, earned) - used)
    if left <= 0:
        return _none(key, ROOM, label, due, "no Roth contribution room left")
    return Lever(
        key,
        ROOM,
        label,
        left,
        due,
        CASH,
        f"limit {allowed:,.0f} less {used:,.0f} already contributed; no tax effect "
        "this year",
        "spendable at any age (contributions, not earnings); shares the IRA limit",
        priced=False,
        need=feasible.Need(cash=left, ira=left),
    )


BUILDERS = (
    _pair,
    _harvest,
    _spend_basis,
    _defer,
    _donate,
    _daf,
    _hsa,
    _se_health,
    _traditional_ira,
    _carryforward,
    _conversion,
    _gain_harvest,
    _inherited,
    _roth_contribution,
)


def _context(
    lay: Layout, year: int, today: date, ov: Overrides
) -> tuple[_Ctx, list[str]]:
    inputs = build(lay, year, ov)
    base = compute(year, inputs.household)
    watch = watch_for(lay, year, inputs)
    watched = lines(base, inputs.household.filing_status, watch)
    th, notes = year_thresholds(lay, year)
    std = next(ln.limit for ln in watched if ln.name == "standard deduction")
    room_line, room = None, None
    for ln in watched:
        gap = income_room(ln, base, std)
        if ln.direction != "watch" and gap > 0 and (room is None or gap < room):
            room_line, room = ln, gap
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        start = today - timedelta(days=washsale.WINDOW)
        recent: dict[str, str] = {}
        for b in washsale.buys(conn):
            if start <= date.fromisoformat(b.date) <= today:
                recent[b.symbol] = max(recent.get(b.symbol, ""), b.date)
        got = need_values(
            conn,
            lay,
            year,
            (
                "prior_capital_loss_carryforward",
                "hsa_employer_contributions",
                "planned_giving",
            ),
        )
        carry = got["prior_capital_loss_carryforward"]
        roth_ytd = r(
            sum(f.value for f in db.facts_for(conn, year, "5498") if f.box == "10")
        )
    finally:
        conn.close()
    profile = load_profile(lay)
    rs = reserve.load(lay, year, today, profile, ov, Projection(inputs, base, watched))
    ctx = _Ctx(
        lay,
        year,
        today,
        ov,
        inputs,
        base,
        watched,
        th,
        portfolio.status(lay, year, today),
        profile,
        recent,
        None if carry is None else float(carry),
        None if got["planned_giving"] is None else float(got["planned_giving"]),
        roth_ytd,
        room,
        room_line,
        float(got["hsa_employer_contributions"] or 0),
        watch,
        rs,
    )
    return ctx, notes


def catalog(
    lay: Layout, year: int, as_of: date | None = None, ov: Overrides | None = None
) -> tuple[_Ctx, list[Lever], list[str]]:
    ctx, notes = _context(lay, year, as_of or date.today(), ov or Overrides())
    return ctx, [b(ctx) for b in BUILDERS], notes


def _apply(hh: Household, levers: list[Lever]) -> Household:
    for lv in levers:
        hh = lv.apply(hh)
    return hh


def menu(
    lay: Layout, year: int, as_of: date | None = None, ov: Overrides | None = None
) -> Menu:
    ctx, levers, notes = catalog(lay, year, as_of, ov)
    base, hh = ctx.base, ctx.hh
    # Year-end and filing-time moves change the annual figures; Medicaid tests
    # income month by month when you apply, so it is never the target here.
    over = [
        ln
        for ln in ctx.lines
        if ln.direction == "get under" and ln.over and ln.name != MEDICAID
    ]
    std = next(ln.limit for ln in ctx.lines if ln.name == "standard deduction")
    target = min(over, key=lambda ln: -income_room(ln, base, std), default=None)
    m = Menu(
        year,
        ctx.today.isoformat(),
        ctx.inputs,
        base,
        ctx.lines,
        target,
        ctx.room,
        ctx.room_line,
        levers,
        notes=notes,
    )
    nothing = Row(None, 0.0, base)

    def priced(res: TaxResult, lv: Lever | None, net: float) -> Row:
        after = lines(res, hh.filing_status, ctx.watch)
        return Row(lv, r(net), res, crossings(ctx.lines, after))

    f = ctx.funds()
    lower: list[Lever] = []
    alone: list[Lever] = []
    for lv in (x for x in levers if x.mode == LOWER and x.available):
        first = next((k.key for k in lower if lv.key in k.overlaps), None)
        hard = feasible.check(f, {k.key: k.need for k in [*lower, lv]}).hard
        if first is not None:
            alone.append(lv)
            m.notes.append(
                f"{lv.key} and {first} move the same dollars: {lv.key} is priced "
                "alone, not stacked in the combined set"
            )
        elif hard:
            alone.append(lv)
            m.notes.append(f"{lv.key} is priced alone, not stacked: {hard[0]}")
        else:
            lower.append(lv)
    if lower:
        together = compute(year, _apply(hh, lower))
        m.together = priced(together, None, cost(base) - cost(together))
        short = feasible.check(
            f, {k.key: k.need for k in lower}, cost(together) - cost(base)
        ).cash
        if short:
            m.notes.append(f"together the moves take more cash than is spare: {short}")
        rows = []
        for lv in lower:
            others = [o for o in lower if o is not lv]
            without = compute(year, _apply(hh, others)) if others else base
            rows.append(Row(lv, r(cost(without) - cost(together)), together))
        for lv in alone:
            res = compute(year, lv.apply(hh))
            rows.append(priced(res, lv, cost(base) - cost(res)))
        rows.sort(key=lambda rw: -rw.net)
        m.lower = [nothing, *rows]
    rooms = []
    for lv in (x for x in levers if x.mode == ROOM and x.available):
        if not lv.priced:
            rooms.append(Row(lv, 0.0, base))
            continue
        res = compute(year, lv.apply(hh))
        rooms.append(priced(res, lv, cost(base) - cost(res)))
    for rw in m.lower + rooms:
        if rw.lever is None:
            continue
        move = rw.lever
        # a stacked row's result is the whole set; its saving is a later refund
        tax = 0.0 if move in lower else cost(rw.result) - cost(base)
        why = feasible.cash_reason(f, move.need.cash, tax)
        if why:
            m.notes.append(f"{move.key}: {why}")
    unchecked = feasible.unchecked(f)
    if unchecked and any(lv.need.cash for lv in levers if lv.available):
        m.notes.append(unchecked)
    rooms.sort(key=lambda rw: (rw.rate is None, rw.rate or 0.0))
    m.rooms = [nothing, *rooms] if rooms else []
    if m.together is not None and m.together.net < 0:
        m.notes.append(
            "together the moves overshoot and cost more than doing nothing: pick "
            "a subset (planner whatif --apply a,b)"
        )
    if any(MEDICAID in c for rw in m.lower + m.rooms for c in rw.crosses) or (
        m.together is not None and any(MEDICAID in c for c in m.together.crosses)
    ):
        m.notes.append(
            "a move crosses the Medicaid line: the ACA credit lost is counted, "
            "Medicaid coverage (no premium) is not, and Medicaid tests income "
            "month by month when you apply, not at filing"
        )
    m.targeting, waiting = targeting(lay, year, ctx.ov)
    m.swaps, held = swaps(ctx.st)
    m.notes += waiting + held + list(ADVICE)
    return m


def whatif(
    lay: Layout,
    year: int,
    keys: list[str],
    amounts: dict[str, float] | None = None,
    as_of: date | None = None,
    ov: Overrides | None = None,
) -> WhatIf:
    """The full year recomputed with ``keys`` applied, before and after."""
    ctx, levers, _ = catalog(lay, year, as_of, ov)
    by_key = {lv.key: lv for lv in levers}
    chosen: list[Lever] = []
    for key in keys:
        if key not in by_key:
            raise ValueError(f"unknown lever {key}; one of {', '.join(by_key)}")
        if any(lv.key == key for lv in chosen):
            raise ValueError(f"{key} is listed twice in --apply")
        lv = by_key[key]
        if not lv.available:
            raise ValueError(f"{key} is not available: {lv.why}")
        if amounts and key in amounts:
            want, top = amounts[key], lv.amount if lv.cap is None else lv.cap
            if not (math.isfinite(want) and want > 0):
                raise ValueError(
                    f"--set {key}={want:,.0f}: the size must be more than 0"
                )
            if want > top:
                raise ValueError(
                    f"--set {key}={want:,.0f} is more than it can move "
                    f"({top:,.0f}: {lv.why})"
                )
            lv = lv.resized(want)
        clash = next(
            (o for o in chosen if lv.key in o.overlaps or o.key in lv.overlaps), None
        )
        if clash is not None:
            first, second = (lv, clash) if clash.key in lv.overlaps else (clash, lv)
            raise ValueError(
                f"{first.key} and {second.key} move the same dollars: apply one "
                "(menu prices them alone)"
            )
        chosen.append(lv)
    for key in amounts or {}:
        if key not in keys:
            raise ValueError(f"--set {key}: add it to --apply")
    f = ctx.funds()
    needs = {lv.key: lv.need for lv in chosen}
    hard = feasible.check(f, needs).hard
    if hard:
        raise ValueError(f"{'; '.join(hard)}: apply fewer (menu prices them alone)")
    after = compute(year, _apply(ctx.hh, chosen))
    status = ctx.hh.filing_status
    return WhatIf(
        year,
        chosen,
        ctx.base,
        after,
        ctx.lines,
        lines(after, status, ctx.watch),
        feasible.check(f, needs, cost(after) - cost(ctx.base)).cash,
    )


def _row_text(rw: Row) -> str:
    if rw.lever is None:
        return f"{DO_NOTHING:20} {'':>12}  net {0:>+11,.2f}"
    lv = rw.lever
    rate = f"  {rw.rate:.1%} per dollar" if rw.rate is not None else ""
    cross = f"  crosses: {'; '.join(rw.crosses)}" if rw.crosses else ""
    return (
        f"{lv.key:20} {lv.amount:>12,.2f}  net {rw.net:>+11,.2f}{rate}  "
        f"[{lv.friction}] by {lv.deadline}{cross}"
    )


def summary(m: Menu, top: int | None = None) -> list[str]:
    out = []
    std = next(ln.limit for ln in m.lines if ln.name == "standard deduction")
    if m.target is not None:
        gap = -income_room(m.target, m.base, std)
        out.append(f"get under: {m.target.name}, over by {gap:,.2f} of income")
    if m.room_line is not None and m.room is not None:
        out.append(f"room: {m.room:,.2f} of income before the {m.room_line.name}")
    if m.lower:
        out.append(f"{LOWER} (worth inside the combined set):")
        out += [f"  {_row_text(rw)}" for rw in m.lower[: (top + 1) if top else None]]
    if m.together is not None:
        cross = "; ".join(m.together.crosses) or "no line crossed"
        out.append(
            f"  together: net {m.together.net:+,.2f}; ACA MAGI "
            f"{m.base.aca_magi:,.2f} -> {m.together.result.aca_magi:,.2f}; {cross}"
        )
    if m.rooms:
        out.append(f"{ROOM} (each alone; they share the room):")
        out += [f"  {_row_text(rw)}" for rw in m.rooms[: (top + 1) if top else None]]
    if m.targeting:
        out.append("income targeting (your choice; never picked for you):")
        out += [
            f"  {t.name:16} convert {t.amount:>10,.0f}  ACA MAGI {t.aca_magi:>10,.0f}  "
            f"tax {t.fed_delta + t.state_delta:>+9,.0f}  credit {t.ptc_delta:>+9,.0f}  "
            f"Medicaid {t.medicaid_months} of 12 months ({t.note})"
            for t in m.targeting
        ]
    if m.swaps:
        out.append("tax-efficient swaps (next year's dividends, not this year's tax):")
        out += swap_lines(m.swaps)
    return out


def render_menu(m: Menu) -> str:
    out = [
        f"Levers {m.year} (as of {m.as_of}); net = federal + "
        f"{states.label(m.inputs.household.state)} tax - ACA credit, "
        "saved (+) or spent (-) against doing nothing",
        "",
        *summary(m),
        "",
    ]
    out += [
        f"{lv.key:20} {lv.why}"
        + (f"; side effects: {lv.side_effects}" if lv.side_effects else "")
        for lv in m.levers
        if lv.available
    ]
    out += [
        f"{lv.key:20} not available: {lv.why}" for lv in m.levers if not lv.available
    ]
    out += [f"note: {n}" for n in m.notes]
    return "\n".join(out) + "\n"


FIGURES = (
    ("AGI", "agi"),
    ("taxable income", "taxable_income"),
    ("ACA MAGI", "aca_magi"),
    ("FPL %", "aca_fpl_pct"),
    ("federal tax", "fed_total_tax"),
    ("state tax", "state_tax"),
    ("ACA credit", "aca_ptc"),
)


def render_whatif(w: WhatIf) -> str:
    out = [
        f"What if {w.year}: "
        + ", ".join(f"{lv.key} {lv.amount:,.0f}" for lv in w.applied),
        f"{'':22} {'before':>14} {'after':>14} {'change':>14}",
    ]
    for label, name in FIGURES:
        b, a = getattr(w.before, name), getattr(w.after, name)
        out.append(f"{label:22} {b:>14,.2f} {a:>14,.2f} {a - b:>+14,.2f}")
    out.append(f"{'net (saved +)':22} {'':>14} {'':>14} {w.net:>+14,.2f}")
    for b, a in zip(w.lines_before, w.lines_after, strict=True):
        state = {False: "under", True: "OVER"}
        out.append(
            f"{a.name:40} {state[b.over]:>5} -> {state[a.over]:5} (room {a.room:,.2f})"
        )
    for lv in w.applied:
        out.append(f"{lv.key}: [{lv.friction}] by {lv.deadline}; {lv.side_effects}")
    if w.short:
        out.append(f"cash: {w.short}")
    return "\n".join(out) + "\n"
