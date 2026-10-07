"""What a year's return carries into the next (master plan unit 6c).

Each carry names its amount, the line it comes from on this year's draft and
the answer next year's draft reads it from:

- the capital loss (Capital Loss Carryover Worksheet): ``planner rollover``
  carries it on its own;
- the basis in traditional IRAs (Form 8606 line 14): next year's
  ``ira_basis`` (or ``spouse_ira_basis``) word ``basis``;
- a passive loss not allowed (Form 8582 Part VII column (c)): next year's
  rental or farm entry word ``prior``;
- the cost still to recover on an annuity (Simplified Method Worksheet line
  11; line 10 is next year's ``recovered``);
- foreign tax over the credit limit (Form 1116 line 14 less line 24): back one
  year or forward ten, next year's ``foreign_tax_carryover``. It is a carry by
  hand: Schedule B (Form 1116), which ages each year's excess and drops what is
  over ten years old, is not drafted;
- a home rental's expenses over the rents (Pub. 527 Worksheet 5-1 lines 7a,
  7b): next year's rental words ``carryover`` and ``carrydep``;
- farm conservation expenses over 25% of farm income (Schedule F line 12):
  next year's farm word ``conservationcarry``.

The tax pack writes them to ``carryforward.csv`` and the rollover checklist
lists each one with the answer to type. Both also keep the year's basis
history, ``data/private/basis/<year>.yaml``: the carries, each open lot's
basis and each Roth conversion's basis, as the ledger held them. Next year's
draft reads it and flags a typed figure that does not match what was carried.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from planner.ledger import db, portfolio
from planner.paths import Layout

if TYPE_CHECKING:
    from planner.taxprep.draft import Draft

CENT = 0.005


@dataclass(frozen=True)
class Carry:
    item: str
    amount: float
    line: str  # where it is on this year's return
    next: str  # the answer next year's draft reads it from
    source: str
    hand: bool = False  # read next year but not computed here: carry by hand


def from_lines(d: Draft) -> list[Carry]:
    """The carries the draft lays out as lines."""
    from planner.taxprep import annuity, f1116, f8582, f8606

    out: list[Carry] = []
    for ln in d.lines:
        if ln.form == "Carryover":
            out.append(
                Carry(
                    ln.label,
                    ln.value,
                    f"Carryover line {ln.line}",
                    "carried by planner rollover",
                    ln.source,
                )
            )
        elif ln.form in (f8606.FORM, f8606.SPOUSE_FORM) and ln.line == "14":
            key = "ira_basis" if ln.form == f8606.FORM else "spouse_ira_basis"
            out.append(
                Carry(
                    "Basis in traditional IRAs",
                    ln.value,
                    f"{ln.form} line 14",
                    f"{key} word basis",
                    ln.source,
                )
            )
        elif ln.form == f8582.FORM and re.fullmatch(r"VII-\d+\(c\)", ln.line):
            if ln.value > CENT:
                out.append(
                    Carry(
                        ln.label,
                        ln.value,
                        f"{ln.form} {ln.line}",
                        "the rental's or farm's word prior",
                        ln.source,
                    )
                )
        elif ln.form.startswith(annuity.FORM) and ln.line == "11" and ln.value > CENT:
            out.append(
                Carry(
                    f"{ln.label} ({ln.form})",
                    ln.value,
                    f"{ln.form} line 11",
                    "annuities word recovered (line 10)",
                    ln.source,
                )
            )
    over = (d.get(f1116.FORM, "14") or 0.0) - (d.get(f1116.FORM, "24") or 0.0)
    if over > CENT:
        out.append(
            Carry(
                "Foreign tax over the credit limit",
                round(over, 2),
                "Form 1116 line 14 less line 24",
                "foreign_tax_carryover, after Schedule B (Form 1116) drops any "
                "year over ten years old (or carry back 1 year)",
                "Schedule B (Form 1116), not drafted",
                hand=True,
            )
        )
    return out


def history_path(lay: Layout, year: int) -> Path:
    return lay.data / "private" / "basis" / f"{year}.yaml"


def record(lay: Layout, year: int, carried: list[Carry], today: date) -> Path:
    """Write ``year``'s basis history; a later run replaces it."""
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        lots = portfolio.lots(conn)
        convs = db.conversions(conn)
    finally:
        conn.close()
    data: dict[str, Any] = {
        "year": year,
        "recorded": today.isoformat(),
        "carries": [asdict(c) for c in carried],
        "lots": [
            {
                "account": lt.account,
                "symbol": lt.symbol,
                "acquired": lt.acquired,
                "quantity": lt.quantity,
                "basis": round(lt.basis, 2),
            }
            for lt in lots
        ],
        "roth_conversions": [
            {
                "date": c.date,
                "account": c.source_account,
                "amount": c.amount,
                "basis": round(c.amount - c.taxable, 2),
                "penalty_free": c.accessible_date,
            }
            for c in convs
        ],
    }
    p = history_path(lay, year)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return p


def load(lay: Layout, year: int) -> list[Carry]:
    """The carries ``year``'s basis history kept; empty when none was kept."""
    p = history_path(lay, year)
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return [Carry(**c) for c in data.get("carries") or []]


def check(d: Draft, prior: list[Carry], foreign_typed: float | None) -> list[str]:
    """CHECK notes where this year's draft does not start from last year's
    carries: the IRA basis on Form 8606 line 2 and the foreign tax carryover."""
    from planner.taxprep import f8606

    out: list[str] = []
    last = d.year - 1
    for c in prior:
        if c.line in (f"{f8606.FORM} line 14", f"{f8606.SPOUSE_FORM} line 14"):
            form = c.line.removesuffix(" line 14")
            got = d.get(form, "2")
            if got is None and c.amount > CENT:
                out.append(
                    f"CHECK: {last} {c.line} carried {c.amount:,.2f} of IRA basis; "
                    f"{d.year} has no {form}: type {c.next} {c.amount:g}"
                )
            elif got is not None and abs(got - c.amount) > CENT:
                out.append(
                    f"CHECK: {form} line 2 is {got:,.2f} but {last} line 14 "
                    f"carried {c.amount:,.2f}"
                )
        elif c.item == "Foreign tax over the credit limit":
            typed = foreign_typed or 0.0
            if abs(typed - c.amount) > CENT:
                out.append(
                    f"CHECK: foreign_tax_carryover is {typed:,.2f} but {last} "
                    f"carried {c.amount:,.2f} over the Form 1116 limit; they "
                    "differ rightly only when Schedule B (Form 1116) dropped an "
                    "expired year or some was carried back"
                )
    return out
