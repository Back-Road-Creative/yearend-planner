"""The "Before Dec 31" list (master plan unit 4d): each year-end move with its
account, lot, amount, trade-by and settle dates, the tax effect of that move
alone (priced through the engine), the cash after it, and what doing nothing
means.

The moves are the recommended Roth conversion (planner.plan.conversion) and
the taxable sales toward the target mix (planner.plan.placement). A sale counts
in the year of its trade date (Pub. 550; Rev. Rul. 93-84), so it trades by the
last NYSE trading day of the year and settles one trading day later (T+1, SEC
Rule 15c6-1): the proceeds of a December 31 trade arrive in January. A
conversion counts in the year the IRA pays it out (Pub. 590-B), so it is done
by the last business day.

Each move's status runs proposed -> chosen -> done -> reconciled and is kept in
``data/plan/yearend-<year>.yaml``. Choosing a move freezes its numbers; a
done move is reconciled when the ledger shows it (the conversion recorded, the
lot's sale imported).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
from datetime import date, timedelta
from typing import Any

import yaml

from planner.engine.household import MissingInputError
from planner.engine.tax import r
from planner.ledger import db, portfolio
from planner.paths import Layout
from planner.plan import calendar, conversion, placement
from planner.plan.inputs import OverrideError, Overrides
from planner.plan.magi import project

STATUSES = ("proposed", "chosen", "done", "reconciled")


class YearEndError(ValueError):
    """A status change the list refuses."""


@dataclass
class Item:
    id: str
    kind: str  # conversion | sale
    account: str
    lot: str  # "VTSAX 2022-03-01" for a sale; "" for a conversion
    amount: float
    trade_by: str
    settles: str
    tax_effect: float | None  # None: not figured (see the notes)
    gain: float = 0.0
    term: str = ""
    why: str = ""
    status: str = "proposed"
    at: str = ""  # the day the status last changed
    cash_after: float = 0.0

    def nothing(self) -> str:
        if self.kind == "conversion":
            return (
                f"do nothing: no tax now; {self.amount:,.2f} stays in the "
                "traditional IRA, taxed as income when it is withdrawn"
            )
        kind = "loss" if self.gain < 0 else "gain"
        return (
            f"do nothing: the {abs(self.gain):,.2f} {kind} stays unrealized and "
            "the mix stays off by this amount; no tax change"
        )

    def lines(self) -> list[str]:
        tax = "not figured" if self.tax_effect is None else f"{self.tax_effect:+,.2f}"
        what = (
            f"convert {self.amount:,.2f} from {self.account} to the Roth"
            if self.kind == "conversion"
            else f"sell {self.amount:,.2f} of {self.lot} in {self.account} "
            f"({self.term} {'loss' if self.gain < 0 else 'gain'} {self.gain:,.2f})"
        )
        return [
            f"[{self.status}] {self.id}: {what}; trade by {self.trade_by}, "
            f"settles {self.settles}; tax effect {tax}; cash after "
            f"{self.cash_after:,.2f}" + (f" ({self.why})" if self.why else ""),
            f"  {self.nothing()}",
        ]


@dataclass
class YearEnd:
    year: int
    as_of: str
    items: list[Item]
    notes: list[str]

    def lines(self) -> list[str]:
        if not self.items:
            return ["no year-end move proposed"]
        out: list[str] = []
        for it in self.items:
            out += it.lines()
        return out


def _nyse_closed(d: date) -> bool:
    """NYSE closed days in the year-end window (late December to early
    January): weekends, Christmas (Saturday observed Friday, Sunday Monday)
    and New Year's Day (Sunday observed Monday; a Saturday New Year's Day is
    not observed on December 31)."""
    if d.weekday() >= 5:
        return True
    xmas = date(d.year, 12, 25)
    xmas = xmas - timedelta(days=1) if xmas.weekday() == 5 else xmas
    xmas = xmas + timedelta(days=1) if xmas.weekday() == 6 else xmas
    new_year = date(d.year, 1, 1)
    new_year = new_year + timedelta(days=1) if new_year.weekday() == 6 else new_year
    return d in (xmas, new_year)


def last_trading_day(year: int) -> date:
    d = date(year, 12, 31)
    while _nyse_closed(d):
        d -= timedelta(days=1)
    return d


def settle_day(trade: date) -> date:
    d = trade + timedelta(days=1)
    while _nyse_closed(d):
        d += timedelta(days=1)
    return d


def path(lay: Layout, year: int) -> Any:
    return lay.data / "plan" / f"yearend-{year}.yaml"


def _stored(lay: Layout, year: int) -> dict[str, Item]:
    p = path(lay, year)
    if not p.exists():
        return {}
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise YearEndError(f"{p}: expected a mapping")
    names = {f.name for f in fields(Item)}
    out: dict[str, Item] = {}
    for key, row in raw.items():
        if not isinstance(row, dict) or set(row) - names:
            raise YearEndError(f"{p}: {key} is malformed")
        try:
            out[str(key)] = Item(**row)
        except TypeError as exc:
            raise YearEndError(f"{p}: {key} is malformed: {exc}") from exc
        if out[str(key)].status not in STATUSES[1:]:
            raise YearEndError(f"{p}: {key} has status {row.get('status')}")
    return out


def _save(lay: Layout, year: int, items: dict[str, Item]) -> None:
    p = path(lay, year)
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = {k: asdict(v) for k, v in sorted(items.items())}
    p.write_text(yaml.safe_dump(rows, sort_keys=False), encoding="utf-8")


def _proposed(
    lay: Layout,
    year: int,
    today: date,
    ov: Overrides,
    sizing: conversion.Sizing | None,
    pl: placement.Placement | None,
    notes: list[str],
) -> list[Item]:
    trade = last_trading_day(year)
    out: list[Item] = []
    st = portfolio.status(lay, year, today)
    try:
        sz = sizing or conversion.size(
            lay,
            year,
            replace(ov, planned_conversion=0.0, conversion_target="manual"),
            as_of=today,
        )
    except (MissingInputError, OverrideError) as exc:
        notes.append(f"the conversion is not sized: {exc}")
        sz = None
    rec = sz.recommendation if sz else None
    if sz and rec and rec.amount - sz.already > 0.005:
        accounts = sorted(
            {p.account for p in st.positions if p.type in portfolio.CONVERTIBLE}
        )
        by = calendar.shift(date(year, 12, 31), calendar.PRIOR)
        out.append(
            Item(
                "conversion",
                "conversion",
                ", ".join(accounts) or "the traditional IRA",
                "",
                r(rec.amount - sz.already),
                by.isoformat(),
                by.isoformat(),
                r(rec.fed_delta + rec.state_delta - rec.ptc_delta),
                why=f"{rec.name}: {rec.note}",
            )
        )
    pl = pl or placement.plan(lay, year, today, ov)
    notes += [n for n in pl.notes if "wash" in n or "no buys of" in n]
    for m in pl.moves:
        if m.step not in ("loss", "gain"):
            continue
        sym, _, acquired = m.sell.partition(" ")
        out.append(
            Item(
                f"sell:{m.account}:{sym}:{acquired}",
                "sale",
                m.account,
                m.sell,
                m.amount,
                trade.isoformat(),
                settle_day(trade).isoformat(),
                None,
                m.gain,
                m.term,
                f"toward the mix: {m.sell_class} to {m.buy_class}",
            )
        )
    _price(lay, year, ov, [i for i in out if i.kind == "sale"], notes)
    return out


def _price(
    lay: Layout, year: int, ov: Overrides, sales: list[Item], notes: list[str]
) -> None:
    """Each sale's tax effect alone: the projected tax with it less without."""
    if not sales:
        return

    def tax(o: Overrides) -> float:
        res = project(lay, year, o).result
        return r(res.fed_total_tax - res.refundable_credits + res.state_tax)

    try:
        before = tax(ov)
        for it in sales:
            short = it.term == "short"
            it.tax_effect = r(
                tax(
                    replace(
                        ov,
                        planned_st_sales=ov.planned_st_sales
                        + (it.gain if short else 0),
                        planned_lt_sales=ov.planned_lt_sales
                        + (0 if short else it.gain),
                    )
                )
                - before
            )
    except (MissingInputError, OverrideError) as exc:
        notes.append(f"the sales' tax effect is not figured: {exc}")


