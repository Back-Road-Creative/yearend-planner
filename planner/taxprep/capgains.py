"""Form 8949 and Schedule D from the realized-lot rows, the 1099-B summaries,
1099-DIV capital gain distributions and last year's loss carryovers.

Each closed lot in a taxable account is one 8949 row. A loss lot whose symbol was
bought again within 30 days either side, in any account (an IRA included, Rev.
Rul. 2008-5), has the loss disallowed in proportion to the replacement shares,
code W. Each replacement share absorbs one loss share, matched in date order. A
broker reports wash sales only inside its own account, so its 1099-B figure can
differ; the difference is named. When no lots are on file the 1099-B summaries go
straight onto Schedule D lines 1a and 8a (basis reported, no adjustments).

A lot whose symbol is on the typed ``collectibles`` list (a bullion trust, say)
carries code C. When Schedule D lines 15 and 16 are both gains, line 18 is the
28% Rate Gain Worksheet (collectibles lots in Part II, 1099-DIV box 2d, K-1
collectibles, less the long-term carryover and a short-term loss) and line 19
the Unrecaptured Section 1250 Gain Worksheet from line 10 (1099-DIV box 2b, a
trust's K-1 box 4c and the typed ``unrecaptured_1250``), both per the Schedule D
instructions. The section 1202 exclusion, Forms 4684, 6252, 6781 and 8824
(28% worksheet lines 2-3) and Form 4797 (1250 worksheet lines 1-9) are named,
not drafted.

The stored ``SCH-D`` facts feed the Needed panel's gains once the year has ended;
until then the year-to-date estimate stands in.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date

from planner.ingest.needs import MANUAL_VALUES, load_manual, need_values
from planner.ledger import db, portfolio
from planner.paths import Layout
from planner.plan import washsale
from planner.taxprep import k1

FORM = "SCH-D"
ISSUER = "planner"
NOT_REPORTED = ("trad_ira", "inherited_ira", "roth", "hsa")
LABELS = {
    "1a": "Short-term, 1099-B basis reported, no adjustments",
    "1b": "Short-term, Form 8949 box A",
    "5": "Net short-term gain or loss from Schedules K-1",
    "6": "Short-term loss carryover",
    "7": "Net short-term gain or loss",
    "8a": "Long-term, 1099-B basis reported, no adjustments",
    "8b": "Long-term, Form 8949 box D",
    "12": "Net long-term gain or loss from Schedules K-1",
    "13": "Capital gain distributions",
    "14": "Long-term loss carryover",
    "15": "Net long-term gain or loss",
    "16": "Combined gain or loss",
    "18": "28% rate gain (28% Rate Gain Worksheet line 7)",
    "19": "Unrecaptured section 1250 gain (its worksheet, line 18)",
}
CARRYOVER = {"6": "st_loss_carryover", "14": "lt_loss_carryover"}


@dataclass(frozen=True)
class Lot:
    """One Form 8949 row; money in dollars, ``adjustment`` positive (column g)."""

    box: str  # A (short-term) | D (long-term)
    description: str
    acquired: str
    sold: str
    proceeds: float
    basis: float
    code: str
    adjustment: float
    account: str

    @property
    def gain(self) -> float:
        return round(self.proceeds - self.basis + self.adjustment, 2)


@dataclass
class CapGains:
    year: int
    lots: list[Lot] = field(default_factory=list)
    lines: dict[str, float] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)  # sales left out (not zero)

    def totals(self, box: str) -> tuple[float, float, float, float]:
        """(proceeds, basis, adjustment, gain) over one 8949 box."""
        mine = [lt for lt in self.lots if lt.box == box]
        return (
            round(sum(lt.proceeds for lt in mine), 2),
            round(sum(lt.basis for lt in mine), 2),
            round(sum(lt.adjustment for lt in mine), 2),
            round(sum(lt.gain for lt in mine), 2),
        )


def _term(row: db.LedgerRow) -> str | None:
    if row.term in ("short", "long"):
        return row.term
    if row.acquired and row.date:
        held = date.fromisoformat(row.acquired)
        try:
            anniversary = held.replace(year=held.year + 1)
        except ValueError:  # bought on February 29
            anniversary = date(held.year + 1, 2, 28)
        return "long" if date.fromisoformat(row.date) > anniversary else "short"
    return None


@dataclass
class _Buy:
    symbol: str
    date: str
    account: str
    left: float  # shares not yet matched to a loss
    type: str | None


def _washed(
    row: db.LedgerRow, sold_on: str, loss: float, pool: list[_Buy], notes: list[str]
) -> float:
    """The disallowed part of one loss lot, consuming replacement shares."""
    sold = date.fromisoformat(sold_on)
    shares = row.quantity or 0.0
    own = shares  # the lot's own purchase is not a replacement
    matched = 0.0
    for buy in pool:
        if buy.symbol != row.symbol or buy.left <= 0:
            continue
        if abs((date.fromisoformat(buy.date) - sold).days) > washsale.WINDOW:
            continue
        if buy.account == row.account and buy.date == row.acquired and own > 0:
            take = min(own, buy.left)
            own -= take
            buy.left -= take
            if buy.left <= 0:
                continue
        take = buy.left if shares <= 0 else min(buy.left, shares - matched)
        if take <= 0:
            break
        buy.left -= take
        matched += take
        if buy.type in NOT_REPORTED:
            notes.append(
                f"{row.symbol} sold {row.date}: replacement bought {buy.date} in an "
                f"{buy.type} account; that loss is lost for good (Rev. Rul. 2008-5)"
            )
    if matched <= 0:
        return 0.0
    if shares <= 0:
        notes.append(
            f"{row.symbol} sold {row.date}: no share count, so the whole loss is "
            "treated as washed"
        )
        return loss
    return round(loss * min(1.0, matched / shares), 2)


def build(conn: sqlite3.Connection, lay: Layout, year: int) -> CapGains:
    """Form 8949 rows and the Schedule D lines for one tax year."""
    cg = CapGains(year)
    accounts = portfolio.load_accounts(lay)
    pool = sorted(
        (
            _Buy(
                b.symbol,
                b.date,
                b.account,
                b.quantity,
                accounts.get(b.account, {}).get("type"),
            )
            for b in washsale.buys(conn)
        ),
        key=lambda b: b.date,
    )
    rows = sorted(
        db.rows_for(conn, year, kind="realized"),
        key=lambda r: (r.date or "", r.symbol, r.line),
    )
    typed = load_manual(lay, year)[MANUAL_VALUES]
    collectible = set(typed.get("collectibles") or ())
    unknown_accounts: set[str] = set()
    for row in rows:
        if row.amount_cents is None or row.basis_cents is None or not row.date:
            cg.notes.append(f"{row.file_name} line {row.line}: no proceeds or basis")
            what = "cost basis" if row.basis_cents is None else "proceeds or date"
            cg.unknown.append(f"{what}: {row.file_name} line {row.line}")
            continue
        kind = accounts.get(row.account, {}).get("type")
        if kind in NOT_REPORTED:
            continue
        if kind is None:
            unknown_accounts.add(row.account)
        term = _term(row)
        if term is None:
            cg.notes.append(
                f"{row.file_name} line {row.line}: no term or acquired date; left off"
            )
            continue
        proceeds, basis = (
            db.from_cents(row.amount_cents),
            db.from_cents(row.basis_cents),
        )
        loss = round(basis - proceeds, 2)
        adj = (
            _washed(row, row.date, loss, pool, cg.notes)
            if loss > 0 and row.symbol
            else 0.0
        )
        qty = f"{row.quantity:g} sh " if row.quantity else ""
        cg.lots.append(
            Lot(
                "A" if term == "short" else "D",
                f"{qty}{row.symbol or row.description}".strip(),
                row.acquired or "VARIOUS",
                row.date,
                proceeds,
                basis,
                ("C" if row.symbol in collectible else "") + ("W" if adj else ""),
                adj,
                row.account,
            )
        )
    if unknown_accounts:
        cg.notes.append(
            f"account(s) {', '.join(sorted(unknown_accounts))} have no type; their "
            "sales are reported as taxable (planner enter account:<number> <type>)"
        )
    if collectible and not cg.lots:
        cg.notes.append(
            "collectibles are typed but no realized-lots CSV is on file: the 1099-B "
            "totals do not separate them, so Schedule D line 18 leaves them out"
        )
    facts = db.facts_for(conn, year)
    carried = need_values(conn, lay, year, tuple(CARRYOVER.values()))
    _lines(cg, facts, typed, carried)
    return cg


def _lines(
    cg: CapGains,
    facts: list[db.FactRow],
    typed: dict[str, object],
    carried: dict[str, object],
) -> None:
    b = [f for f in facts if f.form == "1099-B"]

    def summary(box: str) -> float:
        return round(sum(f.value for f in b if f.box == box), 2)

    issuers = ", ".join(sorted({f.issuer for f in b}))
    if cg.lots:
        for line, box in (("1b", "A"), ("8b", "D")):
            if any(lt.box == box for lt in cg.lots):
                cg.lines[line] = cg.totals(box)[3]
                cg.sources[line] = f"Form 8949 box {box}"
        if b:
            lots_p = round(sum(lt.proceeds for lt in cg.lots), 2)
            form_p = round(summary("st_proceeds") + summary("lt_proceeds"), 2)
            if abs(lots_p - form_p) > 1:
                cg.notes.append(
                    f"lot proceeds {lots_p:,.2f} vs 1099-B proceeds {form_p:,.2f} "
                    f"({issuers}); a sale may be missing from the lots CSV"
                )
            ours = round(sum(lt.adjustment for lt in cg.lots), 2)
            theirs = summary("wash_sale")
            if abs(ours - theirs) > 1:
                cg.notes.append(
                    f"wash sales disallowed {ours:,.2f} here vs {theirs:,.2f} on the "
                    "1099-B; the broker sees only its own account"
                )
    elif b:
        for line, pre in (("1a", "st"), ("8a", "lt")):
            cg.lines[line] = round(
                summary(f"{pre}_proceeds") - summary(f"{pre}_basis"), 2
            )
            cg.sources[line] = f"1099-B {pre}_proceeds - {pre}_basis ({issuers})"
        if summary("wash_sale"):
            cg.notes.append(
                "the 1099-B shows wash sales; import the realized-lots CSV so Form "
                "8949 carries them (code W)"
            )
    dist = [f for f in facts if f.form == "1099-DIV" and f.box == "2a"]
    if dist:
        cg.lines["13"] = round(sum(f.value for f in dist), 2)
        cg.sources["13"] = "1099-DIV box 2a (" + ", ".join(f.issuer for f in dist) + ")"
    k1s = typed.get("k1s")
    k1p = k1.portfolio(k1s if isinstance(k1s, list) else [])
    for line, word in (("5", "stgain"), ("12", "ltgain")):
        got = [i for i in k1p if i[0] == word]
        if got:
            cg.lines[line] = round(sum(a for _, _, a, _ in got), 2)
            cg.sources[line] = "; ".join(f"{p} {s}" for _, p, _, s in got)
    for line, key in CARRYOVER.items():
        value = carried.get(key)
        if value is not None and (float(str(value)) or key in typed):
            cg.lines[line] = -abs(float(str(value)))
            how = "typed" if key in typed else "carried by planner rollover"
            cg.sources[line] = f"{key} ({how})"
    if not cg.lines:
        return
    get = cg.lines.get
    cg.lines["7"] = round(sum(get(k, 0.0) for k in ("1a", "1b", "5", "6")), 2)
    cg.lines["15"] = round(sum(get(k, 0.0) for k in ("8a", "8b", "12", "13", "14")), 2)
    cg.lines["16"] = round(cg.lines["7"] + cg.lines["15"], 2)
    cg.sources.update(
        {"7": "1a + 1b + 5 + 6", "15": "8a + 8b + 12 + 13 + 14", "16": "7 + 15"}
    )
    _rate_gains(cg, facts, typed, k1p)
    cg.lines = {k: cg.lines[k] for k in LABELS if k in cg.lines}


def _rate_gains(
    cg: CapGains,
    facts: list[db.FactRow],
    typed: dict[str, object],
    k1p: list[tuple[str, str, float, str]],
) -> None:
    """Schedule D lines 18 and 19, filled only when lines 15 and 16 are both
    gains (line 17 "Yes"): the 28% Rate Gain Worksheet and the Unrecaptured
    Section 1250 Gain Worksheet in the Schedule D instructions."""

    def reported(box: str, word: str) -> tuple[float, list[str]]:
        div = [f for f in facts if f.form == "1099-DIV" and f.box == box and f.value]
        got = [i for i in k1p if i[0] == word]
        amount = sum(f.value for f in div) + sum(a for _, _, a, _ in got)
        where = [f"1099-DIV box {box} ({f.issuer})" for f in div]
        return round(amount, 2), where + [f"{p} {s}" for _, p, _, s in got]

    get = cg.lines.get
    w1 = round(sum(lt.gain for lt in cg.lots if lt.box == "D" and "C" in lt.code), 2)
    w4, w4_from = reported("2d", "collectibles")
    w5 = get("14", 0.0)  # the long-term carryover, already negative
    w6 = min(get("7", 0.0), 0.0)
    w7 = max(round(w1 + w4 + w5 + w6, 2), 0.0)
    w11, w11_from = reported("2b", "unrecaptured1250")
    w10_12 = float(str(typed.get("unrecaptured_1250") or 0))
    w13 = round(w10_12 + w11, 2)
    w14 = round(w1 + w4, 2)  # 28% worksheet lines 1-4 (2 and 3 not drafted)
    w17 = -min(round(w14 + w6 + w5, 2), 0.0)
    w18 = max(round(w13 - w17, 2), 0.0)
    if any(f.form == "1099-DIV" and f.box == "2c" and f.value for f in facts):
        cg.notes.append(
            "1099-DIV box 2c (section 1202 gain): its exclusion is not drafted, so "
            "the 28% Rate Gain Worksheet line 2 is left out"
        )
    if get("15", 0.0) <= 0 or get("16", 0.0) <= 0:
        return
    if w7 > 0:
        cg.lines["18"] = w7
        parts = (["Form 8949 Part II code C"] if w1 else []) + w4_from
        cg.sources["18"] = (
            "28% Rate Gain Worksheet: " + "; ".join(parts) + ", less lines 14 and "
            "a line 7 loss"
        )
    if w18 > 0:
        cg.lines["19"] = w18
        parts = (["unrecaptured_1250 (typed)"] if w10_12 else []) + w11_from
        cg.sources["19"] = "Unrecaptured Section 1250 Gain Worksheet: " + "; ".join(
            parts
        )
    if w7 > 0 or w18 > 0:
        cg.notes.append(
            "Schedule D lines 18 and 19 leave out the section 1202 exclusion, Forms "
            "4684, 6252, 6781 and 8824 (28% Rate Gain Worksheet lines 2-3) and Form "
            "4797 (Unrecaptured Section 1250 Gain Worksheet lines 1-9)"
        )


def carryover(
    l7: float, l15: float, l16: float, l21: float, taxable_income: float
) -> tuple[float, float]:
    """(short-term, long-term) loss carried to next year: the Capital Loss
    Carryover Worksheet in the Schedule D instructions."""
    if l16 >= 0:
        return 0.0, 0.0
    w2 = abs(l21)
    w4 = min(w2, max(taxable_income + w2, 0.0))
    w5 = -l7 if l7 < 0 else 0.0
    st = max(w5 - (w4 + max(l15, 0.0)), 0.0) if l7 < 0 else 0.0
    lt = max(-l15 - (max(l7, 0.0) + max(w4 - w5, 0.0)), 0.0) if l15 < 0 else 0.0
    return round(st, 2), round(lt, 2)


def store(
    conn: sqlite3.Connection, lay: Layout, year: int, today: date | None = None
) -> CapGains:
    """Rebuild the year; once it has ended, replace its SCH-D facts when they
    changed (an open year keeps the year-to-date estimate)."""
    cg = build(conn, lay, year)
    if (today or date.today()) <= date(year, 12, 31):
        cg.notes.append(
            f"{year} is still open: the Needed panel keeps the year-to-date gains"
        )
        return cg
    db.replace_derived(conn, FORM, year, cg.lines, LABELS, f"Schedule D {year}")
    return cg


def render(cg: CapGains) -> str:
    out = [f"Form 8949 and Schedule D for {cg.year}"]
    if not cg.lots and not cg.lines:
        out.append("no sales, capital gain distributions or carryovers on file")
    for box in ("A", "D"):
        mine = [lt for lt in cg.lots if lt.box == box]
        if not mine:
            continue
        term = "short-term" if box == "A" else "long-term"
        out.append(f"Form 8949 box {box} ({term}, basis reported to the IRS)")
        for lt in mine:
            out.append(
                f"  {lt.description:24.24} {lt.acquired:10} {lt.sold:10} "
                f"{lt.proceeds:>12,.2f} {lt.basis:>12,.2f} {lt.code:2} "
                f"{lt.adjustment:>10,.2f} {lt.gain:>12,.2f}"
            )
        p, c, a, g = cg.totals(box)
        out.append(
            f"  {'totals':56} {p:>12,.2f} {c:>12,.2f}    {a:>10,.2f} {g:>12,.2f}"
        )
    if cg.lines:
        out.append("Schedule D")
    for line, value in cg.lines.items():
        out.append(
            f"  line {line:4} {LABELS[line]:52} {value:>12,.2f}  {cg.sources[line]}"
        )
    if cg.lots:
        out.append(
            "note: box A / D assumes the broker reported basis to the IRS; a "
            "noncovered lot belongs in box B / E"
        )
    out.extend(f"note: {n}" for n in cg.notes)
    return "\n".join(out) + "\n"
