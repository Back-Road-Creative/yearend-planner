"""Tax-aware placement (master plan unit 4c): moves toward the household's own
target mix (planner.goals), the cheapest in tax first.

Each holding's asset class is typed (``planner classify VTSAX stocks``); a cash
account is cash unless typed otherwise, and nothing else is guessed: an
untyped holding is named and left out of the mix. The reserve
(planner.plan.reserve) stays in cash and out of the mix.

The moves, in order, each only as far as the mix needs:

  1. inside tax-advantaged accounts (IRAs, Roth, HSA, 457(b)): sell an
     over-target class and buy an under-target one in the same account. No tax.
  2. spare cash above the reserve in cash and taxable accounts buys an
     under-target class. No tax.
  3. taxable loss lots of an over-target class, the most loss per dollar
     first. A lot whose symbol was bought inside the last 30 days, in any
     account, would be a wash sale (IRC 1091, Rev. Rul. 2008-5) and is skipped.
  4. taxable gain lots, the least gain per dollar first and long-term before
     short-term (planner.plan.withdraw.order).

The sales in 3 and 4 are priced through the engine: the tax effect is the
projected tax with the sales less the tax without them.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta

from planner import goals as goals_
from planner.engine.household import MissingInputError
from planner.engine.tax import r
from planner.ledger import db, portfolio
from planner.paths import Layout
from planner.plan import reserve, washsale, withdraw
from planner.plan.inputs import OverrideError, Overrides
from planner.plan.magi import Projection, project

SHELTERED = ("trad_ira", "simple_ira", "inherited_ira", "roth", "hsa", "gov_457b")
NO_TAX = "no tax"


@dataclass(frozen=True)
class Holding:
    account: str
    kind: str  # the account's type (planner.ledger.portfolio.TYPES)
    symbol: str  # "account:<number>" for a balance typed without holdings
    value: float
    cls: str | None  # None: not typed


@dataclass(frozen=True)
class Move:
    step: str  # sheltered | cash | loss | gain
    account: str
    sell: str  # symbol or lot key sold, or "cash"
    sell_class: str
    buy_class: str
    amount: float
    gain: float = 0.0  # realized; 0 for steps 1 and 2
    term: str = ""

    def line(self) -> str:
        what = (
            f"move {self.amount:,.2f} of cash in {self.account} to {self.buy_class}"
            if self.step == "cash"
            else f"sell {self.amount:,.2f} of {self.sell} ({self.sell_class}) in "
            f"{self.account}, buy {self.buy_class}"
        )
        if self.step in ("sheltered", "cash"):
            return f"{what}: {NO_TAX}"
        kind = "loss" if self.gain < 0 else "gain"
        return f"{what}: {self.term} {kind} {self.gain:,.2f}"


@dataclass
class Placement:
    target: dict[str, float] | None
    base: float = 0.0  # classified value less the reserve held in cash
    held: float = 0.0  # the reserve kept out of the mix
    current: dict[str, float] = field(default_factory=dict)
    moves: list[Move] = field(default_factory=list)
    tax_loss: float | None = None  # engine tax effect of the loss sales
    tax_gain: float | None = None  # of the gain sales on top of them
    untyped: list[Holding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def want(self, cls: str) -> float:
        return r((self.target or {}).get(cls, 0.0) * self.base / 100)

    def lines(self) -> list[str]:
        if self.target is None:
            return [
                "no target mix chosen; the planner proposes no rebalancing "
                "(planner mix stocks=60 bonds=40)"
            ]
        out = [f"mix base {self.base:,.2f} (the reserve {self.held:,.2f} kept in cash)"]
        for cls in goals_.ASSET_CLASSES:
            have, want = self.current.get(cls, 0.0), self.want(cls)
            if not have and not want:
                continue
            pct = have / self.base * 100 if self.base else 0.0
            out.append(
                f"{cls}: {have:,.2f} ({pct:.1f}%), target {want:,.2f} "
                f"({(self.target or {}).get(cls, 0.0):g}%), "
                f"{'over' if have > want else 'under'} {abs(have - want):,.2f}"
            )
        if not self.moves:
            out.append("no move: the mix is on target or nothing can move")
        out.extend(m.line() for m in self.moves)
        for label, tax in (
            ("loss sales", self.tax_loss),
            ("gain sales", self.tax_gain),
        ):
            if tax is not None:
                out.append(f"tax effect of the {label}: {tax:+,.2f} (engine)")
        return out


def holdings(lay: Layout, st: portfolio.Status, g: goals_.Goals) -> list[Holding]:
    """Every holding with its class: the newest holdings export per account,
    and a typed balance as one holding for the whole account."""
    kinds = {p.account: p.type for p in st.positions}
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        rows = db.latest_rows(conn, "holding")
    finally:
        conn.close()
    by: dict[tuple[str, str], float] = {}
    for row in rows:
        key = (row.account, row.symbol or f"account:{row.account}")
        by[key] = by.get(key, 0.0) + db.from_cents(row.amount_cents or 0)
    for p in st.positions:
        if p.origin == "typed":
            by[(p.account, f"account:{p.account}")] = p.value
    out = []
    for (acct, sym), value in sorted(by.items()):
        kind = kinds.get(acct, portfolio.UNKNOWN)
        cls = g.classes.get(sym) or g.classes.get(f"account:{acct}")
        if cls is None and kind == "cash":
            cls = "cash"
        out.append(Holding(acct, kind, sym, r(value), cls))
    return out


def _tax(pj: Projection) -> float:
    res = pj.result
    return r(res.fed_total_tax - res.refundable_credits + res.state_tax)


class _Gaps:
    """What each class is over (positive) or under (negative) its target."""

    def __init__(self, pl: Placement) -> None:
        self.gap = {
            c: r(pl.current.get(c, 0.0) - pl.want(c))
            for c in set(pl.current) | set(pl.target or {})
        }

    def over(self, cls: str | None) -> float:
        return max(self.gap.get(cls or "", 0.0), 0.0)

    def under(self) -> list[tuple[str, float]]:
        return sorted(
            ((c, -v) for c, v in self.gap.items() if v < -0.005),
            key=lambda cv: (-cv[1], cv[0]),
        )

    def take(self, sold: str, bought: str, amount: float) -> None:
        self.gap[sold] = r(self.gap[sold] - amount)
        self.gap[bought] = r(self.gap[bought] + amount)


def _swap(
    gaps: _Gaps,
    step: str,
    h_acct: str,
    sell: str,
    cls: str,
    value: float,
    gain_per: float = 0.0,
    term: str = "",
) -> list[Move]:
    """Sell up to ``value`` of an over-target class into the under-target ones."""
    out: list[Move] = []
    left = min(value, gaps.over(cls))
    for buy, need in gaps.under():
        if left <= 0.005:
            break
        amount = r(min(left, need))
        gaps.take(cls, buy, amount)
        out.append(
            Move(step, h_acct, sell, cls, buy, amount, r(amount * gain_per), term)
        )
        left = r(left - amount)
    return out


def plan(
    lay: Layout,
    year: int,
    as_of: date | None = None,
    overrides: Overrides | None = None,
    rs: reserve.Reserve | None = None,
) -> Placement:
    """The moves toward the target mix, with the engine's tax effect."""
    today = as_of or date.today()
    g = goals_.load(lay)
    pl = Placement(g.target_mix)
    if g.target_mix is None:
        return pl
    st = portfolio.status(lay, year, today)
    hs = holdings(lay, st, g)
    pl.untyped = [h for h in hs if h.cls is None]
    if pl.untyped:
        pl.notes.append(
            "not in the mix, class not typed: "
            + ", ".join(f"{h.symbol} in {h.account} {h.value:,.2f}" for h in pl.untyped)
            + " (planner classify <symbol> <class>)"
        )
    for h in hs:
        if h.cls:
            pl.current[h.cls] = r(pl.current.get(h.cls, 0.0) + h.value)
    rs = rs or reserve.load(lay, year, today, None, overrides)
    pl.held = r(min(rs.amount, pl.current.get("cash", 0.0)))
    if pl.held:
        pl.current["cash"] = r(pl.current["cash"] - pl.held)
    if rs.amount > pl.held:
        pl.notes.append(
            f"the reserve {rs.amount:,.2f} is more than the cash on hand: "
            "no cash is spare"
        )
    pl.base = r(sum(pl.current.values()))
    gaps = _Gaps(pl)
    for h in hs:
        if h.kind in SHELTERED and h.cls:
            pl.moves += _swap(gaps, "sheltered", h.account, h.symbol, h.cls, h.value)
    spare = gaps.over("cash")
    for h in hs:
        if h.cls == "cash" and h.kind in portfolio.SPENDABLE and spare > 0:
            moved = _swap(gaps, "cash", h.account, "cash", "cash", min(h.value, spare))
            spare = r(spare - sum(m.amount for m in moved))
            pl.moves += moved
    _sell_lots(lay, today, st, g, gaps, pl)
    _price(lay, year, overrides, pl)
    _wash_notes(lay, today, hs, pl)
    return pl