def _reconciled(lay: Layout, year: int, today: date, it: Item) -> bool:
    """Whether the ledger shows a done move: conversions recorded since it was
    chosen add up to its amount; the lot's sale is imported, dated in the year."""
    if it.kind == "conversion":
        st = portfolio.status(lay, year, today)
        since = sum(c.amount for c in st.conversions if c.date >= it.at)
        return since >= it.amount - 0.5
    sym, _, acquired = it.lot.partition(" ")
    return any(
        row.account == it.account
        and row.symbol == sym
        and (row.acquired or "") == acquired
        and (row.date or "") >= it.at
        for row in _realized(lay, year)
    )


def _realized(lay: Layout, year: int) -> list[db.LedgerRow]:
    """The year's imported sales (the broker's realized gain/loss rows)."""
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        return db.rows_for(conn, year, kind="realized")
    finally:
        conn.close()


def build(
    lay: Layout,
    year: int,
    as_of: date | None = None,
    overrides: Overrides | None = None,
    sizing: conversion.Sizing | None = None,
    pl: placement.Placement | None = None,
) -> YearEnd:
    """The list: chosen and done moves as frozen when chosen, then the moves
    proposed now, each with the cash after it."""
    today = as_of or date.today()
    ov = overrides or Overrides()
    notes: list[str] = []
    stored = _stored(lay, year)
    fresh = _proposed(lay, year, today, ov, sizing, pl, notes)
    changed = False
    for it in stored.values():
        if it.status == "done" and _reconciled(lay, year, today, it):
            it.status, it.at, changed = "reconciled", today.isoformat(), True
        now = next((f for f in fresh if f.id == it.id), None)
        if now and abs(now.amount - it.amount) > 0.005 and it.status == "chosen":
            notes.append(
                f"{it.id}: chosen at {it.amount:,.2f}; the plan now proposes "
                f"{now.amount:,.2f} (planner yearend --undo {it.id} to take it)"
            )
    if changed:
        _save(lay, year, stored)
    items = list(stored.values()) + [f for f in fresh if f.id not in stored]
    items.sort(key=lambda i: (STATUSES.index(i.status), i.trade_by, i.id))
    st = portfolio.status(lay, year, today)
    cash = r(sum(p.value for p in st.positions if p.type == "cash"))
    for it in items:
        if it.status == "reconciled":
            it.cash_after = cash
            continue
        cash = r(cash + (it.amount if it.kind == "sale" else 0) - (it.tax_effect or 0))
        it.cash_after = cash
    if items:
        notes.append(
            "cash after: the cash accounts plus each sale's proceeds, less each "
            "move's tax effect set aside for when it is due"
        )
    late = [
        i for i in items if i.status in STATUSES[:2] and i.trade_by < today.isoformat()
    ]
    if late:
        notes.append(
            f"past the trade-by date for {year}: "
            + ", ".join(i.id for i in late)
            + f" now count in {year + 1}"
        )
    return YearEnd(year, today.isoformat(), items, notes)


