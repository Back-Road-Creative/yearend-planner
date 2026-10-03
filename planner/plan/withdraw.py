"""Raising cash: given a cash target and a gain budget, pick what to sell.
Cash accounts first; then taxable lots, loss lots before gains and the
highest-basis long-term lots before the rest (the least gain per dollar
raised); a specific-ID list goes first. Every candidate is priced through the
engine so the MAGI and tax effect is verified, not estimated."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta

from planner.engine.tax import r
from planner.ledger import db, portfolio
from planner.paths import Layout
from planner.plan import washsale
from planner.plan.inputs import Overrides
from planner.plan.magi import project

SELLABLE = ("taxable",)


@dataclass(frozen=True)
class Sale:
    account: str
    symbol: str
    acquired: str
    term: str
    quantity: float
    proceeds: float
    basis: float

    @property
    def gain(self) -> float:
        return r(self.proceeds - self.basis)


@dataclass
class Withdrawal:
    year: int
    as_of: str
    target: float
    cash_now: float
    need: float  # target - cash_now, floored at zero
    from_cash: float
    sales: list[Sale] = field(default_factory=list)
    proceeds: float = 0.0
    gain_st: float = 0.0
    gain_lt: float = 0.0
    short: float = 0.0  # need the sales could not cover inside the budget
    magi_before: float = 0.0
    magi_after: float = 0.0
    tax_before: float = 0.0
    tax_after: float = 0.0
    notes: list[str] = field(default_factory=list)


def _cash_target(lay: Layout) -> float:
    from planner.ingest.needs import load_profile

    return float(load_profile(lay).get("cash_target") or 0)


def _key(lot: portfolio.Lot) -> str:
    return f"{lot.account}:{lot.symbol}:{lot.acquired}"


def order(lots: list[portfolio.Lot], specific: list[str]) -> list[portfolio.Lot]:
    """Specific-ID lots first in the order given, then by gain per dollar."""
    first = [lot for key in specific for lot in lots if _key(lot) == key]
    rest = [lot for lot in lots if lot not in first and lot.value > 0]
    rest.sort(key=lambda lot: (lot.gain / lot.value, lot.term == "short"))
    return first + rest


def pick(
    lay: Layout,
    year: int,
    target: float | None = None,
    budget: float | None = None,
    as_of: date | None = None,
    specific: list[str] | None = None,
    overrides: Overrides | None = None,
) -> Withdrawal:
    """Raise ``target`` (default the profile's cash_target) with at most
    ``budget`` dollars of realized gain (default unlimited)."""
    today = as_of or date.today()
    st = portfolio.status(lay, year, today)
    base = project(lay, year, overrides)
    want = float(target) if target is not None else _cash_target(lay)
    cash = r(sum(p.value for p in st.positions if p.type == "cash"))
    need = r(max(want - cash, 0.0))
    w = Withdrawal(year, today.isoformat(), want, cash, need, r(min(cash, want)))
    typed = {p.account: p.type for p in st.positions}
    lots = [lot for lot in st.lots if typed.get(lot.account) in SELLABLE]
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        recent = {
            b.symbol
            for b in washsale.buys(conn)
            if date.fromisoformat(b.date) >= today - timedelta(days=washsale.WINDOW)
        }
    finally:
        conn.close()
    left = need
    gain_used = 0.0
    for lot in order(lots, specific or []):
        if left <= 0:
            break
        frac = min(left / lot.value, 1.0)
        gain = lot.gain * frac
        if budget is not None and gain > 0 and gain_used + gain > budget:
            frac = max((budget - gain_used) / lot.gain, 0.0) if lot.gain else 0.0
            if frac <= 0:
                continue
            gain = lot.gain * frac
        sale = Sale(
            lot.account,
            lot.symbol,
            lot.acquired,
            lot.term,
            round(lot.quantity * frac, 3),
            r(lot.value * frac),
            r(lot.basis * frac),
        )
        w.sales.append(sale)
        left = r(left - sale.proceeds)
        gain_used += max(gain, 0.0)
        if sale.gain < 0 and lot.symbol in recent:
            w.notes.append(
                f"{lot.symbol} loss lot: a buy inside the last {washsale.WINDOW} days "
                "makes this a wash sale; sell a gain lot or wait out the window"
            )
    w.proceeds = r(sum(s.proceeds for s in w.sales))
    w.gain_st = r(sum(s.gain for s in w.sales if s.term == "short"))
    w.gain_lt = r(sum(s.gain for s in w.sales if s.term != "short"))
    w.short = r(max(left, 0.0))
    ov = overrides or Overrides()
    after = project(
        lay,
        year,
        replace(
            ov,
            planned_st_sales=ov.planned_st_sales + w.gain_st,
            planned_lt_sales=ov.planned_lt_sales + w.gain_lt,
        ),
    )
    w.magi_before, w.magi_after = base.result.aca_magi, after.result.aca_magi
    w.tax_before = r(
        base.result.fed_total_tax
        - base.result.refundable_credits
        + base.result.state_tax
    )
    w.tax_after = r(
        after.result.fed_total_tax
        - after.result.refundable_credits
        + after.result.state_tax
    )
    if not lots and need > w.from_cash:
        w.notes.append("no taxable lots in the ledger (drop the cost-basis export)")
    if w.short:
        w.notes.append(
            f"{w.short:,.2f} of the target is not raised inside the gain budget"
        )
    if st.untyped:
        w.notes.append(f"{st.untyped:,.2f} sits in untyped accounts, not sellable")
    return w