def _sell_lots(
    lay: Layout,
    today: date,
    st: portfolio.Status,
    g: goals_.Goals,
    gaps: _Gaps,
    pl: Placement,
) -> None:
    lots = withdraw.sellable(st)
    with_lots = {(lot.account, lot.symbol) for lot in lots}
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        cutoff = today - timedelta(days=washsale.WINDOW)
        recent = {
            b.symbol
            for b in washsale.buys(conn)
            if date.fromisoformat(b.date) >= cutoff
        }
    finally:
        conn.close()
    for lot in withdraw.order(lots, []):
        cls = g.classes.get(lot.symbol)
        if cls is None or not gaps.over(cls) or not gaps.under():
            continue
        if lot.gain < 0 and lot.symbol in recent:
            pl.notes.append(
                f"{lot.symbol} {lot.acquired} loss lot skipped: a buy inside the "
                f"last {washsale.WINDOW} days would make it a wash sale"
            )
            continue
        step = "loss" if lot.gain < 0 else "gain"
        key = f"{lot.symbol} {lot.acquired}"
        pl.moves += _swap(
            gaps, step, lot.account, key, cls, lot.value, lot.gain / lot.value, lot.term
        )
    for h in holdings(lay, st, g):
        if (
            h.kind in withdraw.SELLABLE
            and h.cls
            and gaps.over(h.cls)
            and (h.account, h.symbol) not in with_lots
        ):
            pl.notes.append(
                f"{h.symbol} in {h.account}: no cost basis on file, so its sale "
                "is not priced or proposed (drop the cost-basis export)"
            )


