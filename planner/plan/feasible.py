"""One feasibility check for a move or a set of moves (master plan rule 5).

Conversion sizing, the lever menu and whatif ask the same three questions:
is there cash for it by the deadline above the reserve (planner.plan.reserve), do
the shares it gives or the gain it realizes still exist once the other moves
have used theirs, and does it fit the IRA limit traditional and Roth
contributions share (IRC 408A(c)(2)). Shares and the IRA limit are hard: the
set cannot be done. Cash is shown, never refused: money can be moved in
first. Nothing here prices a move; the engine does that.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from planner.engine.tax import r
from planner.ledger import portfolio
from planner.plan.reserve import Reserve


@dataclass(frozen=True)
class Need:
    """What one move takes from outside the return."""

    cash: float = 0.0  # paid in by its deadline: a contribution, a cash gift
    shares: float = 0.0  # market value of long-term gain lots it gives away
    gain: float = 0.0  # long-term gain it realizes (a gain harvest)
    ira: float = 0.0  # its use of the shared IRA contribution limit

    def scaled(self, k: float) -> Need:
        return Need(*(r(v * k) for v in (self.cash, self.shares, self.gain, self.ira)))


@dataclass(frozen=True)
class Funds:
    cash: float | None  # cash accounts now; None: no account is typed cash
    reserve: float  # planner.plan.reserve: the typed cash_target or the rule
    # long-term taxable lots with a gain, (value, gain), most gain per dollar
    # first: the order the gift levers pick them in
    lots: tuple[tuple[float, float], ...] = ()
    ira_left: float | None = None  # the shared IRA limit left this year
    reserve_set: bool = True

    @property
    def spare(self) -> float | None:
        return None if self.cash is None else r(max(self.cash - self.reserve, 0.0))


@dataclass(frozen=True)
class Verdict:
    hard: list[str] = field(default_factory=list)  # cannot be done together
    cash: str | None = None  # takes more cash than is spare

    @property
    def ok(self) -> bool:
        return not self.hard and self.cash is None


def gain_lots(st: portfolio.Status) -> tuple[tuple[float, float], ...]:
    typed = {p.account: p.type for p in st.positions}
    held = [
        lot
        for lot in st.lots
        if typed.get(lot.account) == "taxable"
        and lot.term != "short"
        and lot.gain > 0
        and lot.value > 0
    ]
    held.sort(key=lambda lot: -lot.gain / lot.value)
    return tuple((lot.value, lot.gain) for lot in held)


def funds(st: portfolio.Status, rs: Reserve, ira_left: float | None = None) -> Funds:
    held = [p.value for p in st.positions if p.type == "cash"]
    return Funds(
        r(sum(held)) if held else None,
        rs.amount,
        gain_lots(st),
        ira_left,
        rs.basis != "none",
    )


def with_ira(f: Funds, ira_left: float) -> Funds:
    return replace(f, ira_left=r(max(ira_left, 0.0)))


def unchecked(f: Funds) -> str | None:
    if f.cash is not None:
        return None
    return (
        "no account is typed cash: moves are not checked against cash on hand "
        "(planner account <number> --type cash --balance <amount>)"
    )


def _names(keys: list[str]) -> str:
    return " and ".join(keys) if len(keys) < 3 else ", ".join(keys)


def cash_reason(f: Funds, paid: float, tax: float = 0.0) -> str | None:
    """Why ``paid`` in deposits plus ``tax`` added is more than the cash on hand
    above the reserve; None when it fits or cash is not known."""
    spare, tax = f.spare, r(max(tax, 0.0))
    total = r(paid + tax)
    if spare is None or total <= spare + 0.005:
        return None
    parts = [
        f"{v:,.0f} {what}" for v, what in ((paid, "paid in"), (tax, "of tax")) if v
    ]
    text = (
        f"takes {total:,.0f} of cash by the deadline ({', '.join(parts)}); "
        f"{spare:,.0f} is on hand above the {f.reserve:,.0f} reserve"
    )
    return text + (
        ""
        if f.reserve_set
        else " (no reserve: cash_target not typed and the reserve rule has "
        "nothing entered)"
    )


def check(f: Funds, needs: dict[str, Need], tax: float = 0.0) -> Verdict:
    """Can the moves in ``needs`` be done together, with ``tax`` the change in
    what the year owes (a refund comes later, so a saving frees no cash now)."""
    hard: list[str] = []
    givers = [k for k, n in needs.items() if n.shares > 0]
    given = r(sum(needs[k].shares for k in givers))
    pool = r(sum(v for v, _ in f.lots))
    if givers and given > pool + 0.005:
        hard.append(
            f"{_names(givers)} give {given:,.0f} of shares held over a year; the "
            f"taxable account holds {pool:,.0f}"
        )
    harvesters = [k for k, n in needs.items() if n.gain > 0]
    if harvesters:
        left, gone = given, 0.0
        for value, gain in f.lots:
            use = min(value, left)
            left -= use
            gone += gain * use / value
        remaining = r(max(sum(g for _, g in f.lots) - gone, 0.0))
        want = r(sum(needs[k].gain for k in harvesters))
        if want > remaining + 0.005:
            once = (
                f" once {_names(givers)} give{'s' if len(givers) == 1 else ''} "
                f"{'its' if len(givers) == 1 else 'their'} shares"
                if givers
                else " in the taxable lots"
            )
            hard.append(
                f"{_names(harvesters)} realize{'s' if len(harvesters) == 1 else ''} "
                f"{want:,.0f} of long-term gain; {remaining:,.0f} is left{once}"
            )
    savers = [k for k, n in needs.items() if n.ira > 0]
    used = r(sum(needs[k].ira for k in savers))
    if f.ira_left is not None and savers and used > f.ira_left + 0.005:
        hard.append(
            f"{_names(savers)} share one IRA limit: {used:,.0f} is more than the "
            f"{f.ira_left:,.0f} left this year"
        )
    paid = r(sum(n.cash for n in needs.values()))
    return Verdict(hard, cash_reason(f, paid, tax))