def advance(
    lay: Layout,
    year: int,
    move_id: str,
    to: str,
    as_of: date | None = None,
    overrides: Overrides | None = None,
) -> Item:
    """``to`` chosen (from proposed), done (from chosen), or proposed (undo
    one step: done back to chosen, chosen back to proposed)."""
    today = as_of or date.today()
    stored = _stored(lay, year)
    it = stored.get(move_id)
    if to == "chosen":
        if it is not None:
            raise YearEndError(f"{move_id} is already {it.status}")
        notes: list[str] = []
        fresh = _proposed(lay, year, today, overrides or Overrides(), None, None, notes)
        it = next((f for f in fresh if f.id == move_id), None)
        if it is None:
            raise YearEndError(
                f"{move_id} is not proposed (planner yearend lists them)"
            )
        it.status = "chosen"
    elif to == "done":
        if it is None or it.status != "chosen":
            raise YearEndError(f"{move_id} must be chosen before it is done")
        it.status = "done"
    elif to == "proposed":
        if it is None or it.status == "reconciled":
            raise YearEndError(f"{move_id} has nothing to undo")
        if it.status == "done":
            it.status = "chosen"
        else:
            stored.pop(move_id)
            _save(lay, year, stored)
            return replace(it, status="proposed")
    else:
        raise YearEndError(f"unknown status {to}")
    it.at = today.isoformat()
    stored[move_id] = it
    _save(lay, year, stored)
    return it
