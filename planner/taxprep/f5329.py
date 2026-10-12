"""Form 5329, Part I (2025 Form 5329 and its instructions): the additional tax
on early distributions from qualified retirement plans, IRAs included, for
each spouse who took one (unit 3f-5).

Line 1 is the early distributions includible in income: box 2a of each 1099-R
coded 1 (early, no known exception) or S (a SIMPLE IRA in its first 2 years),
an IRA's less the Form 8606 line 10 share that comes back tax free, and an
annuity with box 2a blank its Simplified Method taxable amount. A payer codes
2 (or 3, 4) when it knows an exception applies, as for a governmental 457(b)
distribution not attributable to a rollover from another plan type (2026
Instructions for Forms 1099-R and 5498, code 2): those never reach line 1.
Line 2 is the part an exception covers, typed with its number (01-23; 99 when
more than one applies), and exception 12 is taken here for anyone 59 1/2 or
older all year, since a code 1 or S is then wrong. Line 4 is 10% of line 3,
except 25% of a SIMPLE IRA distribution coded S, to Schedule 2 line 8; on a
joint return each spouse files their own form. Not handled, and named:
Roth IRA distributions (code J: Form 8606 Part III, line 25c), recapture of
in-plan Roth rollovers, modified endowment contracts, prohibited transactions
and qualified disaster recovery distributions (Form 8915-F).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

FORM = "Form 5329"
SPOUSE_FORM = "Form 5329 (spouse)"  # each spouse files their own
EXAMPLE = "05 2500; 19 1000 simple"
MONEY_MAX = 100_000_000
HOME_MAX = 10_000.0  # exception 09: a first home, up to $10,000
NUMBERS = {
    "01": "separation from service in or after the year you reach 55",
    "02": "substantially equal periodic payments",
    "03": "total and permanent disability",
    "04": "death",
    "05": "unreimbursed medical expenses over 7.5% of AGI",
    "06": "a qualified domestic relations order",
    "07": "health insurance premiums while unemployed",
    "08": "qualified higher education expenses",
    "09": "a first home, up to $10,000",
    "10": "an IRS levy",
    "11": "a reservist on active duty",
    "12": "incorrectly coded 1, J or S at 59 1/2 or older",
    "13": "a governmental 457(b) not attributable to a rollover",
    "14": "an election before March 1, 1986",
    "15": "section 404(k) dividends",
    "16": "annuity investment before August 14, 1982",
    "17": "federal phased retirement annuity payments",
    "18": "section 414(w) permissible withdrawals",
    "19": "a birth or adoption, up to $5,000 a child",
    "20": "terminal illness",
    "21": "corrective distributions",
    "22": "a victim of domestic abuse",
    "23": "an emergency personal expense",
}
PLAN_ONLY = frozenset({"01", "06", "13"})  # not for IRAs
IRA_ONLY = frozenset({"07", "08", "09"})
RATE = 0.10
SIMPLE_RATE = 0.25
LABELS = {
    "1": "Early distributions includible in income",
    "2": "Early distributions not subject to the additional tax",
    "3": "Amount subject to the additional tax (line 1 less line 2)",
    "4": "Additional tax (10% of line 3; 25% of a SIMPLE IRA's coded S)",
}


@dataclass
class Early:
    """One 1099-R's early distribution: its payer, "ira" or "plan", whether
    coded S, and the part includible in income."""

    issuer: str
    kind: str
    simple: bool
    amount: float


@dataclass
class Form5329:
    who: str  # "you" or "spouse"
    lines: dict[str, float] = field(default_factory=dict)
    number: str = ""  # the line 2 exception number
    notes: list[str] = field(default_factory=list)

    @property
    def tax(self) -> float:
        return self.lines.get("4", 0.0)


def half_birthday(birth: str, years: int) -> date:
    """The day someone is ``years`` and a half: six calendar months after that
    birthday, the month's last day when it is shorter (born August 31: 70 1/2
    on the last day of February)."""
    b = date.fromisoformat(birth)
    month = b.month + 6
    y, m = b.year + years + (month > 12), (month - 1) % 12 + 1
    last = ((date(y + (m == 12), m % 12 + 1, 1)) - timedelta(days=1)).day
    return date(y, m, min(b.day, last))


def aged(birth: str, year: int) -> bool:
    """59 1/2 or older on January 1, so all year."""
    return half_birthday(birth, 59) <= date(year, 1, 1)


def early_code(code: str) -> bool:
    """A 1099-R box 7 code the additional tax may apply to: 1 or S."""
    return "1" in code or "S" in code


def read(facts: Iterable[Any], kinds: dict[int, str]) -> dict[int, dict[str, Any]]:
    """Each 1099-R's document id -> its payer, owner, kind and boxes."""
    out: dict[int, dict[str, Any]] = {}
    for f in facts:
        if f.form != "1099-R":
            continue
        doc = out.setdefault(
            f.document_id,
            {"issuer": f.issuer, "owner": f.owner, "kind": kinds.get(f.document_id)},
        )
        doc[f.box] = f.text if f.text is not None else f.value
    return out


