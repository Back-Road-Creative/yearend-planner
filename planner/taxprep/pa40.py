"""Pennsylvania Form PA-40 and its Schedule SP, laid onto the draft from the
same engine run as the federal return, for a full-year Pennsylvania resident.

Line numbers, the classes of income, the rate, the Schedule SP eligibility
income tables and the use tax table follow the 2025 PA-40 and its instructions
(PA DOR, 2025_pa-40.pdf and 2025_pa-40in.pdf). PA taxes eight classes of
income at one flat rate and nets nothing across them: a loss on line 4, 5 or
6 is shown but never subtracted, and a capital loss has neither the federal
$3,000 limit nor a carryover. Compensation is the W-2's box 16 (PA employers
report the elective deferrals box 1 leaves out), or box 1 plus those deferrals
where box 16 is missing, plus retirement distributions PA taxes (the engine
excludes those paid at or after age 59 1/2). The engine prices the 529
deduction, tax forgiveness inputs, the Schedule DC credit and the use tax;
this module adds what it does not see: unreimbursed employee business
expenses, other states' bond interest and other deductions you type, the HSA
deduction from the federal return, PA withholding from the W-2 and 1099-R
boxes and the PA estimated payments recorded for the year. Not handled, and
named: part-year and nonresident returns, estate or trust income (line 7),
gambling winnings (line 8), the resident credit (line 22, Schedule G-L),
Schedule OC credits, penalties and interest (line 27, REV-1630) and donations
(lines 32-36).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from planner.engine import tax as engine
from planner.ledger import db

FORM = "PA-40"
SP = "PA Sch SP"
ENGINE = (
    "us_govt_interest",
    "tax_exempt_interest_income",
    "short_term_capital_gains",
    "long_term_capital_gains",
    "rental_income",
    "taxable_pension_income",
    "pa_nontaxable_pension_income",
    "pa_nontaxable_retirement_distributions",
    "pa_total_taxable_income",
    "pa_529_plan_deduction",
    "pa_cdcc",
    "pa_eitc",
    "pa_use_tax",
    "is_qualifying_child_dependent",
)
KEYS = (
    "state_withheld",
    "pa_ube",
    "pa_other_interest",
    "pa_deductions",
    "pa_sp_income",
    "pa_use_tax",
)
# PA-40 IN, Line 1a: PA employers report the elective deferrals in box 16; the
# box 12 codes are the deferrals federal box 1 leaves out.
DEFERRALS = ("12D", "12E", "12F", "12G", "12H", "12S")
CARRY_LINES = ("6", "14")  # Schedule D carryovers, which PA does not allow
TOLERANCE = 1.0

Add = Callable[..., float]


def _given(typed: Mapping[str, Any], key: str) -> tuple[float, str]:
    value = typed.get(key)
    if value is None:
        return 0.0, f"{key}: not given (left out, not zero)"
    return float(value), "typed"


def compensation(
    facts: list[db.FactRow], deferrals: tuple[str, ...] = DEFERRALS
) -> tuple[float, list[str]] | None:
    """Line 1a's W-2 part: each W-2's box 16, else its box 1 plus the elective
    ``deferrals`` (box 12 codes) the state taxes; None with no W-2 on file.
    NJ-1040 line 15 reads the same boxes with its own codes."""
    docs: dict[int, list[db.FactRow]] = {}
    for f in facts:
        if f.form == "W-2":
            docs.setdefault(f.document_id, []).append(f)
    if not docs:
        return None
    total, cited = 0.0, []
    for rows in docs.values():
        box = {f.box: f.value for f in rows}
        issuer = rows[0].issuer
        if "16" in box:
            total += box["16"]
            cited.append(f"W-2 box 16 ({issuer})")
        else:
            total += box.get("1", 0.0) + sum(box.get(b, 0.0) for b in deferrals)
            cited.append(f"W-2 box 1 + box 12 deferrals ({issuer}; no box 16)")
    return round(total, 2), cited


def dollars(x: float) -> float:
    """Half up to the dollar: the PA-40 IN asks for amounts rounded to the
    nearest dollar."""
    return float(Decimal(str(round(x, 6))).quantize(Decimal(1), ROUND_HALF_UP))


def forgiveness(year: int, status: str, income: float, children: int) -> float:
    """Schedule SP's forgiveness decimal (Eligibility Income Tables 1-3): 1.0 up
    to the base plus the per-child amount, 10 points less for each $250 or part
    over; married claimants, even filing separately, use twice the base."""
    p = "gov.states.pa.tax.income.forgiveness."
    base = engine._param(p + "base", year)
    if status in ("JOINT", "SEPARATE"):
        base *= 2
    limit = base + engine._param(p + "dependent_rate", year) * children
    steps = math.ceil(
        max(income - limit, 0.0) / engine._param(p + "rate_increment", year)
    )
    return round(
        min(max(1.0 - engine._param(p + "tax_back", year) * steps, 0.0), 1.0), 2
    )


def lay_lines(
    add: Add,
    notes: list[str],
    v: Mapping[str, float],
    facts: list[db.FactRow],
    paid: list[tuple[str, float, str]],
    year: int,
    agi: float,
    typed: Mapping[str, Any],
    status: str,
) -> None:
    """Add the PA-40 and Schedule SP lines. ``paid`` is (date, amount, origin)
    for each PA estimated payment; ``agi`` (1040 line 11a) is unused, since PA
    starts from its own classes of income; ``status`` is the filing status."""
    # Line 1: compensation, with retirement distributions PA taxes
    w2 = compensation(facts)
    wages = w2[0] if w2 else v["employment_income"]
    retire = max(
        v["taxable_ira_distributions"]
        + v["taxable_pension_income"]
        - v["pa_nontaxable_pension_income"]
        - v["pa_nontaxable_retirement_distributions"],
        0.0,
    )
    l1a = add(
        FORM,
        "1a",
        "Gross compensation",
        wages + retire,
        ", ".join(w2[1] if w2 else ["engine employment_income (no W-2 on file)"])
        + (f"; {retire:,.2f} retirement distributions before 59 1/2" if retire else ""),
    )
    ube, ube_src = _given(typed, "pa_ube")
    l1b = add(FORM, "1b", "Unreimbursed employee business expenses", ube, ube_src)
    l1c = add(FORM, "1c", "Net compensation", max(l1a - l1b, 0.0), "1a - 1b")

    # Lines 2-6: the other classes; a loss is shown and never added
    other, other_src = _given(typed, "pa_other_interest")
    l2 = add(
        FORM,
        "2",
        "Interest income",
        max(v["taxable_interest_income"] - v["us_govt_interest"], 0.0) + other,
        "1040 line 2b less US obligations interest, plus pa_other_interest "
        f"({other_src})",
    )
    dist = round(
        sum(f.value for f in facts if (f.form, f.box) == ("1099-DIV", "2a")), 2
    )
    l3 = add(
        FORM,
        "3",
        "Dividend and capital gains distributions income",
        v["dividend_income"] + dist,
        "1040 line 3b + 1099-DIV box 2a",
    )
    l4 = add(
        FORM,
        "4",
        "Net income or loss from a business",
        v["self_employment_income"],
        "engine self_employment_income (Schedule C)",
    )
    carry = -sum(f.value for f in facts if f.form == "SCH-D" and f.box in CARRY_LINES)
    l5 = add(
        FORM,
        "5",
        "Net gain or loss from the sale of property",
        v["short_term_capital_gains"] + v["long_term_capital_gains"] - dist + carry,
        "Schedule D sales less box 2a distributions"
        + (f", federal carryover {carry:,.2f} added back" if carry else ""),
    )
    l6 = add(
        FORM,
        "6",
        "Net income or loss from rents and royalties",
        v["rental_income"],
        "engine rental_income",
    )
    l9 = add(
        FORM,
        "9",
        "Total PA taxable income",
        sum(max(x, 0.0) for x in (l1c, l2, l3, l4, l5, l6)),
        "the positive amounts of 1c and 2-6 (7 and 8 not drafted)",
    )

    # Lines 10-12: deductions and the tax
    typed_ded, ded_src = _given(typed, "pa_deductions")
    ded = v["health_savings_account_ald"] + v["pa_529_plan_deduction"] + typed_ded
    l10 = add(
        FORM,
        "10",
        "Other deductions",
        min(ded, l9),
        "HSA (federal Form 8889) + engine pa_529_plan_deduction + pa_deductions "
        f"({ded_src}); never more than line 9",
    )
    l11 = add(FORM, "11", "Adjusted PA taxable income", l9 - l10, "9 - 10")
    rate = engine._param("gov.states.pa.tax.income.rate", year)
    l12 = add(
        FORM,
        "12",
        "PA tax liability",
        dollars(l11 * rate),
        f"11 x {rate:.2%}, to the dollar",
    )

    # Lines 13-18: withholding and estimated payments
    withheld = typed.get("state_withheld")
    l13 = add(
        FORM,
        "13",
        "Total PA tax withheld",
        float(withheld or 0.0),
        "W-2 box 17, 1099-R box 14 (Needed panel state_withheld)"
        if withheld is not None
        else "state_withheld: not given (left out, not zero)",
    )
    l15 = add(
        FORM,
        "15",
        f"{year} estimated installment payments",
        sum(a for _, a, _ in paid),
        "; ".join(f"{d} {a:,.2f} ({o})" for d, a, o in paid)
        or "no PA payment recorded (planner paid)",
    )
    l18 = add(
        FORM,
        "18",
        "Total estimated payments and credits",
        l15,
        "15 (14, 16, 17 not drafted)",
    )

    # Schedule SP and lines 19b-21: tax forgiveness
    l21 = 0.0
    if status == "SEPARATE":
        notes.append(
            "PA-40 line 21: a married claimant filing separately completes "
            "Schedule SP with the spouse's income too, which the draft does not "
            "see; tax forgiveness is not drafted"
        )
    else:
        sp_other, sp_src = _given(typed, "pa_sp_income")
        children = int(round(v["is_qualifying_child_dependent"]))
        add(
            SP,
            "2",
            "Dependent children",
            children,
            "engine qualifying child dependents",
            0,
        )
        nontax = v["us_govt_interest"] + max(
            v["tax_exempt_interest_income"] - other, 0.0
        )
        add(
            SP,
            "III-2",
            "Nontaxable interest",
            nontax,
            "US obligations + tax-exempt interest",
        )
        income = add(
            SP,
            "III-11",
            "Total eligibility income",
            l9 + nontax + sp_other,
            f"PA-40 line 9 + III-2 + pa_sp_income ({sp_src})",
        )
        pct = add(
            SP,
            "IV-15",
            "Forgiveness decimal",
            forgiveness(year, status, income, children),
            "Eligibility Income Table by status and children",
            2,
        )
        add(FORM, "19b", "Dependent children", children, "Sch SP Section II line 2", 0)
        add(
            FORM, "20", "Total eligibility income", income, "Sch SP Section III line 11"
        )
        l21 = add(
            FORM,
            "21",
            "Tax forgiveness credit",
            dollars(l12 * pct),
            "Sch SP IV line 16: 12 x IV-15, to the dollar",
        )
    l23 = add(
        FORM,
        "23",
        "Total other credits",
        v["pa_cdcc"],
        "engine pa_cdcc (Schedule DC; Schedule OC credits not drafted)",
    )
    l24 = add(
        FORM,
        "24",
        "Total payments and credits",
        l13 + l18 + l21 + l23,
        "13 + 18 + 21 + 23",
    )
    use = typed.get("pa_use_tax")
    l25 = add(
        FORM,
        "25",
        "Use tax",
        float(use) if use is not None else v["pa_use_tax"],
        "typed" if use is not None else "engine pa_use_tax (PA-40 IN use tax table 1)",
    )

    # The refund or the amount due
    if l24 > l12 + l25:
        l29 = add(FORM, "29", "Overpayment", l24 - l12 - l25, "24 - 12 - 25")
        add(FORM, "30", "Refund", l29, "29 (nothing credited forward on 31)")
    else:
        l26 = add(FORM, "26", "Tax due", l12 + l25 - l24, "12 + 25 - 24")
        add(
            FORM,
            "28",
            "Total payment due",
            l26,
            "26 (penalties and interest not drafted)",
        )

    # Line 9 against the engine's PA taxable income, which starts from federal
    # gross income: put back what the classes treat differently (box 16
    # compensation, the netted gain and distributions, business and rental
    # losses) and the typed interest before comparing.
    want = (
        v["pa_total_taxable_income"]
        + (wages - v["employment_income"])
        - max(v["short_term_capital_gains"] + v["long_term_capital_gains"], 0.0)
        + dist
        + max(l5, 0.0)
        - min(l4, 0.0)
        - min(l6, 0.0)
        + other
        - ube
    )
    got = l9 - min(l1a - l1b, 0.0)
    if v["pa_total_taxable_income"] and abs(got - want) > TOLERANCE:
        notes.append(
            f"CHECK: PA-40 line 9 against the engine's PA taxable income (with "
            f"the class differences put back): {got:,.2f} vs {want:,.2f}"
        )
    if not w2 and v["employment_income"]:
        notes.append(
            "PA-40 line 1a is the wages you typed: PA compensation adds the "
            "elective deferrals (W-2 box 12 D-H, S) federal box 1 leaves out; "
            "drop the W-2s in so their box 16 is read"
        )
    if retire:
        notes.append(
            "PA-40 line 1a counts the federal taxable amount of retirement "
            "distributions before 59 1/2; PA taxes only the part over your "
            "contributions (PA-40 IN, line 1a), so check the 1099-R's cost"
        )
    if v["pa_eitc"]:
        notes.append(
            f"the engine prices a Working Pennsylvanians Tax Credit of "
            f"{v['pa_eitc']:,.2f}; the 2025 PA-40 instructions do not carry it, "
            "so the draft leaves it out"
        )
    if use is None:
        notes.append(
            "PA-40 line 25 is the use tax table's figure for your income; type "
            "pa_use_tax when your records show the use tax actually owed (0 when none)"
        )
    notes.append(
        "the PA-40 draft is for a full-year PA resident: part-year and nonresident "
        "returns, lines 7, 8, 14, 16, 17, 22 (resident credit), 27 (penalties, "
        "REV-1630) and Schedule OC are not drafted; local earned income tax is "
        "filed with the local collector, not on the PA-40"
    )
    notes.append(
        "next year's PA safe harbor (REV-1630 Exception 1) is line 12 less the "
        "line 21 forgiveness; 100% forgiveness this year means no estimated "
        "underpayment penalty next year (Act 85 of 2012)"
    )