def _price(lay: Layout, year: int, ov: Overrides | None, pl: Placement) -> None:
    sales = [m for m in pl.moves if m.step in ("loss", "gain")]
    if not sales:
        return
    ov = ov or Overrides()

    def run(ms: list[Move]) -> float:
        st_ = sum(m.gain for m in ms if m.term == "short")
        lt_ = sum(m.gain for m in ms if m.term != "short")
        return _tax(
            project(
                lay,
                year,
                replace(
                    ov,
                    planned_st_sales=ov.planned_st_sales + st_,
                    planned_lt_sales=ov.planned_lt_sales + lt_,
                ),
            )
        )

    try:
        before = _tax(project(lay, year, ov))
        losses = [m for m in sales if m.step == "loss"]
        after_loss = run(losses) if losses else before
        if losses:
            pl.tax_loss = r(after_loss - before)
        if len(losses) < len(sales):
            pl.tax_gain = r(run(sales) - after_loss)
    except (MissingInputError, OverrideError) as exc:
        pl.notes.append(f"the sales' tax effect is not figured: {exc}")


def _wash_notes(lay: Layout, today: date, hs: list[Holding], pl: Placement) -> None:
    until = (today + timedelta(days=washsale.WINDOW + 1)).isoformat()
    for sym in sorted({m.sell.split()[0] for m in pl.moves if m.step == "loss"}):
        elsewhere = sorted({h.account for h in hs if h.symbol == sym})
        pl.notes.append(
            f"after the {sym} loss sale: no buys of {sym} in any account, IRAs and "
            f"dividend reinvestment included, until {until}"
            + (f" (it is held in {', '.join(elsewhere)})" if elsewhere else "")
        )
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        windows = washsale.open_windows(conn, today)
    finally:
        conn.close()
    for sym, no_buys_until, _loss in windows:
        pl.notes.append(
            f"{sym} was sold at a loss: buying {sym} for any class before "
            f"{no_buys_until} makes it a wash sale"
        )