def parse(key: str, s: str) -> list[dict[str, Any]]:
    """Each typed exception as {number, amount, simple}, or [] for none."""
    if s.strip().lower() == "none":
        return []
    out: list[dict[str, Any]] = []
    for part in (p.strip() for p in s.split(";")):
        tokens = part.split()
        simple = bool(tokens) and tokens[-1].lower() == "simple"
        if simple:
            tokens = tokens[:-1]
        if len(tokens) != 2:
            raise ValueError(
                f"{key}: each exception is its number and the amount it covers, "
                f"then simple for a SIMPLE IRA's coded S (like {EXAMPLE}), or none"
            )
        number = tokens[0].zfill(2)
        if number not in NUMBERS:
            raise ValueError(f"{key}: {tokens[0]!r} is not an exception number (01-23)")
        try:
            amount = float(tokens[1].replace(",", "").lstrip("$"))
        except ValueError:
            raise ValueError(f"{key}: {tokens[1]!r} is not a number") from None
        if not 0 < amount <= MONEY_MAX:
            raise ValueError(f"{key}: {tokens[1]} is out of range")
        if simple and number in PLAN_ONLY:
            raise ValueError(f"{key}: exception {number} is not for IRAs")
        out.append({"number": number, "amount": round(amount, 2), "simple": simple})
    if sum(e["amount"] for e in out if e["number"] == "09") > HOME_MAX:
        raise ValueError(f"{key}: exception 09 is up to {HOME_MAX:,.0f}")
    return out


def figure(
    who: str, items: list[Early], typed: list[dict[str, Any]] | None, aged: bool
) -> Form5329:
    """Part I from the person's early distributions and their typed
    exceptions (None when not answered: none taken). ``aged``: 59 1/2 or
    older all year, so exception 12 covers all of it. Raises ValueError when
    an exception covers more than the distributions it can apply to."""
    f = Form5329(who)
    simple = round(sum(x.amount for x in items if x.simple), 2)
    other = round(sum(x.amount for x in items if not x.simple), 2)
    plan = round(sum(x.amount for x in items if x.kind == "plan"), 2)
    ira = round(sum(x.amount for x in items if x.kind != "plan"), 2)
    if aged:
        typed = [{"number": "12", "amount": other + simple, "simple": False}]
        typed = [e for e in typed if e["amount"]]
        off_simple, off_other = simple, other
    else:
        typed = typed or []
        off_simple = round(sum(e["amount"] for e in typed if e["simple"]), 2)
        off_other = round(sum(e["amount"] for e in typed if not e["simple"]), 2)
        if off_simple > simple:
            raise ValueError(
                f"the exceptions marked simple ({off_simple:,.2f}) are more than "
                f"the SIMPLE IRA distributions coded S ({simple:,.2f})"
            )
        if off_other > other:
            raise ValueError(
                f"the exceptions ({off_other:,.2f}) are more than the early "
                f"distributions not coded S ({other:,.2f})"
            )
        only_plan = sum(e["amount"] for e in typed if e["number"] in PLAN_ONLY)
        if only_plan > plan:
            raise ValueError(
                f"exceptions 01, 06 and 13 ({only_plan:,.2f}) apply only to plans, "
                f"and the early plan distributions are {plan:,.2f}"
            )
        only_ira = sum(e["amount"] for e in typed if e["number"] in IRA_ONLY)
        if only_ira > ira:
            raise ValueError(
                f"exceptions 07, 08 and 09 ({only_ira:,.2f}) apply only to IRAs, "
                f"and the early IRA distributions are {ira:,.2f}"
            )
    put = f.lines.__setitem__
    put("1", round(simple + other, 2))
    line2 = round(off_simple + off_other, 2)
    if line2:
        put("2", line2)
        numbers = {e["number"] for e in typed}
        f.number = numbers.pop() if len(numbers) == 1 else "99"
    put("3", round(f.lines["1"] - line2, 2))
    put("4", round(RATE * (other - off_other) + SIMPLE_RATE * (simple - off_simple), 2))
    return f
